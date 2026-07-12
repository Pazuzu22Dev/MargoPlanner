from datetime import datetime
from uuid import uuid4

from services.calendar_service import (
    create_events,
    delete_events,
    search_events,
    update_event,
)
from services.action_formatter import format_plan


def _event_from_data(data):
    return {
        "title": data["title"],
        "start_time": data["start_time"],
        "end_time": data["end_time"],
        "description": data.get("description", ""),
        "location": data.get("location", ""),
        "links": data.get("links", []),
        "contacts": data.get("contacts", []),
        "attendees": data.get("attendees", []),
    }


def find_duplicate_actions(plan):
    duplicates = []
    for index, item in enumerate(plan["actions"]):
        if item["action"] != "create_calendar_event":
            continue
        data = item["data"]
        candidates = search_events({
            "text": data["title"],
            "time_min": data["start_time"],
            "time_max": data["end_time"],
        })
        exact = [
            event for event in candidates
            if event["title"].casefold() == data["title"].casefold()
            and datetime.fromisoformat(event["start_time"])
            == datetime.fromisoformat(data["start_time"])
            and datetime.fromisoformat(event["end_time"])
            == datetime.fromisoformat(data["end_time"])
        ]
        if exact:
            duplicates.append({"action_index": index, "events": exact})
    return duplicates


def execute_plan(plan, user_id, reminder_store, skipped_indexes=None):
    skipped = set(skipped_indexes or [])
    calendar_creates = []
    results = []
    for index, item in enumerate(plan["actions"]):
        if index in skipped:
            results.append({"index": index, "status": "skipped_duplicate"})
            continue
        action = item["action"]
        data = item["data"]
        if action == "create_calendar_event":
            calendar_creates.append((index, _event_from_data(data)))
        elif action == "update_calendar_event":
            update_event(data["id"], _event_from_data(data))
            results.append({"index": index, "status": "done"})
        elif action == "delete_calendar_event":
            delete_events(data.get("ids") or [data["id"]])
            results.append({"index": index, "status": "done"})
        elif action == "create_reminder":
            reminder_store.create(user_id, data["text"], data["remind_at"])
            results.append({"index": index, "status": "done"})
        elif action == "update_reminder":
            reminder_store.update_pending(
                user_id, data["id"], data["text"], data["remind_at"]
            )
            results.append({"index": index, "status": "done"})
        elif action == "delete_reminder":
            reminder_store.delete_pending(user_id, data.get("ids") or [data["id"]])
            results.append({"index": index, "status": "done"})
    if calendar_creates:
        created = create_events(
            [event for _, event in calendar_creates],
            f"import{uuid4().hex}",
        )
        for (index, _), result in zip(calendar_creates, created):
            results.append({"index": index, "status": "done", "result": result})
    return sorted(results, key=lambda item: item["index"])
