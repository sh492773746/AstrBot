"""Cashback is a manual-payment register, never a payment gateway."""

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot import logger
from astrbot.utils.text_redaction import redact_reply

from .cashback_finance import (
    FINANCE_TRANSACTION,
    DashboardFinanceActor,
    FinanceActor,
    FinanceStoreMixin,
)

REQUEST_TRACE = ContextVar("cashback_request_trace", default="")
WRITE_GUARD = ContextVar("cashback_write_guard", default=None)
BEIJING = ZoneInfo("Asia/Shanghai")

# Runtime reads must not depend on an older persisted view definition.
ADMISSION_CHECK_SQL = """
    SELECT r.id,
        EXISTS (
            SELECT 1 FROM current_invitation_evidence p
            WHERE p.group_id=r.group_id AND p.member=r.member
            AND p.source IN ('business_apply_log','cashier_confirmation')
            AND p.member_peer=r.peer AND p.inviter=r.inviter
            AND p.inviter_peer=r.inviter_peer
            AND p.collector_account=g.account
            AND p.event_at>0 AND p.event_at>=r.evidence_started
        ) AND NOT EXISTS (
            SELECT 1 FROM current_invitation_evidence p
            WHERE p.group_id=r.group_id AND p.member=r.member
            AND p.source IN ('business_apply_log','cashier_confirmation')
            AND p.member_peer=r.peer AND p.collector_account=g.account
            AND p.event_at>0 AND p.event_at>=r.evidence_started
            AND (p.inviter<>r.inviter OR p.inviter_peer<>r.inviter_peer)
        ) AND NOT EXISTS (
            SELECT 1 FROM cashback_membership_events m
            WHERE m.group_id=r.group_id AND m.bot_id=g.bot_id
            AND m.account=g.account AND m.member=r.member
            AND m.event_at>=COALESCE(NULLIF(r.evidence_event_at,0),r.evidence_started)
            AND (m.apply_type IN ('LEAVE','KICK','KICK_MEMBER','REMOVE')
                OR m.state IN ('MEMBER_STATE_LEFT','MEMBER_STATE_KICKED','MEMBER_STATE_REMOVED')
                OR m.state='MEMBER_STATE_GOOD' AND (
                    m.apply_type<>'INVITATION' OR m.peer<>r.peer
                    OR m.inviter<>r.inviter OR m.inviter_peer<>r.inviter_peer
                    OR r.evidence_event_id<>'' AND m.event_id<>r.evidence_event_id))
        ) AND NOT EXISTS (
            SELECT 1 FROM cashback_source_quarantine q
            WHERE q.group_id=r.group_id AND q.bot_id=g.bot_id
            AND q.account=g.account AND q.member=r.member
            AND q.reason='invalid_member_event'
            AND (q.event_at=0 OR q.event_at>=r.evidence_started)
        ) AS payable_evidence
    FROM entries r JOIN groups g ON g.id=r.group_id
"""


class CashbackError(Exception):
    def __init__(self, code, text, status=400):
        super().__init__(code)
        self.code, self.text, self.status = code, text, status


def entry_time_window(period, now=None, start_at=None, end_at=None):
    """Return the current Beijing time window used by the ledger filter."""
    if period not in {"all", "month", "day", "hour", "custom"}:
        raise CashbackError("invalid_query", "时间筛选无效。")
    has_custom_bounds = start_at is not None or end_at is not None
    if period != "custom" and has_custom_bounds:
        raise CashbackError("invalid_query", "自定义时间范围参数只能用于自定义筛选。")
    if period == "custom":
        if (
            type(start_at) not in {int, float}
            or type(end_at) not in {int, float}
            or not math.isfinite(start_at)
            or not math.isfinite(end_at)
            or start_at < 0
            or end_at <= start_at
        ):
            raise CashbackError("invalid_query", "请输入有效的开始和结束时间。")
        return start_at, end_at
    if period == "all":
        return None
    local = datetime.fromtimestamp(time.time() if now is None else now, BEIJING)
    if period == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    elif period == "day":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    else:
        start = local.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
    return start.timestamp(), end.timestamp()


def email(value):
    value = str(value).strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise CashbackError("invalid_email", "请输入有效的登录邮箱。")
    return value


def label(value, limit=100):
    return "".join(c for c in str(value) if c.isprintable()).strip()[:limit]


def avatar_category(member):
    """Classify an invitee from explicit avatar-digit evidence only.

    Args:
        member: Normalized invitee data. ``avatar_digit_count`` must be
            produced by a trusted image recognizer; opaque avatar resource
            identifiers are deliberately ignored.

    Returns:
        A tuple of pricing category, digit count, and evidence status.
        Missing image evidence is conservatively treated as ordinary pricing,
        never as an agent.
    """
    count = member.get("avatar_digit_count")
    if type(count) is int and 0 <= count <= 128:
        return ("agent" if count > 0 else "ordinary"), count, "detected"
    detected = member.get("avatar_digits_detected")
    if type(detected) is bool:
        return ("agent" if detected else "ordinary"), int(detected), "detected"
    return "ordinary", 0, "unavailable"


def avatar_reference(member):
    """Return a bounded opaque avatar reference without treating it as text."""
    return label(member.get("avatar_ref") or member.get("userAvatar") or "", 128)


def member_pricing(member, policy):
    """Build the default pricing decision without automatic avatar classification.

    Agent pricing is a cashier decision after external proof. The collector
    must never infer it from avatar resources or untrusted profile fields.
    """
    category, digits, status = "ordinary", 0, "manual"
    if dict(policy).get("agent_only"):
        return category, digits, status, 0, "no_reward"
    amount = policy["rate"]
    pricing = "priced" if amount is not None else "unconfigured"
    return category, digits, status, amount if pricing == "priced" else 0, pricing


def source_timestamp(text):
    """Parse the upstream event time without substituting observation time.

    Args:
        text: Numeric seconds/milliseconds or an ISO timestamp.

    Returns:
        A finite positive Unix timestamp.

    Raises:
        ValueError: If the timestamp cannot be trusted.
    """
    if not isinstance(text, str) or not 1 <= len(text) <= 64:
        raise ValueError("invalid_source_timestamp")
    if text.replace(".", "", 1).isdigit():
        stamp = float(text)
        if stamp > 100000000000:
            stamp /= 1000
    else:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        stamp = (
            parsed if parsed.tzinfo else parsed.replace(tzinfo=BEIJING)
        ).timestamp()
    if not math.isfinite(stamp) or not 0 < stamp <= time.time() + 30:
        raise ValueError("invalid_source_timestamp")
    return stamp


class CashbackStore(FinanceStoreMixin):
    def __init__(self, path, *, read_only=False, reuse_reads=False):
        """Open a ledger, optionally retaining exclusively leased idle readers.

        Args:
            path: Local SQLite ledger path.
            read_only: Skip all migrations and reject write admission.
            reuse_reads: Keep at most four idle read-only connections and one
                short, write-invalidated paid display per connection. Authority
                is always read again; unpaid qualification is never cached.
        """
        self.path = Path(path)
        self._read_only = read_only
        self._reuse_reads = False
        self._readers = []
        self._paid_pages = {}
        self._reader_lock = threading.Lock()
        if read_only:
            self._reuse_reads = reuse_reads
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            self.finance_schema(db)
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users(
                    email TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, subject TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS groups(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, bot_id TEXT NOT NULL,
                    account TEXT NOT NULL, cashier TEXT NOT NULL, enabled INTEGER NOT NULL,
                    rate INTEGER, started REAL NOT NULL, version INTEGER NOT NULL,
                    rate_since REAL NOT NULL DEFAULT 0,
                    scanned REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(cashier) REFERENCES users(email));
                CREATE TABLE IF NOT EXISTS entries(
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, member TEXT NOT NULL,
                    member_name TEXT NOT NULL, peer TEXT NOT NULL, inviter TEXT NOT NULL DEFAULT '',
                    inviter_name TEXT NOT NULL DEFAULT '', inviter_peer TEXT NOT NULL DEFAULT '',
                    first_seen REAL NOT NULL, amount INTEGER NOT NULL, eligible INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', verification TEXT NOT NULL DEFAULT 'pending',
                    present INTEGER NOT NULL DEFAULT 1, ever_left INTEGER NOT NULL DEFAULT 0,
                    left_at REAL, last_seen REAL NOT NULL, paid_at REAL, paid_by TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(group_id,member), FOREIGN KEY(group_id) REFERENCES groups(id));
                CREATE TABLE IF NOT EXISTS evidence(
                    group_id TEXT NOT NULL, member TEXT NOT NULL, source TEXT NOT NULL,
                    event_id TEXT NOT NULL, inviter TEXT NOT NULL, observed REAL NOT NULL,
                    PRIMARY KEY(group_id,member,source,event_id,inviter));
                CREATE TABLE IF NOT EXISTS cashback_inviter_risk(
                    group_id TEXT NOT NULL, account TEXT NOT NULL, inviter TEXT NOT NULL,
                    warning_count INTEGER NOT NULL DEFAULT 0,
                    processed_event_at REAL NOT NULL DEFAULT 0,
                    active_until REAL NOT NULL DEFAULT 0,
                    blocked_at REAL,
                    PRIMARY KEY(group_id,account,inviter));
                CREATE TABLE IF NOT EXISTS cashback_risk_incidents(
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, account TEXT NOT NULL,
                    inviter TEXT NOT NULL, detected REAL NOT NULL,
                    window_start REAL NOT NULL, window_end REAL NOT NULL,
                    member_count INTEGER NOT NULL, matching_count INTEGER NOT NULL,
                    name_length INTEGER NOT NULL, status TEXT NOT NULL,
                    notice_status TEXT NOT NULL DEFAULT '',
                    notice_key TEXT NOT NULL UNIQUE,
                    resolved_at REAL, resolved_by TEXT NOT NULL DEFAULT '',
                    resolution TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, entry_id TEXT,
                    actor TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    before_json TEXT NOT NULL, after_json TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(
                    actor TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
                    result TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(actor,request_id));
                CREATE TABLE IF NOT EXISTS reward_rule_history(
                    group_id TEXT NOT NULL, version INTEGER NOT NULL,
                    effective_at REAL NOT NULL, enabled INTEGER NOT NULL,
                    rate INTEGER NOT NULL, bot_id TEXT NOT NULL, account TEXT NOT NULL,
                    PRIMARY KEY(group_id,version));
                CREATE INDEX IF NOT EXISTS reward_rule_time
                    ON reward_rule_history(group_id,effective_at DESC,version DESC);
                CREATE INDEX IF NOT EXISTS entry_view ON entries(group_id,eligible,state,first_seen DESC);
                CREATE INDEX IF NOT EXISTS entry_inviter ON entries(group_id,inviter,state);
                CREATE INDEX IF NOT EXISTS entry_inviter_page
                    ON entries(group_id,eligible,inviter,state,first_seen,id);
                CREATE INDEX IF NOT EXISTS cashback_risk_incidents_recent
                    ON cashback_risk_incidents(group_id,account,detected DESC);
                CREATE INDEX IF NOT EXISTS audit_group ON audit(group_id,id DESC);
                CREATE INDEX IF NOT EXISTS audit_entry ON audit(entry_id,action,id);
                CREATE INDEX IF NOT EXISTS audit_latest_entry_state
                    ON audit(entry_id,action IN ('pay','exception','correct'),id)
                    WHERE action IN ('qualification_restarted','migration_invitation_recovered','pay','exception','correct');
                CREATE TABLE IF NOT EXISTS login_bindings(
                    subject TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                    principal TEXT UNIQUE NOT NULL REFERENCES users(email),
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_requests(
                    ticket TEXT PRIMARY KEY, subject TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS live_revisions(
                    group_id TEXT PRIMARY KEY, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS contacts(
                    uid TEXT PRIMARY KEY, principal TEXT UNIQUE NOT NULL REFERENCES users(email),
                    name TEXT NOT NULL, active INTEGER NOT NULL, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS invitation_source_records(
                    group_id TEXT NOT NULL, bot_id TEXT NOT NULL, account TEXT NOT NULL,
                    started REAL NOT NULL, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, observed REAL NOT NULL,
                    PRIMARY KEY(group_id,bot_id,account,started,fingerprint));
                CREATE TABLE IF NOT EXISTS collector_migrations(
                    group_id TEXT NOT NULL REFERENCES groups(id),
                    source_bot TEXT NOT NULL, source_account TEXT NOT NULL,
                    target_bot TEXT NOT NULL, target_account TEXT NOT NULL,
                    cutoff REAL NOT NULL, actor TEXT NOT NULL,
                    PRIMARY KEY(group_id,source_bot,source_account,target_bot,target_account));
                CREATE TABLE IF NOT EXISTS cashback_membership_events(
                    group_id TEXT NOT NULL, bot_id TEXT NOT NULL, account TEXT NOT NULL,
                    member TEXT NOT NULL, event_id TEXT NOT NULL, event_at REAL NOT NULL,
                    apply_type TEXT NOT NULL, state TEXT NOT NULL, peer TEXT NOT NULL,
                    inviter TEXT NOT NULL, inviter_peer TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, observed REAL NOT NULL,
                    PRIMARY KEY(group_id,bot_id,account,fingerprint));
                CREATE INDEX IF NOT EXISTS cashback_membership_latest
                    ON cashback_membership_events(group_id,account,member,event_at DESC);
                CREATE TABLE IF NOT EXISTS cashback_source_quarantine(
                    group_id TEXT NOT NULL, bot_id TEXT NOT NULL, account TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, member TEXT NOT NULL, event_id TEXT NOT NULL,
                    event_at REAL NOT NULL, reason TEXT NOT NULL, observed REAL NOT NULL,
                    PRIMARY KEY(group_id,bot_id,account,fingerprint));
            """)
            if "uid" not in {r["name"] for r in db.execute("PRAGMA table_info(users)")}:
                db.execute("ALTER TABLE users ADD COLUMN uid TEXT NOT NULL DEFAULT ''")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS cashier_uid ON users(uid) WHERE uid<>''"
            )
            db.execute(
                "INSERT OR IGNORE INTO contacts(uid,principal,name,active,version) "
                "SELECT uid,email,name,active,version FROM users WHERE uid<>''"
            )
            columns = {r["name"] for r in db.execute("PRAGMA table_info(audit)")}
            for field in ("level", "code", "trace", "bot_id"):
                if field not in columns:
                    db.execute(
                        f"ALTER TABLE audit ADD COLUMN {field} TEXT NOT NULL DEFAULT ''"
                    )
            db.execute("CREATE INDEX IF NOT EXISTS audit_trace ON audit(trace,id DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS audit_level ON audit(level,id DESC)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS audit_sampling "
                "ON audit(action,group_id,actor,level,code,created DESC)"
            )
            if "rate_since" not in {
                row["name"] for row in db.execute("PRAGMA table_info(groups)")
            }:
                db.execute(
                    "ALTER TABLE groups ADD COLUMN rate_since REAL NOT NULL DEFAULT 0"
                )
                db.execute("UPDATE groups SET rate_since=started")
            if "contact" not in {
                row["name"] for row in db.execute("PRAGMA table_info(groups)")
            }:
                db.execute(
                    "ALTER TABLE groups ADD COLUMN contact TEXT NOT NULL DEFAULT ''"
                )
                db.execute(
                    "UPDATE groups SET contact=COALESCE("
                    "(SELECT name FROM contacts WHERE principal=groups.cashier),'')"
                )
        with self.connect(True) as db:
            activate = "qualification_seconds" not in {
                r["name"] for r in db.execute("PRAGMA table_info(groups)")
            }
            additions = {
                "groups": {
                    "baseline_started": "REAL NOT NULL DEFAULT 0",
                    "agent_rate": "INTEGER",
                    "agent_only": "INTEGER NOT NULL DEFAULT 0",
                    "qualification_seconds": "INTEGER NOT NULL DEFAULT 0",
                    "require_speech": "INTEGER NOT NULL DEFAULT 0",
                    "speech_cursor": "REAL NOT NULL DEFAULT 0",
                    "source_cursor": "TEXT NOT NULL DEFAULT ''",
                    "source_scanned": "REAL NOT NULL DEFAULT 0",
                    "source_error": "TEXT NOT NULL DEFAULT ''",
                    "source_retry_at": "REAL NOT NULL DEFAULT 0",
                    "source_failures": "INTEGER NOT NULL DEFAULT 0",
                    "risk_started": "REAL NOT NULL DEFAULT 0",
                    "risk_scanned": "REAL NOT NULL DEFAULT 0",
                },
                "reward_rule_history": {
                    "agent_rate": "INTEGER",
                    "agent_only": "INTEGER NOT NULL DEFAULT 0",
                    "qualification_seconds": "INTEGER NOT NULL DEFAULT 0",
                    "require_speech": "INTEGER NOT NULL DEFAULT 0",
                },
                "entries": {
                    "evidence_started": "REAL NOT NULL DEFAULT 0",
                    "evidence_event_id": "TEXT NOT NULL DEFAULT ''",
                    "evidence_event_at": "REAL NOT NULL DEFAULT 0",
                    "integrity_version": "INTEGER NOT NULL DEFAULT 0",
                    "inviter_class": "TEXT NOT NULL DEFAULT 'legacy'",
                    "inviter_name_snapshot": "TEXT NOT NULL DEFAULT ''",
                    "name_digit_count": "INTEGER NOT NULL DEFAULT 0",
                    "pricing_status": "TEXT NOT NULL DEFAULT 'legacy'",
                    "member_class": "TEXT NOT NULL DEFAULT 'legacy'",
                    "member_avatar_ref": "TEXT NOT NULL DEFAULT ''",
                    "member_avatar_status": "TEXT NOT NULL DEFAULT 'unavailable'",
                    "member_avatar_digit_count": "INTEGER NOT NULL DEFAULT 0",
                    "hold_until": "REAL NOT NULL DEFAULT 0",
                    "rule_version": "INTEGER NOT NULL DEFAULT 0",
                    "self_join": "INTEGER NOT NULL DEFAULT 0",
                    "first_spoke_at": "REAL NOT NULL DEFAULT 0",
                },
                "evidence": {
                    "account": "TEXT NOT NULL DEFAULT ''",
                    "member_peer": "TEXT NOT NULL DEFAULT ''",
                    "inviter_peer": "TEXT NOT NULL DEFAULT ''",
                    "event_at": "REAL NOT NULL DEFAULT 0",
                    "member_name_length": "INTEGER NOT NULL DEFAULT 0",
                },
            }
            group_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(groups)")
            }
            initialize_risk_baseline = "risk_started" not in group_columns
            initialize_evidence_cycles = "baseline_started" not in group_columns
            for table, fields in additions.items():
                columns = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
                for field, definition in fields.items():
                    if field not in columns:
                        db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {field} {definition}"
                        )
            if initialize_evidence_cycles:
                db.execute("UPDATE groups SET baseline_started=started")
                db.execute("UPDATE groups SET source_cursor='',source_scanned=0")
                # Only metadata supported by the original, identity-matched proof
                # is backfilled. Historical balances and attribution stay intact.
                db.execute(
                    "UPDATE entries SET evidence_started=COALESCE(("
                    "SELECT MIN(e.event_at) FROM evidence e JOIN groups g "
                    "ON g.id=e.group_id WHERE e.group_id=entries.group_id "
                    "AND e.member=entries.member AND e.inviter=entries.inviter "
                    "AND e.member_peer=entries.peer AND e.inviter_peer=entries.inviter_peer "
                    "AND e.account=g.account AND e.event_at>0 "
                    "AND (entries.paid_at IS NULL OR e.event_at<=entries.paid_at) "
                    "AND e.source IN ('business_apply_log','cashier_confirmation')),0)"
                )
            if initialize_risk_baseline:
                now = time.time()
                db.execute(
                    "UPDATE groups SET risk_started=?,risk_scanned=?",
                    (now, now),
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS evidence_risk_window "
                "ON evidence(group_id,account,inviter,source,event_at)"
            )
            settlement_index_missing = not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='entry_settlement_summary'"
            ).fetchone()
            db.execute(
                "CREATE INDEX IF NOT EXISTS entry_settlement_summary "
                "ON entries(group_id,state,inviter,first_seen,id,amount,verification,pricing_status) "
                "WHERE eligible=1 AND self_join=0"
            )
            no_reward_index_missing = not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='entry_no_reward_summary'"
            ).fetchone()
            db.execute(
                "CREATE INDEX IF NOT EXISTS entry_no_reward_summary "
                "ON entries(group_id,pricing_status,inviter,first_seen,id,"
                "amount,verification,state,member,peer,inviter_peer) "
                "WHERE eligible=1 AND self_join=0 AND pricing_status='no_reward'"
            )
            if (settlement_index_missing or no_reward_index_missing) and db.execute(
                "SELECT 1 FROM entries LIMIT 1"
            ).fetchone():
                # Existing group-first indexes otherwise mask the selective
                # state and pricing indexes until SQLite has actual cardinalities.
                db.execute("ANALYZE entries")
            db.execute(
                "CREATE INDEX IF NOT EXISTS evidence_cashier_confirmation "
                "ON evidence(group_id,member,inviter,member_peer,inviter_peer) "
                "WHERE source='cashier_confirmation'"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS cashback_entry_proof "
                "ON evidence(group_id,member,source,event_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS cashback_quarantine_member "
                "ON cashback_source_quarantine(group_id,bot_id,account,member)"
            )
            # Transferred evidence keeps its original collecting account. Only
            # an explicit maintenance transfer can authorize historical proof.
            db.execute("""
                CREATE VIEW IF NOT EXISTS current_invitation_evidence AS
                SELECT e.*,g.account AS collector_account
                FROM evidence e JOIN groups g ON g.id=e.group_id
                WHERE e.account=g.account OR EXISTS (
                    SELECT 1 FROM collector_migrations m
                    WHERE m.group_id=e.group_id AND m.source_account=e.account
                    AND m.target_bot=g.bot_id AND m.target_account=g.account
                    AND e.event_at>0 AND e.event_at<=m.cutoff
                    AND e.observed<=m.cutoff
                    AND EXISTS (SELECT 1 FROM entries r
                        WHERE r.group_id=e.group_id AND r.member=e.member
                        AND r.first_seen<=m.cutoff))
            """)
            db.execute(
                "CREATE VIEW IF NOT EXISTS cashback_admission_checks AS "
                + ADMISSION_CHECK_SQL
            )
            if activate:
                now = time.time()
                for old in db.execute("SELECT * FROM groups").fetchall():
                    db.execute(
                        "UPDATE groups SET qualification_seconds=10800,"
                        "version=version+1,rate_since=? WHERE id=?",
                        (now, old["id"]),
                    )
                    db.execute(
                        "INSERT INTO reward_rule_history"
                        "(group_id,version,effective_at,enabled,rate,bot_id,account,"
                        "agent_rate,qualification_seconds) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            old["id"],
                            old["version"] + 1,
                            now,
                            old["enabled"],
                            old["rate"],
                            old["bot_id"],
                            old["account"],
                            None,
                            10800,
                        ),
                    )
                    self._audit(
                        db,
                        old["id"],
                        None,
                        "system",
                        "qualification_rules_activated",
                        {"version": old["version"]},
                        {
                            "version": old["version"] + 1,
                            "qualification_seconds": 10800,
                            "agent_rate": None,
                            "existing_entries_unchanged": True,
                        },
                    )
        self.path.chmod(0o600)
        self._reuse_reads = reuse_reads

    @contextmanager
    def connect(self, write=False):
        if write and self._read_only:
            raise CashbackError("read_only", "账本当前只允许读取。", 503)
        transaction = FINANCE_TRANSACTION.get()
        if transaction is not None and transaction[0] is self:
            if write and (guard := WRITE_GUARD.get()) is not None:
                guard()
            yield transaction[1]
            return
        reuse = not write and self._reuse_reads
        db = None
        if reuse:
            with self._reader_lock:
                if self._readers:
                    db = self._readers.pop()
        if db is None:
            if reuse or self._read_only:
                db = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro",
                    uri=True,
                    timeout=10,
                    check_same_thread=False,
                )
                db.execute("PRAGMA query_only=ON")
            else:
                db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
                guard = WRITE_GUARD.get()
                if guard is not None:
                    guard()
            yield db
            if write and (guard := WRITE_GUARD.get()) is not None:
                guard()
            db.commit()
        except BaseException as exc:
            if isinstance(exc, sqlite3.Error):
                reuse = False
            db.rollback()
            raise
        finally:
            # No read transaction or cursor may outlive its exclusive lease.
            try:
                db.rollback()
            except sqlite3.Error:
                self._paid_pages.pop(db, None)
                db.close()
                raise
            if reuse:
                with self._reader_lock:
                    if self._reuse_reads and len(self._readers) < 4:
                        self._readers.append(db)
                        db = None
            if db is not None:
                self._paid_pages.pop(db, None)
                db.close()

    def close(self):
        """Release idle readers; active leases close when they finish."""
        with self._reader_lock:
            self._reuse_reads = False
            for db in self._readers:
                db.close()
            self._readers.clear()
            self._paid_pages.clear()

    @staticmethod
    def _audit(db, group, entry, actor, action, before, after, reason=""):
        def safe(value):
            if isinstance(value, dict):
                return {
                    key: safe(item)
                    for key, item in value.items()
                    if key not in {"subject", "token", "password", "email"}
                }
            if isinstance(value, list):
                return [safe(item) for item in value]
            return redact_reply(value) if isinstance(value, str) else value

        rule = db.execute("SELECT bot_id FROM groups WHERE id=?", (group,)).fetchone()
        db.execute(
            "INSERT INTO audit(group_id,entry_id,actor,action,reason,before_json,after_json,created,level,trace,bot_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                group,
                entry,
                actor,
                action,
                redact_reply(reason),
                json.dumps(safe(before), ensure_ascii=False),
                json.dumps(safe(after), ensure_ascii=False),
                time.time(),
                "warning"
                if action
                in {
                    "paid_attribution_review_required",
                    "attribution_conflict",
                    "identity_conflict",
                    "inviter_identity_conflict",
                    "invitation_evidence_conflict",
                    "self_join_attribution_conflict",
                    "cashback_risk_warning_detected",
                    "cashback_risk_blocked",
                }
                else "info",
                REQUEST_TRACE.get(),
                rule["bot_id"] if rule else "",
            ),
        )
        db.execute(
            "INSERT INTO live_revisions(group_id,version) VALUES(?,1) "
            "ON CONFLICT(group_id) DO UPDATE SET version=version+1",
            (group,),
        )

    def _evaluate_cashback_risk(self, db, group, account, started, cursor, now):
        candidates = db.execute(
            "SELECT DISTINCT inviter,event_at FROM evidence "
            "WHERE group_id=? AND account=? AND source='business_apply_log' "
            "AND event_at>=? AND event_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM entries r WHERE r.group_id=evidence.group_id "
            "AND r.member=evidence.member AND r.integrity_version=0) "
            "AND observed>? ORDER BY inviter,event_at",
            (group, account, started, now + 30, cursor),
        ).fetchall()
        for inviter in sorted({row["inviter"] for row in candidates}):
            times = sorted(
                {row["event_at"] for row in candidates if row["inviter"] == inviter}
            )
            risk = db.execute(
                "SELECT * FROM cashback_inviter_risk "
                "WHERE group_id=? AND account=? AND inviter=?",
                (group, account, inviter),
            ).fetchone()
            processed = risk["processed_event_at"] if risk else 0
            active_until = risk["active_until"] if risk else 0
            warning_count = risk["warning_count"] if risk else 0
            blocked_at = risk["blocked_at"] if risk else None
            for event_at in times:
                if event_at <= processed or blocked_at:
                    continue
                cohort = db.execute(
                    "SELECT member,event_at,member_name_length FROM evidence "
                    "WHERE group_id=? AND account=? AND inviter=? "
                    "AND source='business_apply_log' "
                    "AND NOT EXISTS (SELECT 1 FROM entries r WHERE r.group_id=evidence.group_id "
                    "AND r.member=evidence.member AND r.integrity_version=0) "
                    "AND event_at>=? AND event_at<=? ORDER BY event_at,event_id",
                    (group, account, inviter, event_at - 300, event_at),
                ).fetchall()
                distinct = {}
                for proof in cohort:
                    distinct.setdefault(proof["member"], proof["member_name_length"])
                lengths = {}
                for length in distinct.values():
                    if length > 0:
                        lengths[length] = lengths.get(length, 0) + 1
                common_length, matching = (
                    max(lengths.items(), key=lambda pair: (pair[1], -pair[0]))
                    if lengths
                    else (0, 0)
                )
                processed = event_at
                if len(distinct) < 10 or matching * 10 < len(distinct) * 9:
                    continue
                if event_at <= active_until:
                    active_until = max(active_until, event_at + 300)
                    continue
                start_at = min(proof["event_at"] for proof in cohort)
                incident_id = uuid.uuid4().hex
                status = "blocked" if warning_count else "warning_pending"
                notice_status = "not_required" if warning_count else "pending"
                db.execute(
                    "INSERT INTO cashback_risk_incidents"
                    "(id,group_id,account,inviter,detected,window_start,window_end,"
                    "member_count,matching_count,name_length,status,notice_status,notice_key) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        incident_id,
                        group,
                        account,
                        inviter,
                        now,
                        start_at,
                        event_at,
                        len(distinct),
                        matching,
                        common_length,
                        status,
                        notice_status,
                        f"cashback-risk/{incident_id}",
                    ),
                )
                if warning_count:
                    blocked_at = now
                    self._audit(
                        db,
                        group,
                        None,
                        "system",
                        "cashback_risk_blocked",
                        {"warning_count": warning_count},
                        {
                            "inviter": inviter,
                            "member_count": len(distinct),
                            "matching_count": matching,
                            "name_length": common_length,
                            "blocked": True,
                        },
                        "A second distinct five-minute name-length anomaly was detected; unpaid cashback is frozen.",
                    )
                else:
                    self._audit(
                        db,
                        group,
                        None,
                        "system",
                        "cashback_risk_warning_detected",
                        {},
                        {
                            "inviter": inviter,
                            "member_count": len(distinct),
                            "matching_count": matching,
                            "name_length": common_length,
                            "notice": "pending",
                        },
                        "At least ten distinct invitees entered within five minutes and at least 90% shared one trimmed display-name length.",
                    )
                active_until = event_at + 300
            db.execute(
                "INSERT INTO cashback_inviter_risk"
                "(group_id,account,inviter,warning_count,processed_event_at,"
                "active_until,blocked_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(group_id,account,inviter) DO UPDATE SET "
                "processed_event_at=excluded.processed_event_at,"
                "active_until=excluded.active_until,blocked_at=excluded.blocked_at",
                (
                    group,
                    account,
                    inviter,
                    warning_count,
                    processed,
                    active_until,
                    blocked_at,
                ),
            )
        db.execute("UPDATE groups SET risk_scanned=? WHERE id=?", (now, group))

    @staticmethod
    def _valid_price_policy(db, policy, rule, entry_at):
        if not policy or not policy["enabled"] or policy["rate"] is None:
            return False
        if policy["bot_id"] == rule["bot_id"] and policy["account"] == rule["account"]:
            return True
        return (
            db.execute(
                "SELECT 1 FROM collector_migrations WHERE group_id=? "
                "AND source_bot=? AND source_account=? AND target_bot=? "
                "AND target_account=? AND cutoff>=? AND cutoff>=? LIMIT 1",
                (
                    policy["group_id"],
                    policy["bot_id"],
                    policy["account"],
                    rule["bot_id"],
                    rule["account"],
                    entry_at,
                    policy["effective_at"],
                ),
            ).fetchone()
            is not None
        )

    @classmethod
    def _entry_price_policy(cls, db, rule, row, event_at=0):
        """Resolve the ordinary/agent prices frozen for one ledger entry."""
        if event_at and event_at < rule["rate_since"]:
            historical = db.execute(
                "SELECT * FROM reward_rule_history "
                "WHERE group_id=? AND effective_at<=? "
                "ORDER BY effective_at DESC,version DESC LIMIT 1",
                (row["group_id"], event_at),
            ).fetchone()
            return (
                historical
                if cls._valid_price_policy(db, historical, rule, row["first_seen"])
                else None
            )
        if row["rule_version"]:
            frozen = db.execute(
                "SELECT * FROM reward_rule_history WHERE group_id=? AND version=?",
                (row["group_id"], row["rule_version"]),
            ).fetchone()
            if cls._valid_price_policy(db, frozen, rule, row["first_seen"]):
                return frozen
        historical = db.execute(
            "SELECT * FROM reward_rule_history "
            "WHERE group_id=? AND effective_at<=? "
            "ORDER BY effective_at DESC,version DESC LIMIT 1",
            (row["group_id"], row["first_seen"]),
        ).fetchone()
        return (
            historical
            if cls._valid_price_policy(db, historical, rule, row["first_seen"])
            else None
        )

    def live_revision(self, actor, groups):
        """Read business revisions without including request-log writes.

        Args:
            actor: Authenticated local principal.
            groups: Currently selected groups visible to this principal.

        Returns:
            Scoped revision values; no invitation or payment details.
        """
        with self.connect() as db:
            person = self._actor(db, actor)
            allowed = set(groups)
            if isinstance(actor, FinanceActor):
                for group in allowed:
                    self._scope(db, actor, group)
            elif person["role"] == "admin":
                allowed.add("")
            else:
                for group in allowed:
                    self._scope(db, actor, group)
            return [
                (row["group_id"], row["version"])
                for row in db.execute("SELECT * FROM live_revisions ORDER BY group_id")
                if row["group_id"] in allowed
            ]

    @staticmethod
    def _actor(db, actor, admin=False):
        if isinstance(actor, (FinanceActor, DashboardFinanceActor)):
            return actor.authorize(db, admin=admin)
        row = db.execute(
            "SELECT * FROM users WHERE email=? AND active=1", (actor,)
        ).fetchone()
        if (
            not row
            or row["role"] not in {"admin", "cashier"}
            or (admin and row["role"] != "admin")
        ):
            raise CashbackError("forbidden", "当前登录身份没有此操作权限。", 403)
        return dict(row)

    @classmethod
    def _scope(cls, db, actor, group, admin=False):
        person = cls._actor(db, actor, admin)
        if isinstance(actor, (FinanceActor, DashboardFinanceActor)):
            actor.authorize(db, admin=admin, group=group)
        row = db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
        if (
            not row
            or isinstance(actor, FinanceActor)
            and row["account"] != actor.account
        ):
            raise CashbackError("group_forbidden", "无权访问此群的结账记录。", 403)
        return dict(row), person

    def bootstrap(self, administrators):
        with self.connect(True) as db:
            addresses = {email(user["email"]) for user in administrators}
            for row in db.execute(
                "SELECT email FROM users WHERE role='admin'"
            ).fetchall():
                if row["email"] not in addresses:
                    db.execute(
                        "UPDATE users SET active=0 WHERE email=?", (row["email"],)
                    )
            for user in administrators:
                address = email(user["email"])
                db.execute(
                    "INSERT INTO users(email,name,role) VALUES(?,?,'admin') "
                    "ON CONFLICT(email) DO UPDATE SET role='admin',active=1,name=excluded.name",
                    (address, label(user.get("name", "管理员"))),
                )

    def identify(self, address, subject):
        """Resolve a verified Access identity, automatically admitting cashiers.

        Args:
            address: Email from the application-specific verified Access JWT.
            subject: Subject from the same verified JWT, never browser input.

        Returns:
            Existing administrator or all-group cashier identity. Contact UIDs
            are not login credentials; legacy bindings preserve payment ownership.
        """
        address = email(address)
        if not isinstance(subject, str) or not subject or len(subject) > 256:
            raise CashbackError("invalid_identity", "登录身份无效，请重新登录。", 401)
        # Established sessions need fresh authorization, not a write lock per tick.
        with self.connect() as db:
            binding = db.execute(
                "SELECT * FROM login_bindings WHERE subject=?", (subject,)
            ).fetchone()
            if binding and binding["email"] != address:
                raise CashbackError(
                    "identity_changed", "登录身份变化，请联系管理员核对。", 403
                )
            principal = binding["principal"] if binding else address
            existing = db.execute(
                "SELECT * FROM users WHERE email=?", (principal,)
            ).fetchone()
            if existing and existing["subject"] == subject:
                subject_owner = db.execute(
                    "SELECT email FROM users WHERE subject=? AND email<>? LIMIT 1",
                    (subject, principal),
                ).fetchone()
                if subject_owner:
                    raise CashbackError(
                        "identity_changed", "登录身份变化，请联系管理员核对。", 403
                    )
                person = self._actor(db, principal)
                return {
                    **{key: person[key] for key in ("email", "name", "role")},
                    "uid": "",
                }
        with self.connect(True) as db:
            binding = db.execute(
                "SELECT * FROM login_bindings WHERE subject=?", (subject,)
            ).fetchone()
            if binding and binding["email"] != address:
                raise CashbackError(
                    "identity_changed", "登录身份变化，请联系管理员核对。", 403
                )
            principal = binding["principal"] if binding else address
            existing = db.execute(
                "SELECT * FROM users WHERE email=?", (principal,)
            ).fetchone()
            subject_owner = db.execute(
                "SELECT email FROM users WHERE subject=? LIMIT 1", (subject,)
            ).fetchone()
            if subject_owner and subject_owner["email"] != principal:
                raise CashbackError(
                    "identity_changed", "登录身份变化，请联系管理员核对。", 403
                )
            if not existing:
                name = "结账员 " + hashlib.sha256(subject.encode()).hexdigest()[:8]
                db.execute(
                    "INSERT INTO users(email,name,role,subject) VALUES(?,?,'cashier',?)",
                    (principal, name, subject),
                )
                self._audit(
                    db,
                    "",
                    None,
                    principal,
                    "access_cashier_admitted",
                    {},
                    {"name": name, "role": "cashier", "scope": "all_configured_groups"},
                )
            person = self._actor(db, principal)
            if person["subject"] and person["subject"] != subject:
                raise CashbackError(
                    "identity_changed", "登录身份发生变化，请联系管理员核对。", 403
                )
            if not person["subject"]:
                db.execute(
                    "UPDATE users SET subject=? WHERE email=?",
                    (subject, person["email"]),
                )
            # Login principals are independent of the displayed group contact.
            return {
                **{key: person[key] for key in ("email", "name", "role")},
                "uid": "",
            }

    def cashier_profiles(self, actor):
        with self.connect() as db:
            if isinstance(actor, FinanceActor):
                raise CashbackError("forbidden", "聊天中不能管理财务授权。", 403)
            self._actor(db, actor, True)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT uid,name,active,version FROM contacts ORDER BY name,uid"
                )
            ]

    def save_cashier(self, actor, uid, name, active, version):
        """Save only a directory-verified business identity.

        Args:
            actor: Authenticated administrator principal.
            uid: Real Wangshangliao UID resolved by the service.
            name: Current platform nickname, not client-provided display text.
            active: Whether the business contact is available for selection.
            version: Expected profile version, or zero for a new profile.
        """
        if isinstance(actor, FinanceActor):
            raise CashbackError("forbidden", "聊天中不能修改全站联系人。", 403)
        if not re.fullmatch(r"[1-9][0-9]{0,19}", uid) or type(active) is not bool:
            raise CashbackError("invalid_cashier", "结账员身份无效。")
        with self.connect(True) as db:
            self._actor(db, actor, True)
            old = db.execute("SELECT * FROM contacts WHERE uid=?", (uid,)).fetchone()
            if version != (old["version"] if old else 0):
                raise CashbackError("conflict", "群内联系人已变化，请刷新后重试。", 409)
            principal = old["principal"] if old else "wsl:" + uid
            db.execute(
                "INSERT OR IGNORE INTO users(email,uid,name,role) VALUES(?,?,?,'contact')",
                (principal, uid, label(name)),
            )
            db.execute(
                "INSERT INTO contacts(uid,principal,name,active,version) VALUES(?,?,?,?,1) "
                "ON CONFLICT(uid) DO UPDATE SET name=excluded.name,active=excluded.active,"
                "version=contacts.version+1",
                (uid, principal, label(name), int(active)),
            )
            self._audit(
                db,
                "",
                None,
                actor,
                "contact_changed",
                {k: old[k] for k in ("uid", "name", "active")} if old else {},
                {"uid": uid, "name": label(name), "active": active},
            )
        return {"status": "saved"}

    def users(self, actor):
        if isinstance(actor, FinanceActor):
            raise CashbackError("forbidden", "聊天中不能查看全站财务身份。", 403)
        with self.connect() as db:
            self._actor(db, actor, True)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT email,name,role,active,version FROM users ORDER BY name,email"
                )
            ]

    def save_user(self, actor, address, name, active, version):
        if isinstance(actor, FinanceActor):
            raise CashbackError("forbidden", "聊天中不能授予网页财务权限。", 403)
        address, name = email(address), label(name)
        if not name or type(active) is not bool:
            raise CashbackError("invalid_user", "请填写结账员名称和启用状态。")
        with self.connect(True) as db:
            self._actor(db, actor, True)
            old = db.execute("SELECT * FROM users WHERE email=?", (address,)).fetchone()
            if old and old["role"] == "admin":
                raise CashbackError(
                    "admin_static", "管理员身份由服务器配置管理，不能在此修改。", 403
                )
            if version != (old["version"] if old else 0):
                raise CashbackError("conflict", "结账员信息已变化，请刷新。", 409)
            db.execute(
                "INSERT INTO users(email,name,role,active) VALUES(?,?,'cashier',?) "
                "ON CONFLICT(email) DO UPDATE SET name=excluded.name,active=excluded.active,version=users.version+1",
                (address, name, int(active)),
            )
            self._audit(
                db,
                "",
                None,
                actor,
                "cashier_changed",
                {k: old[k] for k in ("email", "name", "active")} if old else {},
                {"email": address, "name": name, "active": active},
            )
        return {"status": "saved"}

    def groups(self, actor=None):
        with self.connect() as db:
            if actor is not None:
                self._actor(db, actor)
            rows = db.execute(
                "SELECT g.*,COALESCE(NULLIF(g.contact,''),c.name,u.name) AS cashier_name FROM groups g "
                "JOIN users u ON u.email=g.cashier LEFT JOIN contacts c ON c.principal=g.cashier "
                "ORDER BY g.name,g.id"
            ).fetchall()
            return [
                dict(r)
                for r in rows
                if not isinstance(actor, FinanceActor)
                or r["id"] == actor.group
                and r["account"] == actor.account
            ]

    def get_group(self, group, actor=None):
        with self.connect() as db:
            if actor is not None:
                return self._scope(db, actor, group)[0]
            row = db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
            return dict(row) if row else None

    def invitation_summary(self, group, inviter):
        """Read only one authenticated sender's cash records for a scoped group.

        The chat service validates the platform event and group before calling.
        Payment identities, audit history and other inviters are never returned.
        """
        with self.connect() as db:
            db.execute("BEGIN")
            rule = db.execute(
                "SELECT g.enabled,g.rate,g.agent_rate,g.agent_only,g.scanned,g.error,g.require_speech,"
                "COALESCE(NULLIF(g.contact,''),c.name,'') AS contact_name "
                "FROM groups g LEFT JOIN contacts c ON c.principal=g.cashier "
                "WHERE g.id=?",
                (group,),
            ).fetchone()
            if not rule:
                return None
            risk = db.execute(
                "SELECT warning_count,blocked_at FROM cashback_inviter_risk "
                "WHERE group_id=? AND account=(SELECT account FROM groups WHERE id=?) "
                "AND inviter=?",
                (group, group, inviter),
            ).fetchone()
            risk_status = (
                "blocked"
                if risk and risk["blocked_at"] is not None
                else "warned"
                if risk and risk["warning_count"]
                else ""
            )
            now = time.time()
            where = (
                "group_id=? AND inviter=? AND eligible=1 AND self_join=0 "
                "AND (state!='paid' OR verification='verified') "
                "AND EXISTS (SELECT 1 FROM current_invitation_evidence p "
                "WHERE p.group_id=entries.group_id AND p.member=entries.member "
                "AND p.inviter=entries.inviter "
                "AND p.source IN ('business_apply_log','cashier_confirmation') "
                "AND p.collector_account=(SELECT account FROM groups WHERE id=entries.group_id) "
                "AND p.member_peer=entries.peer AND p.inviter_peer=entries.inviter_peer "
                "AND p.event_at>0 AND (state!='paid' OR p.event_at<=entries.paid_at))"
            )
            totals = db.execute(
                "SELECT COUNT(*) AS total,COALESCE(SUM(present),0) AS present,"
                "COALESCE(SUM(CASE WHEN state='paid' THEN amount ELSE 0 END),0) AS paid,"
                "COALESCE(SUM(CASE WHEN state='pending' AND verification='verified' "
                "AND present=1 AND ever_left=0 AND pricing_status='priced' "
                "AND member_class IN ('ordinary','agent') AND hold_until<=? "
                "AND (?=0 OR first_spoke_at>0) "
                f"AND EXISTS (SELECT 1 FROM ({ADMISSION_CHECK_SQL}) ac "
                "WHERE ac.id=entries.id AND ac.payable_evidence=1) "
                "THEN amount ELSE 0 END),0) AS pending,"
                "COALESCE(SUM(CASE WHEN state='pending' AND "
                "pricing_status!='no_reward' AND (verification!='verified' OR present=0 OR ever_left=1 "
                "OR pricing_status!='priced' OR member_class NOT IN ('ordinary','agent') "
                "OR hold_until>? OR (?=1 AND first_spoke_at=0) "
                f"OR NOT EXISTS (SELECT 1 FROM ({ADMISSION_CHECK_SQL}) ac "
                "WHERE ac.id=entries.id AND ac.payable_evidence=1)) "
                "THEN 1 ELSE 0 END),0) AS review,"
                "COALESCE(SUM(state IN ('pending','verifying') AND pricing_status='no_reward'),0) AS no_reward,"
                "COALESCE(SUM(state='verifying' AND pricing_status!='no_reward'),0) AS verifying,"
                "COALESCE(SUM(state='verifying' AND pricing_status!='no_reward' AND hold_until>?),0) AS hold_pending,"
                "COALESCE(SUM(state IN ('pending','verifying') AND present=1 AND ever_left=0 "
                "AND pricing_status!='no_reward' AND first_spoke_at>0),0) AS spoken,"
                "COALESCE(SUM(state IN ('pending','verifying') AND present=1 AND ever_left=0 "
                "AND pricing_status!='no_reward' AND first_spoke_at=0),0) AS unspoken,"
                "COALESCE(SUM(CASE WHEN state='void' THEN 1 ELSE 0 END),0) AS void "
                f"FROM entries WHERE {where}",
                (
                    now,
                    rule["require_speech"],
                    now,
                    rule["require_speech"],
                    now,
                    group,
                    inviter,
                ),
            ).fetchone()
            blocked_amount = totals["pending"] if risk_status == "blocked" else 0
            if risk_status == "blocked":
                totals = {**dict(totals), "pending": 0}
            items = db.execute(
                "SELECT member_name,present,state,verification,ever_left,inviter_class,"
                "member_class,pricing_status,hold_until,first_spoke_at "
                f"FROM entries WHERE {where} "
                "ORDER BY first_seen DESC,id LIMIT 20",
                (group, inviter),
            ).fetchall()
            return {
                **dict(rule),
                **dict(totals),
                "risk_status": risk_status,
                "risk_blocked_amount": blocked_amount,
                "items": [dict(r) for r in items],
            }

    def private_invitation_summaries(
        self, inviter, bot, account, groups, *, period="all", detail_group=None
    ):
        """Read sender-only aggregates through a physically read-only connection."""
        if not re.fullmatch(r"[1-9][0-9]{0,19}", inviter):
            raise CashbackError("query_scope", "邀请查询身份无效。", 403)
        if not groups:
            return []
        if len(groups) > 100 or any(
            not re.fullmatch(r"[1-9][0-9]{0,19}", group) for group in groups
        ):
            raise CashbackError("query_scope", "邀请查询范围无效。", 403)
        if period not in {"all", "day", "yesterday"} or (
            detail_group is not None and detail_group not in groups
        ):
            raise CashbackError("query_scope", "邀请查询范围无效。", 403)
        now = time.time()
        window = (
            entry_time_window("day", now=now - 86400 if period == "yesterday" else now)
            if period != "all"
            else None
        )
        date_filter = " AND e.first_seen>=? AND e.first_seen<? " if window else ""
        db = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=1
        )
        db.row_factory = sqlite3.Row
        deadline = time.monotonic() + 1
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            placeholders = ",".join("?" for _ in groups)
            rows = db.execute(
                "SELECT g.id,g.version,g.name,g.enabled,g.agent_only,g.scanned,g.error,g.require_speech,"
                "COALESCE(NULLIF(g.contact,''),c.name,'') AS contact_name,"
                "COALESCE(r.warning_count,0) AS risk_warning_count,"
                "CASE WHEN r.blocked_at IS NOT NULL THEN 'blocked' "
                "WHEN COALESCE(r.warning_count,0)>0 THEN 'warned' ELSE '' END AS risk_status,"
                "COUNT(e.id) AS total,COALESCE(SUM(e.present),0) AS present,"
                "COALESCE(SUM(CASE WHEN e.state='paid' THEN e.amount ELSE 0 END),0) AS paid,"
                "COALESCE(SUM(CASE WHEN e.state='pending' AND e.verification='verified' "
                "AND e.present=1 AND e.ever_left=0 AND e.pricing_status='priced' "
                "AND e.member_class IN ('ordinary','agent') AND e.hold_until<=? "
                "AND (g.require_speech=0 OR e.first_spoke_at>0) "
                f"AND EXISTS (SELECT 1 FROM ({ADMISSION_CHECK_SQL}) ac "
                "WHERE ac.id=e.id AND ac.payable_evidence=1) "
                "AND r.blocked_at IS NULL "
                "THEN e.amount ELSE 0 END),0) AS pending,"
                "COALESCE(SUM(CASE WHEN r.blocked_at IS NOT NULL "
                "AND e.state IN ('pending','verifying') THEN e.amount ELSE 0 END),0) "
                "AS risk_blocked_amount,"
                "COALESCE(SUM(CASE WHEN r.blocked_at IS NOT NULL "
                "AND e.state IN ('pending','verifying') THEN 1 ELSE 0 END),0) "
                "AS risk_blocked_count,"
                "COALESCE(SUM(e.state IN ('pending','verifying') AND e.pricing_status='no_reward'),0) AS no_reward,"
                "COALESCE(SUM(e.state='verifying' AND e.pricing_status!='no_reward'),0) AS verifying,"
                "COALESCE(SUM(e.state='verifying' AND e.pricing_status!='no_reward' AND e.hold_until>?),0) AS hold_pending,"
                "COALESCE(SUM(e.state IN ('pending','verifying') AND e.present=1 "
                "AND e.ever_left=0 AND e.pricing_status!='no_reward' AND e.first_spoke_at>0),0) AS spoken,"
                "COALESCE(SUM(e.state IN ('pending','verifying') AND e.present=1 "
                "AND e.ever_left=0 AND e.pricing_status!='no_reward' AND e.first_spoke_at=0),0) AS unspoken,"
                "COALESCE(SUM(e.state='void'),0) AS void "
                "FROM groups g LEFT JOIN contacts c ON c.principal=g.cashier "
                "LEFT JOIN cashback_inviter_risk r ON r.group_id=g.id "
                "AND r.account=g.account AND r.inviter=? "
                "LEFT JOIN entries e ON e.group_id=g.id "
                "AND e.inviter=? AND e.eligible=1 AND e.self_join=0 "
                "AND (e.state!='paid' OR e.verification='verified') "
                "AND EXISTS (SELECT 1 FROM current_invitation_evidence p "
                "WHERE p.group_id=e.group_id AND p.member=e.member "
                "AND p.inviter=e.inviter AND p.source IN ('business_apply_log','cashier_confirmation') "
                "AND p.collector_account=g.account AND p.member_peer=e.peer "
                "AND p.inviter_peer=e.inviter_peer AND p.event_at>0 "
                "AND (e.state!='paid' OR p.event_at<=e.paid_at)) "
                + date_filter
                + "WHERE g.enabled=1 AND g.bot_id=? AND g.account=? "
                f"AND g.id IN ({placeholders}) "
                "GROUP BY g.id ORDER BY g.name,g.id LIMIT 100",
                (now, now, inviter, inviter, *(window or ()), bot, account, *groups),
            ).fetchall()
            result = [dict(row) for row in rows]
            for group in result:
                if window:
                    group["period_date"] = datetime.fromtimestamp(
                        window[0], BEIJING
                    ).strftime("%Y-%m-%d")
                if group["id"] != detail_group:
                    continue
                items = db.execute(
                    "SELECT e.member_name,e.first_seen,e.state,e.present,e.pricing_status,e.hold_until,e.first_spoke_at,"
                    "CASE WHEN e.state='pending' AND e.verification='verified' "
                    "AND e.present=1 AND e.ever_left=0 AND e.pricing_status='priced' "
                    "AND e.member_class IN ('ordinary','agent') AND e.hold_until<=? "
                    "AND (g.require_speech=0 OR e.first_spoke_at>0) "
                    f"AND EXISTS (SELECT 1 FROM ({ADMISSION_CHECK_SQL}) ac "
                    "WHERE ac.id=e.id AND ac.payable_evidence=1) "
                    "THEN 1 ELSE 0 END AS payable "
                    "FROM entries e JOIN groups g ON g.id=e.group_id "
                    "WHERE e.group_id=? AND g.enabled=1 AND g.bot_id=? AND g.account=? "
                    "AND e.inviter=? AND e.eligible=1 AND e.self_join=0 "
                    "AND (e.state!='paid' OR e.verification='verified') "
                    "AND EXISTS (SELECT 1 FROM current_invitation_evidence p "
                    "WHERE p.group_id=e.group_id AND p.member=e.member "
                    "AND p.inviter=e.inviter AND p.source IN "
                    "('business_apply_log','cashier_confirmation') "
                    "AND p.collector_account=g.account AND p.member_peer=e.peer "
                    "AND p.inviter_peer=e.inviter_peer AND p.event_at>0 "
                    "AND (e.state!='paid' OR p.event_at<=e.paid_at)) "
                    + date_filter
                    + "ORDER BY e.first_seen DESC,e.id LIMIT 20",
                    (now, detail_group, bot, account, inviter, *(window or ())),
                ).fetchall()
                group["items"] = [dict(item) for item in items]
                if not group["require_speech"]:
                    for item in group["items"]:
                        item.pop("hold_until")
                        item.pop("first_spoke_at")
            return result
        finally:
            db.close()

    def pending_risk_notices(self, group, account, limit=10):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,inviter,notice_key FROM cashback_risk_incidents "
                    "WHERE group_id=? AND account=? AND status='warning_pending' "
                    "AND notice_status='pending' ORDER BY detected,id LIMIT ?",
                    (group, account, max(1, min(int(limit), 20))),
                )
            ]

    def finish_risk_notice(self, incident_id, status):
        if status not in {"accepted", "verified", "rejected", "unknown"}:
            raise CashbackError("risk_notice_status", "风控通知状态无效。", 400)
        with self.connect(True) as db:
            incident = db.execute(
                "SELECT * FROM cashback_risk_incidents WHERE id=?",
                (incident_id,),
            ).fetchone()
            if not incident or incident["status"] != "warning_pending":
                return False
            delivered = status in {"accepted", "verified"}
            db.execute(
                "UPDATE cashback_risk_incidents SET notice_status=?,status=? "
                "WHERE id=? AND status='warning_pending'",
                (
                    status,
                    "warned" if delivered else "warning_failed",
                    incident_id,
                ),
            )
            if delivered:
                db.execute(
                    "UPDATE cashback_inviter_risk SET warning_count=MAX(warning_count,1) "
                    "WHERE group_id=? AND account=? AND inviter=?",
                    (
                        incident["group_id"],
                        incident["account"],
                        incident["inviter"],
                    ),
                )
            self._audit(
                db,
                incident["group_id"],
                None,
                "system",
                "cashback_risk_notice",
                {"notice_status": "pending"},
                {
                    "inviter": incident["inviter"],
                    "notice_status": status,
                    "delivered": delivered,
                },
            )
            return delivered

    def risk_cases(self, actor, group):
        with self.connect() as db:
            self._actor(db, actor, True)
            if not db.execute("SELECT 1 FROM groups WHERE id=?", (group,)).fetchone():
                return []
            self._scope(db, actor, group, True)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT i.*,r.warning_count,r.blocked_at,"
                    "COALESCE(NULLIF(e.inviter_name,''),i.inviter) AS inviter_name "
                    "FROM cashback_risk_incidents i "
                    "LEFT JOIN cashback_inviter_risk r ON r.group_id=i.group_id "
                    "AND r.account=i.account AND r.inviter=i.inviter "
                    "LEFT JOIN entries e ON e.group_id=i.group_id AND e.member=i.inviter "
                    "WHERE i.group_id=? ORDER BY i.detected DESC,i.id DESC LIMIT 100",
                    (group,),
                )
            ]

    def release_risk(self, actor, group, inviter, reason):
        if not re.fullmatch(r"[1-9][0-9]{0,19}", str(inviter)):
            raise CashbackError("risk_identity", "风控邀请人身份无效。", 400)
        reason = label(reason, 300)
        if len(reason) < 2:
            raise CashbackError("reason_required", "请填写复核依据。")
        with self.connect(True) as db:
            rule, _ = self._scope(db, actor, group, True)
            risk = db.execute(
                "SELECT * FROM cashback_inviter_risk "
                "WHERE group_id=? AND account=? AND inviter=?",
                (group, rule["account"], inviter),
            ).fetchone()
            if not risk or risk["blocked_at"] is None:
                raise CashbackError("risk_not_blocked", "该邀请人当前未被冻结。", 409)
            db.execute(
                "UPDATE cashback_inviter_risk SET warning_count=0,active_until=0,"
                "blocked_at=NULL,processed_event_at=MAX(processed_event_at,?) "
                "WHERE group_id=? AND account=? AND inviter=?",
                (
                    db.execute(
                        "SELECT COALESCE(MAX(event_at),0) FROM evidence WHERE group_id=? "
                        "AND account=? AND inviter=? AND source='business_apply_log'",
                        (group, rule["account"], inviter),
                    ).fetchone()[0],
                    group,
                    rule["account"],
                    inviter,
                ),
            )
            db.execute(
                "UPDATE cashback_risk_incidents SET status='released',resolved_at=?,"
                "resolved_by=?,resolution=? WHERE group_id=? AND account=? "
                "AND inviter=? AND status='blocked'",
                (time.time(), actor, reason, group, rule["account"], inviter),
            )
            self._audit(
                db,
                group,
                None,
                actor,
                "cashback_risk_released",
                {"inviter": inviter, "blocked_at": risk["blocked_at"]},
                {"inviter": inviter, "blocked": False},
                reason,
            )
        return {"status": "released"}

    def private_invitation_scope(self, bot, account, groups):
        """Read active group versions for the final private-delivery guard.

        Args:
            bot: Bound collector instance.
            account: Bound authenticated platform account.
            groups: Previously validated selected group IDs.

        Returns:
            Active group identities and versions in menu order.
        """
        if not groups:
            return []
        if len(groups) > 100:
            raise CashbackError("query_scope", "邀请查询范围无效。", 403)
        db = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=1
        )
        try:
            db.execute("PRAGMA query_only=ON")
            placeholders = ",".join("?" for _ in groups)
            return db.execute(
                "SELECT id,version FROM groups WHERE enabled=1 AND bot_id=? "
                f"AND account=? AND id IN ({placeholders}) ORDER BY name,id LIMIT 100",
                (bot, account, *groups),
            ).fetchall()
        finally:
            db.close()

    def configure(self, actor, config, members, history, now=None):
        now = now or time.time()
        group = config["id"]
        if (
            type(config["enabled"]) is not bool
            or type(config["rate"]) is not int
            or not 0 < config["rate"] <= 99999999
        ):
            raise CashbackError("invalid_rate", "每人金额须为0.01至999999.99元。")
        with self.connect(True) as db:
            self._actor(db, actor, True)
            if isinstance(actor, FinanceActor) and (
                group != actor.group or config["account"] != actor.account
            ):
                raise CashbackError(
                    "finance_forbidden", "不能修改其他群的返现设置。", 403
                )
            old = db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
            if config["version"] != (old["version"] if old else 0):
                raise CashbackError("conflict", "群设置已变化，请刷新。", 409)
            agent_rate = config.get("agent_rate", old["agent_rate"] if old else None)
            agent_only = config.get(
                "agent_only", bool(old["agent_only"]) if old else False
            )
            if type(agent_only) not in (bool, int) or agent_only not in (0, 1):
                raise CashbackError("invalid_reward_rule", "仅代理商返现须为勾选项。")
            agent_only = bool(agent_only)
            qualification = config.get(
                "qualification_seconds", old["qualification_seconds"] if old else 0
            )
            require_speech = config.get(
                "require_speech", bool(old["require_speech"]) if old else False
            )
            if type(require_speech) not in (bool, int) or require_speech not in (0, 1):
                raise CashbackError("invalid_speech_rule", "发言核验开关须为勾选项。")
            require_speech = bool(require_speech)
            if require_speech and qualification != 10800:
                raise CashbackError(
                    "invalid_speech_rule", "发言核验须同时启用三小时在群核验。"
                )
            if (
                (
                    agent_rate is not None
                    and (type(agent_rate) is not int or not 0 < agent_rate <= 99999999)
                )
                or type(qualification) is not int
                or qualification not in {0, 10800}
            ):
                raise CashbackError(
                    "invalid_rate", "代理商金额须为0.01至999999.99元，核验期为3小时。"
                )
            contact = config.get("contact", "")
            if not isinstance(contact, str) or len(contact) > 200:
                raise CashbackError(
                    "invalid_contact", "结账联系方式须为1至200字的单行文字。"
                )
            contact = contact.strip()
            if contact and label(contact, 200) != contact:
                raise CashbackError(
                    "invalid_contact", "结账联系方式须为1至200字的单行文字。"
                )
            if not contact and not config.get("cashier"):
                raise CashbackError("invalid_contact", "请填写结账联系方式。")
            cashier = (
                {"email": old["cashier"] if old else actor}
                if contact
                else db.execute(
                    "SELECT principal AS email FROM contacts WHERE principal=? AND active=1",
                    (config["cashier"],),
                ).fetchone()
            )
            if contact and not old and isinstance(actor, DashboardFinanceActor):
                # A non-login row satisfies the legacy contact foreign key only.
                db.execute(
                    "INSERT INTO users(email,name,role) VALUES(?,?,'dashboard') "
                    "ON CONFLICT(email) DO NOTHING",
                    (str(actor), "管理面板总控"),
                )
            # An unavailable contact must not prevent stopping new rewards.
            if (
                not cashier
                and old
                and not config["enabled"]
                and all(
                    config[key] == old[key]
                    for key in ("cashier", "bot_id", "account", "rate")
                )
            ):
                cashier = {"email": old["cashier"]}
            if not cashier:
                # Legacy group configurations may still use a login principal.
                if config["cashier"].startswith("wsl:"):
                    raise CashbackError(
                        "contact_unavailable", "请先设置有效的群内结账联系人。", 409
                    )
                cashier = self._actor(db, config["cashier"])
            baseline = (
                not old
                or (not old["enabled"] and config["enabled"])
                or (
                    old["bot_id"] != config["bot_id"]
                    or old["account"] != config["account"]
                )
            )
            if baseline and members is None:
                raise CashbackError(
                    "baseline_required",
                    "无法取得完整成员名单，不能启用或切换采集机器人。",
                    409,
                )
            collector_changed = old and (
                old["bot_id"] != config["bot_id"] or old["account"] != config["account"]
            )
            started = now if not old or collector_changed else old["started"]
            baseline_started = now if baseline else old["baseline_started"]
            risk_started = now if baseline else old["risk_started"]
            risk_scanned = now if baseline else old["risk_scanned"]
            rate_since = (
                now
                if not old
                or old["rate"] != config["rate"]
                or old["agent_rate"] != agent_rate
                or bool(old["agent_only"]) != agent_only
                or old["qualification_seconds"] != qualification
                else old["rate_since"]
            )
            db.execute(
                "INSERT INTO groups(id,name,bot_id,account,cashier,enabled,rate,started,rate_since,contact,agent_rate,qualification_seconds,risk_started,risk_scanned,require_speech,agent_only,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,bot_id=excluded.bot_id,account=excluded.account,"
                "cashier=excluded.cashier,enabled=excluded.enabled,rate=excluded.rate,started=excluded.started,"
                "rate_since=excluded.rate_since,contact=excluded.contact,"
                "agent_rate=excluded.agent_rate,qualification_seconds=excluded.qualification_seconds,"
                "risk_started=excluded.risk_started,risk_scanned=excluded.risk_scanned,"
                "require_speech=excluded.require_speech,"
                "agent_only=excluded.agent_only,"
                "version=groups.version+1",
                (
                    group,
                    label(config["name"]),
                    config["bot_id"],
                    config["account"],
                    cashier["email"],
                    int(config["enabled"]),
                    config["rate"],
                    started,
                    rate_since,
                    contact,
                    agent_rate,
                    qualification,
                    risk_started,
                    risk_scanned,
                    int(require_speech),
                    int(agent_only),
                ),
            )
            db.execute(
                "UPDATE groups SET baseline_started=? WHERE id=?",
                (baseline_started, group),
            )
            db.execute(
                "INSERT INTO reward_rule_history"
                "(group_id,version,effective_at,enabled,rate,bot_id,account,agent_rate,qualification_seconds,require_speech,agent_only) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    group,
                    old["version"] + 1 if old else 1,
                    now,
                    int(config["enabled"]),
                    config["rate"],
                    config["bot_id"],
                    config["account"],
                    agent_rate,
                    qualification,
                    int(require_speech),
                    int(agent_only),
                ),
            )
            if baseline:
                db.execute(
                    "UPDATE groups SET source_cursor='',source_scanned=0,"
                    "source_error='',source_retry_at=0,source_failures=0 WHERE id=?",
                    (group,),
                )
                known = {str(uid): {} for uid in history}
                known.update(members)
                for uid, member in known.items():
                    self._insert(db, group, uid, member, now, 0, False, uid in members)
            if baseline or require_speech and (not old or not old["require_speech"]):
                db.execute("UPDATE groups SET speech_cursor=0 WHERE id=?", (group,))
            if require_speech:
                db.execute(
                    "UPDATE entries SET state='verifying',"
                    "hold_until=MAX(hold_until,first_seen+10800),version=version+1 "
                    "WHERE group_id=? AND eligible=1 AND state='pending' "
                    "AND integrity_version=1 "
                    "AND present=1 AND ever_left=0 "
                    "AND (first_spoke_at=0 OR hold_until<first_seen+10800)",
                    (group,),
                )
            self._audit(
                db,
                group,
                None,
                actor,
                "rules_changed",
                dict(old) if old else {},
                {
                    **config,
                    "started": started,
                    "baseline": len(history) if baseline else 0,
                },
            )
        return self.get_group(group, actor)

    @staticmethod
    def _insert(
        db,
        group,
        uid,
        member,
        now,
        rate,
        eligible,
        present=True,
        *,
        qualification=0,
        rule_version=0,
    ):
        avatar_ref = avatar_reference(member)
        db.execute(
            "INSERT OR IGNORE INTO entries("
            "id,group_id,member,member_name,peer,first_seen,amount,eligible,state,"
            "present,last_seen,hold_until,rule_version,inviter_class,pricing_status,"
            "member_class,member_avatar_ref,member_avatar_status,member_avatar_digit_count,"
            "evidence_started,integrity_version"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "(SELECT baseline_started FROM groups WHERE id=?),1)",
            (
                uuid.uuid4().hex,
                group,
                uid,
                label(member.get("name", "名称待核验")),
                str(member.get("peer", "")),
                now,
                0 if eligible and qualification else rate,
                int(eligible),
                ("verifying" if qualification else "pending") if eligible else "void",
                int(present),
                now,
                now + qualification if eligible and qualification else 0,
                rule_version if eligible else 0,
                "unclassified" if eligible and qualification else "legacy",
                "unclassified" if eligible and qualification else "legacy",
                "unclassified" if eligible and qualification else "legacy",
                avatar_ref,
                "unavailable",
                0,
                group,
            ),
        )

    def snapshot(
        self,
        group,
        bot,
        account,
        members,
        evidence,
        now=None,
        guard=None,
        *,
        speech=(),
        speech_until=0,
    ):
        """Apply membership and speech evidence within the collector transaction.

        Args:
            group: Exact business group identifier.
            bot: Current collector instance identifier.
            account: Authenticated collector account.
            members: Complete verified roster with native peer bindings.
            evidence: Reliable invitation evidence for this group.
            now: Observation time, defaulting to the current time.
            guard: Authorization callback checked within the transaction.
            speech: Authenticated human group-message timestamps.
            speech_until: Fully read receipt-time boundary for incremental replay.
        """
        now = now or time.time()
        with self.connect(True) as db:
            if guard:
                guard()
            rule = db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
            if not rule or rule["bot_id"] != bot or rule["account"] != account:
                raise CashbackError(
                    "collector_changed", "采集账号已变化，请重新核验。", 409
                )
            if (
                rule["require_speech"]
                and type(speech_until) in (int, float)
                and 0 < speech_until <= now
            ):
                db.execute(
                    "UPDATE groups SET speech_cursor=MAX(speech_cursor,?) WHERE id=?",
                    (speech_until, group),
                )
            previous = {
                r["member"]: dict(r)
                for r in db.execute("SELECT * FROM entries WHERE group_id=?", (group,))
            }
            for uid, row in previous.items():
                present = uid in members
                if row["present"] and not present:
                    state = "void" if row["state"] != "paid" else "paid"
                    db.execute(
                        "UPDATE entries SET present=0,ever_left=1,left_at=?,state=?,version=version+1 WHERE id=?",
                        (now, state, row["id"]),
                    )
                    if row["eligible"]:
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "member_left",
                            {"state": row["state"], "present": True},
                            {"state": state, "present": False},
                        )
                elif not row["present"] and present:
                    db.execute(
                        "UPDATE entries SET present=1,version=version+1 WHERE id=?",
                        (row["id"],),
                    )
                    if row["eligible"]:
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "member_rejoined",
                            {},
                            {
                                "member": uid,
                                "state": row["state"],
                                "ever_left": bool(row["ever_left"]),
                            },
                        )
                if present:
                    member = members[uid]
                    if (
                        row["eligible"]
                        and (row["integrity_version"] or row["state"] == "verifying")
                        and row["member_class"] != "legacy"
                        and row["state"] in {"verifying", "pending"}
                        and not row["ever_left"]
                        and now - row["last_seen"] > 120
                    ):
                        db.execute(
                            "UPDATE entries SET state='verifying',hold_until=?,version=version+1 WHERE id=?",
                            (now + 10800, row["id"]),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "qualification_restarted",
                            {"hold_until": row["hold_until"]},
                            {"hold_until": now + 10800, "reason": "observation_gap"},
                        )
                    changed = row["peer"] and row["peer"] != member["peer"]
                    if (
                        changed
                        and row["eligible"]
                        and row["verification"] != "conflict"
                    ):
                        db.execute(
                            "UPDATE entries SET verification='conflict',version=version+1 WHERE id=?",
                            (row["id"],),
                        )
                        self._audit(
                            db, group, row["id"], "system", "identity_conflict", {}, {}
                        )
                    db.execute(
                        "UPDATE entries SET member_name=?,member_avatar_ref=?,"
                        "last_seen=? WHERE id=?",
                        (
                            label(member["name"]),
                            avatar_reference(member),
                            now,
                            row["id"],
                        ),
                    )
            for uid, member in members.items():
                if uid not in previous:
                    self._insert(
                        db,
                        group,
                        uid,
                        member,
                        now,
                        rule["rate"],
                        bool(rule["enabled"] and member["eligible"]),
                        qualification=rule["qualification_seconds"],
                        rule_version=rule["version"],
                    )
                    self._audit(
                        db,
                        group,
                        None,
                        "system",
                        "member_observed",
                        {},
                        {
                            "member": uid,
                            "name": member["name"],
                            "eligible": bool(rule["enabled"] and member["eligible"]),
                        },
                    )
            for item in evidence:
                if (
                    item.get("source") != "business_apply_log"
                    or not re.fullmatch(
                        r"[1-9][0-9]{0,19}", str(item.get("member", ""))
                    )
                    or not re.fullmatch(
                        r"[1-9][0-9]{0,19}", str(item.get("inviter", ""))
                    )
                    or item["member"] == item["inviter"]
                    or item["inviter"] == account
                    or not isinstance(item.get("event_id"), str)
                    or not item["event_id"]
                    or len(item["event_id"]) > 128
                    or type(item.get("event_at")) not in {int, float}
                    or not math.isfinite(item["event_at"])
                    or not rule["risk_started"] <= item["event_at"] <= now + 30
                ):
                    continue
                member_peer = str(item.get("member_peer", ""))
                inviter_peer = str(item.get("inviter_peer", ""))
                if any(
                    not peer.isascii()
                    or not peer.isdigit()
                    or not 0 < int(peer) < 1 << 32
                    for peer in (member_peer, inviter_peer)
                ):
                    continue
                if (
                    item["member"] in members
                    and (
                        not members[item["member"]]["eligible"]
                        or members[item["member"]]["peer"] != member_peer
                    )
                    or item["inviter"] in members
                    and (
                        not members[item["inviter"]]["eligible"]
                        or members[item["inviter"]]["peer"] != inviter_peer
                    )
                ):
                    continue
                existing = db.execute(
                    "SELECT eligible FROM entries WHERE group_id=? AND member=?",
                    (group, item["member"]),
                ).fetchone()
                if existing and not existing["eligible"]:
                    continue
                name = label(item.get("member_name", ""), 256)
                db.execute(
                    "INSERT OR IGNORE INTO evidence(group_id,member,source,event_id,"
                    "inviter,observed,account,member_peer,inviter_peer,event_at,"
                    "member_name_length) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        group,
                        item["member"],
                        "business_apply_log",
                        item["event_id"],
                        item["inviter"],
                        now,
                        account,
                        member_peer,
                        inviter_peer,
                        item["event_at"],
                        len(name.strip()),
                    ),
                )
            for item in evidence:
                uid, inviter = item["member"], item["inviter"]
                if (
                    item["source"] not in {"nim_notification", "business_apply_log"}
                    or uid == inviter
                ):
                    continue
                if (
                    uid not in members
                    or inviter not in members
                    or not members[uid]["eligible"]
                    or not members[inviter]["eligible"]
                ):
                    continue
                row = db.execute(
                    "SELECT * FROM entries WHERE group_id=? AND member=?", (group, uid)
                ).fetchone()
                if not row or not row["eligible"]:
                    continue
                prior_proof = db.execute(
                    "SELECT * FROM evidence WHERE group_id=? AND member=? AND source=? "
                    "AND event_id=? AND inviter=?",
                    (group, uid, item["source"], item["event_id"], inviter),
                ).fetchone()
                if (
                    prior_proof
                    and prior_proof["event_at"]
                    and item.get("event_at")
                    and abs(prior_proof["event_at"] - item["event_at"]) > 1
                ):
                    if row["verification"] != "conflict":
                        db.execute(
                            "UPDATE entries SET verification='conflict',version=version+1 WHERE id=?",
                            (row["id"],),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "invitation_evidence_conflict",
                            {"event_at": prior_proof["event_at"]},
                            {
                                "observed_event_at": item["event_at"],
                                "source": item["source"],
                            },
                        )
                    continue
                repeated_join = db.execute(
                    "SELECT 1 FROM evidence WHERE group_id=? AND member=? "
                    "AND source=? AND event_id<>? LIMIT 1",
                    (group, uid, item["source"], item["event_id"]),
                ).fetchone()
                if repeated_join and not row["ever_left"]:
                    db.execute(
                        "UPDATE entries SET ever_left=1,left_at=?,"
                        "state=CASE WHEN state='paid' THEN 'paid' ELSE 'void' END,"
                        "version=version+1 WHERE id=?",
                        (now, row["id"]),
                    )
                    self._audit(
                        db,
                        group,
                        row["id"],
                        "system",
                        "repeated_join",
                        {},
                        {"event_id": item["event_id"], "ever_left": True},
                    )
                member_name_length = len(
                    label(item.get("member_name", ""), 256).strip()
                )
                db.execute(
                    "INSERT INTO evidence(group_id,member,source,event_id,inviter,"
                    "observed,account,member_peer,inviter_peer,event_at,member_name_length) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(group_id,member,source,event_id,inviter) DO UPDATE SET "
                    "account=excluded.account,member_peer=excluded.member_peer,"
                    "inviter_peer=excluded.inviter_peer,event_at=excluded.event_at,"
                    "member_name_length=excluded.member_name_length "
                    "WHERE evidence.account='' OR evidence.event_at=0 "
                    "OR evidence.account<>excluded.account",
                    (
                        group,
                        uid,
                        item["source"],
                        item["event_id"],
                        inviter,
                        now,
                        account,
                        item.get("member_peer", members[uid]["peer"]),
                        item.get("inviter_peer", members[inviter]["peer"]),
                        item.get("event_at", now),
                        member_name_length,
                    ),
                )
            self._evaluate_cashback_risk(
                db, group, account, rule["risk_started"], rule["risk_scanned"], now
            )
            # NIM identifies the join operator, which can be the approving owner.
            # Cashier confirmations are stored separately from upstream invite proof.
            business = {}
            for proof in db.execute(
                "SELECT e.* FROM current_invitation_evidence e JOIN entries r "
                "ON r.group_id=e.group_id AND r.member=e.member "
                "WHERE e.group_id=? AND source IN ('business_apply_log','cashier_confirmation') "
                "AND collector_account=? AND event_at>=r.evidence_started AND event_at>0 AND event_at<=? "
                "ORDER BY event_at,event_id",
                (group, account, now + 30),
            ):
                uid, inviter = proof["member"], proof["inviter"]
                if (
                    uid not in members
                    or inviter not in members
                    or not members[uid]["eligible"]
                    or not members[inviter]["eligible"]
                    or proof["member_peer"] != members[uid]["peer"]
                    or proof["inviter_peer"] != members[inviter]["peer"]
                ):
                    continue
                business.setdefault(uid, []).append(dict(proof))
            resolved = []
            for row in db.execute(
                "SELECT * FROM entries WHERE group_id=? AND eligible=1", (group,)
            ).fetchall():
                if not row["integrity_version"]:
                    # Rollout is not a historical ledger repair. Payment
                    # admission reports conflicts without rewriting old rows.
                    continue
                if not row["inviter"] and row["verification"] == "verified":
                    verification = "conflict" if row["state"] == "paid" else "pending"
                    db.execute(
                        "UPDATE entries SET verification=?,version=version+1 WHERE id=?",
                        (verification, row["id"]),
                    )
                    self._audit(
                        db,
                        group,
                        row["id"],
                        "system",
                        "attribution_revalidation_required",
                        {"verification": "verified", "inviter": ""},
                        {"verification": verification, "payment_unchanged": True},
                    )
                    row = {**dict(row), "verification": verification}
                if (
                    row["inviter"] in members
                    and row["inviter_peer"]
                    and row["inviter_peer"] != members[row["inviter"]]["peer"]
                ):
                    if row["verification"] != "conflict":
                        db.execute(
                            "UPDATE entries SET verification='conflict',version=version+1 WHERE id=?",
                            (row["id"],),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "inviter_identity_conflict",
                            {"inviter_peer": row["inviter_peer"]},
                            {"observed_peer": members[row["inviter"]]["peer"]},
                        )
                    continue
                proofs = business.get(row["member"], [])
                if proofs and row["state"] != "paid":
                    prospective = {
                        **dict(row),
                        "inviter": proofs[0]["inviter"],
                        "inviter_peer": proofs[0]["inviter_peer"],
                    }
                    integrity = self._entry_integrity(db, rule, prospective, now=now)
                    if (
                        integrity["review_reason"] not in {"", "attribution_conflict"}
                        or integrity["membership_left_at"]
                    ):
                        continue
                if row["state"] == "paid" and row["paid_at"]:
                    # A later rejoin belongs to a different admission, not to
                    # the already registered payment for the first invitation.
                    proofs = [
                        dict(proof)
                        for proof in db.execute(
                            "SELECT * FROM current_invitation_evidence "
                            "WHERE group_id=? AND member=? AND source IN ('business_apply_log','cashier_confirmation') "
                            "AND member_peer=? AND event_at>0 AND event_at<=? "
                            "ORDER BY event_at,event_id",
                            (group, row["member"], row["peer"], row["paid_at"]),
                        )
                    ]
                    if (
                        row["verification"] == "conflict"
                        and proofs
                        and {proof["inviter"] for proof in proofs} == {row["inviter"]}
                        and all(
                            proof["inviter_peer"] == row["inviter_peer"]
                            for proof in proofs
                        )
                        and not db.execute(
                            "SELECT 1 FROM audit WHERE entry_id=? AND action IN "
                            "('identity_conflict','inviter_identity_conflict',"
                            "'invitation_evidence_conflict','paid_attribution_review_required',"
                            "'price_conflict','self_join_attribution_conflict') LIMIT 1",
                            (row["id"],),
                        ).fetchone()
                    ):
                        db.execute(
                            "UPDATE entries SET verification='verified',version=version+1 "
                            "WHERE id=?",
                            (row["id"],),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "paid_invitation_history_verified",
                            {"verification": "conflict"},
                            {
                                "verification": "verified",
                                "inviter": row["inviter"],
                                "payment_unchanged": True,
                            },
                            "Later rejoin evidence does not reassign an earlier payment.",
                        )
                        row = {**dict(row), "verification": "verified"}
                inviters = {proof["inviter"] for proof in proofs}
                if row["self_join"]:
                    if inviters and row["verification"] != "conflict":
                        db.execute(
                            "UPDATE entries SET verification='conflict',version=version+1 WHERE id=?",
                            (row["id"],),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "self_join_attribution_conflict",
                            {"self_join": True, "state": row["state"]},
                            {"business_inviters": sorted(inviters), "state": "void"},
                            "Later invitation evidence conflicts with confirmed self entry.",
                        )
                    continue
                native = db.execute(
                    "SELECT 1 FROM evidence WHERE group_id=? AND member=? "
                    "AND source='nim_notification' LIMIT 1",
                    (group, row["member"]),
                ).fetchone()
                established_business = db.execute(
                    "SELECT 1 FROM evidence WHERE group_id=? AND member=? "
                    "AND source IN ('business_apply_log','cashier_confirmation') AND inviter=? AND member_peer=? "
                    "AND inviter_peer=? AND account<>'' AND event_at>0 LIMIT 1",
                    (
                        group,
                        row["member"],
                        row["inviter"],
                        row["peer"],
                        row["inviter_peer"],
                    ),
                ).fetchone()
                if len(inviters) > 1:
                    if row["verification"] != "conflict":
                        db.execute(
                            "UPDATE entries SET verification='conflict',version=version+1 WHERE id=?",
                            (row["id"],),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "attribution_conflict",
                            {"inviter": row["inviter"]},
                            {"business_inviters": sorted(inviters)},
                        )
                    continue
                if (
                    row["inviter"]
                    and native
                    and row["inviter"] not in inviters
                    and not established_business
                ):
                    if row["state"] == "paid":
                        if row["verification"] != "conflict":
                            db.execute(
                                "UPDATE entries SET verification='conflict',version=version+1 WHERE id=?",
                                (row["id"],),
                            )
                            self._audit(
                                db,
                                group,
                                row["id"],
                                "system",
                                "paid_attribution_review_required",
                                {
                                    "inviter": row["inviter"],
                                    "verification": row["verification"],
                                },
                                {
                                    "verification": "conflict",
                                    "business_inviters": sorted(inviters),
                                    "payment_unchanged": True,
                                },
                                "Native join operator is not proof of the recruiter.",
                            )
                        continue
                    verification = (
                        "pending"
                        if row["verification"] != "blocked"
                        and not db.execute(
                            "SELECT 1 FROM audit WHERE entry_id=? AND action IN "
                            "('identity_conflict','inviter_identity_conflict','invitation_evidence_conflict') LIMIT 1",
                            (row["id"],),
                        ).fetchone()
                        else row["verification"]
                    )
                    corrected_amount = db.execute(
                        "SELECT 1 FROM audit WHERE entry_id=? "
                        "AND action='amount_corrected' LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    legacy = row["member_class"] == "legacy" or bool(corrected_amount)
                    db.execute(
                        "UPDATE entries SET inviter='',inviter_name='',inviter_peer='',"
                        "verification=?,amount=?,inviter_class=?,inviter_name_snapshot='',"
                        "name_digit_count=0,member_class=?,pricing_status=?,"
                        "member_avatar_status='unavailable',member_avatar_digit_count=0,"
                        "version=version+1 WHERE id=?",
                        (
                            verification,
                            row["amount"] if legacy else 0,
                            "legacy" if legacy else "unclassified",
                            "legacy" if legacy else "unclassified",
                            "legacy" if legacy else "unclassified",
                            row["id"],
                        ),
                    )
                    self._audit(
                        db,
                        group,
                        row["id"],
                        "system",
                        "join_operator_attribution_removed",
                        {
                            "inviter": row["inviter"],
                            "inviter_name": row["inviter_name"],
                            "amount": row["amount"],
                            "verification": row["verification"],
                        },
                        {"inviter": "", "verification": verification},
                        "Native join operator is not proof of the recruiter.",
                    )
                if (
                    proofs
                    and row["member"] in members
                    and proofs[0]["inviter"] in members
                ):
                    resolved.append(proofs[0])
            for item in resolved:
                uid, inviter = item["member"], item["inviter"]
                row = db.execute(
                    "SELECT * FROM entries WHERE group_id=? AND member=?", (group, uid)
                ).fetchone()
                if (
                    row["peer"] != members[uid]["peer"]
                    or row["verification"] == "blocked"
                    or (row["state"] == "paid" and row["inviter"] != inviter)
                ):
                    continue
                price_policy = self._entry_price_policy(
                    db, rule, row, item.get("event_at", 0)
                )
                if not price_policy:
                    # Never reconstruct missing historical prices from current rules.
                    continue
                if item.get("event_at") and item["event_at"] < rule["rate_since"]:
                    corrected_amount = db.execute(
                        "SELECT 1 FROM audit WHERE entry_id=? AND action='amount_corrected' LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    if corrected_amount and row["member_class"] == "legacy":
                        pass
                    elif row["verification"] == "pending" and not row["inviter"]:
                        db.execute(
                            "UPDATE entries SET amount=?,version=version+1 WHERE id=?",
                            (price_policy["rate"], row["id"]),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "historical_price_verified",
                            {"amount": row["amount"]},
                            {
                                "amount": price_policy["rate"],
                                "rule_version": price_policy["version"],
                                "event_at": item["event_at"],
                            },
                        )
                        row = {**dict(row), "amount": price_policy["rate"]}
                    elif (
                        row["member_class"] == "legacy"
                        and row["amount"] != price_policy["rate"]
                    ):
                        db.execute(
                            "UPDATE entries SET verification='conflict',version=version+1 WHERE id=?",
                            (row["id"],),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "price_conflict",
                            {"amount": row["amount"]},
                            {"observed_amount": price_policy["rate"]},
                        )
                        continue
                if row["inviter"] and row["inviter"] != inviter:
                    if row["verification"] != "conflict":
                        db.execute(
                            "UPDATE entries SET verification='conflict',version=version+1 WHERE id=?",
                            (row["id"],),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "attribution_conflict",
                            {"inviter": row["inviter"]},
                            {"observed_inviter": inviter},
                        )
                elif row["verification"] != "conflict" and (
                    not row["inviter"]
                    or (
                        row["inviter"] == inviter
                        and row["state"] != "paid"
                        and row["member_class"] == "legacy"
                        and not db.execute(
                            "SELECT 1 FROM audit WHERE entry_id=? "
                            "AND action='amount_corrected' LIMIT 1",
                            (row["id"],),
                        ).fetchone()
                    )
                ):
                    member = members[uid]
                    corrected_amount = db.execute(
                        "SELECT 1 FROM audit WHERE entry_id=? "
                        "AND action='amount_corrected' LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    if corrected_amount:
                        category, digits, avatar_status = (
                            "legacy",
                            0,
                            "unavailable",
                        )
                        amount, pricing = row["amount"], "priced"
                    else:
                        category, digits, avatar_status, amount, pricing = (
                            member_pricing(member, price_policy)
                        )
                    action = (
                        "invitation_verified"
                        if not row["inviter"]
                        else "member_avatar_classified"
                    )
                    if rule["require_speech"] and action == "invitation_verified":
                        # Re-read messages if recruiter evidence arrived after
                        # the first roster observation and its speech scan.
                        db.execute(
                            "UPDATE groups SET speech_cursor=MIN(speech_cursor,?) WHERE id=?",
                            (item["event_at"], group),
                        )
                    db.execute(
                        "UPDATE entries SET inviter=?,inviter_name=?,inviter_peer=?,"
                        "verification='verified',amount=?,inviter_class='legacy',"
                        "inviter_name_snapshot=?,name_digit_count=0,member_class=?,"
                        "member_avatar_ref=?,member_avatar_status=?,"
                        "member_avatar_digit_count=?,pricing_status=?,rule_version=?,"
                        "evidence_event_id=?,evidence_event_at=?,version=version+1 WHERE id=?",
                        (
                            inviter,
                            label(members[inviter]["name"]),
                            members[inviter]["peer"],
                            amount,
                            label(members[inviter]["name"]),
                            category,
                            avatar_reference(member),
                            avatar_status,
                            digits,
                            pricing,
                            price_policy["version"],
                            item["event_id"],
                            item["event_at"],
                            row["id"],
                        ),
                    )
                    self._audit(
                        db,
                        group,
                        row["id"],
                        "system",
                        action,
                        {},
                        {
                            "inviter": inviter,
                            "amount": amount,
                            "source": item["source"],
                            "member_class": category,
                            "member_avatar_status": avatar_status,
                            "member_avatar_digit_count": digits,
                            "pricing_status": pricing,
                            "rule_version": price_policy["version"],
                        },
                    )
                elif row["inviter"] == inviter:
                    db.execute(
                        "UPDATE entries SET inviter_name=?,member_avatar_ref=? WHERE id=?",
                        (
                            label(members[inviter]["name"]),
                            avatar_reference(members[uid]),
                            row["id"],
                        ),
                    )
            # Retry only unresolved ordinary pricing with the frozen rule.
            # Agent pricing is never inferred from roster/profile data.
            for row in db.execute(
                "SELECT * FROM entries WHERE group_id=? AND eligible=1 "
                "AND state='verifying' AND verification='verified' "
                "AND present=1 AND ever_left=0 AND pricing_status='unconfigured'",
                (group,),
            ).fetchall():
                if not row["hold_until"]:
                    continue
                policy = self._entry_price_policy(db, rule, row)
                if not policy:
                    continue
                db.execute(
                    "UPDATE entries SET inviter_class='legacy',member_class='ordinary',"
                    "member_avatar_ref=?,member_avatar_status='manual',"
                    "member_avatar_digit_count=0,amount=?,pricing_status=?,"
                    "version=version+1 WHERE id=?",
                    (
                        avatar_reference(members.get(row["member"], {})),
                        0 if policy["agent_only"] else policy["rate"],
                        "no_reward" if policy["agent_only"] else "priced",
                        row["id"],
                    ),
                )
                self._audit(
                    db,
                    group,
                    row["id"],
                    "system",
                    "default_price_applied",
                    {
                        "member_class": row["member_class"],
                        "amount": row["amount"],
                        "pricing_status": row["pricing_status"],
                    },
                    {
                        "member_class": "ordinary",
                        "amount": 0 if policy["agent_only"] else policy["rate"],
                        "pricing_status": "no_reward"
                        if policy["agent_only"]
                        else "priced",
                        "rule_version": policy["version"],
                    },
                    "Robot default pricing is ordinary; agent price requires cashier proof.",
                )
            for uid, member in members.items():
                if not member["eligible"]:
                    db.execute(
                        "UPDATE entries SET verification='blocked',version=version+1 WHERE group_id=? AND member=? AND eligible=1 AND verification!='blocked'",
                        (group, uid),
                    )
            if rule["require_speech"]:
                for message in speech:
                    uid = message.get("member")
                    member = members.get(uid)
                    stamp = message.get("event_at")
                    if (
                        not member
                        or not member["eligible"]
                        or message.get("peer") != member["peer"]
                        or type(stamp) not in (int, float)
                        or not rule["started"] <= stamp <= now
                    ):
                        continue
                    row = db.execute(
                        "SELECT * FROM entries WHERE group_id=? AND member=? "
                        "AND peer=? AND eligible=1 AND present=1 AND ever_left=0 "
                        "AND self_join=0 AND first_spoke_at=0 "
                        "AND state IN ('pending','verifying') "
                        "AND (first_seen<=? OR EXISTS (SELECT 1 FROM current_invitation_evidence e "
                        "WHERE e.group_id=entries.group_id AND e.member=entries.member "
                        "AND e.member_peer=entries.peer AND e.collector_account=? "
                        "AND e.source IN ('business_apply_log','cashier_confirmation') "
                        "AND e.event_at>0 AND e.event_at<=?))",
                        (group, uid, member["peer"], stamp, account, stamp),
                    ).fetchone()
                    if row:
                        db.execute(
                            "UPDATE entries SET first_spoke_at=?,version=version+1 WHERE id=?",
                            (stamp, row["id"]),
                        )
                        self._audit(
                            db,
                            group,
                            row["id"],
                            "system",
                            "member_speech_verified",
                            {"first_spoke_at": 0},
                            {"first_spoke_at": stamp},
                        )
                db.execute(
                    "UPDATE entries SET state='verifying',"
                    "hold_until=MAX(hold_until,first_seen+10800),version=version+1 "
                    "WHERE group_id=? AND eligible=1 AND present=1 AND ever_left=0 "
                    "AND integrity_version=1 "
                    "AND state IN ('pending','verifying') "
                    "AND (hold_until<first_seen+10800 OR (state='pending' AND first_spoke_at=0))",
                    (group,),
                )
            for row in db.execute(
                "SELECT * FROM entries WHERE group_id=? AND eligible=1 AND state='verifying' "
                "AND verification='verified' AND present=1 AND ever_left=0 "
                "AND pricing_status='priced' AND hold_until<=? "
                "AND (?=0 OR first_spoke_at>0)",
                (group, now, rule["require_speech"]),
            ).fetchall():
                integrity = self._entry_integrity(db, rule, row, now=now)
                if integrity["review_reason"] or integrity["membership_left_at"]:
                    continue
                db.execute(
                    "UPDATE entries SET state='pending',version=version+1 WHERE id=?",
                    (row["id"],),
                )
                self._audit(
                    db,
                    group,
                    row["id"],
                    "system",
                    "qualification_completed",
                    {"state": "verifying", "hold_until": row["hold_until"]},
                    {"state": "pending", "observed_at": now},
                )
            db.execute("UPDATE groups SET scanned=?,error='' WHERE id=?", (now, group))
            if guard:
                guard()

    def confirm_self_join(self, actor, group, items, reason):
        """Exclude exact unassigned entries after explicit administrator confirmation.

        Args:
            actor: Active local administrator principal.
            group: Exact business group ID.
            items: Entry IDs and current versions, never nickname matches.
            reason: Administrator's confirmation, not absence of source evidence.

        Returns:
            Number of entries newly classified as self entry.

        Raises:
            CashbackError: On unauthorized, stale, paid or attributed entries.
        """
        reason = label(reason, 300)
        if len(reason) < 2:
            raise CashbackError("reason_required", "请填写主动入群的确认依据。")
        if not 1 <= len(items) <= 100 or len({i["id"] for i in items}) != len(items):
            raise CashbackError("invalid_selection", "请选择不重复的成员记录。")
        with self.connect(True) as db:
            self._scope(db, actor, group, True)
            rows = []
            for item in items:
                row = db.execute(
                    "SELECT * FROM entries WHERE group_id=? AND id=? AND eligible=1",
                    (group, item["id"]),
                ).fetchone()
                if not row or row["version"] != item["version"]:
                    raise CashbackError("conflict", "记录已变化，本批次未修改。", 409)
                if row["self_join"]:
                    continue
                business_proof = db.execute(
                    "SELECT 1 FROM evidence WHERE group_id=? AND member=? "
                    "AND source IN ('business_apply_log','cashier_confirmation') LIMIT 1",
                    (group, row["member"]),
                ).fetchone()
                active_unassigned = (
                    row["state"] in {"pending", "verifying"}
                    and row["verification"] == "pending"
                    and row["present"]
                    and row["amount"] == 0
                )
                departed_void = (
                    row["state"] == "void"
                    and row["verification"] == "pending"
                    and row["ever_left"]
                )
                if (
                    row["inviter"]
                    or row["paid_at"] is not None
                    or row["paid_by"] is not None
                    or not (active_unassigned or departed_void)
                    or business_proof
                ):
                    raise CashbackError(
                        "self_join_unavailable",
                        "存在邀请、付款或冲突证据，不能直接确认为主动入群。",
                        409,
                    )
                rows.append(dict(row))
            for row in rows:
                db.execute(
                    "UPDATE entries SET self_join=1,state='void',verification='blocked',"
                    "version=version+1 WHERE id=?",
                    (row["id"],),
                )
                self._audit(
                    db,
                    group,
                    row["id"],
                    actor,
                    "self_join_confirmed",
                    {
                        "state": row["state"],
                        "verification": row["verification"],
                        "member": row["member"],
                    },
                    {"self_join": True, "state": "void", "verification": "blocked"},
                    reason,
                )
            return len(rows)

    def scan_error(self, group, code):
        with self.connect(True) as db:
            db.execute("UPDATE groups SET error=? WHERE id=?", (code, group))

    @staticmethod
    def _source_group(db, group, bot, account, started):
        """Bind source progress to the exact collector and activation baseline.

        Args:
            db: Transaction connection.
            group: Business group ID.
            bot: Collector instance ID.
            account: Authenticated business account.
            started: Captured activation baseline.

        Returns:
            The matching group rule.

        Raises:
            CashbackError: If the collection scope changed.
        """
        rule = db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
        if (
            not rule
            or rule["bot_id"] != bot
            or rule["account"] != account
            or rule["started"] != started
        ):
            raise CashbackError(
                "collector_changed", "采集范围已变化，请重新核验。", 409
            )
        return rule

    def source_checkpoint(self, group, bot, account, started):
        """Read the persistent cursor and retry boundary for the collector.

        Args:
            group: Business group ID.
            bot: Collector instance ID.
            account: Authenticated business account.
            started: Captured activation baseline.

        Returns:
            The current bound collection checkpoint.

        Raises:
            CashbackError: If the collection scope changed.
        """
        with self.connect() as db:
            rule = dict(self._source_group(db, group, bot, account, started))
            floor = db.execute(
                "SELECT MIN(evidence_started) FROM entries WHERE group_id=? "
                "AND eligible=1 AND self_join=0 AND evidence_started>0",
                (group,),
            ).fetchone()[0]
            rule["evidence_floor"] = min(started, floor) if floor else started
            return rule

    def remember_source_page(self, group, bot, account, started, page):
        """Persist approved candidates before advancing the incremental cursor.

        Args:
            group: Exact business group.
            bot: Collector instance.
            account: Authenticated business account.
            started: Activation baseline captured before the platform request.
            page: Sanitized account-visible invitation response.

        Raises:
            CashbackError: If scope, cursor or candidate framing is inconsistent.
        """
        items = page.get("items")
        cursor = page.get("last_id", "")
        if (
            not isinstance(items, list)
            or len(items) > 4096
            or not isinstance(cursor, str)
            or (cursor and not re.fullmatch(r"[0-9]{1,20}", cursor))
            or (cursor and int(cursor) > time.time() + 30)
        ):
            raise CashbackError("invitation_response", "邀请来源数据无法核验。", 409)
        records, events, quarantined = [], [], []
        for item in items:
            if not isinstance(item, dict):
                raise CashbackError(
                    "invitation_response", "邀请来源数据无法核验。", 409
                )
            clean = {
                key: item.get(key, "")
                for key in (
                    "apply_id",
                    "member_id",
                    "member_nim_id",
                    "inviter_id",
                    "inviter_nim_id",
                    "apply_time",
                    "state",
                )
            }
            clean["apply_type"] = item.get(
                "event_type",
                item.get("apply_type", item.get("group_apply_type", "INVITATION")),
            )
            clean["member_name"] = label(item.get("member_name", ""), 256)
            fingerprint = hashlib.sha256(
                json.dumps(clean, sort_keys=True, default=str).encode()
            ).hexdigest()
            valid = {
                key: isinstance(clean[key], str)
                and re.fullmatch(r"[1-9][0-9]{0,19}", clean[key]) is not None
                for key in (
                    "apply_id",
                    "member_id",
                    "member_nim_id",
                    "inviter_id",
                    "inviter_nim_id",
                )
            }
            try:
                stamp = source_timestamp(clean["apply_time"])
            except (ValueError, TypeError, OverflowError, OSError):
                stamp = 0
            issue = (
                "invalid_member_event"
                if not all(
                    valid[key] for key in ("apply_id", "member_id", "member_nim_id")
                )
                or not stamp
                or not isinstance(clean["state"], str)
                or not 1 <= len(clean["state"]) <= 80
                or not isinstance(clean["apply_type"], str)
                or not 1 <= len(clean["apply_type"]) <= 80
                else "missing_inviter"
                if clean["apply_type"] == "INVITATION"
                and clean["state"] == "MEMBER_STATE_GOOD"
                and not (valid["inviter_id"] and valid["inviter_nim_id"])
                else ""
            )
            if issue:
                quarantined.append(
                    (
                        fingerprint,
                        clean["member_id"] if valid["member_id"] else "",
                        clean["apply_id"] if valid["apply_id"] else "",
                        stamp,
                        issue,
                    )
                )
            if issue == "invalid_member_event":
                continue
            for key in ("inviter_id", "inviter_nim_id"):
                if not valid[key]:
                    clean[key] = ""
            payload = json.dumps(
                clean,
                sort_keys=True,
                separators=(",", ":"),
            )
            records.append((hashlib.sha256(payload.encode()).hexdigest(), payload))
            events.append(
                (
                    clean["member_id"],
                    clean["apply_id"],
                    stamp,
                    clean["apply_type"],
                    clean["state"],
                    clean["member_nim_id"],
                    clean["inviter_id"],
                    clean["inviter_nim_id"],
                    hashlib.sha256(
                        json.dumps(
                            [
                                clean["member_id"],
                                clean["apply_id"],
                                stamp,
                                clean["apply_type"],
                                clean["state"],
                                clean["member_nim_id"],
                                clean["inviter_id"],
                                clean["inviter_nim_id"],
                            ],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                )
            )
        now = time.time()
        with self.connect(True) as db:
            rule = self._source_group(db, group, bot, account, started)
            if (
                cursor
                and rule["source_cursor"]
                and int(cursor) < int(rule["source_cursor"])
            ):
                raise CashbackError(
                    "invitation_cursor", "邀请采集游标异常，未推进进度。", 409
                )
            for fingerprint, payload in records:
                db.execute(
                    "INSERT OR IGNORE INTO invitation_source_records"
                    "(group_id,bot_id,account,started,fingerprint,payload,observed) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (group, bot, account, started, fingerprint, payload, now),
                )
            for event in events:
                db.execute(
                    "INSERT OR IGNORE INTO cashback_membership_events "
                    "(group_id,bot_id,account,member,event_id,event_at,apply_type,state,"
                    "peer,inviter,inviter_peer,fingerprint,observed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (group, bot, account, *event, now),
                )
            for rejected in quarantined:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO cashback_source_quarantine "
                    "(group_id,bot_id,account,fingerprint,member,event_id,event_at,reason,observed) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (group, bot, account, *rejected, now),
                )
                if inserted.rowcount:
                    self._audit(
                        db,
                        group,
                        None,
                        "system",
                        "invitation_source_row_quarantined",
                        {},
                        {
                            "fingerprint": rejected[0],
                            "member": rejected[1],
                            "event_id": rejected[2],
                            "event_at": rejected[3],
                            "reason_code": rejected[4],
                            "source": "business_apply_log",
                        },
                        "Invalid source row isolated; other candidates remain usable.",
                    )
            recovered = rule["source_error"]
            if not rule["source_scanned"] and not recovered:
                failed = db.execute(
                    "SELECT code FROM audit WHERE group_id=? "
                    "AND action='invitation_source_failed' AND (bot_id=? OR bot_id='') "
                    "ORDER BY id DESC LIMIT 1",
                    (group, bot),
                ).fetchone()
                recovered = failed["code"] if failed else ""
            db.execute(
                "UPDATE groups SET source_cursor=?,source_scanned=?,source_error='',"
                "source_retry_at=0,source_failures=0 WHERE id=?",
                (cursor or rule["source_cursor"], now, group),
            )
            if recovered:
                self._audit(
                    db,
                    group,
                    None,
                    "system",
                    "invitation_source_recovered",
                    {"reason_code": recovered},
                    {"source": "business_apply_log", "count": len(records)},
                )
        if recovered:
            logger.info(
                "Cashback invitation source recovered bot=%s group=%s code=%s records=%s",
                bot,
                group,
                recovered,
                len(records),
            )

    def source_records(self, group, bot, account, started):
        """Replay approved candidates when membership mapping arrives later.

        Args:
            group: Business group ID.
            bot: Collector instance ID.
            account: Authenticated business account.
            started: Captured activation baseline.

        Returns:
            Approved source candidates, not unconditionally payable entries.

        Raises:
            CashbackError: If the scope changed or persisted data is invalid.
        """
        with self.connect() as db:
            self._source_group(db, group, bot, account, started)
            try:
                return [
                    json.loads(row["payload"])
                    for row in db.execute(
                        "SELECT payload FROM invitation_source_records WHERE "
                        "group_id=? AND bot_id=? AND account=? AND started=? "
                        "ORDER BY observed,fingerprint",
                        (group, bot, account, started),
                    )
                ]
            except (TypeError, ValueError) as exc:
                raise CashbackError(
                    "invitation_cache", "邀请来源缓存无法核验，暂不推进归属。", 409
                ) from exc

    def source_failed(self, group, bot, account, started, code, retry_after=0):
        """Back off repeated source faults without invalidating reliable history.

        Args:
            group: Business group ID.
            bot: Collector instance ID.
            account: Authenticated business account.
            started: Captured activation baseline.
            code: Allowlisted source error code.
            retry_after: Provider's minimum retry delay in seconds.

        Raises:
            CashbackError: If the collection scope changed.
        """
        with self.connect(True) as db:
            rule = self._source_group(db, group, bot, account, started)
            failures = min(rule["source_failures"] + 1, 1000)
            delay = max(min(35 * 2 ** min(failures, 3), 300), retry_after)
            db.execute(
                "UPDATE groups SET source_error=?,source_retry_at=?,source_failures=? "
                "WHERE id=?",
                (code, time.time() + delay, failures, group),
            )

    def operation_log(
        self,
        action,
        *,
        actor="system",
        group="",
        level="info",
        code="",
        detail=None,
        bot_id="",
        trace="",
        sample_seconds=0,
    ):
        """Persist bounded diagnostic metadata without request bodies or tokens.

        Args:
            action: Fixed service action or route name.
            actor: Verified principal, or system/untrusted marker.
            group: Authorized group scope; empty for authentication failures.
            level: info, warning or error.
            code: Stable error identifier, not an exception message.
            detail: Allowlisted diagnostic fields.
            bot_id: Collector instance identifier.
            trace: Server-generated request correlation identifier.
            sample_seconds: Minimum persisted interval for identical health logs.
        """
        allowed = {
            "status",
            "method",
            "duration_ms",
            "count",
            "source",
            "exception_type",
            "members",
            "evidence",
            "outcome",
            "reason_code",
            "field",
            "operation",
            "member",
            "event_id",
        }
        safe = {
            k: v if type(v) in (int, float, bool) else label(v, 120)
            for k, v in (detail or {}).items()
            if k in allowed
        }
        trace = trace or REQUEST_TRACE.get() or uuid.uuid4().hex
        now = time.time()
        with self.connect(True) as db:
            if sample_seconds:
                previous = db.execute(
                    "SELECT created FROM audit WHERE action=? AND group_id=? "
                    "AND actor=? AND level=? AND code=? ORDER BY created DESC LIMIT 1",
                    (label(action, 80), group, actor, level, label(code, 80)),
                ).fetchone()
                if previous and now - previous["created"] < sample_seconds:
                    return False
            db.execute(
                "INSERT INTO audit(group_id,actor,action,reason,before_json,after_json,created,level,code,trace,bot_id) "
                "VALUES(?,?,?,'','{}',?,?,?,?,?,?)",
                (
                    group,
                    actor,
                    label(action, 80),
                    json.dumps(safe, ensure_ascii=False),
                    now,
                    level,
                    label(code, 80),
                    trace,
                    bot_id,
                ),
            )
        getattr(
            logger,
            "error"
            if level == "error"
            else "warning"
            if level == "warning"
            else "info"
            if safe.get("outcome") == "recovered"
            else "debug",
        )(
            "Cashback action=%s bot=%s group=%s code=%s trace=%s duration_ms=%s",
            action,
            bot_id,
            group,
            code,
            trace,
            safe.get("duration_ms", "-"),
            extra={
                "cashier_diagnostic": {
                    "event": "http_request",
                    "code": label(code, 80),
                    "trace": trace,
                    "method": safe.get("method", ""),
                    "status": safe.get("status", 0),
                    "duration_ms": safe.get("duration_ms", 0),
                    "exception_type": safe.get("exception_type", ""),
                }
            },
        )
        return True

    def _entry_integrity(self, db, rule, row, *, now=None, admission=None):
        """Read admission-cycle conflicts without rewriting historical ledger data.

        Args:
            db: Current read or write transaction.
            rule: Authorized group and collector.
            row: Original ledger entry.
            now: Captured collector time, or the current request time.
            admission: Optional evidence batched within this same read transaction.

        Returns:
            A read-only review reason, departure time, and original proof identity.
        """
        now = time.time() if now is None else now
        proofs = (
            admission["proofs"]
            if admission is not None
            else db.execute(
                "SELECT * FROM current_invitation_evidence WHERE group_id=? AND member=? "
                "AND source IN ('business_apply_log','cashier_confirmation') "
                "AND member_peer=? AND collector_account=? AND event_at>=? "
                "AND event_at>0 AND event_at<=? ORDER BY event_at,event_id",
                (
                    rule["id"],
                    row["member"],
                    row["peer"],
                    rule["account"],
                    row["evidence_started"],
                    now + 30,
                ),
            ).fetchall()
        )
        issue = ""
        if not proofs:
            issue = "invitation_unconfirmed"
        elif {(p["inviter"], p["inviter_peer"]) for p in proofs} != {
            (row["inviter"], row["inviter_peer"])
        }:
            issue = "attribution_conflict"
        anchor = row["evidence_event_at"] or (
            proofs[0]["event_at"] if proofs else row["evidence_started"]
        )
        event_id = row["evidence_event_id"] or (proofs[0]["event_id"] if proofs else "")
        events = (
            [e for e in admission["events"] if e["event_at"] >= anchor]
            if admission is not None
            else db.execute(
                "SELECT * FROM cashback_membership_events WHERE group_id=? AND bot_id=? "
                "AND account=? AND member=? AND event_at>=? "
                "AND (state='MEMBER_STATE_GOOD' OR apply_type IN ('LEAVE','KICK','KICK_MEMBER','REMOVE') "
                "OR state IN ('MEMBER_STATE_LEFT','MEMBER_STATE_KICKED','MEMBER_STATE_REMOVED')) "
                "ORDER BY event_at DESC,event_id DESC",
                (rule["id"], rule["bot_id"], rule["account"], row["member"], anchor),
            ).fetchall()
        )
        departed = [
            e["event_at"]
            for e in events
            if e["apply_type"] in {"LEAVE", "KICK", "KICK_MEMBER", "REMOVE"}
            or e["state"]
            in {"MEMBER_STATE_LEFT", "MEMBER_STATE_KICKED", "MEMBER_STATE_REMOVED"}
        ]
        left_at = max(departed, default=0)
        if events:
            latest = events[0]
            if sum(e["event_at"] == latest["event_at"] for e in events) > 1:
                issue = "membership_event_ambiguous"
            elif latest["peer"] != row["peer"]:
                issue = "member_identity_conflict"
            elif latest["state"] == "MEMBER_STATE_GOOD" and latest[
                "apply_type"
            ] not in {"LEAVE", "KICK", "KICK_MEMBER", "REMOVE"}:
                if latest["apply_type"] != "INVITATION":
                    issue = "admission_superseded"
                elif not latest["inviter"] or not latest["inviter_peer"]:
                    issue = "invitation_unconfirmed"
                elif (latest["inviter"], latest["inviter_peer"]) != (
                    row["inviter"],
                    row["inviter_peer"],
                ):
                    issue = "attribution_conflict"
                elif (
                    event_id
                    and latest["event_id"] != event_id
                    and latest["event_at"] > anchor
                ):
                    issue = "admission_superseded"
        quarantined = (
            any(
                q["event_at"] == 0 or q["event_at"] >= anchor
                for q in admission["quarantine"]
            )
            if admission is not None
            else db.execute(
                "SELECT 1 FROM cashback_source_quarantine WHERE group_id=? AND bot_id=? "
                "AND account=? AND member=? AND reason='invalid_member_event' "
                "AND (event_at=0 OR event_at>=?) LIMIT 1",
                (rule["id"], rule["bot_id"], rule["account"], row["member"], anchor),
            ).fetchone()
        )
        if quarantined:
            issue = "invalid_member_event"
        return {
            "review_reason": issue,
            "membership_left_at": left_at,
            "evidence_event_id": event_id,
            "evidence_event_at": anchor if proofs else 0,
        }

    def _price_preview(self, db, actor, group, items, category):
        """Build the exact price confirmation inside the caller's transaction.

        Args:
            db: Read snapshot or atomic mutation transaction.
            actor: Authorized cashier principal.
            group: Exact group ID.
            items: Versioned entry selections.
            category: Target ordinary or agent price.

        Returns:
            Canonical confirmation including both rule and entry versions.

        Raises:
            CashbackError: If an entry, identity, or price cannot be confirmed.
        """
        rule, _ = self._scope(db, actor, group)
        if category not in {"ordinary", "agent"}:
            raise CashbackError("invalid_price_class", "价格类别无效。")
        if not 1 <= len(items) <= 100 or len({i["id"] for i in items}) != len(items):
            raise CashbackError("invalid_selection", "每次请选择1至100条不重复记录。")
        prices, recipients = [], set()
        for item in items:
            row = db.execute(
                "SELECT * FROM entries WHERE group_id=? AND id=? AND eligible=1",
                (group, item["id"]),
            ).fetchone()
            if not row or row["version"] != item["version"]:
                raise CashbackError("conflict", "记录已变化，请刷新后重新确认。", 409)
            integrity = self._entry_integrity(db, rule, row)
            if (
                row["state"] not in {"pending", "verifying"}
                or row["verification"] != "verified"
                or not row["inviter"]
                or not row["present"]
                or row["ever_left"]
                or row["self_join"]
                or integrity["review_reason"]
                or integrity["membership_left_at"]
                or row["member_class"] not in {"ordinary", "agent"}
            ):
                raise CashbackError(
                    "price_change_forbidden", "此记录不能切换价格，请刷新核验。", 409
                )
            policy = self._entry_price_policy(db, rule, row)
            if not policy:
                raise CashbackError(
                    "historical_price_unavailable", "原价格规则无法核验。", 409
                )
            target = (
                rule["agent_rate"]
                if category == "agent"
                else 0
                if policy["agent_only"]
                else policy["rate"]
            )
            if target is None:
                raise CashbackError(
                    "agent_rate_unconfigured", "尚未设置代理商单价。", 409
                )
            if row["member_class"] == category and row["amount"] == target:
                raise CashbackError(
                    "price_class_unchanged", "该记录已经是当前价格类别。", 409
                )
            prices.append(
                {
                    "id": row["id"],
                    "version": row["version"],
                    "original_amount": row["amount"],
                    "target_amount": target,
                    "original_category": row["member_class"],
                    "price_rule_version": rule["version"]
                    if category == "agent"
                    else policy["version"],
                }
            )
            recipients.add(row["inviter"])
        if len(recipients) != 1:
            raise CashbackError("mixed_recipients", "一次只能为同一邀请人切换价格。")
        confirmation = {
            "group": group,
            "category": category,
            "rule_version": rule["version"],
            "items": sorted(prices, key=lambda p: p["id"]),
        }
        confirmation["digest"] = hashlib.sha256(
            json.dumps(confirmation, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return confirmation

    def price_preview(self, actor, group, items, category):
        """Read a price confirmation from one consistent SQLite snapshot.

        Args:
            actor: Authorized cashier principal.
            group: Exact group ID.
            items: Versioned entry selections.
            category: Target price category.

        Returns:
            Exact server-side price confirmation.
        """
        with self.connect() as db:
            db.execute("BEGIN")
            return self._price_preview(db, actor, group, items, category)

    def prior_request(
        self,
        actor,
        group,
        operation,
        items,
        request_id,
        reason="",
        category=None,
        inviter=None,
        *,
        price_confirmation=None,
        actual_payment_confirmed=False,
    ):
        digest = hashlib.sha256(
            json.dumps(
                [group, operation, items, label(reason, 300), category]
                + ([inviter] if operation == "assign" else [])
                + ([price_confirmation] if operation == "reprice" else [])
                + ([actual_payment_confirmed] if operation == "exception" else []),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.connect() as db:
            self._scope(db, actor, group, operation == "exception")
            prior = db.execute(
                "SELECT * FROM requests WHERE actor=? AND request_id=?",
                (actor, request_id),
            ).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise CashbackError(
                        "idempotency_conflict", "同一请求不能提交不同内容。", 409
                    )
                return json.loads(prior["result"])
        return None

    def inviter_options(self, actor, group):
        """List existing, reliably attributed inviters in the current fresh roster.

        Args:
            actor: Verified cashier principal with access to the group.
            group: Exact configured business group ID.

        Returns:
            Named account identities, never recipients inferred from NIM operators.
        """
        with self.connect() as db:
            rule, _ = self._scope(db, actor, group)
            if rule["error"] or time.time() - rule["scanned"] > 20:
                raise CashbackError(
                    "roster_unavailable", "名单暂不可用，请稍后刷新。", 409
                )
            return [
                dict(row)
                for row in db.execute(
                    "SELECT DISTINCT r.inviter AS uid,m.member_name AS name "
                    "FROM entries r JOIN entries m ON m.group_id=r.group_id "
                    "AND m.member=r.inviter AND m.peer=r.inviter_peer "
                    "WHERE r.group_id=? AND r.inviter<>? AND r.eligible=1 "
                    "AND r.verification='verified' AND r.self_join=0 "
                    "AND m.present=1 AND m.last_seen>=? "
                    "AND EXISTS (SELECT 1 FROM current_invitation_evidence p "
                    "WHERE p.group_id=r.group_id AND p.member=r.member "
                    "AND p.inviter=r.inviter AND p.member_peer=r.peer "
                    "AND p.inviter_peer=r.inviter_peer AND p.event_at>0 "
                    "AND p.source='business_apply_log') ORDER BY name,uid",
                    (group, rule["account"], time.time() - 20),
                )
            ]

    def list_entries(
        self,
        actor,
        group,
        state="pending",
        search="",
        page=1,
        period="all",
        start_at=None,
        end_at=None,
        pagination="entries",
        inviter=None,
        member_page=1,
    ):
        """Read a filtered ledger with bounded account-based grouping.

        Args:
            actor: Authenticated cashier or administrator.
            group: Authorized business group.
            state: Settlement state or unassigned attribution view.
            search: Literal inviter or invitee name substring.
            page: Outer page, using entries or distinct inviter accounts.
            period: Beijing time window selector.
            start_at: Inclusive custom window timestamp.
            end_at: Exclusive custom window timestamp.
            pagination: Legacy entry pages or grouped inviter pages.
            inviter: Exact account for a grouped member-page request; an empty
                string selects the unassigned section.
            member_page: Member page within the requested inviter section.

        Returns:
            Filter-wide totals and bounded rows. Grouped section totals cover
            every matching member, not just the visible member page.

        Raises:
            CashbackError: If query parameters or authorization are invalid.
        """
        if (
            state
            not in {"verifying", "pending", "paid", "void", "unassigned", "no_reward"}
            or type(page) is not int
            or not 1 <= page <= 100000
            or pagination not in {"entries", "inviters"}
            or type(member_page) is not int
            or not 1 <= member_page <= 100000
            or inviter is not None
            and (
                not isinstance(inviter, str)
                or inviter != ""
                and not re.fullmatch(r"[0-9]{1,32}", inviter)
            )
            or pagination == "entries"
            and (inviter is not None or member_page != 1)
            or inviter is None
            and member_page != 1
        ):
            raise CashbackError("invalid_query", "查询条件无效。")
        window = entry_time_window(period, start_at=start_at, end_at=end_at)
        search = label(search, 80)
        with self.connect() as db:
            # All counts, sections and member pages use one read snapshot.
            cache_version = (
                db.execute("PRAGMA data_version").fetchone()[0]
                if self._reuse_reads and state == "paid"
                else None
            )
            db.execute("BEGIN")
            rule, person = self._scope(db, actor, group)
            cache_time = time.time()
            cache_key = (
                actor,
                group,
                search,
                page,
                window,
                pagination,
                inviter,
                member_page,
            )
            if cache_version is not None:
                # Check after the authority read pins the snapshot, too: a
                # writer may commit between the first pragma and that read.
                if db.execute("PRAGMA data_version").fetchone()[0] != cache_version:
                    cache_version = None
                cached = self._paid_pages.get(db)
                if (
                    cached is not None
                    and cached[0] == cache_version
                    and cached[1] == cache_key
                    and cached[2] <= cache_time < cached[3]
                ):
                    return {
                        **cached[4],
                        "items": [dict(item) for item in cached[4]["items"]],
                        "sections": [
                            dict(section) for section in cached[4]["sections"]
                        ],
                        "group": rule,
                    }
            args = [group]
            where = "group_id=? AND eligible=1 AND self_join=0 AND "
            if state == "unassigned":
                where += "inviter='' AND verification!='blocked' AND "
            elif state == "paid":
                # Registered payments remain visible without current invitation proof.
                where += "state='paid' AND "
            else:
                where += (
                    "EXISTS (SELECT 1 FROM current_invitation_evidence e "
                    "WHERE e.group_id=entries.group_id AND e.member=entries.member "
                    "AND e.inviter=entries.inviter "
                    "AND e.source IN ('business_apply_log','cashier_confirmation') "
                    "AND e.collector_account=? AND e.member_peer=entries.peer "
                    "AND e.inviter_peer=entries.inviter_peer AND e.event_at>0) "
                    + (
                        "AND state IN ('pending','verifying') AND pricing_status='no_reward' AND "
                        if state == "no_reward"
                        else "AND state=? AND "
                    )
                )
                args.append(rule["account"])
                if state != "no_reward":
                    args.append(state)
            where += "1=1"
            if search:
                where += " AND (instr(inviter_name,?)>0 OR instr(member_name,?)>0)"
                args.extend((search, search))
            if state == "verifying":
                where += " AND inviter<>''"
            if state in {"pending", "verifying"}:
                where += " AND pricing_status!='no_reward'"
            if window:
                where += " AND first_seen>=? AND first_seen<?"
                args.extend(window)
            summaries = []
            if pagination == "inviters":
                # Reuse one filtered aggregation for totals and the bounded page.
                summary_rows = db.execute(
                    "WITH grouped AS ("
                    "SELECT inviter,COUNT(*) AS count,COALESCE(SUM(amount),0) AS amount,"
                    "MIN(state='paid' AND verification='conflict') AS historical "
                    f"FROM entries WHERE {where} GROUP BY inviter),"
                    "totals AS (SELECT COALESCE(SUM(count),0) AS total,"
                    "COALESCE(SUM(amount),0) AS total_amount,COUNT(*) AS section_total "
                    "FROM grouped), selected AS (SELECT * FROM grouped "
                    + ("WHERE inviter=? " if inviter is not None else "")
                    + "ORDER BY inviter LIMIT 5 OFFSET "
                    + (
                        "0"
                        if inviter is not None
                        else "MAX(0,MIN(?,(SELECT (section_total-1)/5 FROM totals)))*5"
                    )
                    + ") SELECT totals.*,selected.* FROM totals "
                    "LEFT JOIN selected ON 1",
                    (
                        *args,
                        inviter if inviter is not None else page - 1,
                    ),
                ).fetchall()
                total = tuple(
                    summary_rows[0][field]
                    for field in ("total", "total_amount", "section_total")
                )
                summaries = [
                    {
                        field: row[field]
                        for field in ("inviter", "count", "amount", "historical")
                    }
                    for row in summary_rows
                    if row["inviter"] is not None
                ]
            else:
                total = db.execute(
                    f"SELECT COUNT(*),COALESCE(SUM(amount),0),COUNT(DISTINCT inviter) "
                    f"FROM entries WHERE {where}",
                    args,
                ).fetchone()
            page_size = 5 if pagination == "inviters" else 100
            if not total[0]:
                return {
                    "items": [],
                    "total": 0,
                    "amount": 0,
                    "payable_count": 0,
                    "payable_amount": 0,
                    "risk_blocked_count": 0,
                    "risk_blocked_amount": 0,
                    "page": 1,
                    "page_size": page_size,
                    "page_count": 1,
                    "pagination": pagination,
                    "section_total": 0,
                    "sections": [],
                    "group": rule,
                }
            frozen_inviters = {
                row["inviter"]
                for row in db.execute(
                    "SELECT inviter FROM cashback_inviter_risk WHERE group_id=? "
                    "AND account=? AND blocked_at IS NOT NULL",
                    (group, rule["account"]),
                )
            }
            payable = (0, 0)
            if state == "pending":
                payable = db.execute(
                    f"SELECT COUNT(*),COALESCE(SUM(amount),0) FROM entries WHERE {where} "
                    "AND verification='verified' AND present=1 AND ever_left=0 "
                    "AND pricing_status='priced' AND member_class IN ('ordinary','agent') "
                    "AND NOT EXISTS (SELECT 1 FROM cashback_inviter_risk cr "
                    "WHERE cr.group_id=entries.group_id AND cr.account=? "
                    "AND cr.inviter=entries.inviter AND cr.blocked_at IS NOT NULL) "
                    f"AND EXISTS (SELECT 1 FROM ({ADMISSION_CHECK_SQL}) ac "
                    "WHERE ac.id=entries.id AND ac.payable_evidence=1) "
                    "AND hold_until<=? AND (?=0 OR first_spoke_at>0)",
                    (
                        *args,
                        rule["account"],
                        time.time(),
                        rule["require_speech"],
                    ),
                ).fetchone()
            sections = []
            page_count = max(
                1,
                math.ceil(
                    (total[2] if pagination == "inviters" else total[0]) / page_size
                ),
            )
            page = min(page, page_count)
            entry_cursor = db.cursor()
            entry_cursor.row_factory = None
            if pagination == "inviters":
                rows = []
                for summary in summaries:
                    member_count = max(1, math.ceil(summary["count"] / 100))
                    section_page = min(member_page, member_count)
                    section_rows = entry_cursor.execute(
                        f"SELECT * FROM entries WHERE {where} AND inviter=? "
                        "ORDER BY first_seen,id LIMIT 100 OFFSET ?",
                        (*args, summary["inviter"], (section_page - 1) * 100),
                    ).fetchall()
                    rows.extend(section_rows)
                    latest = db.execute(
                        "SELECT e.inviter_name,m.member_name FROM entries e "
                        "LEFT JOIN entries m ON m.group_id=e.group_id "
                        "AND m.member=e.inviter AND m.peer=e.inviter_peer "
                        f"WHERE e.id=(SELECT id FROM entries WHERE {where} AND inviter=? "
                        "ORDER BY first_seen DESC,rowid DESC LIMIT 1)",
                        (*args, summary["inviter"]),
                    ).fetchone()
                    sections.append(
                        {
                            **dict(summary),
                            "risk_blocked": int(summary["inviter"] in frozen_inviters),
                            "inviter_name": latest["member_name"]
                            or latest["inviter_name"],
                            "page": section_page,
                            "page_size": 100,
                            "page_count": member_count,
                        }
                    )
            else:
                rows = entry_cursor.execute(
                    f"SELECT * FROM entries WHERE {where} ORDER BY inviter,first_seen,id LIMIT 100 OFFSET ?",
                    (*args, (page - 1) * 100),
                ).fetchall()
            # Materialize wide entries only once; keep evidence in this snapshot.
            entry_columns = tuple(field[0] for field in entry_cursor.description or ())
            rows = [dict(zip(entry_columns, row)) for row in rows]
            restarts, confirmations, settlements = {}, set(), {}
            admissions = {}
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                admissions = {
                    row["id"]: {"proofs": [], "events": [], "quarantine": []}
                    for row in rows
                }
                for proof in db.execute(
                    "SELECT p.inviter,p.inviter_peer,p.event_id,p.event_at,r.id AS entry_id "
                    "FROM current_invitation_evidence p "
                    "JOIN entries r ON r.group_id=p.group_id AND r.member=p.member "
                    f"WHERE r.id IN ({placeholders}) AND p.member_peer=r.peer "
                    "AND p.source IN ('business_apply_log','cashier_confirmation') "
                    "AND p.collector_account=? AND p.event_at>=r.evidence_started "
                    "AND p.event_at>0 AND p.event_at<=? ORDER BY p.event_at,p.event_id",
                    (*ids, rule["account"], time.time() + 30),
                ):
                    admissions[proof["entry_id"]]["proofs"].append(proof)
                for event in db.execute(
                    "SELECT m.event_id,m.event_at,m.apply_type,m.state,m.peer,m.inviter,"
                    "m.inviter_peer,r.id AS entry_id FROM cashback_membership_events m "
                    "JOIN entries r ON r.group_id=m.group_id AND r.member=m.member "
                    f"WHERE r.id IN ({placeholders}) AND m.account=? AND m.bot_id=? "
                    "AND m.event_at>=r.evidence_started "
                    "AND (m.state='MEMBER_STATE_GOOD' OR m.apply_type IN ('LEAVE','KICK','KICK_MEMBER','REMOVE') "
                    "OR m.state IN ('MEMBER_STATE_LEFT','MEMBER_STATE_KICKED','MEMBER_STATE_REMOVED')) "
                    "ORDER BY m.event_at DESC,m.event_id DESC",
                    (*ids, rule["account"], rule["bot_id"]),
                ):
                    admissions[event["entry_id"]]["events"].append(event)
                for rejected in db.execute(
                    "SELECT q.event_at,r.id AS entry_id FROM cashback_source_quarantine q "
                    "JOIN entries r ON r.group_id=q.group_id AND r.member=q.member "
                    f"WHERE r.id IN ({placeholders}) AND q.account=? AND q.bot_id=? "
                    "AND q.reason='invalid_member_event' "
                    "AND (q.event_at=0 OR q.event_at>=r.evidence_started)",
                    (*ids, rule["account"], rule["bot_id"]),
                ):
                    admissions[rejected["entry_id"]]["quarantine"].append(rejected)
                for restart in db.execute(
                    "WITH latest AS (SELECT MAX(id) AS id FROM audit "
                    f"WHERE entry_id IN ({placeholders}) AND action IN "
                    "('qualification_restarted','migration_invitation_recovered','pay','exception','correct') "
                    "GROUP BY entry_id,action IN ('pay','exception','correct')) "
                    "SELECT a.entry_id,a.actor,a.action,a.after_json,a.created "
                    "FROM audit a JOIN latest ON latest.id=a.id",
                    ids,
                ):
                    if restart["action"] in {"pay", "exception", "correct"}:
                        settlements[restart["entry_id"]] = restart
                    else:
                        restarts[restart["entry_id"]] = restart
                confirmations = {
                    proof["id"]
                    for proof in db.execute(
                        "SELECT r.id FROM entries r JOIN current_invitation_evidence p "
                        "ON p.group_id=r.group_id AND p.member=r.member AND p.inviter=r.inviter "
                        "AND p.member_peer=r.peer AND p.inviter_peer=r.inviter_peer "
                        f"WHERE r.id IN ({placeholders}) AND p.source='cashier_confirmation'",
                        ids,
                    )
                }
            items = []
            for row in rows:
                item = row
                item.update(
                    self._entry_integrity(
                        db, rule, row, admission=admissions[row["id"]]
                    )
                )
                item["risk_blocked"] = row["inviter"] in frozen_inviters
                settlement = settlements.get(row["id"])
                settlement_after = (
                    json.loads(settlement["after_json"])
                    if row["state"] == "paid"
                    and settlement
                    and settlement["action"] == "exception"
                    else {}
                )
                # Match the recorded payment, not the later audit wall clock.
                # Payload principals are redacted; audit.actor is the exact identity.
                item["payment_kind"] = (
                    "exception"
                    if row["state"] == "paid"
                    and settlement_after.get("state") == "paid"
                    and settlement_after.get("paid_at") == row["paid_at"]
                    and settlement["actor"] == row["paid_by"]
                    and settlement_after.get("paid_by") == redact_reply(row["paid_by"])
                    else "pay"
                    if row["state"] == "paid"
                    else ""
                )
                item["can_assign"] = bool(
                    not row["inviter"]
                    and row["verification"] != "blocked"
                    and (
                        row["state"] == "paid"
                        and (person["role"] == "admin" or row["paid_by"] == actor)
                        or row["state"] in {"verifying", "pending"}
                        and row["present"]
                        and not row["ever_left"]
                    )
                )
                item["qualification_started"] = (
                    max(row["first_seen"], row["hold_until"] - 10800)
                    if row["hold_until"]
                    else 0
                )
                restart = restarts.get(row["id"])
                item["qualification_reason"] = (
                    json.loads(restart["after_json"]).get("reason", "observation_gap")
                    if restart and restart["action"] == "qualification_restarted"
                    else "migration_recovery"
                    if restart
                    else ""
                )
                item["attribution_source"] = (
                    "cashier_confirmation"
                    if row["id"] in confirmations
                    else "business_apply_log"
                    if row["inviter"]
                    else ""
                )
                items.append(item)
            result = {
                "items": items,
                "total": total[0],
                "amount": total[1],
                "payable_count": payable[0],
                "payable_amount": payable[1],
                "risk_blocked_count": sum(bool(item["risk_blocked"]) for item in items),
                "risk_blocked_amount": sum(
                    item["amount"]
                    for item in items
                    if item["risk_blocked"]
                    and item["state"] in {"pending", "verifying"}
                ),
                "page": page,
                "page_size": page_size,
                "page_count": page_count,
                "pagination": pagination,
                "section_total": total[2],
                "sections": sections,
                "group": rule,
            }
            if cache_version is not None and self._reuse_reads:
                # A future-dated proof can become visible without a write.
                # Expire before that boundary; unpaid qualification is never cached.
                future = db.execute(
                    "SELECT MIN(event_at) FROM evidence "
                    "WHERE group_id=? AND event_at>?",
                    (group, cache_time + 30),
                ).fetchone()[0]
                self._paid_pages[db] = (
                    cache_version,
                    cache_key,
                    cache_time,
                    min(cache_time + 30, future - 30)
                    if future is not None
                    else cache_time + 30,
                    {
                        **result,
                        "items": [dict(item) for item in items],
                        "sections": [dict(section) for section in sections],
                    },
                )
            return result

    def audit(self, actor, group, before=0, level="", trace=""):
        if isinstance(actor, FinanceActor) and not group:
            raise CashbackError("forbidden", "只能查看本群操作记录。", 403)
        with self.connect() as db:
            if group:
                self._scope(db, actor, group)
            else:
                self._actor(db, actor, True)
        return self.read_logs(group, before, level, trace)

    def read_logs(
        self, group="", before=0, level="", trace="", *, include_global=False
    ):
        """Read the shared module log after the caller's own authorization.

        Args:
            group: Optional exact group filter.
            before: Cursor for older rows.
            level: Optional severity filter.
            trace: Optional exact server correlation ID.
            include_global: Include unscoped events for an authorized administrator.
        """
        if (
            level not in {"", "info", "warning", "error"}
            or not 0 <= before <= 2**63 - 1
        ):
            raise CashbackError("invalid_query", "日志筛选无效。")
        if trace and not re.fullmatch(r"[a-f0-9]{32}", trace):
            raise CashbackError("invalid_query", "日志请求编号无效。")
        with self.connect() as db:
            result = []
            for row in db.execute(
                "SELECT a.*,u.name AS actor_name,u.uid AS actor_uid,"
                "(SELECT MIN(r.created) FROM audit r WHERE "
                "a.action='invitation_source_failed' "
                "AND r.action='invitation_source_recovered' AND r.group_id=a.group_id "
                "AND (r.bot_id=a.bot_id OR a.bot_id='') AND r.created>a.created) AS resolved_at "
                "FROM audit a "
                "LEFT JOIN users u ON u.email=a.actor WHERE (?='' OR group_id=? OR (?=1 AND group_id='')) "
                "AND (?=0 OR a.id<?) AND (?='' OR a.level=?) AND (?='' OR a.trace=?) ORDER BY a.id DESC LIMIT 100",
                (
                    group,
                    group,
                    int(include_global),
                    before,
                    before,
                    level,
                    level,
                    trace,
                    trace,
                ),
            ):
                item = dict(row)
                item["actor"] = item["actor_name"] or (
                    "系统"
                    if row["actor"] == "system"
                    else "后台管理员 "
                    + label(row["actor"].removeprefix("dashboard:"), 60)
                    if row["actor"].startswith("dashboard:")
                    else "未授权身份"
                    if row["actor"] in {"untrusted", "unbound"}
                    else "历史操作者"
                )
                result.append(item)
            return result

    def mutate(
        self,
        actor,
        group,
        operation,
        items,
        request_id,
        reason="",
        category=None,
        inviter=None,
        *,
        members=None,
        price_confirmation=None,
        actual_payment_confirmed=False,
    ):
        if operation not in {"pay", "correct", "exception", "reprice", "assign"}:
            raise CashbackError("invalid_operation", "操作无效。")
        if operation == "assign" and (
            not isinstance(inviter, str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", inviter)
            or len(items) != 1
        ):
            raise CashbackError("invalid_assignment", "请选择一条记录和已有邀请人。")
        if operation == "reprice" and category not in {"ordinary", "agent"}:
            raise CashbackError("invalid_price_class", "价格类别无效。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
            raise CashbackError("invalid_request_id", "请求标识无效，请刷新重试。")
        if not 1 <= len(items) <= 100 or len({i["id"] for i in items}) != len(items):
            raise CashbackError("invalid_selection", "每次请选择1至100条不重复记录。")
        reason = label(reason, 300)
        if operation != "pay" and len(reason) < 2:
            raise CashbackError("reason_required", "请填写具体的更正或异常付款原因。")
        digest = hashlib.sha256(
            json.dumps(
                [group, operation, items, reason, category]
                + ([inviter] if operation == "assign" else [])
                + ([price_confirmation] if operation == "reprice" else [])
                + ([actual_payment_confirmed] if operation == "exception" else []),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.connect(True) as db:
            rule, person = self._scope(db, actor, group, operation == "exception")
            prior = db.execute(
                "SELECT * FROM requests WHERE actor=? AND request_id=?",
                (actor, request_id),
            ).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise CashbackError(
                        "idempotency_conflict", "同一请求不能提交不同内容。", 409
                    )
                return json.loads(prior["result"])
            if operation == "exception" and actual_payment_confirmed is not True:
                raise CashbackError(
                    "actual_payment_confirmation_required",
                    "异常补记须明确确认已实际完成外部付款，请刷新页面并勾选确认。",
                    409,
                )
            if operation == "reprice":
                if not isinstance(price_confirmation, dict):
                    raise CashbackError(
                        "price_confirmation_required",
                        "价格确认已更新，请刷新页面后重新确认原价和目标价。",
                        409,
                    )
                expected = self._price_preview(db, actor, group, items, category)
                if price_confirmation != expected:
                    raise CashbackError(
                        "price_confirmation_changed",
                        "价格或规则已变化，请刷新后重新预览确认；本批次未修改。",
                        409,
                    )
            if operation in {"pay", "correct", "assign", "exception"} and (
                rule["error"] or time.time() - rule["scanned"] > 20
            ):
                raise CashbackError(
                    "roster_unavailable", "成员核验暂不可用，本次没有登记付款。", 409
                )
            if (
                operation in {"pay", "correct", "assign", "exception"}
                and rule["source_error"]
            ):
                raise CashbackError(
                    "invitation_source_unavailable",
                    "邀请来源暂不可用，本次没有登记付款，请等待来源恢复。",
                    409,
                )
            rows = []
            for item in items:
                row = db.execute(
                    "SELECT * FROM entries WHERE id=? AND group_id=? AND eligible=1",
                    (item["id"], group),
                ).fetchone()
                if not row or row["version"] != item["version"]:
                    raise CashbackError(
                        "conflict", "记录已变化，请刷新后重新选择；本批次未修改。", 409
                    )
                risk_inviter = inviter if operation == "assign" else row["inviter"]
                if operation in {"pay", "exception", "assign"} and risk_inviter:
                    frozen = db.execute(
                        "SELECT 1 FROM cashback_inviter_risk WHERE group_id=? "
                        "AND account=? AND inviter=? AND blocked_at IS NOT NULL",
                        (group, rule["account"], risk_inviter),
                    ).fetchone()
                    if frozen:
                        raise CashbackError(
                            "cashback_risk_blocked",
                            "该邀请人已触发返现风控，相关未结记录已冻结；请管理员复核后再处理。",
                            409,
                        )
                if row["self_join"]:
                    raise CashbackError(
                        "not_payable", "已确认主动入群，不参与邀请返现。", 409
                    )
                if operation in {"pay", "exception"}:
                    policy = self._entry_price_policy(db, rule, row)
                    if row["pricing_status"] == "no_reward" or (
                        policy
                        and policy["agent_only"]
                        and row["member_class"] != "agent"
                    ):
                        raise CashbackError(
                            "ordinary_reward_stopped",
                            "该普通成员不参与返现；须由结账员核验证明并切换代理商后才能结账。",
                            409,
                        )
                if operation == "pay" and rule["require_speech"]:
                    if not row["first_spoke_at"]:
                        raise CashbackError(
                            "speech_required",
                            "此群要求入群后发言，未核验到发言，不能登记结账。",
                            409,
                        )
                    if max(row["hold_until"], row["first_seen"] + 10800) > time.time():
                        raise CashbackError(
                            "qualification_required",
                            "入群尚未满三小时，不能登记结账。",
                            409,
                        )
                if operation == "assign":
                    if (
                        row["inviter"]
                        or row["verification"] == "blocked"
                        or row["state"] not in {"verifying", "pending", "paid"}
                        or row["state"] != "paid"
                        and (not row["present"] or row["ever_left"])
                    ):
                        raise CashbackError(
                            "assignment_forbidden",
                            "此记录已有归属、已作废或不符合资格。",
                            409,
                        )
                    if (
                        row["state"] == "paid"
                        and person["role"] != "admin"
                        and row["paid_by"] != actor
                    ):
                        raise CashbackError(
                            "assignment_forbidden",
                            "只能补充本人登记的已结账记录归属。",
                            403,
                        )
                    if (
                        not members
                        or inviter not in members
                        or not members[inviter]["eligible"]
                    ):
                        raise CashbackError(
                            "inviter_unavailable", "邀请人当前身份无法核验。", 409
                        )
                    identity = db.execute(
                        "SELECT peer FROM entries WHERE group_id=? AND member=? AND present=1",
                        (group, inviter),
                    ).fetchone()
                    if not identity or identity["peer"] != members[inviter]["peer"]:
                        raise CashbackError(
                            "identity_conflict", "邀请人身份已变化，请重新核验。", 409
                        )
                    if row["state"] == "paid" and (
                        not row["paid_at"] or not row["paid_by"]
                    ):
                        raise CashbackError(
                            "payment_conflict", "原付款登记不完整，不能人工绕过。", 409
                        )
                    if inviter == row["member"] or inviter == rule["account"]:
                        raise CashbackError(
                            "invalid_assignment", "不能选择本人或采集机器人。", 409
                        )
                    if row["member"] in members:
                        if (
                            not members[row["member"]]["eligible"]
                            or members[row["member"]]["peer"] != row["peer"]
                        ):
                            raise CashbackError(
                                "identity_conflict", "被邀请人身份无法核验。", 409
                            )
                    elif row["state"] != "paid":
                        raise CashbackError(
                            "assignment_forbidden", "成员已不在群，不能恢复返现。", 409
                        )
                    if db.execute(
                        "SELECT 1 FROM audit WHERE entry_id=? AND action IN "
                        "('identity_conflict','inviter_identity_conflict','invitation_evidence_conflict',"
                        "'self_join_attribution_conflict') LIMIT 1",
                        (row["id"],),
                    ).fetchone():
                        raise CashbackError(
                            "identity_conflict",
                            "此记录存在身份冲突，不能人工绕过。",
                            409,
                        )
                    if inviter not in {
                        p["uid"] for p in self.inviter_options(actor, group)
                    }:
                        raise CashbackError(
                            "inviter_unavailable",
                            "请选择本群已有且已确认的邀请人。",
                            409,
                        )
                    options = db.execute(
                        "SELECT inviter,inviter_peer FROM current_invitation_evidence "
                        "WHERE group_id=? AND member=? AND source='business_apply_log' "
                        "AND member_peer=? AND event_at>0 "
                        "AND (?!='paid' OR event_at<=?)",
                        (
                            group,
                            row["member"],
                            row["peer"],
                            row["state"],
                            row["paid_at"],
                        ),
                    ).fetchall()
                    if any(
                        p["inviter"] != inviter
                        or p["inviter_peer"] != members[inviter]["peer"]
                        for p in options
                    ):
                        raise CashbackError(
                            "attribution_conflict",
                            "已有业务证据指向其他邀请人，不能覆盖。",
                            409,
                        )
                    # A source candidate may be decoded before its inviter is
                    # mapped into the roster. Do not overwrite that evidence.
                    for cached in db.execute(
                        "SELECT payload FROM invitation_source_records WHERE group_id=? "
                        "AND bot_id=? AND account=? AND started=?",
                        (group, rule["bot_id"], rule["account"], rule["started"]),
                    ):
                        proof = json.loads(cached["payload"])
                        if proof["member_id"] != row["member"]:
                            continue
                        try:
                            text = proof["apply_time"]
                            if text.replace(".", "", 1).isdigit():
                                stamp = float(text)
                                if stamp > 100000000000:
                                    stamp /= 1000
                            else:
                                parsed = datetime.fromisoformat(
                                    text.replace("Z", "+00:00")
                                )
                                stamp = (
                                    parsed
                                    if parsed.tzinfo
                                    else parsed.replace(tzinfo=BEIJING)
                                ).timestamp()
                            if (
                                not math.isfinite(stamp)
                                or stamp <= 0
                                or stamp > time.time() + 30
                            ):
                                raise ValueError("invalid_source_timestamp")
                        except (TypeError, ValueError, KeyError, AttributeError) as exc:
                            raise CashbackError(
                                "invitation_cache",
                                "邀请来源时间无法核验，未修改归属。",
                                409,
                            ) from exc
                        if stamp < rule["started"] or (
                            row["state"] == "paid" and stamp > row["paid_at"]
                        ):
                            continue
                        if (
                            proof["member_nim_id"] != row["peer"]
                            or proof["inviter_id"] != inviter
                            or proof["inviter_nim_id"] != members[inviter]["peer"]
                        ):
                            raise CashbackError(
                                "attribution_conflict",
                                "已解码的邀请来源存在其他归属，不能人工覆盖。",
                                409,
                            )
                    policy = self._entry_price_policy(db, rule, row)
                    if row["state"] != "paid" and not policy:
                        raise CashbackError(
                            "historical_price_unavailable", "原价格规则无法核验。", 409
                        )
                    rows.append({**dict(row), "_policy": policy})
                    continue
                if operation == "reprice":
                    if (
                        row["state"] not in {"verifying", "pending"}
                        or row["verification"] != "verified"
                        or not row["inviter"]
                        or not row["present"]
                        or row["ever_left"]
                        or row["member_class"] not in {"ordinary", "agent"}
                    ):
                        raise CashbackError(
                            "price_change_forbidden",
                            "只有已核验、仍在群且未结账的邀请记录可以切换价格。",
                            409,
                        )
                    policy = self._entry_price_policy(db, rule, row)
                    if not policy:
                        raise CashbackError(
                            "historical_price_unavailable",
                            "该记录对应的历史价格规则无法核验，暂不能切换。",
                            409,
                        )
                    amount = (
                        rule["agent_rate"]
                        if category == "agent"
                        else 0
                        if policy["agent_only"]
                        else policy["rate"]
                    )
                    if amount is None:
                        raise CashbackError(
                            "agent_rate_unconfigured",
                            "尚未设置代理商单价，不能切换为代理商价格。",
                            409,
                        )
                    if row["member_class"] == category and row["amount"] == amount:
                        raise CashbackError(
                            "price_class_unchanged", "该记录已经是当前价格类别。", 409
                        )
                    rows.append({**dict(row), "_policy": policy, "_amount": amount})
                    continue
                integrity = self._entry_integrity(db, rule, row)
                if operation != "correct":
                    if row["state"] == "paid" or not row["inviter"]:
                        raise CashbackError(
                            "not_payable", "记录已结账或尚未确认邀请人。", 409
                        )
                    if integrity["review_reason"]:
                        raise CashbackError(
                            "not_payable",
                            "原邀请周期存在来源或归属冲突，不能登记新付款，请核对账本。",
                            409,
                        )
                    recipient = db.execute(
                        "SELECT * FROM entries WHERE group_id=? AND member=?",
                        (group, row["inviter"]),
                    ).fetchone()
                    if (
                        row["verification"] != "verified"
                        or not recipient
                        or not recipient["present"]
                        or recipient["peer"] != row["inviter_peer"]
                        or row["inviter"] == row["member"]
                        or row["inviter"] == rule["account"]
                    ):
                        raise CashbackError(
                            "identity_conflict",
                            "收款邀请人身份或原邀请归属无法确认，不能登记新付款。",
                            409,
                        )
                    if (
                        row["pricing_status"] != "priced"
                        or row["member_class"] not in {"ordinary", "agent"}
                        or not policy
                        or row["amount"] <= 0
                    ):
                        raise CashbackError(
                            "historical_price_unavailable",
                            "原价格尚未确认，不能登记新付款。",
                            409,
                        )
                if (
                    operation == "pay"
                    and row["member_class"] != "legacy"
                    and (
                        row["verification"] != "conflict"
                        and (
                            row["state"] != "pending"
                            or row["pricing_status"] != "priced"
                            or row["hold_until"] > time.time()
                            or not row["present"]
                            or row["ever_left"]
                            or integrity["membership_left_at"]
                            or rule["error"]
                            or time.time() - rule["scanned"] > 20
                        )
                    )
                ):
                    raise CashbackError(
                        "qualification_required",
                        "尚未通过三小时在群核验或代理商金额待设置，不能登记结账。",
                        409,
                    )
                if operation == "correct":
                    if row["state"] != "paid" or (
                        person["role"] != "admin" and row["paid_by"] != actor
                    ):
                        raise CashbackError(
                            "correction_forbidden",
                            "只能更正本人登记的已结账记录。",
                            403,
                        )
                elif row["state"] == "paid" or not row["inviter"]:
                    raise CashbackError(
                        "not_payable", "记录已结账或尚未确认邀请人。", 409
                    )
                elif operation == "pay" and (
                    row["state"] != "pending"
                    or row["verification"] != "verified"
                    or not row["present"]
                    or row["ever_left"]
                ):
                    raise CashbackError(
                        "not_payable",
                        "存在退群、无效或归属待核对记录；本批次未修改。",
                        409,
                    )
                rows.append({**dict(row), "_integrity": integrity})
            if len({r["inviter"] for r in rows}) != 1:
                raise CashbackError(
                    "mixed_recipients", "一次只能为同一邀请人登记结账。"
                )
            now = time.time()
            if operation == "assign":
                row = rows[0]
                target = members[inviter]
                amount = (
                    row["amount"]
                    if row["state"] == "paid"
                    or row["pricing_status"] in {"priced", "no_reward"}
                    else 0
                    if row["_policy"]["agent_only"]
                    else row["_policy"]["rate"]
                )
                pricing = (
                    row["pricing_status"]
                    if row["state"] == "paid"
                    or row["pricing_status"] in {"priced", "no_reward"}
                    else "no_reward"
                    if row["_policy"]["agent_only"]
                    else "priced"
                )
                hold_until = row["hold_until"] or (
                    now + 10800 if row["state"] != "paid" else 0
                )
                if row["state"] != "paid" and row["member_class"] == "legacy":
                    # Legacy unknown entries have no continuous qualification proof.
                    hold_until = max(hold_until, now + 10800)
                    self._audit(
                        db,
                        group,
                        row["id"],
                        actor,
                        "qualification_restarted",
                        {"hold_until": row["hold_until"]},
                        {"hold_until": hold_until, "reason": "manual_confirmation"},
                        reason,
                    )
                state = (
                    "paid"
                    if row["state"] == "paid"
                    else "pending"
                    if hold_until <= now
                    and pricing == "priced"
                    and (not rule["require_speech"] or row["first_spoke_at"])
                    else "verifying"
                )
                # This is an audited cashier confirmation, not a decoded platform invitation.
                db.execute(
                    "INSERT INTO evidence(group_id,member,source,event_id,inviter,observed,"
                    "account,member_peer,inviter_peer,event_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        group,
                        row["member"],
                        "cashier_confirmation",
                        request_id,
                        inviter,
                        now,
                        rule["account"],
                        row["peer"],
                        target["peer"],
                        row["first_seen"],
                    ),
                )
                db.execute(
                    "UPDATE entries SET inviter=?,inviter_name=?,inviter_peer=?,inviter_name_snapshot=?,"
                    "verification='verified',state=?,amount=?,hold_until=?,pricing_status=?,"
                    "member_class=?,rule_version=?,version=version+1 WHERE id=?",
                    (
                        inviter,
                        label(target["name"]),
                        target["peer"],
                        label(target["name"]),
                        state,
                        amount,
                        hold_until,
                        pricing,
                        row["member_class"]
                        if row["state"] == "paid"
                        or row["member_class"] in {"ordinary", "agent"}
                        else "ordinary",
                        row["rule_version"]
                        if row["state"] == "paid" or row["rule_version"]
                        else row["_policy"]["version"],
                        row["id"],
                    ),
                )
                self._audit(
                    db,
                    group,
                    row["id"],
                    actor,
                    "inviter_assigned",
                    {
                        key: value
                        for key, value in row.items()
                        if not key.startswith("_")
                    },
                    {
                        "inviter": inviter,
                        "inviter_name": label(target["name"]),
                        "state": state,
                        "amount": amount,
                        "source": "cashier_confirmation",
                        "confirmed_at": now,
                        "hold_until": hold_until,
                        "payment_unchanged": row["state"] == "paid",
                    },
                    reason,
                )
                result = {
                    "status": "assigned",
                    "count": 1,
                    "amount": amount,
                    "state": "no_reward"
                    if pricing == "no_reward" and state != "paid"
                    else state,
                }
                db.execute(
                    "INSERT INTO requests VALUES(?,?,?,?,?)",
                    (actor, request_id, digest, json.dumps(result), now),
                )
                return result
            if operation == "reprice":
                for row in rows:
                    pricing = (
                        "no_reward"
                        if category == "ordinary" and row["_policy"]["agent_only"]
                        else "priced"
                    )
                    db.execute(
                        "UPDATE entries SET amount=?,member_class=?,"
                        "pricing_status=?,member_avatar_status='manual',"
                        "member_avatar_digit_count=0,version=version+1 WHERE id=?",
                        (row["_amount"], category, pricing, row["id"]),
                    )
                    self._audit(
                        db,
                        group,
                        row["id"],
                        actor,
                        "price_class_changed",
                        {
                            "amount": row["amount"],
                            "member_class": row["member_class"],
                            "pricing_status": row["pricing_status"],
                            "version": row["version"],
                        },
                        {
                            "amount": row["_amount"],
                            "member_class": category,
                            "pricing_status": pricing,
                            "version": row["version"] + 1,
                            "rule_version": (
                                rule["version"]
                                if category == "agent"
                                else row["_policy"]["version"]
                            ),
                            "price_confirmation": {
                                "digest": price_confirmation["digest"],
                                "rule_version": price_confirmation["rule_version"],
                                **next(
                                    p
                                    for p in price_confirmation["items"]
                                    if p["id"] == row["id"]
                                ),
                            },
                        },
                        reason,
                    )
                result = {
                    "status": "repriced",
                    "count": len(rows),
                    "amount": sum(r["_amount"] for r in rows),
                }
                db.execute(
                    "INSERT INTO requests VALUES(?,?,?,?,?)",
                    (actor, request_id, digest, json.dumps(result), now),
                )
                return result
            for row in rows:
                state = (
                    (
                        "void"
                        if row["ever_left"]
                        or not row["present"]
                        or row["_integrity"]["membership_left_at"]
                        or row["_integrity"]["review_reason"]
                        or row["verification"] != "verified"
                        else "verifying"
                        if row["hold_until"] > now
                        or row["pricing_status"] != "priced"
                        or rule["require_speech"]
                        and not row["first_spoke_at"]
                        else "pending"
                    )
                    if operation == "correct"
                    else "paid"
                )
                after = {
                    "state": state,
                    "paid_by": actor if state == "paid" else None,
                    "paid_at": now if state == "paid" else None,
                }
                if operation == "exception":
                    after.update(
                        {
                            "actual_payment_confirmed": True,
                            "exception_reason": reason,
                            "qualification_evidence": {
                                key: row[key]
                                for key in (
                                    "state",
                                    "verification",
                                    "present",
                                    "ever_left",
                                    "left_at",
                                    "first_seen",
                                    "hold_until",
                                    "first_spoke_at",
                                    "member_class",
                                    "pricing_status",
                                    "amount",
                                    "rule_version",
                                    "inviter",
                                    "inviter_peer",
                                    "peer",
                                )
                            },
                            "invitation_evidence": row["_integrity"],
                            "source_evidence": [
                                dict(proof)
                                for proof in db.execute(
                                    "SELECT source,event_id,event_at,account,member_peer,"
                                    "inviter,inviter_peer FROM current_invitation_evidence "
                                    "WHERE group_id=? AND member=? AND member_peer=? "
                                    "AND collector_account=? AND event_at>=? AND event_at>0 "
                                    "AND source IN ('business_apply_log','cashier_confirmation') "
                                    "ORDER BY event_at,event_id",
                                    (
                                        group,
                                        row["member"],
                                        row["peer"],
                                        rule["account"],
                                        row["evidence_started"],
                                    ),
                                )
                            ],
                            "qualification_rule": {
                                "version": rule["version"],
                                "require_speech": bool(rule["require_speech"]),
                                "qualification_seconds": rule["qualification_seconds"],
                            },
                        }
                    )
                db.execute(
                    "UPDATE entries SET state=?,paid_by=?,paid_at=?,version=version+1 WHERE id=?",
                    (state, after["paid_by"], after["paid_at"], row["id"]),
                )
                self._audit(
                    db,
                    group,
                    row["id"],
                    actor,
                    operation,
                    {
                        k: row[k]
                        for k in ("state", "paid_by", "paid_at", "amount", "version")
                    },
                    after,
                    reason,
                )
            result = {
                "status": "exception_registered"
                if operation == "exception"
                else "registered",
                "count": len(rows),
                "amount": sum(r["amount"] for r in rows),
            }
            if operation == "exception":
                result["actual_payment_confirmed"] = True
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?)",
                (actor, request_id, digest, json.dumps(result), now),
            )
            return result
