"""Offline group activation replies and shared native image decoding."""

import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import lz4.block
import nacl.bindings
import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import (
    feature_image,
    onboarding_delivery,
)
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


def decode_image(attachment, key):
    """Decode a captured native image for offline group-media assertions.

    Args:
        attachment: Native JSON attachment captured from the mocked transport.
        key: Synthetic message key used by the sending fixture.

    Returns:
        Decoded group-image protobuf, including its image extension.
    """
    envelope = wire.Envelope.FromString(wire.unb64(json.loads(attachment)["b"]))
    compressed = nacl.bindings.crypto_aead_chacha20poly1305_decrypt(
        envelope.body + envelope.tag,
        struct.pack("<Q", envelope.aad),
        struct.pack("<Q", envelope.nonce),
        key,
    )
    return feature_image.ImageMessage.FromString(lz4.block.decompress(compressed))


@pytest_asyncio.fixture
async def image_event(tmp_path, monkeypatch):
    """Capture group-native sends using an isolated ledger and fake connection.

    Args:
        tmp_path: Temporary directory owning all native receipts.
        monkeypatch: Test-scoped pacing override.

    Yields:
        Synthetic event, temporary ledger and captured native requests.
    """
    ledger = Ledger(tmp_path / "native.sqlite3")
    await ledger.open()
    calls = []

    async def send(service, command, props):
        parsed, _ = wire.read_properties(props)
        calls.append((service, command, parsed))
        return 200, wire.properties([(12, b"123"), (7, b"1000"), (11, parsed[11])])

    adapter = SimpleNamespace(
        account="1",
        nim_account="1001",
        config={
            "id": "fixture",
            "_business_active": True,
            "_business_epoch": 1,
            "_business_revision": "1:1",
            "reply_private": False,
        },
        ledger=ledger,
        stopping=asyncio.Event(),
        connection_state="online",
        send_lock=asyncio.Lock(),
        business=SimpleNamespace(
            deployment=SimpleNamespace(key=lambda name: b"a" * 32)
        ),
        nim=SimpleNamespace(request=AsyncMock(side_effect=send)),
        send_reply_text=AsyncMock(),
    )

    async def sleep(seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(onboarding_delivery, "asyncio", SimpleNamespace(sleep=sleep))
    try:
        yield SimpleNamespace(platform=adapter), ledger, calls
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_group_activation_reply_is_native_idempotent_and_not_private(image_event):
    event, ledger, calls = image_event
    for _ in range(2):
        assert (
            await onboarding_delivery.send_onboarding_text(
                event.platform, "5", "905", "activation", "开通成功", lambda: None
            )
            == "accepted"
        )
    assert len(calls) == 1
    service, command, props = calls[0]
    assert (service, command) == (8, 2)
    assert props[0] == b"1" and props[1] == b"905"
    text = wire.open_message(b"a" * 32, props[10].decode())
    assert text.sender.id == 1 and text.target.id == 5 and text.session == 2
    assert text.content.data == "开通成功"
    assert (await ledger.receipt("activation"))["status"] == "accepted"
    async with ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
        assert (await cursor.fetchone())[0] == 0
    assert not event.platform.config["reply_private"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["offline", "revoked", "transport_changed", "stopping", "disabled", "guard"],
)
async def test_group_reply_rechecks_scope_after_pacing(
    image_event, monkeypatch, failure
):
    event, ledger, calls = image_event
    adapter = event.platform
    guard = Mock()

    async def change(seconds):
        if failure == "offline":
            adapter.connection_state = "disconnected"
        elif failure == "revoked":
            adapter.config["_business_epoch"] = 2
        elif failure == "transport_changed":
            adapter.nim_account = "999"
        elif failure == "stopping":
            adapter.stopping.set()
        elif failure == "disabled":
            adapter.config["enable"] = False
        else:
            guard.side_effect = wire.ProtocolError("group_role_revoked")

    monkeypatch.setattr(onboarding_delivery, "asyncio", SimpleNamespace(sleep=change))
    with pytest.raises(wire.ProtocolError):
        await onboarding_delivery.send_onboarding_text(
            adapter, "5", "905", "activation", "开通成功", guard
        )
    assert not calls
    assert await ledger.receipt("activation") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "peer"),
    [("", "905"), ("private/5/905", "905"), ("5", ""), ("5", "team5")],
)
async def test_group_reply_rejects_unverified_route_shapes(image_event, target, peer):
    event, _, calls = image_event
    with pytest.raises(wire.ProtocolError, match="onboarding_scope_changed"):
        await onboarding_delivery.send_onboarding_text(
            event.platform, target, peer, "activation", "开通成功", lambda: None
        )
    assert not calls


@pytest.mark.asyncio
async def test_group_reply_cooldown_yields_lock_and_rechecks_newer_send(
    image_event, monkeypatch
):
    event, _, calls = image_event
    clock, pauses = [1000.0], []
    waiting, release = asyncio.Event(), asyncio.Event()

    async def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            waiting.set()
            await release.wait()
        clock[0] += seconds
        if len(pauses) == 1:
            event.platform._onboarding_sent_at = clock[0] - 1

    monkeypatch.setattr(onboarding_delivery, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(
        onboarding_delivery,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]),
    )
    task = asyncio.create_task(
        onboarding_delivery.send_onboarding_text(
            event.platform, "5", "905", "activation", "开通成功", lambda: None
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not event.platform.send_lock.locked()
        async with event.platform.send_lock:
            event.platform._onboarding_sent_at = clock[0]
        release.set()
        assert await asyncio.wait_for(task, 1) == "accepted"
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert pauses == [3, 2, 1]
    assert event.platform._onboarding_sent_at == clock[0] == 1006
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_group_reply_unknown_delivery_is_not_retried(image_event):
    event, ledger, _ = image_event
    event.platform.nim.request.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await onboarding_delivery.send_onboarding_text(
            event.platform, "5", "905", "activation", "开通成功", lambda: None
        )
    assert (
        await onboarding_delivery.send_onboarding_text(
            event.platform, "5", "905", "activation", "开通成功", lambda: None
        )
        == "unknown"
    )
    event.platform.nim.request.assert_awaited_once()
    assert (await ledger.receipt("activation"))["status"] == "unknown"


@pytest.mark.asyncio
async def test_group_reply_local_encoding_failure_is_not_unknown(
    image_event, monkeypatch
):
    event, ledger, calls = image_event
    monkeypatch.setattr(
        onboarding_delivery.wire,
        "seal_message",
        Mock(side_effect=wire.ProtocolError("message_limit")),
    )
    with pytest.raises(wire.ProtocolError):
        await onboarding_delivery.send_onboarding_text(
            event.platform, "5", "905", "activation", "开通成功", lambda: None
        )
    assert not calls
    assert (await ledger.receipt("activation"))["status"] == "rejected"


@pytest.mark.asyncio
async def test_group_reply_actual_protobuf_overhead_rejected_before_transport(
    image_event,
):
    event, ledger, calls = image_event
    for _ in range(2):
        try:
            result = await onboarding_delivery.send_onboarding_text(
                event.platform, "5", "905", "activation", "x" * 4096, lambda: None
            )
        except wire.ProtocolError as error:
            assert str(error) == "message_limit"
        else:
            assert result == "rejected"
    assert not calls
    assert (await ledger.receipt("activation"))["status"] == "rejected"
    assert not getattr(event.platform, "_onboarding_sent_at", 0)
