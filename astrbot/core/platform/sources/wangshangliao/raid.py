"""Durable, group-scoped newcomer protection without identity inference."""

import hashlib
import json
import math
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from .storage import instance_dir
from .wire import ProtocolError

OBSERVATION_SECONDS = 600
PROTECTION_SECONDS = 900


@dataclass(frozen=True)
class RaidIntent:
    """Bind a short mute to one stored, corroborated membership round."""

    operation: str
    account: str
    group: str
    member: str
    peer: str
    generation: str
    join_id: str
    kind: str
    trigger: str
    minutes: int
    deadline: float


class RaidStore:
    """Keep bounded working state in the existing private moderation database."""

    def __init__(self, instance, *, path=None):
        self.path = path or instance_dir(instance) / "moderation.sqlite3"

    @contextmanager
    def connect(self, *, guard=None):
        """Own a transaction and recheck authority before committing.

        Args:
            guard: Optional synchronous lifecycle and policy fence.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS raid_groups(
                    account TEXT,group_id TEXT,generation TEXT,enabled INTEGER,
                    enabled_at REAL,protection_id TEXT DEFAULT '',
                    protection_started REAL DEFAULT 0,protection_until REAL DEFAULT 0,
                    PRIMARY KEY(account,group_id));
                CREATE TABLE IF NOT EXISTS raid_members(
                    account TEXT,group_id TEXT,member TEXT,peer TEXT,join_id TEXT,
                    joined_at REAL,observed_until REAL,mute_until REAL DEFAULT 0,
                    violations INTEGER DEFAULT 0,updated REAL,
                    PRIMARY KEY(account,group_id,member));
                CREATE TABLE IF NOT EXISTS raid_joins(
                    account TEXT,group_id TEXT,join_id TEXT,member TEXT,event_at REAL,
                    PRIMARY KEY(account,group_id,join_id));
                CREATE TABLE IF NOT EXISTS raid_bursts(
                    account TEXT,group_id TEXT,member TEXT,message TEXT,event_at REAL,
                    PRIMARY KEY(account,group_id,message));
                CREATE INDEX IF NOT EXISTS raid_bursts_recent
                    ON raid_bursts(account,group_id,event_at);
                CREATE TABLE IF NOT EXISTS raid_intents(
                    operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,member TEXT,
                    peer TEXT,generation TEXT,join_id TEXT,kind TEXT,trigger TEXT,
                    minutes INTEGER,deadline REAL,status TEXT,finished INTEGER DEFAULT 0,
                    created REAL,completed REAL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS raid_intents_member
                    ON raid_intents(account,group_id,member,status);
                CREATE TABLE IF NOT EXISTS raid_recalls(
                    account TEXT,group_id TEXT,message TEXT,member TEXT,
                    status TEXT,created REAL,PRIMARY KEY(account,group_id,message));
                CREATE TABLE IF NOT EXISTS raid_audit(
                    id INTEGER PRIMARY KEY,account TEXT,group_id TEXT,member TEXT,
                    kind TEXT,created REAL,details TEXT);
                CREATE TABLE IF NOT EXISTS raid_releases(
                    operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,member TEXT,
                    created REAL);
            """)
            db.execute("BEGIN IMMEDIATE")
            if guard:
                guard()
            yield db
            if guard:
                guard()
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def configure(self, account, group, enabled, *, guard=None, now=None):
        """Create a new authorization generation only when the switch changes.

        Args:
            account: Authenticated collector UID.
            group: Exact enabled group.
            enabled: Explicit saved switch.
            guard: Synchronous policy fence.
            now: Optional test clock in seconds.

        Returns:
            Persisted generation and working member state.
        """
        now = time.time() if now is None else now
        with self.connect(guard=guard) as db:
            old = db.execute(
                "SELECT * FROM raid_groups WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if old is None or bool(old["enabled"]) != enabled:
                db.execute(
                    "INSERT OR REPLACE INTO raid_groups"
                    "(account,group_id,generation,enabled,enabled_at) VALUES(?,?,?,?,?)",
                    (account, group, secrets.token_hex(16), int(enabled), now),
                )
        return self.snapshot(account, group, now=now)

    def snapshot(self, account, group, *, now=None):
        """Return only metadata and bounded receipts, never message contents.

        Args:
            account: Collector UID.
            group: Exact group.
            now: Optional wall clock.

        Returns:
            Group state, live observations and recent action outcomes.
        """
        now = time.time() if now is None else now
        with self.connect() as db:
            state = db.execute(
                "SELECT * FROM raid_groups WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            members = db.execute(
                "SELECT * FROM raid_members WHERE account=? AND group_id=? "
                "AND (observed_until>? OR mute_until>?) ORDER BY updated DESC LIMIT 4096",
                (account, group, now, now),
            ).fetchall()
            outcomes = db.execute(
                "SELECT member,kind,minutes,status,created FROM raid_intents "
                "WHERE account=? AND group_id=? ORDER BY created DESC LIMIT 20",
                (account, group),
            ).fetchall()
            recalls = db.execute(
                "SELECT member,message,status,created FROM raid_recalls "
                "WHERE account=? AND group_id=? ORDER BY created DESC LIMIT 20",
                (account, group),
            ).fetchall()
            return {
                "state": dict(state) if state else {},
                "members": [dict(row) for row in members],
                "outcomes": [dict(row) for row in outcomes],
                "recalls": [dict(row) for row in recalls],
                "checked_at": now,
            }

    def join(self, account, group, member, peer, join_id, event_at, *, guard, now=None):
        """Remember an already corroborated real admission, preserving mute debt.

        Args:
            account: Collector UID.
            group: Exact group.
            member: Verified ordinary member UID.
            peer: Verified transport identity.
            join_id: Canonical provider application event ID.
            event_at: Actual provider admission time.
            guard: Fresh membership and policy authority.
            now: Optional clock.

        Returns:
            Current observation or None for historical or duplicate evidence.
        """
        now = time.time() if now is None else now
        with self.connect(guard=guard) as db:
            state = db.execute(
                "SELECT * FROM raid_groups WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if (
                not state
                or not state["enabled"]
                or not max(state["enabled_at"], now - 120) <= event_at <= now
            ):
                return None
            if not db.execute(
                "INSERT OR IGNORE INTO raid_joins VALUES(?,?,?,?,?)",
                (account, group, join_id, member, event_at),
            ).rowcount:
                return None
            old = db.execute(
                "SELECT * FROM raid_members WHERE account=? AND group_id=? AND member=?",
                (account, group, member),
            ).fetchone()
            if old and old["joined_at"] >= event_at:
                return None
            same = old is not None and old["peer"] == peer
            db.execute(
                "INSERT OR REPLACE INTO raid_members VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    group,
                    member,
                    peer,
                    join_id,
                    event_at,
                    max(
                        event_at + OBSERVATION_SECONDS,
                        old["observed_until"] if same else 0,
                    ),
                    old["mute_until"] if same else 0,
                    old["violations"] if same else 0,
                    now,
                ),
            )
            return dict(
                db.execute(
                    "SELECT * FROM raid_members WHERE account=? AND group_id=? AND member=?",
                    (account, group, member),
                ).fetchone()
            )

    def reserve(self, account, group, member, *, kind, trigger, guard, now=None):
        """Reserve one short mute, writing unknown before any network submission.

        Args:
            account: Collector UID.
            group: Exact group.
            member: Corroborated ordinary member.
            kind: Burst, initial protection hold, or remaining-mute restoration.
            trigger: Authentic triggering message or canonical admission ID.
            guard: Current local authority.
            now: Optional clock.

        Returns:
            Immutable intent, or None when duplicate, covered or unresolved.
        """
        now = time.time() if now is None else now
        if kind not in {"burst", "hold", "restore"}:
            raise ProtocolError("raid_intent_invalid")
        with self.connect(guard=guard) as db:
            state = db.execute(
                "SELECT * FROM raid_groups WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            row = db.execute(
                "SELECT * FROM raid_members WHERE account=? AND group_id=? AND member=?",
                (account, group, member),
            ).fetchone()
            if (
                not state
                or not state["enabled"]
                or not row
                or row["observed_until"] <= now
                or row["joined_at"] < state["enabled_at"]
            ):
                return None
            if kind == "burst":
                recent = db.execute(
                    "SELECT 1 FROM raid_bursts WHERE account=? AND group_id=? "
                    "AND member=? AND event_at>? LIMIT 1",
                    (account, group, member, max(now - 10, state["enabled_at"])),
                ).fetchone()
                if not recent:
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO raid_bursts VALUES(?,?,?,?,?)",
                        (account, group, member, trigger, now),
                    ).rowcount
                    if not inserted:
                        return None
                    db.execute(
                        "UPDATE raid_members SET violations=violations+1,updated=? "
                        "WHERE account=? AND group_id=? AND member=?",
                        (now, account, group, member),
                    )
                    count = db.execute(
                        "SELECT COUNT(DISTINCT member) FROM raid_bursts "
                        "WHERE account=? AND group_id=? AND event_at>=?",
                        (
                            account,
                            group,
                            max(
                                now - 60,
                                state["enabled_at"],
                                state["protection_started"],
                            ),
                        ),
                    ).fetchone()[0]
                    if count >= 2 and state["protection_until"] <= now:
                        db.execute(
                            "UPDATE raid_groups SET protection_id=?,protection_started=?,"
                            "protection_until=? WHERE account=? AND group_id=?",
                            (
                                secrets.token_hex(16),
                                now,
                                now + PROTECTION_SECONDS,
                                account,
                                group,
                            ),
                        )
                if row["mute_until"] > now:
                    return None
            elif kind == "hold":
                if (
                    state["protection_until"] <= now
                    or row["joined_at"] < state["protection_started"]
                    or row["mute_until"] > now
                ):
                    return None
            elif row["mute_until"] <= now:
                return None
            if db.execute(
                "SELECT 1 FROM raid_intents WHERE account=? AND group_id=? AND member=? "
                "AND status='unknown' LIMIT 1",
                (account, group, member),
            ).fetchone():
                return None
            deadline = (
                row["mute_until"]
                if kind == "restore"
                else now + (600 if kind == "burst" else 60)
            )
            minutes = math.ceil((deadline - now) / 60)
            operation = (
                "raid/"
                + hashlib.sha256(
                    json.dumps(
                        [
                            account,
                            group,
                            member,
                            state["generation"],
                            row["join_id"],
                            kind,
                            trigger,
                        ]
                    ).encode()
                ).hexdigest()
            )
            intent = RaidIntent(
                operation,
                account,
                group,
                member,
                row["peer"],
                state["generation"],
                row["join_id"],
                kind,
                trigger,
                minutes,
                deadline,
            )
            if not db.execute(
                "INSERT OR IGNORE INTO raid_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,'unknown',0,?,0)",
                (*asdict(intent).values(), now),
            ).rowcount:
                return None
            return intent

    def authorize(self, intent, *, now=None):
        """Reject invented, stale, completed or expired internal capabilities.

        Args:
            intent: Exact immutable capability from reserve.
            now: Optional wall clock.
        """
        now = time.time() if now is None else now
        if (
            type(intent) is not RaidIntent
            or type(intent.minutes) is not int
            or not 1 <= intent.minutes <= 10
        ):
            raise ProtocolError("raid_intent_invalid")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM raid_intents WHERE operation=?", (intent.operation,)
            ).fetchone()
            state = db.execute(
                "SELECT * FROM raid_groups WHERE account=? AND group_id=?",
                (intent.account, intent.group),
            ).fetchone()
            member = db.execute(
                "SELECT * FROM raid_members WHERE account=? AND group_id=? AND member=?",
                (intent.account, intent.group, intent.member),
            ).fetchone()
            if (
                not row
                or row["finished"]
                or row["status"] != "unknown"
                or not state
                or not state["enabled"]
                or state["generation"] != intent.generation
                or not member
                or member["join_id"] != intent.join_id
                or member["peer"] != intent.peer
                or member["observed_until"] <= now
                or any(
                    row["group_id" if key == "group" else key] != value
                    for key, value in asdict(intent).items()
                )
                or intent.deadline <= now
                or intent.kind == "restore"
                and intent.minutes != math.ceil((intent.deadline - now) / 60)
            ):
                raise ProtocolError("raid_intent_revoked")

    def finish(self, intent, status, *, now=None):
        """Keep unknown outcomes quarantined and preserve the original mute deadline.

        Args:
            intent: Original immutable intent.
            status: Accepted, verified, rejected, skipped, or unknown.
            now: Optional receipt time.
        """
        now = time.time() if now is None else now
        status = (
            status
            if status in {"accepted", "verified", "rejected", "skipped"}
            else "unknown"
        )
        with self.connect() as db:
            changed = db.execute(
                "UPDATE raid_intents SET status=?,finished=1,completed=? "
                "WHERE operation=? AND finished=0",
                (status, now, intent.operation),
            ).rowcount
            if not changed:
                return
            if status in {"accepted", "verified"} and intent.kind != "restore":
                db.execute(
                    "UPDATE raid_members SET mute_until=MAX(mute_until,?),updated=? "
                    "WHERE account=? AND group_id=? AND member=? AND peer=?",
                    (
                        now + intent.minutes * 60,
                        now,
                        intent.account,
                        intent.group,
                        intent.member,
                        intent.peer,
                    ),
                )
            db.execute(
                "INSERT INTO raid_audit(account,group_id,member,kind,created,details) VALUES(?,?,?,?,?,?)",
                (
                    intent.account,
                    intent.group,
                    intent.member,
                    "mute_receipt",
                    now,
                    json.dumps(
                        {
                            "operation": intent.operation,
                            "status": status,
                            "minutes": intent.minutes,
                        }
                    ),
                ),
            )

    def reserve_recalls(self, account, group, member, messages, *, guard):
        """Reserve only new batch members in one transaction without retrying.

        Args:
            account: Original receiving account.
            group: Exact corroborated group.
            member: Verified message author.
            messages: Verified native message identities in arrival order.
            guard: Current lifecycle and policy fence.

        Returns:
            Newly reserved messages, excluding accepted or unresolved receipts.
        """
        pending = []
        with self.connect(guard=guard) as db:
            for message in messages:
                if db.execute(
                    "INSERT OR IGNORE INTO raid_recalls VALUES(?,?,?,?,'unknown',?)",
                    (account, group, message, member, time.time()),
                ).rowcount:
                    pending.append(message)
        return pending

    def recall(self, account, group, member, message, *, status=None, guard=None):
        """Reserve or finish one independent recall without retrying unknowns.

        Args:
            account: Collector UID.
            group: Exact group.
            member: Proven message author.
            message: Authentic original message ID.
            status: None reserves; otherwise records the receipt.
            guard: Local authority.

        Returns:
            Whether the reservation was newly inserted.
        """
        with self.connect(guard=guard) as db:
            if status is None:
                return bool(
                    db.execute(
                        "INSERT OR IGNORE INTO raid_recalls VALUES(?,?,?,?,'unknown',?)",
                        (account, group, message, member, time.time()),
                    ).rowcount
                )
            db.execute(
                "UPDATE raid_recalls SET status=? WHERE account=? AND group_id=? "
                "AND member=? AND message=?",
                (
                    status
                    if status in {"accepted", "verified", "rejected"}
                    else "unknown",
                    account,
                    group,
                    member,
                    message,
                ),
            )
            return False

    def release(self, account, group, member, *, operation="", guard=None):
        """Cancel this mechanism's remaining debt after an explicit successful unmute.

        Args:
            account: Original collector.
            group: Exact group.
            member: Explicitly released UID.
            operation: Successful native unmute identity, deduplicated durably.
            guard: Optional lifecycle fence.
        """
        with self.connect(guard=guard) as db:
            if (
                operation
                and not db.execute(
                    "INSERT OR IGNORE INTO raid_releases VALUES(?,?,?,?,?)",
                    (operation, account, group, member, time.time()),
                ).rowcount
            ):
                return
            db.execute(
                "UPDATE raid_members SET mute_until=0,updated=? "
                "WHERE account=? AND group_id=? AND member=?",
                (time.time(), account, group, member),
            )
            db.execute(
                "UPDATE raid_intents SET status='revoked',finished=1,completed=? "
                "WHERE account=? AND group_id=? AND member=? AND finished=0",
                (time.time(), account, group, member),
            )

    def end(self, account, group, protection_id, *, guard):
        """End exactly the previewed protection period without unmuting anyone.

        Args:
            account: Collector UID.
            group: Exact group.
            protection_id: Original previewed period identity.
            guard: Authorized caller and group fence.
        """
        with self.connect(guard=guard) as db:
            if (
                not protection_id
                or not db.execute(
                    "UPDATE raid_groups SET protection_until=0,protection_started=? WHERE account=? AND group_id=? "
                    "AND protection_id=? AND protection_until>?",
                    (time.time(), account, group, protection_id, time.time()),
                ).rowcount
            ):
                raise ProtocolError("raid_protection_changed")

    def prune(self, *, now=None):
        """Remove only expired working state without unresolved action evidence.

        Args:
            now: Optional maintenance clock.
        """
        cutoff = (time.time() if now is None else now) - 86400
        with self.connect() as db:
            db.execute(
                "DELETE FROM raid_members WHERE MAX(observed_until,mute_until,updated)<? "
                "AND NOT EXISTS(SELECT 1 FROM raid_intents i WHERE i.account=raid_members.account "
                "AND i.group_id=raid_members.group_id AND i.member=raid_members.member "
                "AND i.status='unknown') "
                "AND NOT EXISTS(SELECT 1 FROM raid_recalls r WHERE r.account=raid_members.account "
                "AND r.group_id=raid_members.group_id AND r.member=raid_members.member "
                "AND r.status='unknown')",
                (cutoff,),
            )
            for table in ("raid_joins", "raid_bursts"):
                db.execute(
                    f"DELETE FROM {table} WHERE event_at<? "
                    f"AND NOT EXISTS(SELECT 1 FROM raid_members m WHERE "
                    f"m.account={table}.account AND m.group_id={table}.group_id "
                    f"AND m.member={table}.member) "
                    f"AND NOT EXISTS(SELECT 1 FROM raid_intents i WHERE i.account={table}.account "
                    f"AND i.group_id={table}.group_id AND i.member={table}.member AND i.status='unknown')",
                    (cutoff,),
                )
            db.execute(
                "DELETE FROM raid_intents WHERE completed<? AND finished=1 "
                "AND status!='unknown' AND NOT EXISTS(SELECT 1 FROM raid_members m "
                "WHERE m.account=raid_intents.account AND m.group_id=raid_intents.group_id "
                "AND m.member=raid_intents.member)",
                (cutoff,),
            )
            db.execute(
                "DELETE FROM raid_recalls WHERE created<? AND status!='unknown' "
                "AND NOT EXISTS(SELECT 1 FROM raid_members m WHERE m.account=raid_recalls.account "
                "AND m.group_id=raid_recalls.group_id AND m.member=raid_recalls.member)",
                (cutoff,),
            )
            db.execute("DELETE FROM raid_audit WHERE created<?", (cutoff - 89 * 86400,))


def authorize_raid(
    instance, intent, account, operation, action, group, member, minutes
):
    """Validate the native transport binding independently of plugin callbacks.

    Args:
        instance: Saved transport instance.
        intent: Exact stored capability.
        account: Original authenticated account.
        operation: Native action operation ID.
        action: Only mute is authorized.
        group: Exact business group.
        member: Exact ordinary member.
        minutes: Fixed short duration.
    """
    if type(intent) is not RaidIntent or (
        intent.account,
        intent.operation,
        action,
        intent.group,
        intent.member,
        intent.minutes,
    ) != (account, operation, "mute", str(group), str(member), minutes):
        raise ProtocolError("raid_intent_invalid")
    RaidStore(instance).authorize(intent)
