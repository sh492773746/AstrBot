"""Offline regressions for dashboard scope, approvals and welcome delivery."""

import copy
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    feature_image,
    friend_auto,
    private_interaction,
)
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app
from tests.test_wangshangliao_first_use import decode_image
from tests.test_wangshangliao_first_use import image_event as image_event
from tests.test_wangshangliao_friend_auto import approval as approval
from tests.test_wangshangliao_onboarding_guides import pair as pair

pytestmark = pytest.mark.usefixtures("wsl_command_classifier")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal",
    [
        "FRIENDLOG_STATE_REFUSE",
        "FRIENDLOG_STATE_DELETE",
        "FRIENDLOG_STATE_EXPIRE",
    ],
)
async def test_signed_terminal_readback_is_rejected_not_unknown(approval, terminal):
    worker, adapter, roster, upstream, calls, clock, _ = approval
    normal = upstream.side_effect
    worker.welcome = AsyncMock()

    async def request(client, route, params, *, authorize):
        result = await normal(client, route, params, authorize=authorize)
        if route == friend_auto.APPROVE_ROUTE:
            roster[0]["state"] = terminal
        return result

    upstream.side_effect = request
    await worker.step(adapter)
    with worker.database("fixture") as db:
        assert (
            db.execute("SELECT state FROM approval_attempts").fetchone()[0]
            == "rejected"
        )
    state = await worker.status(adapter)
    assert state["accepted"] == state["inflight"] == state["pending"] == 0
    assert state["paused_reason"] == "friend_approval_rejected"
    worker.welcome.assert_not_awaited()
    clock[0] += 300
    reloaded = friend_auto.FriendAutoApproval(worker.context)
    reloaded.active = True
    await reloaded.step(adapter)
    assert len(calls) == 3
    resumed = await reloaded.reconcile(adapter, state["version"], lambda: None)
    assert not resumed["paused_reason"]
    clock[0] += 61
    await reloaded.step(adapter)
    assert sum(route == friend_auto.APPROVE_ROUTE for route, _, _ in calls) == 1
    assert all(b[2] - a[2] >= 3 for a, b in zip(calls, calls[1:]))


@pytest.mark.asyncio
async def test_legacy_unknown_application_never_becomes_approval_or_welcome(approval):
    worker, adapter, roster, upstream, calls, clock, _ = approval
    roster[0]["state"] = "FRIENDLOG_STATE_UNKNOWN"
    worker.welcome = AsyncMock()
    await worker.notify_welcomes(adapter)
    await worker.step(adapter)
    clock[0] += 61
    await worker.notify_welcomes(adapter)
    await worker.step(adapter)
    assert calls and all(route == friend_auto.LIST_ROUTE for route, _, _ in calls)
    worker.welcome.assert_not_awaited()
    with worker.database("fixture") as db:
        assert not db.execute("SELECT * FROM approval_attempts").fetchall()
    assert (await worker.status(adapter))["pending"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["FRIENDLOG_STATE_UNKNOWN", "FRIENDLOG_STATE_PROCESSING"]
)
async def test_unknown_intent_cannot_reconcile_or_replay(approval, state):
    worker, adapter, roster, _, calls, clock, _ = approval
    await worker.status(adapter)
    with worker.database("fixture") as db:
        db.execute("INSERT INTO approval_attempts VALUES('1',17,21,'unknown',900,900)")
        db.execute("UPDATE approval_settings SET paused='friend_approval_unknown'")
    roster[0]["state"] = state
    for current in [worker, friend_auto.FriendAutoApproval(worker.context)]:
        current.active = True
        clock[0] += 61
        with pytest.raises(ValueError, match="friend_approval_still_unknown"):
            await current.reconcile(adapter, 1, lambda: None)
        await current.step(adapter)
    assert all(route == friend_auto.LIST_ROUTE for route, _, _ in calls)
    assert (await worker.status(adapter))["inflight"] == 1


@pytest.mark.asyncio
async def test_disabled_adapter_does_not_report_active_approvals(approval):
    worker, adapter, _, upstream, _, _, _ = approval
    adapter.config["enable"] = False
    assert (await worker.status(adapter))["active"] is False
    await worker.step(adapter)
    upstream.assert_not_awaited()


@pytest.mark.parametrize("change", ["account", "group", "instance", "configuration"])
def test_activity_revision_binds_equal_values_to_authorized_scope(page_app, change):
    _, page, _, configs, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    config = copy.deepcopy(configs["fixture"])
    first = page._activity_snapshot(adapter, config, "5")
    group = "5"
    if change == "account":
        config["account_id"] = "2"
    elif change == "group":
        group = "6"
    elif change == "instance":
        config["id"] = "another-instance"
    else:
        config["reply_private"] = False
    second = page._activity_snapshot(adapter, config, group)
    assert first["lottery"] == second["lottery"]
    assert first["invite_rate"] == second["invite_rate"]
    assert first["revision"] != second["revision"]


@pytest.mark.asyncio
async def test_activity_revision_from_other_group_cannot_write(page_app, headers):
    app, _, _, _, root = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        result = await client.get(
            f"{PREFIX}/activities",
            params={"bot_id": "fixture", "group_id": "5"},
            headers=headers,
        )
        original = result.json()["data"]
        response = await client.post(
            f"{PREFIX}/activities",
            headers=headers,
            json={
                "bot_id": "fixture",
                "group_id": "6",
                "revision": original["revision"],
                "lottery": {**original["lottery"], "prize": "wrong scope"},
                "invite_rate": original["invite_rate"],
            },
        )
    assert response.status_code == 409
    with sqlite3.connect(root / "activities.sqlite3") as db:
        assert not db.execute("SELECT * FROM lottery_settings").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["groups", "invitations", "cashback-logs"])
async def test_read_result_rechecks_scope_after_io(
    page_app, headers, monkeypatch, endpoint
):
    app, page, _, configs, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.connection_state = "online"

    def changed(*args, **kwargs):
        configs["fixture"]["account_id"] = "2"
        return []

    if endpoint == "groups":
        from astrbot.core.platform.sources.wangshangliao import directory

        async def group_read(*args):
            changed()
            return {"groups": []}

        adapter.business = SimpleNamespace()
        monkeypatch.setattr(directory, "group_directory", group_read)
    elif endpoint == "invitations":
        adapter.get_group_inviters.side_effect = changed
    else:
        page.cashback = SimpleNamespace(
            service=SimpleNamespace(
                collector_id="fixture",
                store=SimpleNamespace(
                    read_logs=Mock(side_effect=changed), operation_log=Mock()
                ),
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/{endpoint}",
            params={"bot_id": "fixture", "group_id": "5"},
            headers=headers,
        )
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "adapter"])
async def test_approval_response_cannot_cross_account_or_adapter_generation(
    page_app, headers, change
):
    app, page, _, configs, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]

    async def changed(_adapter):
        if change == "account":
            configs["fixture"]["account_id"] = adapter.account = "2"
        else:
            page.context.platform_manager.platform_insts[:] = [copy.copy(adapter)]
        return {"enabled": True, "version": 1}

    page.friend_auto.status = AsyncMock(side_effect=changed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/friend-approval", params={"bot_id": "fixture"}, headers=headers
        )
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint", ["friend-approval", "activities", "cashback-logs", "operations"]
)
async def test_audited_routes_require_dashboard_jwt_before_io(page_app, endpoint):
    app, page, service, _, _ = page_app
    page.business_settings = SimpleNamespace(history=Mock(), log=Mock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        params = {"bot_id": "fixture", "group_id": "5"}
        assert (
            await client.get(f"{PREFIX}/{endpoint}", params=params)
        ).status_code == 401
        response = await client.get(
            f"{PREFIX}/{endpoint}",
            params=params,
            headers={"Authorization": "ApiKey fixture-key"},
        )
    assert response.status_code == 403
    service.update_bot.assert_not_awaited()
    page.business_settings.history.assert_not_called()


@pytest.mark.asyncio
async def test_second_image_uncertainty_records_incomplete_and_never_replays(
    pair, monkeypatch
):
    worker, event, ledger, calls = pair
    monkeypatch.setattr(private_interaction, "is_managed_account", lambda _: False)
    original = event.platform.nim.request.side_effect

    async def send(service, command, props):
        result = await original(service, command, props)
        if len(calls) == 3:
            raise TimeoutError("secret provider payload")
        return result

    event.platform.nim.request.side_effect = send
    assert (
        await worker.welcome_friend(event.platform, "2", "902", 17, lambda: None)
        == "delivery_unconfirmed"
    )
    assert [
        decode_image(props[10].decode(), b"a" * 32).format for _, _, props in calls
    ] == [0, 1, 1]
    assert (await ledger.receipt("friend-welcome/1/17/902/guide-instructions"))[
        "status"
    ] == "accepted"
    assert (await ledger.receipt("friend-welcome/1/17/902/guide-examples"))[
        "status"
    ] == "unknown"
    with worker.database("fixture") as db:
        assert (
            db.execute("SELECT outcome FROM onboarding").fetchone()[0]
            == "delivery_unconfirmed"
        )
    reloaded = private_interaction.PrivateInteraction(feature_image.FeatureGuides())
    assert (
        await reloaded.welcome_friend(event.platform, "2", "902", 18, lambda: None)
        == "already_sent"
    )
    assert len(calls) == 3
