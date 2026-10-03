"""Fixed moderation operations for explicit human requests, not AI tools."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing

from .business import BusinessClient
from .diagnostics import Diagnostics
from .directory import member_card, member_directory
from .event import is_managed_account
from .storage import instance_dir
from .wire import ProtocolError


async def execute(
    client: BusinessClient,
    instance: str,
    account: str,
    operation: str,
    action: str,
    group: int,
    member: int = 0,
    text: str = "",
    *,
    minutes: int = 1,
    authorize: Callable[[], None] | None = None,
    expected_card: str | None = None,
    expected_nim: str | None = None,
) -> dict:
    """Execute a human-confirmed action once and retain ambiguous outcomes.

    Args:
        client: Authenticated business client.
        instance: Saved bot instance ID.
        account: Expected business account ID.
        operation: Stable operation ID; reuse it when querying a previous attempt.
        action: Fixed moderation action name.
        group: Positive business group ID.
        member: Member ID for member actions.
        text: Announcement content or new group card.
        minutes: Mute duration in integer minutes, bounded to one day.
        expected_card: Exact previewed group card, required for rename.
        expected_nim: Verified preview transport identity, required for rename.
        authorize: Revalidate current local authorization after remote lookups.

    Returns:
        Persisted operation result, without authentication material.

    Raises:
        ProtocolError: If identity, role, arguments or operation ID conflict.
    """
    if not operation or len(operation) > 128 or type(group) is not int or group <= 0:
        raise ProtocolError("moderation_arguments")
    if str(client.fields[3]) != account:
        raise ProtocolError("moderation_identity")
    if action == "mute" and (type(minutes) is not int or not 1 <= minutes <= 1440):
        raise ProtocolError("moderation_minutes")
    routes = {
        "cleanup": ("/v1/group/remove-group-member", {"groupMemberIds": [member]}),
        "rename": ("/v1/group/set-member-nickname", {"userId": member, "nick": text}),
        "kick": ("/v1/group/remove-group-member", {"groupMemberIds": [member]}),
        "mute": ("/v1/group/set-member-mute", {"userId": member, "min": minutes}),
        "unmute": ("/v1/group/member-mute-cancel", {"userId": member}),
        "mute_all": ("/v1/group/set-group-mute", {"muteMode": "MUTE_MEMBER"}),
        "unmute_all": ("/v1/group/set-group-mute", {"muteMode": "MUTE_NO"}),
        "announce": (
            "/v1/group/add-notice",
            {"noticeContent": text, "noticeMode": "COMMON_NOTICE"},
        ),
    }
    if action not in routes or (
        action in {"mute", "unmute", "kick", "rename", "cleanup"}
        and (type(member) is not int or member <= 0)
    ):
        raise ProtocolError("moderation_arguments")
    if action == "announce" and (not text.strip() or len(text) > 2000):
        raise ProtocolError("moderation_arguments")
    if action == "rename" and (
        not text.strip()
        or len(text.encode()) > 256
        or expected_card is None
        or not expected_nim
        or str(member) == account
        or is_managed_account(str(member))
    ):
        raise ProtocolError("moderation_target")
    route, params = routes[action]
    params = {"groupId": group, **params}
    digest = hashlib.sha256(
        json.dumps(
            [account, route, params, expected_card, expected_nim], sort_keys=True
        ).encode()
    ).hexdigest()
    root = instance_dir(instance)
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "moderation.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL)"
        )
        row = db.execute(
            "SELECT digest,result FROM operations WHERE id=?", (operation,)
        ).fetchone()
        if row:
            if row[0] != digest:
                raise ProtocolError("moderation_operation_conflict")
            return json.loads(row[1])
    groups = await client.request("/v1/group/get-group-list", {"v": "0"})
    target = next(
        (
            g
            for g in groups.get("owner", []) + groups.get("member", [])
            if str(g.get("groupId")) == str(group)
        ),
        None,
    )
    role = (target or {}).get("me", {}).get("role")
    if role not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}:
        raise ProtocolError("moderation_permission")
    if action in {"mute", "unmute", "kick", "rename", "cleanup"}:
        page = await member_directory(client, str(group))
        matches = [
            m
            for m in page.get("groupMemberInfo", [])
            if str(m.get("userId")) == str(member)
        ]
        if (
            len(matches) != 1
            or matches[0].get("groupRole") != "GROUP_ROLE_MEMBER"
            or not matches[0].get("nimId")
        ):
            raise ProtocolError("moderation_target")
        if action == "cleanup" and (
            matches[0].get("accountState")
            not in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
            or str(matches[0]["nimId"]) != expected_nim
            or str(member) == account
            or is_managed_account(str(member))
        ):
            raise ProtocolError("cleanup_target_changed")
        if (
            action == "kick"
            and operation.startswith("automatic-kick/")
            and (not expected_nim or str(matches[0]["nimId"]) != expected_nim)
        ):
            raise ProtocolError("automatic_kick_identity")
        if action == "rename" and matches[0].get("accountState") in {
            "ACCOUNT_STATE_BAN",
            "ACCOUNT_STATUS_CANCELLED",
        }:
            raise ProtocolError("card_account_unavailable")
        if action == "rename" and (
            str(matches[0]["nimId"]) != expected_nim
            or member_card(matches[0]) != expected_card
        ):
            raise ProtocolError("card_preview_stale")
    if authorize is not None:
        authorize()
    result = {
        "operation": operation,
        "action": action,
        "group": group,
        "member": member,
        "status": "unknown",
    }
    if action == "mute":
        result["minutes"] = minutes
    # Commit before the network write: interruption must never trigger a blind retry.
    with closing(sqlite3.connect(root / "moderation.sqlite3")) as db, db:
        try:
            db.execute(
                "INSERT INTO operations VALUES(?,?,?)",
                (operation, digest, json.dumps(result)),
            )
        except sqlite3.IntegrityError:
            raise ProtocolError("moderation_operation_busy") from None
    try:
        receipt = await client.request(route, params)
        result["status"] = "accepted"
        if action == "cleanup" or (
            action == "kick" and operation.startswith("automatic-kick/")
        ):
            roster = await member_directory(client, str(group))
            if not any(
                str(m["userId"]) == str(member) for m in roster["groupMemberInfo"]
            ):
                result["status"] = "verified"
        if action == "rename":
            roster = await member_directory(client, str(group))
            current = [
                m for m in roster["groupMemberInfo"] if str(m["userId"]) == str(member)
            ]
            if (
                len(current) == 1
                and str(current[0]["nimId"]) == expected_nim
                and member_card(current[0]) == text
            ):
                result["status"] = "verified"
        if action == "announce":
            notice_id = str(receipt.get("noticeId", receipt.get("id", "")))
            notices = await client.request(
                "/v1/group/notice-list", {"groupId": group, "v": "0"}
            )
            if notice_id and any(
                str(n.get("noticeId", n.get("id", ""))) == notice_id
                and n.get("noticeContent", n.get("content")) == text
                for n in notices.get("noticeInfoList", [])
            ):
                result.update(status="verified", notice_id=notice_id)
    except Exception:
        result["status"] = "unknown"
    with closing(sqlite3.connect(root / "moderation.sqlite3")) as db, db:
        db.execute(
            "UPDATE operations SET result=? WHERE id=?", (json.dumps(result), operation)
        )
    Diagnostics(instance).emit("moderation", result["status"], operation)
    return result
