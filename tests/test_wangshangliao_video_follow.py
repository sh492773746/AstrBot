"""Same-account video fan-out without re-selection, upload or historical replay."""

import json
import time

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import video_relay
from astrbot.builtin_stars.wangshangliao_moderation import (
    video_relay_delivery as delivery,
)
from astrbot.dashboard.responses import ApiError
from tests.test_wangshangliao_native_video import prepared as prepared
from tests.test_wangshangliao_video_delivery import flow as flow
from tests.test_wangshangliao_video_runtime import worker as worker

ACCOUNT, SOURCE, TARGET = "43506527", "1459282", "1459283"


@pytest_asyncio.fixture
async def following(worker):
    service, flow, row = worker
    flow.adapter.config["enabled_groups"].append(TARGET)
    config = video_relay.saved_config(row["config"]).model_dump()
    config["follow_group_id"] = SOURCE
    preview = await video_relay.control(
        flow.path,
        ACCOUNT,
        TARGET,
        "admin",
        "r",
        {"operation": "preview", "version": 0, "revision": "r", "config": config},
        lambda: None,
    )
    await video_relay.control(
        flow.path,
        ACCOUNT,
        TARGET,
        "admin",
        "r",
        {
            "operation": "commit",
            "version": 0,
            "revision": "r",
            "token": preview["token"],
        },
        lambda: None,
    )
    yield service, flow, row
    await service.close()


async def source_round(service):
    """Complete one due source job before the follower's next scheduling tick.

    Args:
        service: Isolated runtime fixture.
    """
    await service.tick()
    await service.tasks["fixture"]


@pytest.mark.asyncio
async def test_one_selection_and_upload_produce_two_identical_native_assets(following):
    service, flow, _ = following
    await source_round(service)
    flow.fetch.assert_awaited_once()
    flow.upload.assert_awaited_once()
    await service.tick()
    await service.tasks["fixture"]
    flow.fetch.assert_awaited_once()
    flow.upload.assert_awaited_once()
    assert flow.send.await_count == 2
    assert [call.args[1] for call in flow.send.await_args_list] == [SOURCE, TARGET]
    assert flow.send.await_args_list[0].args[2] == flow.send.await_args_list[1].args[2]
    with video_relay.database(flow.path) as db:
        publications = db.execute(
            "SELECT source_id,digest,state FROM relay_publications"
        ).fetchall()
        assert len(publications) == 2
        assert tuple(publications[0]) == tuple(publications[1])
        assert publications[0]["state"] == "accepted"
        assert (
            db.execute(
                "SELECT count(*) FROM relay_video_jobs WHERE cleanup_state='deleted'"
            ).fetchone()[0]
            == 2
        )
    assert not list((flow.path.parent / "video-relay-cache").iterdir())
    await service.tick()
    assert flow.send.await_count == 2


@pytest.mark.asyncio
async def test_following_waits_for_a_new_source_round_without_claiming_or_polling_cms(
    following,
):
    service, flow, _ = following
    with video_relay.database(flow.path) as db:
        db.execute(
            "UPDATE relay_schedules SET next_at=? WHERE group_id=?",
            (time.time() + 300, SOURCE),
        )
    for _ in range(3):
        await service.tick()
    assert not service.tasks
    flow.fetch.assert_not_awaited()
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_source_never_fans_out_or_retries(following):
    service, flow, _ = following
    flow.send.return_value = "unknown"
    await source_round(service)
    for _ in range(3):
        await service.tick()
    flow.send.assert_awaited_once()
    with video_relay.database(flow.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM relay_publications WHERE group_id=?", (TARGET,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_unknown_follower_is_durable_and_never_resent(following):
    service, flow, _ = following
    await source_round(service)
    flow.send.return_value = "unknown"
    await service.tick()
    await service.tasks["fixture"]
    for _ in range(3):
        await service.tick()
    assert flow.send.await_count == 2
    with video_relay.database(flow.path) as db:
        assert (
            db.execute(
                "SELECT state FROM relay_schedules WHERE group_id=?", (TARGET,)
            ).fetchone()[0]
            == "blocked"
        )
        assert (
            db.execute(
                "SELECT state FROM relay_publications WHERE group_id=?", (TARGET,)
            ).fetchone()[0]
            == "unknown"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["accepted", "unknown", "pending", "duplicate"])
async def test_per_group_duplicate_fence_survives_following(following, state):
    service, flow, _ = following
    await source_round(service)
    with video_relay.database(flow.path) as db:
        db.execute(
            "INSERT INTO relay_publications SELECT account,?,source,source_id,digest,'old-target',?,created,updated "
            "FROM relay_publications WHERE group_id=?",
            (TARGET, state, SOURCE),
        )
    await service.tick()
    flow.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_skips_followers_previous_ids_before_selecting(following):
    service, flow, _ = following
    with video_relay.database(flow.path) as db:
        db.execute(
            "INSERT INTO relay_publications VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ACCOUNT,
                TARGET,
                video_relay.TMZYZ_CMS_URL,
                998,
                "a" * 64,
                "old",
                "accepted",
                1,
                1,
            ),
        )
    await source_round(service)
    assert 998 in flow.fetch.await_args.args[1]


@pytest.mark.asyncio
async def test_existing_publications_are_not_replayed_when_following_is_enabled(worker):
    service, flow, row = worker
    await source_round(service)
    flow.adapter.config["enabled_groups"].append(TARGET)
    config = {**json.loads(row["config"]), "follow_group_id": SOURCE}
    preview = await video_relay.control(
        flow.path,
        ACCOUNT,
        TARGET,
        "admin",
        "r",
        {"operation": "preview", "version": 0, "revision": "r", "config": config},
        lambda: None,
    )
    await video_relay.control(
        flow.path,
        ACCOUNT,
        TARGET,
        "admin",
        "r",
        {
            "operation": "commit",
            "version": 0,
            "revision": "r",
            "token": preview["token"],
        },
        lambda: None,
    )
    await service.tick()
    flow.send.assert_awaited_once()
    await service.close()


@pytest.mark.asyncio
async def test_source_revoked_between_preview_and_send_cannot_deliver(following):
    service, flow, _ = following
    await source_round(service)
    preview = await delivery.delivery_control(
        flow.path,
        flow.adapter,
        TARGET,
        "admin",
        "r",
        {"operation": "preview_video", "version": 1, "revision": "r"},
        flow.guard,
        flow.post,
    )
    with video_relay.database(flow.path) as db:
        config = json.loads(
            db.execute(
                "SELECT config FROM relay_configs WHERE group_id=?", (SOURCE,)
            ).fetchone()[0]
        )
        config["enabled"] = False
        db.execute(
            "UPDATE relay_configs SET config=? WHERE group_id=?",
            (json.dumps(config), SOURCE),
        )
    with pytest.raises(ApiError):
        await delivery.delivery_control(
            flow.path,
            flow.adapter,
            TARGET,
            "admin",
            "r",
            {
                "operation": "send_video",
                "version": 1,
                "revision": "r",
                "token": preview["token"],
            },
            flow.guard,
            flow.post,
        )
    flow.send.assert_awaited_once()
    flow.upload.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"follow_group_id": TARGET},
        {"follow_group_id": "999999"},
        {"category_id": 99},
        {"interval_minutes": 1},
        {"cms_url": "https://another.example.com/api"},
    ],
)
async def test_invalid_follow_source_cannot_be_saved(worker, change):
    service, flow, row = worker
    config = {**json.loads(row["config"]), "follow_group_id": SOURCE, **change}
    with pytest.raises(ApiError, match="同步来源"):
        await video_relay.control(
            flow.path,
            ACCOUNT,
            TARGET,
            "admin",
            "r",
            {"operation": "preview", "version": 0, "revision": "r", "config": config},
            lambda: None,
        )
    await service.close()


@pytest.mark.asyncio
async def test_following_cannot_turn_its_source_into_a_recursive_chain(following):
    service, flow, row = following
    config = {**json.loads(row["config"]), "follow_group_id": TARGET}
    with pytest.raises(ApiError, match="同步来源"):
        await video_relay.control(
            flow.path,
            ACCOUNT,
            SOURCE,
            "admin",
            "r",
            {"operation": "preview", "version": 1, "revision": "r", "config": config},
            lambda: None,
        )


@pytest.mark.asyncio
async def test_following_read_disables_independent_manual_selection(following):
    _, flow, _ = following
    result = await video_relay.control(
        flow.path, ACCOUNT, TARGET, "admin", "r", {}, lambda: None
    )
    assert result["config"]["follow_group_id"] == SOURCE
    assert result["runtime"]["can_preview_video"] is False


@pytest.mark.asyncio
async def test_following_restart_does_not_lose_pending_accepted_round(following):
    service, flow, _ = following
    await source_round(service)
    await service.close()
    service.recovered.clear()
    service.active = True
    await service.tick()
    await service.tasks["fixture"]
    assert flow.send.await_count == 2
    flow.fetch.assert_awaited_once()
    flow.upload.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_source_asset_does_not_fallback_to_independent_selection(
    following,
):
    service, flow, _ = following
    await source_round(service)
    with video_relay.database(flow.path) as db:
        db.execute("DELETE FROM relay_video_jobs WHERE group_id=?", (SOURCE,))
    await service.tick()
    await service.tasks["fixture"]
    flow.fetch.assert_awaited_once()
    flow.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_cannot_publish_digest_already_present_in_follower(
    following, prepared
):
    service, flow, _ = following
    with video_relay.database(flow.path) as db:
        db.execute(
            "INSERT INTO relay_publications VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ACCOUNT,
                TARGET,
                video_relay.TMZYZ_CMS_URL,
                998,
                prepared.digest,
                "old",
                "accepted",
                1,
                1,
            ),
        )
    await source_round(service)
    flow.fetch.assert_awaited_once()
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"digest": "a" * 64},
        {"vod_id": 999},
        {"source": "https://another.example.com/api"},
        {"size": 113 * 1024**2},
    ],
)
async def test_inconsistent_source_metadata_blocks_reuse(following, change):
    service, flow, _ = following
    await source_round(service)
    with video_relay.database(flow.path) as db:
        metadata = json.loads(
            db.execute(
                "SELECT metadata FROM relay_video_jobs WHERE group_id=?", (SOURCE,)
            ).fetchone()[0]
        )
        metadata.update(change)
        db.execute(
            "UPDATE relay_video_jobs SET metadata=? WHERE group_id=?",
            (json.dumps(metadata), SOURCE),
        )
    await service.tick()
    await service.tasks["fixture"]
    flow.send.assert_awaited_once()
    flow.upload.assert_awaited_once()
    flow.fetch.assert_awaited_once()
    with video_relay.database(flow.path) as db:
        assert (
            db.execute(
                "SELECT state FROM relay_schedules WHERE group_id=?", (TARGET,)
            ).fetchone()[0]
            == "blocked"
        )


@pytest.mark.asyncio
async def test_readvertising_same_follow_binding_preserves_start_and_pending_round(
    following,
):
    service, flow, _ = following
    with video_relay.database(flow.path) as db:
        start = db.execute("SELECT since FROM relay_follow_starts").fetchone()[0]
    await source_round(service)
    state = await video_relay.control(
        flow.path, ACCOUNT, TARGET, "admin", "r", {}, lambda: None
    )
    preview = await video_relay.control(
        flow.path,
        ACCOUNT,
        TARGET,
        "admin",
        "r",
        {
            "operation": "preview",
            "version": 1,
            "revision": "r",
            "config": state["config"],
        },
        lambda: None,
    )
    await video_relay.control(
        flow.path,
        ACCOUNT,
        TARGET,
        "admin",
        "r",
        {
            "operation": "commit",
            "version": 1,
            "revision": "r",
            "token": preview["token"],
        },
        lambda: None,
    )
    with video_relay.database(flow.path) as db:
        assert (
            db.execute("SELECT since FROM relay_follow_starts").fetchone()[0] == start
        )
    await service.tick()
    await service.tasks["fixture"]
    assert flow.send.await_count == 2


@pytest.mark.asyncio
async def test_disable_and_reenable_drops_old_rounds_instead_of_backfilling(following):
    service, flow, _ = following
    await source_round(service)
    for version, enabled in [(1, False), (2, True)]:
        state = await video_relay.control(
            flow.path, ACCOUNT, TARGET, "admin", "r", {}, lambda: None
        )
        preview = await video_relay.control(
            flow.path,
            ACCOUNT,
            TARGET,
            "admin",
            "r",
            {
                "operation": "preview",
                "version": version,
                "revision": "r",
                "config": {**state["config"], "enabled": enabled},
            },
            lambda: None,
        )
        await video_relay.control(
            flow.path,
            ACCOUNT,
            TARGET,
            "admin",
            "r",
            {
                "operation": "commit",
                "version": version,
                "revision": "r",
                "token": preview["token"],
            },
            lambda: None,
        )
    await service.tick()
    flow.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_target_scope_revocation_blocks_follower_without_selecting_another_video(
    following,
):
    service, flow, _ = following
    await source_round(service)
    flow.adapter.config["enabled_groups"].remove(TARGET)
    await service.tick()
    await service.tasks["fixture"]
    flow.send.assert_awaited_once()
    flow.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_cross_account_source_cannot_be_followed(worker):
    service, flow, row = worker
    with video_relay.database(flow.path) as db:
        db.execute("UPDATE relay_configs SET account='other'")
    with pytest.raises(ApiError, match="同步来源"):
        await video_relay.control(
            flow.path,
            ACCOUNT,
            TARGET,
            "admin",
            "r",
            {
                "operation": "preview",
                "version": 0,
                "revision": "r",
                "config": {**json.loads(row["config"]), "follow_group_id": SOURCE},
            },
            lambda: None,
        )
    await service.close()


@pytest.mark.asyncio
async def test_five_minute_round_follows_immediately_without_a_second_five_minute_wait(
    following,
):
    service, flow, _ = following
    with video_relay.database(flow.path) as db:
        for group in (SOURCE, TARGET):
            config = json.loads(
                db.execute(
                    "SELECT config FROM relay_configs WHERE group_id=?", (group,)
                ).fetchone()[0]
            )
            config["interval_minutes"] = 5
            db.execute(
                "UPDATE relay_configs SET config=? WHERE group_id=?",
                (json.dumps(config), group),
            )
    await source_round(service)
    now = time.time()
    with video_relay.database(flow.path) as db:
        next_source = db.execute(
            "SELECT next_at FROM relay_schedules WHERE group_id=?", (SOURCE,)
        ).fetchone()[0]
        assert now + 295 <= next_source <= now + 301
    await service.tick()
    await service.tasks["fixture"]
    assert flow.send.await_count == 2
    with video_relay.database(flow.path) as db:
        assert (
            db.execute(
                "SELECT next_at FROM relay_schedules WHERE group_id=?", (SOURCE,)
            ).fetchone()[0]
            == next_source
        )
        assert (
            db.execute(
                "SELECT next_at FROM relay_schedules WHERE group_id=?", (TARGET,)
            ).fetchone()[0]
            <= time.time() + 6
        )
