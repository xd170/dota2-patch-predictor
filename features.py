import re
from bisect import bisect_right
from datetime import datetime, timezone
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from config import TIMEZONE_NAME

MINSK_TZ = ZoneInfo(TIMEZONE_NAME)
MAJOR_PATCH_PATTERN = re.compile(r"^\d+\.\d+$")


def parse_iso_utc(value: str) -> datetime:
    """Преобразует ISO-строку времени в UTC datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_major_patch(patch_name: str) -> bool:
    """Возвращает True только для крупных номерных патчей (например, 7.41, 7.42)."""
    return bool(MAJOR_PATCH_PATTERN.match(str(patch_name).strip()))


def normalize_events(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Нормализует, сортирует по возрастанию времени и убирает дубликаты событий."""
    seen_keys: set[tuple[str, Any, Any, str]] = set()
    cleaned: list[dict[str, Any]] = []

    for item in raw_events:
        branch = str(item.get("branch", "")).lower().strip()
        seen_at = item.get("seen_at")
        if branch not in ("staging", "public", "experimental") or not seen_at:
            continue

        dt = parse_iso_utc(str(seen_at))
        key = (branch, item.get("from"), item.get("to"), dt.isoformat())
        if key in seen_keys:
            continue
        seen_keys.add(key)

        delta_val = item.get("delta")
        try:
            delta_int = int(delta_val) if delta_val is not None else 0
        except (TypeError, ValueError):
            delta_int = 0

        cleaned.append(
            {
                "branch": branch,
                "from": item.get("from"),
                "to": item.get("to"),
                "delta": delta_int,
                "dt": dt,
                "ts": dt.timestamp(),
                "source": item.get("source"),
            }
        )

    cleaned.sort(key=lambda e: e["ts"])
    return cleaned


def normalize_patches(raw_patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Нормализует список исторических патчей и классифицирует крупные/буквенные."""
    patches: list[dict[str, Any]] = []
    for item in raw_patches:
        name = str(item.get("patch", "")).strip()
        announced_raw = item.get("announced_at")
        build_seen_raw = item.get("build_seen_at")
        if not name or not announced_raw:
            continue

        announced_dt = parse_iso_utc(str(announced_raw))
        build_seen_dt = (
            parse_iso_utc(str(build_seen_raw)) if build_seen_raw else announced_dt
        )
        release_dt = min(announced_dt, build_seen_dt)

        patches.append(
            {
                "patch": name,
                "is_major": is_major_patch(name),
                "mapping": item.get("mapping"),
                "build": item.get("build"),
                "announced_dt": announced_dt,
                "build_seen_dt": build_seen_dt,
                "release_dt": release_dt,
                "release_ts": release_dt.timestamp(),
            }
        )

    patches.sort(key=lambda p: p["release_ts"])
    return patches


class EventIndex:
    """Быстрый индекс событий по веткам для расчёта признаков без утечки будущего."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.by_branch: dict[str, list[dict[str, Any]]] = {
            "staging": [],
            "public": [],
            "experimental": [],
        }
        for ev in events:
            self.by_branch[ev["branch"]].append(ev)

        self.ts_by_branch: dict[str, list[float]] = {
            branch: [ev["ts"] for ev in ev_list]
            for branch, ev_list in self.by_branch.items()
        }

    def slice_until(self, branch: str, end_ts: float) -> list[dict[str, Any]]:
        ts_list = self.ts_by_branch[branch]
        idx = bisect_right(ts_list, end_ts)
        return self.by_branch[branch][:idx]

    def window_stats(
        self, branch: str, end_ts: float, seconds: float
    ) -> tuple[int, int]:
        ts_list = self.ts_by_branch[branch]
        end_idx = bisect_right(ts_list, end_ts)
        start_idx = bisect_right(ts_list, end_ts - seconds - 1e-6)
        if start_idx >= end_idx:
            return 0, 0
        matched = self.by_branch[branch][start_idx:end_idx]
        return len(matched), sum(ev["delta"] for ev in matched)


def _minutes_to_target_hour(minsk_dt: datetime, target_hour: float = 2.75) -> float:
    """
    Возвращает количество минут до ближайшего наступления target_hour (02:45 по Минску).
    Если сейчас окно 00:00–04:30, возвращает 0.0 (мы уже внутри окна релиза).
    """
    hour_float = minsk_dt.hour + minsk_dt.minute / 60.0 + minsk_dt.second / 3600.0
    if 0.0 <= hour_float <= 4.5:
        return 0.0
    if hour_float < target_hour:
        return (target_hour - hour_float) * 60.0
    return (24.0 - hour_float + target_hour) * 60.0


def extract_features(
    now_utc: datetime,
    index: EventIndex,
    last_major_dt: datetime | None = None,
) -> dict[str, Any]:
    """
    Рассчитывает вектор признаков строго на момент now_utc.
    Не использует ни одной записи позже now_utc.
    """
    now_ts = now_utc.timestamp()

    # --- Staging окна ---
    st_15m, _ = index.window_stats("staging", now_ts, 15 * 60)
    st_30m, _ = index.window_stats("staging", now_ts, 30 * 60)
    st_1h, _ = index.window_stats("staging", now_ts, 3600)
    st_2h, st_d2h = index.window_stats("staging", now_ts, 2 * 3600)
    st_3h, _ = index.window_stats("staging", now_ts, 3 * 3600)
    st_6h, st_d6h = index.window_stats("staging", now_ts, 6 * 3600)
    st_12h, _ = index.window_stats("staging", now_ts, 12 * 3600)
    st_24h, st_d24h = index.window_stats("staging", now_ts, 24 * 3600)
    st_72h, st_d72h = index.window_stats("staging", now_ts, 72 * 3600)
    st_7d, st_d7d = index.window_stats("staging", now_ts, 7 * 86400)

    # --- Временные интервалы и ускорение Staging ---
    st_events = index.slice_until("staging", now_ts)
    st_mins_since_last = (
        (now_ts - st_events[-1]["ts"]) / 60.0 if st_events else 99999.0
    )
    st_last_gap_min = (
        (st_events[-1]["ts"] - st_events[-2]["ts"]) / 60.0
        if len(st_events) >= 2
        else 99999.0
    )
    st_prev_gap_min = (
        (st_events[-2]["ts"] - st_events[-3]["ts"]) / 60.0
        if len(st_events) >= 3
        else 99999.0
    )

    st_72h_events = [ev for ev in st_events if ev["ts"] >= now_ts - 72 * 3600]
    gaps_72h = [
        (st_72h_events[i]["ts"] - st_72h_events[i - 1]["ts"]) / 60.0
        for i in range(1, len(st_72h_events))
    ]
    st_median_gap_72h = median(gaps_72h) if gaps_72h else 99999.0

    st_accelerating = (
        len(st_events) >= 3
        and st_last_gap_min <= 180.0
        and st_last_gap_min < st_prev_gap_min * 0.85
        and st_mins_since_last <= 360.0
    )

    episode_count = 0
    episode_start_ts = now_ts
    if st_events and (now_ts - st_events[-1]["ts"]) <= 36 * 3600:
        episode_count = 1
        episode_start_ts = st_events[-1]["ts"]
        for i in range(len(st_events) - 2, -1, -1):
            if (st_events[i + 1]["ts"] - st_events[i]["ts"]) <= 36 * 3600:
                episode_count += 1
                episode_start_ts = st_events[i]["ts"]
            else:
                break
    st_episode_hours = (now_ts - episode_start_ts) / 3600.0 if episode_count else 0.0

    # --- Public и Experimental ---
    pu_2h, _ = index.window_stats("public", now_ts, 2 * 3600)
    pu_6h, _ = index.window_stats("public", now_ts, 6 * 3600)
    pu_24h, _ = index.window_stats("public", now_ts, 24 * 3600)
    pu_events = index.slice_until("public", now_ts)
    pu_mins_since_last = (now_ts - pu_events[-1]["ts"]) / 60.0 if pu_events else 99999.0

    ex_6h, _ = index.window_stats("experimental", now_ts, 6 * 3600)
    ex_24h, _ = index.window_stats("experimental", now_ts, 24 * 3600)
    ex_72h, _ = index.window_stats("experimental", now_ts, 72 * 3600)
    ex_events = index.slice_until("experimental", now_ts)
    ex_mins_since_last = (now_ts - ex_events[-1]["ts"]) / 60.0 if ex_events else 99999.0

    # --- Время суток (Europe/Minsk) и окно релиза Valve ---
    minsk_dt = now_utc.astimezone(MINSK_TZ)
    minsk_hour = minsk_dt.hour + minsk_dt.minute / 60.0
    in_release_window = 0.0 <= minsk_hour <= 4.5
    in_pre_release_evening = minsk_hour >= 19.5 or minsk_hour <= 4.5
    mins_to_valve_window = _minutes_to_target_hour(minsk_dt, target_hour=2.75)

    days_since_major = (
        (now_utc - last_major_dt).total_seconds() / 86400.0
        if last_major_dt is not None
        else None
    )

    return {
        "now_utc": now_utc.isoformat(),
        "now_minsk": minsk_dt.strftime("%Y-%m-%d %H:%M"),
        "minsk_hour": round(minsk_hour, 2),
        "in_release_window": in_release_window,
        "in_pre_release_evening": in_pre_release_evening,
        "mins_to_valve_window": round(mins_to_valve_window, 1),
        "days_since_major": round(days_since_major, 1) if days_since_major is not None else None,
        "st_15m": st_15m,
        "st_30m": st_30m,
        "st_1h": st_1h,
        "st_2h": st_2h,
        "st_3h": st_3h,
        "st_6h": st_6h,
        "st_12h": st_12h,
        "st_24h": st_24h,
        "st_72h": st_72h,
        "st_7d": st_7d,
        "st_d2h": st_d2h,
        "st_d6h": st_d6h,
        "st_d24h": st_d24h,
        "st_d72h": st_d72h,
        "st_d7d": st_d7d,
        "st_mins_since_last": round(st_mins_since_last, 1),
        "st_last_gap_min": round(st_last_gap_min, 1),
        "st_prev_gap_min": round(st_prev_gap_min, 1),
        "st_median_gap_72h": round(st_median_gap_72h, 1),
        "st_accelerating": st_accelerating,
        "st_episode_count": episode_count,
        "st_episode_hours": round(st_episode_hours, 1),
        "pu_2h": pu_2h,
        "pu_6h": pu_6h,
        "pu_24h": pu_24h,
        "pu_mins_since_last": round(pu_mins_since_last, 1),
        "ex_6h": ex_6h,
        "ex_24h": ex_24h,
        "ex_72h": ex_72h,
        "ex_mins_since_last": round(ex_mins_since_last, 1),
    }
