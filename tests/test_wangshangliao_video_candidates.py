"""Candidate validation, bounded cooldowns and unchanged publication fences."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    native_video,
    video_relay,
)
from astrbot.builtin_stars.wangshangliao_moderation import (
    video_relay_delivery as delivery,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_native_guide_upload import Response, Session
from tests.test_wangshangliao_native_video import prepared as prepared
from tests.test_wangshangliao_video_delivery import call
from tests.test_wangshangliao_video_delivery import flow as flow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["video_duration_limit", "video_format_unsupported", "video_media_invalid"],
)
async def test_bad_local_format_advances_to_next_candidate_in_same_round(
    monkeypatch, prepared, reason
):
    records = [
        {
            "vod_id": value,
            "vod_name": "neutral fixture",
            "vod_play_url": f"media$https://cdn.example.com/{value}.mp4",
        }
        for value in (11, 12)
    ]
    session = Session(
        [
            Response(json.dumps({"code": 1, "list": records}).encode()),
            Response(json.dumps({"code": 1, "list": [records[0]]}).encode()),
            Response(prepared.data),
            Response(json.dumps({"code": 1, "list": [records[1]]}).encode()),
            Response(prepared.data),
        ]
    )
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    validate = AsyncMock(side_effect=[ProtocolError(reason), None])
    rejected = AsyncMock()
    item, data = await delivery.fetch_cms_video(
        video_relay.RelayConfig(cms_url="https://cms.example.com/api"),
        set(),
        Mock(),
        validate_candidate=validate,
        reject_candidate=rejected,
    )
    assert item.vod_id == 12 and data == prepared.data
    assert validate.await_count == 2
    rejected.assert_awaited_once_with(11, reason)
    assert len(session.calls) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["video_duration_limit", "video_format_unsupported"])
async def test_all_candidates_rejected_preserve_actionable_reason(
    monkeypatch, prepared, reason
):
    record = {
        "vod_id": 11,
        "vod_name": "neutral fixture",
        "vod_play_url": "media$https://cdn.example.com/11.mp4",
    }
    payload = json.dumps({"code": 1, "list": [record]}).encode()
    session = Session([Response(payload), Response(payload), Response(prepared.data)])
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    with pytest.raises(ProtocolError, match="^" + reason + "$"):
        await delivery.fetch_cms_video(
            video_relay.RelayConfig(cms_url="https://cms.example.com/api"),
            set(),
            Mock(),
            validate_candidate=AsyncMock(side_effect=ProtocolError(reason)),
        )


@pytest.mark.asyncio
async def test_manifest_failure_is_reported_and_cooled_without_media_download(
    monkeypatch,
):
    record = {
        "vod_id": 11,
        "vod_name": "neutral fixture",
        "vod_play_from": "bkplayer",
        "vod_play_url": "media$" + "a" * 32,
    }
    payload = json.dumps({"code": 1, "list": [record]}).encode()
    session = Session([Response(payload), Response(payload)])
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    monkeypatch.setattr(
        delivery,
        "_tmzyz_playlist",
        AsyncMock(side_effect=ProtocolError("video_manifest_invalid")),
    )
    rejected = AsyncMock()
    with pytest.raises(ProtocolError, match="^video_manifest_invalid$"):
        await delivery.fetch_cms_video(
            video_relay.RelayConfig(cms_url=video_relay.TMZYZ_CMS_URL),
            set(),
            Mock(),
            reject_candidate=rejected,
        )
    rejected.assert_awaited_once_with(11, "video_manifest_invalid")
    assert len(session.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_revocation_and_cancellation_never_continue_candidate_selection(
    monkeypatch, prepared, cancelled
):
    record = {
        "vod_id": 11,
        "vod_name": "neutral fixture",
        "vod_play_url": "media$https://cdn.example.com/11.mp4",
    }
    payload = json.dumps({"code": 1, "list": [record]}).encode()
    session = Session([Response(payload), Response(payload), Response(prepared.data)])
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    error = asyncio.CancelledError() if cancelled else ProtocolError("fixture_revoked")
    rejected = AsyncMock()
    with pytest.raises(type(error)):
        await delivery.fetch_cms_video(
            video_relay.RelayConfig(cms_url="https://cms.example.com/api"),
            set(),
            Mock(),
            validate_candidate=AsyncMock(side_effect=error),
            reject_candidate=rejected,
        )
    rejected.assert_not_awaited()


@pytest.mark.asyncio
async def test_format_failure_cooldown_survives_new_preview_without_publication(
    flow, prepared
):
    async def candidates(*args, **kwargs):
        if 99 not in args[1]:
            await kwargs["reject_candidate"](99, "video_duration_limit")
        await kwargs["validate_candidate"](prepared.data)
        return SimpleNamespace(vod_id=123, title="neutral fixture"), prepared.data

    flow.fetch.side_effect = candidates
    preview = await call(flow)
    with video_relay.database(flow.path) as db:
        row = dict(db.execute("SELECT * FROM relay_candidate_failures").fetchone())
        assert row["source_id"] == 99
        assert row["error"] == "video_duration_limit"
        assert time.time() + 3500 < row["retry_at"] < time.time() + 3610
        assert db.execute("SELECT COUNT(*) FROM relay_publications").fetchone()[0] == 0
        assert set(row) == {
            "account",
            "group_id",
            "source",
            "version",
            "source_id",
            "error",
            "retry_at",
            "updated",
        }
    await call(flow, "cancel_video", token=preview["token"])
    await call(flow)
    assert flow.fetch.call_args.args[1] == {99}
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_cooldown_isolates_account_group_source_version_and_expiry(
    flow,
):
    now = time.time()
    rows = [
        ("43506527", "1459282", video_relay.TMZYZ_CMS_URL, 1, 11, now + 300),
        ("other", "1459282", video_relay.TMZYZ_CMS_URL, 1, 12, now + 300),
        ("43506527", "other", video_relay.TMZYZ_CMS_URL, 1, 13, now + 300),
        ("43506527", "1459282", "https://other.example.com/api", 1, 14, now + 300),
        ("43506527", "1459282", video_relay.TMZYZ_CMS_URL, 2, 15, now + 300),
        ("43506527", "1459282", video_relay.TMZYZ_CMS_URL, 1, 16, now - 1),
    ]
    with video_relay.database(flow.path) as db:
        db.executemany(
            "INSERT INTO relay_candidate_failures VALUES(?,?,?,?,?,?,?,?)",
            [(*row[:5], "video_duration_limit", row[5], now) for row in rows],
        )
    await call(flow)
    assert flow.fetch.call_args.args[1] == {11}
    with video_relay.database(flow.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM relay_candidate_failures WHERE source_id=16"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"max_video_mb": 64}, {"vod_id": 99}])
async def test_targeted_or_larger_limit_preview_does_not_use_or_write_cooldown(
    flow, prepared, override
):
    with video_relay.database(flow.path) as db:
        db.execute(
            "INSERT INTO relay_candidate_failures VALUES(?,?,?,?,?,?,?,?)",
            (
                "43506527",
                "1459282",
                video_relay.TMZYZ_CMS_URL,
                1,
                99,
                "video_size_limit",
                time.time() + 300,
                time.time(),
            ),
        )

    async def candidates(*args, **kwargs):
        assert args[1] == set()
        await kwargs["reject_candidate"](100, "video_size_limit")
        await kwargs["validate_candidate"](prepared.data)
        return SimpleNamespace(vod_id=123, title="neutral fixture"), prepared.data

    flow.fetch.side_effect = candidates
    await call(flow, **override)
    with video_relay.database(flow.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM relay_candidate_failures WHERE source_id=100"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_candidate_cooldown_storage_has_fixed_capacity(flow, prepared):
    now = time.time()
    with video_relay.database(flow.path) as db:
        db.executemany(
            "INSERT INTO relay_candidate_failures VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    "43506527",
                    "other",
                    video_relay.TMZYZ_CMS_URL,
                    1,
                    value,
                    "video_duration_limit",
                    now + 300,
                    now - 1,
                )
                for value in range(1100)
            ],
        )

    async def candidates(*args, **kwargs):
        await kwargs["reject_candidate"](99, "video_duration_limit")
        await kwargs["validate_candidate"](prepared.data)
        return SimpleNamespace(vod_id=123, title="neutral fixture"), prepared.data

    flow.fetch.side_effect = candidates
    await call(flow)
    with video_relay.database(flow.path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM relay_candidate_failures").fetchone()[0]
            == 1024
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM relay_candidate_failures WHERE group_id='1459282'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_duration_limit_is_not_misreported_as_codec_failure(
    monkeypatch, tmp_path, prepared
):
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 640,
                "height": 360,
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "1620.254331"},
    }
    process = AsyncMock(return_value=json.dumps(probe).encode())
    monkeypatch.setattr(native_video, "_media_process", process)
    monkeypatch.setattr(native_video.shutil, "which", lambda name: name)
    with pytest.raises(ProtocolError, match="^video_duration_limit$"):
        await native_video.prepare_video(prepared.data, tmp_path)
    process.assert_awaited_once()
    assert process.call_args.args[0] == "ffprobe"


@pytest.mark.asyncio
async def test_duration_failure_is_actionable_and_cleans_job_files(flow, monkeypatch):
    monkeypatch.setattr(
        delivery,
        "prepare_video",
        AsyncMock(side_effect=ProtocolError("video_duration_limit")),
    )
    from astrbot.dashboard.responses import ApiError

    with pytest.raises(ApiError) as caught:
        await call(flow)
    assert caught.value.data == {"code": "video_duration_limit"}
    with video_relay.database(flow.path) as db:
        assert tuple(
            db.execute(
                "SELECT state,error,cleanup_state FROM relay_video_jobs"
            ).fetchone()
        ) == ("failed", "video_duration_limit", "deleted")
        assert db.execute("SELECT COUNT(*) FROM relay_publications").fetchone()[0] == 0
    assert not list((flow.path.parent / "video-relay-cache").iterdir())
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()
