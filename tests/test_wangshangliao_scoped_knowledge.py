"""Private/group knowledge isolation, fixed menus and bounded model references."""

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from statistics import quantiles
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import knowledge_store
from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import (
    DEFAULT as PROFILE_DEFAULT,
)
from astrbot.builtin_stars.wangshangliao_moderation.knowledge_store import (
    KnowledgeScope,
    KnowledgeStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_plain_commands import native_event
from tests.test_wangshangliao_public_hardening import chat_event

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


def edit(store, scope, operation="add", **fields):
    """Apply an isolated test write using an actual generation and unique request.

    Args:
        store: Temporary knowledge store.
        scope: Exact test ownership.
        operation: Fixed mutation.
        fields: Validated mutation arguments.

    Returns:
        Durable result of the scoped mutation.
    """
    snapshot = store.read(scope)["snapshot"]
    return store.change(
        snapshot,
        operation,
        fields or {"title": "退款说明", "body": "退款请联系服务管理员。"},
        f"{scope.key}/{snapshot.version}/{operation}",
        "nim/test",
        guard=lambda: None,
    )


@pytest.fixture
def store(tmp_path):
    return KnowledgeStore(OpenStore(tmp_path))


@pytest.mark.parametrize(
    "other",
    [
        KnowledgeScope("personal", "nim/other"),
        KnowledgeScope("group", account="1", group="5"),
        KnowledgeScope("group", account="2", group="5"),
        KnowledgeScope("group", account="1", group="6"),
    ],
)
def test_every_query_is_exact_owner_scoped(store, other):
    own = KnowledgeScope("personal", "nim/self")
    edit(store, own, title="退款", body="退款凭据 SELF_ONLY")
    edit(store, other, title="退款", body="退款凭据 OTHER_SECRET")
    snapshot, messages = store.retrieve(own, "退款")
    encoded = json.dumps(messages, ensure_ascii=False)
    assert "SELF_ONLY" in encoded and "OTHER_SECRET" not in encoded
    assert snapshot.scope == own
    assert len(store.read(own)["entries"]) == 1


def test_group_reference_modes_are_separate_off_by_default(store):
    group = KnowledgeScope("group", account="1", group="5")
    edit(store, group)
    assert not store.retrieve(group, "退款", mode="mentions")[1]
    assert not store.retrieve(group, "退款", mode="idle")[1]
    edit(store, group, "mentions", enabled=True)
    assert store.retrieve(group, "退款", mode="mentions")[1]
    assert not store.retrieve(group, "退款", mode="idle")[1]
    edit(store, group, "idle", enabled=True)
    assert store.retrieve(group, "退款", mode="idle")[1]
    edit(store, group, "mentions", enabled=False)
    assert not store.retrieve(group, "退款", mode="mentions")[1]


def test_edit_and_delete_change_index_atomically_and_invalidate_old_generation(store):
    scope = KnowledgeScope("personal", "nim/self")
    edit(store, scope, title="苹果", body="苹果资料")
    before, _ = store.retrieve(scope, "苹果")
    edit(store, scope, "edit", number=1, title="香蕉", body="香蕉资料")
    assert not store.retrieve(scope, "苹果")[1]
    assert "香蕉" in str(store.retrieve(scope, "香蕉")[1])
    with pytest.raises(OpenError, match="knowledge_changed"):
        store.assert_current(before)
    edit(store, scope, "delete", number=1)
    assert not store.retrieve(scope, "香蕉")[1]
    with store.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 0
        assert tuple(
            db.execute("SELECT bytes,entries FROM knowledge_totals").fetchone()
        ) == (
            0,
            0,
        )


def test_version_conflicts_duplicates_and_failed_commit_never_change_other_rows(store):
    scope = KnowledgeScope("personal", "nim/self")
    snapshot = store.read(scope)["snapshot"]
    fields = {"title": "原资料", "body": "原资料内容"}
    expected = store.change(snapshot, "add", fields, "same", "self", guard=lambda: None)
    assert (
        store.change(snapshot, "add", fields, "same", "self", guard=lambda: None)
        == expected
    )
    with pytest.raises(OpenError, match="knowledge_changed"):
        store.change(snapshot, "add", fields, "new", "self", guard=lambda: None)
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OpenError("revoked")

    with pytest.raises(OpenError, match="revoked"):
        store.change(
            store.read(scope)["snapshot"],
            "add",
            fields,
            "rollback",
            "self",
            guard=guard,
        )
    assert len(store.read(scope)["entries"]) == 1
    with store.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 1
        assert db.execute("SELECT entries FROM knowledge_totals").fetchone()[0] == 1


@pytest.mark.parametrize("limit", ["entries", "bytes", "site"])
def test_capacities_fail_before_mutation(store, monkeypatch, limit):
    scope = KnowledgeScope("personal", "nim/self")
    edit(store, scope)
    monkeypatch.setattr(
        knowledge_store,
        {"entries": "MAX_ENTRIES", "bytes": "MAX_BYTES", "site": "SITE_MAX_BYTES"}[
            limit
        ],
        1,
    )
    with pytest.raises(OpenError, match="knowledge_capacity"):
        edit(store, scope, title="第二条", body="不能保存")


def test_departure_cleans_exact_group_but_not_private_other_group_or_other_bot(store):
    departed = KnowledgeScope("group", account="1", group="5")
    others = [
        KnowledgeScope("group", account="2", group="5"),
        KnowledgeScope("group", account="1", group="6"),
        KnowledgeScope("personal", "nim/self"),
    ]
    for scope in [departed, *others]:
        edit(store, scope)
    old = store.read(departed)["snapshot"]
    store.clear_group("1", "5", guard=lambda: None)
    assert not store.read(departed)["entries"]
    assert all(store.read(scope)["entries"] for scope in others)
    with pytest.raises(OpenError, match="knowledge_changed"):
        store.assert_current(old)


def test_retrieval_handles_query_operators_and_caps_reference_size(store):
    scope = KnowledgeScope("personal", "nim/self")
    for i in range(8):
        edit(store, scope, title=f"退款{i}", body="退款 " * 1000)
    _, messages = store.retrieve(scope, '退款" OR * : title NEAR(foo)')
    references = json.loads(messages[-1]["content"].split("\n", 1)[1])
    assert len(references) <= 5
    assert sum(map(len, references)) <= knowledge_store.MAX_CONTEXT_CHARS
    assert "不可信" in messages[0]["content"]


def test_multitenant_retrieval_uses_incremental_index_and_bounded_results(
    store, monkeypatch
):
    """Measure real scoped SQL against 20 full spaces, without a timing threshold."""
    for owner in range(20):
        scope = KnowledgeScope("personal", f"nim/{owner}")
        for number in range(100):
            edit(
                store,
                scope,
                title=f"退款说明{number}",
                body=f"退款联系服务管理员。OWNER_{owner}_ONLY",
            )
    queries = []
    original = store.store.connect

    @contextmanager
    def traced(**kwargs):
        with original(**kwargs) as db:
            db.set_trace_callback(queries.append)
            yield db

    monkeypatch.setattr(store.store, "connect", traced)
    elapsed = []
    for owner in range(20):
        scope = KnowledgeScope("personal", f"nim/{owner}")
        for _ in range(5):
            started = time.perf_counter()
            _, messages = store.retrieve(scope, "退款")
            elapsed.append((time.perf_counter() - started) * 1000)
            references = json.loads(messages[-1]["content"].split("\n", 1)[1])
            assert len(references) <= 5
            assert sum(map(len, references)) <= knowledge_store.MAX_CONTEXT_CHARS
            assert all(f"OWNER_{owner}_ONLY" in text for text in references)
    application_queries = [
        query
        for query in queries
        if not query.lstrip().startswith("--") and "'knowledge_fts_config'" not in query
    ]
    assert len(application_queries) == 300
    assert sum("MATCH" in query for query in application_queries) == 100
    assert not any(
        "CREATE" in query or "INSERT" in query or "UPDATE" in query
        for query in application_queries
    )
    print(
        json.dumps(
            {
                "spaces": 20,
                "entries": 2000,
                "queries": 100,
                "p95_ms": round(quantiles(elapsed, n=20)[18], 3),
                "max_ms": round(max(elapsed), 3),
            }
        )
    )


@pytest.mark.asyncio
async def test_private_menu_creates_edits_reads_and_deletes_without_uid_verification(
    setup,
):
    value, _, template = setup
    event = native_event("菜单", private=True)
    event.platform = template.platform
    event.actor_identity = ActorIdentity("1", "999", "real-peer")
    event.message_obj.sender.user_id = "999"
    value.verify_actor = AsyncMock(side_effect=OpenError("unverified"))
    await step(value, event, "菜单", native=True)
    await step(value, event, "8", native=True)
    assert "我的知识库" in await step(value, event, "5", native=True)
    await step(value, event, "2", native=True)
    assert "即将执行" in await step(
        value, event, "营业时间 | 每天九点开门。\n周末十点。", native=True
    )
    assert not value.knowledge.store.read(KnowledgeScope("personal", "nim/real-peer"))[
        "entries"
    ]
    assert "已保存" in await step(value, event, "1", native=True)
    assert "九点" in await step(value, event, "1", native=True)
    await step(value, event, "3", native=True)
    await step(value, event, "1 营业时间 | 每天十点开门。", native=True)
    assert "已保存" in await step(value, event, "1", native=True)
    await step(value, event, "4", native=True)
    await step(value, event, "1", native=True)
    assert "已删除" in await step(value, event, "1", native=True)
    assert not value.knowledge.store.read(KnowledgeScope("personal", "nim/real-peer"))[
        "entries"
    ]
    value.plugin.private_interaction.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_menu_is_exact_scope_and_other_sender_cannot_confirm(setup):
    value, _, event = setup
    reply = await step(
        value, event, "添加群知识 本群退款 | 本群联系结账员。", native=True
    )
    assert "服务端维护" in reply
    attacker = native_event("1")
    attacker.platform = event.platform
    attacker.message_obj.sender.user_id = "3"
    attacker.actor_identity = ActorIdentity("1", "3", "903", "3")
    await step(value, attacker, "1", native=True)
    assert not value.knowledge.store.read(
        KnowledgeScope("group", account="1", group="5")
    )["entries"]
    await step(value, event, "1", native=True)
    assert not value.knowledge.store.read(
        KnowledgeScope("group", account="1", group="5")
    )["entries"]
    assert not value.knowledge.store.read(
        KnowledgeScope("group", account="1", group="6")
    )["entries"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "pause", "role", "version", "cancel"])
async def test_group_confirmation_cannot_outlive_authority_or_cancel(setup, change):
    value, _, event = setup
    for text in ("菜单", "9", "7", "2", "资料 | 不应保存"):
        await step(value, event, text, native=True)
    if change == "cancel":
        await step(value, event, "取消", native=True)
    elif change == "version":
        edit(
            value.knowledge.store,
            KnowledgeScope("group", account="1", group="5"),
        )
    else:
        with value.store.connect(write=True) as db:
            db.execute(
                "UPDATE tenants SET "
                + {
                    "revoke": """managers='["3"]'""",
                    "pause": "paused=1",
                    "role": "native_role='GROUP_ROLE_MEMBER'",
                }[change]
                + " WHERE account='1' AND group_id='5'"
            )
    await step(value, event, "1", native=True)
    rows = value.knowledge.store.read(KnowledgeScope("group", account="1", group="5"))[
        "entries"
    ]
    assert all(r["body"] != "不应保存" for r in rows)


@pytest.mark.asyncio
async def test_direct_write_group_personal_read_and_forged_identity_are_rejected(setup):
    value, _, event = setup
    reply = await step(value, event, "添加群知识 标题 | 不应保存", native=True)
    assert "服务端维护" in reply
    private = native_event("菜单", private=True)
    private.platform = event.platform
    private.actor_identity = replace(private.actor_identity, account="other")
    assert await step(value, private, "我的知识", native=True) == ""
    assert "私聊" in await step(value, event, "我的知识", native=True)


@pytest.mark.asyncio
async def test_cancelled_worker_holds_bounded_lease_until_it_stops(setup, monkeypatch):
    value, _, template = setup
    event = native_event("菜单", private=True)
    event.platform = template.platform
    entered, release = threading.Event(), threading.Event()
    original = value.knowledge.store.read

    def delayed(*args, **kwargs):
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(value.knowledge.store, "read", delayed)
    task = asyncio.create_task(value.knowledge.capture(event, "添加我的知识"))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert value.knowledge.worker.locked()
    with pytest.raises(OpenError, match="knowledge_busy"):
        await value.knowledge.capture(event, "添加我的知识")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not value.knowledge.worker.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("idle", [False, True])
async def test_group_model_references_never_include_private_or_other_group_data(
    rpc_scope, monkeypatch, idle
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    scope = KnowledgeScope("group", account="99", group="5")
    edit(value.knowledge.store, scope, title="退款", body="退款 GROUP_VISIBLE")
    edit(value.knowledge.store, scope, "idle" if idle else "mentions", enabled=True)
    for other in (
        KnowledgeScope("personal", "nim/1001"),
        KnowledgeScope("group", account="99", group="6"),
    ):
        edit(value.knowledge.store, other, title="退款", body="退款 OTHER_SECRET")
    requests = fake_http(monkeypatch)
    value.group_ai.launch(
        adapter, row, "idle/0" if idle else "mention/kb/2/1002", "退款"
    )
    await asyncio.gather(*value.group_ai.tasks.values())
    body = requests[0][1]["json"]
    encoded = json.dumps(body, ensure_ascii=False)
    assert "GROUP_VISIBLE" in encoded and "OTHER_SECRET" not in encoded
    assert not {"tools", "tool_choice"} & body.keys()


@pytest.mark.asyncio
async def test_knowledge_change_during_model_suppresses_late_group_reply(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    scope = KnowledgeScope("group", account="99", group="5")
    edit(value.knowledge.store, scope)
    edit(value.knowledge.store, scope, "mentions", enabled=True)
    entered, release = asyncio.Event(), asyncio.Event()
    fake_http(monkeypatch, barrier=(entered, release))
    value.group_ai.launch(adapter, row, "mention/change/2/1002", "退款")
    tasks = list(value.group_ai.tasks.values())
    await asyncio.wait_for(entered.wait(), 3)
    edit(value.knowledge.store, scope, "mentions", enabled=False)
    release.set()
    await asyncio.gather(*tasks)
    adapter.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_model_receives_only_robot_business_knowledge_and_quotes_it(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    await model_scope(value, adapter)
    value.profiles.save("consultation", "99", "", 0, PROFILE_DEFAULT, "dashboard/test")
    own = KnowledgeScope("personal", "nim/1001")
    edit(value.knowledge.store, own, title="退款", body="退款 PRIVATE_VISIBLE")
    for scope in (
        KnowledgeScope("personal", "nim/1002"),
        KnowledgeScope("group", account="99", group="5"),
    ):
        edit(value.knowledge.store, scope, title="退款", body="退款 OTHER_SECRET")
    preview = value.support.preview(
        "99",
        0,
        {"knowledge": ["退款 ACCOUNT_BUSINESS"]},
        "server",
        "session",
        guard=lambda: None,
    )
    value.support.commit(
        "99", preview["token"], "server", "session", guard=lambda: None
    )
    event = chat_event(adapter)
    event.message_str = "退款"
    requests = fake_http(monkeypatch, content="请联系服务管理员。")
    await value.chat(event)
    payload = requests[0][1]["json"]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "ACCOUNT_BUSINESS" in encoded
    assert "PRIVATE_VISIBLE" not in encoded and "OTHER_SECRET" not in encoded
    references = [
        row
        for row in payload["messages"]
        if row["content"].startswith("以下是本机器人已确认的公开业务资料")
    ]
    assert len(references) == 1
    quote = value.plans.quote(value.resolve_model(), payload)
    without = {
        **payload,
        "messages": [row for row in payload["messages"] if row not in references],
    }
    without_quote = value.plans.quote(value.resolve_model(), without)
    assert quote["payload_digest"] != without_quote["payload_digest"]
    assert quote["reserved_micro"] == without_quote["reserved_micro"]
    value.plans.assert_quote(quote, payload)
    with pytest.raises(OpenError, match="billing_payload_changed"):
        value.plans.assert_quote(quote, without)
    assert "tools" not in payload
    assert "PRIVATE_VISIBLE" not in str(
        [row.content for row in value.context.conversation_manager.db.rows.values()]
    )


@pytest.mark.asyncio
async def test_closed_budget_gate_skips_knowledge_and_model_work(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    value.knowledge.store.retrieve = lambda *a, **k: pytest.fail("gate bypass")
    requests = fake_http(monkeypatch)
    await value.chat(chat_event(adapter))
    assert not requests


@pytest.mark.asyncio
async def test_writer_lock_wait_rechecks_durable_revocation_before_commit(
    setup, monkeypatch
):
    value, _, event = setup
    group, tenant, guard = await value.rule_replies.context(event)
    draft = value.rule_replies.preview(
        "1",
        group,
        0,
        {},
        "native",
        "session",
        tenant,
        owner=event.actor_identity.verified_uid,
        peer=event.actor_identity.peer,
        guard=guard,
    )
    entered = threading.Event()
    original = value.rule_replies.commit

    def delayed(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(value.rule_replies, "commit", delayed)
    with value.store.connect(write=True) as db:
        task = asyncio.create_task(
            asyncio.to_thread(
                value.rule_replies.commit,
                "1",
                group,
                draft["token"],
                "native",
                "session",
                guard=guard,
            )
        )
        assert await asyncio.to_thread(entered.wait, 3)
        db.execute(
            """UPDATE tenants SET managers='["3"]' WHERE account='1' AND group_id='5'"""
        )
    with pytest.raises(OpenError):
        await task
    assert value.rule_replies.read("1", "5")["version"] == 0
