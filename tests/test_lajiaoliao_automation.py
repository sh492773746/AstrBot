import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_lajiaoliao_businesses import prepared_plugin, settings
from test_lajiaoliao_plugin_api import call

from astrbot.api.message_components import Reply
from data.plugins.astrbot_plugin_lajiaoliao.automation import AUTOMATION_DEFAULTS
from data.plugins.astrbot_plugin_lajiaoliao.business import (
    BusinessStore,
    validate_settings,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def automatic_settings(**rules):
    return settings(
        review_enabled=True, native_actions=["mute"],
        automation={**copy.deepcopy(AUTOMATION_DEFAULTS), "enabled": True, **rules},
    )


def message(config, instance, *, uid="ordinary", private=False, text="prohibited SECRET_CHAT_TEXT"):
    return SimpleNamespace(
        platform=instance, is_private_chat=lambda: private, is_admin=lambda: uid == "admin",
        get_group_id=lambda: "" if private else "g", get_sender_id=lambda: uid,
        get_platform_name=lambda: "lajiaoliao", get_extra=lambda _: None,
        unified_msg_origin="private" if private else "group",
        role="member", stop_event=Mock(), is_at_or_wake_command=False,
        message_obj=SimpleNamespace(
            timestamp=time.time(), message_str=text, message_id="fresh",
            sender=SimpleNamespace(nickname="Member"), message=[],
        ),
    )


def roster_for(plugin, config):
    return {
        plugin.store._read(config["session_ref"])["account_id"]: {"roleLevel": 100},
        "ordinary": {"roleLevel": 20}, "admin": {"roleLevel": 20},
        "delegate": {"roleLevel": 20}, "native-owner": {"roleLevel": 100},
    }


def test_old_settings_defaults_and_invalid_rules():
    old = settings()
    old.pop("automation")
    old.pop("ai_management_enabled")
    assert not validate_settings(old)["automation"]["enabled"]
    for rule in [
        {"mute_minutes": [5, 15, 61]}, {"mute_minutes": [15, 5, 60]},
        {"flood_limit": True}, {"ai_min_confidence": float("nan")},
        {"ai_min_confidence": 0.89}, {"ai_enabled": True},
        {"arbitrary_action": "kick"},
    ]:
        with pytest.raises(ValueError):
            validate_settings(automatic_settings(**rule))
    value = automatic_settings()
    value["native_actions"] = []
    with pytest.raises(ValueError, match="requires_review_and_mute"):
        validate_settings(value)


def test_atomic_reservation_progression_cooldown_unknown_and_restart(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    store = BusinessStore(tmp_path / "one")
    store.save_settings("g", 0, automatic_settings(), "owner")
    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(
            lambda _: store.reserve_automatic("g", "first", "u", now, "keyword", 1), range(4)
        ))
    plans = [outcome for outcome in outcomes if outcome]
    assert len(plans) == 1 and plans[0]["muted_seconds"] == 300
    assert BusinessStore(tmp_path / "one").automatic_events("g")[0]["status"] == "unknown"
    now += 90
    assert store.reserve_automatic("g", "blocked", "u", now, "keyword", 1) is None
    assert store.finish_automatic("g", plans[0]["operation_id"], "succeeded") == "succeeded"
    for index, seconds in [(2, 900), (3, 3600), (4, 3600)]:
        plan = store.reserve_automatic("g", str(index), "u", now, "keyword", 1)
        assert plan["muted_seconds"] == seconds
        store.finish_automatic("g", plan["operation_id"], "succeeded")
        assert store.reserve_automatic("g", "cooldown-" + str(index), "u", now, "keyword", 1) is None
        now += 90
    assert not BusinessStore(tmp_path / "two").automatic_events("g")
    assert not store.automatic_events("other")
    assert store.reserve_automatic("g", "history", "u", now - 61, "keyword", 1) is None
    with pytest.raises(SessionError, match="changed"):
        store.reserve_automatic("g", "stale-version", "u", now, "keyword", 0)


def test_keyword_mute_duplicate_and_protected_members(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        instance.session_ref = config["session_ref"]
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        plugin.business.roster = AsyncMock(return_value=roster_for(plugin, config))
        value = automatic_settings()
        value.update(keywords=["prohibited"], manager_user_ids=["delegate"])
        await plugin.business.db(config["session_ref"], "save_settings", "g", 0, value, "owner")
        event = message(config, instance)
        assert await plugin.commands.observe(event, "") is True
        await plugin.commands.observe(event, "")
        instance.platform_action.assert_awaited_once()
        body = instance.platform_action.call_args.args[0]
        assert body["action"] == "mute" and body["muted_seconds"] == 300
        assert "SECRET_CHAT_TEXT" not in json.dumps(await plugin.business.db(config["session_ref"], "automatic_events", "g"))
        for uid in ["admin", "delegate", "native-owner", plugin.store._read(config["session_ref"])["account_id"]]:
            event = message(config, instance, uid=uid)
            event.message_obj.message_id = "protected-" + uid
            await plugin.commands.observe(event, "")
        instance.platform_action.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["role", "policy", "version", "revoked", "expired", "unknown"])
def test_execution_rechecks_and_unknown_never_retries(tmp_path, monkeypatch, fault):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        instance.session_ref = ref
        instance.platform_action = AsyncMock(return_value={"status": "unknown" if fault == "unknown" else "succeeded"})
        people = roster_for(plugin, config)
        plugin.business.roster = AsyncMock(return_value=people)
        value = automatic_settings()
        value["keywords"] = ["prohibited"]
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        if fault == "role":
            people[plugin.store._read(ref)["account_id"]]["roleLevel"] = 20
        if fault == "revoked":
            instance._revoked_messages = {("group", "g", "fresh"): None}
        original = plugin.business.native_payload
        count = 0

        async def validate(*args):
            nonlocal count
            count += 1
            if count == 2 and fault == "policy":
                config["message_policy"]["group_ids"] = []
            if count == 2 and fault == "version":
                await plugin.business.db(ref, "save_settings", "g", 1, settings(), "owner")
            if count == 2 and fault == "expired":
                monkeypatch.setattr(time, "time", lambda: event.message_obj.timestamp + 61)
            return await original(*args)

        plugin.business.native_payload = validate
        event = message(config, instance)
        await plugin.commands.observe(event, "")
        if fault == "unknown":
            event.message_obj.message_id = "next"
            await plugin.commands.observe(event, "")
            instance.platform_action.assert_awaited_once()
            assert (await plugin.business.db(ref, "automatic_events", "g"))[0]["status"] == "unknown"
        else:
            instance.platform_action.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("result,expected", [
    ({"decision": "violation", "confidence": 0.99}, True),
    ({"decision": "violation", "confidence": 0.89}, False),
    ({"decision": "violation", "confidence": True}, False),
    ({"decision": "violation", "confidence": float("nan")}, False),
    ({"decision": "violation"}, False),
    ({"decision": "violation", "confidence": 0.99, "message_id": "another"}, False),
    ({"decision": "allow", "confidence": 1}, False),
    ({"decision": "review", "confidence": 1}, False),
])
def test_ai_evidence_is_bound_confident_and_tool_free(tmp_path, monkeypatch, result, expected):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        instance.session_ref = ref
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        plugin.business.roster = AsyncMock(return_value=roster_for(plugin, config))
        value = automatic_settings(ai_enabled=True, ai_rules="No scams.", keyword_enabled=False, flood_enabled=False)
        value["review_provider_id"] = "mock"
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(
            completion_text=json.dumps({"message_id": "fresh", **result})
        ))
        await plugin.commands.observe(message(config, instance), "")
        assert bool(instance.platform_action.await_count) == expected
        assert plugin.context.llm_generate.call_args.kwargs["tools"] is None
    asyncio.run(run())


@pytest.mark.parametrize("quoted", [False, True])
def test_reports_and_quotes_are_not_keyword_violations(tmp_path, monkeypatch, quoted):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        instance.session_ref = ref
        instance.platform_action = AsyncMock()
        value = automatic_settings(flood_enabled=False)
        value["keywords"] = ["prohibited"]
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        event = message(config, instance, text="prohibited" if quoted else "举报 prohibited")
        if quoted:
            event.message_obj.message = [Reply(id="original")]
        await plugin.commands.observe(event, "")
        instance.platform_action.assert_not_awaited()
    asyncio.run(run())


def test_ai_delegate_queries_only_and_manual_actions_removed(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        instance.session_ref = ref
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        plugin.business.roster = AsyncMock(return_value=roster_for(plugin, config))
        value = settings(manager_user_ids=["delegate"], native_actions=["mute"], ai_management_enabled=True)
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        event = message(config, instance, uid="delegate", private=True)
        plugin.commands.directories[(ref, "delegate", "private")] = {"selected": "g", "expires": time.time() + 600}
        outcome = json.loads(await plugin.private_management(event, "action_preview", {"command": "禁言", "argument": "ordinary 5"}))
        assert outcome["status"] == "unsupported" and event.role == "member"
        instance.platform_action.assert_not_awaited()
        for ai in (True, False):
            with pytest.raises(SessionError, match="manual_group_actions_removed"):
                await plugin.commands.execute(event, "确认操作", "legacy-operation", ai=ai)
            with pytest.raises(SessionError, match="manual_group_actions_removed"):
                await plugin.commands.execute(event, "禁言", "ordinary 5", ai=ai)
        assert json.loads(await plugin.private_management(event, "settings_preview", {"changes": {"ai_management_enabled": False}}))["status"] == "rejected"
        assert json.loads(await plugin.private_management(event, "rules", {}))["status"] == "queried"
        instance.platform_action.assert_not_awaited()
        value["manager_user_ids"] = []
        await plugin.business.db(ref, "save_settings", "g", 1, value, "owner")
        assert json.loads(await plugin.private_management(event, "rules", {}))["status"] == "rejected"
    asyncio.run(run())


def test_dashboard_automation_confirmation_and_owner_scope(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        body = {"session_ref": ref, "group_id": "g"}
        status, data = await call(plugin, "business_page", "alice", body)
        assert status == 200 and data["automatic"] == []
        assert not data["settings"]["automation"]["enabled"]
        assert (await call(plugin, "business_page", "bob", body))[0] == 403
        assert (await call(plugin, "business_page", "alice", {
            **body, "action": "refresh-automatic", "operation_id": "another-group",
        }))[0] == 409
    asyncio.run(run())


def test_flood_is_serialized_and_duplicate_ids_do_not_inflate_count(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        instance.session_ref = ref
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        plugin.business.roster = AsyncMock(return_value=roster_for(plugin, config))
        await plugin.business.db(ref, "save_settings", "g", 0, automatic_settings(
            flood_limit=2, duplicate_limit=2, keyword_enabled=False,
        ), "owner")
        first = message(config, instance)
        await asyncio.gather(*(plugin.commands.observe(first, "") for _ in range(4)))
        instance.platform_action.assert_not_awaited()
        second = message(config, instance)
        second.message_obj.message_id = "second"
        await plugin.commands.observe(second, "")
        instance.platform_action.assert_awaited_once()
    asyncio.run(run())


def test_disabled_keywords_and_invalid_ai_never_sanction(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        instance.session_ref = ref
        instance.platform_action = AsyncMock()
        value = automatic_settings(keyword_enabled=False, flood_enabled=False, ai_enabled=True, ai_rules="No scams.")
        value.update(keywords=["prohibited"], review_provider_id="mock")
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="<think>bad</think>"))
        await plugin.commands.observe(message(config, instance), "")
        instance.platform_action.assert_not_awaited()
    asyncio.run(run())


def test_settings_preview_cannot_grant_automation_and_receipt_lookup_is_read_only(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        instance.session_ref = ref
        value = automatic_settings()
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        value["automation"]["enabled"] = False
        await plugin.business.db(ref, "save_settings", "g", 1, value, "owner")
        event = message(config, instance, uid="admin", private=True)
        plugin.commands.directories[(ref, "admin", "private")] = {"selected": "g", "expires": time.time() + 600}
        assert json.loads(await plugin.private_management(event, "settings_preview", {
            "changes": {"automation": {**value["automation"], "enabled": True}},
        }))["status"] == "rejected"
        await plugin.business.db(ref, "save_settings", "g", 2, automatic_settings(), "owner")
        plan = await plugin.business.db(ref, "reserve_automatic", "g", "sample", "ordinary", time.time(), "keyword", 3)
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        status, _ = await call(plugin, "business_page", "alice", {
            "session_ref": ref, "group_id": "g", "action": "refresh-automatic", "operation_id": plan["operation_id"],
        })
        assert status == 200
        instance.platform_action.assert_awaited_once_with({"query": True, "operation_id": plan["operation_id"]})
    asyncio.run(run())


def test_automatic_violation_stops_before_regular_model_reply(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        instance.session_ref = config["session_ref"]
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        plugin.business.roster = AsyncMock(return_value=roster_for(plugin, config))
        plugin.context.llm_generate = AsyncMock()
        value = automatic_settings()
        value["keywords"] = ["prohibited"]
        await plugin.business.db(config["session_ref"], "save_settings", "g", 0, value, "owner")
        event = message(config, instance)
        await plugin.group_businesses(event)
        event.stop_event.assert_called_once()
        plugin.context.llm_generate.assert_not_awaited()
        instance.send_parts.assert_not_awaited()
    asyncio.run(run())
