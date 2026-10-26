"""Legacy plaintext is classified without granting it application authority."""

import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nacl.bindings
import pytest

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


def plaintext_message(format_id):
    """Build an unsigned fixture that must never become actionable evidence.

    Args:
        format_id: Claimed official application format.

    Returns:
        Encoded untrusted envelope containing deliberately sensitive text.
    """
    message = wire.ApplicationMessage(
        sender=wire.Source(id=1002, name="not a trusted identity"),
        target=wire.Source(id=1234),
        session=2,
        device=1,
        format=format_id,
        content=wire.Content(data="private fixture text"),
    )
    envelope = wire.Envelope(
        header=1002 << 34 | 1000 << 4 | 2 << 2 | 1,
        timestamp=1000,
        nonce=123,
        body=message.SerializeToString(),
    )
    return wire.b64(envelope.SerializeToString())


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("format_id", [0, 1, 3, 7, 8, 9, 999, 65536, -1])
def test_plaintext_inspection_never_returns_an_authenticated_message(
    wrapped, format_id
):
    text = plaintext_message(format_id)
    if wrapped:
        text = json.dumps({"b": text})
    with pytest.raises(wire.ProtocolError, match="^message_decode$") as caught:
        wire.open_message(bytes(32), text)
    metadata = caught.value.unverified
    assert metadata["format"] == (format_id if 0 <= format_id <= 65535 else -1)
    assert metadata["reason"] == "unsigned_plaintext"
    assert set(metadata) == {"reason", "format"}
    assert "private fixture text" not in repr(metadata)
    assert "not a trusted identity" not in repr(metadata)


@pytest.mark.parametrize("tag", [b"x", bytes(15), bytes(16)])
def test_broken_authentication_never_gets_a_plaintext_classification(tag):
    envelope = wire.Envelope(
        header=1,
        timestamp=1000,
        nonce=123,
        body=wire.ApplicationMessage(format=7).SerializeToString(),
        tag=tag,
    )
    with pytest.raises(wire.ProtocolError) as caught:
        wire.open_message(bytes(32), wire.b64(envelope.SerializeToString()))
    assert caught.value.unverified == {}


def test_compressed_or_malformed_unsigned_body_is_not_guessed():
    for envelope in (
        wire.Envelope(header=1, body=b"\xff"),
        wire.Envelope(
            header=1,
            aad=1,
            body=wire.ApplicationMessage(format=7).SerializeToString(),
        ),
    ):
        with pytest.raises(wire.ProtocolError) as caught:
            wire.open_message(bytes(32), wire.b64(envelope.SerializeToString()))
        assert caught.value.unverified == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("format_id", [0, 1, 3, 7, 8, 9, 999])
@pytest.mark.parametrize("scope", ["group", "unknown_group", "private"])
async def test_unsigned_ingress_archives_only_and_uses_transport_scope(
    tmp_path, monkeypatch, format_id, scope
):
    adapter = WangshangliaoAdapter(
        {"id": "plaintext-fixture", "account_id": "1000", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.groups = {"1000": "9000"}
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    adapter.diagnostics.emit = Mock()
    observe = Mock(side_effect=AssertionError("Unverified content reached business"))
    monkeypatch.setattr(runtime, "observe", observe)
    route = "0" if scope == "private" else "1"
    target = "9000" if scope != "unknown_group" else "9001"
    outer = {
        0: route,
        1: target,
        2: "9002",
        7: "1000",
        8: "100",
        10: plaintext_message(format_id),
        12: "77",
    }
    try:
        for _ in range(2):
            await adapter.ingest(outer)
        assert adapter.nim.acknowledge.await_count == 2
        adapter.nim.acknowledge.assert_awaited_with(int(route), "77")
        async with adapter.ledger.db.execute(
            "SELECT team,payload,state,sender FROM inbox"
        ) as cursor:
            rows = await cursor.fetchall()
        notice = scope == "group" and format_id in {7, 8}
        assert rows == [
            (
                "1000" if scope == "group" else f"unsupported/{route}/{target}",
                json.dumps({"reason": "unsigned_plaintext", "format": format_id}),
                "plaintext_notice" if notice else "unverified_message",
                "",
            )
        ]
        assert adapter.receive_health["decode_failures"] == 0
        assert adapter.receive_health["decoded_messages"] == 0
        assert adapter.receive_health["plaintext_notices"] == (2 if notice else 0)
        assert adapter.receive_health["unverified_messages"] == (0 if notice else 2)
        assert not adapter.dispatch_wakeup.is_set()
        assert adapter._event_queue.empty()
        assert adapter.ledger.review_groups == set()
        observe.assert_not_called()
        label = str(format_id) if format_id <= 40 else "other"
        assert adapter.get_stats()["receive_health"]["unverified_formats"] == {label: 2}
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_unverified_archive_failure_never_acknowledges(tmp_path):
    adapter = WangshangliaoAdapter(
        {"id": "plaintext-failure", "account_id": "1000", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    adapter.ledger = SimpleNamespace(ingest=AsyncMock(side_effect=OSError("fixture")))
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    with pytest.raises(OSError, match="fixture"):
        await adapter.ingest(
            {
                0: "1",
                1: "9000",
                2: "9002",
                8: "100",
                10: plaintext_message(7),
                12: "77",
            }
        )
    adapter.nim.acknowledge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("format_id", [2, 6, 7, 8, 65536, -1])
async def test_authenticated_nonchat_format_keeps_type_only_evidence(
    tmp_path, monkeypatch, format_id
):
    adapter = WangshangliaoAdapter(
        {"id": "nonchat-fixture", "account_id": "1000", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.groups = {"1000": "9000"}
    adapter.members = {"1000": {"1002": "9002"}}
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    message = wire.ApplicationMessage(
        sender=wire.Source(id=1002),
        target=wire.Source(id=1000),
        session=2,
        device=1,
        format=format_id,
        content=wire.Content(data="not actionable notification text"),
    )
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        message.SerializeToString(), bytes(8), struct.pack("<Q", 123), bytes(32)
    )
    envelope = wire.Envelope(
        header=1002 << 34 | 1000 << 4 | 2 << 2 | 1,
        nonce=123,
        body=sealed[:-16],
        tag=sealed[-16:],
    )
    monkeypatch.setattr(runtime, "open_service", None)
    observe = Mock(side_effect=AssertionError("Notification reached business"))
    monkeypatch.setattr(runtime, "observe", observe)
    try:
        await adapter.ingest(
            {
                0: "1",
                1: "9000",
                2: "9002",
                8: "100",
                10: wire.b64(envelope.SerializeToString()),
                12: "77",
            }
        )
        async with adapter.ledger.db.execute(
            "SELECT payload,state FROM inbox"
        ) as cursor:
            payload, state = await cursor.fetchone()
        notice = format_id in {7, 8}
        assert state == ("application_notice" if notice else "unsupported")
        assert json.loads(payload) == {
            "format": format_id if 0 <= format_id <= 65535 else -1,
            "reason": "application_notice" if notice else "unsupported_format",
        }
        assert adapter.receive_health["unsupported_formats"] == int(not notice)
        assert adapter.receive_health["decoded_messages"] == 1
        assert not adapter.dispatch_wakeup.is_set()
        observe.assert_not_called()
        adapter.nim.acknowledge.assert_awaited_once_with(1, "77")
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_acknowledgement_failure_keeps_archive_and_retry_is_idempotent(tmp_path):
    adapter = WangshangliaoAdapter(
        {"id": "ack-fixture", "account_id": "1000", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.groups = {"1000": "9000"}
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.nim = SimpleNamespace(
        acknowledge=AsyncMock(
            side_effect=[wire.ProtocolError("nim_disconnected"), None]
        )
    )
    outer = {
        0: "1",
        1: "9000",
        2: "9002",
        8: "100",
        10: plaintext_message(7),
        12: "77",
    }
    try:
        with pytest.raises(wire.ProtocolError, match="nim_disconnected"):
            await adapter.ingest(outer)
        await adapter.ingest(outer)
        async with adapter.ledger.db.execute(
            "SELECT COUNT(*) FROM inbox WHERE state='plaintext_notice'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
        assert not adapter.dispatch_wakeup.is_set()
    finally:
        await adapter.ledger.close()
