import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from config import (
    DATA_DIR,
    HISTORY_URL,
    HTTP_HEADERS,
    LATEST_URL,
    REQUEST_TIMEOUT_SECONDS,
)


def fetch_json(url: str) -> Any:
    """Загружает JSON по указанному URL и проверяет корректность ответа."""
    response = requests.get(
        url,
        headers=HTTP_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as error:
        raise ValueError(f"Источник вернул некорректный JSON: {url}") from error


def save_json_file(path: Path, data: Any) -> None:
    """Сохраняет JSON в файл с красивым форматированием."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def collect_all(save_raw: bool = False) -> dict[str, Any]:
    """Собирает текущие данные и исторический архив Finargot."""
    collected_at = datetime.now(timezone.utc).isoformat()
    latest = fetch_json(LATEST_URL)
    history = fetch_json(HISTORY_URL)

    if save_raw:
        save_json_file(DATA_DIR / "raw_latest.json", latest)
        save_json_file(DATA_DIR / "raw_history.json", history)

    return {
        "collected_at": collected_at,
        "latest": latest,
        "history": history,
    }


if __name__ == "__main__":
    result = collect_all(save_raw=True)
    print(f"Сбор завершен успешно. Время UTC: {result['collected_at']}")
