import html
from typing import Any

import requests

from config import TARGET_PATCH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS

TELEGRAM_TIMEOUT = 15

LEVEL_TITLES = {
    "critical": "🔥 КРИТИЧЕСКИЙ СИГНАЛ ПАТЧА",
    "high": "🚨 ВЫСОКАЯ АКТИВНОСТЬ STAGING",
    "elevated": "⚡ ОБНОВЛЕНИЕ STAGING",
    "calm": "ℹ️ МОНИТОРИНГ STAGING",
}


def send_telegram_message(text: str) -> dict[str, Any]:
    """Отправляет сообщение всем получателям из TELEGRAM_CHAT_IDS."""
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram: TELEGRAM_BOT_TOKEN не задан, отправка пропущена.")
        return {"sent": 0, "failed": 0, "skipped": True}

    if not TELEGRAM_CHAT_IDS:
        print("Telegram: TELEGRAM_CHAT_IDS пуст, отправка пропущена.")
        return {"sent": 0, "failed": 0, "skipped": True}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    sent = 0
    failed = 0

    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
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
            f"{s['patch']} ({s['similarity_pct']}%, вышел в {s['release_time_minsk']})"
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


def format_release_message(
    patch_name: str,
    evaluated_minsk: str,
    public_change: dict[str, Any] | None,
    confirmed_via_patches: bool,
) -> str:
    """Формирует сообщение о выходе патча."""
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
        lines.append("• Подтверждено в списке официальных патчей.")
    else:
        lines.append("• Обновился Public-сервер на фоне высокой активности Staging.")

    lines.extend(
        [
            "",
            f"Прогнозные уведомления по {html.escape(patch_name)} остановлены до следующей цели.",
        ]
    )
    return "\n".join(lines)
