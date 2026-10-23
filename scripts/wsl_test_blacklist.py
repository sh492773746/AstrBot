"""Opt-in fixed-target blacklist membership helper; importing performs no IO.

No CLI or automatic runner is provided. An authorized acceptance driver must
explicitly bind each original TestWindow with begin_blacklist, call change for
each fixed step, use the normal service/native blacklist path for sanctions,
remove the test blacklist and mute, then await fixture.restore(). A uniquely
proven pending invitation may be accepted once by its exact recipient; unknown
receipts, timeouts, cancellation and failed verification freeze the operation.
This helper does not extend MembershipTest, whose PEER remains 43506527.
"""

import asyncio
import copy
import sqlite3
import time
from contextlib import closing
from datetime import datetime

from astrbot.core.platform.sources.wangshangliao.business_http import (
    _exchange_business,
    request_business,
)
from astrbot.core.platform.sources.wangshangliao.test_blacklist import (
    GROUP,
    OWNER,
    STEPS,
    TARGET,
    BlacklistFixture,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

INVITE = "/v1/group/add-group-member"
REMOVE = "/v1/group/remove-group-member"
ACCEPT = "/v1/group/group-member-invite"


class BlacklistMembershipTest:
    """Allow only one explicit original-owner leave and exact-target rejoin.

    Args:
        fixture: Exact currently installed blacklist fixture, not an operation string.

    Raises:
        ProtocolError: If the capability is absent, revoked or unresolved.
    """

    def __init__(self, fixture):
        if type(fixture) is not BlacklistFixture:
            raise ProtocolError("test_blacklist_scope")
        fixture.check()
        self.fixture = fixture
        self.last_request = 0.0
        self.lock = asyncio.Lock()
        self._sending = None
        self._submitted = False

    async def request(self, route, params):
        """Submit only a reserved original-owner single-target membership write.

        Args:
            route: Remove for explicit owner-controlled leave, or the single invite.
            params: Exact numeric test group and the one exact target.

        Returns:
            Signed provider response; not proof of membership on its own.

        Raises:
            ProtocolError: If raw access, scope, role or identity differs.
        """
        intent = self._sending
        if (
            intent is None
            or self._submitted
            or route != (REMOVE if intent.step == "leave" else INVITE)
            or type(params) is not dict
            or set(params) != {"groupId", "groupMemberIds"}
            or type(params["groupId"]) is not int
            or params["groupId"] != int(GROUP)
            or type(params["groupMemberIds"]) is not list
            or len(params["groupMemberIds"]) != 1
            or type(params["groupMemberIds"][0]) is not int
            or params["groupMemberIds"] != [int(TARGET)]
        ):
            raise ProtocolError("test_blacklist_unreserved")
        self._submitted = True
        fixture = self.fixture
        owner = next(a for a in fixture.service.adapters() if a.account == OWNER)
        client = owner.business

        async def authorize():
            """Refresh exact roster and recheck revocation at final submission."""
            fixture.check(intent)
            if owner.business is not client or str(client.fields[3]) != OWNER:
                raise ProtocolError("test_blacklist_identity")
            directory = await fixture.window.receiver.get_moderation_members(GROUP)
            fixture.check(intent)
            fixture.roster(directory, present=intent.step == "leave")

        await asyncio.sleep(max(0, self.last_request + 3 - time.monotonic()))
        await authorize()
        self.last_request = time.monotonic()
        # Removal is an explicit owner test step, never a blacklist sanction.
        exchange = _exchange_business if route == INVITE else request_business
        return await exchange(client, route, copy.deepcopy(params), authorize=authorize)

    async def change(self, step, *, group=GROUP, member=TARGET):
        """Execute one explicit step; never resume or retry an unknown mutation.

        Args:
            step: Exactly leave or invite/1, each requested explicitly.
            group: Must remain the one authorized test group.
            member: Must remain the one controlled ordinary-role test target.

        Returns:
            Verified exact-target membership evidence, not blacklist acceptance.

        Raises:
            ProtocolError: On reordered steps, stale roles, unknown or scope changes.
        """
        if (
            type(step) is not str
            or step not in STEPS
            or (group, member) != (GROUP, TARGET)
        ):
            raise ProtocolError("test_blacklist_scope")
        async with self.lock:
            fixture = self.fixture
            fixture.check()
            with closing(sqlite3.connect(fixture.path)) as db:
                last = db.execute(
                    "SELECT COALESCE(MAX(created),0) FROM attempts"
                ).fetchone()[0]
            if last:
                await asyncio.sleep(max(0, last + 30 - time.time()))
            fixture.check()
            directory = await fixture.window.receiver.get_moderation_members(GROUP)
            fixture.check()
            fixture.roster(directory, present=step == "leave")
            intent = fixture.reserve(step)
            self._sending, self._submitted = intent, False
            try:
                await self.request(
                    REMOVE if step == "leave" else INVITE,
                    {"groupId": int(GROUP), "groupMemberIds": [int(TARGET)]},
                )
                if step != "leave":
                    directory = await fixture.window.receiver.get_moderation_members(
                        GROUP
                    )
                    fixture.check(intent)
                    present = any(
                        str(row.get("userId")) == TARGET
                        for row in directory.get("groupMemberInfo", [])
                    )
                    if not present:
                        await self.accept_invitation(intent)
                await fixture.verify(intent)
            finally:
                # A failed request is permanently unknown, including on cancellation.
                fixture._pending.pop(intent.nonce, None)
                self._sending = None
            return {"step": step, "group": GROUP, "member": TARGET, "state": "verified"}

    async def accept_invitation(self, intent):
        """Accept one proven fresh invitation without resubmitting the invitation.

        Args:
            intent: Original pending membership capability for this exact round.

        Raises:
            ProtocolError: If the invitation, roster or once-only receipt is unknown.
        """
        fixture = self.fixture
        fixture.check(intent)
        if self._sending is not intent or intent.step != "invite/1":
            raise ProtocolError("test_blacklist_intent")
        target = fixture.target.business
        with closing(sqlite3.connect(fixture.path)) as db:
            created = db.execute(
                "SELECT created FROM attempts WHERE nonce=?", (intent.nonce,)
            ).fetchone()[0]

        async def evidence():
            fixture.check(intent)
            if str(target.fields[3]) != TARGET or fixture.target.business is not target:
                raise ProtocolError("test_blacklist_identity")
            result = await target.request("/v1/group/get-apply-logs", {})
            fixture.check(intent)
            rows = result.get("list")
            if not isinstance(rows, list) or len(rows) > 4096:
                raise ProtocolError("test_blacklist_invitation")
            matches = []
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or str(row.get("groupId")) != GROUP
                    or str((row.get("applicant") or {}).get("userId")) != TARGET
                    or str((row.get("applicant") or {}).get("nimId"))
                    != fixture.peers[TARGET]
                    or str((row.get("inviter") or {}).get("userId")) != OWNER
                    or row.get("groupApplyType") != "INVITATION"
                ):
                    continue
                try:
                    stamp = datetime.fromisoformat(
                        str(row["applyTime"]).replace("Z", "+00:00")
                    )
                    if stamp.tzinfo is None:
                        raise ValueError
                    identity = row["applyId"]
                    if (
                        type(identity) is not str
                        or not identity.isascii()
                        or not identity.isdecimal()
                        or not 0 < int(identity) < 1 << 63
                    ):
                        raise ValueError
                except (ValueError, KeyError):
                    raise ProtocolError("test_blacklist_invitation") from None
                if created - 5 <= stamp.timestamp() <= time.time() + 5:
                    matches.append(row)
            if len(matches) != 1 or matches[0].get("state") not in {
                "MEMBER_STATE_INVITED",
                "MEMBER_STATE_GOOD",
            }:
                raise ProtocolError("test_blacklist_invitation")
            return matches[0]

        row = await evidence()
        if row["state"] == "MEMBER_STATE_GOOD":
            return
        with closing(sqlite3.connect(fixture.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS acceptances("
                "step TEXT PRIMARY KEY,apply_id TEXT UNIQUE,state TEXT NOT NULL)"
            )
            try:
                db.execute(
                    "INSERT INTO acceptances VALUES(?,?,'unknown')",
                    (intent.step, row["applyId"]),
                )
            except sqlite3.IntegrityError:
                raise ProtocolError("test_blacklist_no_replay") from None

        async def authorize():
            fixture.check(intent)
            latest = await evidence()
            if (
                latest["applyId"] != row["applyId"]
                or latest["state"] != "MEMBER_STATE_INVITED"
            ):
                raise ProtocolError("test_blacklist_invitation")
            directory = await fixture.receiver.get_moderation_members(GROUP)
            fixture.check(intent)
            fixture.roster(directory, present=False)

        await asyncio.sleep(max(0, self.last_request + 3 - time.monotonic()))
        self.last_request = time.monotonic()
        await _exchange_business(
            target,
            ACCEPT,
            {"applyId": row["applyId"], "agree": True},
            authorize=authorize,
        )
        latest = await evidence()
        if (
            latest["applyId"] != row["applyId"]
            or latest["state"] != "MEMBER_STATE_GOOD"
        ):
            raise ProtocolError("test_blacklist_invitation_unknown")
        directory = await fixture.receiver.get_moderation_members(GROUP)
        fixture.check(intent)
        fixture.roster(directory)
        with closing(sqlite3.connect(fixture.path)) as db, db:
            db.execute(
                "UPDATE acceptances SET state='verified' WHERE step=?", (intent.step,)
            )
