"""Durable fastlane ownership does not stall the ordinary sender FIFO."""

import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    member_exemptions,
    open_service,
)
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import fresh_bot, ingest
from tests.test_wangshangliao_open_service import service

bot = fresh_bot


def bind_roster(bot, *, role="GROUP_ROLE_MEMBER", complete=True):
    bot.member_snapshots = {
        "5": (
            bot.account,
            time.monotonic(),
            {
                "complete": complete,
                "groupMemberInfo": [{"userId": "2", "nimId": "22", "groupRole": role}],
            },
        )
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        None,
        "closed",
        "offline",
        "stopping",
        "revision",
        "account",
        "group",
        "sender",
        "message",
        "future",
        "pre_reload",
        "command",
    ],
)
async def test_fastlane_ownership_is_bound_to_current_trusted_scope(bot, change):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    payload = copy.deepcopy(await ingest(bot, "100", "fixture-ad"))
    evidence = payload["_ingress_content"]
    if change == "closed":
        runtime.active = False
    elif change == "offline":
        bot.connection_state = "offline"
    elif change == "stopping":
        bot.stopping.set()
    elif change == "revision":
        bot.config["_business_revision"] = "changed"
    elif change in {"account", "group", "sender", "message"}:
        evidence[change] = "other"
    elif change == "future":
        evidence["received_at"] += 100
    elif change == "pre_reload":
        evidence["received_at"] = runtime.started_at - 1
    elif change == "command":
        payload["_dispatch_class"] = "command"
    try:
        assert runtime.owns(bot, "5", "100", payload) is (change is None)
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poller_state",
    ["running", "stopped", "absent", "full", "exempt", "native_admin", "role_unknown"],
)
async def test_open_pipeline_defers_only_to_a_healthy_native_worker(
    bot, tmp_path, monkeypatch, poller_state
):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    bind_roster(
        bot,
        role="GROUP_ROLE_ADMIN"
        if poller_state == "native_admin"
        else "GROUP_ROLE_MEMBER",
        complete=poller_state != "role_unknown",
    )
    runtime = runtime_for(bot)
    if poller_state == "full":
        runtime.backlog = 0
    monkeypatch.setattr(
        member_exemptions, "is_exempt", Mock(return_value=poller_state == "exempt")
    )
    payload = await ingest(bot, "100", "fixture-ad")
    value = service(tmp_path / "service")
    value.plugin.flood_runtime = runtime
    value.navigation.key = lambda _: "fixture-menu"
    value.navigation.accepts = lambda *_: False
    value.navigation.handle = AsyncMock(return_value=False)
    value.group_ai.observe = AsyncMock()
    value.rule_replies.observe = AsyncMock(return_value=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_handle(*args, **kwargs):
        entered.set()
        await release.wait()
        return poller_state not in {"exempt", "native_admin", "role_unknown"}

    handler = AsyncMock(side_effect=blocked_handle)
    monkeypatch.setattr(open_service, "handle", handler)
    poller = asyncio.create_task(asyncio.Event().wait())
    if poller_state == "stopped":
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    if poller_state != "absent":
        value.plugin.flood_poller = poller
    extras = {"wangshangliao_payload": payload}
    event = SimpleNamespace(
        platform=bot,
        message_str="fixture-ad",
        message_obj=SimpleNamespace(message_id="100"),
        is_private_chat=lambda: False,
        get_group_id=lambda: "5",
        get_sender_id=lambda: "2",
        get_extra=lambda key: extras.get(key),
        set_extra=lambda key, item: extras.__setitem__(key, item),
        stop_event=Mock(),
    )
    job = asyncio.create_task(value._on_event(event))
    try:
        if poller_state == "running":
            await asyncio.wait_for(job, 1)
            handler.assert_not_awaited()
            assert extras["_component_claimed"] is True
            value.group_ai.observe.assert_awaited_once_with(event, consumed=True)
            # Deferring is not a punishment receipt: the durable worker still
            # processes the original inbox row through all native guards.
            assert not bot.calls
            await runtime.step()
            await asyncio.gather(*runtime.workers.values())
            assert bot.calls == [("recall", "100", "22")]
        else:
            await asyncio.wait_for(entered.wait(), 1)
            assert not job.done()
            release.set()
            await asyncio.wait_for(job, 1)
            handler.assert_awaited_once()
            assert not runtime.assigned
            if poller_state in {"exempt", "native_admin", "role_unknown"}:
                value.rule_replies.observe.assert_awaited_once_with(event)
                value.group_ai.observe.assert_awaited_once_with(event, consumed=False)
    finally:
        release.set()
        poller.cancel()
        await asyncio.gather(poller, job, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_handoff_is_bounded_deduplicated_and_acknowledges_admission(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    bind_roster(bot)
    runtime = runtime_for(bot, slots=1, backlog=1)
    first = await ingest(bot, "100", "fixture-ad")
    second = await ingest(bot, "101", "fixture-ad")
    entered, release = asyncio.Event(), asyncio.Event()
    executed = []

    async def execute(key, adapter, mid, payload, epoch):
        executed.append(mid)
        entered.set()
        await release.wait()

    runtime.execute = execute
    try:
        assert await runtime.handoff(bot, "5", "100", first)
        assert await runtime.handoff(bot, "5", "100", first)
        assert len(runtime.assigned) == 1
        assert sum(map(len, runtime.pending.values())) == 1
        assert not await runtime.handoff(bot, "5", "101", second)
        await runtime.step()
        await entered.wait()
        assert await runtime.handoff(bot, "5", "100", first)
        assert not runtime.pending
        assert await runtime.handoff(bot, "5", "101", second)
        await runtime.step()
        assert executed == ["100"]
        assert sum(map(len, runtime.pending.values())) == 1
        assert len(runtime.assigned) == 2
        release.set()
        await asyncio.gather(*runtime.workers.values())
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert executed == ["100", "101"]
        assert not runtime.assigned
        assert not runtime.pending
        assert not await runtime.handoff(bot, "5", "100", first)
        runtime.cursors.clear()
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert executed == ["100", "101"]
        assert not bot.calls
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_handoff_rechecks_scope_after_local_exemption_lookup(bot, monkeypatch):
    from astrbot.builtin_stars.wangshangliao_moderation import flood

    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    bind_roster(bot)
    runtime = runtime_for(bot)
    payload = await ingest(bot, "100", "fixture-ad")

    async def lookup(*args):
        bot.config["_business_revision"] = "revoked"
        return False

    monkeypatch.setattr(flood.asyncio, "to_thread", lookup)
    try:
        assert not await runtime.handoff(bot, "5", "100", payload)
        assert not runtime.assigned
        assert not runtime.pending
        assert not bot.calls
    finally:
        await runtime.close()
