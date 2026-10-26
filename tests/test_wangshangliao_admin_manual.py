"""Generated administrator procedures are actually retrieved, not just provisioned."""

import json

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.conversation_guidance import (
    MANUAL_PREFIX,
    admin_manual_documents,
)
from astrbot.builtin_stars.wangshangliao_moderation.knowledge_store import (
    KnowledgeScope,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_admin_guidance import admin_profile
from tests.test_wangshangliao_conversation_profiles import configure
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_integrated_service import service
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_public_hardening import chat_event
from tests.test_wangshangliao_scoped_knowledge import edit


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("关键词自动回复怎么添加", "添加回复 名称|精确"),
        ("定时广告每天发广告怎么设置", "添加广告 名称|每日"),
        ("每天定时禁言和解除时间怎么设置", "管理 定时禁言 23:00 08:00"),
        ("私信选择群怎么操作", "管理 选择群"),
        ("客服负责人怎么添加客服知识", "添加客服知识"),
        ("免费指引试用次数每天重置吗", "不每日重置"),
        ("成员发言白名单怎么添加", "真正@一名成员并发送「白名单」"),
    ],
)
def test_manual_is_searchable_for_real_procedures(tmp_path, question, expected):
    value = service(tmp_path)
    result = value.support.sync_guidance_manual("server/test", guard=lambda: None)
    assert result["entries"] == len(admin_manual_documents()) > 10
    snapshot, references = value.knowledge.store.retrieve(
        KnowledgeScope("admin_manual"), question
    )
    assert snapshot.version > 0
    assert expected in json.dumps(references, ensure_ascii=False)


def test_manual_sync_is_idempotent_preserves_unmanaged_and_other_scopes(tmp_path):
    value = service(tmp_path)
    knowledge = value.knowledge.store
    manual = KnowledgeScope("admin_manual")
    support = KnowledgeScope("support", account="99")
    edit(knowledge, manual, title="服务端自定义说明", body="保留人工维护资料")
    edit(knowledge, support, title="机器人业务", body="ACCOUNT_ONLY")
    first = value.support.sync_guidance_manual("server/test", guard=lambda: None)
    before = knowledge.read(manual)
    second = value.support.sync_guidance_manual("server/test", guard=lambda: None)
    assert first == second and knowledge.read(manual) == before
    assert any(row["body"] == "保留人工维护资料" for row in before["entries"])
    assert knowledge.read(support)["entries"][0]["body"] == "ACCOUNT_ONLY"
    assert not knowledge.retrieve(support, "定时禁言")[1]
    number = next(
        n
        for n, row in enumerate(before["entries"], 1)
        if row["title"] == MANUAL_PREFIX + "关键词自动回复"
    )
    edit(
        knowledge,
        manual,
        "edit",
        number=number,
        title=MANUAL_PREFIX + "关键词自动回复",
        body="已过时资料",
    )
    stale = knowledge.read(manual)["snapshot"]
    value.support.sync_guidance_manual("server/test", guard=lambda: None)
    with pytest.raises(OpenError, match="knowledge_changed"):
        knowledge.assert_current(stale)
    assert "添加回复 名称|精确" in str(knowledge.retrieve(manual, "关键词自动回复"))


def test_manual_sync_revocation_never_writes(tmp_path):
    value = service(tmp_path)

    def revoked():
        raise OpenError("operation_revoked")

    with pytest.raises(OpenError, match="operation_revoked"):
        value.support.sync_guidance_manual("server/test", guard=revoked)
    assert not value.knowledge.store.read(KnowledgeScope("admin_manual"))["entries"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("关键词自动回复怎么添加", "添加回复 名称|精确"),
        ("成员发言白名单怎么添加", "真正@一名成员并发送「白名单」"),
    ],
)
async def test_administrator_chat_includes_retrieved_procedure_without_tools(
    rpc_scope,
    monkeypatch,
    question,
    expected,
):
    value, adapter, _ = rpc_scope
    configure(value)
    admin_profile(value)
    value.support.sync_guidance_manual("server/test", guard=lambda: None)
    await model_scope(value, adapter)
    requests = fake_http(monkeypatch)
    event = chat_event(adapter)
    event.message_str = question
    await value.chat(event)
    assert len(requests) == 1
    payload = requests[0][1]["json"]
    messages = json.dumps(payload["messages"], ensure_ascii=False)
    assert expected in messages
    assert "已授权知识参考" in messages
    assert not {"tools", "tool_choice", "functions"} & payload.keys()
