"""Manually accept an explicit group invitation using Xiaoman's saved HTTP session."""

import argparse
import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import aiohttp

from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.business_http import _exchange_business
from astrbot.core.platform.sources.wangshangliao.invitations import (
    latest_membership_events,
)
from astrbot.core.platform.sources.wangshangliao.storage import Vault
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

ACCOUNT = "43506527"
INSTANCE = "wangshangliao_f79c5b81"
ACCEPT = "/v1/group/group-member-invite"
AUDIT = (
    Path(__file__).resolve().parents[1]
    / "data/platform_data/wangshangliao/manual-group-join"
)


def select_invitation(response: dict, group_number: str, nim_id: str) -> dict:
    """Resolve the newest self-addressed invitation for an exact display group number.

    Args:
        response: Signed account-visible application log response.
        group_number: User-specified display group number, not a business group ID.
        nim_id: Native identity bound to the saved account session.

    Returns:
        Unambiguous provider invitation, without session credentials.

    Raises:
        ProtocolError: If the recipient, group or newest event cannot be verified.
    """
    if not isinstance(response, dict) or not isinstance(response.get("list"), list):
        raise ProtocolError("join_invitation_records")
    if len(response["list"]) > 4096:
        raise ProtocolError("join_invitation_records")
    rows = [
        row
        for row in response["list"]
        if isinstance(row, dict)
        and str(row.get("groupAccount")) == group_number
        and isinstance(row.get("applicant"), dict)
        and str(row["applicant"].get("userId")) == ACCOUNT
    ]
    groups = {str(row.get("groupId", "")) for row in rows}
    if len(groups) != 1:
        raise ProtocolError("join_invitation_missing_or_ambiguous")
    group = groups.pop()
    if not group.isascii() or not group.isdigit() or int(group) <= 0:
        raise ProtocolError("join_group_identity")
    events = latest_membership_events({"list": rows}, group)
    event = events.get(ACCOUNT)
    if not event or event["ambiguous"]:
        raise ProtocolError("join_invitation_missing_or_ambiguous")
    row = event["row"]
    if (
        row.get("groupApplyType") != "INVITATION"
        or str(row["applicant"].get("nimId")) != nim_id
        or row.get("state") not in {"MEMBER_STATE_INVITED", "MEMBER_STATE_GOOD"}
        or not str(row.get("groupCloudId", "")).isascii()
        or not str(row.get("groupCloudId", "")).isdigit()
        or int(row["groupCloudId"]) <= 0
    ):
        raise ProtocolError("join_invitation_binding")
    return row


class ManualInvitation:
    """Accept only a confirmed self-invitation; retain unknown writes without retry."""

    def __init__(self, client, nim_id: str, audit: Path = AUDIT):
        self.client = client
        self.nim_id = nim_id
        self.audit = audit
        self.last_read = 0.0

    def guard(self):
        """Reject use with any other business account."""
        if str(self.client.fields[3]) != ACCOUNT:
            raise ProtocolError("join_account_binding")

    async def read(self, route: str, params: dict):
        """Pace the two reviewed membership reads without opening another IM session."""
        self.guard()
        if route not in {"/v1/group/get-group-list", "/v1/group/get-apply-logs"}:
            raise ProtocolError("join_read_scope")
        await asyncio.sleep(max(0, self.last_read + 3 - time.monotonic()))
        self.last_read = time.monotonic()
        return await self.client.request(route, params, authorize=self.guard)

    async def inspect(self, group_number: str) -> tuple[dict | None, dict | None]:
        """Read membership first, then resolve an invitation only if not already joined."""
        if (
            not isinstance(group_number, str)
            or not group_number.isascii()
            or not group_number.isdigit()
            or not 0 < int(group_number) < 1 << 63
        ):
            raise ProtocolError("join_group_number")
        listed = await self.read("/v1/group/get-group-list", {"v": "0"})
        if not isinstance(listed, dict) or any(
            not isinstance(listed.get(key), list) for key in ("owner", "member")
        ):
            raise ProtocolError("join_membership_shape")
        matches = [
            row
            for row in listed["owner"] + listed["member"]
            if isinstance(row, dict) and str(row.get("groupAccount")) == group_number
        ]
        if len(matches) > 1:
            raise ProtocolError("join_membership_ambiguous")
        if matches:
            return matches[0], None
        logs = await self.read("/v1/group/get-apply-logs", {"lastId": "0"})
        return None, select_invitation(logs, group_number, self.nim_id)

    async def accept(self, group_number: str, apply_id: str) -> dict:
        """Reserve a durable intent, send once, and verify joined membership separately."""
        member, row = await self.inspect(group_number)
        if member:
            return {"state": "already_joined", "group": member}
        if str(row["applyId"]) != apply_id or row["state"] != "MEMBER_STATE_INVITED":
            raise ProtocolError("join_invitation_changed")
        self.audit.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.audit / "attempts.sqlite3"
        if self.audit.is_symlink() or path.is_symlink():
            raise ProtocolError("join_audit_path")
        # The primary key also prevents concurrent tools from submitting the same invitation.
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts ("
                "account TEXT NOT NULL, apply_id TEXT NOT NULL, "
                "group_number TEXT NOT NULL, group_id TEXT NOT NULL, "
                "state TEXT NOT NULL, created REAL NOT NULL, detail TEXT NOT NULL, "
                "PRIMARY KEY(account,apply_id))"
            )
            path.chmod(0o600)
            try:
                db.execute(
                    "INSERT INTO attempts VALUES(?,?,?,?,?,?,?)",
                    (
                        ACCOUNT,
                        apply_id,
                        group_number,
                        str(row["groupId"]),
                        "unknown",
                        time.time(),
                        "",
                    ),
                )
            except sqlite3.IntegrityError:
                raise ProtocolError("join_no_replay_check_membership") from None

        def update(state, detail=""):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "UPDATE attempts SET state=?,detail=? WHERE account=? AND apply_id=?",
                    (state, detail, ACCOUNT, apply_id),
                )

        submission = {"started": False, "rejected": False}
        await asyncio.sleep(max(0, self.last_read + 3 - time.monotonic()))
        self.guard()
        try:
            # This route is deliberately unavailable to generic production action callers.
            await _exchange_business(
                self.client,
                ACCEPT,
                {"applyId": apply_id, "agree": True},
                authorize=self.guard,
                submission=submission,
            )
        except Exception as exc:
            state = (
                "rejected"
                if submission["rejected"]
                else "unknown"
                if submission["started"]
                else "not_submitted"
            )
            update(state, f"{type(exc).__name__}:{getattr(exc, 'number', 0)}")
            raise
        update("accepted")
        for _ in range(3):
            self.last_read = time.monotonic()
            member, _ = await self.inspect(group_number)
            if member:
                if str(member.get("groupId")) != str(row["groupId"]) or str(
                    member.get("groupCloudId")
                ) != str(row["groupCloudId"]):
                    raise ProtocolError("join_membership_binding")
                update("verified")
                return {"state": "verified", "group": member, "apply_id": apply_id}
        return {"state": "accepted_membership_pending", "apply_id": apply_id}


async def main():
    """Preview by default; accept only when the operator supplies the exact invitation ID."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True, help="Exact display group number")
    parser.add_argument("--accept", help="Exact invitation ID from the preview")
    args = parser.parse_args()
    saved = Vault(INSTANCE).load()
    if not saved or not saved.get("nim_id"):
        raise ProtocolError("join_session_missing")
    async with aiohttp.ClientSession() as http:
        client = BusinessClient(Deployment.load(), http)
        client.restore(saved["business"])
        worker = ManualInvitation(client, str(saved["nim_id"]))
        if args.accept:
            result = await worker.accept(args.group, args.accept)
        else:
            member, row = await worker.inspect(args.group)
            result = (
                {"state": "already_joined", "group": member}
                if member
                else {"state": "invited", "invitation": row}
            )
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
