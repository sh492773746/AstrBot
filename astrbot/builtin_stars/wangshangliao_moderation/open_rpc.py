"""Allowlisted in-process control plane for the integrated public bot."""

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Literal

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from astrbot.api import logger
from astrbot.core.platform.sources.wangshangliao.database import AsyncDatabase
from astrbot.core.platform.sources.wangshangliao.identity import account_nickname
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .group_management import native_manager_ids
from .open_store import OpenError

SOCKET = "/run/astrbot-open/control.sock"


def require_group_call(call):
    """Keep profile selection and page schemas bound to group capabilities.

    Args:
        call: Validated control-plane request.

    Raises:
        OpenError: If a request selects a non-group profile or unknown page.
    """
    if (
        call.action == "ai_profile"
        and call.payload.get("kind") != "group"
        or call.action == "page_schema"
        and call.payload.get("name") != "business_rules"
    ):
        raise OpenError("group_control_scope")


class OpenCall(BaseModel):
    """Keep the bridge versioned and reject generic proxy or method invocation."""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    action: Literal[
        "status",
        "login",
        "login_save",
        "tenant",
        "tenant_preview",
        "rules_provision_preview",
        "rules_provision",
        "ai_admission_preview",
        "ai_admission",
        "tenant_members",
        "limits",
        "model",
        "ai_budget",
        "review_resume",
        "manager_quota",
        "group_ai",
        "ai_profile",
        "business_rules",
        "group_welcome",
        "member_whitelist",
        "member_blacklist",
        "raid_defense",
        "test_raid_preflight",
        "member_mutes",
        "message_recalls",
        "group_knowledge",
        "command_capabilities",
        "component_policy",
        "component_status",
        "component_recover",
        "page_schema",
        "billing_settings",
        "billing_price",
        "plan_preview",
        "plan_grant",
        "payer_nominate",
        "finance_status",
        "test_probe",
        "test_quotes",
        "test_lottery",
        "test_advertising",
        "test_flood",
        "test_blacklist",
        "test_group_ai",
    ]
    actor: str = Field(min_length=1, max_length=256)
    payload: dict = Field(default_factory=dict)


class OpenRPC:
    """Dispatch authenticated dashboard calls without a network proxy."""

    def __init__(self, service, path=None):
        self.service = service
        self.path = Path(path or SOCKET)
        self.runner = None
        self.inode = None
        self.lock = asyncio.Lock()
        self.test_quotes_lock = asyncio.Lock()

    def control_guard(self, adapter, tenant, members):
        """Freeze exact total-control authority across separate ledger stores.

        Args:
            adapter: Account selected before any authorization IO.
            tenant: Durable pre-mutation tenant snapshot.
            members: Fresh complete member directory for the exact target group.

        Returns:
            Admission and commit guard checking live and persisted authority.
        """
        expected = dict(tenant)
        check = self.service.write_guard(
            adapter,
            group=tenant["group_id"],
            tenant=tenant,
            members=members,
            control=True,
        )

        def verify():
            check()
            if (
                self.service.store.tenant(expected["account"], expected["group_id"])
                != expected
            ):
                raise OpenError("tenant_changed")

        return verify

    async def invoke(self, request):
        """Reject retired HTTP entry points without reading or executing a call."""
        return web.json_response(
            {
                "error": "isolated_mode_retired",
                "message": "独立开放服务已停用，请在 AstrBot 旺商聊管理页面操作。",
            },
            status=410,
        )

    async def dispatch(self, call):
        """Preserve the whitelist while governing only the server canary."""
        require_group_call(call)
        service = self.service
        governance = getattr(service, "governance", None)
        if governance is not None:
            if call.action == "component_status":
                from astrbot.core.star.component_runtime import component_runtime

                from .component_governance import PLUGIN

                if call.payload:
                    raise OpenError("component_payload_invalid")
                return {"entries": component_runtime.status(PLUGIN)}
            if call.action == "component_recover":
                from astrbot.core.star.component_runtime import (
                    RuntimeRejected,
                    component_runtime,
                )
                from astrbot.core.star.components import (
                    ComponentRecoveryRequest,
                    components,
                )

                from .component_governance import PLUGIN

                payload = ComponentRecoveryRequest.model_validate(call.payload)
                spec = components.specs.get(payload.capability)
                if spec is None or spec.plugin != PLUGIN:
                    raise OpenError("component_scope_invalid")
                try:
                    return await component_runtime.recover(
                        payload.capability,
                        payload.scope,
                        payload.version,
                        call.actor,
                        reset=payload.operation == "reset_after_review",
                        reason=payload.reason,
                    )
                except RuntimeRejected as exc:
                    raise OpenError(str(exc)) from None
                finally:
                    with service.store.connect(write=True) as db:
                        service.store.log(
                            db,
                            call.actor,
                            "",
                            "",
                            "component_recover",
                            {
                                "capability": payload.capability,
                                "scope": payload.scope,
                                "version": payload.version,
                                "operation": payload.operation,
                                "reason": payload.reason,
                            },
                        )
            if call.action == "component_policy":
                if not call.payload:
                    return governance.policy()
                if set(call.payload) != {"version", "enabled"}:
                    raise OpenError("component_pilot_invalid")
                return governance.configure(
                    call.payload["version"], call.payload["enabled"], call.actor
                )
            if call.action == "page_schema":
                if set(call.payload) != {"name"} or call.payload["name"] not in {
                    "business_rules",
                }:
                    raise OpenError("component_page_invalid")
                return governance.page(call.payload["name"])
            account, group = (
                call.payload.get("account"),
                call.payload.get("group_id", ""),
            )
            adapter = next(
                (a for a in service.adapters() if str(a.account) == account), None
            )
            if adapter is not None:
                return await governance.run(
                    "control", adapter, lambda: self._dispatch(call), group=group
                )
        return await self._dispatch(call)

    async def _dispatch(self, call):
        """Expose only explicit public-service administrative operations."""
        require_group_call(call)
        service, payload = self.service, call.payload
        if not service.integrated and call.action != "status":
            raise OpenError("isolated_mode_retired")
        if call.action == "test_blacklist":
            from .test_blacklist import control_blacklist

            return await control_blacklist(service, call.actor, payload)
        if call.action == "member_mutes":
            from .member_mutes import member_mutes

            return await member_mutes(self, call)
        if call.action == "raid_defense":
            return await service.raid_defense.control(self, call)
        if call.action == "test_raid_preflight":
            from .test_raid import preflight

            if call.payload:
                raise OpenError("raid_arguments")
            return await preflight(service, call.actor)
        if call.action == "message_recalls":
            return await service.message_recalls.handle(self, call)
        if call.action == "member_whitelist":
            from .member_whitelist import REQUEST

            data = REQUEST.validate_python(payload).model_dump()
            account, group = data.pop("account"), data.pop("group_id")
            adapter = next(
                (a for a in service.adapters() if str(a.account) == account), None
            )
            if adapter is None:
                raise OpenError("account_unavailable")
            operation = data.pop("operation")
            if operation == "history":
                data["history_query"] = data.pop("query")
            return await service.member_whitelist.run(
                adapter,
                group,
                operation,
                actor=call.actor,
                session=("dashboard", call.actor, account, group),
                **data,
            )
        if call.action == "member_blacklist":
            from .member_blacklist import REQUEST

            data = REQUEST.validate_python(payload).model_dump()
            account, group = data.pop("account"), data.pop("group_id")
            adapter = next(
                (a for a in service.adapters() if str(a.account) == account), None
            )
            if adapter is None:
                raise OpenError("account_unavailable")
            operation = data.pop("operation")
            return await service.member_blacklist.run(
                adapter,
                group,
                operation,
                actor=call.actor,
                session=("dashboard", call.actor, account, group),
                **data,
            )
        if call.action in {"rules_provision_preview", "rules_provision"}:
            from .rules_provisioning import provision_rules

            return await provision_rules(service, call)
        if call.action in {"ai_admission_preview", "ai_admission"}:
            from .ai_admission import upgrade_ai_admission

            return await upgrade_ai_admission(self, call)
        if call.action == "command_capabilities":
            from .command_capabilities import CAPABILITIES

            if payload:
                raise OpenError("command_capabilities_invalid")
            return {"commands": list(CAPABILITIES.values())}
        if call.action == "test_probe":
            from .test_probe import send_probe

            try:
                return await send_probe(service, call.actor, payload)
            except OpenError as exc:
                logger.warning(f"WSL controlled test probe rejected: code={exc}")
                raise
        if call.action == "test_quotes":
            from .test_probe import control_quotes

            async with self.test_quotes_lock:
                return await control_quotes(service, call.actor, payload)
        if call.action == "test_lottery":
            from .test_lottery import control_lottery

            return await control_lottery(service, call.actor, payload)
        if call.action == "test_advertising":
            from .test_advertising import begin_advertising

            return await begin_advertising(service, call.actor, payload)
        if call.action == "test_flood":
            from .test_flood import control_flood

            return await control_flood(service, call.actor, payload)
        if call.action == "test_group_ai":
            from .test_group_ai import control_group_ai

            return await control_group_ai(service, call.actor, payload)
        if call.action.startswith("finance_"):
            return await self.finance_control(call)
        if call.action in {"business_rules", "group_knowledge", "group_welcome"}:
            account = payload.get("account")
            if (
                not isinstance(account, str)
                or account
                and (not account.isascii() or not account.isdigit())
            ):
                raise OpenError("account_invalid")
            adapter = next(
                (a for a in service.adapters() if str(a.account) == account), None
            )
            if adapter is None:
                raise OpenError("account_unavailable")
            guard = service.write_guard(adapter)
            guard()
            operation = payload.get("operation", "read")
            session = "dashboard/" + call.actor
            group = payload.get("group_id")
            if not isinstance(group, str) or not group.isascii() or not group.isdigit():
                raise OpenError("group_invalid")
            tenant = await asyncio.to_thread(service.store.tenant, account, group)
            if not tenant or tenant["version"] != payload.get("tenant_version"):
                raise OpenError("tenant_changed")
            members = await service.roster(adapter, group)
            service.member_evidence[(account, group)] = members
            guard = self.control_guard(adapter, tenant, members)
            guard()
            common = {"account", "group_id", "tenant_version", "operation"}
            if call.action == "group_welcome":
                return await asyncio.to_thread(
                    service.welcome.control,
                    payload,
                    call.actor,
                    session,
                    tenant,
                    guard,
                )
            if call.action == "group_knowledge":
                from .knowledge_store import KnowledgeScope

                scope = KnowledgeScope("group", account=account, group=group)
                knowledge = await asyncio.to_thread(service.knowledge.store.read, scope)
                snapshot, entries = knowledge["snapshot"], knowledge["entries"]
                if operation == "read" and set(payload) == common:
                    return {"version": snapshot.version, "entries": entries}
                if (
                    operation not in {"add", "edit", "delete", "mentions", "idle"}
                    or set(payload) != common | {"version", "data", "request"}
                    or snapshot.version != payload["version"]
                    or not isinstance(payload["data"], dict)
                    or set(payload["data"]) - {"title", "body", "number", "enabled"}
                    or not isinstance(payload["request"], str)
                    or not 1 <= len(payload["request"]) <= 128
                ):
                    raise OpenError("knowledge_invalid")
                return await asyncio.to_thread(
                    service.knowledge.store.change,
                    snapshot,
                    operation,
                    payload["data"],
                    "server/" + payload["request"],
                    call.actor,
                    guard=guard,
                )
            if operation == "read" and set(payload) == common:
                result = await asyncio.to_thread(
                    service.rule_replies.read, account, group
                )
                with service.store.connect() as db:
                    replacement = db.execute(
                        "SELECT operation,previous,state,error,updated FROM business_ad_heads "
                        "WHERE account=? AND group_id=?",
                        (account, group),
                    ).fetchone()
                    result["ad_replacement"] = (
                        dict(replacement) if replacement else None
                    )
                    result["schedules"] = [
                        dict(row)
                        for row in db.execute(
                            "SELECT c.rule_id,c.next_at,c.blocked,s.state AS last_state,"
                            "s.error AS last_error,s.created AS last_at "
                            "FROM business_ad_cursors c LEFT JOIN business_rule_sends s "
                            "ON s.operation=(SELECT operation FROM business_rule_sends "
                            "WHERE account=c.account AND group_id=c.group_id "
                            "AND rule_id=c.rule_id AND kind='advertisement' "
                            "ORDER BY created DESC LIMIT 1) "
                            "WHERE c.account=? AND c.group_id=?",
                            (account, group),
                        )
                    ]
                result["images"] = await asyncio.to_thread(
                    service.rule_replies.media.previews,
                    account,
                    group,
                    result["config"],
                )
                return result
            if operation == "image_upload" and set(payload) == common | {"data"}:
                return await asyncio.to_thread(
                    service.rule_replies.media.upload,
                    account,
                    group,
                    payload["data"],
                    guard=guard,
                )
            if operation == "preview" and set(payload) == common | {
                "version",
                "config",
            }:
                if type(payload["version"]) is not int:
                    raise OpenError("business_rules_invalid")
                return await asyncio.to_thread(
                    service.rule_replies.preview,
                    account,
                    group,
                    payload["version"],
                    payload["config"],
                    call.actor,
                    session,
                    tenant,
                    guard=guard,
                )
            if operation == "commit" and set(payload) == common | {"token"}:
                return await asyncio.to_thread(
                    service.rule_replies.commit,
                    account,
                    group,
                    payload["token"],
                    call.actor,
                    session,
                    guard=guard,
                )
            raise OpenError("business_rules_invalid")
        if call.action == "status":
            if payload:
                raise OpenError("status_arguments")
            budget = dict(service.budget)
            if budget["confirmed"]:
                try:
                    budget["confirmed"] = budget.get(
                        "binding"
                    ) == service.store.model_binding(
                        service.resolve_model(
                            {"provider_id": budget["provider"]}
                            if service.integrated
                            else None
                        )
                    )
                except (ValueError, OpenError):
                    budget["confirmed"] = False
            media = getattr(service.plugin, "media_moderation", None)
            media_inspection = None
            if media is not None:
                from .media_moderation import (
                    MAX_ACCOUNT_PENDING_IMAGES,
                    MAX_GROUP_PENDING_IMAGES,
                    MAX_MEDIA_WORKERS,
                    MAX_PENDING_IMAGES,
                )

                media_inspection = {
                    **media.counts,
                    "pending": media.queued,
                    "running": len(media.running),
                    "capacity": MAX_PENDING_IMAGES,
                    "account_capacity": MAX_ACCOUNT_PENDING_IMAGES,
                    "group_capacity": MAX_GROUP_PENDING_IMAGES,
                    "workers": MAX_MEDIA_WORKERS,
                }
            return {
                "version": 1,
                "mode": "integrated" if service.integrated else "isolated",
                "features": {
                    "group_welcome": all(
                        getattr(a, "supports_welcome_recall", False) is True
                        for a in service.adapters()
                    ),
                },
                "settings": service.settings,
                "ai_budget": {
                    key: budget[key]
                    for key in ("version", "confirmed", "provider", "checked")
                },
                "allocated_count": sum(t["active"] for t in service.tenants.values()),
                "operating_count": sum(
                    t["active"]
                    and t["present"]
                    and not t["paused"]
                    and t.get("admission", "legacy") in {"legacy", "verified"}
                    and (t["account"], t["group_id"]) not in service.suspended_groups
                    for t in service.tenants.values()
                )
                if not service.settings["paused"] and not service.capacity_paused
                else 0,
                "group_limit": 20,
                "health": {
                    "disk_free_bytes": service.disk_free_bytes,
                    "disk_warning": service.disk_free_bytes < 2 * 1024**3,
                    "capacity_paused": service.capacity_paused,
                },
                "queue_health": [
                    {
                        "account": str(a.account),
                        "pending": getattr(a.diagnostics, "gauges", {}).get(
                            "pending", 0
                        ),
                        "rejected": getattr(a.ledger, "rejected_count", 0),
                        "review_groups": sorted(
                            getattr(a.ledger, "review_groups", set())
                        ),
                    }
                    for a in service.adapters()
                    if getattr(a, "diagnostics", None) is not None
                    and getattr(a, "ledger", None) is not None
                ],
                "media_inspection": media_inspection,
                "usage": await asyncio.to_thread(service.store.usage),
                "billing": await self.billing_status(),
                "manager_quotas": await asyncio.to_thread(service.store.manager_quotas),
                "group_ai": [
                    {
                        **row,
                        "readiness": await service.group_ai.readiness(
                            next(
                                (
                                    a
                                    for a in service.adapters()
                                    if str(a.account) == row["account"]
                                ),
                                None,
                            ),
                            row,
                        ),
                    }
                    for row in service.group_ai.configs.values()
                ],
                "accounts": [
                    {
                        "instance_id": a.meta().id,
                        "account": str(a.account),
                        "nickname": account_nickname(a),
                        "state": a.connection_state,
                        "enabled_groups": (
                            sum(
                                t["account"] == str(a.account)
                                and t["active"]
                                and t["present"]
                                and not t["paused"]
                                and t.get("admission", "legacy")
                                in {"legacy", "verified"}
                                and (t["account"], t["group_id"])
                                not in service.suspended_groups
                                for t in service.tenants.values()
                            )
                            if not service.settings["paused"]
                            and not service.capacity_paused
                            else 0
                        ),
                    }
                    for a in service.adapters()
                ],
                "groups": [
                    {
                        **tenant,
                        "membership_pending": (tenant["account"], tenant["group_id"])
                        in service.suspended_groups,
                        "native_capability": (
                            capability
                            if tenant["present"]
                            and capability.get("epoch") == tenant["epoch"]
                            and time.time() - capability.get("checked_at", 0) < 120
                            else {"state": "unknown", "checked_at": 0}
                        ),
                    }
                    for tenant in service.tenants.values()
                    for capability in [
                        service.capabilities.get(
                            (tenant["account"], tenant["group_id"]), {}
                        )
                    ]
                ],
                "model": {
                    k: service.model.get(k, "")
                    for k in ("base_url", "model", "provider_id")
                },
                "providers": [
                    {"id": p.meta().id, "model": p.get_model()}
                    for p in service.context.get_all_providers()
                    if p.meta().type == "openai_chat_completion"
                ]
                if service.integrated
                else [],
                "model_configured": bool(service.model),
                "ai_profiles": [
                    row
                    for row in await asyncio.to_thread(service.profiles.rows)
                    if row["kind"] == "group"
                ],
                "ai_choices": service.profiles.inventory(),
                "audit": [
                    {
                        k: row[k]
                        for k in (
                            "id",
                            "created",
                            "actor",
                            "account",
                            "group_id",
                            "action",
                        )
                    }
                    for row in await asyncio.to_thread(service.store.audit)
                ],
                "admins": [
                    str(u)
                    for u in (
                        service.quota_admins
                        if service.integrated
                        else service.context.get_config().get("admins_id", [])
                    )
                ],
            }
        if call.action in {"login", "login_save"}:
            return await service.account_action(call.actor, call.action, payload)
        if call.action == "manager_quota":
            raise OpenError("manager_quota_retired_use_plans")
        if call.action == "billing_settings":
            if set(payload) - {"trial_monthly_cny"} != {
                "version",
                "contact_name",
                "contact",
                "monthly_cny",
            }:
                raise OpenError("billing_settings_invalid")
            await asyncio.to_thread(
                service.plans.configure, actor=call.actor, **payload
            )
            return {"saved": True}
        if call.action == "billing_price":
            if (
                set(payload) != {"provider", "version", "config"}
                or not isinstance(payload["provider"], str)
                or not 1 <= len(payload["provider"]) <= 256
            ):
                raise OpenError("billing_price_invalid")
            resolved = service.resolve_model({"provider_id": payload["provider"]})
            await asyncio.to_thread(
                service.plans.configure_price,
                service.store.model_binding(resolved),
                payload["provider"],
                payload["version"],
                payload["config"],
                call.actor,
            )
            return {"saved": True}
        if call.action in {"plan_preview", "plan_grant", "payer_nominate"}:
            required = {"account", "group_id", "tenant_version", "uid", "version"}
            required |= (
                {"pool_version"}
                if call.action == "payer_nominate"
                else {"tier", "received_cny"}
            )
            if call.action == "plan_grant":
                required.add("confirmation")
            if (
                set(payload) != required
                or any(
                    type(payload[k]) is not int for k in ("tenant_version", "version")
                )
                or any(
                    not isinstance(payload[key], str)
                    or not payload[key].isascii()
                    or not payload[key].isdecimal()
                    for key in ("account", "group_id")
                )
                or (
                    call.action == "payer_nominate"
                    and type(payload["pool_version"]) is not int
                )
            ):
                raise OpenError("billing_grant_invalid")
            adapter = next(
                (a for a in service.adapters() if str(a.account) == payload["account"]),
                None,
            )
            if adapter is None:
                raise OpenError("account_unavailable")
            tenant = await asyncio.to_thread(
                service.store.tenant, payload["account"], payload["group_id"]
            )
            if not tenant or tenant["version"] != payload["tenant_version"]:
                raise OpenError("tenant_changed")
            roster = await service.roster(adapter, payload["group_id"])
            if (
                not isinstance(payload["uid"], str)
                or payload["uid"] not in tenant["managers"]
                or payload["uid"] not in roster
            ):
                raise OpenError("manager_required")
            authority = self.control_guard(adapter, tenant, roster)
            settings = dict(service.settings)

            def guard():
                authority()
                if service.settings != settings or service.store.settings() != settings:
                    raise OpenError("settings_changed")

            guard()
            principal = "nim/" + str(roster[payload["uid"]]["nimId"])
            if call.action == "payer_nominate":
                await asyncio.to_thread(
                    service.plans.bind,
                    payload["account"],
                    payload["group_id"],
                    payload["uid"],
                    principal,
                    payload["version"],
                    tenant["version"],
                    payload["pool_version"],
                    call.actor,
                    guard=guard,
                    control=True,
                )
                await service.refresh()
                return {"saved": True}
            server_payload = {k: v for k, v in payload.items() if k != "confirmation"}
            server_payload["principal"] = principal
            if call.action == "plan_preview":
                return await asyncio.to_thread(
                    service.plans.preview, call.actor, server_payload, guard=guard
                )
            await asyncio.to_thread(
                service.plans.grant,
                call.actor,
                server_payload,
                payload["confirmation"],
                guard=guard,
            )
            return {"saved": True}
        if call.action == "review_resume":
            if set(payload) != {"account", "group_id", "version"}:
                raise OpenError("review_arguments")
            if type(payload["version"]) is not int:
                raise OpenError("review_arguments")
            adapter = next(
                (a for a in service.adapters() if str(a.account) == payload["account"]),
                None,
            )
            if adapter is None:
                raise OpenError("review_version_changed")
            guard = service.write_guard(adapter)
            tenant = await asyncio.to_thread(
                service.store.tenant, payload["account"], payload["group_id"]
            )
            guard()
            if (
                not tenant
                or tenant["version"] != payload["version"]
                or not tenant["active"]
                or not tenant["present"]
            ):
                raise OpenError("review_version_changed")
            roster = await service.roster(adapter, payload["group_id"])
            guard()
            current = await asyncio.to_thread(
                service.store.tenant, payload["account"], payload["group_id"]
            )
            guard()
            if current != tenant:
                raise OpenError("review_version_changed")
            guard = self.control_guard(adapter, current, roster)
            guard()
            ledger = adapter.ledger
            async with ledger.ingress_lock:
                # Keep the last authority check adjacent to COMMIT, including
                # revocation received while an awaited DELETE is in flight.
                async with AsyncDatabase(
                    lambda: sqlite3.connect(ledger.path, timeout=5),
                    write=True,
                    guard=guard,
                ) as db:
                    hold = await db.fetchone(
                        "SELECT created FROM review_holds WHERE account=? AND team=?",
                        (payload["account"], payload["group_id"]),
                    )
                    unresolved = await db.fetchone(
                        "SELECT 1 FROM inbox WHERE account=? AND team=? "
                        "AND state IN ('pending','processing','needs_review') LIMIT 1",
                        (payload["account"], payload["group_id"]),
                    )
                    if hold is None or time.time() - hold[0] < 300 or unresolved:
                        raise OpenError("review_unresolved")
                    await db.execute(
                        "DELETE FROM review_holds WHERE account=? AND team=?",
                        (payload["account"], payload["group_id"]),
                    )
                ledger.review_groups.discard(payload["group_id"])
            with service.store.connect() as db:
                service.store.log(
                    db,
                    call.actor,
                    payload["account"],
                    payload["group_id"],
                    "review_resumed",
                    {"version": payload["version"]},
                )
            return {"resumed": True}
        if call.action == "tenant_members":
            if (
                set(payload)
                not in (
                    {"account", "group_id"},
                    {"account", "group_id", "muted_only"},
                )
                or type(payload.get("muted_only", False)) is not bool
            ):
                raise OpenError("tenant_arguments")
            adapter = next(
                (a for a in service.adapters() if str(a.account) == payload["account"]),
                None,
            )
            if adapter is None:
                raise OpenError("account_unavailable")
            guard = service.write_guard(adapter)
            roster = await service.roster(adapter, payload["group_id"])
            guard()
            muted = None
            if payload.get("muted_only"):
                from astrbot.core.platform.sources.wangshangliao.directory import (
                    muted_member_ids,
                )

                try:
                    muted = await muted_member_ids(
                        adapter, payload["group_id"], authorize=guard
                    )
                except ProtocolError as exc:
                    logger.warning("WSL dashboard mute list read failed: %s", str(exc))
                    raise
                guard()
            return {
                "native_capability": service.capabilities.get(
                    (payload["account"], payload["group_id"]),
                    {"state": "unknown"},
                ),
                "members": [
                    {
                        "id": uid,
                        "role": row.get("groupRole", ""),
                        "name": str(
                            row.get("groupMemberNick")
                            or row.get("userNick")
                            or "群成员"
                        )[:128],
                    }
                    for uid, row in roster.items()
                    if uid != str(adapter.account)
                    and (
                        muted is None
                        or uid in muted
                        and row.get("groupRole") == "GROUP_ROLE_MEMBER"
                    )
                ],
            }
        if (
            call.action == "ai_profile"
            and not payload.get("account")
            and not payload.get("group_id")
        ):
            if set(payload) != {"kind", "account", "group_id", "version", "selection"}:
                raise OpenError("ai_profile_invalid")
            if not any(
                row["kind"] == payload["kind"]
                and not row["account"]
                and not row["group_id"]
                and row["selection"]
                for row in service.profiles.rows()
            ):
                raise OpenError("ai_profile_missing")
            await asyncio.to_thread(
                service.profiles.save,
                payload["kind"],
                "",
                "",
                payload["version"],
                payload["selection"],
                call.actor,
            )
            return {"saved": True}
        if call.action in {"group_ai", "ai_profile"}:
            common = {"account", "group_id", "version", "tenant_version"}
            required = common | (
                {"kind", "selection"}
                if call.action == "ai_profile"
                else {"config", "resume"}
            )
            if (
                set(payload) != required
                or type(payload["version"]) is not int
                or type(payload["tenant_version"]) is not int
                or any(
                    not isinstance(payload[key], str)
                    or not payload[key].isascii()
                    or not payload[key].isdecimal()
                    for key in ("account", "group_id")
                )
            ):
                raise OpenError("ai_control_arguments")
            adapter = next(
                (a for a in service.adapters() if str(a.account) == payload["account"]),
                None,
            )
            if adapter is None:
                raise OpenError("account_unavailable")
            guard = service.write_guard(adapter)
            tenant = await asyncio.to_thread(
                service.store.tenant, payload["account"], payload["group_id"]
            )
            guard()
            if not tenant or tenant["version"] != payload["tenant_version"]:
                raise OpenError("tenant_changed")
            roster = await service.roster(adapter, payload["group_id"])
            guard()
            current = await asyncio.to_thread(
                service.store.tenant, payload["account"], payload["group_id"]
            )
            if current != tenant:
                raise OpenError("tenant_changed")
            authority_guard = self.control_guard(adapter, current, roster)
            settings = dict(service.settings)

            def guard():
                authority_guard()
                if service.settings != settings or service.store.settings() != settings:
                    raise OpenError("settings_changed")

            guard()
            if call.action == "ai_profile":
                await asyncio.to_thread(
                    service.profiles.save,
                    payload["kind"],
                    payload["account"],
                    payload["group_id"],
                    payload["version"],
                    payload["selection"],
                    call.actor,
                    guard=guard,
                    tenant_version=payload["tenant_version"],
                )
            else:
                await asyncio.to_thread(
                    service.group_ai.configure,
                    payload["account"],
                    payload["group_id"],
                    payload["version"],
                    payload["tenant_version"],
                    payload["config"],
                    call.actor,
                    control=True,
                    resume=payload["resume"],
                    guard=guard,
                )
        elif call.action == "limits":
            if set(payload) != {
                "version",
                "paused",
                "personal_limit",
                "global_limit",
                "admins",
            }:
                raise OpenError("limits_arguments")
            if payload["personal_limit"] != 20:
                raise OpenError("manager_quota_retired_use_plans")
            admins = payload["admins"]
            if (
                not isinstance(admins, list)
                or len(admins) > 20
                or any(
                    not isinstance(u, str)
                    or not u.isascii()
                    or not u.isdigit()
                    or not 0 < int(u) < 1 << 30
                    for u in admins
                )
            ):
                raise OpenError("admins_invalid")
            await asyncio.to_thread(
                service.store.configure,
                payload["version"],
                payload["paused"],
                payload["personal_limit"],
                payload["global_limit"],
                call.actor,
                admins=admins,
            )
        elif call.action == "ai_budget":
            if set(payload) != {"version", "confirmed", "provider", "evidence"}:
                raise OpenError("budget_arguments")
            provider = payload["provider"]
            if not isinstance(provider, str):
                raise OpenError("budget_provider_changed")
            resolved = service.resolve_model({"provider_id": provider})
            await asyncio.to_thread(
                service.store.confirm_budget,
                payload["version"],
                payload["confirmed"],
                provider,
                payload["evidence"],
                call.actor,
                binding=service.store.model_binding(resolved),
            )
        elif call.action == "model":
            await asyncio.to_thread(service.configure_model, payload)
        elif call.action in {"tenant", "tenant_preview"}:
            if set(payload) - {
                "confirmation",
                "account",
                "group_id",
                "version",
                "rules",
                "managers",
                "paused",
            } or not {
                "account",
                "group_id",
                "version",
                "rules",
                "managers",
                "paused",
            } <= set(payload):
                raise OpenError("tenant_arguments")
            if type(payload["version"]) is not int:
                raise OpenError("tenant_arguments")
            adapter = next(
                (a for a in service.adapters() if str(a.account) == payload["account"]),
                None,
            )
            if adapter is None:
                raise OpenError("account_unavailable")
            guard = service.write_guard(adapter)
            tenant = await asyncio.to_thread(
                service.store.tenant, payload["account"], payload["group_id"]
            )
            guard()
            if not tenant or tenant["version"] != payload["version"]:
                raise OpenError("tenant_changed")
            roster = await service.roster(adapter, payload["group_id"])
            guard()
            current = await asyncio.to_thread(
                service.store.tenant, payload["account"], payload["group_id"]
            )
            guard()
            if not current or (current["version"], current["epoch"]) != (
                tenant["version"],
                tenant["epoch"],
            ):
                raise OpenError("tenant_changed")
            guard = self.control_guard(adapter, current, roster)
            guard()
            authority = guard

            def guard():
                authority()
                eligible = native_manager_ids(
                    service.member_evidence.get(
                        (payload["account"], payload["group_id"]), {}
                    ).values(),
                    adapter.account,
                )
                if not isinstance(payload["managers"], list) or any(
                    not isinstance(u, str) or u not in eligible
                    for u in payload["managers"]
                ):
                    raise OpenError("manager_native_role_required")

            guard()
            if call.action == "tenant_preview":
                token = await asyncio.to_thread(
                    service.store.preview_control,
                    payload["account"],
                    payload["group_id"],
                    payload["version"],
                    call.actor,
                    payload["rules"],
                    payload["managers"],
                    payload["paused"],
                    guard=guard,
                )
                return {"confirmation": token, "expires_in": 600}
            await service.commit_tenant(
                payload["account"],
                payload["group_id"],
                service.store.change,
                payload["account"],
                payload["group_id"],
                payload["version"],
                call.actor,
                rules=payload["rules"],
                managers=payload["managers"],
                paused=payload["paused"],
                control=True,
                guard=guard,
                confirmation=payload.get("confirmation", ""),
            )
            return {"saved": True}
        await service.refresh()
        return {"saved": True}

    async def billing_status(self):
        """Expose pricing references only, with credential-bound freshness."""
        result = await asyncio.to_thread(self.service.plans.status)
        for row in result["prices"]:
            try:
                row["current"] = row["binding"] == self.service.store.model_binding(
                    self.service.resolve_model({"provider_id": row["provider"]})
                )
            except (OpenError, ValueError):
                row["current"] = False
        return result

    async def finance_control(self, call):
        """Read historical chat finance grants without granting retired access.

        Args:
            call: Allowlisted dashboard request, never a chat or model request.

        Returns:
            Historical grants and accounting group metadata.
        """
        require_group_call(call)
        owner, payload = self.service, call.payload
        domain = getattr(getattr(owner.plugin, "cashback", None), "service", None)
        if not call.actor.startswith("dashboard/") or domain is None:
            raise OpenError("finance_unavailable")
        if call.action != "finance_status" or payload:
            raise OpenError("finance_arguments")
        configured = {g["id"]: g for g in await asyncio.to_thread(domain.store.groups)}
        return {
            "version": 1,
            "grants": await asyncio.to_thread(domain.store.finance_grants),
            "groups": [
                {
                    "account": str(a.account),
                    "group_id": t["group_id"],
                    "name": domain.group_name(a, t["group_id"]),
                    "configured": t["group_id"] in configured,
                    "bot_id": a.config["id"],
                }
                for a in owner.adapters()
                if a.config["id"] == domain.collector_id
                for t in owner.tenants.values()
                if t["account"] == str(a.account)
            ],
            "test_active": False,
            "test_version": 0,
        }

    async def start(self):
        """Refuse to recreate the retired independent socket service."""
        raise OpenError("isolated_mode_retired")

    async def close(self):
        """Leave legacy files untouched; integrated control owns no socket."""
        return None


async def call_open(action, actor, payload):
    """Keep imports compatible while refusing all legacy network dispatch."""
    raise OpenError("isolated_mode_retired")
