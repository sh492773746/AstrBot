"""Native, fixed-command editing of private and exact-group knowledge."""

import asyncio
import hashlib
import re
import sqlite3

from astrbot.core import logger
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity

from .knowledge_store import MAX_BYTES, MAX_ENTRIES, KnowledgeScope, KnowledgeStore
from .open_store import OpenError
from .policy import require_group_admin
from .syntax import GROUP_KNOWLEDGE_COMMANDS, PERSONAL_KNOWLEDGE_COMMANDS

READ_COMMANDS = {
    "我的知识",
    "查看我的知识",
    "我的知识容量",
    "本群知识",
    "查看群知识",
    "本群知识容量",
    "本群知识范围",
}
REPLIES = {
    "knowledge_private": "个人知识只在私聊中查看和编辑，请私信我发送「菜单」，选择8，再选择5。",
    "knowledge_denied": "无权限：只能管理自己获授权的群知识，请先私聊选择对应群。",
    "knowledge_changed": "知识内容、当前群或你的权限已变化，没有修改。请重新查看后再选择。",
    "knowledge_number": "没有这个资料序号，请先查看知识列表。",
    "knowledge_invalid": "格式不正确，没有保存。请填写：标题 | 正文；标题最多80字，每条合计不超过8KiB。",
    "knowledge_capacity": "知识容量已满，没有保存。每个空间最多100条、合计1MiB；全站也有容量保护，请先清理资料。",
    "knowledge_busy": "知识服务正在处理其他操作，请稍后再试。本次没有修改或发起AI调用。",
}


class Knowledge:
    """Share one bounded text index without exposing AstrBot's global KB authority."""

    def __init__(self, service):
        self.service = service
        self.store = KnowledgeStore(service.store)
        self.worker = asyncio.Lock()

    async def work(self, operation, *args, **kwargs):
        """Keep the single-worker lease until a cancelled SQLite worker exits.

        Args:
            operation: Bounded synchronous storage operation.
            args: Storage operation positional arguments.
            kwargs: Storage operation keyword arguments.

        Returns:
            The completed worker result.
        """
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise

    async def context(self, event, action):
        """Derive ownership and fresh authority from the actual native caller.

        Args:
            event: Transport-authenticated command event.
            action: Fixed personal or group knowledge command.

        Returns:
            Scope and a revocation-aware transaction guard.

        Raises:
            OpenError: For unavailable transport or missing exact-group authority.
        """
        service = self.service
        identity = getattr(event, "actor_identity", None)
        if (
            not isinstance(identity, ActorIdentity)
            or identity.account != str(event.platform.account)
            or identity.claimed_uid != str(event.get_sender_id())
        ):
            raise OpenError("knowledge_denied")
        principal = identity.principal
        account = identity.account
        frozen = service.write_guard(event.platform, event)
        if action in PERSONAL_KNOWLEDGE_COMMANDS:
            if not event.is_private_chat():
                raise OpenError("knowledge_private")
            scope = KnowledgeScope("personal", owner=principal)
        elif action in GROUP_KNOWLEDGE_COMMANDS:
            if action not in READ_COMMANDS:
                raise OpenError("group_knowledge_server_owned")
            if event.is_private_chat():
                await service.verify_actor(event)
            identity = event.actor_identity
            identity.require_verified()
            group = event.get_group_id() or service.navigation.stamp(event)[3]
            if not group or not service.management.allowed(event, group):
                raise OpenError("knowledge_denied")
            require_group_admin(event.platform.config, group)
            tenant = dict(service.tenants[(account, group)])
            members = await service.roster(event.platform, group)
            frozen()
            if (
                str(members.get(identity.verified_uid, {}).get("nimId", ""))
                != identity.peer
                or str(members.get(account, {}).get("nimId", ""))
                != str(event.platform.nim_account)
                or members.get(account, {}).get("groupRole")
                not in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
            ):
                raise OpenError("knowledge_denied")
            service.member_evidence[(account, group)] = members
            frozen = service.write_guard(
                event.platform, event, group=group, tenant=tenant, members=members
            )
            scope = KnowledgeScope("group", account=account, group=group)
            bind_reply = getattr(event, "bind_management_reply_guard", None)
            if callable(bind_reply):
                bind_reply(service.management, (group,))
        else:
            raise OpenError("knowledge_invalid")

        def guard():
            frozen()
            current = getattr(event, "actor_identity", None)
            if (
                not isinstance(current, ActorIdentity)
                or current.account != account
                or current.claimed_uid != str(event.get_sender_id())
                or current.principal != principal
                or service.settings["paused"]
            ):
                raise OpenError("knowledge_denied")
            if scope.kind == "group":
                with service.store.connect() as db:
                    saved = db.execute(
                        "SELECT * FROM tenants WHERE account=? AND group_id=?",
                        (account, scope.group),
                    ).fetchone()
                    if not saved or (saved["version"], saved["epoch"]) != (
                        tenant["version"],
                        tenant["epoch"],
                    ):
                        raise OpenError("knowledge_changed")
                    service.store.management_guard(db, saved, identity.verified_uid)

        guard()
        return scope, guard

    async def capture(self, event, command):
        """Capture an exact-space version before requesting editable user text.

        Args:
            event: Actual caller and selected group.
            command: Fixed knowledge command.

        Returns:
            Immutable scope/version snapshot for the subsequent preview.
        """
        action = command.split(" ", 1)[0]
        scope, guard = await self.context(event, action)
        if self.worker.locked():
            raise OpenError("knowledge_busy")
        try:
            async with self.worker:
                data = await self.work(self.store.read, scope, guard=guard)
        except sqlite3.OperationalError:
            raise OpenError("knowledge_busy") from None
        return data["snapshot"]

    async def command(self, event, command, *, expected=None, state=None):
        """Read own data or apply an explicitly confirmed fixed menu mutation.

        Args:
            event: Native caller; no model-generated operation is accepted.
            command: Fixed action plus optional bounded content.
            expected: Generation captured before the edit prompt or toggle preview.
            state: Owning numeric-menu session, required for every write.

        Returns:
            Bounded native reply; errors expose no unrelated user's data.
        """
        try:
            action, _, argument = command.partition(" ")
            scope, frozen = await self.context(event, action)
            if expected is not None and expected.scope != scope:
                raise OpenError("knowledge_changed")
            if self.worker.locked():
                raise OpenError("knowledge_busy")
            async with self.worker:
                data = await self.work(self.store.read, scope, guard=frozen)
                if expected is not None and expected != data["snapshot"]:
                    raise OpenError("knowledge_changed")
                if action in READ_COMMANDS:
                    entries = data["entries"]
                    label = "我的知识库" if scope.kind == "personal" else "本群知识库"
                    if action.endswith("容量"):
                        return (
                            f"【{label} · 容量】\n"
                            f"条目：{len(entries)}/{MAX_ENTRIES}\n"
                            f"文本：{data['bytes']}/{MAX_BYTES}字节（1MiB）\n"
                            "仅支持文本资料；保存和索引不调用AI，不消耗AI次数。"
                        )
                    if action == "本群知识范围":
                        return (
                            f"【{label} · 使用范围】\n"
                            f"@回复引用：{'开启' if data['mentions'] else '关闭'}\n"
                            f"主动热群引用：{'开启' if data['idle'] else '关闭'}\n"
                            "仅使用本群资料，不读取管理者的个人知识。"
                        )
                    if action.startswith("查看"):
                        if not re.fullmatch(
                            r"[1-9][0-9]{0,2}", argument
                        ) or not 1 <= int(argument) <= len(entries):
                            raise OpenError("knowledge_number")
                        row = entries[int(argument) - 1]
                        return (
                            f"【{label} · {argument}】\n{row['title']}\n{row['body']}"
                        )
                    page = (
                        int(argument) if re.fullmatch(r"[1-9][0-9]?", argument) else 1
                    )
                    if argument and not re.fullmatch(r"[1-9][0-9]?", argument):
                        raise OpenError("knowledge_number")
                    pages = max(1, (len(entries) + 4) // 5)
                    if page > pages:
                        raise OpenError("knowledge_number")
                    start = (page - 1) * 5
                    listing = "\n".join(
                        f"{i}. {r['title']}\n{r['body'][:160]}"
                        for i, r in enumerate(entries[start : start + 5], start + 1)
                    )
                    return (
                        f"【{label} · {page}/{pages}页】\n"
                        + (listing or "暂无资料。")
                        + "\n"
                        + (
                            "仅用于本人私聊；不会自动公开给群。"
                            if scope.kind == "personal"
                            else "资料仅属于本群，引用范围需另行开启。"
                        )
                        + "\n查看全文："
                        + ("查看我的知识" if scope.kind == "personal" else "查看群知识")
                        + " 序号；翻页："
                        + ("我的知识" if scope.kind == "personal" else "本群知识")
                        + " 页码。"
                    )
                if expected is None or state is None:
                    return (
                        "请发送「菜单」，选择"
                        + ("8 → 5" if scope.kind == "personal" else "9 → 7")
                        + "，预览后确认修改；直接发送文字不会修改知识库。"
                    )
                message = state.message
                stamp = state.stamp

                def guard():
                    frozen()
                    if (
                        state.closed
                        or state.message != message
                        or service_key != self.service.navigation.key(event)
                        or stamp != self.service.navigation.stamp(event)
                    ):
                        raise OpenError("knowledge_changed")

                service_key = self.service.navigation.key(event)
                operation = (
                    "add"
                    if action.startswith("添加")
                    else "edit"
                    if action.startswith("修改")
                    else "delete"
                    if action.startswith("删除")
                    else ""
                )
                fields = {}
                if operation in {"edit", "delete"}:
                    number, _, argument = argument.partition(" ")
                    if not re.fullmatch(r"[1-9][0-9]{0,2}", number):
                        raise OpenError("knowledge_number")
                    fields["number"] = int(number)
                if operation in {"add", "edit"}:
                    title, separator, body = argument.partition("|")
                    if not separator:
                        raise OpenError("knowledge_invalid")
                    fields.update(title=title.strip(), body=body.strip())
                elif operation == "delete" and argument:
                    raise OpenError("knowledge_invalid")
                elif action == "群知识引用":
                    modes = {
                        "回复 开启": ("mentions", True),
                        "回复 关闭": ("mentions", False),
                        "热群 开启": ("idle", True),
                        "热群 关闭": ("idle", False),
                    }
                    if argument not in modes:
                        raise OpenError("knowledge_invalid")
                    operation, enabled = modes[argument]
                    fields["enabled"] = enabled
                request = hashlib.sha256(
                    f"knowledge/{event.actor_identity.account}/{event.actor_identity.principal}/"
                    f"{event.message_obj.message_id}".encode()
                ).hexdigest()
                return await self.work(
                    self.store.change,
                    expected,
                    operation,
                    fields,
                    request,
                    event.actor_identity.principal,
                    guard=guard,
                )
        except OpenError as exc:
            if str(exc) in REPLIES:
                return REPLIES[str(exc)]
            raise
        except sqlite3.OperationalError:
            logger.warning("Wangshangliao knowledge storage operation was unavailable")
            return REPLIES["knowledge_busy"]
