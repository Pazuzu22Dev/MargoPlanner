import tempfile
import unittest
from pathlib import Path

from services.action_history_service import ActionHistoryStore


class ActionHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.store = ActionHistoryStore(
            Path(self.temp_directory.name) / "actions.sqlite"
        )

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_last_action_can_be_marked_as_undone(self):
        first_id = self.store.record("create_events", {"events": []})
        second_id = self.store.record("delete_events", {"events": []})
        self.assertEqual(self.store.get_last_active()["id"], second_id)
        self.store.mark_undone(second_id)
        self.assertEqual(self.store.get_last_active()["id"], first_id)

    def test_action_payload_survives_reopening(self):
        self.store.record(
            "update_event",
            {"before": {"title": "Старая встреча"}},
        )
        reopened = ActionHistoryStore(self.store.database_path)
        self.assertEqual(
            reopened.get_last_active()["payload"]["before"]["title"],
            "Старая встреча",
        )


if __name__ == "__main__":
    unittest.main()
