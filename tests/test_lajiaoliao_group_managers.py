import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_businesses import prepared_plugin, settings
from test_lajiaoliao_plugin_api import call

from astrbot.core.platform.instance_control import instance_revision
from data.plugins.astrbot_plugin_lajiaoliao.business import (
    DEFAULTS,
    BusinessStore,
    validate_settings,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def chat(config, uid="delegate", group=""):
    return SimpleNamespace(
        platform=SimpleNamespace(session_ref=config["session_ref"], config=config),
        is_private_chat=lambda: not group,
        is_admin=lambda: False,
        get_sender_id=lambda: uid,
        get_group_id=lambda: group,
        get_platform_name=lambda: "lajiaoliao",
        get_extra=lambda _: None,
        unified_msg_origin="private-" + uid if not group else "group-" + group,
    )


@pytest.mark.parametrize("managers", [
    "uid", [1], [""], [" uid"], ["uid\n"], ["uid", "uid"], ["用戶"],
    [str(i) for i in range(101)],
])
def test_invalid_manager_grants_rejected(managers):
    with pytest.raises(ValueError, match="group_managers"):
        validate_settings(settings(manager_user_ids=managers))


def test_old_settings_default_and_grants_persist_isolated(tmp_path):
    one, two = BusinessStore(tmp_path / "one"), BusinessStore(tmp_path / "two")
    old = copy.deepcopy(DEFAULTS)
    old.pop("manager_user_ids")
    with one.connect() as db:
        db.execute("INSERT INTO settings VALUES(?,?,?)", ("g", 1, json.dumps(old)))
    assert one.settings("g")["settings"]["manager_user_ids"] == []
    one.save_settings("g", 1, settings(manager_user_ids=["delegate"]), "dashboard:owner")
    assert one.managed_groups("delegate") == ["g"]
    assert not two.managed_groups("delegate")
    assert not one.settings("other")["settings"]["manager_user_ids"]
    assert BusinessStore(tmp_path / "one").managed_groups("delegate") == ["g"]


def test_grants_require_owned_group_current_version_and_real_nonbot_member(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        account = plugin.store._read(ref)["account_id"]
        plugin.business.roster = AsyncMock(return_value={"delegate": {}, account: {}})
        body = {
            "session_ref": ref, "group_id": "g", "action": "save-managers",
            "confirm": True, "manager_user_ids": ["delegate"], "version": 0,
            "instance_revision": instance_revision(config),
        }
        assert (await call(plugin, "business_page", "bob", body))[0] == 403
        assert (await call(plugin, "business_page", "alice", {**body, "group_id": "other"}))[0] == 403
        assert (await call(plugin, "business_page", "alice", {**body, "instance_revision": "old"}))[0] == 409
        for uid in ("outsider", account):
            result = await call(plugin, "business_page", "alice", {**body, "manager_user_ids": [uid]})
            assert result[0] == 409 and result[1]["message"] == "group_manager_member_required"
        assert (await call(plugin, "business_page", "alice", body))[0] == 200
        assert (await call(plugin, "business_page", "alice", body))[0] == 409
        assert (await plugin.business.settings(ref, "g"))["settings"]["manager_user_ids"] == ["delegate"]
        assert plugin.context.get_config()["admins_id"] == ["admin"]
        assert plugin.business.stores[ref].audit("g")[0]["subject"] == "dashboard:alice"
        instance.send_parts.assert_not_awaited()
        # Removing a no-longer-present member needs no roster or upstream effect.
        plugin.business.roster.reset_mock()
        plugin.business.roster.side_effect = RuntimeError("offline")
        assert (await call(plugin, "business_page", "alice", {
            **body, "version": 1, "manager_user_ids": [],
        }))[0] == 200
        plugin.business.roster.assert_not_awaited()
        assert not await plugin.business.group_administrator(ref, "g", "delegate", "private")
    asyncio.run(run())


def test_delegate_private_commands_scope_identity_and_stale_selection(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        config["message_policy"]["group_ids"].append("other")
        await plugin.business.db(ref, "save_settings", "g", 0, settings(manager_user_ids=["delegate"]), "owner")
        plugin.manager.request.return_value = {"groups": [
            {"groupID": "g", "groupName": "Allowed"},
            {"groupID": "other", "groupName": "Not delegated"},
        ]}
        event = chat(config)
        listing = await plugin.commands.execute(event, "群列表")
        assert "Allowed" in listing and "Not delegated" not in listing
        await plugin.commands.execute(event, "选择群", "1")
        assert "本群机器人管理员" in await plugin.commands.execute(event, "我的权限")
        assert not event.is_admin()
        assert not plugin.business.administrator("delegate", event.unified_msg_origin)
        assert "普通用户" in await plugin.commands.execute(chat(config, "ordinary", "g"), "我的权限")
        assert "普通用户" in await plugin.commands.execute(chat(config, "delegate", "other"), "我的权限")
        assert await plugin.commands.execute(chat(config, "ordinary"), "帮助") == "无权限"
        state = plugin.commands.selection(event)
        state["groups"].append({"groupID": "other"})
        with pytest.raises(SessionError, match="chat_admin_required"):
            await plugin.commands.execute(event, "选择群", "2")
        await plugin.business.db(ref, "save_settings", "g", 1, settings(), "owner")
        assert await plugin.commands.execute(event, "规则") == "无权限"
        assert "普通用户" in await plugin.commands.execute(event, "我的权限")
        assert not await plugin.business.accessible_groups(ref, "delegate", event.unified_msg_origin)
    asyncio.run(run())


def test_chat_and_ai_cannot_redelegate_or_gain_global_tools(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(ref, "save_settings", "g", 0, settings(manager_user_ids=["delegate"]), "owner")
        event = chat(config)
        plugin.commands.directories[(ref, "delegate", event.unified_msg_origin)] = {
            "selected": "g", "expires": time.time() + 600,
        }
        proposal = settings(manager_user_ids=["delegate", "outsider"])
        draft = await plugin.business.db(ref, "preview_settings", "g", "delegate", event.unified_msg_origin, proposal, 1)
        with pytest.raises(SessionError, match="dashboard_manager_grants_required"):
            await plugin.commands.execute(event, "确认设置", draft)
        assert json.loads(await plugin.private_management(event, "groups", {}))["status"] == "rejected"
        assert (await plugin.business.settings(ref, "g"))["settings"]["manager_user_ids"] == ["delegate"]
        event.get_extra = lambda _: False
        assert await plugin.commands.execute(event, "帮助") == "无权限"
        assert "普通用户" in await plugin.commands.execute(event, "我的权限")
    asyncio.run(run())


def test_delegate_native_preview_revocation_and_protected_manager(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        account = plugin.store._read(ref)["account_id"]
        value = settings(manager_user_ids=["delegate"], native_actions=["mute"])
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        plugin.business.roster = AsyncMock(return_value={
            account: {"roleLevel": 100}, "delegate": {"roleLevel": 20}, "ordinary": {"roleLevel": 20},
        })
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        event = chat(config)
        event.platform.platform_action = instance.platform_action
        plugin.commands.directories[(ref, "delegate", event.unified_msg_origin)] = {
            "selected": "g", "expires": time.time() + 600,
        }
        with pytest.raises(SessionError, match="manual_group_actions_removed"):
            await plugin.commands.execute(event, "禁言", "delegate 1")
        with pytest.raises(SessionError, match="manual_group_actions_removed"):
            await plugin.commands.execute(event, "禁言", "ordinary 1")
        instance.platform_action.assert_not_awaited()
        with pytest.raises(SessionError, match="manual_group_actions_removed"):
            await plugin.commands.execute(event, "确认操作", "old-preview")
        value["manager_user_ids"] = []
        await plugin.business.db(ref, "save_settings", "g", 1, value, "owner")
        assert await plugin.commands.execute(event, "确认操作", "old-preview") == "无权限"
        instance.platform_action.assert_not_awaited()
    asyncio.run(run())


def test_manager_grant_still_requires_native_role_and_action_grant(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        account = plugin.store._read(ref)["account_id"]
        await plugin.business.db(ref, "save_settings", "g", 0, settings(manager_user_ids=["delegate"]), "owner")
        plugin.business.roster = AsyncMock(return_value={account: {"roleLevel": 20}, "ordinary": {"roleLevel": 20}})
        event = chat(config)
        plugin.commands.directories[(ref, "delegate", event.unified_msg_origin)] = {"selected": "g", "expires": time.time() + 600}
        with pytest.raises(SessionError, match="manual_group_actions_removed"):
            await plugin.commands.execute(event, "禁言", "ordinary 1")
        await plugin.business.db(ref, "save_settings", "g", 1, settings(manager_user_ids=["delegate"], native_actions=["mute"]), "owner")
        with pytest.raises(SessionError, match="manual_group_actions_removed"):
            await plugin.commands.execute(event, "禁言", "ordinary 1")
        instance.send_parts.assert_not_awaited()
    asyncio.run(run())


def test_manager_reminder_is_paused_by_grant_revocation(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(ref, "save_settings", "g", 0, settings(manager_user_ids=["delegate"], reminders_enabled=True), "owner")
        event = chat(config)
        plugin.commands.directories[(ref, "delegate", event.unified_msg_origin)] = {"selected": "g", "expires": time.time() + 600}
        with pytest.raises(SessionError, match="manual_group_actions_removed"):
            await plugin.commands.execute(event, "定时通知", "23:59 test")
        code = await plugin.business.db(ref, "preview_reminder", "g", "delegate", event.unified_msg_origin, "23:59", "test", 1)
        await plugin.business.db(ref, "confirm_reminder", "g", "delegate", event.unified_msg_origin, code, 1)
        assert (await plugin.business.db(ref, "reminders", "g"))[0]["state"] == "active"
        await plugin.business.db(ref, "save_settings", "g", 1, settings(reminders_enabled=True), "owner")
        assert (await plugin.business.db(ref, "reminders", "g"))[0]["state"] == "paused"
        assert not (await plugin.business.db(ref, "due", time.time() + 86400))[0]
        instance.send_parts.assert_not_awaited()
    asyncio.run(run())


def test_group_users_reports_native_roles_separately_and_checks_owner(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        account = plugin.store._read(ref)["account_id"]
        await plugin.business.db(ref, "save_settings", "g", 0, settings(manager_user_ids=["delegate"]), "owner")
        plugin.manager.directory.return_value = {"members": [
            {"groupID": "g", "userID": uid, "nickname": uid, "roleLevel": role}
            for uid, role in [(account, 20), ("admin", 20), ("delegate", 20), ("native-owner", 100)]
        ]}
        body = {"session_ref": ref, "group_id": "g"}
        assert (await call(plugin, "group_users", "bob", body))[0] == 403
        status, data = await call(plugin, "group_users", "alice", body)
        assert status == 200
        assert {m["user_id"]: m["identity"] for m in data["members"]} == {
            account: "bot", "admin": "astrbot_admin", "delegate": "group_manager", "native-owner": "member",
        }
        assert not data["has_more"]
        plugin.manager.directory.return_value = {"members": [{"groupID": "other", "userID": "u"}]}
        assert (await call(plugin, "group_users", "alice", body))[0] == 409
    asyncio.run(run())


def test_revoked_creator_cancels_lottery_and_plugin_exclusion_blocks_delegation(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(ref, "save_settings", "g", 0, settings(
            lottery_enabled=True, manager_user_ids=["delegate"]
        ), "owner")
        plugin.business.roster = AsyncMock(return_value={"member": {"roleLevel": 20}})
        identifier, state = await plugin.business.start_lottery(ref, "g", "delegate", "private")
        assert state == "open"
        assert await plugin.business.group_administrator(ref, "g", "delegate", "private")
        await plugin.business.db(ref, "save_settings", "g", 1, settings(lottery_enabled=True), "owner")
        assert not await plugin.business.group_administrator(ref, "g", "delegate", "private")
        assert (await plugin.business.db(ref, "activities", "g"))[0]["state"] == "cancelled"
        instance.send_parts.assert_awaited_once()
        await plugin.business.db(ref, "save_settings", "g", 2, settings(manager_user_ids=["delegate"]), "owner")
        plugin.context.get_config = lambda umo=None: {"platform": [config], "admins_id": ["admin"], "plugin_set": []}
        assert not await plugin.business.group_administrator(ref, "g", "delegate", "private")
        assert not await plugin.business.accessible_groups(ref, "delegate", "private")
        assert identifier
    asyncio.run(run())
