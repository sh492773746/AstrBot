"""Dashboard-only cashier dispatch over the existing financial service."""

import asyncio
import copy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .cashback_finance import DashboardFinanceActor
from .cashback_models import ClientError, GroupRule, Payment, PricePreview, RiskRelease
from .cashback_store import WRITE_GUARD, CashbackError


class CashierCall(BaseModel):
    """Allow only explicitly supported plugin-page operations."""

    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "me",
        "status",
        "groups",
        "entries",
        "inviters",
        "payments",
        "price-preview",
        "audit",
        "admin/catalog",
        "admin/groups",
        "admin/risk",
        "admin/risk/release",
        "client-error",
    ]
    payload: dict = Field(default_factory=dict)


class LedgerQuery(BaseModel):
    """Validate bounded ledger filters received from the plugin bridge."""

    model_config = ConfigDict(extra="forbid", str_max_length=300)
    group: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    state: Literal[
        "pending", "paid", "void", "verifying", "unassigned", "no_reward"
    ] = "pending"
    search: str = ""
    page: int = Field(default=1, ge=1, le=1000000)
    period: Literal["all", "month", "day", "hour", "custom"] = "all"
    start_at: int | None = Field(default=None, ge=0)
    end_at: int | None = Field(default=None, ge=0)
    pagination: Literal["entries", "inviters"] = "inviters"
    inviter: str | None = Field(default=None, pattern=r"^(|[1-9][0-9]{0,19})$")
    member_page: int = Field(default=1, ge=1, le=1000000)


class GroupQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: str = Field(pattern=r"^[1-9][0-9]{0,19}$")


class AuditQuery(GroupQuery):
    before: int = Field(default=0, ge=0, le=2**63 - 1)
    level: Literal["", "info", "warning", "error"] = ""
    trace: str = Field(default="", pattern=r"^(|[a-f0-9]{32})$")


async def dispatch(page, bots, username, call):
    """Reuse ledger semantics with transaction-bound dashboard authorization.

    Args:
        page: Live authenticated plugin page.
        bots: Persisted dashboard bot service.
        username: Verified dashboard JWT subject, never payload data.
        call: Validated allowlisted request.

    Returns:
        Existing cashier result without Access credentials or local login grants.
    """
    runtime = getattr(page, "cashback", None)
    service = getattr(runtime, "service", None)
    if service is None:
        raise CashbackError("unavailable", "邀请结账服务尚未启用。", 503)
    config = bots.get_bot(service.collector_id)["bot"]
    scope_guard = page._scope_guard(bots, config, None)
    allowed = set(config.get("enabled_groups", []))
    account = str(config.get("account_id", ""))
    writes = call.action in {
        "payments",
        "price-preview",
        "admin/groups",
        "admin/risk/release",
    }
    merged = getattr(getattr(page, "plugin", None), "open_service", None)
    scoped_group = call.payload.get("group", call.payload.get("id"))
    if scoped_group is not None and not isinstance(scoped_group, str):
        raise ValueError("invalid_group")
    tenant = (
        copy.deepcopy(merged.tenants.get((account, scoped_group), {}))
        if merged and merged.integrated and scoped_group
        else None
    )

    def guard(group=None):
        scope_guard()
        if getattr(getattr(page, "cashback", None), "service", None) is not service:
            raise CashbackError("service_changed", "结账服务已变化，请刷新。", 409)
        if group is not None:
            if group not in allowed:
                raise CashbackError(
                    "group_forbidden", "请选择采集机器人的已勾选群。", 403
                )
            saved = service.store.get_group(group)
            if saved and (
                saved["bot_id"] != service.collector_id or saved["account"] != account
            ):
                raise CashbackError("collector_changed", "群账本所属账号已变化。", 409)
            if writes and merged and merged.integrated:
                current = merged.tenants.get((account, group))
                if (
                    not merged.active
                    or merged.settings["paused"]
                    or not current
                    or not current["active"]
                    or not current["present"]
                    or current["paused"]
                    or dict(current) != tenant
                ):
                    raise CashbackError(
                        "business_service_inactive",
                        "本群业务已暂停或授权状态变化，当前仅可查询历史账本。",
                        409,
                    )

    guard()
    actor = DashboardFinanceActor(username, guard)
    action, payload = call.action, call.payload
    token = WRITE_GUARD.set(lambda: guard(payload.get("group", payload.get("id"))))
    try:
        if action in {"me", "status", "groups", "admin/catalog"}:
            if payload:
                raise ValueError("unexpected_payload")
            if action == "me":
                return {"name": "管理面板总控", "role": "admin", "uid": ""}
            if action == "status":
                adapter = page._adapter(service.collector_id)
                active = bool(
                    adapter
                    and config.get("enable", True)
                    and adapter.config.get("_business_active") is not False
                    and not adapter.config.get("_open_paused")
                    and str(adapter.account) == account
                    and adapter in service.adapters()
                )
                return {"read_only": not active, "api_version": 1}
            if action == "admin/catalog":
                saved = {
                    item["id"]: item
                    for item in await asyncio.to_thread(service.store.groups, actor)
                }
                adapter = page._adapter(service.collector_id)
                result = [
                    {
                        "id": group,
                        "bot_id": service.collector_id,
                        "name": service.group_name(adapter, group)
                        if adapter
                        else saved.get(group, {}).get("name", group),
                    }
                    for group in sorted(allowed)
                ]
                guard()
                return result
            saved = {
                item["id"]: item
                for item in await asyncio.to_thread(service.store.groups, actor)
                if item["account"] == account and item["bot_id"] == service.collector_id
            }
            adapter = page._adapter(service.collector_id)
            result = [
                {
                    **saved.get(
                        group,
                        {
                            "id": group,
                            "rate": None,
                            "agent_rate": None,
                            "contact": "",
                            "enabled": 0,
                            "version": 0,
                            "error": "",
                            "scanned": 0,
                            "require_speech": 0,
                            "agent_only": 0,
                            "qualification_seconds": 10800,
                        },
                    ),
                    "name": service.group_name(adapter, group)
                    if adapter
                    else saved.get(group, {}).get("name", group),
                    "bot_id": service.collector_id,
                    "configured": group in saved,
                }
                for group in sorted(allowed)
            ]
            guard()
            return [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"cashier", "account"}
                }
                for item in result
                if item["id"] in allowed
            ]
        if action == "client-error":
            body = ClientError.model_validate(payload)
            await asyncio.to_thread(
                service.store.operation_log,
                "dashboard_cashier_client_error",
                actor=str(actor),
                level="warning",
                code=body.kind,
                sample_seconds=60,
            )
            return {"status": "logged"}
        if action == "entries":
            query = LedgerQuery.model_validate(payload)
            guard(query.group)
            if service.store.get_group(query.group) is None:
                return {
                    "items": [],
                    "total": 0,
                    "amount": 0,
                    "payable_amount": 0,
                    "page": 1,
                    "page_size": 5 if query.pagination == "inviters" else 100,
                    "page_count": 1,
                    "pagination": query.pagination,
                    "section_total": 0,
                    "sections": [],
                }
            result = await asyncio.to_thread(
                service.store.list_entries, actor, **query.model_dump()
            )
            guard(query.group)
            result.pop("group", None)
            for row in result["items"]:
                row["can_correct"] = row["state"] == "paid"
                row.pop("paid_by", None)
            return result
        if action == "audit":
            query = AuditQuery.model_validate(payload)
            guard(query.group)
            result = await asyncio.to_thread(
                service.store.read_logs, **query.model_dump()
            )
        elif action in {"inviters", "admin/risk"}:
            query = GroupQuery.model_validate(payload)
            guard(query.group)
            result = await asyncio.to_thread(
                service.store.inviter_options
                if action == "inviters"
                else service.risk_cases,
                actor,
                query.group,
            )
        elif action == "admin/groups":
            body = GroupRule.model_validate(payload)
            guard(body.id)
            adapter = service.adapter(body.bot_id)
            live_guard = service._lifecycle_guard(
                body.id, {"bot_id": body.bot_id, "account": account}
            )
            live_guard()
            if adapter is not page._adapter(service.collector_id):
                raise CashbackError("collector_changed", "采集机器人已变化。", 409)
            config_body = body.model_dump()
            for field in ("require_speech", "agent_only"):
                if field not in body.model_fields_set:
                    config_body.pop(field)
            live_token = WRITE_GUARD.set(live_guard)
            try:
                await service.configure(actor, config_body)
            finally:
                WRITE_GUARD.reset(live_token)
            result = {"status": "saved"}
        elif action == "price-preview":
            body = PricePreview.model_validate(payload)
            guard(body.group)
            result = await service.price_preview(
                actor,
                body.group,
                [item.model_dump() for item in body.items],
                body.category,
            )
        elif action == "admin/risk/release":
            body = RiskRelease.model_validate(payload)
            guard(body.group)
            result = await service.release_risk(
                actor, body.group, body.inviter, body.reason
            )
        else:
            body = Payment.model_validate(payload)
            guard(body.group)
            result = await service.mutate(
                actor,
                body.group,
                body.operation,
                [item.model_dump() for item in body.items],
                body.request_id,
                body.reason,
                body.category,
                body.inviter,
                price_confirmation=body.price_confirmation.model_dump()
                if body.price_confirmation
                else None,
                actual_payment_confirmed=body.actual_payment_confirmed,
            )
        guard(payload.get("group", payload.get("id")))
        return result
    finally:
        WRITE_GUARD.reset(token)
