"""Offline regressions for retained group dashboard scopes and read barriers."""

import copy
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app

pytestmark = pytest.mark.usefixtures("wsl_command_classifier")


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
        config["reply_groups"]["5"] = not config["reply_groups"]["5"]
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
@pytest.mark.parametrize("endpoint", ["activities", "cashback-logs", "operations"])
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
