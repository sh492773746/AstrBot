"""Per-group chat gates, delegated private scopes and bounded work admission."""

import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_lajiaoliao_businesses import prepared_plugin, settings
from test_lajiaoliao_group_managers import chat
from test_lajiaoliao_plugin_api import call

from astrbot.core.platform.instance_control import instance_revision
from astrbot.core.platform.platform_metadata import PlatformMetadata
from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS, BusinessStore
from data.plugins.astrbot_plugin_lajiaoliao.event import (
    LajiaoLiaoEvent,
    incoming_to_message,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def test_legacy_chat_preserved_new_groups_off_commands_on(tmp_path):
    store = BusinessStore(tmp_path)
    legacy = copy.deepcopy(DEFAULTS)
    legacy.pop("group_ai_enabled")
    legacy.pop("commands_enabled")
    with store.connect() as db:
        db.execute("INSERT INTO settings VALUES(?,?,?)", ("old", 1, json.dumps(legacy)))
    assert store.settings("old")["settings"]["group_ai_enabled"]
    assert store.settings("old")["settings"]["commands_enabled"]
    assert not store.settings("new")["settings"]["group_ai_enabled"]
    assert store.settings("new")["settings"]["commands_enabled"]


@pytest.mark.asyncio
async def test_old_dashboard_save_preserves_explicit_new_switches(
    tmp_path, monkeypatch
):
    plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
    ref = config["session_ref"]
    value = settings(group_ai_enabled=False, commands_enabled=False)
    await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
    submitted = copy.deepcopy(value)
    submitted.pop("group_ai_enabled")
    submitted.pop("commands_enabled")
    status, response = await call(
        plugin,
        "business_page",
        "alice",
        {
            "session_ref": ref,
            "group_id": "g",
            "action": "save",
            "version": 1,
            "confirm": True,
            "instance_revision": instance_revision(config),
            "settings": submitted,
        },
    )
    assert status == 200
    assert not response["settings"]["group_ai_enabled"]
    assert not response["settings"]["commands_enabled"]
    await plugin.business.close()


@pytest.mark.asyncio
async def test_group_chat_toggle_core_profile_and_command_independence(
    tmp_path, monkeypatch
):
    plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
    ref = config["session_ref"]
    plugin.logger = SimpleNamespace(warning=Mock())
    instance.meta = lambda: PlatformMetadata("lajiaoliao", "test", "one")
    instance.session_ref = ref
    instance.member_ids = {}
    original_config = plugin.context.get_config
    enabled = True
    plugin.context.get_config = lambda umo=None: {
        **original_config(umo),
        "provider_settings": {"enable": enabled},
    }

    def event(text="hello"):
        payload = {
            "message_id": "msg-" + text,
            "sender_id": "ordinary",
            "target_id": "g",
            "kind": "group",
            "text": text,
            "timestamp": time.time(),
        }
        result = LajiaoLiaoEvent(incoming_to_message(payload, "account-one"), instance)
        result.is_at_or_wake_command = True
        return result

    off = event()
    await plugin.group_businesses(off)
    assert not off.is_at_or_wake_command and not off.is_stopped()
    # Rules work without AI or a model and never fall through to an LLM.
    command = event("排名")
    await plugin.group_businesses(command)
    assert command.is_stopped() and not command.is_at_or_wake_command
    instance.send_parts.assert_awaited_once()
    await plugin.business.db(
        ref, "save_settings", "g", 0, settings(group_ai_enabled=True), "owner"
    )
    on = event()
    await plugin.group_businesses(on)
    assert on.is_at_or_wake_command
    enabled = False
    routed_off = event()
    await plugin.group_businesses(routed_off)
    assert not routed_off.is_at_or_wake_command
    # A queued request rechecks the latest group and effective profile.
    await plugin.restrict_management_tool(on, SimpleNamespace(func_tool=None))
    assert on.is_stopped()
    enabled = True
    await plugin.business.db(
        ref, "save_settings", "g", 1, settings(commands_enabled=False), "owner"
    )
    instance.send_parts.reset_mock()
    disabled_command = event("排名")
    await plugin.group_businesses(disabled_command)
    assert disabled_command.is_stopped()
    instance.send_parts.assert_not_awaited()
    plugin.business.settings = AsyncMock(side_effect=SessionError("instance_offline"))
    failed = event()
    await plugin.group_businesses(failed)
    assert not failed.is_at_or_wake_command
    with pytest.raises(SessionError):
        await plugin.restrict_management_tool(failed, SimpleNamespace(func_tool=None))
    assert failed.is_stopped()
    await plugin.business.close()


@pytest.mark.asyncio
async def test_private_single_group_inferred_multiple_groups_explicit_and_revoked(
    tmp_path, monkeypatch
):
    plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
    ref = config["session_ref"]
    await plugin.business.db(
        ref,
        "save_settings",
        "g",
        0,
        settings(manager_user_ids=["delegate"], ai_management_enabled=True),
        "owner",
    )
    event = chat(config)
    identity = await plugin.commands.execute(event, "我的权限")
    assert "本群机器人管理员" in identity and "当前群：g" in identity
    assert "递进" in await plugin.commands.execute(event, "规则")
    assert await plugin._ai_management_authorized(event)
    assert not event.is_admin()
    config["message_policy"]["group_ids"].append("other")
    await plugin.business.db(
        ref,
        "save_settings",
        "other",
        0,
        settings(manager_user_ids=["delegate"]),
        "owner",
    )
    plugin.commands.directories.clear()
    assert await plugin.commands.resolve_private_group(event) == ""
    assert "多群机器人管理员" in await plugin.commands.execute(event, "我的权限")
    with pytest.raises(SessionError, match="group_selection_required"):
        await plugin.commands.execute(event, "规则")
    assert not await plugin._ai_management_authorized(event)
    plugin.commands.directories[(ref, "delegate", event.unified_msg_origin)] = {
        "selected": "g",
        "expires": time.time() + 600,
    }
    await plugin.business.db(ref, "save_settings", "g", 1, settings(), "owner")
    assert await plugin.commands.resolve_private_group(event) == ""
    assert await plugin.commands.resolve_private_group(event) == "other"
    assert not await plugin._ai_management_authorized(event)
    await plugin.business.db(ref, "save_settings", "other", 1, settings(), "owner")
    assert await plugin.commands.resolve_private_group(event) == ""
    assert await plugin.commands.execute(event, "规则") == "无权限"
    await plugin.business.close()


@pytest.mark.asyncio
async def test_settings_singleflight_and_immediate_local_invalidation(
    tmp_path, monkeypatch
):
    plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
    business, ref = plugin.business, config["session_ref"]
    original = business.db
    business.db = AsyncMock(wraps=original)
    results = await asyncio.gather(*(business.settings(ref, "g") for _ in range(100)))
    assert business.db.await_count == 1
    assert all(value == results[0] for value in results)
    await business.db(
        ref, "save_settings", "g", 0, settings(group_ai_enabled=True), "owner"
    )
    assert (await business.settings(ref, "g"))["settings"]["group_ai_enabled"]
    await business.close()


@pytest.mark.asyncio
async def test_database_admission_backpressure_and_cancellation_no_leak(
    tmp_path, monkeypatch
):
    plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
    business, ref = plugin.business, config["session_ref"]
    await business.db(ref, "settings", "g")
    entered, release = threading.Event(), threading.Event()
    original = business.stores[ref].settings

    def slow(group):
        entered.set()
        release.wait(timeout=5)
        return original(group)

    business.stores[ref].settings = slow
    tasks = [asyncio.create_task(business.db(ref, "settings", "g")) for _ in range(200)]
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0.05)
        assert len(business.database_tasks) == 64
        assert business.database_peak <= 64
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        release.set()
        await business.close()
    assert not business.database_tasks
    assert business.database_admission._value == 64


@pytest.mark.asyncio
async def test_account_scheduler_is_bounded_and_does_not_block_peers(
    tmp_path, monkeypatch
):
    plugin, _, _ = prepared_plugin(tmp_path, monkeypatch)
    business = plugin.business
    business.stores = dict.fromkeys(["blocked", "peer", "three", "four", "five"])
    started = set()
    release = asyncio.Event()

    async def work(ref):
        started.add(ref)
        await release.wait()

    business.poll_account = work
    business.poller = asyncio.create_task(business.poll())
    try:
        await asyncio.sleep(2.1)
        assert {"blocked", "peer"} <= started
        assert len(business.account_pollers) == 4
    finally:
        await business.close()
    assert not business.account_pollers
