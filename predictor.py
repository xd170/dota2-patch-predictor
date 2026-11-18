import math
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from config import PREDICTION_HORIZONS_MINUTES
from features import EventIndex, MINSK_TZ, extract_features


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _release_window_density(utc_dt: datetime) -> float:
    """
    Плотность вероятности выхода крупного патча по времени суток (Europe/Minsk).
    Исторически 7 из 7 крупных патчей вышли между 01:08 и 03:52 (пик ~02:45).
    """
    minsk_dt = utc_dt.astimezone(MINSK_TZ)
    hour_float = minsk_dt.hour + minsk_dt.minute / 60.0 + minsk_dt.second / 3600.0

    diff = abs(hour_float - 2.75)
    if diff > 12.0:
        diff = 24.0 - diff

    gaussian = math.exp(-0.5 * (diff / 1.45) ** 2)
    return 0.02 + 0.98 * gaussian


def _horizon_window_factor(now_utc: datetime, horizon_minutes: int) -> float:
    """Оценивает перекрытие интервала [now, now + horizon_minutes] с окном Valve."""
    steps = max(3, horizon_minutes // 5)
    step_sec = (horizon_minutes * 60.0) / steps
    densities = [
        _release_window_density(now_utc + timedelta(seconds=step_sec * (i + 0.5)))
        for i in range(steps)
    ]
    avg_density = sum(densities) / len(densities)
    max_density = max(densities)
    return 0.55 * avg_density + 0.45 * max_density


def _compute_readiness(features: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """Рассчитывает индекс готовности (0..1) по признакам без утечки будущего."""
    s_72h = _clamp(features["st_72h"] / 8.0)
    s_d72h = _clamp(features["st_d72h"] / 12.0)
    s_7d = _clamp(features["st_7d"] / 14.0)
    buildup = 0.45 * s_72h + 0.30 * s_d72h + 0.25 * s_7d

    s_24h = _clamp(features["st_24h"] / 5.0)
    s_6h = _clamp(features["st_6h"] / 3.0)
    s_2h = _clamp(features["st_2h"] / 2.0)

    mins_since = features["st_mins_since_last"]
    if mins_since <= 180.0:
        recency = 1.0
    elif mins_since <= 360.0:
        recency = 1.0 - 0.45 * ((mins_since - 180.0) / 180.0)
    elif mins_since <= 720.0:
        recency = 0.55 - 0.35 * ((mins_since - 360.0) / 360.0)
    else:
        recency = 0.20 * math.exp(-(mins_since - 720.0) / 720.0)

    accel_bonus = 0.0
    if features["st_accelerating"]:
        accel_bonus = 0.16
    elif features["st_last_gap_min"] <= 120.0 and mins_since <= 240.0:
        accel_bonus = 0.08

    if features["st_24h"] == 0 and features["st_72h"] <= 1:
        impulse = 0.0
    else:
        impulse = _clamp(
            0.35 * s_6h + 0.25 * s_24h + 0.20 * s_2h + 0.20 * recency + accel_bonus
        )

    days_since = features["days_since_major"]
    cycle_modifier = 1.0
    if days_since is not None:
        if days_since < 25.0:
            cycle_modifier = 0.25
        elif days_since < 50.0:
            cycle_modifier = 0.65

    raw_readiness = (0.40 * buildup + 0.60 * impulse) * cycle_modifier
    components = {
        "buildup": round(buildup, 3),
        "impulse": round(impulse, 3),
        "recency": round(recency, 3),
        "cycle_modifier": round(cycle_modifier, 3),
    }
    return _clamp(raw_readiness), components


def _find_similar_patches(
    now_utc: datetime,
    features: dict[str, Any],
    index: EventIndex,
    patches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Находит прошлые крупные патчи (строго ДО now_utc), похожие по динамике Staging."""
    now_ts = now_utc.timestamp()
    past_majors = [
        p for p in patches if p["is_major"] and p["release_ts"] < now_ts
    ]
    if not past_majors:
        return []

    matches = []
    for p in past_majors:
        cutoff = p["release_dt"] - timedelta(seconds=1)
        hist_feat = extract_features(cutoff, index)
        rel_minsk = p["release_dt"].astimezone(MINSK_TZ)
        rel_mins_of_day = rel_minsk.hour * 60 + rel_minsk.minute

        dist = (
            abs(features["st_72h"] - hist_feat["st_72h"]) / 4.0
            + abs(features["st_24h"] - hist_feat["st_24h"]) / 3.0
            + abs(features["st_6h"] - hist_feat["st_6h"]) / 2.0
            + abs(features["st_d72h"] - hist_feat["st_d72h"]) / 6.0
        ) / 4.0
        similarity = round(max(0.0, 1.0 - dist * 0.55) * 100.0, 1)
        matches.append(
            {
                "patch": p["patch"],
                "similarity_pct": similarity,
                "release_time_minsk": rel_minsk.strftime("%H:%M"),
                "release_mins_of_day": rel_mins_of_day,
                "hist_st_72h": hist_feat["st_72h"],
                "hist_st_24h": hist_feat["st_24h"],
                "hist_st_6h": hist_feat["st_6h"],
                "hist_last_mins": hist_feat["st_mins_since_last"],
            }
        )

    matches.sort(key=lambda m: m["similarity_pct"], reverse=True)
    return matches[:3]


def _estimate_release_eta(
    now_utc: datetime,
    features: dict[str, Any],
    readiness: float,
    similar_patches: list[dict[str, Any]],
    past_majors: list[dict[str, Any]],
    index: EventIndex,
) -> dict[str, Any]:
    """
    Рассчитывает ориентировочное окно выхода патча по архиву прошлых крупных релизов.
    """
    minsk_now = now_utc.astimezone(MINSK_TZ)
    now_mins_of_day = minsk_now.hour * 60 + minsk_now.minute

    ref_mins_of_day: list[int] = []
    ref_delays: list[float] = []

    for m in similar_patches:
        if m["similarity_pct"] >= 45.0:
            ref_mins_of_day.append(m["release_mins_of_day"])
            if m["hist_last_mins"] <= 260.0:
                ref_delays.append(m["hist_last_mins"])

    if not ref_mins_of_day and past_majors:
        for p in past_majors:
            rel_m = p["release_dt"].astimezone(MINSK_TZ)
            ref_mins_of_day.append(rel_m.hour * 60 + rel_m.minute)

    if not ref_delays and past_majors:
        for p in past_majors:
            cutoff = p["release_dt"] - timedelta(seconds=1)
            f = extract_features(cutoff, index)
            if f["st_mins_since_last"] <= 260.0:
                ref_delays.append(f["st_mins_since_last"])

    low_m = min(ref_mins_of_day) if ref_mins_of_day else 68
    med_m = int(median(ref_mins_of_day)) if ref_mins_of_day else 167
    high_m = max(ref_mins_of_day) if ref_mins_of_day else 232

    def fmt_hhmm(total_mins: int) -> str:
        total_mins = total_mins % 1440
        return f"{total_mins // 60:02d}:{total_mins % 60:02d}"

    clock_window = f"{fmt_hhmm(low_m)}–{fmt_hhmm(high_m)}"
    clock_median = fmt_hhmm(med_m)

    mins_to_med = (med_m - now_mins_of_day) % 1440
    in_night = 0 <= now_mins_of_day <= 270
    elapsed_st = features["st_mins_since_last"]

    if readiness < 0.25:
        summary = (
            f"Активность низкая; базовое историческое окно релизов: "
            f"{clock_window} Минск (медиана {clock_median})"
        )
        return {"summary": summary, "clock_window": clock_window, "clock_median": clock_median}

    if (in_night or now_mins_of_day >= 1260) and elapsed_st <= 240.0 and features["st_24h"] >= 2:
        d_low = int(min(ref_delays)) if ref_delays else 80
        d_med = int(median(ref_delays)) if ref_delays else 112
        d_high = int(max(ref_delays)) if ref_delays else 227

        rem_low = max(5, int(d_low - elapsed_st))
        rem_med = max(15, int(d_med - elapsed_st))
        rem_high = max(30, int(d_high - elapsed_st))

        eta_start = (minsk_now + timedelta(minutes=rem_low)).strftime("%H:%M")
        eta_end = (minsk_now + timedelta(minutes=rem_high)).strftime("%H:%M")
        summary = (
            f"Через ~{rem_low}–{rem_high} мин (ориентир ~{rem_med} мин → "
            f"{eta_start}–{eta_end} Минск; окно архива {clock_window})"
        )
        return {"summary": summary, "clock_window": clock_window, "clock_median": clock_median}

    if in_night:
        if now_mins_of_day <= high_m:
            rem = max(5, high_m - now_mins_of_day)
            summary = (
                f"Идет окно релизов ({clock_window} Минск, медиана {clock_median}; "
                f"до конца окна ~{rem} мин)"
            )
        else:
            summary = f"Пиковое окно ({clock_window} Минск) на сегодня уже прошло"
        return {"summary": summary, "clock_window": clock_window, "clock_median": clock_median}

    hours_to = mins_to_med / 60.0
    summary = (
        f"Ближайшее окно по архиву: {clock_window} Минск "
        f"(медиана {clock_median}, через ~{hours_to:.1f} ч при вечернем ускорении Staging)"
    )
    return {"summary": summary, "clock_window": clock_window, "clock_median": clock_median}


def _build_explanation(
    features: dict[str, Any],
    readiness: float,
    probs: dict[int, float],
) -> tuple[list[str], list[str], str]:
    """Формирует причины оценки, недостающие признаки и уровень сигнала."""
    reasons: list[str] = []
    missing: list[str] = []

    if features["st_7d"] >= 10:
        reasons.append(
            f"7-дневный разогрев Staging: {features['st_7d']} смен (+{features['st_d7d']} билдов)"
        )
    elif features["st_72h"] >= 4:
        reasons.append(
            f"Активность Staging за 72ч: {features['st_72h']} смен (+{features['st_d72h']} билдов)"
        )
    else:
        missing.append("многодневная серия Staging (обычно 5–10 смен за 72ч)")

    if features["st_6h"] >= 2:
        reasons.append(
            f"Сгущение за 6ч: {features['st_6h']} смен (последняя {features['st_mins_since_last']:.0f} мин назад)"
        )
    elif features["st_24h"] >= 3:
        reasons.append(
            f"За сутки {features['st_24h']} смен Staging (последняя {features['st_mins_since_last']:.0f} мин назад)"
        )
    else:
        missing.append("вечернее сгущение Staging (2+ смены за 6 часов)")

    if features["st_accelerating"]:
        reasons.append(
            f"Ускорение интервалов: {features['st_prev_gap_min']:.0f}м → {features['st_last_gap_min']:.0f}м"
        )
    else:
        missing.append("сокращение интервалов между сборками (< 120 мин)")

    if features["in_release_window"]:
        reasons.append(
            f"Ночное окно релизов Valve ({features['now_minsk'].split()[-1]} Минск)"
        )
    elif features["mins_to_valve_window"] <= 180:
        reasons.append(
            f"До окна релизов Valve (01:00–04:00 Минск) ~{features['mins_to_valve_window']:.0f} мин"
        )
    else:
        missing.append("вход в ночное окно Valve (01:00–04:00 по Минску)")

    p180 = probs.get(180, 0.0)
    if readiness >= 0.75 and p180 >= 25.0:
        level = "critical"
    elif readiness >= 0.58 and p180 >= 10.0:
        level = "high"
    elif readiness >= 0.35 or features["st_7d"] >= 10:
        level = "elevated"
    else:
        level = "calm"

    return reasons, missing, level


def predict(
    now_utc: datetime,
    index: EventIndex,
    patches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Возвращает краткосрочный прогноз на 15, 30, 60, 120 и 180 минут и оценку окна выхода."""
    now_ts = now_utc.timestamp()
    past_majors = [
        p for p in patches if p["is_major"] and p["release_ts"] < now_ts
    ]
    last_major_dt = past_majors[-1]["release_dt"] if past_majors else None

    features = extract_features(now_utc, index, last_major_dt=last_major_dt)
    readiness, components = _compute_readiness(features)

    horizon_caps = {
        15: 18.0,
        30: 28.0,
        60: 42.0,
        120: 56.0,
        180: 66.0,
    }

    probabilities: dict[int, float] = {}
    prev_prob = 0.0
    for horizon in PREDICTION_HORIZONS_MINUTES:
        win_factor = _horizon_window_factor(now_utc, horizon)
        combined = (readiness ** 2.55) * (win_factor ** 1.35)
        raw_pct = 0.1 + horizon_caps[horizon] * combined
        calibrated = round(max(prev_prob, min(raw_pct, horizon_caps[horizon])), 1)
        probabilities[horizon] = calibrated
        prev_prob = calibrated

    similar_patches = _find_similar_patches(now_utc, features, index, patches)
    reasons, missing, level = _build_explanation(features, readiness, probabilities)
    eta = _estimate_release_eta(
        now_utc, features, readiness, similar_patches, past_majors, index
    )

    return {
        "evaluated_at_utc": now_utc.isoformat(),
        "evaluated_at_minsk": features["now_minsk"],
        "signal_level": level,
        "readiness_pct": round(readiness * 100.0, 1),
        "components": components,
        "probabilities": probabilities,
        "features": features,
        "similar_patches": similar_patches,
        "reasons": reasons,
        "missing": missing,
        "eta": eta,
        "window_note": eta["summary"],
    }
