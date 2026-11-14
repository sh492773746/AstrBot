"""Current platform mute state and single-member dashboard release."""

import asyncio
import hashlib
import json
import secrets
import sqlite3
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.blacklist import BlacklistStore
from astrbot.core.platform.sources.wangshangliao.directory import muted_member_ids
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .member_whitelist import member_name
from .open_store import OpenError


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,9}$")


class Read(Scope):
    operation: Literal["read"]


class Preview(Scope):
    operation: Literal["preview"]
    version: int = Field(ge=1)
    member: str = Field(pattern=r"^[1-9][0-9]{0,9}$")


class Commit(Preview):
    operation: Literal["commit"]
    confirmation: str = Field(min_length=16, max_length=128)


REQUEST = TypeAdapter(
    Annotated[Read | Preview | Commit, Field(discriminator="operation")]
)


async def release_member(
    adapter, group, member, peer, *, operation, actor, guard, store, version
):
    """Unmute one authorized member and end only the captured blacklist version.

    Args:
        adapter: Original receiving account.
        group: Authenticated business group.
        member: Verified business UID.
        peer: Verified native identity of that UID.
        operation: Single-use native operation identifier.
        actor: Verified administrator identity or dashboard principal.
        guard: Synchronous authority, lifecycle and target identity check.
        store: Blacklist store for the receiving instance.
        version: List version captured before submission or by the preview.

    Returns:
        Independent native receipt, verification and blacklist removal outcomes.

    Raises:
        OpenError: If the original authority is no longer valid.
        ProtocolError: If the blacklist changed before submission.
    """
    account = str(adapter.account)
    guard()
    current_version, entries = await asyncio.to_thread(store.read, account, group)
    guard()
    entry = next((row for row in entries if row["member"] == member), None)
    if current_version != version or entry and entry["peer"] != peer:
        raise ProtocolError("blacklist_version_changed")
    current = await muted_member_ids(
        adapter, group, authorize=guard, expected_member=(member, peer)
    )
    guard()

    async def authorize():
        guard()
        live_version, _ = await asyncio.to_thread(store.read, account, group)
        guard()
        if live_version != version:
            raise ProtocolError("blacklist_version_changed")
        live = await muted_member_ids(
            adapter, group, authorize=guard, expected_member=(member, peer)
        )
        guard()
        if member not in live:
            raise ProtocolError("member_no_longer_muted")

    verified = member not in current
    result = {"status": "already_unmuted", "submitted": False}
    if not verified:
        result = await adapter.execute_action(
            ActionRequest.bind(
                adapter,
                operation,
                "unmute",
                int(group),
                member=int(member),
                expected_nim=peer,
            ),
            authorize=authorize,
        )
        if result.get("status") in {"accepted", "verified"}:
            try:
                current = await muted_member_ids(
                    adapter, group, authorize=guard, expected_member=(member, peer)
                )
                guard()
                verified = member not in current
            except (ProtocolError, OpenError, OSError, TimeoutError):
                pass
    outcome = {
        **result,
        "verified": verified,
        "blacklist_status": "retained" if entry else "not_listed",
    }
    if not verified:
        return outcome
    try:
        guard()
        if entry:
            # The store checks this version atomically with removal, preserving re-adds.
            await asyncio.to_thread(
                store.set_member,
                account,
                group,
                member,
                peer,
                entry["name"],
                enabled=False,
                actor=actor,
                guard=guard,
                expected_version=version,
            )
            outcome["blacklist_status"] = "removed"
        else:
            live_version, _ = await asyncio.to_thread(store.read, account, group)
            guard()
            if live_version != version:
                outcome["blacklist_status"] = "changed"
    except (ProtocolError, OpenError):
        outcome["blacklist_status"] = "changed"
    except (sqlite3.Error, OSError, TimeoutError):
        outcome["blacklist_status"] = "failed"
    return outcome


async def member_mutes(rpc, call):
    """Read actual mutes or release one previewed, still-muted ordinary member."""
    if not call.actor.startswith("dashboard/"):
        raise OpenError("server_required")
    request = REQUEST.validate_python(call.payload)
    account, group = request.account, request.group_id
    if any(int(uid) >= 1 << 30 for uid in (account, group)):
        raise OpenError("tenant_invalid")
    service = rpc.service
    adapter = next((a for a in service.adapters() if str(a.account) == account), None)
    if adapter is None:
        raise OpenError("account_unavailable")
    frozen = service.write_guard(adapter)
    roster = await service.roster(adapter, group)
    frozen()
    tenant = await asyncio.to_thread(service.store.tenant, account, group)
    if not tenant or (
        request.operation != "read" and tenant["version"] != request.version
    ):
        raise OpenError("tenant_changed")
    authority = rpc.control_guard(adapter, tenant, roster)
    member = getattr(request, "member", "")
    peer = str(roster.get(member, {}).get("nimId", ""))

    def guard():
        frozen()
        authority()
        if member:
            latest = service.member_evidence.get((account, group), {}).get(member, {})
            if (
                not peer
                or str(latest.get("nimId", "")) != peer
                or latest.get("groupRole") != "GROUP_ROLE_MEMBER"
                or member == account
            ):
                raise OpenError("mute_member_changed")

    guard()
    muted = await muted_member_ids(
        adapter,
        group,
        authorize=guard,
        expected_member=(member, peer) if member else None,
    )
    guard()
    if request.operation == "read":
        return {
            "version": tenant["version"],
            "checked_at": time.time(),
            "members": [
                {
                    "id": uid,
                    "name": member_name(row),
                    "can_unmute": row.get("groupRole") == "GROUP_ROLE_MEMBER",
                }
                for uid, row in roster.items()
                if uid in muted
            ],
        }
    if member not in muted:
        raise OpenError("member_no_longer_muted")
    blacklist = BlacklistStore(adapter.config["id"])
    blacklist_version, entries = await asyncio.to_thread(blacklist.read, account, group)
    guard()
    entry = next((row for row in entries if row["member"] == member), None)
    target = json.dumps(
        {
            "tenant": tenant,
            "member": member,
            "peer": peer,
            "blacklist_version": blacklist_version,
        },
        sort_keys=True,
    )

    def reserve():
        with service.store.connect(write=True, guard=guard) as db:
            if request.operation == "preview":
                token = secrets.token_urlsafe(24)
                db.execute(
                    "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        "member_unmute",
                        account,
                        group,
                        tenant["epoch"],
                        tenant["version"],
                        call.actor,
                        "",
                        target,
                        "",
                        "",
                        time.time() + 600,
                    ),
                )
                return {
                    "confirmation": token,
                    "expires_in": 600,
                    "removes_blacklist": entry is not None,
                }
            digest = hashlib.sha256(request.confirmation.encode()).hexdigest()
            preview = db.execute(
                "SELECT * FROM capabilities WHERE digest=?", (digest,)
            ).fetchone()
            if (
                not preview
                or preview["used"]
                or preview["expires"] <= time.time()
                or (
                    preview["kind"],
                    preview["account"],
                    preview["group_id"],
                    preview["epoch"],
                    preview["version"],
                    preview["actor"],
                    preview["target"],
                )
                != (
                    "member_unmute",
                    account,
                    group,
                    tenant["epoch"],
                    tenant["version"],
                    call.actor,
                    target,
                )
            ):
                raise OpenError("confirmation_invalid")
            db.execute("UPDATE capabilities SET used=1 WHERE digest=?", (digest,))
            operation = "dashboard-unmute/" + digest
            service.store.log(
                db,
                call.actor,
                account,
                group,
                "member_unmute_submitting",
                {"member": member, "operation": operation},
            )
            return operation

    reserved = await asyncio.to_thread(reserve)
    if request.operation == "preview":
        return reserved

    result = await release_member(
        adapter,
        group,
        member,
        peer,
        operation=reserved,
        actor=call.actor,
        guard=guard,
        store=blacklist,
        version=blacklist_version,
    )

    def log_receipt():
        with service.store.connect(write=True) as db:
            service.store.log(
                db,
                call.actor,
                account,
                group,
                "member_unmute_receipt",
                {
                    "member": member,
                    "operation": reserved,
                    "status": result.get("status", "unknown"),
                    "verified": result["verified"],
                    "blacklist_status": result["blacklist_status"],
                },
            )

    await asyncio.to_thread(log_receipt)
    return {
        "operation": reserved,
        "status": result.get("status", "unknown"),
        "verified": result["verified"],
        "blacklist_status": result["blacklist_status"],
    }
