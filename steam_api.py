import re
from datetime import datetime, timezone
from typing import Any

import requests

from config import HTTP_HEADERS

STEAM_GC_VERSION_URL = "https://api.steampowered.com/IGCVersion_{appid}/GetServerVersion/v1/"
STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_TIMEOUT = 10

BRANCH_APPIDS = {
    "public": 570,
    "staging": 2305270,
    "experimental": 247040,
}


def check_steam_versions_detailed() -> dict[str, dict[str, int | None]]:
    """
    Напрямую запрашивает у Steam Web API (IGCVersion) deploy_version и active_version
    для public, staging и experimental веток Dota 2.
    """
    details: dict[str, dict[str, int | None]] = {}
    for branch, appid in BRANCH_APPIDS.items():
        url = STEAM_GC_VERSION_URL.format(appid=appid)
        try:
            response = requests.get(
                url,
                params={"format": "json"},
                headers=HTTP_HEADERS,
                timeout=STEAM_TIMEOUT,
            )
            if not response.ok:
                details[branch] = {"deploy_version": None, "active_version": None}
                continue
            result = response.json().get("result", {})
            if result.get("success"):
                deploy_v = result.get("deploy_version")
                active_v = result.get("active_version")
                details[branch] = {
                    "deploy_version": int(deploy_v) if deploy_v is not None else None,
                    "active_version": int(active_v) if active_v is not None else None,
                }
            else:
                details[branch] = {"deploy_version": None, "active_version": None}
        except Exception:
            details[branch] = {"deploy_version": None, "active_version": None}
    return details


def check_steam_versions() -> dict[str, int | None]:
    """
    Возвращает актуальный номер билда по каждой ветке напрямую от серверов Valve.
    Используется deploy_version (она обновляется первой при начале выкатки).
    """
    details = check_steam_versions_detailed()
    versions: dict[str, int | None] = {}
    for branch, info in details.items():
        dv = info.get("deploy_version")
        av = info.get("active_version")
        if dv is not None and av is not None:
            versions[branch] = max(dv, av)
        else:
            versions[branch] = dv if dv is not None else av
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
