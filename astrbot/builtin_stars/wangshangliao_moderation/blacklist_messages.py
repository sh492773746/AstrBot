"""Contain fresh blacklist messages without inventing admission or mute evidence."""

import asyncio
import sqlite3

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.blacklist import (
    BlacklistStore,
    protected_target,
)
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .open_store import OpenError


async def contain(service, event):
    """Recall a currently blacklisted sender's fresh, original group message.

    Args:
        service: Receiving account's active moderation service.
        event: Original authenticated event, already checked for freshness.

    Returns:
        Whether the message is claimed by blacklist handling, including failures.
    """
    adapter = event.platform
    account, group = str(adapter.account), event.get_group_id()
    payload = event.get_extra("wangshangliao_payload") or {}
    member, route = payload.get("sender"), payload.get("recall_route")
    identity = getattr(event, "actor_identity", None)
    tenant = dict(service.tenants.get((account, group), {}))
    if (
        event.is_private_chat()
        or not tenant.get("active")
        or not tenant.get("present")
        or tenant.get("paused")
        or service.settings["paused"]
        or payload.get("_audit_incomplete")
        or group in getattr(getattr(adapter, "ledger", None), "review_groups", ())
        or not isinstance(member, str)
        or member != str(event.get_sender_id())
        or not isinstance(identity, ActorIdentity)
        or (identity.account, identity.claimed_uid) != (account, member)
        or not member.isascii()
        or not member.isdecimal()
        or not isinstance(route, dict)
        or not isinstance(route.get("peer"), str)
        or identity.peer != route["peer"]
        or adapter.members.get(group, {}).get(member) != route["peer"]
        or payload.get("_open_revision") != group_revision(adapter.config, group)
        or protected_target(adapter, group, member, route["peer"])
    ):
        return False
    path = BlacklistStore(adapter.config["id"]).path
    if not await asyncio.to_thread(path.is_file):
        return False
    frozen = account_guard(adapter)
    claimed = False
    message = str(event.message_obj.message_id)
    try:
        async with AsyncDatabase(
            lambda: sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
            ),
            guard=frozen,
        ) as db:
            sql = (
                "SELECT id,peer,actor,actor_peer,version FROM blacklist_entries "
                "WHERE account=? AND group_id=? AND member=? AND status='active'"
            )
            parameters = (account, group, member)
            entry = await db.fetchone(sql, parameters)
            if not entry or entry[1] != route["peer"]:
                return False
            service.membership_wakeup.set()
            service.member_blacklist.tick(urgent=(account, group))
            task = service.member_blacklist.tasks.get((account, group))
            if task is not None and task is not asyncio.current_task():
                # Never cancel a submitted mute merely to start message cleanup.
                await asyncio.wait((task,), timeout=8)
            members = await service.roster(adapter, group)
            if members.get(member, {}).get("groupRole") in {
                "GROUP_ROLE_OWNER",
                "GROUP_ROLE_ADMIN",
            }:
                return False
            claimed = True
            event.set_extra("_component_claimed", True)
            event.stop_event()
            base = service.write_guard(
                adapter, group=group, tenant=tenant, members=members, control=True
            )

            def guard():
                base()
                current = service.tenants.get((account, group), {})
                latest = service.member_evidence.get((account, group), {})
                snapshot = getattr(adapter, "member_snapshots", {}).get(group)
                if snapshot and snapshot[0] == account and snapshot[2].get("complete"):
                    latest = {
                        str(row["userId"]): row
                        for row in snapshot[2]["groupMemberInfo"]
                    }
                target, creator = latest.get(member, {}), latest.get(entry[2], {})
                if (
                    service.settings["paused"]
                    or current.get("paused")
                    or not adapter.config.get("moderation", {}).get(
                        "automation_enabled"
                    )
                    or target.get("groupRole") != "GROUP_ROLE_MEMBER"
                    or str(target.get("nimId", "")) != entry[1]
                    or protected_target(adapter, group, member, entry[1])
                    or not entry[2].startswith("dashboard/")
                    and (
                        entry[2] not in current.get("managers", [])
                        or str(creator.get("nimId", "")) != entry[3]
                        or creator.get("groupRole")
                        not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
                    )
                ):
                    raise OpenError("blacklist_message_authority_changed")
                authorize_action(adapter.config, group, "recall")

            async def authorize():
                guard()
                if await db.fetchone(sql, parameters) != entry:
                    raise OpenError("blacklist_message_entry_changed")
                guard()

            await authorize()
            result = await adapter.execute_action(
                ActionRequest.bind(
                    adapter,
                    f"recall/{account}/{group}/{message}",
                    "recall",
                    int(group),
                    member=int(member),
                    expected_nim=entry[1],
                    message=message,
                ),
                authorize=authorize,
            )
            status = result.get("status", "unknown")
            adapter.diagnostics.emit(
                "blacklist_message",
                "recalled" if status in {"accepted", "verified"} else status,
                message,
                group=group,
                failed=status not in {"accepted", "verified"},
                aggregate=True,
            )
    except (OpenError, ProtocolError, OSError, sqlite3.Error) as exc:
        adapter.diagnostics.emit(
            "blacklist_message",
            "needs_review",
            message,
            group=group,
            failed=True,
            error=str(exc)[:128],
            aggregate=True,
        )
    return claimed
