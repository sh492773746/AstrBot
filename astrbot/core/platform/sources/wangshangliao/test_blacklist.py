"""Exact, revocable blacklist-test capability; never an account-wide exemption."""

import re
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from types import MappingProxyType

from .blacklist import BLACKLIST_MUTE_MINUTES, BlacklistIntent
from .group_scope import GroupAdapter
from .storage import instance_dir
from .test_window import TestWindow
from .wire import ProtocolError

GROUP, RECEIVER, OWNER, TARGET = "1459282", "43506527", "31846829", "23691273"
STEPS = ("leave", "invite/1")


@dataclass(frozen=True)
class MembershipIntent:
    """One in-process capability backed by a durable unknown-before-IO record."""

    step: str
    nonce: str


@dataclass(frozen=True, eq=False)
class BlacklistFixture:
    """Bind only the fixed target to an original window and original authority.

    Args:
        window: Existing original receiver/target test window.
        operation: Exact dashboard operation marker.
        peers: Original verified business-to-NIM pairs.
        service: Original control plane installing this capability.
        binding_guard: Synchronous original service, tenant and account checks.
        restoration_guard: Async read-only blacklist and mute restoration checks.
    """

    window: TestWindow
    operation: str
    peers: dict
    service: object
    binding_guard: object
    restoration_guard: object
    deadline: float = field(init=False)
    scopes: tuple = field(init=False)
    receiver: object = field(init=False, repr=False)
    target: object = field(init=False, repr=False)
    _pending: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        """Freeze original window identities, scope and peer mapping without IO."""
        object.__setattr__(self, "deadline", self.window.expires)
        object.__setattr__(self, "scopes", tuple(self.window.scopes))
        object.__setattr__(self, "receiver", self.window.receiver)
        object.__setattr__(self, "target", self.window.sender)
        object.__setattr__(self, "peers", MappingProxyType(dict(self.peers)))

    @property
    def path(self):
        """Return the fixed private test ledger, not a business database."""
        return (
            instance_dir(self.receiver.config["id"])
            / "test-blacklist"
            / self.operation
            / "fixture.sqlite3"
        )

    def check_native(self, blacklist_intent=None):
        """Freeze on unknown sanctions except the exact current durable intent.

        Args:
            blacklist_intent: Native BlacklistIntent at its final submission gate.

        Raises:
            ProtocolError: If another unresolved intent exists or binding differs.
        """
        native = (
            instance_dir(self.window.receiver.config["id"]) / "member_blacklist.sqlite3"
        )
        if not native.exists():
            if blacklist_intent is not None:
                raise ProtocolError("test_blacklist_intent")
            return
        with closing(sqlite3.connect(native.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM blacklist_intents WHERE account=? AND group_id=? "
                "AND member=? AND (status='unknown' OR "
                "status='accepted' AND action='kick')",
                (RECEIVER, GROUP, TARGET),
            ).fetchall()
            if not rows and blacklist_intent is None:
                return
            intent = blacklist_intent
            if (
                type(intent) is not BlacklistIntent
                or len(rows) != 1
                or rows[0]["finished"]
                or "policy_version" not in rows[0].keys()
                or intent.instance != self.window.receiver.config["id"]
                or (intent.account, intent.group, intent.member, intent.peer)
                != (RECEIVER, GROUP, TARGET, self.peers[TARGET])
                or intent.action != "mute"
                or type(intent.minutes) is not int
                or not 1 <= intent.minutes <= BLACKLIST_MUTE_MINUTES
                or type(intent.policy_version) is not int
                or intent.policy_version < 0
                or any(
                    type(rows[0]["group_id" if key == "group" else key])
                    is not type(value)
                    or rows[0]["group_id" if key == "group" else key] != value
                    for key, value in asdict(intent).items()
                )
            ):
                raise ProtocolError("test_blacklist_unknown")
            entry = db.execute(
                "SELECT * FROM blacklist_entries WHERE id=?", (intent.entry,)
            ).fetchone()
            policy = db.execute(
                "SELECT mute_minutes,policy_version FROM blacklist_settings "
                "WHERE account=? AND group_id=?",
                (RECEIVER, GROUP),
            ).fetchone()
            if (
                entry is None
                or entry["status"] != "active"
                or intent.minutes
                != (policy["mute_minutes"] if policy else BLACKLIST_MUTE_MINUTES)
                or intent.policy_version != (policy["policy_version"] if policy else 0)
                or any(
                    entry["group_id" if key == "group" else key] != getattr(intent, key)
                    for key in (
                        "account",
                        "group",
                        "member",
                        "peer",
                        "cycle",
                        "version",
                    )
                )
            ):
                raise ProtocolError("test_blacklist_intent")

    def check(self, intent=None, *, blacklist_intent=None):
        """Check original authority and freeze on every unresolved test intent.

        Args:
            intent: Exact locally reserved capability, only during its own IO.
            blacklist_intent: Exact native sanction intent, never a flag or string.

        Raises:
            ProtocolError: If revoked, expired, restored or outcome is unknown.
        """
        self.binding_guard()
        window = self.window
        if (
            type(window) is not TestWindow
            or window.receiver is not self.receiver
            or window.sender is not self.target
            or getattr(self.service, "_blacklist_fixture", None) is not self
            or window.receiver.test_window is not window
            or getattr(window, "_blacklist_fixture", None) is not self
            or window.receiver.account != RECEIVER
            or window.sender.account != TARGET
            or window.groups != [GROUP]
            or window.scopes not in (["group_rules"], ["group_commands"])
            or tuple(window.scopes) != self.scopes
            or window.keywords != [self.operation]
            or not re.fullmatch(r"WSL_TEST_BLACKLIST_[0-9a-f]{32}", self.operation)
            or window.expires != self.deadline
            or not window.active()
        ):
            raise ProtocolError("test_blacklist_revoked")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT restored FROM fixture").fetchone() != (0,):
                raise ProtocolError("test_blacklist_restored")
            unknown = db.execute(
                "SELECT step,nonce FROM attempts WHERE state='unknown'"
            ).fetchall()
        if unknown and (
            type(intent) is not MembershipIntent
            or self._pending.get(intent.nonce) is not intent
            or unknown != [(intent.step, intent.nonce)]
        ):
            raise ProtocolError("test_blacklist_unknown")
        self.check_native(blacklist_intent)

    def roster(self, directory, *, present=True):
        """Require exact owner/admin/member identities, never cached role guesses.

        Args:
            directory: Fresh complete authenticated native directory.
            present: True for three members, False for the owner and receiver,
                or None to accept either exact roster during segment renewal.

        Raises:
            ProtocolError: If roles, identities, health or completeness differ.
        """
        rows = directory.get("groupMemberInfo", [])
        roles = {OWNER: "GROUP_ROLE_OWNER", RECEIVER: "GROUP_ROLE_ADMIN"}
        if present is True or present is None and len(rows) == 3:
            roles[TARGET] = "GROUP_ROLE_MEMBER"
        members = {str(row.get("userId")): row for row in rows}
        if (
            directory.get("complete") is not True
            or len(rows) != len(roles)
            or set(members) != set(roles)
            or any(
                row.get("groupRole") != roles[uid]
                or row.get("accountState") != "ACCOUNT_STATE_GOOD"
                or str(row.get("nimId", "")) != self.peers[uid]
                for uid, row in members.items()
            )
        ):
            raise ProtocolError("test_blacklist_roster")

    async def authorize_submission(
        self, adapter, group, member, peer, *, blacklist_intent=None, directory=None
    ):
        """Refresh roles inside the final native authorization callback.

        Args:
            adapter: Original receiver or its genuine group-scoped view.
            group: Exact native business group.
            member: Exact protected business member.
            peer: Exact native target identity.
            blacklist_intent: Original native intent at the final submission gate.

        Raises:
            ProtocolError: If this capability or the fresh native roster differs.
        """
        if not protected_target_allowed(
            adapter,
            group,
            member,
            peer,
            fixture=self,
            blacklist_intent=blacklist_intent,
        ):
            raise ProtocolError("test_blacklist_scope")
        if directory is None:
            directory = await self.window.receiver.get_moderation_members(GROUP)
        self.check(blacklist_intent=blacklist_intent)
        self.roster(directory)
        if not protected_target_allowed(
            adapter,
            group,
            member,
            peer,
            fixture=self,
            blacklist_intent=blacklist_intent,
        ):
            raise ProtocolError("test_blacklist_scope")

    def reserve(self, step):
        """Persist a one-shot unknown intent before a helper's membership write.

        Args:
            step: Explicit owner-controlled leave or the single reinvitation.

        Returns:
            Original in-process capability; never reconstructible after restart.

        Raises:
            ProtocolError: On unknown outcomes, duplicates or reordered steps.
        """
        self.check()
        intent = MembershipIntent(step, secrets.token_hex(16))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT step,state FROM attempts ORDER BY rowid"
            ).fetchall()
            if (
                len(rows) >= len(STEPS)
                or step != STEPS[len(rows)]
                or any(state != "verified" for _, state in rows)
                or [row[0] for row in rows] != list(STEPS[: len(rows)])
            ):
                raise ProtocolError("test_blacklist_no_replay")
            db.execute(
                "INSERT INTO attempts VALUES(?,?,'unknown',?)",
                (step, intent.nonce, time.time()),
            )
        self._pending[intent.nonce] = intent
        return intent

    async def verify(self, intent):
        """Verify the observed native result without resubmitting any mutation.

        Args:
            intent: Original in-process capability for this single submission.

        Raises:
            ProtocolError: If identity, authority or the membership result differs.
        """
        if (
            type(intent) is not MembershipIntent
            or self._pending.get(intent.nonce) is not intent
        ):
            raise ProtocolError("test_blacklist_intent")
        self.check(intent)
        directory = await self.window.receiver.get_moderation_members(GROUP)
        self.check(intent)
        self.roster(directory, present=intent.step != "leave")
        with closing(sqlite3.connect(self.path)) as db, db:
            changed = db.execute(
                "UPDATE attempts SET state='verified' "
                "WHERE step=? AND nonce=? AND state='unknown'",
                (intent.step, intent.nonce),
            ).rowcount
            if changed != 1:
                raise ProtocolError("test_blacklist_no_replay")
        self._pending.pop(intent.nonce, None)

    async def restore(self):
        """Close only after one rejoin, original roster and native cleanup.

        Raises:
            ProtocolError: If attempts, blacklist removal or unmute are unproven.
        """
        self.check()
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT step,state FROM attempts ORDER BY rowid"
            ).fetchall()
        if rows != [(step, "verified") for step in STEPS]:
            raise ProtocolError("test_blacklist_incomplete")
        directory = await self.window.receiver.get_moderation_members(GROUP)
        self.check()
        self.roster(directory)
        await self.restoration_guard()
        self.check()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE fixture SET restored=1")


def protected_target_allowed(
    adapter, group, member, peer, *, fixture=None, blacklist_intent=None
):
    """Permit only a currently installed exact capability, not a flag or marker.

    This exempts ONLY managed-target protection. Normal blacklist intent, actor,
    native role, policy, join-evidence and once-only execution checks still apply.
    Native callers must also await fixture.authorize_submission at final IO.

    Args:
        adapter: Original receiver or genuine GroupAdapter.
        group: Exact business group.
        member: Exact business target.
        peer: Exact verified NIM target.
        fixture: Original capability installed by begin_blacklist.
        blacklist_intent: Exact native intent during its own submission only.

    Returns:
        Whether only this protected-target check may be bypassed.
    """
    receiver = adapter.adapter if isinstance(adapter, GroupAdapter) else adapter
    if (
        type(fixture) is not BlacklistFixture
        or receiver is not fixture.window.receiver
        or (group, member, peer) != (GROUP, TARGET, fixture.peers[TARGET])
        or isinstance(adapter, GroupAdapter)
        and adapter.group != GROUP
    ):
        return False
    fixture.check(blacklist_intent=blacklist_intent)
    return True
