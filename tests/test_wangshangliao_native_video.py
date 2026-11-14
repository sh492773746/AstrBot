"""Offline native video upload, protobuf, receipt and lifecycle contracts."""

import asyncio
import hashlib
import io
import json
import shutil
import struct
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import lz4.block
import nacl.bindings
import pytest
from google.protobuf import timestamp_pb2
from PIL import Image

from astrbot.builtin_stars.wangshangliao_moderation import native_video as video
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_native_guide_upload import Response, Session

ORIGIN = "https://business.invalid"
UPLOAD = "https://media.invalid/upload"
PREFIX = "https://assets.invalid/native/"
ASSET = "1234567890123456789"


def test_video_timestamp_schema_isolated_from_legacy_text_and_image():
    legacy = wire.ApplicationMessage.DESCRIPTOR.fields_by_name["created_at"]
    native = video.VideoMessage.DESCRIPTOR.fields_by_name["created_at"]
    assert legacy.type == legacy.TYPE_INT64
    assert native.type == native.TYPE_MESSAGE
    assert {
        field.name: (field.number, field.type) for field in native.message_type.fields
    } == {"seconds": (1, legacy.TYPE_INT64), "nanos": (2, legacy.TYPE_INT32)}


@pytest.fixture
def prepared():
    stream = io.BytesIO()
    Image.new("RGB", (160, 90), "#298868").save(stream, "JPEG")
    data = b"\x00\x00\x00\x18ftypisom" + b"fixture-mp4"
    return video.PreparedVideo(
        data, stream.getvalue(), 160, 90, 2, hashlib.sha256(data).hexdigest()
    )


@pytest.fixture
def transport(monkeypatch, prepared):
    session = Session(
        [
            Response(json.dumps({"code": 0, "id": ASSET}).encode()),
            Response(prepared.data),
            Response(prepared.thumbnail),
        ]
    )
    factory = Mock(return_value=session)
    monkeypatch.setattr(video.aiohttp, "ClientSession", factory)
    monkeypatch.setattr(video, "REVIEWED_MEDIA_ENDPOINTS", {ORIGIN: (UPLOAD, PREFIX)})
    client = SimpleNamespace(
        deployment=SimpleNamespace(origin=ORIGIN),
        fields=[0, 0, 0, 43506527],
        jwt="fixture-not-a-token",
        lock=asyncio.Lock(),
    )
    return client, session, factory


@pytest.mark.asyncio
async def test_upload_has_native_type_known_length_and_private_readback(
    transport, prepared
):
    client, session, factory = transport
    receipt = await video.upload_video(client, prepared, authorize=Mock())
    assert receipt == video.VideoAsset(
        "43506527", ASSET, prepared.digest, len(prepared.data), 160, 90, 2
    )
    assert factory.call_args.kwargs["trust_env"] is False
    assert isinstance(
        factory.call_args.kwargs["cookie_jar"], video.aiohttp.DummyCookieJar
    )
    assert [(method, url) for method, url, _ in session.calls] == [
        ("POST", UPLOAD),
        ("GET", PREFIX + ASSET),
        ("GET", PREFIX + ASSET + ".jpg"),
    ]
    form = session.calls[0][2]["data"]
    fields = {name["name"]: value for name, _headers, value in form._fields}
    assert fields["asset_type"] == "3"
    assert fields["use_for"] == "1"
    assert fields["width"] == "160" and fields["height"] == "90"
    assert fields["file"].size == len(prepared.data)
    assert form().size is not None
    writer = SimpleNamespace(write=AsyncMock())
    await fields["file"].write(writer)
    assert (
        b"".join(call.args[0] for call in writer.write.call_args_list) == prepared.data
    )
    for method, _url, kwargs in session.calls:
        assert kwargs["allow_redirects"] is False and kwargs["ssl"] is True
        assert kwargs["headers"] == ({"token": client.jwt} if method == "POST" else {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"digest": "0" * 64},
        {"data": b"not an mp4"},
        {"width": 0},
        {"duration": 0},
        {"thumbnail": b"broken"},
    ],
)
async def test_invalid_local_media_never_opens_network(transport, prepared, change):
    client, _session, factory = transport
    with pytest.raises(wire.ProtocolError) as caught:
        await video.upload_video(client, replace(prepared, **change), authorize=Mock())
    assert caught.value.submitted is False
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        Response(b"not-json"),
        Response(b'{"code":0,"id":1,"id":2}'),
        Response(b'{"code":true,"id":1}'),
        Response(b'{"code":0,"id":"https://other/"}'),
        Response(status=400),
    ],
)
async def test_bad_upload_receipt_is_not_a_delivery_receipt(
    transport, prepared, response
):
    client, session, _factory = transport
    session.responses = [response]
    with pytest.raises(wire.ProtocolError) as caught:
        await video.upload_video(client, prepared, authorize=Mock())
    assert caught.value.submitted is True
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("part", [1, 2])
async def test_changed_original_or_thumbnail_stops_before_send(
    transport, prepared, part
):
    client, session, _factory = transport
    session.responses[part] = Response(b"changed")
    with pytest.raises(wire.ProtocolError, match="video_readback_mismatch") as caught:
        await video.upload_video(client, prepared, authorize=Mock())
    assert caught.value.asset_id == ASSET
    assert caught.value.submitted


@pytest.mark.asyncio
async def test_upload_does_not_occupy_business_lock_or_follow_changed_credentials(
    transport, prepared
):
    client, session, _factory = transport

    def changed():
        assert not client.lock.locked()
        client.jwt = "rotated-fixture"

    session.responses[0].after_read = changed
    with pytest.raises(wire.ProtocolError, match="video_upload_scope_changed"):
        await video.upload_video(client, prepared, authorize=Mock())
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_authority_revocation_during_body_write_stops_later_chunks(
    transport, prepared
):
    client, session, _factory = transport
    allowed = True

    def guard():
        if not allowed:
            raise wire.ProtocolError("fixture_revoked")

    await video.upload_video(client, prepared, authorize=guard)
    form = session.calls[0][2]["data"]
    payload = next(
        value for name, _headers, value in form._fields if name["name"] == "file"
    )
    allowed = False
    writer = SimpleNamespace(write=AsyncMock())
    with pytest.raises(wire.ProtocolError, match="fixture_revoked"):
        await payload.write(writer)
    writer.write.assert_not_awaited()


@pytest.fixture
def asset(prepared):
    return video.VideoAsset(
        "43506527", ASSET, prepared.digest, len(prepared.data), 160, 90, 2
    )


@pytest.fixture
def adapter():
    return SimpleNamespace(
        account="43506527",
        nim_account="9001",
        config={
            "id": "video-fixture",
            "_business_active": True,
            "enable": True,
            "_business_epoch": 1,
            "_business_revision": "r",
            "proactive_send": {"enabled": True, "targets": ["1459282"]},
        },
        groups={"1459282": "7001"},
        enabled_groups=["1459282"],
        connection_state="online",
        stopping=asyncio.Event(),
        send_lock=asyncio.Lock(),
        nim=SimpleNamespace(request=AsyncMock()),
        business=SimpleNamespace(
            deployment=SimpleNamespace(key=lambda name: b"k" * 32)
        ),
    )


@pytest.mark.asyncio
async def test_native_codec_round_trip_and_duplicate_send_is_query_only(
    tmp_path, adapter, asset
):
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    try:
        adapter.nim.request.return_value = (
            200,
            wire.properties([(12, b"1234"), (7, b"1793610000000")]),
        )
        assert (
            await video.send_video(
                adapter, "1459282", asset, "fixed-key", authorize=Mock()
            )
            == "accepted"
        )
        service, command, properties = adapter.nim.request.call_args.args
        assert (service, command) == (8, 2)
        fields, end = wire.read_properties(properties)
        assert end == len(properties) and fields[0] == b"1" and fields[1] == b"7001"
        # The official legacy MessageDecryptor unwraps this exact prefix/suffix
        # before Base64 decoding; generic JSON acceptance is not sufficient.
        attachment = fields[10].decode("ascii")
        assert attachment.startswith('{"b":"') and attachment.endswith('"}')
        assert wire.unb64(attachment[6:-2]) == wire.unb64(json.loads(attachment)["b"])
        envelope = wire.Envelope.FromString(wire.unb64(json.loads(fields[10])["b"]))
        packed = nacl.bindings.crypto_aead_chacha20poly1305_decrypt(
            envelope.body + envelope.tag,
            struct.pack("<Q", envelope.aad),
            struct.pack("<Q", envelope.nonce),
            b"k" * 32,
        )
        plain = lz4.block.decompress(packed)
        # Official Message.created_at is Timestamp (tag 26), not int64 (tag 24).
        offset = 0
        timestamp = None
        while offset < len(plain):
            tag, offset = wire.read_varint(plain, offset)
            number, kind = tag >> 3, tag & 7
            if kind == 0:
                assert number != 3
                _, offset = wire.read_varint(plain, offset, bits=64)
            else:
                assert kind == 2
                size, offset = wire.read_varint(plain, offset)
                if number == 3:
                    timestamp = timestamp_pb2.Timestamp.FromString(
                        plain[offset : offset + size]
                    )
                offset += size
        assert timestamp is not None
        assert abs(timestamp.seconds - video.time.time()) < 5
        assert 0 <= timestamp.nanos < 1_000_000_000
        message = video.VideoMessage.FromString(plain)
        assert message.format == 3 and message.session == 2 and message.version == 2
        assert message.sender.id == 43506527 and message.target.id == 1459282
        assert message.video.asset.uri == ASSET and message.video.HasField("screenshot")
        assert not message.video.screenshot.uri and not message.video.external_url
        assert (message.video.width, message.video.height, message.video.duration) == (
            160,
            90,
            2,
        )
        assert (
            message.video.size == asset.size
            and message.client_id.encode() == fields[11]
        )
        adapter._onboarding_sent_at = 0
        assert (
            await video.send_video(
                adapter, "1459282", asset, "fixed-key", authorize=Mock()
            )
            == "accepted"
        )
        adapter.nim.request.assert_awaited_once()
        assert (await adapter.ledger.receipt("fixed-key"))["server_id"] == "1234"
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "timeout", "bad_receipt"])
async def test_native_outcomes_are_persisted_without_automatic_retry(
    tmp_path, adapter, asset, outcome
):
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    if outcome == "timeout":
        adapter.nim.request.side_effect = TimeoutError("not logged")
    else:
        adapter.nim.request.return_value = (400 if outcome == "rejected" else 200, b"")
    try:
        if outcome == "rejected":
            assert (
                await video.send_video(
                    adapter, "1459282", asset, "key", authorize=Mock()
                )
                == "rejected"
            )
        else:
            with pytest.raises((TimeoutError, wire.ProtocolError)):
                await video.send_video(
                    adapter, "1459282", asset, "key", authorize=Mock()
                )
        state = "rejected" if outcome == "rejected" else "unknown"
        assert (await adapter.ledger.receipt("key"))["status"] == state
        adapter._onboarding_sent_at = 0
        assert (
            await video.send_video(adapter, "1459282", asset, "key", authorize=Mock())
            == state
        )
        adapter.nim.request.assert_awaited_once()
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "group", "disabled", "offline", "proactive", "durable"]
)
async def test_revoked_or_cross_scope_video_is_not_submitted(
    tmp_path, adapter, asset, change
):
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    try:
        if change == "account":
            asset = replace(asset, account="31846829")
        elif change == "group":
            adapter.groups.clear()
        elif change == "disabled":
            adapter.config["enable"] = False
        elif change == "offline":
            adapter.connection_state = "offline"
        elif change == "proactive":
            adapter.config["proactive_send"]["enabled"] = False

        async def check():
            if change == "durable":
                raise wire.ProtocolError("fixture_durable_revoked")

        with pytest.raises(wire.ProtocolError):
            await video.send_video(
                adapter, "1459282", asset, "key", authorize=Mock(), before_send=check
            )
        adapter.nim.request.assert_not_awaited()
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_real_neutral_mp4_is_remuxed_and_cover_is_extracted(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Optional FFmpeg executables are not installed")
    source = tmp_path / "neutral.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=teal:s=160x90:r=10",
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=20,
    )
    prepared = await video.prepare_video(source.read_bytes(), tmp_path)
    assert (prepared.width, prepared.height, prepared.duration) == (160, 90, 2)
    assert prepared.data[4:8] == b"ftyp"
    with Image.open(io.BytesIO(prepared.thumbnail)) as image:
        assert image.format == "JPEG" and max(image.size) <= 320
    assert prepared.digest == hashlib.sha256(prepared.data).hexdigest()
    assert not (tmp_path / "source.mp4").exists()


@pytest.mark.asyncio
async def test_media_process_timeout_and_cancellation_reap_child(monkeypatch):
    child = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(side_effect=TimeoutError)),
        returncode=None,
        kill=Mock(),
        wait=AsyncMock(),
    )
    monkeypatch.setattr(
        video.asyncio, "create_subprocess_exec", AsyncMock(return_value=child)
    )
    with pytest.raises(wire.ProtocolError, match="video_tool_unavailable_or_timeout"):
        await video._media_process("fixture")
    child.kill.assert_called_once()
    child.wait.assert_awaited_once()
