"""Existing AstrBot profiles may supply conversation style, never agent authority."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.command_menu import TREE
from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import DEFAULT
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_integrated_service import service
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_public_hardening import chat_event


def configure(value):
    provider = value.context.get_provider_by_id("existing/model")
    provider.provider_config["custom_extra_body"] = {
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 800,
        "tools": [{"type": "dangerous"}],
        "messages": [{"content": "secret"}],
        "stream": True,
        "extra_headers": {"private": "secret"},
    }
    cfg = {
        "agent_runner": {
            "runner_type": "local",
            "config": {
                "model": {"provider_id": "existing/model", "request_max_retries": 99},
                "persona": {"persona_id": "support"},
                "compression": {"max_turns": 3, "overflow_strategy": "llm_compress"},
            },
        },
        "kb_names": ["private-finance"],
        "admins_id": ["injected-admin"],
    }
    persona = {
        "name": "support",
        "prompt": "说话简洁友好。",
        "tools": None,
        "skills": ["shell"],
    }
    value.context.astrbot_config_mgr = SimpleNamespace(
        confs={"default": cfg},
        get_conf_list=lambda: [
            {"id": "default", "name": "客服配置", "path": "/secret"}
        ],
    )
    value.context.persona_manager = SimpleNamespace(
        personas_v3=[persona],
        get_persona_v3_by_id=lambda key: persona if key == "support" else None,
    )
    value.model = {"provider_id": "existing/model"}
    return cfg, persona


def save(
    value, kind="consultation", *, account="", group="", version=0, selection=None
):
    value.profiles.save(
        kind,
        account,
        group,
        version,
        {**DEFAULT, "config_id": "default"} if selection is None else selection,
        "dashboard/test",
        guard=lambda: None,
        tenant_version=value.store.tenant(account, group)["version"] if group else None,
        rebind=True,
    )


@pytest.mark.parametrize("global_binding", [False, True])
@pytest.mark.parametrize("account_binding", ["absent", "null", "empty"])
def test_account_consultation_requires_own_nonempty_binding(
    tmp_path, global_binding, account_binding
):
    value = service(tmp_path)
    configure(value)
    if global_binding:
        save(value)
    save(value, account="199")
    if account_binding != "absent":
        with value.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO ai_profiles VALUES(?,?,?,?,?)",
                (
                    "consultation",
                    "99",
                    "",
                    1,
                    json.dumps(None if account_binding == "null" else {}),
                ),
            )
    with pytest.raises(OpenError, match="ai_profile_missing"):
        value.profiles.resolve("consultation", "99")
    assert value.store.usage()["used"] == 0


def test_account_profiles_ignore_global_selection_and_version_changes(tmp_path):
    value = service(tmp_path)
    _, persona = configure(value)
    save(value)
    save(value, account="99", selection={**DEFAULT, "persona_id": "__builtin__"})
    save(value, account="199")
    own = value.profiles.resolve("consultation", "99")
    other = value.profiles.resolve("consultation", "199")
    assert own["prompt"] == ""
    assert own["versions"] == [("consultation", "99", "", 1)]
    assert other["prompt"] == persona["prompt"]
    save(value, version=1, selection={**DEFAULT, "persona_id": "__builtin__"})
    assert value.profiles.resolve("consultation", "99") == own
    assert value.profiles.resolve("consultation", "199") == other
    persona["prompt"] += " changed"
    value.profiles.assert_current(own, "consultation", "99")
    with pytest.raises(OpenError, match="ai_profile_changed"):
        value.profiles.assert_current(other, "consultation", "199")


@pytest.mark.parametrize("remove", [False, True])
def test_missing_account_binding_revokes_snapshot_without_global_fallback(
    tmp_path, remove
):
    value = service(tmp_path)
    configure(value)
    save(value)
    save(value, account="99")
    snapshot = value.profiles.resolve("consultation", "99")
    with value.store.connect(write=True) as db:
        if remove:
            db.execute("DELETE FROM ai_profiles WHERE account='99'")
        else:
            db.execute("UPDATE ai_profiles SET selection='null' WHERE account='99'")
    with pytest.raises(OpenError, match="ai_profile_missing"):
        value.profiles.assert_current(snapshot, "consultation", "99")


@pytest.mark.asyncio
async def test_unbound_private_consultation_never_calls_global_persona_or_bills(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    configure(value)
    save(value)
    await model_scope(value, adapter)
    requests = fake_http(monkeypatch)
    event = chat_event(adapter)
    await value.chat(event)
    assert not requests
    assert value.store.usage()["used"] == 0
    assert not value.context.conversation_manager.db.rows
    assert "AI咨询暂不可用" in event.send.call_args.args[0]


def test_profile_resolves_only_allowlisted_values_and_never_copies_secrets(tmp_path):
    value = service(tmp_path)
    configure(value)
    save(value)
    profile = value.profiles.resolve("consultation")
    assert profile["parameters"] == {
        "temperature": 0.7,
        "top_p": 0.9,
        "max_completion_tokens": 800,
    }
    assert profile["history_turns"] == 3
    assert profile["prompt"] == "说话简洁友好。"
    assert not {"tools", "skills", "kb_names", "admins_id"} & profile.keys()
    inventory = json.dumps(value.profiles.inventory())
    assert "/secret" not in inventory and "secret" not in inventory
    assert "说话简洁" not in inventory
    rows = json.dumps(value.profiles.rows())
    assert "server-only-test-key" not in rows and "api_base" not in rows
    with pytest.raises(OpenError, match="ai_profile_changed"):
        save(value)
    reopened = service(tmp_path)
    configure(reopened)
    assert (
        reopened.profiles.resolve("consultation")["fingerprint"]
        == profile["fingerprint"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature", 3),
        ("temperature", True),
        ("temperature", float("nan")),
        ("top_p", -1),
        ("max_tokens", 4097),
        ("max_tokens", 2.5),
        ("history_turns", 21),
        ("history_turns", True),
        ("config_id", "../config"),
        ("persona_id", "missing"),
        ("provider_id", "missing"),
    ],
)
def test_invalid_selection_never_persists_or_consumes_quota(tmp_path, field, value):
    current = service(tmp_path)
    configure(current)
    with pytest.raises(OpenError):
        save(current, selection={**DEFAULT, field: value})
    assert current.profiles.rows() == [] and current.store.usage()["used"] == 0


@pytest.mark.parametrize(
    "dialogs",
    [
        "not-a-list",
        {"role": "system", "content": "not-a-dialogue"},
        ["unpaired"],
        ["question", {"tool_calls": []}],
        ["question", ""],
        ["question", " "],
        ["question", "字" * 683],
        ["question", "answer"] * 17,
        ["a" * 1024, "b" * 1024] * 9,
    ],
)
def test_invalid_persona_examples_never_reserve_quota_or_grant_roles(tmp_path, dialogs):
    value = service(tmp_path)
    _, persona = configure(value)
    persona["begin_dialogs"] = dialogs
    with pytest.raises(OpenError):
        save(value, "group")
    assert not value.profiles.rows()
    assert value.store.usage()["used"] == 0


@pytest.mark.parametrize("dialogs", [None, []])
def test_personas_without_examples_keep_existing_payload(tmp_path, dialogs):
    value = service(tmp_path)
    _, persona = configure(value)
    persona["begin_dialogs"] = dialogs
    save(value)
    profile = value.profiles.resolve("consultation")
    assert profile["examples"] == []
    assert len(value.profiles.persona_messages(profile)) == 1


@pytest.mark.asyncio
async def test_private_examples_are_style_only_and_never_saved_as_user_history(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    _, persona = configure(value)
    persona["begin_dialogs"] = ["preset-only question", "preset-only answer"]
    save(value, account="99")
    await model_scope(value, adapter)
    requests = fake_http(monkeypatch, content="你好")
    event = chat_event(adapter)
    await value.chat(event)
    messages = requests[0][1]["json"]["messages"]
    assert messages[3:5] == [
        {"role": "user", "content": "preset-only question"},
        {"role": "assistant", "content": "preset-only answer"},
    ]
    assert "不是当前群聊" in messages[2]["content"]
    assert "预设示例结束" in messages[5]["content"]
    assert messages[-1]["content"] == event.message_str
    cid = await value.context.conversation_manager.get_curr_conversation_id(
        event.unified_msg_origin
        + "/wsl-ai/99/customer/"
        + value.profiles.resolve("consultation", "99")["fingerprint"]
        + "/"
        + value.support.fingerprint("99")
    )
    history = value.context.conversation_manager.db.rows[cid].content
    assert "preset-only" not in json.dumps(history)
    assert event.send.call_args.args[0] == "你好"


@pytest.mark.parametrize(
    "mutation", ["profile", "persona", "examples", "credential", "remove"]
)
def test_live_edits_revoke_frozen_profile(tmp_path, mutation):
    value = service(tmp_path)
    cfg, persona = configure(value)
    save(value)
    snapshot = value.profiles.resolve("consultation")
    if mutation == "profile":
        cfg["agent_runner"]["config"]["compression"]["max_turns"] = 0
    elif mutation == "persona":
        persona["prompt"] += "变化"
    elif mutation == "examples":
        persona["begin_dialogs"] = ["示例提问", "示例回答"]
    elif mutation == "credential":
        value.context.get_provider_by_id("existing/model").get_current_key = lambda: (
            "new-key"
        )
    else:
        value.context.astrbot_config_mgr.confs.clear()
    with pytest.raises(OpenError):
        value.profiles.assert_current(snapshot, "consultation")


@pytest.mark.asyncio
async def test_group_override_is_scoped_and_can_return_to_default(rpc_scope):
    value, _, _ = rpc_scope
    configure(value)
    save(value, "group")
    save(
        value,
        "group",
        account="99",
        group="5",
        selection={**DEFAULT, "persona_id": "__builtin__", "temperature": 0.1},
    )
    assert value.profiles.resolve("group", "99", "5")["prompt"] == ""
    assert value.profiles.resolve("group", "199", "5")["prompt"] == "说话简洁友好。"
    assert (
        value.profiles.resolve("group", "99", "6")["parameters"]["temperature"] == 0.7
    )
    row = value.store.tenant("99", "5")
    value.profiles.save(
        "group",
        "99",
        "5",
        1,
        None,
        "dashboard/test",
        tenant_version=row["version"],
        guard=lambda: None,
    )
    assert (
        value.profiles.resolve("group", "99", "5")["parameters"]["temperature"] == 0.7
    )
    with pytest.raises(OpenError, match="ai_profile_changed"):
        value.profiles.save(
            "group",
            "99",
            "5",
            1,
            None,
            "dashboard/test",
            tenant_version=row["version"],
            guard=lambda: None,
        )
    value.store.depart("99", "5", row["epoch"], "test")
    assert not any(
        r["account"] == "99" and r["group_id"] == "5" for r in value.profiles.rows()
    )
    assert value.profiles.rows()[0]["kind"] == "group"


@pytest.mark.asyncio
async def test_rpc_saves_global_and_group_profiles_without_enabling_ai(rpc_scope):
    value, _, _ = rpc_scope
    configure(value)
    save(value)
    save(value, "group")
    for kind, account, group in [
        ("consultation", "", ""),
        ("group", "", ""),
        ("group", "99", "5"),
    ]:
        payload = {
            "kind": kind,
            "account": account,
            "group_id": group,
            "version": 0 if account else 1,
            "selection": {**DEFAULT, "config_id": "default"},
        }
        if account:
            payload["tenant_version"] = value.store.tenant(account, group)["version"]
        assert await OpenRPC(value).dispatch(
            OpenCall(action="ai_profile", actor="dashboard/test", payload=payload)
        ) == {"saved": True}
    assert not value.budget["confirmed"] and not value.group_ai.configs
    result = await OpenRPC(value).dispatch(
        OpenCall(action="status", actor="dashboard/test")
    )
    assert result["ai_choices"]["configs"][0]["name"] == "客服配置"
    assert "server-only-test-key" not in json.dumps(result)


@pytest.mark.asyncio
async def test_private_request_uses_persona_options_and_own_bounded_history(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    configure(value)
    save(
        value,
        account="99",
        selection={
            **DEFAULT,
            "config_id": "default",
            "history_turns": 1,
            "max_tokens": 300,
        },
    )
    await model_scope(value, adapter)
    event = chat_event(adapter)
    await value.context.conversation_manager.new_conversation(
        event.unified_msg_origin
        + "/wsl-ai/99/customer/"
        + value.profiles.resolve("consultation", "99")["fingerprint"]
        + "/"
        + value.support.fingerprint("99"),
        content=[
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old"},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "recent"},
        ],
    )
    requests = fake_http(monkeypatch, content="你好")
    await value.chat(event)
    body = requests[0][1]["json"]
    assert body["temperature"] == 0.7 and body["max_completion_tokens"] == 300
    assert "说话简洁友好" in body["messages"][1]["content"]
    assert "old" not in json.dumps(body)
    assert "tools" not in body and "stream" not in body
    assert len(body["messages"]) == 6
    assert event.send.call_args.args[0] == "你好"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["prompt", "examples"])
async def test_persona_change_during_slow_model_suppresses_late_reply(
    rpc_scope, monkeypatch, mutation
):
    value, adapter, _ = rpc_scope
    _, persona = configure(value)
    save(value, "group")
    row = await model_scope(value, adapter)
    barrier = (asyncio.Event(), asyncio.Event())
    requests = fake_http(monkeypatch, barrier=barrier)
    task = asyncio.create_task(
        value.group_ai.run(adapter, row, "mention/m1/2/1002", "你好")
    )
    await barrier[0].wait()
    if mutation == "prompt":
        persona["prompt"] = "新的人格"
    else:
        persona["begin_dialogs"] = ["新的提问", "新的语气"]
    barrier[1].set()
    await task
    assert len(requests) == 1
    adapter.send_text.assert_not_awaited()
    assert not value.group_ai.configs[("99", "5")]["blocked"]


def test_ai_consultation_and_group_switches_have_separate_menu_branches():
    assert [child.command for child in TREE[7].children[:4]] == [
        "/chat",
        "/exit",
        "我的套餐",
        "套餐说明",
    ]
    assert TREE[8].children[0].command == "热群状态"
    assert TREE[7].children[4].label == "我的知识库"
    assert TREE[8].children[6].label == "本群知识库"
