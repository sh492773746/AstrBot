"""Run only Dashboard-configured automation, never chat-issued moderation."""

import asyncio
import copy
import sqlite3
import time

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

GROUP_ADMIN_REQUIRED = "当前机器人不是本群群管理员，本群功能暂不可用。请先把机器人设为群管理员，再使用命令。"


def require_group_admin(config, group):
    """Require verified native bot privileges for public group functionality.

    Args:
        config: Current account policy, not user-supplied message content.
        group: Exact business group whose functionality will be used.

    Raises:
        ProtocolError: If public bot privileges are missing or unverified.
    """
    if "_open_groups" in config and config.get("_open_native_roles", {}).get(
        group
    ) not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}:
        raise ProtocolError("group_native_admin_required")


def is_fresh_message(payload: dict, now: float) -> bool:
    """Check authenticated message time, never substituting local receipt time."""
    try:
        route = payload.get("recall_route") or {}
        raw = route.get("time")
        if raw is not None:
            if isinstance(raw, bool):
                return False
            stamp = float(raw)
            if stamp > 100000000000:
                stamp /= 1000
        else:
            raw = payload.get("created_at", 0)
            if type(raw) not in (int, float):
                return False
            stamp = raw / 1000
        return 0 < stamp <= now + 5 and now - stamp <= 300
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


async def handle(
    adapter,
    group: str,
    mid: str,
    payload: dict,
    *,
    assessment=None,
    execution_check=None,
) -> bool:
    """Apply an authorized keyword rule without granting chat control.

    Args:
        adapter: Authenticated native platform instance.
        group: Business group ID.
        mid: Stable incoming message ID.
        payload: Authenticated incoming message payload.
        assessment: Optional validated model assessment of this exact message.
        execution_check: Optional authority check for independent business workers.

    Returns:
        Whether the rule consumed this event.
    """
    from astrbot.core.platform.sources.wangshangliao.group_scope import scoped

    from .member_exemptions import is_exempt

    adapter = scoped(adapter, group)
    if await asyncio.to_thread(
        is_exempt,
        adapter.config.get("id", ""),
        str(getattr(adapter, "account", "")),
        group,
        str(payload.get("sender", "")),
        payload.get("recall_route"),
    ):
        return False
    try:
        require_group_admin(adapter.config, group)
    except ProtocolError:
        return False
    if payload.get("_audit_incomplete") or group in getattr(
        getattr(adapter, "ledger", None), "review_groups", set()
    ):
        adapter.diagnostics.emit(
            "moderation", "audit_incomplete", mid, failed=True, aggregate=True
        )
        return False
    policy = adapter.config.get("moderation", {})
    if str(payload.get("sender", "")) in adapter.config.get("_open_managers", {}).get(
        group, []
    ):
        return False
    text = payload.get("text", "").strip()
    if group.startswith("private/"):
        return False
    if not policy.get("enabled") or not policy.get("automation_enabled"):
        return False
    if (
        policy.get("content_rules_since")
        or policy.get("progressive_mute")
        or payload.get("kind") in ("image", "video", "forward")
        or (assessment and assessment.get("category") == "message_flood")
    ):
        from .content_rules import enforce

        if execution_check:
            return await enforce(
                adapter,
                group,
                mid,
                payload,
                assessment=assessment,
                execution_check=execution_check,
            )
        return await enforce(adapter, group, mid, payload, assessment=assessment)
    mute = any(
        word in text
        for word in policy.get("mute_keywords", policy.get("keywords", []))
        if word
    )
    kick = any(word in text for word in policy.get("kick_keywords", []) if word)
    if policy.get("manual_kick_only"):
        kick = False
    if not mute and not kick:
        return False
    # Reconnect history must not increment warnings or trigger new sanctions.
    if not is_fresh_message(payload, time.time()):
        adapter.diagnostics.emit("moderation", "historical_or_unknown_time", mid)
        return True
    action = "mute"
    account = adapter.account
    frozen_policy = copy.deepcopy(policy)
    path = instance_dir(adapter.config["id"]) / "moderation.sqlite3"

    def authorize():
        authorize_action(adapter.config, group, action)
        if is_exempt(
            adapter.config["id"],
            str(adapter.account),
            group,
            str(payload.get("sender", "")),
            payload.get("recall_route"),
        ):
            raise ProtocolError("moderation_member_exempt")
        if adapter.config.get("moderation", {}) != frozen_policy:
            raise ProtocolError("automation_policy_changed")

    guard = account_guard(adapter, authorize)
    try:
        authorize_action(adapter.config, group, action)
        sender = str(payload["sender"])
        if sender == adapter.account:
            return True
        roster = await adapter.get_moderation_members(group)
        matches = [
            m
            for m in roster.get("groupMemberInfo", [])
            if str(m.get("userId")) == sender
        ]
        if (
            not roster.get("complete")
            or len(matches) != 1
            or not matches[0].get("nimId")
            or str(matches[0]["nimId"]) != adapter.members.get(group, {}).get(sender)
        ):
            raise ProtocolError("moderation_identity")
        if matches[0].get("groupRole") != "GROUP_ROLE_MEMBER":
            return True
        authorize_action(adapter.config, group, action)
        if not adapter.config.get("moderation", {}).get("automation_enabled"):
            return True
        async with AsyncDatabase(
            lambda: sqlite3.connect(path), write=True, guard=guard
        ) as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS violations(account TEXT, group_id TEXT, sender TEXT, started REAL, count INTEGER, PRIMARY KEY(account,group_id,sender))",
                "CREATE TABLE IF NOT EXISTS violation_warnings(account TEXT, group_id TEXT, sender TEXT, started REAL, status TEXT, PRIMARY KEY(account,group_id,sender))",
                "CREATE TABLE IF NOT EXISTS violation_messages(account TEXT, group_id TEXT, message TEXT, PRIMARY KEY(account,group_id,message))",
            ):
                await db.execute(statement)
            await db.execute(
                "CREATE TABLE IF NOT EXISTS rate_limits (account TEXT, group_id TEXT, updated REAL, PRIMARY KEY(account, group_id))"
            )
            if await db.fetchone(
                "SELECT 1 FROM violation_messages WHERE account=? AND group_id=? AND message=?",
                (adapter.account, group, mid),
            ):
                return True
            now = time.time()
            prior = await db.fetchone(
                "SELECT started,count FROM violations WHERE account=? AND group_id=? AND sender=?",
                (adapter.account, group, sender),
            )
            started, count = (
                (prior[0], prior[1] + 1)
                if prior and 0 <= now - prior[0] < 86400
                else (now, 1)
            )
            await db.execute(
                "INSERT INTO violation_messages VALUES(?,?,?)",
                (adapter.account, group, mid),
            )
            await db.execute(
                "INSERT OR REPLACE INTO violations VALUES(?,?,?,?,?)",
                (adapter.account, group, sender, started, count),
            )
            warning = await db.fetchone(
                "SELECT started,status FROM violation_warnings WHERE account=? AND group_id=? AND sender=?",
                (adapter.account, group, sender),
            )
            needs_warning = (
                count == 1
                or warning is None
                or bool(warning and warning[0] == started and warning[1] == "rejected")
            )
            if needs_warning:
                await db.execute(
                    "INSERT OR REPLACE INTO violation_warnings VALUES(?,?,?,?,?)",
                    (adapter.account, group, sender, started, "unknown"),
                )
            row = await db.fetchone(
                "SELECT updated FROM rate_limits WHERE account=? AND group_id=?",
                (adapter.account, group),
            )
            cooling = bool(row and now - row[0] < policy.get("cooldown_seconds", 60))
            warned = bool(
                warning
                and warning[0] == started
                and warning[1] in {"accepted", "verified"}
            )
            if count > 1 and warned and not needs_warning and not cooling:
                await db.execute(
                    "INSERT OR REPLACE INTO rate_limits VALUES(?,?,?)",
                    (adapter.account, group, now),
                )
        guard()
        peer = str(matches[0]["nimId"])
        if policy.get("recall_enabled"):
            try:
                await adapter.execute_action(
                    ActionRequest.bind(
                        adapter,
                        f"recall/{account}/{group}/{mid}",
                        "recall",
                        int(group),
                        member=int(sender),
                        expected_nim=peer,
                        message=mid,
                    ),
                    authorize=guard,
                )
            except Exception:
                adapter.diagnostics.emit(
                    "recall", "failed_or_unknown", mid, failed=True
                )
        guard()
        if needs_warning:
            if not peer.isdigit() or not 0 < int(peer) < 1 << 32:
                raise ProtocolError("mention_identity")
            nick = str(payload.get("name") or sender)
            prefix = f"@{nick} "
            status = "unknown"
            try:
                result = await adapter.execute_action(
                    ActionRequest.bind(
                        adapter,
                        f"warning/{group}/{mid}",
                        "warning",
                        int(group),
                        member=int(sender),
                        expected_nim=peer,
                        text=prefix + "违规警告：24小时内再次违规将执行群管理处罚。",
                        mention_name=nick,
                    ),
                    authorize=guard,
                )
                status = result["status"]
            except ProtocolError as exc:
                if str(exc) in {"not_online", "text_limit", "mention_identity"}:
                    status = "rejected"
                adapter.diagnostics.emit(
                    "warning", "failed_or_unknown", mid, failed=True
                )
            except Exception:
                adapter.diagnostics.emit(
                    "warning", "failed_or_unknown", mid, failed=True
                )
            finally:
                async with AsyncDatabase(
                    lambda: sqlite3.connect(path), write=True
                ) as db:
                    await db.execute(
                        "UPDATE violation_warnings SET status=? WHERE account=? AND group_id=? AND sender=? AND started=?",
                        (
                            status
                            if isinstance(status, str)
                            and status in {"accepted", "verified", "rejected"}
                            else "unknown",
                            account,
                            group,
                            sender,
                            started,
                        ),
                    )
        elif warned and not cooling:
            if action == "mute" and policy.get("auto_kick", {}).get(group) is True:
                from astrbot.core.platform.sources.wangshangliao.automatic import (
                    mute_and_escalate,
                )

                await mute_and_escalate(
                    adapter, f"message/{group}/{mid}", group, sender, mid
                )
            else:
                await adapter.execute_action(
                    ActionRequest.bind(
                        adapter,
                        f"message/{group}/{mid}",
                        action,
                        int(group),
                        member=int(sender),
                    ),
                    authorize=guard,
                )
    except (ProtocolError, ValueError):
        adapter.diagnostics.emit("moderation", "rejected", mid)
    return True
