"""Server-confirmed upgrades from rules-only admission without billing consent."""

import asyncio
import hashlib
import json
import secrets
import time

from pydantic import BaseModel, ConfigDict, Field

from .group_management import native_manager_ids
from .open_store import OpenError


class AIAdmissionRequest(BaseModel):
    """Select one current rules-only tenant, without accepting new privileges."""

    model_config = ConfigDict(extra="forbid", strict=True)
    account: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    version: int = Field(ge=1)


class AIAdmissionCommit(AIAdmissionRequest):
    """Require an actor-bound, single-use upgrade preview."""

    confirmation: str = Field(min_length=16, max_length=128)


async def upgrade_ai_admission(rpc, call):
    """Remove only the rules-only restriction after explicit server confirmation.

    Args:
        rpc: Authenticated integrated control dispatcher.
        call: Exact tenant version and optional one-use confirmation.

    Returns:
        A ten-minute preview or the committed tenant version.

    Raises:
        OpenError: If admission, membership, authority or confirmation changed.
    """
    if not call.actor.startswith("dashboard/"):
        raise OpenError("server_required")
    commit = call.action == "ai_admission"
    request = (AIAdmissionCommit if commit else AIAdmissionRequest).model_validate(
        call.payload
    )
    account, group = request.account, request.group_id
    if any(int(uid) >= 1 << 30 for uid in (account, group)):
        raise OpenError("tenant_invalid")
    service = rpc.service
    adapter = next((a for a in service.adapters() if str(a.account) == account), None)
    if adapter is None:
        raise OpenError("account_unavailable")
    frozen = service.write_guard(adapter)
    frozen()
    tenant = await asyncio.to_thread(service.store.tenant, account, group)
    frozen()
    if not tenant or tenant["version"] != request.version:
        raise OpenError("tenant_changed")
    roster = await service.roster(adapter, group)
    frozen()
    authority = rpc.control_guard(adapter, tenant, roster)
    settings = dict(service.settings)
    principals = {
        uid: str(roster.get(uid, {}).get("nimId", "")) for uid in tenant["managers"]
    }
    key = (account, group)

    def guard():
        frozen()
        authority()
        latest = service.member_evidence.get(key, {})
        if (
            not tenant["rules_only"]
            or tenant["paused"]
            or settings["paused"]
            or service.settings != settings
            or service.store.settings() != settings
            or service.reconciling_groups.get(key)
            or key in service.store.membership_blocks()
        ):
            raise OpenError("ai_admission_unavailable")
        eligible = native_manager_ids(latest.values(), account)
        if not principals or any(
            uid not in eligible
            or not peer
            or str(latest.get(uid, {}).get("nimId", "")) != peer
            or not service.store.verified_actor(account, uid, peer)
            for uid, peer in principals.items()
        ):
            raise OpenError("manager_native_role_required")

    guard()
    target = json.dumps(
        {
            "tenant": tenant,
            "principals": principals,
            "settings_version": settings["version"],
            "mode": "full",
        },
        sort_keys=True,
    )

    def save():
        with service.store.connect(write=True, guard=guard) as db:
            if not commit:
                token = secrets.token_urlsafe(24)
                db.execute(
                    "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        "ai_admission",
                        account,
                        group,
                        tenant["epoch"],
                        request.version,
                        call.actor,
                        "",
                        target,
                        "",
                        "",
                        time.time() + 600,
                    ),
                )
                service.store.log(
                    db,
                    call.actor,
                    account,
                    group,
                    "ai_admission_preview",
                    {"version": request.version},
                )
                return {"confirmation": token, "expires_in": 600, "mode": "full"}
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
                    "ai_admission",
                    account,
                    group,
                    tenant["epoch"],
                    request.version,
                    call.actor,
                    target,
                )
            ):
                raise OpenError("confirmation_invalid")
            db.execute("UPDATE capabilities SET used=1 WHERE digest=?", (digest,))
            if (
                db.execute(
                    "UPDATE tenants SET rules_only=0,version=version+1 "
                    "WHERE account=? AND group_id=? AND version=? AND epoch=? "
                    "AND active=1 AND present=1 AND rules_only=1",
                    (account, group, request.version, tenant["epoch"]),
                ).rowcount
                != 1
            ):
                raise OpenError("tenant_changed")
            # Inconsistent legacy switches must not start AI as a side effect.
            old = db.execute(
                "SELECT config FROM group_ai WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if old:
                config = json.loads(old["config"])
                switches = ("enabled", "mentions", "autonomous", "idle")
                if any(config.get(mode) for mode in switches):
                    config.update(dict.fromkeys(switches, False))
                    db.execute(
                        "UPDATE group_ai SET config=?,version=version+1 "
                        "WHERE account=? AND group_id=?",
                        (json.dumps(config), account, group),
                    )
            service.store.log(
                db,
                call.actor,
                account,
                group,
                "ai_admission_upgraded",
                {"version": request.version + 1, "mode": "full"},
            )
            return {"saved": True, "mode": "full", "version": request.version + 1}

    if commit:
        return await service.commit_tenant(account, group, save)
    return await asyncio.to_thread(save)
