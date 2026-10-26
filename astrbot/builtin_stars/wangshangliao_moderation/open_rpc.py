"""Allowlisted in-process control plane for the integrated public bot."""

import asyncio
import re
import sqlite3
import time
import uuid
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


class OpenCall(BaseModel):
    """Keep the bridge versioned and reject generic proxy or method invocation."""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    action: Literal[
        "status",
        "login",
        "login_save",
        "activation_code",
        "tenant",
        "tenant_preview",
        "rules_provision_preview",
        "rules_provision",
        "ai_admission_preview",
        "ai_admission",
        "guidance_trial_preview",
        "guidance_trial",
        "tenant_members",
        "limits",
        "model",
        "ai_budget",
        "review_resume",
        "manager_quota",
        "group_ai",
        "ai_profile",
        "support_settings",
        "support_editor",
        "owner_support",
        "business_rules",
        "member_whitelist",
        "member_blacklist",
        "member_mutes",
        "provision_profiles",
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
        "finance_members",
        "finance_grant",
        "finance_test",
        "test_probe",
        "test_quotes",
        "test_guides",
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
        self.finance_test_lock = asyncio.Lock()
        self.test_guides_lock = asyncio.Lock()
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
                    "support_settings",
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
        service, payload = self.service, call.payload
        if not service.integrated and call.action != "status":
            raise OpenError("isolated_mode_retired")
        if call.action == "test_blacklist":
            from .test_blacklist import control_blacklist

            return await control_blacklist(service, call.actor, payload)
        if call.action == "member_mutes":
            from .member_mutes import member_mutes

            return await member_mutes(self, call)
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
        if call.action == "owner_support":
            from .owner_support import TEMPLATE

            caller = asyncio.current_task()

            def owner_guard():
                if (
                    not service.active
                    or service.capacity_paused
                    or caller is not None
                    and caller.cancelling()
                ):
                    raise OpenError("operation_revoked")

            owner_guard()
            operation = payload.get("operation")
            if operation == "list" and set(payload) in (
                {"operation"},
                {"operation", "account"},
            ):
                owners = service.owner_support.rows()
                if "account" in payload:
                    account = payload["account"]
                    if not isinstance(account, str) or not any(
                        str(adapter.account) == account
                        for adapter in service.adapters()
                    ):
                        raise OpenError("account_unavailable")
                    with service.store.connect() as db:
                        ids = {
                            row["uid"]
                            for row in db.execute(
                                "SELECT b.uid FROM support_group_owners b JOIN tenants t "
                                "ON t.account=b.account AND t.group_id=b.group_id "
                                "WHERE b.account=? AND b.epoch=t.epoch AND t.present=1",
                                (account,),
                            )
                        }
                    owners = [row for row in owners if row["uid"] in ids]
                return {"owners": owners}
            uid = payload.get("uid")
            if (
                not isinstance(uid, str)
                or uid
                and (not uid.isascii() or not uid.isdigit() or len(uid) > 32)
            ):
                raise OpenError("support_owner_invalid")
            owner = next(
                (row for row in service.owner_support.rows() if row["uid"] == uid), None
            )
            if uid and not owner:
                raise OpenError("support_owner_unavailable")
            scope = "owner:" + uid if uid else TEMPLATE
            common = {"operation", "uid"}
            if operation == "read" and set(payload) == common:
                return await asyncio.to_thread(service.support.read, scope)
            if operation == "preview" and set(payload) == common | {
                "version",
                "config",
            }:
                if type(payload["version"]) is not int:
                    raise OpenError("support_settings_invalid")
                return await asyncio.to_thread(
                    service.support.preview,
                    scope,
                    payload["version"],
                    payload["config"],
                    call.actor,
                    "dashboard/" + call.actor,
                    guard=owner_guard,
                )
            if operation == "commit" and set(payload) == common | {"token"}:
                return await asyncio.to_thread(
                    service.support.commit,
                    scope,
                    payload["token"],
                    call.actor,
                    "dashboard/" + call.actor,
                    guard=owner_guard,
                )
            if uid and operation == "provision" and set(payload) == common:
                return await service.owner_support.ensure_profile(
                    uid, call.actor, guard=owner_guard
                )
            if (
                uid
                and operation == "profile"
                and set(payload) == common | {"version", "selection"}
            ):
                if not owner["active"]:
                    raise OpenError("support_owner_unavailable")
                if not any(
                    row["kind"] == "consultation"
                    and row["account"] == scope
                    and not row["group_id"]
                    and row["selection"]
                    for row in service.profiles.rows()
                ):
                    raise OpenError("ai_profile_missing")

                def profile_guard():
                    owner_guard()
                    if (
                        next(
                            (
                                row
                                for row in service.owner_support.rows()
                                if row["uid"] == uid
                            ),
                            None,
                        )
                        != owner
                    ):
                        raise OpenError("support_owner_changed")

                await asyncio.to_thread(
                    service.profiles.save,
                    "consultation",
                    scope,
                    "",
                    payload["version"],
                    payload["selection"],
                    call.actor,
                    guard=profile_guard,
                )
                return {"saved": True}
            if (
                uid
                and operation == "state"
                and set(payload) == common | {"version", "active"}
            ):
                return await asyncio.to_thread(
                    service.owner_support.state,
                    uid,
                    payload["version"],
                    payload["active"],
                    call.actor,
                    guard=owner_guard,
                )
            raise OpenError("support_owner_invalid")
        if call.action in {"rules_provision_preview", "rules_provision"}:
            from .rules_provisioning import provision_rules

            return await provision_rules(service, call)
        if call.action in {"ai_admission_preview", "ai_admission"}:
            from .ai_admission import upgrade_ai_admission

            return await upgrade_ai_admission(self, call)
        if call.action in {"guidance_trial_preview", "guidance_trial"}:
            from .guidance_trial import provision_trial

            return await provision_trial(self, call)
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
        if call.action == "test_guides":
            from .test_probe import send_guides

            async with self.test_guides_lock:
                return await send_guides(service, call.actor, payload)
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
        if call.action == "finance_test":
            # Dashboard calls already hold the outer control-plane lock.
            async with self.finance_test_lock:
                return await self.finance_control(call)
        if call.action.startswith("finance_"):
            return await self.finance_control(call)
        if call.action in {
            "support_settings",
            "support_editor",
            "business_rules",
            "provision_profiles",
            "group_knowledge",
        } or (
            call.action == "ai_profile"
            and payload.get("kind") == "consultation"
            and payload.get("account")
            and not payload.get("group_id")
        ):
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
            if adapter is None and not (
                call.action == "provision_profiles" and account == ""
            ):
                raise OpenError("account_unavailable")
            caller = asyncio.current_task()

            def global_guard():
                if (
                    not service.active
                    or service.capacity_paused
                    or caller is not None
                    and caller.cancelling()
                ):
                    raise OpenError("operation_revoked")

            guard = service.write_guard(adapter) if adapter else global_guard
            guard()
            if call.action == "provision_profiles":
                if set(payload) != {"account"}:
                    raise OpenError("profile_provision_invalid")
                return await service.support.provision(account, call.actor, guard=guard)
            if call.action == "ai_profile":
                if set(payload) != {
                    "kind",
                    "account",
                    "group_id",
                    "version",
                    "selection",
                }:
                    raise OpenError("ai_profile_invalid")
                if not any(
                    row["kind"] == "consultation"
                    and row["account"] == account
                    and not row["group_id"]
                    and row["selection"]
                    for row in service.profiles.rows()
                ):
                    raise OpenError("ai_profile_missing")
                await asyncio.to_thread(
                    service.profiles.save,
                    "consultation",
                    account,
                    "",
                    payload["version"],
                    payload["selection"],
                    call.actor,
                    guard=guard,
                )
                return {"saved": True}
            operation = payload.get("operation", "read")
            session = "dashboard/" + call.actor
            if call.action == "support_editor":
                if operation == "read" and set(payload) == {"account", "operation"}:
                    return {"editors": service.support.editors(account)}
                if operation != "save" or set(payload) != {
                    "account",
                    "operation",
                    "uid",
                    "peer",
                    "version",
                    "active",
                }:
                    raise OpenError("support_grant_invalid")
                return await asyncio.to_thread(
                    service.support.grant,
                    account,
                    payload["uid"],
                    payload["peer"],
                    payload["version"],
                    payload["active"],
                    call.actor,
                    guard=guard,
                )
            if call.action == "support_settings":
                if operation == "read" and set(payload) == {"account", "operation"}:
                    return await asyncio.to_thread(service.support.read, account)
                if (
                    operation == "preview"
                    and set(payload) == {"account", "operation", "version", "config"}
                    and type(payload["version"]) is int
                ):
                    return await asyncio.to_thread(
                        service.support.preview,
                        account,
                        payload["version"],
                        payload["config"],
                        call.actor,
                        session,
                        guard=guard,
                    )
                if operation == "commit" and set(payload) == {
                    "account",
                    "operation",
                    "token",
                }:
                    return await asyncio.to_thread(
                        service.support.commit,
                        account,
                        payload["token"],
                        call.actor,
                        session,
                        guard=guard,
                    )
                raise OpenError("support_settings_invalid")
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
            return {
                "version": 1,
                "mode": "integrated" if service.integrated else "isolated",
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
                "ai_profiles": await asyncio.to_thread(service.profiles.rows),
                "ai_choices": service.profiles.inventory(),
                "guidance_status": await asyncio.to_thread(
                    service.support.guidance_status
                ),
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
        if call.action == "activation_code":
            if set(payload) != {"account", "group_id"}:
                raise OpenError("activation_arguments")
            tenant = await asyncio.to_thread(
                service.store.tenant, payload["account"], payload["group_id"]
            )
            if (
                not tenant
                or tenant.get("admission", "legacy") != "verified"
                or tenant["cleanup_epoch"] > tenant["cleanup_done"]
            ):
                raise OpenError("inviter_unproven")
            token = await asyncio.to_thread(
                service.store.mint, payload["account"], payload["group_id"], call.actor
            )
            return {"code": token, "expires_in": 600}
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
        """Manage scoped chat financial grants from authenticated total control.

        Args:
            call: Allowlisted dashboard request, never a chat or model request.

        Returns:
            Financial status, verified members or an idempotent grant result.
        """
        from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

        from .cashback_store import WRITE_GUARD

        owner, payload = self.service, call.payload
        domain = getattr(getattr(owner.plugin, "cashback", None), "service", None)
        if not call.actor.startswith("dashboard/") or domain is None:
            raise OpenError("finance_unavailable")
        try:
            if call.action == "finance_test":
                from .finance_chat import FinanceChat
                from .finance_sandbox import (
                    TEST_ACCOUNT,
                    TEST_GROUP,
                    TEST_MANAGER,
                    FinanceSandboxService,
                )

                if (
                    set(payload) != {"active", "version", "request_id"}
                    or type(payload["active"]) is not bool
                    or type(payload["version"]) is not int
                    or payload["version"] < 0
                    or not isinstance(payload["request_id"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", payload["request_id"])
                ):
                    raise OpenError("finance_arguments")
                finance = getattr(owner, "finance_chat", None)
                if finance is None:
                    owner.finance_chat = finance = FinanceChat(owner, domain)
                request_key = (call.actor, payload["request_id"])
                previous = finance.test_requests.get(request_key)
                if previous:
                    if previous[0] != payload:
                        raise OpenError("finance_test_changed")
                    return previous[1]
                if payload["version"] != getattr(finance, "test_version", 0):
                    raise OpenError("finance_test_changed")
                if not payload["active"]:
                    finance.test_until = 0
                    finance.test_version = payload["version"] + 1
                    result = {"status": "closed", "version": finance.test_version}
                    with owner.store.connect(write=True) as db:
                        owner.store.log(
                            db,
                            call.actor,
                            TEST_ACCOUNT,
                            TEST_GROUP,
                            "chat_finance_test_closed",
                            {"version": finance.test_version},
                        )
                    finance.test_requests[request_key] = (dict(payload), result)
                    if len(finance.test_requests) > 128:
                        finance.test_requests.popitem(last=False)
                    return result
                adapter = next(
                    (a for a in owner.adapters() if str(a.account) == TEST_ACCOUNT),
                    None,
                )
                window = getattr(adapter, "test_window", None)
                if (
                    adapter is None
                    or window is None
                    or not window.active()
                    or str(window.sender.account) != TEST_MANAGER
                    or TEST_GROUP not in window.groups
                    or "private_commands" not in window.scopes
                    or finance.test_until > time.time()
                ):
                    raise OpenError("finance_test_window_required")
                roster = await owner.roster(adapter, TEST_GROUP)
                manager = roster.get(TEST_MANAGER)
                if not manager or not manager.get("nimId"):
                    raise OpenError("finance_identity_unverified")
                # A new directory prevents an old rehearsal from being reset or
                # its state being mistaken for a first production transaction.
                test = FinanceSandboxService(
                    domain.context,
                    domain.store.path.parent / "chat-tests" / uuid.uuid4().hex,
                    domain.collector_id,
                )
                await asyncio.to_thread(
                    test.seed,
                    str(manager["nimId"]),
                    str(manager.get("userNick") or manager.get("name") or "林夕"),
                )
                finance.test_service = test
                finance.test_until = time.time() + min(300, window.status()["seconds"])
                finance.test_version = payload["version"] + 1
                result = {
                    "status": "opened",
                    "version": finance.test_version,
                    "test_ledger": True,
                }
                with owner.store.connect(write=True) as db:
                    owner.store.log(
                        db,
                        call.actor,
                        TEST_ACCOUNT,
                        TEST_GROUP,
                        "chat_finance_test_opened",
                        {"version": finance.test_version},
                    )
                finance.test_requests[request_key] = (dict(payload), result)
                if len(finance.test_requests) > 128:
                    finance.test_requests.popitem(last=False)
                return result
            if call.action == "finance_status":
                if payload:
                    raise OpenError("finance_arguments")
                configured = {
                    g["id"]: g for g in await asyncio.to_thread(domain.store.groups)
                }
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
                    "test_active": bool(
                        getattr(owner, "finance_chat", None)
                        and owner.finance_chat.test_until > time.time()
                    ),
                    "test_version": getattr(
                        getattr(owner, "finance_chat", None), "test_version", 0
                    ),
                }
            expected = (
                {"account", "group_id"}
                if call.action == "finance_members"
                else {
                    "account",
                    "group_id",
                    "uid",
                    "role",
                    "active",
                    "version",
                    "request_id",
                }
            )
            if set(payload) != expected:
                raise OpenError("finance_arguments")
            if any(
                not isinstance(payload[k], str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", payload[k])
                for k in ("account", "group_id")
            ):
                raise OpenError("finance_arguments")
            if call.action == "finance_grant" and (
                not isinstance(payload["uid"], str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", payload["uid"])
                or not isinstance(payload["role"], str)
                or payload["role"] not in {"cashier", "finance_admin"}
                or type(payload["active"]) is not bool
                or type(payload["version"]) is not int
                or payload["version"] < 0
                or not isinstance(payload["request_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", payload["request_id"])
            ):
                raise OpenError("finance_arguments")
            adapter = next(
                (
                    a
                    for a in owner.adapters()
                    if str(a.account) == payload["account"]
                    and a.config["id"] == domain.collector_id
                ),
                None,
            )
            tenant = owner.tenants.get((payload["account"], payload["group_id"]))
            old = next(
                (
                    r
                    for r in await asyncio.to_thread(domain.store.finance_grants)
                    if r["account"] == payload["account"]
                    and r["group_id"] == payload["group_id"]
                    and r["uid"] == payload.get("uid")
                ),
                None,
            )
            if call.action == "finance_grant" and payload["active"] is False:
                if old is None:
                    raise OpenError("finance_identity_unverified")

                def guard():
                    if not owner.active:
                        raise OpenError("finance_unavailable")

                member = {"nimId": old["peer"], "name": old["name"]}
            else:
                if adapter is None or tenant is None:
                    raise OpenError("finance_scope")
                guard = owner.write_guard(adapter, control=True)
                roster = await owner.roster(adapter, payload["group_id"])
                guard()
                if call.action == "finance_members":
                    return {
                        "members": [
                            {
                                "uid": uid,
                                "name": str(
                                    m.get("userNick")
                                    or m.get("name")
                                    or m.get("userName")
                                    or uid
                                ),
                            }
                            for uid, m in roster.items()
                            if uid != str(adapter.account)
                        ]
                    }
                member = roster.get(payload["uid"])
                if not member or not member.get("nimId"):
                    raise OpenError("finance_identity_unverified")
                guard = self.control_guard(adapter, tenant, roster)
            token = WRITE_GUARD.set(guard)
            try:
                return await asyncio.to_thread(
                    domain.store.finance_grant,
                    call.actor,
                    {
                        **payload,
                        "peer": str(member["nimId"]),
                        "name": str(
                            member.get("userNick")
                            or member.get("name")
                            or member.get("userName")
                            or payload["uid"]
                        )[:100],
                    },
                )
            finally:
                WRITE_GUARD.reset(token)
        except ProtocolError as exc:
            if str(exc) == "secondary_security_code_required":
                raise OpenError("finance_secondary_security_required") from None
            raise OpenError("finance_directory_unavailable") from None

    async def start(self):
        """Refuse to recreate the retired independent socket service."""
        raise OpenError("isolated_mode_retired")

    async def close(self):
        """Leave legacy files untouched; integrated control owns no socket."""
        return None


async def call_open(action, actor, payload):
    """Keep imports compatible while refusing all legacy network dispatch."""
    raise OpenError("isolated_mode_retired")
