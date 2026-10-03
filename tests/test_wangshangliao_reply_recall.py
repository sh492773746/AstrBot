"""Native self-recall timing, durable routing, scoping and once-only attempts."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.core.platform.sources.wangshangliao import reply_recall, wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(reply_recall.time, "time", lambda: now[0])
    monkeypatch.setattr(wire, "seal_message", lambda *args: "sealed")
    adapter = WangshangliaoAdapter(
        {"id": "fixture", "account_id": "1", "enable": True, "enabled_groups": ["5"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.groups = {"5": "905"}
    adapter.members = {"5": {"1": "901", "2": "902"}}
    adapter.nim_account = "901"
    adapter.connection_state = "online"
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: b"k" * 32)
    )
    adapter.diagnostics = SimpleNamespace(emit=Mock())
    count = [122]

    async def request(service, command, body):
        if (service, command) == (7, 13):
            return 200, b""
        fields, _ = wire.read_properties(body)
        count[0] += 1
        return 200, wire.properties(
            [
                (7, str(int(now[0] * 1000)).encode()),
                (11, fields[11]),
                (12, str(count[0]).encode()),
            ]
        )

    adapter.nim = SimpleNamespace(request=AsyncMock(side_effect=request))
    yield adapter, now
    await adapter.ledger.close()


async def jobs(adapter):
    async with adapter.ledger.db.execute(
        "SELECT key,server_id,deadline,state FROM reply_recalls ORDER BY key"
    ) as cursor:
        return await cursor.fetchall()


@pytest.mark.asyncio
async def test_exact_twenty_seconds_and_own_native_route(setup):
    adapter, now = setup
    assert (
        await adapter.send_text("5", "reply", "Ranking", auto_recall=True) == "accepted"
    )
    assert await jobs(adapter) == [("reply", "123", 1020.0, "pending")]
    now[0] = 1019.99
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 1
    now[0] = 1020.0
    await asyncio.gather(
        reply_recall.recall_due(adapter), reply_recall.recall_due(adapter)
    )
    assert adapter.nim.request.await_count == 2
    call = adapter.nim.request.await_args.args
    assert call[:2] == (7, 13)
    fields, _ = wire.read_properties(call[2])
    assert fields == {
        0: b"1000000",
        1: b"8",
        2: b"905",
        3: b"901",
        10: str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture/reply")).encode(),
        11: b"123",
        16: b"901",
    }
    assert (await jobs(adapter))[0][3] == "accepted"
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 2


@pytest.mark.asyncio
async def test_segments_each_have_one_recall_and_repeat_does_not_reschedule(setup):
    adapter, now = setup
    text = "a" * 5000
    await adapter.send_reply_text("5", "reply", text, auto_recall=True)
    assert len(await jobs(adapter)) == 2
    await adapter.send_reply_text("5", "reply", text, auto_recall=True)
    assert len(await jobs(adapter)) == 2 and adapter.nim.request.await_count == 2
    now[0] = 1020
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 4
    assert all(row[3] == "accepted" for row in await jobs(adapter))


@pytest.mark.asyncio
async def test_private_and_unflagged_chat_do_not_schedule(setup):
    adapter, _ = setup
    await adapter.send_text("5", "chat", "Chat")
    session = "private/2/" + wire.b64(b"902")
    await adapter.ledger.ingest("1", session, "m", {"sender": "2"})
    await adapter.send_reply_text(session, "private", "Private", auto_recall=True)
    assert await jobs(adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [403, TimeoutError()])
async def test_failed_or_unknown_send_never_schedules(setup, outcome):
    adapter, _ = setup
    if isinstance(outcome, Exception):
        adapter.nim.request.side_effect = outcome
        with pytest.raises(TimeoutError):
            await adapter.send_text("5", "reply", "Ranking", auto_recall=True)
    else:
        adapter.nim.request.side_effect = None
        adapter.nim.request.return_value = outcome, b""
        assert (
            await adapter.send_text("5", "reply", "Ranking", auto_recall=True)
            == "rejected"
        )
    assert await jobs(adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected",
    [
        (403, "rejected"),
        (TimeoutError(), "unknown"),
        (asyncio.CancelledError(), "unknown"),
    ],
)
async def test_failed_or_unknown_recall_is_never_retried(setup, outcome, expected):
    adapter, now = setup
    await adapter.send_text("5", "reply", "Ranking", auto_recall=True)
    now[0] = 1020
    if isinstance(outcome, BaseException):
        adapter.nim.request.side_effect = outcome
    else:
        adapter.nim.request.side_effect = None
        adapter.nim.request.return_value = outcome, b""
    if isinstance(outcome, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await reply_recall.recall_due(adapter)
    else:
        await reply_recall.recall_due(adapter)
    assert (await jobs(adapter))[0][3] == expected
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "account",
        "group",
        "peer",
        "disabled",
        "removed",
        "client",
        "receipt",
        "expired",
    ],
)
async def test_changed_or_invalid_scope_cannot_recall(setup, change):
    adapter, now = setup
    await adapter.send_text("5", "reply", "Ranking", auto_recall=True)
    now[0] = 1020
    if change == "account":
        adapter.account = "2"
    elif change == "group":
        adapter.groups["5"] = "906"
    elif change == "peer":
        adapter.nim_account = "902"
    elif change == "disabled":
        adapter.config["enable"] = False
    elif change == "removed":
        adapter.config["enabled_groups"] = []
    elif change == "client":
        await adapter.ledger.db.execute("UPDATE reply_recalls SET client='other'")
        await adapter.ledger.db.commit()
    elif change == "receipt":
        await adapter.ledger.finish("reply", "accepted", "999")
    elif change == "expired":
        now[0] = 1321
    await reply_recall.recall_due(adapter)
    assert (await jobs(adapter))[0][3] == "rejected"
    assert adapter.nim.request.await_count == 1


@pytest.mark.asyncio
async def test_restart_restores_pending_but_quarantines_uncertain_attempt(setup):
    adapter, now = setup
    await adapter.send_text("5", "pending", "Ranking", auto_recall=True)
    await adapter.send_text("5", "uncertain", "Result", auto_recall=True)
    await adapter.ledger.db.execute(
        "UPDATE reply_recalls SET state='sending' WHERE key='uncertain'"
    )
    await adapter.ledger.db.commit()
    await adapter.ledger.close()
    await adapter.ledger.open()
    now[0] = 1020
    adapter.connection_state = "reconnecting"
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 2
    adapter.connection_state = "online"
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 3
    assert {row[0]: row[3] for row in await jobs(adapter)} == {
        "pending": "accepted",
        "uncertain": "unknown",
    }


@pytest.mark.asyncio
async def test_worker_stops_without_leaving_a_timer(setup):
    adapter, _ = setup
    task = asyncio.create_task(reply_recall.run(adapter))
    await asyncio.sleep(0)
    adapter.stopping.set()
    await asyncio.wait_for(task, 1)
