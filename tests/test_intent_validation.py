import os
import unittest

os.environ.setdefault("GEMINI_API_KEY", "test-key")

from services.intent_service import validate_intent


class IntentValidationTests(unittest.TestCase):
    def test_valid_event_is_normalized(self):
        result = validate_intent(
            {
                "action": "create_events",
                "events": [
                    {
                        "title": " Встреча ",
                        "start_time": "2026-07-16T15:00:00+02:00",
                        "end_time": "2026-07-16T16:00:00+02:00",
                    }
                ],
            }
        )
        self.assertEqual(result["events"][0]["title"], "Встреча")

    def test_event_without_timezone_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_intent(
                {
                    "action": "create_events",
                    "events": [
                        {
                            "title": "Встреча",
                            "start_time": "2026-07-16T15:00:00",
                            "end_time": "2026-07-16T16:00:00",
                        }
                    ],
                }
            )

    def test_end_before_start_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_intent(
                {
                    "action": "create_events",
                    "events": [
                        {
                            "title": "Встреча",
                            "start_time": "2026-07-16T16:00:00+02:00",
                            "end_time": "2026-07-16T15:00:00+02:00",
                        }
                    ],
                }
            )

    def test_update_requires_one_new_event(self):
        result = validate_intent(
            {
                "action": "update_event",
                "target_event_id": "",
                "search": {"text": "Даша"},
                "events": [
                    {
                        "title": "Встреча с Дашей",
                        "start_time": "2026-07-17T15:00:00+02:00",
                        "end_time": "2026-07-17T16:00:00+02:00",
                    }
                ],
            }
        )
        self.assertEqual(result["action"], "update_event")
        self.assertEqual(result["search"]["text"], "Даша")

    def test_delete_requires_a_real_search_or_candidate_id(self):
        with self.assertRaises(ValueError):
            validate_intent({"action": "delete_event", "events": []})

    def test_chat_can_return_a_valid_memory_update(self):
        result = validate_intent(
            {
                "action": "chat",
                "events": [],
                "memory_updates": [
                    {
                        "operation": "set",
                        "category": "preference",
                        "key": "утро",
                        "value": "Не назначать встречи раньше 10:00",
                    }
                ],
            }
        )
        self.assertEqual(result["memory_updates"][0]["key"], "утро")

    def test_delete_events_accepts_a_date_range(self):
        result = validate_intent(
            {
                "action": "delete_events",
                "events": [],
                "search": {
                    "text": "",
                    "time_min": "2026-07-30T00:00:00+02:00",
                    "time_max": "2026-07-31T00:00:00+02:00",
                },
            }
        )
        self.assertEqual(result["action"], "delete_events")
        self.assertEqual(result["search"]["time_min"][:10], "2026-07-30")

    def test_contact_email_does_not_become_an_attendee_implicitly(self):
        result = validate_intent(
            {
                "action": "create_events",
                "events": [
                    {
                        "title": "Созвон с Дашей",
                        "start_time": "2026-07-16T15:00:00+02:00",
                        "end_time": "2026-07-16T16:00:00+02:00",
                        "links": ["https://zoom.us/example"],
                        "contacts": ["dasha@example.com"],
                        "attendees": [],
                    }
                ],
            }
        )
        event = result["events"][0]
        self.assertEqual(event["contacts"], ["dasha@example.com"])
        self.assertEqual(event["attendees"], [])

    def test_valid_telegram_reminder(self):
        result = validate_intent(
            {
                "action": "create_reminder",
                "reminder": {
                    "text": "Ответить в LinkedIn",
                    "remind_at": "2026-07-12T10:00:00+02:00",
                },
            }
        )
        self.assertEqual(result["events"], [])
        self.assertEqual(result["reminder"]["text"], "Ответить в LinkedIn")

    def test_reminder_without_timezone_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_intent(
                {
                    "action": "create_reminder",
                    "reminder": {
                        "text": "Ответить в LinkedIn",
                        "remind_at": "2026-07-12T10:00:00",
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
