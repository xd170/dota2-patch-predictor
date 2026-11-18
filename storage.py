import json
from typing import Any

from config import DATA_DIR, OBSERVATIONS_FILE, STATE_FILE

DEFAULT_STATE: dict[str, Any] = {
    "last_branches": {},
    "last_notified_at_utc": None,
    "last_notified_p180": 0.0,
    "last_notified_level": "calm",
    "last_notified_staging_version": None,
    "public_release_alerted_version": None,
    "known_patches": [],
    "target_patch_released": False,
}


def load_state() -> dict[str, Any]:
    """Загружает сохраненное состояние между запусками GitHub Actions."""
    if not STATE_FILE.exists():
        return dict(DEFAULT_STATE)

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except Exception as error:
        print(f"Предупреждение: не удалось прочитать {STATE_FILE}: {error}")
        return dict(DEFAULT_STATE)


def save_state(state: dict[str, Any]) -> None:
    """Сохраняет актуальное состояние в data/state.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_branch_snapshot(latest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Преобразует список веток из versions.json в словарь по имени ветки."""
    snapshot: dict[str, dict[str, Any]] = {}
    for item in latest.get("branches", []):
        name = str(item.get("branch", "")).lower().strip()
        if not name:
            continue
        snapshot[name] = {
            "client_version": item.get("client_version"),
            "server_deploy_version": item.get("server_deploy_version"),
            "last_change_at": item.get("last_change_at"),
            "changes_24h": item.get("changes_24h"),
            "changes_7d": item.get("changes_7d"),
        }
    return snapshot


def detect_branch_changes(
    prev_branches: dict[str, dict[str, Any]],
    curr_branches: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Возвращает список веток, у которых изменилась client_version."""
    if not prev_branches:
        return []

    changes: list[dict[str, Any]] = []
    for branch_name in ("staging", "public", "experimental"):
        prev = prev_branches.get(branch_name)
        curr = curr_branches.get(branch_name)
        if not prev or not curr:
            continue

        old_v = prev.get("client_version")
        new_v = curr.get("client_version")
        if old_v is not None and new_v is not None and old_v != new_v:
            try:
                delta = int(new_v) - int(old_v)
            except (TypeError, ValueError):
                delta = 0
            changes.append(
                {
                    "branch": branch_name,
                    "old_version": old_v,
                    "new_version": new_v,
                    "delta": delta,
                }
            )
    return changes


def append_observation(
    collected_at: str,
    latest: dict[str, Any],
    curr_branches: dict[str, dict[str, Any]],
    prediction: dict[str, Any],
) -> None:
    """Дописывает компактное наблюдение в data/observations.jsonl."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "collected_at": collected_at,
        "generated_at": latest.get("generated_at"),
        "last_successful_poll": latest.get("last_successful_poll"),
        "branches": curr_branches,
        "signal_level": prediction.get("signal_level"),
        "readiness_pct": prediction.get("readiness_pct"),
        "probabilities": prediction.get("probabilities"),
    }
    with OBSERVATIONS_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
