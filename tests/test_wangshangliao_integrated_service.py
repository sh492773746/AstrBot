"""Merged transport, tenant rules and financial-query boundary regressions."""

import asyncio
import copy
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation.group_management import (
    GroupManagement,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    OpenService,
    enabled,
    recognize_integrated,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
    PolicyRuntime,
    PolicySnapshot,
)
from tests.fixtures.wsl_native_conversations import conversation_manager
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


def service(tmp_path):
    """Build an embedded service without any native login or external model.

    Args:
        tmp_path: Isolated pytest storage.

    Returns:
        Service with deterministic total-control dependencies.
    """
    config = {"admins_id": ["existing-framework-admin"]}
    provider = SimpleNamespace(
        meta=lambda: SimpleNamespace(
            id="existing/model", type="openai_chat_completion"
        ),
        get_model=lambda: "configured-model",
        get_current_key=lambda: "server-only-test-key",
        provider_config={
            "enable": True,
            "api_base": "https://model.example/v1",
            "custom_extra_body": {"tools": [{"type": "dangerous"}]},
        },
    )
    context = SimpleNamespace(
        conversation_manager=conversation_manager(),
        get_config=lambda *a: config,
        get_all_providers=lambda: [provider],
        get_provider_by_id=lambda uid: provider if uid == "existing/model" else None,
        platform_manager=SimpleNamespace(platform_insts=[]),
    )
    plugin = SimpleNamespace(
        context=context,
        commands=SimpleNamespace(
            selections={}, run=AsyncMock(return_value="public-result")
        ),
        legacy_management=GroupManagement(context, tmp_path / "legacy"),
        cashback=SimpleNamespace(
            private_invitation_reply=AsyncMock(return_value="本人返现")
        ),
        private_interaction=SimpleNamespace(handle=AsyncMock(return_value=True)),
    )
    return OpenService(plugin, tmp_path / "embedded", integrated=True)


def import_tenant(value, group="5"):
    """Import a legacy group with no inferred chat administrators.

    Args:
        value: Isolated embedded service.
        group: Test group identifier.
    """
    value.store.discover("99", {group: {"name": "old group", "team": "1005"}})
    value.store.import_group(
        "99",
        group,
        {"advertising": True, "flood": True, "keywords": ["old"]},
        [],
        "system/merge",
    )


def bind_actor(event):
    """Complete a synthetic event's authenticated transport envelope.

    Args:
        event: Partial offline event fixture.
    """
    account = str(event.platform.account)
    actor = getattr(event, "actor_identity", ActorIdentity(account, "1", "1001", "1"))
    event.actor_identity = actor
    if not hasattr(event, "get_sender_id"):
        event.get_sender_id = lambda: actor.claimed_uid
    if not hasattr(event, "get_group_id"):
        event.get_group_id = lambda: "" if event.is_private_chat() else "5"
    if not hasattr(event, "unified_msg_origin"):
        event.unified_msg_origin = account + "/offline/" + event.get_group_id()
    if not hasattr(event, "message_obj"):
        event.message_obj = SimpleNamespace(message_id="offline")
    event.platform.config.setdefault("id", "fixture")


def test_integrated_mode_is_explicit_not_enabled_by_chat(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRBOT_WSL_OPEN", raising=False)
    assert not enabled(SimpleNamespace(get_config=lambda: {}))
    assert enabled(
        SimpleNamespace(
            get_config=lambda: {"wangshangliao_open": {"mode": "integrated"}}
        )
    )
    assert not enabled(
        SimpleNamespace(get_config=lambda: {"wangshangliao_open": {"mode": "isolated"}})
    )
    monkeypatch.setenv("ASTRBOT_WSL_OPEN", "1")
    assert not enabled()
    assert not enabled(SimpleNamespace(get_config=lambda: {}))


def test_core_configuration_preserves_the_registered_integration_mode(tmp_path):
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot.core.config.default import DEFAULT_CONFIG

    path = tmp_path / "config.json"
    source = copy.deepcopy(DEFAULT_CONFIG)
    source["wangshangliao_open"] = {"mode": "integrated"}
    path.write_text(json.dumps(source), encoding="utf-8")
    config = AstrBotConfig(str(path))
    assert config["wangshangliao_open"]["mode"] == "integrated"
    assert (
        json.loads(path.read_text(encoding="utf-8-sig"))["wangshangliao_open"]["mode"]
        == "integrated"
    )


def test_instance_recreation_reuses_business_state_not_login_credentials(
    tmp_path, monkeypatch
):
    from astrbot.core import astrbot_config
    from astrbot.core.platform.sources.wangshangliao import storage

    monkeypatch.delenv("ASTRBOT_WSL_OPEN", raising=False)
    monkeypatch.setattr(storage, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setitem(astrbot_config, "wangshangliao_open", {"mode": "integrated"})
    monkeypatch.setitem(
        astrbot_config,
        "platform",
        [
            {"id": "old", "type": "wangshangliao", "account_id": "99"},
        ],
    )
    old = storage.instance_dir("old")
    assert old.name == hashlib.sha256(b"old").hexdigest()
    old.mkdir(parents=True, exist_ok=True)
    sentinel = old / "private_interaction.sqlite3"
    sentinel.write_bytes(b"existing business state")
    monkeypatch.setitem(
        astrbot_config,
        "platform",
        [
            {"id": "new", "type": "wangshangliao", "account_id": "99"},
            {"id": "other", "type": "wangshangliao", "account_id": "100"},
        ],
    )
    assert storage.instance_dir("new") == old
    assert sentinel.read_bytes() == b"existing business state"
    assert storage.instance_dir("other") != old
    assert storage.Vault("new").root != old
    assert storage.Vault("new").root.name == hashlib.sha256(b"new").hexdigest()


def test_activity_authority_is_group_scoped_not_global_admin(tmp_path):
    from astrbot.builtin_stars.wangshangliao_moderation.activities import (
        GroupActivities,
    )
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    value = service(tmp_path)
    activities = GroupActivities(value.context)
    config = {
        "enabled_groups": ["5", "6"],
        "_business_active": True,
        "_open_integrated": True,
        "_open_groups": {"5": {}, "6": {}},
        "_open_managers": {"5": ["1"], "6": ["2"]},
        "_open_native_roles": dict.fromkeys(("5", "6"), "GROUP_ROLE_ADMIN"),
    }
    adapter = SimpleNamespace(
        config=config,
        account="99",
        connection_state="online",
        stopping=asyncio.Event(),
        groups={"5": "1005", "6": "1006"},
    )
    activities.authorize(adapter, "5", "1")
    with pytest.raises(ProtocolError, match="activity_owner_revoked"):
        activities.authorize(adapter, "6", "1")
    with pytest.raises(ProtocolError, match="activity_owner_revoked"):
        activities.authorize(adapter, "5", "existing-framework-admin")
    config["_open_managers"]["5"] = []
    with pytest.raises(ProtocolError, match="activity_owner_revoked"):
        activities.authorize(adapter, "5", "1")
    config["_open_paused"] = True
    with pytest.raises(ProtocolError, match="activity_scope"):
        activities.authorize(adapter, "5")


def test_legacy_import_is_once_and_never_guesses_an_inviter(tmp_path):
    value = service(tmp_path)
    import_tenant(value)
    row = value.store.tenant("99", "5")
    assert row["active"] and not row["managers"] and not row["inviter"]
    changed = {"advertising": False, "flood": True, "keywords": ["new"]}
    value.store.change(
        "99", "5", row["version"], "dashboard/root", rules=changed, control=True
    )
    value.store.import_group("99", "5", row["rules"], ["2"], "system/merge")
    assert value.store.tenant("99", "5")["rules"] == changed
    assert value.store.tenant("99", "5")["managers"] == []


@pytest.mark.asyncio
async def test_retired_group_cannot_fall_back_to_legacy_policy_or_onboarding(tmp_path):
    value = service(tmp_path)
    import_tenant(value)
    value.store.retire_group("5")
    await value.refresh()
    base = {
        "enabled_groups": ["5", "6"],
        "reply_groups": {"5": True, "6": True},
        "moderation": {"permissions": {"5": ["mute"]}, "card_auto": {"5": True}},
        "proactive_send": {"enabled": True, "targets": ["5", "6"]},
    }
    overlay = value.policy_values("99", base)
    assert overlay["enabled_groups"] == ["6"]
    assert overlay["proactive_send"]["targets"] == ["6"]
    assert "5" not in overlay["moderation"]["permissions"]
    assert "5" not in overlay["moderation"]["card_auto"]
    assert "5" not in overlay["reply_groups"]
    value.candidate = AsyncMock()
    value.admit_candidate(None, "5", {}, SimpleNamespace())
    assert not value.candidate_tasks
    value.candidate.assert_not_awaited()
    assert base["enabled_groups"] == ["5", "6"]


@pytest.mark.asyncio
async def test_overlay_keeps_saved_activity_and_cashback_policy_unchanged(tmp_path):
    value = service(tmp_path)
    import_tenant(value)
    tenant = value.store.tenant("99", "5")
    value.store.native_role("99", "5", tenant["epoch"], "GROUP_ROLE_ADMIN")
    await value.refresh()
    original = {
        "enabled_groups": ["5"],
        "reply_private": True,
        "moderation": {
            "permissions": {"5": ["mute", "recall", "rename"]},
            "card_auto": {"5": True},
        },
        "ai_routes": {"groups": {"5": "old/model"}},
        "proactive_send": {"enabled": True, "targets": ["5"]},
    }
    before = copy.deepcopy(original)
    policy = value.policy_values("99", original)
    assert original == before
    assert policy["enabled_groups"] == ["5"]
    assert policy["moderation"]["card_auto"] == {"5": True}
    assert policy["moderation"]["permissions"]["5"] == ["mute", "recall", "rename"]
    assert policy["_open_managers"]["5"] == []
    assert value.policy_values("100", original)["_open_groups"] == {}
    settings = value.store.settings()
    value.store.configure(settings["version"], True, 20, 1000, "root")
    await value.refresh()
    paused = value.policy_values("99", original)
    assert paused["enabled_groups"] == ["5"] and paused["reply_private"]
    assert not paused["moderation"]["enabled"] and paused["_open_paused"]
    assert not paused["proactive_send"]["enabled"]


@pytest.mark.asyncio
async def test_runtime_uses_overlay_without_replacing_base_snapshot(tmp_path):
    value = service(tmp_path)
    import_tenant(value)
    await value.refresh()
    runtime = PolicyRuntime()
    runtime.service = SimpleNamespace(active=True)
    runtime.open_service = value
    base = PolicySnapshot(
        "99",
        "99:6",
        json.dumps({"enabled_groups": ["5"], "reply_private": True}),
        time.time(),
    )
    runtime.publish("bot", base)
    config = {"id": "bot", "account_id": "99"}
    assert runtime.view(config)["_open_groups"]["5"]["keywords"] == ["old"]
    assert runtime.snapshots["bot"] is base
    observed = []
    runtime.observer = lambda snapshot, *args: observed.append(snapshot.values())
    runtime.observe(config, "99", "5", "m", {}, time.time())
    assert observed[0]["_open_integrated"]
    assert runtime.snapshots["bot"].values() == {
        "enabled_groups": ["5"],
        "reply_private": True,
    }


@pytest.mark.asyncio
async def test_quota_admins_do_not_change_global_framework_roles(tmp_path):
    value = service(tmp_path)
    original = copy.deepcopy(value.context.get_config())
    rpc = OpenRPC(value)
    await rpc.dispatch(
        OpenCall(
            action="limits",
            actor="dashboard/root",
            payload={
                "version": value.settings["version"],
                "paused": False,
                "personal_limit": 20,
                "global_limit": 1000,
                "admins": ["1"],
            },
        )
    )
    assert value.context.get_config() == original
    assert value.quota_admins == ["1"]
    reopened = OpenService(value.plugin, value.root, integrated=True)
    assert reopened.quota_admins == ["1"]
    with pytest.raises(OpenError, match="settings_changed"):
        await rpc.dispatch(
            OpenCall(
                action="limits",
                actor="dashboard/root",
                payload={
                    "version": 1,
                    "paused": False,
                    "personal_limit": 20,
                    "global_limit": 1000,
                    "admins": ["2"],
                },
            )
        )
    assert value.store.quota_admins() == ["1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,error",
    [
        ("existing/model", None),
        ("missing", "provider_unavailable"),
        (123, "budget_provider_changed"),
    ],
)
async def test_integrated_budget_requires_provider_binding_without_global_writes(
    tmp_path, provider, error
):
    value = service(tmp_path)
    before_config = copy.deepcopy(value.context.get_config())
    before_budget = value.store.budget()
    call = OpenCall(
        action="ai_budget",
        actor="dashboard/root",
        payload={
            "version": before_budget["version"],
            "confirmed": True,
            "provider": provider,
            "evidence": "Verified offline fixture cap",
        },
    )
    if error:
        with pytest.raises(OpenError, match=error):
            await OpenRPC(value).dispatch(call)
        assert value.store.budget() == before_budget
    else:
        assert await OpenRPC(value).dispatch(call) == {"saved": True}
        budget = value.store.budget()
        assert budget["confirmed"]
        assert budget["version"] == before_budget["version"] + 1
        assert budget["provider"] == provider
        assert budget["binding"] == value.store.model_binding(
            value.resolve_model({"provider_id": provider})
        )
    assert value.context.get_config() == before_config


@pytest.mark.asyncio
async def test_model_uses_existing_provider_without_copying_secrets_or_tools(tmp_path):
    value = service(tmp_path)
    value.configure_model({"provider_id": "existing/model"})
    assert value.resolve_model() == {
        "api_key": "server-only-test-key",
        "base_url": "https://model.example/v1",
        "model": "configured-model",
    }
    assert json.loads(value.model_path.read_text()) == {"provider_id": "existing/model"}
    response = await OpenRPC(value).dispatch(
        OpenCall(action="status", actor="dashboard/root")
    )
    assert response["mode"] == "integrated"
    assert "server-only-test-key" not in json.dumps(response)
    assert response["providers"] == [
        {"id": "existing/model", "model": "configured-model"}
    ]
    with pytest.raises(OpenError):
        value.configure_model({"provider_id": "missing"})
    assert value.model == {"provider_id": "existing/model"}
    with pytest.raises(OpenError, match="model_invalid"):
        value.configure_model(
            {
                "base_url": "https://model.example/v1",
                "model": "old",
                "api_key": "secret",
            }
        )
    assert json.loads(value.model_path.read_text()) == {"provider_id": "existing/model"}
    assert not value.model_path.with_suffix(".tmp").exists()


@pytest.mark.asyncio
async def test_embedded_control_never_starts_a_second_account_login(tmp_path):
    value = service(tmp_path)
    for action in ("login", "login_save"):
        with pytest.raises(OpenError, match="use_platform_login"):
            await OpenRPC(value).dispatch(
                OpenCall(action=action, actor="dashboard/root")
            )
    assert not value.context.platform_manager.platform_insts


@pytest.mark.parametrize(
    "text",
    [
        "我的邀请",
        "1",
        "我的邀请 2 昨日明细",
        "参加抽奖",
        "排名 2",
        "管理 添加关键词 abc",
        "管理 开启抽奖",
        "管理 抽奖奖励 20元",
    ],
)
def test_existing_fixed_queries_survive_merge(text):
    assert recognize_integrated(text)


@pytest.mark.parametrize(
    "text",
    [
        "删除好友 100",
        "/exec rm -rf /",
        "自然语言帮我禁言",
        "管理 添加关键词 abc\n管理 公告 abc",
    ],
)
def test_unmatched_commands_never_gain_authority(text):
    assert not recognize_integrated(text)


@pytest.mark.asyncio
async def test_my_invitation_bypasses_chat_and_only_calls_sender_bound_query(tmp_path):
    value = service(tmp_path)
    value.chat = AsyncMock()
    value.store.remember_roster("99", {"1": {"nimId": "1001"}})
    extra = {}
    event = SimpleNamespace(
        actor_identity=ActorIdentity("99", "1", "1001"),
        platform=SimpleNamespace(account="99", config={"id": "fixture"}),
        processing_completion=asyncio.get_running_loop().create_future(),
        message_str="我的邀请",
        eligible=True,
        get_sender_id=lambda: "1",
        is_private_chat=lambda: True,
        get_group_id=lambda: "",
        get_extra=lambda k: (
            extra.get(k)
            if k != "wangshangliao_payload"
            else {
                "recall_route": {"time": str(int(time.time() * 1000))},
            }
        ),
        set_extra=lambda k, v: extra.update({k: v}),
        send=AsyncMock(),
        plain_result=lambda text: text,
        stop_event=lambda: None,
    )
    bind_actor(event)
    await value.on_event(event)
    value.plugin.cashback.private_invitation_reply.assert_awaited_once_with(event)
    value.plugin.private_interaction.handle.assert_not_awaited()
    value.chat.assert_not_awaited()
    assert event.send.call_args.args[0] == "本人返现"


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_group_write_guard_does_not_require_model_wake_eligibility(
    tmp_path, private
):
    value = service(tmp_path)
    event = SimpleNamespace(
        is_private_chat=lambda: private,
        eligible=False,
        processing_completion=asyncio.get_running_loop().create_future(),
    )
    adapter = SimpleNamespace(account="99", config={"id": "fixture"})
    guard = value.write_guard(adapter, event)
    if private:
        with pytest.raises(OpenError, match="operation_revoked"):
            guard()
    else:
        guard()
        event.processing_completion.cancel()
        with pytest.raises(OpenError, match="operation_revoked"):
            guard()


@pytest.mark.asyncio
async def test_revoked_selected_group_reports_denial_without_reading_roster(tmp_path):
    value = service(tmp_path)
    value.management.allowed = Mock(return_value=False)
    value.roster = AsyncMock()
    adapter = SimpleNamespace(account="99", config={})
    event = SimpleNamespace(
        platform=adapter,
        get_sender_id=lambda: "1",
        get_group_id=lambda: "",
        unified_msg_origin="private/1",
        is_private_chat=lambda: True,
    )
    key = (id(adapter), "99", "1", "private/1")
    value.plugin.commands.selections[key] = {
        "group": "5",
        "expires": time.monotonic() + 60,
    }
    assert await value.manager_command(event, "确认授权 stale") == "无权限"
    value.roster.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_native_permissions_return_explicit_failure_not_success(tmp_path):
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    value = service(tmp_path)
    value.store.remember_roster("99", {"1": {"nimId": "1001"}})
    value.manager_command = AsyncMock(
        side_effect=ProtocolError("moderation_permission")
    )
    extras = {
        "wangshangliao_payload": {
            "created_at": int(time.time() * 1000),
        }
    }
    event = SimpleNamespace(
        actor_identity=ActorIdentity("99", "1", "1001"),
        platform=SimpleNamespace(account="99", config={"id": "fixture"}),
        processing_completion=asyncio.get_running_loop().create_future(),
        message_str="管理 全员禁言",
        eligible=True,
        get_sender_id=lambda: "1",
        is_private_chat=lambda: True,
        get_extra=lambda k: extras.get(k),
        set_extra=lambda k, v: extras.update({k: v}),
        plain_result=lambda text: text,
        send=AsyncMock(),
        stop_event=Mock(),
    )
    bind_actor(event)
    await value.on_event(event)
    assert "未执行" in event.send.call_args.args[0]
    assert "请先把我设为群管理员" in event.send.call_args.args[0]
    assert "原生" not in event.send.call_args.args[0]
    assert "成功" not in event.send.call_args.args[0]
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_activation_cooldown_is_shared_and_silent(tmp_path):
    value = service(tmp_path)
    value.store.remember_roster("99", {"1": {"nimId": "1001"}})
    value.command_at[("99", "nim/1001")] = time.monotonic()
    value.activate = AsyncMock()
    diagnostics = Mock()
    extras = {"wangshangliao_payload": {"created_at": int(time.time() * 1000)}}
    event = SimpleNamespace(
        actor_identity=ActorIdentity("99", "1", "1001"),
        platform=SimpleNamespace(
            account="99", config={"id": "fixture"}, diagnostics=diagnostics
        ),
        processing_completion=asyncio.get_running_loop().create_future(),
        message_obj=SimpleNamespace(message_id="safe-correlation"),
        message_str="认领 5 secret-code",
        eligible=True,
        get_sender_id=lambda: "1",
        is_private_chat=lambda: True,
        get_extra=lambda key: extras.get(key),
        set_extra=lambda key, val: extras.update({key: val}),
        plain_result=lambda text: text,
        send=AsyncMock(),
        stop_event=Mock(),
    )
    bind_actor(event)
    await value.on_event(event)
    event.send.assert_not_awaited()
    assert "secret-code" not in repr(diagnostics.emit.call_args)
    diagnostics.emit.assert_not_called()
    value.activate.assert_not_awaited()
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["/chat", "/exit"])
async def test_private_chat_controls_survive_astrbot_wake_prefix_stripping(
    tmp_path, control
):
    value = service(tmp_path)
    value.chat = AsyncMock()
    extras = {
        "wangshangliao_payload": {
            "text": control,
            "created_at": int(time.time() * 1000),
        }
    }
    event = SimpleNamespace(
        platform=SimpleNamespace(account="99", config={}),
        processing_completion=asyncio.get_running_loop().create_future(),
        message_str=control[1:],
        eligible=True,
        is_private_chat=lambda: True,
        get_extra=lambda k: extras.get(k),
        set_extra=lambda k, v: extras.update({k: v}),
        stop_event=Mock(),
    )
    bind_actor(event)
    await value.on_event(event)
    assert event.message_str == control
    value.plugin.private_interaction.handle.assert_awaited_once_with(event, control)
    value.chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["菜单", "帮助"])
async def test_existing_friend_can_request_fixed_menu_picture_link(tmp_path, command):
    from astrbot.builtin_stars.wangshangliao_moderation.feature_image import (
        GUIDE_LINKS,
    )

    value = service(tmp_path)
    value.verify_actor = AsyncMock()
    extras = {"wangshangliao_payload": {"created_at": int(time.time() * 1000)}}
    event = SimpleNamespace(
        actor_identity=ActorIdentity("99", "unverified", "1001"),
        platform=SimpleNamespace(account="99", config={}),
        processing_completion=asyncio.get_running_loop().create_future(),
        message_str=command,
        eligible=True,
        is_private_chat=lambda: True,
        get_extra=lambda key: extras.get(key),
        set_extra=lambda key, val: extras.update({key: val}),
        plain_result=lambda text: text,
        send=AsyncMock(),
        stop_event=Mock(),
    )
    bind_actor(event)
    await value.on_event(event)
    menu = value.navigation.screen(
        value.navigation.sessions[value.navigation.key(event)]
    )
    if command == "帮助":
        menu += "\n群内操作本群；私聊先发送「群列表」「选择群 1」。"
    assert event.send.call_args.args[0] == menu + "\n\n" + GUIDE_LINKS
    value.verify_actor.assert_not_awaited()
    value.plugin.cashback.private_invitation_reply.assert_not_awaited()
    value.plugin.private_interaction.handle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ordinary advertisement", "排名", "管理 规则"])
async def test_unmentioned_group_events_are_audited_and_fixed_commands_work(
    tmp_path, monkeypatch, text
):
    from astrbot.builtin_stars.wangshangliao_moderation import open_service

    value = service(tmp_path)
    value.manager_command = AsyncMock(return_value="rules")
    value.chat = AsyncMock()
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(open_service, "handle", handler)
    extras = {
        "wangshangliao_payload": {
            "recall_route": {"time": str(int(time.time() * 1000))},
            "sender": "1",
        },
    }
    event = SimpleNamespace(
        platform=SimpleNamespace(account="99", config={}),
        processing_completion=asyncio.get_running_loop().create_future(),
        message_obj=SimpleNamespace(message_id="11"),
        message_str=text,
        eligible=False,
        get_sender_id=lambda: "1",
        is_private_chat=lambda: False,
        get_group_id=lambda: "5",
        get_extra=lambda k: extras.get(k),
        set_extra=lambda k, v: extras.update({k: v}),
        send=AsyncMock(),
        plain_result=lambda text: text,
        stop_event=Mock(),
    )
    bind_actor(event)
    await value.on_event(event)
    if text == "ordinary advertisement":
        handler.assert_awaited_once()
        event.send.assert_not_awaited()
    else:
        event.send.assert_awaited_once()
        assert extras["wsl_plain_command"]
    value.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_group_management_does_not_require_a_chatbot_mention(tmp_path):
    value = service(tmp_path)
    import_tenant(value)
    tenant = value.store.tenant("99", "5")
    value.store.native_role("99", "5", tenant["epoch"], "GROUP_ROLE_ADMIN")
    token = value.store.preview_control(
        "99", "5", tenant["version"], "dashboard/root", tenant["rules"], ["1"], None
    )
    value.store.change(
        "99",
        "5",
        tenant["version"],
        "dashboard/root",
        managers=["1"],
        control=True,
        confirmation=token,
    )
    await value.refresh()
    event = SimpleNamespace(
        platform=SimpleNamespace(account="99", config={"enabled_groups": ["5"]}),
        eligible=False,
        processing_completion=asyncio.get_running_loop().create_future(),
        get_sender_id=lambda: "1",
        is_private_chat=lambda: False,
        get_extra=lambda _: None,
    )
    bind_actor(event)
    assert value.management.groups(event) == ["5"]
    event.is_private_chat = lambda: True
    assert value.management.groups(event) == []


@pytest.mark.asyncio
async def test_newly_opened_group_ignores_retired_disabled_group_reply_defaults(
    tmp_path,
):
    value = service(tmp_path)
    import_tenant(value)
    await value.refresh()
    original = {
        "enabled_groups": [],
        "reply_groups": {"5": False},
        "moderation": {"enabled": False, "automation_enabled": False},
    }
    overlay = value.policy_values("99", original)
    assert overlay["reply_groups"]["5"]
    assert overlay["moderation"]["enabled"]
    assert overlay["moderation"]["automation_enabled"]
    assert overlay["moderation"]["recall_enabled"]
    assert overlay["moderation"]["progressive_mute"]
    assert not original["moderation"]["enabled"]


@pytest.mark.asyncio
async def test_native_permission_save_never_persists_effective_tenant_overlay(
    page_app, headers, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation.dashboard import revision
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
        BUSINESS_FIELDS,
        runtime,
    )

    app, page, _, configs, _ = page_app
    base = {
        k: copy.deepcopy(v)
        for k, v in configs["fixture"].items()
        if k in BUSINESS_FIELDS
    }
    monkeypatch.setattr(
        runtime,
        "snapshots",
        {
            "fixture": PolicySnapshot("1", "1:1", json.dumps(base), time.time()),
        },
    )
    page.plugin = SimpleNamespace(open_service=SimpleNamespace(integrated=True))
    effective = configs["fixture"]
    effective["_business_revision"] = "1:1"
    effective["enabled_groups"].append("7")
    effective["moderation"]["permissions"]["7"] = ["mute", "recall"]
    effective["moderation"]["permissions"].pop("6")
    effective["proactive_send"]["targets"].append("7")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            "/api/v1/plugins/extensions/wangshangliao_moderation/settings",
            headers=headers,
            json={
                "bot_id": "fixture",
                "revision": revision(effective),
                "group_id": "7",
                "group": {"permissions": ["rename"]},
                "reply_private": False,
            },
        )
    assert response.status_code == 200, response.text
    saved = configs["fixture"]
    assert saved["enabled_groups"] == base["enabled_groups"]
    assert saved["proactive_send"] == base["proactive_send"]
    assert saved["moderation"]["permissions"] == {
        **base["moderation"]["permissions"],
        "7": ["rename"],
    }
    assert saved["moderation"]["semantic"] == base["moderation"]["semantic"]
    assert not saved["reply_private"]
