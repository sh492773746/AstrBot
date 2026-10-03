"""Durable admission, duplicate suppression and manual pause."""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def validate(config):
    if config.get("disclosure_confirmed") is not True:
        raise ValueError(
            "AI identity disclosure and participant authorization required"
        )
    for key in ("allowed_chats", "allowed_senders"):
        values = config.get(key)
        if not isinstance(values, list) or not values:
            raise ValueError(f"{key} must be an explicit nonempty list")
        for value in values:
            if not str(value).lstrip("-").isdigit():
                raise ValueError(f"{key} must contain numeric IDs")
            if key == "allowed_chats" and int(value) >= 0:
                raise ValueError("Only authorized groups are supported")
            if key == "allowed_senders" and int(value) <= 0:
                raise ValueError("Sender IDs must be positive")
    value = config.get("cooldown_seconds")
    if type(value) is not int or not 10 <= value <= 3600:
        raise ValueError("cooldown_seconds outside safe range")


class Gate:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (
                account TEXT, chat TEXT, message TEXT, at REAL, day TEXT,
                PRIMARY KEY(account, chat, message)
            );
            CREATE TABLE IF NOT EXISTS pauses (account TEXT PRIMARY KEY, paused INTEGER);
        """)

    def paused(self, account):
        row = self.db.execute(
            "SELECT paused FROM pauses WHERE account=?", (account,)
        ).fetchone()
        return bool(row and row[0])

    def pause(self, account, value=True):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO pauses VALUES (?,?)", (account, int(value))
            )

    def admit(self, account, chat, message, config, now=None):
        now = time.time() if now is None else now
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.paused(account):
                return False
            last = self.db.execute(
                "SELECT MAX(at) FROM attempts WHERE account=?", (account,)
            ).fetchone()[0]
            if last is not None and now - last < config["cooldown_seconds"]:
                return False
            try:
                self.db.execute(
                    "INSERT INTO attempts VALUES (?,?,?,?,?)",
                    (account, str(chat), str(message), now, day),
                )
            except sqlite3.IntegrityError:
                return False
            self.db.execute("DELETE FROM attempts WHERE at < ?", (now - 7 * 86400,))
            return True

    def close(self):
        self.db.close()
