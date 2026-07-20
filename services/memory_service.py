import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ALLOWED_CATEGORIES = {"person", "place", "project", "preference"}
ALLOWED_OPERATIONS = {"set", "delete"}
SENSITIVE_MARKERS = {
    "password",
    "пароль",
    "token",
    "токен",
    "api key",
    "api_key",
    "secret",
    "секретный ключ",
}


def infer_stable_memories(user_text):
    """Extract only durable profile facts from ordinary planning messages."""
    normalized = " ".join(str(user_text).casefold().replace("ё", "е").split())
    updates = []
    if re.search(r"\bсмен\w*\b", normalized):
        updates.append({
            "operation": "set",
            "category": "preference",
            "key": "сменный рабочий график",
            "value": (
                "Марго работает по сменному графику. Конкретные даты и время "
                "смен нужно брать из актуального сообщения или Google Calendar."
            ),
        })
    if any(marker in normalized for marker in (
        "я художник", "работаю художником", "ui artist", "ui/ux artist",
        "ui designer", "дизайнер интерфейсов",
    )):
        updates.append({
            "operation": "set",
            "category": "preference",
            "key": "профессия Марго",
            "value": (
                "Марго — Senior UI Artist / Lead UI Designer, художник и "
                "дизайнер игровых интерфейсов."
            ),
        })
    return updates


def validate_memory_updates(raw_updates):
    if raw_updates is None:
        return []
    if not isinstance(raw_updates, list):
        raise ValueError("memory_updates должен быть списком")

    updates = []
    for raw in raw_updates:
        if not isinstance(raw, dict):
            raise ValueError("Запись памяти должна быть объектом")
        operation = str(raw.get("operation", "set")).strip().lower()
        category = str(raw.get("category", "")).strip().lower()
        key = str(raw.get("key", "")).strip()
        value = str(raw.get("value", "")).strip()
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError("Неизвестная операция памяти")
        if category not in ALLOWED_CATEGORIES:
            raise ValueError("Неизвестная категория памяти")
        if not key or len(key) > 200 or len(value) > 1000:
            raise ValueError("Некорректный размер записи памяти")
        searchable = f"{key} {value}".lower()
        if any(marker in searchable for marker in SENSITIVE_MARKERS):
            continue
        if operation == "set" and not value:
            raise ValueError("Для сохранения памяти нужно значение")
        updates.append(
            {
                "operation": operation,
                "category": category,
                "key": key,
                "value": value,
            }
        )
    return updates


class MemoryStore:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (category, normalized_key)
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(memories)")
            }
            if "normalized_key" not in columns:
                connection.execute(
                    "ALTER TABLE memories ADD COLUMN normalized_key TEXT"
                )
                rows = connection.execute(
                    "SELECT rowid, key FROM memories"
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "UPDATE memories SET normalized_key = ? WHERE rowid = ?",
                        (row["key"].casefold(), row["rowid"]),
                    )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS memories_normalized_key
                ON memories(category, normalized_key)
                """
            )

    def apply_updates(self, raw_updates):
        updates = validate_memory_updates(raw_updates)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            for update in updates:
                if update["operation"] == "delete":
                    connection.execute(
                        """
                        DELETE FROM memories
                        WHERE category = ? AND normalized_key = ?
                        """,
                        (update["category"], update["key"].casefold()),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO memories (
                            category, key, normalized_key, value, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(category, normalized_key) DO UPDATE SET
                            key = excluded.key,
                            value = excluded.value,
                            updated_at = excluded.updated_at
                        """,
                        (
                            update["category"],
                            update["key"],
                            update["key"].casefold(),
                            update["value"],
                            now,
                        ),
                    )
        return updates

    def get_all(self):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT category, key, value, updated_at
                FROM memories
                ORDER BY category, key
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def as_prompt_context(self):
        return json.dumps(self.get_all(), ensure_ascii=False, indent=2)
