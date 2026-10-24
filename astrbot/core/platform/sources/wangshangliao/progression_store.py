"""Worker-owned transactions for existing progressive sanction evidence."""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class ProgressionStore:
    """Preserve the existing ledger while removing IO from message coroutines."""

    def __init__(self, path):
        self.path = Path(path)
        self.initialized = False
        self.schema_lock = threading.Lock()

    @contextmanager
    def connect(self):
        """Open a worker-local connection and initialize additive schemas once."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with self.schema_lock:
                if not self.initialized:
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS progressive_warnings(
                            account TEXT,group_id TEXT,member TEXT,message TEXT,status TEXT,
                            PRIMARY KEY(account,group_id,member,message));
                        CREATE TABLE IF NOT EXISTS progressive_actions(
                            operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,member TEXT,
                            message TEXT,recall_status TEXT,result TEXT,created REAL);
                        CREATE TABLE IF NOT EXISTS moderation_cycles(
                            account TEXT,group_id TEXT,member TEXT,peer TEXT,
                            warnings INTEGER NOT NULL,reset_after REAL NOT NULL,
                            PRIMARY KEY(account,group_id,member,peer));
                    """)
                    self.initialized = True
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        """Prepare the schema during business activation."""
        with self.connect():
            pass

    def reserve(self, operation, account, group, member, message, peer=""):
        """Atomically reserve an action and read prior successful recalls.

        Args:
            operation: Stable idempotency key.
            account: Frozen real robot identity.
            group: Business group identity.
            member: Real target identity.
            message: Upstream message identity.
            peer: Frozen native identity, isolating replacement bindings.

        Returns:
            Warning count (-1 during the final mute) and a prior result.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT result FROM progressive_actions WHERE operation=?", (operation,)
            ).fetchone()
            if previous:
                return 0, json.loads(previous[0])
            scope = (account, group, member, peer)
            cycle = db.execute(
                "SELECT warnings,reset_after FROM moderation_cycles "
                "WHERE account=? AND group_id=? AND member=? AND peer=?",
                scope,
            ).fetchone()
            if cycle is None:
                # Import legacy grace once, without copying it to a new native identity.
                known = db.execute(
                    "SELECT 1 FROM moderation_cycles WHERE account=? AND group_id=? AND member=?",
                    scope[:3],
                ).fetchone()
                warnings = (
                    0
                    if known
                    else db.execute(
                        "SELECT COUNT(*) FROM progressive_warnings WHERE account=? AND group_id=? "
                        "AND member=? AND status IN ('accepted','verified')",
                        scope[:3],
                    ).fetchone()[0]
                )
                db.execute(
                    "INSERT INTO moderation_cycles VALUES(?,?,?,?,?,0)",
                    (*scope, min(warnings, 3)),
                )
            else:
                warnings, reset_after = cycle
                if (
                    not reset_after
                    and warnings >= 3
                    and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='automatic_mute_durations' AND type='table'"
                    ).fetchone()
                ):
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='operations' AND type='table'"
                    ).fetchone():
                        pending = db.execute(
                            "SELECT m.operation,o.result FROM automatic_mutes m "
                            "JOIN automatic_mute_durations d ON d.operation=m.operation "
                            "JOIN operations o ON o.id=m.operation "
                            "WHERE m.account=? AND m.group_id=? AND m.member=? AND m.peer=? "
                            "AND m.closed=0 AND m.status='unknown' AND d.minutes=10",
                            scope,
                        ).fetchall()
                        for identifier, encoded in pending:
                            receipt = json.loads(encoded)
                            if (
                                receipt.get("action") == "mute"
                                and str(receipt.get("group")) == group
                                and str(receipt.get("member")) == member
                                and receipt.get("status") in {"accepted", "verified"}
                            ):
                                # Recover local bookkeeping, never resubmit a final mute.
                                db.execute(
                                    "UPDATE automatic_mutes SET status=? WHERE operation=?",
                                    (receipt["status"], identifier),
                                )
                                db.execute(
                                    "UPDATE automatic_mute_durations SET accepted_at=? "
                                    "WHERE operation=? AND accepted_at=0",
                                    (time.time(), identifier),
                                )
                    final = db.execute(
                        "SELECT d.accepted_at FROM automatic_mutes m "
                        "JOIN automatic_mute_durations d ON d.operation=m.operation "
                        "WHERE m.account=? AND m.group_id=? AND m.member=? AND m.peer=? "
                        "AND m.closed=0 AND m.status IN ('accepted','verified') "
                        "AND d.minutes=10 AND d.accepted_at>0 ORDER BY d.accepted_at DESC LIMIT 1",
                        scope,
                    ).fetchone()
                    if final:
                        reset_after = final[0] + 600
                        db.execute(
                            "UPDATE moderation_cycles SET reset_after=? "
                            "WHERE account=? AND group_id=? AND member=? AND peer=?",
                            (reset_after, *scope),
                        )
                if reset_after and time.time() >= reset_after:
                    db.execute(
                        "UPDATE moderation_cycles SET warnings=0,reset_after=0 "
                        "WHERE account=? AND group_id=? AND member=? AND peer=?",
                        scope,
                    )
                    db.execute(
                        "UPDATE automatic_mutes SET closed=1 WHERE account=? AND group_id=? "
                        "AND member=? AND peer=? AND status IN ('accepted','verified') AND closed=0",
                        scope,
                    )
                    warnings = 0
                elif reset_after:
                    warnings = -1
            db.execute(
                "INSERT INTO progressive_actions VALUES(?,?,?,?,?,'unknown',?,?)",
                (
                    operation,
                    account,
                    group,
                    member,
                    message,
                    json.dumps({"status": "unknown", "recall_status": "unknown"}),
                    time.time(),
                ),
            )
            return warnings, None

    def warning(self, account, group, member, message, status, peer=""):
        """Record the recall receipt without incrementing failed warnings."""
        with self.connect() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO progressive_warnings VALUES(?,?,?,?,?)",
                (account, group, member, message, status),
            )
            if inserted.rowcount and status in {"accepted", "verified"}:
                db.execute(
                    "UPDATE moderation_cycles SET warnings=MIN(warnings+1,3) "
                    "WHERE account=? AND group_id=? AND member=? AND peer=?",
                    (account, group, member, peer),
                )

    def finish(self, operation, recall_status, result, peer=""):
        """Persist independent action outcomes, retaining unknown reservations."""
        with self.connect() as db:
            if (
                result.get("action") == "mute"
                and result.get("status") in {"accepted", "verified"}
                and result.get("minutes") == 10
            ):
                row = db.execute(
                    "SELECT account,group_id,member,result FROM progressive_actions WHERE operation=?",
                    (operation,),
                ).fetchone()
                if row and json.loads(row[3]).get("minutes") != 10:
                    db.execute(
                        "UPDATE moderation_cycles SET reset_after=? "
                        "WHERE account=? AND group_id=? AND member=? AND peer=?",
                        (time.time() + 600, *row[:3], peer),
                    )
            db.execute(
                "UPDATE progressive_actions SET recall_status=?,result=? WHERE operation=?",
                (
                    recall_status,
                    json.dumps({**result, "recall_status": recall_status}),
                    operation,
                ),
            )
