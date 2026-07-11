import unittest

from services.conversation_service import (
    ConversationState,
    add_user_message,
    apply_intent,
    new_conversation,
)


class ConversationServiceTests(unittest.TestCase):
    def test_repeated_clarifications_keep_the_whole_history(self):
        conversation = new_conversation("Через три дня вроде 16-го")
        conversation = apply_intent(
            conversation,
            {
                "action": "clarify",
                "clarification_question": "Выбрать 14-е или 16-е?",
                "reason": "Даты противоречат друг другу",
                "events": [],
            },
        )
        conversation = add_user_message(conversation, "Нет, 16-го")
        conversation = apply_intent(
            conversation,
            {
                "action": "clarify",
                "clarification_question": "Во сколько встреча?",
                "reason": "Дата известна, время нет",
                "events": [],
            },
        )

        user_messages = [
            message["text"]
            for message in conversation["messages"]
            if message["role"] == "user"
        ]
        self.assertEqual(
            user_messages,
            ["Через три дня вроде 16-го", "Нет, 16-го"],
        )
        self.assertEqual(conversation["revision"], 2)
        self.assertEqual(
            conversation["state"],
            ConversationState.WAITING_FOR_CLARIFICATION,
        )

    def test_correction_replaces_the_draft_without_losing_history(self):
        conversation = new_conversation("Встреча в четверг в 15:00")
        conversation = apply_intent(
            conversation,
            {
                "action": "create_events",
                "clarification_question": "",
                "reason": "Встреча в четверг",
                "events": [{"title": "Встреча в четверг"}],
            },
        )
        conversation = add_user_message(conversation, "Лучше в пятницу")
        friday_events = [{"title": "Встреча в пятницу"}]
        conversation = apply_intent(
            conversation,
            {
                "action": "create_events",
                "clarification_question": "",
                "reason": "Встреча перенесена",
                "events": friday_events,
            },
        )

        self.assertEqual(conversation["draft"]["events"], friday_events)
        self.assertEqual(conversation["original_text"], "Встреча в четверг в 15:00")
        self.assertEqual(
            conversation["state"],
            ConversationState.WAITING_FOR_CONFIRMATION,
        )


if __name__ == "__main__":
    unittest.main()
