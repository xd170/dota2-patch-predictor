import re
from datetime import datetime, timezone
from typing import Any

import requests

from config import HTTP_HEADERS

STEAM_UP_TO_DATE_URL = "https://api.steampowered.com/ISteamApps/UpToDateCheck/v1/"
STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_TIMEOUT = 10

BRANCH_APPIDS = {
    "public": 570,
    "staging": 2305270,
    "experimental": 247040,
}


def check_steam_versions() -> dict[str, int | None]:
    """
    Напрямую запрашивает у Steam Web API актуальный номер версии (required_version).
    Если ветка закрыта от публичного UpToDateCheck, безопасно возвращает None.
    """
    versions: dict[str, int | None] = {}
    for branch, appid in BRANCH_APPIDS.items():
        try:
            response = requests.get(
                STEAM_UP_TO_DATE_URL,
                params={"appid": appid, "version": 0, "format": "json"},
                headers=HTTP_HEADERS,
                timeout=STEAM_TIMEOUT,
            )
            if not response.ok:
                versions[branch] = None
                continue
            payload = response.json().get("response", {})
            if payload.get("success") and payload.get("required_version") is not None:
                versions[branch] = int(payload["required_version"])
            else:
                versions[branch] = None
        except Exception:
            versions[branch] = None
    return versions


def find_steam_patch_announcement(target_patch: str) -> dict[str, Any] | None:
    """
    Проверяет официальные новости Dota 2 в Steam на наличие анонса целевого патча.
    """
    pattern = re.compile(
        rf"(?<![\d.]){re.escape(target_patch.strip())}(?![\da-zA-Z])"
    )
    try:
        response = requests.get(
            STEAM_NEWS_URL,
            params={
                "appid": 570,
                "count": 5,
                "maxlength": 300,
                "feeds": "steam_announcements",
            },
            headers=HTTP_HEADERS,
            timeout=STEAM_TIMEOUT,
        )
        if not response.ok:
            return None

        items = response.json().get("appnews", {}).get("newsitems", [])
        for item in items:
            title = str(item.get("title", ""))
            contents = str(item.get("contents", ""))
            if pattern.search(title) or pattern.search(contents):
                ts = item.get("date")
                date_utc = (
                    datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                    if ts
                    else None
                )
                return {
                    "title": title,
                    "url": item.get("url"),
                    "date_utc": date_utc,
                }
    except Exception as error:
        print(f"Предупреждение Steam News API: {error}")
    return None
