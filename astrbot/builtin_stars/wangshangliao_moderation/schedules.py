"""Private administrator previews and persistent daily group mute schedules."""

import asyncio
import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.schedule_store import (
    database,
    group_lock,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .observability import record
from .policy import require_group_admin


def next_boundary(start, end, now):
    """Return the strictly future daily boundary in Asia/Shanghai.

    Args:
        start: HH:MM mute time.
        end: HH:MM unmute time.
        now: UTC epoch seconds.

    Returns:
        Epoch seconds and fixed action name.
    """
    current = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))
    candidates = []
    for value, action in ((start, "mute_all"), (end, "unmute_all")):
        hour, minute = map(int, value.split(":"))
        point = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if point.timestamp() <= now:
            point += timedelta(days=1)
        candidates.append((point.timestamp(), action))
    return min(candidates)


class GroupSchedules:
    """Own only fixed-purpose daily group toggles, never model jobs."""

    def __init__(self, context):
        self.context = context
        self.recovered = set()

    async def native_permission(self, adapter, group, available):
        """Pause native-authority jobs and resume only at a strictly future boundary.

        Args:
            adapter: Authenticated robot whose own native role was verified.
            group: Exact business group, isolated from other jobs.
            available: Whether the robot is a native owner or administrator.
        """
        guard = account_guard(adapter)
        async with group_lock(adapter, group):
            async with AsyncDatabase(
                lambda: database(adapter), write=True, guard=guard
            ) as db:
                row = await db.fetchone(
                    "SELECT * FROM schedules WHERE account=? AND group_id=?",
                    (adapter.account, group),
                )
                if not row:
                    return
                if not available and row[8] == "active":
                    await db.execute(
                        "UPDATE schedules SET status='role_paused',error='native_permission_lost',"
                        "version=version+1 WHERE account=? AND group_id=? AND version=?",
                        (adapter.account, group, row[7]),
                    )
                    await db.execute(
                        "DELETE FROM confirmations WHERE account=? AND group_id=?",
                        (adapter.account, group),
                    )
                elif available and row[8] == "role_paused":
                    try:
                        self.authorize(adapter, row[2], row[3], group)
                    except ProtocolError:
                        return
                    planned, action = next_boundary(row[4], row[5], time.time())
                    await db.execute(
                        "UPDATE schedules SET status='active',error='',version=version+1,"
                        "next_at=?,next_action=? WHERE account=? AND group_id=? AND version=?",
                        (planned, action, adapter.account, group, row[7]),
                    )

    def authorize(self, adapter, owner, session, group):
        """Recheck effective creator authority and both group capabilities.

        Args:
            adapter: Original account adapter.
            owner: Creating administrator UID.
            session: Original private session used for profile routing.
            group: Authorized target group.
        """
        management = getattr(self, "management", None)
        if "_open_groups" in adapter.config and (
            management is None
            or owner
            not in management.assignments.get(management.key(adapter, group), ())
            or group not in adapter.config.get("enabled_groups", [])
        ):
            raise ProtocolError("schedule_owner_revoked")
        if owner not in {
            str(x) for x in self.context.get_config(session).get("admins_id", [])
        } and (
            management is None
            or owner
            not in management.assignments.get(management.key(adapter, group), ())
            or group not in adapter.config.get("enabled_groups", [])
        ):
            raise ProtocolError("schedule_owner_revoked")
        if adapter.connection_state != "online" or adapter.stopping.is_set():
            raise ProtocolError("schedule_offline")
        require_group_admin(adapter.config, group)
        for action in ("mute_all", "unmute_all"):
            authorize_action(adapter.config, group, action)

    async def command(self, event, group, action, argument, *, precondition=None):
        """Handle authorized schedule commands with conversation-bound previews.

        Args:
            event: Authenticated private or current-group administrator event.
            group: Current or privately selected group.
            action: Fixed Chinese command name.
            argument: Time pair or confirmation token.
            precondition: Optional bound draft check under the schedule lock.

        Returns:
            Plain-text preview or durable status.
        """
        management = getattr(self, "management", None)
        group_management = (
            "_open_groups" in event.platform.config
            and management is not None
            and not event.is_private_chat()
            and str(event.get_group_id()) == str(group)
        )
        if not (event.is_private_chat() or group_management) or not (
            management.allowed(event, group)
            if management is not None
            else event.is_admin()
        ):
            return "无权限"
        adapter, owner, session = (
            event.platform,
            str(event.get_sender_id()),
            str(event.unified_msg_origin),
        )

        version_key = management.key(adapter, group) if management is not None else None
        grant_version = management.versions.get(version_key, 0) if management else 0

        def authorized():
            if (
                management is not None
                and management.versions.get(version_key, 0) != grant_version
            ):
                raise ProtocolError("schedule_owner_revoked")
            if owner not in {
                str(uid)
                for uid in self.context.get_config(session).get("admins_id", [])
            } and not (management is not None and management.allowed(event, group)):
                raise ProtocolError("schedule_owner_revoked")
            if action not in {"定时状态", "暂停定时", "删除定时"}:
                self.authorize(adapter, owner, session, group)

        db_guard = account_guard(adapter, authorized)
        async with group_lock(adapter, group):
            db_guard()
            if precondition is not None:
                await precondition()
            async with AsyncDatabase(
                lambda: database(adapter), write=True, guard=db_guard
            ) as db:
                if management is not None and "_open_groups" in adapter.config:
                    if "grant_version" not in {
                        column[1]
                        for column in await db.fetchall(
                            "PRAGMA table_info(confirmations)"
                        )
                    }:
                        await db.execute(
                            "ALTER TABLE confirmations ADD COLUMN grant_version INTEGER NOT NULL DEFAULT -1"
                        )
                row = await db.fetchone(
                    "SELECT * FROM schedules WHERE account=? AND group_id=?",
                    (adapter.account, group),
                )
                version = row[7] if row else 0
                if action == "定时状态":
                    if not row or row[8] == "deleted":
                        return "本群尚未启用定时规则。"
                    last = await db.fetchone(
                        "SELECT action,status FROM executions WHERE account=? AND group_id=? ORDER BY planned DESC LIMIT 1",
                        (adapter.account, group),
                    )
                    when = datetime.fromtimestamp(row[9], ZoneInfo(row[6])).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    return f"群：{group}\n每日 {row[4]} 禁言，{row[5]} 解禁（北京时间）\n状态：{row[8]}\n下一计划边界：{when} {row[10]}（仅 active 执行）\n最近结果：{last or '无'}\n原因：{row[11] or '无'}"
                if action in {"暂停定时", "删除定时"}:
                    (
                        await db.execute(
                            "UPDATE confirmations SET used=1 WHERE account=? AND group_id=?",
                            (adapter.account, group),
                        )
                    )
                    (
                        await db.execute(
                            "UPDATE schedules SET status=?,version=version+1,error=? WHERE account=? AND group_id=?",
                            (
                                "deleted" if action == "删除定时" else "paused",
                                "administrator_request",
                                adapter.account,
                                group,
                            ),
                        )
                    )
                    return "已停止后续调度；当前群禁言状态未改变。"
                self.authorize(adapter, owner, session, group)
                if action == "确认定时":
                    preview = await db.fetchone(
                        "SELECT * FROM confirmations WHERE token=?", (argument,)
                    )
                    if (
                        not preview
                        or preview[1:6]
                        != (adapter.account, group, owner, session, version)
                        or preview[8] <= time.time()
                        or preview[9] == str(event.message_obj.message_id)
                        or preview[10]
                        or management is not None
                        and "_open_groups" in adapter.config
                        and preview[11] != grant_version
                    ):
                        return "确认码无效、过期或已使用，请重新预览。"
                    start, end = preview[6:8]
                    planned, operation = next_boundary(start, end, time.time())
                    (
                        await db.execute(
                            "UPDATE confirmations SET used=1 WHERE token=?", (argument,)
                        )
                    )
                    (
                        await db.execute(
                            "INSERT OR REPLACE INTO schedules(account,group_id,owner,session,start,end,zone,version,status,next_at,next_action,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                adapter.account,
                                group,
                                owner,
                                session,
                                start,
                                end,
                                "Asia/Shanghai",
                                version + 1,
                                "active",
                                planned,
                                operation,
                                "",
                            ),
                        )
                    )
                    return "定时规则已启用，从下一未来边界执行；当前群状态未改变。"
                if action == "恢复定时":
                    if not row or row[8] == "deleted":
                        return "没有可恢复的规则，请先设置定时禁言。"
                    start, end = row[4:6]
                else:
                    parts = argument.split()
                    if (
                        len(parts) != 2
                        or any(
                            not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v)
                            for v in parts
                        )
                        or parts[0] == parts[1]
                    ):
                        return "用法：定时禁言 23:00 08:00；两个时间不可相同。"
                    start, end = parts
                token = secrets.token_hex(12)
                columns = "token,account,group_id,owner,session,version,start,end,expires,message,used"
                parameters = (
                    token,
                    adapter.account,
                    group,
                    owner,
                    session,
                    version,
                    start,
                    end,
                    time.time() + 600,
                    str(event.message_obj.message_id),
                    0,
                )
                if management is not None and "_open_groups" in adapter.config:
                    columns += ",grant_version"
                    parameters += (grant_version,)
                await db.execute(
                    f"INSERT INTO confirmations({columns}) VALUES({','.join('?' for _ in parameters)})",
                    parameters,
                )
                planned, operation = next_boundary(start, end, time.time())
                when = datetime.fromtimestamp(
                    planned, ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d %H:%M")
                return f"尚未启用。群：{group}\n每日北京时间 {start} 禁言，{end} 解禁\n下一动作：{when} {operation}\n不会立即改变当前状态。十分钟内在当前对话发新消息：\n确认定时 {token}"

    async def tick(self, adapter):
        """Check one adapter, recover crash boundaries and run due transitions.

        Args:
            adapter: Active native adapter whose account owns these schedules.
        """
        if not adapter.account:
            return
        now = time.time()
        identity = (id(adapter), adapter.account)
        db_guard = account_guard(adapter)
        async with AsyncDatabase(
            lambda: database(adapter), write=True, guard=db_guard
        ) as db:
            (
                await db.execute(
                    "UPDATE schedules SET status='paused',error='account_changed',version=version+1 WHERE account<>? AND status='active'",
                    (adapter.account,),
                )
            )
            if identity not in self.recovered:
                (
                    await db.execute(
                        "UPDATE schedules SET status='paused',error='restart_review',version=version+1 WHERE account=? AND status='active' AND (next_at<=? OR EXISTS(SELECT 1 FROM executions e WHERE e.account=schedules.account AND e.group_id=schedules.group_id AND e.version IN (0,schedules.version) AND e.status='claimed'))",
                        (adapter.account, now),
                    )
                )
            rows = await db.fetchall(
                "SELECT * FROM schedules WHERE account=? AND status='active'",
                (adapter.account,),
            )
        self.recovered.add(identity)
        for row in rows:
            (
                account,
                group,
                owner,
                session,
                start,
                end,
                _,
                version,
                _,
                planned,
                action,
                _,
            ) = row
            operation = (
                "schedule/"
                + hashlib.sha256(
                    f"{account}/{group}/{version}/{planned}/{action}".encode()
                ).hexdigest()
            )
            claimed = False

            def authorized():
                db_guard()
                self.authorize(adapter, owner, session, group)
                if time.time() - planned > 30:
                    raise ProtocolError("schedule_missed_boundary")
                if adapter.account != account:
                    raise ProtocolError("schedule_account_changed")

            async def check():
                nonlocal claimed
                authorized()
                management = getattr(self, "management", None)
                if management is not None and owner not in {
                    str(uid)
                    for uid in self.context.get_config(session).get("admins_id", [])
                }:
                    roster = await adapter.get_moderation_members(group)
                    authorized()
                    if not roster.get("complete") or owner not in {
                        str(m["userId"]) for m in roster.get("groupMemberInfo", [])
                    }:
                        raise ProtocolError("schedule_owner_revoked")
                async with AsyncDatabase(
                    lambda: database(adapter), write=True, guard=authorized
                ) as db:
                    current = await db.fetchone(
                        "SELECT version,status FROM schedules WHERE account=? AND group_id=?",
                        (account, group),
                    )
                    if current != (version, "active"):
                        raise ProtocolError("schedule_changed")
                    if not claimed:
                        await db.execute(
                            "INSERT INTO executions(operation,account,group_id,planned,action,status,version) VALUES(?,?,?,?,?,'claimed',?)",
                            (operation, account, group, planned, action, version),
                        )
                claimed = True
                authorized()

            try:
                if planned > now:
                    continue
                self.authorize(adapter, owner, session, group)
                if now - planned > 30:
                    raise ProtocolError("schedule_missed_boundary")

                async def execute():
                    lease_guard = account_guard(adapter)

                    async def authorize():
                        lease_guard()
                        await check()
                        lease_guard()

                    return await adapter.execute_action(
                        ActionRequest.bind(adapter, operation, action, int(group)),
                        authorize=authorize,
                    )

                governance = getattr(self, "governance", None)
                result = (
                    await governance.run("scheduled", adapter, execute, group=group)
                    if governance is not None
                    else await execute()
                )
                status = result.get("status", "unknown")
                record(
                    adapter,
                    "schedule_result",
                    status,
                    operation,
                    group=group,
                    action=action,
                    failed=status not in {"accepted", "verified"},
                    aggregate=False,
                )
                async with AsyncDatabase(
                    lambda: database(adapter), write=True, guard=db_guard
                ) as db:
                    (
                        await db.execute(
                            "UPDATE executions SET status=? WHERE operation=?",
                            (status, operation),
                        )
                    )
                if status not in {"accepted", "verified"}:
                    raise ProtocolError("schedule_unknown_result")
                point, following = next_boundary(start, end, planned)
                if point <= time.time():
                    raise ProtocolError("schedule_missed_boundary")
                async with AsyncDatabase(
                    lambda: database(adapter), write=True, guard=db_guard
                ) as db:
                    (
                        await db.execute(
                            "UPDATE schedules SET next_at=?,next_action=?,error='' WHERE account=? AND group_id=? AND version=? AND status='active'",
                            (point, following, account, group, version),
                        )
                    )
            except Exception as exc:
                async with group_lock(adapter, group):
                    async with AsyncDatabase(
                        lambda: database(adapter), write=True, guard=db_guard
                    ) as db:
                        (
                            await db.execute(
                                "UPDATE schedules SET status='paused',error=?,version=version+1 WHERE account=? AND group_id=? AND version=?",
                                (
                                    str(exc)
                                    if isinstance(exc, ProtocolError)
                                    else "execution_failed",
                                    account,
                                    group,
                                    version,
                                ),
                            )
                        )
                record(
                    adapter,
                    "schedule",
                    "paused",
                    operation,
                    group=group,
                    failed=True,
                    error="validation_failed"
                    if isinstance(exc, ProtocolError)
                    else "operation_failed",
                )

    async def poll(self):
        """Poll fixed schedules until plugin shutdown, without posting messages."""
        while True:
            for adapter in list(self.context.platform_manager.platform_insts):
                if adapter.meta().name != "wangshangliao":
                    continue
                try:
                    await self.tick(adapter)
                except Exception:
                    record(
                        adapter,
                        "schedule_scan",
                        "failed",
                        failed=True,
                        error="operation_failed",
                    )
            await asyncio.sleep(5)
