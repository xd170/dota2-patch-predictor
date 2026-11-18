import json
import time
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

HISTORY_CACHE_FILE = DATA_DIR / "history_archive.json"
HISTORY_CACHE_TTL_SECONDS = 24 * 3600


def fetch_json(url: str, retries: int = 2) -> Any:
    """Загружает JSON по указанному URL с повторными попытками при сбоях сети."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                headers=HTTP_HEADERS,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.JSONDecodeError as error:
            raise ValueError(f"Источник вернул некорректный JSON: {url}") from error
        except Exception as error:
            last_error = error
            if attempt < retries:
                time.sleep(3)
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}") from last_error


def save_json_file(path: Path, data: Any) -> None:
    """Сохраняет JSON в файл."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_or_refresh_history(force_refresh: bool = False) -> dict[str, Any]:
    """
    Возвращает 3-летний архив событий из локального кэша (обновляется раз в 24 часа).
    При временном сбое сети использует сохраненный кэш.
    """
    cached_data: dict[str, Any] | None = None
    cache_fresh = False

    if HISTORY_CACHE_FILE.exists():
        try:
            cached = json.loads(HISTORY_CACHE_FILE.read_text(encoding="utf-8"))
            cached_data = cached.get("payload")
            cached_at = cached.get("cached_at_ts", 0)
            if cached_data and (time.time() - float(cached_at)) < HISTORY_CACHE_TTL_SECONDS:
                cache_fresh = True
        except Exception as error:
            print(f"Предупреждение чтения кэша архива: {error}")

    if cache_fresh and not force_refresh and cached_data is not None:
        return cached_data

    try:
        fresh_history = fetch_json(HISTORY_URL)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_CACHE_FILE.write_text(
            json.dumps(
                {"cached_at_ts": time.time(), "payload": fresh_history},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print("Архив Finargot обновлен и сохранен в кэш.")
        return fresh_history
    except Exception as error:
        if cached_data is not None:
            print(f"Сбой загрузки архива ({error}), используем локальный кэш.")
            return cached_data
        raise


def collect_all(save_raw: bool = False) -> dict[str, Any]:
    """Собирает текущие данные и исторический архив Finargot."""
    collected_at = datetime.now(timezone.utc).isoformat()

    latest: dict[str, Any] | None = None
    latest_error: str | None = None
    try:
        latest = fetch_json(LATEST_URL)
    except Exception as error:
        latest_error = str(error)
        print(f"Предупреждение: источник Finargot временно недоступен: {error}")

    history = load_or_refresh_history()

    if save_raw and latest is not None:
        save_json_file(DATA_DIR / "raw_latest.json", latest)
        save_json_file(DATA_DIR / "raw_history.json", history)

    return {
        "collected_at": collected_at,
        "latest": latest,
        "latest_error": latest_error,
        "history": history,
    }
