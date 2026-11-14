"""Clock-controlled CMS scheduling with no real network or group delivery."""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    video_relay,
)
from astrbot.builtin_stars.wangshangliao_moderation import (
    video_relay_runtime as runtime,
)
from tests.test_wangshangliao_native_video import prepared as prepared
from tests.test_wangshangliao_video_delivery import flow as flow


@pytest.fixture
def worker(flow, monkeypatch):
    config = video_relay.RelayConfig(cms_url=video_relay.TMZYZ_CMS_URL, enabled=True)
    with video_relay.database(flow.path) as db:
        db.execute("UPDATE relay_configs SET config=?", (config.model_dump_json(),))
        db.execute(
            "INSERT INTO relay_schedules VALUES(?,?,?,?,?,?)",
            ("43506527", "1459282", 1, time.time() - 1, "scheduled", ""),
        )
        row = dict(
            db.execute(
                "SELECT s.*,c.config FROM relay_schedules s JOIN relay_configs c "
                "ON s.account=c.account AND s.group_id=c.group_id"
            ).fetchone()
        )
    adapter = flow.adapter
    adapter.meta = lambda: SimpleNamespace(id="fixture", name="wangshangliao")
    adapter.connection_state = "online"
    adapter.config = {
        "id": "fixture",
        "type": "wangshangliao",
        "account_id": adapter.account,
        "enable": True,
        "enabled_groups": ["1459282"],
    }
    plugin = SimpleNamespace(
        dashboard_page=SimpleNamespace(active=True),
        context=SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[adapter])
        ),
        open_service=SimpleNamespace(
            active=True,
            rule_replies=SimpleNamespace(
                read=Mock(return_value={"version": 1}),
                send_guard=Mock(),
            ),
        ),
    )
    monkeypatch.setattr(runtime, "account_guard", lambda _: Mock())
    path = flow.path
    monkeypatch.setattr(runtime, "instance_dir", lambda _: path.parent)
    worker = runtime.VideoRelayRuntime(plugin)
    worker.active = True
    return worker, flow, row


@pytest.mark.asyncio
async def test_due_schedule_sends_once_then_waits_full_interval(worker):
    service, flow, row = worker
    await service.tick()
    await service.tasks["fixture"]
    flow.send.assert_awaited_once()
    with video_relay.database(flow.path) as db:
        schedule = dict(db.execute("SELECT * FROM relay_schedules").fetchone())
        assert schedule["state"] == "scheduled"
        assert schedule["next_at"] >= time.time() + 1190
        assert (
            db.execute("SELECT cleanup_state FROM relay_video_jobs").fetchone()[0]
            == "deleted"
        )
    await service.tick()
    flow.send.assert_awaited_once()
    await service.close()


@pytest.mark.asyncio
async def test_unknown_receipt_blocks_schedule_without_retry(worker):
    service, flow, row = worker
    flow.send.return_value = "unknown"
    await service.tick()
    await service.tasks["fixture"]
    await service.tick()
    await service.tick()
    flow.send.assert_awaited_once()
    with video_relay.database(flow.path) as db:
        assert (
            db.execute("SELECT state FROM relay_schedules").fetchone()[0] == "blocked"
        )
        assert (
            db.execute("SELECT state FROM relay_publications").fetchone()[0]
            == "unknown"
        )
    await service.close()


@pytest.mark.asyncio
async def test_restart_never_restarts_claimed_schedule(worker):
    service, flow, _ = worker
    with video_relay.database(flow.path) as db:
        db.execute("UPDATE relay_schedules SET state='running'")
    await service.tick()
    flow.fetch.assert_not_awaited()
    with video_relay.database(flow.path) as db:
        assert tuple(
            db.execute("SELECT state,error FROM relay_schedules").fetchone()
        ) == (
            "blocked",
            "process_interrupted",
        )
    await service.close()




@pytest.mark.asyncio
async def test_interrupted_schedule_resumes_only_after_recovery_tick(worker):
    service, flow, _ = worker
    with video_relay.database(flow.path) as db:
        db.execute("UPDATE relay_schedules SET state='running'")
    await service.tick()
    flow.fetch.assert_not_awaited()
    with video_relay.database(flow.path) as db:
        assert tuple(
            db.execute("SELECT state,error FROM relay_schedules").fetchone()
        ) == ("blocked", "process_interrupted")
    await service.tick()
    with video_relay.database(flow.path) as db:
        assert db.execute("SELECT state FROM relay_schedules").fetchone()[0] in {
            "scheduled",
            "running",
        }
    await service.close()

@pytest.mark.asyncio
async def test_disable_before_upload_invalidates_inflight_preview(worker):
    service, flow, _ = worker

    async def disable(*args):
        with video_relay.database(flow.path) as db:
            db.execute("UPDATE relay_configs SET version=2")
            db.execute("UPDATE relay_schedules SET state='stopped',version=2")
        return flow.fetch.return_value

    flow.fetch.side_effect = disable
    await service.tick()
    await service.tasks["fixture"]
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()
    assert not list((flow.path.parent / "video-relay-cache").iterdir())
    await service.close()


@pytest.mark.asyncio
async def test_offline_or_disabled_config_does_not_claim_schedule(worker):
    service, flow, _ = worker
    flow.adapter.connection_state = "offline"
    await service.tick()
    assert not service.tasks
    flow.adapter.connection_state = "online"
    with video_relay.database(flow.path) as db:
        row = json.loads(db.execute("SELECT config FROM relay_configs").fetchone()[0])
        row["enabled"] = False
        db.execute("UPDATE relay_configs SET config=?", (json.dumps(row),))
    await service.tick()
    assert not service.tasks
    flow.fetch.assert_not_awaited()
    await service.close()
