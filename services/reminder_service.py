import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ReminderStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                )
                """
            )

    def _connect(self):
        return sqlite3.connect(self.path)

    def create(self, user_id, text, remind_at):
        parsed = datetime.fromisoformat(remind_at)
        if parsed.tzinfo is None:
            raise ValueError("Время напоминания должно содержать часовой пояс")
        remind_at_utc = parsed.astimezone(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO reminders (user_id, text, remind_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, text.strip(), remind_at_utc, now),
            )
            return cursor.lastrowid

    def claim_due(self, now=None):
        current = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, user_id, text, remind_at FROM reminders "
                "WHERE status = 'pending' AND remind_at <= ? ORDER BY remind_at",
                (current,),
            ).fetchall()
            if rows:
                connection.executemany(
                    "UPDATE reminders SET status = 'sending' WHERE id = ?",
                    [(row[0],) for row in rows],
                )
        return [
            {"id": row[0], "user_id": row[1], "text": row[2], "remind_at": row[3]}
            for row in rows
        ]

    def mark_sent(self, reminder_id):
        with self._connect() as connection:
            connection.execute(
                "UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), reminder_id),
            )

    def release(self, reminder_id):
        with self._connect() as connection:
            connection.execute(
                "UPDATE reminders SET status = 'pending' WHERE id = ? "
                "AND status = 'sending'",
                (reminder_id,),
            )

    def recover_interrupted(self):
        with self._connect() as connection:
            connection.execute(
                "UPDATE reminders SET status = 'pending' WHERE status = 'sending'"
            )
