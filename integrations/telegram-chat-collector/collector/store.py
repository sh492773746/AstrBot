"""SQLite persistence; accessed exclusively by the collector event loop."""

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


class Store:
    """Store activity facts and eligibility intervals in a single local database."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS group_access (
                id INTEGER PRIMARY KEY, mode TEXT NOT NULL, username TEXT,
                pts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS backfills (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                start REAL NOT NULL, end REAL NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
                scanned INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT,
                PRIMARY KEY(chat_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS removed_groups (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS targets (
                kind TEXT NOT NULL, id INTEGER NOT NULL, name TEXT NOT NULL,
                enabled INTEGER NOT NULL, PRIMARY KEY(kind,id)
            );
            CREATE TABLE IF NOT EXISTS intervals (
                kind TEXT NOT NULL, id INTEGER NOT NULL,
                start REAL NOT NULL, end REAL
            );
            CREATE INDEX IF NOT EXISTS interval_lookup ON intervals(kind,id,start);
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, sent REAL NOT NULL, text TEXT,
                media TEXT NOT NULL, edited REAL, reply_to INTEGER,
                deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS message_time ON messages(sent,user_id,chat_id);
            CREATE INDEX IF NOT EXISTS person_time ON messages(user_id,sent);
            CREATE TABLE IF NOT EXISTS progress (
                chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL DEFAULT 0,
                last_sync REAL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS gaps (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                start REAL NOT NULL, end REAL, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL);
        """)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS watches (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL, PRIMARY KEY(chat_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS watch_intervals (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                start REAL NOT NULL, end REAL
            );
            CREATE INDEX IF NOT EXISTS watch_lookup
                ON watch_intervals(chat_id,user_id,start);
            CREATE TABLE IF NOT EXISTS pauses (start REAL NOT NULL, end REAL);
        """)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS link_rules (user_id INTEGER PRIMARY KEY, patterns TEXT NOT NULL, enabled INTEGER NOT NULL)"
        )
        if "links" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(messages)")
        }:
            self.db.execute("ALTER TABLE messages ADD COLUMN links TEXT")
            self.db.commit()
        # Preserve existing global enrollment once when upgrading to scoped watches.
        with self.db:
            if not self.db.execute(
                "SELECT 1 FROM metadata WHERE key='scoped_watches_v1'"
            ).fetchone():
                for group in self.targets("groups"):
                    for person in self.targets("people"):
                        start = self.db.execute(
                            "SELECT MIN(start) FROM intervals WHERE (kind='groups' AND id=?) "
                            "OR (kind='people' AND id=?)",
                            (group["id"], person["id"]),
                        ).fetchone()[0]
                        if start is not None:
                            self.db.execute(
                                "INSERT OR IGNORE INTO watches VALUES(?,?,1)",
                                (group["id"], person["id"]),
                            )
                            self.db.execute(
                                "INSERT INTO watch_intervals VALUES(?,?,?,NULL)",
                                (group["id"], person["id"], start),
                            )
                self.db.execute("INSERT INTO metadata VALUES('scoped_watches_v1',1)")
        self.prune()

    def targets(self, kind: str) -> list[dict]:
        """Return configured targets.

        Args:
            kind: Either groups or people.

        Returns:
            Target records ordered by numeric ID.
        """
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM targets WHERE kind=? AND NOT (kind='groups' AND id IN (SELECT id FROM removed_groups)) ORDER BY id",
                (kind,),
            )
        ]

    def configure(
        self,
        kind: str,
        target_id: int,
        name: str,
        enabled: bool,
        now: float | None = None,
    ) -> None:
        """Change a target without rewriting previous eligibility periods.

        Args:
            kind: Either groups or people.
            target_id: Telegram numeric ID.
            name: Display name.
            enabled: Desired collection state.
            now: Optional timestamp for deterministic tests.

        Raises:
            ValueError: If the target or configured capacity is invalid.
        """
        now = time.time() if now is None else now
        if (
            kind not in ("groups", "people")
            or not name.strip()
            or len(name) > 100
            or not -(2**63) < target_id < 2**63
        ):
            raise ValueError("Invalid target")
        if (kind == "people" and target_id <= 0) or (
            kind == "groups" and target_id >= 0
        ):
            raise ValueError("Invalid Telegram ID")
        old = self.db.execute(
            "SELECT * FROM targets WHERE kind=? AND id=?", (kind, target_id)
        ).fetchone()
        limit = 30 if kind == "groups" else 50
        active = sum(bool(t["enabled"]) for t in self.targets(kind))
        if enabled and not (old and old["enabled"]) and active >= limit:
            raise ValueError("Active target capacity reached")
        with self.db:
            if kind == "groups":
                self.db.execute("DELETE FROM removed_groups WHERE id=?", (target_id,))
            self.db.execute(
                "INSERT INTO targets VALUES(?,?,?,?) ON CONFLICT(kind,id) "
                "DO UPDATE SET name=excluded.name,enabled=excluded.enabled",
                (kind, target_id, name.strip(), int(enabled)),
            )
            if enabled and not (old and old["enabled"]):
                self.db.execute(
                    "INSERT INTO intervals VALUES(?,?,?,NULL)", (kind, target_id, now)
                )
            elif not enabled and old and old["enabled"]:
                self.db.execute(
                    "UPDATE intervals SET end=? WHERE kind=? AND id=? AND end IS NULL",
                    (now, kind, target_id),
                )
            if kind == "groups":
                self.db.execute(
                    "INSERT OR IGNORE INTO progress(chat_id) VALUES(?)", (target_id,)
                )
                if not enabled:
                    self.db.execute(
                        "UPDATE gaps SET end=? WHERE chat_id=? AND end IS NULL",
                        (now, target_id),
                    )

    def watches(self, group: int = 0) -> list[dict]:
        """List group-scoped watches and their effective target switches.

        Args:
            group: Group ID, or zero to list all groups.

        Returns:
            Watch records with group and person names and switches.
        """
        return [
            dict(row)
            for row in self.db.execute(
                """
            SELECT w.*,g.name AS group_name,p.name AS person_name,
                g.enabled AS group_enabled,p.enabled AS person_enabled,
                (SELECT MIN(start) FROM watch_intervals i WHERE i.chat_id=w.chat_id AND i.user_id=w.user_id) AS started_at,
                b.start AS history_start,b.state AS backfill_state,b.matched AS backfill_count,b.error AS backfill_error
            FROM watches w JOIN targets g ON g.kind='groups' AND g.id=w.chat_id
            JOIN targets p ON p.kind='people' AND p.id=w.user_id
            LEFT JOIN backfills b ON b.chat_id=w.chat_id AND b.user_id=w.user_id
            WHERE (?=0 OR w.chat_id=?)
            AND w.chat_id NOT IN (SELECT id FROM removed_groups)
            ORDER BY w.chat_id,w.user_id
        """,
                (group, group),
            )
        ]

    def configure_watch(
        self, group: int, person: int, enabled: bool, now: float | None = None
    ) -> None:
        """Enable or pause one person in one group without backfilling pauses.

        Args:
            group: Configured group ID.
            person: Configured person ID.
            enabled: Desired per-group watch state.
            now: Optional UTC timestamp for deterministic tests.

        Raises:
            ValueError: If either target does not exist.
        """
        now = time.time() if now is None else now
        for kind, target_id in (("groups", group), ("people", person)):
            if not self.db.execute(
                "SELECT 1 FROM targets WHERE kind=? AND id=?", (kind, target_id)
            ).fetchone():
                raise ValueError("Configure the group and person first")
        old = self.db.execute(
            "SELECT enabled FROM watches WHERE chat_id=? AND user_id=?", (group, person)
        ).fetchone()
        with self.db:
            self.db.execute(
                "INSERT INTO watches VALUES(?,?,?) ON CONFLICT(chat_id,user_id) "
                "DO UPDATE SET enabled=excluded.enabled",
                (group, person, int(enabled)),
            )
            if enabled and not (old and old[0]):
                self.db.execute(
                    "INSERT INTO watch_intervals VALUES(?,?,?,NULL)",
                    (group, person, now),
                )
            elif not enabled and old and old[0]:
                self.db.execute(
                    "UPDATE watch_intervals SET end=? WHERE chat_id=? AND user_id=? AND end IS NULL",
                    (now, group, person),
                )

    def pause(self, paused: bool, now: float | None = None) -> None:
        """Change the persistent global pause without modifying target switches.

        Args:
            paused: Whether new message times must be excluded.
            now: Optional UTC timestamp for deterministic tests.
        """
        now = time.time() if now is None else now
        current = self.db.execute("SELECT 1 FROM pauses WHERE end IS NULL").fetchone()
        with self.db:
            if paused and not current:
                self.db.execute("INSERT INTO pauses VALUES(?,NULL)", (now,))
            elif not paused and current:
                self.db.execute("UPDATE pauses SET end=? WHERE end IS NULL", (now,))

    def record(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        sent: float,
        text: str,
        media: str,
        edited: float | None = None,
        reply_to: int | None = None,
        links: list[str] | None = None,
    ) -> bool:
        """Upsert an eligible message without counting edits or replays twice.

        Args:
            chat_id: Source group ID.
            message_id: Group-local message ID.
            user_id: Positive user ID, excluding anonymous/channel identities.
            sent: Original UTC timestamp.
            text: Original text or caption.
            media: Media category.
            edited: Optional UTC edit timestamp.
            reply_to: Optional replied-to message ID.
            links: Explicit Telegram URL entity targets, including masked links.

        Returns:
            Whether the message falls within both target eligibility periods.
        """
        if user_id <= 0:
            return False
        history = self.db.execute(
            "SELECT 1 FROM backfills b JOIN watches w USING(chat_id,user_id) "
            "WHERE b.chat_id=? AND b.user_id=? AND b.start<=? AND b.end>?",
            (chat_id, user_id, sent, sent),
        ).fetchone()
        if not history:
            for kind, target_id in (("groups", chat_id), ("people", user_id)):
                eligible = self.db.execute(
                    "SELECT 1 FROM intervals WHERE kind=? AND id=? AND start<=? "
                    "AND (end IS NULL OR end>?) LIMIT 1",
                    (kind, target_id, sent, sent),
                ).fetchone()
                if not eligible:
                    return False
            if self.db.execute(
                "SELECT 1 FROM pauses WHERE start<=? AND (end IS NULL OR end>?)",
                (sent, sent),
            ).fetchone():
                return False
            if not self.db.execute(
                "SELECT 1 FROM watch_intervals WHERE chat_id=? AND user_id=? AND start<=? "
                "AND (end IS NULL OR end>?)",
                (chat_id, user_id, sent, sent),
            ).fetchone():
                return False
        cutoff = self.db.execute(
            "SELECT value FROM metadata WHERE key='raw_cutoff'"
        ).fetchone()[0]
        with self.db:
            self.db.execute(
                """
                INSERT INTO messages(chat_id,message_id,user_id,sent,text,media,edited,reply_to)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,message_id) DO UPDATE SET
                text=CASE WHEN messages.deleted=1 THEN NULL ELSE excluded.text END,
                media=excluded.media, edited=excluded.edited
                WHERE COALESCE(excluded.edited,0)>=COALESCE(messages.edited,0)
            """,
                (
                    chat_id,
                    message_id,
                    user_id,
                    sent,
                    text if sent >= cutoff else None,
                    media,
                    edited,
                    reply_to,
                ),
            )
            self.db.execute(
                "UPDATE messages SET links=? WHERE chat_id=? AND message_id=? AND deleted=0 AND sent>=? AND COALESCE(edited,0)=?",
                (
                    json.dumps(
                        list(
                            dict.fromkeys(
                                (links or [])
                                + re.findall(
                                    r"(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>\"，。！？；]+",
                                    text or "",
                                )
                            )
                        ),
                        ensure_ascii=False,
                    ),
                    chat_id,
                    message_id,
                    cutoff,
                    edited or 0,
                ),
            )
        return True

    def delete(self, chat_id: int, ids: list[int]) -> None:
        """Hide the bodies of locatable deletions, retaining activity facts.

        Args:
            chat_id: Known source group ID.
            ids: Deleted message IDs.
        """
        with self.db:
            self.db.executemany(
                "UPDATE messages SET deleted=1,text=NULL,links=NULL WHERE chat_id=? AND message_id=?",
                [(chat_id, message_id) for message_id in ids],
            )

    def prune(self, now: float | None = None) -> None:
        """Expire bodies while preserving deduplication and historical statistics.

        Args:
            now: Optional UTC timestamp for deterministic tests.
        """
        cutoff = (time.time() if now is None else now) - 90 * 86400
        with self.db:
            self.db.execute(
                "INSERT INTO metadata VALUES('raw_cutoff',?) ON CONFLICT(key) "
                "DO UPDATE SET value=MAX(value,excluded.value)",
                (cutoff,),
            )
            self.db.execute(
                "UPDATE messages SET text=NULL,links=NULL WHERE sent<? AND (text IS NOT NULL OR links IS NOT NULL)",
                (cutoff,),
            )

    def gap(self, chat_id: int, reason: str, now: float) -> None:
        """Record a recoverable interval without repeatedly creating the same gap.

        Args:
            chat_id: A configured group ID.
            reason: Sanitized machine-readable failure code.
            now: UTC failure timestamp.
        """
        row = self.db.execute(
            "SELECT last_sync FROM progress WHERE chat_id=?", (chat_id,)
        ).fetchone()
        start = (
            row[0]
            if row and row[0]
            else self.db.execute(
                "SELECT MIN(start) FROM intervals WHERE kind='groups' AND id=?",
                (chat_id,),
            ).fetchone()[0]
        )
        with self.db:
            self.db.execute(
                "UPDATE progress SET error=? WHERE chat_id=?", (reason, chat_id)
            )
            if not self.db.execute(
                "SELECT 1 FROM gaps WHERE chat_id=? AND end IS NULL", (chat_id,)
            ).fetchone():
                self.db.execute(
                    "INSERT INTO gaps(chat_id,start,reason) VALUES(?,?,?)",
                    (chat_id, start or now, reason),
                )

    def stats(self, start: float, end: float, cumulative_start: float) -> list[dict]:
        """Compute rankings including enabled zero-message personnel.

        Args:
            start: Inclusive ranking window start.
            end: Exclusive ranking window end.
            cumulative_start: Inclusive cumulative-day window start.

        Returns:
            Count, cumulative count, and cumulative distinct groups per person.
        """
        rows = self.db.execute(
            """
            SELECT t.id,t.name,
                SUM(CASE WHEN m.sent>=? THEN 1 ELSE 0 END) AS count,
                COUNT(m.message_id) AS cumulative,
                COUNT(DISTINCT m.chat_id) AS groups
            FROM targets t LEFT JOIN messages m
                ON m.user_id=t.id AND m.sent>=? AND m.sent<?
            WHERE t.kind='people'
            GROUP BY t.id HAVING t.enabled=1 OR COUNT(m.message_id)>0
            ORDER BY count DESC,t.id ASC
        """,
            (start, cumulative_start, end),
        )
        return [dict(r) for r in rows]

    def messages(
        self, start: float, end: float, person: int, group: int, page: int
    ) -> dict:
        """Return a stable bounded page of original records.

        Args:
            start: Inclusive UTC timestamp.
            end: Exclusive UTC timestamp.
            person: User ID, or zero for all people.
            group: Group ID, or zero for all groups.
            page: Zero-based page index.

        Returns:
            At most ten items and a next-page indicator.
        """
        rows = self.db.execute(
            """
            SELECT m.*,t.name AS group_name,p.name AS person_name FROM messages m
            JOIN targets t ON t.kind='groups' AND t.id=m.chat_id
            JOIN targets p ON p.kind='people' AND p.id=m.user_id
            WHERE sent>=? AND sent<? AND (?=0 OR user_id=?) AND (?=0 OR chat_id=?)
            ORDER BY sent DESC,chat_id DESC,message_id DESC LIMIT 11 OFFSET ?
        """,
            (start, end, person, person, group, group, page * 10),
        ).fetchall()
        items = []
        for row in rows[:10]:
            item = dict(row)
            for field in ("sent", "edited"):
                if item[field] is not None:
                    item[field] = datetime.fromtimestamp(
                        item[field], timezone.utc
                    ).isoformat()
            items.append(item)
        return {"items": items, "has_next": len(rows) > 10, "page": page}
