"""Native group text events with explicit reply eligibility."""

import asyncio
import inspect
import re

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Plain, Reply

from .diagnostics import Diagnostics
from .identity import ActorIdentity
from .text import plain_text, redact_reply
from .wire import ProtocolError, unb64


def is_managed_account(account: str) -> bool:
    """Check whether a sender belongs to a configured Wangshangliao bot.

    Args:
        account: Business account ID, not a NIM transport ID.

    Returns:
        Whether the account is managed locally, including disabled bots.
    """
    from astrbot.core import astrbot_config

    return bool(account) and any(
        item.get("type") == "wangshangliao"
        and str(item.get("account_id", "")) == str(account)
        for item in astrbot_config.get("platform", [])
    )


def mentioned(text: str, own_id: str, mentions: list[str]) -> bool:
    """Match only the authenticated account's NIM mention identity.

    Args:
        text: Display text, deliberately not used as identity evidence.
        own_id: NIM identity from the authenticated session, not a business ID.
        mentions: Structured NIM identities from the platform message.

    Returns:
        Whether this account was explicitly mentioned.
    """
    return bool(own_id) and own_id in mentions


def managed_group_control(platform, group: str, payload: dict) -> str:
    """Admit only explicit blacklist controls from a bound group manager account.

    This is transport admission, not mutation authority. The command handler
    still verifies the current complete roster and the exact target.
    """
    from .group_scope import group_revision
    from .runtime_policy import runtime

    config = platform.config
    if (
        not isinstance(payload, dict)
        or not isinstance(group, str)
        or group.startswith("private/")
        or not config.get("_open_integrated")
        or config.get("_open_paused")
        or config.get("_business_active") is not True
        or not config.get("enable", True)
        or group not in config.get("_open_groups", {})
        or not config.get("reply_groups", {}).get(group, True)
        or payload.get("kind")
    ):
        return ""
    sender = payload.get("sender")
    route = payload.get("recall_route")
    if (
        not isinstance(sender, str)
        or sender == str(platform.account)
        or sender not in config.get("_open_managers", {}).get(group, ())
        or not isinstance(route, dict)
        or not isinstance(route.get("peer"), str)
        or not route["peer"]
        or platform.members.get(group, {}).get(sender) != route["peer"]
        or payload.get("_open_revision") != group_revision(config, group)
    ):
        return ""
    text = payload.get("text")
    if not isinstance(text, str):
        return ""
    command = runtime.recognize(text)
    if (
        not isinstance(command, str)
        or not command
        or command.split(maxsplit=1)[0] not in {"黑名单", "移除黑名单", "黑名单列表"}
    ):
        return ""
    return command


class WangshangliaoEvent(AstrMessageEvent):
    """Waitable event whose replies carry an incoming-message idempotency key."""

    def __init__(self, message, platform, eligible: bool):
        super().__init__(
            message.message_str, message, platform.meta(), message.session_id
        )
        self.platform = platform
        raw = getattr(message, "raw_message", None)
        raw = raw if isinstance(raw, dict) else {}
        peer = raw.get("_transport_peer", "")
        if not isinstance(peer, str):
            peer = ""
        try:
            if len(peer.encode()) > 1024:
                peer = ""
        except UnicodeError:
            peer = ""
        if not peer and self.is_private_chat():
            try:
                peer = unb64(message.session_id.split("/", 3)[3], 1024).decode()
            except (IndexError, UnicodeError, ProtocolError):
                pass
        self.actor_identity = ActorIdentity(
            str(getattr(platform, "account", message.self_id)),
            str(message.sender.user_id),
            peer,
            str(message.sender.user_id) if peer and not self.is_private_chat() else "",
        )
        config = getattr(platform, "config", {})
        self.policy_epoch = config.get("_business_epoch")
        allowed = (
            config.get("reply_private", True)
            if self.is_private_chat()
            else config.get("reply_groups", {}).get(self.get_group_id(), True)
        )
        self.eligible = eligible and allowed
        self.processing_completion = asyncio.get_running_loop().create_future()
        self.set_extra("_context_only", not self.eligible)
        self.reply_index = 0

    def is_admin(self) -> bool:
        """Require a verified transport binding for framework permissions.

        Returns:
            Whether the current role has independently verified native identity.
        """
        if not super().is_admin():
            return False
        actor = self.actor_identity
        return (
            bool(actor.peer)
            and actor.account == str(self.platform.account)
            and actor.claimed_uid == str(self.get_sender_id())
            and actor.verified_uid == actor.claimed_uid
        )

    def is_ai_admin(self, context) -> bool:
        """Recheck private management authority against the effective profile.

        Args:
            context: Framework context used for current session configuration.

        Returns:
            Whether this live event retains private administrator authority.
        """
        config = self.platform.config
        return (
            config.get("_business_active") is not False
            and self.policy_epoch == config.get("_business_epoch")
            and self.is_private_chat()
            and self.is_admin()
            and self.actor_identity.verified_uid == str(self.get_sender_id())
            and self.get_extra("_api_key_allow_admin_role") is not False
            and str(self.get_sender_id())
            in {
                str(uid)
                for uid in context.get_config(self.unified_msg_origin).get(
                    "admins_id", []
                )
            }
        )

    def get_ai_conversation_origin(self, context) -> str:
        """Separate role histories without changing the native reply address.

        Args:
            context: Framework context providing the current authorization.

        Returns:
            Persistent conversation key scoped to account, session and role.
        """
        role = "admin" if self.is_ai_admin(context) else "customer"
        return f"{self.unified_msg_origin}/wsl-ai/{self.platform.account}/{role}"

    def authorize_ai_tool(self, tool, context) -> bool:
        """Allow native management, sender-only invitations and scoped knowledge.

        Args:
            tool: Actual tool object, not a model-supplied tool name.
            context: Framework context used to recheck authority at execution.

        Returns:
            Whether the concrete tool may run for the current event.
        """
        from .runtime_policy import runtime

        if runtime.open_service is not None:
            return False
        from astrbot.builtin_stars.wangshangliao_moderation.private_invitation import (
            MyInvitationsTool,
            private_query_guard,
        )
        from astrbot.core.tools.knowledge_base_tools import KnowledgeBaseQueryTool

        config = self.platform.config
        if config.get("_business_active") is False or self.policy_epoch != config.get(
            "_business_epoch"
        ):
            return False
        if type(tool) is KnowledgeBaseQueryTool:
            return tool.name == "astr_kb_search" and not tool.is_background_task
        if type(tool) is MyInvitationsTool:
            if (
                tool.name != "wsl_my_invitations"
                or not tool.active
                or tool.handler is not None
                or tool.is_background_task
            ):
                return False
            try:
                private_query_guard(self)
                return True
            except ProtocolError:
                return False
        return (
            self.is_ai_admin(context)
            and getattr(tool, "name", "") == "wsl_private_management"
            and getattr(tool, "handler_module_path", "")
            == "astrbot.builtin_stars.wangshangliao_moderation.main"
            and not getattr(tool, "is_background_task", False)
        )

    def uses_independent_rate_limit(self) -> bool:
        """Keep the public dispatcher out of framework-wide chat stall queues.

        Its own AI concurrency/budgets, command cooldowns and moderation limits
        remain authoritative. Legacy adapters retain the shared chat limiter.
        """
        from .runtime_policy import runtime

        service = runtime.open_service
        config = self.platform.config
        return (
            service is not None
            and getattr(service, "integrated", False) is True
            and getattr(service, "active", False) is True
            and config.get("_open_integrated") is True
            and config.get("_business_active") is True
            and self.policy_epoch == config.get("_business_epoch")
        )

    async def prepare_waking_check(self, config: dict, handler_registry) -> bool:
        """Keep native private-command authorization outside the shared pipeline.

        Args:
            config: Effective session profile, not the global administrator list.
            handler_registry: Active filters including third-party command aliases.

        Returns:
            False after a bounded denial, otherwise True for normal waking checks.
        """
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.filter.command_group import CommandGroupFilter
        from astrbot.core.star.star_handler import EventType

        from .runtime_policy import runtime

        if runtime.open_service is not None:
            # Public events are consumed by the isolated command/chat dispatcher.
            # Framework aliases and administrator tools are never an entry point.
            self.role = "member"
            self.set_extra("_context_only", True)
            return True
        admin = (
            str(self.get_sender_id())
            in {str(uid) for uid in config.get("admins_id", [])}
            and self.get_extra("_api_key_allow_admin_role") is not False
            and (
                not self.is_private_chat()
                or self.actor_identity.verified_uid == str(self.get_sender_id())
            )
        )
        self.role = "admin" if admin else "member"
        if not self.is_private_chat():
            return True
        text = self.message_str.strip()
        own_query = runtime.recognize(text)
        if own_query in {"私聊菜单", "我的邀请"} or re.fullmatch(
            r"我的邀请 (?:[1-9][0-9]?|100)", own_query or ""
        ):
            return True
        if (
            admin
            and self.get_extra("wsl_test_scope") == "private_commands"
            and text in {"/new", "/stats"}
        ):
            self.set_extra("wsl_plain_command", True)
            self.set_extra("wsl_command_result", True)
        candidates = [text] + [
            text[len(prefix) :].strip()
            for prefix in config.get("wake_prefix", [])
            if prefix and text.startswith(prefix)
        ]
        command = bool(runtime.recognize(text)) or any(
            candidate.startswith("/") for candidate in candidates
        )
        for handler in handler_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        ):
            for item in handler.event_filters:
                if isinstance(item, (CommandFilter, CommandGroupFilter)):
                    command = command or any(
                        candidate == name
                        or (
                            candidate.startswith(name)
                            and candidate[len(name) : len(name) + 1].isspace()
                        )
                        for name in item.get_complete_command_names()
                        for candidate in candidates
                    )
        if not command or admin:
            return True
        if not self.get_extra("wsl_permission_denial"):
            self.set_extra("wsl_permission_denial", True)
            self.stop_event()
            await self.send(MessageChain().message("无权限"))
        else:
            self.stop_event()
        return False

    async def handle_builtin_help(self) -> bool:
        """Delegate native help only to the currently active business extension.

        Returns:
            True, including silent handling when the extension is unavailable.
        """
        from .runtime_policy import runtime

        try:
            if runtime.service and runtime.service.active and runtime.help_responder:
                await runtime.help_responder(self)
        finally:
            self.stop_event()
        return True

    def _log_reply(self, outcome: str, stage: str = "reply_gate") -> None:
        Diagnostics(getattr(self.platform, "config", {}).get("id", "unknown")).emit(
            stage,
            outcome,
            str(self.message_obj.message_id),
            route="private" if self.is_private_chat() else "group",
            group=self.get_group_id(),
            actor=self.get_sender_id(),
            session=self.unified_msg_origin,
            failed=stage == "reply_result" and outcome not in {"accepted", "verified"},
            error="result_unknown"
            if stage == "reply_result" and outcome == "unknown"
            else "",
            aggregate=stage != "reply_result",
        )

    def bind_management_reply_guard(self, management, groups):
        """Freeze the actual data scope before producing a private query result.

        Args:
            management: Grant service that authorized the result.
            groups: Exact groups read by the command, never a later selection.
        """
        from .database import account_guard
        from .group_scope import group_revision
        from .runtime_policy import runtime

        if not self.is_private_chat() or management is None:
            return
        adapter = self.platform
        actor, session = self.actor_identity, self.session_id
        service = getattr(management, "service", None)
        frozen = account_guard(adapter)
        previous = self.get_extra("_wsl_management_delivery_guard")
        versions = {
            group: (
                management.versions.get(management.key(adapter, group)),
                group_revision(adapter.config, group),
                adapter.members.get(group, {}).get(actor.claimed_uid),
                service.tenants.get((str(adapter.account), group), {}).get(
                    "native_role"
                )
                if service is not None
                else None,
                adapter.config.get("_open_native_roles", {}).get(group),
            )
            for group in groups
        }

        def check():
            frozen()
            if callable(previous):
                previous()
            if (
                service is not None
                and (runtime.open_service is not service or service.capacity_paused)
                or self.platform is not adapter
                or self.actor_identity != actor
                or self.session_id != session
                or not actor.peer
                or actor.verified_uid != actor.claimed_uid
                or not management.allowed(self)
            ):
                raise ProtocolError("management_reply_scope_changed")
            for group, (
                version,
                revision,
                peer,
                native_role,
                configured_role,
            ) in versions.items():
                tenant = (
                    service.tenants.get((str(adapter.account), group), {})
                    if service is not None
                    else {}
                )
                current_peer = adapter.members.get(group, {}).get(actor.claimed_uid)
                if (
                    not management.allowed(self, group)
                    or management.versions.get(management.key(adapter, group))
                    != version
                    or group_revision(adapter.config, group) != revision
                    or tenant.get("native_role") != native_role
                    or adapter.config.get("_open_native_roles", {}).get(group)
                    != configured_role
                    or peer is not None
                    and current_peer != peer
                    or current_peer is not None
                    and current_peer != actor.peer
                ):
                    raise ProtocolError("management_reply_scope_changed")

        self.set_extra("_wsl_management_delivery_guard", check)

    async def send(self, message: MessageChain) -> None:
        """Send only text replies from explicitly mentioned, active events."""
        self.set_extra("_wsl_native_reply_result", "not_sent")
        config = getattr(self.platform, "config", {})
        delivery_guard = self.get_extra("_wsl_invitation_delivery_guard")
        management_guard = self.get_extra("_wsl_management_delivery_guard")
        guards = [
            guard for guard in (delivery_guard, management_guard) if callable(guard)
        ]

        async def authorize_reply():
            for guard in guards:
                checked = guard()
                if inspect.isawaitable(checked):
                    await checked

        if guards:
            try:
                await authorize_reply()
            except ProtocolError:
                self._log_reply(
                    "management_reply_scope_changed"
                    if management_guard is not None
                    else "invitation_query_scope_changed"
                )
                return
        if config.get("_business_active") is False or self.policy_epoch != config.get(
            "_business_epoch"
        ):
            self._log_reply("business_suspended")
            return
        allowed = (
            config.get("reply_private", True)
            if self.is_private_chat()
            else config.get("reply_groups", {}).get(self.get_group_id(), True)
        )
        permission_denial = self.is_private_chat() and self.get_extra(
            "wsl_permission_denial"
        )
        scoped_permission_denial = (
            permission_denial and self.get_extra("wsl_test_scope") == "private_commands"
        )
        command_reply = (
            self.get_extra("wsl_plain_command") is True and self.reply_index == 0
        )
        managed_control = (
            command_reply
            and not self.is_private_chat()
            and bool(command := self.get_extra("_wsl_managed_group_control"))
            and command
            == managed_group_control(
                self.platform,
                self.get_group_id(),
                self.get_extra("wangshangliao_payload") or {},
            )
        )
        if managed_control:

            def check_managed_control():
                if (
                    self.processing_completion.cancelled()
                    or self.policy_epoch != self.platform.config.get("_business_epoch")
                    or command
                    != managed_group_control(
                        self.platform,
                        self.get_group_id(),
                        self.get_extra("wangshangliao_payload") or {},
                    )
                ):
                    raise ProtocolError("management_reply_scope_changed")

            guards.append(check_managed_control)
        auto_recall = (
            self.get_extra("wsl_plain_command") is True and not self.is_private_chat()
        )
        ranking_reply = (
            command_reply
            and not self.is_private_chat()
            and self.get_extra("wsl_ranking_result") is True
        )
        if permission_denial and (
            self.reply_index
            or not message
            or len(message.chain) != 1
            or not isinstance(message.chain[0], Plain)
            or message.chain[0].text != "无权限"
        ):
            self._log_reply("invalid_or_duplicate_denial")
            return
        if (
            (not allowed and not permission_denial)
            or (not self.eligible and not permission_denial and not command_reply)
            or (
                is_managed_account(self.get_sender_id())
                and not self.get_extra("wsl_command_result")
                and not scoped_permission_denial
                and not managed_control
                and not (
                    not permission_denial
                    and (
                        self.is_private_chat()
                        and self.get_extra("wsl_test_scope") == "private_ai"
                        or not self.is_private_chat()
                        and self.get_extra("wsl_test_scope") == "group_ai"
                    )
                )
            )
            or self.processing_completion.cancelled()
        ):
            self._log_reply(
                "reply_disabled"
                if not allowed and not permission_denial
                else "not_eligible"
                if not self.eligible and not permission_denial and not command_reply
                else "cancelled"
                if self.processing_completion.cancelled()
                else "managed_account_blocked"
            )
            return
        if not message or not message.chain:
            self._log_reply("empty_reply")
            return
        replies = [part for part in message.chain if isinstance(part, Reply)]
        if len(replies) > 1:
            raise ProtocolError("quote_identity")
        reply_to = (
            str(replies[0].id) if replies and not self.is_private_chat() else None
        )
        message = MessageChain(
            [part for part in message.chain if not isinstance(part, Reply)]
        )
        if not message.chain:
            self._log_reply("empty_reply")
            return
        if not self.is_private_chat() and not ranking_reply:
            message = MessageChain(
                [At(qq=self.get_sender_id(), name=self.get_sender_name())]
                + [part for part in message.chain if not isinstance(part, At)]
            )
        if any(not isinstance(part, (Plain, At)) for part in message.chain):
            raise ProtocolError("text_only")
        text = ""
        people = []
        for part in message.chain:
            if isinstance(part, Plain):
                text += redact_reply(
                    part.text if ranking_reply else plain_text(part.text),
                    preserve_whitespace=ranking_reply,
                )
                continue
            if self.is_private_chat():
                raise ProtocolError("group_mentions_only")
            peer = self.platform.members.get(self.get_group_id(), {}).get(
                str(part.qq), ""
            )
            if not peer.isascii() or not peer.isdigit() or not 0 < int(peer) < 1 << 32:
                raise ProtocolError("mention_identity")
            nick = str(part.name or part.qq)
            start = len(text.encode("utf-16-le")) // 2
            text += f"@{nick} "
            people.append(
                {
                    "uid": int(peer),
                    "nick": nick,
                    "start": start,
                    "end": len(text.encode("utf-16-le")) // 2,
                }
            )
        if not text.strip():
            self._log_reply("empty_reply")
            return
        key = f"{self.session_id}/{self.message_obj.message_id}/{self.reply_index}"
        target = (
            self.session_id.split("/", 1)[1]
            if self.is_private_chat()
            else self.get_group_id()
        )
        window = None
        if (
            self.is_private_chat()
            and (
                scoped_permission_denial
                or self.get_extra("wsl_test_scope")
                in {"private_commands", "private_ai"}
            )
            and is_managed_account(self.get_sender_id())
            and not managed_control
        ):
            window = getattr(self.platform, "test_window", None)
            if not window or not window.reply(target, self.message_obj.message_id, key):
                self._log_reply("test_window_rejected")
                return
        if (
            not self.is_private_chat()
            and (
                self.get_extra("wsl_command_result")
                or self.get_extra("wsl_test_scope") == "group_ai"
            )
            and is_managed_account(self.get_sender_id())
            and not managed_control
        ):
            window = getattr(self.platform, "test_window", None)
            if not window or not window.reply(target, self.message_obj.message_id, key):
                self._log_reply("test_window_rejected")
                return
        if window is not None:
            original_window = window

            def check_test_reply():
                if (
                    self.platform.test_window is not original_window
                    or not original_window.reply(
                        target, self.message_obj.message_id, key
                    )
                ):
                    raise ProtocolError("test_window_rejected")

            guards.append(check_test_reply)
        authorization = {"authorize": authorize_reply} if guards else {}
        if reply_to is not None:
            authorization["reply_to"] = reply_to
        if people:
            authorization["mentions"] = people
        elif (
            self.get_extra("wsl_command_result")
            or self.get_extra("wsl_test_scope") == "private_ai"
            or scoped_permission_denial
        ):
            authorization["test_mid"] = self.message_obj.message_id
        if auto_recall:
            authorization["auto_recall"] = True
        receipt = await self.platform.send_reply_text(
            target, key, text, **authorization
        )
        self.reply_index += 1
        self.set_extra(
            "_wsl_native_reply_result",
            receipt
            if isinstance(receipt, str)
            and receipt in {"accepted", "verified", "rejected"}
            else "unknown",
        )
        self._log_reply(
            receipt
            if isinstance(receipt, str)
            and receipt in {"accepted", "verified", "rejected", "unknown"}
            else "unknown",
            "reply_result",
        )
        await super().send(message)
