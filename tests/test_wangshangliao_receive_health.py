"""Receive diagnostics distinguish undecoded packets from failed chat messages."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.nim import NimClient
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ('{"unexpected":"private-secret"}', "wrapper"),
        ("invalid-base64-with-private-secret!", "encoding"),
        (wire.b64(wire.Envelope(aad=1).SerializeToString()), "envelope"),
        (
            wire.b64(
                wire.Envelope(
                    aad=1, nonce=2, body=b"invalid", tag=bytes(16)
                ).SerializeToString()
            ),
            "authentication",
        ),
    ],
)
def test_decode_diagnostics_never_preserve_unverified_message_content(value, reason):
    with pytest.raises(wire.ProtocolError, match="^message_decode$") as caught:
        wire.open_message(bytes(32), value)
    assert caught.value.reason == reason
    assert caught.value.__context__ is not None
    assert caught.value.__suppress_context__ is True
    assert "private-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_message", [False, True])
async def test_unhandled_packets_do_not_become_message_decode_failures(
    tmp_path, invalid_message
):
    adapter = WangshangliaoAdapter(
        {"id": "receive-health", "account_id": "1", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    adapter.nim = NimClient(None)
    adapter.nim.acknowledge = AsyncMock()
    adapter.diagnostics.emit = Mock()
    packet = (99, 99, 0, 200, b"")
    if invalid_message:
        packet = (
            7,
            2,
            0,
            200,
            wire.properties(
                [
                    (0, b"1"),
                    (1, b"9005"),
                    (2, b"1002"),
                    (7, b"1000"),
                    (8, b"100"),
                    (10, b"invalid!"),
                    (12, b"77"),
                ]
            ),
        )
    await adapter.nim.pushes.put(packet)
    task = asyncio.create_task(adapter.consume_nim_pushes(adapter.nim))
    try:
        field = "decode_failures" if invalid_message else "unhandled_packets"
        for _ in range(200):
            if adapter.receive_health[field] == 1:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Expected receive health counter was not published")
        assert adapter.unsupported_pushes == int(invalid_message)
        assert adapter.receive_health["decode_failures"] == int(invalid_message)
        assert adapter.receive_health["unhandled_packets"] == int(not invalid_message)
        assert adapter.decode_reasons == ({"encoding": 1} if invalid_message else {})
        if invalid_message:
            adapter.nim.acknowledge.assert_awaited_once_with(1, "77")
            assert adapter.diagnostics.emit.call_args.kwargs["action"] == "encoding"
            async with adapter.ledger.db.execute(
                "SELECT payload,state FROM inbox"
            ) as cursor:
                assert await cursor.fetchall() == [("{}", "unsupported")]
        else:
            adapter.nim.acknowledge.assert_not_awaited()
            adapter.diagnostics.emit.assert_not_called()
        assert adapter._event_queue.empty()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.ledger.close()
