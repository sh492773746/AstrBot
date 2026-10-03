"""Owner-bound preview and confirmation for natural-language admin settings."""

import copy
import hashlib
import json
import re
import secrets
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation

from astrbot.core import astrbot_config
from astrbot.core.platform.sources.wangshangliao.policy import (
    authorize_action,
    validate_policy,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.dashboard.responses import ApiError

from .activity_store import database as activity_database
from .dashboard import DashboardPage


class AdminConfigDrafts:
    """Validate config proposals and bind one-time confirmations to admin sessions."""

    def __init__(self, context, schedules, activities):
        self.context = context
        self.schedules = schedules
        self.activities = activities
        self.drafts = {}

    async def preview(self, event, value):
        try:
            proposal = json.loads(value)
            if not isinstance(proposal, dict) or set(proposal) != {
                "target",
                "group",
                "changes",
            }:
                raise ValueError("proposal must contain target, group and changes")
            group = self._resolve_group(event.platform, proposal["group"])
            changes = proposal["changes"]
            if not group or not isinstance(changes, dict) or not changes:
                raise ValueError("group missing, ambiguous or no changes")
            target = proposal["target"]
            adapter = event.platform
            if target == "moderation":
                draft, diff = self._moderation(adapter, group, changes)
            elif target == "activities":
                draft, diff = self._activities(adapter, group, changes)
            elif target == "lottery_start":
                if set(changes) != {"lottery"}:
                    raise ValueError("lottery_start requires lottery settings only")
                self.activities.authorize(
                    adapter,
                    group,
                    event.get_sender_id(),
                    event.unified_msg_origin,
                    posting=True,
                )
                draft, diff = self._activities(adapter, group, changes)
                lottery = draft["values"]["lottery"]
                if not lottery["prize"] or not lottery["contact"]:
                    raise ValueError("prize and contact are required before opening")
                current = DashboardPage._activity_snapshot(
                    adapter, adapter.config, group
                )["current_lottery"]
                if current and current["status"] not in {"finished", "cancelled"}:
                    raise ValueError("existing lottery must be resolved first")
                draft["lottery_state"] = self._lottery_state(current)
                diff["operation"] = "确认后保存参数、开启抽奖并发送群通知"
            elif target == "schedule":
                draft, diff = self._schedule(adapter, group, changes)
            else:
                raise ValueError("unsupported configuration area")
            token = secrets.token_urlsafe(12)
            draft.update(
                {
                    "target": target,
                    "group": group,
                    "token": token,
                    "expires": time.monotonic() + 600,
                    "bot_revision": self._revision(adapter.config),
                    "diff": diff,
                }
            )
            key = self._key(event)
            self.drafts[key] = draft
            group_name = getattr(adapter, "group_names", {}).get(group, group)
            display = {}
            if target == "lottery_start":
                display["display_text"] = self._lottery_preview_text(
                    group_name, diff, token
                )
            elif target == "moderation" and set(changes) == {"card_auto"}:
                before = "开启" if diff["before"]["card_auto"] else "关闭"
                after = "开启" if diff["after"]["card_auto"] else "关闭"
                display["display_text"] = (
                    f"【自动名片设置预览】\n群：{group_name}\n"
                    f"自动规范成员名片：{before} -> {after}\n"
                    "仅影响这个群，不修改动作授权。关闭不恢复已改名片。\n"
                    "开启后后台按既有规则扫描：通常保留原名的前两个字符，"
                    "短名称使用已有名称或生成群员名；不是任意自定义模板。\n"
                    f"尚未保存，10分钟内在本私聊发送：确认设置 {token}"
                )
            return json.dumps(
                {
                    "status": "preview",
                    "group": group_name,
                    "changes": diff,
                    "expires_in_seconds": 600,
                    "confirmation": f"在本私聊发送：确认设置 {token}",
                    **display,
                },
                ensure_ascii=False,
            )
        except ApiError as exc:
            return json.dumps(
                {"status": "rejected", "reason": exc.message[:160]},
                ensure_ascii=False,
            )
        except (TypeError, ValueError, KeyError, ProtocolError) as exc:
            return json.dumps(
                {"status": "rejected", "reason": str(exc)[:160]},
                ensure_ascii=False,
            )

    async def confirm(self, event, value):
        key = self._key(event)
        draft = self.drafts.get(key)
        token = value.strip()
        if (
            not draft
            or time.monotonic() >= draft["expires"]
            or not secrets.compare_digest(draft["token"], token)
            or not re.fullmatch(
                rf"确认设置\s+{re.escape(token)}", event.message_str.strip()
            )
        ):
            return json.dumps(
                {
                    "status": "rejected",
                    "reason": "invalid_expired_or_nonexplicit_confirmation",
                }
            )
        self.drafts.pop(key, None)
        adapter = event.platform
        if draft["bot_revision"] != self._revision(adapter.config):
            return json.dumps(
                {"status": "rejected", "reason": "configuration_changed_preview_again"}
            )
        try:
            if draft["target"] == "moderation":
                await self._apply_moderation(adapter, draft)
            elif draft["target"] == "activities":
                await self._apply_activities(adapter, draft)
            elif draft["target"] == "lottery_start":
                return await self._apply_activities(adapter, draft, start_event=event)
            else:
                result = await self.schedules.command(
                    event,
                    draft["group"],
                    "定时禁言",
                    f"{draft['start']} {draft['end']}",
                )
                return json.dumps(
                    {"status": "schedule_confirmation_required", "text": result},
                    ensure_ascii=False,
                )
        except (ApiError, ValueError, ProtocolError) as exc:
            return json.dumps({"status": "rejected", "reason": type(exc).__name__})
        except Exception as exc:
            return json.dumps({"status": "rejected", "reason": type(exc).__name__})
        return json.dumps(
            {
                "status": "saved",
                "group": draft["group"],
                "changes": draft["diff"],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _key(event):
        adapter = event.platform
        return (
            adapter.config.get("id"),
            adapter.account,
            str(event.get_sender_id()),
            str(event.unified_msg_origin),
        )

    @staticmethod
    def _revision(config):
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _lottery_state(current):
        return (current["id"], current["status"]) if current else None

    @staticmethod
    def _lottery_preview_text(group, diff, token):
        before, after = diff["before"]["lottery"], diff["after"]["lottery"]
        lines = [f"【创建抽奖预览】\n群：{group}\n尚未保存或开启活动。\n"]
        for field, label in (
            ("prize", "奖品"),
            ("winners", "中奖人数"),
            ("duration_minutes", "倒计时（分钟）"),
            ("capacity", "参与上限（0为不限）"),
            ("invite_gate", "邀请门槛"),
            ("contact", "领奖联系人"),
        ):
            old = before[field] if before[field] != "" else "未设置"
            lines.append(f"{label}：{old} -> {after[field]}")
        lines.extend(
            [
                f"\n确认后开启报名并通知群；开启后 {after['duration_minutes']} 分钟到时开奖。",
                f"10分钟内在本私聊发送：确认设置 {token}",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _resolve_group(adapter, requested):
        if not isinstance(requested, str):
            return ""
        enabled = set(adapter.config.get("enabled_groups", [])) & set(
            getattr(adapter, "groups", {})
        )
        if requested in enabled:
            return requested
        matches = [
            group
            for group in enabled
            if getattr(adapter, "group_names", {}).get(group) == requested
        ]
        return matches[0] if len(matches) == 1 else ""

    def _moderation(self, adapter, group, changes):
        allowed = {
            "enabled",
            "automation_enabled",
            "recall_enabled",
            "progressive_mute",
            "cooldown_seconds",
            "mute_keywords",
            "kick_keywords",
            "semantic",
            "auto_kick",
            "card_auto",
        }
        if set(changes) - allowed:
            raise ValueError("protected or unknown moderation setting")
        config = copy.deepcopy(adapter.config)
        policy = config.setdefault("moderation", {})
        before, after = {}, {}
        for field, value in changes.items():
            if field in {"auto_kick", "card_auto"}:
                if type(value) is not bool:
                    raise ValueError(f"{field} must be boolean")
                if field == "card_auto" and value:
                    authorize_action(adapter.config, group, "rename")
                before[field] = policy.setdefault(field, {}).get(group, False)
                policy[field][group] = value
                after[field] = value
            elif field == "semantic":
                if not isinstance(value, dict) or set(value) - {
                    "enabled",
                    "provider_id",
                    "timeout_seconds",
                    "context_limit",
                }:
                    raise ValueError("invalid semantic settings")
                before[field] = copy.deepcopy(policy.get(field, {}))
                policy[field] = {**policy.get(field, {}), **value}
                after[field] = copy.deepcopy(policy[field])
            else:
                before[field] = copy.deepcopy(policy.get(field))
                policy[field] = copy.deepcopy(value)
                after[field] = copy.deepcopy(value)
        validate_policy(policy)
        if policy.get("semantic", {}).get("enabled") and not policy.get(
            "content_rules_since"
        ):
            policy["content_rules_since"] = time.time()
        provider_ids = {item.meta().id for item in self.context.get_all_providers()}
        semantic = policy.get("semantic", {})
        if semantic.get("provider_id") and semantic["provider_id"] not in provider_ids:
            raise ValueError("moderation provider unavailable")
        return {"config": config}, {"before": before, "after": after}

    @staticmethod
    def _activities(adapter, group, changes):
        if set(changes) - {"lottery", "invite_rate"}:
            raise ValueError("unknown activity setting")
        current = DashboardPage._activity_snapshot(adapter, adapter.config, group)
        lottery = copy.deepcopy(current["lottery"])
        update = changes.get("lottery", {})
        if not isinstance(update, dict) or set(update) - set(lottery):
            raise ValueError("invalid lottery fields")
        lottery.update(update)
        rate = changes.get("invite_rate", current["invite_rate"])
        try:
            decimal_rate = Decimal(str(rate))
        except (InvalidOperation, ValueError):
            raise ValueError("invalid invitation reward") from None
        if (
            not decimal_rate.is_finite()
            or decimal_rate < 0
            or decimal_rate > Decimal("999999.99")
            or decimal_rate.as_tuple().exponent < -2
        ):
            raise ValueError("invitation reward must be 0..999999.99 with 2 decimals")
        for field, low, high in (
            ("winners", 1, 100),
            ("duration_minutes", 1, 10080),
            ("capacity", 0, 10000),
            ("invite_gate", 0, 10000),
        ):
            DashboardPage._bounded_int(lottery[field], low, high, field)
        if lottery["capacity"] and lottery["winners"] > lottery["capacity"]:
            raise ValueError("winner count exceeds entry capacity")
        for field in ("prize", "contact"):
            if field in update and not lottery[field]:
                raise ValueError(f"{field} must not be empty")
            if lottery[field]:
                DashboardPage._activity_text(lottery[field], field)
        next_values = {"lottery": lottery, "invite_rate": str(decimal_rate)}
        return (
            {"revision": current["revision"], "values": next_values},
            {
                "before": {
                    "lottery": current["lottery"],
                    "invite_rate": current["invite_rate"],
                },
                "after": next_values,
            },
        )

    @staticmethod
    def _schedule(adapter, group, changes):
        if (
            set(changes) != {"start", "end"}
            or any(
                not isinstance(changes[key], str)
                or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", changes[key])
                for key in ("start", "end")
            )
            or changes["start"] == changes["end"]
        ):
            raise ValueError("schedule requires distinct HH:MM start and end")
        return (
            {"start": changes["start"], "end": changes["end"]},
            {
                "before": "当前时段",
                "after": {"start": changes["start"], "end": changes["end"]},
            },
        )

    async def _apply_moderation(self, adapter, draft):
        config = draft["config"]
        platforms = astrbot_config.get("platform", [])
        index = next(
            (i for i, item in enumerate(platforms) if item.get("id") == config["id"]),
            None,
        )
        if index is None:
            raise ValueError("bot configuration missing")
        previous_global = copy.deepcopy(platforms[index])
        previous_live = copy.deepcopy(adapter.config)
        platforms[index] = copy.deepcopy(config)
        adapter.config.clear()
        adapter.config.update(copy.deepcopy(config))
        try:
            committed = await astrbot_config.save_config_async()
            if committed is False:
                raise ValueError("configuration persistence superseded")
        except Exception:
            platforms[index] = previous_global
            adapter.config.clear()
            adapter.config.update(previous_live)
            await astrbot_config.save_config_async()
            raise ValueError("configuration persistence failed") from None

    async def _apply_activities(self, adapter, draft, *, start_event=None):
        group = draft["group"]
        values = draft["values"]
        async with self.activities.lock(adapter, group):
            current = DashboardPage._activity_snapshot(adapter, adapter.config, group)
            if current["revision"] != draft["revision"]:
                raise ValueError("activity settings changed; preview again")
            if start_event is not None:
                self.activities.authorize(
                    adapter,
                    group,
                    start_event.get_sender_id(),
                    start_event.unified_msg_origin,
                    posting=True,
                )
                if (
                    self._lottery_state(current["current_lottery"])
                    != draft["lottery_state"]
                ):
                    raise ValueError("lottery state changed; preview again")
            with closing(activity_database(adapter)) as db, db:
                if (
                    db.execute(
                        "SELECT 1 FROM lotteries WHERE account=? AND group_id=? "
                        "AND status IN ('announcing','open','blocked','drawing')",
                        (adapter.account, group),
                    ).fetchone()
                    and values["lottery"] != current["lottery"]
                ):
                    raise ValueError("active lottery settings are frozen")
                lottery = values["lottery"]
                db.execute(
                    "INSERT OR IGNORE INTO lottery_settings(account,group_id) VALUES(?,?)",
                    (adapter.account, group),
                )
                db.execute(
                    "UPDATE lottery_settings SET prize=?,winners=?,duration=?,capacity=?,"
                    "invite_gate=?,contact=? WHERE account=? AND group_id=?",
                    (
                        lottery["prize"],
                        lottery["winners"],
                        lottery["duration_minutes"] * 60,
                        lottery["capacity"],
                        lottery["invite_gate"],
                        lottery["contact"],
                        adapter.account,
                        group,
                    ),
                )
                if start_event is None:
                    db.execute(
                        "INSERT INTO invite_rules(account,group_id,rate) VALUES(?,?,?) "
                        "ON CONFLICT(account,group_id) DO UPDATE SET rate=excluded.rate",
                        (
                            adapter.account,
                            group,
                            int(Decimal(values["invite_rate"]) * 100),
                        ),
                    )
            if start_event is not None:
                try:
                    text = await self.activities.start(start_event, group)
                except Exception:
                    text = "参数已保存，但开启或通知结果未确认；请查询抽奖状态，不要重复提交。"
                command = hashlib.sha256(
                    f"{adapter.account}/{start_event.unified_msg_origin}/"
                    f"{start_event.message_obj.message_id}".encode()
                ).hexdigest()
                with closing(activity_database(adapter)) as db:
                    row = db.execute(
                        "SELECT id,status FROM lotteries WHERE command=?", (command,)
                    ).fetchone()
                return json.dumps(
                    {
                        "status": "started"
                        if row and row["status"] == "open"
                        else "not_confirmed",
                        "settings_saved": True,
                        "group": group,
                        "lottery_id": row["id"] if row else None,
                        "lottery_status": row["status"] if row else None,
                        "text": text,
                    },
                    ensure_ascii=False,
                )
