"""Synchronous review transactions intended exclusively for database workers."""

import hashlib
import json
import math
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

REVIEW_RETENTION_SECONDS = 90 * 86400
_RECEIPT_FINAL = frozenset({"accepted", "verified", "rejected"})
_CONTENT_FINAL = frozenset(
    {
        "warned",
        "muted",
        "kicked",
        "recalled",
        "execution_rejected",
        "preflight_failed",
        "historical_or_unknown_time",
        "identity_unverified",
        "administrator_exempt",
        "disabled",
    }
)


class ReviewStore:
    """Keep review schemas and transaction boundaries out of message handlers."""

    def __init__(self, path):
        self.path = Path(path)
        self.schema_lock = threading.Lock()
        self.initialized = False
        self.maintenance_lock = threading.Lock()
        self.retention_cursors = {}
        self.retention_turns = {}
        self.retention_stats = {"account": "", "scanned": 0, "skipped": ""}
        self.receipt_cursor = {}

    @contextmanager
    def connect(self, *, timeout=10):
        """Initialize once per repository and close each worker-owned connection."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=timeout)
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
                        CREATE TABLE IF NOT EXISTS flood_warning_evidence(
                            operation TEXT PRIMARY KEY,messages TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS moderation_action_audit(
                            operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,
                            member TEXT,message TEXT,action TEXT,status TEXT,
                            submitted INTEGER,error TEXT,provider_code INTEGER,
                            created REAL);
                        CREATE INDEX IF NOT EXISTS moderation_action_group
                            ON moderation_action_audit(account,group_id,created DESC);
                        CREATE INDEX IF NOT EXISTS moderation_recall_unresolved
                            ON moderation_action_audit(account,action,status,created,operation);
                        CREATE TABLE IF NOT EXISTS content_rule_errors(
                            account TEXT,group_id TEXT,message TEXT,error TEXT,
                            provider_code INTEGER,
                            PRIMARY KEY(account,group_id,message));
                        CREATE TABLE IF NOT EXISTS review_retention(
                            account TEXT PRIMARY KEY,flood_before REAL NOT NULL);
                        CREATE INDEX IF NOT EXISTS content_retention
                            ON content_violations(account,observed);
                        CREATE INDEX IF NOT EXISTS semantic_retention
                            ON semantic_reviews(account,created);
                        CREATE INDEX IF NOT EXISTS flood_retention
                            ON flood_batches(account,last_seen);
                        CREATE INDEX IF NOT EXISTS action_retention
                            ON moderation_action_audit(account,created);
                        CREATE INDEX IF NOT EXISTS action_message_retention
                            ON moderation_action_audit(account,group_id,message,status);
                        CREATE INDEX IF NOT EXISTS flood_trigger_retention
                            ON flood_batches(account,group_id,trigger);
                        CREATE INDEX IF NOT EXISTS flood_unresolved_recalls
                            ON flood_recalls(batch)
                            WHERE status IS NULL
                            OR status NOT IN ('accepted','verified','rejected');
                    """)
                    for reference in (
                        "automatic_mutes",
                        "progressive_actions",
                        "progressive_warnings",
                    ):
                        if db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (reference,),
                        ).fetchone():
                            db.execute(
                                f"CREATE INDEX IF NOT EXISTS review_ref_{reference} "
                                f"ON {reference}(account,group_id,message)"
                            )
                    self.path.chmod(0o600)
                    self.initialized = True
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        """Prepare schemas and existing sanction-reference indexes before activation.

        Call again after initializing newly added progressive/automatic schemas.
        Index construction belongs to this explicit worker phase, not maintenance.
        """
        with self.connect() as db:
            with self.schema_lock:
                references = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                        "('automatic_mutes','progressive_actions','progressive_warnings')"
                    )
                }
                for reference in sorted(references):
                    db.execute(
                        f"CREATE INDEX IF NOT EXISTS review_ref_{reference} "
                        f"ON {reference}(account,group_id,message)"
                    )

    def _retention_pending(self, db, account, group, message, references):
        """Find unresolved references through indexed exact-message lookups.

        Args:
            db: Maintenance transaction connection.
            account: Frozen account owning the candidate.
            group: Candidate's exact business group.
            message: Original message identity.
            references: Existing external sanction tables with indexed message keys.

        Returns:
            Whether any review-owned evidence still has an uncertain outcome.
        """
        pending = (
            db.execute(
                """
            SELECT 1 FROM content_violations
            WHERE account=? AND group_id=? AND message=? AND (
                status IS NULL OR status NOT IN (
                    'warned','muted','kicked','recalled','execution_rejected',
                    'preflight_failed','historical_or_unknown_time',
                    'identity_unverified','administrator_exempt','disabled')
                OR warning IS NULL
                OR warning NOT IN ('none','accepted','verified','rejected'))
            UNION ALL
            SELECT 1 FROM moderation_action_audit
            WHERE account=? AND group_id=? AND message=? AND (
                status IS NULL OR status NOT IN ('accepted','verified','rejected'))
            UNION ALL
            SELECT 1 FROM flood_recalls r LEFT JOIN flood_batches b
                ON b.operation=r.batch AND b.account=r.account AND b.group_id=r.group_id
            WHERE r.account=? AND r.group_id=? AND r.message=? AND (
                r.status IS NULL OR r.status NOT IN ('accepted','verified','rejected')
                OR b.operation IS NULL OR b.mute_status IS NULL
                OR b.mute_status NOT IN ('none','accepted','verified','rejected')
                OR EXISTS(SELECT 1 FROM flood_recalls sibling WHERE sibling.batch=b.operation
                    AND (sibling.status IS NULL
                        OR sibling.status NOT IN ('accepted','verified','rejected'))))
            UNION ALL
            SELECT 1 FROM flood_batches b
            WHERE b.account=? AND b.group_id=? AND b.trigger=? AND (
                b.mute_status IS NULL
                OR b.mute_status NOT IN ('none','accepted','verified','rejected')
                OR EXISTS(SELECT 1 FROM flood_recalls r WHERE r.batch=b.operation
                    AND (r.status IS NULL
                        OR r.status NOT IN ('accepted','verified','rejected'))))
            LIMIT 1
            """,
                (account, group, message) * 4,
            ).fetchone()
            is not None
        )
        if pending:
            return True
        for table in ("automatic_mutes", "progressive_warnings"):
            if (
                table in references
                and db.execute(
                    f"SELECT 1 FROM {table} WHERE account=? AND group_id=? AND message=? "
                    "AND (status IS NULL OR status NOT IN ('accepted','verified','rejected','archived')) "
                    "LIMIT 1",
                    (account, group, message),
                ).fetchone()
            ):
                return True
        if "progressive_actions" in references:
            for recall, encoded in db.execute(
                "SELECT recall_status,result FROM progressive_actions "
                "WHERE account=? AND group_id=? AND message=?",
                (account, group, message),
            ):
                if recall not in _RECEIPT_FINAL | {"none"}:
                    return True
                try:
                    if not isinstance(encoded, str) or len(encoded) > 8192:
                        return True
                    result = json.loads(encoded)
                    if (
                        not isinstance(result, dict)
                        or result.get("status") not in _RECEIPT_FINAL
                    ):
                        return True
                except (ValueError, RecursionError):
                    return True
        return False

    def prune(self, account, *, now=None, limit=128, guard=None):
        """Reclaim old, completed working records from a database worker only.

        Native sanctions, legacy and progressive deduplication are never touched.
        Unknown outcomes and their review-owned references have no expiry. Content
        callers must reject stale original message times before calling observe.

        Args:
            account: Frozen adapter account; other accounts are never modified.
            now: Trusted maintenance wall time, defaulting to the current time.
            limit: Total candidate budget, from 1 to 512. Associated flood recalls
                have a separate equal budget; errors/evidence are removed with owners.
            guard: Synchronous identity guard checked before IO and before commit.

        Returns:
            Only per-table delete counts. Last-pass scanned counts and a skipped
            reason are available separately in retention_stats.
            Uninitialized/missing stores are not created. Writer contention or
            exhausted SQL time budgets roll back the entire maintenance pass.

        Raises:
            ValueError: If the account, clock or budget is invalid.
            Exception: Identity revocation or unexpected database failures.
        """
        now = time.time() if now is None else now
        if (
            not isinstance(account, str)
            or not account
            or type(limit) is not int
            or not 1 <= limit <= 512
            or type(now) not in (int, float)
            or not math.isfinite(now)
            or now <= 0
        ):
            raise ValueError("review_retention_scope")
        tables = (
            "content_violations",
            "semantic_reviews",
            "moderation_action_audit",
            "flood_batches",
        )
        counts = dict.fromkeys(
            (*tables, "content_rule_errors", "flood_recalls", "flood_warning_evidence"),
            0,
        )
        scanned = 0
        self.retention_stats = {"account": account, "scanned": 0, "skipped": "aborted"}
        if not self.initialized:
            self.retention_stats["skipped"] = "uninitialized"
            return counts
        if self.path.is_symlink() or not self.path.is_file():
            self.retention_stats["skipped"] = "missing"
            return counts
        if not self.maintenance_lock.acquire(blocking=False):
            self.retention_stats["skipped"] = "busy"
            return counts
        db = None
        cursors = dict(self.retention_cursors)
        turn = self.retention_turns.get(account, 0)
        try:
            if guard:
                guard()
            db = sqlite3.connect(
                self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=0.05
            )
            schema = dict(
                db.execute(
                    "SELECT name,type FROM sqlite_master WHERE name IN "
                    "('automatic_mutes','progressive_actions','progressive_warnings',"
                    "'review_ref_automatic_mutes','review_ref_progressive_actions',"
                    "'review_ref_progressive_warnings','operations')"
                )
            )
            references = {
                name
                for name, kind in schema.items()
                if kind == "table" and name != "operations"
            }
            if any(schema.get("review_ref_" + name) != "index" for name in references):
                self.retention_stats["skipped"] = "unindexed"
                return counts
            db.execute("BEGIN IMMEDIATE")
            deadline = time.monotonic() + 0.05
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            cutoff = max(0, now - REVIEW_RETENTION_SECONDS)
            db.execute(
                "INSERT INTO review_retention VALUES(?,?) "
                "ON CONFLICT(account) DO UPDATE SET "
                "flood_before=MAX(flood_before,excluded.flood_before)",
                (account, cutoff),
            )
            child_budget = limit
            for table in tables[turn:] + tables[:turn]:
                budget = min(max(1, limit // len(tables)), limit - scanned)
                if not budget:
                    break
                clock = (
                    "observed"
                    if table == "content_violations"
                    else "last_seen"
                    if table == "flood_batches"
                    else "created"
                )
                fields = (
                    "status,warning"
                    if table == "content_violations"
                    else "mute_status,operation"
                    if table == "flood_batches"
                    else "status,operation"
                    if table == "moderation_action_audit"
                    else "NULL,NULL"
                )
                message = "trigger" if table == "flood_batches" else "message"
                key = (account, table)
                position = cursors.get(key, (0, 0))
                select = (
                    f"SELECT rowid,{clock},group_id,{message},{fields} FROM {table} "
                )
                # Separate the equal-clock tail so rowid also participates in the
                # index seek when many records share an imported timestamp.
                rows = (
                    db.execute(
                        select + f"WHERE account=? AND {clock}=? AND rowid>? "
                        f"AND {clock}<? ORDER BY rowid LIMIT ?",
                        (account, *position, cutoff, budget),
                    ).fetchall()
                    if position[0] > 0
                    else []
                )
                if len(rows) < budget:
                    rows.extend(
                        db.execute(
                            select + f"WHERE account=? AND {clock}>? AND {clock}<? "
                            f"ORDER BY {clock},rowid LIMIT ?",
                            (account, position[0], cutoff, budget - len(rows)),
                        ).fetchall()
                    )
                exhausted = len(rows) < budget
                for rowid, stamp, group, mid, status, detail in rows:
                    scanned += 1
                    cursors[key] = (stamp, rowid)
                    if (
                        table == "content_violations"
                        and (
                            status not in _CONTENT_FINAL
                            or detail not in _RECEIPT_FINAL | {"none"}
                        )
                        or table == "moderation_action_audit"
                        and status not in _RECEIPT_FINAL
                        or table == "flood_batches"
                        and status not in _RECEIPT_FINAL | {"none"}
                        or mid
                        and self._retention_pending(db, account, group, mid, references)
                    ):
                        continue
                    if table in {"moderation_action_audit", "flood_batches"}:
                        pending_operation = False
                        for reference in sorted(references - {"progressive_warnings"}):
                            source = db.execute(
                                f"SELECT message FROM {reference} WHERE operation=? "
                                "AND account=? AND group_id=?",
                                (detail, account, group),
                            ).fetchone()
                            if source and self._retention_pending(
                                db, account, group, source[0], references
                            ):
                                pending_operation = True
                                break
                        if "operations" in schema:
                            receipt = db.execute(
                                "SELECT result FROM operations WHERE id=?", (detail,)
                            ).fetchone()
                            if receipt:
                                try:
                                    encoded = receipt[0]
                                    if (
                                        not isinstance(encoded, str)
                                        or len(encoded) > 8192
                                    ):
                                        pending_operation = True
                                    else:
                                        saved = json.loads(encoded)
                                        if (
                                            not isinstance(saved, dict)
                                            or saved.get("status") not in _RECEIPT_FINAL
                                        ):
                                            pending_operation = True
                                except (ValueError, RecursionError):
                                    pending_operation = True
                        if pending_operation:
                            continue
                    if (
                        table == "moderation_action_audit"
                        and db.execute(
                            "SELECT 1 FROM flood_batches b WHERE operation=? AND account=? "
                            "AND (mute_status IS NULL OR mute_status NOT IN "
                            "('none','accepted','verified','rejected') OR EXISTS("
                            "SELECT 1 FROM flood_recalls WHERE batch=b.operation AND "
                            "(status IS NULL OR status NOT IN ('accepted','verified','rejected'))))",
                            (detail, account),
                        ).fetchone()
                    ):
                        continue
                    if table == "flood_batches":
                        if not child_budget:
                            cursors[key] = (stamp, rowid - 1)
                            exhausted = False
                            break
                        if (
                            db.execute(
                                "SELECT 1 FROM flood_recalls WHERE batch=? AND "
                                "(status IS NULL OR status NOT IN ('accepted','verified','rejected')) "
                                "LIMIT 1",
                                (detail,),
                            ).fetchone()
                            or db.execute(
                                "SELECT 1 FROM moderation_action_audit WHERE operation=? "
                                "AND account=? AND (status IS NULL OR "
                                "status NOT IN ('accepted','verified','rejected'))",
                                (detail, account),
                            ).fetchone()
                        ):
                            continue
                        recalls = db.execute(
                            "SELECT rowid,account,group_id,message FROM flood_recalls "
                            "WHERE batch=? LIMIT ?",
                            (detail, child_budget),
                        ).fetchall()
                        child_budget -= len(recalls)
                        if any(
                            owner != account
                            or self._retention_pending(
                                db, account, recall_group, recall_mid, references
                            )
                            for _, owner, recall_group, recall_mid in recalls
                        ):
                            continue
                        for recall_id, _, _, _ in recalls:
                            counts["flood_recalls"] += db.execute(
                                "DELETE FROM flood_recalls WHERE rowid=? AND account=?",
                                (recall_id, account),
                            ).rowcount
                        if db.execute(
                            "SELECT 1 FROM flood_recalls WHERE batch=? LIMIT 1",
                            (detail,),
                        ).fetchone():
                            continue
                        counts["flood_warning_evidence"] += db.execute(
                            "DELETE FROM flood_warning_evidence WHERE operation=?",
                            (detail,),
                        ).rowcount
                    elif table == "content_violations":
                        counts["content_rule_errors"] += db.execute(
                            "DELETE FROM content_rule_errors WHERE account=? "
                            "AND group_id=? AND message=?",
                            (account, group, mid),
                        ).rowcount
                    counts[table] += db.execute(
                        f"DELETE FROM {table} WHERE rowid=? AND account=?",
                        (rowid, account),
                    ).rowcount
                if exhausted:
                    cursors[key] = (0, 0)
            db.set_progress_handler(None, 0)
            if guard:
                guard()
            db.commit()
            self.retention_cursors = cursors
            self.retention_turns[account] = (turn + 1) % len(tables)
            self.retention_stats = {
                "account": account,
                "scanned": scanned,
                "skipped": "",
            }
            return counts
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 255
            if code in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
                sqlite3.SQLITE_INTERRUPT,
            ):
                self.retention_stats = {
                    "account": account,
                    "scanned": 0,
                    "skipped": "budget" if code == sqlite3.SQLITE_INTERRUPT else "busy",
                }
                return dict.fromkeys(counts, 0)
            raise
        finally:
            if db is not None:
                db.set_progress_handler(None, 0)
                if db.in_transaction:
                    db.rollback()
                db.close()
            self.maintenance_lock.release()

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
                "AND status IN ('claimed','warned','muted','unknown') "
                "AND category NOT IN "
                "('restricted_video','restricted_forward','image_qr','image_digits',"
                "'consecutive_digits')",
                (account, group, sender, since, now - 86400, mid),
            ).fetchone()
            db.execute(
                "UPDATE content_violations SET status='claimed' WHERE account=? AND group_id=? AND message=?",
                (account, group, mid),
            )
            return row, prior

    def finish(
        self, account, group, mid, status, warning, *, error="", provider_code=None
    ):
        """Persist independent action outcomes without replaying an unknown action."""
        with self.connect() as db:
            db.execute(
                "UPDATE content_violations SET status=?,warning=? WHERE account=? AND group_id=? AND message=?",
                (status, warning, account, group, mid),
            )
            if error:
                db.execute(
                    "INSERT OR REPLACE INTO content_rule_errors VALUES(?,?,?,?,?)",
                    (account, group, mid, error, provider_code),
                )

    def content_outcome(self, account, group, mid):
        """Read the persisted outcome, not the handler's consumed-message flag."""
        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM content_violations "
                "WHERE account=? AND group_id=? AND message=?",
                (account, group, mid),
            ).fetchone()
            return row[0] if row else "not_applied"

    def flood_continuation(
        self, account, group, sender, peer, revision, received_at, *, scope=""
    ):
        """Identify a finished batch whose mute cannot be submitted again.

        Args:
            account: Original collector account.
            group: Exact business group.
            sender: Authenticated sender identity.
            peer: Original transport identity.
            revision: Current group policy revision.
            received_at: Original trigger ingress time.
            scope: Optional isolated test generation.

        Returns:
            Whether a completed trigger permits skipping preliminary roster IO.
            This never authorizes an action; new recalls still recheck native roles.
        """
        if scope:
            revision = "test/" + json.dumps([revision, scope], separators=(",", ":"))
        with self.connect() as db:
            row = db.execute(
                "SELECT b.mute_status,v.status FROM flood_batches b "
                "LEFT JOIN content_violations v ON v.account=b.account "
                "AND v.group_id=b.group_id AND v.message=b.trigger "
                "AND v.sender=b.sender AND v.category='message_flood' "
                "WHERE b.account=? AND b.group_id=? AND b.sender=? AND b.peer=? "
                "AND b.revision=? AND b.last_seen>=? "
                "ORDER BY b.last_seen DESC LIMIT 1",
                (account, group, sender, peer, revision, received_at - 30),
            ).fetchone()
            return bool(
                row
                and row[0] in {"accepted", "verified", "rejected", "unknown"}
                and row[1] in {"muted", "execution_rejected", "unknown"}
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
        windows=(),
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
            windows: Verified (threshold, message IDs) groups for each live rule.

        Returns:
            Batch ID, prior mute status or None, and newly reserved recall IDs.
        """
        if scope:
            # Keep the existing schema and production revision values unchanged.
            revision = "test/" + json.dumps([revision, scope], separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            horizon = db.execute(
                "SELECT flood_before FROM review_retention WHERE account=?", (account,)
            ).fetchone()
            if horizon and received_at <= horizon[0]:
                return operation, "unknown", []
            prior = db.execute(
                "SELECT operation,mute_status,trigger FROM flood_batches "
                "WHERE account=? AND group_id=? AND sender=? AND peer=? AND revision=? "
                "AND last_seen>=? ORDER BY last_seen DESC LIMIT 1",
                (account, group, sender, peer, revision, received_at - 30),
            ).fetchone()
            if prior and prior[1] == "none" and windows:
                # Reuse old recall reservations, but do not merge new, disjoint
                # qualifying bursts into one warning for as long as spam continues.
                if not db.execute(
                    "SELECT 1 FROM flood_warning_evidence WHERE operation=?",
                    (prior[0],),
                ).fetchone():
                    # Freeze legacy evidence once; later individual recalls are
                    # not additional warnings until a fresh threshold is met.
                    legacy = [
                        row[0]
                        for row in db.execute(
                            "SELECT message FROM flood_recalls WHERE batch=? "
                            "ORDER BY rowid DESC LIMIT 256",
                            (prior[0],),
                        )
                    ]
                    db.execute(
                        "INSERT INTO flood_warning_evidence VALUES(?,?)",
                        (prior[0], json.dumps(legacy)),
                    )
                warned = db.execute(
                    "SELECT 1 FROM flood_recalls WHERE account=? AND group_id=? "
                    "AND message=? AND batch=? AND status IN ('accepted','verified')",
                    (account, group, prior[2], prior[0]),
                ).fetchone()
                counted = set()
                for (encoded,) in db.execute(
                    "SELECT e.messages FROM flood_warning_evidence e "
                    "JOIN flood_batches b ON b.operation=e.operation "
                    "WHERE b.account=? AND b.group_id=? AND b.sender=? AND b.peer=? "
                    "AND b.revision=? AND b.last_seen>=?",
                    (account, group, sender, peer, revision, received_at - 30),
                ):
                    counted.update(json.loads(encoded))
                fresh = set(messages) - counted
                if (
                    warned
                    and mid in fresh
                    and any(
                        len(fresh.intersection(candidates)) >= threshold
                        for threshold, candidates in windows
                    )
                ):
                    prior = None
            if prior:
                operation, mute_status = prior[:2]
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
                db.execute(
                    "INSERT INTO flood_warning_evidence VALUES(?,?)",
                    (operation, json.dumps(messages)),
                )
                db.execute(
                    "DELETE FROM flood_warning_evidence WHERE operation IN "
                    "(SELECT operation FROM flood_batches WHERE account=? "
                    "AND group_id=? AND last_seen<?)",
                    (account, group, received_at - 86400),
                )
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

    def reserve_warning(self, operation, account, group, member):
        """Reserve one notification without replaying an existing receipt.

        Args:
            operation: Stable warning operation, not a new retry identifier.
            account: Original authenticated business account.
            group: Exact business group.
            member: Original target member.

        Returns:
            Whether a new queued receipt was inserted.
        """
        with self.connect() as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO moderation_action_audit "
                    "VALUES(?,?,?,?,?,'warning','queued',0,'',NULL,?)",
                    (operation, account, group, member, "", time.time()),
                ).rowcount
                == 1
            )

    def cancel_warning(self, operation, account, group, error):
        """Reject only a notification that has not reached the native port.

        Args:
            operation: Existing queued notification operation.
            account: Original authenticated business account.
            group: Exact business group.
            error: Fixed diagnostic label, never exception or message text.
        """
        with self.connect() as db:
            db.execute(
                "UPDATE moderation_action_audit SET status='rejected',error=? "
                "WHERE operation=? AND account=? AND group_id=? "
                "AND action='warning' AND status='queued' AND submitted=0",
                (error, operation, account, group),
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

    def reconcile_recalls(self, account, ledger_path):
        """Refine a bounded audit batch only from matching terminal transport receipts.

        Args:
            account: Frozen account owning both ledgers.
            ledger_path: Existing transport database, opened read-only.

        Returns:
            Number of reconciled rows; no native requests or retries are performed.
        """
        if not self.initialized:
            return 0
        last = self.receipt_cursor.get(account, (0, ""))
        with self.connect(timeout=0.05) as db:
            rows = db.execute(
                "SELECT operation,group_id,member,message,created FROM moderation_action_audit "
                "WHERE account=? AND action='recall' AND status='unknown' "
                "AND (created,operation)>(?,?) ORDER BY created,operation LIMIT 32",
                (account, *last),
            ).fetchall()
        if not rows:
            self.receipt_cursor.pop(account, None)
            return 0
        changes = []
        with closing(
            sqlite3.connect(
                Path(ledger_path).resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.05,
            )
        ) as ledger:
            try:
                for operation, group, member, message, created in rows:
                    self.receipt_cursor[account] = (created, operation)
                    if operation != f"recall/{account}/{group}/{message}":
                        continue
                    receipt = ledger.execute(
                        "SELECT fingerprint,state FROM outbox WHERE key=?", (operation,)
                    ).fetchone()
                    original = ledger.execute(
                        "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
                        (account, group, message),
                    ).fetchone()
                    if not receipt or receipt[1] not in _RECEIPT_FINAL or not original:
                        continue
                    payload = json.loads(original[0])
                    route = payload.get("recall_route")
                    if (
                        str(payload.get("sender")) != member
                        or not isinstance(route, dict)
                        or not route.get("client")
                        or not route.get("peer")
                        or receipt[0]
                        != hashlib.sha256(
                            json.dumps(route, sort_keys=True).encode()
                        ).hexdigest()
                    ):
                        continue
                    changes.append(
                        (
                            receipt[1],
                            1 if receipt[1] in {"accepted", "verified"} else None,
                            operation,
                            account,
                            group,
                            member,
                            message,
                        )
                    )
            finally:
                if ledger.in_transaction:
                    ledger.rollback()
        with self.connect(timeout=0.05) as db:
            for (
                status,
                submitted,
                operation,
                account,
                group,
                member,
                message,
            ) in changes:
                db.execute(
                    "UPDATE moderation_action_audit SET status=?,submitted=COALESCE(?,submitted),error='',provider_code=NULL "
                    "WHERE operation=? AND account=? AND group_id=? AND member=? AND message=? "
                    "AND action='recall' AND status='unknown'",
                    (status, submitted, operation, account, group, member, message),
                )
                db.execute(
                    "UPDATE flood_recalls SET status=? WHERE account=? AND group_id=? "
                    "AND message=? AND status='unknown'",
                    (status, account, group, message),
                )
        return len(changes)

    def has_flood_recall(self, account, group, message):
        """Check an exact reserved recall before allowing an ordinary reply.

        Args:
            account: Original authenticated account.
            group: Original business group.
            message: Exact upstream message.

        Returns:
            Whether cleanup is pending, uncertain or already accepted.
        """
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM flood_recalls WHERE account=? AND group_id=? AND message=? "
                    "AND status IN ('unknown','accepted','verified')",
                    (account, group, message),
                ).fetchone()
                is not None
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
