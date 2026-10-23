"""Native quoted-text protocol, inbox scoping and recall races."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Reply
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger

pytestmark = pytest.mark.usefixtures("wsl_active_policy")


@pytest_asyncio.fixture
async def native(tmp_path):
    adapter = WangshangliaoAdapter(
        {
            "id": "quote-test",
            "account_id": "99",
            "enabled_groups": ["5"],
            "proactive_send": {"enabled": True, "targets": ["5"]},
        },
        {},
        asyncio.Queue(),
    )
    adapter.groups = {"5": "1005", "6": "1006"}
    adapter.members = {"5": {"99": "1099", "2": "1002"}, "6": {"2": "1002"}}
    adapter.nim_account = "1099"
    adapter.connection_state = "online"
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32)),
    )
    adapter.nim = SimpleNamespace(
        request=AsyncMock(
            return_value=(200, wire.properties([(7, b"1000000"), (12, b"456")])),
        )
    )
    adapter.ledger = Ledger(tmp_path / "quotes.sqlite")
    await adapter.ledger.open()
    await adapter.ledger.ingest(
        "99",
        "5",
        "123",
        {
            "sender": "2",
            "text": "今天下班后吃什么？" * 100,
            "recall_route": {
                "peer": "1002",
                "client": "source-client",
                "time": "1000000",
            },
        },
    )
    try:
        yield adapter
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_encrypted_reply_and_nim_reference_use_real_source(native):
    result = await native.send_text("5", "reply/one", "来碗热面？", reply_to="123")
    assert result == "accepted"
    channel, command, raw = native.nim.request.call_args.args
    assert (channel, command) == (8, 2)
    fields, end = wire.read_properties(raw)
    assert end == len(raw)
    message = wire.open_message(bytes(32), fields[10].decode())
    assert message.appoint == 1
    assert message.reply.client_id == "source-client"
    assert message.reply.server_id == "123"
    assert message.content.data == "来碗热面？"
    assert len(message.reply.content.data.encode()) <= 1024
    for base in (29, 34):
        assert [fields[base + index] for index in range(5)] == [
            b"1002",
            b"1005",
            b"1000000",
            b"123",
            b"source-client",
        ]
    await native.send_text("5", "reply/one", "来碗热面？", reply_to="123")
    assert native.nim.request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group,message",
    [
        ("6", "123"),
        ("5", "124"),
        ("private/2/MTAwMg", "123"),
        ("5", "0"),
        ("5", "-1"),
        ("5", "1" * 100),
        ("5", "１２３"),
    ],
)
async def test_quote_cannot_cross_groups_or_invent_native_ids(native, group, message):
    with pytest.raises(wire.ProtocolError, match="quote_"):
        await native.send_text(group, "bad-quote", "内容", reply_to=message)
    native.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["local_recall", "observed_recall", "peer", "incomplete"]
)
async def test_quote_rechecks_source_after_waiting_for_send_lock(native, change):
    await native.send_lock.acquire()
    task = asyncio.create_task(native.send_text("5", "racy", "内容", reply_to="123"))
    try:
        for _ in range(100):
            if native.send_lock._waiters:
                break
            await asyncio.sleep(0.001)
        assert native.send_lock._waiters
        if change == "local_recall":
            await native.ledger.reserve("recall/99/5/123", "fixture")
            await native.ledger.finish("recall/99/5/123", "accepted")
        elif change == "observed_recall":
            await native.ledger.observe_recall(
                "99",
                "5",
                {
                    "notice_id": "n1",
                    "team": "1005",
                    "message_id": "123",
                    "client": "source-client",
                    "author": "1002",
                    "operator": "1099",
                    "message_time": "1000000",
                    "recalled_at": "1000001",
                },
            )
        elif change == "peer":
            native.members["5"]["2"] = "9999"
        else:
            async with native.ledger.db.execute(
                "SELECT payload FROM inbox WHERE account='99' AND team='5' AND message='123'"
            ) as cursor:
                data = json.loads((await cursor.fetchone())[0])
            data["_audit_incomplete"] = True
            await native.ledger.db.execute(
                "UPDATE inbox SET payload=? WHERE account='99' AND team='5' AND message='123'",
                (json.dumps(data),),
            )
            await native.ledger.db.commit()
    finally:
        native.send_lock.release()
    with pytest.raises(wire.ProtocolError, match="quote_"):
        await task
    native.nim.request.assert_not_awaited()
    assert await native.ledger.receipt("racy") is None


@pytest.mark.asyncio
async def test_long_reply_counts_quote_bytes_and_quotes_only_first_part(native):
    assert (
        await native.send_reply_text(
            "5",
            "long",
            "中文😀" * 1500,
            reply_to="123",
        )
        == "accepted"
    )
    messages = []
    for call in native.nim.request.call_args_list:
        fields, _ = wire.read_properties(call.args[2])
        inner = wire.open_message(bytes(32), fields[10].decode())
        assert inner.ByteSize() <= 4096
        messages.append(inner)
    assert len(messages) > 1
    assert messages[0].HasField("reply")
    assert all(not m.HasField("reply") for m in messages[1:])
    assert "".join(m.content.data for m in messages) == "中文😀" * 1500


@pytest.mark.asyncio
async def test_framework_reply_component_is_preserved_by_event_and_proactive_route(
    native,
):
    from tests.test_wangshangliao_plain_commands import native_event

    event = native_event("你好")
    event.is_at_or_wake_command = True
    event.eligible = True
    await event.send(MessageChain([Reply(id="123"), Plain("本群回复")]))
    assert event.platform.send_reply_text.call_args.kwargs["reply_to"] == "123"
    native.send_reply_text = AsyncMock(return_value="accepted")
    session = MessageSession("quote-test", MessageType.GROUP_MESSAGE, "99/5")
    await native.send_by_session(
        session, MessageChain([Reply(id="123"), Plain("主动回复")])
    )
    assert native.send_reply_text.call_args.kwargs["reply_to"] == "123"
    with pytest.raises(wire.ProtocolError, match="quote_identity"):
        await native.send_by_session(
            session,
            MessageChain([Reply(id="123"), Reply(id="124"), Plain("拒绝歧义")]),
        )
    assert native.send_reply_text.await_count == 1
