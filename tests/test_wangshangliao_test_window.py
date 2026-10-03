import asyncio
from types import SimpleNamespace

import pytest

from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.parametrize("text", ["/help", "/帮助", "/群管帮助"])
def test_private_help_aliases_admitted(text):
    from astrbot.core.platform.sources.wangshangliao.wire import b64
    a = SimpleNamespace(account="1", nim_account="901", enabled_groups={"5"}, stopping=asyncio.Event())
    b = SimpleNamespace(account="2", stopping=asyncio.Event())
    w = Window(a, b, [], ["private_commands"], [], 300, 1)
    a.test_window = w
    assert w.send_command(b, "private/1/" + b64(b"901"), text, "m")
    assert w.admit("private/2/peer", "m", {"sender": "2", "text": text}) == "private_commands"
    assert not w.admit("5", "m2", {"sender": "2", "text": text})


def test_private_ai_scope_is_bounded_and_not_commands():
    from astrbot.core.platform.sources.wangshangliao.wire import b64

    a = SimpleNamespace(account="1", nim_account="901", enabled_groups={"5"}, stopping=asyncio.Event())
    b = SimpleNamespace(account="2", stopping=asyncio.Event())
    w = Window(a, b, [], ["private_ai"], [], 300, 1)
    a.test_window = w
    target = "private/1/" + b64(b"901")
    assert not w.send_command(b, target, "/help", "c")
    assert w.send_command(b, target, "hello", "m")
    assert not w.admit("private/2/peer", "c", {"sender": "2", "text": "/群管 禁言 1"})
    assert not w.admit("5", "g", {"sender": "2", "text": "hello"})
    assert w.admit("private/2/peer", "m", {"sender": "2", "text": "hello"}) == "private_ai"
    assert not w.admit("private/2/peer", "n", {"sender": "2", "text": "again"})
    assert w.reply("private/2/peer", "m")
    assert not w.reply("private/2/peer", "m")


def test_group_ai_requires_native_mention_and_is_bounded():
    a = SimpleNamespace(account="1", nim_account="901", enabled_groups={"5"}, stopping=asyncio.Event())
    b = SimpleNamespace(account="2", stopping=asyncio.Event())
    w = Window(a, b, ["5"], ["group_ai"], [], 300, 1)
    payload = {"sender": "2", "text": "@bot hello", "mentions": ["901"]}
    assert not w.admit("5", "fake", {**payload, "mentions": []})
    assert not w.admit("5", "wrong", {**payload, "mentions": ["902"]})
    assert not w.admit("6", "other", payload)
    assert not w.admit("5", "command", {**payload, "text": "排名"})
    assert not w.admit("5", "slash", {**payload, "text": "/help"})
    assert not w.admit("5", "sender", {**payload, "sender": "3"})
    assert w.admit("5", "m", payload) == "group_ai"
    assert not w.admit("5", "second", payload)
    assert w.reply("5", "m", "operation")
    assert w.reply("5", "m", "operation")
    assert not w.reply("5", "m", "different")
    w.expires = 0
    assert not w.reply("5", "m", "operation")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,active", [(True, True), (False, True), (True, False)])
async def test_group_ai_event_requires_reply_switch_and_live_window(monkeypatch, enabled, active):
    from unittest.mock import AsyncMock

    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.wangshangliao import event as native

    monkeypatch.setattr(native, "is_managed_account", lambda _: True)
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.self_id, msg.session_id, msg.group_id, msg.message_id = "1", "1/5", "5", "m"
    msg.sender = MessageMember("2", "Test")
    msg.message_str, msg.message = "hello", [Plain("hello")]
    platform = SimpleNamespace(
        account="1", nim_account="901", enabled_groups={"5"}, stopping=asyncio.Event(),
        members={"5": {"2": "902"}}, config={"reply_groups": {"5": enabled}},
        send_reply_text=AsyncMock(), meta=lambda: PlatformMetadata("wangshangliao", "test", id="test"),
    )
    sender = SimpleNamespace(account="2", stopping=asyncio.Event())
    window = Window(platform, sender, ["5"], ["group_ai"], [], 300, 1)
    platform.test_window = window
    window.admit("5", "m", {"sender": "2", "text": "hello", "mentions": ["901"]})
    if not active:
        window.expires = 0
    event = native.WangshangliaoEvent(msg, platform, True)
    event.set_extra("wsl_test_scope", "group_ai")
    await event.send(MessageChain([Plain("reply")]))
    await event.send(MessageChain([Plain("second reply")]))
    assert platform.send_reply_text.await_count == int(enabled and active)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_ai_event_reply_respects_private_switch(monkeypatch, enabled):
    from unittest.mock import AsyncMock

    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.wangshangliao import event as native

    monkeypatch.setattr(native, "is_managed_account", lambda _: True)
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE
    msg.self_id = "1"
    msg.session_id = "1/private/2/peer"
    msg.group_id = ""
    msg.message_id = "m"
    msg.sender = MessageMember("2", "Test")
    msg.message_str = "hello"
    msg.message = [Plain("hello")]
    platform = SimpleNamespace(account="1", config={"reply_private": enabled}, send_reply_text=AsyncMock(), meta=lambda: PlatformMetadata("wangshangliao", "test", id="test"))
    event = native.WangshangliaoEvent(msg, platform, True)
    event.set_extra("wsl_test_scope", "private_ai")
    await event.send(MessageChain([Plain("reply")]))
    assert platform.send_reply_text.await_count == int(enabled)
    if enabled:
        assert platform.send_reply_text.call_args.kwargs["test_mid"] == "m"


def test_private_outbound_shares_window_and_never_opens_chat():
    from astrbot.core.platform.sources.wangshangliao.wire import b64

    a = SimpleNamespace(account="1", nim_account="901", enabled_groups={"5"}, stopping=asyncio.Event())
    b = SimpleNamespace(account="2", nim_account="902", stopping=asyncio.Event())
    w = Window(a, b, [], ["private_commands"], [], 300, 1)
    a.test_window = w
    target = "private/1/" + b64(b"901")
    assert not w.send_command(a, target, "/群管 帮助", "one")
    assert not w.send_command(b, "private/1/wrong", "/群管 帮助", "one")
    assert not w.send_command(b, target, "hello", "one")
    assert w.send_command(b, target, "/群管 帮助", "one", consume=True)
    assert w.send_command(b, target, "/群管 帮助", "one", consume=True)
    assert not w.send_command(b, target, "/群管 帮助", "two", consume=True)
    a.test_window = None
    assert not w.send_command(b, target, "/群管 帮助", "one")


def test_window_scope_budget_and_result():
    a = SimpleNamespace(account="1", enabled_groups={"5"}, stopping=asyncio.Event())
    b = SimpleNamespace(account="2", stopping=asyncio.Event(), config={"id": "b"})
    w = Window(a, b, ["5"], ["private_commands", "group_rules"], ["WSL_TEST_ONLY"], 300, 1)
    assert not w.admit("private/2/peer", "0", {"sender": "2", "text": "hello"})
    payload = {"sender": "2", "text": "/群管 帮助"}
    assert w.admit("private/2/peer", "1", payload) == "private_commands"
    assert w.admit("private/2/peer", "1", payload) == "private_commands"
    assert not w.admit("private/2/peer", "2", payload)
    assert w.reply("private/2/peer", "1")
    assert not w.reply("private/2/peer", "1")
    b.stopping.set()
    assert not w.active()


@pytest.mark.parametrize("field,value", [
    ("groups", [{}]), ("scopes", [[]]), ("seconds", True),
    ("seconds", 301), ("budget", 11), ("budget", 0),
    ("scopes", []), ("groups", ["6"]),
    ("scopes", ["group_commands"]),
])
def test_invalid_window_arguments(field, value):
    a = SimpleNamespace(account="1", enabled_groups={"5"}, stopping=asyncio.Event())
    b = SimpleNamespace(account="2", stopping=asyncio.Event())
    arguments = {"groups": [], "scopes": ["private_commands"], "keywords": [], "seconds": 300, "budget": 10}
    arguments[field] = value
    with pytest.raises(ProtocolError):
        Window(a, b, **arguments)


@pytest.mark.parametrize("side,field", [("receiver", "account"), ("sender", "account"), ("sender", "nim_account")])
def test_account_replacement_closes_window(side, field):
    a = SimpleNamespace(account="1", nim_account="n1", enabled_groups={"5"}, stopping=asyncio.Event())
    b = SimpleNamespace(account="2", nim_account="n2", stopping=asyncio.Event())
    w = Window(a, b, [], ["private_commands"], [], 300, 10)
    setattr(getattr(w, side), field, "replacement")
    assert not w.active()
    assert not w.admit("private/2/n2", "m", {"sender": "2", "text": "/群管 帮助"})


def test_group_scope_identity_keyword_and_expiry():
    a = SimpleNamespace(account="1", nim_account="n1", enabled_groups={"5"}, stopping=asyncio.Event())
    b = SimpleNamespace(account="2", stopping=asyncio.Event())
    w = Window(a, b, ["5"], ["group_commands", "group_rules"], ["WSL_TEST_ONLY"], 300, 10)
    payload = {"sender": "2", "text": "@bot /群管 帮助", "mention_spans": [{"uid": "n1", "nick": "bot", "start": 0, "end": 4}]}
    assert not w.admit("6", "m", payload)
    assert not w.admit("5", "m", {**payload, "sender": "3"})
    assert w.admit("5", "m", payload) == "group_commands"
    assert not w.admit("5", "other", {"sender": "2", "text": "normal chat"})
    assert w.admit("5", "rule", {"sender": "2", "text": "WSL_TEST_ONLY"}) == "group_rules"
    assert not w.reply("5", "rule")
    w.expires = 0
    assert not w.reply("5", "m")
