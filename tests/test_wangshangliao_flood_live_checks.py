"""Safety and immutable routing for the explicit live flood acceptance harness."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from scripts.check_wsl_flood_live import ORDINARY_ACCOUNT, LiveFlood


@pytest_asyncio.fixture
async def sender(tmp_path):
    test = LiveFlood.__new__(LiveFlood)
    test.marker = "WSL_FLOOD_LIVE_fixture"
    test.team = "55"
    test.peer = "22"
    test.sent_receipts = {}
    test.sender = SimpleNamespace(fields=[0, 0, 0, int(ORDINARY_ACCOUNT)])
    test.deployment = SimpleNamespace(key=lambda name: b"x" * 32)
    test.nim = SimpleNamespace(
        request=AsyncMock(
            return_value=(
                200,
                wire.properties([(7, b"1791480000000"), (12, b"123456789")]),
            )
        )
    )
    test.ledger = Ledger(tmp_path / "sender.sqlite3")
    await test.ledger.open()
    try:
        yield test
    finally:
        await test.ledger.close()


@pytest.mark.asyncio
async def test_live_sender_uses_the_production_transport_field_contract(sender):
    result = await sender.send(sender.marker + "-probe", "probe")
    assert result["status"] == "accepted" and result["message"] == "123456789"
    assert sender.sent_receipts[result["message"]] == result
    service, command, encoded = sender.nim.request.call_args.args
    assert (service, command) == (8, 2)
    fields, end = wire.read_properties(encoded)
    assert end == len(encoded)
    assert fields[0] == b"1" and fields[1] == b"55" and fields[8] == b"100"
    assert fields[11].decode() == result["client"]
    assert await sender.ledger.receipt(sender.marker + "/probe") == {
        "status": "accepted",
        "server_id": "123456789",
    }
    with pytest.raises(AssertionError, match="cannot be retried"):
        await sender.send(sender.marker + "-probe", "probe")
    assert sender.nim.request.await_count == 1


@pytest.mark.asyncio
async def test_rejected_muted_probe_is_not_recorded_as_accepted(sender):
    sender.nim.request.return_value = (802, b"")
    result = await sender.send(sender.marker + "-probe", "rejected")
    assert result["status"] == "rejected" and result["code"] == 802
    assert "message" not in result
    assert (await sender.ledger.receipt(sender.marker + "/rejected"))[
        "status"
    ] == "rejected"


@pytest.mark.asyncio
async def test_unknown_live_send_is_quarantined_without_retry(sender):
    sender.nim.request.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await sender.send(sender.marker + "-probe", "timeout")
    assert (await sender.ledger.receipt(sender.marker + "/timeout"))[
        "status"
    ] == "unknown"
    with pytest.raises(AssertionError, match="cannot be retried"):
        await sender.send(sender.marker + "-probe", "timeout")
    assert sender.nim.request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "marker"])
async def test_sender_rejects_identity_changes_or_unmarked_messages(sender, change):
    text = sender.marker + "-probe"
    if change == "identity":
        sender.sender.fields[3] = 999
    else:
        text = "unrelated production message"
    with pytest.raises(AssertionError):
        await sender.send(text, "invalid")
    sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_enrollment_escape_allows_only_the_fixed_public_command(sender):
    with pytest.raises(AssertionError):
        await sender.send("开启抽奖", "invalid-command", enrollment=True)
    sender.nim.request.assert_not_awaited()
    result = await sender.send("参加抽奖", "enrollment", enrollment=True)
    assert result["status"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", [None, "team", "sender", "client", "time", "route", "truncated"]
)
async def test_witness_requires_exact_native_recall_scope_and_receipt(sender, changed):
    receipt = await sender.send(sender.marker + "-probe", "probe")
    fields = {
        0: b"1791484891870",
        1: b"8",
        2: sender.team.encode(),
        3: sender.peer.encode(),
        10: receipt["client"].encode(),
        11: receipt["message"].encode(),
        14: receipt["time"].encode(),
    }
    key = {"team": 2, "sender": 3, "client": 10, "time": 14}.get(changed)
    if key is not None:
        fields[key] = b"999"
    nested = wire.packet(
        7, 13 if changed == "route" else 14, 0, wire.properties(list(fields.items()))
    )
    body = b"\x00" * 8 + nested
    if changed == "truncated":
        body = body[:-1]
    sender.nim.pushes = asyncio.Queue()
    sender.nim.acknowledge = AsyncMock()
    sender.events = []
    sender.dashboard = SimpleNamespace(save=lambda *args: None)
    await sender.nim.pushes.put((4, 1, 0, 200, body))
    task = asyncio.create_task(sender.witness())
    try:
        for _ in range(20):
            if sender.events or task.done():
                break
            await asyncio.sleep(0)
        assert len(sender.events) == 1
        if changed is None:
            assert sender.events[0]["recall"]["message"] == receipt["message"]
            assert sender.events[0]["recall"]["original_time"] == receipt["time"]
        else:
            assert "recall" not in sender.events[0]
        assert "body_hex" not in sender.events[0]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
