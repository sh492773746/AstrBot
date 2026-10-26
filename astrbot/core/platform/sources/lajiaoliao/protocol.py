"""Local bridge protocol for the Lajiao Chat adapter.

The bridge deliberately exposes a provider-neutral contract.  The upstream
OpenIM implementation is not bundled until a compatible SDK is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class LajiaoLiaoProtocolError(RuntimeError):
    """A bridge or upstream operation failed."""


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    sender_id: str
    sender_name: str
    target_id: str
    text: str
    is_group: bool
    timestamp: int
    mentions: tuple[str, ...] = ()
    components: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> IncomingMessage:
        kind = payload.get("kind", "private")
        return cls(
            message_id=str(payload["message_id"]),
            sender_id=str(payload["sender_id"]),
            sender_name=str(payload.get("sender_name") or payload["sender_id"]),
            target_id=str(payload["target_id"]),
            text=str(payload.get("text", "")),
            is_group=bool(payload.get("is_group", kind == "group")),
            timestamp=int(payload.get("timestamp", 0)),
            mentions=tuple(str(item) for item in payload.get("mentions", [])),
            components=tuple(
                item for item in payload.get("components", []) if isinstance(item, dict)
            ),
        )


def session_id_for(message: IncomingMessage) -> str:
    """Use stable, non-ambiguous private/group session identifiers."""
    prefix = "group" if message.is_group else "private"
    return f"{prefix}:{message.target_id}"


def message_payload(
    target_id: str,
    message_id: str,
    parts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create the bridge send envelope without provider-specific fields."""
    return {
        "target_id": str(target_id),
        "reply_to": str(message_id) if message_id else "",
        "parts": parts,
    }
