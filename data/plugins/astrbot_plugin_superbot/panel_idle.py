"""Persist idle deadlines for disposable game selectors, not active tables."""

from .store import Rejected


class PanelIdle:
    """Retire known bot panels through the existing durable deletion queue."""

    def __init__(self, store):
        self.store = store
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS game_panel_idle(
                chat TEXT NOT NULL,message INTEGER NOT NULL,kind TEXT NOT NULL,
                due REAL NOT NULL,status TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY(chat,message));
            CREATE INDEX IF NOT EXISTS game_panel_idle_due ON game_panel_idle(status,due);
        """)

    def touch(self, chat, message, kind):
        """Start or extend an authorized panel's idle window without revival.

        Args:
            chat: Verified panel group.
            message: Exact bot message ID.
            kind: Hub, slot selector, or wheel panel.
        """
        now = self.store.clock()
        row = self.store.db.execute(
            "SELECT * FROM game_panel_idle WHERE chat=? AND message=?",
            (str(chat), message),
        ).fetchone()
        if row and (
            row["kind"] != kind or row["status"] != "active" or row["due"] <= now
        ):
            raise Rejected("面板已闲置30秒，请重新打开玩法。")
        self.store.db.execute(
            "INSERT INTO game_panel_idle(chat,message,kind,due) VALUES(?,?,?,?) "
            "ON CONFLICT(chat,message) DO UPDATE SET due=excluded.due",
            (str(chat), message, kind, now + 30),
        )

    def expire(self):
        """Claim expired selectors atomically before queueing exact-ID deletion."""
        now = self.store.clock()
        with self.store.tx() as db:
            for row in db.execute(
                "SELECT * FROM game_panel_idle WHERE status='active' AND due<=? ORDER BY due LIMIT 50",
                (now,),
            ).fetchall():
                db.execute(
                    "UPDATE game_panel_idle SET status='expired' WHERE chat=? AND message=?",
                    (row["chat"], row["message"]),
                )
                db.execute(
                    "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                    (row["chat"], row["message"], row["message"], now, now),
                )
