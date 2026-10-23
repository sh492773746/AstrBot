"""Explicit server grants for a bounded, read-only private guidance trial."""

import asyncio
import hashlib
import json
import secrets
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .group_management import native_manager_ids
from .open_store import OpenError


class GuidanceTrialRequest(BaseModel):
    """Limit server grants to twenty calls or revocation, never a paid upgrade."""

    model_config = ConfigDict(extra="forbid", strict=True)
    account: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    uid: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    tenant_version: int = Field(ge=1)
    version: int = Field(ge=0)
    uses: Literal[0, 20]


class GuidanceTrialCommit(GuidanceTrialRequest):
    """Require a one-use preview for either granting or revoking a trial."""

    confirmation: str = Field(min_length=16, max_length=128)


async def provision_trial(rpc, call):
    """Bind a server-confirmed trial to current native manager authority.

    Args:
        rpc: Existing authenticated control dispatcher.
        call: Whitelisted server request, including exact tenant and grant versions.

    Returns:
        A confirmation preview or committed grant version.

    Raises:
        OpenError: If authority, identity, confirmation or versions have changed.
    """
    if not call.actor.startswith("dashboard/"):
        raise OpenError("server_required")
    if type(call.payload.get("uses")) is not int:
        raise OpenError("guidance_trial_invalid")
    commit = call.action == "guidance_trial"
    request = (GuidanceTrialCommit if commit else GuidanceTrialRequest).model_validate(
        call.payload
    )
    service = rpc.service
    account, group, uid = request.account, request.group_id, request.uid
    adapter = next((a for a in service.adapters() if str(a.account) == account), None)
    if adapter is None:
        raise OpenError("account_unavailable")
    tenant = await asyncio.to_thread(service.store.tenant, account, group)
    if not tenant or tenant["version"] != request.tenant_version:
        raise OpenError("tenant_changed")
    roster = await service.roster(adapter, group)
    authority = rpc.control_guard(adapter, tenant, roster)
    principal = "nim/" + str(roster.get(uid, {}).get("nimId", ""))
    settings = dict(service.settings)

    def guard():
        authority()
        current = service.member_evidence.get((account, group), {})
        if (
            not tenant["active"]
            or not tenant["present"]
            or tenant["paused"]
            or not tenant.get("rules_only")
            or uid not in tenant["managers"]
            or uid not in native_manager_ids(current.values(), account)
            or principal != "nim/" + str(current.get(uid, {}).get("nimId", ""))
            or not service.store.verified_actor(account, uid, principal[4:])
            or service.settings != settings
            or service.store.settings() != settings
            or settings["paused"]
        ):
            raise OpenError("guidance_trial_authority_changed")

    guard()
    target = json.dumps(
        {**request.model_dump(exclude={"confirmation"}), "principal": principal},
        sort_keys=True,
    )
    with service.store.connect(write=True, guard=guard) as db:
        old = db.execute(
            "SELECT * FROM guidance_trials WHERE account=? AND group_id=? AND uid=?",
            (account, group, uid),
        ).fetchone()
        if (old["version"] if old else 0) != request.version:
            raise OpenError("guidance_trial_changed")
        if old and (old["principal"], old["epoch"]) != (principal, tenant["epoch"]):
            raise OpenError("guidance_trial_identity_changed")
        if not commit:
            token = secrets.token_urlsafe(24)
            db.execute(
                "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    "guidance_trial",
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
            service.store.log(
                db,
                call.actor,
                account,
                group,
                "guidance_trial_preview",
                {"uid": uid, "uses": request.uses, "version": request.version},
            )
            return {"confirmation": token, "expires_in": 600, "uses": request.uses}
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
                "guidance_trial",
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
        db.execute(
            "INSERT INTO guidance_trials VALUES(?,?,?,?,?,?,20,?,?) "
            "ON CONFLICT(account,group_id,uid) DO UPDATE SET "
            "version=excluded.version,active=excluded.active",
            (
                account,
                group,
                uid,
                principal,
                tenant["epoch"],
                request.version + 1,
                int(request.uses > 0),
                old["created"] if old else time.time(),
            ),
        )
        service.store.log(
            db,
            call.actor,
            account,
            group,
            "guidance_trial_saved",
            {"uid": uid, "uses": request.uses, "version": request.version + 1},
        )
    return {"saved": True, "version": request.version + 1, "uses": request.uses}


def trial_access(store, account, identity, management, selected):
    """Read a stable grant snapshot without resetting or spending any calls.

    Args:
        store: Existing tenant store with durable grant records.
        account: Authenticated bot account.
        identity: Independently verified private sender.
        management: Current native-verified management scope.
        selected: Optional explicitly selected group.

    Returns:
        The exact live grant, or None when no matching ready scope exists.
    """
    with store.connect() as db:
        for row in management:
            if not row["ready"] or selected and selected != row["group"]:
                continue
            grant = db.execute(
                "SELECT * FROM guidance_trials WHERE account=? AND group_id=? "
                "AND uid=? AND principal=? AND epoch=? AND active=1",
                (
                    account,
                    row["group"],
                    identity.verified_uid,
                    identity.principal,
                    row["epoch"],
                ),
            ).fetchone()
            if grant:
                return dict(grant)
    return None
