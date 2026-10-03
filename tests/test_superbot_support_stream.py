"""Offline support stream lifecycle tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TimedOut

from data.plugins.astrbot_plugin_superbot.support_stream import install


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, BadRequest("emoji unavailable"), TimedOut()])
async def test_brand_placeholder_entity_and_safe_fallback(error):
    original = AsyncMock()
    client = SimpleNamespace(send_message_draft=AsyncMock(side_effect=error))
    event = SimpleNamespace(
        send_streaming=AsyncMock(),
        _send_message_draft=original,
        send_typing=AsyncMock(),
        client=client,
    )
    install(event, [])
    await event._send_message_draft("123", 5, "⏳", "7")
    sent = client.send_message_draft.call_args.kwargs
    assert sent["entities"][0].custom_emoji_id == "6165948762029043979"
    assert sent["entities"][0].length == 2
    assert sent["message_thread_id"] == 7
    if isinstance(error, BadRequest):
        assert original.call_args.args[2] == "✍️"
    else:
        original.assert_not_called()


@pytest.mark.asyncio
async def test_normal_draft_is_unchanged():
    original = AsyncMock()
    event = SimpleNamespace(send_streaming=AsyncMock(), _send_message_draft=original)
    install(event, [])
    await event._send_message_draft("123", 5, "回答内容", parse_mode="MarkdownV2")
    original.assert_awaited_once_with(
        "123", 5, "回答内容", None, parse_mode="MarkdownV2"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_typing_is_scoped_and_stopped(fail):
    tasks = []
    started = asyncio.Event()

    async def typing():
        started.set()

    async def original(generator, use_fallback=False):
        await asyncio.wait_for(started.wait(), 1)
        if fail:
            raise RuntimeError("model failed")
        return "complete"

    event = SimpleNamespace(
        send_streaming=original,
        _send_message_draft=AsyncMock(),
        send_typing=typing,
    )
    install(event, tasks)
    wrapped = event.send_streaming
    install(event, tasks)
    assert event.send_streaming is wrapped
    if fail:
        with pytest.raises(RuntimeError):
            await event.send_streaming(None)
    else:
        assert await event.send_streaming(None) == "complete"
    assert tasks == []


@pytest.mark.asyncio
async def test_stream_cancellation_releases_typing():
    started = asyncio.Event()
    tasks = []

    async def original(generator, use_fallback=False):
        started.set()
        await asyncio.Event().wait()

    event = SimpleNamespace(
        send_streaming=original,
        _send_message_draft=AsyncMock(),
        send_typing=AsyncMock(),
    )
    install(event, tasks)
    stream = asyncio.create_task(event.send_streaming(None))
    await started.wait()
    stream.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stream
    assert not tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,fallback",
    [
        (BadRequest("Can't parse entities"), True),
        (BadRequest("chat not found"), False),
        (TimedOut(), False),
    ],
)
async def test_final_reply_only_format_rejection_may_resend(failure, fallback):
    client = SimpleNamespace(send_message=AsyncMock(side_effect=[failure, None]))
    event = SimpleNamespace(
        send_streaming=AsyncMock(),
        _send_message_draft=AsyncMock(),
        _send_final_segment=AsyncMock(),
        _split_message=lambda text: [text],
        client=client,
    )
    install(event, [])
    if fallback:
        await event._send_final_segment("**重点**，自然回答", {"chat_id": "123"})
        assert client.send_message.await_count == 2
        assert client.send_message.await_args.kwargs["parse_mode"] is None
    else:
        with pytest.raises(type(failure)):
            await event._send_final_segment("**重点**，自然回答", {"chat_id": "123"})
        assert client.send_message.await_count == 1


@pytest.mark.asyncio
async def test_unknown_draft_does_not_trigger_native_plain_retry():
    original = AsyncMock(side_effect=TimedOut())
    event = SimpleNamespace(send_streaming=AsyncMock(), _send_message_draft=original)
    install(event, [])
    await event._send_message_draft("123", 5, "内容", parse_mode="MarkdownV2")
    assert original.await_count == 1
