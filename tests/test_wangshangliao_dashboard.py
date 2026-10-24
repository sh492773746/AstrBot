"""Plugin page access, narrow configuration edits and read-only audit."""

import asyncio
import copy
import json
import os
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from astrbot.builtin_stars.wangshangliao_moderation import (
    activity_store,
    dashboard,
    friend_auto,
)
from astrbot.core.platform.sources.wangshangliao import schedule_store
from astrbot.core.star.star_manager import PluginManager
from astrbot.dashboard.api.plugins import router
from astrbot.dashboard.responses import ApiError, error
from astrbot.dashboard.services.plugin_page_service import PluginPageService

PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation"
SECRET = "wangshangliao-plugin-page-fixture-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "instruction"),
    [
        ("blacklist_permission", "原生管理权限"),
        ("blacklist_changed", "重新搜索"),
        ("blacklist_confirmation", "重新预览"),
        ("blacklist_selection", "重新搜索"),
        ("blacklist_protected", "受保护目标"),
        ("blacklist_narrow", "缩小搜索范围"),
        ("blacklist_query", "成员名称"),
        ("blacklist_target", "真正@一名成员"),
        ("blacklist_quote", "目标原消息"),
        ("blacklist_arguments", "刷新当前群"),
        ("operation_revoked", "机器人是本群管理员"),
        ("membership_changed", "机器人是本群管理员"),
        ("group_unavailable", "机器人是本群管理员"),
        ("test_blacklist_accounts", "既定的三个接入账号"),
        ("test_blacklist_window", "300秒"),
        ("test_blacklist_revoked", "重新开窗"),
        ("test_blacklist_identity", "三个原接入账号"),
        ("test_blacklist_existing", "不要新建操作绕过"),
        ("test_blacklist_unresolved", "不会自动重试"),
    ],
)
async def test_blacklist_failures_explain_the_next_safe_step(
    page_app, headers, code, instruction
):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    app, page, _, _, _ = page_app
    dispatch = AsyncMock(side_effect=OpenError(code))
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            integrated=True,
            active=True,
            rpc=SimpleNamespace(lock=asyncio.Lock(), dispatch=dispatch),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            f"{PREFIX}/open-service",
            headers=headers,
            json={"action": "member_blacklist", "payload": {}},
        )
    assert response.status_code == 409
    assert instruction in response.json()["message"]
    assert "开放服务校验未通过" not in response.json()["message"]
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_action_failure_audit_is_scoped_and_distinguishes_no_submission(
    page_app, headers
):
    from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore

    app, _, _, _, root = page_app
    store = ReviewStore(root / "moderation.sqlite3")
    store.initialize()
    with store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO content_violations VALUES"
            "('1','5','failure','2','external_promotion',1900000000,1,'unknown','unknown')"
        )
    store.save_action(
        "recall/1/5/failure",
        "1",
        "5",
        "2",
        "failure",
        "recall",
        "rejected",
        False,
        "moderation_permission",
        None,
    )
    store.save_action(
        "recall/9/5/failure",
        "9",
        "5",
        "2",
        "failure",
        "recall",
        "unknown",
        True,
        "transport",
        999,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers
        )
    assert response.status_code == 200
    row = next(
        item
        for item in response.json()["data"]["rules"]
        if item["message"] == "failure"
    )
    assert row["recall_status"] == "rejected"
    assert row["recall_submitted"] == 0
    assert row["recall_error"] == "moderation_permission"
    assert row["provider_code"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["audit", "activities", "schedule"])
async def test_dashboard_reads_do_not_block_and_reject_changed_account(
    page_app, headers, endpoint
):
    app, page, _, configs, root = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    if endpoint == "activities":
        activity_store.database(adapter).close()
    elif endpoint == "schedule":
        schedule_store.database(adapter).close()
    path = (
        root
        / {
            "audit": "moderation.sqlite3",
            "activities": "activities.sqlite3",
            "schedule": "schedules.sqlite3",
        }[endpoint]
    )
    blocker = sqlite3.connect(path)
    blocker.execute("PRAGMA journal_mode=DELETE")
    blocker.execute("BEGIN EXCLUSIVE")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        work = asyncio.create_task(
            client.get(
                f"{PREFIX}/{endpoint}?bot_id=fixture&group_id=5", headers=headers
            )
        )
        try:
            await asyncio.sleep(0.05)
            assert not work.done()
            configs["fixture"]["account_id"] = "other"
            blocker.rollback()
            response = await asyncio.wait_for(work, 3)
            assert response.status_code == 409
        finally:
            blocker.rollback()
            blocker.close()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["saved_account", "live_account", "group", "unload", "concurrent", "cancel"],
)
async def test_dashboard_activity_writer_wait_revalidates_scope(
    page_app, headers, change
):
    app, page, _, configs, root = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    db = activity_store.database(adapter)
    db.close()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        current = await client.get(
            f"{PREFIX}/activities?bot_id=fixture&group_id=5", headers=headers
        )
        data = current.json()["data"]
        body = {
            "bot_id": "fixture",
            "group_id": "5",
            "revision": data["revision"],
            "lottery": {**data["lottery"], "prize": "Prize", "contact": "Admin"},
            "invite_rate": "2",
        }
        blocker = sqlite3.connect(root / "activities.sqlite3")
        blocker.execute("BEGIN IMMEDIATE")
        work = asyncio.create_task(
            client.post(f"{PREFIX}/activities", headers=headers, json=body)
        )
        try:
            await asyncio.sleep(0.05)
            assert not work.done()
            if change == "saved_account":
                configs["fixture"]["account_id"] = "other"
            elif change == "live_account":
                adapter.account = "other"
            elif change == "group":
                configs["fixture"]["enabled_groups"] = []
            elif change == "unload":
                page.active = False
            elif change == "concurrent":
                blocker.execute(
                    "INSERT INTO lottery_settings(account,group_id,prize) "
                    "VALUES('1','5','external')"
                )
            else:
                work.cancel()
            blocker.commit()
            if change == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(work, 3)
            else:
                response = await asyncio.wait_for(work, 3)
                assert response.status_code in {409, 503}
            rows = blocker.execute("SELECT prize FROM lottery_settings").fetchall()
            assert rows == ([("external",)] if change == "concurrent" else [])
            assert (
                blocker.execute("SELECT COUNT(*) FROM invite_rules").fetchone()[0] == 0
            )
        finally:
            blocker.rollback()
            blocker.close()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_card_suffix_save_is_group_scoped_and_requires_rename_for_auto(
    page_app, headers
):
    app, _, service, configs, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        payload = {
            "bot_id": "fixture",
            "group_id": "5",
            "revision": dashboard.revision(configs["fixture"]),
            "group": {"card_auto": True, "card_suffix": "·群员"},
        }
        response = await client.post(
            f"{PREFIX}/settings", json=payload, headers=headers
        )
        assert response.status_code == 400
        assert "授权" in response.json()["message"]
        service.update_bot.assert_not_awaited()
        payload["group"]["permissions"] = ["rename"]
        response = await client.post(
            f"{PREFIX}/settings", json=payload, headers=headers
        )
        assert response.status_code == 200
        policy = configs["fixture"]["moderation"]
        assert policy["card_auto"] == {"5": True}
        assert policy["card_suffix"] == {"5": "·群员"}
        assert policy["permissions"]["6"] == ["unmute"]
        payload["revision"] = dashboard.revision(configs["fixture"])
        payload["group"] = {"card_suffix": "x" * 129}
        response = await client.post(
            f"{PREFIX}/settings", json=payload, headers=headers
        )
        assert response.status_code == 400
        assert policy["card_suffix"] == {"5": "·群员"}
        payload["group"] = {}
        payload["policy"] = {"enabled": False}
        response = await client.post(
            f"{PREFIX}/settings", json=payload, headers=headers
        )
        assert response.status_code == 200
        assert configs["fixture"]["moderation"]["enabled"] is False
        assert configs["fixture"]["moderation"]["card_auto"] == {"5": True}


@pytest.mark.asyncio
async def test_cashback_logs_share_ledger_and_require_selected_collector(
    page_app, headers
):
    from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
        CashbackStore,
    )

    app, page, _, _, root = page_app
    store = CashbackStore(root / "cashback.sqlite3")
    page.cashback = SimpleNamespace(
        service=SimpleNamespace(collector_id="fixture", store=store)
    )
    trace = "a" * 32
    store.operation_log(
        "invitation_scan",
        group="5",
        bot_id="fixture",
        level="error",
        code="roster_unavailable",
        trace=trace,
    )
    store.operation_log("client_error", level="error", code="script_error")
    store.operation_log("other_group_error", group="6", level="error")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        endpoint = f"{PREFIX}/cashback-logs"
        params = {
            "bot_id": "fixture",
            "group_id": "5",
            "level": "error",
            "trace": trace,
        }
        assert (await client.get(endpoint, params=params)).status_code == 401
        response = await client.get(endpoint, params=params, headers=headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["collector_id"] == "fixture"
        assert len(data["items"]) == 1
        assert data["items"][0]["code"] == "roster_unavailable"
        assert store.read_logs("5")[0]["action"] == "dashboard_log_query"
        response = await client.get(
            endpoint,
            params={
                "bot_id": "fixture",
                "group_id": "5",
                "level": "error",
                "include_global": "1",
            },
            headers=headers,
        )
        assert {row["action"] for row in response.json()["data"]["items"]} == {
            "invitation_scan",
            "client_error",
        }
        for bot_id, group in [("other", "5"), ("fixture", "7")]:
            response = await client.get(
                endpoint,
                params={"bot_id": bot_id, "group_id": group},
                headers=headers,
            )
            assert response.status_code == 403


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
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    async def save_business(value, expected, actor):
        assert actor == "dashboard:fixture-admin"
        assert expected == dashboard.revision(configs["fixture"])
        configs["fixture"] = copy.deepcopy(value)
        adapter.config = copy.deepcopy(value)
        return dashboard.revision(value)

    monkeypatch.setattr(
        runtime,
        "service",
        SimpleNamespace(
            active=True,
            save=AsyncMock(side_effect=save_business),
        ),
    )
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
        return JSONResponse(error(exc.message, exc.data), status_code=exc.status_code)

    app.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(dashboard, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(activity_store, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(schedule_store, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(friend_auto, "instance_dir", lambda _: tmp_path)
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
        assert data["bots"][0]["proactive_send"]["enabled"] is True
        assert data["bots"][0]["developer_test"] == {"budget": 7}


@pytest.mark.asyncio
async def test_settings_uses_self_account_name_without_saving_or_changing_revision(
    page_app, headers
):
    """Expose a verified account label without mutating configuration.

    Args:
        page_app: Authenticated plugin API fixture.
        headers: Dashboard JWT authorization headers.
    """
    app, page, service, configs, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.nim_account = "101"
    adapter.member_snapshots = {
        "5": (
            "1",
            10,
            {
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "2", "nimId": "102", "userNick": "Other"},
                    {
                        "userId": "1",
                        "nimId": "101",
                        "userNick": "Current Account",
                        "groupMemberNick": "Group Alias",
                    },
                ],
            },
        )
    }
    before = copy.deepcopy(configs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(f"{PREFIX}/settings", headers=headers)
    assert response.status_code == 200
    bot = response.json()["data"]["bots"][0]
    assert bot["nickname"] == "Current Account"
    assert bot["id"] == "fixture" and bot["account_id"] == "1"
    assert bot["revision"] == dashboard.revision(before["fixture"])
    assert configs == before
    service.update_bot.assert_not_awaited()


@pytest.mark.asyncio
async def test_settings_resolves_missing_group_names_from_directory(page_app, headers):
    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.business = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "owner": [],
                "member": [{"groupId": 5, "groupName": "测试兼职群"}],
            }
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(f"{PREFIX}/settings", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["bots"][0]["group_names"]["5"] == "测试兼职群"
    assert adapter.group_names["5"] == "测试兼职群"


@pytest.mark.asyncio
async def test_settings_discovers_new_groups_even_when_saved_names_exist(
    page_app, headers
):
    app, page, _, configs, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.group_names = {"5": "已启用群", "6": "另一个已启用群"}
    adapter.business = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "owner": [],
                "member": [{"groupId": 7, "groupName": "新加入的代理聊天群"}],
            }
        )
    )
    before = copy.deepcopy(configs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        first, second = await asyncio.gather(
            client.get(f"{PREFIX}/settings", headers=headers),
            client.get(f"{PREFIX}/settings", headers=headers),
        )
    for response in (first, second):
        assert response.status_code == 200
        assert (
            response.json()["data"]["bots"][0]["group_names"]["7"]
            == "新加入的代理聊天群"
        )
        assert response.json()["data"]["bots"][0]["enabled_groups"] == ["5", "6"]
    adapter.business.request.assert_awaited_once()
    assert configs == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["saved", "live", "unload"])
async def test_group_name_refresh_does_not_publish_changed_account(
    page_app, headers, change
):
    app, page, _, configs, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.group_names = {"5": "已启用群"}
    entered, release = asyncio.Event(), asyncio.Event()

    async def directory(*_):
        entered.set()
        await release.wait()
        return {"owner": [], "member": [{"groupId": 7, "groupName": "不得发布"}]}

    adapter.business = SimpleNamespace(request=AsyncMock(side_effect=directory))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        task = asyncio.create_task(client.get(f"{PREFIX}/settings", headers=headers))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "saved":
                configs["fixture"]["account_id"] = "9"
            elif change == "live":
                adapter.account = "9"
            else:
                page.active = False
            release.set()
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 409
            assert adapter.group_names == {"5": "已启用群"}
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
async def test_friend_cleanup_route_is_removed_for_every_principal(page_app, headers):
    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.nim = SimpleNamespace(request=AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        for authority in ({}, headers, {"Authorization": "ApiKey fixture-key"}):
            for method in ("GET", "POST"):
                response = await client.request(
                    method,
                    f"{PREFIX}/friends",
                    headers=authority,
                    **(
                        {"params": {"bot_id": "fixture"}}
                        if method == "GET"
                        else {
                            "json": {
                                "bot_id": "fixture",
                                "action": "execute",
                                "preview_id": "a" * 32,
                                "confirmed": True,
                            }
                        }
                    ),
                )
                if not authority:
                    assert response.status_code == 401
                else:
                    assert response.status_code == 200
                    assert response.json() == {
                        "status": "error",
                        "message": "未找到该路由",
                        "data": {},
                    }
    assert not hasattr(page, "friend_cleanup")
    adapter.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_removes_old_registered_friend_handlers(page_app, headers):
    """Remove retired handlers without altering other plugin routes.

    Args:
        page_app: Isolated dashboard and registry.
        headers: Authenticated dashboard principal.
    """
    app, page, _, _, _ = page_app
    old_handler = AsyncMock()
    other = ("/other-plugin/friends", AsyncMock(), ["POST"], "Unrelated")
    page.context.registered_web_apis.extend(
        [
            ("/wangshangliao_moderation/friends", old_handler, ["GET"], "Retired"),
            ("/wangshangliao_moderation/friends", old_handler, ["POST"], "Retired"),
            other,
        ]
    )
    page.register()
    assert other in page.context.registered_web_apis
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(f"{PREFIX}/friends", headers=headers, json={})
        assert response.json()["status"] == "error"
        assert response.json()["message"] == "未找到该路由"
    old_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_friend_approval_default_on_dashboard_only_and_versioned(
    page_app, headers
):
    app, page, _, configs, _ = page_app
    page.business_settings = SimpleNamespace(log=MagicMock())
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.business = SimpleNamespace(request=AsyncMock())
    before = copy.deepcopy(configs)
    path = f"{PREFIX}/friend-approval"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        assert (await client.get(path + "?bot_id=fixture")).status_code == 401
        assert (
            await client.get(
                path + "?bot_id=fixture",
                headers={"Authorization": "ApiKey fixture-key"},
            )
        ).status_code == 403
        result = await client.get(path + "?bot_id=fixture", headers=headers)
        assert result.status_code == 200
        state = result.json()["data"]
        assert state["enabled"] is True
        assert state["accepted"] == state["pending"] == 0
        body = {
            "bot_id": "fixture",
            "revision": dashboard.revision(configs["fixture"]),
            "version": state["version"],
            "enabled": False,
        }
        saved = await client.post(path, headers=headers, json=body)
        assert saved.status_code == 200 and saved.json()["data"]["enabled"] is False
        assert (
            await client.post(path, headers=headers, json={**body, "enabled": True})
        ).status_code == 409
        assert (await client.get(path + "?bot_id=fixture", headers=headers)).json()[
            "data"
        ]["enabled"] is False
        adapter.account = "2"
        assert (
            await client.get(path + "?bot_id=fixture", headers=headers)
        ).status_code == 409
    assert configs == before
    adapter.business.request.assert_not_awaited()
    page.business_settings.log.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "true"},
        {"enabled": 1},
        {"version": True},
        {"version": 0},
        {"account_id": "2"},
        {"actor": "admin"},
        {"flId": 17},
        {"route": "/v1/user/login"},
    ],
)
async def test_friend_approval_invalid_input_cannot_mutate(page_app, headers, patch):
    app, page, _, configs, _ = page_app
    page.friend_auto.configure = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        result = await client.post(
            f"{PREFIX}/friend-approval",
            headers=headers,
            json={
                "bot_id": "fixture",
                "revision": dashboard.revision(configs["fixture"]),
                "version": 1,
                "enabled": True,
                **patch,
            },
        )
        assert result.status_code == 400
    page.friend_auto.configure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_FRIEND_PAGE_STATIC"),
    reason="Opt-in built-page browser test",
)
async def test_built_friend_approval_toggle_and_mobile_layout(page_app, headers):
    from playwright.async_api import async_playwright, expect

    app, service, _, configs, _ = page_app
    adapter = service.context.platform_manager.platform_insts[0]
    configs["fixture"]["enabled_groups"] = []
    adapter.config["_business_active"] = True
    adapter.nim_account, adapter.connection_state = "999", "online"
    adapter.get_stats = lambda: {
        "connection_state": "online",
        "business_active": True,
    }
    adapter.nim = SimpleNamespace(request=AsyncMock())
    service.business_settings = SimpleNamespace(log=MagicMock())
    static = Path(os.environ["WSL_FRIEND_PAGE_STATIC"]).read_text()
    bridge = """
    const api = async (endpoint, method, data) => {
      const url = '/api/v1/plugins/extensions/wangshangliao_moderation/' + endpoint;
      const response = await fetch(method === 'GET' ? url + '?' + new URLSearchParams(data || {}) : url,
        { method, headers: { 'Content-Type': 'application/json' },
          ...(method === 'POST' ? { body: JSON.stringify(data) } : {}) });
      const result = await response.json();
      if (!response.ok || result.status !== 'ok') throw new Error(result.message || 'Request failed');
      return result.data;
    };
    const bridge = {
      ready: async () => {}, getLocale: () => 'zh-CN', onContext: () => () => {},
      apiGet: (endpoint, data) => api(endpoint, 'GET', data),
      apiPost: (endpoint, data) => api(endpoint, 'POST', data),
    };
    Object.defineProperty(window, 'AstrBotPluginPage', { get: () => bridge, set: () => {} });
    """
    output = Path(
        os.environ.get("WSL_FRIEND_PAGE_OUTPUT", "data/temp/friends-browser-20261011")
    )
    output.mkdir(parents=True, exist_ok=True)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://fixture"
        ) as api,
        async_playwright() as playwright,
    ):
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            for width in (320, 390, 555, 760, 768, 1024, 1440):
                context = await browser.new_context(
                    viewport={"width": width, "height": 900}, reduced_motion="reduce"
                )
                await context.add_init_script(bridge)
                page = await context.new_page()
                errors = []
                requests = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))

                async def transport(route):
                    if route.request.url.endswith("/page"):
                        await route.fulfill(
                            status=200, content_type="text/html", body=static
                        )
                        return
                    requests.append(route.request.url)
                    response = await api.request(
                        route.request.method,
                        route.request.url,
                        content=route.request.post_data,
                        headers={**headers, "Content-Type": "application/json"},
                    )
                    await route.fulfill(
                        status=response.status_code,
                        content_type="application/json",
                        body=response.content,
                    )

                await page.route("https://fixture/**", transport)
                await page.goto("https://fixture/page")
                await expect(page.get_by_role("tab", name="好友清理")).to_have_count(0)
                await page.get_by_role("tab", name="好友申请").click()
                automatic = page.get_by_role("checkbox", name="自动同意好友申请")
                await expect(automatic).to_be_checked()
                await automatic.uncheck()
                await expect(automatic).not_to_be_checked()
                await automatic.check()
                await expect(automatic).to_be_checked()
                await page.get_by_role("button", name="刷新申请状态").click()
                await expect(automatic).to_be_checked()
                await expect(
                    page.get_by_role("button", name="确认删除", exact=True)
                ).to_have_count(0)
                await page.screenshot(path=str(output / f"friend-approval-{width}.png"))
                assert await page.evaluate(
                    "() => document.documentElement.scrollWidth <= innerWidth"
                )
                assert await page.locator(".friend-approval").evaluate(
                    """element => {
                      const box = element.getBoundingClientRect();
                      return box.left >= 0 && box.right <= innerWidth &&
                        [...element.querySelectorAll('button')].every(button => {
                          const rect = button.getBoundingClientRect();
                          return button.scrollWidth <= button.clientWidth &&
                            rect.left >= box.left && rect.right <= box.right;
                        });
                    }"""
                )
                assert not any("/friends" in url for url in requests)
                adapter.nim.request.assert_not_awaited()
                assert not errors
                await context.close()
            adapter.current_error, adapter.connection_state = "nim_kicked", "error"
            adapter.get_stats = lambda: {
                "connection_state": "error",
                "current_error": "nim_kicked",
                "business_active": True,
            }
            context = await browser.new_context(
                viewport={"width": 390, "height": 900}, reduced_motion="reduce"
            )
            await context.add_init_script(bridge)
            page = await context.new_page()
            await page.route("https://fixture/**", transport)
            await page.goto("https://fixture/page")
            await page.get_by_role("tab", name="好友申请").click()
            await expect(
                page.get_by_role("checkbox", name="自动同意好友申请")
            ).to_be_checked()
            await expect(page.locator(".friend-approval")).to_contain_text(
                "等待服务在线"
            )
            await page.screenshot(path=str(output / "friend-approval-offline-390.png"))
            adapter.nim.request.assert_not_awaited()
            await context.close()
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_FRIEND_PAGE_STATIC"),
    reason="Opt-in built-page browser test",
)
async def test_built_audit_names_keep_ids_escape_markup_and_fit_mobile(
    page_app, headers
):
    from playwright.async_api import async_playwright, expect

    app, service, _, _, root = page_app
    adapter = service.context.platform_manager.platform_insts[0]
    adapter.nim_account = "101"
    name = "<script>window.auditInjected=true</script>群成员名片很长也应正确换行"
    adapter.member_snapshots = {
        "5": (
            "1",
            10,
            {
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "2", "nimId": "102", "userNick": name},
                    {"userId": "3", "nimId": "103", "userNick": "群管理员"},
                ],
            },
        )
    }
    with sqlite3.connect(root / "moderation.sqlite3") as db:
        db.executescript("""
            CREATE TABLE automatic_mutes(operation,account,group_id,member,status,created,closed);
            CREATE TABLE content_violations(account,group_id,message,sender,category,status,observed);
            INSERT INTO automatic_mutes VALUES('op','1','5','2','accepted',3,0);
            INSERT INTO content_violations VALUES('1','5','m','2','message_flood','recalled',3);
            INSERT INTO content_violations VALUES('1','5','w','2','external_promotion','recalled',2);
        """)
    with sqlite3.connect(root / "messages.sqlite3") as db:
        db.execute("CREATE TABLE inbox(account,team,message,sender,payload)")
        db.execute(
            "INSERT INTO inbox VALUES('1','5','m','2',?)",
            (json.dumps({"sender": "2", "name": name}),),
        )
        db.execute(
            "INSERT INTO inbox VALUES('1','5','w','2',?)",
            (json.dumps({"sender": "2", "name": name}),),
        )
        db.executescript("""
            CREATE TABLE outbox(key PRIMARY KEY,state);
            CREATE TABLE reply_recalls(key PRIMARY KEY,account,group_id,server_id,client,peer,state,sender);
            CREATE TABLE observed_recalls(account,group_id,message_id,client,author,operator,recalled_at,received_at);
            INSERT INTO outbox VALUES('open-warning/1/5/w','accepted');
            INSERT INTO reply_recalls VALUES('open-warning/1/5/w','1','5','warning','c','101','accepted','');
            INSERT INTO observed_recalls VALUES('1','5','m','c','102','103','1000',1001);
        """)
    static = Path(os.environ["WSL_FRIEND_PAGE_STATIC"]).read_text()
    output = Path(os.environ.get("WSL_FRIEND_PAGE_OUTPUT", "data/temp/audit-browser"))
    output.mkdir(parents=True, exist_ok=True)
    bridge = """
      const api = async (endpoint, method, data) => {
        const url = '/api/v1/plugins/extensions/wangshangliao_moderation/' + endpoint;
        const response = await fetch(method === 'GET' ? url + '?' + new URLSearchParams(data || {}) : url,
          {method, headers: {'Content-Type': 'application/json'},
           ...(method === 'POST' ? {body: JSON.stringify(data)} : {})});
        const result = await response.json();
        if (!response.ok || result.status !== 'ok') throw new Error(result.message || 'Request failed');
        return result.data;
      };
      const bridge = {
        ready: async () => {}, getLocale: () => 'zh-CN', onContext: () => () => {},
        apiGet: (endpoint, data) => api(endpoint, 'GET', data),
        apiPost: (endpoint, data) => api(endpoint, 'POST', data),
      };
      Object.defineProperty(window, 'AstrBotPluginPage', {get: () => bridge, set: () => {}});
    """
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://fixture"
        ) as api,
        async_playwright() as playwright,
    ):
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            for width in (320, 390, 1440):
                if width == 1440:
                    adapter.member_snapshots.clear()
                context = await browser.new_context(
                    viewport={"width": width, "height": 1000}
                )
                await context.add_init_script(bridge)
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))

                async def transport(route):
                    if route.request.url.endswith("/page"):
                        await route.fulfill(
                            status=200, content_type="text/html", body=static
                        )
                        return
                    response = await api.request(
                        route.request.method,
                        route.request.url,
                        content=route.request.post_data,
                        headers={**headers, "Content-Type": "application/json"},
                    )
                    await route.fulfill(
                        status=response.status_code,
                        content_type="application/json",
                        body=response.content,
                    )

                await page.route("https://fixture/**", transport)
                await page.goto("https://fixture/page")
                await page.get_by_role("tab", name="运行与日志", exact=True).click()
                await page.get_by_role("tab", name="处罚记录", exact=True).click()
                panel = page.locator("#panel-audit")
                await expect(panel.locator(".audit-member")).to_have_count(6)
                for cell in (await panel.locator(".audit-member").all())[:4]:
                    await expect(cell.locator("span")).to_have_text(name)
                    await expect(cell.locator("small.mono")).to_have_text("2")
                    assert await cell.locator("span").evaluate(
                        "e => e.scrollWidth <= e.clientWidth"
                    )
                await expect(panel).to_contain_text("刷屏")
                await expect(panel).to_contain_text("已撤回")
                await expect(panel).to_contain_text("警告曾被自动清理")
                await expect(panel).to_contain_text("已发送（平台受理）")
                await expect(panel).to_contain_text("其他成员撤回")
                assert "message_flood" not in await panel.inner_text()
                assert "recalled" not in await panel.inner_text()
                if width == 1440:
                    assert await panel.get_by_text("历史名称", exact=True).count() == 4
                assert await page.evaluate("() => window.auditInjected") is None
                assert await page.evaluate(
                    "() => document.documentElement.scrollWidth <= innerWidth"
                )
                assert not errors
                await panel.screenshot(path=str(output / f"audit-names-{width}.png"))
                await context.close()
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"action": []},
        {"action": "execute", "preview_id": {}},
        {"action": "execute", "preview_id": "123", "confirmed": True},
        {"action": "preview", "peers": []},
        {"action": "preview", "peers": [""]},
        {"action": "preview", "peers": [123]},
        {"action": "preview", "peers": "123"},
        {"action": "preview", "peers": ["123", "456"]},
        {"action": "preview", "peers": [str(i) for i in range(100)]},
        {"action": "preview", "peers": [str(i) for i in range(200)]},
        {"action": "preview", "peers": [str(i) for i in range(300)]},
        {"action": "preview", "peers": ["123"], "batch_size": 100},
        {"action": "preview", "peers": ["123"], "confirmed": True},
        {
            "action": "execute",
            "preview_id": "a" * 32,
            "confirmed": True,
            "peers": ["123", "456"],
        },
        {"action": "accept", "peers": ["123"]},
        {"action": "auto_accept", "peers": ["123"]},
    ],
)
async def test_old_friend_inputs_have_no_handler(page_app, headers, payload):
    app, page, _, configs, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        result = await client.post(
            f"{PREFIX}/friends",
            headers=headers,
            json={
                "bot_id": "fixture",
                "revision": dashboard.revision(configs["fixture"]),
                **payload,
            },
        )
    assert result.status_code == 200
    assert result.json() == {"status": "error", "message": "未找到该路由", "data": {}}
    assert not hasattr(page, "friend_cleanup")


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
async def test_activity_draft_can_round_trip_empty_factory_fields(page_app, headers):
    app, _, _, _, root = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        endpoint = f"{PREFIX}/activities?bot_id=fixture&group_id=5"
        original = (await client.get(endpoint, headers=headers)).json()["data"]
        assert original["lottery"]["prize"] == original["lottery"]["contact"] == ""
        payload = {
            "bot_id": "fixture",
            "group_id": "5",
            "revision": original["revision"],
            "lottery": {**original["lottery"], "prize": "Test", "contact": "Admin"},
            "invite_rate": original["invite_rate"],
        }
        configured = await client.post(
            f"{PREFIX}/activities", headers=headers, json=payload
        )
        assert configured.status_code == 200
        payload.update(
            revision=configured.json()["data"]["revision"],
            lottery=original["lottery"],
        )
        restored = await client.post(
            f"{PREFIX}/activities", headers=headers, json=payload
        )
        assert restored.status_code == 200
        result = restored.json()["data"]
        assert result["lottery"] == original["lottery"]
        assert result["invite_rate"] == original["invite_rate"]
        for invalid in (" ", "\n", "\x00", " text ", "a" * 513):
            rejected = await client.post(
                f"{PREFIX}/activities",
                headers=headers,
                json={
                    **payload,
                    "revision": result["revision"],
                    "lottery": {**original["lottery"], "prize": invalid},
                },
            )
            assert rejected.status_code == 400
    with sqlite3.connect(root / "activities.sqlite3") as db:
        assert db.execute(
            "SELECT prize,contact FROM lottery_settings WHERE account='1' AND group_id='5'"
        ).fetchone() == ("", "")
        assert db.execute("SELECT COUNT(*) FROM lotteries").fetchone() == (0,)


@pytest.mark.asyncio
async def test_activity_controls_read_and_save_scoped_values(page_app, headers):
    app, _, service, configs, root = page_app
    adapter = app.state.core_lifecycle.star_context.platform_manager.platform_insts[0]
    with activity_store.database(adapter) as db:
        db.execute(
            "INSERT INTO invite_credits VALUES(?,?,?,?,?,?,?,?)",
            ("1", "5", "10", "2", "peer-10", "peer-2", 125, 1),
        )
        db.execute(
            "INSERT INTO invite_rules(account,group_id,presence_checked_at) "
            "VALUES('1','5',1)"
        )
        db.executemany(
            "INSERT INTO invite_seen(account,group_id,member,status,left_at) "
            "VALUES(?,?,?,?,?)",
            [
                ("1", "5", "10", "credited", 0),
                ("1", "5", "11", "credited", 1),
            ],
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
        assert data["invitation"]["active_count"] == 1
        assert data["invitation"]["left_count"] == 1
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
    assert result["lottery"] == {
        **payload["lottery"],
        "total_amount": "0.00",
        "packet_mode": "prize",
        "by_invites": True,
        "remarks": "",
    }
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
        scope_revision = state.json()["data"]["revision"]
        stale = await client.post(
            f"{PREFIX}/schedule",
            headers=headers,
            json={
                **params,
                "version": 3,
                "action": "pause",
                "revision": scope_revision,
            },
        )
        assert stale.status_code == 409
        paused = await client.post(
            f"{PREFIX}/schedule",
            headers=headers,
            json={
                **params,
                "version": 4,
                "action": "pause",
                "revision": scope_revision,
            },
        )
        assert paused.status_code == 200
        state = await client.get(f"{PREFIX}/schedule", params=params, headers=headers)
    assert state.json()["data"]["schedule"]["status"] == "paused"
    assert state.json()["data"]["schedule"]["version"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "group", "missing_revision"])
async def test_schedule_pause_requires_original_scope(page_app, headers, change):
    app, page, _, configs, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    with schedule_store.database(adapter) as db:
        for account, group in (("1", "5"), ("2", "5"), ("1", "6")):
            db.execute(
                "INSERT INTO schedules(account,group_id,owner,session,start,end,zone,"
                "version,status,next_at,next_action,error) "
                "VALUES(?,?,'9','session','23:00','08:00','Asia/Shanghai',4,"
                "'active',200,'mute_all','')",
                (account, group),
            )
    configs["fixture"]["enabled_groups"].append("6")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        state = await client.get(
            f"{PREFIX}/schedule?bot_id=fixture&group_id=5", headers=headers
        )
        payload = {
            "bot_id": "fixture",
            "group_id": "5",
            "version": 4,
            "action": "pause",
            "revision": state.json()["data"]["revision"],
        }
        if change == "account":
            configs["fixture"]["account_id"] = "2"
            adapter.account = "2"
        elif change == "group":
            payload["group_id"] = "6"
        else:
            del payload["revision"]
        response = await client.post(
            f"{PREFIX}/schedule", headers=headers, json=payload
        )
    assert response.status_code == (400 if change == "missing_revision" else 409)
    with schedule_store.database(adapter) as db:
        assert db.execute(
            "SELECT DISTINCT status,version FROM schedules"
        ).fetchall() == [("active", 4)]


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
    service.update_bot.assert_not_awaited()


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
        "recalls": [],
        "recalls_available": False,
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
    assert row["minutes"] == 3 and row["status"] == "accepted"
    assert row["recall_status"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["accepted", "verified"], "accepted"),
        (["accepted", "unknown"], "unknown"),
        (["accepted", "rejected"], "unknown"),
        ([], "none"),
    ],
)
async def test_flood_audit_aggregates_only_the_original_burst(
    page_app, headers, statuses, expected
):
    app, _, _, _, root = page_app
    with sqlite3.connect(root / "moderation.sqlite3") as db:
        db.executescript("""
            CREATE TABLE automatic_mutes(operation,account,group_id,member,status,created,closed);
            CREATE TABLE flood_recalls(account,group_id,message,batch,status);
        """)
        db.execute(
            "INSERT INTO automatic_mutes VALUES('flood/op','1','5','2','accepted',1,0)"
        )
        db.executemany(
            "INSERT INTO flood_recalls VALUES('1','5',?,'flood/op',?)",
            [(str(n), status) for n, status in enumerate(statuses)],
        )
        db.execute(
            "INSERT INTO flood_recalls VALUES('9','5','other','flood/op','unknown')"
        )
        db.execute(
            "INSERT INTO flood_recalls VALUES('1','6','other','flood/op','unknown')"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        result = await client.get(
            f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers
        )
    assert result.status_code == 200
    sanction = result.json()["data"]["sanctions"][0]
    assert sanction["status"] == "accepted"
    assert sanction["recall_status"] == expected


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
async def test_warning_audit_uses_native_send_not_legacy_recall_field(
    page_app, headers
):
    app, _, _, _, root = page_app
    with sqlite3.connect(root / "moderation.sqlite3") as db:
        db.executescript("""
            CREATE TABLE content_violations(account,group_id,message,sender,category,status,observed,warning);
            CREATE TABLE progressive_actions(account,group_id,message,recall_status,result,created);
            INSERT INTO content_violations VALUES('1','5','a','2','external_promotion','recalled',3,'accepted');
            INSERT INTO content_violations VALUES('1','5','b','3','external_promotion','review_only',2,'accepted');
            INSERT INTO content_violations VALUES('1','5','c','4','message_flood','muted',1,'accepted');
            INSERT INTO progressive_actions VALUES('1','5','a','accepted','{"warning_number":1}',3);
        """)
    native = root / "messages.sqlite3"
    with sqlite3.connect(native) as db:
        db.executescript("""
            CREATE TABLE inbox(account,team,message,sender,payload);
            CREATE TABLE outbox(key PRIMARY KEY,state);
            CREATE TABLE reply_recalls(key PRIMARY KEY,account,group_id,state);
            INSERT INTO outbox VALUES('open-warning/1/5/a','accepted');
            INSERT INTO outbox VALUES('open-warning/9/5/b','accepted');
            INSERT INTO outbox VALUES('recall/1/5/b','accepted');
            INSERT INTO reply_recalls VALUES('open-warning/1/5/a','1','5','accepted');
        """)
    before = native.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers
        )
    assert response.status_code == 200
    a, b, c = response.json()["data"]["rules"]
    assert a["warning_number"] == 1
    assert a["warning_status"] == "sent"
    assert a["warning_cleanup_status"] == "accepted"
    assert b["warning_status"] == "not_recorded"
    assert b["status"] == "review_only" and b["recall_status"] == "accepted"
    assert c["warning_status"] == "not_applicable"
    assert native.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", ["trusted", "partial", "duplicate"])
async def test_recall_sources_are_scoped_deduplicated_and_receipt_based(
    page_app, headers, directory
):
    app, page, _, _, root = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.nim_account = "101"
    members = [
        {"userId": "1", "nimId": "101", "userNick": "Bot"},
        {"userId": "2", "nimId": "102", "userNick": "Author"},
        {"userId": "3", "nimId": "103", "userNick": "Human admin"},
    ]
    if directory == "duplicate":
        members.append({"userId": "4", "nimId": "103", "userNick": "Conflict"})
    adapter.member_snapshots = {
        "5": ("1", 10, {"complete": directory != "partial", "groupMemberInfo": members})
    }
    native = root / "messages.sqlite3"
    with sqlite3.connect(native) as db:
        db.executescript("""
            CREATE TABLE observed_recalls(
                account,group_id,notice_id,message_id,client,author,operator,recalled_at,received_at);
            CREATE TABLE outbox(key PRIMARY KEY,state);
            CREATE TABLE reply_recalls(
                key PRIMARY KEY,account,group_id,server_id,client,peer,state,sender);
            INSERT INTO outbox VALUES('recall/1/5/rule','accepted');
            INSERT INTO outbox VALUES('recall/1/5/failed','unknown');
            INSERT INTO outbox VALUES('recall/9/5/wrong-account','accepted');
            INSERT INTO outbox VALUES('recall/1/6/wrong-group','accepted');
            INSERT INTO reply_recalls VALUES('recall/1/5/command','1','5','command','c','102','accepted','2');
            INSERT INTO reply_recalls VALUES('open-warning/1/5/a','1','5','reply','c','101','accepted','');
            INSERT INTO reply_recalls VALUES('wrong-route','1','5','mismatch','different','102','accepted','');
        """)
        for index, (mid, author, operator) in enumerate(
            [
                ("rule", "102", "101"),
                ("command", "102", "101"),
                ("reply", "101", "101"),
                ("self", "102", "102"),
                ("human", "102", "103"),
                ("failed", "102", "101"),
                ("wrong-account", "102", "101"),
                ("wrong-group", "102", "101"),
                ("mismatch", "102", "101"),
                ("unidentified", "102", ""),
            ]
        ):
            db.execute(
                "INSERT INTO observed_recalls VALUES('1','5',?,?, 'c',?,?,?,?)",
                (str(index), mid, author, operator, str(1000 + index), 2000 + index),
            )
        db.executescript("""
            INSERT INTO observed_recalls VALUES('1','5','duplicate','human','c','102','103','1004',3000);
            INSERT INTO observed_recalls VALUES('9','5','foreign-account','foreign','c','102','103','1004',3000);
            INSERT INTO observed_recalls VALUES('1','6','foreign-group','foreign','c','102','103','1004',3000);
        """)
    before = native.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers
        )
    assert response.status_code == 200
    data = response.json()["data"]
    rows = {row["message"]: row for row in data["recalls"]}
    assert len(data["recalls"]) == len(rows) == 10
    assert data["rules"] == [] and data["recalls_available"]
    assert rows["rule"]["source"] == "bot_rule_recall"
    assert rows["command"]["source"] == "bot_command_cleanup"
    assert rows["reply"]["source"] == "bot_reply_cleanup"
    assert rows["self"]["source"] == "self_recall"
    assert rows["human"]["source"] == "other_member_recall"
    for mid in ("failed", "wrong-account", "wrong-group", "mismatch"):
        assert rows[mid]["source"] == "bot_account_unattributed"
        assert "receipt_status" not in rows[mid]
    assert rows["unidentified"]["source"] == "unattributed"
    assert rows["human"].get("operator_name") == (
        "Human admin" if directory == "trusted" else None
    )
    assert [row["recalled_at"] for row in data["recalls"]] == list(range(1009, 999, -1))
    assert native.read_bytes() == before
    assert not (root / "moderation.sqlite3").exists()


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
@pytest.mark.parametrize(
    "source",
    ["scoped", "other_account", "other_group", "partial", "duplicate", "offline"],
)
async def test_audit_names_are_display_only_and_never_cross_directory_scope(
    page_app, headers, source
):
    app, page, service, _, root = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    name = "<script>alert(1)</script> A very long group display name"
    members = [{"userId": "2", "nimId": "102", "userNick": name}]
    if source == "duplicate":
        members.append({"userId": "2", "userNick": "Conflicting identity"})
    adapter.member_snapshots = {
        "6" if source == "other_group" else "5": (
            "9" if source == "other_account" else "1",
            10,
            {"complete": source != "partial", "groupMemberInfo": members},
        )
    }
    adapter.get_moderation_members = AsyncMock(side_effect=AssertionError("No HTTP"))
    if source == "offline":
        page.context.platform_manager.platform_insts.clear()
    path = root / "moderation.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE automatic_mutes(operation,account,group_id,member,status,created,closed);
            CREATE TABLE content_violations(account,group_id,message,sender,category,status,observed);
            INSERT INTO automatic_mutes VALUES('op','1','5','2','accepted',3,0);
            INSERT INTO content_violations VALUES('1','5','m','2','external_promotion','recalled',3);
        """)
    before = path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers
        )
    assert response.status_code == 200
    result = response.json()["data"]
    for field, key in (
        ("counts", "member"),
        ("sanctions", "member"),
        ("rules", "sender"),
    ):
        assert result[field][0][key] == "2"
        assert result[field][0].get(f"{key}_name") == (
            name if source == "scoped" else None
        )
    assert path.read_bytes() == before
    adapter.get_moderation_members.assert_not_awaited()
    service.update_bot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "message",
        "other_account",
        "other_group",
        "other_sender",
        "body_sender",
        "corrupt",
        "unrelated",
    ],
)
async def test_audit_historical_names_use_only_exact_scoped_original_messages(
    page_app, headers, source
):
    app, page, _, _, root = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.member_snapshots = {}
    name = "Retained display name"
    with sqlite3.connect(root / "moderation.sqlite3") as db:
        db.executescript("""
            CREATE TABLE automatic_mutes(operation,account,group_id,member,status,created,closed);
            CREATE TABLE content_violations(account,group_id,message,sender,category,status,observed);
            INSERT INTO automatic_mutes VALUES('op','1','5','2','accepted',3,0);
            INSERT INTO content_violations VALUES('1','5','m','2','external_promotion','recalled',3);
        """)
    inbox = root / "messages.sqlite3"
    with sqlite3.connect(inbox) as db:
        db.execute("CREATE TABLE inbox(account,team,message,sender,payload)")
        db.execute(
            "INSERT INTO inbox VALUES(?,?,?,?,?)",
            (
                "9" if source == "other_account" else "1",
                "6" if source == "other_group" else "5",
                "other-message" if source == "unrelated" else "m",
                "3" if source == "other_sender" else "2",
                "corrupt"
                if source == "corrupt"
                else json.dumps(
                    {"sender": "3" if source == "body_sender" else "2", "name": name}
                ),
            ),
        )
    before = inbox.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers
        )
    assert response.status_code == 200
    result = response.json()["data"]
    for field, key in (
        ("counts", "member"),
        ("sanctions", "member"),
        ("rules", "sender"),
    ):
        row = result[field][0]
        assert row[key] == "2"
        assert row.get(f"{key}_name") == (name if source == "message" else None)
        assert row.get(f"{key}_name_source") == (
            "message" if source == "message" else None
        )
    assert inbox.read_bytes() == before


@pytest.mark.asyncio
async def test_audit_name_read_rejects_account_change_while_waiting(page_app, headers):
    app, _, _, configs, root = page_app
    with sqlite3.connect(root / "moderation.sqlite3") as db:
        db.executescript("""
            CREATE TABLE content_violations(account,group_id,message,sender,category,status,observed);
            INSERT INTO content_violations VALUES('1','5','m','2','external_promotion','recalled',3);
        """)
    blocker = sqlite3.connect(root / "messages.sqlite3")
    blocker.execute("CREATE TABLE inbox(account,team,message,sender,payload)")
    blocker.commit()
    blocker.execute("BEGIN EXCLUSIVE")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        task = asyncio.create_task(
            client.get(f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers)
        )
        try:
            await asyncio.sleep(0.05)
            assert not task.done()
            configs["fixture"]["account_id"] = "9"
            blocker.rollback()
            response = await asyncio.wait_for(task, 3)
            assert response.status_code == 409
        finally:
            blocker.rollback()
            blocker.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["rejected", "rejected"], "rejected"),
        (["accepted", "verified"], "accepted"),
        (["rejected", "unknown"], "unknown"),
        (["accepted", "rejected"], "unknown"),
    ],
)
async def test_audit_flood_aggregate_preserves_explicit_rejections(
    page_app, headers, statuses, expected
):
    app, _, _, _, root = page_app
    with sqlite3.connect(root / "moderation.sqlite3") as db:
        db.executescript("""
            CREATE TABLE automatic_mutes(operation,account,group_id,member,status,created,closed);
            CREATE TABLE flood_recalls(batch,account,group_id,message,status);
            INSERT INTO automatic_mutes VALUES('flood/op','1','5','2','rejected',3,0);
        """)
        db.executemany(
            "INSERT INTO flood_recalls VALUES('flood/op','1','5',?,?)",
            [(str(i), status) for i, status in enumerate(statuses)],
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            f"{PREFIX}/audit?bot_id=fixture&group_id=5", headers=headers
        )
    assert response.status_code == 200
    assert response.json()["data"]["sanctions"][0]["recall_status"] == expected


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
