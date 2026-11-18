import math
from datetime import datetime, timedelta, timezone
from typing import Any

from collector import fetch_json
from config import HISTORY_URL, LATEST_URL
from features import EventIndex, MINSK_TZ, normalize_events, normalize_patches
from predictor import predict


def brier_score(preds: list[float], actuals: list[int]) -> float:
    if not preds:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(preds, actuals)) / len(preds)


def log_loss(preds: list[float], actuals: list[int]) -> float:
    if not preds:
        return 0.0
    eps = 1e-6
    total = 0.0
    for p, y in zip(preds, actuals):
        p_c = max(eps, min(1.0 - eps, p))
        total += -(y * math.log(p_c) + (1 - y) * math.log(1.0 - p_c))
    return total / len(preds)


def run_backtest() -> None:
    print("Загрузка данных для честного исторического теста (backtesting)...")
    history = fetch_json(HISTORY_URL)
    latest = fetch_json(LATEST_URL)

    events = normalize_events(history.get("events", []))
    patches = normalize_patches(latest.get("patches", []))
    if not events:
        print("Нет событий для проверки.")
        return

    majors = [p for p in patches if p["is_major"]]
    index = EventIndex(events)

    start_dt = events[0]["dt"] + timedelta(days=7)
    end_dt = events[-1]["dt"]

    horizons = (60, 120, 180)
    preds_by_h: dict[int, list[float]] = {h: [] for h in horizons}
    actuals_by_h: dict[int, list[int]] = {h: [] for h in horizons}
    alert_records: list[dict[str, Any]] = []

    current = start_dt
    step = timedelta(minutes=30)
    steps_count = 0

    while current <= end_dt:
        res = predict(current, index, patches)
        curr_ts = current.timestamp()

        for h in horizons:
            p_prob = res["probabilities"][h] / 100.0
            end_ts = curr_ts + h * 60.0
            hit = 1 if any(curr_ts < m["release_ts"] <= end_ts for m in majors) else 0
            preds_by_h[h].append(p_prob)
            actuals_by_h[h].append(hit)

        p180 = res["probabilities"][180]
        if p180 >= 10.0:
            hit_6h = any(curr_ts < m["release_ts"] <= curr_ts + 6 * 3600 for m in majors)
            hit_24h = any(curr_ts < m["release_ts"] <= curr_ts + 24 * 3600 for m in majors)
            alert_records.append(
                {
                    "dt": current,
                    "p180": p180,
                    "p60": res["probabilities"][60],
                    "readiness": res["readiness_pct"],
                    "hit_6h": hit_6h,
                    "hit_24h": hit_24h,
                }
            )

        current += step
        steps_count += 1

    print(f"\nПроверено временных точек (шаг 30 мин): {steps_count}")
    print("\n--- МЕТРИКИ КАЧЕСТВА ПО ГОРИЗОНТАМ ---")
    for h in horizons:
        bs = brier_score(preds_by_h[h], actuals_by_h[h])
        ll = log_loss(preds_by_h[h], actuals_by_h[h])
        pos = sum(actuals_by_h[h])
        print(
            f"Горизонт {h:3d} мин | Положительных точек: {pos:3d} "
            f"| Brier Score: {bs:.5f} | Log Loss: {ll:.5f}"
        )

    print("\n--- КАЛИБРОВКА ДЛЯ ОКНА 180 МИН (3 ЧАСА) ---")
    buckets = [(0, 5), (5, 12), (12, 22), (22, 35), (35, 100)]
    for low, high in buckets:
        indices = [
            i for i, p in enumerate(preds_by_h[180]) if low <= p * 100.0 < high
        ]
        if not indices:
            print(f"  Прогноз {low:2d}–{high:2d}%: 0 точек")
            continue
        hits = sum(actuals_by_h[180][i] for i in indices)
        avg_p = sum(preds_by_h[180][i] for i in indices) * 100.0 / len(indices)
        fact_p = hits * 100.0 / len(indices)
        print(
            f"  Прогноз {low:2d}–{high:2d}% (средн. {avg_p:4.1f}%): "
            f"точек={len(indices):5d}, факт={hits:2d} ({fact_p:4.1f}%)"
        )

    print("\n--- НЕЗАВИСИМЫЕ ЭПИЗОДЫ ТРЕВОГ (пауза >= 12 часов) ---")
    for threshold in (12.0, 20.0, 30.0):
        episodes = []
        last_dt = None
        for rec in alert_records:
            if rec["p180"] < threshold:
                continue
            if last_dt is None or (rec["dt"] - last_dt) >= timedelta(hours=12):
                episodes.append(rec)
                last_dt = rec["dt"]
        n = len(episodes)
        h6 = sum(1 for e in episodes if e["hit_6h"])
        h24 = sum(1 for e in episodes if e["hit_24h"])
        print(
            f"  Порог P(3ч) >= {threshold:4.1f}%: эпизодов за 3 года={n:2d} "
            f"| Выход за 6ч={h6}/{n if n else 1}, за 24ч={h24}/{n if n else 1}"
        )

    print("\n--- ПОВЕДЕНИЕ МОДЕЛИ ПЕРЕД ВСЕМИ 7 КРУПНЫМИ ПАТЧАМИ ---")
    for m in majors:
        rel = m["release_dt"]
        print(
            f"\nПатч {m['patch']} (релиз {rel.astimezone(MINSK_TZ).strftime('%Y-%m-%d %H:%M')}):"
        )
        for mins_before in (360, 180, 120, 60, 30, 15):
            t_check = rel - timedelta(minutes=mins_before)
            res = predict(t_check, index, patches)
            p = res["probabilities"]
            print(
                f"  За {mins_before:3d} мин ({t_check.astimezone(MINSK_TZ).strftime('%H:%M')}): "
                f"готовность={res['readiness_pct']:4.1f}% | "
                f"15м={p[15]:4.1f}% | 30м={p[30]:4.1f}% | 1ч={p[60]:4.1f}% | "
                f"2ч={p[120]:4.1f}% | 3ч={p[180]:4.1f}%"
            )

    print("\n--- ПРОГНОЗ ПРЯМО СЕЙЧАС ---")
    now_utc = datetime.now(timezone.utc)
    live = predict(now_utc, index, patches)
    p = live["probabilities"]
    print(
        f"Время (Минск): {live['evaluated_at_minsk']} | "
        f"Статус: {live['signal_level']} | Готовность: {live['readiness_pct']}%"
    )
    print(
        f"Вероятности: 15м={p[15]}% | 30м={p[30]}% | 1ч={p[60]}% | "
        f"2ч={p[120]}% | 3ч={p[180]}%"
    )
    print(f"Окно выхода: {live['window_note']}")
    print(f"Причины: {live['reasons']}")
    print(f"Чего не хватает: {live['missing']}")
    print(f"Похожие патчи: {live['similar_patches']}")


if __name__ == "__main__":
    run_backtest()
