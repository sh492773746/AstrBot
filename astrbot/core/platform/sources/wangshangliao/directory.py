"""Validated group directory with explicit permission evidence."""

import time

from .wire import ProtocolError


async def member_directory(client, group: str, *, require_mapping: bool = True) -> dict:
    """Collect the member roster without publishing partial identity mappings.

    Args:
        client: Authenticated business client.
        group: Business group ID.
        require_mapping: Require a transport identity for message or action execution.

    Returns:
        Deduplicated members and pagination completeness.

    Raises:
        ProtocolError: If pagination or member identities are inconsistent.
    """
    members, peers, visited = {}, {}, set()
    cursor = ""
    for _ in range(100):
        page = await client.request(
            "/v1/group/get-group-members",
            {"groupId": int(group), "v": "0", **({"cursor": cursor} if cursor else {})},
        )
        if not isinstance(page, dict) or not isinstance(
            page.get("groupMemberInfo"), list
        ):
            raise ProtocolError("members_shape")
        for member in page["groupMemberInfo"]:
            if not isinstance(member, dict):
                raise ProtocolError("member_identity")
            uid, peer = str(member.get("userId", "")), member.get("nimId")
            if not require_mapping and peer is None:
                peer = ""
            if type(peer) is int and peer > 0:
                peer = str(peer)
            if (
                not uid.isascii()
                or not uid.isdigit()
                or int(uid) <= 0
                or not isinstance(peer, str)
                or (require_mapping and not peer.strip())
                or len(peer.encode()) > 1024
                or (peer and peer in peers and peers[peer] != uid)
            ):
                raise ProtocolError("member_identity")
            normalized = {**member, "userId": uid, "nimId": peer}
            if uid in members and any(
                members[uid].get(k) != normalized.get(k)
                for k in ("nimId", "groupRole", "role")
            ):
                raise ProtocolError("member_identity")
            members[uid], peers[peer] = normalized, uid
        info = page.get("pageInfo") or {}
        if not isinstance(info, dict):
            raise ProtocolError("member_cursor")
        next_cursor = (
            page.get("nextCursor")
            or page.get("next_cursor")
            or page.get("nextPageToken")
            or info.get("nextCursor")
            or info.get("nextPageToken")
        )
        if not next_cursor:
            if page.get("hasMore") or info.get("hasMore"):
                raise ProtocolError("member_pagination_incomplete")
            return {
                "groupMemberInfo": list(members.values()),
                "complete": True,
                "next_cursor": None,
            }
        if not isinstance(next_cursor, str) or len(next_cursor) > 8192:
            raise ProtocolError("member_cursor")
        if next_cursor in visited:
            raise ProtocolError("member_pagination")
        visited.add(next_cursor)
        cursor = next_cursor
    raise ProtocolError("member_limit")


async def group_directory(client, account: str) -> dict:
    """Read membership and resolve the current account's group role.

    Args:
        client: Authenticated business client with serialized HTTP requests.
        account: Stable business account identity.

    Returns:
        Group entries and permission evidence, without authentication material.

    Raises:
        ProtocolError: If the group directory cannot be decoded or authenticated.
    """
    data = await client.request("/v1/group/get-group-list", {"v": "0"})
    if not isinstance(data, dict) or any(
        not isinstance(data.get(k), list) for k in ("owner", "member")
    ):
        raise ProtocolError("groups_shape")
    groups = {}
    for entry in data["owner"] + data["member"]:
        if not isinstance(entry, dict):
            raise ProtocolError("groups_shape")
        identity = str(entry.get("groupId", ""))
        if not identity.isascii() or not identity.isdigit() or int(identity) <= 0:
            raise ProtocolError("group_identity")
        owner = entry.get(
            "ownerUserId", entry.get("groupOwnerId", entry.get("ownerId"))
        )
        role = "owner" if str(owner) == account else "unknown"
        me = entry.get("me")
        listed_role = (
            {
                "GROUP_ROLE_OWNER": "owner",
                "GROUP_ROLE_ADMIN": "admin",
                "GROUP_ROLE_MEMBER": "member",
            }.get(me.get("role"))
            if isinstance(me, dict)
            else None
        )
        conflict = role == "owner" and listed_role not in (None, "owner")
        if listed_role:
            role = listed_role
        if conflict:
            role = "unknown"
        groups[identity] = {
            "id": identity,
            "name": str(entry.get("groupName") or entry.get("name") or identity)[:256],
            "role": role,
            "role_source": "group_list_me"
            if listed_role
            else ("group_owner" if role == "owner" else "unavailable"),
            "role_reason": "role_conflict"
            if conflict
            else ("" if role != "unknown" else "member_not_found"),
        }
    # Group-list membership does not prove an administrator role. Resolve only
    # the logged-in user's member record; partial rosters stay unknown.
    for identity, group in groups.items():
        if group["role"] != "unknown" or group["role_reason"] == "role_conflict":
            continue
        try:
            page = await member_directory(client, identity, require_mapping=False)
            for member in page["groupMemberInfo"]:
                if not isinstance(member, dict) or str(member.get("userId")) != account:
                    continue
                raw = (
                    str(
                        next(
                            (
                                member[k]
                                for k in (
                                    "groupRole",
                                    "role",
                                    "identity",
                                    "memberRole",
                                    "type",
                                )
                                if k in member
                            ),
                            "",
                        )
                    )
                    .strip()
                    .upper()
                )
                if raw in {
                    "1",
                    "OWNER",
                    "MASTER",
                    "GROUP_OWNER",
                    "GROUP_ROLE_OWNER",
                    "MSG_MASTER",
                }:
                    group["role"] = "owner"
                elif raw in {
                    "2",
                    "ADMIN",
                    "MANAGER",
                    "ADMINISTRATOR",
                    "GROUP_ADMIN",
                    "GROUP_ROLE_ADMIN",
                    "MSG_ADMIN",
                }:
                    group["role"] = "admin"
                elif raw in {"MEMBER", "GROUP_MEMBER", "GROUP_ROLE_MEMBER"}:
                    group["role"] = "member"
                group["role_reason"] = (
                    "role_unrecognized" if group["role"] == "unknown" else ""
                )
                group["role_source"] = (
                    "member_record" if group["role"] != "unknown" else "unavailable"
                )
                break
        except ProtocolError as exc:
            group["role_reason"] = "member_request_failed"
            if str(exc) in {"reauth_required", "rate_limited"}:
                raise
    return {
        "groups": list(groups.values()),
        "source": "platform",
        "updated_at": int(time.time()),
        "complete": False,
        "incomplete_reason": "platform_pagination_not_verified",
    }


def member_card(member: dict) -> str:
    """Read the raw group card without substituting the account nickname.

    Args:
        member: Verified platform member record.

    Returns:
        Exact card string, or an empty string for an unset card.
    """
    for key in ("groupMemberNick", "nick", "groupNick", "cardName"):
        value = member.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""
