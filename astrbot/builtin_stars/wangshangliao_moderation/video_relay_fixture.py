"""Fixed neutral video for explicitly confirmed, test-group-only acceptance."""

import asyncio
import hashlib
import json
import shutil
import struct
import time

from google.protobuf import unknown_fields
from google.protobuf.message import DecodeError

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.dashboard.responses import ApiError

from .native_video import VideoMessage, _media_process
from .open_store import OpenError
from .video_relay import CmsItem, database

TEST_SOURCE = "fixture:neutral-video-v3"


def require_test_scope(account, group):
    """Keep the fixture limited to the existing main account and test group."""
    if (str(account), group) != ("43506527", "1459282"):
        raise ProtocolError("video_test_scope_rejected")


async def inspect_test_videos(adapter, group, authorize):
    """Read bounded own-account video metadata through the existing connection.

    Args:
        adapter: Online transport for the fixed test account.
        group: Fixed isolated test group, never an arbitrary history target.
        authorize: Current authenticated dashboard scope check.

    Returns:
        Structural metadata only, without chat text, URLs, keys or media bytes.

    Raises:
        ApiError: If scope, transport, authentication or history framing fails.
    """
    try:
        require_test_scope(adapter.account, group)
        guard = account_guard(adapter, authorize)
        nim, business, team = adapter.nim, adapter.business, adapter.groups.get(group)

        def check():
            guard()
            require_test_scope(adapter.account, group)
            if (
                not nim
                or not business
                or not team
                or adapter.nim is not nim
                or adapter.business is not business
                or adapter.groups.get(group) != team
                or adapter.connection_state != "online"
            ):
                raise ProtocolError("video_inspection_scope_changed")

        check()
        now = int(time.time() * 1000)
        # Official Web SDK getTeamHistoryMsgs (8,23): four LE longs, LE int,
        # bool, then a varint-sized long array. This does not ACK or replay.
        body = struct.pack("<QQQQI?", int(team), now - 7200000, now, 0, 10, False)
        body += wire.varint(1) + struct.pack("<Q", 100)
        code, response = await nim.request(8, 23, body)
        check()
        if code != 200:
            raise ProtocolError("video_inspection_history_rejected")
        count, offset = wire.read_varint(response, 0)
        if count > 10:
            raise ProtocolError("video_inspection_history_limit")
        rows = []
        for _ in range(count):
            fields, offset = wire.read_properties(response, offset)
            rows.append(fields)
        if offset != len(response):
            raise ProtocolError("video_inspection_history_shape")
        videos, undecodable = [], 0
        for fields in rows:
            if fields.get(8) != b"100":
                continue
            try:
                text = fields.get(10, b"").decode("utf-8")
                message = wire.open_message(
                    business.deployment.key("message_key"), text
                )
            except (ProtocolError, UnicodeError):
                undecodable += 1
                continue
            if (
                message.sender.id != int(adapter.account)
                or message.target.id != int(group)
                or message.session != 2
                or message.format != 3
            ):
                continue
            native = VideoMessage.FromString(message.SerializeToString())
            encoded = wire.unb64(
                json.loads(text)["b"] if text.startswith("{") else text
            )
            envelope = wire._message_envelope(encoded)
            signed = len(envelope.tag) != 16
            if signed:
                packet = wire.MessagePacket.FromString(encoded)
                envelope = wire._message_envelope(wire.unb64(packet.msg))
            asset = native.video.asset.uri
            screenshot = native.video.screenshot.uri
            videos.append(
                {
                    "message_id": fields.get(12, b"").decode("ascii"),
                    "nim_time": fields.get(7, b"").decode("ascii"),
                    "nim_fields": {str(k): len(v) for k, v in fields.items()},
                    "nim_scalars": {
                        str(k): int(v)
                        for k, v in fields.items()
                        if k
                        in {
                            0,
                            4,
                            8,
                            26,
                            28,
                            100,
                            101,
                            102,
                            103,
                            105,
                            107,
                            108,
                            109,
                            110,
                        }
                        and v.isdigit()
                        and len(v) <= 10
                    },
                    "compact_wrapper": (
                        text == json.dumps(json.loads(text), separators=(",", ":"))
                        if text.startswith("{")
                        else None
                    ),
                    "signed_packet": signed,
                    "compressed": bool(envelope.aad),
                    "envelope_fields": [
                        [field.number, {4: 0, 6: 1, 12: 2}[field.type]]
                        for field, _value in envelope.ListFields()
                    ],
                    "envelope_unknown_fields": [
                        [field.field_number, field.wire_type]
                        for field in unknown_fields.UnknownFieldSet(envelope)
                    ],
                    "envelope_time": envelope.timestamp,
                    "device": native.device,
                    "version": native.version,
                    "created_at": {
                        "seconds": native.created_at.seconds,
                        "nanos": native.created_at.nanos,
                    },
                    "fields": [
                        [field.number, field.type]
                        for field, _value in native.ListFields()
                    ],
                    "unknown_fields": [
                        [field.field_number, field.wire_type]
                        for field in unknown_fields.UnknownFieldSet(native)
                    ],
                    "sender_unknown_fields": [
                        [field.field_number, field.wire_type]
                        for field in unknown_fields.UnknownFieldSet(native.sender)
                    ],
                    "video": {
                        "asset": asset if asset.isdecimal() else "",
                        "screenshot": screenshot if screenshot.isdecimal() else "",
                        "external_url_present": bool(native.video.external_url),
                        "duration": native.video.duration,
                        "width": native.video.width,
                        "height": native.video.height,
                        "size": native.video.size,
                        "fields": [
                            [field.number, field.type]
                            for field, _value in native.video.ListFields()
                        ],
                        "unknown_fields": [
                            [field.field_number, field.wire_type]
                            for field in unknown_fields.UnknownFieldSet(native.video)
                        ],
                    },
                }
            )
        check()
        return {"videos": videos, "scanned": count, "undecodable": undecodable}
    except (ValueError, UnicodeError, TimeoutError, struct.error, DecodeError):
        raise ApiError(
            "测试群视频结构读取失败；未下载、发送或重放任何消息。", status_code=409
        ) from None


async def is_fixture_request(path, account, group, actor, revision, body):
    """Recognize only the fixed preview or its exact unconsumed confirmation."""
    if body["operation"] == "preview_test_video":
        if (account, group) != ("43506527", "1459282"):
            raise ApiError("中性验收视频仅限指定测试账号及测试群。", status_code=403)
        return True
    if body["operation"] != "send_video":
        return False
    async with AsyncDatabase(lambda: database(path)) as db:
        row = await db.fetchone(
            "SELECT metadata FROM relay_video_jobs WHERE token_digest=? AND account=? "
            "AND group_id=? AND actor=? AND revision=? AND version=? "
            "AND state='prepared' AND expires>?",
            (
                hashlib.sha256(body["token"].encode()).hexdigest(),
                account,
                group,
                actor,
                revision,
                body["version"],
                time.time(),
            ),
        )
    if row is None or json.loads(row["metadata"]).get("source") != TEST_SOURCE:
        return False
    require_test_scope(account, group)
    return True


async def posting_guard(service, adapter, group, guard):
    """Check membership and posting grants, without any moderation privilege.

    The synthetic fixture sends no advertisement or moderation action. Its
    explicitly authenticated operator need not promote the bot to administrator.
    """
    guard()
    account = str(adapter.account)
    require_test_scope(account, group)
    tenant = await asyncio.to_thread(service.store.tenant, account, group)
    key = (account, group)
    if (
        not service.active
        or service.settings["paused"]
        or service.capacity_paused
        or not tenant
        or not tenant["active"]
        or not tenant["present"]
        or tenant["paused"]
        or tenant.get("admission", "legacy") not in {"legacy", "verified"}
        or tenant["cleanup_epoch"] > tenant["cleanup_done"]
        or key in service.suspended_groups
        or service.membership_receiving.get(key)
        or service.reconciling_groups.get(key)
        or group in service.retired_groups
        or adapter.connection_state != "online"
        or not adapter.config.get("_business_active")
        or not adapter.config.get("reply_groups", {}).get(group, True)
        or not adapter.config.get("proactive_send", {}).get("enabled", False)
        or group not in adapter.config.get("proactive_send", {}).get("targets", [])
    ):
        raise OpenError("business_rules_inactive")
    members = service.member_evidence.get(key, {})
    if service.capabilities.get(key, {}).get("checked_at", 0) < time.time() - 60:
        members = await service.roster(adapter, group)
        guard()
    if str(members.get(account, {}).get("nimId", "")) != str(
        adapter.nim_account
    ) or members.get(account, {}).get("groupRole") not in {
        "GROUP_ROLE_MEMBER",
        "GROUP_ROLE_ADMIN",
        "GROUP_ROLE_OWNER",
    }:
        raise OpenError("group_unavailable")
    current = await asyncio.to_thread(service.store.tenant, account, group)
    if current != tenant:
        raise OpenError("tenant_changed")
    guard()


async def neutral_video(directory):
    """Generate five seconds of moving test bars and a quiet audio tone.

    Args:
        directory: Operation-owned private cache directory.

    Returns:
        Fixed source identity and bounded MP4 bytes for the normal delivery flow.

    Raises:
        ProtocolError: If local FFmpeg generation fails.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ProtocolError("video_tools_missing")
    output = directory / "fixture.mp4"
    await _media_process(
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=25",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "5",
        "-vf",
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "text='WSL TEST VIDEO V3':fontsize=36:fontcolor=white:box=1:"
        "boxcolor=black@0.7:x=(w-text_w)/2:y=(h-text_h)/2",
        "-af",
        "volume=0.15",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "26",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-threads",
        "1",
        "-movflags",
        "+faststart",
        str(output),
    )
    return (
        CmsItem(vod_id=3, title="WSL_TEST_VIDEO_V3_5_SECONDS"),
        await asyncio.to_thread(output.read_bytes),
    )
