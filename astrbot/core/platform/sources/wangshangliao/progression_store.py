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

    def reserve(self, operation, account, group, member, message):
        """Atomically reserve an action and read prior successful recalls.

        Args:
            operation: Stable idempotency key.
            account: Frozen real robot identity.
            group: Business group identity.
            member: Real target identity.
            message: Upstream message identity.

        Returns:
            Successful warning count and a prior result, if already reserved.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            warnings = db.execute(
                "SELECT COUNT(*) FROM progressive_warnings WHERE account=? AND group_id=? "
                "AND member=? AND status IN ('accepted','verified')",
                (account, group, member),
            ).fetchone()[0]
            previous = db.execute(
                "SELECT result FROM progressive_actions WHERE operation=?", (operation,)
            ).fetchone()
            if previous:
                return warnings, json.loads(previous[0])
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

    def warning(self, account, group, member, message, status):
        """Record the recall receipt without incrementing failed warnings."""
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO progressive_warnings VALUES(?,?,?,?,?)",
                (account, group, member, message, status),
            )

    def finish(self, operation, recall_status, result):
        """Persist independent action outcomes, retaining unknown reservations."""
        with self.connect() as db:
            db.execute(
                "UPDATE progressive_actions SET recall_status=?,result=? WHERE operation=?",
                (
                    recall_status,
                    json.dumps({**result, "recall_status": recall_status}),
                    operation,
                ),
            )
