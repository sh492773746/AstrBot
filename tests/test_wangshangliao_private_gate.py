import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.message_components import Plain
from astrbot.core.pipeline.waking_check.stage import (
    WakingCheckStage,
    star_handlers_registry,
)
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window
from astrbot.core.star.filter.command import CommandFilter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,admin,private,expected",
    [
        ("/sid", False, True, 1),
        ("/help", False, True, 1),
        ("/unknown", False, True, 1),
        ("!alias arg", False, True, 1),
        ("!plugin\targ", False, True, 1),
        ("plugin arg", False, True, 1),
        ("hello", False, True, 0),
        ("/help", True, True, 0),
        ("/help", False, False, 0),
    ],
)
async def test_gate_before_plugins_with_replies_disabled(
    monkeypatch, text, admin, private, expected
):
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    msg.self_id = "1"
    msg.session_id = "1/private/2/peer" if private else "1/5"
    msg.group_id = "" if private else "5"
    msg.message_id = "message"
    msg.sender = MessageMember("2", "Member")
    msg.message_str = text
    msg.message = [Plain(text)]
    platform = SimpleNamespace(
        config={"reply_private": False, "reply_groups": {"5": False}},
        meta=lambda: PlatformMetadata("wangshangliao", "test", id="test"),
        send_reply_text=AsyncMock(),
    )
    event = WangshangliaoEvent(msg, platform, False)
    handler = SimpleNamespace(
        event_filters=[CommandFilter("plugin", alias={"alias"})], extras_configs={}
    )
    monkeypatch.setattr(
        star_handlers_registry, "get_handlers_by_event_type", lambda _: [handler]
    )
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(
        astrbot_config={"admins_id": ["2"] if admin else [], "wake_prefix": ["!", "/"]}
    )
    await stage.process(event)
    await stage.process(event)
    assert platform.send_reply_text.await_count == expected
    if expected:
        assert platform.send_reply_text.await_args.args[2] == "无权限"
        assert event.is_stopped()
    else:
        assert not event.is_at_or_wake_command


@pytest.mark.asyncio
async def test_other_platform_unchanged(monkeypatch):
    stage = WakingCheckStage()
    event = SimpleNamespace(
        get_platform_name=lambda: "telegram",
        get_extra=lambda _: True,
        set_extra=lambda *args: None,
    )
    monkeypatch.setattr(
        star_handlers_registry, "get_handlers_by_event_type", lambda _: []
    )
    await stage.process(event)
    assert event.is_wake is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,window_state,expected",
    [
        ("private_commands", "active", 1),
        ("private_commands", "expired", 0),
        ("private_commands", "absent", 0),
        ("private_commands", "unadmitted", 0),
        ("private_ai", "active", 0),
        (None, "active", 0),
    ],
)
async def test_managed_sender_denial_is_bounded_to_admitted_command(
    monkeypatch, scope, window_state, expected
):
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.event.is_managed_account",
        lambda _: True,
    )
    monkeypatch.setattr(
        star_handlers_registry, "get_handlers_by_event_type", lambda _: []
    )
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE
    msg.self_id = "1"
    msg.session_id = "1/private/2/peer"
    msg.group_id = ""
    msg.message_id = "message"
    msg.sender = MessageMember("2", "Member")
    msg.message_str = "禁言 1"
    msg.message = [Plain(msg.message_str)]
    platform = SimpleNamespace(
        account="1",
        nim_account="901",
        enabled_groups=set(),
        stopping=asyncio.Event(),
        config={"reply_private": False},
        meta=lambda: PlatformMetadata("wangshangliao", "test", id="test"),
        send_reply_text=AsyncMock(),
    )
    sender = SimpleNamespace(account="2", stopping=asyncio.Event())
    window = Window(platform, sender, [], ["private_commands"], [], 300, 1)
    if window_state != "unadmitted":
        assert (
            window.admit(
                "private/2/peer", "message", {"sender": "2", "text": msg.message_str}
            )
            == "private_commands"
        )
    if window_state == "expired":
        window.expires = 0
    platform.test_window = None if window_state == "absent" else window
    event = WangshangliaoEvent(msg, platform, False)
    event.set_extra("wsl_test_scope", scope)
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(astrbot_config={"admins_id": [], "wake_prefix": ["/"]})
    await stage.process(event)
    await stage.process(event)
    assert event.is_stopped()
    assert not event.is_admin()
    assert platform.send_reply_text.await_count == expected
    if expected:
        call = platform.send_reply_text.await_args
        assert call.args[2] == "无权限"
        assert call.kwargs == {"test_mid": "message"}
        assert window.replies == {
            ("private/2/peer", "message"): "1/private/2/peer/message/0"
        }
