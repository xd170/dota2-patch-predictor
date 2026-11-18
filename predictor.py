import math
from datetime import datetime, timedelta
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
    return 0.025 + 0.975 * gaussian


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
                "hist_st_72h": hist_feat["st_72h"],
                "hist_st_24h": hist_feat["st_24h"],
                "hist_st_6h": hist_feat["st_6h"],
                "hist_last_mins": hist_feat["st_mins_since_last"],
            }
        )

    matches.sort(key=lambda m: m["similarity_pct"], reverse=True)
    return matches[:3]


def _estimate_window_note(features: dict[str, Any], readiness: float) -> str:
    """Экспериментальная оценка наиболее вероятного окна выхода (Минск)."""
    if readiness < 0.25:
        return "Недостаточно активности Staging для оценки времени выхода"

    if features["in_release_window"]:
        if features["st_mins_since_last"] <= 240:
            return (
                f"Идет окно релизов (01:00–04:00 Минск); "
                f"после последнего Staging прошло {features['st_mins_since_last']:.0f} мин "
                f"(обычно перед релизом 80–230 мин, экспериментально)"
            )
        return "Идет окно релизов (01:00–04:00 Минск), но свежих билдов Staging за 4ч нет"

    mins_left = features["mins_to_valve_window"]
    return (
        f"Обычно релиз происходит в 01:00–04:00 Минск "
        f"(до пика окна ~{mins_left / 60.0:.1f} ч, экспериментально)"
    )


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
            f"Сильный 7-дневный разогрев Staging: {features['st_7d']} смен (+{features['st_d7d']} билдов)"
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
    if readiness >= 0.60 and p180 >= 25.0:
        level = "critical"
    elif readiness >= 0.42 and p180 >= 12.0:
        level = "high"
    elif readiness >= 0.25 or features["st_7d"] >= 10:
        level = "elevated"
    else:
        level = "calm"

    return reasons, missing, level


def predict(
    now_utc: datetime,
    index: EventIndex,
    patches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Возвращает краткосрочный прогноз на 15, 30, 60, 120 и 180 минут."""
    now_ts = now_utc.timestamp()
    past_majors = [
        p for p in patches if p["is_major"] and p["release_ts"] < now_ts
    ]
    last_major_dt = past_majors[-1]["release_dt"] if past_majors else None

    features = extract_features(now_utc, index, last_major_dt=last_major_dt)
    readiness, components = _compute_readiness(features)

    horizon_caps = {
        15: 16.0,
        30: 26.0,
        60: 38.0,
        120: 52.0,
        180: 62.0,
    }

    probabilities: dict[int, float] = {}
    prev_prob = 0.0
    for horizon in PREDICTION_HORIZONS_MINUTES:
        win_factor = _horizon_window_factor(now_utc, horizon)
        combined = (readiness ** 1.35) * (win_factor ** 1.15)
        raw_pct = 0.1 + horizon_caps[horizon] * combined
        calibrated = round(max(prev_prob, min(raw_pct, horizon_caps[horizon])), 1)
        probabilities[horizon] = calibrated
        prev_prob = calibrated

    similar_patches = _find_similar_patches(now_utc, features, index, patches)
    reasons, missing, level = _build_explanation(features, readiness, probabilities)
    window_note = _estimate_window_note(features, readiness)

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
        "window_note": window_note,
    }
