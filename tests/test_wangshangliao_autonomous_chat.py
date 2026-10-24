"""Bounded autonomous conversation, persona routing and native-source guards."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import group_ai
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


def inbound(adapter, text="今天下班后准备吃什么呀", mid="123", mentions=()):
    """Construct an authenticated event-shaped observation, without transport I/O."""
    payload = {"mentions": list(mentions)}
    return SimpleNamespace(
        platform=adapter,
        actor_identity=ActorIdentity("99", "2", "1002", "2"),
        get_sender_id=lambda: "2",
        get_sender_name=lambda: "测试成员",
        get_group_id=lambda: "5",
        is_private_chat=lambda: False,
        message_str=text,
        message_obj=SimpleNamespace(message_id=mid),
        get_extra=lambda key: payload,
    )


def batch():
    return (
        {
            "message": "123",
            "uid": "2",
            "peer": "1002",
            "name": "成员",
            "text": "今天下班后准备吃什么呀",
            "received": time.time(),
        },
    )


@pytest.mark.asyncio
async def test_ordinary_messages_coalesce_without_waiting_for_a_model(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    monkeypatch.setattr(group_ai, "COALESCE_SECONDS", 0.01)
    monkeypatch.setattr(group_ai, "is_managed_account", lambda uid: False)
    launches = []
    value.group_ai.launch = lambda *a, **kw: launches.append((a, kw))
    for index in range(20):
        await value.group_ai.observe(inbound(adapter, mid=str(index + 1)))
    assert not launches and len(value.group_ai.pending) == 1
    assert len(value.group_ai.buffers[("99", "5")]) == 12
    await asyncio.gather(*value.group_ai.pending.values())
    assert len(launches) == 1
    assert launches[0][0][2].startswith("auto/")
    assert len(launches[0][1]["context"]) == 12
    assert not value.group_ai.buffers and not value.group_ai.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "确认已付款 ABC",
        "结账",
        "我的邀请",
        "/chat",
        "12345",
        "收到",
        "验证码：123456",
        "银行卡尾号1234",
        "现在还能领多少返现",
        "转我20usdt可以吗",
        "Bearer test-secret-example",
        "access_token: test-secret",
        "手机号 13812345678",
        "给我发邮件 user@example.com",
        "哦",
        "x" * 2049,
    ],
)
async def test_commands_finance_secrets_and_low_information_are_not_buffered(
    rpc_scope, monkeypatch, text
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    monkeypatch.setattr(group_ai, "is_managed_account", lambda uid: False)
    await value.group_ai.observe(inbound(adapter, text))
    assert not value.group_ai.pending and not value.group_ai.buffers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked", ["moderated", "identity", "robot", "disabled", "audit"]
)
async def test_unsafe_observations_never_reach_autonomous_model(
    rpc_scope, monkeypatch, blocked
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    monkeypatch.setattr(group_ai, "is_managed_account", lambda uid: blocked == "robot")
    event = inbound(adapter)
    if blocked == "identity":
        event.actor_identity = ActorIdentity("99", "2", "9999", "")
    elif blocked == "disabled":
        row["config"]["autonomous"] = False
    elif blocked == "audit":
        event.get_extra("")["_audit_incomplete"] = True
    await value.group_ai.observe(event, consumed=blocked == "moderated")
    assert not value.group_ai.pending and not value.group_ai.buffers


@pytest.mark.asyncio
async def test_native_at_enters_group_ai_even_when_autonomous_is_off(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    assert not row["config"]["autonomous"]
    monkeypatch.setattr(group_ai, "is_managed_account", lambda uid: False)
    launches = []
    value.group_ai.launch = lambda *a, **kw: launches.append((a, kw))
    await value.group_ai.observe(inbound(adapter, mentions=["1099"]))
    assert len(launches) == 1
    assert launches[0][0][2] == "mention/123/2/1002"
    assert not value.group_ai.buffers


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", [False, True])
async def test_model_selects_quote_but_never_its_identity(
    rpc_scope, monkeypatch, quote
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    adapter.quote_message = AsyncMock(return_value={})
    requests = fake_http(
        monkeypatch,
        content=json.dumps(
            {
                "reply": True,
                "target": 1,
                "quote": quote,
                "text": "来碗热面怎么样？",
            }
        ),
    )
    await value.group_ai.run(adapter, row, "auto/one", "", context=batch())
    assert len(requests) == 1
    body = requests[0][1]["json"]
    assert "tools" not in body and "tool_choice" not in body
    assert json.loads(body["messages"][-1]["content"]) == [
        {
            "number": 1,
            "name": "成员",
            "text": "今天下班后准备吃什么呀",
        }
    ]
    assert body["max_completion_tokens"] <= 512
    assert body["response_format"] == {"type": "json_object"}
    assert "只影响text的语气" in body["messages"][-2]["content"]
    adapter.quote_message.assert_awaited_with("5", "123")
    args, kwargs = adapter.send_text.call_args
    assert args[0] == "5" and args[2] == "来碗热面怎么样？"
    assert kwargs.get("reply_to") == ("123" if quote else None)
    assert value.store.usage()["used"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        {"reply": False},
        {"reply": True, "target": 2, "quote": True, "text": "超出本群候选"},
        {"reply": True, "target": True, "quote": True, "text": "伪造序号"},
        {"reply": True, "target": 1, "quote": True, "text": "内容", "group": "6"},
        {"reply": False, "text": "不应发送"},
        {"reply": True, "target": 1, "quote": "yes", "text": "错误类型"},
        {"reply": True, "target": 1, "quote": False, "text": "", "tool_calls": []},
    ],
)
async def test_silence_or_invalid_target_never_sends_and_usage_is_accounted(
    rpc_scope, monkeypatch, decision
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    adapter.quote_message = AsyncMock()
    fake_http(monkeypatch, content=json.dumps(decision))
    await value.group_ai.run(adapter, row, "auto/silent", "", context=batch())
    adapter.send_text.assert_not_awaited()
    adapter.quote_message.assert_not_awaited()
    assert value.store.usage()["used"] == 1
    with value.store.connect() as db:
        charge = db.execute("SELECT state FROM ai_charges").fetchone()[0]
        call = db.execute("SELECT state FROM calls").fetchone()[0]
    assert charge == "settled"
    assert call == ("completed" if decision == {"reply": False} else "rejected")


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["", "```json\n{}\n```", "```\n{}\n```"])
@pytest.mark.parametrize("silent", [False, True])
async def test_json_envelope_preserves_decision_and_reports_outcome(
    rpc_scope, monkeypatch, wrapper, silent
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    adapter.quote_message = AsyncMock(return_value={})
    decision = (
        {"reply": False}
        if silent
        else {"reply": True, "target": 1, "quote": False, "text": "来碗热面怎么样？"}
    )
    content = json.dumps(decision)
    fake_http(monkeypatch, content=wrapper.format(content) if wrapper else content)
    logs = []
    monkeypatch.setattr(group_ai.logger, "info", lambda *args: logs.append(args))
    await value.group_ai.run(adapter, row, "auto/envelope", "", context=batch())
    with value.store.connect() as db:
        assert db.execute("SELECT state FROM calls").fetchone()[0] == "completed"
    if silent:
        adapter.send_text.assert_not_awaited()
    else:
        assert adapter.send_text.call_args.args[2] == "来碗热面怎么样？"
    assert logs[-1][-1] == ("model_silent" if silent else "none")
    assert "来碗热面" not in str(logs)
    assert "今天下班后" not in str(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '说明文字\n{"reply":false}',
        '```json\n{"reply":false}\n```\n附加指令',
        '```python\n{"reply":false}\n```',
        '{"reply":true,"target":999,"quote":false,"text":"错误目标"}',
    ],
)
async def test_bad_envelope_never_delivers_and_logs_safe_reason(
    rpc_scope, monkeypatch, content
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    fake_http(monkeypatch, content=content)
    logs = []
    monkeypatch.setattr(group_ai.logger, "info", lambda *args: logs.append(args))
    await value.group_ai.run(adapter, row, "auto/invalid-envelope", "", context=batch())
    adapter.send_text.assert_not_awaited()
    with value.store.connect() as db:
        assert db.execute("SELECT state FROM calls").fetchone()[0] == "rejected"
    assert logs[-1][-1] == "model_reply_invalid"
    assert content not in str(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "expired", "recalled", "identity"])
async def test_slow_model_cannot_deliver_a_stale_reply(rpc_scope, monkeypatch, change):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    adapter.quote_message = AsyncMock(return_value={})
    context = batch()
    barrier = (asyncio.Event(), asyncio.Event())
    fake_http(
        monkeypatch,
        content=json.dumps(
            {
                "reply": True,
                "target": 1,
                "quote": True,
                "text": "迟到回复",
            }
        ),
        barrier=barrier,
    )
    task = asyncio.create_task(
        value.group_ai.run(
            adapter,
            row,
            "auto/stale",
            "",
            context=context,
        )
    )
    await barrier[0].wait()
    if change == "disable":
        row["config"]["autonomous"] = False
    elif change == "expired":
        context[0]["received"] -= 121
    elif change == "recalled":
        adapter.quote_message.side_effect = ValueError("quote_unavailable")
    else:
        context[0]["peer"] = "9999"
    barrier[1].set()
    await task
    adapter.send_text.assert_not_awaited()
    assert not value.group_ai.rows()[0]["blocked"]


@pytest.mark.asyncio
async def test_busy_service_never_queues_model_work(rpc_scope, monkeypatch):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"]["autonomous"] = True
    monkeypatch.setattr(group_ai, "COALESCE_SECONDS", 0)
    monkeypatch.setattr(group_ai, "is_managed_account", lambda uid: False)
    value.chat_count = 2
    requests = fake_http(monkeypatch)
    await value.group_ai.observe(inbound(adapter))
    await asyncio.gather(*value.group_ai.pending.values())
    assert not requests and not value.group_ai.tasks and not value.group_ai.pending
    await value.group_ai.close()
    assert not value.group_ai.buffers


@pytest.mark.asyncio
@pytest.mark.parametrize("mention", [False, True])
async def test_all_group_modes_use_group_persona_not_customer_service(
    rpc_scope, monkeypatch, mention
):
    from tests.test_wangshangliao_conversation_profiles import configure, save

    value, adapter, _ = rpc_scope
    _, persona = configure(value)
    persona["prompt"] = "仅热群使用的独立人格标记"
    save(value, "group")
    row = await model_scope(value, adapter)
    requests = fake_http(monkeypatch)
    await value.group_ai.run(
        adapter,
        row,
        "mention/123/2/1002" if mention else "idle/0",
        "怎样查看命令" if mention else "",
    )
    body = requests[0][1]["json"]
    assert "仅热群使用的独立人格标记" in json.dumps(body, ensure_ascii=False)
    assert "只解释固定命令" not in body["messages"][0]["content"]
    assert adapter.send_text.call_args.args[2] == "大家今天有什么开心的事？"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "foreign_origin",
    [
        "rpc-offline:GroupMessage:99/5/wsl-ai/99/group/mention/1/1001",
        "rpc-offline:GroupMessage:99/6/wsl-ai/99/group/mention/2/1002",
        "rpc-offline:GroupMessage:199/5/wsl-ai/199/group/mention/2/1002",
        "rpc-offline:GroupMessage:99/5/wsl-ai/99/group/mention/2/9999",
        "rpc-offline:FriendMessage:99/private/1002/wsl-ai/99/customer",
        "rpc-offline:GroupMessage:99/5/wsl-ai/99/group",
        "rpc-offline:GroupMessage:99/5/wsl-ai/99/consultation",
    ],
)
async def test_native_at_history_is_isolated_by_account_group_member_and_mode(
    rpc_scope, monkeypatch, foreign_origin
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    manager = value.context.conversation_manager
    fingerprint = value.profiles.resolve("group", "99", "5")["fingerprint"]
    own_origin = (
        f"rpc-offline:GroupMessage:99/5/wsl-ai/99/group/{fingerprint}/mention/2/1002"
    )
    for account in ("99", "199"):
        foreign_origin = foreign_origin.replace(
            f"/wsl-ai/{account}/group",
            f"/wsl-ai/{account}/group/{fingerprint}",
        )
    own = await manager.new_conversation(
        own_origin,
        content=[
            {"role": "user", "content": "OWN_HISTORY"},
            {"role": "assistant", "content": "OWN_ANSWER"},
        ],
    )
    foreign = await manager.new_conversation(
        foreign_origin,
        content=[{"role": "user", "content": "FOREIGN_HISTORY"}],
    )
    requests = fake_http(monkeypatch, content="自然回复")
    await value.group_ai.run(adapter, row, "mention/123/2/1002", "接着聊聊")
    messages = json.dumps(requests[0][1]["json"]["messages"], ensure_ascii=False)
    assert "OWN_HISTORY" in messages and "OWN_ANSWER" in messages
    assert "FOREIGN_HISTORY" not in messages
    assert await manager.get_curr_conversation_id(own_origin) == own
    assert await manager.get_curr_conversation_id(foreign_origin) == foreign
    assert manager.db.rows[foreign].content == [
        {"role": "user", "content": "FOREIGN_HISTORY"}
    ]
    assert manager.db.rows[own].content[-2:] == [
        {"role": "user", "content": "接着聊聊"},
        {"role": "assistant", "content": "自然回复"},
    ]
    assert adapter.send_text.call_args.args[2] == "自然回复"


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [0, 1])
async def test_native_at_uses_bounded_group_override_not_private_history_settings(
    rpc_scope, monkeypatch, turns
):
    from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import (
        DEFAULT,
    )
    from tests.test_wangshangliao_conversation_profiles import configure, save

    value, adapter, _ = rpc_scope
    _, persona = configure(value)
    persona["prompt"] = "EXACT_GROUP_STYLE"
    save(value, "consultation", selection={**DEFAULT, "history_turns": 20})
    save(
        value,
        "group",
        account="99",
        group="5",
        selection={**DEFAULT, "config_id": "default", "history_turns": turns},
    )
    row = await model_scope(value, adapter)
    manager = value.context.conversation_manager
    fingerprint = value.profiles.resolve("group", "99", "5")["fingerprint"]
    cid = await manager.new_conversation(
        f"rpc-offline:GroupMessage:99/5/wsl-ai/99/group/{fingerprint}/mention/2/1002",
        content=[
            {"role": "user", "content": "OLD_HISTORY"},
            {"role": "assistant", "content": "OLD_ANSWER"},
            {"role": "user", "content": "RECENT_HISTORY"},
            {"role": "assistant", "content": "RECENT_ANSWER"},
        ],
    )
    requests = fake_http(monkeypatch, content="热群回复")
    await value.group_ai.run(adapter, row, "mention/123/2/1002", "今天聊什么")
    body = requests[0][1]["json"]
    encoded = json.dumps(body["messages"], ensure_ascii=False)
    assert "EXACT_GROUP_STYLE" in encoded and "OLD_HISTORY" not in encoded
    assert ("RECENT_HISTORY" in encoded) is bool(turns)
    assert body["max_completion_tokens"] <= 512
    assert manager.db.rows[cid].content == [
        {"role": "user", "content": "今天聊什么"},
        {"role": "assistant", "content": "热群回复"},
    ]


@pytest.mark.asyncio
async def test_private_profile_change_does_not_revoke_group_at_reply(
    rpc_scope, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import (
        DEFAULT,
    )
    from tests.test_wangshangliao_conversation_profiles import save

    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    barrier = (asyncio.Event(), asyncio.Event())
    requests = fake_http(monkeypatch, barrier=barrier)
    task = asyncio.create_task(
        value.group_ai.run(adapter, row, "mention/123/2/1002", "今天好吗")
    )
    await barrier[0].wait()
    try:
        save(value, "consultation", selection={**DEFAULT, "history_turns": 0})
    finally:
        barrier[1].set()
    await task
    assert len(requests) == 1
    adapter.send_text.assert_awaited_once()
