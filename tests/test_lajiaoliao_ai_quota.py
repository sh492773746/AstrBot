"""Native persona isolation, model-boundary admission and transactional quotas."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from test_lajiaoliao_businesses import prepared_plugin, settings
from test_lajiaoliao_plugin_api import call

from astrbot.core.platform.instance_control import instance_revision
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.event_guard import AIRequestDenied, guard_provider
from data.plugins.astrbot_plugin_lajiaoliao.ai import (
    AI_DEFAULTS,
    CHAT_PERSONA,
    MANAGEMENT_PERSONA,
    AIService,
    quota_day,
    validate_ai_config,
)
from data.plugins.astrbot_plugin_lajiaoliao.business import BusinessStore
from data.plugins.astrbot_plugin_lajiaoliao.event import (
    LajiaoLiaoEvent,
    incoming_to_message,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def test_defaults_validation_and_protected_storage(tmp_path):
    store = BusinessStore(tmp_path)
    assert store.ai_config("one")["settings"] == AI_DEFAULTS
    assert store.ai_group_config("one", "g")["settings"]["daily_limit"] == 500
    assert store.path.stat().st_mode & 0o777 == 0o600
    for changes in (
        {"user_daily_limit": True},
        {"management_daily_limit": -1},
        {"unknown": 1},
        {"chat_persona": MANAGEMENT_PERSONA},
    ):
        with pytest.raises(ValueError):
            validate_ai_config({**AI_DEFAULTS, **changes})


def test_twenty_boundary_idempotency_restart_and_isolation(tmp_path):
    store = BusinessStore(tmp_path)
    for index in range(20):
        assert store.ai_check("one", "u", "g", "chat", str(index), consume=True)[
            "allowed"
        ]
    assert not store.ai_check("one", "u", "g", "chat", "20", consume=True)["allowed"]
    assert (
        store.ai_check("one", "u", "g", "chat", "19", consume=True)["reason"]
        == "duplicate_request"
    )
    restored = BusinessStore(tmp_path)
    assert not restored.ai_check("one", "u", "", "chat", "new")["allowed"]
    assert restored.ai_check("two", "u", "g", "chat", "new", consume=True)["allowed"]
    assert restored.ai_check("one", "u", "g", "management", "m", consume=True)[
        "allowed"
    ]
    alerts = restored.ai_pending_notifications("one")
    assert len(alerts) == 1 and alerts[0]["used"] == 20


def test_hundred_management_and_five_hundred_group(tmp_path):
    store = BusinessStore(tmp_path)
    for i in range(100):
        assert store.ai_check("one", "admin", "g", "management", f"m{i}", consume=True)[
            "allowed"
        ]
    assert (
        store.ai_check("one", "admin", "g", "management", "limit")["pool"]
        == "management"
    )
    for i in range(500):
        assert store.ai_check("one", f"u{i}", "g", "chat", f"g{i}", consume=True)[
            "allowed"
        ]
    assert store.ai_check("one", "fresh", "g", "chat", "limit")["pool"] == "group"
    usage = store.ai_usage_page("one", "g")
    assert usage["group_used"] == 500
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM ai_usage WHERE subject='fresh'").fetchone()


def test_racing_last_slot_double_debit_and_versions(tmp_path):
    store = BusinessStore(tmp_path)
    value = {**AI_DEFAULTS, "user_daily_limit": 100}
    store.save_ai_pair("one", 0, value, "g", 0, {"daily_limit": 1}, "owner")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda i: store.ai_check(
                    "one", f"u{i}", "g", "chat", str(i), consume=True
                ),
                range(16),
            )
        )
    assert sum(row["allowed"] for row in results) == 1
    with store.connect() as db:
        assert (
            db.execute("SELECT SUM(used) FROM ai_usage WHERE pool='user'").fetchone()[0]
            == 1
        )
    with pytest.raises(SessionError):
        store.save_ai_pair("one", 1, value, "g", 0, {"daily_limit": 2}, "owner")
    assert store.ai_config("one")["version"] == 1
    store.save_ai_pair("one", 1, value, "g", 1, {"daily_limit": 2}, "owner")
    assert store.ai_check("one", "fresh", "g", "chat", "new", consume=True)["allowed"]
    store.save_ai_pair("one", 2, value, "g", 2, {"daily_limit": 0}, "owner")
    assert not store.ai_check("one", "another", "g", "chat", "zero")["allowed"]


def test_beijing_day_reset_and_notifications_unknown(tmp_path):
    store = BusinessStore(tmp_path)
    now = datetime(
        2026, 10, 10, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai")
    ).timestamp()
    day, reset = quota_day(now)
    assert day == "2026-10-10" and reset == now + 1
    store.save_ai_config("one", 0, {**AI_DEFAULTS, "user_daily_limit": 1}, "owner")
    assert store.ai_check("one", "u", "", "chat", "a", consume=True, now=now)["allowed"]
    assert not store.ai_check("one", "u", "", "chat", "b", now=now)["allowed"]
    assert store.ai_check("one", "u", "", "chat", "c", consume=True, now=now + 1)[
        "allowed"
    ]
    keys = [(day, "user", "u")]
    assert store.ai_notification_claim("one", "recipient", keys, now)
    restored = BusinessStore(tmp_path)
    assert (
        restored.ai_usage_page("one", now=now)["notifications"][0]["state"] == "unknown"
    )
    assert not restored.ai_notification_claim("one", "recipient", keys, now + 10)
    expired = BusinessStore(tmp_path / "expired")
    expired.save_ai_config("one", 0, {**AI_DEFAULTS, "user_daily_limit": 1}, "owner")
    expired.ai_check("one", "u", "", "chat", "first", consume=True, now=now)
    assert not expired.ai_pending_notifications("one", now=now + 1)
    assert not expired.ai_notification_claim("one", "recipient", keys, now + 1)
    assert (
        expired.ai_usage_page("one", now=now)["notifications"][0]["state"] == "expired"
    )


def live_fixture(tmp_path, monkeypatch):
    plugin, config, platform = prepared_plugin(tmp_path, monkeypatch)
    plugin.ai = AIService(plugin)
    plugin.manager.ai = plugin.ai
    platform.manager = plugin.manager
    platform.session_ref = config["session_ref"]
    platform.member_ids = {}
    platform.meta = lambda: PlatformMetadata("lajiaoliao", "test", config["id"])
    plugin.context.persona_manager = SimpleNamespace(
        personas=[
            SimpleNamespace(persona_id=CHAT_PERSONA),
            SimpleNamespace(persona_id=MANAGEMENT_PERSONA),
        ],
        get_persona_v3_by_id=lambda name: (
            {"name": name} if name in (CHAT_PERSONA, MANAGEMENT_PERSONA) else None
        ),
    )
    plugin.logger = SimpleNamespace(warning=lambda *a: None)

    def event(uid="ordinary", group="g", message=None):
        return LajiaoLiaoEvent(
            incoming_to_message(
                {
                    "message_id": message or str(time.time_ns()),
                    "sender_id": uid,
                    "target_id": group or "self",
                    "kind": "group" if group else "private",
                    "text": "Hello",
                    "timestamp": time.time(),
                },
                "self",
            ),
            platform,
        )

    return plugin, config, platform, event


@pytest.mark.asyncio
async def test_persona_scope_role_revocation_and_no_old_history(tmp_path, monkeypatch):
    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    ref = config["session_ref"]
    await plugin.business.db(
        ref,
        "save_settings",
        "g",
        0,
        settings(
            group_ai_enabled=True,
            manager_user_ids=["delegate"],
            ai_management_enabled=True,
        ),
        "owner",
    )
    ordinary = event(group="")
    await ordinary.prepare_ai_request(plugin.context)
    assert ordinary.get_ai_persona_id() == CHAT_PERSONA
    assert ordinary.get_ai_conversation_origin(plugin.context).endswith(
        "/lajiao-ai/chat"
    )
    assert not ordinary.authorize_ai_tool(
        SimpleNamespace(name="lajiaoliao_private_management"), plugin.context
    )
    await ordinary.finish_ai_request()
    admin = event("delegate", "")
    await admin.prepare_ai_request(plugin.context)
    assert admin.get_ai_persona_id() == MANAGEMENT_PERSONA
    assert admin.get_ai_conversation_origin(plugin.context).endswith("/management/g")
    group_admin = event("delegate")
    await group_admin.prepare_ai_request(plugin.context)
    assert group_admin.get_ai_persona_id() == CHAT_PERSONA
    await group_admin.finish_ai_request()
    await plugin.business.db(
        ref, "save_settings", "g", 1, settings(group_ai_enabled=True), "owner"
    )
    with pytest.raises(AIRequestDenied):
        await admin.authorize_ai_model_request("chat")
    assert admin.is_stopped()
    await admin.finish_ai_request()
    assert not plugin.ai.events and not plugin.ai.running
    next_event = event("delegate", "")
    await next_event.prepare_ai_request(plugin.context)
    assert next_event.get_ai_persona_id() == CHAT_PERSONA
    await next_event.finish_ai_request()
    await plugin.business.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["", "g"])
@pytest.mark.parametrize("manager", [False, True])
async def test_bound_peer_cannot_trigger_ai_even_when_authorized(
    tmp_path, monkeypatch, group, manager
):
    plugin, config, platform, event = live_fixture(tmp_path, monkeypatch)
    ref = config["session_ref"]
    peer = plugin.store.create("other-owner")["session_ref"]
    plugin.store.complete(peer, "other-owner", "bound-peer")
    plugin.store.bind(peer, "other-owner", "peer-instance")
    await plugin.business.db(
        ref, "save_settings", "g", 0,
        settings(group_ai_enabled=True, ai_management_enabled=True),
        "owner",
    )
    plugin.business.accessible_groups = AsyncMock(
        return_value=["g"] if manager else []
    )
    e = event("bound-peer", group)
    with pytest.raises(AIRequestDenied, match="bound_bot_ai_excluded"):
        await plugin.ai.resolve(e)
    with pytest.raises(AIRequestDenied, match="ai_admission_denied"):
        await e.prepare_ai_request(plugin.context)
    assert e.is_stopped()
    assert not plugin.ai.events and not plugin.ai.running
    platform.send_parts.assert_not_awaited()
    assert not (await plugin.business.db(ref, "ai_usage_page", config["id"]))["usage"]
    # Deterministic commands keep their free, correctly isolated history scope.
    assert (await plugin.ai.resolve(e, require_ai=False))[4] == (
        "management" if manager and not group else "chat"
    )
    await plugin.business.close()


@pytest.mark.asyncio
async def test_binding_a_queued_sender_blocks_model_and_reply(tmp_path, monkeypatch):
    from astrbot.api.event import MessageChain

    plugin, config, platform, event = live_fixture(tmp_path, monkeypatch)
    e = event(group="")
    await e.prepare_ai_request(plugin.context)
    peer = plugin.store.create("other-owner")["session_ref"]
    plugin.store.complete(peer, "other-owner", "ordinary")
    plugin.store.bind(peer, "other-owner", "peer-instance")
    provider = SimpleNamespace(text_chat=AsyncMock())
    with pytest.raises(AIRequestDenied):
        await guard_provider(provider, e).text_chat(prompt="Must not be sent")
    provider.text_chat.assert_not_awaited()
    await e.send(MessageChain().message("Must not reply to a newly bound bot"))
    platform.send_parts.assert_not_awaited()
    assert not (await plugin.business.db(
        config["session_ref"], "ai_usage_page", config["id"]
    ))["usage"]
    await e.finish_ai_request()
    assert not plugin.ai.events and not plugin.ai.running
    await plugin.business.close()


@pytest.mark.asyncio
async def test_actual_model_boundary_failure_fallback_and_no_debit_before_call(
    tmp_path, monkeypatch
):
    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    e = event(group="")
    await e.prepare_ai_request(plugin.context)
    ref = config["session_ref"]
    assert not (await plugin.business.db(ref, "ai_usage_page", config["id"]))["usage"]
    response = SimpleNamespace(usage=SimpleNamespace(input=12, output=3))
    provider = SimpleNamespace(
        text_chat=AsyncMock(side_effect=RuntimeError("synthetic"))
    )
    guarded = guard_provider(provider, e, "image_caption")
    with pytest.raises(RuntimeError, match="synthetic"):
        await guarded.text_chat()
    fallback = SimpleNamespace(text_chat=AsyncMock(return_value=response))
    await guard_provider(fallback, e, "compression").text_chat()
    await guard_provider(fallback, e).text_chat()
    data = await plugin.business.db(ref, "ai_usage_page", config["id"])
    assert data["usage"][0]["used"] == 1
    assert data["metrics"]["model_calls"] == 3
    assert data["metrics"]["input_tokens"] == 24
    assert data["metrics"]["unknown_usage"] == 1
    await e.finish_ai_request()
    duplicate = event(group="", message=e.message_obj.message_id)
    with pytest.raises(AIRequestDenied):
        await duplicate.prepare_ai_request(plugin.context)
    await plugin.business.close()


@pytest.mark.asyncio
async def test_manager_switch_off_multiple_scopes_and_no_ordinary_fallback(
    tmp_path, monkeypatch
):
    plugin, config, platform, event = live_fixture(tmp_path, monkeypatch)
    ref = config["session_ref"]
    await plugin.business.db(
        ref, "save_settings", "g", 0, settings(manager_user_ids=["delegate"]), "owner"
    )
    with pytest.raises(AIRequestDenied):
        await event("delegate", "").prepare_ai_request(plugin.context)
    platform.send_parts.assert_awaited_once()
    assert not (await plugin.business.db(ref, "ai_usage_page", config["id"]))["usage"]
    config["message_policy"]["group_ids"].append("other")
    await plugin.business.db(
        ref,
        "save_settings",
        "other",
        0,
        settings(manager_user_ids=["delegate"], ai_management_enabled=True),
        "owner",
    )
    plugin.commands.directories.clear()
    with pytest.raises(AIRequestDenied):
        await event("delegate", "").prepare_ai_request(plugin.context)
    assert not plugin.ai.events
    await plugin.business.close()


@pytest.mark.asyncio
async def test_api_owner_version_recipient_and_atomic_group_save(tmp_path, monkeypatch):
    plugin, config, _, _ = live_fixture(tmp_path, monkeypatch)
    body = {"session_ref": config["session_ref"], "group_id": "g"}
    assert (await call(plugin, "ai_config", "bob", body))[0] == 403
    status, current = await call(plugin, "ai_config", "alice", body)
    assert status == 200 and current["settings"]["user_daily_limit"] == 20
    data = {
        **body,
        "action": "save",
        "confirm": True,
        "version": 0,
        "group_version": 0,
        "instance_revision": instance_revision(config),
        "settings": AI_DEFAULTS,
        "group_settings": {"daily_limit": 501},
    }
    assert (await call(plugin, "ai_config", "alice", data))[0] == 200
    assert (await call(plugin, "ai_config", "alice", data))[0] == 409
    data.update(version=1, group_version=1, group_settings={"notify_uid": "stranger"})
    assert (await call(plugin, "ai_config", "alice", data))[0] == 409
    assert (await plugin.business.db(config["session_ref"], "ai_config", config["id"]))[
        "version"
    ] == 1
    await plugin.business.close()


@pytest.mark.asyncio
async def test_notification_recipient_revocation_private_policy_and_unknown(
    tmp_path, monkeypatch
):
    plugin, config, platform, _ = live_fixture(tmp_path, monkeypatch)
    ref, instance = config["session_ref"], config["id"]
    await plugin.business.db(
        ref,
        "save_ai_pair",
        instance,
        0,
        {**AI_DEFAULTS, "private_notify_uid": "admin"},
        "",
        None,
        {},
        "owner",
    )
    await plugin.business.db(
        ref, "ai_check", instance, "u", "", "chat", "one", consume=True
    )
    await plugin.business.db(
        ref,
        "save_ai_config",
        instance,
        1,
        {**AI_DEFAULTS, "user_daily_limit": 1, "private_notify_uid": "admin"},
        "owner",
    )
    await plugin.business.db(ref, "ai_check", instance, "u", "", "chat", "two")
    platform.send_parts.return_value = {"status": "unknown"}
    await plugin.ai.notify(ref)
    platform.send_parts.assert_awaited_once()
    await plugin.ai.notify(ref)
    assert platform.send_parts.await_count == 1
    await plugin.business.close()


def test_other_platform_guard_is_identity():
    provider = object()
    assert guard_provider(provider, SimpleNamespace()) is provider


@pytest.mark.asyncio
async def test_core_persona_injection_and_unified_private_history(
    tmp_path, monkeypatch
):
    import astrbot.core.astr_main_agent as core
    from astrbot.core.agent.message import Message
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
        InternalAgentSubStage,
    )
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest

    plugin, _, _, event = live_fixture(tmp_path, monkeypatch)
    e = event(group="")
    await e.prepare_ai_request(plugin.context)
    origin = e.get_ai_conversation_origin(plugin.context)
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="cid"),
        get_conversation=AsyncMock(
            return_value=SimpleNamespace(
                cid="cid",
                user_id=origin,
                persona_id="old-mixed",
                history="[]",
            )
        ),
        update_conversation=AsyncMock(),
    )
    plugin.context.conversation_manager = manager
    conversation = await core._get_session_conv(e, plugin.context)
    manager.get_curr_conversation_id.assert_awaited_once_with(origin)
    plugin.context.persona_manager.get_persona_v3_by_id = lambda name: {
        "prompt": "selected:" + name,
        "_begin_dialogs_processed": [{"role": "user", "content": "preset"}],
        "tools": [],
        "skills": [],
    }
    plugin.context.get_llm_tool_manager = lambda: SimpleNamespace(
        get_full_tool_set=lambda: [],
    )
    plugin.context.subagent_orchestrator = None
    monkeypatch.setattr(
        core,
        "SkillManager",
        lambda: SimpleNamespace(
            list_skills=lambda **kwargs: [],
        ),
    )
    req = ProviderRequest(prompt="hello", conversation=conversation)
    await core._ensure_persona_and_skills(req, {}, plugin.context, e)
    assert req.system_prompt.count("# Persona Instructions") == 1
    assert CHAT_PERSONA in req.system_prompt
    assert "old-mixed" not in req.system_prompt
    assert req.contexts[0]["content"] == "preset"
    stage = object.__new__(InternalAgentSubStage)
    stage.ctx = SimpleNamespace(plugin_manager=SimpleNamespace(context=plugin.context))
    stage.conv_manager = manager
    await stage._save_to_history(
        e,
        req,
        LLMResponse(role="assistant", completion_text="hello"),
        [Message(role="user", content="hello")],
        None,
    )
    assert manager.update_conversation.await_args.args == (origin, "cid")
    assert e.unified_msg_origin != origin
    await e.finish_ai_request()
    await plugin.business.close()


@pytest.mark.asyncio
async def test_stream_continuation_one_debit_and_metric(tmp_path, monkeypatch):
    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    e = event(group="")
    await e.prepare_ai_request(plugin.context)

    async def stream():
        yield SimpleNamespace(is_chunk=True, usage=None)
        yield SimpleNamespace(is_chunk=False, usage=SimpleNamespace(input=2, output=1))

    provider = SimpleNamespace(text_chat_stream=stream)
    assert (
        len([chunk async for chunk in guard_provider(provider, e).text_chat_stream()])
        == 2
    )
    assert (
        len([chunk async for chunk in guard_provider(provider, e).text_chat_stream()])
        == 2
    )
    usage = await plugin.business.db(
        config["session_ref"], "ai_usage_page", config["id"]
    )
    assert usage["usage"][0]["used"] == 1
    assert usage["metrics"]["model_calls"] == 2
    assert usage["metrics"]["input_tokens"] == 4
    await e.finish_ai_request()
    await plugin.business.close()


@pytest.mark.asyncio
async def test_admission_queue_cancellation_and_timeout_no_charge(
    tmp_path, monkeypatch
):
    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    holders = [event(group="") for _ in range(4)]
    for e in holders:
        await e.prepare_ai_request(plugin.context)
    queued = [event(group="") for _ in range(68)]
    tasks = [asyncio.create_task(e.prepare_ai_request(plugin.context)) for e in queued]
    await asyncio.wait(tasks, timeout=1)
    assert plugin.ai.waiting == 64
    assert sum(plugin.ai.running.values()) == 4
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert plugin.ai.waiting == 0
    timed = event(group="")
    timed.ai_queue_timeout = lambda: 0.02
    with pytest.raises(AIRequestDenied):
        await timed.prepare_ai_request(plugin.context)
    assert plugin.ai.waiting == 0
    for e in holders:
        await e.finish_ai_request()
        await e.finish_ai_request()
    assert not plugin.ai.events and not plugin.ai.running
    assert not (
        await plugin.business.db(config["session_ref"], "ai_usage_page", config["id"])
    )["usage"]
    await plugin.business.close()


@pytest.mark.asyncio
async def test_shared_lock_wait_expires_without_model_call(tmp_path, monkeypatch):
    from astrbot.core.provider.event_guard import event_session_lock
    from astrbot.core.utils.session_lock import session_lock_manager

    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    e = event(group="")
    await e.prepare_ai_request(plugin.context)
    origin = e.get_ai_conversation_origin(plugin.context)
    e.set_extra("_lajiao_ai_queue_deadline", time.monotonic() + 0.02)
    async with session_lock_manager.acquire_lock(origin):
        with pytest.raises(AIRequestDenied):
            async with event_session_lock(e, origin):
                pytest.fail("timed-out session entered")
    await e.finish_ai_request()
    assert not (
        await plugin.business.db(config["session_ref"], "ai_usage_page", config["id"])
    )["usage"]
    await plugin.business.close()


@pytest.mark.asyncio
async def test_notification_batch_window_and_send_time_policy(tmp_path, monkeypatch):
    plugin, config, platform, _ = live_fixture(tmp_path, monkeypatch)
    ref, instance = config["session_ref"], config["id"]
    await plugin.business.db(
        ref,
        "save_ai_config",
        instance,
        0,
        {**AI_DEFAULTS, "user_daily_limit": 1, "private_notify_uid": "admin"},
        "owner",
    )
    for uid in ("u1", "u2"):
        await plugin.business.db(
            ref, "ai_check", instance, uid, "", "chat", uid, consume=True
        )
    await plugin.ai.notify(ref)
    assert platform.send_parts.await_count == 1
    text = platform.send_parts.await_args.kwargs["parts"][0]["text"]
    assert "u1" in text and "u2" in text
    await plugin.business.db(
        ref, "ai_check", instance, "u3", "", "chat", "u3", consume=True
    )
    await plugin.ai.notify(ref)
    assert platform.send_parts.await_count == 1
    store = plugin.business.stores[ref]
    with store.connect() as db:
        db.execute("UPDATE ai_notification_window SET next_run=0")
    actual_db = plugin.business.db

    async def revoke_before_send(ref, method, *args, **kwargs):
        result = await actual_db(ref, method, *args, **kwargs)
        if method == "ai_notification_claim":
            config["message_policy"]["private_enabled"] = False
        return result

    monkeypatch.setattr(plugin.business, "db", revoke_before_send)
    await plugin.ai.notify(ref)
    assert platform.send_parts.await_count == 1
    data = store.ai_usage_page(instance)
    assert (
        next(row for row in data["notifications"] if row["subject"] == "u3")["state"]
        == "blocked"
    )
    await plugin.business.close()


@pytest.mark.asyncio
async def test_ai_usage_get_query_owner_and_bad_page(tmp_path, monkeypatch):
    from astrbot.api.web import bind_request_context

    plugin, config, _, _ = live_fixture(tmp_path, monkeypatch)
    query = {"session_ref": config["session_ref"], "group_id": "g", "page": "1"}
    request = SimpleNamespace(username="alice", method="GET", query=query)
    with bind_request_context(request):
        response = await plugin.ai_usage()
    assert response.status_code == 200
    assert json.loads(response.body)["page"] == 1
    request.username = "bob"
    with bind_request_context(request):
        assert (await plugin.ai_usage()).status_code == 403
    request.username = "alice"
    query["page"] = "bad"
    with bind_request_context(request):
        assert (await plugin.ai_usage()).status_code == 400
    await plugin.business.close()


@pytest.mark.asyncio
async def test_native_runner_uses_guarded_provider(tmp_path, monkeypatch):
    from test_tool_loop_agent_runner import MockHooks, MockProvider, MockToolExecutor

    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.provider.entities import ProviderRequest

    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    e = event(group="")
    await e.prepare_ai_request(plugin.context)
    provider = MockProvider()
    provider.should_call_tools = False
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=guard_provider(provider, e),
        request=ProviderRequest(prompt="synthetic", contexts=[]),
        run_context=ContextWrapper(context=None),
        tool_executor=MockToolExecutor(),
        agent_hooks=MockHooks(),
    )
    async for _ in runner.step_until_done(3):
        pass
    assert provider.call_count == 1
    assert runner.done()
    data = await plugin.business.db(
        config["session_ref"], "ai_usage_page", config["id"]
    )
    assert data["usage"][0]["used"] == 1
    assert data["metrics"]["input_tokens"] == 10
    await e.finish_ai_request()
    await plugin.business.close()


@pytest.mark.asyncio
async def test_global_admission_eight_and_close_reclaims_processing(
    tmp_path, monkeypatch
):
    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    resolve = plugin.ai.resolve

    async def two_instance_scope(e):
        scope = await resolve(e)
        return (
            scope[:1] + ("synthetic-second",) + scope[2:]
            if e.get_sender_id().startswith("second")
            else scope
        )

    monkeypatch.setattr(plugin.ai, "resolve", two_instance_scope)
    released = asyncio.Event()
    events = [
        event(uid=f"{prefix}-{i}", group="")
        for prefix in ("first", "second")
        for i in range(4)
    ]

    async def run(e):
        try:
            await e.prepare_ai_request(plugin.context)
            await released.wait()
        finally:
            await e.finish_ai_request()

    tasks = [asyncio.create_task(run(e)) for e in events]
    for e, task in zip(events, tasks, strict=True):
        e.processing_task = task
    await asyncio.wait(tasks, timeout=0.2)
    assert sum(plugin.ai.running.values()) == 8
    assert sorted(plugin.ai.running.values()) == [4, 4]
    await plugin.ai.close()
    assert all(task.done() for task in tasks)
    assert not plugin.ai.events and not plugin.ai.running
    assert all(e.is_stopped() for e in events)
    await plugin.business.close()


@pytest.mark.asyncio
async def test_metric_ledger_failure_stops_continuation(tmp_path, monkeypatch):
    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    e = event(group="")
    await e.prepare_ai_request(plugin.context)
    actual_db = plugin.business.db

    async def fail_metric(ref, method, *args, **kwargs):
        if method == "ai_metric":
            raise RuntimeError("synthetic unavailable")
        return await actual_db(ref, method, *args, **kwargs)

    monkeypatch.setattr(plugin.business, "db", fail_metric)
    provider = SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(usage=None))
    )
    with pytest.raises(AIRequestDenied):
        await guard_provider(provider, e).text_chat()
    assert e.is_stopped()
    with pytest.raises(AIRequestDenied):
        await guard_provider(provider, e).text_chat()
    assert provider.text_chat.await_count == 1
    await e.finish_ai_request()
    await plugin.business.close()


@pytest.mark.asyncio
async def test_revoked_authority_cannot_read_prior_management_history(
    tmp_path, monkeypatch
):
    from astrbot.core.astr_main_agent import _get_session_conv

    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    ref = config["session_ref"]
    await plugin.business.db(
        ref,
        "save_settings",
        "g",
        0,
        settings(manager_user_ids=["delegate"], ai_management_enabled=True),
        "owner",
    )
    e = event(uid="delegate", group="")
    await e.prepare_ai_request(plugin.context)
    reader = AsyncMock()
    plugin.context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=reader,
    )
    await plugin.business.db(ref, "save_settings", "g", 1, settings(), "owner")
    with pytest.raises(AIRequestDenied):
        await _get_session_conv(e, plugin.context)
    reader.assert_not_awaited()
    await e.finish_ai_request()
    await plugin.business.close()


@pytest.mark.asyncio
async def test_guarded_pipeline_does_not_merge_unscoped_followups(
    tmp_path, monkeypatch
):
    import astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal as core
    from astrbot.core.astr_main_agent import MainAgentBuildConfig

    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    e = event(group="")
    capture = MagicMock(side_effect=AssertionError("must not merge private scopes"))
    monkeypatch.setattr(core, "try_capture_follow_up", capture)
    monkeypatch.setattr(core, "call_event_hook", AsyncMock(return_value=False))
    monkeypatch.setattr(core, "build_main_agent", AsyncMock(return_value=None))
    stage = object.__new__(core.InternalAgentSubStage)
    stage.ctx = SimpleNamespace(plugin_manager=SimpleNamespace(context=plugin.context))
    stage.streaming_response = False
    stage.show_reasoning = False
    stage.main_agent_cfg = MainAgentBuildConfig(tool_call_timeout=30)
    stage._send_llm_error_message = AsyncMock()
    assert [item async for item in stage.process(e, "")] == []
    capture.assert_not_called()
    assert not plugin.ai.events and not plugin.ai.running
    assert not (
        await plugin.business.db(config["session_ref"], "ai_usage_page", config["id"])
    )["usage"]
    await plugin.business.close()


@pytest.mark.asyncio
async def test_native_conversation_command_scope_is_free_when_ai_disabled(
    tmp_path, monkeypatch
):
    from astrbot.builtin_stars.builtin_commands.commands.conversation import (
        ConversationCommands,
    )

    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    ref = config["session_ref"]
    await plugin.business.db(
        ref,
        "save_settings",
        "g",
        0,
        settings(manager_user_ids=["delegate"], ai_management_enabled=False),
        "owner",
    )
    await plugin.business.db(
        ref,
        "save_ai_config",
        config["id"],
        0,
        {**AI_DEFAULTS, "user_daily_limit": 0, "management_daily_limit": 0},
        "owner",
    )
    get_config = plugin.context.get_config
    plugin.context.get_config = lambda umo=None: {
        **get_config(umo),
        "provider_settings": {"enable": False},
        "agent_runner": {"runner_type": "local"},
    }
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value=None),
        new_conversation=AsyncMock(return_value="cid"),
    )
    plugin.context.conversation_manager = manager
    commands = ConversationCommands(plugin.context)
    ordinary = event(group="")
    admin = event(uid="delegate", group="")
    await commands.new_conv(ordinary)
    await commands.new_conv(admin)
    origins = [call.args[0] for call in manager.new_conversation.await_args_list]
    assert origins[0].endswith("/lajiao-ai/chat")
    assert origins[1].endswith("/lajiao-ai/management/g")
    assert ordinary.get_extra("_lajiao_ai_scope") is None
    assert admin.get_extra("_lajiao_ai_scope") is None
    await commands.stats(ordinary)
    assert manager.get_curr_conversation_id.await_args.args == (origins[0],)
    assert not (await plugin.business.db(ref, "ai_usage_page", config["id"]))["usage"]
    assert not plugin.ai.events
    await plugin.business.close()


@pytest.mark.asyncio
async def test_external_runner_refused_without_affecting_other_platforms(
    tmp_path, monkeypatch
):
    from astrbot.core.pipeline.process_stage import stage as process_stage
    from astrbot.core.pipeline.process_stage.method.agent_request import (
        AgentRequestSubStage,
    )
    from astrbot.core.star.session_llm_manager import SessionServiceManager

    assert process_stage.AgentRequestSubStage is AgentRequestSubStage

    plugin, _, _, event = live_fixture(tmp_path, monkeypatch)
    stage = object.__new__(AgentRequestSubStage)
    stage.ctx = SimpleNamespace(
        astrbot_config={
            "agent_runner": {"runner_type": "dify"},
            "provider_settings": {"enable": True},
        }
    )
    seen = []

    async def process(e, prefix):
        seen.append(e)
        yield None

    stage.agent_sub_stage = SimpleNamespace(process=process)
    stage.prov_wake_prefix = ""
    monkeypatch.setattr(
        SessionServiceManager,
        "should_process_llm_request",
        AsyncMock(return_value=True),
    )
    e = event(group="")
    assert [item async for item in stage.process(e)] == []
    assert e.is_stopped() and not seen
    other = SimpleNamespace()
    assert [item async for item in stage.process(other)] == [None]
    assert seen == [other]
    await plugin.business.close()


@pytest.mark.asyncio
async def test_review_shares_admission_without_chat_debit(tmp_path, monkeypatch):
    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    ref = config["session_ref"]
    await plugin.business.db(
        ref,
        "save_settings",
        "g",
        0,
        settings(review_enabled=True, review_provider_id="review"),
        "owner",
    )
    holders = [event(group="") for _ in range(4)]
    for e in holders:
        await e.prepare_ai_request(plugin.context)
    reviewing = event(group="g")
    entered, release = asyncio.Event(), asyncio.Event()

    async def review():
        async with plugin.ai.review_slot(reviewing, "review"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(review())
    await asyncio.wait([task], timeout=0.1)
    assert not entered.is_set()
    await holders[0].finish_ai_request()
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert sum(plugin.ai.running.values()) == 4
    assert list(plugin.ai.purposes.values()).count("review") == 1
    release.set()
    await task
    for e in holders:
        await e.finish_ai_request()
    assert not plugin.ai.events and not plugin.ai.owners and not plugin.ai.purposes
    assert not (await plugin.business.db(ref, "ai_usage_page", config["id"]))["usage"]
    await plugin.business.close()


@pytest.mark.asyncio
async def test_bound_peer_cannot_enter_automatic_review(tmp_path, monkeypatch):
    plugin, config, _, event = live_fixture(tmp_path, monkeypatch)
    ref = config["session_ref"]
    await plugin.business.db(
        ref, "save_settings", "g", 0,
        settings(review_enabled=True, review_provider_id="review"), "owner",
    )
    peer = plugin.store.create("other-owner")["session_ref"]
    plugin.store.complete(peer, "other-owner", "bound-peer")
    plugin.store.bind(peer, "other-owner", "peer-instance")
    with pytest.raises(AIRequestDenied, match="review_not_authorized"):
        async with plugin.ai.review_slot(event("bound-peer"), "review"):
            pytest.fail("A bound robot entered a model review")
    assert not plugin.ai.events and not plugin.ai.running and not plugin.ai.purposes
    assert not (await plugin.business.db(ref, "ai_usage_page", config["id"]))["usage"]
    await plugin.business.close()
