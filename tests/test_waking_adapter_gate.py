"""Adapter-owned early gates must not weaken the shared waking pipeline."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.api.message_components import Plain
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata


@pytest.fixture
def make_event():
    def create(platform):
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.sender = MessageMember("2", "Member")
        message.self_id = "1"
        message.message_id = "m"
        message.session_id = "private"
        message.message_str = "hello"
        message.message = [Plain("hello")]
        event = AstrMessageEvent(
            "hello",
            message,
            PlatformMetadata(platform, "test", id="fixture"),
            "private",
        )
        event.set_extra("_context_only", True)
        return event

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["telegram", "lajiaoliao", "third_party"])
async def test_default_gate_preserves_context_only_events(make_event, platform):
    event = make_event(platform)
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(astrbot_config={})
    await stage.process(event)
    assert not event.is_stopped()
    assert event.get_extra("handlers_parsed_params") == {}
    assert not event.is_wake


@pytest.mark.asyncio
async def test_denied_adapter_gate_stops_before_context_admission(make_event):
    event = make_event("third_party")
    event.prepare_waking_check = AsyncMock(return_value=False)
    stage = WakingCheckStage()
    config = {"admins_id": ["2"]}
    stage.ctx = SimpleNamespace(astrbot_config=config)
    await stage.process(event)
    assert event.is_stopped()
    assert event.get_extra("handlers_parsed_params") is None
    assert event.prepare_waking_check.await_args.args[0] is config


@pytest.mark.asyncio
async def test_failed_adapter_gate_never_falls_back_to_commands(make_event):
    event = make_event("third_party")
    event.prepare_waking_check = AsyncMock(side_effect=RuntimeError("denied"))
    event.send = AsyncMock()
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(astrbot_config={})
    with pytest.raises(RuntimeError, match="denied"):
        await stage.process(event)
    assert event.get_extra("handlers_parsed_params") is None
    event.send.assert_not_awaited()


def test_shared_waking_source_has_no_wsl_import_or_platform_branch():
    from pathlib import Path

    from astrbot.core.pipeline.waking_check import stage

    source = Path(stage.__file__).read_text()
    assert "wangshangliao" not in source
    assert "prepare_waking_check" in source


@pytest.mark.asyncio
async def test_adapter_gate_runs_before_context_only_marker(make_event):
    event = make_event("third_party")
    original = event.get_extra
    visited = []

    async def prepare(*_):
        visited.append("gate")
        return True

    def extra(key):
        if key == "_context_only":
            visited.append("context")
        return original(key)

    event.prepare_waking_check = prepare
    event.get_extra = Mock(side_effect=extra)
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(astrbot_config={})
    await stage.process(event)
    assert visited == ["gate", "context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["telegram", "lajiaoliao", "third_party"])
async def test_default_builtin_help_preserves_other_platforms(
    monkeypatch, make_event, platform
):
    from astrbot.builtin_stars.builtin_commands.commands import help as help_module

    event = make_event(platform)
    command = help_module.HelpCommand(None)
    command._query_astrbot_notice = AsyncMock(return_value="Notice")
    command._build_reserved_command_lines = AsyncMock(return_value=["/existing"])
    monkeypatch.setattr(
        help_module, "get_dashboard_version", AsyncMock(return_value="fixture")
    )
    await command.help(event)
    command._query_astrbot_notice.assert_awaited_once()
    assert not event.is_stopped()
    assert "/existing" in event.get_result().get_plain_text()
    assert "Notice" in event.get_result().get_plain_text()


def test_builtin_help_does_not_import_the_native_business_extension():
    from pathlib import Path

    from astrbot.builtin_stars.builtin_commands.commands import help as help_module

    assert "wangshangliao" not in Path(help_module.__file__).read_text()
