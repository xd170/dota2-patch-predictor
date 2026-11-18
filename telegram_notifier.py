import html
from datetime import datetime
from typing import Any

import requests

from config import TARGET_PATCH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS
from features import MINSK_TZ, parse_iso_utc

TELEGRAM_TIMEOUT = 15

LEVEL_TITLES = {
    "critical": "🔥 КРИТИЧЕСКИЙ СИГНАЛ ПАТЧА",
    "high": "🚨 ВЫСОКАЯ АКТИВНОСТЬ STAGING",
    "elevated": "⚡ ОБНОВЛЕНИЕ STAGING",
    "calm": "ℹ️ МОНИТОРИНГ STAGING",
}

LEVEL_LABELS_RU = {
    "critical": "🔥 Критический",
    "high": "🚨 Высокий",
    "elevated": "⚡ Повышенный",
    "calm": "🟢 Спокойный",
}

BTN_STATUS = "📊 Статус патча"
BTN_BUILDS = "🔧 Последние билды"
BTN_HISTORY = "📈 Окно и патчи"
BTN_HELP = "ℹ️ Справка"

REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": BTN_STATUS}, {"text": BTN_BUILDS}],
        [{"text": BTN_HISTORY}, {"text": BTN_HELP}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

BUTTON_TO_COMMAND = {
    BTN_STATUS: "status",
    BTN_BUILDS: "builds",
    BTN_HISTORY: "history",
    BTN_HELP: "help",
}

BOT_COMMANDS = [
    {"command": "status", "description": "Актуальный прогноз и статус веток"},
    {"command": "builds", "description": "Версии веток и последние билды"},
    {"command": "history", "description": "Окно выхода и похожие патчи"},
    {"command": "start", "description": "Показать кнопки меню"},
    {"command": "help", "description": "Как работает бот и команды"},
]


def _format_branch_age(iso_ts: str | None, now_utc: datetime) -> str:
    """Форматирует время последнего изменения ветки в Минске и возраст события."""
    if not iso_ts:
        return "неизвестно"
    try:
        dt_utc = parse_iso_utc(str(iso_ts))
        dt_minsk = dt_utc.astimezone(MINSK_TZ)
        mins = max(0.0, (now_utc - dt_utc).total_seconds() / 60.0)
        if mins < 60.0:
            age_str = f"{int(round(mins))} мин назад"
        elif mins < 24.0 * 60.0:
            age_str = f"{mins / 60.0:.1f} ч назад"
        else:
            age_str = f"{mins / (24.0 * 60.0):.1f} дн назад"
        return f"{dt_minsk.strftime('%d.%m %H:%M')} ({age_str})"
    except Exception:
        return str(iso_ts)


def _normalize_command(raw_text: str) -> str | None:
    """Преобразует текст сообщения или нажатие кнопки в имя команды."""
    text = raw_text.strip()
    if not text:
        return None
    if text in BUTTON_TO_COMMAND:
        return BUTTON_TO_COMMAND[text]
    if text.startswith("/"):
        cmd = text.split()[0][1:].split("@")[0].lower()
        if cmd in ("start", "menu"):
            return "start"
        if cmd in ("status", "predict", "patch"):
            return "status"
        if cmd in ("builds", "versions", "branches"):
            return "builds"
        if cmd in ("history", "eta", "window"):
            return "history"
        if cmd == "help":
            return "help"
    return None


def ensure_bot_commands() -> bool:
    """Регистрирует меню /команд в интерфейсе Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    try:
        response = requests.post(
            url,
            json={"commands": BOT_COMMANDS},
            timeout=TELEGRAM_TIMEOUT,
        )
        return bool(response.ok)
    except Exception as error:
        print(f"Предупреждение setMyCommands: {error}")
        return False


def _answer_callback_query(callback_id: str) -> None:
    """Закрывает индикатор загрузки у inline-кнопки (если она была нажата)."""
    if not TELEGRAM_BOT_TOKEN or not callback_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_id}, timeout=8)
    except Exception:
        pass


def fetch_pending_telegram_commands(
    last_update_id: int | None,
) -> tuple[list[tuple[str, str]], int | None]:
    """
    Читает накопленные команды и нажатия кнопок из Telegram getUpdates.
    Отвечает только чатам из TELEGRAM_CHAT_IDS и убирает дубли повторных нажатий.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return [], last_update_id

    allowed = {str(cid) for cid in TELEGRAM_CHAT_IDS}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params: dict[str, Any] = {
        "timeout": 0,
        "limit": 100,
        "allowed_updates": '["message","callback_query"]',
    }
    if last_update_id is not None:
        params["offset"] = int(last_update_id) + 1

    try:
        response = requests.get(url, params=params, timeout=TELEGRAM_TIMEOUT)
        if not response.ok:
            print(f"Предупреждение Telegram getUpdates: HTTP {response.status_code}")
            return [], last_update_id
        payload = response.json()
        if not payload.get("ok"):
            return [], last_update_id
        updates = payload.get("result", [])
    except Exception as error:
        print(f"Предупреждение Telegram getUpdates: {error}")
        return [], last_update_id

    max_update_id = last_update_id
    pending: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for upd in updates:
        upd_id = upd.get("update_id")
        if isinstance(upd_id, int):
            if max_update_id is None or upd_id > max_update_id:
                max_update_id = upd_id

        chat_id_str = ""
        raw_text = ""

        if "message" in upd and isinstance(upd["message"], dict):
            msg = upd["message"]
            chat_id_str = str(msg.get("chat", {}).get("id", ""))
            raw_text = str(msg.get("text", "") or "")
        elif "callback_query" in upd and isinstance(upd["callback_query"], dict):
            cq = upd["callback_query"]
            _answer_callback_query(str(cq.get("id", "")))
            cq_msg = cq.get("message") or {}
            chat_id_str = str(cq_msg.get("chat", {}).get("id", ""))
            raw_text = str(cq.get("data", "") or "")

        if not chat_id_str or chat_id_str not in allowed:
            continue

        cmd = _normalize_command(raw_text)
        if cmd and (chat_id_str, cmd) not in seen_pairs:
            seen_pairs.add((chat_id_str, cmd))
            pending.append((chat_id_str, cmd))

    return pending, max_update_id


def send_telegram_message(
    text: str,
    chat_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Отправляет сообщение указанным чатам или всем получателям из TELEGRAM_CHAT_IDS."""
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram: TELEGRAM_BOT_TOKEN не задан, отправка пропущена.")
        return {"sent": 0, "failed": 0, "skipped": True}

    targets = chat_ids if chat_ids is not None else TELEGRAM_CHAT_IDS
    if not targets:
        print("Telegram: список получателей пуст, отправка пропущена.")
        return {"sent": 0, "failed": 0, "skipped": True}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    sent = 0
    failed = 0

    for chat_id in targets:
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": REPLY_KEYBOARD,
                },
                timeout=TELEGRAM_TIMEOUT,
            )
            if response.ok:
                sent += 1
            else:
                failed += 1
                print(
                    f"Ошибка Telegram для {chat_id}: "
                    f"HTTP {response.status_code} {response.text}"
                )
        except Exception as error:
            failed += 1
            print(f"Исключение при отправке в Telegram ({chat_id}): {error}")

    print(f"Telegram рассылка: отправлено={sent}, ошибок={failed}")
    return {"sent": sent, "failed": failed, "skipped": False}


def format_prediction_message(
    prediction: dict[str, Any],
    changes: list[dict[str, Any]],
    trigger_reason: str,
) -> str:
    """Формирует аналитическую сводку для Telegram."""
    level = prediction.get("signal_level", "calm")
    header = LEVEL_TITLES.get(level, "DOTA 2 PATCH PREDICTOR")
    probs = prediction["probabilities"]
    feat = prediction["features"]
    eta_summary = prediction.get("eta", {}).get("summary", "")

    lines: list[str] = [
        f"<b>{html.escape(header)} (цель: {html.escape(TARGET_PATCH)})</b>",
        f"🕒 Время (Минск): <b>{html.escape(prediction['evaluated_at_minsk'])}</b>",
        f"📌 Повод: {html.escape(trigger_reason)}",
    ]

    if changes:
        lines.append("")
        lines.append("<b>Изменения веток:</b>")
        for ch in changes:
            b = html.escape(str(ch["branch"]).upper())
            old_v = html.escape(str(ch["old_version"]))
            new_v = html.escape(str(ch["new_version"]))
            delta_str = f" (+{ch['delta']})" if ch.get("delta") else ""
            lines.append(f"• {b}: <code>{old_v} → {new_v}</code>{delta_str}")

    lines.extend(
        [
            "",
            f"<b>Вероятность выхода {html.escape(TARGET_PATCH)}:</b>",
            f"• 15 мин: <b>{probs[15]}%</b>",
            f"• 30 мин: <b>{probs[30]}%</b>",
            f"• 1 час: <b>{probs[60]}%</b>",
            f"• 2 часа: <b>{probs[120]}%</b>",
            f"• 3 часа: <b>{probs[180]}%</b>",
            f"📊 Индекс готовности: <b>{prediction['readiness_pct']}%</b>",
        ]
    )

    if eta_summary:
        lines.extend(
            [
                "",
                "<b>⏳ Ориентир времени выхода (по архиву):</b>",
                html.escape(eta_summary),
            ]
        )

    reasons = prediction.get("reasons", [])
    if reasons:
        lines.append("")
        lines.append("<b>Почему такая оценка:</b>")
        for r in reasons[:4]:
            lines.append(f"• {html.escape(r)}")

    missing = prediction.get("missing", [])
    if missing and level != "critical":
        lines.append("")
        lines.append("<b>Чего пока не хватает до пика:</b>")
        for m in missing[:2]:
            lines.append(f"• {html.escape(m)}")

    similar = prediction.get("similar_patches", [])
    if similar:
        sim_parts = [
            f"{s['patch']} ({s['similarity_pct']}%, вышел в {s.get('release_time_minsk', '??:??')})"
            for s in similar[:2]
        ]
        lines.extend(
            [
                "",
                "<b>Похожие прошлые патчи:</b> " + html.escape(", ".join(sim_parts)),
            ]
        )

    lines.extend(
        [
            "",
            f"<i>Staging за 2ч/6ч/24ч/7д: {feat['st_2h']}/{feat['st_6h']}/{feat['st_24h']}/{feat['st_7d']}</i>",
        ]
    )
    return "\n".join(lines)


def format_status_message(
    prediction: dict[str, Any],
    curr_branches: dict[str, dict[str, Any]],
    steam_versions: dict[str, int | None],
    now_utc: datetime,
) -> str:
    """Формирует полный актуальный статус патча и веток по запросу пользователя."""
    level = prediction.get("signal_level", "calm")
    level_ru = LEVEL_LABELS_RU.get(level, level)
    probs = prediction["probabilities"]
    feat = prediction["features"]
    eta_summary = prediction.get("eta", {}).get("summary", "")

    lines: list[str] = [
        f"📊 <b>АКТУАЛЬНЫЙ СТАТУС (цель: {html.escape(TARGET_PATCH)})</b>",
        f"🕒 Минск: <b>{html.escape(prediction['evaluated_at_minsk'])}</b>",
        f"🚦 Уровень: <b>{html.escape(level_ru)}</b> | Готовность: <b>{prediction['readiness_pct']}%</b>",
        "",
        f"<b>Вероятность выхода {html.escape(TARGET_PATCH)}:</b>",
        f"• 15м / 30м / 1ч: <b>{probs[15]}% / {probs[30]}% / {probs[60]}%</b>",
        f"• 2ч / 3ч: <b>{probs[120]}% / {probs[180]}%</b>",
        "",
        "<b>Текущие версии веток:</b>",
    ]

    for branch_key, label in (("staging", "STAGING"), ("public", "PUBLIC"), ("experimental", "EXP")):
        b = curr_branches.get(branch_key)
        if not b:
            continue
        ver = html.escape(str(b.get("client_version", "?")))
        age = html.escape(_format_branch_age(b.get("last_change_at"), now_utc))
        c24 = b.get("changes_24h", 0)
        c7d = b.get("changes_7d", 0)
        lines.append(f"• {label}: <code>{ver}</code> — {age} ({c24}/24ч, {c7d}/7д)")

    steam_pub = steam_versions.get("public")
    if steam_pub is not None:
        lines.append(f"• Steam API Public: <code>{steam_pub}</code>")

    if eta_summary:
        lines.extend(
            [
                "",
                "<b>⏳ Ориентир окна выхода:</b>",
                html.escape(eta_summary),
            ]
        )

    reasons = prediction.get("reasons", [])
    if reasons:
        lines.append("")
        lines.append("<b>Активные факторы:</b>")
        for r in reasons[:3]:
            lines.append(f"• {html.escape(r)}")

    missing = prediction.get("missing", [])
    if missing and level != "critical":
        lines.append("")
        lines.append("<b>До пикового сигнала не хватает:</b>")
        for m in missing[:2]:
            lines.append(f"• {html.escape(m)}")

    lines.extend(
        [
            "",
            f"<i>Staging за 2ч/6ч/24ч/7д: {feat['st_2h']}/{feat['st_6h']}/{feat['st_24h']}/{feat['st_7d']}</i>",
        ]
    )
    return "\n".join(lines)


def format_builds_message(
    curr_branches: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    steam_versions: dict[str, int | None],
    now_utc: datetime,
) -> str:
    """Показывает детальные версии веток и ленту последних обновлений билдов."""
    minsk_str = now_utc.astimezone(MINSK_TZ).strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        "🔧 <b>ВЕРСИИ ВЕТОК И ПОСЛЕДНИЕ БИЛДЫ</b>",
        f"🕒 Проверено (Минск): <b>{html.escape(minsk_str)}</b>",
        "",
    ]

    for branch_key, label in (
        ("staging", "STAGING"),
        ("public", "PUBLIC"),
        ("experimental", "EXPERIMENTAL"),
    ):
        b = curr_branches.get(branch_key)
        if not b:
            continue
        c_ver = html.escape(str(b.get("client_version", "?")))
        s_ver = html.escape(str(b.get("server_deploy_version", "?")))
        age = html.escape(_format_branch_age(b.get("last_change_at"), now_utc))
        c24 = b.get("changes_24h", 0)
        c7d = b.get("changes_7d", 0)
        lines.append(
            f"• <b>{label}</b>: client <code>{c_ver}</code> | server <code>{s_ver}</code>\n"
            f"  Изм.: {age} (24ч: {c24}, 7д: {c7d})"
        )

    steam_pub = steam_versions.get("public")
    if steam_pub is not None:
        lines.append(f"• Steam UpToDateCheck: <code>{steam_pub}</code>")

    recent = list(reversed(events[-8:])) if events else []
    if recent:
        lines.extend(["", "<b>Последние 8 смен билдов:</b>"])
        for ev in recent:
            dt_minsk = ev["dt"].astimezone(MINSK_TZ).strftime("%d.%m %H:%M")
            b_name = str(ev["branch"]).upper()
            old_v = ev.get("from", "?")
            new_v = ev.get("to", "?")
            delta = ev.get("delta", 0)
            delta_str = f" (+{delta})" if delta else ""
            lines.append(
                f"• <code>{dt_minsk}</code> {html.escape(b_name)}: "
                f"<code>{html.escape(str(old_v))} → {html.escape(str(new_v))}</code>{delta_str}"
            )

    return "\n".join(lines)


def format_history_message(prediction: dict[str, Any]) -> str:
    """Показывает окно выхода по архиву и наиболее похожие прошлые патчи."""
    feat = prediction["features"]
    eta_summary = prediction.get("eta", {}).get("summary", "Нет данных")
    days_since = feat.get("days_since_major")

    lines: list[str] = [
        f"📈 <b>ИСТОРИЯ И ОКНО ВЫХОДА (цель: {html.escape(TARGET_PATCH)})</b>",
        f"🕒 Минск: <b>{html.escape(prediction['evaluated_at_minsk'])}</b>",
        f"📊 Текущая готовность: <b>{prediction['readiness_pct']}%</b>",
    ]
    if days_since is not None:
        lines.append(f"📅 Дней с прошлого крупного патча: <b>{days_since:.1f}</b>")

    lines.extend(
        [
            "",
            "<b>⏳ Статистика окна Valve:</b>",
            "• 7 из 7 последних крупных патчей вышли в <b>01:08–03:52 Минск</b>",
            f"• {html.escape(eta_summary)}",
        ]
    )

    similar = prediction.get("similar_patches", [])
    if similar:
        lines.extend(["", "<b>Наиболее похожие прошлые патчи:</b>"])
        for s in similar[:4]:
            r_time = s.get("release_time_minsk", "??:??")
            lines.append(
                f"• <b>{html.escape(str(s['patch']))}</b> — сходство {s['similarity_pct']}% "
                f"(вышел в {html.escape(str(r_time))} Минск)"
            )

    return "\n".join(lines)


def format_help_message() -> str:
    """Справка по командам и кнопкам бота."""
    return "\n".join(
        [
            f"🤖 <b>Dota 2 Patch Predictor (цель: {html.escape(TARGET_PATCH)})</b>",
            "",
            "Используйте кнопки под строкой ввода или команды:",
            "• /status — вероятности выхода, индекс готовности и версии",
            "• /builds — подробные билды веток и последние 8 обновлений",
            "• /history — окно релиза и похожие прошлые патчи",
            "• /help — эта справка",
            "",
            "<i>Бот работает без платного сервера через GitHub Actions: "
            "ответ на нажатие кнопки приходит на ближайшем цикле проверки (обычно 1–5 минут). "
            "А при резком всплеске Staging или выходе патча бот напишет сам!</i>",
        ]
    )


def format_daily_pulse_message(
    prediction: dict[str, Any],
    steam_versions: dict[str, int | None],
) -> str:
    """Короткая вечерняя сводка в 21:00 Минск, подтверждающая, что монитор работает."""
    feat = prediction["features"]
    eta_summary = prediction.get("eta", {}).get("summary", "")
    pub_v = steam_versions.get("public")
    pub_note = f" | Steam Public: <code>{pub_v}</code>" if pub_v else ""
    lines = [
        f"🌙 <b>ВЕЧЕРНИЙ ПУЛЬС МОНИТОРА (цель: {html.escape(TARGET_PATCH)})</b>",
        f"🕒 Время (Минск): <b>{html.escape(prediction['evaluated_at_minsk'])}</b>",
        f"📊 Индекс готовности к ночи: <b>{prediction['readiness_pct']}%</b>{pub_note}",
        f"• Staging за 24ч / 7д: <b>{feat['st_24h']} / {feat['st_7d']}</b> смен",
    ]
    if eta_summary:
        lines.append(f"• Ориентир: {html.escape(eta_summary)}")
    lines.append("")
    lines.append("<i>Монитор активен. Если ночью начнется серия Staging — придет срочный сигнал.</i>")
    return "\n".join(lines)


def format_release_message(
    patch_name: str,
    evaluated_minsk: str,
    public_change: dict[str, Any] | None,
    confirmed_via_patches: bool,
    announcement: dict[str, Any] | None = None,
) -> str:
    """Формирует сообщение о выходе патча со ссылкой на пост Valve (если доступен)."""
    lines = [
        f"🚨 <b>ВЕРОЯТНО, ПАТЧ {html.escape(patch_name)} УЖЕ ВЫШЕЛ!</b>",
        f"🕒 Время (Минск): <b>{html.escape(evaluated_minsk)}</b>",
    ]
    if public_change:
        lines.append(
            f"• PUBLIC: <code>{html.escape(str(public_change['old_version']))} → "
            f"{html.escape(str(public_change['new_version']))}</code>"
        )
    if confirmed_via_patches:
        lines.append("• Подтверждено официальным списком / новостями Steam.")
    else:
        lines.append("• Обновился Public-сервер на фоне высокой активности Staging.")
    if announcement and announcement.get("url"):
        title = html.escape(str(announcement.get("title", "Патчноут в Steam")))
        url = html.escape(str(announcement["url"]), quote=True)
        lines.append(f'• 📰 <a href="{url}">{title}</a>')
    lines.extend(
        [
            "",
            f"Прогнозные уведомления по {html.escape(patch_name)} остановлены до следующей цели.",
        ]
    )
    return "\n".join(lines)
