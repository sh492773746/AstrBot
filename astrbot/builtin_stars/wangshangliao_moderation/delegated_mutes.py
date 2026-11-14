"""Explicit report-member mute grants, separate from administrator authority."""

import asyncio
import hashlib
import json
import re

from astrbot.api import logger
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.blacklist import BlacklistStore
from astrbot.core.platform.sources.wangshangliao.business import BusinessClient
from astrbot.core.platform.sources.wangshangliao.directory import muted_member_ids
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.policy import (
    DELEGATED_MUTE_MAX_MINUTES,
    authorize_action,
)
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir

from .member_exemptions import is_exempt
from .open_store import OpenError
from .speech_activity import SpeechActivity
from .syntax import RETIRED_CHAT_REPLY


def read_grants(instance, account):
    """Read explicit mute-only grants intersected with the report definition.

    Args:
        instance: Saved native instance ID.
        account: Receiving robot's authenticated business UID.

    Returns:
        A group-to-member-list mapping; missing or invalid files grant nothing.
    """
    path = instance_dir(instance) / "mute_delegates.json"
    try:
        if not path.is_file():
            return {}
        if path.is_symlink() or path.stat().st_size > 16384:
            return {}
        config = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(config, dict)
            or set(config) != {"account", "groups", "members"}
            or config["account"] != account
        ):
            return {}
        for key, limit in (("groups", 20), ("members", 30)):
            items = config[key]
            if (
                not isinstance(items, list)
                or not 1 <= len(items) <= limit
                or any(
                    not isinstance(item, str)
                    or not item.isascii()
                    or not item.isdecimal()
                    or len(item) > 20
                    or not 0 < int(item) < 1 << 63
                    for item in items
                )
                or len(set(items)) != len(items)
            ):
                return {}
        report = SpeechActivity.report_config(instance, account)
        if report is None:
            return {}
        groups = {row["id"] for row in report["groups"]}
        members = {row["id"] for row in report["members"]}
        return {
            group: [uid for uid in config["members"] if uid in members]
            for group in config["groups"]
            if group in groups
        }
    except (OSError, ValueError, TypeError):
        return {}


class DelegatedMutes:
    """Authorize only explicit, scoped, real-mention member mutes."""

    def __init__(self, service):
        self.service = service
        self.grants = {}

    async def refresh(self, adapter):
        """Refresh cheap transport admission without granting mutation authority.

        Args:
            adapter: Original receiving native adapter.
        """
        self.grants[str(adapter.account)] = await asyncio.to_thread(
            read_grants, adapter.config["id"], str(adapter.account)
        )

    async def command(self, event, command):
        """Mute one proven ordinary member using a durable native operation.

        Args:
            event: Original authenticated group event, never an AI request.
            command: Canonical mute command containing one mention and minutes.

        Returns:
            Explicit acceptance, rejection or unknown-result feedback.

        Raises:
            OpenError: If scope, grant, identity or protection checks fail.
        """
        if event.is_private_chat():
            raise OpenError("private_messages_retired")
        service, adapter = self.service, event.platform
        account, group = str(adapter.account), event.get_group_id()
        actor = event.actor_identity
        grants = await asyncio.to_thread(read_grants, adapter.config["id"], account)
        if actor.claimed_uid not in grants.get(group, ()):
            return RETIRED_CHAT_REPLY
        match = re.fullmatch(
            r"禁言 (@[^\r\n]{1,256}?)\s+([0-9]{1,5})(?:分钟|分)?[。.!！]?", command
        )
        if not match:
            return "未执行：请真正@一名成员并填写禁言时长，例如「@成员 禁言128」或「@成员 禁言30分」；须为1至4320整分钟（最多3天），不填时长不会禁言。"
        if not 1 <= int(match[2]) <= DELEGATED_MUTE_MAX_MINUTES:
            return "未执行：禁言时长须为1至4320整分钟（最多3天），本次未提交禁言；请核对分钟数后重新发送。"
        minutes = int(match[2])
        tenant = service.tenants.get((account, group))
        if tenant is None:
            raise OpenError("delegated_mute_revoked")
        version = (tenant["version"], tenant["epoch"])
        frozen = service.write_guard(adapter, event)
        payload = event.get_extra("wangshangliao_payload") or {}

        def guard():
            frozen()
            current = service.tenants.get((account, group), {})
            if (
                not service.integrated
                or service.settings["paused"]
                or not current.get("active")
                or not current.get("present")
                or current.get("paused")
                or (current.get("version"), current.get("epoch")) != version
                or current.get("admission", "legacy") not in {"legacy", "verified"}
                or current.get("cleanup_epoch", 0) > current.get("cleanup_done", 0)
                or (account, group) in service.suspended_groups
                or service.membership_receiving.get((account, group))
                or actor.account != account
                or actor.claimed_uid != str(event.get_sender_id())
                or actor.verified_uid != actor.claimed_uid
                or not actor.peer
                or adapter.members.get(group, {}).get(actor.claimed_uid) != actor.peer
            ):
                raise OpenError("delegated_mute_revoked")
            authorize_action(adapter.config, group, "mute")

        guard()
        roster = await service.roster(adapter, group)
        guard()
        try:
            member, peer = await service.member_blacklist.target(event, roster)
        except OpenError as exc:
            raise OpenError(
                {
                    "blacklist_member_unavailable": "delegated_mute_departed",
                    "blacklist_identity_changed": "delegated_mute_revoked",
                    "blacklist_target_conflict": "delegated_mute_conflict",
                }.get(str(exc), "delegated_mute_target")
            ) from None
        spans = [
            span
            for span in payload.get("mention_spans", [])
            if str(span.get("uid")) == peer
        ]
        if (
            {
                str(uid)
                for uid in payload.get("mentions", [])
                if str(uid) != str(adapter.nim_account)
            }
            != {peer}
            or len(spans) != 1
            or match[1] != "@" + spans[0]["nick"]
        ):
            raise OpenError("delegated_mute_target")
        if str(member) == actor.claimed_uid:
            raise OpenError("delegated_mute_protected")
        operation = (
            f"delegate-mute/{actor.claimed_uid}/"
            + hashlib.sha256(
                f"{account}/{group}/{actor.claimed_uid}/{event.message_obj.message_id}".encode()
            ).hexdigest()
        )
        checked = False

        async def authorize(*, recalling=False):
            nonlocal checked
            # Re-read grants and native evidence even under the final HTTP lock.
            guard()
            current_grants = await asyncio.to_thread(
                read_grants, adapter.config["id"], account
            )
            if current_grants != grants:
                raise OpenError("delegated_mute_revoked")
            if (
                not recalling
                and checked
                and isinstance(getattr(adapter, "business", None), BusinessClient)
                and getattr(adapter.business, "_authorization_task", None)
                is not asyncio.current_task()
            ):
                # Intermediate action fences are local; the final HTTP fence
                # always reads native evidence again without reversing locks.
                guard()
                return
            current = (
                service.member_evidence.get((account, group), {})
                if recalling
                else await service.roster(adapter, group)
            )
            guard()
            caller = current.get(actor.claimed_uid, {})
            target = current.get(member, {})
            if (
                str(caller.get("nimId", "")) != actor.peer
                or str(target.get("nimId", "")) != peer
                or adapter.members.get(group, {}).get(member) != peer
            ):
                raise OpenError("delegated_mute_revoked")
            if (
                target.get("groupRole") != "GROUP_ROLE_MEMBER"
                or str(member) == account
                or is_managed_account(str(member))
                or current.get(account, {}).get("groupRole")
                not in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
            ):
                raise OpenError("delegated_mute_protected")
            if not recalling and member in await muted_member_ids(
                adapter, group, authorize=guard, expected_member=(member, peer)
            ):
                raise OpenError("delegated_mute_existing")
            if await asyncio.to_thread(
                is_exempt, adapter.config["id"], account, group, member, peer
            ):
                raise OpenError("delegated_mute_protected")
            _, blocked = await asyncio.to_thread(
                BlacklistStore(adapter.config["id"]).read, account, group
            )
            if any(row["member"] == member for row in blocked):
                raise OpenError("delegated_mute_protected")
            if (
                await asyncio.to_thread(read_grants, adapter.config["id"], account)
                != grants
            ):
                raise OpenError("delegated_mute_revoked")
            guard()
            checked = True

        await authorize()
        result = await adapter.execute_action(
            ActionRequest.bind(
                adapter,
                operation,
                "mute",
                int(group),
                member=int(member),
                minutes=minutes,
                expected_nim=peer,
                delegated_mute=True,
            ),
            authorize=authorize,
        )
        reply = {
            "accepted": f"禁言已受理：{minutes}分钟。",
            "verified": f"禁言已核验：{minutes}分钟。",
            "rejected": "禁言被拒绝，未执行；请联系管理员核对平台权限。",
            "unknown": "禁言结果未知，请管理员核对当前状态，不自动重试。",
        }.get(result.get("status"), "禁言结果未知，请管理员核对，不自动重试。")
        if result.get("status") not in {"accepted", "verified"}:
            return reply

        async def recall_authorize():
            guard()
            authorize_action(adapter.config, group, "recall")
            await authorize(recalling=True)

        try:
            stamp = payload.get("created_at")
            if type(stamp) not in {int, float}:
                raise OpenError("recall_time_range_invalid")
            job = await service.message_recalls.delegate(
                adapter,
                group,
                member,
                peer,
                operation,
                actor.claimed_uid,
                stamp / 1000,
                recall_authorize,
            )
        except Exception as exc:
            logger.warning("Delegated recall was not scheduled after accepted mute")
            reason = {
                "recall_job_busy": "机器人正在执行另一批撤回，请管理员在后台查看",
                "recall_selection_limit": "消息数量超过安全批量上限，请管理员在后台分批处理",
                "recall_time_range_invalid": "原命令时间信息缺失或已超时",
                "delegated_mute_protected": "成员已受保护或转入黑名单流程",
                "delegated_mute_revoked": "授权、群状态或成员身份已变化",
            }.get(str(exc), "权限或消息记录暂不可核实，请管理员在后台核对")
            return (
                reply + " 最近30分钟消息撤回未启动：" + reason + "；不影响已受理禁言。"
            )
        if job is None:
            return reply + " 最近30分钟内没有可核实的目标消息，不追查更早记录。"
        if job["state"] not in {"queued", "running"}:
            return (
                reply + f" 此命令已有撤回任务{job['id'][:8]}，"
                "请在后台核对结果；不重复启动或重试。"
            )
        return (
            reply + f" 已安排撤回该成员命令前30分钟的{job['total']}条消息"
            f"（任务{job['id'][:8]}，逐条限速处理，进度请在后台查看）。"
        )
