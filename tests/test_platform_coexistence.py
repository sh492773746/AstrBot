"""Offline coexistence checks for the shared bus, not production load capacity."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation.concurrency import AIRuntime
from astrbot.core.event_bus import EventBus
from astrbot.core.platform.sources.wangshangliao.progression_store import (
    ProgressionStore,
)

PLATFORMS = ("wangshangliao", "lajiaoliao", "telegram")


@pytest_asyncio.fixture
async def shared_bus():
    """Run the production bus with synthetic events and replaceable handlers."""
    queue = asyncio.Queue()
    manager = SimpleNamespace(
        get_conf_info=lambda origin: {"id": origin.split(":", 1)[0]}
    )
    bus = EventBus(queue, {}, manager)
    bus._print_event = Mock()
    events = []

    def event(platform_name, identity, **values):
        result = SimpleNamespace(
            unified_msg_origin=f"{platform_name}:{identity}",
            platform_name=platform_name,
            processing_completion=asyncio.get_running_loop().create_future(),
            **values,
        )
        events.append(result)
        queue.put_nowait(result)
        return result

    dispatcher = asyncio.create_task(bus.dispatch())
    try:
        yield bus, event
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
        pending = list(bus._pending_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for item in events:
            if not item.processing_completion.done():
                item.processing_completion.cancel()
            elif not item.processing_completion.cancelled():
                item.processing_completion.exception()


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled", PLATFORMS)
@pytest.mark.parametrize("shared_profile", [False, True])
async def test_async_platform_backlog_does_not_serialize_other_platforms(
    shared_bus, stalled, shared_profile
):
    bus, submit = shared_bus
    release, admitted = asyncio.Event(), asyncio.Event()
    completed = []
    started = 0
    # Telegram has no equivalent downstream bus admission limit. This is a
    # finite synthetic backlog, not a claim that 64 is its configured limit.
    count = {"wangshangliao": 16, "lajiaoliao": 32, "telegram": 64}[stalled]

    async def execute(event):
        nonlocal started
        if event.platform_name == stalled:
            started += 1
            if started == count:
                admitted.set()
            await release.wait()
        completed.append(event.unified_msg_origin)

    scheduler = SimpleNamespace(execute=execute)
    if shared_profile:
        bus.astrbot_config_mgr.get_conf_info = lambda _: {"id": "shared"}
        bus.pipeline_scheduler_mapping["shared"] = scheduler
    else:
        bus.pipeline_scheduler_mapping.update(dict.fromkeys(PLATFORMS, scheduler))
    waiting = [submit(stalled, str(index)) for index in range(count)]
    await asyncio.wait_for(admitted.wait(), 2)
    others = [name for name in PLATFORMS if name != stalled]
    probes = [submit(name, "probe") for name in others]
    await asyncio.wait_for(
        asyncio.gather(*(event.processing_completion for event in probes)), 2
    )
    assert completed == [event.unified_msg_origin for event in probes]
    assert all(not event.processing_completion.done() for event in waiting)
    release.set()
    await asyncio.wait_for(
        asyncio.gather(*(event.processing_completion for event in waiting)), 2
    )
    assert len(completed) == len(set(completed)) == count + len(others)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", PLATFORMS)
@pytest.mark.parametrize("outcome", ["exception", "cancelled"])
async def test_platform_failure_does_not_cancel_unrelated_events(
    shared_bus, failed, outcome
):
    bus, submit = shared_bus
    entered = asyncio.Event()
    finished = []

    async def execute(event):
        if event.platform_name == failed:
            entered.set()
            if outcome == "exception":
                raise RuntimeError("synthetic_platform_failure")
            await asyncio.Event().wait()
        finished.append(event.platform_name)

    bus.pipeline_scheduler_mapping.update(
        dict.fromkeys(PLATFORMS, SimpleNamespace(execute=execute))
    )
    bad = submit(failed, "failure")
    await asyncio.wait_for(entered.wait(), 2)
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="synthetic_platform_failure"):
            await asyncio.wait_for(bad.processing_completion, 2)
    else:
        bad.processing_completion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bad.processing_task
    probes = [submit(name, "after_failure") for name in PLATFORMS if name != failed]
    await asyncio.wait_for(
        asyncio.gather(*(event.processing_completion for event in probes)), 2
    )
    assert set(finished) == set(PLATFORMS) - {failed}


@pytest.mark.asyncio
async def test_saturated_wsl_customer_slots_leave_other_accounts_and_platforms_free(
    shared_bus,
):
    bus, submit = shared_bus
    runtime = AIRuntime()
    release, saturated = asyncio.Event(), asyncio.Event()
    admitted = []
    bots = {
        name: SimpleNamespace(
            account=name,
            config={"id": name, "_business_active": True, "_business_epoch": 1},
        )
        for name in ("first", "second")
    }

    async def execute(event):
        if event.platform_name == "wangshangliao":
            assert await runtime.customer(event)
            admitted.append(event.unified_msg_origin)
            if event.platform.account == "first":
                if len(admitted) == 2:
                    saturated.set()
                await release.wait()

    bus.pipeline_scheduler_mapping.update(
        dict.fromkeys(PLATFORMS, SimpleNamespace(execute=execute))
    )
    first = [
        submit("wangshangliao", str(i), platform=bots["first"], policy_epoch=1)
        for i in range(3)
    ]
    await asyncio.wait_for(saturated.wait(), 2)
    probes = [
        submit("wangshangliao", "other", platform=bots["second"], policy_epoch=1),
        submit("lajiaoliao", "probe"),
        submit("telegram", "probe"),
    ]
    await asyncio.wait_for(
        asyncio.gather(*(event.processing_completion for event in probes)), 2
    )
    assert first[2].unified_msg_origin not in admitted
    assert all(not event.processing_completion.done() for event in first)
    release.set()
    await asyncio.wait_for(
        asyncio.gather(*(event.processing_completion for event in first)), 2
    )
    await asyncio.sleep(0)
    assert not runtime.sessions
    assert all(semaphore._value == 2 for semaphore in runtime.customers.values())


@pytest.mark.asyncio
async def test_worker_owned_wsl_database_wait_leaves_other_platforms_free(
    tmp_path, shared_bus
):
    bus, submit = shared_bus
    store = ProgressionStore(tmp_path / "moderation.sqlite3")
    await asyncio.to_thread(store.initialize)
    blocker = sqlite3.connect(store.path)
    blocker.execute("BEGIN IMMEDIATE")
    entered = asyncio.Event()

    async def execute(event):
        if event.platform_name == "wangshangliao":
            entered.set()
            await asyncio.to_thread(store.reserve, "operation", "1", "2", "3", "4")

    bus.pipeline_scheduler_mapping.update(
        dict.fromkeys(PLATFORMS, SimpleNamespace(execute=execute))
    )
    pending = submit("wangshangliao", "database")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        probes = [submit(name, "probe") for name in ("lajiaoliao", "telegram")]
        await asyncio.wait_for(
            asyncio.gather(*(event.processing_completion for event in probes)), 2
        )
        assert not pending.processing_completion.done()
    finally:
        blocker.rollback()
        blocker.close()
        await asyncio.wait_for(pending.processing_completion, 12)
