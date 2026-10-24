"""Durable, narrowly scoped authority for native member blacklist actions."""

import inspect
import json
import math
import secrets
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass

from .identity import ActorIdentity
from .storage import instance_dir
from .wire import ProtocolError

BLACKLIST_MUTE_MINUTES = 41760
# Membership writers acquire this in workers before either store's SQLite locks.
MEMBER_LIST_WRITE_LOCK = threading.RLock()


def protected_target(adapter, group, member, peer, *, fixture=None, intent=None):
    """Keep managed robots protected outside the exact live-test capability.

    Args:
        adapter: Original authenticated receiver or its genuine scoped view.
        group: Exact business group.
        member: Target business UID.
        peer: Proven native peer.
        fixture: Optional original isolated-test capability, never a boolean.
        intent: Original durable blacklist action when one has been reserved.

    Returns:
        Whether managed-target protection prevents this blacklist operation.
    """
    from .event import is_managed_account

    if str(member) == str(adapter.account):
        return True
    if not is_managed_account(str(member)):
        return False
    from .test_blacklist import protected_target_allowed

    return not protected_target_allowed(
        adapter,
        str(group),
        str(member),
        str(peer),
        fixture=fixture,
        blacklist_intent=intent,
    )


def _identity(value):
    """Normalize a positive canonical business identity.

    Args:
        value: Integer or canonical decimal string.

    Returns:
        Canonical decimal identity.

    Raises:
        ProtocolError: If the identity is not canonical.
    """
    text = str(value)
    if (
        type(value) not in {int, str}
        or len(text) > 19
        or not text.isascii()
        or not text.isdigit()
        or not 0 < int(text) < 1 << 63
        or str(int(text)) != text
    ):
        raise ProtocolError("blacklist_identity")
    return text


def _guard(check):
    """Run a synchronous authority guard on the transaction's owning thread.

    Args:
        check: Synchronous authority check, or None for receipt-only writes.

    Raises:
        ProtocolError: If a callback is asynchronous or explicitly denies access.
    """
    if check is not None:
        result = check()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ProtocolError("blacklist_guard_async")
        if result is False:
            raise ProtocolError("blacklist_authority_changed")


@dataclass(frozen=True)
class BlacklistIntent:
    """Immutable action capability whose complete binding must exist on disk.

    Args:
        id: Random durable intent identity.
        instance: Owning platform instance.
        account: Authenticated robot business identity.
        group: Business group identity.
        member: Blacklisted business member identity.
        peer: Verified native transport identity.
        entry: Durable blacklist entry identity.
        cycle: Member's blacklist enrollment cycle.
        version: Enrollment's policy version.
        join_id: Canonical native membership application ID, not a wakeup message.
        action: Mute-only action selected by the store.
        minutes: Group-configured blacklist mute duration.
        policy_version: Duration policy generation, invalidating unsubmitted work.
        trigger: Trusted rejoin evidence or an explicit native administrator command.
    """

    id: str
    instance: str
    account: str
    group: str
    member: str
    peer: str
    entry: str
    cycle: int
    version: int
    join_id: str
    action: str
    minutes: int
    policy_version: int = 0
    trigger: str = "join"

    @property
    def operation(self):
        """Return the only operation name this capability permits.

        Returns:
            Exact native operation identity.
        """
        return f"blacklist/{self.id}"


class BlacklistStore:
    """Store blacklist cycles and receipts with worker-owned transactions.

    Args:
        instance: Original platform instance identifier.
    """

    def __init__(self, instance):
        self.instance = instance
        self.path = instance_dir(instance) / "member_blacklist.sqlite3"

    @contextmanager
    def _transaction(self, guard=None):
        """Own one SQLite writer transaction, including additive initialization.

        Args:
            guard: Synchronous authority check at admission and commit.

        Yields:
            Connection owned solely by the calling worker.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            self.path.chmod(0o600)
            db.execute("BEGIN IMMEDIATE")
            _guard(guard)
            for sql in (
                """CREATE TABLE IF NOT EXISTS blacklist_versions(
                    account TEXT,group_id TEXT,version INTEGER NOT NULL,
                    PRIMARY KEY(account,group_id))""",
                """CREATE TABLE IF NOT EXISTS blacklist_settings(
                    account TEXT,group_id TEXT,mute_minutes INTEGER NOT NULL,
                    policy_version INTEGER NOT NULL,
                    PRIMARY KEY(account,group_id))""",
                """CREATE TABLE IF NOT EXISTS blacklist_entries(
                    id TEXT PRIMARY KEY,account TEXT NOT NULL,group_id TEXT NOT NULL,
                    member TEXT NOT NULL,peer TEXT NOT NULL,name TEXT NOT NULL,
                    cycle INTEGER NOT NULL,version INTEGER NOT NULL,
                    kicks INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,
                    status TEXT NOT NULL,actor TEXT NOT NULL,actor_peer TEXT NOT NULL,
                    UNIQUE(account,group_id,member,cycle))""",
                """CREATE UNIQUE INDEX IF NOT EXISTS blacklist_active_member
                    ON blacklist_entries(account,group_id,member) WHERE status='active'""",
                """CREATE TABLE IF NOT EXISTS blacklist_notices(
                    account TEXT,group_id TEXT,message TEXT,peers TEXT NOT NULL,
                    event_at REAL NOT NULL,state TEXT NOT NULL,
                    PRIMARY KEY(account,group_id,message))""",
                """CREATE TABLE IF NOT EXISTS blacklist_intents(
                    id TEXT PRIMARY KEY,instance TEXT NOT NULL,
                    account TEXT NOT NULL,group_id TEXT NOT NULL,
                    member TEXT NOT NULL,peer TEXT NOT NULL,entry TEXT NOT NULL,
                    cycle INTEGER NOT NULL,version INTEGER NOT NULL,join_id TEXT NOT NULL,
                    action TEXT NOT NULL,minutes INTEGER NOT NULL,event_at REAL NOT NULL,
                    status TEXT NOT NULL,finished INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(account,group_id,member,join_id))""",
                """CREATE INDEX IF NOT EXISTS blacklist_unresolved
                    ON blacklist_intents(account,group_id,member,status)""",
                """CREATE TABLE IF NOT EXISTS blacklist_audit(
                    id INTEGER PRIMARY KEY,recorded REAL NOT NULL,kind TEXT NOT NULL,
                    account TEXT NOT NULL,group_id TEXT NOT NULL,payload TEXT NOT NULL)""",
                """CREATE TRIGGER IF NOT EXISTS blacklist_audit_no_update
                    BEFORE UPDATE ON blacklist_audit BEGIN
                    SELECT RAISE(ABORT,'immutable blacklist audit'); END""",
                """CREATE TRIGGER IF NOT EXISTS blacklist_audit_no_delete
                    BEFORE DELETE ON blacklist_audit BEGIN
                    SELECT RAISE(ABORT,'immutable blacklist audit'); END""",
            ):
                db.execute(sql)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(blacklist_notices)")
            }
            if "scope" not in columns:
                db.execute("ALTER TABLE blacklist_notices ADD COLUMN scope TEXT")
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(blacklist_intents)")
            }
            if "policy_version" not in columns:
                db.execute(
                    "ALTER TABLE blacklist_intents ADD COLUMN "
                    "policy_version INTEGER NOT NULL DEFAULT -1"
                )
            if "trigger" not in columns:
                db.execute(
                    "ALTER TABLE blacklist_intents ADD COLUMN "
                    "trigger TEXT NOT NULL DEFAULT 'join'"
                )
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(blacklist_settings)")
            }
            if "effective_at" not in columns:
                db.execute(
                    "ALTER TABLE blacklist_settings ADD COLUMN "
                    "effective_at REAL NOT NULL DEFAULT 0"
                )
            yield db
            _guard(guard)
            db.commit()
        finally:
            db.close()

    def read(self, account, group):
        """Read the editable list and its optimistic concurrency version.

        Args:
            account: Robot business identity.
            group: Business group identity.

        Returns:
            Version and active entry dictionaries, including creating manager IDs.
        """
        account, group = _identity(account), _identity(group)
        with self._transaction() as db:
            row = db.execute(
                "SELECT version FROM blacklist_versions WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            entries = db.execute(
                "SELECT id,member,peer,name,cycle,version,kicks,created,status,actor,"
                "actor_peer,(SELECT CASE WHEN i.status='unknown' AND i.finished=0 "
                "THEN 'pending' ELSE i.status END FROM blacklist_intents i "
                "WHERE i.account=blacklist_entries.account "
                "AND i.group_id=blacklist_entries.group_id "
                "AND i.member=blacklist_entries.member "
                "AND i.entry=blacklist_entries.id "
                "ORDER BY CASE WHEN i.status IN ('unknown','accepted') THEN 0 ELSE 1 END,"
                "i.rowid DESC LIMIT 1) AS enforcement_status "
                "FROM blacklist_entries WHERE account=? AND group_id=? "
                "AND status='active' ORDER BY created,id",
                (account, group),
            ).fetchall()
            return (row["version"] if row else 0), [dict(row) for row in entries]

    def settings(self, account, group):
        """Read one robot/group's duration and optimistic policy generation.

        Args:
            account: Robot business identity.
            group: Business group identity.

        Returns:
            List version, mute minutes and duration policy version.
        """
        account, group = _identity(account), _identity(group)
        with self._transaction() as db:
            version = db.execute(
                "SELECT version FROM blacklist_versions WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            policy = db.execute(
                "SELECT mute_minutes,policy_version FROM blacklist_settings "
                "WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            return {
                "version": version["version"] if version else 0,
                "mute_minutes": policy["mute_minutes"]
                if policy
                else BLACKLIST_MUTE_MINUTES,
                "policy_version": policy["policy_version"] if policy else 0,
            }

    def configure(self, account, group, minutes, actor, guard, expected_version=None):
        """Change future rejoin mutes without touching existing native sanctions.

        Args:
            account: Robot business identity.
            group: Business group identity.
            minutes: Whole minutes between one and the validated 29-day bound.
            actor: Authenticated native or dashboard audit principal.
            guard: Required synchronous permission and lifecycle check.
            expected_version: List version captured by a confirmed preview.

        Returns:
            New list version, mute minutes and policy version.

        Raises:
            ProtocolError: On invalid inputs, stale preview or revoked authority.
        """
        account, group = _identity(account), _identity(group)
        if (
            type(minutes) is not int
            or not 1 <= minutes <= BLACKLIST_MUTE_MINUTES
            or not callable(guard)
            or expected_version is not None
            and (type(expected_version) is not int or expected_version < 0)
        ):
            raise ProtocolError("blacklist_minutes")
        actor_peer = ""
        if isinstance(actor, ActorIdentity):
            if actor.account != account:
                raise ProtocolError("blacklist_actor")
            actor_peer, actor = actor.peer, actor.require_verified()
        if isinstance(actor, dict):
            actor_peer = actor.get("peer", "")
            actor = actor.get("member", actor.get("uid", actor.get("id")))
        if isinstance(actor, str) and actor.startswith("dashboard/"):
            if (
                len(actor) <= len("dashboard/")
                or len(actor.encode()) > 256
                or any(not char.isprintable() for char in actor)
            ):
                raise ProtocolError("blacklist_actor")
        else:
            actor = _identity(actor)
        if not isinstance(actor_peer, str) or len(actor_peer.encode()) > 1024:
            raise ProtocolError("blacklist_actor")
        with self._transaction(guard) as db:
            row = db.execute(
                "SELECT version FROM blacklist_versions WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            version = row["version"] if row else 0
            if expected_version is not None and expected_version != version:
                raise ProtocolError("blacklist_version_changed")
            row = db.execute(
                "SELECT mute_minutes,policy_version FROM blacklist_settings "
                "WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            previous = row["mute_minutes"] if row else BLACKLIST_MUTE_MINUTES
            policy_version = row["policy_version"] if row else 0
            if minutes != previous:
                version += 1
                policy_version += 1
                effective_at = time.time()
                db.execute(
                    "INSERT INTO blacklist_versions VALUES(?,?,?) "
                    "ON CONFLICT(account,group_id) DO UPDATE SET version=excluded.version",
                    (account, group, version),
                )
                db.execute(
                    "INSERT INTO blacklist_settings"
                    "(account,group_id,mute_minutes,policy_version,effective_at) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(account,group_id) DO UPDATE SET "
                    "mute_minutes=excluded.mute_minutes,policy_version=excluded.policy_version,"
                    "effective_at=excluded.effective_at",
                    (account, group, minutes, policy_version, effective_at),
                )
                db.execute(
                    "INSERT INTO blacklist_audit(recorded,kind,account,group_id,payload) "
                    "VALUES(?,'configure',?,?,?)",
                    (
                        time.time(),
                        account,
                        group,
                        json.dumps(
                            {
                                "actor": actor,
                                "actor_peer": actor_peer,
                                "previous_minutes": previous,
                                "mute_minutes": minutes,
                                "version": version,
                                "policy_version": policy_version,
                                "effective_at": effective_at,
                            }
                        ),
                    ),
                )
            return {
                "version": version,
                "mute_minutes": minutes,
                "policy_version": policy_version,
            }

    def set_member(
        self,
        account,
        group,
        member,
        peer,
        name,
        enabled,
        actor,
        guard,
        expected_version=None,
    ):
        """Start or end an enrollment without resetting a repeated active add.

        Args:
            account: Robot business identity.
            group: Business group identity.
            member: Verified member business identity.
            peer: Verified member transport identity.
            name: Display label, never authority.
            enabled: Whether the member belongs to the blacklist.
            actor: Verified ActorIdentity, manager UID/peer dict, or dashboard principal.
            guard: Required synchronous manager and lifecycle check.
            expected_version: Optional list version captured by a preview.

        Returns:
            Current list version and active entries, as returned by read.

        Raises:
            ProtocolError: If inputs, authority or preview version are invalid,
                or the member is already whitelisted (member_list_conflict).
        """
        account, group, member = map(_identity, (account, group, member))
        if (
            not callable(guard)
            or type(enabled) is not bool
            or not isinstance(peer, str)
            or not peer.strip()
            or len(peer.encode()) > 1024
            or not isinstance(name, str)
            or len(name.encode()) > 1024
            or expected_version is not None
            and (type(expected_version) is not int or expected_version < 0)
        ):
            raise ProtocolError("blacklist_arguments")
        actor_peer = ""
        if isinstance(actor, ActorIdentity):
            if actor.account != account:
                raise ProtocolError("blacklist_actor")
            actor_peer, actor = actor.peer, actor.require_verified()
        if isinstance(actor, dict):
            actor_peer = actor.get("peer", "")
            actor = actor.get("member", actor.get("uid", actor.get("id")))
        if isinstance(actor, str) and actor.startswith("dashboard/"):
            if (
                len(actor) <= len("dashboard/")
                or len(actor.encode()) > 256
                or any(not char.isprintable() for char in actor)
            ):
                raise ProtocolError("blacklist_actor")
        else:
            actor = _identity(actor)
        if not isinstance(actor_peer, str) or len(actor_peer.encode()) > 1024:
            raise ProtocolError("blacklist_actor")
        with MEMBER_LIST_WRITE_LOCK, self._transaction(guard) as db:
            row = db.execute(
                "SELECT version FROM blacklist_versions WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            version = row["version"] if row else 0
            if expected_version is not None and version != expected_version:
                raise ProtocolError("blacklist_version_changed")
            if enabled:
                opposite = self.path.with_name("member_exemptions.sqlite3")
                try:
                    opposite.stat()
                except FileNotFoundError:
                    pass
                else:
                    with closing(
                        sqlite3.connect(
                            opposite.resolve().as_uri() + "?mode=ro", uri=True
                        )
                    ) as other:
                        # Legacy whitelist IDs may contain leading zeroes.
                        if other.execute(
                            "SELECT 1 FROM speech_exemptions "
                            "WHERE ltrim(account,'0')=? AND ltrim(group_id,'0')=? "
                            "AND ltrim(member,'0')=? AND enabled=1 LIMIT 1",
                            (account, group, member),
                        ).fetchone():
                            raise ProtocolError("member_list_conflict")
            current = db.execute(
                "SELECT * FROM blacklist_entries WHERE account=? AND group_id=? "
                "AND member=? AND status='active'",
                (account, group, member),
            ).fetchone()
            if (enabled and current is None) or (not enabled and current is not None):
                version += 1
                db.execute(
                    "INSERT INTO blacklist_versions VALUES(?,?,?) "
                    "ON CONFLICT(account,group_id) DO UPDATE SET version=excluded.version",
                    (account, group, version),
                )
                if enabled:
                    cycle = db.execute(
                        "SELECT COALESCE(MAX(cycle),0)+1 FROM blacklist_entries "
                        "WHERE account=? AND group_id=? AND member=?",
                        (account, group, member),
                    ).fetchone()[0]
                    db.execute(
                        "INSERT INTO blacklist_entries(id,account,group_id,member,peer,"
                        "name,cycle,version,created,status,actor,actor_peer) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)",
                        (
                            secrets.token_hex(16),
                            account,
                            group,
                            member,
                            peer,
                            name,
                            cycle,
                            version,
                            time.time(),
                            actor,
                            actor_peer,
                        ),
                    )
                else:
                    db.execute(
                        "UPDATE blacklist_entries SET status='removed' WHERE id=?",
                        (current["id"],),
                    )
                db.execute(
                    "INSERT INTO blacklist_audit(recorded,kind,account,group_id,payload) "
                    "VALUES(?,?,?,?,?)",
                    (
                        time.time(),
                        "add" if enabled else "remove",
                        account,
                        group,
                        json.dumps(
                            {
                                "member": member,
                                "peer": peer,
                                "actor": actor,
                                "actor_peer": actor_peer,
                                "version": version,
                            }
                        ),
                    ),
                )
            entries = db.execute(
                "SELECT id,member,peer,name,cycle,version,kicks,created,status,actor,"
                "actor_peer,(SELECT CASE WHEN i.status='unknown' AND i.finished=0 "
                "THEN 'pending' ELSE i.status END FROM blacklist_intents i "
                "WHERE i.account=blacklist_entries.account "
                "AND i.group_id=blacklist_entries.group_id "
                "AND i.member=blacklist_entries.member "
                "AND i.entry=blacklist_entries.id "
                "ORDER BY CASE WHEN i.status IN ('unknown','accepted') THEN 0 ELSE 1 END,"
                "i.rowid DESC LIMIT 1) AS enforcement_status "
                "FROM blacklist_entries WHERE account=? AND group_id=? "
                "AND status='active' ORDER BY created,id",
                (account, group),
            ).fetchall()
            return version, [dict(row) for row in entries]

    def observe(self, account, group, message, peers, event_at, guard, *, scope=None):
        """Persist bounded native wakeups, not canonical membership authority.

        Args:
            account: Robot business identity.
            group: Verified business group identity.
            message: Native join-notice message ID validated by ingress.
            peers: At most 200 native join peers validated by ingress.
            event_at: Native event timestamp in seconds.
            guard: Required synchronous ingress and lifecycle guard.
            scope: Optional receiving tenant epoch/version and business generation.

        Returns:
            Whether a new wakeup was stored.

        Raises:
            ProtocolError: If bounded metadata or guard is invalid.
        """
        account, group = _identity(account), _identity(group)
        if (
            not callable(guard)
            or not isinstance(message, str)
            or not message.strip()
            or len(message.encode()) > 128
            or any(ord(char) < 32 for char in message)
            or not isinstance(peers, (list, tuple))
            or not 1 <= len(peers) <= 200
            or any(
                not isinstance(p, str) or not p.strip() or len(p.encode()) > 1024
                for p in peers
            )
            or type(event_at) not in {int, float}
            or not math.isfinite(event_at)
            or not 0 < event_at <= time.time() + 60
        ):
            raise ProtocolError("blacklist_notice")
        peers = list(dict.fromkeys(peers))
        with self._transaction(guard) as db:
            changed = db.execute(
                "INSERT OR IGNORE INTO blacklist_notices"
                "(account,group_id,message,peers,event_at,state,scope) "
                "VALUES(?,?,?,?,?,'pending',?)",
                (
                    account,
                    group,
                    message,
                    json.dumps(peers),
                    event_at,
                    json.dumps(scope),
                ),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO blacklist_audit(recorded,kind,account,group_id,payload) "
                    "VALUES(?,'notice',?,?,?)",
                    (
                        time.time(),
                        account,
                        group,
                        json.dumps(
                            {"message": message, "peers": peers, "event_at": event_at}
                        ),
                    ),
                )
            return bool(changed)

    def pending(self, account):
        """Read persisted pending join wakeups for one robot account.

        Args:
            account: Robot business identity.

        Returns:
            Wakeup dictionaries with account, group, message, peers and event_at.
        """
        account = _identity(account)
        with self._transaction() as db:
            rows = db.execute(
                "SELECT account,group_id AS 'group',message,peers,event_at,state,scope "
                "FROM blacklist_notices WHERE account=? AND state='pending' "
                "ORDER BY event_at,message",
                (account,),
            ).fetchall()
            return [
                {
                    **dict(row),
                    "peers": json.loads(row["peers"]),
                    "scope": json.loads(row["scope"]) if row["scope"] else None,
                }
                for row in rows
            ]

    def finish_notice(self, account, group, message, state):
        """Finish a wakeup once without ever rearming completed work.

        Args:
            account: Robot business identity.
            group: Business group identity.
            message: Stored native message ID.
            state: Non-pending terminal audit label.

        Raises:
            ProtocolError: If the terminal label is invalid.
        """
        account, group = _identity(account), _identity(group)
        if (
            not isinstance(state, str)
            or not state
            or state == "pending"
            or len(state) > 64
            or not state.isascii()
            or any(not (c.isalnum() or c == "_") for c in state)
        ):
            raise ProtocolError("blacklist_notice_state")
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE blacklist_notices SET state=? WHERE account=? AND group_id=? "
                "AND message=? AND state='pending'",
                (state, account, group, message),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO blacklist_audit(recorded,kind,account,group_id,payload) "
                    "VALUES(?,'notice_done',?,?,?)",
                    (
                        time.time(),
                        account,
                        group,
                        json.dumps({"message": message, "state": state}),
                    ),
                )

    def reserve(self, account, group, member, peer, join_id, event_at, guard):
        """Reserve a canonical join round, persisting unknown before any mutation.

        Args:
            account: Robot business identity.
            group: Business group identity.
            member: Verified joining member business identity.
            peer: Freshly verified member transport identity.
            join_id: Canonical positive native membership application ID.
            event_at: Canonical membership event timestamp in seconds.
            guard: Required synchronous authority and lifecycle guard.

        Returns:
            Bound capability, or None for old, duplicate or unresolved work.

        Raises:
            ProtocolError: If inputs or authority are invalid.
        """
        account, group, member = map(_identity, (account, group, member))
        raw_join_id, join_id = join_id, str(join_id)
        if (
            type(raw_join_id) not in {int, str}
            or not 1 <= len(join_id) <= 20
            or not join_id.isascii()
            or not join_id.isdigit()
            or int(join_id) <= 0
            or str(int(join_id)) != join_id
        ):
            raise ProtocolError("blacklist_join")
        if (
            not callable(guard)
            or not isinstance(peer, str)
            or not peer.strip()
            or len(peer.encode()) > 1024
            or type(event_at) not in {int, float}
            or not math.isfinite(event_at)
            or not 0 < event_at <= time.time()
        ):
            raise ProtocolError("blacklist_join")
        return self._reserve(
            account, group, member, peer, join_id, event_at, guard, trigger="join"
        )

    def reserve_command(self, account, group, member, peer, message, guard):
        """Bind an immediate mute to one explicit, verified native command.

        Command IDs have a separate namespace from real membership applications.
        Duplicate deliveries and unresolved actions never renew a mute.
        """
        account, group, member = map(_identity, (account, group, member))
        if (
            not isinstance(message, str)
            or not 1 <= len(message.encode()) <= 128
            or any(not char.isprintable() for char in message)
            or not callable(guard)
            or not isinstance(peer, str)
            or not peer.strip()
            or len(peer.encode()) > 1024
        ):
            raise ProtocolError("blacklist_command")
        return self._reserve(
            account,
            group,
            member,
            peer,
            "command/" + message,
            time.time(),
            guard,
            trigger="command",
        )

    def _reserve(
        self, account, group, member, peer, join_id, event_at, guard, *, trigger
    ):
        """Persist an evidence-scoped capability before any native mutation."""
        with self._transaction(guard) as db:
            entry = db.execute(
                "SELECT * FROM blacklist_entries WHERE account=? AND group_id=? "
                "AND member=? AND status='active'",
                (account, group, member),
            ).fetchone()
            if entry is None or entry["peer"] != peer or event_at < entry["created"]:
                return None
            if db.execute(
                "SELECT 1 FROM blacklist_intents WHERE account=? AND group_id=? "
                "AND member=? AND (join_id=? OR status='unknown' "
                "OR status='accepted' AND action='kick')",
                (account, group, member, join_id),
            ).fetchone():
                return None
            policy = db.execute(
                "SELECT mute_minutes,policy_version,effective_at FROM blacklist_settings "
                "WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if policy and event_at < policy["effective_at"]:
                return None
            intent = BlacklistIntent(
                secrets.token_hex(16),
                self.instance,
                account,
                group,
                member,
                peer,
                entry["id"],
                entry["cycle"],
                entry["version"],
                join_id,
                "mute",
                policy["mute_minutes"] if policy else BLACKLIST_MUTE_MINUTES,
                policy["policy_version"] if policy else 0,
                trigger,
            )
            db.execute(
                "INSERT INTO blacklist_intents(id,instance,account,group_id,member,peer,entry,"
                "cycle,version,join_id,action,minutes,event_at,policy_version,trigger,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unknown')",
                (
                    intent.id,
                    self.instance,
                    account,
                    group,
                    member,
                    peer,
                    intent.entry,
                    intent.cycle,
                    intent.version,
                    join_id,
                    intent.action,
                    intent.minutes,
                    event_at,
                    intent.policy_version,
                    intent.trigger,
                ),
            )
            db.execute(
                "INSERT INTO blacklist_audit(recorded,kind,account,group_id,payload) "
                "VALUES(?,'reserve',?,?,?)",
                (time.time(), account, group, json.dumps(asdict(intent))),
            )
            return intent

    def done(self, intent, result):
        """Retain one terminal receipt; only proven absent kicks increase counts.

        Args:
            intent: Original capability returned by reserve.
            result: Native receipt; verified kicks require complete absence proof.

        Raises:
            ProtocolError: If the capability is forged or bound to another store.
        """
        if type(intent) is not BlacklistIntent or intent.instance != self.instance:
            raise ProtocolError("blacklist_intent_invalid")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM blacklist_intents WHERE id=?",
                (intent.id,),
            ).fetchone()
            if row is None or any(
                type(row["group_id" if key == "group" else key]) is not type(value)
                or row["group_id" if key == "group" else key] != value
                for key, value in asdict(intent).items()
            ):
                raise ProtocolError("blacklist_intent_invalid")
            if row["finished"]:
                return row["status"]
            result = dict(result) if isinstance(result, dict) else {}
            status = result.get("status")
            if status not in ("verified", "rejected", "accepted", "unknown"):
                status = "unknown"
            if (
                status in {"accepted", "verified"}
                and intent.action == "mute"
                and not (
                    result.get("submitted") is True
                    and result.get("operation") == intent.operation
                    and result.get("action") == "mute"
                    and str(result.get("group")) == intent.group
                    and str(result.get("member")) == intent.member
                    and result.get("expected_nim") == intent.peer
                    and type(result.get("minutes")) is int
                    and result["minutes"] == intent.minutes
                )
            ):
                status = "unknown"
            if (
                status == "verified"
                and intent.action == "kick"
                and not (
                    result.get("absence_verified") is True
                    and result.get("readback_complete") is True
                    and result.get("expected_nim") == intent.peer
                    and result.get("operation") == intent.operation
                    and result.get("action") == intent.action
                    and str(result.get("group")) == intent.group
                    and str(result.get("member")) == intent.member
                )
            ):
                status = "unknown"
            db.execute(
                "UPDATE blacklist_intents SET status=?,finished=1,result=? WHERE id=?",
                (status, json.dumps({**result, "status": status}), intent.id),
            )
            if status == "verified" and intent.action == "kick":
                db.execute(
                    "UPDATE blacklist_entries SET kicks=kicks+1 WHERE id=? "
                    "AND cycle=? AND version=? AND status='active'",
                    (intent.entry, intent.cycle, intent.version),
                )
            db.execute(
                "INSERT INTO blacklist_audit(recorded,kind,account,group_id,payload) "
                "VALUES(?,'done',?,?,?)",
                (
                    time.time(),
                    intent.account,
                    intent.group,
                    json.dumps(
                        {"intent": intent.id, "result": result, "status": status}
                    ),
                ),
            )
            return status


def authorize_intent(
    instance, account, operation, action, group, member, minutes, intent
):
    """Recheck the full durable capability at every execution boundary.

    Args:
        instance: Original platform instance.
        account: Current authenticated robot identity.
        operation: Exact blacklist operation identifier.
        action: Requested mute-only native action.
        group: Target business group.
        member: Target business member.
        minutes: Exact duration bound by the capability.
        intent: Original immutable stored capability.

    Raises:
        ProtocolError: If any binding or current persistent authority differs.
    """
    if (
        type(intent) is not BlacklistIntent
        or intent.instance != instance
        or intent.account != str(account)
        or intent.group != str(group)
        or intent.member != str(member)
        or intent.operation != operation
        or intent.action != action
        or not isinstance(action, str)
        or type(minutes) is not int
        or intent.minutes != minutes
        or type(intent.cycle) is not int
        or intent.cycle <= 0
        or type(intent.version) is not int
        or intent.version <= 0
        or type(intent.minutes) is not int
        or action != "mute"
        or not 1 <= minutes <= BLACKLIST_MUTE_MINUTES
        or type(intent.policy_version) is not int
        or intent.policy_version < 0
        or not isinstance(intent.trigger, str)
        or intent.trigger not in {"join", "command"}
        or not isinstance(intent.join_id, str)
        or (intent.trigger == "command") != intent.join_id.startswith("command/")
    ):
        raise ProtocolError("blacklist_intent_invalid")
    store = BlacklistStore(instance)
    with store._transaction() as db:
        row = db.execute(
            "SELECT * FROM blacklist_intents WHERE id=?",
            (intent.id,),
        ).fetchone()
        entry = db.execute(
            "SELECT * FROM blacklist_entries WHERE id=?",
            (intent.entry,),
        ).fetchone()
        policy = db.execute(
            "SELECT mute_minutes,policy_version FROM blacklist_settings "
            "WHERE account=? AND group_id=?",
            (intent.account, intent.group),
        ).fetchone()
        if (
            row is None
            or entry is None
            or row["finished"]
            or row["status"] != "unknown"
            or entry["status"] != "active"
            or entry["account"] != intent.account
            or entry["group_id"] != intent.group
            or entry["member"] != intent.member
            or entry["cycle"] != intent.cycle
            or entry["version"] != intent.version
            or entry["peer"] != intent.peer
            or any(
                row["group_id" if key == "group" else key] != value
                for key, value in asdict(intent).items()
            )
            or intent.minutes
            != (policy["mute_minutes"] if policy else BLACKLIST_MUTE_MINUTES)
            or intent.policy_version != (policy["policy_version"] if policy else 0)
        ):
            raise ProtocolError("blacklist_intent_stale")
