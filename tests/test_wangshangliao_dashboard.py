"""Plugin page access, narrow configuration edits and read-only audit."""

import copy
import json
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from astrbot.builtin_stars.wangshangliao_moderation import activity_store, dashboard
from astrbot.core.platform.sources.wangshangliao import schedule_store
from astrbot.core.star.star_manager import PluginManager
from astrbot.dashboard.api.plugins import router
from astrbot.dashboard.responses import ApiError
from astrbot.dashboard.services.plugin_page_service import PluginPageService

PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation"
SECRET = "wangshangliao-plugin-page-fixture-secret"


@pytest.fixture
def page_app(tmp_path, monkeypatch):
    config = {
        "id": "fixture",
        "type": "wangshangliao",
        "account_id": "1",
        "nickname": "Fixture",
        "enable": True,
        "enabled_groups": ["5", "6"],
        "reply_private": True,
        "reply_groups": {"5": True, "6": False},
        "proactive_send": {"enabled": True, "targets": ["private/2"]},
        "developer_test": {"budget": 7},
        "session_ref": "must-not-appear",
        "password": "must-not-appear",
        "moderation": {
            "enabled": True,
            "automation_enabled": True,
            "recall_enabled": True,
            "content_rules_since": 1700000000,
            "semantic": {"enabled": True, "provider_id": "fixture-model"},
            "permissions": {"5": ["mute", "unmute", "recall", "kick"], "6": ["unmute"]},
            "auto_kick": {"5": True, "6": False},
        },
    }
    configs = {"fixture": config, "other": {"id": "other", "type": "telegram"}}

    def get_bot(bot_id):
        if bot_id not in configs:
            raise ValueError("not_found")
        return {"bot": copy.deepcopy(configs[bot_id])}

    async def update_bot(bot_id, value, owner=""):
        assert owner == "fixture-admin"
        configs[bot_id] = copy.deepcopy(value)

    service = SimpleNamespace(
        list_bots=lambda **_: {"bots": [copy.deepcopy(configs["fixture"])]},
        get_bot=get_bot,
        update_bot=AsyncMock(side_effect=update_bot),
    )
    provider = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="fixture-model"),
        get_model=lambda: "fixture-model-name",
    )
    adapter = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="fixture", name="wangshangliao"),
        config=copy.deepcopy(config),
        account="1",
        get_stats=lambda: {"connection_state": "online"},
        get_group_inviters=AsyncMock(
            return_value={
                "group_id": "5",
                "scope": "current_members",
                "source": "nim_team_member_inviter",
                "items": [
                    {
                        "member_id": "2",
                        "inviter_id": "1",
                        "status": "attributed",
                    }
                ],
            }
        ),
        get_group_invitation_records=AsyncMock(
            return_value={
                "group_id": "5",
                "source": "business_apply_logs",
                "scope": "account_visible_records",
                "complete": False,
                "items": [
                    {
                        "member_id": "99",
                        "inviter_id": "1",
                        "pending": True,
                        "state": "MEMBER_STATE_INVITED",
                    }
                ],
            }
        ),
    )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter]),
        get_all_providers=lambda: [provider],
        registered_web_apis=[],
    )
    context.register_web_api = lambda *args: context.registered_web_apis.append(args)
    page = dashboard.DashboardPage(context)
    page.register()
    app = FastAPI()
    app.state.jwt_secret = SECRET
    app.state.services = SimpleNamespace(bots=service)
    app.state.core_lifecycle = SimpleNamespace(star_context=context)
    app.state.db = SimpleNamespace(
        get_active_api_key_by_hash=AsyncMock(
            return_value=SimpleNamespace(key_id="plugin-only", scopes=["plugin"])
        ),
        touch_api_key=AsyncMock(),
    )

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return JSONResponse(
            {"status": "error", "message": exc.message}, status_code=exc.status_code
        )

    app.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(dashboard, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(activity_store, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(schedule_store, "instance_dir", lambda _: tmp_path)
    return app, page, service, configs, tmp_path


@pytest.fixture
def headers():
    return {
        "Authorization": "Bearer "
        + jwt.encode({"username": "fixture-admin"}, SECRET, algorithm="HS256")
    }


@pytest.mark.asyncio
async def test_page_settings_require_auth_and_never_return_secrets(page_app, headers):
    app, _, _, _, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        assert (await client.get(f"{PREFIX}/settings")).status_code == 401
        response = await client.get(f"{PREFIX}/settings", headers=headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["bots"][0]["state"] == "online"
        assert data["providers"] == [
            {"id": "fixture-model", "model": "fixture-model-name"}
        ]
        assert "must-not-appear" not in response.text
        assert "proactive_send" not in data["bots"][0]


@pytest.mark.asyncio
async def test_plugin_scope_does_not_grant_bot_settings(page_app):
    app, _, _, _, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/settings", headers={"Authorization": "ApiKey fixture-key"}
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_unloaded_page_rejects_registered_routes(page_app, headers):
    app, page, _, _, _ = page_app
    page.active = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        assert (
            await client.get(f"{PREFIX}/settings", headers=headers)
        ).status_code == 403


@pytest.mark.asyncio
async def test_invitation_reads_are_authenticated_and_do_not_save(page_app, headers):
    app, page, service, _, root = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        url = f"{PREFIX}/invitations?bot_id=fixture&group_id=5&member_id=2"
        assert (await client.get(url)).status_code == 401
        assert (
            await client.get(url, headers={"Authorization": "ApiKey fixture-key"})
        ).status_code == 403
        adapter.get_group_inviters.assert_not_called()
        response = await client.get(url, headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["items"][0]["inviter_id"] == "1"
        assert "must-not-appear" not in response.text
        assert (await client.post(url, headers=headers)).json()["status"] == "error"
        page.active = False
        assert (await client.get(url, headers=headers)).status_code == 403
    adapter.get_group_inviters.assert_awaited_once_with("5", "2")
    service.update_bot.assert_not_called()
    assert not list(root.glob("*.sqlite3"))


@pytest.mark.asyncio
async def test_pending_record_reads_require_jwt_and_never_save(page_app, headers):
    app, page, service, _, root = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        url = f"{PREFIX}/invitations?bot_id=fixture&group_id=5&source=records"
        assert (await client.get(url)).status_code == 401
        assert (
            await client.get(url, headers={"Authorization": "ApiKey fixture-key"})
        ).status_code == 403
        adapter.get_group_invitation_records.assert_not_called()
        response = await client.get(url, headers=headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"][0]["pending"]
        assert data["items"][0]["inviter_id"] == "1"
        assert data["scope"] == "account_visible_records"
        assert not data["complete"]
        page.active = False
        assert (await client.get(url, headers=headers)).status_code == 403
    adapter.get_group_invitation_records.assert_awaited_once_with(
        "5", "", pending_only=True, last_id=""
    )
    adapter.get_group_inviters.assert_not_called()
    service.update_bot.assert_not_called()
    assert not list(root.glob("*.sqlite3"))


@pytest.mark.asyncio
async def test_activity_controls_read_and_save_scoped_values(page_app, headers):
    app, _, service, configs, root = page_app
    adapter = app.state.core_lifecycle.star_context.platform_manager.platform_insts[0]
    with activity_store.database(adapter) as db:
        db.execute(
            "INSERT INTO invite_credits VALUES(?,?,?,?,?,?,?,?)",
            ("1", "5", "10", "2", "peer-10", "peer-2", 125, 1),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        params = {"bot_id": "fixture", "group_id": "5"}
        assert (
            await client.get(f"{PREFIX}/activities", params=params)
        ).status_code == 401
        response = await client.get(
            f"{PREFIX}/activities", params=params, headers=headers
        )
        data = response.json()["data"]
        assert data["lottery"]["winners"] == 3
        assert data["invitation"]["credited_count"] == 1
        assert data["invitation"]["credited_total"] == "1.25"
        payload = {
            "bot_id": "fixture",
            "group_id": "5",
            "revision": data["revision"],
            "lottery": {
                "prize": "猪脚饭",
                "winners": 2,
                "duration_minutes": 15,
                "capacity": 20,
                "invite_gate": 3,
                "contact": "群管理员",
            },
            "invite_rate": "5.50",
        }
        saved = await client.post(f"{PREFIX}/activities", headers=headers, json=payload)
    result = saved.json()["data"]
    assert result["lottery"] == payload["lottery"]
    assert result["invite_rate"] == "5.50"
    assert result["invitation"]["credited_total"] == "1.25"
    assert configs["fixture"]["moderation"]["permissions"]["6"] == ["unmute"]
    service.update_bot.assert_not_awaited()
    assert (root / "activities.sqlite3").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"invite_rate": "-1"},
        {"invite_rate": "1.001"},
        {"lottery": {"winners": 21}},
        {"group_id": "9"},
        {"revision": "stale"},
    ],
)
async def test_invalid_activity_edits_never_mutate(page_app, headers, patch):
    app, _, _, _, _ = page_app
    adapter = app.state.core_lifecycle.star_context.platform_manager.platform_insts[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        current = await client.get(
            f"{PREFIX}/activities?bot_id=fixture&group_id=5", headers=headers
        )
        assert current.status_code == 200
        data = current.json()["data"]
        body = {
            "bot_id": "fixture",
            "group_id": "5",
            "revision": data["revision"],
            "lottery": {
                "prize": "奖品",
                "winners": 3,
                "duration_minutes": 10,
                "capacity": 15,
                "invite_gate": 0,
                "contact": "联系人",
            },
            "invite_rate": "5",
        }
        if "lottery" in patch:
            body["lottery"] = patch["lottery"]
        body.update({key: value for key, value in patch.items() if key != "lottery"})
        response = await client.post(f"{PREFIX}/activities", headers=headers, json=body)
    if patch.get("group_id") == "9":
        assert response.status_code == 400
    else:
        assert response.status_code in (400, 409)
    with activity_store.database(adapter) as db:
        row = db.execute(
            "SELECT prize,winners,duration,capacity,invite_gate,contact "
            "FROM lottery_settings WHERE account='1' AND group_id='5'"
        ).fetchone()
        assert row is None


@pytest.mark.asyncio
async def test_active_lottery_settings_are_frozen_in_dashboard(page_app, headers):
    app, _, _, _, _ = page_app
    adapter = app.state.core_lifecycle.star_context.platform_manager.platform_insts[0]
    with activity_store.database(adapter) as db:
        db.execute(
            "INSERT INTO lotteries(id,command,account,group_id,prize,winners,duration,"
            "capacity,invite_gate,contact,status,ends,created) "
            "VALUES('active','command','1','5','Old',1,600,10,0,'Admin','open',100,1)"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        current = await client.get(
            f"{PREFIX}/activities?bot_id=fixture&group_id=5", headers=headers
        )
        data = current.json()["data"]
        response = await client.post(
            f"{PREFIX}/activities",
            headers=headers,
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "revision": data["revision"],
                "lottery": {
                    **data["lottery"],
                    "prize": "Changed",
                    "contact": "联系人",
                },
                "invite_rate": "0",
            },
        )
    assert response.status_code == 409, response.text
    with activity_store.database(adapter) as db:
        assert (
            db.execute(
                "SELECT prize FROM lottery_settings WHERE account='1' AND group_id='5'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_schedule_status_and_versioned_pause(page_app, headers):
    app, _, _, _, _ = page_app
    adapter = app.state.core_lifecycle.star_context.platform_manager.platform_insts[0]
    with schedule_store.database(adapter) as db:
        db.execute(
            "INSERT INTO schedules(account,group_id,owner,session,start,end,zone,version,"
            "status,next_at,next_action,error) "
            "VALUES('1','5','9','wangshangliao_fixture:FriendMessage:1/private/9',"
            "'23:00','08:00','Asia/Shanghai',4,'active',200,'mute_all','')"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        params = {"bot_id": "fixture", "group_id": "5"}
        state = await client.get(f"{PREFIX}/schedule", params=params, headers=headers)
        assert state.json()["data"]["schedule"]["status"] == "active"
        stale = await client.post(
            f"{PREFIX}/schedule",
            headers=headers,
            json={**params, "version": 3, "action": "pause"},
        )
        assert stale.status_code == 409
        paused = await client.post(
            f"{PREFIX}/schedule",
            headers=headers,
            json={**params, "version": 4, "action": "pause"},
        )
        assert paused.status_code == 200
        state = await client.get(f"{PREFIX}/schedule", params=params, headers=headers)
    assert state.json()["data"]["schedule"]["status"] == "paused"
    assert state.json()["data"]["schedule"]["version"] == 5


@pytest.mark.asyncio
async def test_record_read_passes_exact_cursor_state_and_account_filter(
    page_app, headers
):
    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/invitations?bot_id=fixture&group_id=5&source=records"
            "&pending_only=false&last_id=91&member_id=99",
            headers=headers,
        )
    assert response.status_code == 200
    adapter.get_group_invitation_records.assert_awaited_once_with(
        "5", "99", pending_only=False, last_id="91"
    )
    adapter.get_group_inviters.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "source=unreviewed",
        "source=records&pending_only=maybe",
        "source=records&last_id=private-value",
        "source=records&last_id=" + "9" * 21,
        "source=members&last_id=91",
        "source=records&member_id=nickname",
        "source=records&group_id=9",
        "source=records&bot_id=other",
    ],
)
async def test_record_read_invalid_scope_or_parameters_do_not_send(
    page_app, headers, query
):
    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    params = {"bot_id": "fixture", "group_id": "5"}
    params.update(part.split("=", 1) for part in query.split("&"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/invitations", params=params, headers=headers
        )
    assert response.status_code == 400
    adapter.get_group_invitation_records.assert_not_called()
    adapter.get_group_inviters.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("invitation_read_cooldown", 429),
        ("invitation_scope", 503),
        ("invitation_response", 503),
        ("business_rejected", 503),
    ],
)
async def test_record_read_failure_is_not_an_empty_success(
    page_app, headers, error, status
):
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.get_group_invitation_records.side_effect = ProtocolError(error)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/invitations?bot_id=fixture&group_id=5&source=records",
            headers=headers,
        )
    assert response.status_code == status
    assert "items" not in response.json()
    adapter.get_group_inviters.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "bot_id=other&group_id=5",
        "bot_id=missing&group_id=5",
        "bot_id=fixture&group_id=9",
        "bot_id=fixture&group_id=5&member_id=nickname",
        "bot_id=fixture&group_id=5&member_id=0",
        "bot_id=fixture&group_id=5&member_id=" + "9" * 30,
    ],
)
async def test_invitation_read_invalid_scope_never_reaches_platform(
    page_app, headers, query
):
    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(f"{PREFIX}/invitations?{query}", headers=headers)
    assert response.status_code in (400, 404)
    adapter.get_group_inviters.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("invitation_read_cooldown", 429),
        ("invitation_member_not_found", 404),
        ("invitation_scope", 503),
        ("invitation_query_rejected", 503),
        ("invitation_response", 503),
    ],
)
async def test_invitation_query_errors_do_not_return_guessed_results(
    page_app, headers, error, status
):
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.get_group_inviters.side_effect = ProtocolError(error)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/invitations?bot_id=fixture&group_id=5", headers=headers
        )
    assert response.status_code == status
    assert "items" not in response.json()


@pytest.mark.asyncio
async def test_save_preserves_other_group_identity_and_ledger(page_app, headers):
    app, _, service, configs, root = page_app
    original = copy.deepcopy(configs["fixture"])
    ledger = root / "moderation.sqlite3"
    ledger.write_bytes(b"untouched-ledger-fixture")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            f"{PREFIX}/settings",
            headers=headers,
            json={
                "bot_id": "fixture",
                "revision": dashboard.revision(original),
                "group_id": "5",
                "policy": {"semantic": {"enabled": False}},
                "group": {
                    "permissions": ["mute", "unmute"],
                    "auto_kick": False,
                    "reply": False,
                    "customer_provider_id": "fixture-model",
                },
                "admin_provider_id": "fixture-model",
                "reply_private": False,
            },
        )
    assert response.status_code == 200
    current = configs["fixture"]
    assert current["moderation"]["semantic"]["enabled"] is False
    assert current["moderation"]["auto_kick"] == {"5": False, "6": False}
    assert current["moderation"]["permissions"]["6"] == ["unmute"]
    assert current["moderation"]["content_rules_since"] == 1700000000
    assert current["proactive_send"] == original["proactive_send"]
    assert current["developer_test"] == original["developer_test"]
    assert current["account_id"] == original["account_id"]
    assert current["enabled_groups"] == original["enabled_groups"]
    assert current["reply_groups"] == {"5": False, "6": False}
    assert current["ai_routes"] == {
        "admin_provider_id": "fixture-model",
        "groups": {"5": "fixture-model"},
    }
    assert current["reply_private"] is False
    assert ledger.read_bytes() == b"untouched-ledger-fixture"
    service.update_bot.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,status",
    [
        ({"revision": "stale"}, 409),
        ({"bot_id": "missing"}, 404),
        ({"bot_id": "other"}, 400),
        ({"group_id": "7"}, 400),
        ({"group_id": "", "group": {"auto_kick": True}}, 400),
        ({"account_id": "someone-else"}, 400),
        ({"policy": {"content_rules_since": 0}}, 400),
        ({"policy": {"permissions": {"7": ["kick"]}}}, 400),
        ({"policy": {"semantic": []}}, 400),
        ({"policy": {"semantic": {"enabled": True, "provider_id": "unknown"}}}, 400),
        ({"policy": {"semantic": {"enabled": True, "context_limit": 100}}}, 400),
        ({"group": {"auto_kick": "yes"}}, 400),
        ({"group": {"permissions": ["shell"]}}, 400),
        ({"group": {"permissions": ["mute", "mute"]}}, 400),
        ({"reply_private": "yes"}, 400),
    ],
)
async def test_invalid_and_stale_saves_never_mutate(page_app, headers, patch, status):
    app, _, service, configs, _ = page_app
    original = copy.deepcopy(configs)
    body = {
        "bot_id": "fixture",
        "revision": dashboard.revision(configs["fixture"]),
        "group_id": "5",
        **patch,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(f"{PREFIX}/settings", headers=headers, json=body)
    assert response.status_code == status
    assert configs == original
    service.update_bot.assert_not_awaited()


@pytest.mark.asyncio
async def test_semantic_first_enable_uses_server_version(page_app, headers):
    app, _, _, configs, _ = page_app
    configs["fixture"]["moderation"].pop("content_rules_since")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            f"{PREFIX}/settings",
            headers=headers,
            json={
                "bot_id": "fixture",
                "revision": dashboard.revision(configs["fixture"]),
                "policy": {"semantic": {"enabled": True}},
            },
        )
    assert response.status_code == 200
    assert configs["fixture"]["moderation"]["content_rules_since"] > 1700000000


@pytest.mark.asyncio
async def test_empty_audit_does_not_create_database(page_app, headers):
    app, _, _, _, root = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit",
            params={"bot_id": "fixture", "group_id": "5"},
            headers=headers,
        )
    assert response.json()["data"] == {
        "reviews": [],
        "sanctions": [],
        "counts": [],
        "rules": [],
    }
    assert not (root / "moderation.sqlite3").exists()


@pytest.mark.asyncio
async def test_progressive_setting_requires_recall_and_keeps_other_groups(
    page_app, headers
):
    app, _, service, configs, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        body = {
            "bot_id": "fixture",
            "group_id": "5",
            "revision": dashboard.revision(configs["fixture"]),
            "policy": {"progressive_mute": True, "recall_enabled": False},
        }
        assert (
            await client.post(f"{PREFIX}/settings", headers=headers, json=body)
        ).status_code == 400
        service.update_bot.assert_not_awaited()
        body["policy"]["recall_enabled"] = True
        assert (
            await client.post(f"{PREFIX}/settings", headers=headers, json=body)
        ).status_code == 200
    assert configs["fixture"]["moderation"]["progressive_mute"] is True
    assert configs["fixture"]["moderation"]["permissions"]["6"] == ["unmute"]


@pytest.mark.asyncio
async def test_progressive_audit_shows_separate_recall_and_mute_results(
    page_app, headers
):
    from contextlib import closing

    from astrbot.core.platform.sources.wangshangliao import automatic

    app, _, _, _, root = page_app
    with closing(sqlite3.connect(root / "moderation.sqlite3")) as db, db:
        db.executescript("""
            CREATE TABLE automatic_mutes(operation TEXT,account TEXT,group_id TEXT,
                member TEXT,status TEXT,created REAL,closed INTEGER);
            CREATE TABLE operations(id TEXT,result TEXT);
            CREATE TABLE progressive_actions(operation TEXT,account TEXT,group_id TEXT,recall_status TEXT);
        """)
        db.execute(
            "INSERT INTO automatic_mutes VALUES('op','1','5','2','accepted',1,0)"
        )
        db.execute(
            "INSERT INTO operations VALUES('op',?)",
            (json.dumps({"minutes": automatic.MUTE_MINUTES[1]}),),
        )
        db.execute("INSERT INTO progressive_actions VALUES('op','1','5','rejected')")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        result = await client.get(
            f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers
        )
    assert result.status_code == 200
    row = result.json()["data"]["sanctions"][0]
    assert row["minutes"] == 15 and row["status"] == "accepted"
    assert row["recall_status"] == "rejected"


@pytest.mark.asyncio
async def test_audit_is_bounded_account_and_group_scoped_read_only(page_app, headers):
    app, _, _, _, root = page_app
    path = root / "moderation.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE semantic_reviews(account,group_id,message,result,provider,created);
            CREATE TABLE automatic_mutes(operation,account,group_id,member,peer,message,status,created,closed);
            CREATE TABLE automatic_kicks(operation,account,group_id,member,peer,trigger_operation,status,created);
            CREATE TABLE content_violations(account,group_id,message,sender,category,observed,version,status,warning);
        """)
        for index in range(60):
            db.execute(
                "INSERT INTO semantic_reviews VALUES(?,?,?,?,?,?)",
                (
                    "1",
                    "5",
                    str(index),
                    json.dumps(
                        {
                            "decision": "allow",
                            "category": "none",
                            "reason": "<script>untrusted</script>",
                            "evidence": ["not-returned"],
                        }
                    ),
                    "fixture-model",
                    index,
                ),
            )
        for account, group, member, status, closed in (
            ("1", "5", "2", "accepted", 0),
            ("1", "5", "2", "unknown", 0),
            ("1", "5", "2", "rejected", 0),
            ("1", "5", "3", "verified", 1),
            ("9", "5", "999", "accepted", 0),
            ("1", "6", "888", "accepted", 0),
        ):
            db.execute(
                "INSERT INTO automatic_mutes VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"{account}-{group}-{member}-{status}",
                    account,
                    group,
                    member,
                    "peer",
                    "m",
                    status,
                    3,
                    closed,
                ),
            )
        db.execute(
            "INSERT INTO automatic_kicks VALUES('kick','1','5','2','peer','mute','unknown',4)"
        )
        db.execute(
            "INSERT INTO content_violations VALUES('1','5','m','2','external_promotion',5,1,'warned','warning')"
        )
    before = path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit",
            params={"bot_id": "fixture", "group_id": "5"},
            headers=headers,
        )
    data = response.json()["data"]
    assert len(data["reviews"]) == 50
    assert data["reviews"][0]["message"] == "59"
    assert "evidence" not in data["reviews"][0]
    assert data["counts"] == [
        {"member": "2", "accepted": 1, "unknown": 1, "created": 3}
    ]
    assert {row["member"] for row in data["sanctions"]} == {"2", "3"}
    assert data["sanctions"][0]["status"] == "unknown"
    assert data["rules"][0]["status"] == "warned"
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_corrupt_audit_does_not_reset_records(page_app, headers):
    app, _, _, _, root = page_app
    path = root / "moderation.sqlite3"
    path.write_bytes(b"corrupt")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit",
            params={"bot_id": "fixture", "group_id": "5"},
            headers=headers,
        )
    assert response.status_code == 503
    assert path.read_bytes() == b"corrupt"


@pytest.mark.asyncio
async def test_builtin_plugin_page_discovery():
    root = Path(dashboard.__file__).resolve().parent
    manager = SimpleNamespace(reserved_plugin_path=str(root.parent))
    service = PluginPageService(manager)
    plugin = SimpleNamespace(reserved=True, root_dir_name=root.name)
    pages = await service.discover_plugin_pages(plugin)
    assert [page.name for page in pages] == ["management"]
    translations = PluginManager._load_plugin_i18n(str(root))
    assert translations["zh-CN"]["metadata"]["display_name"] == "旺商聊群管"
    assert translations["zh-CN"]["pages"]["management"]["title"] == "旺商聊管理"
    assert translations["en-US"]["pages"]["management"]["title"] == (
        "Wangshangliao Management"
    )


def test_page_does_not_fetch_authenticated_assets_from_opaque_iframe():
    root = Path(dashboard.__file__).resolve().parent
    assert {
        path.name for path in (root / "pages/management").iterdir() if path.is_file()
    } == {
        "index.html",
    }
    html = (root / "pages/management/index.html").read_text(encoding="utf-8")
    service = PluginPageService(SimpleNamespace(reserved_plugin_path=str(root.parent)))
    served = service.rewrite_plugin_page_html(
        html,
        dashboard.PLUGIN_NAME,
        "management",
        "index.html",
        theme="light",
        extra_query_params={"asset_token": "fixture"},
    )
    assets = []
    scripts = []

    class AssetParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "script":
                scripts.append(values)
                if values.get("src"):
                    assets.append(values["src"])
            if tag == "link" and values.get("rel") == "stylesheet":
                assets.append(values.get("href"))

    parser = AssetParser()
    parser.feed(served)
    assert not assets
    assert len(scripts) == 2
    assert scripts[0]["data-astrbot-bridge"] == "/api/plugin/page/bridge-sdk.js"
    assert all("type" not in script for script in scripts)
    assert all(script.get("data-cfasync") == "false" for script in scripts)
    assert "window.AstrBotPluginPage" in served
    assert 'data-theme="light"' in served
