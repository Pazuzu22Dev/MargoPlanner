import tempfile
import unittest
from pathlib import Path

from services.memory_service import MemoryStore, validate_memory_updates


class MemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "memory.sqlite"
        self.store = MemoryStore(self.database_path)

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_memory_survives_a_new_store_instance(self):
        self.store.apply_updates(
            [
                {
                    "operation": "set",
                    "category": "person",
                    "key": "Даша",
                    "value": "Подруга Марго, живёт в Баре",
                }
            ]
        )
        reopened = MemoryStore(self.database_path)
        self.assertEqual(reopened.get_all()[0]["key"], "Даша")

    def test_set_updates_an_existing_fact_case_insensitively(self):
        self.store.apply_updates(
            [
                {
                    "operation": "set",
                    "category": "place",
                    "key": "Дом",
                    "value": "Будва",
                },
                {
                    "operation": "set",
                    "category": "place",
                    "key": "дом",
                    "value": "Бар",
                },
            ]
        )
        memories = self.store.get_all()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["value"], "Бар")

    def test_delete_forgets_a_fact(self):
        self.store.apply_updates(
            [
                {
                    "operation": "set",
                    "category": "preference",
                    "key": "время встреч",
                    "value": "Не раньше 10 утра",
                }
            ]
        )
        self.store.apply_updates(
            [
                {
                    "operation": "delete",
                    "category": "preference",
                    "key": "время встреч",
                    "value": "",
                }
            ]
        )
        self.assertEqual(self.store.get_all(), [])

    def test_sensitive_values_are_not_saved(self):
        updates = validate_memory_updates(
            [
                {
                    "operation": "set",
                    "category": "preference",
                    "key": "API key",
                    "value": "very-secret-value",
                }
            ]
        )
        self.assertEqual(updates, [])


if __name__ == "__main__":
    unittest.main()
