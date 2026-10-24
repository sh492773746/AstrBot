"""Synchronous review transactions intended exclusively for database workers."""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class ReviewStore:
    """Keep review schemas and transaction boundaries out of message handlers."""

    def __init__(self, path):
        self.path = Path(path)
        self.schema_lock = threading.Lock()
        self.initialized = False

    @contextmanager
    def connect(self):
        """Initialize once per repository and close each worker-owned connection."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with self.schema_lock:
                if not self.initialized:
                    db.executescript("""
                        PRAGMA journal_mode=WAL;
                        CREATE TABLE IF NOT EXISTS content_violations(
                            account TEXT,group_id TEXT,message TEXT,sender TEXT,
                            category TEXT,observed REAL,version REAL,status TEXT,warning TEXT,
                            PRIMARY KEY(account,group_id,message));
                        CREATE INDEX IF NOT EXISTS content_group_observed
                            ON content_violations(account,group_id,observed DESC);
                        CREATE INDEX IF NOT EXISTS content_sender_window
                            ON content_violations(account,group_id,sender,version,observed);
                        CREATE TABLE IF NOT EXISTS semantic_reviews(
                            account TEXT,group_id TEXT,message TEXT,result TEXT NOT NULL,
                            provider TEXT NOT NULL,created REAL NOT NULL,
                            PRIMARY KEY(account,group_id,message));
                        CREATE INDEX IF NOT EXISTS semantic_group_created
                            ON semantic_reviews(account,group_id,created DESC);
                        CREATE TABLE IF NOT EXISTS flood_batches(
                            operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,
                            sender TEXT,peer TEXT,revision TEXT,trigger TEXT,
                            last_seen REAL,mute_status TEXT);
                        CREATE INDEX IF NOT EXISTS flood_member_recent
                            ON flood_batches(account,group_id,sender,peer,revision,last_seen DESC);
                        CREATE INDEX IF NOT EXISTS flood_group_recent
                            ON flood_batches(account,group_id,last_seen DESC);
                        CREATE TABLE IF NOT EXISTS flood_recalls(
                            account TEXT,group_id TEXT,message TEXT,batch TEXT,status TEXT,
                            PRIMARY KEY(account,group_id,message));
                        CREATE INDEX IF NOT EXISTS flood_recall_batch
                            ON flood_recalls(batch);
                        CREATE TABLE IF NOT EXISTS moderation_action_audit(
                            operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,
                            member TEXT,message TEXT,action TEXT,status TEXT,
                            submitted INTEGER,error TEXT,provider_code INTEGER,
                            created REAL);
                        CREATE INDEX IF NOT EXISTS moderation_action_group
                            ON moderation_action_audit(account,group_id,created DESC);
                    """)
                    self.path.chmod(0o600)
                    self.initialized = True
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        """Prepare schemas before business activation."""
        with self.connect():
            pass

    def observe(self, account, group, mid, sender, category, now, since):
        """Reserve new content evidence using the database's unique constraint."""
        with self.connect() as db:
            return bool(
                db.execute(
                    "INSERT OR IGNORE INTO content_violations VALUES(?,?,?,?,?,?,?,'observed','none')",
                    (account, group, mid, sender, category, now, since),
                ).rowcount
            )

    def claim(self, account, group, mid, sender, since, now):
        """Atomically claim legacy warning processing and its prior history."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,warning FROM content_violations WHERE account=? AND group_id=? AND message=?",
                (account, group, mid),
            ).fetchone()
            if row[0] != "observed":
                return row, None
            prior = db.execute(
                "SELECT COUNT(*),MAX(CASE WHEN warning IN ('accepted','verified') THEN 1 ELSE 0 END) "
                "FROM content_violations WHERE account=? AND group_id=? AND sender=? "
                "AND version=? AND observed>? AND message<>? "
                "AND status IN ('claimed','warned','muted','unknown')",
                (account, group, sender, since, now - 86400, mid),
            ).fetchone()
            db.execute(
                "UPDATE content_violations SET status='claimed' WHERE account=? AND group_id=? AND message=?",
                (account, group, mid),
            )
            return row, prior

    def finish(self, account, group, mid, status, warning):
        """Persist independent action outcomes without replaying an unknown action."""
        with self.connect() as db:
            db.execute(
                "UPDATE content_violations SET status=?,warning=? WHERE account=? AND group_id=? AND message=?",
                (status, warning, account, group, mid),
            )

    def reserve_flood(
        self,
        operation,
        account,
        group,
        sender,
        peer,
        revision,
        mid,
        received_at,
        messages,
        *,
        scope="",
    ):
        """Reserve one mute per live burst and each recall before any network IO.

        Args:
            operation: Deterministic proposed batch identifier.
            account: Frozen collector account.
            group: Business group ID.
            sender: Verified ordinary member ID.
            peer: Verified NIM identity.
            revision: Frozen business revision.
            mid: Triggering message ID.
            received_at: Trusted ingress time, not processing time.
            messages: Verified bounded burst message IDs.
            scope: Optional test generation; empty preserves production burst dedup.

        Returns:
            Batch ID, prior mute status or None, and newly reserved recall IDs.
        """
        if scope:
            # Keep the existing schema and production revision values unchanged.
            revision = "test/" + json.dumps([revision, scope], separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT operation,mute_status FROM flood_batches "
                "WHERE account=? AND group_id=? AND sender=? AND peer=? AND revision=? "
                "AND last_seen>=? ORDER BY last_seen DESC LIMIT 1",
                (account, group, sender, peer, revision, received_at - 30),
            ).fetchone()
            if prior:
                operation, mute_status = prior
                db.execute(
                    "UPDATE flood_batches SET last_seen=MAX(last_seen,?) WHERE operation=?",
                    (received_at, operation),
                )
            else:
                db.execute(
                    "INSERT INTO flood_batches VALUES(?,?,?,?,?,?,?,?, 'unknown')",
                    (
                        operation,
                        account,
                        group,
                        sender,
                        peer,
                        revision,
                        mid,
                        received_at,
                    ),
                )
                mute_status = None
            pending = []
            for message in messages:
                if db.execute(
                    "INSERT OR IGNORE INTO flood_recalls VALUES(?,?,?,?,'unknown')",
                    (account, group, message, operation),
                ).rowcount:
                    pending.append(message)
            return operation, mute_status, pending

    def finish_flood(self, operation, status, *, message=None):
        """Keep mute and per-message recall receipts independently.

        Args:
            operation: Original durable flood batch ID.
            status: Accepted, verified, rejected or unknown transport outcome.
            message: Recall ID, or None for the batch mute receipt.
        """
        with self.connect() as db:
            if message is None:
                db.execute(
                    "UPDATE flood_batches SET mute_status=? WHERE operation=?",
                    (status, operation),
                )
            else:
                db.execute(
                    "UPDATE flood_recalls SET status=? WHERE batch=? AND message=?",
                    (status, operation, message),
                )

    def save_action(
        self,
        operation,
        account,
        group,
        member,
        message,
        action,
        status,
        submitted,
        error,
        provider_code,
    ):
        """Retain sanitized action outcomes without rewriting transport history.

        Args:
            operation: Original deterministic operation identifier.
            account: Authenticated business account.
            group: Exact business group.
            member: Original target identity.
            message: Exact source message, when the action carries one.
            action: Fixed native action name.
            status: Independent action outcome.
            submitted: Native submission evidence, or None when unavailable.
            error: Allowlisted diagnostic label.
            provider_code: Bounded numeric provider error, or None.
        """
        with self.connect() as db:
            db.execute(
                "INSERT INTO moderation_action_audit VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(operation) DO UPDATE SET "
                "status=excluded.status,submitted=excluded.submitted,"
                "error=excluded.error,provider_code=excluded.provider_code "
                "WHERE moderation_action_audit.account=excluded.account "
                "AND moderation_action_audit.group_id=excluded.group_id "
                "AND moderation_action_audit.member=excluded.member "
                "AND moderation_action_audit.message=excluded.message "
                "AND moderation_action_audit.action=excluded.action "
                "AND moderation_action_audit.status NOT IN ('accepted','verified') "
                "AND (excluded.status IN ('accepted','verified') "
                "OR excluded.submitted=1 OR moderation_action_audit.submitted IS NOT 1)",
                (
                    operation,
                    account,
                    group,
                    member,
                    message,
                    action,
                    status,
                    submitted,
                    error,
                    provider_code,
                    time.time(),
                ),
            )

    def read_review(self, account, group, mid):
        """Read a frozen semantic decision for this message only."""
        with self.connect() as db:
            return db.execute(
                "SELECT result FROM semantic_reviews WHERE account=? AND group_id=? AND message=?",
                (account, group, mid),
            ).fetchone()

    def save_review(self, account, group, mid, result, provider_id):
        """Persist audit evidence; this method cannot execute a platform action."""
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO semantic_reviews VALUES(?,?,?,?,?,?)",
                (
                    account,
                    group,
                    mid,
                    json.dumps(result, ensure_ascii=False),
                    provider_id,
                    time.time(),
                ),
            )


def review_store(adapter, path):
    """Resolve the one repository belonging to an adapter and its ledger path."""
    store = getattr(adapter, "_review_store", None)
    if not isinstance(store, ReviewStore) or store.path != Path(path):
        store = adapter._review_store = ReviewStore(path)
    return store
