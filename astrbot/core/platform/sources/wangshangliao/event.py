"""Native group text events with explicit reply eligibility."""

import asyncio
import inspect

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Plain, Reply

from .diagnostics import Diagnostics
from .identity import ActorIdentity
from .text import plain_text, redact_reply
from .wire import ProtocolError


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
    """Admit explicit list controls or a separately granted mute-only command.

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
        or payload.get("kind")
    ):
        return ""
    sender = payload.get("sender")
    route = payload.get("recall_route")
    if (
        not isinstance(sender, str)
        or sender == str(platform.account)
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
        isinstance(command, str)
        and command.startswith("禁言 ")
        and sender in config.get("_open_mute_delegates", {}).get(group, ())
    ):
        return command
    if (
        sender not in config.get("_open_managers", {}).get(group, ())
        or not isinstance(command, str)
        or not command
        or command.split(maxsplit=1)[0]
        not in {
            "黑名单",
            "移除黑名单",
            "黑名单列表",
            "白名单",
            "移除白名单",
            "选择白名单",
            "确认白名单",
        }
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
        if self.is_private_chat():
            peer = ""
        self.actor_identity = ActorIdentity(
            str(getattr(platform, "account", message.self_id)),
            str(message.sender.user_id),
            peer,
            str(message.sender.user_id) if peer and not self.is_private_chat() else "",
        )
        config = getattr(platform, "config", {})
        self.policy_epoch = config.get("_business_epoch")
        allowed = not self.is_private_chat() and config.get("reply_groups", {}).get(
            self.get_group_id(), True
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
        """Keep group AI separate from native management permissions.

        Args:
            context: Framework context used for current session configuration.

        Returns:
            False; native administrator roles never grant AI tool authority.
        """
        return False

    def get_ai_conversation_origin(self, context) -> str:
        """Separate role histories without changing the native reply address.

        Args:
            context: Framework context providing the current authorization.

        Returns:
            Persistent conversation key scoped to account, session and role.
        """
        return f"{self.unified_msg_origin}/wsl-ai/{self.platform.account}/customer"

    def authorize_ai_tool(self, tool, context) -> bool:
        """Allow only scoped knowledge in the legacy group consultation route.

        Args:
            tool: Actual tool object, not a model-supplied tool name.
            context: Framework context used to recheck authority at execution.

        Returns:
            Whether the concrete tool may run for the current event.
        """
        if self.is_private_chat():
            return False
        from .runtime_policy import runtime

        if runtime.open_service is not None:
            return False
        from astrbot.core.tools.knowledge_base_tools import KnowledgeBaseQueryTool

        config = self.platform.config
        if config.get("_business_active") is False or self.policy_epoch != config.get(
            "_business_epoch"
        ):
            return False
        return (
            type(tool) is KnowledgeBaseQueryTool
            and tool.name == "astr_kb_search"
            and not tool.is_background_task
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
        """Retire private events before resolving group waking permissions.

        Args:
            config: Effective session profile, not the global administrator list.
            handler_registry: Active filters including third-party command aliases.

        Returns:
            False for private traffic, otherwise True for group waking checks.
        """
        if self.is_private_chat():
            self.stop_event()
            return False
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
        )
        self.role = "admin" if admin else "member"
        return True

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

    async def send(self, message: MessageChain) -> None:
        """Send only text replies from explicitly mentioned, active events."""
        self.set_extra("_wsl_native_reply_result", "not_sent")
        if self.is_private_chat():
            self.stop_event()
            self._log_reply("private_messages_retired")
            return
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
        allowed = config.get("reply_groups", {}).get(self.get_group_id(), True)
        command_reply = (
            self.get_extra("wsl_plain_command") is True and self.reply_index == 0
        )
        managed_control = (
            command_reply
            and bool(command := self.get_extra("_wsl_managed_group_control"))
            and command
            == managed_group_control(
                self.platform,
                self.get_group_id(),
                self.get_extra("wangshangliao_payload") or {},
            )
        )
        allowed = allowed or managed_control
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
        auto_recall = self.get_extra("wsl_plain_command") is True
        ranking_reply = command_reply and self.get_extra("wsl_ranking_result") is True
        public_named_reply = ranking_reply or (
            command_reply and self.get_extra("wsl_lottery_entry_result") is True
        )
        if (
            not allowed
            or (not self.eligible and not command_reply)
            or (
                is_managed_account(self.get_sender_id())
                and not self.get_extra("wsl_command_result")
                and not managed_control
                and self.get_extra("wsl_test_scope") != "group_ai"
            )
            or self.processing_completion.cancelled()
        ):
            self._log_reply(
                "reply_disabled"
                if not allowed
                else "not_eligible"
                if not self.eligible and not command_reply
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
        reply_to = str(replies[0].id) if replies else None
        message = MessageChain(
            [part for part in message.chain if not isinstance(part, Reply)]
        )
        if not message.chain:
            self._log_reply("empty_reply")
            return
        if not public_named_reply:
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
                    part.text if public_named_reply else plain_text(part.text),
                    preserve_whitespace=public_named_reply,
                )
                continue
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
        target = self.get_group_id()
        window = None
        if (
            (
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
        elif self.get_extra("wsl_command_result"):
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
