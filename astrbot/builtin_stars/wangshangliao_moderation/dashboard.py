"""Authenticated plugin settings and read-only moderation audit."""

import asyncio
import copy
import hashlib
import json
import sqlite3
import time
import unicodedata
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
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.identity import account_nickname
from astrbot.core.platform.sources.wangshangliao.policy import validate_policy
from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
    BUSINESS_FIELDS,
    strip_private_settings,
)
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
from .lottery_rules import (
    DEFAULT_ENTRY_PHRASE,
    packet_plan,
    validate_entry_phrase,
    validate_luckiest_bonus,
    validate_reserved_members,
)
from .lottery_time import beijing_time, parse_start_at
from .recall_audit import read_native_audit
from .settings import business_revision, validate_settings

PLUGIN_NAME = "wangshangliao_moderation"
LOTTERY_MEMBER_LOOKUP_SECONDS = 15
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

    def register(self):
        """Register the plugin-local routes when the plugin starts."""
        self.active = True
        # Plugin reloads replace matching routes but do not remove retired ones.
        routes = self.context.registered_web_apis
        routes[:] = [
            api
            for api in routes
            if api[0]
            not in {f"/{PLUGIN_NAME}/friends", f"/{PLUGIN_NAME}/friend-approval"}
        ]
        for endpoint, handler, method in (
            ("settings", self.settings, "GET"),
            ("settings", self.save, "POST"),
            ("audit", self.audit, "GET"),
            ("speech-activity", self.speech_activity, "GET"),
            ("speech-report", self.speech_report, "GET"),
            ("video-relay", self.video_relay, "GET"),
            ("video-relay", self.video_relay, "POST"),
            ("cashback-logs", self.cashback_logs, "GET"),
            ("cashier", self.cashier, "POST"),
            ("invitations", self.invitations, "GET"),
            ("activities", self.activities, "GET"),
            ("activities", self.save_activities, "POST"),
            ("lottery-members", self.lottery_members, "GET"),
            ("lottery-control", self.lottery_control, "POST"),
            ("schedule", self.schedule, "GET"),
            ("schedule", self.pause_schedule, "POST"),
            ("groups", self.groups, "GET"),
            ("cards", self.cards, "POST"),
            ("test-window", self.test_window, "GET"),
            ("test-window", self.test_window, "POST"),
            ("operations", self.operations, "GET"),
            ("group-managers", self.group_managers, "GET"),
            ("group-managers", self.group_managers, "POST"),
            ("open-service", self.open_service, "GET"),
            ("open-service", self.open_service, "POST"),
        ):
            if method == "POST" and endpoint not in {
                "open-service",
                "cashier",
                "video-relay",
            }:
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
        from .open_rpc import OpenCall, require_group_call
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
            require_group_call(call)
            if not integrated.active:
                raise OpenError("service_unavailable")
            async with integrated.rpc.lock:
                return ok(await integrated.rpc.dispatch(call))
        except OpenError as exc:
            if str(exc) == "private_messages_retired":
                raise ApiError("私信功能已移除。", status_code=410) from None
            if body["action"] == "message_recalls":
                messages = {
                    "recall_time_range_invalid": "只能预览最近24小时内的时间段，请按北京时间核对截止时间和时长。",
                    "recall_selection_limit": "命中范围过大：单次最多500条，请缩短最近时间后重新预览。",
                    "recall_previous_unconfirmed": "其中有过撤回请求且结果未确认，已停止。请先核对原回执，不会自动重试。",
                    "recall_no_flood_messages": "最近时间内没有可核实且尚未撤回的刷屏消息，请核对成员和时间范围。",
                    "recall_member_changed": "目标已离群、身份变化或属于受保护成员，请刷新成员目录重新选择。",
                    "recall_member_required": "请搜索并选择当前群内一名明确的普通成员。",
                    "recall_job_missing": "当前机器人和群中没有这个任务，请刷新进度。",
                    "recall_job_busy": "这个机器人正在处理另一批撤回，请等待结束或停止原任务。",
                    "confirmation_invalid": "撤回预览已过期、已使用或不属于当前账号和群，请重新预览。",
                    "service_paused": "机器人或当前群已暂停，请先核对服务状态；已提交的任务仍可查看和停止。",
                    "operation_revoked": "群状态或操作权限已变化，本次未继续提交；请刷新核对。",
                }
                if str(exc) in messages:
                    raise ApiError(messages[str(exc)], status_code=409) from None
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
                "welcome_changed",
                "welcome_confirmation_invalid",
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
                "welcome_arguments": "欢迎语参数无效，请刷新当前群页面后重试。",
                "raid_arguments": "防袭群参数无效，请刷新当前群页面后重试。",
                "raid_protection_changed": "本轮防护已结束或已变化，请刷新状态后重新预览。",
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
                "blacklist_member_unavailable": "黑名单未修改，未提交禁言或解禁：当前群成员名单中找不到该成员，可能已退群或成员资料尚未同步。请先核实其是否仍在本群；不能仅据此确认已退群。",
                "blacklist_identity_changed": "黑名单未修改，未提交禁言或解禁：当前成员身份绑定不一致或不唯一，请联系机器人负责人核对成员资料。",
                "blacklist_target_conflict": "黑名单未修改，未提交禁言或解禁：@成员与回复对象不是同一人，请只保留一个目标后重新操作。",
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
                "member_directory_rejected": "旺商聊暂时拒绝读取本群成员名单，无法核实管理员和目标身份。本次未使用旧名单继续操作；请稍后刷新，仍失败请联系机器人负责人检查成员接口。",
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

    async def video_relay(self):
        """Manage relay drafts and explicit single-video previews and delivery.

        Returns:
            Scoped configuration, single-use preview or durable native receipt.

        Raises:
            ApiError: If authentication, scope, fields or confirmation are invalid.
        """
        from .open_store import OpenError
        from .video_relay import control
        from .video_relay_delivery import (
            delivery_control,
            expire_previews,
            fetch_cms_categories,
            refresh_native_evidence,
        )
        from .video_relay_fixture import inspect_test_videos, is_fixture_request
        from .video_relay_fixture import posting_guard as test_posting_guard

        service = await self.authorize()
        writing = request._request.method == "POST"
        if writing:
            raw = bytearray()
            async for chunk in request._request.stream():
                raw.extend(chunk)
                if len(raw) > 16384:
                    raise ApiError("视频转发配置过大。", status_code=413)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ApiError("视频转发参数无效。", status_code=400) from None
        else:
            body = dict(request.query)
        operation = body.get("operation") if isinstance(body, dict) else None
        keys = {"bot_id", "group_id"}
        if writing:
            keys |= {"operation", "version", "revision"}
            if operation == "preview":
                keys |= {"config"}
            elif operation == "preview_video":
                if "category_id" in body:
                    keys.add("category_id")
                if "max_video_mb" in body:
                    keys.add("max_video_mb")
                if "vod_id" in body:
                    keys.add("vod_id")
            elif operation not in {"preview_test_video", "cancel_active"}:
                keys |= {"token"}
        elif operation in {"inspect_test_video", "categories"}:
            keys.add("operation")
        if (
            not isinstance(body, dict)
            or set(body) != keys
            or any(
                not isinstance(body.get(k), str) or not body[k]
                for k in ("bot_id", "group_id")
            )
            or writing
            and (
                operation
                not in {
                    "preview",
                    "commit",
                    "preview_video",
                    "preview_test_video",
                    "send_video",
                    "cancel_video",
                    "cancel_active",
                }
                or type(body["version"]) is not int
                or body["version"] < 0
                or not isinstance(body["revision"], str)
                or operation in {"commit", "send_video", "cancel_video"}
                and (
                    not isinstance(body["token"], str)
                    or not 16 <= len(body["token"]) <= 128
                )
                or "category_id" in body
                and (
                    type(body["category_id"]) is not int
                    or not 0 <= body["category_id"] <= 2147483647
                )
                or "max_video_mb" in body
                and (
                    type(body["max_video_mb"]) is not int
                    or not 1 <= body["max_video_mb"] <= 112
                )
                or "vod_id" in body
                and (
                    type(body["vod_id"]) is not int or not 0 < body["vod_id"] < 1 << 63
                )
            )
        ):
            raise ApiError("视频转发参数无效。", status_code=400)
        try:
            config = service.get_bot(body["bot_id"])["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在。", status_code=404) from None
        group = body["group_id"]
        adapter = self._adapter(body["bot_id"])
        if (
            config.get("type") != "wangshangliao"
            or not config.get("enable", True)
            or group not in config.get("enabled_groups", [])
            or adapter is None
        ):
            raise ApiError("请选择已启用机器人的当前群。", status_code=409)
        guard = self._scope_guard(service, config, group, adapter)
        guard()
        try:
            path = instance_dir(body["bot_id"]) / "video-relay-control.sqlite3"
            if not writing and operation == "inspect_test_video":
                result = await inspect_test_videos(adapter, group, guard)
            elif not writing and operation == "categories":
                result = await fetch_cms_categories(
                    path, str(adapter.account), group, guard
                )
            elif operation in {
                "preview_video",
                "preview_test_video",
                "send_video",
                "cancel_video",
                "cancel_active",
            }:
                governance = getattr(
                    getattr(self, "plugin", None), "open_service", None
                )
                if operation not in {"cancel_video", "cancel_active"} and (
                    governance is None or not governance.active
                ):
                    raise ApiError(
                        "当前群管理服务不可用，未准备或发送视频。", status_code=409
                    )
                rules = governance.rule_replies if governance is not None else None
                if (
                    operation not in {"cancel_video", "cancel_active"}
                    and governance is not None
                ):
                    try:
                        async with asyncio.timeout(15):
                            await refresh_native_evidence(
                                governance, adapter, group, guard
                            )
                    except TimeoutError:
                        raise ApiError(
                            "刷新群管理权限超时，请稍后重试；本次未下载或发送视频。",
                            status_code=409,
                        ) from None
                fixture = (
                    await is_fixture_request(
                        path,
                        str(adapter.account),
                        group,
                        "dashboard:" + request.username,
                        revision(config),
                        body,
                    )
                    if operation not in {"cancel_video", "cancel_active"}
                    else False
                )
                rule = (
                    await asyncio.to_thread(rules.read, str(adapter.account), group)
                    if operation not in {"cancel_video", "cancel_active"}
                    and not fixture
                    else None
                )
                if rule is not None and not rule.get("version"):
                    raise ApiError(
                        "请先在本群规则页预览并确认保存规则，再预览视频。",
                        status_code=409,
                    )

                async def posting_guard():
                    guard()
                    if fixture:
                        await test_posting_guard(governance, adapter, group, guard)
                    elif rule is not None:
                        await asyncio.to_thread(
                            rules.send_guard, adapter, rule, posting=True
                        )
                    guard()

                result = await delivery_control(
                    path,
                    adapter,
                    group,
                    "dashboard:" + request.username,
                    revision(config),
                    body,
                    guard,
                    posting_guard,
                    refresh_authority=lambda: refresh_native_evidence(
                        governance, adapter, group, guard
                    ),
                )
            else:
                if not writing:
                    await expire_previews(path)
                result = await control(
                    path,
                    str(adapter.account),
                    group,
                    "dashboard:" + request.username,
                    revision(config),
                    body,
                    guard,
                )
        except OpenError as exc:
            raise ApiError(
                {
                    "business_native_permission": "机器人当前群管权限尚未核实，请刷新群管理状态后重试。",
                    "business_owner_revoked": "原规则管理员权限已变更，请由现任管理员重新预览并保存本群规则。",
                    "proactive_not_authorized": "本群尚未授权主动发送，请启用本群主动发送后重新预览视频。",
                }.get(
                    str(exc),
                    "本群规则已变化、已暂停或未启用，请刷新本群规则后重新预览视频。",
                ),
                status_code=409,
            ) from None
        except ValidationError:
            raise ApiError(
                "请填写公开HTTPS CMS接口；分类ID须为非负整数，间隔1—1440分钟，视频1—112MiB；不接受账号密钥或私网地址。",
                status_code=400,
            ) from None
        except sqlite3.OperationalError:
            raise ApiError(
                "配置存储暂不可用，请刷新核对；不会自动重试保存。", status_code=503
            ) from None
        return ok(result)

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
            config = copy.deepcopy(config)
            strip_private_settings(config)
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
                            "reply_groups",
                            "moderation",
                        )
                        if field in config
                    },
                    "ai_routes": {
                        "groups": copy.deepcopy(
                            config.get("ai_routes", {}).get("groups", {})
                        )
                    },
                    "proactive_send": copy.deepcopy(config.get("proactive_send", {})),
                    "developer_test": copy.deepcopy(config.get("developer_test", {})),
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
        if isinstance(body, dict) and (
            "reply_private" in body
            or "admin_provider_id" in body
            or isinstance(body.get("proactive_send"), dict)
            and isinstance(body["proactive_send"].get("targets"), list)
            and any(
                isinstance(target, str) and target.startswith("private/")
                for target in body["proactive_send"]["targets"]
            )
            or isinstance(body.get("developer_test"), dict)
            and isinstance(body["developer_test"].get("scopes"), list)
            and any(
                scope in ("private_commands", "private_ai")
                for scope in body["developer_test"]["scopes"]
            )
        ):
            raise ApiError("私信功能已移除。", status_code=410)
        if (
            not isinstance(body, dict)
            or set(body)
            - {
                "bot_id",
                "revision",
                "group_id",
                "policy",
                "group",
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
            strip_private_settings(config)
            if merged is not None and merged.integrated:
                from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
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
            if "customer_provider_id" in group_settings:
                routes = config.setdefault("ai_routes", {})
                routes.setdefault("groups", {})[group] = group_settings[
                    "customer_provider_id"
                ]
                routes.setdefault("groups", {}).pop("", None)
                config["ai_routes"] = routes
            try:
                validate_settings(
                    {key: config[key] for key in BUSINESS_FIELDS if key in config}
                )
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
        if isinstance(body.get("scopes"), list) and any(
            scope in ("private_commands", "private_ai") for scope in body["scopes"]
        ):
            raise ApiError("私信功能已移除。", status_code=410)
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

    async def speech_activity(self):
        """Read scoped two-day speech statistics, never message bodies.

        Returns:
            Group totals, member choices and exact member statistics.

        Raises:
            ApiError: If authentication, scope or parameters are invalid.
        """
        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        group = request.query.get("group_id", "")
        member = request.query.get("member_id", "")
        query = request.query.get("query", "").strip()
        if len(query) > 64 or (
            member
            and (
                not member.isascii()
                or not member.isdigit()
                or len(member) > 20
                or int(member) <= 0
            )
        ):
            raise ApiError("请输入64字以内的名称或有效成员UID", status_code=400)
        try:
            config = service.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        adapter = self._adapter(bot_id)
        if (
            adapter is None
            or config.get("type") != "wangshangliao"
            or not config.get("enable", True)
            or group not in config.get("enabled_groups", [])
        ):
            raise ApiError("请选择已启用机器人的当前群", status_code=400)
        guard = self._scope_guard(service, config, group, adapter)
        guard()
        names = {uid: uid for uid in getattr(adapter, "members", {}).get(group, {})}
        snapshot = getattr(adapter, "member_snapshots", {}).get(group)
        if (
            isinstance(snapshot, tuple)
            and len(snapshot) == 3
            and snapshot[0] == str(adapter.account)
            and isinstance(snapshot[2], dict)
            and snapshot[2].get("complete") is True
        ):
            for row in snapshot[2].get("groupMemberInfo", []):
                uid = str(row.get("userId", ""))
                if uid in names:
                    names[uid] = str(
                        row.get("groupMemberNick")
                        or row.get("userNick")
                        or row.get("userName")
                        or uid
                    )[:128]
        try:
            result = await asyncio.to_thread(
                self.plugin.speech_activity.read,
                bot_id,
                str(adapter.account),
                group,
                member=member,
                query=query,
                names=names,
            )
        except (sqlite3.Error, OSError):
            raise ApiError(
                "统计暂时不可读，请稍后刷新；不影响群消息处理。", status_code=503
            ) from None
        await self.authorize()
        guard()
        return ok(result)

    async def speech_report(self):
        """Read configured text-only member totals after guarding every group.

        Returns:
            A two-day member report, or an unconfigured-state response.

        Raises:
            ApiError: If the bot, definition or any configured scope is invalid.
        """
        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        try:
            config = service.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        adapter = self._adapter(bot_id)
        if (
            adapter is None
            or config.get("type") != "wangshangliao"
            or not config.get("enable", True)
        ):
            raise ApiError("请选择已启用的旺商聊机器人", status_code=400)
        bot_guard = self._scope_guard(service, config, None, adapter)
        bot_guard()
        try:
            definition = await asyncio.to_thread(
                self.plugin.speech_activity.report_config, bot_id, str(adapter.account)
            )
            if definition is None:
                await self.authorize()
                bot_guard()
                return ok({"configured": False})
            guards = []
            for group in definition["groups"]:
                if group["id"] not in config.get("enabled_groups", []):
                    raise ApiError(
                        "汇报中的群已停用，请核对汇报范围；未显示不完整汇总。",
                        status_code=409,
                    )
                guard = self._scope_guard(service, config, group["id"], adapter)
                guard()
                guards.append(guard)
            result = await asyncio.to_thread(
                self.plugin.speech_activity.report,
                bot_id,
                str(adapter.account),
                definition,
            )
        except (sqlite3.Error, OSError):
            raise ApiError(
                "专门汇报暂时不可读，请稍后刷新。", status_code=503
            ) from None
        except (ValueError, TypeError, KeyError):
            raise ApiError(
                "汇报成员或群配置无效，请核对后重试。", status_code=400
            ) from None
        await self.authorize()
        for guard in guards:
            guard()
        return ok({"configured": True, **result})

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
                    if "content_rule_errors" in tables:
                        for row in result["rules"]:
                            failure = await db.fetchone(
                                "SELECT error,provider_code FROM content_rule_errors "
                                "WHERE account=? AND group_id=? AND message=?",
                                (account, group, str(row["message"])),
                            )
                            if failure:
                                row.update(
                                    processing_error=failure[0],
                                    processing_provider_code=failure[1],
                                )
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

    async def lottery_members(self):
        """Search current group members for optional fixed-tier reservations.

        Returns:
            At most 20 verified current group members with transport identities.

        Raises:
            ApiError: If the scope, query or live member directory is unavailable.
        """
        from .open_store import OpenError

        service = await self.authorize()
        bot_id = request.query.get("bot_id", "")
        group = request.query.get("group_id", "")
        query = request.query.get("query", "").strip()
        if not 1 <= len(query) <= 64 or any(not char.isprintable() for char in query):
            raise ApiError("请输入1至64字的当前群成员名称或UID", status_code=400)
        try:
            config = service.get_bot(bot_id)["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        adapter = self._adapter(bot_id)
        integrated = getattr(getattr(self, "plugin", None), "open_service", None)
        if (
            adapter is None
            or integrated is None
            or not integrated.active
            or config.get("type") != "wangshangliao"
            or group not in config.get("enabled_groups", [])
        ):
            raise ApiError("当前群成员目录不可用", status_code=503)
        guard = self._scope_guard(service, config, group, adapter)
        guard()
        try:
            members = await asyncio.wait_for(
                integrated.roster(adapter, group), LOTTERY_MEMBER_LOOKUP_SECONDS
            )
        except TimeoutError:
            raise ApiError(
                "当前群成员查询超时，请稍后重试；未使用旧名单",
                status_code=503,
            ) from None
        except (OpenError, ProtocolError):
            raise ApiError(
                "当前群成员名单暂时无法读取，请稍后刷新后再搜索",
                status_code=503,
            ) from None
        guard()
        needle = unicodedata.normalize("NFKC", query).casefold()
        matches = []
        candidates = ((query, members[query]),) if query in members else members.items()
        for uid, member in candidates:
            peer = str(member.get("nimId", ""))
            if (
                uid == str(adapter.account)
                or is_managed_account(uid)
                or member.get("accountState") != "ACCOUNT_STATE_GOOD"
                or not uid.isascii()
                or not uid.isdigit()
                or not 1 <= len(uid) <= 20
                or int(uid) <= 0
                or not peer.isascii()
                or not peer.isdigit()
                or not 1 <= len(peer) <= 20
                or int(peer) <= 0
            ):
                continue
            names = [
                str(member.get(field) or "").strip()
                for field in ("groupMemberNick", "userNick", "userName", "name")
            ]
            if query != uid and not any(
                needle in unicodedata.normalize("NFKC", name).casefold()
                for name in names
                if name
            ):
                continue
            name = clean_activity_text(next((name for name in names if name), uid))
            matches.append({"member": uid, "peer": peer, "name": name})
            if len(matches) > 20:
                raise ApiError("匹配超过20位，请输入更完整的名称", status_code=400)
        await self.authorize()
        guard()
        return ok({"members": matches})

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
        optional_fields = {
            "remarks",
            "entry_phrase",
            "prize_tiers",
            "start_at",
            "luckiest_bonus",
            "integer_packets",
            "draw_on_full",
            "reserved_members",
        }
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
                or not legacy_lottery_fields <= set(body["lottery"])
                or set(body["lottery"]) - legacy_lottery_fields - optional_fields
                not in (set(), packet_fields)
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
        for flag in ("integer_packets", "draw_on_full"):
            if flag in lottery and type(lottery[flag]) is not bool:
                raise ApiError("整数元及满员开奖开关须为布尔值", status_code=400)
        prize = self._activity_text(lottery["prize"], "奖品")
        contact = self._activity_text(lottery["contact"], "领奖联系人")
        remarks = (
            self._activity_text(
                lottery["remarks"], "活动备注", limit=500, multiline=True
            )
            if "remarks" in lottery
            else None
        )
        start_at = lottery.get("start_at")
        if "start_at" in lottery:
            try:
                parse_start_at(start_at, future=True)
            except ValueError as exc:
                raise ApiError(str(exc), status_code=400) from None
            start_at = start_at.replace(" ", "T")
        winners = self._bounded_int(lottery["winners"], 1, 100, "中奖人数")
        duration = self._bounded_int(lottery["duration_minutes"], 1, 10080, "倒计时")
        capacity = self._bounded_int(lottery["capacity"], 0, 10000, "参与上限")
        invite_gate = self._bounded_int(lottery["invite_gate"], 0, 10000, "邀请门槛")
        packet_mode = lottery.get("packet_mode", "prize")
        by_invites = lottery.get("by_invites", bool(invite_gate))
        if (
            not isinstance(packet_mode, str)
            or packet_mode not in {"prize", "ordinary", "lucky", "tiered"}
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
        luckiest_bonus = None
        if "luckiest_bonus" in lottery:
            if type(lottery["luckiest_bonus"]) not in (str, int, float):
                raise ApiError("人气王额外奖励须为有效金额", status_code=400)
            try:
                bonus = Decimal(str(lottery["luckiest_bonus"]))
                if (
                    not bonus.is_finite()
                    or not 0 <= bonus <= Decimal("999999.99")
                    or bonus.as_tuple().exponent < -2
                ):
                    raise ValueError("invalid_bonus")
                luckiest_bonus = int(bonus * 100)
            except (InvalidOperation, ValueError):
                raise ApiError(
                    "人气王额外奖励须为0至999999.99元，最多两位小数", status_code=400
                ) from None
        try:
            entry_phrase = (
                validate_entry_phrase(lottery["entry_phrase"])
                if "entry_phrase" in lottery
                else None
            )
            tiers = None
            if "prize_tiers" in lottery:
                if (
                    type(lottery["prize_tiers"]) is not list
                    or len(lottery["prize_tiers"]) > 10
                ):
                    raise ValueError("固定金额档位最多10档")
                tiers = []
                for tier in lottery["prize_tiers"]:
                    if (
                        type(tier) is not dict
                        or set(tier) != {"amount", "count"}
                        or type(tier["amount"]) not in (str, int, float)
                    ):
                        raise ValueError("固定金额档位须包含金额和份数")
                    try:
                        tier_amount = Decimal(str(tier["amount"]))
                    except InvalidOperation:
                        raise ValueError("固定档位金额无效") from None
                    if (
                        not tier_amount.is_finite()
                        or not 0 < tier_amount <= Decimal("999999.99")
                        or tier_amount.as_tuple().exponent < -2
                    ):
                        raise ValueError("固定档位金额须大于0，最多两位小数")
                    tiers.append(
                        {"amount": int(tier_amount * 100), "count": tier["count"]}
                    )
            reserved_members = None
            if "reserved_members" in lottery:
                if (
                    type(lottery["reserved_members"]) is not list
                    or len(lottery["reserved_members"]) > 100
                ):
                    raise ValueError("指定档位成员最多100人")
                reserved_members = []
                for reserved in lottery["reserved_members"]:
                    if (
                        type(reserved) is not dict
                        or set(reserved) != {"member", "peer", "amount"}
                        or not isinstance(reserved["member"], str)
                        or not reserved["member"].isascii()
                        or not reserved["member"].isdigit()
                        or not 1 <= len(reserved["member"]) <= 20
                        or int(reserved["member"]) <= 0
                        or not isinstance(reserved["peer"], str)
                        or not reserved["peer"].isascii()
                        or not reserved["peer"].isdigit()
                        or not 1 <= len(reserved["peer"]) <= 20
                        or int(reserved["peer"]) <= 0
                        or type(reserved["amount"]) not in (str, int, float)
                    ):
                        raise ValueError("指定成员参数无效，请重新搜索当前群成员")
                    try:
                        reserved_amount = Decimal(str(reserved["amount"]))
                    except InvalidOperation:
                        raise ValueError("指定成员档位金额无效") from None
                    if (
                        not reserved_amount.is_finite()
                        or not 0 < reserved_amount <= Decimal("999999.99")
                        or reserved_amount.as_tuple().exponent < -2
                    ):
                        raise ValueError("指定成员档位金额须大于0，最多两位小数")
                    reserved_members.append(
                        {
                            "member": reserved["member"],
                            "peer": reserved["peer"],
                            "amount": int(reserved_amount * 100),
                        }
                    )
        except ValueError as exc:
            raise ApiError(str(exc), status_code=400) from None
        if reserved_members and packet_mode != "tiered":
            raise ApiError("指定档位成员仅支持分档拼手气", status_code=400)
        if reserved_members and tiers is not None:
            try:
                validate_reserved_members(
                    [{**reserved, "name": ""} for reserved in reserved_members],
                    tiers,
                )
            except ValueError as exc:
                raise ApiError(str(exc), status_code=400) from None
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
        resolved_reserved_members = None
        if reserved_members is not None:
            resolved_reserved_members = []
            if reserved_members:
                from .open_store import OpenError

                integrated = getattr(
                    getattr(self, "plugin", None), "open_service", None
                )
                if integrated is None or not integrated.active:
                    raise ApiError("当前群成员目录不可用", status_code=503)
                guard()
                try:
                    members = await asyncio.wait_for(
                        integrated.roster(adapter, group),
                        LOTTERY_MEMBER_LOOKUP_SECONDS,
                    )
                except TimeoutError:
                    raise ApiError(
                        "当前群成员查询超时，指定方案未保存，请稍后重试",
                        status_code=503,
                    ) from None
                except (OpenError, ProtocolError):
                    raise ApiError(
                        "当前群成员名单暂时无法读取，指定方案未保存",
                        status_code=503,
                    ) from None
                guard()
                for reserved in reserved_members:
                    live = members.get(reserved["member"])
                    if (
                        not live
                        or str(live.get("nimId", "")) != reserved["peer"]
                        or live.get("accountState") != "ACCOUNT_STATE_GOOD"
                        or reserved["member"] == account
                        or is_managed_account(reserved["member"])
                    ):
                        raise ApiError(
                            "指定成员已退群、身份变化或为机器人，请重新搜索后选择",
                            status_code=409,
                        )
                    resolved_reserved_members.append(
                        {
                            **reserved,
                            "name": clean_activity_text(
                                live.get("groupMemberNick")
                                or live.get("userNick")
                                or live.get("userName")
                                or live.get("name")
                                or reserved["member"]
                            ),
                        }
                    )

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
                        "AND status IN ('scheduled','schedule_blocked','announcing','open',"
                        "'blocked','drawing','notice_unknown','result_unknown')",
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
                        "entry_phrase": current["lottery"]["entry_phrase"]
                        if entry_phrase is None
                        else entry_phrase,
                        "start_at": current["lottery"]["start_at"]
                        if start_at is None
                        else start_at,
                        "luckiest_bonus": current["lottery"]["luckiest_bonus"]
                        if luckiest_bonus is None
                        else f"{Decimal(luckiest_bonus) / 100:.2f}",
                        "integer_packets": lottery.get(
                            "integer_packets", current["lottery"]["integer_packets"]
                        ),
                        "draw_on_full": lottery.get(
                            "draw_on_full", current["lottery"]["draw_on_full"]
                        ),
                        "prize_tiers": current["lottery"]["prize_tiers"]
                        if tiers is None
                        else [
                            {
                                "amount": f"{Decimal(tier['amount']) / 100:.2f}",
                                "count": tier["count"],
                            }
                            for tier in tiers
                        ],
                        "reserved_members": current["lottery"]["reserved_members"]
                        if resolved_reserved_members is None
                        else [
                            {
                                "member": reserved["member"],
                                "peer": reserved["peer"],
                                "name": reserved["name"],
                                "amount": f"{Decimal(reserved['amount']) / 100:.2f}",
                            }
                            for reserved in resolved_reserved_members
                        ],
                    }
                    resolved_tiers = [
                        {
                            "amount": int(Decimal(tier["amount"]) * 100),
                            "count": tier["count"],
                        }
                        for tier in lottery_values["prize_tiers"]
                    ]
                    resolved_reservations = [
                        {
                            "member": reserved["member"],
                            "peer": reserved["peer"],
                            "name": reserved["name"],
                            "amount": int(Decimal(reserved["amount"]) * 100),
                        }
                        for reserved in lottery_values["reserved_members"]
                    ]
                    if "lottery" in body:
                        try:
                            validate_luckiest_bonus(
                                int(Decimal(lottery_values["luckiest_bonus"]) * 100),
                                packet_mode,
                            )
                            if packet_mode == "prize" and (
                                lottery_values["integer_packets"]
                                or lottery_values["draw_on_full"]
                            ):
                                raise ValueError("整数元及满员开奖仅支持红包活动")
                            if (
                                lottery_values["integer_packets"]
                                and int(Decimal(lottery_values["luckiest_bonus"]) * 100)
                                % 100
                            ):
                                raise ValueError("整数元红包的额外奖励也须为整元")
                            if packet_mode != "prize":
                                packet_plan(
                                    total_amount,
                                    capacity,
                                    packet_mode,
                                    resolved_tiers,
                                    generate=False,
                                    integer_packets=lottery_values["integer_packets"],
                                )
                                validate_reserved_members(
                                    resolved_reservations, resolved_tiers
                                )
                                if resolved_reservations and packet_mode != "tiered":
                                    raise ValueError("指定档位成员仅支持分档拼手气")
                            elif resolved_tiers:
                                raise ValueError("奖品抽奖不能设置红包金额档位")
                            elif resolved_reservations:
                                raise ValueError("奖品抽奖不能设置指定档位成员")
                        except ValueError as exc:
                            raise ApiError(str(exc), status_code=400) from None
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
                            "by_invites=?,remarks=?,entry_phrase=?,prize_tiers=?,start_at=?,luckiest_bonus=?,"
                            "integer_packets=?,draw_on_full=?,reserved_members=? "
                            "WHERE account=? AND group_id=?",
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
                                lottery_values["entry_phrase"],
                                json.dumps(resolved_tiers),
                                lottery_values["start_at"],
                                int(Decimal(lottery_values["luckiest_bonus"]) * 100),
                                int(lottery_values["integer_packets"]),
                                int(lottery_values["draw_on_full"]),
                                json.dumps(resolved_reservations, ensure_ascii=True),
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

    async def lottery_control(self):
        """Control a saved round using explicit dashboard authority.

        Returns:
            Creation or cancellation result and current activity snapshot.

        Raises:
            ApiError: If identity, scope, settings or native authority changed.
        """
        from .open_store import OpenError

        service = await self.authorize()
        body = await request.json()
        if (
            type(body) is not dict
            or set(body) - {"draw_at"}
            != {
                "bot_id",
                "group_id",
                "revision",
                "operation_id",
                "action",
                "lottery_id",
            }
            or any(type(body[key]) is not str for key in body)
            or body["action"] not in {"start", "cancel", "reschedule"}
            or ("draw_at" in body) != (body["action"] == "reschedule")
            or not 16 <= len(body["operation_id"]) <= 64
            or not all(
                char.isascii() and (char.isalnum() or char in "-_")
                for char in body["operation_id"]
            )
        ):
            raise ApiError("抽奖控制参数无效", status_code=400)
        deadline = 0
        if body["action"] == "reschedule":
            try:
                deadline = parse_start_at(body["draw_at"], future=True)
                if not deadline:
                    raise ValueError("开奖时间不能为空")
            except ValueError as exc:
                raise ApiError(
                    str(exc).replace("开始时间", "开奖时间"), status_code=400
                ) from None
        try:
            config = service.get_bot(body["bot_id"])["bot"]
        except ValueError:
            raise ApiError("旺商聊实例不存在", status_code=404) from None
        adapter = self._adapter(body["bot_id"])
        integrated = getattr(getattr(self, "plugin", None), "open_service", None)
        activities = getattr(getattr(self, "plugin", None), "activities", None)
        if (
            not adapter
            or not activities
            or not integrated
            or not integrated.active
            or not integrated.integrated
        ):
            raise ApiError("当前活动服务不可用", status_code=503)
        group, actor = body["group_id"], "dashboard/" + request.username
        scope = self._scope_guard(service, config, group, adapter)
        scope()
        try:
            members = await integrated.roster(adapter, group)
        except (OpenError, ProtocolError):
            raise ApiError(
                "无法核验本群成员与原生管理权限，请核对机器人权限后刷新",
                status_code=409,
            ) from None
        scope()
        tenant = dict(integrated.tenants.get((str(adapter.account), group), {}))
        if not tenant or not tenant.get("active") or tenant.get("paused"):
            raise ApiError("当前群未开通或已暂停", status_code=409)
        authority = integrated.rpc.control_guard(adapter, tenant, members)
        managers = [
            uid
            for uid in tenant.get("managers", [])
            if members.get(uid, {}).get("groupRole")
            in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
        ]
        if not managers:
            raise ApiError(
                "本群暂无已核验的负责管理员，请先在开通页面设置", status_code=409
            )
        manager = managers[0]

        def guard():
            scope()
            try:
                authority()
                activities.authorize(adapter, group, manager, actor, posting=True)
            except (OpenError, ProtocolError):
                raise ApiError(
                    "当前群、机器人或负责管理员授权已变化，请刷新核对",
                    status_code=409,
                ) from None
            if (
                not self.active
                or getattr(self.plugin, "activities", None) is not activities
                or not integrated.active
                or integrated.member_evidence.get((str(adapter.account), group), {})
                .get(manager, {})
                .get("groupRole")
                not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
            ):
                raise ApiError(
                    "活动服务或负责管理员权限已变化，请刷新核对", status_code=409
                )

        async with activities.lock(adapter, group):
            guard()
            current = await asyncio.to_thread(
                self._activity_snapshot, adapter, config, group
            )
            guard()
            if current["revision"] != body["revision"]:
                raise ApiError("活动参数已变化，请刷新后重新操作", status_code=409)
            if body["action"] == "start":
                if body["lottery_id"]:
                    raise ApiError("开启请求不能指定历史活动", status_code=400)
                result = await activities._start(
                    adapter,
                    group,
                    manager,
                    actor,
                    clean_activity_text(
                        members[manager].get("userNick")
                        or members[manager].get("name")
                        or manager
                    ),
                    body["operation_id"],
                    control_actor=actor,
                    precondition=guard,
                )
            elif body["action"] == "reschedule":
                identifier = hashlib.sha256(
                    json.dumps(
                        [adapter.account, group, actor, body["operation_id"]]
                    ).encode()
                ).hexdigest()
                async with AsyncDatabase(
                    lambda: activity_database(adapter), write=True, guard=guard
                ) as db:
                    previous = await db.fetchone(
                        "SELECT * FROM lottery_controls WHERE id=?", (identifier,)
                    )
                    changed = previous is None
                    if previous:
                        if (
                            previous["lottery"] != body["lottery_id"]
                            or previous["action"] != "reschedule"
                            or previous["value"] != str(deadline)
                        ):
                            raise ApiError(
                                "同一请求不能修改活动或开奖时间", status_code=409
                            )
                    else:
                        row = await db.fetchone(
                            "SELECT * FROM lotteries WHERE id=? AND account=? AND group_id=?",
                            (body["lottery_id"], adapter.account, group),
                        )
                        if (
                            not row
                            or row["status"] != "open"
                            or not row["draw_on_full"]
                            and row["ends"] <= time.time()
                            or deadline <= time.time()
                        ):
                            raise ApiError(
                                "活动已截止、结束或回执不明，不能调整开奖时间",
                                status_code=409,
                            )
                        await db.execute(
                            "INSERT INTO lottery_controls VALUES(?,?,?,?,?,?,?,?)",
                            (
                                identifier,
                                row["id"],
                                actor,
                                "reschedule",
                                str(deadline),
                                json.dumps(
                                    {
                                        "ends": row["ends"],
                                        "draw_on_full": row["draw_on_full"],
                                    }
                                ),
                                time.time(),
                                "notice_pending",
                            ),
                        )
                        await db.execute(
                            "UPDATE lotteries SET ends=?,draw_on_full=0 "
                            "WHERE id=? AND status='open'",
                            (deadline, row["id"]),
                        )
                notice_status = previous["status"] if previous else "notice_pending"
                if changed:
                    try:
                        notice_status = await adapter.send_reply_text(
                            group,
                            f"lottery-deadline/{adapter.account}/{identifier}",
                            f"本期抽奖改为{beijing_time(deadline)}自动公布，"
                            "报满不提前开奖；原报名和红包金额保留。"
                            "未领完的份额记未支出。",
                            proactive=True,
                            auto_recall=False,
                            authorize=guard,
                        )
                    except Exception:
                        notice_status = "notice_unknown"
                    if notice_status != "accepted":
                        notice_status = "notice_unknown"
                    async with AsyncDatabase(
                        lambda: activity_database(adapter),
                        write=True,
                        guard=account_guard(adapter),
                    ) as db:
                        await db.execute(
                            "UPDATE lottery_controls SET status=? WHERE id=?",
                            (notice_status, identifier),
                        )
                result = (
                    f"本期已设为{beijing_time(deadline)}自动公布，报满不提前开奖；"
                    "原报名、固定红包份额及额外奖励未改变。"
                    + (
                        "群变更通知已受理。"
                        if notice_status == "accepted"
                        else "开奖时间已保存，群变更通知未确认，不自动重发。"
                    )
                )
            else:
                async with AsyncDatabase(
                    lambda: activity_database(adapter), write=True, guard=guard
                ) as db:
                    row = await db.fetchone(
                        "SELECT * FROM lotteries WHERE id=? AND account=? AND group_id=?",
                        (body["lottery_id"], adapter.account, group),
                    )
                    if not row or row["status"] not in {
                        "scheduled",
                        "schedule_blocked",
                        "open",
                        "blocked",
                        "cancelled",
                    }:
                        raise ApiError(
                            "活动已结束、正在开奖或回执不明，不能取消或重抽",
                            status_code=409,
                        )
                    changed = row["status"] != "cancelled"
                    if changed:
                        await db.execute(
                            "UPDATE lotteries SET status='cancelled',unclaimed_amount=CASE "
                            "WHEN packet_amounts!='[]' THEN total_amount ELSE unclaimed_amount END WHERE id=?",
                            (row["id"],),
                        )
                result = "活动已取消；未分配金额记未支出，不自动付款或退款。"
                if changed and row["status"] in {"open", "blocked"}:
                    await adapter.send_reply_text(
                        group,
                        f"lottery-cancel/{adapter.account}/{row['id']}",
                        f"活动已取消：{row['prize']}。",
                        proactive=True,
                        auto_recall=False,
                        authorize=guard,
                    )
            await self.authorize()
            guard()
            snapshot = await asyncio.to_thread(
                self._activity_snapshot, adapter, config, group
            )
            return ok({"result": result, "state": snapshot})

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
            "entry_phrase": "参与抽奖",
            "prize_tiers": [],
            "reserved_members": [],
            "start_at": "",
            "luckiest_bonus": "0.00",
            "integer_packets": False,
            "draw_on_full": False,
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
                                "start_at": row["start_at"]
                                if "start_at" in row.keys()
                                else "",
                                "luckiest_bonus": f"{Decimal(row['luckiest_bonus']) / 100:.2f}"
                                if "luckiest_bonus" in row.keys()
                                else "0.00",
                                "integer_packets": bool(row["integer_packets"])
                                if "integer_packets" in row.keys()
                                else False,
                                "draw_on_full": bool(row["draw_on_full"])
                                if "draw_on_full" in row.keys()
                                else False,
                                "entry_phrase": row["entry_phrase"]
                                if "entry_phrase" in row.keys()
                                else DEFAULT_ENTRY_PHRASE,
                                "prize_tiers": [
                                    {
                                        "amount": f"{Decimal(tier['amount']) / 100:.2f}",
                                        "count": tier["count"],
                                    }
                                    for tier in json.loads(row["prize_tiers"])
                                ]
                                if "prize_tiers" in row.keys()
                                else [],
                                "reserved_members": [
                                    {
                                        "member": reserved["member"],
                                        "peer": reserved["peer"],
                                        "name": reserved["name"],
                                        "amount": f"{Decimal(reserved['amount']) / 100:.2f}",
                                    }
                                    for reserved in json.loads(row["reserved_members"])
                                ]
                                if "reserved_members" in row.keys()
                                else [],
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
                            "SELECT rowid AS number,* "
                            "FROM lotteries WHERE account=? AND group_id=? "
                            "ORDER BY created DESC LIMIT 1",
                            (account, group),
                        ).fetchone()
                        if row:
                            # Keep internal commands, packet pools and identities private.
                            current = {
                                field: row[field]
                                for field in (
                                    "number",
                                    "id",
                                    "prize",
                                    "winners",
                                    "duration",
                                    "capacity",
                                    "invite_gate",
                                    "contact",
                                    "status",
                                    "ends",
                                    "created",
                                    "total",
                                    "error",
                                )
                            }
                            packet_columns = set(row.keys())
                            for flag in ("integer_packets", "draw_on_full"):
                                current[flag] = (
                                    bool(row[flag]) if flag in packet_columns else False
                                )
                            current["starts"] = (
                                row["starts"] if "starts" in packet_columns else 0
                            )
                            current["luckiest_bonus"] = "0.00"
                            if "luckiest_bonus" in packet_columns:
                                current["luckiest_bonus"] = (
                                    f"{Decimal(row['luckiest_bonus']) / 100:.2f}"
                                )
                            if {"entry_phrase", "prize_tiers"} <= packet_columns:
                                current.update(
                                    entry_phrase=row["entry_phrase"],
                                    prize_tiers=[
                                        {
                                            "amount": f"{Decimal(tier['amount']) / 100:.2f}",
                                            "count": tier["count"],
                                        }
                                        for tier in json.loads(row["prize_tiers"])
                                    ],
                                )
                            if {
                                "total_amount",
                                "packet_mode",
                                "by_invites",
                            } <= packet_columns:
                                current.update(
                                    total_amount=f"{Decimal(row['total_amount']) / 100:.2f}",
                                    packet_mode=row["packet_mode"],
                                    by_invites=bool(row["by_invites"]),
                                )
                            if {
                                "remarks",
                                "packet_amounts",
                                "unclaimed_amount",
                            } <= packet_columns:
                                allocations = json.loads(row["results"])
                                current.update(
                                    remarks=row["remarks"],
                                    fixed_packets=row["packet_amounts"] != "[]",
                                    allocated_amount=f"{Decimal(sum(item.get('amount', 0) for item in allocations)) / 100:.2f}",
                                    allocated_count=len(allocations),
                                    unclaimed_amount=f"{Decimal(row['unclaimed_amount']) / 100:.2f}",
                                    bonus_allocated_amount=f"{Decimal(sum(item.get('bonus_amount', 0) for item in allocations)) / 100:.2f}",
                                    total_payable_amount=f"{Decimal(sum(item.get('amount', 0) + item.get('bonus_amount', 0) for item in allocations)) / 100:.2f}",
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
