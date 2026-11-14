"""Retired private routes cannot persist, dispatch, send or invoke business work."""

import asyncio
import importlib.util
import json
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import lz4.block
import nacl.bindings
import pytest
import pytest_asyncio
from test_wangshangliao_native import deployment as deployment

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.builtin_stars.wangshangliao_moderation import (
    feature_image,
    native_guide_upload,
)
from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
from astrbot.builtin_stars.wangshangliao_moderation.main import Main
from astrbot.builtin_stars.wangshangliao_moderation.member_blacklist import (
    MemberBlacklist,
)
from astrbot.builtin_stars.wangshangliao_moderation.member_whitelist import (
    MemberWhitelist,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import RuleReplies
from astrbot.builtin_stars.wangshangliao_moderation.settings import BusinessSettings
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.nim import NimClient
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window


@pytest_asyncio.fixture
async def bot(tmp_path, deployment, wsl_active_policy):
    adapter = WangshangliaoAdapter(
        {"id": "retirement", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.nim_account = "1012"
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock(), request=AsyncMock())
    adapter.connection_state = "online"
    adapter.groups = {"34": "1005"}
    try:
        yield adapter
    finally:
        adapter.stopping.set()
        adapter.dispatch_wakeup.set()
        for task in adapter.workers.values():
            task.cancel()
        await asyncio.gather(*adapter.workers.values(), return_exceptions=True)
        await adapter.ledger.close()


def private_push(*, target=12, kind=0, text="private-body-canary", mid="55"):
    inner = wire.ApplicationMessage(
        sender=wire.Source(id=13),
        target=wire.Source(id=target),
        session=1,
        device=1,
        format=kind,
        content=wire.Content(data=text),
    )
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        lz4.block.compress(inner.SerializeToString(), store_size=True),
        struct.pack("<Q", 6),
        struct.pack("<Q", 5),
        bytes(32),
    )
    envelope = wire.Envelope(
        header=13 << 34 | target << 4 | 1 << 2 | 1,
        timestamp=7,
        nonce=5,
        aad=6,
        body=sealed[:-16],
        tag=sealed[-16:],
    )
    return {
        0: "0",
        1: "1012",
        2: "1013",
        7: str(int(time.time() * 1000)),
        8: "100",
        10: json.dumps({"b": wire.b64(envelope.SerializeToString())}),
        12: mid,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,text", [(0, "菜单"), (0, "/chat"), (0, "黑名单"), (1, "")]
)
async def test_private_push_acknowledged_without_retention_or_dispatch(
    bot, monkeypatch, kind, text
):
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    observer = Mock()
    monkeypatch.setattr(runtime, "observe", observer)
    push = private_push(kind=kind, text=text)
    decoder = Mock(side_effect=AssertionError("private body must not be decoded"))
    monkeypatch.setattr(wire, "open_message", decoder)
    await bot.ingest(push)
    await bot.ingest(push)
    assert bot.nim.acknowledge.await_count == 2
    bot.nim.acknowledge.assert_awaited_with(0, "55")
    async with bot.ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
        assert (await cursor.fetchone())[0] == 0
    assert bot._event_queue.empty() and not bot.dispatch_wakeup.is_set()
    observer.assert_not_called()
    decoder.assert_not_called()
    bot.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authenticated,recipient,peer",
    [
        ("", "1012", "1013"),
        ("1012", "other-account", "1013"),
        ("1012", "1012", "1012"),
        ("1012", "1012", ""),
        ("1012", "1012", "x" * 1025),
        ("1012", "1012", None),
    ],
)
async def test_private_ack_requires_authenticated_transport_identity(
    bot, authenticated, recipient, peer
):
    bot.nim_account = authenticated
    push = private_push()
    push.update({1: recipient, 2: peer})
    await bot.ingest(push)
    bot.nim.acknowledge.assert_not_awaited()
    bot.nim.request.assert_not_awaited()
    assert bot._event_queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send_text", "send_reply_text"])
async def test_private_send_rejected_before_accessing_runtime_or_ledger(method):
    adapter = object.__new__(WangshangliaoAdapter)
    with pytest.raises(wire.ProtocolError, match="private_messages_retired"):
        await getattr(adapter, method)("private/13/MTAxMw==", "no-intent", "canary")


@pytest.mark.asyncio
async def test_private_proactive_send_rejected_before_configuration_access():
    adapter = object.__new__(WangshangliaoAdapter)
    session = SimpleNamespace(message_type=MessageType.FRIEND_MESSAGE)
    with pytest.raises(wire.ProtocolError, match="private_messages_retired"):
        await adapter.send_by_session(session, MessageChain([Plain("canary")]))


@pytest.mark.asyncio
async def test_historical_private_pending_not_dispatched_or_deleted(bot):
    now = time.time()
    payload = {
        "sender": "13",
        "name": "fixture",
        "text": "historical-private-canary",
        "created_at": int(now * 1000),
        "mentions": [],
    }
    route = "private/13/MTAxMw=="
    # Seed historical evidence directly; live ingress no longer accepts this route.
    await bot.ledger.db.execute(
        "INSERT INTO inbox(account,team,message,payload,state) "
        "VALUES('12',?,'old-private',?,'pending')",
        (route, json.dumps(payload)),
    )
    await bot.ledger.db.commit()
    assert not await bot.ledger.ingest("12", route, "new-private", payload)
    await bot.process_group(route)
    assert bot._event_queue.empty()
    await bot.ledger.ingest("12", "34", "group", {**payload, "text": "group-canary"})
    dispatcher = asyncio.create_task(bot.dispatch_pending())
    try:
        event = await asyncio.wait_for(bot._event_queue.get(), 2)
        assert event.message_obj.type is MessageType.GROUP_MESSAGE
        assert event.get_group_id() == "34"
        assert event.message_str == "group-canary"
        event.processing_completion.set_result(None)
        async with bot.ledger.db.execute(
            "SELECT payload,state FROM inbox WHERE message='old-private'"
        ) as cursor:
            encoded, state = await cursor.fetchone()
        assert json.loads(encoded) == payload and state == "pending"
        async with bot.ledger.db.execute(
            "SELECT 1 FROM inbox WHERE message='new-private'"
        ) as cursor:
            assert await cursor.fetchone() is None
    finally:
        bot.stopping.set()
        bot.dispatch_wakeup.set()
        await asyncio.wait_for(dispatcher, 2)


@pytest.mark.asyncio
async def test_cached_private_event_cannot_wake_or_send(bot):
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "12"
    message.session_id = "12/private/13/MTAxMw=="
    message.group_id = ""
    message.message_id = "old"
    message.sender = MessageMember("13", "fixture")
    message.message_str = "菜单"
    event = WangshangliaoEvent(message, bot, True)
    event.set_extra("wsl_plain_command", True)
    event.set_extra("wsl_test_scope", "private_commands")
    assert not event.eligible
    assert not await event.prepare_waking_check({"admins_id": ["13"]}, None)
    bot.send_reply_text = AsyncMock()
    await event.send(MessageChain([Plain("canary")]))
    bot.send_reply_text.assert_not_awaited()
    assert event.is_stopped()
    event.processing_completion.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["_moderate", "moderation_command"])
async def test_private_plugin_entry_stops_before_service(entry):
    plugin = object.__new__(Main)
    plugin.open_service = SimpleNamespace(on_event=AsyncMock())
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        stop_event=Mock(),
    )
    args = ("黑名单",) if entry == "moderation_command" else ()
    await getattr(plugin, entry)(event, *args)
    event.stop_event.assert_called_once()
    plugin.open_service.on_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_model_request_stops_before_conversation_work():
    plugin = object.__new__(Main)
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        stop_event=Mock(),
    )
    request = SimpleNamespace(func_tool="old-tools", tool_calls_result="old-result")
    await plugin.plain_text_request(event, request)
    assert isinstance(request.func_tool, ToolSet) and request.tool_calls_result is None
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_cached_private_command_does_not_construct_selection_or_services():
    commands = object.__new__(Commands)
    assert (
        await commands.execute(
            SimpleNamespace(is_private_chat=lambda: True), "禁言", "1"
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("service_type", [MemberBlacklist, MemberWhitelist])
async def test_private_member_list_helpers_reject_before_scope_or_service_access(
    service_type,
):
    service = object.__new__(service_type)
    event = SimpleNamespace(is_private_chat=lambda: True)
    assert await service.command(event, "确认黑名单 old-token") == ""
    with pytest.raises(OpenError, match="private_messages_retired"):
        await service.run(
            None, "34", "commit", event=event, actor="old-actor", session="old-session"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["context", "command"])
async def test_private_rule_helpers_reject_before_identity_navigation_or_storage(entry):
    rules = object.__new__(RuleReplies)
    event = SimpleNamespace(is_private_chat=lambda: True)
    args = ("定时发言",) if entry == "command" else ()
    with pytest.raises(OpenError, match="^private_messages_retired$"):
        await getattr(rules, entry)(event, *args)
    assert vars(rules) == {}


def test_private_delivery_implementations_do_not_exist():
    assert not hasattr(feature_image, "FeatureImage")
    assert not hasattr(feature_image, "FeatureGuides")
    assert not hasattr(native_guide_upload, "FeatureGuides")
    assert feature_image.ImageMessage.DESCRIPTOR.fields_by_name["image"].number == 101
    assert not hasattr(NimClient, "get_friends")
    assert not hasattr(NimClient, "get_friend_names")
    assert not hasattr(Window, "send_command")
    assert not hasattr(WangshangliaoEvent, "bind_management_reply_guard")


@pytest.mark.parametrize(
    "module",
    [
        "friend_auto",
        "private_batches",
        "private_interaction",
        "private_invitation",
        "owner_support",
        "service_settings",
        "consultation_access",
    ],
)
def test_private_business_modules_do_not_exist(module):
    assert (
        importlib.util.find_spec(
            f"astrbot.builtin_stars.wangshangliao_moderation.{module}"
        )
        is None
    )


def test_private_observation_stops_before_snapshot_or_finance_read():
    settings = object.__new__(BusinessSettings)
    payload = {"text": "finance-canary"}
    settings.observe(None, "12", "private/13/MTAxMw==", "old", payload, time.time())
    assert payload == {"text": "finance-canary"}


@pytest.mark.parametrize("text", ["/chat", "/exit", "管理 群列表", "/管理 选择群 1"])
def test_private_only_aliases_are_not_admitted_as_group_commands(text):
    assert recognize_command(text) == ""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("@成员 黑名单", "黑名单 @成员"),
        ("白名单 @成员", "白名单 @成员"),
        ("排名", "排名"),
    ],
)
def test_group_command_recognition_survives_retirement(text, expected):
    assert recognize_command(text) == expected
