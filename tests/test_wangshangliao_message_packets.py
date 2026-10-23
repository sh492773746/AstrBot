"""Reviewed official 3.7.3 packet framing preserves both AEAD checks."""

import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nacl.bindings
import pytest

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger

KEY = bytes(32)


def application():
    return wire.ApplicationMessage(
        sender=wire.Source(id=1002, name="fixture"),
        target=wire.Source(id=1000),
        device=1,
        session=1,
        version=2,
        format=0,
        content=wire.Content(data="menu"),
    )


def inner_message(*, compressed=True, key=KEY):
    message = application()
    if compressed:
        return wire.seal_message(key, message, 123, 321, 1000)
    plain = message.SerializeToString()
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        plain, bytes(8), struct.pack("<Q", 123), key
    )
    envelope = wire.Envelope(
        header=1002 << 34 | 1000 << 4 | 1 << 2 | 1,
        timestamp=1000,
        nonce=123,
        body=sealed[:-16],
        tag=sealed[-16:],
    )
    return json.dumps({"b": wire.b64(envelope.SerializeToString())})


def packet_message(inner=None, *, key=KEY, version=1, metadata=b"\x08\x01"):
    """Construct official field numbers independently of the packet descriptor."""
    inner = inner_message() if inner is None else inner
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        metadata, struct.pack("<Q", version), bytes(8), key
    )
    signature = wire.b64(bytes(8) + sealed[-16:] + sealed[:-16])
    raw = b""
    for field, value in ((1, inner), (2, signature)):
        encoded = value.encode()
        raw += bytes([field << 3 | 2]) + wire.varint(len(encoded)) + encoded
    raw += b"\x18" + wire.varint(version)
    return json.dumps({"b": wire.b64(raw)})


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
def test_legacy_and_current_packets_authenticate_the_same_application(
    compressed, wrapped
):
    text = inner_message(compressed=compressed)
    if wrapped:
        text = packet_message(text)
    assert wire.open_message(KEY, text) == application()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (lambda: packet_message(key=bytes([1]) * 32), "packet_signature"),
        (lambda: packet_message(inner_message(key=bytes([1]) * 32)), "authentication"),
        (lambda: packet_message(version=2), "packet_version"),
        (lambda: packet_message(packet_message()), "packet_message"),
        (lambda: packet_message(metadata=b"\xff"), "packet_signature"),
    ],
)
def test_invalid_packet_layers_never_fall_back_to_unverified_content(text, reason):
    with pytest.raises(wire.ProtocolError, match="^message_decode$") as caught:
        wire.open_message(KEY, text())
    assert caught.value.reason == reason


@pytest.mark.parametrize("field", ["msg", "sign"])
def test_missing_packet_fields_are_not_accepted(field):
    packet = wire.MessagePacket.FromString(
        wire.unb64(json.loads(packet_message())["b"])
    )
    packet.ClearField(field)
    with pytest.raises(wire.ProtocolError, match="^message_decode$"):
        wire.open_message(KEY, wire.b64(packet.SerializeToString()))


def test_zero_aad_still_requires_valid_authentication():
    raw = wire.unb64(json.loads(inner_message(compressed=False))["b"])
    envelope = wire.Envelope.FromString(raw)
    envelope.tag = bytes(16)
    with pytest.raises(wire.ProtocolError, match="^message_decode$") as caught:
        wire.open_message(KEY, wire.b64(envelope.SerializeToString()))
    assert caught.value.reason == "authentication"


@pytest.mark.parametrize("header_varint", [False, True])
@pytest.mark.parametrize("nonce_varint", [False, True])
@pytest.mark.parametrize("aad_fixed", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
def test_official_scalar_wire_variants_preserve_authenticated_routing(
    header_varint, nonce_varint, aad_fixed, wrapped
):
    raw = wire.unb64(json.loads(inner_message())["b"])
    envelope = wire.Envelope.FromString(raw)
    suffix = b""
    for number, name, kind in (
        (1, "header", 0 if header_varint else 1),
        (3, "nonce", 0 if nonce_varint else 1),
        (6, "aad", 1 if aad_fixed else 0),
    ):
        value = getattr(envelope, name)
        envelope.ClearField(name)
        suffix += wire.varint(number << 3 | kind)
        suffix += wire.varint(value) if kind == 0 else struct.pack("<Q", value)
    text = wire.b64(envelope.SerializeToString() + suffix)
    assert (
        wire.open_message(KEY, packet_message(text) if wrapped else text)
        == application()
    )


@pytest.mark.parametrize("number", [1, 3, 6])
def test_conflicting_scalar_encodings_never_reach_dispatch(number):
    raw = wire.unb64(json.loads(inner_message())["b"])
    kind = 1 if number == 6 else 0
    encoded = struct.pack("<Q", 42) if kind == 1 else wire.varint(42)
    with pytest.raises(wire.ProtocolError, match="^message_decode$"):
        wire.open_message(
            KEY, wire.b64(raw + wire.varint(number << 3 | kind) + encoded)
        )


def test_unsigned_provider_content_is_not_treated_as_authenticated_plaintext():
    message = application()
    unsigned = wire.Envelope(
        header=1002 << 34 | 1000 << 4 | 1 << 2 | 1,
        body=message.SerializeToString(),
    )
    with pytest.raises(wire.ProtocolError, match="^message_decode$") as caught:
        wire.open_message(KEY, wire.b64(unsigned.SerializeToString()))
    assert caught.value.reason == "envelope"
    assert caught.value.shape == "f1w1_f4w2"
    assert "menu" not in str(caught.value)


def test_authenticated_packet_metadata_cannot_replace_message_identity():
    metadata = wire.MessageSign(
        id=88, nid=999, public_key="untrusted", msg_sign="not-an-authorization"
    ).SerializeToString()
    result = wire.open_message(KEY, packet_message(metadata=metadata))
    assert result.sender.id == 1002
    assert result.target.id == 1000


@pytest.mark.asyncio
async def test_current_packet_reaches_durable_ingress_without_model_or_management(
    tmp_path,
):
    adapter = WangshangliaoAdapter(
        {"id": "current-packet", "account_id": "1000", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.nim_account = "9000"
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(deployment=SimpleNamespace(key=lambda _: KEY))
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    adapter.diagnostics.emit = Mock()
    try:
        await adapter.ingest(
            {
                0: "0",
                1: "9000",
                2: "9002",
                7: "1000",
                8: "100",
                10: packet_message(),
                12: "77",
            }
        )
        adapter.nim.acknowledge.assert_awaited_once_with(0, "77")
        assert adapter.receive_health["decode_failures"] == 0
        assert adapter.receive_health["decoded_messages"] == 1
        async with adapter.ledger.db.execute(
            "SELECT payload,state FROM inbox WHERE message='77'"
        ) as cursor:
            payload, state = await cursor.fetchone()
        assert json.loads(payload)["sender"] == "1002"
        assert json.loads(payload)["transport_peer"] == "9002"
        assert state == "business_suspended"
        assert adapter._event_queue.empty()
    finally:
        await adapter.ledger.close()
