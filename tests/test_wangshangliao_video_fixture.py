"""The neutral fixture is posting-only and cannot relax CMS or moderation gates."""

import json
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import lz4.block
import nacl.bindings
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    video_relay_fixture as fixture,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.dashboard.responses import ApiError


@pytest.fixture
def context():
    tenant = {
        "active": True,
        "present": True,
        "paused": False,
        "admission": "verified",
        "cleanup_epoch": 0,
        "cleanup_done": 0,
        "version": 1,
    }
    key = ("43506527", "1459282")
    members = {"43506527": {"nimId": "123", "groupRole": "GROUP_ROLE_MEMBER"}}
    service = SimpleNamespace(
        active=True,
        capacity_paused=False,
        settings={"paused": False},
        store=SimpleNamespace(tenant=Mock(return_value=tenant)),
        suspended_groups=set(),
        membership_receiving={},
        reconciling_groups={},
        retired_groups=set(),
        member_evidence={key: members},
        capabilities={key: {"checked_at": time.time()}},
        roster=AsyncMock(return_value=members),
    )
    adapter = SimpleNamespace(
        account="43506527",
        nim_account="123",
        connection_state="online",
        config={
            "_business_active": True,
            "proactive_send": {"enabled": True, "targets": ["1459282"]},
        },
    )
    return service, adapter, tenant


@pytest.mark.asyncio
async def test_neutral_fixture_does_not_need_administrator_or_stale_advertisement(
    context,
):
    service, adapter, _ = context
    await fixture.posting_guard(service, adapter, "1459282", Mock())
    service.roster.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pause", "grant", "identity", "leaving", "revoked"])
async def test_neutral_fixture_preserves_live_posting_fences(context, case):
    service, adapter, tenant = context
    if case == "pause":
        tenant["paused"] = True
    elif case == "grant":
        adapter.config["proactive_send"]["targets"] = []
    elif case == "identity":
        adapter.nim_account = "different"
    elif case == "leaving":
        service.membership_receiving[("43506527", "1459282")] = True
    else:
        service.active = False
    with pytest.raises(OpenError):
        await fixture.posting_guard(service, adapter, "1459282", Mock())


@pytest.mark.asyncio
async def test_neutral_preview_rejected_before_any_database_access_outside_test_scope(
    tmp_path,
):
    with pytest.raises(ApiError) as error:
        await fixture.is_fixture_request(
            tmp_path / "relay.sqlite3",
            "43506527",
            "1459534",
            "admin",
            "r",
            {"operation": "preview_test_video"},
        )
    assert error.value.status_code == 403
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_regular_cms_preview_never_uses_test_permission_path(tmp_path):
    assert not await fixture.is_fixture_request(
        tmp_path / "relay.sqlite3",
        "43506527",
        "1459282",
        "admin",
        "r",
        {"operation": "preview_video"},
    )
    assert not list(tmp_path.iterdir())


@pytest.fixture
def history_transport():
    key = b"k" * 32
    native = fixture.VideoMessage(
        sender={"id": 43506527, "name": "PRIVATE NAME"},
        target={"id": 1459282},
        device=3,
        session=2,
        version=2,
        format=3,
        created_at={"seconds": int(time.time()), "nanos": 0},
        video={
            "asset": {"uri": "1234567890123456789"},
            "external_url": "https://private.invalid/?secret=PRIVATE",
            "duration": 12,
            "width": 720,
            "height": 1280,
            "size": 1048576,
        },
    )

    def encode(message):
        nonce, aad = struct.pack("<Q", 1), struct.pack("<Q", 2)
        sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
            lz4.block.compress(message.SerializeToString(), store_size=True),
            aad,
            nonce,
            key,
        )
        envelope = wire.Envelope(
            header=message.sender.id << 34
            | message.target.id << 4
            | 2 << 2
            | message.device,
            timestamp=int(time.time()),
            nonce=1,
            aad=2,
            body=sealed[:-16],
            tag=sealed[-16:],
        )
        return wire.properties(
            [
                (0, b"1"),
                (1, b"123"),
                (8, b"100"),
                (
                    10,
                    json.dumps({"b": wire.b64(envelope.SerializeToString())}).encode(),
                ),
                (7, b"1793655344733"),
                (12, b"4297058878635377937"),
            ]
        )

    adapter = SimpleNamespace(
        account="43506527",
        config={"id": "test", "_business_active": True},
        nim=SimpleNamespace(
            request=AsyncMock(return_value=(200, b"\x01" + encode(native)))
        ),
        business=SimpleNamespace(
            deployment=SimpleNamespace(key=Mock(return_value=key))
        ),
        groups={"1459282": "123"},
        connection_state="online",
    )
    return adapter, native, encode


@pytest.mark.asyncio
async def test_history_inspection_is_bounded_read_only_and_excludes_private_content(
    history_transport,
):
    adapter, _native, _encode = history_transport
    result = await fixture.inspect_test_videos(adapter, "1459282", Mock())
    assert result["scanned"] == 1 and result["undecodable"] == 0
    video = result["videos"][0]
    assert video["video"]["duration"] == 12
    assert video["video"]["external_url_present"] is True
    assert video["video"]["asset"] == "1234567890123456789"
    assert video["device"] == 3 and [103, 11] in video["fields"]
    assert "PRIVATE" not in json.dumps(result)
    assert "private.invalid" not in json.dumps(result)
    service, command, body = adapter.nim.request.call_args.args
    assert (service, command) == (8, 23)
    team, begin, end, last, limit, reverse = struct.unpack("<QQQQI?", body[:37])
    assert (team, end - begin, last, limit, reverse) == (123, 7200000, 0, 10, False)
    assert body[37:] == b"\x01" + struct.pack("<Q", 100)
    adapter.nim.request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["account", "group", "offline"])
async def test_history_inspection_rejects_other_scope_before_query(
    history_transport, case
):
    adapter, _native, _encode = history_transport
    group = "1459282"
    if case == "account":
        adapter.account = "31846829"
    elif case == "group":
        group = "1459534"
    else:
        adapter.connection_state = "offline"
    with pytest.raises(ApiError):
        await fixture.inspect_test_videos(adapter, group, Mock())
    adapter.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["rejected", "trailing", "limit", "truncated", "revoked"]
)
async def test_history_inspection_rejects_bad_response_or_changed_authority(
    history_transport, case
):
    adapter, _native, _encode = history_transport
    if case == "rejected":
        adapter.nim.request.return_value = (403, b"")
    elif case == "trailing":
        adapter.nim.request.return_value = (200, b"\x00\x00")
    elif case == "limit":
        adapter.nim.request.return_value = (200, b"\x0b")
    elif case == "truncated":
        adapter.nim.request.return_value = (200, b"\x01")
    else:

        async def revoke(*args):
            adapter.config["_business_active"] = False
            return 200, b"\x00"

        adapter.nim.request.side_effect = revoke
    with pytest.raises(ApiError):
        await fixture.inspect_test_videos(adapter, "1459282", Mock())
    adapter.nim.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_inspection_does_not_return_other_members_or_other_groups(
    history_transport,
):
    adapter, native, encode = history_transport
    native.sender.id = 31846829
    adapter.nim.request.return_value = (200, b"\x01" + encode(native))
    result = await fixture.inspect_test_videos(adapter, "1459282", Mock())
    assert result["videos"] == []
    native.sender.id = 43506527
    native.target.id = 1459534
    adapter.nim.request.return_value = (200, b"\x01" + encode(native))
    result = await fixture.inspect_test_videos(adapter, "1459282", Mock())
    assert result["videos"] == []
