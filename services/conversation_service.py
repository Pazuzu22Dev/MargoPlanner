from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4


class ConversationState:
    IDLE = "idle"
    WAITING_FOR_CLARIFICATION = "waiting_for_clarification"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    WAITING_FOR_UNDO_CONFIRMATION = "waiting_for_undo_confirmation"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_conversation(user_text):
    """Create a serializable conversation that can live in Telegram user_data."""
    return {
        "state": ConversationState.IDLE,
        "original_text": user_text,
        "messages": [{"role": "user", "text": user_text}],
        "draft": {"events": [], "reason": ""},
        "clarification_question": "",
        "revision": 0,
        "updated_at": _now_iso(),
    }


def add_user_message(conversation, user_text):
    updated = deepcopy(conversation)
    updated.setdefault("messages", []).append(
        {"role": "user", "text": user_text}
    )
    updated["updated_at"] = _now_iso()
    return updated


def apply_intent(conversation, intent):
    """Apply Gemini's result without discarding facts from previous turns."""
    updated = deepcopy(conversation)
    action = intent["action"]

    if action == "clarify":
        updated["state"] = ConversationState.WAITING_FOR_CLARIFICATION
        updated["clarification_question"] = intent["clarification_question"]
    elif action in {
        "create_events",
        "update_event",
        "delete_event",
        "delete_events",
    }:
        updated["state"] = ConversationState.WAITING_FOR_CONFIRMATION
        updated["clarification_question"] = ""
    else:
        updated["state"] = ConversationState.IDLE
        updated["clarification_question"] = ""

    # Gemini returns the complete current draft. An empty draft during a
    # clarification does not erase the last useful version.
    events = intent.get("events", [])
    if events or action in {
        "create_events",
        "update_event",
        "delete_event",
        "delete_events",
    }:
        updated["draft"] = {
            "operation": action,
            "events": events,
            "reason": intent.get("reason", ""),
            "conflicts": [],
            "batch_id": uuid4().hex if action == "create_events" else "",
            "target_event_id": intent.get("target_event_id", ""),
            "search": intent.get("search", {}),
            "target": None,
            "targets": [],
            "candidates": [],
        }
    elif intent.get("reason"):
        updated.setdefault("draft", {})["reason"] = intent["reason"]

    assistant_text = (
        intent.get("clarification_question")
        or intent.get("reason")
        or action
    )
    updated.setdefault("messages", []).append(
        {"role": "assistant", "text": assistant_text}
    )
    updated["revision"] = updated.get("revision", 0) + 1
    updated["updated_at"] = _now_iso()
    return updated


def save_conversation(context, conversation):
    context.user_data["conversation"] = conversation


def get_conversation(context):
    return context.user_data.get("conversation")


def clear_conversation(context):
    context.user_data.pop("conversation", None)
