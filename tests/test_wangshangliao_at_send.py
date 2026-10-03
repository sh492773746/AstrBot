from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter


@pytest.mark.asyncio
async def test_at_mapping_utf16_and_wire_roundtrip():
    adapter = SimpleNamespace(config={"id": "fixture", "enabled_groups": ["5"],
        "proactive_send": {"enabled": True}}, groups={"5": "cloud"},
        members={"5": {"2": "902"}}, send_reply_text=AsyncMock())
    session = MessageSession("fixture", MessageType.GROUP_MESSAGE, "1/5")
    await WangshangliaoAdapter.send_by_session(adapter, session,
        MessageChain([Plain("\U0001f600"), At(qq="2", name="Test"), Plain("hello")]))
    call = adapter.send_reply_text.call_args
    people = call.kwargs["mentions"]
    assert people == [{"uid": 902, "nick": "Test", "start": 2, "end": 8}]
    message = wire.ApplicationMessage(sender=wire.Source(id=1), target=wire.Source(id=5),
                                     session=2, device=1, format=0)
    message.mentions.content.data = call.args[2]
    message.mentions.people.add(**people[0])
    sealed = wire.seal_message(bytes(32), message, 1, 2, 3)
    assert wire.open_message(bytes(32), sealed) == message
    message.mentions.people[0].start = 1
    with pytest.raises(wire.ProtocolError):
        wire.seal_message(bytes(32), message, 1, 2, 3)
    with pytest.raises(wire.ProtocolError, match="mention_identity"):
        await WangshangliaoAdapter.send_by_session(adapter, session, MessageChain([At(qq="9")]))
    assert adapter.send_reply_text.await_count == 1
