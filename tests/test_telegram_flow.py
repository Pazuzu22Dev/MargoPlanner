import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "123")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

from telegram_bot import (
    authorize_update,
    handle_message,
    handle_voice,
    parse_candidate_selection,
    parse_allowed_user_id,
)
from services.action_history_service import ActionHistoryStore


EVENTS = [
    {
        "title": "Встреча",
        "start_time": "2026-07-16T15:00:00+02:00",
        "end_time": "2026-07-16T16:00:00+02:00",
    }
]


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.voice = None

    async def reply_text(self, text):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, text, user_id=123):
        self.message = FakeMessage(text)
        self.effective_message = self.message
        self.effective_user = type("User", (), {"id": user_id})()


class FakeContext:
    def __init__(self):
        self.user_data = {}


class FakeTelegramFile:
    async def download_as_bytearray(self):
        return bytearray(b"fake-ogg-audio")


class FakeBot:
    async def get_file(self, file_id):
        return FakeTelegramFile()


class TelegramFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner_patch = patch("telegram_bot.ALLOWED_USER_ID", 123)
        self.owner_patch.start()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.history_store = ActionHistoryStore(
            Path(self.temp_directory.name) / "actions.sqlite"
        )
        self.history_patch = patch(
            "telegram_bot.action_history_store",
            self.history_store,
        )
        self.history_patch.start()

    def tearDown(self):
        self.history_patch.stop()
        self.temp_directory.cleanup()
        self.owner_patch.stop()

    def test_allowed_user_id_must_be_a_positive_integer(self):
        self.assertEqual(parse_allowed_user_id("123"), 123)
        with self.assertRaises(RuntimeError):
            parse_allowed_user_id("Марго")
        with self.assertRaises(RuntimeError):
            parse_allowed_user_id("-1")

    def test_candidate_selection_understands_all_and_numbers(self):
        self.assertEqual(parse_candidate_selection("все", 2), [0, 1])
        self.assertEqual(parse_candidate_selection("1 и 2", 2), [0, 1])
        self.assertEqual(parse_candidate_selection("2", 2), [1])
        self.assertIsNone(parse_candidate_selection("30 июля", 2))

    async def test_unauthorized_user_is_silently_rejected(self):
        update = FakeUpdate("Покажи календарь", user_id=999)
        with patch("telegram_bot.detect_intent") as intent_mock:
            await handle_message(update, FakeContext())
        intent_mock.assert_not_called()
        self.assertEqual(update.message.replies, [])

    async def test_setup_mode_only_returns_the_senders_id(self):
        update = FakeUpdate("Привет", user_id=777)
        with patch("telegram_bot.ALLOWED_USER_ID", None):
            authorized = await authorize_update(update)
        self.assertFalse(authorized)
        self.assertIn("777", update.message.replies[0])

    async def test_voice_transcript_enters_the_normal_text_flow(self):
        update = FakeUpdate("")
        update.message.voice = type(
            "Voice",
            (),
            {
                "duration": 12,
                "file_size": 1024,
                "file_id": "voice-file",
                "mime_type": "audio/ogg",
            },
        )()
        context = FakeContext()
        context.bot = FakeBot()
        with patch(
            "telegram_bot.transcribe_voice",
            return_value="В пятницу встреча в 15:00",
        ), patch("telegram_bot.process_user_text") as process_mock:
            await handle_voice(update, context)

        process_mock.assert_awaited_once_with(
            update,
            context,
            "В пятницу встреча в 15:00",
        )
        self.assertIn("Я услышала", update.message.replies[-1])

    async def test_proposal_then_confirmation_creates_the_saved_draft(self):
        context = FakeContext()
        proposal = FakeUpdate("Встреча 16-го в 15:00")
        intent = {
            "action": "create_events",
            "clarification_question": "",
            "reason": "Встреча понята",
            "events": EVENTS,
        }

        with patch("telegram_bot.detect_intent", return_value=intent), patch(
            "telegram_bot.find_conflicts", return_value=[]
        ):
            await handle_message(proposal, context)

        conversation = context.user_data["conversation"]
        self.assertEqual(conversation["draft"]["events"], EVENTS)
        self.assertIn("Создать?", proposal.message.replies[0])

        confirmation = FakeUpdate("да")
        created = [{"htmlLink": "https://calendar/event"}]
        with patch("telegram_bot.find_conflicts", return_value=[]), patch(
            "telegram_bot.create_events", return_value=created
        ) as create_mock:
            await handle_message(confirmation, context)

        create_mock.assert_called_once_with(
            EVENTS,
            conversation["draft"]["batch_id"],
        )
        self.assertNotIn("conversation", context.user_data)
        self.assertIn("https://calendar/event", confirmation.message.replies[0])

    async def test_multiple_matches_require_clarification(self):
        context = FakeContext()
        update = FakeUpdate("Перенеси встречу с Дашей на пятницу")
        intent = {
            "action": "update_event",
            "clarification_question": "",
            "reason": "Нужно перенести встречу",
            "target_event_id": "",
            "search": {"text": "Даша"},
            "events": EVENTS,
        }
        candidates = [
            {
                "id": f"event-{number}",
                "etag": f"version-{number}",
                "title": "Встреча с Дашей",
                "start_time": f"2026-07-{15 + number}T15:00:00+02:00",
                "end_time": f"2026-07-{15 + number}T16:00:00+02:00",
            }
            for number in (1, 2)
        ]
        with patch("telegram_bot.detect_intent", return_value=intent), patch(
            "telegram_bot.search_events", return_value=candidates
        ):
            await handle_message(update, context)

        conversation = context.user_data["conversation"]
        self.assertEqual(conversation["draft"]["candidates"], candidates)
        self.assertIn("Нашла несколько вариантов", update.message.replies[0])
        self.assertIn("1.", update.message.replies[0])
        self.assertIn("2.", update.message.replies[0])

    async def test_delete_happens_only_after_confirmation(self):
        context = FakeContext()
        request = FakeUpdate("Удали встречу с Дашей завтра")
        intent = {
            "action": "delete_event",
            "clarification_question": "",
            "reason": "Удалить встречу",
            "target_event_id": "",
            "search": {"text": "Даша"},
            "events": [],
        }
        target = {
            "id": "event-to-delete",
            "etag": "version-1",
            "title": "Встреча с Дашей",
            "start_time": "2026-07-16T15:00:00+02:00",
            "end_time": "2026-07-16T16:00:00+02:00",
        }
        with patch("telegram_bot.detect_intent", return_value=intent), patch(
            "telegram_bot.search_events", return_value=[target]
        ), patch("telegram_bot.delete_event") as delete_mock:
            await handle_message(request, context)
        delete_mock.assert_not_called()
        self.assertIn("Подтвердить?", request.message.replies[0])

        confirmation = FakeUpdate("да")
        with patch("telegram_bot.get_event", return_value=target), patch(
            "telegram_bot.delete_event"
        ) as delete_mock:
            await handle_message(confirmation, context)
        delete_mock.assert_called_once_with("event-to-delete")
        self.assertNotIn("conversation", context.user_data)

    async def test_delete_all_date_events_previews_the_whole_batch(self):
        context = FakeContext()
        request = FakeUpdate("Удали все события на 30 июля")
        intent = {
            "action": "delete_events",
            "clarification_question": "",
            "reason": "Удалить события за день",
            "target_event_id": "",
            "search": {
                "text": "",
                "time_min": "2026-07-30T00:00:00+02:00",
                "time_max": "2026-07-31T00:00:00+02:00",
            },
            "events": [],
        }
        candidates = [
            {
                "id": f"event-{number}",
                "etag": f"version-{number}",
                "title": title,
                "start_time": f"2026-07-30T{8 + number:02d}:00:00+02:00",
                "end_time": f"2026-07-30T{9 + number:02d}:00:00+02:00",
            }
            for number, title in ((1, "Тату"), (2, "Занятие"))
        ]
        with patch("telegram_bot.detect_intent", return_value=intent), patch(
            "telegram_bot.search_events", return_value=candidates
        ):
            await handle_message(request, context)

        draft = context.user_data["conversation"]["draft"]
        self.assertEqual(draft["targets"], candidates)
        self.assertIn("Тату", request.message.replies[0])
        self.assertIn("Занятие", request.message.replies[0])
        self.assertIn("Подтвердить?", request.message.replies[0])

    async def test_undo_last_creation_requires_confirmation(self):
        self.history_store.record(
            "create_events",
            {
                "events": [
                    {
                        "id": "created-event",
                        "title": "Встреча",
                        "start_time": "2026-07-30T15:00:00+02:00",
                        "end_time": "2026-07-30T16:00:00+02:00",
                    }
                ]
            },
        )
        context = FakeContext()
        request = FakeUpdate("отмени последнее действие")
        with patch("telegram_bot.delete_calendar_events") as delete_mock:
            await handle_message(request, context)
        delete_mock.assert_not_called()
        self.assertIn("Отменить это действие?", request.message.replies[0])

        confirmation = FakeUpdate("да")
        with patch("telegram_bot.delete_calendar_events") as delete_mock:
            await handle_message(confirmation, context)
        delete_mock.assert_called_once_with(["created-event"])
        self.assertIsNone(self.history_store.get_last_active())


if __name__ == "__main__":
    unittest.main()
