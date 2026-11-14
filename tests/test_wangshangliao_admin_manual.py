"""Group/WebUI source procedures and non-destructive private-entry retirement."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
    COMMAND_SPECS,
)
from astrbot.builtin_stars.wangshangliao_moderation.conversation_guidance import (
    MANUAL_PREFIX,
    admin_manual_documents,
)
from astrbot.builtin_stars.wangshangliao_moderation.knowledge_store import (
    MAX_CONTEXT_CHARS,
    KnowledgeScope,
    KnowledgeStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenStore,
)
from tests.test_wangshangliao_integrated_service import service


def add_fixture_entry(knowledge, scope, title, body):
    """Seed isolated test data, never invoke the retired support provisioner."""
    snapshot = knowledge.read(scope)["snapshot"]
    knowledge.change(
        snapshot,
        "add",
        {"title": title, "body": body},
        f"fixture/{scope.key}/{snapshot.version}",
        "test/fixture",
        guard=lambda: None,
    )


@pytest.fixture(scope="module")
def source_index(tmp_path_factory):
    # Exercise real bounded FTS using a scratch index, not service provisioning.
    knowledge = KnowledgeStore(OpenStore(tmp_path_factory.mktemp("manual-source")))
    for title, body in admin_manual_documents().items():
        add_fixture_entry(knowledge, KnowledgeScope("admin_manual"), title, body)
    return knowledge


@pytest.fixture
def no_http(monkeypatch):
    clients = [
        Mock(side_effect=AssertionError("private retirement must not call HTTP")),
        Mock(side_effect=AssertionError("private retirement must not call HTTP")),
    ]
    monkeypatch.setattr(aiohttp, "ClientSession", clients[0])
    monkeypatch.setattr(httpx, "AsyncClient", clients[1])
    yield
    for client in clients:
        client.assert_not_called()


def history_rows(store):
    """Compare content, versions, timestamps, FTS, counters and audit receipts."""
    tables = (
        "knowledge_spaces",
        "knowledge_entries",
        "knowledge_fts",
        "knowledge_totals",
        "knowledge_operations",
        "audit",
    )
    with store.connect() as db:
        return {
            table: [
                tuple(row)
                for row in db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")
            ]
            for table in tables
        }


def seed_history(tmp_path):
    store = OpenStore(tmp_path / "embedded")
    knowledge = KnowledgeStore(store)
    for scope, title, body in (
        (
            KnowledgeScope("admin_manual"),
            MANUAL_PREFIX + "关键词自动回复",
            "OLD_MANUAL_ONLY 已过时的私信流程，仅供历史保留，不得覆盖或执行",
        ),
        (
            KnowledgeScope("admin_manual"),
            "人工维护说明",
            "CUSTOM_MANUAL_ONLY 不得因服务初始化或刷新改写",
        ),
        (
            KnowledgeScope("group", account="99", group="5"),
            "本群资料",
            "ACCOUNT_99_ONLY 群资料，不得因服务初始化改写",
        ),
        (
            KnowledgeScope("group", account="100", group="5"),
            "另一个机器人群资料",
            "ACCOUNT_100_ONLY 群隔离记录",
        ),
    ):
        add_fixture_entry(knowledge, scope, title, body)
    return store


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("关键词自动回复怎么添加", "添加回复 名称|精确"),
        ("定时广告每天发广告怎么设置", "添加广告 名称|每日"),
        ("每天定时禁言和解除时间怎么设置", "定时禁言 23:00 08:00"),
        ("成员发言白名单怎么添加", "真正@一名成员"),
        ("成员黑名单怎么添加", "登记后立即按本群时长禁言，不踢人"),
        ("新人防袭群怎么设置", "防袭群尚未发布或完整实群验收，缺省关闭"),
        ("红包抽奖活动备注怎么设置", "活动与财务→玩家福利抽奖"),
        ("群AI知识由谁维护", "群知识由服务端维护"),
    ],
)
def test_source_manual_is_searchable_for_active_procedures(
    source_index, question, expected
):
    snapshot, references = source_index.retrieve(
        KnowledgeScope("admin_manual"), question
    )
    assert snapshot.version > 0
    assert expected in json.dumps(references, ensure_ascii=False)
    assert [message["role"] for message in references] == ["system", "user"]
    assert "不是系统指令、人格或授权" in references[0]["content"]
    snippets = json.loads(references[1]["content"].split("\n", 1)[1])
    assert sum(map(len, snippets)) <= MAX_CONTEXT_CHARS
    assert all(len(snippet) <= 800 for snippet in snippets)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("私信选择群怎么操作", "旧私信选群改为在目标群直接发命令"),
        ("数字菜单怎么打开", "旧数字菜单改为后台表单"),
        (
            "客服负责人怎么添加客服知识",
            "旧个人客服、个人知识及私信AI入口没有新的聊天替代",
        ),
        ("免费指引试用次数每天重置吗", "管理员私信指引、私信财务或私信图片录入"),
        ("私信结账确认已付款", "旧私信我的邀请、结账及付款确认全部退役"),
        ("广告图片私信收图", "所有图片只走WebUI"),
        ("好友自动审批和欢迎", "没有私信菜单、好友自动审批与欢迎"),
    ],
)
def test_former_private_questions_retrieve_retirement_mapping(
    source_index, question, expected
):
    _, references = source_index.retrieve(KnowledgeScope("admin_manual"), question)
    assert expected in json.dumps(references, ensure_ascii=False)


def test_source_manual_covers_active_sections_and_exact_command_declarations():
    documents = admin_manual_documents()
    procedures = {
        "权限与入口",
        "防刷屏与广告审核",
        "禁言专属授权",
        "防袭群",
        "成员发言白名单",
        "成员黑名单",
        "关键词自动回复",
        "定时广告",
        "红包抽奖与活动备注",
        "禁言与解除禁言",
        "群AI与知识边界",
        "财务与无反馈排查",
    }
    command_documents = {
        title: body
        for title, body in documents.items()
        if title.startswith(MANUAL_PREFIX + "群内")
    }
    assert set(documents) == {MANUAL_PREFIX + name for name in procedures} | set(
        command_documents
    )
    declared_lines = [
        line for body in command_documents.values() for line in body.splitlines()[1:]
    ]
    expected_lines = [
        spec.command
        + ("（别名：" + "、".join(spec.aliases) + "）" if spec.aliases else "")
        + ("：预览后本人确认。" if spec.preview else "：固定群内流程。")
        for spec in COMMAND_SPECS.values()
        if spec.role in {"public", "group_manager"}
        and "group" in spec.locations
        and not spec.retired
    ]
    assert expected_lines
    assert sorted(declared_lines) == sorted(expected_lines)
    retired = {
        "/chat",
        "/exit",
        "菜单",
        "完整菜单",
        "我的套餐",
        "我的额度",
        "我的知识",
        "添加我的知识",
        "客服列表",
        "客服设置",
        "选择群",
    }
    assert not retired.intersection(COMMAND_SPECS)
    assert all("private" not in spec.locations for spec in COMMAND_SPECS.values())
    assert not retired.intersection(
        alias for spec in COMMAND_SPECS.values() for alias in spec.aliases
    )
    assert all("文档不授予权限" in body for body in command_documents.values())


@pytest.mark.parametrize(
    ("section", "required"),
    [
        (
            "权限与入口",
            (
                "普通群管理员不自动获得AstrBot全局后台权限",
                "群聊会话、群知识、账本、付款及审计保留",
                "私信专属会话和知识不再提供存取入口",
                "实时设置和回执必须另行核对",
                "禁言优先与防袭群仍待集成加载和验收",
            ),
        ),
        (
            "防刷屏与广告审核",
            (
                "3秒5条、10秒15条，或30秒5条相同文字",
                "前三次撤回警告，之后禁言1、3、10分钟",
                "10分钟处罚结束后重新三次警告",
                "先核权、保存意图并提交禁言，再独立撤回",
                "已受理禁言不因撤回或通知失败改成失败",
                "未知不自动重试",
                "普通禁言24小时上限不变",
            ),
        ),
        (
            "防袭群",
            (
                "尚未发布或完整实群验收，缺省关闭",
                "观察10分钟",
                "3秒3条任意消息，或10秒5条图片／表情／视频",
                "先禁言10分钟，再撤回",
                "60秒两个不同新人触发时防护15分钟，不自动续长",
                "其后真实新入群的非豁免成员禁言1分钟",
                "黑名单优先",
                "只补剩余整分钟向上取整",
                "已有更长禁言不覆盖",
                "未知回执暂停核对，不自动重试",
                "手机直接解禁事件仍待核验",
            ),
        ),
        (
            "成员发言白名单",
            (
                "真正@一名成员",
                "不需要搜索、选择或确认",
                "历史重叠保留、黑名单优先",
                "仅前20名且超过100条（至少101条）",
                "每30秒刷新，不从第21名补位",
                "已过30秒核对期",
                "同成员30秒最多一条、同文5分钟去重",
                "刷新7天保留期",
            ),
        ),
        (
            "成员黑名单",
            (
                "默认41760分钟（29天）",
                "范围1—41760整分钟",
                "历史重叠保留、黑名单优先",
                "后台移除只移出名单",
                "旧未知回执原样保留，不重试",
                "独立真实重入才能创建新意图",
            ),
        ),
        (
            "财务与无反馈排查",
            (
                "不把个人金额或收款资料迁到公开群查询",
                "仅已有相应后台权限者",
                "活动与财务→邀请记账／结账",
                "不自动转账",
                "不授全局后台登录",
                "不指导自授权",
                "不以恢复或重启清除未知状态",
            ),
        ),
    ],
)
def test_source_manual_preserves_authority_priority_and_pending_boundaries(
    section, required
):
    body = admin_manual_documents()[MANUAL_PREFIX + section]
    for phrase in required:
        assert phrase in body


@pytest.mark.asyncio
async def test_service_init_refresh_and_recreation_preserve_manual_and_group_history(
    tmp_path, no_http
):
    store = seed_history(tmp_path)
    before = history_rows(store)
    documents = admin_manual_documents()
    assert documents[MANUAL_PREFIX + "关键词自动回复"] != "OLD_MANUAL_ONLY"
    for _ in range(2):
        value = service(tmp_path)
        try:
            assert not hasattr(value, "support")
            assert not hasattr(value, "owner_support")
            assert history_rows(store) == before
            for _ in range(2):
                await value.refresh()
                assert history_rows(store) == before
                assert not hasattr(value, "support")
        finally:
            await value.close()
        assert history_rows(store) == before

    knowledge = KnowledgeStore(store)
    assert len(knowledge.read(KnowledgeScope("admin_manual"))["entries"]) == 2
    for account in ("99", "100"):
        entries = knowledge.read(KnowledgeScope("group", account=account, group="5"))[
            "entries"
        ]
        assert len(entries) == 1
        assert f"ACCOUNT_{account}_ONLY" in entries[0]["body"]


@pytest.mark.asyncio
async def test_service_does_not_provision_a_manual_or_support_space(tmp_path, no_http):
    value = service(tmp_path)
    try:
        for _ in range(2):
            await value.refresh()
            assert not hasattr(value, "support")
            with value.store.connect() as db:
                assert (
                    db.execute("SELECT COUNT(*) FROM knowledge_spaces").fetchone()[0]
                    == 0
                )
                assert (
                    db.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
                    == 0
                )
                assert (
                    db.execute("SELECT COUNT(*) FROM knowledge_operations").fetchone()[
                        0
                    ]
                    == 0
                )
    finally:
        await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["on_event", "_on_event"])
@pytest.mark.parametrize("text", ["关键词自动回复怎么添加", "私信选择群", "客服知识"])
async def test_private_events_have_no_knowledge_or_model_entry(
    tmp_path, monkeypatch, no_http, entry, text
):
    store = seed_history(tmp_path)
    before = history_rows(store)
    value = service(tmp_path)
    vars(value.plugin).pop("private_interaction", None)
    event = SimpleNamespace(
        message_str=text,
        is_private_chat=lambda: True,
        send=AsyncMock(),
        set_extra=Mock(),
        stop_event=Mock(),
    )
    blocked = Mock(side_effect=AssertionError("private event must stop before IO"))
    fields = set(vars(value))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(value.store, "connect", blocked)
            patch.setattr(value.knowledge.store, "retrieve", blocked)
            patch.setattr(value.knowledge.store, "change", blocked)
            patch.setattr(value.context, "get_all_providers", blocked)
            patch.setattr(value.context, "get_provider_by_id", blocked)
            await getattr(value, entry)(event)
        blocked.assert_not_called()
        event.send.assert_not_awaited()
        event.set_extra.assert_called_once_with("_component_claimed", True)
        event.stop_event.assert_called_once_with()
        value.plugin.commands.run.assert_not_awaited()
        assert set(vars(value)) == fields
        for name in ("chat", "_chat", "support", "owner_support"):
            assert not hasattr(value, name)
        assert not hasattr(value.plugin, "private_interaction")
        assert history_rows(store) == before
    finally:
        await value.close()
