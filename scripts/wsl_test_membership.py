"""Fixed test-group membership operations, never imported by production."""

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
from astrbot.core.platform.sources.wangshangliao.directory import member_directory
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

GROUP = "1459282"
OWNER = "31846829"
PEER = "43506527"
ORDINARY = "23691273"
INVITE = "/v1/group/add-group-member"
REMOVE = "/v1/group/remove-group-member"
ACCEPT = "/v1/group/group-member-invite"


class MembershipTest:
    """Pace exact test operations and persist intent before any mutation."""

    def __init__(self, client, account, target_nim, output):
        """Bind one controlled actor and the fixed test bot.

        Args:
            client: Independently restored authenticated business session.
            account: Controlled owner or ordinary test inviter.
            target_nim: Preflight-verified transport identity of the peer test bot.
            output: Private acceptance directory, not a production data directory.

        Raises:
            ProtocolError: If the actor or target identity is not bounded.
        """
        if account not in {OWNER, ORDINARY} or not target_nim:
            raise ProtocolError("test_membership_scope")
        self.client, self.account, self.target_nim = client, account, target_nim
        self.path = output / "membership.sqlite3"
        self.last_request = 0
        self.sending = ""
        self.lock = asyncio.Lock()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts("
                "action TEXT PRIMARY KEY,account TEXT NOT NULL,"
                "state TEXT NOT NULL,created REAL NOT NULL)"
            )
        self.path.chmod(0o600)

    async def request(self, route, params):
        """Expose only exact test-group reads and a reserved single-target write.

        Args:
            route: Fixed test read or membership route.
            params: Exact group and target parameters.

        Returns:
            Decoded provider response.

        Raises:
            ProtocolError: If an unreserved or out-of-scope request is attempted.
        """

        def guard():
            if str(self.client.fields[3]) != self.account:
                raise ProtocolError("test_membership_identity")
            if route in {INVITE, REMOVE}:
                if params != {"groupId": int(GROUP), "groupMemberIds": [int(PEER)]}:
                    raise ProtocolError("test_membership_scope")
                expected = "invite" if route == INVITE else "kick"
                if self.sending != expected or (
                    route == REMOVE and self.account != OWNER
                ):
                    raise ProtocolError("test_membership_unreserved")
            elif route != "/v1/group/get-group-members" or (
                set(params) - {"groupId", "v", "cursor"}
                or type(params.get("groupId")) is not int
                or params["groupId"] != int(GROUP)
                or params.get("v") != "0"
            ):
                raise ProtocolError("test_membership_scope")

        guard()
        await asyncio.sleep(max(0, self.last_request + 3 - time.monotonic()))
        guard()
        self.last_request = time.monotonic()
        # The production route whitelist deliberately does not include INVITE.
        exchange = _exchange_business if route == INVITE else request_business
        return await exchange(
            self.client, route, copy.deepcopy(params), authorize=guard
        )

    async def change(self, action, *, group=GROUP, member=PEER, recipient=None):
        """Verify current native roles, reserve once, execute and verify membership.

        Args:
            action: Exactly kick or invite.
            group: Must be the fixed authorized test group.
            member: Must be the verified peer test bot, never a human or owner.
            recipient: Controlled peer session for accepting a pending invitation.

        Returns:
            Verified operation evidence without credentials or applicant profiles.

        Raises:
            ProtocolError: On scope mismatch, stale membership, replay or uncertainty.
        """
        if (
            not isinstance(action, str)
            or action not in {"kick", "invite"}
            or group != GROUP
            or member != PEER
            or (action == "kick" and self.account != OWNER)
        ):
            raise ProtocolError("test_membership_scope")
        async with self.lock:
            with closing(sqlite3.connect(self.path)) as db:
                prior = db.execute(
                    "SELECT action,state,created FROM attempts"
                ).fetchall()
            if any(row[0] == action or row[1] != "verified" for row in prior):
                raise ProtocolError("test_membership_no_replay")
            if action == "invite" and not any(row[0] == "kick" for row in prior):
                raise ProtocolError("test_membership_missing_removal")
            await asyncio.sleep(
                max(0, max((row[2] for row in prior), default=0) + 30 - time.time())
            )
            roster = await member_directory(self, GROUP)
            members = {str(row["userId"]): row for row in roster["groupMemberInfo"]}
            expected = (
                {OWNER, PEER, ORDINARY} if action == "kick" else {OWNER, ORDINARY}
            )
            if (
                set(members) != expected
                or members[OWNER].get("groupRole") != "GROUP_ROLE_OWNER"
                or members[ORDINARY].get("groupRole") != "GROUP_ROLE_MEMBER"
                or any(
                    row.get("accountState") != "ACCOUNT_STATE_GOOD"
                    for row in members.values()
                )
                or action == "kick"
                and (
                    members[PEER].get("groupRole") != "GROUP_ROLE_MEMBER"
                    or members[PEER].get("nimId") != self.target_nim
                )
            ):
                raise ProtocolError("test_membership_native_scope")
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT action,state,created FROM attempts"
                ).fetchall()
                if current != prior:
                    raise ProtocolError("test_membership_concurrent")
                db.execute(
                    "INSERT INTO attempts VALUES(?,?,'unknown',?)",
                    (action, self.account, time.time()),
                )
            self.sending = action
            try:
                await self.request(
                    INVITE if action == "invite" else REMOVE,
                    {"groupId": int(GROUP), "groupMemberIds": [int(PEER)]},
                )
                verified = await member_directory(self, GROUP)
                members = {
                    str(row["userId"]): row for row in verified["groupMemberInfo"]
                }
                expected = (
                    {OWNER, ORDINARY} if action == "kick" else {OWNER, PEER, ORDINARY}
                )
                if action == "invite" and recipient:
                    return await self.finish_invitation(recipient)
                if (
                    set(members) != expected
                    or action == "invite"
                    and (
                        members[PEER].get("nimId") != self.target_nim
                        or members[PEER].get("groupRole") != "GROUP_ROLE_MEMBER"
                        or members[PEER].get("accountState") != "ACCOUNT_STATE_GOOD"
                    )
                ):
                    raise ProtocolError("test_membership_result_unknown")
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(
                        "UPDATE attempts SET state='verified' WHERE action=?", (action,)
                    )
                return {
                    "action": action,
                    "account": self.account,
                    "group": GROUP,
                    "member": PEER,
                    "state": "verified",
                }
            finally:
                self.sending = ""

    async def finish_invitation(self, recipient):
        """Accept only the already-recorded test invitation, never reinvite.

        Args:
            recipient: Authenticated session of the exact peer test bot.

        Returns:
            Verified membership evidence.

        Raises:
            ProtocolError: If invitation evidence, recipient or prior intent is ambiguous.
        """
        with closing(sqlite3.connect(self.path)) as db:
            prior = db.execute(
                "SELECT account,state,created FROM attempts WHERE action='invite'"
            ).fetchone()
        if not prior or prior[0] != self.account or str(recipient.fields[3]) != PEER:
            raise ProtocolError("test_invitation_binding")
        if prior[1] == "verified":
            return {
                "action": "invite",
                "account": self.account,
                "group": GROUP,
                "member": PEER,
                "state": "verified",
            }
        if prior[1] != "unknown":
            raise ProtocolError("test_invitation_state")

        def guard():
            if (
                str(recipient.fields[3]) != PEER
                or str(self.client.fields[3]) != self.account
            ):
                raise ProtocolError("test_invitation_binding")

        async def invitation():
            """Read the single fresh test invitation from signed provider records.

            Returns:
                Exact provider invitation record.

            Raises:
                ProtocolError: If source, target, time or record identity is ambiguous.
            """
            await asyncio.sleep(max(0, self.last_request + 3 - time.monotonic()))
            guard()
            self.last_request = time.monotonic()
            result = await request_business(
                recipient, "/v1/group/get-apply-logs", {}, authorize=guard
            )
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("list"), list)
                or len(result["list"]) > 4096
            ):
                raise ProtocolError("test_invitation_records")
            matching = []
            for row in result["list"]:
                if not isinstance(row, dict):
                    continue
                applicant, inviter = (
                    row.get("applicant") or {},
                    row.get("inviter") or {},
                )
                if not isinstance(applicant, dict) or not isinstance(inviter, dict):
                    continue
                if (
                    str(row.get("groupId")) != GROUP
                    or str(applicant.get("userId")) != PEER
                    or str(applicant.get("nimId")) != self.target_nim
                    or str(inviter.get("userId")) != self.account
                    or row.get("groupApplyType") != "INVITATION"
                ):
                    continue
                identity = row.get("applyId")
                if (
                    not isinstance(identity, str)
                    or not identity.isascii()
                    or not identity.isdigit()
                    or not 0 < int(identity) < 1 << 63
                ):
                    raise ProtocolError("test_invitation_records")
                try:
                    stamp = datetime.fromisoformat(
                        str(row["applyTime"]).replace("Z", "+00:00")
                    )
                    if stamp.tzinfo is None:
                        raise ValueError
                except (KeyError, ValueError):
                    raise ProtocolError("test_invitation_time") from None
                if prior[2] - 5 <= stamp.timestamp() <= time.time() + 5:
                    matching.append(row)
            if len(matching) != 1 or matching[0].get("state") not in {
                "MEMBER_STATE_INVITED",
                "MEMBER_STATE_GOOD",
            }:
                raise ProtocolError("test_invitation_unproven")
            return matching[0]

        row = await invitation()
        if row["state"] == "MEMBER_STATE_INVITED":
            with closing(sqlite3.connect(self.path)) as db:
                if db.execute(
                    "SELECT 1 FROM attempts WHERE action='accept'"
                ).fetchone():
                    raise ProtocolError("test_invitation_no_replay")
            await asyncio.sleep(max(0, prior[2] + 30 - time.time()))
            latest = await invitation()
            if (
                latest["applyId"] != row["applyId"]
                or latest["state"] != "MEMBER_STATE_INVITED"
            ):
                raise ProtocolError("test_invitation_changed")
            roster = await member_directory(self, GROUP)
            members = {str(r["userId"]): r for r in roster["groupMemberInfo"]}
            if (
                set(members) != {OWNER, ORDINARY}
                or members[OWNER].get("groupRole") != "GROUP_ROLE_OWNER"
                or members[ORDINARY].get("groupRole") != "GROUP_ROLE_MEMBER"
            ):
                raise ProtocolError("test_invitation_native_scope")
            with closing(sqlite3.connect(self.path)) as db, db:
                try:
                    db.execute(
                        "INSERT INTO attempts VALUES('accept',?,'unknown',?)",
                        (PEER, time.time()),
                    )
                except sqlite3.IntegrityError:
                    raise ProtocolError("test_invitation_no_replay") from None
            await asyncio.sleep(max(0, self.last_request + 3 - time.monotonic()))
            guard()
            self.last_request = time.monotonic()
            await _exchange_business(
                recipient,
                ACCEPT,
                {"applyId": row["applyId"], "agree": True},
                authorize=guard,
            )
        for _ in range(10):
            roster = await member_directory(self, GROUP)
            members = {str(r["userId"]): r for r in roster["groupMemberInfo"]}
            if set(members) == {OWNER, PEER, ORDINARY}:
                break
        else:
            raise ProtocolError("test_invitation_result_unknown")
        if (
            members[PEER].get("nimId") != self.target_nim
            or members[PEER].get("groupRole") != "GROUP_ROLE_MEMBER"
            or members[PEER].get("accountState") != "ACCOUNT_STATE_GOOD"
        ):
            raise ProtocolError("test_invitation_target_changed")
        verified = await invitation()
        if (
            verified["applyId"] != row["applyId"]
            or verified["state"] != "MEMBER_STATE_GOOD"
        ):
            raise ProtocolError("test_invitation_result_unknown")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE attempts SET state='verified' WHERE action IN ('invite','accept')"
            )
        return {
            "action": "invite",
            "account": self.account,
            "group": GROUP,
            "member": PEER,
            "state": "verified",
            "accepted_invitation": row["applyId"],
        }
