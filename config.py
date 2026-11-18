import os
from pathlib import Path

# Настройки Telegram (берутся из GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [
    chat_id.strip()
    for chat_id in os.getenv("TELEGRAM_CHAT_IDS", "").split(",")
    if chat_id.strip()
]

# Источники данных Finargot
HISTORY_URL = "https://finargot.ru/updates/data/versions-history.json"
LATEST_URL = "https://finargot.ru/updates/data/versions.json"

# Целевой патч и часовой пояс для уведомлений
TARGET_PATCH = os.getenv("TARGET_PATCH", "7.42").strip()
TIMEZONE_NAME = "Europe/Minsk"

# Горизонты краткосрочного прогноза (в минутах)
PREDICTION_HORIZONS_MINUTES = (15, 30, 60, 120, 180)

# Параметры HTTP-запросов
REQUEST_TIMEOUT_SECONDS = 30
HTTP_HEADERS = {
    "User-Agent": "Dota2PatchPredictor/2.0"
}

# Локальные пути к базе проекта
DATA_DIR = Path("data")
OBSERVATIONS_FILE = DATA_DIR / "observations.jsonl"
EVENTS_FILE = DATA_DIR / "events.jsonl"
STATE_FILE = DATA_DIR / "state.json"
PATCHES_CATALOG_FILE = DATA_DIR / "patches_catalog.json"
