"""Request-scoped typing for customer support, using AstrBot's native drafts."""

import asyncio
import time

import telegramify_markdown
from telegram import MessageEntity
from telegram.error import BadRequest, TelegramError

BRAND_EMOJI_ID = "6165948762029043979"


def install(event, tasks):
    """Wrap one private support event without changing other bots or menus.

    Args:
        event: Authenticated private Telegram event.
        tasks: Plugin-owned task registry for unload cancellation.
    """
    if getattr(event, "_superbot_support_stream", False):
        return
    event._superbot_support_stream = True
    original_stream = event.send_streaming
    original_draft = event._send_message_draft
    last_draft = 0.0

    async def draft(chat_id, draft_id, text, message_thread_id=None, parse_mode=None):
        nonlocal last_draft
        # Reserve room for typing requests in Telegram's per-chat rate limit.
        delay = 1.0 - (time.monotonic() - last_draft)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            if text == "\u23f3" and parse_mode is None:
                payload = {
                    "chat_id": int(chat_id),
                    "draft_id": draft_id,
                    "text": "😀",
                    "entities": [
                        MessageEntity(
                            "custom_emoji", 0, 2, custom_emoji_id=BRAND_EMOJI_ID
                        )
                    ],
                }
                if message_thread_id:
                    payload["message_thread_id"] = int(message_thread_id)
                try:
                    return await event.client.send_message_draft(**payload)
                except BadRequest:
                    # Explicit rejection is safe to replace; no retry on timeout.
                    text = "✍️"
                except TelegramError:
                    return
            try:
                return await original_draft(
                    chat_id, draft_id, text, message_thread_id, parse_mode=parse_mode
                )
            except BadRequest as exc:
                if any(
                    term in str(exc).lower()
                    for term in (
                        "parse entities",
                        "can't find end",
                        "unsupported start tag",
                    )
                ):
                    raise
                # A transient draft must not trigger a second transport attempt.
                return
            except (TelegramError, TimeoutError):
                return
        finally:
            last_draft = time.monotonic()

    async def typing():
        # Bounded even if the upstream model never produces a response.
        for _ in range(45):
            try:
                await asyncio.wait_for(event.send_typing(), timeout=3)
            except Exception:
                return
            await asyncio.sleep(4)

    async def stream(generator, use_fallback=False):
        task = asyncio.create_task(typing())
        tasks.append(task)
        try:
            return await original_stream(generator, use_fallback=use_fallback)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if task in tasks:
                tasks.remove(task)

    async def final_segment(text, payload):
        """Use the native Markdown converter with narrow, request-local fallback.

        Args:
            text: Completed model segment.
            payload: Existing Telegram routing parameters.
        """
        for chunk in event._split_message(text):
            try:
                formatted = telegramify_markdown.markdownify(chunk)
            except ValueError:
                await event.client.send_message(text=chunk, parse_mode=None, **payload)
                continue
            try:
                await event.client.send_message(
                    text=formatted, parse_mode="MarkdownV2", **payload
                )
            except BadRequest as exc:
                if not any(
                    term in str(exc).lower()
                    for term in (
                        "parse entities",
                        "can't find end",
                        "unsupported start tag",
                    )
                ):
                    raise
                await event.client.send_message(text=chunk, parse_mode=None, **payload)

    event._send_message_draft = draft
    event.send_streaming = stream
    if hasattr(event, "_send_final_segment"):
        event._send_final_segment = final_segment
