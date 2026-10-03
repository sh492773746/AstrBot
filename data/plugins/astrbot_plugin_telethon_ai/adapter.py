"""Telethon transport feeding AstrBot's standard event/config/persona pipeline."""

import asyncio
import fcntl
import re
import time
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import registry
from .policy import Gate, validate
from .tenants import Denied, Tenants

NAME = "astrbot_plugin_telethon_ai"
CONTEXT = None
PIPELINE = None
ACCOUNTS = {}
DEFAULT = {
    "id": "TelethonAI",
    "type": "telethon_ai",
    "enable": False,
    "account": "collector",
    "allowed_chats": [],
    "allowed_senders": [],
    "disclosure_confirmed": False,
    "cooldown_seconds": 30,
}


def state_path():
    return Path(get_astrbot_plugin_data_path()) / NAME / "gate.db"


def credentials(account):
    return registry.credentials(account)


class TelethonEvent(AstrMessageEvent):
    def __init__(self, message, adapter, peer, reservation):
        super().__init__(
            message.message_str, message, adapter.meta(), message.session_id
        )
        self.adapter = adapter
        self.peer = peer
        self.sent = False
        self.reservation = reservation

    async def send(self, message):
        text = "".join(
            part.text for part in message.chain if isinstance(part, Plain)
        ).strip()
        if not text or self.sent:
            return
        # A response cannot escape its original admitted event.
        async with self.adapter.send_lock:
            if (
                self.adapter.stopping
                or self.adapter.gate.paused(self.adapter.account)
                or time.time() - self.created_at > 120
                or not self.adapter.tenants.can_send(self.reservation)
            ):
                return
            validate(self.adapter.config)
            profile = CONTEXT.get_config(self.unified_msg_origin) if CONTEXT else {}
            if (
                profile.get("admins_id")
                or not profile.get("disable_builtin_commands")
                or profile.get("plugin_set") != [NAME]
                or not profile.get("provider_settings", {}).get("enable")
            ):
                return
            if self.get_group_id() not in map(
                str, self.adapter.config["allowed_chats"]
            ) or self.get_sender_id() not in map(
                str, self.adapter.config["allowed_senders"]
            ):
                return
            self.sent = True
            try:
                await self.adapter.client.send_message(
                    self.peer,
                    "[AI] " + text[:1500],
                    reply_to=int(self.message_obj.message_id),
                    parse_mode=None,
                    link_preview=False,
                )
            except (Exception, asyncio.CancelledError) as exc:
                # No automatic retry or other-account failover on an ambiguous send.
                self.adapter.gate.pause(self.adapter.account)
                self.adapter.tenants.finish(self.reservation, "uncertain")
                self.adapter.record_error(
                    "Telegram send failed; manually review before resuming"
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeError("Telethon send failed; gateway paused") from None
            self.adapter.tenants.finish(self.reservation, "sent")
        await super().send(message)


@register_platform_adapter(
    "telethon_ai",
    "授权账号 AI 对话",
    default_config_tmpl=DEFAULT,
    adapter_display_name="Telethon AI 账号",
    support_streaming_message=False,
)
class TelethonAdapter(Platform):
    def __init__(self, platform_config, platform_settings, event_queue):
        super().__init__(platform_config, event_queue)
        self.account = platform_config.get("account", "")
        self.client = None
        self.gate = Gate(state_path())
        self.tenants = Tenants(state_path().with_name("tenants.db"))
        self.stopping = True
        self.send_lock = asyncio.Lock()
        self.started = 0

    def meta(self):
        return PlatformMetadata(
            "telethon_ai",
            "授权账号 AI 对话",
            self.config["id"],
            support_streaming_message=False,
            support_proactive_message=False,
        )

    async def send_by_session(self, session, message_chain):
        raise PermissionError(
            "Proactive sending is disabled; an admitted incoming event is required"
        )

    async def receive(self, incoming):
        raw = incoming.message
        if (
            self.stopping
            or raw.out
            or not incoming.is_group
            or raw.fwd_from
            or raw.media
            or not raw.raw_text
            or raw.raw_text.lstrip().startswith("/")
            or raw.date.timestamp() < self.started
            or incoming.sender_id in ACCOUNTS.values()
            or str(incoming.chat_id) not in map(str, self.config["allowed_chats"])
            or str(incoming.sender_id) not in map(str, self.config["allowed_senders"])
        ):
            return
        sender = await incoming.get_sender()
        if sender is None or getattr(sender, "bot", True):
            return
        # Only an explicit username mention or reply to our own message can wake.
        username = self.username
        mentioned = bool(
            username
            and re.search(r"@" + re.escape(username) + r"(?!\w)", raw.raw_text, re.I)
        )
        replied = False
        if not mentioned and raw.is_reply:
            parent = await incoming.get_reply_message()
            replied = bool(parent and parent.sender_id == self.self_id)
        if not mentioned and not replied:
            return
        umo = (
            f"{self.config['id']}:GroupMessage:{incoming.chat_id}_{incoming.sender_id}"
        )
        if CONTEXT is None:
            return
        profile = CONTEXT.get_config(umo)
        if (
            profile.get("admins_id")
            or not profile.get("disable_builtin_commands")
            or profile.get("plugin_set") != [NAME]
            or not profile.get("provider_settings", {}).get("enable")
            or profile.get("agent_runner", {}).get("runner_type") != "local"
            or profile.get("kb_names")
        ):
            return
        if not self.gate.admit(self.account, incoming.chat_id, raw.id, self.config):
            return
        try:
            reservation, tenant = self.tenants.reserve(
                self.account, incoming.chat_id, raw.id
            )
        except Denied:
            return
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.self_id = str(self.self_id)
        message.group_id = str(incoming.chat_id)
        message.sender = MessageMember(str(incoming.sender_id), str(incoming.sender_id))
        message.session_id = f"{tenant}_{incoming.chat_id}_{incoming.sender_id}"
        message.message_id = str(raw.id)
        message.message_str = raw.raw_text[:2000]
        message.message = [At(qq=str(self.self_id)), Plain(text=message.message_str)]
        message.raw_message = None
        peer = await incoming.get_input_chat()
        event = TelethonEvent(message, self, peer, reservation)
        if PIPELINE is None:
            self.tenants.finish(reservation, "released")
            self.record_error("Plugin-owned account pipeline is unavailable")
        else:
            try:
                await PIPELINE.dispatch(event)
            except asyncio.CancelledError:
                if not event.sent:
                    self.tenants.finish(reservation, "released")
                raise
            except Exception:
                if not event.sent:
                    self.tenants.finish(reservation, "released")
                self.record_error("Account pipeline failed; no automatic message retry")

    async def run(self):
        validate(self.config)
        if self.gate.paused(self.account):
            raise ValueError("Gateway paused; explicit administrator resume required")
        from telethon import TelegramClient, events

        spec, proxy, data = credentials(self.account)
        with ExitStack() as locks:
            # Shared flock allows both gateway accounts, excluding the collector's exclusive lock.
            if data is not None:
                guard = locks.enter_context((data / "collector.lock").open("a"))
                fcntl.flock(guard.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            locks.enter_context(
                FileLock(spec["session"] + ".session.unified.lock", timeout=0)
            )
            self.client = TelegramClient(
                spec["session"],
                spec["api_id"],
                spec["api_hash"],
                proxy=proxy,
                flood_sleep_threshold=0,
                request_retries=0,
                connection_retries=1,
                catch_up=False,
            )
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise ValueError(
                        "Account requires operator login; automatic login disabled"
                    )
                me = await self.client.get_me()
                if me.id != ACCOUNTS[self.account]:
                    raise ValueError("Account identity mismatch")
                self.self_id = me.id
                self.username = me.username or ""
                self.started = time.time()
                self.stopping = False
                self.client.add_event_handler(
                    self.receive, events.NewMessage(incoming=True)
                )
                await self.client.run_until_disconnected()
            finally:
                self.stopping = True
                await self.client.disconnect()
                self.status = PlatformStatus.STOPPED

    async def terminate(self):
        self.stopping = True
        if self.client:
            await self.client.disconnect()
        self.status = PlatformStatus.STOPPED
