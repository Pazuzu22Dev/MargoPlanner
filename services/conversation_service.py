class ConversationState:
    IDLE = "idle"
    WAITING_FOR_CLARIFICATION = "waiting_for_clarification"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"


def save_conversation(context, state, original_text, data=None):
    context.user_data["conversation"] = {
        "state": state,
        "original_text": original_text,
        "data": data or {},
    }


def get_conversation(context):
    return context.user_data.get("conversation")


def clear_conversation(context):
    context.user_data.pop("conversation", None)