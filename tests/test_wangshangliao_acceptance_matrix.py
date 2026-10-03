"""Exhaustive capability gates and direct model-entry counters."""

from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.message_components import Plain
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.policy import ACTIONS, authorize_action
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.parametrize(
    "enabled,moderation,group,grant", list(product([False, True], repeat=4))
)
@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_all_action_authorization_combinations(
    enabled, moderation, group, grant, action
):
    config = {
        "enable": enabled,
        "enabled_groups": ["5"] if group else [],
        "moderation": {
            "enabled": moderation,
            "permissions": {"5": [action] if grant else []},
        },
    }
    if all([enabled, moderation, group, grant]):
        authorize_action(config, "5", action)
    else:
        with pytest.raises(ProtocolError, match="moderation_permission"):
            authorize_action(config, "5", action)
    with pytest.raises(ProtocolError):
        authorize_action(config, "6", action)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,enabled,mentioned",
    [(True, False, True), (False, False, True), (False, True, False)],
)
async def test_disabled_message_model_entry_count_zero(
    monkeypatch, private, enabled, mentioned
):
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    msg.self_id = "1"
    msg.session_id = "1/private/2/peer" if private else "1/5"
    msg.group_id = "" if private else "5"
    msg.message_id = "counter-test"
    msg.sender = MessageMember("2", "Test")
    msg.message_str = "hello"
    msg.message = [Plain("hello")]
    platform = SimpleNamespace(
        config={"reply_private": enabled, "reply_groups": {"5": enabled}},
        meta=lambda: PlatformMetadata("wangshangliao", "test", id="test"),
    )
    event = WangshangliaoEvent(msg, platform, mentioned)
    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type",
        lambda _: [],
    )
    waking = WakingCheckStage()
    waking.ctx = SimpleNamespace(astrbot_config={"admins_id": [], "wake_prefix": ["/"]})
    await waking.process(event)
    counter = AsyncMock()

    async def model_entry(_):
        await counter()
        yield None

    process = ProcessStage()
    process.ctx = SimpleNamespace(
        astrbot_config={"provider_settings": {"enable": True}}
    )
    process.agent_sub_stage = SimpleNamespace(process=model_entry)
    async for _ in process.process(event):
        pass
    counter.assert_not_awaited()
    # Positive control proves the counter is attached to the model entry.
    event.is_at_or_wake_command = True
    async for _ in process.process(event):
        pass
    counter.assert_awaited_once()
