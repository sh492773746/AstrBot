"""Offline coverage for removing private business from the group-only core."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.wangshangliao import adapter as native
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.business_http import request_business
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.nim import NimClient, messages
from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
    BUSINESS_FIELDS,
    PolicyRuntime,
    PolicySnapshot,
    runtime,
)
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window

KEY = bytes(32)
PRIVATE = "private/2/OTAwMg=="


@pytest_asyncio.fixture
async def bot(tmp_path, monkeypatch, wsl_active_policy):
    """Create an isolated ledger and mocked transport with an active group policy."""
    monkeypatch.setattr(runtime, "open_service", None)
    adapter = native.WangshangliaoAdapter(
        {"id": "core-private-removal", "account_id": "1", "enabled_groups": ["5"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(deployment=SimpleNamespace(key=lambda _: KEY))
    adapter.nim_account = "9001"
    adapter.nim = SimpleNamespace(
        acknowledge=AsyncMock(),
        request=AsyncMock(
            return_value=(
                200,
                wire.properties([(7, b"1000"), (12, b"800")]),
            )
        ),
    )
    adapter.connection_state = "online"
    adapter.groups = {"5": "8005"}
    adapter.members = {"5": {"1": "9001", "2": "9002"}}
    adapter.diagnostics.emit = Mock()
    try:
        yield adapter
    finally:
        adapter.stopping.set()
        adapter.dispatch_wakeup.set()
        for task in adapter.workers.values():
            task.cancel()
        await asyncio.gather(*adapter.workers.values(), return_exceptions=True)
        for event in adapter.events:
            event.processing_completion.cancel()
        await adapter.ledger.close()


def push(*, scene="0", body=b"private-body-canary", kind="100", mid="55"):
    """Build NIM properties independently from application message decoding."""
    return wire.properties(
        [
            (0, scene.encode()),
            (1, b"9001" if scene == "0" else b"8005"),
            (2, b"9002"),
            (7, b"1000"),
            (8, kind.encode()),
            (10, body),
            (12, mid.encode()),
        ]
    )


@pytest.mark.parametrize("command", [2, 101])
@pytest.mark.parametrize("body", [b"private-body-canary", b"\xff\xfe"])
def test_private_transport_decodes_only_ack_headers(command, body):
    decoded, boundary = messages((7, command, 1, 200, push(body=body)))
    assert decoded == [{0: "0", 1: "9001", 2: "9002", 7: "1000", 8: "100", 12: "55"}]
    assert boundary is None


def test_mixed_sync_batch_preserves_group_body_and_boundary():
    group = push(scene="1", body=b"group-body", mid="56")
    packet = (4, 4, 1, 200, wire.varint(2) + push(body=b"\xff") + group)
    decoded, boundary = messages(packet)
    assert 10 not in decoded[0]
    assert decoded[1][10] == "group-body"
    assert boundary is None
    assert messages((5, 1, 1, 200, (123).to_bytes(8, "little"))) == ([], 123)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["100", "5", "0", "3"])
async def test_private_ingress_never_decrypts_observes_retains_or_sends(
    bot, monkeypatch, kind
):
    decrypt = Mock(side_effect=AssertionError("private body must not be opened"))
    observer = Mock()
    monkeypatch.setattr(wire, "open_message", decrypt)
    monkeypatch.setattr(runtime, "observe", observer)
    bot.business = None
    outer = {0: "0", 1: "9001", 2: "9002", 8: kind, 10: "canary", 12: "55"}
    await bot.ingest(outer)
    await bot.ingest(outer)
    assert bot.nim.acknowledge.await_count == 2
    bot.nim.acknowledge.assert_awaited_with(0, "55")
    decrypt.assert_not_called()
    observer.assert_not_called()
    bot.nim.request.assert_not_awaited()
    assert not bot.workers and not bot.events and bot._event_queue.empty()
    assert not bot.dispatch_wakeup.is_set()
    assert bot.receive_health["decoded_messages"] == 0
    async with bot.ledger.db.execute(
        "SELECT (SELECT COUNT(*) FROM inbox),(SELECT COUNT(*) FROM outbox)"
    ) as cursor:
        assert await cursor.fetchone() == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recipient,peer",
    [
        ("other", "9002"),
        ("9001", "9001"),
        ("9001", ""),
        ("9001", "x" * 1025),
        ("9001", None),
    ],
)
async def test_private_ack_requires_current_transport_recipient(bot, recipient, peer):
    await bot.ingest({0: "0", 1: recipient, 2: peer, 12: "55"})
    bot.nim.acknowledge.assert_not_awaited()
    assert bot._event_queue.empty()


@pytest.mark.asyncio
async def test_mixed_push_consumer_preserves_group_notifications_and_sync(bot):
    bot.nim.closed = asyncio.Event()
    bot.nim.pushes = asyncio.Queue()
    group = push(
        scene="1",
        kind="5",
        mid="56",
        body=b'{"id":0,"data":{"ids":["9003"]}}',
    )
    await bot.nim.pushes.put(
        (4, 4, 1, 200, wire.varint(2) + push(body=b"\xff\xfe") + group)
    )
    await bot.nim.pushes.put((5, 1, 1, 200, (123).to_bytes(8, "little")))
    consumer = asyncio.create_task(bot.consume_nim_pushes(bot.nim))
    try:
        for _ in range(200):
            async with bot.ledger.db.execute("SELECT cursor FROM sync") as cursor:
                boundary = await cursor.fetchone()
            if boundary == ("123",):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("mixed push did not reach its sync boundary")
        assert [call.args for call in bot.nim.acknowledge.await_args_list] == [
            (0, "55"),
            (1, "56"),
        ]
        async with bot.ledger.db.execute(
            "SELECT team,message,payload,state FROM inbox"
        ) as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 1
        team, mid, encoded, state = rows[0]
        assert (team, mid, state) == ("5", "56", "invite_notification")
        assert json.loads(encoded)["member_peers"] == ["9003"]
        assert "text" not in json.loads(encoded)
        bot.nim.request.assert_not_awaited()
        assert bot._event_queue.empty()
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "ignored_bot", "business_suspended"])
async def test_ledger_refuses_new_private_rows_including_witnesses(bot, state):
    observer, authorize = Mock(), Mock()
    assert not await bot.ledger.ingest(
        "1", PRIVATE, "new", {"text": "canary"}, state=state, observe=observer
    )
    assert not await bot.ledger.ingest(
        "1",
        PRIVATE,
        "witness",
        {"text": "canary"},
        state="ignored_bot",
        authorize=authorize,
    )
    assert not await bot.ledger._ingest_witness(
        "1", PRIVATE, "direct", {"text": "canary"}, authorize
    )
    observer.assert_not_called()
    authorize.assert_not_called()
    async with bot.ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_historical_private_control_is_untouched_and_never_dispatched(bot):
    payload = {
        "sender": "2",
        "name": "fixture",
        "text": "historical-private-canary",
        "created_at": 1000,
        "mentions": [],
    }
    encoded = json.dumps(payload)
    await bot.ledger.db.execute(
        "INSERT INTO inbox(account,team,message,payload,state,sender,dispatch_class) "
        "VALUES('1',?,'old',?,'pending','9002','control')",
        (PRIVATE, encoded),
    )
    await bot.ledger.db.commit()
    await bot.process_group(PRIVATE)
    await bot.ledger.ingest("1", "5", "group", {**payload, "text": "group-canary"})
    dispatcher = asyncio.create_task(bot.dispatch_pending())
    try:
        event = await asyncio.wait_for(bot._event_queue.get(), 2)
        assert event.get_group_id() == "5"
        assert event.message_str == "group-canary"
        assert set(bot.worker_classes.values()) == {"message"}
        event.processing_completion.set_result(None)
        async with bot.ledger.db.execute(
            "SELECT payload,state,dispatch_class FROM inbox WHERE message='old'"
        ) as cursor:
            assert await cursor.fetchone() == (encoded, "pending", "control")
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)


@pytest.mark.asyncio
async def test_group_receive_dispatch_identity_and_native_reply(bot):
    inner = native._text_message(
        "fixture",
        "2",
        "5",
        "source",
        "@bot group-canary",
        [{"uid": 9001, "nick": "bot", "start": 0, "end": 5}],
        False,
        int(time.time() * 1000),
    )
    inner.sender.name = "fixture"
    outer = {
        0: "1",
        1: "8005",
        2: "9002",
        7: str(int(time.time() * 1000)),
        8: "100",
        10: wire.seal_message(KEY, inner, 123, 321, 1000),
        12: "55",
    }
    await bot.ingest(outer)
    bot.nim.acknowledge.assert_awaited_once_with(1, "55")
    worker = asyncio.create_task(bot.process_group("5", single=True))
    try:
        event = await asyncio.wait_for(bot._event_queue.get(), 2)
        assert event.eligible and event.message_str == "group-canary"
        assert event.actor_identity.require_verified() == "2"
        assert event.actor_identity.principal == "nim/9002"
        event.role = "admin"
        assert event.is_admin()
        assert not event.is_ai_admin(None)
        await event.send(MessageChain([Plain("group-reply-canary")]))
        assert event.get_extra("_wsl_native_reply_result") == "accepted"
        service, command, body = bot.nim.request.call_args.args
        assert (service, command) == (8, 2)
        fields, _ = wire.read_properties(body)
        assert fields[0] == b"1" and fields[1] == b"8005"
        sent = wire.open_message(KEY, fields[10].decode())
        assert sent.session == 2 and sent.target.id == 5
        assert sent.mentions.content.data == "@fixture group-reply-canary"
        event.processing_completion.set_result(None)
        await asyncio.wait_for(worker, 2)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra", ["_wsl_management_delivery_guard", "_wsl_invitation_delivery_guard"]
)
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_shared_group_reply_guard_survives_lock_wait_and_revocation(
    bot, extra, asynchronous
):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = "1"
    message.session_id = "1/5"
    message.group_id = "5"
    message.message_id = "query"
    message.sender = MessageMember("2", "fixture")
    message.message_str = "query"
    message.raw_message = {"_transport_peer": "9002"}
    event = WangshangliaoEvent(message, bot, True)
    checked = asyncio.Event()
    allowed = True

    def guard():
        checked.set()
        if not allowed:
            raise wire.ProtocolError("group_scope_revoked")

    async def async_guard():
        await asyncio.sleep(0)
        guard()

    event.set_extra(extra, async_guard if asynchronous else guard)
    await bot.send_lock.acquire()
    task = asyncio.create_task(event.send(MessageChain([Plain("group-query-result")])))
    try:
        await asyncio.wait_for(checked.wait(), 2)
        allowed = False
        bot.send_lock.release()
        with pytest.raises(wire.ProtocolError, match="group_scope_revoked"):
            await asyncio.wait_for(task, 2)
        bot.nim.request.assert_not_awaited()
        assert await bot.ledger.receipt("1/5/query/0") is None
    finally:
        if bot.send_lock.locked():
            bot.send_lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        event.processing_completion.cancel()


@pytest.mark.asyncio
async def test_group_dispatch_keeps_command_slots_and_has_no_control_workers(bot):
    rows = (
        [("5", f"message{i}", f"member{i}", "message") for i in range(20)]
        + [("5", f"command{i}", f"manager{i}", "command") for i in range(6)]
        + [(PRIVATE, "retired-control", "9002", "control")]
    )
    await bot.ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,sender,dispatch_class,payload,state) "
        "VALUES('1',?,?,?,?,'{}','pending')",
        rows,
    )
    await bot.ledger.db.commit()
    selected, queries = [], []
    await bot.ledger.db.set_trace_callback(
        lambda sql: queries.append(sql) if sql.lstrip().startswith("WITH ") else None
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def process(group, *, single, sender):
        selected.append((bot.worker_classes[(group, sender)], group, sender))
        if len(selected) == 16:
            started.set()
        await release.wait()

    bot.process_group = process
    dispatcher = asyncio.create_task(bot.dispatch_pending())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert [row[0] for row in selected] == ["command"] * 4 + ["message"] * 12
        assert {row[1] for row in selected} == {"5"}
        assert len(queries) == 1
        assert len(bot.workers) == 16
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "historical_team,expected", [(PRIVATE, "pending"), ("5", "queue_full")]
)
async def test_group_queue_limit_ignores_retired_private_backlog(
    bot, historical_team, expected
):
    await bot.ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,payload,state) "
        "VALUES('1',?,?,'{}','pending')",
        [(historical_team, str(index)) for index in range(4096)],
    )
    await bot.ledger.db.commit()
    observer = Mock()
    assert await bot.ledger.ingest(
        "1",
        "5",
        "new",
        {"sender": "2", "text": "group-canary", "created_at": 1000},
        observe=observer,
    )
    async with bot.ledger.db.execute(
        "SELECT state,sender,payload FROM inbox WHERE message='new'"
    ) as cursor:
        state, sender, encoded = await cursor.fetchone()
    assert state == expected and sender == "2"
    if expected == "pending":
        observer.assert_called_once()
        assert json.loads(encoded)["text"] == "group-canary"
        assert not bot.ledger.review_groups
    else:
        observer.assert_not_called()
        assert "text" not in json.loads(encoded)
        assert bot.ledger.review_groups == {"5"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send_text", "send_reply_text"])
async def test_private_send_has_no_outbox_intent_or_native_io(bot, method):
    with pytest.raises(wire.ProtocolError, match="private_messages_retired"):
        await getattr(bot, method)(PRIVATE, "retired", "canary")
    bot.nim.request.assert_not_awaited()
    assert await bot.ledger.receipt("retired") is None


@pytest.mark.asyncio
async def test_private_proactive_and_cached_event_have_no_send_route(bot):
    session = MessageSession(bot.config["id"], MessageType.FRIEND_MESSAGE, PRIVATE)
    with pytest.raises(wire.ProtocolError, match="private_messages_retired"):
        await bot.send_by_session(session, MessageChain([Plain("canary")]))
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "1"
    message.session_id = "1/" + PRIVATE
    message.group_id = ""
    message.message_id = "cached"
    message.sender = MessageMember("2", "fixture")
    message.message_str = "canary"
    message.raw_message = {"_transport_peer": "9002"}
    event = WangshangliaoEvent(message, bot, True)
    try:
        assert event.actor_identity.peer == ""
        assert event.actor_identity.verified_uid == ""
        assert not event.eligible
        assert not await event.prepare_waking_check({"admins_id": ["2"]}, None)
        event.set_extra("wsl_plain_command", True)
        event.set_extra("wsl_test_scope", "private_ai")
        await event.send(MessageChain([Plain("canary")]))
        assert event.is_stopped()
        bot.nim.request.assert_not_awaited()
    finally:
        event.processing_completion.cancel()


@pytest.mark.parametrize("private", [True, 1, None])
def test_shared_text_builder_can_only_build_group_messages(private):
    with pytest.raises(wire.ProtocolError, match="group_messages_only"):
        native._text_message("bot", "1", "2", "key", "canary", None, private, 1000)


def test_wire_encoder_cannot_create_private_application_messages():
    message = wire.ApplicationMessage(
        sender=wire.Source(id=1),
        target=wire.Source(id=2),
        device=1,
        session=1,
        format=0,
        content=wire.Content(data="canary"),
    )
    with pytest.raises(wire.ProtocolError, match="message_binding"):
        wire.seal_message(KEY, message, 123, 321, 1000)


@pytest.mark.parametrize(
    "route", ["/v1/friend/friend-apply-list", "/v1/friend/friend-apply-handler"]
)
@pytest.mark.asyncio
async def test_friend_business_routes_removed_before_credentials_or_io(route):
    with pytest.raises(BusinessError, match="unsupported_route"):
        await request_business(None, route, {})


def test_private_business_methods_are_deleted_instead_of_rejection_stubs():
    assert not hasattr(NimClient, "get_friends")
    assert not hasattr(NimClient, "get_friend_names")
    assert not hasattr(Window, "send_command")
    assert not hasattr(WangshangliaoEvent, "bind_management_reply_guard")


@pytest.mark.parametrize("scope", ["private_commands", "private_ai"])
def test_private_test_scopes_are_not_constructible(bot, scope):
    sender = SimpleNamespace(account="2")
    with pytest.raises(wire.ProtocolError, match="test_window_arguments"):
        Window(bot, sender, ["5"], [scope], [], 60, 2)


def test_group_test_window_retains_shared_budgets_but_no_private_admission(bot):
    sender = SimpleNamespace(account="2", nim_account="9002", stopping=asyncio.Event())
    window = Window(bot, sender, ["5"], ["group_ai"], [], 60, 2)
    bot.test_window = window
    payload = {"sender": "2", "text": "hello", "mentions": ["9001"]}
    assert window.admit(PRIVATE, "private", payload) == ""
    assert not window.reply(PRIVATE, "private", "operation")
    assert window.admit("5", "group", payload) == "group_ai"
    assert window.reply("5", "group", "operation")
    assert window.reply("5", "group", "operation")
    assert not window.reply("5", "group", "other-operation")
    assert window.remaining == 1
    assert window.outbound_remaining == 2 and window.outbound_keys == set()


def test_old_private_reply_setting_is_not_core_runtime_state():
    policy = PolicyRuntime()
    policy.service = SimpleNamespace(active=True)
    snapshot = PolicySnapshot(
        "1", "revision", json.dumps({"reply_private": True, "enabled_groups": ["5"]}), 1
    )
    policy.publish("bot", snapshot)
    for public in (False, True):
        view = policy.view(
            {"id": "bot", "account_id": "1", "reply_private": True}, public=public
        )
        assert "reply_private" not in view and view["enabled_groups"] == ["5"]
    assert "reply_private" not in BUSINESS_FIELDS
