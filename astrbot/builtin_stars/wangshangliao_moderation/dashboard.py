"""Authenticated plugin settings and read-only moderation audit."""

import asyncio
import copy
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from astrbot.api.web import request
from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.diagnostics import (
    ERROR_LABELS,
    Diagnostics,
)
from astrbot.core.platform.sources.wangshangliao.identity import account_nickname
from astrbot.core.platform.sources.wangshangliao.policy import validate_policy
from astrbot.core.platform.sources.wangshangliao.schedule_store import (
    database as schedule_database,
)
from astrbot.core.platform.sources.wangshangliao.schedule_store import (
    group_lock,
)
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.dashboard.api.auth import require_scope
from astrbot.dashboard.responses import ApiError, ok

from .activities import clean as clean_activity_text
from .activity_store import database as activity_database
from .cashback_store import CashbackError
from .friend_auto import FriendAutoApproval
from .recall_audit import read_native_audit
from .settings import business_revision

PLUGIN_NAME = "wangshangliao_moderation"
BLACKLIST_MINUTES_ERROR = (
    "黑名单禁言时长未修改：请输入1—41760整分钟，默认41760分钟（29天）；"
    "只影响之后的群内拉黑和真实重入，不追罚旧名单或重设已有禁言，普通禁言24小时上限不变。"
)
POLICY_FIELDS = {
    "enabled",
    "automation_enabled",
    "recall_enabled",
    "progressive_mute",
    "cooldown_seconds",
    "semantic",
    "mute_keywords",
    "kick_keywords",
}


def revision(config: dict) -> str:
    """Fingerprint a saved configuration without exposing its full contents.

    Args:
        config: Saved platform configuration.

    Returns:
        Stable configuration revision.
    """
    return business_revision(config)


class DashboardPage:
    """Expose settings without adding a new sanction execution entry point."""

    def __init__(self, context):
        self.context = context
        self.active = False
        self.save_lock = asyncio.Lock()
        self.directory_lock = asyncio.Lock()
        self.directory_refresh_at = {}
        self.friend_auto = FriendAutoApproval(context)

    def register(self):
        """Register the plugin-local routes when the plugin starts."""
        self.active = True
        # Plugin reloads replace matching routes but do not remove retired ones.
        routes = self.context.registered_web_apis
        routes[:] = [api for api in routes if api[0] != f"/{PLUGIN_NAME}/friends"]
        for endpoint, handler, method in (
            ("settings", self.settings, "GET"),
            ("settings", self.save, "POST"),
            ("audit", self.audit, "GET"),
            ("cashback-logs", self.cashback_logs, "GET"),
            ("cashier", self.cashier, "POST"),
            ("invitations", self.invitations, "GET"),
            ("activities", self.activities, "GET"),
            ("activities", self.save_activities, "POST"),
            ("schedule", self.schedule, "GET"),
            ("schedule", self.pause_schedule, "POST"),
            ("groups", self.groups, "GET"),
            ("friend-approval", self.friend_approval, "GET"),
            ("friend-approval", self.friend_approval, "POST"),
            ("cards", self.cards, "POST"),
            ("test-window", self.test_window, "GET"),
            ("test-window", self.test_window, "POST"),
            ("operations", self.operations, "GET"),
            ("group-managers", self.group_managers, "GET"),
            ("group-managers", self.group_managers, "POST"),
            ("open-service", self.open_service, "GET"),
            ("open-service", self.open_service, "POST"),
        ):
            if method == "POST" and endpoint not in {"open-service", "cashier"}:
                handler = self.governed_control(handler)
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                [method],
                "Wangshangliao moderation settings and audit",
            )

    def governed_control(self, handler):
        """Fence scoped legacy writes while retaining their native authorization."""
        from functools import wraps

        @wraps(handler)
        async def invoke():
            service = getattr(getattr(self, "plugin", None), "open_service", None)
            governance = getattr(service, "governance", None)
            if governance is None or not governance.policy()["enabled"]:
                return await handler()
            await self.authorize()
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("bot_id"), str):
                return await handler()
            if not body.get("group_id"):
                return await handler()
            adapter = self._adapter(body["bot_id"])
            if adapter is None:
                return await handler()
            return await governance.run(
                "control",
                adapter,
                handler,
                group=body.get("group_id", ""),
            )

        return invoke

    async def open_service(self):
        """Dispatch authenticated total-control operations inside AstrBot."""
        from .open_rpc import OpenCall
        from .open_store import OpenError

        await self.authorize()
        if request._request.method == "POST":
            raw = bytearray()
            async for chunk in request._request.stream():
                raw.extend(chunk)
                if len(raw) > 7 * 1024 * 1024:
                    raise ApiError(
                        "图片或请求过大，请选择5MB以内的原图。", status_code=413
                    )
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ApiError("开放服务参数无效", status_code=400) from None
        else:
            body = {"action": "status", "payload": {}}
        if (
            not isinstance(body, dict)
            or set(body) != {"action", "payload"}
            or not isinstance(body["payload"], dict)
        ):
            raise ApiError("开放服务参数无效", status_code=400)
        payload = dict(body["payload"])
        integrated = getattr(getattr(self, "plugin", None), "open_service", None)
        integrated = integrated if integrated and integrated.integrated else None
        if integrated is None:
            raise ApiError(
                "独立开放服务已停用，请启用 AstrBot 中的合并模式后刷新。",
                status_code=503,
            )
        try:
            call = OpenCall(
                action=body["action"],
                actor="dashboard/" + request.username,
                payload=payload,
            )
            if not integrated.active:
                raise OpenError("service_unavailable")
            async with integrated.rpc.lock:
                return ok(await integrated.rpc.dispatch(call))
        except OpenError as exc:
            if str(exc) in {
                "advertisement_image_invalid",
                "advertisement_image_scope",
                "advertisement_image_quota",
            }:
                messages = {
                    "advertisement_image_invalid": "请选择5MB以内的JPG、PNG或WebP静态图片，不支持动图或超大尺寸。",
                    "advertisement_image_scope": "图片不属于当前机器人和群，请重新上传并预览。",
                    "advertisement_image_quota": "本群图片上传过多，请稍后再试；未保存的旧图片会自动清理。",
                }
                raise ApiError(messages[str(exc)], status_code=400) from None
            if str(exc) in {
                "open_service_not_connected",
                "open_response_limit",
                "operation_unconfirmed",
                "open_service_failed",
            }:
                raise ApiError(
                    "开放服务未连接或操作结果未确认，请刷新核对。", status_code=503
                ) from None
            if str(exc) in {
                "tenant_changed",
                "settings_changed",
                "activation_changed",
                "request_rejected",
                "quota_changed",
                "group_ai_changed",
                "ai_profile_changed",
                "billing_changed",
                "billing_price_changed",
                "business_rules_changed",
                "business_confirmation_invalid",
            }:
                raise ApiError(
                    "开放服务设置或参数已变化，请刷新核对后重新保存。", status_code=409
                ) from None
            if str(exc) in {"ai_config_missing", "ai_persona_missing"}:
                raise ApiError(
                    "固定配置或人格已不存在，请联系服务端核实，不会自动切换到其他业务。",
                    status_code=409,
                ) from None
            if str(exc) in {"ai_profile_binding_fixed", "ai_profile_missing"}:
                raise ApiError(
                    "本业务使用固定专属配置和人格；请先创建专用配置，不能切换到其他业务。",
                    status_code=409,
                ) from None
            if str(exc) in {"ai_runner_unsupported", "provider_unavailable"}:
                raise ApiError(
                    "请选择本地对话配置和已启用的HTTPS兼容聊天模型，不支持代理工作流。",
                    status_code=409,
                ) from None
            if str(exc) in {"ai_profile_invalid", "ai_persona_oversized"}:
                raise ApiError(
                    "AI配置参数无效或人格过长，请核对数值范围及所选人格。",
                    status_code=409,
                ) from None
            if str(exc) == "manager_required":
                raise ApiError(
                    "只能给该群已核验且获授权的管理员授予套餐，请先核对名单。",
                    status_code=409,
                ) from None
            if body["action"] == "member_blacklist" and str(exc) in {
                "operation_revoked",
                "membership_changed",
                "group_unavailable",
            }:
                raise ApiError(
                    "黑名单未修改：机器人或本人的当前群权限、成员身份或群状态已变化。"
                    "请确认机器人是本群管理员、群已开通且未暂停，然后刷新核对。",
                    status_code=409,
                ) from None
            billing_messages = {
                "member_list_conflict": "名单未修改：该成员已在本群另一份名单中，请先移除另一名单，再重新添加。黑白名单不能同时添加，历史记录不会自动删除。",
                "blacklist_permission": "黑名单未修改：请先核对当前群、本人及机器人的原生管理权限和本群授权，然后刷新。",
                "blacklist_changed": "黑名单未修改：当前群配置、名单或禁言时长版本、成员身份已变化。请刷新；成员操作须重新搜索，时长设置请重新预览。",
                "blacklist_minutes": BLACKLIST_MINUTES_ERROR,
                "blacklist_version_changed": "黑名单或禁言时长已被修改，本次未保存；请刷新后重新预览并本人确认。",
                "blacklist_confirmation": "黑名单未修改：确认已过期、已使用或不属于当前群，请重新预览并本人确认。",
                "blacklist_selection": "黑名单未修改：搜索结果已变化或序号无效，请重新搜索并选择唯一成员。",
                "blacklist_protected": "黑名单未修改：群主、管理员和机器人属于受保护目标，请选择普通成员。",
                "blacklist_narrow": "匹配成员过多，未修改黑名单。请填写更完整的成员名称，缩小搜索范围。",
                "blacklist_query": "请填写有效的成员名称后搜索；不会根据昵称或账号直接猜人执行。",
                "blacklist_target": "黑名单未修改：请原生回复一条同群成员消息或真正@一名成员，不要同时指定多人。",
                "blacklist_quote": "黑名单未修改：原生引用无法在同群消息记录中核实，请重新回复目标原消息或真正@一人。",
                "blacklist_arguments": "黑名单参数无效，未修改名单。请刷新当前群页面，重新搜索、预览并确认。",
                "test_blacklist_accounts": "黑名单验收未开始：必须使用既定的三个接入账号，不能替换账号或新增连接。",
                "test_blacklist_window": "黑名单验收未开始：请核对测试群、目标账号、操作标记及300秒以内的测试窗口。",
                "test_blacklist_revoked": "黑名单验收已阻断：当前群开关、管理员授权或账号绑定已变化，请核对后重新开窗。",
                "test_blacklist_identity": "黑名单验收已阻断：三个原接入账号必须在线、启用且身份配置有效，请核对账号状态。",
                "test_blacklist_existing": "黑名单验收未开始：已有夹具或名单记录，请先核对原验收状态，不要新建操作绕过。",
                "test_blacklist_unresolved": "黑名单验收已挂起：之前操作尚未核实，请检查原始回执和群成员状态，不会自动重试。",
                "mute_member_changed": "成员身份或群角色已变化，本次未解禁，请刷新名单。",
                "member_no_longer_muted": "该成员已不在当前禁言名单中，请刷新。",
                "ai_admission_unavailable": "仅可升级未暂停的已开通仅规则群，请核对本群状态及服务开关后重新预览。",
                "confirmation_invalid": "确认已过期、已使用或群配置及成员身份已变化，请重新预览。",
                "manager_native_role_required": "只能选择本群的群主或群管理员。普通成员不能获得群管理授权，请刷新名单后重新选择。",
                "tenant_rules_refresh": "本群已有组合规则，请刷新页面后重新保存，避免覆盖已有规则。",
                "custom_rules_invalid": "组合规则未保存：每群最多20条，名称1—24字，每条须填写2—5个不同的词，每词2—40字；名称不能重复。",
                "custom_rules_redundant": "组合词不能互相包含，例如「收款」和「收款截图」会退化成一个条件，请换成不同的条件。",
                "tenant_invalid": "本群设置未保存，请核对关键词、组合规则及管理员名单。关键词最多50个，每个1—100字且不能重复。",
                "finance_secondary_security_required": (
                    "旺商聊暂不允许此机器人读取成员（1400）。"
                    "请核对官方App中对应账号的二级安全码设置；"
                    "若已设置仍报错，请核对登录状态或联系旺商聊客服。不要把安全码发给机器人。"
                ),
                "finance_directory_unavailable": "群成员身份暂时无法核验，本次没有授予财务权限，请稍后刷新。",
                "finance_test_window_required": "请先为指定测试账号和测试群打开受控测试窗口。",
                "finance_test_changed": "测试结账状态已变化，请刷新后重试。",
                "test_lottery_not_due": "红包抽奖尚未到设定开奖时间，请到时再执行；不会提前公布或占用开奖回执。",
                "manager_quota_retired_use_plans": "旧增额入口已停用，请刷新页面后在AI套餐中授予Plus或Pro。",
                "billing_contact_required": "请先填写统一升级客服名称和联系方式。",
                "billing_confirmation_invalid": "套餐确认已过期、已使用或内容变化，请重新预览。",
                "billing_money_invalid": "金额格式不正确，请输入最多六位小数的非负人民币金额。",
                "billing_grant_invalid": "套餐授予参数无效，请核对套餐和实际收款金额。",
                "billing_price_invalid": "计费依据不完整，请核对输入输出价格、倍率、全部计费Token上界和核验记录。",
            }
            if str(exc) in billing_messages:
                raise ApiError(billing_messages[str(exc)], status_code=409) from None
            if str(exc) == "production_account_forbidden":
                raise ApiError(
                    "该账号正在生产使用，不能绑定到开放服务。", status_code=409
                ) from None
            if str(exc) == "use_platform_login":
                raise ApiError(
                    "请在AstrBot消息平台页面接入账号，此处直接使用已接入实例。",
                    status_code=409,
                ) from None
            raise ApiError(
                "开放服务校验未通过，请核对账号、当前群成员身份及参数。",
                status_code=409,
            ) from None
        except CashbackError as exc:
            raise ApiError(exc.text, status_code=exc.status) from None
        except ProtocolError as exc:
            if str(exc) == "blacklist_minutes":
                raise ApiError(
                    BLACKLIST_MINUTES_ERROR,
                    status_code=400,
                ) from None
            if str(exc) == "blacklist_version_changed":
                raise ApiError(
                    "黑名单或禁言时长已被修改，本次未保存；请刷新后重新预览并本人确认。",
                    status_code=409,
                ) from None
            code = (
                str(exc)
                if str(exc) in ERROR_LABELS
                or str(exc)
                in {
                    "members_shape",
                    "member_identity",
                    "member_cursor",
                    "member_pagination",
                    "member_limit",
                    "body_limit",
                    "response_limit",
                    "body_authentication_or_compression",
                }
                else "operation_failed"
            )
            number = exc.number if isinstance(exc, BusinessError) else None
            Diagnostics(
                next(
                    (
                        adapter.meta().id
                        for adapter in integrated.adapters()
                        if str(adapter.account) == str(payload.get("account", ""))
                    ),
                    "dashboard",
                )
            ).emit(
                "dashboard",
                "rejected",
                failed=True,
                error=code,
                action=body["action"],
                group=str(payload.get("group_id", "")),
                provider_code=number,
            )
            messages = {
                "member_identity": "群成员的账号资料不完整或相互冲突，暂时无法核对身份。其他群不受影响。",
                "members_shape": "旺商聊返回的成员资料格式暂不兼容，请联系机器人服务管理员。",
                "member_pagination_incomplete": "旺商聊尚未返回完整成员名单，不能使用不完整名单进行管理或结账。",
                "body_limit": "旺商聊返回的资料超过安全读取上限，本次未使用不完整资料。",
                "response_limit": "旺商聊返回的资料超过安全读取上限，本次未使用不完整资料。",
                "secondary_security_code_required": "旺商聊要求此账号完成二级安全验证，请在官方App核对设置并重新登录。不要把安全码发给机器人。",
                "secondary_security_unverified": "此账号尚未完成旺商聊二级安全验证，请在登录页面完成验证。",
                "reauth_required": "此账号的登录已失效，请在消息平台页面重新登录。",
                "rate_limited": "旺商聊要求暂缓请求，请稍后再试，不要连续刷新。",
                "transport": "读取旺商聊资料时连接中断，请稍后核对；这不代表账号被封。",
                "business_rejected": "旺商聊未接受本次请求，请核对对应群权限；这不代表账号被封。",
            }
            raise ApiError(
                messages.get(
                    code, "旺商聊资料未能核对，请稍后刷新或联系机器人服务管理员。"
                ),
                status_code=429 if code == "rate_limited" else 502,
                data={"error": code, "provider_code": number},
            ) from None
        except ValueError as exc:
            if (
                body["action"] == "member_blacklist"
                and payload.get("operation") == "settings_preview"
                and isinstance(exc, ValidationError)
                and any(error["loc"][-1:] == ("minutes",) for error in exc.errors())
            ):
                raise ApiError(BLACKLIST_MINUTES_ERROR, status_code=400) from None
            raise ApiError(
                "参数格式或字段范围不正确，请核对后重新预览。",
                status_code=400,
            ) from None
        except OSError:
            raise ApiError(
                "开放服务未连接或操作未确认，请刷新核对；生产邀请返现不受此开关控制。",
                status_code=503,
            ) from None

    async def cashier(self):
        """Serve total-control settlement operations through the plugin bridge.

        Returns:
            A cashier result from the existing shared ledger.

        Raises:
            ApiError: If dashboard authentication, scope or input is invalid.
        """
        from .cashback_dashboard import CashierCall, dispatch

        bots = await self.authorize()
        auth = await require_scope(request._request, "system")
        if auth.via != "jwt":
            raise ApiError("结账操作须使用管理面板总控登录", status_code=403)
        try:
            body = await request.json()
            if len(json.dumps(body).encode()) > 65536:
                raise ValueError("body_too_large")
            call = CashierCall.model_validate(body)
            return ok(await dispatch(self, bots, auth.username, call))
        except CashbackError as exc:
            raise ApiError(exc.text, status_code=exc.status) from None
        except ValueError:
            raise ApiError(
                "结账参数无效或配置已变化，请刷新核对", status_code=400
            ) from None

    async def cashback_logs(self):
        """Read the shared cashier log using existing dashboard authorization.

        Returns:
            Group-scoped invitation bookkeeping and settlement diagnostics.

        Raises:
            ApiError: If the collector, selected group or runtime is unavailable.
        """
        bots = await self.authorize()
        runtime = getattr(self, "cashback", None)
        service = getattr(runtime, "service", None)
        if service is None:
            raise ApiError("邀请记账／结账服务尚未启用", status_code=503)
        bot_id = request.query.get("bot_id", "")
        group = request.query.get("group_id", "")
        try:
            config = bots.get_bot(bot_id)["bot"]
            if bot_id != service.collector_id or group not in config.get(
                "enabled_groups", []
            ):
                raise ApiError("请选择指定采集机器人的已勾选群", status_code=403)
            guard = self._scope_guard(bots, config, group)
            guard()
            before = int(request.query.get("before", "0"))
            rows = await asyncio.to_thread(
                service.store.read_logs,
                group,
                before,
                request.query.get("level", ""),
                request.query.get("trace", ""),
                include_global=request.query.get("include_global", "0") == "1",
            )
            await self.authorize()
            guard()
            await asyncio.to_thread(
                service.store.operation_log,
                "dashboard_log_query",
                actor="dashboard:" + str(request.username),
                group=group,
                bot_id=bot_id,
                detail={"count": len(rows)},
            )
            guard()
            return ok({"items": rows, "collector_id": service.collector_id})
        except ValueError:
            raise ApiError("实例或日志筛选无效", status_code=400) from None
        except CashbackError as exc:
            raise ApiError(exc.text, status_code=exc.status) from None

    async def group_managers(self):
        """Read or replace dashboard-owned grants without writing cashback.

        Returns:
            Exact group version, assigned accounts and current member choices.

        Raises:
            ApiError: If dashboard identity, group, account or version changed.
        """
        service = await self.authorize()
        merged = getattr(getattr(self, "plugin", None), "open_service", None)
        if merged is not None and merged.integrated:
            raise ApiError(
                "群管理员已统一到群管理页面，请在群管理中预览并确认授权。",
                status_code=409,
            )
        body = (
            await request.json()
            if request._request.method == "POST"
            else dict(request.query)
        )
        if not isinstance(body, dict) or set(body) - {
            "bot_id",
            "group_id",
            "users",
            "version",
            "revision",
        }:
            raise ApiError("群管理员参数无效", status_code=400)
        bot_id, group = body.get("bot_id", ""), body.get("group_id", "")
        try:
            saved = service.get_bot(bot_id)["bot"]
        except (ValueError, TypeError):
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        adapter = self._adapter(bot_id)
        management = getattr(self, "management", None)
        if (
            management is None
            or adapter is None
            or saved.get("type") != "wangshangliao"
            or not isinstance(group, str)
            or group not in saved.get("enabled_groups", [])
            or adapter.connection_state != "online"
        ):
            raise ApiError("请选择已连接旺商聊实例的启用群", status_code=409)
        account = str(adapter.account)
        expected = revision(saved)

        def authorize():
            current = service.get_bot(bot_id)["bot"]
            if (
                not self.active
                or str(adapter.account) != account
                or str(current.get("account_id")) != account
                or group not in current.get("enabled_groups", [])
                or revision(current) != expected
            ):
                raise ProtocolError("group_management_changed")

        guard = account_guard(adapter, authorize)
        try:
            guard()
            if request._request.method == "POST":
                if body.get("revision") != expected:
                    raise ProtocolError("group_management_changed")
                version = await management.save(
                    adapter,
                    group,
                    body.get("users"),
                    body.get("version"),
                    "dashboard:" + request.username,
                    guard,
                )
                return ok({"version": version}, message="群管理员已保存")
            roster = await adapter.get_moderation_members(group)
            guard()
            if not roster.get("complete"):
                raise ProtocolError("group_management_members")
            key = management.key(adapter, group)
            from .group_management import native_manager_ids

            eligible = native_manager_ids(roster.get("groupMemberInfo", []), account)
            return ok(
                {
                    "version": management.versions.get(key, 0),
                    "revision": expected,
                    "users": list(management.assignments.get(key, ())),
                    "members": [
                        {
                            "id": str(m["userId"]),
                            "name": str(
                                m.get("groupMemberNick")
                                or m.get("userNick")
                                or m["userId"]
                            )[:128],
                        }
                        for m in roster.get("groupMemberInfo", [])
                        if str(m["userId"]) in eligible
                    ],
                }
            )
        except ProtocolError as exc:
            raise ApiError(
                "群、账号、成员或授权版本已变化，请刷新核对。",
                status_code=400 if str(exc) == "group_management_arguments" else 409,
            ) from None

    async def authorize(self):
        """Require a live plugin and a Dashboard principal with bot access.

        Returns:
            Existing bot configuration service.

        Raises:
            ApiError: If authentication or plugin lifecycle is invalid.
        """
        if not self.active or request.plugin_name != PLUGIN_NAME:
            raise ApiError("插件未启用", status_code=403)
        auth = await require_scope(request._request, "bot")
        if auth.via != "jwt":
            raise ApiError("请使用管理面板登录账号", status_code=403)
        return request._request.app.state.services.bots

    async def settings(self):
        """Return public bot settings and runtime state, never credentials.

        Returns:
            Dashboard response containing configured instances and models.
        """
        service = await self.authorize()
        adapters = {
            adapter.meta().id: adapter
            for adapter in self.context.platform_manager.platform_insts
            if adapter.meta().name == "wangshangliao"
        }
        bots = []
        for config in service.list_bots(type_="wangshangliao")["bots"]:
            adapter = adapters.get(config["id"])
            stats = adapter.get_stats() if adapter else {}
            names = dict(getattr(adapter, "group_names", {}) or {})
            for identity, item in (getattr(adapter, "group_catalog", {}) or {}).items():
                if item.get("name") and item["name"] != identity:
                    names.setdefault(identity, item["name"])
            if (
                adapter
                and getattr(adapter, "business", None)
                and stats.get("connection_state") == "online"
            ):
                try:
                    async with self.directory_lock:
                        if str(config.get("account_id")) != str(adapter.account):
                            raise ProtocolError("database_authority_changed")
                        key = (config["id"], str(adapter.account))
                        if (
                            time.monotonic()
                            - self.directory_refresh_at.get(key, -float("inf"))
                            >= 30
                        ):
                            self.directory_refresh_at[key] = time.monotonic()
                            guard = account_guard(adapter)
                            guard()
                            directory = await asyncio.wait_for(
                                adapter.business.request(
                                    "/v1/group/get-group-list", {"v": "0"}
                                ),
                                timeout=5,
                            )
                            guard()
                            if not self.active or str(
                                service.get_bot(config["id"])["bot"]["account_id"]
                            ) != str(adapter.account):
                                raise ProtocolError("database_authority_changed")
                            if not isinstance(directory, dict) or any(
                                not isinstance(directory.get(field), list)
                                for field in ("owner", "member")
                            ):
                                raise ProtocolError("groups_shape")
                            refreshed = {}
                            for item in directory["owner"] + directory["member"]:
                                if not isinstance(item, dict):
                                    raise ProtocolError("groups_shape")
                                identity = str(item.get("groupId", ""))
                                if (
                                    not identity.isascii()
                                    or not identity.isdigit()
                                    or int(identity) <= 0
                                ):
                                    raise ProtocolError("group_identity")
                                name = " ".join(
                                    str(
                                        item.get("groupName") or item.get("name") or ""
                                    ).split()
                                )[:128]
                                if name:
                                    refreshed[identity] = name
                            names.update(refreshed)
                            adapter.group_names = names
                        else:
                            names.update(getattr(adapter, "group_names", {}) or {})
                except ProtocolError as exc:
                    if str(exc) == "database_authority_changed":
                        raise ApiError(
                            "机器人身份或配置已变化，请刷新。", status_code=409
                        ) from None
                except Exception:
                    # A name lookup must not prevent access to policy controls.
                    pass
            bots.append(
                {
                    **{
                        field: copy.deepcopy(config[field])
                        for field in (
                            "id",
                            "nickname",
                            "name",
                            "enable",
                            "account_id",
                            "enabled_groups",
                            "reply_private",
                            "reply_groups",
                            "moderation",
                            "ai_routes",
                            "proactive_send",
                            "developer_test",
                        )
                        if field in config
                    },
                    "nickname": account_nickname(adapter, config),
                    "revision": revision(config),
                    "state": stats.get("connection_state", "stopped"),
                    "current_error": (
                        "nim_kicked"
                        if stats.get("current_error") == "nim_kicked"
                        else ""
                    ),
                    "last_connected_at": stats.get("last_connected_at"),
                    "directory": stats.get("group_directory_state", {}),
                    "business_active": stats.get("business_active", False),
                    "group_names": names,
                }
            )
        providers = [
            {"id": provider.meta().id, "model": provider.get_model()}
            for provider in self.context.get_all_providers()
        ]
        return ok(
            {
                "bots": bots,
                "providers": providers,
                "open_mode": (
                    "integrated"
                    if getattr(getattr(self, "plugin", None), "open_service", None)
                    and self.plugin.open_service.integrated
                    else "isolated"
                ),
            }
        )

    async def save(self):
        """Patch only editable global settings and one enabled group's grants.

        Returns:
            Saved configuration revision.

        Raises:
            ApiError: If input is invalid, stale, or targets another platform.
        """
        service = await self.authorize()
        body = await request.json()
        if (
            not isinstance(body, dict)
            or set(body)
            - {
                "bot_id",
                "revision",
                "group_id",
                "policy",
                "group",
                "reply_private",
                "admin_provider_id",
                "enabled_groups",
                "proactive_send",
                "developer_test",
            }
            or not isinstance(body.get("bot_id"), str)
            or not isinstance(body.get("revision"), str)
            or not isinstance(body.get("group_id", ""), str)
        ):
            raise ApiError("配置参数无效", status_code=400)
        async with self.save_lock:
            try:
                current = service.get_bot(body["bot_id"])["bot"]
            except ValueError:
                raise ApiError("旺商聊实例不存在", status_code=404) from None
            if current.get("type") != "wangshangliao":
                raise ApiError("只能修改旺商聊实例", status_code=400)
            if revision(current) != body["revision"]:
                raise ApiError("配置已被修改，请刷新后重新编辑", status_code=409)
            policy = body.get("policy", {})
            group_settings = body.get("group", {})
            group = body.get("group_id", "")
            merged = getattr(getattr(self, "plugin", None), "open_service", None)
            if (
                merged is not None
                and merged.integrated
                and (
                    any(
                        value != current.get("moderation", {}).get(key)
                        for key, value in policy.items()
                    )
                    if isinstance(policy, dict)
                    else False
                )
            ):
                raise ApiError(
                    "广告、防刷屏和关键词已统一到群管理页面，请逐群保存。",
                    status_code=409,
                )
            if (
                merged is not None
                and merged.integrated
                and (
                    "enabled_groups" in body
                    and body["enabled_groups"] != current.get("enabled_groups", [])
                )
            ):
                raise ApiError(
                    "群开通已统一到群管理页面，请使用开通流程或暂停本群。",
                    status_code=409,
                )
            if (
                not isinstance(policy, dict)
                or set(policy) - POLICY_FIELDS
                or not isinstance(group_settings, dict)
                or set(group_settings)
                - {
                    "permissions",
                    "auto_kick",
                    "card_auto",
                    "card_suffix",
                    "reply",
                    "customer_provider_id",
                }
                or (group and group not in current.get("enabled_groups", []))
                or (not group and group_settings)
                or ("reply_private" in body and type(body["reply_private"]) is not bool)
                or (
                    "admin_provider_id" in body
                    and not isinstance(body["admin_provider_id"], str)
                )
                or (
                    "customer_provider_id" in group_settings
                    and not isinstance(group_settings["customer_provider_id"], str)
                )
                or any(
                    type(group_settings[field]) is not bool
                    for field in ("auto_kick", "card_auto", "reply")
                    if field in group_settings
                )
            ):
                raise ApiError("配置字段或目标群无效", status_code=400)
            config = copy.deepcopy(current)
            if merged is not None and merged.integrated:
                from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
                    BUSINESS_FIELDS,
                    runtime,
                )

                snapshot = runtime.snapshot(current)
                if snapshot is None:
                    raise ApiError("业务配置尚未载入，请刷新。", status_code=409)
                # Persist native patches on the base, not the effective tenant overlay.
                for field in BUSINESS_FIELDS:
                    config.pop(field, None)
                config.update(snapshot.values())
            for field in ("enabled_groups", "proactive_send", "developer_test"):
                if field in body:
                    if (
                        merged is not None
                        and merged.integrated
                        and field == "enabled_groups"
                    ):
                        continue
                    config[field] = copy.deepcopy(body[field])
            if group and group not in current.get("enabled_groups", []):
                raise ApiError("目标群未勾选，请先保存启用群聊", status_code=400)
            moderation = config.setdefault("moderation", {})
            if merged is None or not merged.integrated:
                moderation.update(copy.deepcopy(policy))
            for field in ("permissions", "auto_kick", "card_auto", "card_suffix"):
                if field in group_settings:
                    moderation.setdefault(field, {})[group] = group_settings[field]
            if "reply" in group_settings:
                config.setdefault("reply_groups", {})[group] = group_settings["reply"]
            if "admin_provider_id" in body or "customer_provider_id" in group_settings:
                routes = config.setdefault("ai_routes", {})
                if "admin_provider_id" in body:
                    routes["admin_provider_id"] = body["admin_provider_id"]
                if "customer_provider_id" in group_settings and group:
                    routes.setdefault("groups", {})[group] = group_settings[
                        "customer_provider_id"
                    ]
                routes.setdefault("groups", {}).pop("", None)
                config["ai_routes"] = routes
            if "reply_private" in body:
                config["reply_private"] = body["reply_private"]
            try:
                validate_policy(moderation)
                if (
                    moderation.get("enabled")
                    and moderation.get("card_auto", {}).get(group)
                    and "rename" not in moderation.get("permissions", {}).get(group, [])
                ):
                    raise ValueError(
                        "请启用机器人群管并授权本群修改普通成员群名片，再开启自动规范"
                    )
                semantic = moderation.get("semantic", {})
                if semantic.get("enabled") and not moderation.get(
                    "content_rules_since"
                ):
                    moderation["content_rules_since"] = time.time()
                if semantic.get("enabled") and semantic.get("provider_id"):
                    if semantic["provider_id"] not in {
                        provider.meta().id
                        for provider in self.context.get_all_providers()
                    }:
                        raise ValueError("moderation_provider_unavailable")
                provider_ids = {
                    provider.meta().id for provider in self.context.get_all_providers()
                }
                routes = config.get("ai_routes", {})
                if (
                    routes.get("admin_provider_id")
                    and routes["admin_provider_id"] not in provider_ids
                ):
                    raise ValueError("admin_provider_unavailable")
                if any(
                    provider_id and provider_id not in provider_ids
                    for provider_id in routes.get("groups", {}).values()
                ):
                    raise ValueError("customer_provider_unavailable")
                from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
                    runtime,
                )

                if not runtime.service:
                    raise ApiError("业务服务未启用", status_code=503)
                await runtime.service.save(
                    config, body["revision"], "dashboard:" + request.username
                )
            except ValueError as exc:
                if str(exc) == "configuration_changed_preview_again":
                    raise ApiError(
                        "配置已被修改，请刷新后重试", status_code=409
                    ) from None
                raise ApiError(f"配置无效：{exc}", status_code=400) from None
            return ok(
                {"revision": revision(service.get_bot(config["id"])["bot"])},
                message="配置已保存",
            )

    async def groups(self):
        """Read the authenticated account's complete available group directory."""
        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        try:
            config = service.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        if config.get("type") != "wangshangliao":
            raise ApiError("机器人类型无效", status_code=400)
        adapter = self._adapter(bot_id)
        if adapter is None or adapter.connection_state != "online":
            raise ApiError("机器人未连接，请先在接入器登录", status_code=409)
        from astrbot.core.platform.sources.wangshangliao.directory import (
            group_directory,
        )

        guard = self._scope_guard(service, config, None, adapter)
        guard()
        try:
            directory = await group_directory(adapter.business, adapter.account)
            guard()
            directory["private_targets"] = []
            if getattr(getattr(adapter, "ledger", None), "db", None) is not None:
                from astrbot.core.platform.sources.wangshangliao.event import (
                    is_managed_account,
                )

                async with adapter.ledger.db.execute(
                    "SELECT team,json_extract(payload,'$.name'),json_extract(payload,'$.sender') "
                    "FROM inbox WHERE rowid IN (SELECT MAX(rowid) FROM inbox "
                    "WHERE account=? AND team LIKE 'private/%' GROUP BY team) "
                    "AND json_valid(payload) ORDER BY rowid DESC LIMIT 1000",
                    (adapter.account,),
                ) as cursor:
                    rows = await cursor.fetchall()
                directory["private_targets"] = [
                    {"value": team, "name": str(name or uid), "uid": str(uid)}
                    for team, name, uid in rows
                    if uid
                    and str(uid) != adapter.account
                    and not is_managed_account(str(uid))
                    and team.startswith(f"private/{uid}/")
                ]
            await self.authorize()
            guard()
            return ok(directory)
        except ProtocolError as exc:
            raise ApiError(f"群目录读取失败：{exc}", status_code=400) from None

    async def cards(self):
        """Run fixed card-task actions under a dashboard-bound preview."""
        service = await self.authorize()
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {
            "bot_id",
            "revision",
            "action",
            "group_id",
            "card_job_id",
            "card_member",
            "card_name",
            "cleanup_limit",
            "cleanup_state",
        }:
            raise ApiError("名片操作参数无效", status_code=400)
        adapter = self._adapter(body.get("bot_id", ""))
        if adapter is None:
            raise ApiError("机器人未连接", status_code=409)
        if body.get("revision") != revision(service.get_bot(adapter.meta().id)["bot"]):
            raise ApiError("配置已变化，请刷新后重试", status_code=409)
        action = body.get("action")
        binding = "dashboard/" + request.username
        manager = self.plugin.cards
        try:
            if action in {"card_preview", "cleanup_preview"}:
                result = await manager.preview(
                    adapter,
                    str(body.get("group_id", "")),
                    binding,
                    member=body.get("card_member"),
                    card_name=body.get("card_name"),
                    cleanup=action == "cleanup_preview",
                    cleanup_limit=body.get("cleanup_limit"),
                    cleanup_state=body.get("cleanup_state"),
                )
            elif action == "card_execute":
                result = await manager.start(
                    adapter, str(body.get("card_job_id", "")), binding
                )
            elif action == "card_stop":
                result = await manager.stop(
                    adapter, str(body.get("card_job_id", "")), binding
                )
            elif action == "card_status":
                result = await manager.refresh_status(
                    adapter, str(body.get("card_job_id", "")), binding
                )
            else:
                raise ValueError("card_action_invalid")
            await asyncio.to_thread(
                self.business_settings.log,
                adapter.meta().id,
                binding,
                action,
                "completed",
            )
            return ok(result)
        except (ValueError, ProtocolError) as exc:
            await asyncio.to_thread(
                self.business_settings.log,
                adapter.meta().id,
                binding,
                action or "cards",
                "failed",
                str(exc),
            )
            raise ApiError(f"名片操作失败：{exc}", status_code=400) from None

    async def friend_approval(self):
        """Expose the account-bound approval toggle only to dashboard users.

        Returns:
            Local settings and outcomes, without upstream profile queries.

        Raises:
            ApiError: If parameters, saved identity or settings version are invalid.
        """
        bots = await self.authorize()
        write = request._request.method == "POST"
        body = await request.json() if write else dict(request.query)
        reconcile = (
            write and isinstance(body, dict) and body.get("action") == "reconcile"
        )
        required = (
            {"bot_id", "revision", "version", "action"}
            if reconcile
            else {"bot_id", "revision", "version", "enabled"}
            if write
            else {"bot_id"}
        )
        if (
            not isinstance(body, dict)
            or set(body) != required
            or not isinstance(body.get("bot_id"), str)
            or not body["bot_id"]
            or (
                write
                and (
                    (not reconcile and type(body.get("enabled")) is not bool)
                    or type(body.get("version")) is not int
                    or body["version"] < 1
                    or not isinstance(body.get("revision"), str)
                )
            )
        ):
            raise ApiError("自动同意设置参数无效", status_code=400)
        bot_id = body["bot_id"]
        try:
            config = bots.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        adapter = self._adapter(bot_id)
        if config.get("type") != "wangshangliao" or adapter is None:
            raise ApiError("旺商聊实例未载入", status_code=409)
        saved_revision = revision(config)
        account = str(adapter.account)

        def authorize():
            latest = bots.get_bot(bot_id)["bot"]
            if (
                not self.active
                or latest.get("type") != "wangshangliao"
                or self._adapter(bot_id) is not adapter
                or str(adapter.account) != account
                or str(latest.get("account_id", "")) != account
                or revision(latest) != saved_revision
            ):
                raise ProtocolError("friend_authority_changed")

        guard = account_guard(adapter, authorize)
        try:
            guard()
            if write:
                if body["revision"] != saved_revision:
                    raise ValueError("friend_approval_settings_changed")
                result = (
                    await self.friend_auto.reconcile(adapter, body["version"], guard)
                    if reconcile
                    else await self.friend_auto.configure(
                        adapter, body["enabled"], body["version"], guard
                    )
                )
                await asyncio.to_thread(
                    self.business_settings.log,
                    bot_id,
                    "dashboard/" + request.username,
                    "friend_approval_settings",
                    "saved",
                    "",
                    {"account": str(adapter.account), "enabled": result["enabled"]},
                )
            else:
                result = await self.friend_auto.status(adapter)
            await self.authorize()
            guard()
            return ok(result)
        except (ValueError, ProtocolError) as exc:
            raise ApiError(
                "申请结果仍未确认，没有重新提交同意；请核对官方客户端。"
                if str(exc) == "friend_approval_still_unknown"
                else "自动同意设置或账号已变化，请刷新后核对。",
                status_code=409,
            ) from exc

    async def test_window(self):
        """Inspect or explicitly open a bounded, non-authorizing test window."""
        service = await self.authorize()
        body = (
            await request.json()
            if request._request.method == "POST"
            else dict(request.query)
        )
        if not isinstance(body, dict) or set(body) - {
            "bot_id",
            "revision",
            "action",
            "sender_instance",
            "groups",
            "scopes",
            "keywords",
            "seconds",
            "budget",
        }:
            raise ApiError("测试窗口参数无效", status_code=400)
        adapter = self._adapter(body.get("bot_id", ""))
        if adapter is None:
            raise ApiError("机器人未连接", status_code=409)
        instances = [
            p
            for p in self.context.platform_manager.platform_insts
            if p.meta().name == "wangshangliao"
        ]
        action = body.get("action", "test_status")
        try:
            if action != "test_status":
                if body.get("revision") != revision(
                    service.get_bot(adapter.meta().id)["bot"]
                ):
                    raise ApiError("配置已变化，请刷新后重试", status_code=409)
                if action == "test_close":
                    previous = adapter.test_window
                    if (
                        previous is not None
                        and getattr(previous.sender, "test_command_window", None)
                        is previous
                    ):
                        previous.sender.test_command_window = None
                    adapter.test_window = None
                elif action == "test_open":
                    from astrbot.core.platform.sources.wangshangliao.test_window import (
                        TestWindow,
                    )

                    sender = next(
                        (
                            p
                            for p in instances
                            if p.meta().id == body.get("sender_instance")
                        ),
                        None,
                    )
                    if not sender or any(
                        p.connection_state != "online" for p in (adapter, sender)
                    ):
                        raise ValueError("test_instances_offline")
                    if adapter.test_window and adapter.test_window.status()["active"]:
                        raise ValueError("test_window_already_active")
                    adapter.test_window = TestWindow(
                        adapter,
                        sender,
                        body.get("groups", []),
                        body.get("scopes", []),
                        body.get("keywords", []),
                        body.get("seconds", 300),
                        body.get("budget", 10),
                    )
                    sender.test_command_window = adapter.test_window
                else:
                    raise ValueError("test_action_invalid")
                await asyncio.to_thread(
                    self.business_settings.log,
                    adapter.meta().id,
                    "dashboard:" + request.username,
                    action,
                    "completed",
                )
            return ok(
                {
                    "window": adapter.test_window.status()
                    if adapter.test_window
                    else {"active": False, "remaining": 0, "seconds": 0},
                    "instances": [
                        {"id": p.meta().id, "account": p.account}
                        for p in instances
                        if p is not adapter
                    ],
                }
            )
        except (ValueError, ProtocolError) as exc:
            await asyncio.to_thread(
                self.business_settings.log,
                adapter.meta().id,
                "dashboard:" + request.username,
                action,
                "failed",
                str(exc),
            )
            raise ApiError(f"测试窗口操作失败：{exc}", status_code=400) from None

    async def operations(self):
        """Read policy revisions and sanitized operational errors."""
        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        try:
            if service.get_bot(bot_id)["bot"].get("type") != "wangshangliao":
                raise ValueError("invalid_bot")
            before = int(request.query.get("before", "0"))
        except ValueError:
            raise ApiError("日志筛选无效", status_code=400) from None
        result = await asyncio.to_thread(self.business_settings.history, bot_id, before)
        await self.authorize()
        return ok(result)

    async def audit(self):
        """Read bounded account/group-scoped audit rows without changing ledgers.

        Returns:
            Recent decisions, sanctions, rule audit, mute counts and recall sources.

        Raises:
            ApiError: If the instance or group is not enabled.
        """
        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        group = request.query.get("group_id", "")
        try:
            config = service.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        if config.get("type") != "wangshangliao" or group not in config.get(
            "enabled_groups", []
        ):
            raise ApiError("目标群未启用", status_code=400)
        result = {
            "reviews": [],
            "sanctions": [],
            "counts": [],
            "rules": [],
            "recalls": [],
            "recalls_available": False,
        }
        path = instance_dir(bot_id) / "moderation.sqlite3"
        # Native recall observations may exist even before the first violation.
        audit_uri = (
            f"{path.as_uri()}?mode=ro"
            if path.is_file()
            else "file::memory:?mode=memory"
        )
        account = str(config.get("account_id", ""))
        guard = self._scope_guard(service, config, group)
        try:
            async with AsyncDatabase(
                lambda: sqlite3.connect(audit_uri, uri=True, timeout=1),
                guard=guard,
            ) as db:
                await db.call(setattr, "row_factory", sqlite3.Row)
                tables = {
                    row[0]
                    for row in await db.fetchall(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "semantic_reviews" in tables:
                    for row in await db.fetchall(
                        "SELECT message,result,provider,created FROM semantic_reviews "
                        "WHERE account=? AND group_id=? ORDER BY created DESC LIMIT 50",
                        (account, group),
                    ):
                        assessment = json.loads(row["result"])
                        if not isinstance(assessment, dict):
                            raise ValueError("invalid_review")
                        result["reviews"].append(
                            {
                                "message": row["message"],
                                "provider": row["provider"],
                                "created": row["created"],
                                **{
                                    key: assessment.get(key)
                                    for key in ("decision", "category", "reason")
                                },
                            }
                        )
                if "automatic_mutes" in tables:
                    result["counts"] = [
                        dict(row)
                        for row in await db.fetchall(
                            "SELECT member,SUM(status IN ('accepted','verified')) AS accepted,"
                            "SUM(status='unknown') AS unknown,MAX(created) AS created "
                            "FROM automatic_mutes WHERE account=? AND group_id=? AND closed=0 "
                            "GROUP BY member ORDER BY created DESC LIMIT 100",
                            (account, group),
                        )
                    ]
                for table, action in (
                    ("automatic_mutes", "mute"),
                    ("automatic_kicks", "kick"),
                ):
                    if table in tables:
                        result["sanctions"].extend(
                            {**dict(row), "action": action}
                            for row in await db.fetchall(
                                f"SELECT operation,member,status,created FROM {table} "
                                "WHERE account=? AND group_id=? ORDER BY created DESC LIMIT 50",
                                (account, group),
                            )
                        )
                result["sanctions"] = sorted(
                    result["sanctions"], key=lambda row: row["created"], reverse=True
                )[:50]
                for sanction in result["sanctions"]:
                    if "moderation_action_audit" in tables:
                        failure = await db.fetchone(
                            "SELECT status,submitted,error,provider_code "
                            "FROM moderation_action_audit WHERE operation=? "
                            "AND account=? AND group_id=? AND member=? AND action=?",
                            (
                                sanction["operation"],
                                account,
                                group,
                                str(sanction["member"]),
                                sanction["action"],
                            ),
                        )
                        if failure:
                            sanction.update(
                                action_status=failure[0],
                                action_submitted=failure[1],
                                action_error=failure[2],
                                provider_code=failure[3],
                            )
                    if "operations" in tables:
                        receipt = await db.fetchone(
                            "SELECT result FROM operations WHERE id=?",
                            (sanction["operation"],),
                        )
                        saved = json.loads(receipt[0]) if receipt else {}
                        if (
                            sanction["action"] == "mute"
                            and type(saved.get("minutes")) is int
                        ):
                            sanction["minutes"] = saved["minutes"]
                    if "progressive_actions" in tables:
                        recalled = await db.fetchone(
                            "SELECT recall_status FROM progressive_actions "
                            "WHERE operation=? AND account=? AND group_id=?",
                            (sanction["operation"], account, group),
                        )
                        if recalled:
                            sanction["recall_status"] = recalled[0]
                    if "flood_recalls" in tables and sanction["operation"].startswith(
                        "flood/"
                    ):
                        recalled = await db.fetchone(
                            "SELECT COUNT(*),SUM(status IN ('accepted','verified')),"
                            "SUM(status='rejected') "
                            "FROM flood_recalls WHERE batch=? AND account=? AND group_id=?",
                            (sanction["operation"], account, group),
                        )
                        sanction["recall_status"] = (
                            "accepted"
                            if recalled[0] and recalled[0] == recalled[1]
                            else "rejected"
                            if recalled[0] and recalled[0] == recalled[2]
                            else "unknown"
                            if recalled[0]
                            else "none"
                        )
                if "content_violations" in tables:
                    result["rules"] = [
                        dict(row)
                        for row in await db.fetchall(
                            "SELECT message,sender,category,status,observed FROM content_violations "
                            "WHERE account=? AND group_id=? ORDER BY observed DESC LIMIT 50",
                            (account, group),
                        )
                    ]
                    if "progressive_actions" in tables and result["rules"]:
                        for row in result["rules"]:
                            receipt = await db.fetchone(
                                "SELECT recall_status,result FROM progressive_actions "
                                "WHERE account=? AND group_id=? AND message=? "
                                "ORDER BY created DESC LIMIT 1",
                                (account, group, str(row["message"])),
                            )
                            if receipt:
                                row["recall_status"] = receipt[0]
                                saved = json.loads(receipt[1])
                                if not isinstance(saved, dict):
                                    raise ValueError("invalid_progressive_receipt")
                                number = saved.get("warning_number")
                                if type(number) is int and 1 <= number <= 3:
                                    row["warning_number"] = number
                                elif saved.get("action") in {"mute", "kick"}:
                                    row["warning_status"] = "not_applicable"
                    if "moderation_action_audit" in tables:
                        for row in result["rules"]:
                            failure = await db.fetchone(
                                "SELECT status,submitted,error,provider_code "
                                "FROM moderation_action_audit WHERE operation=? "
                                "AND account=? AND group_id=? AND member=? AND action='recall'",
                                (
                                    f"recall/{account}/{group}/{row['message']}",
                                    account,
                                    group,
                                    str(row["sender"]),
                                ),
                            )
                            if failure:
                                row.update(
                                    recall_status=failure[0],
                                    recall_submitted=failure[1],
                                    recall_error=failure[2],
                                    provider_code=failure[3],
                                )
        except (sqlite3.Error, ValueError):
            raise ApiError(
                "审计记录暂时不可读取，请稍后刷新", status_code=503
            ) from None
        await self.authorize()
        guard()
        adapter = self._adapter(bot_id)
        snapshot = getattr(adapter, "member_snapshots", {}).get(group)
        names, peers = {}, {}
        if (
            isinstance(snapshot, tuple)
            and len(snapshot) == 3
            and snapshot[0] == account
            and isinstance(snapshot[2], dict)
            and snapshot[2].get("complete") is True
        ):
            # Labels come only from this account's complete directory for this group.
            # They are display data, never identity or permission evidence.
            duplicates, seen = set(), set()
            for member in snapshot[2].get("groupMemberInfo", []):
                if not isinstance(member, dict):
                    continue
                uid = str(member.get("userId", ""))
                name = (
                    member.get("userNick")
                    or member.get("name")
                    or member.get("userName")
                )
                if uid in seen:
                    duplicates.add(uid)
                seen.add(uid)
                if uid and isinstance(name, str) and name.strip():
                    names[uid] = name.strip()[:200]
            for uid in duplicates:
                names.pop(uid, None)
            peer_duplicates = set()
            for member in snapshot[2].get("groupMemberInfo", []):
                if not isinstance(member, dict):
                    continue
                uid, peer = str(member.get("userId", "")), str(member.get("nimId", ""))
                if (
                    uid in duplicates
                    or not peer.isascii()
                    or not peer.isdigit()
                    or int(peer) <= 0
                ):
                    continue
                if peer in peers:
                    peer_duplicates.add(peer)
                peers[peer] = (uid, names.get(uid, ""))
            for peer in peer_duplicates:
                peers.pop(peer, None)
        historical = {}
        missing = {
            str(row["message"]): str(row["sender"])
            for row in result["rules"]
            if str(row["sender"]) not in names
        }
        inbox = path.with_name("messages.sqlite3")
        if inbox.is_file():
            try:
                async with AsyncDatabase(
                    lambda: sqlite3.connect(
                        f"{inbox.as_uri()}?mode=ro", uri=True, timeout=1
                    ),
                    guard=guard,
                ) as db:
                    # Exact primary-key lookups only; do not scan chat history or
                    # borrow labels from another account, group or message author.
                    rows = (
                        await db.fetchall(
                            "SELECT message,sender,payload FROM inbox "
                            "WHERE account=? AND team=? AND message IN ("
                            + ",".join("?" for _ in missing)
                            + ")",
                            (account, group, *missing),
                        )
                        if missing
                        else []
                    )
                    recalls = await read_native_audit(
                        db,
                        account,
                        group,
                        result["rules"],
                        peers,
                        str(getattr(adapter, "nim_account", "") or ""),
                    )
                    result["recalls"] = recalls or []
                    result["recalls_available"] = recalls is not None
                for mid, sender, raw in rows:
                    try:
                        item = json.loads(raw)
                        name = item.get("name")
                        if (
                            str(sender) == missing[str(mid)]
                            and str(item.get("sender")) == str(sender)
                            and isinstance(name, str)
                            and name.strip()
                        ):
                            historical[str(mid)] = name.strip()[:200]
                    except (ValueError, TypeError, AttributeError):
                        continue
            except sqlite3.Error:
                pass
        recorded_names = {}
        for row in result["rules"]:
            if str(row["message"]) in historical:
                recorded_names.setdefault(
                    str(row["sender"]), historical[str(row["message"])]
                )
        for key, field in (
            ("counts", "member"),
            ("sanctions", "member"),
            ("rules", "sender"),
        ):
            for row in result[key]:
                uid = str(row[field])
                name = names.get(uid) or (
                    historical.get(str(row["message"]))
                    if key == "rules"
                    else recorded_names.get(uid)
                )
                if name:
                    row[f"{field}_name"] = name
                    row[f"{field}_name_source"] = (
                        "directory" if uid in names else "message"
                    )
        guard()
        return ok(result)

    async def invitations(self):
        """Read member attribution or account-visible invitation records.

        Returns:
            Bounded member/inviter pairs without applying or approving invitations.

        Raises:
            ApiError: If authentication, scope or platform evidence is unavailable.
        """
        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        group = request.query.get("group_id", "")
        member = request.query.get("member_id", "")
        source = request.query.get("source", "members")
        pending_only = request.query.get("pending_only", "true")
        last_id = request.query.get("last_id", "")
        try:
            config = service.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        if (
            config.get("type") != "wangshangliao"
            or not config.get("enable", True)
            or group not in config.get("enabled_groups", [])
            or source not in {"members", "records"}
            or pending_only not in {"true", "false"}
            or (
                last_id
                and (
                    source != "records"
                    or not last_id.isascii()
                    or not last_id.isdigit()
                    or len(last_id) > 20
                )
            )
            or (
                member
                and (
                    not member.isascii()
                    or not member.isdigit()
                    or len(member) > 20
                    or int(member) <= 0
                )
            )
        ):
            raise ApiError("目标群或成员参数无效", status_code=400)
        adapter = next(
            (
                item
                for item in self.context.platform_manager.platform_insts
                if item.meta().id == bot_id and item.meta().name == "wangshangliao"
            ),
            None,
        )
        if not adapter:
            raise ApiError("旺商聊实例未在线", status_code=503)
        guard = self._scope_guard(service, config, group, adapter)
        guard()
        try:
            result = await asyncio.wait_for(
                adapter.get_group_invitation_records(
                    group,
                    member,
                    pending_only=pending_only == "true",
                    last_id=last_id,
                )
                if source == "records"
                else adapter.get_group_inviters(group, member),
                timeout=30,
            )
        except ProtocolError as exc:
            if str(exc) == "invitation_read_cooldown":
                raise ApiError("查询过于频繁，请稍后重试", status_code=429) from None
            if str(exc) == "invitation_member_not_found":
                raise ApiError("该账号不在当前群成员名单中", status_code=404) from None
            raise ApiError(
                "平台邀请归属暂时无法核实，不据此计数", status_code=503
            ) from None
        except TimeoutError:
            raise ApiError("邀请归属查询超时，不据此计数", status_code=503) from None
        await self.authorize()
        guard()
        return ok(result)

    async def activities(self):
        """Read per-group lottery and invitation control state."""
        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        group = request.query.get("group_id", "")
        try:
            config = service.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        if config.get("type") != "wangshangliao" or group not in config.get(
            "enabled_groups", []
        ):
            raise ApiError("目标群未启用", status_code=400)
        adapter = self._adapter(bot_id)
        if not adapter:
            raise ApiError("旺商聊实例未在线", status_code=503)
        guard = self._scope_guard(service, config, group, adapter)
        result = await asyncio.to_thread(
            self._activity_snapshot, adapter, copy.deepcopy(config), group
        )
        await self.authorize()
        guard()
        return ok(result)

    async def save_activities(self):
        """Update next-lottery settings and the uniform invitation reward rate."""
        service = await self.authorize()
        body = await request.json()
        legacy_lottery_fields = {
            "prize",
            "winners",
            "duration_minutes",
            "capacity",
            "invite_gate",
            "contact",
        }
        packet_fields = {"total_amount", "packet_mode", "by_invites"}
        if (
            not isinstance(body, dict)
            or set(body)
            not in (
                {"bot_id", "group_id", "revision", "lottery", "invite_rate"},
                {"bot_id", "group_id", "revision", "lottery"},
                {"bot_id", "group_id", "revision", "invite_rate"},
            )
            or not all(
                isinstance(body.get(key), str)
                for key in ("bot_id", "group_id", "revision")
            )
            or "lottery" in body
            and (
                not isinstance(body["lottery"], dict)
                or set(body["lottery"])
                not in (
                    legacy_lottery_fields,
                    legacy_lottery_fields | {"remarks"},
                    legacy_lottery_fields | packet_fields,
                    legacy_lottery_fields | packet_fields | {"remarks"},
                )
            )
        ):
            raise ApiError("活动配置参数无效", status_code=400)
        try:
            config = service.get_bot(body["bot_id"])["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        group = body["group_id"]
        if config.get("type") != "wangshangliao" or group not in config.get(
            "enabled_groups", []
        ):
            raise ApiError("目标群未启用", status_code=400)
        adapter = self._adapter(body["bot_id"])
        if not adapter:
            raise ApiError("旺商聊实例未在线", status_code=503)

        lottery = body.get(
            "lottery",
            {
                "prize": "",
                "contact": "",
                "winners": 1,
                "duration_minutes": 10,
                "capacity": 0,
                "invite_gate": 0,
            },
        )
        prize = self._activity_text(lottery["prize"], "奖品")
        contact = self._activity_text(lottery["contact"], "领奖联系人")
        remarks = (
            self._activity_text(
                lottery["remarks"], "活动备注", limit=500, multiline=True
            )
            if "remarks" in lottery
            else None
        )
        winners = self._bounded_int(lottery["winners"], 1, 100, "中奖人数")
        duration = self._bounded_int(lottery["duration_minutes"], 1, 10080, "倒计时")
        capacity = self._bounded_int(lottery["capacity"], 0, 10000, "参与上限")
        invite_gate = self._bounded_int(lottery["invite_gate"], 0, 10000, "邀请门槛")
        packet_mode = lottery.get("packet_mode", "prize")
        by_invites = lottery.get("by_invites", bool(invite_gate))
        if (
            not isinstance(packet_mode, str)
            or packet_mode not in {"prize", "ordinary", "lucky"}
            or type(by_invites) is not bool
        ):
            raise ApiError("红包类型或邀请开关无效", status_code=400)
        try:
            amount = Decimal(str(lottery.get("total_amount", "0.00")))
        except (InvalidOperation, ValueError):
            raise ApiError("红包总金额须为有效金额", status_code=400) from None
        if (
            not amount.is_finite()
            or not 0 <= amount <= Decimal("999999.99")
            or amount.as_tuple().exponent < -2
        ):
            raise ApiError("红包总金额须为0至999999.99，最多两位小数", status_code=400)
        total_amount = int(amount * 100)
        if packet_mode != "prize":
            if not 1 <= capacity <= 100 or total_amount < capacity:
                raise ApiError(
                    "红包参加人数须为1至100，每人至少0.01元", status_code=400
                )
            winners = capacity
        elif capacity and winners > capacity:
            raise ApiError("中奖人数不能大于参与上限", status_code=400)
        if by_invites and invite_gate < 1:
            raise ApiError("按邀请抽奖时须设置至少1人的有效邀请门槛", status_code=400)
        if not by_invites:
            invite_gate = 0
        try:
            reward = Decimal(str(body.get("invite_rate", "0.00")))
        except (InvalidOperation, ValueError):
            raise ApiError("邀请奖励须为0至999999.99", status_code=400) from None
        if (
            not reward.is_finite()
            or reward < 0
            or reward > Decimal("999999.99")
            or reward.as_tuple().exponent < -2
        ):
            raise ApiError("邀请奖励须为0至999999.99，最多两位小数", status_code=400)
        rate = int(reward * 100)
        account = str(config.get("account_id", ""))
        guard = self._scope_guard(service, config, group, adapter)

        async with self.save_lock:
            async with self._activity_lock(adapter, group):
                async with AsyncDatabase(
                    lambda: activity_database(adapter), write=True, guard=guard
                ) as db:
                    await self.authorize()
                    current = await db.call(
                        lambda connection: self._activity_snapshot(
                            adapter, copy.deepcopy(config), group
                        )
                    )
                    if current["revision"] != body["revision"]:
                        raise ApiError(
                            "活动配置已被修改，请刷新后重新编辑", status_code=409
                        )
                    active = await db.fetchone(
                        "SELECT 1 FROM lotteries WHERE account=? AND group_id=? "
                        "AND status IN ('announcing','open','blocked','drawing')",
                        (account, group),
                    )
                    lottery_values = {
                        "prize": prize,
                        "winners": winners,
                        "duration_minutes": duration,
                        "capacity": capacity,
                        "invite_gate": invite_gate,
                        "contact": contact,
                        "total_amount": f"{amount:.2f}",
                        "packet_mode": packet_mode,
                        "by_invites": by_invites,
                        "remarks": current["lottery"]["remarks"]
                        if remarks is None
                        else remarks,
                    }
                    if (
                        "lottery" in body
                        and active
                        and any(
                            current["lottery"][key] != value
                            for key, value in lottery_values.items()
                        )
                    ):
                        raise ApiError(
                            "抽奖进行中，当前期参数已冻结；请结束后再修改",
                            status_code=409,
                        )
                    if "lottery" in body:
                        await db.execute(
                            "INSERT OR IGNORE INTO lottery_settings(account,group_id) "
                            "VALUES(?,?)",
                            (account, group),
                        )
                        await db.execute(
                            "UPDATE lottery_settings SET prize=?,winners=?,duration=?,"
                            "capacity=?,invite_gate=?,contact=?,total_amount=?,packet_mode=?,"
                            "by_invites=?,remarks=? WHERE account=? AND group_id=?",
                            (
                                prize,
                                winners,
                                duration * 60,
                                capacity,
                                invite_gate,
                                contact,
                                total_amount,
                                packet_mode,
                                int(by_invites),
                                lottery_values["remarks"],
                                account,
                                group,
                            ),
                        )
                    if "invite_rate" in body:
                        await db.execute(
                            "INSERT INTO invite_rules(account,group_id,rate) VALUES(?,?,?) "
                            "ON CONFLICT(account,group_id) DO UPDATE SET rate=excluded.rate",
                            (account, group, rate),
                        )
                    await self.authorize()
                result = await asyncio.to_thread(
                    self._activity_snapshot, adapter, copy.deepcopy(config), group
                )
                guard()
                return ok(result)

    async def schedule(self):
        """Read a group's current schedule plus bounded recent execution history."""
        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        group = request.query.get("group_id", "")
        try:
            config = service.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        if config.get("type") != "wangshangliao" or group not in config.get(
            "enabled_groups", []
        ):
            raise ApiError("目标群未启用", status_code=400)
        if not self._adapter(bot_id):
            raise ApiError("旺商聊实例未在线", status_code=503)
        scope_revision = hashlib.sha256(
            json.dumps(
                [bot_id, str(config.get("account_id", "")), group, revision(config)]
            ).encode()
        ).hexdigest()
        path = instance_dir(bot_id) / "schedules.sqlite3"
        if not path.is_file():
            return ok(
                {
                    "schedule": None,
                    "executions": [],
                    "history": [],
                    "revision": scope_revision,
                }
            )
        account = str(config.get("account_id", ""))
        guard = self._scope_guard(service, config, group)
        try:
            async with AsyncDatabase(
                lambda: sqlite3.connect(
                    f"{path.as_uri()}?mode=ro", uri=True, timeout=1
                ),
                guard=guard,
            ) as db:
                await db.call(setattr, "row_factory", sqlite3.Row)
                tables = {
                    row["name"]
                    for row in await db.fetchall(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                schedule_row = (
                    await db.fetchone(
                        "SELECT group_id,start,end,zone,version,status,next_at,"
                        "next_action,error FROM schedules WHERE account=? AND group_id=?",
                        (account, group),
                    )
                    if "schedules" in tables
                    else None
                )
                executions = (
                    [
                        dict(row)
                        for row in await db.fetchall(
                            "SELECT operation,planned,action,status FROM executions "
                            "WHERE account=? AND group_id=? ORDER BY planned DESC LIMIT 20",
                            (account, group),
                        )
                    ]
                    if "executions" in tables
                    else []
                )
                history = (
                    [
                        dict(row)
                        for row in await db.fetchall(
                            "SELECT recorded,version,start,end,status,error FROM rule_audit "
                            "WHERE account=? AND group_id=? ORDER BY id DESC LIMIT 20",
                            (account, group),
                        )
                    ]
                    if "rule_audit" in tables
                    else []
                )
        except sqlite3.Error:
            raise ApiError("定时记录暂时不可读取", status_code=503) from None
        await self.authorize()
        guard()
        return ok(
            {
                "schedule": dict(schedule_row) if schedule_row else None,
                "executions": executions,
                "history": history,
                "revision": scope_revision,
            }
        )

    async def pause_schedule(self):
        """Pause future work without changing the group's current mute state."""
        service = await self.authorize()
        body = await request.json()
        if (
            not isinstance(body, dict)
            or set(body) != {"bot_id", "group_id", "version", "action", "revision"}
            or not isinstance(body.get("bot_id"), str)
            or not isinstance(body.get("group_id"), str)
            or type(body.get("version")) is not int
            or not isinstance(body.get("revision"), str)
            or body.get("action") != "pause"
        ):
            raise ApiError("定时操作参数不完整，请刷新页面后再操作", status_code=400)
        try:
            config = service.get_bot(body["bot_id"])["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        group = body["group_id"]
        if config.get("type") != "wangshangliao" or group not in config.get(
            "enabled_groups", []
        ):
            raise ApiError("目标群未启用", status_code=400)
        adapter = self._adapter(body["bot_id"])
        if not adapter:
            raise ApiError("旺商聊实例未在线", status_code=503)
        guard = self._scope_guard(service, config, group, adapter)
        scope_revision = hashlib.sha256(
            json.dumps(
                [
                    body["bot_id"],
                    str(config.get("account_id", "")),
                    group,
                    revision(config),
                ]
            ).encode()
        ).hexdigest()
        if body["revision"] != scope_revision:
            raise ApiError("定时规则所属账号或群配置已变化，请刷新", status_code=409)
        async with group_lock(adapter, group):
            async with AsyncDatabase(
                lambda: schedule_database(adapter), write=True, guard=guard
            ) as db:
                await self.authorize()
                row = await db.fetchone(
                    "SELECT version,status FROM schedules WHERE account=? AND group_id=?",
                    (str(config.get("account_id", "")), group),
                )
                if not row or row[1] == "deleted":
                    raise ApiError("当前群没有可暂停的定时规则", status_code=404)
                if row[0] != body["version"]:
                    raise ApiError("定时规则已变化，请刷新后再操作", status_code=409)
                if row[1] == "active":
                    await db.execute(
                        "UPDATE confirmations SET used=1 WHERE account=? AND group_id=?",
                        (str(config.get("account_id", "")), group),
                    )
                    await db.execute(
                        "UPDATE schedules SET status='paused',version=version+1,"
                        "error='dashboard_operator' WHERE account=? AND group_id=?",
                        (str(config.get("account_id", "")), group),
                    )
                await self.authorize()
        return ok({"paused": True, "message": "已暂停后续定时操作；当前群状态未改变。"})

    def _scope_guard(self, service, config, group, adapter=None):
        """Bind worker IO to the saved scope and live account generation.

        Args:
            service: Saved bot configuration service.
            config: Authorized configuration before asynchronous IO.
            group: Authorized group, or None for an account-wide directory read.
            adapter: Optional live adapter for account-generation validation.

        Returns:
            Synchronous guard for transaction admission and commit.
        """
        bot_id = config["id"]
        expected = revision(config)
        identity = (str(config.get("account_id", "")), config.get("enable", True))
        live_guard = account_guard(adapter) if adapter is not None else None

        def check():
            try:
                current = service.get_bot(bot_id)["bot"]
            except ValueError:
                raise ApiError("旺商聊实例不存在", status_code=404) from None
            if (
                not self.active
                or current.get("type") != "wangshangliao"
                or group is not None
                and group not in current.get("enabled_groups", [])
                or revision(current) != expected
                or (str(current.get("account_id", "")), current.get("enable", True))
                != identity
                or (adapter is not None and self._adapter(bot_id) is not adapter)
                or (adapter is not None and str(adapter.account) != identity[0])
            ):
                raise ApiError("配置已变化，请刷新后重试", status_code=409)
            if live_guard is not None:
                try:
                    live_guard()
                except ProtocolError:
                    raise ApiError(
                        "账号已变化，请刷新后重试", status_code=409
                    ) from None

        return check

    def _adapter(self, bot_id):
        return next(
            (
                item
                for item in self.context.platform_manager.platform_insts
                if item.meta().id == bot_id and item.meta().name == "wangshangliao"
            ),
            None,
        )

    @staticmethod
    def _activity_lock(adapter, group):
        if not hasattr(adapter, "activity_locks"):
            adapter.activity_locks = {}
        key = (adapter.config["id"], adapter.account, group)
        return adapter.activity_locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _bounded_int(value, low, high, label):
        if type(value) is not int or not low <= value <= high:
            raise ApiError(f"{label}范围：{low}至{high}", status_code=400)
        return value

    @staticmethod
    def _activity_text(value, label, *, limit=128, multiline=False):
        """Validate bounded visible text, optionally preserving remark line breaks.

        Args:
            value: Untrusted activity setting.
            label: Public field name used in validation errors.
            limit: Maximum character count.
            multiline: Allow explicit line breaks only for remarks.

        Returns:
            The unchanged validated string, including an optional empty value.

        Raises:
            ApiError: If the text contains hidden controls or exceeds the limit.
        """
        if not isinstance(value, str):
            raise ApiError(f"{label}须为单行文字", status_code=400)
        if value == "":
            return ""
        clean = clean_activity_text(value, limit, multiline=multiline)
        if not clean or clean != value or len(clean.encode("utf-8")) > limit * 4:
            lines = "多行文字" if multiline else "单行文字"
            raise ApiError(f"{label}须为1至{limit}字的{lines}", status_code=400)
        return clean

    @staticmethod
    def _activity_snapshot(adapter, config, group):
        account = str(config.get("account_id", ""))
        lottery = {
            "prize": "",
            "winners": 3,
            "duration_minutes": 10,
            "capacity": 15,
            "invite_gate": 0,
            "contact": "",
            "total_amount": "0.00",
            "packet_mode": "prize",
            "by_invites": False,
            "remarks": "",
        }
        invitation = {
            "rate": "0.00",
            "enabled": False,
            "last_scan": 0,
            "presence_checked_at": 0,
            "error": "",
            "credited_count": 0,
            "credited_total": "0.00",
            "active_count": 0,
            "left_count": 0,
            "pending_count": 0,
            "expired_count": 0,
        }
        current = None
        path = instance_dir(config["id"]) / "activities.sqlite3"
        if path.is_file():
            try:
                with closing(
                    sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
                ) as db:
                    db.row_factory = sqlite3.Row
                    tables = {
                        row["name"]
                        for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "lottery_settings" in tables:
                        row = db.execute(
                            "SELECT * FROM lottery_settings WHERE account=? AND group_id=?",
                            (account, group),
                        ).fetchone()
                        if row:
                            lottery = {
                                "prize": row["prize"],
                                "winners": row["winners"],
                                "duration_minutes": row["duration"] // 60,
                                "capacity": row["capacity"],
                                "invite_gate": row["invite_gate"],
                                "contact": row["contact"],
                                "total_amount": (
                                    f"{Decimal(row['total_amount']) / 100:.2f}"
                                    if "total_amount" in row.keys()
                                    else "0.00"
                                ),
                                "packet_mode": row["packet_mode"]
                                if "packet_mode" in row.keys()
                                else "prize",
                                "by_invites": bool(row["by_invites"])
                                if "by_invites" in row.keys()
                                else bool(row["invite_gate"]),
                                "remarks": row["remarks"]
                                if "remarks" in row.keys()
                                else "",
                            }
                    if "invite_rules" in tables:
                        rule_columns = {
                            field["name"]
                            for field in db.execute("PRAGMA table_info(invite_rules)")
                        }
                        presence_field = (
                            ",presence_checked_at"
                            if "presence_checked_at" in rule_columns
                            else ""
                        )
                        row = db.execute(
                            "SELECT rate,enabled,last_scan,error"
                            + presence_field
                            + " FROM invite_rules "
                            "WHERE account=? AND group_id=?",
                            (account, group),
                        ).fetchone()
                        if row:
                            invitation.update(
                                rate=f"{row['rate'] / 100:.2f}",
                                enabled=bool(row["enabled"]),
                                last_scan=row["last_scan"],
                                presence_checked_at=(
                                    row["presence_checked_at"]
                                    if "presence_checked_at" in row.keys()
                                    else 0
                                ),
                                error=row["error"],
                            )
                    if "invite_credits" in tables:
                        row = db.execute(
                            "SELECT COUNT(*),COALESCE(SUM(amount),0) FROM invite_credits "
                            "WHERE account=? AND group_id=?",
                            (account, group),
                        ).fetchone()
                        invitation.update(
                            credited_count=row[0],
                            credited_total=f"{row[1] / 100:.2f}",
                        )
                    if "invite_seen" in tables:
                        seen_columns = {
                            field["name"]
                            for field in db.execute("PRAGMA table_info(invite_seen)")
                        }
                        states = {
                            row["status"]: row["count"]
                            for row in db.execute(
                                "SELECT status,COUNT(*) AS count FROM invite_seen "
                                "WHERE account=? AND group_id=? GROUP BY status",
                                (account, group),
                            )
                        }
                        invitation.update(
                            pending_count=states.get("pending", 0),
                            expired_count=states.get("expired", 0),
                        )
                        if "left_at" in seen_columns:
                            row = db.execute(
                                "SELECT "
                                "COALESCE(SUM(CASE WHEN left_at=0 THEN 1 ELSE 0 END),0),"
                                "COALESCE(SUM(CASE WHEN left_at>0 THEN 1 ELSE 0 END),0) "
                                "FROM invite_seen WHERE account=? AND group_id=? "
                                "AND status='credited'",
                                (account, group),
                            ).fetchone()
                            invitation.update(active_count=row[0], left_count=row[1])
                    if "lotteries" in tables:
                        row = db.execute(
                            "SELECT rowid AS number,id,prize,winners,duration,capacity,"
                            "invite_gate,contact,status,ends,created,total,error "
                            "FROM lotteries WHERE account=? AND group_id=? "
                            "ORDER BY created DESC LIMIT 1",
                            (account, group),
                        ).fetchone()
                        if row:
                            current = dict(row)
                            packet_columns = {
                                field["name"]
                                for field in db.execute("PRAGMA table_info(lotteries)")
                            }
                            if {
                                "total_amount",
                                "packet_mode",
                                "by_invites",
                            } <= packet_columns:
                                packet = db.execute(
                                    "SELECT total_amount,packet_mode,by_invites FROM lotteries WHERE id=?",
                                    (row["id"],),
                                ).fetchone()
                                current.update(
                                    total_amount=f"{Decimal(packet['total_amount']) / 100:.2f}",
                                    packet_mode=packet["packet_mode"],
                                    by_invites=bool(packet["by_invites"]),
                                )
                            if {
                                "remarks",
                                "packet_amounts",
                                "unclaimed_amount",
                            } <= packet_columns:
                                detail = db.execute(
                                    "SELECT remarks,packet_amounts,unclaimed_amount,results "
                                    "FROM lotteries WHERE id=?",
                                    (row["id"],),
                                ).fetchone()
                                allocations = json.loads(detail["results"])
                                current.update(
                                    remarks=detail["remarks"],
                                    fixed_packets=detail["packet_amounts"] != "[]",
                                    allocated_amount=f"{Decimal(sum(item.get('amount', 0) for item in allocations)) / 100:.2f}",
                                    allocated_count=len(allocations),
                                    unclaimed_amount=f"{Decimal(detail['unclaimed_amount']) / 100:.2f}",
                                )
                            current["entries"] = (
                                db.execute(
                                    "SELECT COUNT(*) FROM lottery_entries WHERE lottery=?",
                                    (row["id"],),
                                ).fetchone()[0]
                                if "lottery_entries" in tables
                                else 0
                            )
            except sqlite3.Error:
                raise ApiError("活动记录暂时不可读取", status_code=503) from None
        editable = {"lottery": lottery, "invite_rate": invitation["rate"]}
        return {
            **editable,
            "invitation": invitation,
            "current_lottery": current,
            "revision": hashlib.sha256(
                json.dumps(
                    {
                        "bot_id": config["id"],
                        "account": account,
                        "group_id": group,
                        "configuration": revision(config),
                        **editable,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        }
