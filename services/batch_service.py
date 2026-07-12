from datetime import datetime

from services.action_formatter import format_calendar_action
from services.calendar_service import search_events


def _same_event(existing, new):
    return (
        existing["title"].casefold() == new["title"].casefold()
        and datetime.fromisoformat(existing["start_time"])
        == datetime.fromisoformat(new["start_time"])
        and datetime.fromisoformat(existing["end_time"])
        == datetime.fromisoformat(new["end_time"])
    )


def analyze_batch(plan):
    analysis = []
    for index, item in enumerate(plan["actions"]):
        entry = {
            "action_index": index,
            "classification": "free",
            "existing": [],
            "decision": "create",
        }
        if item["action"] == "create_calendar_event":
            data = item["data"]
            existing = search_events({
                "text": "",
                "time_min": data["start_time"],
                "time_max": data["end_time"],
            })
            exact = [event for event in existing if _same_event(event, data)]
            if exact:
                entry.update(
                    classification="exact_duplicate",
                    existing=exact,
                    decision="skip",
                )
            elif existing:
                entry.update(
                    classification="conflict",
                    existing=existing,
                    decision=None,
                )
        analysis.append(entry)
    return analysis


def batch_counts(analysis):
    return {
        "total": len(analysis),
        "free": sum(item["classification"] == "free" for item in analysis),
        "duplicates": sum(item["classification"] == "exact_duplicate" for item in analysis),
        "conflicts": sum(item["classification"] == "conflict" for item in analysis),
        "remaining": sum(item["classification"] == "conflict" and item["decision"] is None for item in analysis),
    }


def format_batch_report(analysis):
    counts = batch_counts(analysis)
    return (
        f"Всего элементов: {counts['total']}\n"
        f"✅ Свободно: {counts['free']}\n"
        f"🟰 Точных дублей: {counts['duplicates']}\n"
        f"⚠️ Конфликтов: {counts['conflicts']}"
    )


def format_conflict(plan, entry):
    new_data = plan["actions"][entry["action_index"]]["data"]
    existing_blocks = []
    for event in entry["existing"]:
        existing_blocks.append(format_calendar_action(event))
    return (
        "⚠️ Одинаковое или пересекающееся время\n\n"
        "Уже в календаре:\n"
        + "\n\n".join(existing_blocks)
        + "\n\nНовое событие:\n"
        + format_calendar_action(new_data)
    )
