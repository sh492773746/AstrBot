"""Single-video confirmations, isolation, cleanup and uncertain-outcome fences."""

import asyncio
import json
import shutil
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
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.dashboard.responses import ApiError
from tests.test_wangshangliao_native_guide_upload import Response, Session
from tests.test_wangshangliao_native_video import prepared as prepared


@pytest.fixture
def flow(tmp_path, monkeypatch, prepared):
    path = tmp_path / "video-relay-control.sqlite3"
    config = video_relay.RelayConfig(cms_url=video_relay.TMZYZ_CMS_URL, min_free_gb=1)
    with video_relay.database(path) as db:
        db.execute(
            "INSERT INTO relay_configs VALUES(?,?,?,?,?,?)",
            ("43506527", "1459282", 1, config.model_dump_json(), time.time(), "admin"),
        )
    item = SimpleNamespace(vod_id=123, title="短视频精选推荐")
    fetch = AsyncMock(return_value=(item, prepared.data))

    async def prepare(data, directory, *, max_bytes):
        (directory / "video.mp4").write_bytes(prepared.data)
        (directory / "cover.jpg").write_bytes(prepared.thumbnail)
        return prepared

    monkeypatch.setattr(delivery, "fetch_cms_video", fetch)
    monkeypatch.setattr(delivery, "prepare_video", prepare)
    monkeypatch.setattr(
        delivery.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 1024**3)
    )
    asset = native_video.VideoAsset(
        "43506527", "1234567890", prepared.digest, len(prepared.data), 160, 90, 2
    )
    upload, send = AsyncMock(return_value=asset), AsyncMock(return_value="accepted")
    monkeypatch.setattr(delivery, "upload_video", upload)
    monkeypatch.setattr(delivery, "send_video", send)
    return SimpleNamespace(
        path=path,
        adapter=SimpleNamespace(account="43506527", business=object()),
        fetch=fetch,
        upload=upload,
        send=send,
        post=AsyncMock(),
        guard=Mock(),
    )


async def call(flow, operation="preview_video", *, refresh_authority=None, **changes):
    body = {"operation": operation, "revision": "revision", "version": 1}
    body.update(changes)
    return await delivery.delivery_control(
        flow.path,
        flow.adapter,
        "1459282",
        "dashboard:admin",
        "revision",
        body,
        flow.guard,
        flow.post,
        refresh_authority=refresh_authority,
    )


@pytest.mark.asyncio
async def test_stale_native_evidence_is_refreshed_only_once_before_send(
    flow, monkeypatch
):
    now = time.time()
    roster = AsyncMock(return_value={})
    flow.post = AsyncMock()
    service = SimpleNamespace(
        capabilities={("43506527", "1459282"): {"checked_at": now - 61}},
        roster=roster,
    )
    await delivery.refresh_native_evidence(service, flow.adapter, "1459282", flow.guard)
    roster.assert_awaited_once_with(flow.adapter, "1459282")
    service.capabilities[("43506527", "1459282")]["checked_at"] = now
    await delivery.refresh_native_evidence(service, flow.adapter, "1459282", flow.guard)
    roster.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_evidence_refresh_leaves_send_deadline_margin(flow):
    service = SimpleNamespace(
        capabilities={("43506527", "1459282"): {"checked_at": time.time() - 46}},
        roster=AsyncMock(),
    )
    await delivery.refresh_native_evidence(service, flow.adapter, "1459282", flow.guard)
    service.roster.assert_awaited_once()


@pytest.mark.asyncio
async def test_long_preparation_and_upload_refresh_only_outside_send_lock(
    flow, monkeypatch
):
    clock = Mock(return_value=time.time())
    monkeypatch.setattr(delivery.time, "time", clock)
    flow.adapter.send_lock = asyncio.Lock()
    key = ("43506527", "1459282")
    service = SimpleNamespace(capabilities={key: {"checked_at": clock()}})

    async def roster(adapter, group):
        assert not adapter.send_lock.locked()
        assert group == key[1]
        service.capabilities[key]["checked_at"] = clock()

    service.roster = AsyncMock(side_effect=roster)

    async def posting_guard():
        if service.capabilities[key]["checked_at"] < clock() - 60:
            raise OpenError("business_native_permission")

    async def fetch(*args, **kwargs):
        clock.return_value += 70
        return flow.fetch.return_value

    async def upload(*args, **kwargs):
        clock.return_value += 70
        return flow.upload.return_value

    async def send(*args, before_send, **kwargs):
        refreshed = service.roster.await_count
        async with flow.adapter.send_lock:
            await before_send()
        assert service.roster.await_count == refreshed
        return "accepted"

    flow.post.side_effect = posting_guard
    flow.fetch.side_effect = fetch
    flow.upload.side_effect = upload
    flow.send.side_effect = send

    async def refresh():
        await delivery.refresh_native_evidence(
            service, flow.adapter, "1459282", flow.guard
        )

    preview = await call(flow, refresh_authority=refresh)
    result = await call(
        flow, "send_video", token=preview["token"], refresh_authority=refresh
    )
    assert result["state"] == "accepted"
    assert service.roster.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "business_native_permission",
        "business_rules_inactive",
        "business_owner_revoked",
        "proactive_not_authorized",
        "unrecognized-private-error",
    ],
)
@pytest.mark.parametrize("after_upload", [False, True])
async def test_authority_failure_is_not_misreported_as_media_failure(
    flow, reason, after_upload
):
    expected = (
        reason if reason != "unrecognized-private-error" else "video_authority_rejected"
    )
    if after_upload:
        preview = await call(flow)

        async def upload(*args, **kwargs):
            flow.post.side_effect = OpenError(reason)
            return flow.upload.return_value

        flow.upload.side_effect = upload
        result = await call(flow, "send_video", token=preview["token"])
        assert result["state"] == "not_sent" and result["error"] == expected
    else:

        async def fetch(*args, **kwargs):
            flow.post.side_effect = OpenError(reason)
            return flow.fetch.return_value

        flow.fetch.side_effect = fetch
        with pytest.raises(ApiError) as error:
            await call(flow)
        assert error.value.data == {"code": expected}
        flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()
    with video_relay.database(flow.path) as db:
        job = db.execute("SELECT * FROM relay_video_jobs").fetchone()
        assert job["error"] == expected and job["cleanup_state"] == "deleted"
        assert job["state"] == ("not_sent" if after_upload else "failed")


@pytest.mark.asyncio
async def test_refresh_timeout_after_upload_does_not_submit_message(flow):
    preview = await call(flow)
    uploaded = False

    async def upload(*args, **kwargs):
        nonlocal uploaded
        uploaded = True
        return flow.upload.return_value

    async def refresh():
        if uploaded:
            raise TimeoutError

    flow.upload.side_effect = upload
    result = await call(
        flow, "send_video", token=preview["token"], refresh_authority=refresh
    )
    assert result["state"] == "not_sent"
    assert result["error"] == "video_permission_timeout"
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_active_settles_only_unsubmitted_previews(flow):
    body = {"operation": "cancel_active", "revision": "revision", "version": 1}
    with pytest.raises(ApiError, match="没有可取消"):
        await delivery.delivery_control(
            flow.path,
            flow.adapter,
            "1459282",
            "dashboard:admin",
            "revision",
            body,
            flow.guard,
            flow.post,
        )
    preview = await call(flow)
    result = await delivery.delivery_control(
        flow.path,
        flow.adapter,
        "1459282",
        "dashboard:admin",
        "revision",
        body,
        flow.guard,
        flow.post,
    )
    assert result["id"] == preview["id"] and result["state"] == "cancelled"
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()
    with pytest.raises(ApiError):
        await call(flow, "send_video", token=preview["token"])
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_does_not_upload_or_send_and_confirm_is_single_use(flow):
    preview = await call(flow)
    assert preview["state"] == "prepared"
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()
    assert preview["metadata"]["size"] > 0
    result = await call(flow, "send_video", token=preview["token"])
    assert result == {"id": preview["id"], "state": "accepted"}
    flow.upload.assert_awaited_once()
    flow.send.assert_awaited_once()
    assert flow.send.call_args.args[3] == "video-relay/" + preview["id"]
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()
    with pytest.raises(ApiError):
        await call(flow, "send_video", token=preview["token"])
    flow.send.assert_awaited_once()
    with video_relay.database(flow.path) as db:
        row = db.execute("SELECT * FROM relay_video_jobs").fetchone()
        assert row["state"] == "accepted" and row["asset"] == "1234567890"
        assert row["token_digest"] == ""
        assert preview["token"] not in json.dumps(dict(row))


@pytest.mark.asyncio
async def test_cancel_discards_only_own_preview_without_native_actions(flow):
    preview = await call(flow)
    result = await call(flow, "cancel_video", token=preview["token"])
    assert result["state"] == "cancelled"
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_neutral_fixture_uses_normal_confirmation_and_dedup(
    flow, monkeypatch
):
    generate = AsyncMock(
        return_value=(
            SimpleNamespace(vod_id=3, title="WSL_TEST_VIDEO_V3_5_SECONDS"),
            b"mp4",
        )
    )
    monkeypatch.setattr(delivery, "neutral_video", generate)
    with video_relay.database(flow.path) as db:
        db.execute(
            "UPDATE relay_configs SET config=?",
            (video_relay.RelayConfig().model_dump_json(),),
        )
    preview = await call(flow, "preview_test_video")
    assert preview["metadata"]["source"] == "fixture:neutral-video-v3"
    flow.fetch.assert_not_awaited()
    flow.send.assert_not_awaited()
    generate.assert_awaited_once()
    result = await call(flow, "send_video", token=preview["token"])
    assert result["state"] == "accepted"
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()
    second = await call(flow, "preview_test_video")
    with pytest.raises(ApiError, match="阻止重复"):
        await call(flow, "send_video", token=second["token"])
    flow.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account,group", [("other", "1459282"), ("43506527", "1459534")]
)
async def test_neutral_fixture_never_expands_to_other_accounts_or_groups(
    flow, account, group
):
    flow.adapter.account = account
    with pytest.raises(ApiError) as error:
        await delivery.delivery_control(
            flow.path,
            flow.adapter,
            group,
            "dashboard:admin",
            "revision",
            {"operation": "preview_test_video", "revision": "revision", "version": 1},
            flow.guard,
            flow.post,
        )
    assert error.value.status_code == 403
    flow.fetch.assert_not_awaited()
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="local media tools required",
)
async def test_neutral_fixture_is_real_playable_mp4_with_extracted_cover(tmp_path):
    from astrbot.builtin_stars.wangshangliao_moderation.video_relay_fixture import (
        neutral_video,
    )

    item, data = await neutral_video(tmp_path)
    assert item.vod_id == 3 and item.title == "WSL_TEST_VIDEO_V3_5_SECONDS"
    prepared = await native_video.prepare_video(data, tmp_path)
    assert (prepared.width, prepared.height, prepared.duration) == (640, 360, 5)
    assert prepared.data[4:8] == b"ftyp"
    assert prepared.thumbnail[:3] == b"\xff\xd8\xff"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["actor", "group", "account", "revision", "version", "expired"]
)
async def test_preview_cannot_be_reused_outside_exact_scope(flow, mismatch):
    preview = await call(flow)
    with video_relay.database(flow.path) as db:
        if mismatch == "expired":
            db.execute("UPDATE relay_video_jobs SET expires=?", (time.time() - 1,))
        else:
            field = {
                "actor": "actor",
                "group": "group_id",
                "account": "account",
                "revision": "revision",
                "version": "version",
            }[mismatch]
            value = 999 if mismatch == "version" else "different"
            db.execute(f"UPDATE relay_video_jobs SET {field}=?", (value,))
    with pytest.raises(ApiError):
        await call(flow, "send_video", token=preview["token"])
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_upload_is_audited_without_send_or_retry_or_cached_video(flow):
    preview = await call(flow)
    error = ProtocolError("video_upload_unknown")
    error.submitted = True
    flow.upload.side_effect = error
    result = await call(flow, "send_video", token=preview["token"])
    assert result["state"] == "unknown"
    flow.send.assert_not_awaited()
    with pytest.raises(ApiError):
        await call(flow, "send_video", token=preview["token"])
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()
    flow.upload.assert_awaited_once()
    with video_relay.database(flow.path) as db:
        assert (
            db.execute("SELECT state FROM relay_video_jobs").fetchone()[0] == "unknown"
        )


@pytest.mark.asyncio
async def test_failed_download_cleans_files_and_sanitizes_source_urls(flow):
    flow.fetch.side_effect = RuntimeError(
        "https://cdn.invalid/?private-signature=secret"
    )
    with pytest.raises(ApiError):
        await call(flow)
    with video_relay.database(flow.path) as db:
        row = dict(db.execute("SELECT * FROM relay_video_jobs").fetchone())
    assert row["state"] == "failed" and row["error"] == "video_operation_failed"
    assert "private-signature" not in json.dumps(row)
    assert not list((flow.path.parent / "video-relay-cache").iterdir())
    flow.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_cached_file_is_never_uploaded(flow):
    preview = await call(flow)
    directory = flow.path.parent / "video-relay-cache" / preview["id"]
    (directory / "video.mp4").write_bytes(b"replacement")
    with pytest.raises(ApiError):
        await call(flow, "send_video", token=preview["token"])
    flow.upload.assert_not_awaited()
    assert not directory.exists()


@pytest.mark.asyncio
async def test_one_prepared_video_per_instance_bounds_concurrency(flow):
    await call(flow)
    with pytest.raises(ApiError, match="已有视频"):
        await call(flow)
    flow.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_expiration_cleans_preview_and_quarantines_interrupted_submission(flow):
    preview = await call(flow)
    with video_relay.database(flow.path) as db:
        db.execute(
            "UPDATE relay_video_jobs SET state='uploading',updated=?",
            (time.time() - 601,),
        )
    await delivery.expire_previews(flow.path)
    with video_relay.database(flow.path) as db:
        row = dict(db.execute("SELECT * FROM relay_video_jobs").fetchone())
    assert row["state"] == "unknown" and row["token_digest"] == ""
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()
    flow.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_accepted_source_is_skipped_without_cross_group_deduplication(flow):
    preview = await call(flow)
    await call(flow, "send_video", token=preview["token"])
    await call(flow)
    assert flow.fetch.call_args.args[1] == {123}
    with video_relay.database(flow.path) as db:
        publication = dict(db.execute("SELECT * FROM relay_publications").fetchone())
        assert publication["state"] == "accepted"
        db.execute("DELETE FROM relay_video_jobs")
    preview = await call(flow)
    with pytest.raises(ApiError, match="重复发送"):
        await call(flow, "send_video", token=preview["token"])
    flow.upload.assert_awaited_once()
    flow.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_changed_source_id_same_video_is_not_uploaded_again(flow):
    preview = await call(flow)
    await call(flow, "send_video", token=preview["token"])
    flow.fetch.return_value = (
        SimpleNamespace(vod_id=456, title="新闻视频"),
        flow.fetch.return_value[1],
    )
    preview = await call(flow)
    with pytest.raises(ApiError, match="重复发送"):
        await call(flow, "send_video", token=preview["token"])
    flow.upload.assert_awaited_once()
    await call(flow)
    assert flow.fetch.call_args.args[1] == {123, 456}


@pytest.mark.asyncio
async def test_same_numeric_id_in_different_cms_has_independent_source_scope(flow):
    preview = await call(flow)
    await call(flow, "send_video", token=preview["token"])
    config = video_relay.RelayConfig(cms_url="https://news.example.com/api")
    with video_relay.database(flow.path) as db:
        db.execute("UPDATE relay_configs SET config=?", (config.model_dump_json(),))
    await call(flow)
    assert flow.fetch.call_args.args[1] == set()


@pytest.mark.asyncio
async def test_publication_keys_do_not_block_another_destination_group(flow):
    preview = await call(flow)
    await call(flow, "send_video", token=preview["token"])
    with video_relay.database(flow.path) as db:
        db.execute(
            "INSERT INTO relay_configs SELECT account,'99',version,config,updated,actor "
            "FROM relay_configs WHERE group_id='1459282'"
        )
    body = {"operation": "preview_video", "revision": "revision", "version": 1}
    preview = await delivery.delivery_control(
        flow.path,
        flow.adapter,
        "99",
        "dashboard:admin",
        "revision",
        body,
        flow.guard,
        flow.post,
    )
    assert flow.fetch.call_args.args[1] == set()
    result = await delivery.delivery_control(
        flow.path,
        flow.adapter,
        "99",
        "dashboard:admin",
        "revision",
        {**body, "operation": "send_video", "token": preview["token"]},
        flow.guard,
        flow.post,
    )
    assert result["state"] == "accepted"
    assert flow.send.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_failure_is_visible_and_retried_without_resending(
    flow, monkeypatch
):
    preview = await call(flow)
    remove = delivery.shutil.rmtree
    monkeypatch.setattr(delivery.shutil, "rmtree", Mock(side_effect=OSError))
    await call(flow, "send_video", token=preview["token"])
    with video_relay.database(flow.path) as db:
        assert (
            db.execute("SELECT cleanup_state FROM relay_video_jobs").fetchone()[0]
            == "pending"
        )
    monkeypatch.setattr(delivery.shutil, "rmtree", remove)
    await delivery.expire_previews(flow.path)
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()
    with video_relay.database(flow.path) as db:
        assert (
            db.execute("SELECT cleanup_state FROM relay_video_jobs").fetchone()[0]
            == "deleted"
        )
    flow.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_upload_records_unknown_and_reclaims_local_files(flow):
    preview = await call(flow)
    error = asyncio.CancelledError()
    error.submitted = True
    flow.upload.side_effect = error
    with pytest.raises(asyncio.CancelledError):
        await call(flow, "send_video", token=preview["token"])
    with video_relay.database(flow.path) as db:
        assert (
            db.execute("SELECT state FROM relay_video_jobs").fetchone()[0] == "unknown"
        )
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()


@pytest.mark.asyncio
async def test_late_upload_cannot_publish_a_quarantined_job(flow):
    preview = await call(flow)
    asset = flow.upload.return_value

    async def late_upload(*args, **kwargs):
        with video_relay.database(flow.path) as db:
            db.execute("UPDATE relay_video_jobs SET updated=?", (time.time() - 601,))
        await delivery.expire_previews(flow.path)
        return asset

    flow.upload.side_effect = late_upload
    result = await call(flow, "send_video", token=preview["token"])
    assert result["state"] == "not_sent"
    assert result["error"] == "video_operation_expired"
    flow.send.assert_not_awaited()
    with pytest.raises(ApiError):
        await call(flow, "send_video", token=preview["token"])
    assert not (flow.path.parent / "video-relay-cache" / preview["id"]).exists()


@pytest.mark.asyncio
async def test_cancel_is_allowed_after_configuration_change_or_posting_revocation(flow):
    preview = await call(flow)
    with video_relay.database(flow.path) as db:
        db.execute("UPDATE relay_configs SET version=2")
    flow.post.side_effect = ProtocolError("fixture_paused")
    assert (await call(flow, "cancel_video", token=preview["token"]))[
        "state"
    ] == "cancelled"
    flow.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_change_after_upload_prevents_native_submission(flow):
    preview = await call(flow)
    asset = flow.upload.return_value

    async def changed(*args, **kwargs):
        with video_relay.database(flow.path) as db:
            db.execute("UPDATE relay_configs SET version=2")
        return asset

    flow.upload.side_effect = changed
    result = await call(flow, "send_video", token=preview["token"])
    assert result["state"] == "not_sent"
    assert result["error"] == "video_configuration_changed"
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_range", [False, True])
@pytest.mark.parametrize("byte_shifted", [False, True])
async def test_cms_download_checks_all_metadata_and_exact_byte_ranges(
    monkeypatch, prepared, invalid_range, byte_shifted
):
    resource, manifest = "a" * 32, "b" * 32
    cdn = "https://cdn.eos-dongguan-6.cmecloud.cn/video?signature=fixture"
    detail = {
        "code": 1,
        "list": [
            {
                "vod_id": 123,
                "vod_name": "短视频精选推荐",
                "type_id": 71,
                "type_name": "短视频",
                "vod_play_from": "bkplayer",
                "vod_play_url": "HD$" + resource,
                "vod_pic": f"https://img.tmzyz.com/vod/{resource}.jpg",
                "vod_tag": "精选,短视频",
                "vod_class": "精选",
            }
        ],
    }
    listing = {"code": 1, "list": detail["list"]}
    original = prepared.data + bytes(range(256))
    length = len(original) - 12
    playlist = "\n".join(
        [
            "#EXTM3U",
            f'#EXT-X-MAP:URI="{cdn}",BYTERANGE="12@0"',
            "#EXTINF:2.0,",
            f"#EXT-X-BYTERANGE:{length}@12",
            cdn,
            "#EXT-X-ENDLIST",
        ]
    )
    media = (
        original.translate(bytes((value + 1) & 0xFF for value in range(256)))
        if byte_shifted
        else original
    )
    first = Response(media[:12], status=206)
    first.headers = {"Content-Range": f"bytes 0-11/{len(original)}"}
    second = Response(media[12:], status=206)
    second.headers = {
        "Content-Range": f"bytes {'0' if invalid_range else '12'}-{len(original) - 1}/{len(original)}"
    }
    session = Session(
        [
            Response(json.dumps(listing).encode()),
            Response(json.dumps(detail).encode()),
            Response(f"api.php?mpd={manifest}".encode()),
            Response(playlist.encode()),
            first,
            second,
        ]
    )
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    if invalid_range:
        with pytest.raises(ProtocolError, match="video_range_invalid"):
            await delivery.fetch_cms_video(
                video_relay.RelayConfig(cms_url=video_relay.TMZYZ_CMS_URL),
                set(),
                Mock(),
            )
    else:
        item, data = await delivery.fetch_cms_video(
            video_relay.RelayConfig(cms_url=video_relay.TMZYZ_CMS_URL), set(), Mock()
        )
        assert item.vod_id == 123 and data == original
    assert session.calls[-2][2]["headers"] == {"Range": "bytes=0-11"}
    assert session.calls[-1][2]["headers"] == {"Range": f"bytes=12-{len(original) - 1}"}
    assert all(kwargs.get("allow_redirects") is False for _, _, kwargs in session.calls)
    assert not any(
        "token" in kwargs.get("headers", {}) for _, _, kwargs in session.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fd00::1"]
)
async def test_source_dns_cannot_rebind_to_private_or_metadata_addresses(
    monkeypatch, address
):
    monkeypatch.setattr(
        delivery.aiohttp.resolver.ThreadedResolver,
        "resolve",
        AsyncMock(return_value=[{"host": address}]),
    )
    resolver = delivery.PublicResolver()
    try:
        with pytest.raises(OSError, match="video_source_address_rejected"):
            await resolver.resolve("cdn.eos-dongguan-6.cmecloud.cn", 443)
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_operator_category_overrides_only_the_single_preview(
    monkeypatch, prepared
):
    item = {
        "vod_id": 89,
        "vod_name": "分类视频",
        "type_id": 41,
        "type_name": "热点黑料",
        "vod_play_from": "direct",
        "vod_play_url": "高清$https://cdn.news.example.com/89.mp4",
    }
    session = Session(
        [
            Response(json.dumps({"code": 1, "list": [item]}).encode()),
            Response(json.dumps({"code": 1, "list": [item]}).encode()),
            Response(prepared.data),
        ]
    )
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    config = video_relay.RelayConfig(
        cms_url="https://news.example.com/api", category_id=22
    )
    result, data = await delivery.fetch_cms_video(config, set(), Mock(), category=41)
    assert result.vod_id == 89 and data == prepared.data
    assert result.category_id == 41 and result.category_name == "热点黑料"
    assert session.calls[0][1] == "https://news.example.com/api?ac=list&t=41"
    assert session.calls[1][1] == "https://news.example.com/api?ac=detail&ids=89"
    assert config.category_id == 22


@pytest.mark.asyncio
@pytest.mark.parametrize("byte_shifted", [False, True])
async def test_direct_news_mp4_download_needs_no_title_or_tag_allowlist(
    monkeypatch, prepared, byte_shifted
):
    item = {
        "vod_id": 89,
        "vod_name": "涉黄案件新闻通报",
        "type_id": 22,
        "vod_tag": "新闻,公告",
        "vod_play_from": "direct",
        "vod_play_url": "高清$https://cdn.news.example.com/89.mp4",
    }
    media = (
        prepared.data.translate(bytes((value + 1) & 0xFF for value in range(256)))
        if byte_shifted
        else prepared.data
    )
    session = Session(
        [
            Response(json.dumps({"code": 1, "list": [item]}).encode()),
            Response(json.dumps({"code": 1, "list": [item]}).encode()),
            Response(media),
        ]
    )
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    config = video_relay.RelayConfig(
        cms_url="https://news.example.com/api", category_id=22
    )
    result, data = await delivery.fetch_cms_video(config, set(), Mock())
    assert result.vod_id == 89 and data == media
    assert session.calls[0][1] == "https://news.example.com/api?ac=list&t=22"
    assert session.calls[1][1] == "https://news.example.com/api?ac=detail&ids=89"
    assert len(session.calls) == 3


def test_legacy_accepted_records_migrate_to_persistent_dedup_without_video_bytes(
    tmp_path,
):
    path = tmp_path / "relay.sqlite3"
    with video_relay.database(path) as db:
        db.execute("DELETE FROM relay_metadata WHERE key='publication_migration'")
        db.execute(
            "INSERT INTO relay_video_jobs(id,account,group_id,actor,revision,version,"
            "token_digest,expires,state,metadata,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "a" * 32,
                "1",
                "5",
                "old-admin",
                "old-revision",
                1,
                "",
                0,
                "accepted",
                json.dumps({"vod_id": 123, "digest": "c" * 64}),
                1,
                2,
            ),
        )
    with video_relay.database(path) as db:
        row = dict(db.execute("SELECT * FROM relay_publications").fetchone())
        assert row["source"] == video_relay.TMZYZ_CMS_URL
        assert row["source_id"] == 123 and row["digest"] == "c" * 64
        assert row["state"] == "accepted"
        db.execute("DELETE FROM relay_video_jobs")
    with video_relay.database(path) as db:
        assert db.execute("SELECT COUNT(*) FROM relay_publications").fetchone()[0] == 1
