"""Explicit group blacklists with durable, evidence-bound rejoin enforcement."""

import asyncio
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.blacklist import (
    BlacklistStore,
    protected_target,
)
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.invitations import (
    latest_membership_events,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .custom_rules import normalize
from .member_exemptions import read_exemptions
from .member_whitelist import TOKEN, Scope, member_name
from .open_store import OpenError

COMMANDS = {
    "黑名单",
    "黑名单列表",
    "添加黑名单",
    "移除黑名单",
    "选择黑名单",
    "确认黑名单",
    "黑名单禁言时长",
}


class Read(Scope):
    operation: Literal["read"]


class Search(Scope):
    operation: Literal["search"]
    mode: Literal["add", "remove"]
    query: str = Field(min_length=1, max_length=64)


class Preview(Scope):
    operation: Literal["preview"]
    selection: TOKEN
    index: int = Field(ge=1, le=20)


class Commit(Scope):
    operation: Literal["commit"]
    token: TOKEN


class SettingsPreview(Scope):
    operation: Literal["settings_preview"]
    minutes: int = Field(ge=1, le=41760)


REQUEST = TypeAdapter(
    Annotated[
        Read | Search | Preview | Commit | SettingsPreview,
        Field(discriminator="operation"),
    ]
)


class MemberBlacklist:
    """Share scoped administration and fail-closed native rejoin processing."""

    def __init__(self, service):
        self.service = service
        self.pending = {}
        self.chat_search = {}
        self.tasks = {}
        self.last_poll = {}

    def notice_scope(self, adapter, tenant):
        """Capture the receiving authority without persisting private identities."""
        return {
            "tenant_epoch": tenant["epoch"],
            "tenant_version": tenant["version"],
            "business_epoch": adapter.config.get("_business_epoch"),
            "business_revision": adapter.config.get("_business_revision"),
        }

    def cancel(self, event):
        """Discard drafts belonging to the exact cancelled native conversation.

        Args:
            event: Original authenticated conversation.
        """
        session = self.service.navigation.key(event)
        self.chat_search.pop(session, None)
        for token, row in list(self.pending.items()):
            if row["session"] == session:
                self.pending.pop(token, None)

    async def target(self, event, members):
        """Resolve one real mention or same-group original-message reference.

        Args:
            event: Authenticated original native group message.
            members: Fresh complete directory for this exact group.

        Returns:
            Business UID and native peer of the uniquely proven target.

        Raises:
            OpenError: If the target is missing, conflicting or unproven.
        """
        payload = event.get_extra("wangshangliao_payload") or {}
        adapter = event.platform
        mentions = payload.get("mentions", [])
        spans = payload.get("mention_spans", [])
        if not isinstance(mentions, list) or not isinstance(spans, list):
            raise OpenError("blacklist_target")
        peers = {
            str(peer) for peer in mentions if str(peer) != str(adapter.nim_account)
        }
        if len(peers) > 1:
            raise OpenError("blacklist_target")
        target = None
        if peers:
            peer = next(iter(peers))
            selected = [
                uid for uid, row in members.items() if str(row["nimId"]) == peer
            ]
            matching = [row for row in spans if str(row.get("uid")) == peer]
            text = str(payload.get("text", "")).encode("utf-16-le")
            if len(selected) != 1 or len(matching) != 1:
                raise OpenError("blacklist_target")
            span = matching[0]
            start, end = span.get("start"), span.get("end")
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(text) // 2
                or not isinstance(span.get("nick"), str)
            ):
                raise OpenError("blacklist_target")
            try:
                label = text[start * 2 : end * 2].decode("utf-16-le").strip()
            except UnicodeError:
                raise OpenError("blacklist_target") from None
            if label != "@" + span["nick"]:
                raise OpenError("blacklist_target")
            target = (selected[0], peer)
        reply = payload.get("reply")
        if reply is not None:
            if (
                type(reply) is not dict
                or set(reply) != {"server", "client"}
                or any(not isinstance(value, str) for value in reply.values())
                or not reply["server"]
                or any(len(value.encode()) > 128 for value in reply.values())
            ):
                raise OpenError("blacklist_quote")
            # The quoted display text is deliberately never an identity source.
            async with adapter.ledger.ingress_lock:
                async with adapter.ledger.db.execute(
                    "SELECT message,payload,state FROM inbox "
                    "WHERE account=? AND team=? AND message=?",
                    (str(adapter.account), event.get_group_id(), reply["server"]),
                ) as cursor:
                    rows = await cursor.fetchall()
            if len(rows) != 1 or rows[0][2] in {
                "rejected_identity",
                "unsupported",
                "business_suspended",
            }:
                raise OpenError("blacklist_quote")
            try:
                original = json.loads(rows[0][1])
                uid = original["sender"]
                route = original["recall_route"]
                peer = route["peer"]
            except (KeyError, ValueError, TypeError):
                raise OpenError("blacklist_quote") from None
            if (
                not isinstance(uid, str)
                or not isinstance(peer, str)
                or str(members.get(uid, {}).get("nimId", "")) != peer
                or reply["client"]
                and route.get("client") != reply["client"]
                or target is not None
                and target != (uid, peer)
            ):
                raise OpenError("blacklist_quote")
            target = (uid, peer)
        if target is None:
            raise OpenError("blacklist_target")
        return target

    async def run(
        self,
        adapter,
        group,
        operation,
        *,
        actor,
        session,
        event=None,
        tenant_version=None,
        mode="",
        query="",
        selection="",
        index=0,
        token="",
        minutes=None,
    ):
        """Authorize the original scope before each management transaction.

        Args:
            adapter: Existing receiving account, not a caller-selected client.
            group: Exact current group.
            operation: Read, search, preview, settings_preview, commit or native direct.
            actor: Authenticated dashboard or native audit principal.
            session: Immutable conversation or dashboard identity.
            event: Original native command, absent only for dashboard calls.
            tenant_version: Dashboard expected tenant version.
            mode: Add or remove.
            query: Member display-name search, not an account identifier.
            selection: Opaque server-issued search result.
            index: One-based selection.
            token: Opaque server-issued preview.
            minutes: Whole minutes for a future-rejoin policy preview.

        Returns:
            Current entries, a frozen preview or the persisted mutation result.

        Raises:
            OpenError: On missing permission, stale selection or unsafe target.
        """
        service, account = self.service, str(adapter.account)
        tenant = dict(service.tenants.get((account, group), {}))
        if (
            not tenant
            or tenant_version is not None
            and tenant_version != tenant["version"]
            or event is not None
            and not service.management.allowed(event, group)
            or event is None
            and not actor.startswith("dashboard/")
        ):
            raise OpenError("blacklist_permission")
        base = service.write_guard(adapter, event)
        members = await service.roster(adapter, group)
        base()
        authority = service.write_guard(
            adapter,
            event,
            group=group,
            tenant=tenant,
            members=members,
            control=event is None,
        )
        candidate = None

        def guard(*, intent=None):
            authority()
            if (
                service.settings["paused"]
                or tenant["paused"]
                or service.store.tenant(account, group) != tenant
            ):
                raise OpenError("blacklist_changed")
            latest = service.member_evidence.get((account, group), {})
            if event is not None:
                identity = event.actor_identity
                source = latest.get(identity.verified_uid, {})
                if (
                    identity.verified_uid != event.get_sender_id()
                    or str(source.get("nimId", "")) != identity.peer
                    or source.get("groupRole")
                    not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
                ):
                    raise OpenError("blacklist_permission")
            if candidate is not None and mode == "add":
                live = latest.get(candidate["id"], {})
                if (
                    str(live.get("nimId", "")) != candidate["peer"]
                    or live.get("groupRole") != "GROUP_ROLE_MEMBER"
                    or candidate["id"] == account
                    or protected_target(
                        adapter,
                        group,
                        candidate["id"],
                        candidate["peer"],
                        fixture=getattr(service, "_blacklist_fixture", None),
                        intent=intent,
                    )
                    or live.get("accountState")
                    in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
                ):
                    raise OpenError("blacklist_protected")

        guard()
        store = await asyncio.to_thread(BlacklistStore, adapter.config["id"])
        settings = await asyncio.to_thread(store.settings, account, group)
        version, entries = await asyncio.to_thread(store.read, account, group)
        guard()
        if version != settings["version"]:
            raise OpenError("blacklist_changed")
        current_stamp = (
            adapter.config["id"],
            account,
            group,
            tenant["version"],
            tenant["epoch"],
            service.settings["version"],
            adapter.config.get("_business_epoch"),
            adapter.config.get("_business_revision"),
            service.management.versions.get(service.management.key(adapter, group), 0),
        )
        if event is not None:
            current_stamp += (service.navigation.stamp(event),)
        by_uid = {str(row.get("member", row.get("id"))): row for row in entries}
        visible = [
            {
                **row,
                "id": uid,
                "name": member_name(members[uid]) if uid in members else row["name"],
                "phase": "mute",
                "status": row.get("enforcement_status") or "idle",
            }
            for uid, row in by_uid.items()
        ]
        if operation == "read":
            _, exemptions = await asyncio.to_thread(
                read_exemptions, adapter.config["id"], account, group
            )
            guard()
            exemption_ids = {uid.lstrip("0") for uid in exemptions}
            for row in visible:
                if row["id"] in exemption_ids:
                    row["conflict"] = True
            return {
                "version": version,
                "mute_minutes": settings["mute_minutes"],
                "members": visible,
            }
        now = time.monotonic()
        self.pending = {
            key: value for key, value in self.pending.items() if value["expires"] > now
        }
        if operation == "settings_preview":
            if type(minutes) is not int or not 1 <= minutes <= 41760:
                raise OpenError("blacklist_minutes")
            if len(self.pending) >= 4096:
                raise OpenError("service_busy")
            for key, row in list(self.pending.items()):
                if row["session"] == session:
                    self.pending.pop(key, None)
            self.chat_search.pop(session, None)
            token = secrets.token_urlsafe(18)
            self.pending[token] = {
                "kind": "preview",
                "actor": actor,
                "session": session,
                "stamp": current_stamp,
                "version": version,
                "mode": "settings",
                "minutes": minutes,
                "message": str(event.message_obj.message_id)
                if event is not None
                else "",
                "expires": now + 600,
            }
            return {
                "token": token,
                "version": version,
                "policy_version": settings["policy_version"],
                "mode": "settings",
                "mute_minutes": minutes,
            }
        if operation == "direct":
            if (
                event is None
                or event.is_private_chat()
                or mode not in {"add", "remove"}
            ):
                raise OpenError("blacklist_target")
            uid, peer = await self.target(event, members)
            candidate = {"id": uid, "peer": peer, "name": member_name(members[uid])}
        elif operation == "search":
            if (
                mode not in {"add", "remove"}
                or not isinstance(query, str)
                or not 1 <= len(query) <= 64
                or not normalize(query)
                or any(not char.isprintable() for char in query)
            ):
                raise OpenError("blacklist_query")
            candidates = []
            source = members if mode == "add" else by_uid
            if mode == "remove" and query in source:
                source = {query: source[query]}
            for uid, row in source.items():
                if mode == "add" and (
                    uid in by_uid
                    or uid == account
                    or protected_target(
                        adapter,
                        group,
                        uid,
                        str(row.get("nimId", "")),
                        fixture=getattr(service, "_blacklist_fixture", None),
                    )
                    or row.get("groupRole") != "GROUP_ROLE_MEMBER"
                    or row.get("accountState")
                    in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
                ):
                    continue
                live = members.get(uid, {})
                same_peer = str(live.get("nimId", "")) == str(row.get("peer", ""))
                name = (
                    member_name(row)
                    if mode == "add"
                    else member_name(live)
                    if same_peer
                    else row["name"]
                )
                names = (
                    [
                        str(row.get(field) or "")
                        for field in ("userNick", "groupMemberNick")
                    ]
                    if mode == "add"
                    else [
                        row["name"],
                        *(
                            [
                                str(live.get(field) or "")
                                for field in ("userNick", "groupMemberNick")
                            ]
                            if same_peer
                            else []
                        ),
                    ]
                )
                if any(normalize(query) in normalize(name) for name in names) or (
                    mode == "remove" and query == uid
                ):
                    candidates.append(
                        {
                            "id": uid,
                            "peer": str(row["nimId"]) if mode == "add" else row["peer"],
                            "name": name,
                        }
                    )
            if len(candidates) > 20:
                raise OpenError("blacklist_narrow")
            if len(self.pending) >= 4096:
                raise OpenError("service_busy")
            for key, row in list(self.pending.items()):
                if row["session"] == session:
                    self.pending.pop(key, None)
            selection = secrets.token_urlsafe(18)
            if candidates:
                self.pending[selection] = {
                    "kind": "search",
                    "actor": actor,
                    "session": session,
                    "stamp": current_stamp,
                    "version": version,
                    "mode": mode,
                    "candidates": candidates,
                    "expires": now + 600,
                }
                if event is not None:
                    self.chat_search[session] = selection
            else:
                self.chat_search.pop(session, None)
            return {
                "selection": selection if candidates else "",
                "version": version,
                "members": [
                    {"id": row["id"], "name": row["name"]} for row in candidates
                ],
            }
        else:
            saved = self.pending.get(selection if operation == "preview" else token)
            if (
                not saved
                or saved["actor"] != actor
                or saved["session"] != session
                or saved["stamp"] != current_stamp
                or saved["version"] != version
                or saved["kind"] != ("search" if operation == "preview" else "preview")
            ):
                raise OpenError("blacklist_changed")
            mode = saved["mode"]
            if operation == "preview":
                if type(index) is not int or not 1 <= index <= len(saved["candidates"]):
                    raise OpenError("blacklist_selection")
                candidate = saved["candidates"][index - 1]
            elif operation == "commit":
                if event is not None and saved["message"] == str(
                    event.message_obj.message_id
                ):
                    raise OpenError("blacklist_confirmation")
                if mode == "settings":
                    guard()
                    self.pending.pop(token, None)
                    configured = await asyncio.to_thread(
                        store.configure,
                        account,
                        group,
                        saved["minutes"],
                        actor=event.actor_identity if event is not None else actor,
                        guard=guard,
                        expected_version=version,
                    )
                    guard()
                    return {"saved": True, "mode": mode, **configured}
                candidate = saved["member"]
            else:
                raise OpenError("blacklist_arguments")
            live = (
                members.get(candidate["id"], {})
                if mode == "add"
                else by_uid.get(candidate["id"], {})
            )
            if (
                str(live.get("nimId" if mode == "add" else "peer", ""))
                != candidate["peer"]
                or mode == "add"
                and member_name(live) != candidate["name"]
            ):
                raise OpenError("blacklist_changed")
            guard()
            if operation == "preview":
                token = secrets.token_urlsafe(18)
                self.pending.pop(selection, None)
                self.pending[token] = {
                    **saved,
                    "kind": "preview",
                    "member": candidate,
                    "message": str(event.message_obj.message_id)
                    if event is not None
                    else "",
                }
                return {
                    "token": token,
                    "version": version,
                    "mode": mode,
                    "mute_minutes": settings["mute_minutes"],
                    "member": {"id": candidate["id"], "name": candidate["name"]},
                }
            self.pending.pop(token, None)
        guard()
        _, saved_entries = await asyncio.to_thread(
            store.set_member,
            account,
            group,
            candidate["id"],
            candidate["peer"],
            candidate["name"],
            enabled=mode == "add",
            actor=event.actor_identity if event is not None else actor,
            guard=guard,
            expected_version=version,
        )
        guard()
        result = {
            "saved": True,
            "mode": mode,
            "member": candidate["name"],
            "mute_minutes": settings["mute_minutes"],
        }
        if operation == "direct" and mode == "add":
            entry = next(
                row for row in saved_entries if row["member"] == candidate["id"]
            )
            result["enforcement"] = await self.mute_command(
                adapter, group, event, store, entry, guard
            )
        return result

    async def mute_command(self, adapter, group, event, store, entry, guard):
        """Immediately mute one proven command target without inventing a join."""
        service, account = self.service, str(adapter.account)
        uid, peer = entry["member"], entry["peer"]
        command_message = str(event.message_obj.message_id)
        identity = event.actor_identity
        intent = None

        def submission_guard():
            guard(intent=intent)

        async def authorize():
            submission_guard()
            fresh = await service.roster(adapter, group)
            submission_guard()
            if (
                str(event.message_obj.message_id) != command_message
                or event.actor_identity != identity
                or await self.target(event, fresh) != (uid, peer)
                or protected_target(
                    adapter,
                    group,
                    uid,
                    peer,
                    fixture=getattr(service, "_blacklist_fixture", None),
                    intent=intent,
                )
            ):
                raise OpenError("blacklist_changed")
            submission_guard()

        await authorize()
        intent = await asyncio.to_thread(
            store.reserve_command,
            account,
            group,
            uid,
            peer,
            command_message,
            submission_guard,
        )
        if intent is None:
            return {"status": "duplicate_or_unresolved", "submitted": False}
        result = {"status": "unknown"}
        try:
            request = ActionRequest.bind(
                adapter,
                intent.operation,
                "mute",
                int(group),
                member=int(uid),
                minutes=intent.minutes,
                expected_nim=peer,
                blacklist_intent=intent,
                blacklist_fixture=(
                    getattr(service, "_blacklist_fixture", None)
                    if is_managed_account(uid)
                    else None
                ),
                blacklist_guard=submission_guard,
            )
            receipt = await adapter.execute_action(request, authorize=authorize)
            result = dict(receipt) if isinstance(receipt, dict) else result
        except (OpenError, ProtocolError, OSError, TimeoutError) as exc:
            result = {
                "status": "rejected"
                if getattr(exc, "submitted", None) is False
                else "unknown",
                "submitted": getattr(exc, "submitted", None),
            }
        finally:
            status = await asyncio.shield(asyncio.to_thread(store.done, intent, result))
        return {**result, "status": status}

    async def command(self, event, command):
        """Provide direct group commands and a short private member workflow.

        Args:
            event: Original authenticated native conversation.
            command: Recognized fixed blacklist command.

        Returns:
            Literal persisted-state feedback or corrective instructions.
        """
        action, _, argument = command.partition(" ")
        session = self.service.navigation.key(event)
        group = event.get_group_id() or self.service.navigation.stamp(event)[3]
        if not group:
            return "请先发送「群列表」「选择群 序号」，再管理该群黑名单。"
        options, operation = {}, "read"
        payload = event.get_extra("wangshangliao_payload") or {}
        if action == "黑名单禁言时长":
            match = re.fullmatch(r"([0-9]{1,5})\s*(分钟|小时|天)", argument)
            minutes = (
                int(match[1]) * {"分钟": 1, "小时": 60, "天": 1440}[match[2]]
                if match
                else 0
            )
            if not 1 <= minutes <= 41760:
                return (
                    "请发送「黑名单禁言时长 N分钟／小时／天」，"
                    "换算后须为1—41760整分钟，默认41760分钟（29天）。"
                    "仅影响之后的群内拉黑和真实重入，不追罚旧名单或重设已有禁言；普通禁言24小时上限不变。"
                )
            operation, options = "settings_preview", {"minutes": minutes}
        elif not event.is_private_chat() and action in {"黑名单", "移除黑名单"}:
            operation = "direct"
            options["mode"] = "add" if action == "黑名单" else "remove"
            if (
                action == "移除黑名单"
                and not payload.get("reply")
                and not payload.get("mentions")
            ):
                if not argument:
                    return "回复成员原消息或真正@一人，发送「移除黑名单」；已离群成员可按名称搜索。"
                operation = "search"
                options["query"] = argument
        elif action in {"添加黑名单", "移除黑名单"}:
            if not argument:
                return f"请发送「{action} 成员名称」，搜索后选择并确认。"
            if not event.is_private_chat() and action == "添加黑名单":
                return "群内请回复成员原消息，或从客户端真正@一人，发送「黑名单」。"
            operation = "search"
            options = {
                "mode": "add" if action == "添加黑名单" else "remove",
                "query": argument,
            }
        elif action == "选择黑名单":
            if not argument.isascii() or not argument.isdecimal():
                return "先搜索成员，再发送「选择黑名单 搜索结果序号」。"
            operation = "preview"
            options = {
                "selection": self.chat_search.get(session, ""),
                "index": int(argument),
            }
        elif action == "确认黑名单":
            operation, options = "commit", {"token": argument}
        result = await self.run(
            event.platform,
            group,
            operation,
            actor=event.actor_identity.principal,
            session=session,
            event=event,
            **options,
        )
        if operation in {"read", "search"}:
            lines = [
                f"{n}. {row['name']} · UID {row['id']}"
                + (
                    f" · 每次重进禁言{result['mute_minutes']}分钟"
                    + (f" · {row['status']}" if row.get("status") else "")
                    if operation == "read"
                    else ""
                )
                for n, row in enumerate(result["members"], 1)
            ]
            return (
                ("【本群黑名单】\n" if operation == "read" else "【本群成员搜索】\n")
                + ("\n".join(lines) or "暂无匹配成员。")
                + (
                    f"\n仅当前机器人和本群有效；群内真实@或回复成员消息发送「黑名单」，加入后立即禁言{result['mute_minutes']}分钟。每次可信真实重新入群也按本群时长禁言，不踢人。"
                    "\n可「黑名单禁言时长 N分钟／小时／天」预览后本人确认，范围1—41760整分钟，默认41760分钟（29天）。"
                    "\n修改时长不追罚旧名单或重设已有禁言，仅影响之后的群内拉黑和真实重入；普通禁言24小时上限不变。"
                    "\n移除不自动解禁；解禁不会移除黑名单。私信选群后可「添加黑名单 名称」。"
                    if operation == "read"
                    else "\n发送「选择黑名单 序号」，核对后确认。"
                )
            )
        if operation == "settings_preview":
            return (
                f"预览：本群黑名单重入禁言时长改为{result['mute_minutes']}分钟\n"
                "群内真实@或回复发送「黑名单」立即禁言；每次可信真实重新入群也禁言，不踢人。\n"
                "仅影响之后的群内拉黑和真实重入，不追罚旧名单或重设已有禁言；普通禁言24小时上限不变。\n"
                f"请在本对话发新消息：确认黑名单 {result['token']}\n十分钟有效。"
            )
        if operation == "preview":
            return (
                f"预览：{'加入' if result['mode'] == 'add' else '移出'}本群黑名单\n"
                f"{result['member']['name']} · UID {result['member']['id']}\n"
                f"加入不立即处罚；每次可信真实重新入群禁言{result['mute_minutes']}分钟，不踢人。移除不自动解禁。\n"
                f"请在本对话发新消息：确认黑名单 {result['token']}\n十分钟有效。"
            )
        if result["mode"] == "settings":
            return (
                f"已保存本群黑名单重入禁言时长：{result['mute_minutes']}分钟。"
                "仅影响之后的群内拉黑和真实重入，不追罚旧名单或重设已有禁言；普通禁言24小时上限不变。"
            )
        if result["mode"] == "add" and operation == "direct":
            status = result["enforcement"]["status"]
            outcome = {
                "verified": f"已核实禁言{result['mute_minutes']}分钟。",
                "accepted": f"平台已受理禁言{result['mute_minutes']}分钟；实际到期时间以平台为准。",
                "rejected": "本次禁言被拒绝，未成功；请检查机器人管理权限和处罚记录。",
                "duplicate_or_unresolved": "该命令已处理或已有待核对处罚，本次未重复提交；请查看处罚记录。",
            }.get(status, "禁言结果待核对，未自动重试；请查看处罚记录。")
            return (
                f"已加入本群黑名单：{result['member']}。{outcome}"
                "以后真实重新入群也按本群时长禁言，不踢人。移除黑名单不会自动解禁。"
            )
        return (
            f"已{'加入' if result['mode'] == 'add' else '移出'}本群黑名单：{result['member']}。"
            + (
                f"当前不立即处罚；每次可信真实重新入群禁言{result['mute_minutes']}分钟，不踢人。"
                if result["mode"] == "add"
                else "已有禁言不自动解除，需单独解禁。"
            )
        )

    async def receive(self, adapter, group, message, notification):
        """Durably enqueue authenticated join metadata before the native ACK.

        Args:
            adapter: Original receiving transport.
            group: Server-resolved business group.
            message: Original native server message ID.
            notification: Strictly parsed native join notification.
        """
        stamp = notification.get("event_at")
        tenant = self.service.tenants.get((str(adapter.account), group), {})
        if (
            type(stamp) is not int
            or not 0 < stamp <= int((time.time() + 60) * 1000)
            or len(str(stamp)) != 13
            or not tenant
            or not tenant.get("active")
            or tenant.get("paused")
            or self.service.settings["paused"]
        ):
            return
        guard = account_guard(adapter)
        store = await asyncio.to_thread(BlacklistStore, adapter.config["id"])
        await asyncio.to_thread(
            store.observe,
            str(adapter.account),
            group,
            message,
            notification["member_peers"],
            stamp / 1000,
            guard=guard,
            scope=self.notice_scope(adapter, tenant),
        )
        self.service.membership_wakeup.set()

    def tick(self):
        """Start bounded workers without blocking discovery or message handling."""
        for key, task in list(self.tasks.items()):
            if task.done():
                if not task.cancelled():
                    task.exception()
                self.tasks.pop(key, None)
        if not self.service.active or self.service.settings["paused"]:
            return
        for adapter in self.service.adapters():
            account = str(adapter.account)
            if (
                account in self.tasks
                or adapter.connection_state != "online"
                or time.monotonic() - self.last_poll.get(account, -3) < 3
            ):
                continue
            self.last_poll[account] = time.monotonic()
            self.tasks[account] = asyncio.create_task(self.process(adapter))

    async def process(self, adapter, *, notice=None):
        """Corroborate each real admission, then submit at most one durable intent.

        Args:
            adapter: Current authenticated receiving account.
        """
        service, account = self.service, str(adapter.account)
        frozen = account_guard(adapter)
        try:
            store = await asyncio.to_thread(BlacklistStore, adapter.config["id"])
            if notice is None:
                notices = await asyncio.to_thread(store.pending, account)
                for item in notices[:20]:
                    await self.process(adapter, notice=item)
                return
            notices = [notice]
            for notice in notices[:20]:
                group = notice["group"]
                tenant = dict(service.tenants.get((account, group), {}))
                if (
                    not tenant
                    or notice.get("scope") != self.notice_scope(adapter, tenant)
                    or service.settings["paused"]
                    or tenant.get("paused")
                ):
                    await asyncio.to_thread(
                        store.finish_notice,
                        account,
                        group,
                        notice["message"],
                        "revoked",
                    )
                    continue
                version, entries = await asyncio.to_thread(store.read, account, group)
                if not entries:
                    await asyncio.to_thread(
                        store.finish_notice,
                        account,
                        group,
                        notice["message"],
                        "ignored",
                    )
                    continue
                members = await service.roster(adapter, group)
                frozen()
                await adapter.get_moderation_members(group)
                frozen()
                base = service.write_guard(
                    adapter, group=group, tenant=tenant, members=members, control=True
                )

                def guard():
                    base()
                    if (
                        service.settings["paused"]
                        or tenant["paused"]
                        or service.store.tenant(account, group) != tenant
                    ):
                        raise OpenError("blacklist_changed")

                guard()
                events = latest_membership_events(
                    await adapter.business.request("/v1/group/get-apply-logs", {}),
                    group,
                )
                guard()
                peers = set(notice["peers"])
                for entry in entries:
                    uid = str(entry.get("member", entry.get("id")))
                    row, evidence = members.get(uid, {}), events.get(uid)
                    if (
                        entry["peer"] not in peers
                        or not evidence
                        or evidence["ambiguous"]
                        or evidence["row"].get("state") != "MEMBER_STATE_GOOD"
                        or evidence["row"].get("groupApplyType")
                        not in {"INVITATION", "APPLY"}
                        or str(evidence["row"]["applicant"].get("nimId", ""))
                        != entry["peer"]
                        or row.get("groupRole") != "GROUP_ROLE_MEMBER"
                        or str(row.get("nimId", "")) != entry["peer"]
                        or uid == account
                        or protected_target(
                            adapter,
                            group,
                            uid,
                            entry["peer"],
                            fixture=getattr(service, "_blacklist_fixture", None),
                        )
                        or abs(evidence["timestamp"] - notice["event_at"]) > 120
                    ):
                        continue
                    join_id = str(evidence["row"]["applyId"])

                    intent = None
                    fixture = (
                        getattr(service, "_blacklist_fixture", None)
                        if is_managed_account(uid)
                        else None
                    )

                    def submission_guard(entry=entry, uid=uid):
                        guard()
                        latest = service.member_evidence.get((account, group), {})
                        current_target = latest.get(uid, {})
                        creator_uid = entry.get("actor", "")
                        creator_peer = entry.get("actor_peer", "")
                        creator = latest.get(creator_uid, {})
                        if (
                            str(current_target.get("nimId", "")) != entry["peer"]
                            or current_target.get("groupRole") != "GROUP_ROLE_MEMBER"
                            or creator_uid
                            and not creator_uid.startswith("dashboard/")
                            and (
                                creator_uid not in tenant.get("managers", [])
                                or str(creator.get("nimId", "")) != creator_peer
                                or creator.get("groupRole")
                                not in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
                            )
                        ):
                            raise OpenError("blacklist_permission")

                    async def authorize(entry=entry, uid=uid, join_id=join_id):
                        guard()
                        fresh = await service.roster(adapter, group)
                        guard()
                        target = fresh.get(uid, {})
                        if (
                            str(target.get("nimId", "")) != entry["peer"]
                            or target.get("groupRole") != "GROUP_ROLE_MEMBER"
                            or protected_target(
                                adapter,
                                group,
                                uid,
                                entry["peer"],
                                fixture=getattr(service, "_blacklist_fixture", None),
                                intent=intent,
                            )
                        ):
                            raise OpenError("blacklist_protected")
                        creator_uid = entry.get("actor", "")
                        creator_peer = entry.get("actor_peer", "")
                        if creator_uid and not creator_uid.startswith("dashboard/"):
                            source = fresh.get(creator_uid, {})
                            current = service.tenants.get((account, group), {})
                            if (
                                creator_uid not in current.get("managers", [])
                                or str(source.get("nimId", "")) != creator_peer
                                or source.get("groupRole")
                                not in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
                            ):
                                raise OpenError("blacklist_permission")
                        current_events = latest_membership_events(
                            await adapter.business.request(
                                "/v1/group/get-apply-logs", {}
                            ),
                            group,
                        )
                        guard()
                        current = current_events.get(uid)
                        if (
                            not current
                            or current["ambiguous"]
                            or str(current["row"]["applyId"]) != join_id
                            or current["row"].get("state") != "MEMBER_STATE_GOOD"
                        ):
                            raise OpenError("blacklist_join_changed")
                        submission_guard()

                    await authorize()
                    intent = await asyncio.to_thread(
                        store.reserve,
                        account,
                        group,
                        uid,
                        entry["peer"],
                        join_id,
                        evidence["timestamp"],
                        guard=guard,
                    )
                    if intent is None:
                        continue
                    result = {"status": "unknown"}
                    inspect_native = False
                    try:
                        request = ActionRequest.bind(
                            adapter,
                            "blacklist/" + intent.id,
                            intent.action,
                            int(group),
                            member=int(uid),
                            minutes=intent.minutes,
                            expected_nim=entry["peer"],
                            blacklist_intent=intent,
                            blacklist_fixture=fixture,
                            blacklist_guard=submission_guard,
                        )
                        result = await adapter.execute_action(
                            request, authorize=authorize
                        )
                    except BaseException as exc:
                        if getattr(exc, "submitted", None) is False:
                            result = {"status": "rejected", "submitted": False}
                        else:
                            inspect_native = True
                        raise
                    finally:

                        def finish_intent():
                            receipt = result
                            if inspect_native and receipt.get("status") not in {
                                "accepted",
                                "verified",
                            }:
                                # Native writes persist this ID first. Missing files
                                # or schema also prove absence without external deletion.
                                try:
                                    path = store.path.with_name("moderation.sqlite3")
                                    try:
                                        path.stat()
                                    except FileNotFoundError:
                                        absent = True
                                    else:
                                        with closing(
                                            sqlite3.connect(
                                                path.resolve().as_uri() + "?mode=ro",
                                                uri=True,
                                            )
                                        ) as db:
                                            table = db.execute(
                                                "SELECT 1 FROM sqlite_master "
                                                "WHERE type='table' AND name='operations'"
                                            ).fetchone()
                                            absent = (
                                                table is None
                                                or db.execute(
                                                    "SELECT 1 FROM operations WHERE id=?",
                                                    (intent.operation,),
                                                ).fetchone()
                                                is None
                                            )
                                    if absent:
                                        receipt = {
                                            "status": "rejected",
                                            "submitted": False,
                                        }
                                except (sqlite3.Error, OSError):
                                    pass
                            store.done(intent, receipt)

                        await asyncio.shield(asyncio.to_thread(finish_intent))
                await asyncio.to_thread(
                    store.finish_notice, account, group, notice["message"], "processed"
                )
        except asyncio.CancelledError:
            raise
        except (OpenError, ProtocolError, OSError) as exc:
            adapter.diagnostics.emit(
                "member_blacklist",
                "paused",
                failed=True,
                error=str(exc)[:128],
            )

    async def close(self):
        """Stop native workers before closing the underlying service."""
        self.pending.clear()
        self.chat_search.clear()
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
