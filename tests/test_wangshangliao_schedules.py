import asyncio
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import schedules as module
from astrbot.core.platform.sources.wangshangliao import schedule_store as store


def epoch(value):
    return (
        datetime.fromisoformat(value)
        .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        .timestamp()
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "instance_dir", lambda _: tmp_path)
    now = [epoch("2026-09-22T22:00:00")]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    admins = ["2"]
    adapter = SimpleNamespace(
        account="1",
        connection_state="online",
        stopping=asyncio.Event(),
        config={
            "id": "test",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "permissions": {"5": ["mute_all", "unmute_all"]},
            },
        },
    )

    async def execute(operation, action, group, *, scheduled_check):
        async with store.group_lock(adapter, str(group)):
            scheduled_check()
            return {"status": "accepted"}

    adapter.execute_moderation = AsyncMock(side_effect=execute)
    scheduler = module.GroupSchedules(
        SimpleNamespace(get_config=lambda _: {"admins_id": admins})
    )
    event = SimpleNamespace(
        platform=adapter,
        get_sender_id=lambda: "2",
        unified_msg_origin="bot:private:2",
        is_admin=lambda: True,
        is_private_chat=lambda: True,
        message_obj=SimpleNamespace(message_id="preview"),
    )
    return scheduler, adapter, event, now, admins


async def enable(setup):
    scheduler, adapter, event, _, _ = setup
    preview = await scheduler.command(event, "5", "定时禁言", "23:00 08:00")
    token = preview.split()[-1]
    event.message_obj.message_id = "confirm"
    assert "已启用" in await scheduler.command(event, "5", "确认定时", token)
    adapter.execute_moderation.assert_not_awaited()
    return token


def state(adapter):
    with closing(store.database(adapter)) as db:
        return db.execute("SELECT status,error FROM schedules").fetchone()


@pytest.mark.parametrize(
    "clock,action,expected",
    [
        ("2026-09-22T22:00:00", "mute_all", "2026-09-22T23:00:00"),
        ("2026-09-22T23:00:00", "unmute_all", "2026-09-23T08:00:00"),
        ("2026-09-23T08:00:00", "mute_all", "2026-09-23T23:00:00"),
    ],
)
def test_future_boundaries(clock, action, expected):
    assert module.next_boundary("23:00", "08:00", epoch(clock)) == (
        epoch(expected),
        action,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["24:00 08:00", "23:60 08:00", "8:00 23:00", "08:00 08:00", ""]
)
async def test_invalid_times(setup, value):
    scheduler, _, event, _, _ = setup
    assert "用法" in await scheduler.command(event, "5", "定时禁言", value)


@pytest.mark.asyncio
async def test_cycle_idempotency_and_manual_pause(setup):
    scheduler, adapter, event, now, _ = setup
    await enable(setup)
    await scheduler.tick(adapter)
    for point in ["2026-09-22T23:00:00", "2026-09-23T08:00:00"]:
        now[0] = epoch(point)
        await scheduler.tick(adapter)
        await scheduler.tick(adapter)
    assert [c.args[1] for c in adapter.execute_moderation.await_args_list] == [
        "mute_all",
        "unmute_all",
    ]
    store.pause(adapter, "5", "manual_override")
    now[0] = epoch("2026-09-23T23:00:00")
    await scheduler.tick(adapter)
    assert adapter.execute_moderation.await_count == 2
    assert state(adapter) == ("paused", "manual_override")
    assert "尚未启用" in await scheduler.command(event, "5", "恢复定时", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["same_message", "expired", "user", "session", "pause", "delete"]
)
async def test_confirmation_binding(setup, change):
    scheduler, adapter, event, now, _ = setup
    token = (await scheduler.command(event, "5", "定时禁言", "23:00 08:00")).split()[-1]
    if change != "same_message":
        event.message_obj.message_id = "new"
    if change == "expired":
        now[0] += 600
    if change == "user":
        event.get_sender_id = lambda: "3"
        setup[4].append("3")
    if change == "session":
        event.unified_msg_origin = "other"
    if change in {"pause", "delete"}:
        await scheduler.command(
            event, "5", "暂停定时" if change == "pause" else "删除定时", ""
        )
    assert "确认码无效" in await scheduler.command(event, "5", "确认定时", token)
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_and_old_version_confirmation(setup):
    scheduler, _, event, _, _ = setup
    token = await enable(setup)
    assert "确认码无效" in await scheduler.command(event, "5", "确认定时", token)
    token = (await scheduler.command(event, "5", "恢复定时", "")).split()[-1]
    await scheduler.command(event, "5", "暂停定时", "")
    event.message_obj.message_id = "third"
    assert "确认码无效" in await scheduler.command(event, "5", "确认定时", token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["offline", "revoked", "grant", "account", "missed", "restart", "unknown"]
)
async def test_fail_closed(setup, fault):
    scheduler, adapter, _, now, admins = setup
    await enable(setup)
    await scheduler.tick(adapter)
    now[0] = epoch("2026-09-22T23:00:00")
    if fault == "offline":
        adapter.connection_state = "offline"
    elif fault == "revoked":
        admins.clear()
    elif fault == "grant":
        adapter.config["moderation"]["permissions"]["5"] = []
    elif fault == "account":
        adapter.account = "other"
    elif fault in {"missed", "restart"}:
        now[0] = epoch("2026-09-22T23:01:00")
        if fault == "restart":
            scheduler.recovered.clear()
    elif fault == "unknown":

        async def unknown(*args, scheduled_check):
            scheduled_check()
            return {"status": "unknown"}

        adapter.execute_moderation.side_effect = unknown
        now[0] = epoch("2026-09-22T23:00:00")
    await scheduler.tick(adapter)
    assert state(adapter)[0] == "paused"
    await scheduler.tick(adapter)
    assert adapter.execute_moderation.await_count == (1 if fault == "unknown" else 0)
    if fault == "unknown":
        with closing(store.database(adapter)) as db:
            assert (
                db.execute("SELECT status FROM executions").fetchone()[0] == "unknown"
            )


@pytest.mark.asyncio
async def test_unfinished_request_and_audit_retained(setup):
    scheduler, adapter, event, now, _ = setup
    await enable(setup)
    with closing(store.database(adapter)) as db, db:
        db.execute(
            "INSERT INTO executions(operation,account,group_id,planned,action,status) VALUES('pending','1','5',?,'mute_all','claimed')",
            (now[0],),
        )
    await scheduler.tick(adapter)
    assert state(adapter) == ("paused", "restart_review")
    await scheduler.command(event, "5", "删除定时", "")
    with closing(store.database(adapter)) as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM rule_audit").fetchone()[0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_first", [True, False])
async def test_real_wrapper_serializes_manual_and_schedule(setup, manual_first):
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter

    scheduler, adapter, _, now, _ = setup
    await enable(setup)
    await scheduler.tick(adapter)
    entered, release = asyncio.Event(), asyncio.Event()
    operations = []

    async def upstream(operation, action, *args, scheduled_check=None):
        if scheduled_check:
            scheduled_check()
        operations.append(operation)
        entered.set()
        await release.wait()
        return {"status": "accepted"}

    adapter._execute_moderation = upstream
    adapter.execute_moderation = WangshangliaoAdapter.execute_moderation.__get__(
        adapter
    )
    now[0] = epoch("2026-09-22T23:00:00")
    if manual_first:
        first = asyncio.create_task(adapter.execute_moderation("manual", "mute_all", 5))
        await entered.wait()
        second = asyncio.create_task(scheduler.tick(adapter))
    else:
        first = asyncio.create_task(scheduler.tick(adapter))
        await entered.wait()
        second = asyncio.create_task(
            adapter.execute_moderation("manual", "mute_all", 5)
        )
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert state(adapter) == ("paused", "manual_override")
    assert operations == ["manual"] if manual_first else len(operations) == 2
    assert operations[-1] == "manual"


@pytest.mark.asyncio
async def test_cancelled_claim_survives_restart(setup):
    scheduler, adapter, _, now, _ = setup
    await enable(setup)
    await scheduler.tick(adapter)
    entered = asyncio.Event()

    async def pending(*args, scheduled_check):
        scheduled_check()
        entered.set()
        await asyncio.Event().wait()

    adapter.execute_moderation.side_effect = pending
    now[0] = epoch("2026-09-22T23:00:00")
    task = asyncio.create_task(scheduler.tick(adapter))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    scheduler.recovered.clear()
    await scheduler.tick(adapter)
    assert state(adapter) == ("paused", "restart_review")
    assert adapter.execute_moderation.await_count == 1


@pytest.mark.asyncio
async def test_future_schedule_survives_startup_and_repeated_migration(setup):
    scheduler, adapter, _, _, _ = setup
    await enable(setup)
    scheduler.recovered.clear()
    adapter.account = ""
    adapter.connection_state = "reconnecting"
    await scheduler.tick(adapter)
    adapter.account = "1"
    await scheduler.tick(adapter)
    assert state(adapter) == ("active", "")
    for _ in range(3):
        with closing(store.database(adapter)) as db:
            assert db.execute("SELECT count(*) FROM schedules").fetchone()[0] == 1
    adapter.connection_state = "online"
    await scheduler.tick(adapter)
    adapter.execute_moderation.assert_not_awaited()
