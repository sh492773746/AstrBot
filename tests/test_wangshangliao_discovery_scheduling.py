"""Slow directory reads must not block independent scheduling or survive close."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_service import OpenService
from astrbot.core.platform.sources.wangshangliao.business import BusinessError


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_directory_read_is_single_flight_without_blocking_ticks(
    tmp_path, rejected
):
    entered, release = asyncio.Event(), asyncio.Event()
    adapter = SimpleNamespace(
        account="99",
        connection_state="online",
        diagnostics=SimpleNamespace(emit=Mock()),
    )

    async def discover(current):
        assert current is adapter
        entered.set()
        await release.wait()
        if rejected:
            raise BusinessError("http_rejected", 502)

    service = SimpleNamespace(
        active=True,
        root=tmp_path,
        last_maintenance=time.time(),
        discovery_tasks={},
        last_discovery={},
        tenants={},
        store=SimpleNamespace(membership_pending=Mock(return_value=[])),
        adapters=lambda: [adapter],
        discover=AsyncMock(side_effect=discover),
        membership_wakeup=asyncio.Event(),
        group_ai=SimpleNamespace(tick=Mock()),
        rule_replies=SimpleNamespace(tick=Mock()),
        member_blacklist=SimpleNamespace(tick=Mock()),
        raid_defense=SimpleNamespace(tick=Mock()),
        welcome=SimpleNamespace(tick=Mock()),
    )
    poll = asyncio.create_task(OpenService.poll(service))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for component in (
            service.group_ai,
            service.rule_replies,
            service.member_blacklist,
            service.raid_defense,
            service.welcome,
        ):
            component.tick.assert_called_once()
        service.membership_wakeup.set()
        for _ in range(100):
            if service.group_ai.tick.call_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert service.group_ai.tick.call_count >= 2
        assert service.welcome.tick.call_count >= 2
        assert service.discover.await_count == len(service.discovery_tasks) == 1
        task = service.discovery_tasks["99"]
        release.set()
        await task
        if rejected:
            assert adapter.diagnostics.emit.call_args.kwargs["provider_code"] == 502
        else:
            adapter.diagnostics.emit.assert_not_called()
    finally:
        service.active = False
        service.membership_wakeup.set()
        await asyncio.wait_for(poll, 1)
        for task in service.discovery_tasks.values():
            task.cancel()
        await asyncio.gather(*service.discovery_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_close_reaps_directory_task_before_closing_dependents(monkeypatch):
    from astrbot.core.star.component_runtime import component_runtime

    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def discover():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def close_component():
        assert cancelled.is_set()

    task = asyncio.create_task(discover())
    await entered.wait()
    close = AsyncMock(side_effect=close_component)
    service = SimpleNamespace(
        active=True,
        discovery_tasks={"99": task},
        message_recalls=SimpleNamespace(close=close),
        navigation=SimpleNamespace(sessions={}),
        member_whitelist=SimpleNamespace(pending={}, chat_search={}),
        member_blacklist=SimpleNamespace(close=close),
        raid_defense=SimpleNamespace(close=close),
        welcome=SimpleNamespace(close=close),
        group_ai=SimpleNamespace(close=close),
        rule_replies=SimpleNamespace(close=close),
        candidate_tasks=set(),
        rpc=None,
    )
    runtime_close = AsyncMock()
    monkeypatch.setattr(component_runtime, "close", runtime_close)
    await OpenService.close(service)
    assert task.cancelled()
    assert not service.discovery_tasks
    assert not service.active
    assert close.await_count == 6
    runtime_close.assert_awaited_once_with("wangshangliao_moderation")
