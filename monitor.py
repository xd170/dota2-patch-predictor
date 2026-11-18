import os
from datetime import datetime
from typing import Any

from collector import collect_all
from config import TARGET_PATCH
from features import EventIndex, MINSK_TZ, normalize_events, normalize_patches, parse_iso_utc
from predictor import predict
from steam_api import check_steam_versions, find_steam_patch_announcement
from storage import (
    append_observation,
    detect_branch_changes,
    extract_branch_snapshot,
    load_state,
    save_state,
)
from telegram_notifier import (
    format_daily_pulse_message,
    format_prediction_message,
    format_release_message,
    send_telegram_message,
)

LEVEL_RANK = {"calm": 0, "elevated": 1, "high": 2, "critical": 3}


def _minutes_since(iso_ts: str | None, now_utc: datetime) -> float:
    if not iso_ts:
        return 99999.0
    try:
        prev_dt = parse_iso_utc(iso_ts)
        return (now_utc - prev_dt).total_seconds() / 60.0
    except Exception:
        return 99999.0


def main() -> None:
    print("========== DOTA 2 PATCH PREDICTOR ==========")
    force_notify = os.getenv("FORCE_NOTIFY", "").lower() == "true"

    data = collect_all(save_raw=False)
    latest = data["latest"]
    history = data["history"]
    now_utc = parse_iso_utc(data["collected_at"])
    minsk_now = now_utc.astimezone(MINSK_TZ)
    minsk_str = minsk_now.strftime("%Y-%m-%d %H:%M")

    steam_versions = check_steam_versions()
    steam_announcement = find_steam_patch_announcement(TARGET_PATCH)
    print(f"Steam API прямая проверка: {steam_versions}")

    state = load_state()

    prev_steam_pub = state.get("last_steam_public_version")
    curr_steam_pub = steam_versions.get("public")
    steam_public_change: dict[str, Any] | None = None
    if (
        prev_steam_pub is not None
        and curr_steam_pub is not None
        and prev_steam_pub != curr_steam_pub
    ):
        steam_public_change = {
            "branch": "public",
            "old_version": prev_steam_pub,
            "new_version": curr_steam_pub,
            "delta": curr_steam_pub - prev_steam_pub,
        }

    # Аварийный режим, если Finargot временно не отвечает
    if latest is None:
        print("Переключаемся на резервную проверку только через Steam API.")
        if steam_announcement and not state.get("target_patch_released", False):
            msg = format_release_message(
                patch_name=TARGET_PATCH,
                evaluated_minsk=minsk_str,
                public_change=steam_public_change,
                confirmed_via_patches=True,
                announcement=steam_announcement,
            )
            send_telegram_message(msg)
            state["target_patch_released"] = True
        elif (
            steam_public_change
            and not state.get("target_patch_released", False)
            and state.get("last_notified_level") in ("high", "critical")
            and state.get("public_release_alerted_version") != steam_public_change["new_version"]
        ):
            msg = format_release_message(
                patch_name=TARGET_PATCH,
                evaluated_minsk=minsk_str,
                public_change=steam_public_change,
                confirmed_via_patches=False,
                announcement=None,
            )
            send_telegram_message(msg)
            state["public_release_alerted_version"] = steam_public_change["new_version"]
        if curr_steam_pub is not None:
            state["last_steam_public_version"] = curr_steam_pub
        save_state(state)
        return

    # Основной режим: объединяем архив и свежие 500 событий
    raw_events = (history.get("events", []) or []) + (latest.get("events", []) or [])
    events = normalize_events(raw_events)
    patches = normalize_patches(latest.get("patches", []) or [])
    index = EventIndex(events)

    prediction = predict(now_utc, index, patches)
    probs = prediction["probabilities"]
    print(
        f"Время Минск: {prediction['evaluated_at_minsk']} | "
        f"Уровень: {prediction['signal_level']} | "
        f"Готовность: {prediction['readiness_pct']}% | "
        f"Вероятности 15м/30м/1ч/2ч/3ч: "
        f"{probs[15]}/{probs[30]}/{probs[60]}/{probs[120]}/{probs[180]}%"
    )

    curr_branches = extract_branch_snapshot(latest)
    prev_branches = state.get("last_branches", {})
    changes = detect_branch_changes(prev_branches, curr_branches)

    if changes:
        print(f"Обнаружены изменения веток: {changes}")
    else:
        print("Изменений версий веток с прошлого опроса нет.")

    patch_names = [p["patch"] for p in patches]
    if not state.get("known_patches"):
        state["known_patches"] = patch_names

    target_confirmed_now = (
        (TARGET_PATCH in patch_names or steam_announcement is not None)
        and not state.get("target_patch_released", False)
    )

    staging_change = next((c for c in changes if c["branch"] == "staging"), None)
    public_change = next((c for c in changes if c["branch"] == "public"), None) or steam_public_change

    # 1. Официальное подтверждение выхода целевого патча
    if target_confirmed_now:
        msg = format_release_message(
            patch_name=TARGET_PATCH,
            evaluated_minsk=prediction["evaluated_at_minsk"],
            public_change=public_change,
            confirmed_via_patches=True,
            announcement=steam_announcement,
        )
        send_telegram_message(msg)
        state["target_patch_released"] = True
        state["known_patches"] = patch_names

    # 2. Обновление Public на фоне высокой активности Staging
    elif (
        public_change is not None
        and not state.get("target_patch_released", False)
        and (
            prediction["probabilities"][180] >= 15.0
            or prediction["readiness_pct"] >= 58.0
            or (
                prediction["features"]["st_6h"] >= 2
                and prediction["features"]["in_release_window"]
            )
        )
        and state.get("public_release_alerted_version") != public_change["new_version"]
    ):
        msg = format_release_message(
            patch_name=TARGET_PATCH,
            evaluated_minsk=prediction["evaluated_at_minsk"],
            public_change=public_change,
            confirmed_via_patches=False,
            announcement=steam_announcement,
        )
        send_telegram_message(msg)
        state["public_release_alerted_version"] = public_change["new_version"]
        state["last_notified_at_utc"] = now_utc.isoformat()

    # 3. Прогнозные уведомления или вечерний «пульс»
    elif not state.get("target_patch_released", False):
        p180 = probs[180]
        prev_p180 = float(state.get("last_notified_p180", 0.0) or 0.0)
        prev_level = str(state.get("last_notified_level", "calm"))
        curr_level = str(prediction["signal_level"])
        mins_since_notif = _minutes_since(state.get("last_notified_at_utc"), now_utc)

        should_notify = False
        trigger_reason = ""

        if force_notify:
            should_notify = True
            trigger_reason = "Тестовая проверка уведомлений"
        elif staging_change is not None:
            should_notify = True
            trigger_reason = "Обновился Staging-сервер"
        elif mins_since_notif >= 45.0:
            level_up = (
                LEVEL_RANK.get(curr_level, 0) > LEVEL_RANK.get(prev_level, 0)
                and p180 >= 10.0
            )
            prob_jump = p180 >= 10.0 and (p180 - prev_p180) >= 6.0
            if level_up or prob_jump:
                should_notify = True
                trigger_reason = "Заметный рост вероятности выхода патча"

        if should_notify:
            msg = format_prediction_message(prediction, changes, trigger_reason)
            res = send_telegram_message(msg)
            if not res.get("skipped"):
                state["last_notified_at_utc"] = now_utc.isoformat()
                state["last_notified_p180"] = p180
                state["last_notified_level"] = curr_level
                if staging_change:
                    state["last_notified_staging_version"] = staging_change["new_version"]
        else:
            # Вечерний «пульс» 1 раз в сутки в 21:00–21:30 Минск
            today_minsk = minsk_now.strftime("%Y-%m-%d")
            in_pulse_window = minsk_now.hour == 21 and minsk_now.minute <= 30
            if (
                in_pulse_window
                and state.get("last_pulse_date_minsk") != today_minsk
                and mins_since_notif >= 120.0
            ):
                pulse_msg = format_daily_pulse_message(prediction, steam_versions)
                res = send_telegram_message(pulse_msg)
                if not res.get("skipped"):
                    state["last_pulse_date_minsk"] = today_minsk
            else:
                print("Условие для отправки в Telegram не достигнуто — уведомление не требуется.")

    state["last_branches"] = curr_branches
    if curr_steam_pub is not None:
        state["last_steam_public_version"] = curr_steam_pub
    if "staging" in curr_branches and state.get("last_notified_staging_version") is None:
        state["last_notified_staging_version"] = curr_branches["staging"].get("client_version")

    append_observation(data["collected_at"], latest, curr_branches, prediction)
    save_state(state)
    print("Состояние и наблюдение успешно сохранены.")


if __name__ == "__main__":
    main()
