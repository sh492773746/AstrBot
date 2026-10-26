"""AstrBot event and outbound conversion for Lajiao Chat."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType

from .protocol import IncomingMessage, LajiaoLiaoProtocolError, session_id_for


def _component_payload(component: Any, members: dict[str, str]) -> dict[str, Any]:
    if isinstance(component, Plain):
        return {"type": "text", "text": component.text}
    if isinstance(component, At):
        user_id = str(component.qq)
        return {
            "type": "mention",
            "user_id": members.get(user_id, user_id),
            "name": component.name or user_id,
        }
    if isinstance(component, Image):
        url = getattr(component, "url", None) or getattr(component, "file", None)
        if not url:
            raise LajiaoLiaoProtocolError(
                "image_without_url", delivery_status="rejected"
            )
        return {"type": "image", "url": str(url)}
    if isinstance(component, Record):
        url = getattr(component, "url", None) or getattr(component, "file", None)
        if not url:
            raise LajiaoLiaoProtocolError(
                "record_without_url", delivery_status="unsupported"
            )
        return {"type": "audio", "url": str(url)}
    if isinstance(component, File):
        url = getattr(component, "url", None) or getattr(component, "file", None)
        if not url:
            raise LajiaoLiaoProtocolError(
                "file_without_url", delivery_status="unsupported"
            )
        return {"type": "file", "url": str(url), "name": getattr(component, "name", "")}
    if isinstance(component, Reply):
        return {"type": "quote", "message_id": str(component.id)}
    raise LajiaoLiaoProtocolError(
        f"unsupported_component:{type(component).__name__}",
        delivery_status="unsupported",
    )


def chain_to_parts(
    chain: MessageChain,
    members: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Convert AstrBot message components into bridge-neutral parts."""
    if isinstance(chain, MessageChain) and chain.type == "reasoning":
        raise LajiaoLiaoProtocolError(
            "reasoning_output_blocked", delivery_status="rejected"
        )
    components = chain.chain if isinstance(chain, MessageChain) else chain
    return [_component_payload(part, members or {}) for part in components]


class LajiaoLiaoEvent(AstrMessageEvent):
    """Incoming Lajiao Chat event whose replies use the local bridge."""

    ai_follow_up_enabled = False
    ai_supported_runner_types = ("local",)

    def __init__(
        self,
        message: AstrBotMessage,
        platform: Any,
    ) -> None:
        super().__init__(
            message.message_str,
            message,
            platform.meta(),
            message.session_id,
        )
        self.platform = platform
        self.processing_completion = asyncio.get_running_loop().create_future()

    def ignored_bot_sender(self) -> bool:
        guard = getattr(self.platform, "ignore_bot_sender", None)
        return guard is not None and guard(
            str(self.get_sender_id()), group=bool(self.message_obj.group_id)
        )

    def ai_service(self):
        """Resolve the live plugin, never a stale manager after plugin reload."""
        from astrbot.core.provider.event_guard import AIRequestDenied

        from . import runtime

        manager = getattr(self.platform, "manager", None)
        service = getattr(manager, "ai", None)
        if service is None or runtime.active is not manager:
            self.stop_event()
            raise AIRequestDenied("ai_service_unavailable")
        return service

    async def prepare_ai_request(self, context):
        self.set_extra("_lajiao_ai_service", self.ai_service())
        await self.ai_service().prepare(self)
        return True

    def ai_queue_timeout(self):
        return max(0, self.get_extra("_lajiao_ai_queue_deadline", 0) - time.monotonic())

    async def finish_ai_request(self):
        service = self.get_extra("_lajiao_ai_service")
        if service is not None:
            await service.finish(self)

    async def authorize_ai_model_request(self, purpose):
        await self.ai_service().authorize(self, purpose)

    def ai_metering_context(self, provider, purpose):
        from .metering import metering_context

        return metering_context(self.ai_service().plugin, self, provider, purpose)

    async def record_ai_model_response(self, purpose, response):
        service = self.get_extra("_lajiao_ai_service")
        if service is not None:
            await service.metric(self, purpose, response)

    def get_ai_persona_id(self):
        from astrbot.core.provider.event_guard import AIRequestDenied

        scope = self.get_extra("_lajiao_ai_scope")
        if scope is None:
            self.stop_event()
            raise AIRequestDenied("ai_scope_required")
        return scope[5]

    async def validate_ai_history_access(self):
        from astrbot.core.provider.event_guard import AIRequestDenied

        scope = self.get_extra("_lajiao_ai_scope")
        if (
            not scope
            or self.is_stopped()
            or await self.ai_service().resolve(self) != scope
        ):
            self.stop_event()
            raise AIRequestDenied("ai_history_scope_changed")

    def get_ai_conversation_origin(self, context):
        """Keep management histories separate without changing reply addresses."""
        from astrbot.core.provider.event_guard import AIRequestDenied

        scope = self.get_extra("_lajiao_ai_scope")
        if scope is None:
            self.stop_event()
            raise AIRequestDenied("ai_scope_required")
        return self._ai_conversation_origin(scope)

    async def get_ai_command_conversation_origin(self, context):
        scope = await self.ai_service().resolve(self, require_ai=False)
        return self._ai_conversation_origin(scope)

    def _ai_conversation_origin(self, scope):
        if self.get_group_id():
            return self.unified_msg_origin
        suffix = (
            "chat" if scope[4] == "chat" else "management/" + quote(scope[3], safe="")
        )
        return self.unified_msg_origin + "/lajiao-ai/" + suffix

    def authorize_ai_tool(self, tool, context):
        """Only the concrete private management tool may execute in management."""
        scope = self.get_extra("_lajiao_ai_scope")
        return bool(
            scope
            and scope[4] == "management"
            and self.is_private_chat()
            and getattr(tool, "name", "") == "lajiaoliao_private_management"
            and getattr(tool, "handler_module_path", "")
            == "data.plugins.astrbot_plugin_lajiaoliao.main"
            and not getattr(tool, "is_background_task", False)
            and not self.is_stopped()
        )

    async def prepare_waking_check(self, config, handlers) -> bool:
        guard = getattr(self.platform, "require_subscription", None)
        if callable(guard):
            from .storage import SessionError

            try:
                guard(raw=self.message_obj.raw_message)
            except SessionError:
                self.stop_event()
                self.processing_completion.cancel()
                return False
        if self.ignored_bot_sender():
            self.processing_completion.cancel()
            return False
        return True

    async def send(self, message: MessageChain) -> None:
        if self.processing_completion.cancelled() or self.ignored_bot_sender():
            return
        scope = self.get_extra("_lajiao_ai_scope")
        if scope:
            try:
                if self.is_stopped() or await self.ai_service().resolve(self) != scope:
                    return
            except Exception:
                self.stop_event()
                return
        target_id = self.message_obj.group_id or self.message_obj.sender.user_id
        parts = chain_to_parts(message, getattr(self.platform, "member_ids", {}))
        receipt = await self.platform.send_parts(
            target_id=target_id,
            reply_to="",
            parts=parts,
            kind="group" if self.message_obj.group_id else "private",
        )
        if self.get_extra("lajiaoliao_functional_reply") and self.message_obj.group_id:
            callback = getattr(
                getattr(self.platform, "manager", None), "on_functional_reply", None
            )
            if callback is not None:
                await callback(
                    self.platform.session_ref,
                    str(self.message_obj.group_id),
                    receipt.get("message_id", ""),
                    "group",
                    str(target_id),
                )
        await super().send(message)


def incoming_to_message(payload: dict[str, Any], self_id: str) -> AstrBotMessage:
    incoming = IncomingMessage.from_payload(payload)
    message = AstrBotMessage()
    message.self_id = self_id
    message.message_id = incoming.message_id
    message.session_id = session_id_for(incoming)
    message.sender = MessageMember(incoming.sender_id, incoming.sender_name)
    message.message_str = incoming.text
    message.message = _incoming_components(incoming)
    message.raw_message = payload
    message.timestamp = incoming.timestamp
    message.type = (
        MessageType.GROUP_MESSAGE if incoming.is_group else MessageType.FRIEND_MESSAGE
    )
    if incoming.is_group:
        from astrbot.api.platform import Group

        message.group = Group(group_id=incoming.target_id)
    return message


def _incoming_components(
    incoming: IncomingMessage,
) -> list[Any]:
    components: list[Any] = []
    if incoming.text:
        components.append(Plain(incoming.text))
    for item in incoming.components:
        kind = item.get("type")
        if kind == "image" and item.get("url"):
            components.append(Image(file=str(item["url"])))
        elif kind == "audio" and item.get("url"):
            components.append(Record(file=str(item["url"])))
        elif kind == "file" and item.get("url"):
            components.append(
                File(name=str(item.get("name") or "file"), url=str(item["url"]))
            )
        elif kind == "mention" and item.get("user_id"):
            components.append(
                At(
                    qq=str(item["user_id"]),
                    name=str(item.get("name") or item["user_id"]),
                )
            )
        elif kind == "quote" and item.get("message_id"):
            text = str(item.get("text") or "")
            components.append(
                Reply(
                    id=str(item["message_id"]),
                    sender_id=str(item.get("sender_id") or ""),
                    sender_nickname=str(item.get("sender_name") or ""),
                    message_str=text,
                    chain=[Plain(text)] if text else [],
                    time=int(item.get("timestamp") or 0),
                )
            )
    return components
