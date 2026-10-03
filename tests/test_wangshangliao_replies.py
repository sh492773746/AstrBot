from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,enabled,mentioned",
    [(True, False, True), (False, False, True), (False, True, False)],
)
async def test_disabled_or_unmentioned_reply_never_sends(private, enabled, mentioned):
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.self_id = "1"
    message.session_id = "1/private/2/cGVlcg" if private else "1/5"
    message.group_id = "" if private else "5"
    message.message_id = "test"
    message.sender = MessageMember("2", "Test")
    message.message_str = "test"
    message.message = [Plain("test")]
    platform = SimpleNamespace(
        config={"reply_private": enabled, "reply_groups": {"5": enabled}},
        send_text=AsyncMock(),
        meta=lambda: PlatformMetadata("wangshangliao", "test", id="test"),
    )
    event = WangshangliaoEvent(message, platform, mentioned)
    assert not event.eligible
    assert event.get_extra("_context_only")
    await event.send(MessageChain([Plain("reply")]))
    platform.send_text.assert_not_awaited()
