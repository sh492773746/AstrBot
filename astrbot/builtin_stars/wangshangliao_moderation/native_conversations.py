"""Use AstrBot's conversation manager without storing a second dialogue history."""

import json
from dataclasses import dataclass, field

from astrbot.core import logger

from .open_store import OpenError


@dataclass
class Dialogue:
    """Hold one request's native conversation snapshot, never a separate archive."""

    origin: str
    title: str
    persona: str
    limit: int
    cid: str | None = None
    history: list = field(default_factory=list)
    original: str = "[]"


class NativeConversations:
    """Keep ownership checks and bounded, tool-free context around the native API."""

    def __init__(self, context):
        """Bind the existing framework context.

        Args:
            context: AstrBot context owning the native conversation manager.
        """
        self.context = context

    @property
    def manager(self):
        """Return the native manager without opening a plugin-owned database.

        Returns:
            AstrBot's existing conversation manager.

        Raises:
            OpenError: If framework conversation storage is unavailable.
        """
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            raise OpenError("conversation_unavailable")
        return manager

    async def load(self, origin, title, persona, *, turns=20, fresh=False):
        """Read owned, bounded dialogue without importing another role's data.

        Args:
            origin: Framework-derived origin scoped to account, session and AI role.
            title: Display label for a newly admitted native conversation.
            persona: Resolved AstrBot persona identifier.
            turns: Maximum context/recorded exchanges, bounded to twenty.
            fresh: Whether this explicit consultation must start without old context.

        Returns:
            Request-local snapshot referencing native storage.

        Raises:
            OpenError: If the selected conversation has a different owner.
        """
        parts = origin.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"FriendMessage", "GroupMessage"}:
            raise OpenError("conversation_scope_invalid")
        dialogue = Dialogue(origin, title, persona, max(2, min(40, turns * 2)))
        if fresh:
            return dialogue
        cid = await self.manager.get_curr_conversation_id(origin)
        conversation = await self.manager.get_conversation(origin, cid) if cid else None
        if conversation is None:
            return dialogue
        if conversation.user_id != origin:
            raise OpenError("conversation_scope_invalid")
        try:
            history = json.loads(conversation.history or "[]")
        except (ValueError, TypeError):
            return dialogue
        if not isinstance(history, list) or any(
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in {"user", "assistant"}
            or not isinstance(message["content"], str)
            or len(message["content"]) > 8192
            for message in history
        ):
            # Never inherit tool results, checkpoints or externally edited prompts.
            return dialogue
        dialogue.cid = cid
        dialogue.original = conversation.history or "[]"
        dialogue.history = history[-dialogue.limit :]
        return dialogue

    async def start(self, dialogue, guard):
        """Create a native conversation only after successful budget admission.

        Args:
            dialogue: Snapshot prepared without creating a conversation.
            guard: Current account, session, profile and budget authorization.
        """
        guard()
        if dialogue.cid is None:
            dialogue.cid = await self.manager.new_conversation(
                dialogue.origin,
                title=dialogue.title,
                persona_id=dialogue.persona or None,
            )
        else:
            await self.current(dialogue)
        guard()

    async def current(self, dialogue):
        """Reject deletion, switching, foreign ownership or concurrent edits.

        Args:
            dialogue: Native conversation snapshot used to prepare this request.

        Raises:
            OpenError: If the snapshot is no longer the current owned record.
        """
        if await self.manager.get_curr_conversation_id(dialogue.origin) != dialogue.cid:
            raise OpenError("conversation_changed")
        conversation = await self.manager.get_conversation(
            dialogue.origin, dialogue.cid
        )
        if (
            conversation is None
            or conversation.user_id != dialogue.origin
            or (conversation.history or "[]") != dialogue.original
            or await self.manager.get_curr_conversation_id(dialogue.origin)
            != dialogue.cid
        ):
            raise OpenError("conversation_changed")

    async def save(self, dialogue, question, answer, guard, *, token_usage=None):
        """Record delivered text without retrying a reply if storage fails.

        Args:
            dialogue: Native snapshot already bound to the admitted model call.
            question: Actual user question or selected group message.
            answer: Delivered, sanitized reply text.
            guard: Final account/session or group authorization check.
            token_usage: Validated provider usage, or None if unavailable.
        """
        try:
            guard()
            await self.current(dialogue)
            guard()
            await self.manager.update_conversation(
                dialogue.origin,
                dialogue.cid,
                history=(
                    dialogue.history
                    + [
                        {"role": "user", "content": question[:8192]},
                        {"role": "assistant", "content": answer[:8192]},
                    ]
                )[-dialogue.limit :],
                token_usage=token_usage,
            )
        except Exception as exc:
            logger.warning(
                "[Wangshangliao] Native conversation recording failed (%s); "
                "the delivered reply will not be retried.",
                type(exc).__name__,
            )
