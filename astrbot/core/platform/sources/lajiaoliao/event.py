"""AstrBot event and outbound conversion for Lajiao Chat."""

from __future__ import annotations

import asyncio
from typing import Any

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
            raise LajiaoLiaoProtocolError("image_without_url")
        return {"type": "image", "url": str(url)}
    if isinstance(component, Record):
        url = getattr(component, "url", None) or getattr(component, "file", None)
        if not url:
            raise LajiaoLiaoProtocolError("record_without_url")
        return {"type": "audio", "url": str(url)}
    if isinstance(component, File):
        url = getattr(component, "url", None) or getattr(component, "file", None)
        if not url:
            raise LajiaoLiaoProtocolError("file_without_url")
        return {"type": "file", "url": str(url), "name": getattr(component, "name", "")}
    if isinstance(component, Reply):
        return {"type": "quote", "message_id": str(component.id)}
    raise LajiaoLiaoProtocolError(
        f"unsupported_component:{type(component).__name__}"
    )


def chain_to_parts(
    chain: MessageChain,
    members: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Convert AstrBot message components into bridge-neutral parts."""
    components = chain.chain if isinstance(chain, MessageChain) else chain
    return [_component_payload(part, members or {}) for part in components]


class LajiaoLiaoEvent(AstrMessageEvent):
    """Incoming Lajiao Chat event whose replies use the local bridge."""

    def __init__(
        self,
        message: AstrBotMessage,
        platform: Any,
        enabled: bool,
    ) -> None:
        super().__init__(
            message.message_str,
            message,
            platform.meta(),
            message.session_id,
        )
        self.platform = platform
        self.enabled = enabled
        self.processing_completion = asyncio.get_running_loop().create_future()

    async def send(self, message: MessageChain) -> None:
        if not self.enabled or self.processing_completion.cancelled():
            return
        target_id = self.message_obj.group_id or self.message_obj.sender.user_id
        parts = chain_to_parts(message, getattr(self.platform, "member_ids", {}))
        await self.platform.send_parts(
            target_id=target_id,
            reply_to="",
            parts=parts,
            kind="group" if self.message_obj.group_id else "private",
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
            components.append(Reply(id=str(item["message_id"])))
    return components
