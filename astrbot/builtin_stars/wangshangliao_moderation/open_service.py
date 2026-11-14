"""Account-scoped public bot orchestration within the existing AstrBot process."""

import asyncio
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.directory import member_directory
from astrbot.core.platform.sources.wangshangliao.group_scope import scoped
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.invitations import (
    latest_membership_events,
)
from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
    PolicySnapshot,
    runtime,
)
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

from .ai_plans import AIPlans
from .command_menu import CommandMenu, Session
from .conversation_profiles import ConversationProfiles
from .delegated_mutes import DelegatedMutes
from .group_ai import COMMANDS as GROUP_AI_COMMANDS
from .group_ai import DEFAULT as GROUP_AI_DEFAULT
from .group_ai import MODE_COMMANDS as GROUP_AI_MODES
from .group_ai import GroupAI
from .group_management import GroupManagement
from .knowledge import REPLIES as KNOWLEDGE_REPLIES
from .knowledge import Knowledge
from .lifecycle import (
    clear_shared_group_data,
    clear_working_data,
    self_membership_notice,
)
from .member_blacklist import COMMANDS as BLACKLIST_COMMANDS
from .member_blacklist import MemberBlacklist
from .member_whitelist import MemberWhitelist
from .native_conversations import NativeConversations
from .observability import record_event
from .onboarding_delivery import send_onboarding_text
from .open_store import OpenError, OpenStore
from .pc28 import PC28Analysis
from .policy import GROUP_ADMIN_REQUIRED, handle, is_fresh_message, require_group_admin
from .raid_defense import RaidDefense
from .rule_replies import COMMANDS as BUSINESS_RULE_COMMANDS
from .rule_replies import RuleReplies
from .syntax import (
    KNOWLEDGE_COMMANDS,
    RETIRED_CHAT_ACTIONS,
    RETIRED_CHAT_REPLY,
    WHITELIST_COMMANDS,
    is_removed_group_command,
)
from .text import redact_reply
from .welcome import WelcomeMessages

ACTIVATION_REPLIES = {
    "inviter_unproven": "还没确认是谁拉我进群，所以暂时不能开通。请拉我的群主或群管理员稍等片刻，再发「开通」；仍不成功请联系机器人负责人。",
    "inviter_not_native_admin": "拉我进群的人不是群主或群管理员，暂时不能开通，我会退出这个群。请让群主或群管理员重新拉我进群。",
    "permission_denied": "只有拉我进群的群主或群管理员才能开通。请让他在群里发送「开通」。",
    "already_active_or_unknown": "这个群已开通过，或我还没获取到群信息。已开通的群请在插件管理页核对；新群请稍等片刻再发「开通」。",
    "original_manager_required": "这个群以前开通过，请让原来的机器人管理员操作。把我踢出再拉进群，不会更换管理员。",
    "activation_changed": "群里的情况刚刚有变化，这次没有开通。请稍等片刻，再发「开通」。",
    "activation_code_invalid": "开通码不正确、已过期或已使用，请向机器人负责人领取新的开通码。",
    "activation_cooldown": "请间隔3秒再申请开通，不用连续发送。",
    "tenant_limit": "机器人目前可开通的群已满，请联系机器人负责人。",
}
RULE_COMMANDS = (
    {
        "管理员列表",
        "添加管理员",
        "移除管理员",
        "确认授权",
        "开启广告审核",
        "关闭广告审核",
        "开启防刷屏",
        "关闭防刷屏",
        "添加关键词",
        "删除关键词",
        "修改关键词",
        "添加规则",
        "修改规则",
        "删除规则",
        "启用规则",
        "停用规则",
        "测试规则",
    }
    | GROUP_AI_COMMANDS
    | {"本群额度", "确认本群扣费"}
    | WHITELIST_COMMANDS
    | BLACKLIST_COMMANDS
)
OPEN_COMMANDS = RULE_COMMANDS | {
    "成员列表",
    "禁言名单",
    "成员搜索",
    "下一页",
    "能力",
    "规则",
    "违规计数",
    "结果",
    "公告",
    "解禁",
    "踢出",
    "确认踢出",
    "定时禁言",
    "确认定时",
    "定时状态",
    "暂停定时",
    "恢复定时",
    "删除定时",
}


@dataclass(frozen=True)
class ActivationContext:
    """Represent a native, roster-verified group activation without a framework event."""

    platform: object
    actor: str
    policy_epoch: object

    def get_sender_id(self):
        """Return the roster-verified business actor."""
        return self.actor

    def is_private_chat(self):
        """Keep candidate activation bound to its group transport."""
        return False


def enabled(context=None):
    """Read the embedded service mode from the effective context configuration."""
    return (
        context is not None
        and context.get_config().get("wangshangliao_open", {}).get("mode")
        == "integrated"
    )


def recognize(text):
    """Recognize only bounded whole-line public commands."""
    from .syntax import recognize_command

    if (
        not isinstance(text, str)
        or len(text) > 4096
        or any(c in text for c in ("\n", "\r", "\u2028", "\u2029", "\x00"))
    ):
        return ""
    text = text.strip()
    if text in {"开通", "取消", "0"}:
        return text
    if text.startswith(("管理 ", "/管理 ")):
        inner = text.split(" ", 1)[1].strip()
        if inner.startswith(("管理 ", "/管理 ")) or inner in {"/chat", "/exit"}:
            return ""
        text = inner
    command = recognize_command(text)
    return (
        command
        if command.split(" ", 1)[0]
        in OPEN_COMMANDS
        | RETIRED_CHAT_ACTIONS
        | KNOWLEDGE_COMMANDS
        | BUSINESS_RULE_COMMANDS
        else ""
    )


def recognize_integrated(text):
    """Retain bounded group activity and WebUI finance guidance after merging.

    Args:
        text: Actual authenticated user message.

    Returns:
        Fixed command classification, or an empty string.
    """
    command = recognize(text)
    if command:
        return command
    from .activities import ADMIN_COMMANDS, PUBLIC_COMMANDS
    from .syntax import recognize_command

    if not isinstance(text, str):
        return ""
    text = text.strip()
    if text.startswith(("管理 ", "/管理 ")):
        text = text.split(" ", 1)[1].strip()
        if text.startswith(("管理 ", "/管理 ")):
            return ""
    command = recognize_command(text)
    if command.split(" ", 1)[0] in {"结账", "结账结果", "确认已付款", "确认修改"}:
        return command
    return (
        command
        if command.split(" ", 1)[0] in PUBLIC_COMMANDS | ADMIN_COMMANDS | {"排名"}
        else ""
    )


class OpenManagement(GroupManagement):
    """Present stable public tenant grants to existing guarded command services."""

    def __init__(self, context, service):
        super().__init__(context)
        self.service = service

    async def load(self):
        """Load only the isolated tenant store, not legacy group grants."""
        await self.service.refresh()

    def key(self, adapter, group):
        """Exclude ephemeral instance IDs from public authorization identity."""
        return ("wangshangliao", str(adapter.account), str(group))

    def superadmin(self, event):
        """Keep total control in AstrBot; AI quota exemption grants no chat authority."""
        return False

    def groups(self, event):
        """Require current service state, exact account and enabled group membership."""
        if not self.service.active or self.service.settings["paused"]:
            return []
        adapter = event.platform
        config = adapter.config
        completion = getattr(event, "processing_completion", None)
        if (
            not config.get("enable", True)
            or config.get("_business_active") is False
            or event.is_private_chat()
            or (
                not isinstance(getattr(event, "actor_identity", None), ActorIdentity)
                or not event.actor_identity.peer
                or event.actor_identity.account != str(adapter.account)
                or event.actor_identity.claimed_uid != str(event.get_sender_id())
                or event.actor_identity.verified_uid != str(event.get_sender_id())
            )
            or completion is not None
            and completion.cancelled()
            or getattr(event, "policy_epoch", config.get("_business_epoch"))
            != config.get("_business_epoch")
            or event.get_extra("_api_key_allow_admin_role") is False
        ):
            return []
        groups = config.get("enabled_groups", [])
        return [
            group
            for group in groups
            if str(event.get_sender_id())
            in self.assignments.get(self.key(adapter, group), ())
            and (tenant := self.service.tenants.get((str(adapter.account), group)))
            and tenant["present"]
            and tenant["active"]
            and not tenant["paused"]
            and str(event.get_sender_id()) in tenant["managers"]
            and tenant.get("admission", "legacy") in {"legacy", "verified"}
            and (str(adapter.account), group) not in self.service.suspended_groups
            and not self.service.reconciling_groups.get((str(adapter.account), group))
            and not self.service.membership_receiving.get((str(adapter.account), group))
        ]


class OpenService:
    """Own group admission, detached policy snapshots and group workflows."""

    def __init__(self, plugin, root=None, *, integrated=False):
        self.plugin, self.context = plugin, plugin.context
        self.integrated = integrated
        self.root = Path(
            root or Path(get_astrbot_plugin_data_path()) / "wangshangliao_open"
        )
        self.store = OpenStore(self.root)
        self.knowledge = Knowledge(self)
        self.plans = AIPlans(self.store)
        self.settings = self.store.settings()
        self.quota_admins = self.store.quota_admins()
        self.manager_limits = self.store.manager_quotas()
        self.retired_groups = self.store.retired_groups()
        self.tenants = {}
        self.active = True
        self.management = OpenManagement(self.context, self)
        self.lock = asyncio.Lock()
        self.claim_attempts = {}
        self.last_discovery = {}
        self.discovery_tasks = {}
        self.seen_accounts = set()
        self.command_at = {}
        self.navigation = CommandMenu(self)
        self.member_whitelist = MemberWhitelist(self)
        self.member_blacklist = MemberBlacklist(self)
        self.delegated_mutes = DelegatedMutes(self)
        self.raid_defense = RaidDefense(self)
        self.welcome = WelcomeMessages(self)
        self.capabilities = {}
        self.candidate_tasks = set()
        self.membership_wakeup = asyncio.Event()
        self.membership_locks = {}
        self.membership_request_at = {}
        self.suspended_groups = set()
        self.suspension_tasks = {}
        self.reconciling_groups = {}
        self.membership_receiving = {}
        self.member_evidence = {}
        self.model_path = self.root / "model.json"
        self.model = (
            json.loads(self.model_path.read_text()) if self.model_path.exists() else {}
        )
        self.budget = self.store.budget()
        self.last_maintenance = 0
        self.capacity_paused = False
        self.disk_free_bytes = shutil.disk_usage(self.root).free
        self.rpc = None
        self.profiles = ConversationProfiles(self)
        self.conversations = NativeConversations(self.context)
        self.chat_count = 0
        self.group_ai = GroupAI(self)
        self.rule_replies = RuleReplies(self)
        self.pc28 = PC28Analysis(self.store)
        from .component_governance import Governance

        self.governance = Governance(self)
        from .message_recalls import MessageRecalls

        self.message_recalls = MessageRecalls(self)

    def policy_values(self, account, values):
        """Overlay group grants on native settings without changing financial bindings.

        Args:
            account: Authenticated account bound to the base policy.
            values: Detached base business configuration.

        Returns:
            Effective group rules retaining existing activity and invitation scope.
        """
        selected = [
            t
            for t in self.tenants.values()
            if t["account"] == account
            and t["present"]
            and t["active"]
            and t["group_id"] not in self.retired_groups
            and t.get("admission", "legacy") in {"legacy", "verified"}
            and (account, t["group_id"]) not in self.suspended_groups
            and not self.reconciling_groups.get((account, t["group_id"]))
            and not self.membership_receiving.get((account, t["group_id"]))
        ]
        operating = [
            t
            for t in selected
            if not t["paused"]
            and not self.settings["paused"]
            and not self.capacity_paused
        ]
        known = self.retired_groups | {
            t["group_id"] for t in self.tenants.values() if t["account"] == account
        }
        groups = list(
            dict.fromkeys(
                [g for g in values.get("enabled_groups", []) if g not in known]
                + [t["group_id"] for t in selected]
            )
        )
        policy = copy.deepcopy(values)
        policy.pop("reply_private", None)
        policy.update(
            enabled_groups=groups,
            _open_groups={t["group_id"]: t["rules"] for t in operating},
            _open_managers={t["group_id"]: t["managers"] for t in operating},
            _open_mute_delegates={
                t["group_id"]: self.delegated_mutes.grants.get(account, {}).get(
                    t["group_id"], []
                )
                for t in operating
            },
            _open_revisions={
                t[
                    "group_id"
                ]: f"{account}/{t['group_id']}/{t['version']}/{self.settings['version']}"
                for t in selected
            },
            _open_integrated=True,
            _open_native_roles={
                t["group_id"]: t.get("native_role", "") for t in selected
            },
            _open_paused=bool(
                self.settings["paused"] or not self.active or self.capacity_paused
            ),
        )
        replies = policy.setdefault("reply_groups", {})
        permissions = policy.setdefault("moderation", {}).setdefault("permissions", {})
        cards = policy["moderation"].setdefault("card_auto", {})
        targets = policy.setdefault("proactive_send", {}).setdefault("targets", [])
        operating_groups = {t["group_id"] for t in operating}
        targets[:] = [
            g
            for g in targets
            if not g.startswith("private/")
            and (g not in known or g in operating_groups)
        ]
        for group in known - operating_groups:
            permissions.pop(group, None)
            if group in self.retired_groups:
                cards.pop(group, None)
                replies.pop(group, None)
            else:
                cards[group] = False
                replies[group] = False
        for tenant in operating:
            group = tenant["group_id"]
            replies[group] = (
                values.get("reply_groups", {}).get(group, True)
                if group in values.get("enabled_groups", [])
                else True
            )
            if group not in permissions:
                permissions[group] = [
                    "mute",
                    "unmute",
                    "recall",
                    "announce",
                    "mute_all",
                    "unmute_all",
                    "kick",
                ]
            if group not in targets:
                targets.append(group)
            if tenant.get("native_role") not in {
                "GROUP_ROLE_OWNER",
                "GROUP_ROLE_ADMIN",
            }:
                permissions[group] = []
                cards[group] = False
        if operating:
            policy["proactive_send"]["enabled"] = True
            policy["moderation"].update(
                enabled=True,
                automation_enabled=True,
                recall_enabled=True,
                progressive_mute=True,
                manual_kick_only=True,
            )
        if not self.active or self.settings["paused"] or self.capacity_paused:
            policy["moderation"] = {"enabled": False}
            policy["proactive_send"] = {"enabled": False, "targets": []}
        return policy

    def adapters(self):
        """Select only this process's native public accounts."""
        return [
            a
            for a in self.context.platform_manager.platform_insts
            if a.meta().name == "wangshangliao"
        ]

    def recognize_command(self, text):
        """Classify only fixed public commands for transport-side busy admission."""
        return recognize_integrated(text) if self.integrated else recognize(text)

    async def refresh(self):
        """Publish committed tenant state without restarting a native connection."""
        async with self.lock:
            rows, self.settings = (
                await asyncio.to_thread(self.store.tenants),
                await asyncio.to_thread(self.store.settings),
            )
            self.retired_groups = await asyncio.to_thread(self.store.retired_groups)
            self.budget = await asyncio.to_thread(self.store.budget)
            self.suspended_groups.update(
                await asyncio.to_thread(self.store.membership_blocks)
            )
            self.tenants = {(t["account"], t["group_id"]): t for t in rows}
            if self.integrated:
                self.quota_admins = await asyncio.to_thread(self.store.quota_admins)
            self.manager_limits = await asyncio.to_thread(self.store.manager_quotas)
            await self.group_ai.refresh()
            self.management.assignments = {
                ("wangshangliao", t["account"], t["group_id"]): tuple(t["managers"])
                for t in rows
                if t["present"]
                and t["active"]
                and not t["paused"]
                and t.get("admission", "legacy") in {"legacy", "verified"}
                and (t["account"], t["group_id"]) not in self.suspended_groups
            }
            self.management.versions = {
                ("wangshangliao", t["account"], t["group_id"]): t["version"]
                for t in rows
            }
            for adapter in self.adapters():
                account, instance = str(adapter.account), adapter.meta().id
                await self.delegated_mutes.refresh(adapter)
                for field in (
                    "groups",
                    "group_names",
                    "group_catalog",
                    "members",
                    "member_snapshots",
                    "group_directory_state",
                ):
                    cache = getattr(adapter, field, None)
                    if isinstance(cache, dict):
                        for group in self.retired_groups:
                            cache.pop(group, None)
                if self.integrated:
                    adapter.dispatch_wakeup.set()
                    continue
                selected = [
                    t
                    for t in rows
                    if t["account"] == account
                    and t["present"]
                    and t["active"]
                    and not t["paused"]
                    and not self.settings["paused"]
                    and t.get("admission", "legacy") in {"legacy", "verified"}
                    and (account, t["group_id"]) not in self.suspended_groups
                    and not self.reconciling_groups.get((account, t["group_id"]))
                ]
                groups = [t["group_id"] for t in selected]
                values = {
                    "enabled_groups": groups,
                    "reply_groups": dict.fromkeys(groups, True),
                    "ai_routes": {},
                    "proactive_send": {"enabled": True, "targets": groups},
                    "moderation": {
                        "enabled": True,
                        "automation_enabled": True,
                        "recall_enabled": True,
                        "progressive_mute": True,
                        "manual_kick_only": True,
                        "mute_keywords": [],
                        "kick_keywords": [],
                        "cooldown_seconds": 30,
                        "permissions": {
                            g: [
                                "mute",
                                "unmute",
                                "recall",
                                "announce",
                                "mute_all",
                                "unmute_all",
                                "kick",
                            ]
                            for g in groups
                        },
                    },
                    "developer_test": {},
                    "_open_groups": {t["group_id"]: t["rules"] for t in selected},
                    "_open_managers": {t["group_id"]: t["managers"] for t in selected},
                    "_open_native_roles": {
                        t["group_id"]: t.get("native_role", "") for t in selected
                    },
                    "_open_revisions": {
                        t[
                            "group_id"
                        ]: f"{account}/{t['group_id']}/{t['version']}/{self.settings['version']}"
                        for t in selected
                    },
                }
                payload = json.dumps(values, sort_keys=True)
                revision = (
                    "open:"
                    + hashlib.sha256(
                        json.dumps(
                            [
                                payload,
                                self.settings["version"],
                                [(t["group_id"], t["version"]) for t in selected],
                            ]
                        ).encode()
                    ).hexdigest()
                )
                current = runtime.snapshots.get(instance)
                if current is None or current.revision != revision:
                    runtime.publish(
                        instance,
                        PolicySnapshot(account, revision, payload, time.time()),
                    )
                adapter.dispatch_wakeup.set()

            await self.raid_defense.refresh()

    async def commit_tenant(self, account, group, operation, *args, **kwargs):
        """Drain a tenant mutation and reconcile authority before cancellation returns.

        Args:
            account: Exact authenticated account owning the mutation.
            group: Exact business group affected by the mutation.
            operation: Synchronous, guarded tenant-store operation.
            *args: Positional arguments for the store operation.
            **kwargs: Keyword arguments, including its commit guard.

        Returns:
            The completed store operation's result.

        Raises:
            asyncio.CancelledError: After a cancelled mutation has been reconciled.
            Exception: If the store operation or authority refresh fails.
        """
        key = (str(account), str(group))
        interrupted = False
        failure = None
        result = None
        try:
            for index, work in enumerate(
                (partial(asyncio.to_thread, operation, *args, **kwargs), self.refresh)
            ):
                pending = asyncio.create_task(work())
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        if not interrupted:
                            self.reconciling_groups[key] = (
                                self.reconciling_groups.get(key, 0) + 1
                            )
                            self.management.assignments.pop(
                                ("wangshangliao", *key), None
                            )
                        interrupted = True
                    except Exception:
                        break
                try:
                    outcome = pending.result()
                except BaseException as exc:
                    if index:
                        self.suspended_groups.add(key)
                        raise
                    failure = exc
                else:
                    if not index:
                        result = outcome
        finally:
            if interrupted:
                self.reconciling_groups[key] -= 1
                if not self.reconciling_groups[key]:
                    self.reconciling_groups.pop(key)
        if interrupted:
            raise asyncio.CancelledError
        if failure is not None:
            raise failure
        return result

    async def discover(self, adapter):
        """Refresh metadata only, preserving active transport/member caches."""
        guard = self.write_guard(adapter)
        data = await self.membership_request(
            adapter, "/v1/group/get-group-list", {"v": "0"}
        )
        guard()
        if not isinstance(data, dict) or any(
            not isinstance(data.get(k), list) for k in ("owner", "member")
        ):
            raise OpenError("directory_invalid")
        catalog = {}
        for entry in data["owner"] + data["member"]:
            group, team = (
                str(entry.get("groupId", "")),
                str(entry.get("groupCloudId", "")),
            )
            if group in catalog and catalog[group]["team"] != team:
                raise OpenError("directory_conflict")
            catalog[group] = {
                "id": group,
                "team": team,
                "name": str(entry.get("groupName") or entry.get("name") or group),
                "role": (entry.get("me") or {}).get("role", ""),
            }
        account = str(adapter.account)
        notices = await asyncio.to_thread(self.store.membership_pending, account)
        for notice in notices:
            tenant = next(
                (
                    t
                    for t in self.tenants.values()
                    if t["account"] == account and t["team"] == notice["team"]
                ),
                None,
            )
            if tenant is None:
                if notice["kind"] != "join" or not any(
                    e["team"] == notice["team"] for e in catalog.values()
                ):
                    await asyncio.to_thread(
                        self.store.membership_finish,
                        account,
                        notice["team"],
                        notice["message"],
                        "unbound",
                    )
                    continue
                continue
            group = tenant["group_id"]
            if notice["event_at"] < tenant["joined"] - 120:
                await asyncio.to_thread(
                    self.store.membership_finish,
                    account,
                    notice["team"],
                    notice["message"],
                    "stale",
                )
                self.suspended_groups.discard((account, group))
                continue
            if notice["kind"] in {"kick", "leave"} and group not in catalog:
                await asyncio.to_thread(
                    self.store.depart,
                    account,
                    group,
                    tenant["epoch"],
                    notice["message"],
                )
                await asyncio.to_thread(
                    self.store.membership_finish,
                    account,
                    notice["team"],
                    notice["message"],
                    "departure_verified",
                )
            elif group in catalog:
                if notice["kind"] == "join":
                    continue
                if notice["kind"] in {"kick", "leave"}:
                    # A delayed departure must not erase a subsequent membership.
                    history = await self.membership_request(
                        adapter, "/v1/group/get-apply-logs", {}
                    )
                    current = latest_membership_events(history, group).get(account)
                    if (
                        not current
                        or current["ambiguous"]
                        or current["timestamp"] <= notice["event_at"]
                    ):
                        continue
                if notice["kind"] in {"role_lost", "role_gained"}:
                    role = catalog[group]["role"]
                    if role not in {
                        "GROUP_ROLE_OWNER",
                        "GROUP_ROLE_ADMIN",
                        "GROUP_ROLE_MEMBER",
                    }:
                        rows = await self.roster(adapter, group, paced=True)
                        role = rows[account].get("groupRole")
                        if role not in {
                            "GROUP_ROLE_OWNER",
                            "GROUP_ROLE_ADMIN",
                            "GROUP_ROLE_MEMBER",
                        }:
                            continue
                    guard()
                    await asyncio.to_thread(
                        self.store.native_role, account, group, tenant["epoch"], role
                    )
                    await self.suspend_group(adapter, group)
                    catalog[group]["role"] = role
                await asyncio.to_thread(
                    self.store.membership_finish,
                    account,
                    notice["team"],
                    notice["message"],
                    "membership_verified",
                )
                self.suspended_groups.discard((account, group))
        await self.refresh()
        reload_policy = False
        for tenant in list(self.tenants.values()):
            if tenant["account"] != account:
                continue
            group = tenant["group_id"]
            if (
                group not in catalog
                and tenant["leave_state"] == "accepted"
                and tenant["admission"] == "rejected"
            ):
                await asyncio.to_thread(
                    self.store.depart,
                    account,
                    group,
                    tenant["epoch"],
                    f"automatic_leave/{tenant['epoch']}",
                )
                await self.refresh()
                tenant = self.tenants[(account, group)]
            if tenant["cleanup_epoch"] > tenant["cleanup_done"]:
                cleanup_epoch = tenant["cleanup_epoch"]

                def cleanup_guard():
                    guard()
                    current = self.store.tenant(account, group)
                    if (
                        not current
                        or current["present"]
                        or current["epoch"] != cleanup_epoch
                    ):
                        raise OpenError("membership_changed")

                await self.suspend_group(adapter, group)
                member_lock = getattr(adapter, "member_refresh_locks", {}).get(group)
                if member_lock is not None:
                    await member_lock.acquire()
                try:
                    cleanup_guard()
                    for field in (
                        "groups",
                        "group_names",
                        "group_catalog",
                        "members",
                        "member_snapshots",
                        "group_directory_state",
                        "member_refresh_at",
                    ):
                        cache = getattr(adapter, field, None)
                        if isinstance(cache, dict):
                            cache.pop(group, None)
                    self.capabilities.pop((account, group), None)
                    selections = self.plugin.commands.selections
                    for key, selection in list(selections.items()):
                        if (
                            len(key) == 4
                            and key[:2] == (id(adapter), account)
                            and (
                                selection.get("group") == group
                                or group in selection.get("groups", [])
                            )
                        ):
                            selections.pop(key, None)
                finally:
                    if member_lock is not None:
                        member_lock.release()
                await asyncio.to_thread(
                    clear_working_data,
                    instance_dir(adapter.meta().id),
                    account,
                    group,
                    tenant["team"],
                    cleanup_guard,
                )
                await asyncio.to_thread(
                    clear_shared_group_data,
                    Path(get_astrbot_data_path()),
                    account,
                    group,
                    cleanup_guard,
                    platforms=self.context.get_config().get("platform", []),
                )
                await asyncio.to_thread(
                    self.knowledge.store.clear_group,
                    account,
                    group,
                    guard=cleanup_guard,
                )
                reload_policy = True
                await asyncio.to_thread(
                    self.store.cleanup_finished, account, group, cleanup_epoch
                )
                self.suspended_groups.discard((account, group))
        catalog = await asyncio.to_thread(
            self.store.discover,
            str(adapter.account),
            catalog,
            allow_auto=str(adapter.account) in self.seen_accounts,
            guard=guard,
        )
        for notice in notices:
            if notice["kind"] == "join" and any(
                entry["team"] == notice["team"] for entry in catalog.values()
            ):
                await asyncio.to_thread(
                    self.store.membership_finish,
                    account,
                    notice["team"],
                    notice["message"],
                    "directory_verified",
                )
        self.seen_accounts.add(str(adapter.account))
        guard()
        await self.refresh()
        for group, entry in catalog.items():
            tenant = self.tenants.get((account, group))
            if not tenant or not tenant["present"]:
                continue
            if entry["role"] in {
                "GROUP_ROLE_OWNER",
                "GROUP_ROLE_ADMIN",
                "GROUP_ROLE_MEMBER",
            }:
                changed = await asyncio.to_thread(
                    self.store.native_role,
                    account,
                    group,
                    tenant["epoch"],
                    entry["role"],
                )
                if changed or (
                    entry["role"] == "GROUP_ROLE_MEMBER"
                    and tenant.get("native_role") != entry["role"]
                    and tenant["active"]
                ):
                    await self.suspend_group(adapter, group, retain=True)
                elif tenant.get("native_role") != entry["role"]:
                    await self.refresh()
                schedules = getattr(self.plugin, "schedules", None)
                if schedules is not None and hasattr(schedules, "native_permission"):
                    await schedules.native_permission(
                        adapter, group, entry["role"] != "GROUP_ROLE_MEMBER"
                    )
            if tenant["admission"] == "pending" and tenant["auto_claim"]:
                try:
                    await self.check_inviter(adapter, group)
                except OpenError as exc:
                    if str(exc) not in {"inviter_unproven", "inviter_not_native_admin"}:
                        raise
        adapter.verified_nickname = await asyncio.to_thread(
            self.store.label, str(adapter.account), str(adapter.nim_account)
        )
        adapter.group_catalog = catalog
        for group in list(adapter.groups):
            if group not in catalog:
                adapter.groups.pop(group, None)
                adapter.members.pop(group, None)
        await self.refresh()
        if self.integrated:
            baseline = runtime.snapshots.get(adapter.meta().id)
            if baseline and baseline.account == str(adapter.account):
                values = baseline.values()
                legacy = self.plugin.legacy_management
                for group in values.get("enabled_groups", []):
                    tenant = self.tenants.get((str(adapter.account), group))
                    if (
                        not tenant
                        or tenant["active"]
                        or not tenant["present"]
                        or tenant["cleanup_epoch"]
                    ):
                        continue
                    roster = await self.roster(adapter, group)
                    guard()
                    policy = values.get("moderation", {})
                    words = list(
                        dict.fromkeys(
                            policy.get("mute_keywords", [])
                            + policy.get("kick_keywords", [])
                        )
                    )
                    if len(words) > 50:
                        raise OpenError("legacy_keyword_limit")
                    await asyncio.to_thread(
                        self.store.import_group,
                        str(adapter.account),
                        group,
                        {
                            "advertising": bool(
                                policy.get("enabled")
                                and policy.get("automation_enabled")
                            ),
                            "flood": bool(
                                policy.get("enabled")
                                and policy.get("automation_enabled")
                            ),
                            "keywords": words,
                        },
                        [
                            uid
                            for uid in legacy.assignments.get(
                                legacy.key(adapter, group), ()
                            )
                            if uid in roster and uid != str(adapter.account)
                        ],
                        "system/merge",
                        guard=self.write_guard(adapter),
                    )
                await self.refresh()
        for group in adapter.enabled_groups:
            entry = catalog.get(group)
            if not entry:
                continue
            adapter.groups[group] = entry["team"]
            adapter.group_names[group] = entry["name"]
            if group not in adapter.members:
                await adapter.refresh_member_mapping(group)
        if not adapter.verified_nickname and catalog:
            await self.roster(adapter, next(iter(catalog)))
        if reload_policy:
            guard()
            settings = getattr(self.plugin, "business_settings", None)
            if settings is not None:
                await asyncio.to_thread(settings.load, adapter._transport_config)
            legacy = getattr(self.plugin, "legacy_management", None)
            if legacy is not None:
                await legacy.load()

    async def membership_request(self, adapter, route, params):
        """Pace automatic membership IO without adding a generic operation proxy.

        Args:
            adapter: Authenticated native account adapter.
            route: One of the four fixed membership read or self-exit routes.
            params: Internally constructed operation parameters.

        Returns:
            The signed business response.
        """
        if route not in {
            "/v1/group/get-group-list",
            "/v1/group/get-apply-logs",
            "/v1/group/get-group-members",
            "/v1/group/leave-group",
        }:
            raise OpenError("membership_route_denied")
        account = str(adapter.account)
        lock = self.membership_locks.setdefault(account, asyncio.Lock())
        guard = self.write_guard(adapter)
        tenant = None
        if route == "/v1/group/leave-group":
            if set(params) != {"groupId"} or type(params["groupId"]) is not int:
                raise OpenError("membership_exit_denied")
            tenant = self.tenants.get((account, str(params["groupId"])))
            if (
                not tenant
                or tenant["admission"] != "rejected"
                or tenant["leave_state"] != "unknown"
            ):
                raise OpenError("membership_exit_denied")

        def authorize():
            guard()
            if tenant is not None:
                current = self.store.tenant(account, tenant["group_id"])
                if not current or (
                    current["epoch"],
                    current["version"],
                    current["admission"],
                    current["leave_state"],
                ) != (tenant["epoch"], tenant["version"], "rejected", "unknown"):
                    raise OpenError("membership_changed")

        async with lock:
            await asyncio.sleep(
                max(
                    0, self.membership_request_at.get(account, 0) + 3 - time.monotonic()
                )
            )
            authorize()
            self.membership_request_at[account] = time.monotonic()
            return await adapter.business.request(route, params, authorize=authorize)

    async def suspend_group(self, adapter, group, *, retain=False):
        """Cancel exact-group pending work without cancelling other groups or accounts.

        Args:
            adapter: The affected authenticated robot.
            group: Exact business group.
            retain: Preserve message bodies for a native-role change.
        """
        account = str(adapter.account)
        caller = asyncio.current_task()
        key = (account, group)
        state = self.suspension_tasks.get(key)
        if state is None or state["task"].done() or state.get("releasing"):
            state = {
                "callers": set(),
                "changed": asyncio.Event(),
                "retain": retain,
            }
            self.suspension_tasks[key] = state
        state["callers"].add(caller)
        state["retain"] = state["retain"] and retain
        state["changed"].set()
        self.suspended_groups.add(key)

        async def reconcile():
            await self.refresh()
            tasks = []
            for lane, task in getattr(adapter, "workers", {}).items():
                if lane[0] == group and not task.done():
                    task.cancel()
                    tasks.append(task)
            for event in tuple(getattr(adapter, "events", ())):
                if str(event.message_obj.group_id) == group:
                    event.processing_completion.cancel()
                    task = getattr(event, "processing_task", None)
                    if isinstance(task, asyncio.Task):
                        task.cancel()
                        tasks.append(task)
            flood = getattr(self.plugin, "flood_runtime", None)
            if flood is not None:
                for lane in list(flood.pending):
                    if lane[1:3] == (account, group):
                        flood.pending.pop(lane, None)
                for lane, task in flood.workers.items():
                    if lane[1:3] == (account, group) and not task.done():
                        task.cancel()
                        tasks.append(task)
            cards = getattr(self.plugin, "cards", None)
            media = getattr(self.plugin, "media_moderation", None)
            for task in tuple(getattr(media, "tasks", ())):
                if (
                    task.get_name().startswith(f"wsl-media/{account}/{group}/")
                    and not task.done()
                ):
                    task.cancel()
                    tasks.append(task)
            for task in tuple(getattr(cards, "tasks", ())):
                if (
                    task.get_name().startswith(f"wsl-card/{account}/{group}/")
                    and not task.done()
                ):
                    task.cancel()
                    tasks.append(task)
            # New callers may join while a cancelled event drains its own work.
            tasks = set(tasks)
            while tasks:
                state["changed"].clear()
                tasks = {
                    task
                    for task in tasks
                    if not task.done() and task not in state["callers"]
                }
                if not tasks:
                    break
                changed = asyncio.create_task(state["changed"].wait())
                try:
                    await asyncio.wait(
                        tasks | {changed}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
            ledger = getattr(adapter, "ledger", None)
            if ledger is not None and ledger.db is not None:
                await ledger.db.execute(
                    "UPDATE inbox SET state='membership_revoked',completed_at=? "
                    "WHERE account=? AND team=? AND state IN ('pending','processing')",
                    (time.time(), account, group),
                )
                await ledger.db.commit()
            windows = getattr(
                getattr(self.plugin, "business_settings", None), "flood_windows", None
            )
            for window_key in list(getattr(windows, "windows", {})):
                if window_key[:2] == (account, group):
                    windows.windows.pop(window_key, None)
            if state["retain"]:
                state["releasing"] = True
                self.suspended_groups.discard(key)
                await self.refresh()

        if "task" not in state:
            state["task"] = asyncio.create_task(reconcile())
        reconciling = state["task"]
        interrupted = False
        try:
            while not reconciling.done():
                try:
                    await asyncio.shield(reconciling)
                except asyncio.CancelledError:
                    interrupted = True
            reconciling.result()
        finally:
            state["callers"].discard(caller)
            if (
                not state["callers"]
                and reconciling.done()
                and self.suspension_tasks.get(key) is state
            ):
                self.suspension_tasks.pop(key)
        if interrupted:
            raise asyncio.CancelledError

    async def receive_membership(self, adapter, outer):
        """Persist self-membership metadata without doing network IO in the ACK lane.

        Args:
            adapter: Authenticated transport account.
            outer: Native upstream envelope, not decoded user text.

        Returns:
            Whether this was a recognized self-membership notice.
        """
        notice = self_membership_notice(outer, str(adapter.nim_account))
        if notice is None:
            return False
        account = str(adapter.account)
        tenant = next(
            (
                t
                for t in self.tenants.values()
                if t["account"] == account and t["team"] == notice["team"]
            ),
            None,
        )
        key = (
            (account, tenant["group_id"])
            if tenant
            and notice["kind"] in {"kick", "leave", "role_lost"}
            and notice["event_at"] >= tenant["joined"] - 120
            else None
        )
        # Fence before the worker waits for SQLite: the current writer may be
        # precisely the manager confirmation this notice must revoke.
        if key is not None:
            self.membership_receiving[key] = self.membership_receiving.get(key, 0) + 1
        try:
            fresh = await asyncio.to_thread(
                self.store.membership_notice, account, **notice
            )
            if fresh:
                if key is not None:
                    self.suspended_groups.add(key)
                self.membership_wakeup.set()
                adapter.dispatch_wakeup.set()
        except BaseException:
            if key is not None:
                self.suspended_groups.add(key)
            raise
        finally:
            if key is not None:
                self.membership_receiving[key] -= 1
                if not self.membership_receiving[key]:
                    self.membership_receiving.pop(key, None)
        return True

    async def poll(self):
        """Bound discovery to once per minute per account, never replaying actions."""
        while self.active:
            for account, task in list(self.discovery_tasks.items()):
                if task.done():
                    if not task.cancelled():
                        task.exception()
                    del self.discovery_tasks[account]
            try:
                self.disk_free_bytes = shutil.disk_usage(self.root).free
                self.capacity_paused = self.disk_free_bytes < 512 * 1024**2
                if (
                    not self.capacity_paused
                    and time.time() - self.last_maintenance >= 60
                ):
                    await asyncio.to_thread(self.store.prune)
                    await asyncio.to_thread(self.plans.prune)
                    await self.raid_defense.maintain()
                    try:
                        await asyncio.to_thread(self.rule_replies.media.cleanup)
                    except (ValueError, TypeError, KeyError):
                        # Invalid reference data must retain assets, not stop
                        # scheduling or expose saved rule contents in logs.
                        for adapter in self.adapters():
                            adapter.diagnostics.emit(
                                "rule_media_cleanup", "reference_invalid", failed=True
                            )
                    self.last_maintenance = time.time()
            except (OSError, sqlite3.Error):
                self.capacity_paused = True
            if self.capacity_paused:
                for adapter in self.adapters():
                    adapter.diagnostics.emit(
                        "open_capacity", "automatic_pause", failed=True
                    )
                await asyncio.sleep(3)
                continue
            for adapter in self.adapters():
                account = str(adapter.account)
                urgent = bool(
                    await asyncio.to_thread(self.store.membership_pending, account)
                ) or any(
                    t["account"] == account
                    and (
                        t["cleanup_epoch"] > t["cleanup_done"]
                        or t["admission"] == "rejected"
                        and t["leave_state"] == "accepted"
                    )
                    for t in self.tenants.values()
                )
                if (
                    adapter.connection_state != "online"
                    or account in self.discovery_tasks
                    or time.monotonic() - self.last_discovery.get(account, -60)
                    < (3 if urgent or self.membership_wakeup.is_set() else 60)
                ):
                    continue
                self.last_discovery[account] = time.monotonic()

                async def refresh(current=adapter):
                    try:
                        await self.discover(current)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        current.diagnostics.emit(
                            "open_discovery",
                            "unavailable",
                            failed=True,
                            error=str(exc)
                            if isinstance(exc, ProtocolError)
                            else "other",
                            provider_code=getattr(exc, "number", None),
                        )

                self.discovery_tasks[account] = asyncio.create_task(
                    refresh(), name=f"wsl-discovery/{account}"
                )
            self.membership_wakeup.clear()
            self.group_ai.tick()
            self.member_blacklist.tick()
            self.raid_defense.tick()
            self.welcome.tick()
            self.rule_replies.tick()
            try:
                await asyncio.wait_for(self.membership_wakeup.wait(), 3)
            except asyncio.TimeoutError:
                pass

    async def roster(self, adapter, group, *, paced=False):
        """Read complete membership, retrying one transient read rejection only.

        Args:
            adapter: Original authenticated account and policy generation.
            group: Exact business group identity.
            paced: Use the existing automatic-membership request pacing.

        Returns:
            Complete current membership, never a cached fallback.

        Raises:
            OpenError: If membership is unavailable or the service was revoked.
            ProtocolError: On invalid identity, authentication or transport errors.
        """
        account = str(adapter.account)
        tenant = self.tenants.get((account, group))
        if not tenant or not tenant["present"] or adapter.connection_state != "online":
            raise OpenError("group_unavailable")
        guard = account_guard(adapter)
        business = adapter.business
        client = (
            SimpleNamespace(request=partial(self.membership_request, adapter))
            if paced
            else business
        )
        shared_refresh = getattr(adapter, "refresh_member_mapping", None)
        # Final authorization owns the HTTP lock; a waiting reader may own the
        # refresh lock. Keep its fresh read reentrant instead of reversing locks.
        use_shared = (
            callable(shared_refresh)
            and not paced
            and getattr(business, "_authorization_task", None)
            is not asyncio.current_task()
        )
        for attempt in range(2):
            guard()
            if (
                not self.active
                or adapter.business is not business
                or adapter.connection_state != "online"
            ):
                raise OpenError("operation_revoked")
            try:
                result = (
                    await shared_refresh(group)
                    if use_shared
                    else await member_directory(client, group)
                )
            except ProtocolError as exc:
                diagnostic = getattr(adapter, "diagnostics", None)
                if diagnostic is not None:
                    diagnostic.emit(
                        "member_directory",
                        "rejected",
                        group=group,
                        error=str(exc),
                        provider_code=getattr(exc, "number", None),
                        failed=True,
                        aggregate=True,
                    )
                if (
                    not attempt
                    and not use_shared
                    and isinstance(exc, BusinessError)
                    and str(exc) == "business_rejected"
                    and exc.number == -1
                ):
                    # Retry only this read, before publishing any identity evidence.
                    await asyncio.sleep(1)
                    continue
                if str(exc) == "business_rejected":
                    raise OpenError("member_directory_rejected") from None
                raise
            break
        guard()
        rows = {str(m["userId"]): m for m in result["groupMemberInfo"]}
        if (
            not result["complete"]
            or account not in rows
            or str(rows[account].get("nimId")) != str(adapter.nim_account)
        ):
            raise OpenError("membership_unproven")
        await asyncio.to_thread(self.store.remember_roster, account, rows, guard=guard)
        own = rows[account]
        if str(own.get("nimId")) == str(adapter.nim_account):
            nickname = own.get("userNick")
            if isinstance(nickname, str) and nickname.strip():
                await asyncio.to_thread(
                    self.store.save_label, account, str(adapter.nim_account), nickname
                )
                adapter.verified_nickname = nickname
        role = rows[account].get("groupRole")
        if role in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN", "GROUP_ROLE_MEMBER"}:
            changed = await asyncio.to_thread(
                self.store.native_role, account, group, tenant["epoch"], role
            )
            if changed or (
                role == "GROUP_ROLE_MEMBER"
                and tenant.get("native_role") != role
                and tenant["active"]
            ):
                await self.suspend_group(adapter, group, retain=True)
            elif tenant.get("native_role") != role:
                await self.refresh()
        self.capabilities[(account, group)] = {
            "state": (
                "available"
                if role in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
                else "missing"
                if role == "GROUP_ROLE_MEMBER"
                else "unknown"
            ),
            "epoch": tenant["epoch"],
            "checked_at": time.time(),
        }
        self.member_evidence[(account, group)] = rows
        return rows

    def write_guard(
        self, adapter, event=None, *, group="", tenant=None, members=None, control=False
    ):
        """Recheck caller and optional exact tenant authority before committing.

        Args:
            adapter: Authenticated account owning the write.
            event: Native caller, if the operation originated in chat.
            group: Exact business group for a management mutation.
            tenant: Pre-mutation tenant version and membership epoch.
            members: Fresh complete roster used to authorize the mutation.
            control: Total-control manager mutation, not chat manager authority.

        Returns:
            Synchronous transaction admission and commit guard.
        """
        frozen = account_guard(adapter)
        caller = asyncio.current_task()
        key = (str(adapter.account), group)
        version = (tenant["version"], tenant["epoch"]) if tenant else None
        scope = (tenant["account"], tenant["group_id"]) if tenant else None
        peers = (
            {uid: str(member["nimId"]) for uid, member in members.items()}
            if members is not None
            else {}
        )
        cached_members = dict(getattr(adapter, "members", {}).get(group, {}))

        def check():
            if event is not None and event.is_private_chat():
                raise OpenError("private_messages_retired")
            frozen()
            if self.capacity_paused:
                raise OpenError("service_paused")
            if not self.active or caller is not None and caller.cancelling():
                raise OpenError("operation_revoked")
            if event is not None:
                completion = getattr(event, "processing_completion", None)
                if (
                    event.is_private_chat()
                    and not getattr(event, "eligible", True)
                    or completion is not None
                    and completion.cancelled()
                    or getattr(
                        event, "policy_epoch", adapter.config.get("_business_epoch")
                    )
                    != adapter.config.get("_business_epoch")
                ):
                    raise OpenError("operation_revoked")
            if group:
                current = self.tenants.get(key)
                if (
                    not control
                    and self.settings["paused"]
                    or not current
                    or scope != key
                    or (current["version"], current["epoch"]) != version
                    or not current["present"]
                    or not current["active"]
                    or not control
                    and current["paused"]
                    or current.get("admission", "legacy") not in {"legacy", "verified"}
                    or current["cleanup_epoch"] > current["cleanup_done"]
                    or current["native_role"]
                    not in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
                    or key in self.suspended_groups
                    or self.membership_receiving.get(key)
                    or not control
                    and (event is None or not self.management.allowed(event, group))
                ):
                    raise OpenError("operation_revoked")
                require_group_admin(adapter.config, group)
                latest = self.member_evidence.get(key, {})
                actor = event.actor_identity if event is not None else None
                if (
                    not control
                    and (
                        actor is None
                        or actor.claimed_uid not in peers
                        or peers[actor.claimed_uid] != actor.peer
                    )
                    or peers.get(key[0]) != str(adapter.nim_account)
                    or latest.get(key[0], {}).get("groupRole")
                    not in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
                    or any(
                        str(latest.get(uid, {}).get("nimId", "")) != peer
                        for uid, peer in peers.items()
                    )
                    or getattr(adapter, "members", {}).get(group, {}) != cached_members
                    and any(
                        getattr(adapter, "members", {}).get(group, {}).get(uid) != peer
                        for uid, peer in peers.items()
                    )
                ):
                    raise OpenError("membership_changed")

        return check

    async def prove(self, adapter, group, *, paced=False):
        """Match the bot itself to a fresh successful business invitation record."""
        tenant = self.tenants[(str(adapter.account), group)]
        if not tenant["auto_claim"]:
            raise OpenError("inviter_unproven")
        data = (
            await self.membership_request(adapter, "/v1/group/get-apply-logs", {})
            if paced
            else await adapter.business.request("/v1/group/get-apply-logs", {})
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("list"), list)
            or len(data["list"]) > 4096
        ):
            raise OpenError("invitation_invalid")
        try:
            latest = latest_membership_events(data, group).get(str(adapter.account))
        except ProtocolError:
            raise OpenError("inviter_unproven") from None
        if not latest or latest["ambiguous"]:
            raise OpenError("inviter_unproven")
        row = latest["row"]
        inviter = row.get("inviter") or {}
        uid, proof = str(inviter.get("userId", "")), str(row.get("applyId", ""))
        if (
            row.get("groupApplyType") != "INVITATION"
            or row.get("state") != "MEMBER_STATE_GOOD"
            or str(row["applicant"].get("nimId", "")) != str(adapter.nim_account)
            or not uid.isascii()
            or not uid.isdigit()
            or uid == str(adapter.account)
        ):
            raise OpenError("inviter_unproven")
        await asyncio.to_thread(
            self.store.prove_inviter,
            str(adapter.account),
            group,
            uid,
            proof,
            tenant["epoch"],
            latest["timestamp"],
        )
        await self.refresh()

    async def check_inviter(self, adapter, group):
        """Admit only a real native administrator inviter, otherwise exit once.

        Args:
            adapter: Authenticated robot account.
            group: Current exact business group.

        Raises:
            OpenError: If invitation, native role or membership generation is unproven.
        """
        guard = self.write_guard(adapter)
        before = self.tenants[(str(adapter.account), group)]
        if before["admission"] == "rejected":
            raise OpenError("inviter_not_native_admin")
        await self.prove(adapter, group, paced=True)
        guard()
        tenant = self.tenants[(str(adapter.account), group)]
        roster = await self.roster(adapter, group, paced=True)
        guard()
        current = await asyncio.to_thread(
            self.store.tenant, str(adapter.account), group
        )
        if not current or (current["epoch"], current["version"]) != (
            before["epoch"],
            before["version"],
        ):
            raise OpenError("membership_changed")
        inviter = roster.get(tenant["inviter"])
        if inviter is None or inviter.get("groupRole") not in {
            "GROUP_ROLE_OWNER",
            "GROUP_ROLE_ADMIN",
            "GROUP_ROLE_MEMBER",
        }:
            raise OpenError("inviter_unproven")
        allowed = inviter["groupRole"] in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
        submit_exit = await asyncio.to_thread(
            self.store.admit_inviter,
            str(adapter.account),
            group,
            before["epoch"],
            before["version"],
            tenant["inviter"],
            tenant["evidence"],
            allowed,
            guard=guard,
        )
        await self.refresh()
        if allowed:
            if before["admission"] == "pending" and not before["active"]:
                self.queue_group_guide(
                    adapter, group, tenant["inviter"], activated=False
                )
            return
        await self.suspend_group(adapter, group)
        if submit_exit:
            try:
                # The durable unknown reservation prevents retries after interruption.
                await self.membership_request(
                    adapter, "/v1/group/leave-group", {"groupId": int(group)}
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from astrbot.core.platform.sources.wangshangliao.business import (
                    BusinessError,
                )

                state = (
                    "rejected"
                    if isinstance(exc, BusinessError)
                    and str(exc) == "business_rejected"
                    else "unknown"
                )
                await asyncio.to_thread(
                    self.store.leave_result,
                    str(adapter.account),
                    group,
                    before["epoch"],
                    state,
                )
            else:
                await asyncio.to_thread(
                    self.store.leave_result,
                    str(adapter.account),
                    group,
                    before["epoch"],
                    "accepted",
                )
            self.membership_wakeup.set()
        raise OpenError("inviter_not_native_admin")

    async def activate(self, event, group, code=""):
        """Require fresh account membership both before and after invitation lookup."""
        if event.is_private_chat():
            raise OpenError("private_messages_retired")
        adapter, actor = event.platform, str(event.get_sender_id())
        guard = self.write_guard(adapter, event)
        tenant = self.tenants.get((str(adapter.account), group))
        if not tenant or tenant["active"]:
            raise OpenError("already_active_or_unknown")
        roster = await self.roster(adapter, group)
        guard()
        if (
            actor not in roster
            or actor == str(adapter.account)
            or roster[actor].get("groupRole")
            not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
        ):
            raise OpenError("permission_denied")
        if tenant["auto_claim"]:
            await self.check_inviter(adapter, group)
        elif not code or tenant["admission"] != "verified":
            raise OpenError("inviter_unproven")
        roster = await self.roster(adapter, group)
        guard()
        if (
            actor not in roster
            or roster[actor].get("groupRole")
            not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
            or (
                event.is_private_chat()
                and str(roster[actor]["nimId"]) != event.actor_identity.peer
            )
        ):
            raise OpenError("permission_denied")
        current = self.tenants.get((str(adapter.account), group))
        if (
            not current
            or current["epoch"] != tenant["epoch"]
            or current["version"] != tenant["version"]
        ):
            raise OpenError("activation_changed")
        if roster.get(current["inviter"], {}).get("groupRole") not in {
            "GROUP_ROLE_OWNER",
            "GROUP_ROLE_ADMIN",
        }:
            await self.check_inviter(adapter, group)
            raise OpenError("inviter_unproven")
        key = (str(adapter.account), group)
        inviter = current["inviter"]
        actor_peer, inviter_peer = (
            str(roster[uid]["nimId"]) for uid in (actor, inviter)
        )
        identity = getattr(event, "actor_identity", None)
        if isinstance(identity, ActorIdentity) and (
            identity.account != key[0]
            or identity.claimed_uid != actor
            or identity.peer != actor_peer
        ):
            raise OpenError("permission_denied")
        self.member_evidence[key] = roster

        def activation_guard():
            guard()
            current = self.tenants.get(key)
            members = self.member_evidence.get(key, {})
            if (
                self.settings["paused"]
                or not current
                or not current["present"]
                or current["active"]
                or current["paused"]
                or (current["epoch"], current["version"])
                != (tenant["epoch"], tenant["version"])
                or current["team"] != tenant["team"]
                or current["inviter"] != inviter
                or current["cleanup_epoch"] > current["cleanup_done"]
                or current["admission"] in {"rejected", "departed"}
                or key in self.suspended_groups
                or self.membership_receiving.get(key)
                or str(members.get(key[0], {}).get("nimId")) != str(adapter.nim_account)
                or any(
                    str(members.get(uid, {}).get("nimId")) != peer
                    or members.get(uid, {}).get("groupRole")
                    not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
                    for uid, peer in ((actor, actor_peer), (inviter, inviter_peer))
                )
            ):
                raise OpenError("activation_changed")

        await self.commit_tenant(
            str(adapter.account),
            group,
            self.store.activate,
            str(adapter.account),
            group,
            actor,
            code=code,
            epoch=tenant["epoch"],
            version=tenant["version"],
            guard=activation_guard,
        )
        adapter.groups[group] = tenant["team"]
        adapter.group_names[group] = tenant["name"]
        adapter.members[group] = {
            uid: str(member["nimId"]) for uid, member in roster.items()
        }
        self.queue_group_guide(adapter, group, actor, activated=True)
        if roster[str(adapter.account)].get("groupRole") not in {
            "GROUP_ROLE_OWNER",
            "GROUP_ROLE_ADMIN",
        }:
            return (
                "本群已开通！\n"
                + GROUP_ADMIN_REQUIRED
                + "\n设为管理员后，不用加好友，直接在群里发送「菜单」即可使用。"
            )
        return "本群已开通，我已是本群管理员，广告拦截和防刷屏已开启。\n不用加好友，直接在群里发送「菜单」即可使用。"

    def queue_group_guide(self, adapter, group, actor, *, activated):
        """Keep legacy admission callbacks inert after guide delivery retirement.

        Args:
            adapter: Current native transport.
            group: Exact business group with verified current membership.
            actor: Verified native administrator inviter or activation claimant.
            activated: Whether this is the post-activation fallback.
        """
        return

    async def candidate(self, adapter, group, outer, inner):
        """Handle only fresh activation text from a transport-authenticated candidate."""
        if (
            not self.active
            or self.settings["paused"]
            or self.capacity_paused
            or str(inner.sender.id) == str(adapter.account)
            or not inner.HasField("content")
            or inner.content.data.strip() != "开通"
        ):
            return
        key = ("candidate", str(adapter.account), group, outer.get(2, ""))
        now = time.monotonic()
        self.claim_attempts = {
            k: v for k, v in self.claim_attempts.items() if now - v < 600
        }
        if (
            len(self.claim_attempts) >= 4096
            or now - self.claim_attempts.get(key, -60) < 3
        ):
            return
        self.claim_attempts[key] = now
        # Candidate events never enter the framework, AI, inbox or ordinary rule paths.
        payload = {"recall_route": {"time": outer.get(7, "")}}
        if not is_fresh_message(payload, time.time()):
            return
        guard = self.write_guard(adapter)
        if (str(adapter.account), group) not in self.tenants:
            await self.discover(adapter)
            guard()
        if adapter.group_catalog.get(group, {}).get("team") != outer.get(1):
            raise OpenError("group_transport_unproven")
        roster = await self.roster(adapter, group)
        guard()
        actor = str(inner.sender.id)
        if actor not in roster or str(roster[actor].get("nimId")) != outer.get(2):
            raise OpenError("membership_unproven")
        tenant = self.tenants.get((str(adapter.account), group))
        if not tenant or not tenant["present"] or tenant["paused"]:
            return
        epoch = tenant["epoch"]
        transport = (
            str(adapter.account),
            adapter.business,
            getattr(adapter, "nim", None),
            adapter.nim_account,
        )
        event = ActivationContext(adapter, actor, adapter.config.get("_business_epoch"))
        try:
            reply = await self.activate(event, group)
        except (OpenError, ProtocolError) as exc:
            reply = ACTIVATION_REPLIES.get(
                str(exc), "这次还没开通，请稍等片刻再试；仍不成功请联系机器人负责人。"
            )
            current = self.tenants.get((str(adapter.account), group), {})
            if (
                str(exc) == "inviter_unproven"
                and current.get("admission") == "verified"
                and current.get("inviter")
                and current["inviter"] != actor
            ):
                reply = (
                    "请让拉我进群的群主或群管理员发送「开通」。其他人暂不能代他开通。"
                )
        if transport != (
            str(adapter.account),
            adapter.business,
            getattr(adapter, "nim", None),
            adapter.nim_account,
        ):
            return
        # Activation publishes a new policy generation. The reply gets a new guard,
        # but only after the same transport and fresh command identity are rechecked.
        guard = self.write_guard(adapter)
        guard()
        current = self.tenants.get((str(adapter.account), group))
        if not current or not current["present"] or current["epoch"] != epoch:
            return
        # Only this fresh, roster-verified command can use the onboarding reply lane.
        for attempt in range(2):
            try:
                roster = await self.roster(adapter, group)
                break
            except ProtocolError as exc:
                if str(exc) != "database_authority_changed" or attempt:
                    raise
                guard = self.write_guard(adapter)
                guard()
        current = self.tenants.get((str(adapter.account), group))
        if not current or current["epoch"] != epoch or not current["present"]:
            return
        version = current["version"]
        if actor not in roster or str(roster[actor].get("nimId")) != outer.get(2):
            return

        def reply_guard():
            guard()
            current = self.tenants.get((str(adapter.account), group))
            if (
                self.settings["paused"]
                or not current
                or not current["present"]
                or current["paused"]
                or current["epoch"] != epoch
                or current["version"] != version
                or transport
                != (
                    str(adapter.account),
                    adapter.business,
                    getattr(adapter, "nim", None),
                    adapter.nim_account,
                )
                or adapter.group_catalog.get(group, {}).get("team") != outer.get(1)
                or (str(adapter.account), group) in self.suspended_groups
            ):
                raise OpenError("operation_revoked")

        await send_onboarding_text(
            adapter,
            group,
            outer[1],
            f"open-activation/{adapter.account}/{group}/{epoch}/{outer[12]}",
            reply,
            reply_guard,
        )

    def admit_candidate(self, adapter, group, outer, inner):
        """Keep onboarding network IO out of the native push acknowledgement lane."""
        if self.integrated:
            fixture = getattr(self.group_ai, "test_fixture", None)
            if fixture is not None and fixture.observe_reply(
                adapter, group, outer, inner
            ):
                return
            from .test_probe import observe_reply

            if observe_reply(self, adapter, group, outer, inner):
                return
        if (
            not self.active
            or self.settings["paused"]
            or group in self.retired_groups
            or len(self.candidate_tasks) >= 2
        ):
            return
        if not inner.HasField("content") or inner.content.data.strip() != "开通":
            return
        if str(inner.sender.id) == str(adapter.account):
            return

        async def process():
            try:
                await self.candidate(adapter, group, outer, inner)
            except asyncio.CancelledError:
                raise
            except Exception:
                adapter.diagnostics.emit("open_activation", "unconfirmed", outer[12])

        task = asyncio.create_task(process())
        self.candidate_tasks.add(task)
        task.add_done_callback(self.candidate_tasks.discard)

    async def close(self):
        """Cancel bounded onboarding work before closing the control plane."""
        self.active = False
        discovery = list(self.discovery_tasks.values())
        for task in discovery:
            task.cancel()
        if discovery:
            await asyncio.gather(*discovery, return_exceptions=True)
        self.discovery_tasks.clear()
        await self.message_recalls.close()
        from astrbot.core.star.component_runtime import component_runtime

        await component_runtime.close("wangshangliao_moderation")
        self.navigation.sessions.clear()
        self.member_whitelist.pending.clear()
        self.member_whitelist.chat_search.clear()
        await self.member_blacklist.close()
        await self.raid_defense.close()
        await self.welcome.close()
        await self.group_ai.close()
        await self.rule_replies.close()
        tasks = list(self.candidate_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.rpc:
            await self.rpc.close()

    def admit_command(self, event):
        """Reserve one shared cooldown slot for this native message.

        Args:
            event: Transport-bound command or navigation event.

        Returns:
            Whether processing or feedback is admitted for this message.
        """
        if event.is_private_chat():
            return False
        identity = self.navigation.key(event)
        if identity is None:
            return False
        message = (identity, str(event.message_obj.message_id))
        reserved = event.get_extra("_wsl_command_admission")
        if reserved is not None and reserved[0] == message:
            return reserved[1]
        now = time.monotonic()
        key = identity[:2]
        self.command_at = {k: v for k, v in self.command_at.items() if now - v < 60}
        admitted = (
            len(self.command_at) < 4096 and now - self.command_at.get(key, -60) >= 3
        )
        if admitted:
            self.command_at[key] = now
        event.set_extra("_wsl_command_admission", (message, admitted))
        return admitted

    async def reject_reply(self, event, text):
        """Share one cooldown for command and ordinary AI rejection feedback.

        Args:
            event: Native caller, reusing any existing admission for this message.
            text: Fixed non-operational rejection or failure text.
        """
        if not event.is_private_chat() and self.admit_command(event):
            await event.send(event.plain_result(text))

    async def manager_command(self, event, command):
        """Dispatch fixed tenant mutations before existing capability-checked actions."""
        if event.is_private_chat():
            raise OpenError("private_messages_retired")
        parts = command.split(maxsplit=1)
        action, argument = parts[0], parts[1] if len(parts) == 2 else ""
        from .activities import ADMIN_COMMANDS

        if action == "禁言" and self.integrated:
            return await self.delegated_mutes.command(event, command)
        if action in RETIRED_CHAT_ACTIONS:
            return RETIRED_CHAT_REPLY
        if action not in OPEN_COMMANDS | (ADMIN_COMMANDS if self.integrated else set()):
            return "无权限"
        require_group_admin(event.platform.config, event.get_group_id())
        if action in WHITELIST_COMMANDS:
            return await self.member_whitelist.command(event, command)
        if action in BLACKLIST_COMMANDS:
            return await self.member_blacklist.command(event, command)
        if action not in RULE_COMMANDS and action not in {"规则", "能力"}:
            result = await self.plugin.commands.run(event, command)
            return (
                result if result is not None else "此命令暂不可用，请在插件管理页核对。"
            )
        adapter = event.platform
        key = (
            id(adapter),
            adapter.account,
            event.get_sender_id(),
            event.unified_msg_origin,
        )
        selection = self.plugin.commands.selections.get(key, {})
        group = event.get_group_id() or (
            selection.get("group", "")
            if selection.get("expires", 0) > time.monotonic()
            else ""
        )
        if not group:
            return "请在对应群里发送命令，或在WebUI选择目标群操作。"
        if not self.management.allowed(event, group):
            return "无权限"
        require_group_admin(adapter.config, group)
        tenant = self.tenants[(str(adapter.account), group)]
        bind_reply = getattr(event, "bind_management_reply_guard", None)
        if action in {
            "管理员列表",
            "能力",
            "规则",
            "添加管理员",
            "移除管理员",
            "热群状态",
            "测试规则",
        } and callable(bind_reply):
            bind_reply(self.management, (group,))
        guard = self.write_guard(adapter, event)
        roster = await self.roster(adapter, group)
        actor = str(event.get_sender_id())
        if actor not in roster or actor not in tenant["managers"]:
            return "无权限"
        if roster[str(adapter.account)].get("groupRole") not in {
            "GROUP_ROLE_OWNER",
            "GROUP_ROLE_ADMIN",
        }:
            raise ProtocolError("group_native_admin_required")
        guard()
        if self.tenants[(str(adapter.account), group)]["version"] != tenant["version"]:
            raise OpenError("tenant_changed")
        self.member_evidence[(str(adapter.account), group)] = roster
        guard = self.write_guard(
            adapter, event, group=group, tenant=tenant, members=roster
        )
        guard()
        if action in GROUP_AI_COMMANDS:
            row = self.group_ai.configs.get((str(adapter.account), group))
            config = dict(row["config"] if row else GROUP_AI_DEFAULT)
            if action == "热群状态":
                readiness = (
                    await self.group_ai.readiness(adapter, row)
                    if row
                    else {"ready": False, "reasons": ["尚未配置本群AI。"]}
                )
                return (
                    f"群聊AI：{'开启' if config['enabled'] else '关闭'}\n"
                    f"@热群对话：{'开启' if config['mentions'] else '关闭'}（热群人格）\n"
                    f"普通聊天自主接话：{'开启' if config['autonomous'] else '关闭'}（热群人格）\n"
                    f"冷场发话：{'开启' if config['idle'] else '关闭'}，"
                    f"间隔{config['interval_minutes']}分钟，"
                    + (
                        f"本群每日另限{config['daily_limit']}次。\n"
                        if config["daily_limit"]
                        else "使用管理员共享额度。\n"
                    )
                    + (
                        "全天可接话，不限制时段。\n"
                        if config["start_hour"] == config["end_hour"]
                        else f"北京时间{config['start_hour']:02d}:00—{config['end_hour']:02d}:00。\n"
                    )
                    + (
                        "基础条件已满足，按话题与剩余额度决定是否接话。"
                        if readiness["ready"]
                        else "暂未发话的原因：\n" + "\n".join(readiness["reasons"])
                    )
                )
            if action in GROUP_AI_MODES:
                raise OpenError("group_ai_server_owned")
            else:
                config["enabled"] = action == "开启热群"
            await asyncio.to_thread(
                self.group_ai.configure,
                str(adapter.account),
                group,
                row["version"] if row else 0,
                tenant["version"],
                config,
                actor,
                guard=guard,
            )
            await self.group_ai.refresh()
            return (
                (
                    "本群AI设置已保存。\n"
                    f"总开关：{'开启' if config['enabled'] else '关闭'}；"
                    f"@热群：{'开启' if config['mentions'] else '关闭'}；"
                    f"自主接话：{'开启' if config['autonomous'] else '关闭'}；"
                    f"冷场发话：{'开启' if config['idle'] else '关闭'}。"
                    + (
                        "\n总开关当前关闭；需要使用时，请在菜单中开启「本群AI总开关」。"
                        if not config["enabled"]
                        and any(config[m] for m in ("mentions", "autonomous", "idle"))
                        else "\n所有回复方式都已关闭，总开关也已关闭。"
                        if not config["enabled"] and action in GROUP_AI_MODES
                        else ""
                    )
                )
                + (
                    "\n公共AI预算尚未核实，暂不调用模型；设置已保存。"
                    if config["enabled"] and not self.budget["confirmed"]
                    else ""
                )
                + (
                    "\n上次调用或发送结果待核对，仍暂停发话，请联系负责人核对。"
                    if row and row["blocked"]
                    else ""
                )
            )
        if action in {"本群额度", "确认本群扣费"}:
            payer = await asyncio.to_thread(
                self.plans.payer, str(adapter.account), group
            )
            if action == "本群额度":
                if (
                    not payer
                    or not payer["confirmed"]
                    or payer["epoch"] != tenant["epoch"]
                ):
                    return "本群还没有确认额度承担人。默认由开通者确认；换人请联系机器人服务管理员。"
                if payer["uid"] not in roster or payer["uid"] not in tenant["managers"]:
                    return "额度承担人已不在本群或已被撤权，本群AI暂停，请联系负责人重新指定。"
                state = await asyncio.to_thread(
                    self.plans.snapshot, payer["principal"], payer["uid"]
                )
                name = roster[payer["uid"]].get("userNick") or "本群管理"
                exempt = payer["uid"] in set(self.quota_admins) | {
                    str(u) for u in self.context.get_config().get("admins_id", [])
                }
                return f"本群额度承担人：{name}\n" + self.plans.render(
                    state, exempt=exempt
                )
            identity = event.actor_identity
            identity.require_verified()
            approved = event.get_extra("_wsl_billing_consent_stamp")
            if approved is None:
                stamp = self.navigation.stamp(event)
                message = str(event.message_obj.message_id)
                self.navigation.sessions[self.navigation.key(event)] = Session(
                    time.monotonic() + 600,
                    confirm="确认本群扣费",
                    stamp=stamp,
                    message=message,
                    preview_message=message,
                )
                await asyncio.to_thread(
                    self.store.link_principal, actor, identity.principal
                )
                state = await asyncio.to_thread(
                    self.plans.snapshot, identity.principal, actor
                )
                return (
                    "即将由你承担本群AI用量，与你的其他绑定群共享套餐。\n"
                    + self.plans.render(state)
                    + "\n回复 1 确认；0 放弃返回；取消 退出。"
                )
            authority = guard

            def guard():
                authority()
                if self.navigation.stamp(event) != approved:
                    raise OpenError("billing_changed")

            guard()
            await asyncio.to_thread(
                self.store.link_principal, actor, identity.principal
            )
            state = await asyncio.to_thread(
                self.plans.snapshot, identity.principal, actor
            )
            await asyncio.to_thread(
                self.plans.bind,
                str(adapter.account),
                group,
                actor,
                identity.principal,
                payer["version"] if payer else 0,
                tenant["version"],
                state["version"],
                actor,
                guard=guard,
            )
            return "已确认：本群AI使用我的共享套餐额度池。次数或费用用完将暂停本群AI，可在WebUI核对。"
        if action == "管理员列表":
            bindings = await asyncio.to_thread(
                self.store.manager_bindings, str(adapter.account), group
            )
            guard()
            self.plugin.commands.selections[key] = {
                "expires": time.monotonic() + 600,
                "group": group,
                "kind": "managers",
                "version": tenant["version"],
                "epoch": tenant["epoch"],
                "members": [
                    {"userId": uid, "nimId": bindings.get(uid, "")}
                    for uid in tenant["managers"]
                ],
            }
            return "本群管理员：\n" + "\n".join(
                f"{i}. {roster.get(uid, {}).get('userNick') or roster.get(uid, {}).get('groupMemberNick') or ('群管理' if uid in roster else '已离群管理员')}（UID {uid}）"
                for i, uid in enumerate(tenant["managers"], 1)
            )
        if action == "能力":
            role = roster[str(adapter.account)].get("groupRole")
            return (
                "我已是本群群主或群管理员，可以尝试禁言、撤回等操作；操作后会告诉你是否成功。"
                if role in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
                else "我目前不是群管理员，无法禁言或撤回别人的消息。请群主先把我设为群管理员。"
            )
        if action == "规则":
            rules = tenant["rules"]
            return (
                f"广告审核：{'开启' if rules['advertising'] else '关闭'}\n"
                f"防刷屏：{'开启' if rules['flood'] else '关闭'}\n"
                f"仅白名单成员可发视频：{'开启' if rules.get('video_whitelist_only') else '关闭'}\n"
                f"合并记录撤回：{'开启' if rules.get('recall_forward') else '关闭'}；"
                f"二维码图片：{'开启' if rules.get('image_qr_recall') else '关闭'}；"
                f"含数字图片：{'开启' if rules.get('image_digits_recall') else '关闭'}\n"
                "以上媒体限制只撤回、不累计禁言；白名单和群管理豁免。\n"
                "广告和刷屏每轮前三次撤回警告，后续禁言1/3/10分钟；10分钟结束后重新累计，不自动踢人。\n"
                "防刷屏：3秒5条、10秒15条，或30秒同文5条，撤回对应消息。\n"
                "昨天有效发言前20名且至少101条自动加入白名单；"
                "每次有效文字聊天刷新7天保留期，连续7天无有效聊天退出。"
                "撤回消息不计数，手动名单不变，黑名单优先。\n"
                "添加白名单：群内真正@一名成员并发送「白名单」，无需搜索或确认。\n关键词：\n"
                + (
                    "\n".join(f"{i}. {w}" for i, w in enumerate(rules["keywords"], 1))
                    or "未设置"
                )
                + "\n组合规则（同一条消息须同时包含全部词）：\n"
                + (
                    "\n".join(
                        f"{i}. {'开启' if rule['enabled'] else '停用'} · {rule['name']}："
                        + " + ".join(rule["terms"])
                        for i, rule in enumerate(rules.get("custom_rules", []), 1)
                    )
                    or "未设置"
                )
                + "\n默认广告规则保留；自定义关键词和组合规则只影响本群，关闭广告审核后均不处罚。"
            )
        if action == "测试规则":
            from .content_rules import classify, keyword_category

            category = keyword_category(
                argument,
                {
                    "keywords": tenant["rules"]["keywords"],
                    "custom_rules": tenant["rules"].get("custom_rules", []),
                },
            ) or classify(argument)
            return (
                ("命中广告规则。" if category else "没有命中广告规则。")
                + (
                    "\n本群广告审核当前关闭，不会执行处罚。"
                    if not tenant["rules"]["advertising"]
                    else ""
                )
                + "\n仅测试文字，不发送警告、禁言或撤回。"
            )
        if action in {"添加管理员", "移除管理员"}:
            from astrbot.api.message_components import At

            from .group_management import native_manager_ids

            removing = action == "移除管理员"
            bindings = (
                await asyncio.to_thread(
                    self.store.manager_bindings, str(adapter.account), group
                )
                if removing
                else {}
            )
            guard()
            mentions = [
                str(c.qq)
                for c in event.get_messages()
                if isinstance(c, At) and str(c.qq) != str(adapter.account)
            ]
            if len(mentions) == 1:
                target = mentions[0]
            elif (
                argument.isascii()
                and argument.isdigit()
                and selection.get("group") == group
                and selection.get("expires", 0) > time.monotonic()
                and 1 <= int(argument) <= len(selection.get("members", []))
            ):
                if selection.get("kind") == "managers" and (
                    not removing
                    or (selection.get("version"), selection.get("epoch"))
                    != (tenant["version"], tenant["epoch"])
                ):
                    return "列表已变化或不能用于添加管理员，请重新查询对应列表。"
                member = selection["members"][int(argument) - 1]
                target = str(member["userId"])
                current_peer = (
                    bindings.get(target, "")
                    if removing
                    else str(roster.get(target, {}).get("nimId", ""))
                )
                if not current_peer or current_peer != str(member.get("nimId", "")):
                    return "成员身份已变化或缺少可信绑定，请重新查询对应列表。"
            else:
                return "请使用真实@成员，或先查询成员列表（移除也可查管理员列表）后使用当前页序号。"
            peer = (
                bindings.get(target, "")
                if removing
                else str(roster.get(target, {}).get("nimId", ""))
            )
            if not peer or target in roster and str(roster[target]["nimId"]) != peer:
                raise OpenError("member_unavailable")
            authority = guard

            def guard():
                authority()
                if not removing and target not in native_manager_ids(
                    self.member_evidence.get(
                        (str(adapter.account), group), {}
                    ).values(),
                    adapter.account,
                ):
                    raise OpenError("manager_native_role_required")

            guard()
            token = await asyncio.to_thread(
                self.store.preview_manager,
                str(adapter.account),
                group,
                tenant["version"],
                actor,
                event.unified_msg_origin,
                str(event.message_obj.message_id),
                target,
                "remove" if removing else "add",
                peer,
                guard=guard,
            )
            name = (
                roster.get(target, {}).get("userNick")
                or roster.get(target, {}).get("groupMemberNick")
                or ("该成员" if target in roster else f"已离群管理员 UID {target}")
            )
            return f"预览：{action} {name}。请在当前对话发新消息：确认授权 {token}\n十分钟有效；不能移除最后一位管理。"
        if action == "确认授权":
            from .group_management import native_manager_ids

            eligible = native_manager_ids(roster.values(), adapter.account)
            authority = guard

            def guard():
                authority()
                if eligible != native_manager_ids(
                    self.member_evidence.get(
                        (str(adapter.account), group), {}
                    ).values(),
                    adapter.account,
                ):
                    raise OpenError("manager_native_role_required")

            await self.commit_tenant(
                str(adapter.account),
                group,
                self.store.confirm_manager,
                argument,
                actor,
                event.unified_msg_origin,
                str(event.message_obj.message_id),
                str(adapter.account),
                group,
                {uid: str(m["nimId"]) for uid, m in roster.items()},
                eligible_managers=eligible,
                guard=guard,
            )
        else:
            rules = copy.deepcopy(tenant["rules"])
            if action in {"开启广告审核", "关闭广告审核"}:
                rules["advertising"] = action.startswith("开启")
            elif action in {"开启防刷屏", "关闭防刷屏"}:
                rules["flood"] = action.startswith("开启")
            elif action == "添加关键词":
                rules["keywords"].append(argument.strip())
            elif action in {"删除关键词", "修改关键词"}:
                fields = argument.split(maxsplit=1)
                index = int(fields[0]) - 1
                if not 0 <= index < len(rules["keywords"]):
                    raise OpenError("keyword_index")
                if action == "删除关键词":
                    rules["keywords"].pop(index)
                else:
                    rules["keywords"][index] = fields[1].strip()
            elif action in {"添加规则", "修改规则", "删除规则", "启用规则", "停用规则"}:
                from .custom_rules import parse

                custom = rules.setdefault("custom_rules", [])
                if action == "添加规则":
                    custom.append(parse(argument))
                else:
                    fields = argument.split(maxsplit=1)
                    index = int(fields[0]) - 1
                    if not 0 <= index < len(custom):
                        raise OpenError("custom_rule_index")
                    if action == "删除规则":
                        custom.pop(index)
                    elif action == "修改规则":
                        custom[index] = {
                            **parse(fields[1]),
                            "enabled": custom[index]["enabled"],
                        }
                    else:
                        custom[index]["enabled"] = action == "启用规则"
            await self.commit_tenant(
                str(adapter.account),
                group,
                self.store.change,
                str(adapter.account),
                group,
                tenant["version"],
                actor,
                rules=rules,
                guard=guard,
            )
        return "已保存本群设置。"

    async def on_event(self, event):
        """Route opted-in fixed work through the shared capability executor."""
        if event.is_private_chat():
            event.set_extra("_component_claimed", True)
            event.stop_event()
            return
        governance = getattr(self, "governance", None)
        if governance is None:
            return await OpenService._on_event(self, event)
        command = self.recognize_command(event.message_str.strip())
        if is_removed_group_command(event.message_str):
            event.set_extra("_component_claimed", True)
            event.stop_event()
            return
        activities = getattr(self.plugin, "activities", None)
        if (
            not command
            and activities is not None
            and not event.is_private_chat()
            and (event.get_extra("wangshangliao_payload") or {}).get("kind", "text")
            == "text"
        ):
            command = await activities.entry_command(event)
        fixed = bool(
            command or self.navigation.accepts(event, event.message_str.strip())
        )
        try:
            return await governance.run(
                "commands" if fixed else "rules",
                event.platform,
                lambda: self._on_event(event),
                group=event.get_group_id(),
                event=event,
                claim=fixed,
            )
        except Exception:
            # Unknown routing state is terminal, never ordinary AI input.
            event.set_extra("_component_claimed", True)
            event.stop_event()
            raise

    async def _on_event(self, event):
        """Consume only group events, including explicit group AI admission."""
        if event.is_private_chat():
            event.set_extra("_component_claimed", True)
            event.stop_event()
            return
        config = event.platform.config
        completion = getattr(event, "processing_completion", None)
        payload = event.get_extra("wangshangliao_payload") or {}
        if not is_fresh_message(payload, time.time()):
            event.stop_event()
            return
        media = payload.get("kind") in ("image", "video", "forward", "game")
        text = "" if media else event.message_str.strip()
        if is_removed_group_command(text):
            event.set_extra("_component_claimed", True)
            event.stop_event()
            return
        if text in {"/结账", "/结账结果"}:
            text = text[1:]
        command = self.recognize_command(text)
        activities = getattr(self.plugin, "activities", None)
        if (
            not command
            and not media
            and activities is not None
            and not event.is_private_chat()
        ):
            command = await activities.entry_command(event)
        consumed = False
        menu_key = self.navigation.key(event)
        if menu_key is None:
            event.stop_event()
            return
        if (
            not self.active
            or self.settings["paused"]
            or self.capacity_paused
            or not config.get("enable", True)
            or config.get("_business_active") is False
            or getattr(event, "policy_epoch", config.get("_business_epoch"))
            != config.get("_business_epoch")
            or completion is not None
            and completion.cancelled()
        ):
            if command in {"取消", "0"}:
                self.navigation.dismiss(event)
            event.stop_event()
            return
        from .blacklist_messages import contain

        if await contain(self, event):
            return
        menu_input = not media and self.navigation.accepts(event, text)
        if command or menu_input:
            event.set_extra("_component_claimed", True)
            if not event.is_private_chat():
                from astrbot.core.platform.sources.wangshangliao.event import (
                    managed_group_control,
                )

                if not config.get("reply_groups", {}).get(
                    event.get_group_id(), True
                ) and not managed_group_control(
                    event.platform, event.get_group_id(), payload
                ):
                    if command in {"取消", "0"}:
                        self.navigation.dismiss(event)
                    event.stop_event()
                    return
            event.set_extra("wsl_plain_command", True)
            event.set_extra("wsl_ranking_result", command.split(" ", 1)[0] == "排名")
            from astrbot.core.platform.sources.wangshangliao.reply_recall import (
                schedule_command,
            )

            await schedule_command(event)
            if event.get_extra("wsl_test_scope"):
                event.set_extra("wsl_command_result", True)
        try:
            if command in {"取消", "0"}:
                await self.navigation.handle(event, text)
                return
            if (command or menu_input) and not self.admit_command(event):
                if menu_input:
                    record_event(event, "menu", "cooldown")
                return
            if not event.is_private_chat() and (
                text != "开通"
                or self.tenants.get(
                    (str(event.platform.account), event.get_group_id()), {}
                ).get("active")
            ):
                try:
                    require_group_admin(config, event.get_group_id())
                except ProtocolError:
                    if command or menu_input:
                        self.navigation.sessions.pop(menu_key, None)
                        event.set_extra("wsl_keep_reply", True)
                        await event.send(event.plain_result(GROUP_ADMIN_REQUIRED))
                    return
            if (command or menu_input) and command not in {
                "取消",
                "0",
            }:
                from astrbot.core.platform.sources.wangshangliao.group_scope import (
                    group_revision,
                )

                current = event.platform.config
                group = event.get_group_id()
                if "_open_groups" in current and payload.get(
                    "_open_revision"
                ) != group_revision(current, group):
                    return
            if not media and await self.navigation.handle(event, text):
                return
            if payload.get("_ai_busy"):
                await self.reject_reply(event, "本群AI正在处理，请稍后再试。")
                return
            if text == "开通":
                reply = await self.activate(event, event.get_group_id())
                await event.send(event.plain_result(reply))
            elif command.split(" ", 1)[0] in BUSINESS_RULE_COMMANDS:
                event.set_extra("wsl_keep_reply", True)
                await event.send(
                    event.plain_result(await self.rule_replies.command(event, command))
                )
            elif command.split(" ", 1)[0] in KNOWLEDGE_COMMANDS:
                await event.send(
                    event.plain_result(await self.knowledge.command(event, command))
                )
            elif command.split(" ", 1)[0] in {
                "结账",
                "结账结果",
                "确认已付款",
                "确认修改",
            }:
                await event.send(
                    event.plain_result("请在WebUI结账页面查看账目和处理结算。")
                )
            elif command:
                from .activities import ADMIN_COMMANDS

                if (
                    command.split(" ", 1)[0]
                    in OPEN_COMMANDS | ADMIN_COMMANDS | RETIRED_CHAT_ACTIONS
                ):
                    reply = await self.manager_command(event, command)
                else:
                    reply = await self.plugin.commands.run(event, command)
                    if reply is None:
                        return
                event.set_extra("wsl_keep_reply", True)
                await event.send(
                    event.plain_result(redact_reply(reply))
                    if isinstance(reply, str)
                    else event.chain_result(reply.chain)
                )
            elif str(event.get_sender_id()) not in self.management.assignments.get(
                self.management.key(event.platform, event.get_group_id()), ()
            ):
                from .content_rules import flood_category

                adapter = scoped(event.platform, event.get_group_id())
                worker = getattr(self.plugin, "flood_runtime", None)
                poller = getattr(self.plugin, "flood_poller", None)
                if (
                    worker is not None
                    and isinstance(poller, asyncio.Task)
                    and not poller.done()
                    and await worker.handoff(
                        event.platform,
                        event.get_group_id(),
                        str(event.message_obj.message_id),
                        payload,
                    )
                ):
                    # The durable inbox worker owns the outcome. Do not wait on
                    # its network IO again in this sender's ordinary chat FIFO.
                    consumed = True
                    event.set_extra("_component_claimed", True)
                else:
                    category = flood_category(
                        adapter,
                        event.get_group_id(),
                        event.message_obj.message_id,
                        payload,
                    )
                    consumed = await handle(
                        adapter,
                        event.get_group_id(),
                        event.message_obj.message_id,
                        payload,
                        assessment={
                            "decision": "violation",
                            "category": category,
                            "message_id": str(event.message_obj.message_id),
                        }
                        if category
                        else None,
                        media_runtime=getattr(self.plugin, "media_moderation", None),
                    )
            if media:
                # Media never becomes command text, keyword input or model input.
                event.set_extra("_component_claimed", True)
                return
            if not event.is_private_chat() and not command and not menu_input:
                if not consumed:
                    consumed = await self.rule_replies.observe(event)
                await self.group_ai.observe(event, consumed=consumed)
        except (ValueError, OpenError, ProtocolError) as exc:
            reason = str(exc)
            if reason in {"activation_cooldown", "command_cooldown"}:
                return
            diagnostic = getattr(event.platform, "diagnostics", None)
            if diagnostic is not None and reason in {
                "activation_cooldown",
                "activation_changed",
                "activation_code_invalid",
                "already_active_or_unknown",
                "operation_revoked",
                "database_authority_changed",
                "confirmation_invalid",
                "tenant_changed",
                "moderation_permission",
            }:
                diagnostic.emit(
                    "open_command",
                    "rejected",
                    str(getattr(getattr(event, "message_obj", None), "message_id", "")),
                    error=reason
                    if reason in {"confirmation_invalid", "moderation_permission"}
                    else "configuration_conflict",
                    action=reason,
                    failed=True,
                )
            if event.is_private_chat() or command:
                messages = {
                    **KNOWLEDGE_REPLIES,
                    **ACTIVATION_REPLIES,
                    "group_ai_server_owned": "群聊和热群的模式、主题、时段及知识由服务端维护；本群授权管理只能切换已开通的AI总开关。",
                    "group_ai_admission_required": "服务端开通、预算核验或本群承担人确认尚未完成，未开启AI。",
                    "group_knowledge_server_owned": "群知识由服务端维护，现有资料保留；本群管理不能修改。",
                    "business_rules_changed": "本群规则或权限已变化，请重新查看并预览。",
                    "business_group_required": "请在对应群操作，或由已有后台权限的负责人在插件页面选择目标群。",
                    "business_confirmation_invalid": "规则确认已过期或不属于本对话，请重新预览。",
                    "last_manager": "不能移除最后一名管理员，请先添加接任者。",
                    "manager_native_role_required": "只能添加本群的群主或群管理员。请先在旺商聊里设置，再重新选择。",
                    "personal_quota": "今日AI咨询额度已用完，请明天再试；固定命令不受影响。",
                    "global_quota": "今日AI公共额度已用完；固定命令仍可使用。",
                    "chat_cooldown": "请间隔5秒再发送问题。",
                    "command_cooldown": "请间隔3秒再发送命令。",
                    "moderation_permission": "未执行：请先把我设为群管理员，才能禁言或撤回消息。",
                    "delegated_mute_revoked": "未执行：禁言专属授权、群状态或成员身份已变化；请联系机器人负责人核对。",
                    "delegated_mute_target": "未执行：请用客户端真正@一名成员发送「@成员 禁言128」，不要手打昵称或同时@多人。",
                    "delegated_mute_departed": "未执行：该成员已不在当前完整群名单中，可能已退群；本次未提交禁言，也未修改名单。",
                    "delegated_mute_conflict": "未执行：@成员与回复对象不是同一人；请只保留一个真实@目标重新发送。",
                    "delegated_mute_protected": "未执行：不能处理自己、群主、管理员、机器人、有效白名单或黑名单成员；黑名单按原流程处理。",
                    "delegated_mute_existing": "未执行：该成员已经被禁言，不覆盖已有处罚；请管理员核对当前禁言。",
                    "member_directory_rejected": "未执行：旺商聊暂时拒绝读取本群成员名单，无法核实管理员和目标身份；不是命令格式错误。请联系机器人负责人检查成员接口，恢复后再发送命令。",
                    "group_native_admin_required": GROUP_ADMIN_REQUIRED,
                    "custom_rules_invalid": "未保存。每群最多20条组合规则；名称1—24字，须填写2—5个不同的词，每词2—40字。格式：添加规则 名称/词一/词二（也可用 | 分隔）",
                    "custom_rules_redundant": "未保存。组合词不能互相包含，例如「收款」和「收款截图」只算一个条件，请换成不同的条件。",
                    "custom_rule_index": "没有这个规则序号，请发送「规则」查看最新列表。",
                    "keyword_index": "没有这个关键词序号，请发送「规则」查看最新列表。",
                    "member_whitelist_permission": "未执行：需要同时具备本群原生群管身份和机器人管理授权；请联系机器人负责人检查本群授权。",
                    "member_whitelist_target": "未加入：请在本群用客户端真正@一名成员并发送「白名单」；不要手打昵称，不要同时@多人，回复对象也不能与@对象冲突。",
                    "member_whitelist_remove_target": "尚未移出：请在本群用客户端真正@一名成员发送「删白」，不要手打@或同时@多人，回复对象也不能与@对象冲突。已退群成员可发送「删白 保存的名称或精确UID」搜索，再选择并确认。",
                    "member_whitelist_query": "请输入1—64字的成员名称，仅搜索当前群。",
                    "member_whitelist_narrow": "同名或匹配成员超过20位，请输入更完整的名称缩小搜索。",
                    "member_list_conflict": "名单未修改：该成员已在本群另一份名单中，请先移除另一名单，再重新添加；不会自动删除已有记录。",
                    "member_whitelist_changed": "成员、白名单、群设置或权限已变化，请重新搜索、选择并确认。",
                    "member_whitelist_selection": "请从本次本群搜索结果中选择有效序号，不能直接输入账号添加。",
                    "member_whitelist_confirmation": "请用一条新消息确认本次白名单预览。",
                    "blacklist_permission": "未执行：需要同时具备本群原生群管身份和机器人管理授权；请联系机器人负责人检查本群授权。",
                    "blacklist_target": "未执行：请回复该成员原消息，或从客户端真正@一名成员，发送「黑名单」；不要手打昵称。",
                    "blacklist_member_unavailable": "未执行：当前群成员名单中找不到该成员，可能已退群或成员资料尚未同步。本次未修改黑名单，也未提交禁言或解禁；请先核实该成员是否仍在本群。",
                    "blacklist_identity_changed": "未执行：当前成员身份绑定不一致或不唯一，暂时无法安全确认目标。本次未修改黑名单，也未提交禁言或解禁；请联系机器人负责人核对成员资料。",
                    "blacklist_target_conflict": "未执行：@成员与回复对象不是同一人。本次未修改黑名单，也未提交禁言或解禁；请只保留一个目标后重新操作。",
                    "blacklist_quote": "未执行：无法核实回复的同群原消息，记录可能缺失或引用不匹配。本次未修改黑名单，也未提交禁言或解禁；请在本群真正@一名成员操作。",
                    "blacklist_protected": "未执行：群主、管理员、机器人及身份已变化的成员不能加入黑名单。",
                    "blacklist_changed": "群设置、名单或权限已变化；请重新查询、选择并确认。",
                    "blacklist_version_changed": "黑名单或禁言时长已变化；请重新查询并预览确认。",
                    "blacklist_minutes": "禁言时长须为1至41760整分钟；可发送「黑名单禁言时长 1天」重新预览。",
                    "blacklist_query": "请输入1—64字的成员名称，仅搜索当前群。",
                    "blacklist_narrow": "匹配超过20人，请输入更完整的成员名称。",
                    "blacklist_selection": "请从本次本群搜索结果选择序号，不能直接提交账号。",
                    "blacklist_confirmation": "请在当前对话用一条新消息确认本次黑名单预览。",
                    "tenant_rules_refresh": "本群规则已更新，请刷新页面后再保存，避免覆盖已有规则。",
                    "tenant_changed": "本群设置或权限已变化，请重新查看规则后再操作。",
                    "tenant_invalid": "未保存。关键词最多50个，每个1—100字且不能重复；请核对本群设置。",
                }
                await self.reject_reply(
                    event,
                    messages.get(
                        str(exc),
                        "这次没有完成，请检查命令后重新发送；仍不成功请联系机器人负责人。",
                    ),
                )
        finally:
            event.stop_event()

    def configure_model(self, payload):
        """Save only an integrated provider selection without copying credentials.

        Args:
            payload: Exact server-owned provider selection.

        Raises:
            OpenError: If independent execution is retired or selection is invalid.
        """
        if not self.integrated:
            raise OpenError("isolated_mode_retired")
        if set(payload) != {"provider_id"} or not isinstance(
            payload["provider_id"], str
        ):
            raise OpenError("model_invalid")
        value = dict(payload)
        self.resolve_model(value)
        temporary = self.model_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.model_path)
        finally:
            temporary.unlink(missing_ok=True)
        self.model = value

    def resolve_model(self, selection=None):
        """Resolve a server-owned provider without copying keys into the tenant store.

        Args:
            selection: Optional total-control provider selection.

        Returns:
            A detached, private single-request HTTP configuration.

        Raises:
            OpenError: If the selected provider is missing or incompatible.
        """
        selection = self.model if selection is None else selection
        if not self.integrated:
            return dict(selection)
        provider = self.context.get_provider_by_id(selection.get("provider_id", ""))
        if provider is None or provider.meta().type != "openai_chat_completion":
            raise OpenError("provider_unavailable")
        config = provider.provider_config
        base = config.get("api_base") or "https://api.openai.com/v1"
        parsed = urlsplit(base)
        if (
            not config.get("enable", True)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise OpenError("provider_unavailable")
        return {
            "base_url": base,
            "model": provider.get_model(),
            "api_key": provider.get_current_key(),
        }

        # Retain historical sessions and native conversations without opening
        # or modifying any retired private-runtime database.

    async def account_action(self, actor, action, payload):
        """Reject legacy login dispatch without registering or saving credentials.

        Args:
            actor: Legacy authenticated dashboard principal.
            action: Legacy login operation.
            payload: Unused legacy operation arguments.

        Raises:
            OpenError: Always; current logins belong to the platform configuration.
        """
        if self.integrated:
            raise OpenError("use_platform_login")
        raise OpenError("isolated_mode_retired")
