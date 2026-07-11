import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ActionHistoryStore:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    undone_at TEXT
                )
                """
            )

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, action_type, payload):
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO calendar_actions (action_type, payload, created_at)
                VALUES (?, ?, ?)
                """,
                (action_type, json.dumps(payload, ensure_ascii=False), created_at),
            )
            return cursor.lastrowid

    def get_last_active(self):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, action_type, payload, created_at
                FROM calendar_actions
                WHERE undone_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def mark_undone(self, action_id):
        undone_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE calendar_actions
                SET undone_at = ?
                WHERE id = ? AND undone_at IS NULL
                """,
                (undone_at, action_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Действие уже отменено или не найдено")
