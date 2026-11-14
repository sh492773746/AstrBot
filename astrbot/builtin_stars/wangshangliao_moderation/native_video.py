"""Bounded native group videos using the reviewed Android 3.7.3 contract."""

import asyncio
import hashlib
import io
import json
import math
import secrets
import shutil
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import lz4.block
import nacl.bindings
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from PIL import Image

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.database import account_guard

from .native_guide_upload import (
    REVIEWED_MEDIA_ENDPOINTS,
    _read_bounded,
    is_native_asset_id,
)

VIDEO_LIMIT = 112 * 1024 * 1024

# Android 3.7.3 CMessage.Message.video=103; MessageHelper leaves screenshot empty
# and clients resolve the multipart thumbnail at the video's asset ID + ".jpg".
_schema = descriptor_pb2.FileDescriptorProto()
_schema.ParseFromString(wire.ApplicationMessage.DESCRIPTOR.file.serialized_pb)
_schema.name = "wangshangliao-native-video.proto"
_timestamp = _schema.message_type.add(name="NativeTimestamp")
_timestamp.field.add(name="seconds", number=1, type=3, label=1)
_timestamp.field.add(name="nanos", number=2, type=5, label=1)
_video = _schema.message_type.add(name="NativeVideo")
for _name, _number, _type in (
    ("asset", 1, 11),
    ("screenshot", 2, 11),
    ("external_url", 3, 9),
    ("duration", 4, 5),
    ("width", 5, 5),
    ("height", 6, 5),
    ("size", 7, 5),
):
    _field = _video.field.add(name=_name, number=_number, type=_type, label=1)
    if _type == 11:
        _field.type_name = ".wsl.IncomingAsset"
for _message in _schema.message_type:
    if _message.name == "ApplicationMessage":
        # Keep the legacy text/image codec unchanged; videos use official Timestamp.
        for _field in _message.field:
            if _field.name == "created_at":
                _field.type = 11
                _field.type_name = ".wsl.NativeTimestamp"
        _message.field.add(
            name="video", number=103, type=11, label=1, type_name=".wsl.NativeVideo"
        )
_pool = descriptor_pool.DescriptorPool()
_pool.Add(_schema)
VideoMessage = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("wsl.ApplicationMessage")
)


@dataclass(frozen=True)
class PreparedVideo:
    """Server-normalized MP4 and its locally extracted JPEG thumbnail."""

    data: bytes
    thumbnail: bytes
    width: int
    height: int
    duration: int
    digest: str


@dataclass(frozen=True)
class VideoAsset:
    """Account-bound upload evidence, not evidence of message delivery."""

    account: str
    asset_id: str
    digest: str
    size: int
    width: int
    height: int
    duration: int


async def _media_process(*arguments, timeout=30):
    """Run a local media tool with bounded output and cancellation cleanup.

    Args:
        arguments: Server-owned executable and argument vector, never a shell.
        timeout: Maximum process lifetime in seconds.

    Returns:
        At most 64 KiB of standard output.

    Raises:
        wire.ProtocolError: If the tool is missing, fails or exceeds a limit.
    """
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(timeout):
            output = bytearray()
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > 65536:
                    raise wire.ProtocolError("video_probe_limit")
            if await process.wait() != 0:
                raise wire.ProtocolError("video_media_invalid")
            return bytes(output)
    except (FileNotFoundError, TimeoutError):
        raise wire.ProtocolError("video_tool_unavailable_or_timeout") from None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def prepare_video(data, directory: Path, *, max_bytes=VIDEO_LIMIT):
    """Validate, remux and extract a cover without executing remote media URLs.

    Args:
        data: Downloaded bounded MP4/fMP4 bytes.
        directory: Private operation-owned temporary directory.
        max_bytes: Effective single-video bound, capped at 112 MiB.

    Returns:
        Validated H.264 MP4 with at most one AAC audio stream.

    Raises:
        wire.ProtocolError: If tools, metadata, media or limits are invalid.
    """
    if (
        type(data) is not bytes
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= VIDEO_LIMIT
        or not 12 <= len(data) <= max_bytes
        or data[4:8] != b"ftyp"
    ):
        raise wire.ProtocolError("video_media_invalid")
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise wire.ProtocolError("video_tools_missing")
    source, output, cover = (
        directory / "source.mp4",
        directory / "video.mp4",
        directory / "cover.jpg",
    )
    await asyncio.to_thread(source.write_bytes, data)
    raw = await _media_process(
        ffprobe,
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        "mov",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,pix_fmt:"
        "stream_side_data=rotation",
        "-of",
        "json",
        str(source),
    )
    try:
        metadata = json.loads(raw)
        streams = metadata["streams"]
        videos = [row for row in streams if row["codec_type"] == "video"]
        audios = [row for row in streams if row["codec_type"] == "audio"]
        duration = float(metadata["format"]["duration"])
        video = videos[0]
        width, height = video["width"], video["height"]
        if math.isfinite(duration) and duration > 1200:
            raise wire.ProtocolError("video_duration_limit")
        if (
            len(videos) != 1
            or len(audios) > 1
            or len(streams) != len(videos) + len(audios)
            or video["codec_name"] != "h264"
            or video.get("pix_fmt") not in {"yuv420p", "yuvj420p"}
            or any(row["codec_name"] != "aac" for row in audios)
            or type(width) is not int
            or type(height) is not int
            or not 2 <= width <= 2048
            or not 2 <= height <= 2048
            or not math.isfinite(duration)
            or not 1 <= duration <= 1200
            or any(
                row.get("rotation", 0) != 0 for row in video.get("side_data_list", [])
            )
        ):
            raise ValueError
    except wire.ProtocolError:
        raise
    except (KeyError, TypeError, ValueError, IndexError):
        raise wire.ProtocolError("video_format_unsupported") from None
    # Remux, do not transcode or silently truncate. The input demuxer cannot
    # resolve HTTP, playlists or external data references.
    await _media_process(
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-threads",
        "1",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        "mov",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        str(output),
    )
    if not 0 < (await asyncio.to_thread(output.stat)).st_size <= max_bytes:
        raise wire.ProtocolError("video_size_limit")
    await _media_process(
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-threads",
        "1",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        "mov",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
        "-i",
        str(output),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:320:force_original_aspect_ratio=decrease",
        "-threads",
        "1",
        "-q:v",
        "3",
        str(cover),
    )
    if not 0 < (await asyncio.to_thread(cover.stat)).st_size <= 1024 * 1024:
        raise wire.ProtocolError("video_thumbnail_invalid")
    normalized = await asyncio.to_thread(output.read_bytes)
    thumbnail = await asyncio.to_thread(cover.read_bytes)
    digest = await asyncio.to_thread(lambda: hashlib.sha256(normalized).hexdigest())
    await asyncio.to_thread(source.unlink)
    return PreparedVideo(normalized, thumbnail, width, height, int(duration), digest)


async def upload_video(client, video: PreparedVideo, *, authorize):
    """Upload MP4 plus JPEG and verify both private native asset readbacks.

    Args:
        client: Existing authenticated business client; never logs in.
        video: Server-prepared immutable MP4 and thumbnail.
        authorize: Synchronous current account, group and operation guard.

    Returns:
        Verified account-bound native video asset.

    Raises:
        wire.ProtocolError: On invalid input, rejection, unknown result or revocation.
    """
    submitted = False
    asset = ""
    try:
        if (
            not isinstance(video, PreparedVideo)
            or not 12 <= len(video.data) <= VIDEO_LIMIT
            or video.data[4:8] != b"ftyp"
            or not 0 < len(video.thumbnail) <= 1024 * 1024
            or type(video.width) is not int
            or type(video.height) is not int
            or not 2 <= video.width <= 2048
            or not 2 <= video.height <= 2048
            or type(video.duration) is not int
            or not 1 <= video.duration <= 1200
            or await asyncio.to_thread(lambda: hashlib.sha256(video.data).hexdigest())
            != video.digest
        ):
            raise wire.ProtocolError("video_media_invalid")
        with Image.open(io.BytesIO(video.thumbnail)) as image:
            if image.format != "JPEG" or max(image.size) > 320:
                raise wire.ProtocolError("video_thumbnail_invalid")
            image.verify()
        deployment = client.deployment
        origin = deployment.origin.rstrip("/")
        endpoints = REVIEWED_MEDIA_ENDPOINTS.get(origin)
        if type(endpoints) is not tuple or len(endpoints) != 2:
            raise wire.ProtocolError("video_endpoint_unreviewed")
        upload_url, prefix = endpoints
        for address in endpoints:
            parsed = urlsplit(address)
            if (
                not address.isascii()
                or len(address) > 512
                or any(ord(char) <= 32 or ord(char) == 127 for char in address)
                or "\\" in address
                or "%" in address
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise wire.ProtocolError("video_endpoint_invalid")
        if not prefix.endswith("/"):
            raise wire.ProtocolError("video_endpoint_invalid")
        # Snapshot credentials briefly; a video upload must not hold the
        # moderation business HTTP lock for its entire upload/readback lifetime.
        async with client.lock:
            authorize()
            account, token = client.fields[3], client.jwt
            if (
                type(account) is not int
                or not 0 < account < 1 << 30
                or type(token) is not str
                or not 0 < len(token) <= 65536
                or not token.isascii()
                or any(ord(char) <= 32 or ord(char) == 127 for char in token)
            ):
                raise wire.ProtocolError("reauth_required")

        def check():
            authorize()
            if (
                client.deployment is not deployment
                or deployment.origin.rstrip("/") != origin
                or client.fields[3] != account
                or client.jwt != token
                or REVIEWED_MEDIA_ENDPOINTS.get(origin) != endpoints
            ):
                raise wire.ProtocolError("video_upload_scope_changed")

        class VideoPayload(aiohttp.payload.BytesPayload):
            async def write(self, writer):
                for offset in range(0, len(video.data), 65536):
                    check()
                    await writer.write(video.data[offset : offset + 65536])

            async def write_with_length(self, writer, content_length):
                if content_length is not None and content_length != len(video.data):
                    raise wire.ProtocolError("video_upload_length")
                await self.write(writer)

        check()
        form = aiohttp.FormData()
        form.add_field("asset_type", "3")
        form.add_field("use_for", "1")
        form.add_field("width", str(video.width))
        form.add_field("height", str(video.height))
        form.add_field(
            "file",
            VideoPayload(video.data, content_type="video/mp4"),
            filename="video.mp4",
            content_type="video/mp4",
        )
        form.add_field(
            "thumbnail",
            video.thumbnail,
            filename="cover.jpg",
            content_type="image/jpeg",
        )
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=1200, connect=10, sock_read=180),
        ) as http:
            check()
            submitted = True
            async with http.post(
                upload_url,
                data=form,
                headers={"token": token},
                allow_redirects=False,
                ssl=True,
            ) as response:
                if response.status != 200:
                    detail = await _read_bounded(response, 4096)
                    text = detail.decode("utf-8", "ignore").lower()
                    if response.status in {400, 409} and any(
                        marker in text
                        for marker in ("重复", "已存在", "duplicate", "exists")
                    ):
                        raise wire.ProtocolError("video_upload_duplicate")
                    status = {
                        401: "auth",
                        403: "forbidden",
                        413: "size",
                        429: "rate",
                        500: "upstream",
                        502: "upstream",
                        503: "upstream",
                        504: "upstream",
                    }.get(response.status, "rejected")
                    raise wire.ProtocolError("video_upload_rejected_" + status)
                raw = await _read_bounded(response, 65536)

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            receipt = json.loads(raw, object_pairs_hook=unique_object)
            if (
                type(receipt) is not dict
                or type(receipt.get("code")) is not int
                or receipt["code"] != 0
            ):
                raise wire.ProtocolError("video_upload_receipt_invalid")
            value = receipt.get("id")
            asset = str(value) if type(value) is int else value
            if not is_native_asset_id(asset):
                asset = ""
                raise wire.ProtocolError("video_upload_asset_invalid")
            for suffix, expected, limit in (
                ("", video.digest, len(video.data)),
                (
                    ".jpg",
                    hashlib.sha256(video.thumbnail).hexdigest(),
                    len(video.thumbnail),
                ),
            ):
                check()
                async with http.get(
                    prefix + asset + suffix, headers={}, allow_redirects=False, ssl=True
                ) as response:
                    if response.status != 200:
                        raise wire.ProtocolError("video_readback_failed")
                    digest, size = hashlib.sha256(), 0
                    async for chunk in response.content.iter_chunked(65536):
                        check()
                        size += len(chunk)
                        if size > limit:
                            raise wire.ProtocolError("video_readback_mismatch")
                        digest.update(chunk)
                    if size != limit or digest.hexdigest() != expected:
                        raise wire.ProtocolError("video_readback_mismatch")
            check()
        return VideoAsset(
            str(account),
            asset,
            video.digest,
            len(video.data),
            video.width,
            video.height,
            video.duration,
        )
    except BaseException as cause:
        if isinstance(cause, (asyncio.CancelledError, wire.ProtocolError)):
            error = cause
        elif isinstance(cause, Exception):
            error = wire.ProtocolError(
                "video_upload_unknown" if submitted else "video_upload_invalid"
            )
        else:
            raise
        error.submitted = submitted
        error.asset_id = asset
        raise error from None


async def send_video(
    adapter, group, asset: VideoAsset, key, *, authorize, before_send=None
):
    """Submit one native video with a durable idempotency key and receipt.

    Args:
        adapter: Existing native account transport.
        group: Exact enabled destination group.
        asset: Verified upload receipt owned by this account.
        key: Stable operation key, never regenerated for an uncertain result.
        authorize: Current saved job, configuration and tenant authority guard.
        before_send: Optional asynchronous durable authority check under send lock.

    Returns:
        Accepted, rejected or the existing uncertain outcome, never phone verification.

    Raises:
        wire.ProtocolError: If scope, transport, media or a receipt changes.
    """
    account, nim, business, ledger = (
        str(adapter.account),
        adapter.nim,
        adapter.business,
        adapter.ledger,
    )
    team, nim_account = adapter.groups.get(group), adapter.nim_account
    guard = account_guard(adapter, authorize)

    def check():
        guard()
        if (
            not isinstance(asset, VideoAsset)
            or asset.account != account
            or not is_native_asset_id(asset.asset_id)
            or not all(
                type(value) is str
                and value.isascii()
                and value.isdecimal()
                and not value.startswith("0")
                and len(value) <= 10
                and 0 < int(value) < 1 << 30
                for value in (account, group)
            )
            or not 0 < asset.size <= VIDEO_LIMIT
            or not 2 <= asset.width <= 2048
            or not 2 <= asset.height <= 2048
            or not 1 <= asset.duration <= 1200
            or not team
            or adapter.groups.get(group) != team
            or adapter.nim is not nim
            or adapter.business is not business
            or adapter.ledger is not ledger
            or adapter.nim_account != nim_account
            or adapter.stopping.is_set()
            or adapter.connection_state != "online"
            or not adapter.config.get("_business_active")
            or not adapter.config.get("enable", True)
            or group not in adapter.enabled_groups
            or not adapter.config.get("proactive_send", {}).get("enabled", False)
            or group not in adapter.config.get("proactive_send", {}).get("targets", [])
        ):
            raise wire.ProtocolError("video_send_scope_changed")

    while True:
        check()
        previous = getattr(adapter, "_onboarding_sent_at", 0)
        await asyncio.sleep(max(0, 3 - (time.monotonic() - previous)))
        async with adapter.send_lock:
            check()
            if getattr(adapter, "_onboarding_sent_at", 0) != previous:
                continue
            if before_send is not None:
                await before_send()
                check()
            nonce, state = await ledger.reserve(
                key, json.dumps([account, group, asset.__dict__], sort_keys=True)
            )
            if state != "new":
                return "unknown" if state == "sending" else state
            client_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{adapter.config['id']}/{key}")
            )
            try:
                seconds, nanos = divmod(time.time_ns(), 1_000_000_000)
                message = VideoMessage(
                    sender={"id": int(account)},
                    target={"id": int(group)},
                    created_at={"seconds": seconds, "nanos": nanos},
                    device=1,
                    session=2,
                    version=2,
                    format=3,
                    client_id=client_id,
                    video={
                        "asset": {"uri": asset.asset_id},
                        "screenshot": {},
                        "duration": asset.duration,
                        "width": asset.width,
                        "height": asset.height,
                        "size": asset.size,
                    },
                )
                aad = secrets.randbits(64) or 1
                sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
                    lz4.block.compress(message.SerializeToString(), store_size=True),
                    struct.pack("<Q", aad),
                    struct.pack("<Q", int(nonce)),
                    business.deployment.key("message_key"),
                )
                envelope = wire.Envelope(
                    header=int(account) << 34 | int(group) << 4 | 2 << 2 | 1,
                    timestamp=int(time.time()),
                    nonce=int(nonce),
                    aad=aad,
                    body=sealed[:-16],
                    tag=sealed[-16:],
                )
                properties = wire.properties(
                    [
                        (0, b"1"),
                        (1, str(team).encode()),
                        (8, b"100"),
                        (
                            10,
                            json.dumps(
                                {"b": wire.b64(envelope.SerializeToString())},
                                separators=(",", ":"),
                            ).encode(),
                        ),
                        (11, client_id.encode()),
                    ]
                )
                if before_send is not None:
                    await before_send()
                check()
            except BaseException:
                await ledger.finish(key, "rejected")
                raise
            adapter._onboarding_sent_at = time.monotonic()
            try:
                code, body = await nim.request(8, 2, properties)
                if code != 200:
                    await ledger.finish(key, "rejected")
                    return "rejected"
                receipt, end = wire.read_properties(body)
                if (
                    end != len(body)
                    or not is_native_asset_id(receipt.get(12, b"").decode("ascii"))
                    or not receipt.get(7, b"").isdigit()
                    or receipt.get(11, client_id.encode()) != client_id.encode()
                ):
                    raise wire.ProtocolError("video_send_receipt_invalid")
                await ledger.finish(key, "accepted", receipt[12].decode("ascii"))
                return "accepted"
            except BaseException:
                await ledger.finish(key, "unknown")
                raise
