import json
from collections import Counter
from typing import Any

from collector import fetch_json
from config import HISTORY_URL, LATEST_URL


def print_section(title: str) -> None:
    print(f"\n{'=' * 20} {title} {'=' * 20}")


def summarize_event_list(title: str, events: Any) -> None:
    print(f"\n--- {title} ---")
    if not isinstance(events, list):
        print(f"Не список, тип: {type(events).__name__}")
        if isinstance(events, dict):
            print(f"Ключи словаря: {list(events.keys())}")
        return

    print(f"Всего записей: {len(events)}")
    if not events:
        return

    first = events[0]
    if isinstance(first, dict):
        print(f"Поля записи: {list(first.keys())}")
        branches = Counter(str(item.get("branch", "UNKNOWN")) for item in events)
        print(f"По веткам: {dict(branches)}")

        time_key = next(
            (key for key in ("seen_at", "timestamp", "at", "date", "created_at") if key in first),
            None,
        )
        if time_key:
            times = [str(item[time_key]) for item in events if item.get(time_key)]
            if times:
                print(f"Самая ранняя метка ({time_key}): {min(times)}")
                print(f"Самая поздняя метка ({time_key}): {max(times)}")

    print("\nПервые 3 записи:")
    for item in events[:3]:
        print(json.dumps(item, ensure_ascii=False))

    print("\nПоследние 3 записи:")
    for item in events[-3:]:
        print(json.dumps(item, ensure_ascii=False))


def inspect_history_source(history: Any) -> None:
    print_section("ИСТОЧНИК 1: versions-history.json")
    if not isinstance(history, dict):
        print(f"Неожиданный тип данных: {type(history).__name__}")
        return

    print(f"Ключи верхнего уровня: {list(history.keys())}")
    print(f"generated_at: {history.get('generated_at')}")
    print(f"note: {history.get('note')}")
    summarize_event_list("history['events']", history.get("events"))


def inspect_latest_source(latest: Any) -> None:
    print_section("ИСТОЧНИК 2: versions.json")
    if not isinstance(latest, dict):
        print(f"Неожиданный тип данных: {type(latest).__name__}")
        return

    print(f"Ключи верхнего уровня: {list(latest.keys())}")
    for meta_key in (
        "schema_version",
        "revision",
        "generated_at",
        "last_successful_poll",
        "content_modified",
        "events_note",
        "note",
    ):
        if meta_key in latest:
            print(f"{meta_key}: {latest.get(meta_key)}")

    print("\n--- ВЕТКИ (branches) ---")
    print(json.dumps(latest.get("branches", []), ensure_ascii=False, indent=2))

    print("\n--- АКТИВНОСТЬ (activity) ---")
    print(json.dumps(latest.get("activity", {}), ensure_ascii=False, indent=2))

    print("\n--- ЭПИЗОДЫ (episodes) ---")
    print(json.dumps(latest.get("episodes", {}), ensure_ascii=False, indent=2))

    print("\n--- СИГНАЛЫ (signals) ---")
    print(json.dumps(latest.get("signals", []), ensure_ascii=False, indent=2))

    for list_key in ("history", "events", "archive"):
        if list_key in latest:
            value = latest.get(list_key)
            if isinstance(value, dict):
                print(f"\n--- latest['{list_key}'] (dict) ---")
                print(f"Ключи: {list(value.keys())}")
                for sub_key, sub_val in value.items():
                    if isinstance(sub_val, list):
                        summarize_event_list(f"latest['{list_key}']['{sub_key}']", sub_val)
                    else:
                        print(f"  {sub_key}: {sub_val}")
            else:
                summarize_event_list(f"latest['{list_key}']", value)

    print_section("ВСЕ ПАТЧИ (patches)")
    patches = latest.get("patches", [])
    print(f"Всего патчей в списке: {len(patches)}")

    mappings = Counter(str(p.get("mapping", "NONE")) for p in patches if isinstance(p, dict))
    print(f"Распределение mapping: {dict(mappings)}")

    for index, patch in enumerate(patches, start=1):
        print(f"\n--- Патч #{index} ---")
        print(json.dumps(patch, ensure_ascii=False, indent=2))


def main() -> None:
    print("Загрузка данных с Finargot...")
    history = fetch_json(HISTORY_URL)
    latest = fetch_json(LATEST_URL)

    inspect_history_source(history)
    inspect_latest_source(latest)
    print("\n========== РАЗВЕДКА ЗАВЕРШЕНА ==========")


if __name__ == "__main__":
    main()
