"""Authenticated plugin settings and read-only moderation audit."""

import asyncio
import copy
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation

from astrbot.api.web import request
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

PLUGIN_NAME = "wangshangliao_moderation"
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
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


class DashboardPage:
    """Expose settings without adding a new sanction execution entry point."""

    def __init__(self, context):
        self.context = context
        self.active = False
        self.save_lock = asyncio.Lock()

    def register(self):
        """Register the plugin-local routes when the plugin starts."""
        self.active = True
        for endpoint, handler, method in (
            ("settings", self.settings, "GET"),
            ("settings", self.save, "POST"),
            ("audit", self.audit, "GET"),
            ("invitations", self.invitations, "GET"),
            ("activities", self.activities, "GET"),
            ("activities", self.save_activities, "POST"),
            ("schedule", self.schedule, "GET"),
            ("schedule", self.pause_schedule, "POST"),
        ):
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                [method],
                "Wangshangliao moderation settings and audit",
            )

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
                        )
                        if field in config
                    },
                    "revision": revision(config),
                    "state": stats.get("connection_state", "stopped"),
                    "last_connected_at": stats.get("last_connected_at"),
                    "directory": stats.get("group_directory_state", {}),
                }
            )
        providers = [
            {"id": provider.meta().id, "model": provider.get_model()}
            for provider in self.context.get_all_providers()
        ]
        return ok({"bots": bots, "providers": providers})

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
            if (
                not isinstance(policy, dict)
                or set(policy) - POLICY_FIELDS
                or not isinstance(group_settings, dict)
                or set(group_settings)
                - {
                    "permissions",
                    "auto_kick",
                    "card_auto",
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
            moderation = config.setdefault("moderation", {})
            moderation.update(copy.deepcopy(policy))
            for field in ("permissions", "auto_kick", "card_auto"):
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
                await service.update_bot(config["id"], config, owner=request.username)
            except ValueError as exc:
                raise ApiError(f"配置无效：{exc}", status_code=400) from None
            return ok(
                {"revision": revision(service.get_bot(config["id"])["bot"])},
                message="配置已保存",
            )

    async def audit(self):
        """Read bounded account/group-scoped audit rows without changing ledgers.

        Returns:
            Recent decisions, automatic sanctions, rule audit and mute counts.

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
        result = {"reviews": [], "sanctions": [], "counts": [], "rules": []}
        path = instance_dir(bot_id) / "moderation.sqlite3"
        if not path.is_file():
            return ok(result)
        account = str(config.get("account_id", ""))
        try:
            with closing(
                sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
            ) as db:
                db.row_factory = sqlite3.Row
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "semantic_reviews" in tables:
                    for row in db.execute(
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
                        for row in db.execute(
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
                            for row in db.execute(
                                f"SELECT operation,member,status,created FROM {table} "
                                "WHERE account=? AND group_id=? ORDER BY created DESC LIMIT 50",
                                (account, group),
                            )
                        )
                result["sanctions"] = sorted(
                    result["sanctions"], key=lambda row: row["created"], reverse=True
                )[:50]
                for sanction in result["sanctions"]:
                    if "operations" in tables:
                        receipt = db.execute(
                            "SELECT result FROM operations WHERE id=?",
                            (sanction["operation"],),
                        ).fetchone()
                        saved = json.loads(receipt[0]) if receipt else {}
                        if (
                            sanction["action"] == "mute"
                            and type(saved.get("minutes")) is int
                        ):
                            sanction["minutes"] = saved["minutes"]
                    if "progressive_actions" in tables:
                        recalled = db.execute(
                            "SELECT recall_status FROM progressive_actions "
                            "WHERE operation=? AND account=? AND group_id=?",
                            (sanction["operation"], account, group),
                        ).fetchone()
                        if recalled:
                            sanction["recall_status"] = recalled[0]
                if "content_violations" in tables:
                    result["rules"] = [
                        dict(row)
                        for row in db.execute(
                            "SELECT message,sender,category,status,observed FROM content_violations "
                            "WHERE account=? AND group_id=? ORDER BY observed DESC LIMIT 50",
                            (account, group),
                        )
                    ]
        except (sqlite3.Error, ValueError):
            raise ApiError(
                "审计记录暂时不可读取，请稍后刷新", status_code=503
            ) from None
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
        return ok(self._activity_snapshot(adapter, config, group))

    async def save_activities(self):
        """Update next-lottery settings and the uniform invitation reward rate."""
        service = await self.authorize()
        body = await request.json()
        lottery_fields = {
            "prize",
            "winners",
            "duration_minutes",
            "capacity",
            "invite_gate",
            "contact",
        }
        if (
            not isinstance(body, dict)
            or set(body) != {"bot_id", "group_id", "revision", "lottery", "invite_rate"}
            or not all(
                isinstance(body.get(key), str)
                for key in ("bot_id", "group_id", "revision")
            )
            or not isinstance(body.get("lottery"), dict)
            or set(body["lottery"]) != lottery_fields
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

        lottery = body["lottery"]
        prize = self._activity_text(lottery["prize"], "奖品")
        contact = self._activity_text(lottery["contact"], "领奖联系人")
        winners = self._bounded_int(lottery["winners"], 1, 100, "中奖人数")
        duration = self._bounded_int(lottery["duration_minutes"], 1, 10080, "倒计时")
        capacity = self._bounded_int(lottery["capacity"], 0, 10000, "参与上限")
        invite_gate = self._bounded_int(lottery["invite_gate"], 0, 10000, "邀请门槛")
        if capacity and winners > capacity:
            raise ApiError("中奖人数不能大于参与上限", status_code=400)
        try:
            reward = Decimal(str(body["invite_rate"]))
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

        async with self.save_lock:
            async with self._activity_lock(adapter, group):
                current = self._activity_snapshot(adapter, config, group)
                if current["revision"] != body["revision"]:
                    raise ApiError(
                        "活动配置已被修改，请刷新后重新编辑", status_code=409
                    )
                with closing(activity_database(adapter)) as db, db:
                    active = db.execute(
                        "SELECT 1 FROM lotteries WHERE account=? AND group_id=? "
                        "AND status IN ('announcing','open','blocked','drawing')",
                        (account, group),
                    ).fetchone()
                    lottery_values = {
                        "prize": prize,
                        "winners": winners,
                        "duration_minutes": duration,
                        "capacity": capacity,
                        "invite_gate": invite_gate,
                        "contact": contact,
                    }
                    if active and any(
                        current["lottery"][key] != value
                        for key, value in lottery_values.items()
                    ):
                        raise ApiError(
                            "抽奖进行中，当前期参数已冻结；请结束后再修改",
                            status_code=409,
                        )
                    db.execute(
                        "INSERT OR IGNORE INTO lottery_settings(account,group_id) "
                        "VALUES(?,?)",
                        (account, group),
                    )
                    db.execute(
                        "UPDATE lottery_settings SET prize=?,winners=?,duration=?,"
                        "capacity=?,invite_gate=?,contact=? WHERE account=? AND group_id=?",
                        (
                            prize,
                            winners,
                            duration * 60,
                            capacity,
                            invite_gate,
                            contact,
                            account,
                            group,
                        ),
                    )
                    db.execute(
                        "INSERT INTO invite_rules(account,group_id,rate) VALUES(?,?,?) "
                        "ON CONFLICT(account,group_id) DO UPDATE SET rate=excluded.rate",
                        (account, group, rate),
                    )
                return ok(self._activity_snapshot(adapter, config, group))

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
        path = instance_dir(bot_id) / "schedules.sqlite3"
        if not path.is_file():
            return ok({"schedule": None, "executions": [], "history": []})
        account = str(config.get("account_id", ""))
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
                schedule_row = (
                    db.execute(
                        "SELECT group_id,start,end,zone,version,status,next_at,"
                        "next_action,error FROM schedules WHERE account=? AND group_id=?",
                        (account, group),
                    ).fetchone()
                    if "schedules" in tables
                    else None
                )
                executions = (
                    [
                        dict(row)
                        for row in db.execute(
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
                        for row in db.execute(
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
        return ok(
            {
                "schedule": dict(schedule_row) if schedule_row else None,
                "executions": executions,
                "history": history,
            }
        )

    async def pause_schedule(self):
        """Pause future work without changing the group's current mute state."""
        service = await self.authorize()
        body = await request.json()
        if (
            not isinstance(body, dict)
            or set(body) != {"bot_id", "group_id", "version", "action"}
            or not isinstance(body.get("bot_id"), str)
            or not isinstance(body.get("group_id"), str)
            or type(body.get("version")) is not int
            or body.get("action") != "pause"
        ):
            raise ApiError("定时操作参数无效", status_code=400)
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
        async with group_lock(adapter, group):
            with closing(schedule_database(adapter)) as db, db:
                row = db.execute(
                    "SELECT version,status FROM schedules WHERE account=? AND group_id=?",
                    (str(config.get("account_id", "")), group),
                ).fetchone()
                if not row or row[1] == "deleted":
                    raise ApiError("当前群没有可暂停的定时规则", status_code=404)
                if row[0] != body["version"]:
                    raise ApiError("定时规则已变化，请刷新后再操作", status_code=409)
                if row[1] == "active":
                    db.execute(
                        "UPDATE confirmations SET used=1 WHERE account=? AND group_id=?",
                        (str(config.get("account_id", "")), group),
                    )
                    db.execute(
                        "UPDATE schedules SET status='paused',version=version+1,"
                        "error='dashboard_operator' WHERE account=? AND group_id=?",
                        (str(config.get("account_id", "")), group),
                    )
        return ok({"paused": True, "message": "已暂停后续定时操作；当前群状态未改变。"})

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
    def _activity_text(value, label):
        if not isinstance(value, str):
            raise ApiError(f"{label}须为单行文字", status_code=400)
        clean = clean_activity_text(value)
        if not clean or clean != value or len(clean.encode("utf-8")) > 512:
            raise ApiError(f"{label}须为1至128字的单行文字", status_code=400)
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
        }
        invitation = {
            "rate": "0.00",
            "enabled": False,
            "last_scan": 0,
            "error": "",
            "credited_count": 0,
            "credited_total": "0.00",
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
                            }
                    if "invite_rules" in tables:
                        row = db.execute(
                            "SELECT rate,enabled,last_scan,error FROM invite_rules "
                            "WHERE account=? AND group_id=?",
                            (account, group),
                        ).fetchone()
                        if row:
                            invitation.update(
                                rate=f"{row['rate'] / 100:.2f}",
                                enabled=bool(row["enabled"]),
                                last_scan=row["last_scan"],
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
            "revision": revision(editable),
        }
