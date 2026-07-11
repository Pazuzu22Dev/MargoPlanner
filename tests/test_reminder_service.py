import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.reminder_service import ReminderStore


class ReminderStoreTests(unittest.TestCase):
    def test_due_reminder_is_claimed_once_and_marked_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReminderStore(Path(directory) / "reminders.sqlite")
            due = datetime.now(timezone.utc) - timedelta(minutes=1)
            reminder_id = store.create(457923330, "Ответить", due.isoformat())
            reminders = store.claim_due()
            self.assertEqual([item["id"] for item in reminders], [reminder_id])
            self.assertEqual(store.claim_due(), [])
            store.mark_sent(reminder_id)
            self.assertEqual(store.claim_due(), [])

    def test_interrupted_delivery_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReminderStore(Path(directory) / "reminders.sqlite")
            due = datetime.now(timezone.utc) - timedelta(minutes=1)
            store.create(457923330, "Ответить", due.isoformat())
            self.assertEqual(len(store.claim_due()), 1)
            store.recover_interrupted()
            self.assertEqual(len(store.claim_due()), 1)


if __name__ == "__main__":
    unittest.main()
