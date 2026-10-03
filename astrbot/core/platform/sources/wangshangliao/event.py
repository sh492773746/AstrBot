"""Native group text events with explicit reply eligibility."""

import asyncio

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Plain, Reply

from .diagnostics import Diagnostics
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


class WangshangliaoEvent(AstrMessageEvent):
    """Waitable event whose replies carry an incoming-message idempotency key."""

    def __init__(self, message, platform, eligible: bool):
        super().__init__(
            message.message_str, message, platform.meta(), message.session_id
        )
        self.platform = platform
        config = getattr(platform, "config", {})
        allowed = (
            config.get("reply_private", True)
            if self.is_private_chat()
            else config.get("reply_groups", {}).get(self.get_group_id(), True)
        )
        self.eligible = eligible and allowed
        self.processing_completion = asyncio.get_running_loop().create_future()
        self.set_extra("_context_only", not self.eligible)
        self.reply_index = 0

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
        config = getattr(self.platform, "config", {})
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
        auto_recall = (
            command_reply
            and not self.is_private_chat()
            and self.get_extra("wsl_keep_reply") is not True
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
        # Native quote transport is not implemented. Preserve the reply body
        # instead of rejecting AstrBot's optional quote decoration.
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
        if scoped_permission_denial and is_managed_account(self.get_sender_id()):
            window = self.platform.test_window
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
        ):
            window = self.platform.test_window
            if not window or not window.reply(target, self.message_obj.message_id, key):
                self._log_reply("test_window_rejected")
                return
        if people:
            receipt = await self.platform.send_reply_text(
                target,
                key,
                text,
                mentions=people,
                **({"auto_recall": True} if auto_recall else {}),
            )
        else:
            if (
                self.get_extra("wsl_command_result")
                or self.get_extra("wsl_test_scope") == "private_ai"
                or scoped_permission_denial
            ):
                receipt = await self.platform.send_reply_text(
                    target,
                    key,
                    text,
                    test_mid=self.message_obj.message_id,
                    **({"auto_recall": True} if auto_recall else {}),
                )
            else:
                receipt = await self.platform.send_reply_text(
                    target,
                    key,
                    text,
                    **({"auto_recall": True} if auto_recall else {}),
                )
        self.reply_index += 1
        self._log_reply(
            receipt
            if isinstance(receipt, str)
            and receipt in {"accepted", "verified", "rejected", "unknown"}
            else "unknown",
            "reply_result",
        )
        await super().send(message)
