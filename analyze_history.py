import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from collector import fetch_json
from config import HISTORY_URL, LATEST_URL, TIMEZONE_NAME

MINSK_TZ = ZoneInfo(TIMEZONE_NAME)
MAJOR_PATCH_PATTERN = re.compile(r"^\d+\.\d+$")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def fmt_minsk(dt: datetime) -> str:
    return dt.astimezone(MINSK_TZ).strftime("%Y-%m-%d %H:%M:%S")


def is_major_patch(name: str) -> bool:
    return bool(MAJOR_PATCH_PATTERN.match(name.strip()))


def prepare_events(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = []
    for item in raw_events:
        seen_at = item.get("seen_at")
        branch = str(item.get("branch", "")).lower()
        if not seen_at or branch not in ("staging", "public", "experimental"):
            continue
        parsed.append(
            {
                "branch": branch,
                "from": item.get("from"),
                "to": item.get("to"),
                "delta": item.get("delta") or 0,
                "dt": parse_dt(seen_at),
                "source": item.get("source"),
            }
        )
    parsed.sort(key=lambda x: x["dt"])
    return parsed


def prepare_patches(raw_patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in raw_patches:
        name = str(item.get("patch", "")).strip()
        announced_raw = item.get("announced_at")
        build_seen_raw = item.get("build_seen_at")
        if not name or not announced_raw:
            continue
        announced_dt = parse_dt(announced_raw)
        build_seen_dt = parse_dt(build_seen_raw) if build_seen_raw else announced_dt
        release_dt = min(announced_dt, build_seen_dt)
        result.append(
            {
                "patch": name,
                "is_major": is_major_patch(name),
                "mapping": item.get("mapping"),
                "build": item.get("build"),
                "announced_dt": announced_dt,
                "build_seen_dt": build_seen_dt,
                "release_dt": release_dt,
            }
        )
    result.sort(key=lambda x: x["release_dt"])
    return result


def count_in_window(
    events: list[dict[str, Any]], end_dt: datetime, hours: float
) -> tuple[int, int]:
    start_dt = end_dt - timedelta(hours=hours)
    matched = [e for e in events if start_dt <= e["dt"] <= end_dt]
    return len(matched), sum(int(e["delta"] or 0) for e in matched)


def analyze_major_patches(
    events_by_branch: dict[str, list[dict[str, Any]]],
    patches: list[dict[str, Any]],
) -> None:
    print("\n" + "=" * 24 + " 7 КРУПНЫХ ПАТЧЕЙ " + "=" * 24)
    staging = events_by_branch["staging"]
    public = events_by_branch["public"]
    experimental = events_by_branch["experimental"]

    majors = [p for p in patches if p["is_major"]]
    for p in majors:
        rel = p["release_dt"]
        print(
            f"\n>>> ПАТЧ {p['patch']} | Релиз(Минск): {fmt_minsk(rel)} "
            f"| mapping={p['mapping']} | build={p['build']}"
        )

        # Строго до момента релиза (с допуском 1 минута назад, чтобы не захватить сам релиз)
        cutoff = rel - timedelta(seconds=1)
        st_before = [e for e in staging if e["dt"] <= cutoff]
        pu_before = [e for e in public if e["dt"] <= cutoff]
        ex_before = [e for e in experimental if e["dt"] <= cutoff]

        windows = [0.5, 1, 2, 3, 6, 12, 24, 72]
        st_summary = []
        for h in windows:
            cnt, d_sum = count_in_window(st_before, cutoff, h)
            label = f"{int(h * 60)}m" if h < 1 else f"{int(h)}h"
            st_summary.append(f"{label}:{cnt}(+{d_sum})")
        print("  Staging окна [смен(+билд)]: " + " | ".join(st_summary))

        pu_24, _ = count_in_window(pu_before, cutoff, 24)
        pu_6, _ = count_in_window(pu_before, cutoff, 6)
        ex_24, _ = count_in_window(ex_before, cutoff, 24)
        ex_6, _ = count_in_window(ex_before, cutoff, 6)
        print(f"  Public 24h/6h: {pu_24}/{pu_6} | Experimental 24h/6h: {ex_24}/{ex_6}")

        recent_st = [e for e in st_before if e["dt"] >= rel - timedelta(hours=48)]
        if not recent_st:
            last_st = st_before[-1] if st_before else None
            if last_st:
                gap_h = (rel - last_st["dt"]).total_seconds() / 3600
                print(f"  За 48ч смен Staging не было. Последняя за {gap_h:.1f}ч до патча.")
            else:
                print("  Событий Staging до патча не найдено.")
        else:
            print("  Лента Staging за последние 48ч до релиза:")
            prev_dt = None
            for e in recent_st:
                mins_to_rel = (rel - e["dt"]).total_seconds() / 60
                gap_str = (
                    f"+{(e['dt'] - prev_dt).total_seconds() / 60:.0f}м от пред."
                    if prev_dt
                    else "старт окна"
                )
                print(
                    f"    {fmt_minsk(e['dt'])} | {e['from']}->{e['to']} (+{e['delta']}) "
                    f"| {gap_str} | до патча: {mins_to_rel:.0f} мин"
                )
                prev_dt = e["dt"]


def evaluate_staging_signals(
    staging: list[dict[str, Any]],
    patches: list[dict[str, Any]],
) -> None:
    print("\n" + "=" * 20 + " ЧЕСТНАЯ ПРОВЕРКА СИГНАЛОВ (ЗА 3 ГОДА) " + "=" * 20)
    majors = [p for p in patches if p["is_major"]]
    letters = [p for p in patches if not p["is_major"]]

    # Для каждого события Staging считаем признаки строго на момент события (без заглядывания в будущее)
    enriched = []
    for idx, ev in enumerate(staging):
        t = ev["dt"]
        c15m, _ = count_in_window(staging[: idx + 1], t, 0.25)
        c30m, _ = count_in_window(staging[: idx + 1], t, 0.5)
        c1h, _ = count_in_window(staging[: idx + 1], t, 1)
        c2h, _ = count_in_window(staging[: idx + 1], t, 2)
        c6h, _ = count_in_window(staging[: idx + 1], t, 6)
        c24h, _ = count_in_window(staging[: idx + 1], t, 24)
        c72h, _ = count_in_window(staging[: idx + 1], t, 72)

        gap_prev_min = (
            (t - staging[idx - 1]["dt"]).total_seconds() / 60 if idx > 0 else 99999.0
        )
        gap_prev2_min = (
            (staging[idx - 1]["dt"] - staging[idx - 2]["dt"]).total_seconds() / 60
            if idx > 1
            else 99999.0
        )
        accelerating = gap_prev_min < 90 and gap_prev_min < gap_prev2_min

        # Проверяем, вышел ли патч ПОСЛЕ этого события в окнах 30м, 1ч, 2ч, 3ч, 6ч, 24ч
        def hit_within(patch_list: list[dict[str, Any]], minutes: int) -> bool:
            end = t + timedelta(minutes=minutes)
            return any(t < p["release_dt"] <= end for p in patch_list)

        enriched.append(
            {
                "dt": t,
                "c15m": c15m,
                "c30m": c30m,
                "c1h": c1h,
                "c2h": c2h,
                "c6h": c6h,
                "c24h": c24h,
                "c72h": c72h,
                "gap_prev_min": gap_prev_min,
                "accelerating": accelerating,
                "major_1h": hit_within(majors, 60),
                "major_2h": hit_within(majors, 120),
                "major_3h": hit_within(majors, 180),
                "major_6h": hit_within(majors, 360),
                "major_24h": hit_within(majors, 1440),
                "letter_3h": hit_within(letters, 180),
                "letter_24h": hit_within(letters, 1440),
            }
        )

    rules = [
        ("Любая смена Staging", lambda r: True),
        ("c2h >= 2 (2+ смен за 2ч)", lambda r: r["c2h"] >= 2),
        ("c2h >= 3 (3+ смен за 2ч)", lambda r: r["c2h"] >= 3),
        ("c6h >= 3 (3+ смен за 6ч)", lambda r: r["c6h"] >= 3),
        ("c6h >= 4 (4+ смен за 6ч)", lambda r: r["c6h"] >= 4),
        ("c24h >= 4 (4+ смен за 24ч)", lambda r: r["c24h"] >= 4),
        ("c24h >= 6 (6+ смен за 24ч)", lambda r: r["c24h"] >= 6),
        ("Ускорение (интервал <90м и сокращается)", lambda r: r["accelerating"]),
        ("c6h >= 3 И ускорение", lambda r: r["c6h"] >= 3 and r["accelerating"]),
        ("Ночь Минск (00:00–06:00) И c6h >= 2", lambda r: (0 <= r["dt"].astimezone(MINSK_TZ).hour < 6) and r["c6h"] >= 2),
    ]

    print("\nОценка по независимым эпизодам (пауза между тревогами >= 6 часов):")
    for title, pred in rules:
        episodes = []
        last_ep_dt = None
        for row in enriched:
            if not pred(row):
                continue
            if last_ep_dt is None or (row["dt"] - last_ep_dt) >= timedelta(hours=6):
                episodes.append(row)
                last_ep_dt = row["dt"]

        n = len(episodes)
        if n == 0:
            print(f"- {title}: 0 эпизодов")
            continue

        m1 = sum(1 for r in episodes if r["major_1h"])
        m2 = sum(1 for r in episodes if r["major_2h"])
        m3 = sum(1 for r in episodes if r["major_3h"])
        m6 = sum(1 for r in episodes if r["major_6h"])
        m24 = sum(1 for r in episodes if r["major_24h"])
        l24 = sum(1 for r in episodes if r["letter_24h"] and not r["major_24h"])
        print(
            f"- {title}: эпизодов={n} | Крупный патч: "
            f"1ч={m1}/{n} ({m1*100/n:.1f}%), "
            f"2ч={m2}/{n} ({m2*100/n:.1f}%), "
            f"3ч={m3}/{n} ({m3*100/n:.1f}%), "
            f"6ч={m6}/{n} ({m6*100/n:.1f}%), "
            f"24ч={m24}/{n} ({m24*100/n:.1f}%) "
            f"| Буквенный за 24ч={l24}/{n}"
        )


def analyze_current_staging(staging: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 20 + " ТЕКУЩАЯ СЕРИЯ STAGING (ПОСЛЕДНИЕ 7 ДНЕЙ) " + "=" * 20)
    if not staging:
        print("Нет событий.")
        return
    latest_dt = staging[-1]["dt"]
    recent = [e for e in staging if e["dt"] >= latest_dt - timedelta(days=7)]
    prev_dt = None
    for e in recent:
        gap_str = (
            f"+{(e['dt'] - prev_dt).total_seconds() / 60:.0f}м"
            if prev_dt
            else "старт"
        )
        print(
            f"  {fmt_minsk(e['dt'])} | {e['from']}->{e['to']} (+{e['delta']}) | пауза: {gap_str}"
        )
        prev_dt = e["dt"]


def main() -> None:
    history = fetch_json(HISTORY_URL)
    latest = fetch_json(LATEST_URL)

    events = prepare_events(history.get("events", []))
    patches = prepare_patches(latest.get("patches", []))

    events_by_branch = {
        "staging": [e for e in events if e["branch"] == "staging"],
        "public": [e for e in events if e["branch"] == "public"],
        "experimental": [e for e in events if e["branch"] == "experimental"],
    }

    print(
        f"Всего событий: {len(events)} "
        f"(staging={len(events_by_branch['staging'])}, "
        f"public={len(events_by_branch['public'])}, "
        f"experimental={len(events_by_branch['experimental'])})"
    )
    print(
        f"Всего патчей: {len(patches)} "
        f"(крупных={sum(1 for p in patches if p['is_major'])}, "
        f"буквенных={sum(1 for p in patches if not p['is_major'])})"
    )

    analyze_major_patches(events_by_branch, patches)
    evaluate_staging_signals(events_by_branch["staging"], patches)
    analyze_current_staging(events_by_branch["staging"])


if __name__ == "__main__":
    main()
