from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.core.platform.sources.lajiaoliao.event import (
    chain_to_parts,
    incoming_to_message,
)
from astrbot.core.platform.sources.lajiaoliao.protocol import (
    IncomingMessage,
    message_payload,
    session_id_for,
)
import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from astrbot.core.platform.sources.lajiaoliao.adapter import LajiaoLiaoAdapter


def test_incoming_group_message_maps_to_astrbot():
    payload = {
        "message_id": "m-1",
        "sender_id": "u-1",
        "sender_name": "测试用户",
        "target_id": "g-1",
        "is_group": True,
        "timestamp": 123,
        "text": "你好",
    }
    message = incoming_to_message(payload, "bot-1")
    assert message.self_id == "bot-1"
    assert message.group_id == "g-1"
    assert message.session_id == "group:g-1"
    assert message.message_str == "你好"


def test_protocol_session_and_send_envelope():
    incoming = IncomingMessage(
        "m-1", "u-1", "user", "g-1", "text", True, 1
    )
    assert session_id_for(incoming) == "group:g-1"
    assert message_payload("g-1", "m-1", [{"type": "text", "text": "ok"}]) == {
        "target_id": "g-1",
        "reply_to": "m-1",
        "parts": [{"type": "text", "text": "ok"}],
    }


def test_chain_maps_text_and_mentions():
    parts = chain_to_parts(
        [Plain("hi "), At(qq="42", name="Alice")],
        {"42": "openim-42"},
    )
    assert parts == [
        {"type": "text", "text": "hi "},
        {"type": "mention", "user_id": "openim-42", "name": "Alice"},
    ]


def test_media_has_explicit_bridge_part():
    parts = chain_to_parts([Image(file="https://example.test/a.png")])
    assert parts == [{"type": "image", "url": "https://example.test/a.png"}]


def test_incoming_components_are_preserved():
    message = incoming_to_message(
        {
            "message_id": "m-2",
            "sender_id": "u-1",
            "target_id": "u-2",
            "text": "请看",
            "components": [
                {"type": "image", "url": "https://example.test/a.png"},
                {"type": "quote", "message_id": "m-1"},
            ],
        },
        "bot-1",
    )
    assert isinstance(message.message[0], Plain)
    assert isinstance(message.message[1], Image)
    assert isinstance(message.message[2], Reply)


@pytest.mark.asyncio
async def test_bridge_stream_queues_once_acks_duplicates_and_blocks_unlisted_groups():
    queue = asyncio.Queue()
    adapter = LajiaoLiaoAdapter(
        {"id": "lajiao-test", "enabled_groups": ["test-group"]}, {}, queue
    )
    task_id = adapter.client_self_id
    def incoming(mid, target, kind):
        return {"type": "message", "data": {
            "message_id": mid, "sender_id": "user",
            "target_id": target, "kind": kind, "text": "test",
        }}
    events = [
        {"type": "ready", "data": {"status": "online", "account_id": "bot"}},
        incoming("m1", "test-group", "group"),
        incoming("m1", "test-group", "group"),
        incoming("m2", "other-group", "group"),
        incoming("m3", "user", "private"),
    ]
    sent = []
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            if not events:
                raise StopAsyncIteration
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(events.pop(0)))
        async def send_json(self, value):
            sent.append(value)
    adapter.http = SimpleNamespace(ws_connect=lambda *args, **kwargs: Socket())
    with pytest.raises(RuntimeError, match="bridge_events_closed"):
        await adapter._consume_events()
    assert adapter.account_id == "bot"
    assert adapter.client_self_id == task_id
    assert queue.qsize() == 2
    assert len(sent) == 4
    assert queue.get_nowait().message_obj.group_id == "test-group"


@pytest.mark.asyncio
async def test_empty_group_allowlist_is_fail_closed():
    adapter = LajiaoLiaoAdapter({"id": "lajiao-test"}, {}, asyncio.Queue())
    message = incoming_to_message({
        "message_id": "m1", "sender_id": "user", "target_id": "group",
        "kind": "group", "text": "test",
    }, "bot")
    assert not adapter.create_event(message).enabled


@pytest.mark.asyncio
async def test_reply_preserves_target_kind_without_implicit_quote(monkeypatch):
    from unittest.mock import AsyncMock
    from astrbot.api.event import AstrMessageEvent
    monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
    from astrbot.core.platform.sources.lajiaoliao.event import LajiaoLiaoEvent
    adapter = LajiaoLiaoAdapter({"id": "lajiao-test"}, {}, asyncio.Queue())
    sent = []
    async def send(**kwargs):
        sent.append(kwargs)
    adapter.send_parts = send
    message = incoming_to_message({
        "message_id": "m1", "sender_id": "user", "target_id": "group",
        "kind": "group", "text": "test",
    }, "bot")
    event = LajiaoLiaoEvent(message, adapter, True)
    from astrbot.api.event import MessageChain
    await event.send(MessageChain([Plain("reply")]))
    assert sent[0]["kind"] == "group"
    assert sent[0]["reply_to"] == ""
