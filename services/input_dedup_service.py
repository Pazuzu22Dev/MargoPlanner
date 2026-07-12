import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class InputDedupStore:
    def __init__(self, path, ttl_minutes=15):
        self.path = Path(path)
        self.ttl = timedelta(minutes=ttl_minutes)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS processed_inputs ("
                "input_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, "
                "created_at TEXT NOT NULL)"
            )

    def claim(self, file_id, content):
        digest = hashlib.sha256(content).hexdigest()
        now = datetime.now(timezone.utc)
        cutoff = (now - self.ttl).isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "DELETE FROM processed_inputs WHERE created_at < ?", (cutoff,)
            )
            duplicate = connection.execute(
                "SELECT 1 FROM processed_inputs WHERE input_key = ? OR content_hash = ?",
                (str(file_id), digest),
            ).fetchone()
            if duplicate:
                return False
            connection.execute(
                "INSERT INTO processed_inputs (input_key, content_hash, created_at) "
                "VALUES (?, ?, ?)",
                (str(file_id), digest, now.isoformat()),
            )
        return True
