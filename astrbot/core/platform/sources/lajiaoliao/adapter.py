"""Lajiao Chat adapter backed by the local OpenIM bridge."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.platform import (
    AstrBotMessage,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.message_session import MessageSession

from .event import LajiaoLiaoEvent, chain_to_parts, incoming_to_message
from .protocol import LajiaoLiaoProtocolError, message_payload


@register_platform_adapter(
    "lajiaoliao",
    "辣椒聊 OpenIM 本地桥接",
    default_config_tmpl={
        "type": "lajiaoliao",
        "enable": False,
        "id": "lajiaoliao",
        "bridge_base_url": "http://127.0.0.1:17890",
        "bridge_events_url": "ws://127.0.0.1:17890/v1/events",
        "account_id": "",
        "enabled_groups": [],
        "reply_private": True,
    },
    adapter_display_name="辣椒聊",
    support_streaming_message=False,
)
class LajiaoLiaoAdapter(Platform):
    """One configured account connected through one local bridge instance."""

    supports_operation_ids = True

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.base_url = str(
            platform_config.get("bridge_base_url", "http://127.0.0.1:17890")
        ).rstrip("/")
        self.events_url = str(
            platform_config.get(
                "bridge_events_url", f"{self.base_url}/v1/events"
            )
        )
        self.account_id = str(platform_config.get("account_id", ""))
        if self.account_id:
            self.client_self_id = self.account_id
        self.enabled_groups = {
            str(item) for item in platform_config.get("enabled_groups", [])
        }
        self.member_ids: dict[str, str] = {}
        self.http: aiohttp.ClientSession | None = None
        self.stop_event = asyncio.Event()
        self.connection_state = "stopped"
        self.current_error = ""
        self.last_message_id = ""
        self._seen_message_ids: set[str] = set()
        self._seen_message_order: deque[str] = deque(maxlen=4096)

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="lajiaoliao",
            description="辣椒聊 OpenIM 本地桥接",
            id=self.config["id"],
            adapter_display_name="辣椒聊",
            support_streaming_message=False,
            support_proactive_message=True,
        )

    def get_stats(self) -> dict:
        return {
            **super().get_stats(),
            "connection_state": self.connection_state,
            "current_error": self.current_error,
            "account_id": self.account_id,
            "enabled_groups": sorted(self.enabled_groups),
            "bridge_base_url": self.base_url,
        }

    async def run(self) -> None:
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=float(self.config.get("request_timeout", 15))
            )
        )
        delay = max(1, int(self.config.get("reconnect_delay", 5)))
        try:
            while not self.stop_event.is_set():
                try:
                    await self._consume_events()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connection_state = "error"
                    self.current_error = str(exc)
                    logger.warning("LajiaoLiao bridge unavailable: %s", exc)
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.connection_state = "stopped"
            if self.http:
                await self.http.close()
                self.http = None

    async def _consume_events(self) -> None:
        if not self.http:
            raise LajiaoLiaoProtocolError("http_session_missing")
        self.connection_state = "connecting"
        async with self.http.ws_connect(self.events_url, heartbeat=20) as ws:
            self.connection_state = "connecting"
            self.current_error = ""
            async for item in ws:
                if item.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(item.data)
                    if payload.get("type") == "ready":
                        state = payload.get("data", {})
                        self.connection_state = str(state.get("status", "connecting"))
                        self.current_error = "" if self.connection_state == "online" else self.connection_state
                        user_id = str(state.get("account_id", ""))
                        if self.account_id and user_id and user_id != self.account_id:
                            raise LajiaoLiaoProtocolError("bridge_account_mismatch")
                        if user_id:
                            self.account_id = user_id
                        continue
                    if payload.get("type") == "error":
                        raise LajiaoLiaoProtocolError(str(payload.get("error", "bridge_error")))
                    if payload.get("type") != "message":
                        continue
                    message = incoming_to_message(
                        payload["data"],
                        self.account_id,
                    )
                    allowed = (
                        message.group_id in self.enabled_groups
                        if message.group_id
                        else bool(self.config.get("reply_private", True))
                    )
                    if allowed and str(message.sender.user_id) != self.account_id:
                        if self._remember_message(message.message_id):
                            self.last_message_id = str(message.message_id)
                            self.commit_event(self.create_event(message))
                    await ws.send_json({"type": "ack", "message_id": str(message.message_id)})
                elif item.type in {
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                }:
                    raise LajiaoLiaoProtocolError("bridge_events_closed")
            raise LajiaoLiaoProtocolError("bridge_events_closed")

    def _remember_message(self, message_id: str) -> bool:
        message_id = str(message_id)
        if not message_id or message_id in self._seen_message_ids:
            return False
        if len(self._seen_message_order) == self._seen_message_order.maxlen:
            expired = self._seen_message_order.popleft()
            self._seen_message_ids.discard(expired)
        self._seen_message_order.append(message_id)
        self._seen_message_ids.add(message_id)
        return True

    async def send_parts(
        self,
        target_id: str,
        reply_to: str,
        parts: list[dict[str, Any]],
        kind: str = "private",
    ) -> dict[str, Any]:
        if not self.http:
            raise LajiaoLiaoProtocolError("bridge_not_started")
        payload = message_payload(target_id, reply_to, parts)
        payload["kind"] = kind
        if kind == "group" and target_id not in self.enabled_groups:
            raise LajiaoLiaoProtocolError("group_not_allowed")
        async with self.http.post(f"{self.base_url}/v1/messages", json=payload) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise LajiaoLiaoProtocolError(
                    str(data.get("error", f"bridge_http_{resp.status}"))
                )
            if data.get("status") != "sent" or not data.get("message_id"):
                raise LajiaoLiaoProtocolError("missing_send_receipt")
            return data

    async def send_by_session(
        self,
        session: MessageSession,
        message_chain: MessageChain,
    ) -> None:
        kind, sep, target = session.session_id.partition(":")
        if not sep or kind not in {"group", "private"}:
            raise LajiaoLiaoProtocolError("invalid_session_target")
        parts = chain_to_parts(message_chain, self.member_ids)
        await self.send_parts(target, "", parts, kind=kind)
        await super().send_by_session(session, message_chain)

    async def terminate(self) -> None:
        self.stop_event.set()
        if self.http:
            await self.http.close()

    def create_event(self, message: AstrBotMessage) -> LajiaoLiaoEvent:
        enabled = bool(
            message.group_id
            and (
                message.group_id in self.enabled_groups
            )
        ) or not message.group_id
        if message.group_id:
            enabled = enabled and bool(
                self.config.get("reply_groups", {}).get(message.group_id, True)
            )
        elif not self.config.get("reply_private", True):
            enabled = False
        return LajiaoLiaoEvent(message, self, enabled)
