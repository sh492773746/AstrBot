"""Explicit server admission for proactively joined, rules-only groups."""

import asyncio
import hashlib
import json
import secrets
import time

from pydantic import BaseModel, ConfigDict, Field

from .group_ai import DEFAULT
from .group_management import native_manager_ids
from .open_store import (
    DEFAULT_GROUP_RULES,
    DEFAULT_GROUP_TEMPLATE_VERSION,
    DEFAULT_RULES,
    OpenError,
)


class RulesProvision(BaseModel):
    """Bind admission to one discovered tenant and one native manager."""

    model_config = ConfigDict(extra="forbid", strict=True)
    account: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    version: int = Field(ge=1)
    manager: str = Field(pattern=r"^[1-9][0-9]{0,9}$")


class RulesProvisionCommit(RulesProvision):
    """Consume an exact, short-lived server preview."""

    confirmation: str = Field(min_length=16, max_length=128)


async def provision_rules(service, call):
    """Preview or commit rules admission without invitation or financial grants."""
    if not call.actor.startswith("dashboard/"):
        raise OpenError("server_required")
    commit = call.action == "rules_provision"
    request = (RulesProvisionCommit if commit else RulesProvision).model_validate(
        call.payload
    )
    account, group = request.account, request.group_id
    if (
        any(int(uid) >= 1 << 30 for uid in (account, group, request.manager))
        or request.manager == account
    ):
        raise OpenError("tenant_invalid")
    adapter = next((a for a in service.adapters() if str(a.account) == account), None)
    if adapter is None:
        raise OpenError("account_unavailable")
    authority = service.write_guard(adapter)
    authority()
    tenant = await asyncio.to_thread(service.store.tenant, account, group)
    if not tenant or tenant["version"] != request.version:
        raise OpenError("tenant_changed")
    roster = await service.roster(adapter, group)
    authority()
    settings = dict(service.settings)
    caller = asyncio.current_task()
    peers = {uid: str(row["nimId"]) for uid, row in roster.items()}
    manager = request.manager

    def guard():
        authority()
        current = service.store.tenant(account, group)
        latest = service.member_evidence.get((account, group), {})
        if (
            caller.cancelling()
            or current != tenant
            or not current["present"]
            or current["active"]
            or current["admission"] in {"rejected", "departed"}
            or current["cleanup_epoch"] > current["cleanup_done"]
            or settings["paused"]
            or service.settings != settings
            or service.store.settings() != settings
            or (account, group) in service.suspended_groups
            or (account, group) in service.store.membership_blocks()
            or service.membership_receiving.get((account, group))
            or service.reconciling_groups.get((account, group))
        ):
            raise OpenError("activation_changed")
        eligible = native_manager_ids(latest.values(), account)
        if (
            manager not in eligible
            or peers.get(account) != str(adapter.nim_account)
            or latest.get(account, {}).get("groupRole")
            not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
            or any(
                str(latest.get(uid, {}).get("nimId", "")) != peer
                for uid, peer in peers.items()
            )
            or not service.store.verified_actor(account, manager, peers[manager])
        ):
            raise OpenError("manager_native_role_required")

    guard()
    use_template = tenant["rules"] == DEFAULT_RULES
    rules = json.loads(
        json.dumps(DEFAULT_GROUP_RULES if use_template else tenant["rules"])
    )
    template_version = DEFAULT_GROUP_TEMPLATE_VERSION if use_template else None
    target = json.dumps(
        {
            "manager": manager,
            "principal": "nim/" + peers[manager],
            "mode": "rules_only",
            "rules": rules,
            "template_version": template_version,
        },
        sort_keys=True,
    )

    def save():
        with service.store.connect(write=True, guard=guard) as db:
            if (
                db.execute("SELECT COUNT(*) FROM tenants WHERE active=1").fetchone()[0]
                >= 20
            ):
                raise OpenError("tenant_limit")
            if not commit:
                token = secrets.token_urlsafe(24)
                db.execute(
                    "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        "rules_provision",
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
                    "rules_service_preview",
                    {"manager": manager, "version": request.version},
                )
                return {"confirmation": token, "expires_in": 600, "mode": "rules_only"}
            digest = hashlib.sha256(request.confirmation.encode()).hexdigest()
            capability = db.execute(
                "SELECT * FROM capabilities WHERE digest=?", (digest,)
            ).fetchone()
            if (
                not capability
                or capability["used"]
                or capability["expires"] <= time.time()
                or (
                    capability["kind"],
                    capability["account"],
                    capability["group_id"],
                    capability["epoch"],
                    capability["version"],
                    capability["actor"],
                    capability["target"],
                )
                != (
                    "rules_provision",
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
            db.execute(
                "UPDATE tenants SET active=1,paused=0,auto_claim=0,admission='verified',"
                "rules_only=1,managers=?,rules=?,version=version+1 WHERE account=? AND group_id=?",
                (json.dumps([manager]), json.dumps(rules), account, group),
            )
            old = db.execute(
                "SELECT config,version FROM group_ai WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            config = {**DEFAULT, **(json.loads(old["config"]) if old else {})}
            config.update(enabled=False, mentions=False, autonomous=False, idle=False)
            db.execute(
                "INSERT INTO group_ai(account,group_id,version,config) VALUES(?,?,?,?) "
                "ON CONFLICT(account,group_id) DO UPDATE SET "
                "version=excluded.version,config=excluded.config",
                (account, group, old["version"] + 1 if old else 1, json.dumps(config)),
            )
            service.store.log(
                db,
                call.actor,
                account,
                group,
                "rules_service_enabled",
                {
                    "manager": manager,
                    "principal": "nim/" + peers[manager],
                    "version": request.version + 1,
                    "mode": "rules_only",
                    "rules": rules,
                    "template_version": template_version,
                },
            )
            return {"saved": True, "mode": "rules_only"}

    result = await asyncio.to_thread(save)
    if commit:
        await service.refresh()
    return result
