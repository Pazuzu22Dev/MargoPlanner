import re
from datetime import datetime
from difflib import SequenceMatcher

from services.action_formatter import format_calendar_action
from services.calendar_service import search_events


def _normalized_title(value):
    return " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())


def _same_event(existing, new):
    existing_title = _normalized_title(existing["title"])
    new_title = _normalized_title(new["title"])
    titles_match = (
        existing_title == new_title
        or SequenceMatcher(None, existing_title, new_title).ratio() >= 0.88
    )
    return (
        titles_match
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


def format_execution_report(summary, statuses=None):
    labels = {
        "created": "Создано",
        "skipped": "Пропущено",
        "replaced": "Заменено",
        "cancelled": "Отменено",
    }
    selected = statuses or tuple(labels)
    sections = []
    for status in selected:
        entries = [item for item in summary.get("details", []) if item["status"] == status]
        if not entries:
            continue
        lines = []
        for entry in entries:
            action = entry["action"]
            data = action["data"]
            if action["action"] in {"create_calendar_event", "update_calendar_event"}:
                rendered = format_calendar_action(data).replace("\n", " · ")
            else:
                rendered = data.get("text") or data.get("title") or action["action"]
            lines.append(f"• {rendered} — {entry['reason']}")
        sections.append(labels[status] + ":\n" + "\n".join(lines))
    return "\n\n".join(sections) or "В этой категории ничего нет."


def batch_followup_response(user_text, summary):
    normalized = user_text.casefold()
    status = None
    if "пропущ" in normalized:
        status = "skipped"
    elif "замен" in normalized:
        status = "replaced"
    elif "создан" in normalized or "добавлен" in normalized:
        status = "created"
    elif "отмен" in normalized:
        status = "cancelled"
    if status is None:
        return None
    return format_execution_report(summary, (status,))


def find_retry_action(user_text, summary):
    normalized = user_text.casefold().replace("ё", "е")
    if not any(word in normalized for word in ("создай", "добавь", "поставь")):
        return None
    number_match = re.search(r"\b(\d+)\b", normalized)
    number = int(number_match.group(1)) if number_match else None
    if number is None:
        ordinals = {
            "перв": 1,
            "втор": 2,
            "трет": 3,
            "четверт": 4,
            "пят": 5,
            "шест": 6,
            "седьм": 7,
            "восьм": 8,
            "девят": 9,
            "десят": 10,
        }
        number = next(
            (value for stem, value in ordinals.items() if stem in normalized),
            None,
        )
    if number is None:
        return None
    for detail in summary.get("details", []):
        if detail.get("index") == number - 1:
            return detail.get("action")
    return None
