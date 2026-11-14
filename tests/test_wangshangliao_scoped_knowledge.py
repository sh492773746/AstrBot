"""Group knowledge isolation, retired entry guards and bounded model references."""

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from statistics import quantiles
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import knowledge_store
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
        KnowledgeScope("admin_manual"),
        KnowledgeScope("group", account="2", group="5"),
        KnowledgeScope("group", account="1", group="6"),
        KnowledgeScope("group", account="2", group="6"),
    ],
)
def test_every_query_is_exact_owner_scoped(store, other):
    own = KnowledgeScope("group", account="1", group="5")
    edit(store, own, title="退款", body="退款凭据 SELF_ONLY")
    edit(store, other, title="退款", body="退款凭据 OTHER_SECRET")
    edit(store, own, "mentions", enabled=True)
    snapshot, messages = store.retrieve(own, "退款", mode="mentions")
    encoded = json.dumps(messages, ensure_ascii=False)
    assert "SELF_ONLY" in encoded and "OTHER_SECRET" not in encoded
    assert snapshot.scope == own
    assert len(store.read(own)["entries"]) == 1


@pytest.mark.parametrize(
    "scope",
    [
        KnowledgeScope("personal", "nim/self"),
        KnowledgeScope("personal", account="1", group="5"),
        KnowledgeScope("support", account="1"),
        KnowledgeScope("support", account="1", group="5"),
        KnowledgeScope("group", owner="nim/self", account="1", group="5"),
        KnowledgeScope("group", account="1"),
        KnowledgeScope("group", group="5"),
        KnowledgeScope("admin_manual", owner="nim/self"),
        KnowledgeScope("admin_manual", account="1"),
        KnowledgeScope("admin_manual", group="5"),
    ],
)
def test_retired_or_forged_scopes_cannot_write_or_borrow_group_authority(
    store, monkeypatch, scope
):
    snapshot = replace(
        store.read(KnowledgeScope("group", account="1", group="5"))["snapshot"],
        scope=scope,
    )
    connect = Mock(side_effect=AssertionError("invalid scope must not start a write"))
    monkeypatch.setattr(store.store, "connect", connect)
    with pytest.raises(OpenError, match="^knowledge_scope_invalid$"):
        _ = scope.key
    with pytest.raises(OpenError, match="^knowledge_scope_invalid$"):
        store.change(
            snapshot,
            "add",
            {"title": "资料", "body": "不得保存"},
            "invalid",
            "test",
            guard=lambda: None,
        )
    connect.assert_not_called()


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
    scope = KnowledgeScope("group", account="1", group="5")
    edit(store, scope, title="苹果", body="苹果资料")
    edit(store, scope, "mentions", enabled=True)
    before, messages = store.retrieve(scope, "苹果", mode="mentions")
    assert "苹果资料" in str(messages)
    edit(store, scope, "edit", number=1, title="香蕉", body="香蕉资料")
    assert not store.retrieve(scope, "苹果", mode="mentions")[1]
    assert "香蕉" in str(store.retrieve(scope, "香蕉", mode="mentions")[1])
    with pytest.raises(OpenError, match="knowledge_changed"):
        store.assert_current(before)
    edit(store, scope, "delete", number=1)
    assert not store.retrieve(scope, "香蕉", mode="mentions")[1]
    with store.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 0
        assert tuple(
            db.execute("SELECT bytes,entries FROM knowledge_totals").fetchone()
        ) == (
            0,
            0,
        )


def test_version_conflicts_duplicates_and_failed_commit_never_change_other_rows(store):
    scope = KnowledgeScope("group", account="1", group="5")
    other = KnowledgeScope("group", account="2", group="5")
    edit(store, other, title="隔离资料", body="OTHER_ONLY")
    other_before = store.read(other)
    snapshot = store.read(scope)["snapshot"]
    fields = {"title": "原资料", "body": "原资料内容"}
    expected = store.change(snapshot, "add", fields, "same", "self", guard=lambda: None)
    assert (
        store.change(snapshot, "add", fields, "same", "self", guard=lambda: None)
        == expected
    )
    with pytest.raises(OpenError, match="knowledge_changed"):
        store.change(snapshot, "add", fields, "new", "self", guard=lambda: None)
    with store.store.connect() as db:
        before = {
            table: [
                tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in (
                "knowledge_entries",
                "knowledge_spaces",
                "knowledge_fts",
                "knowledge_totals",
                "knowledge_operations",
                "audit",
            )
        }
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
    assert store.read(other) == other_before
    with store.store.connect() as db:
        for table, rows in before.items():
            assert [
                tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")
            ] == rows
        assert db.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 2
        assert db.execute("SELECT entries FROM knowledge_totals").fetchone()[0] == 2


@pytest.mark.parametrize("limit", ["entries", "bytes", "site", "site_entries"])
def test_capacities_fail_before_mutation(store, monkeypatch, limit):
    scope = KnowledgeScope("group", account="1", group="5")
    edit(store, scope)
    before = store.read(scope)
    monkeypatch.setattr(
        knowledge_store,
        {
            "entries": "MAX_ENTRIES",
            "bytes": "MAX_BYTES",
            "site": "SITE_MAX_BYTES",
            "site_entries": "SITE_MAX_ENTRIES",
        }[limit],
        1,
    )
    with pytest.raises(OpenError, match="knowledge_capacity"):
        edit(store, scope, title="第二条", body="不能保存")
    assert store.read(scope) == before
    with store.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 1
        assert tuple(
            db.execute("SELECT bytes,entries FROM knowledge_totals").fetchone()
        ) == (before["bytes"], 1)


def test_departure_cleans_exact_group_but_not_manual_other_group_or_other_bot(store):
    departed = KnowledgeScope("group", account="1", group="5")
    others = [
        KnowledgeScope("group", account="2", group="5"),
        KnowledgeScope("group", account="1", group="6"),
        KnowledgeScope("admin_manual"),
    ]
    for scope in [departed, *others]:
        edit(store, scope)
    old = store.read(departed)["snapshot"]
    store.clear_group("1", "5", guard=lambda: None)
    assert not store.read(departed)["entries"]
    assert all(store.read(scope)["entries"] for scope in others)
    with store.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 3
        assert tuple(
            db.execute("SELECT bytes,entries FROM knowledge_totals").fetchone()
        ) == (sum(store.read(scope)["bytes"] for scope in others), 3)
    with pytest.raises(OpenError, match="knowledge_changed"):
        store.assert_current(old)


def test_retrieval_handles_query_operators_and_caps_reference_size(store):
    scope = KnowledgeScope("group", account="1", group="5")
    for i in range(8):
        edit(store, scope, title=f"退款{i}", body="退款 " * 1000)
    edit(store, scope, "mentions", enabled=True)
    _, messages = store.retrieve(scope, '退款" OR * : title NEAR(foo)', mode="mentions")
    references = json.loads(messages[-1]["content"].split("\n", 1)[1])
    assert len(references) <= 5
    assert sum(map(len, references)) <= knowledge_store.MAX_CONTEXT_CHARS
    assert "不可信" in messages[0]["content"]


def test_multitenant_retrieval_uses_incremental_index_and_bounded_results(
    store, monkeypatch
):
    """Measure real scoped SQL against 20 full spaces, without a timing threshold."""
    for owner in range(20):
        scope = KnowledgeScope("group", account="1", group=str(owner + 1))
        for number in range(100):
            edit(
                store,
                scope,
                title=f"退款说明{number}",
                body=f"退款联系服务管理员。OWNER_{owner}_ONLY",
            )
        edit(store, scope, "mentions", enabled=True)
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
        scope = KnowledgeScope("group", account="1", group=str(owner + 1))
        for _ in range(5):
            started = time.perf_counter()
            _, messages = store.retrieve(scope, "退款", mode="mentions")
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
async def test_group_reads_are_exact_scope_and_other_sender_cannot_read(setup):
    value, _, event = setup
    own = KnowledgeScope("group", account="1", group="5")
    other = KnowledgeScope("group", account="1", group="6")
    edit(value.knowledge.store, own, title="退款", body="退款 GROUP_VISIBLE")
    edit(value.knowledge.store, other, title="退款", body="退款 OTHER_SECRET")
    reply = await value.knowledge.command(event, "查看群知识 1")
    assert "GROUP_VISIBLE" in reply and "OTHER_SECRET" not in reply
    attacker = native_event("本群知识")
    attacker.platform = event.platform
    attacker.message_obj.sender.user_id = "3"
    attacker.actor_identity = ActorIdentity("1", "3", "903", "3")
    with pytest.raises(OpenError, match="knowledge_denied"):
        await value.knowledge.capture(attacker, "本群知识")
    assert len(value.knowledge.store.read(own)["entries"]) == 1
    assert len(value.knowledge.store.read(other)["entries"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "pause", "role", "version"])
async def test_group_write_guard_cannot_outlive_durable_authority(setup, change):
    value, _, event = setup
    scope, guard = await value.knowledge.context(event, "本群知识")
    before = value.knowledge.store.read(scope)
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET "
            + {
                "revoke": """managers='["3"]'""",
                "pause": "paused=1",
                "role": "native_role='GROUP_ROLE_MEMBER'",
                "version": "version=version+1",
            }[change]
            + " WHERE account='1' AND group_id='5'"
        )
    with pytest.raises(OpenError):
        value.knowledge.store.change(
            before["snapshot"],
            "add",
            {"title": "资料", "body": "不应保存"},
            "revoked-write",
            "native/2",
            guard=guard,
        )
    assert value.knowledge.store.read(scope) == before


@pytest.mark.asyncio
async def test_group_chat_cannot_write_server_owned_knowledge_or_forge_identity(setup):
    value, _, event = setup
    reply = await step(value, event, "添加群知识 标题 | 不应保存", native=True)
    assert "服务端维护" in reply
    assert not value.knowledge.store.read(
        KnowledgeScope("group", account="1", group="5")
    )["entries"]
    event.actor_identity = replace(event.actor_identity, account="other")
    with pytest.raises(OpenError, match="knowledge_denied"):
        await value.knowledge.capture(event, "本群知识")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["菜单", "我的知识", "添加我的知识 标题 | 不得保存"])
async def test_private_knowledge_words_have_no_native_entry(setup, monkeypatch, text):
    value, _, template = setup
    event = native_event(text, private=True)
    event.platform = template.platform
    event.send = AsyncMock()
    blocked = Mock(side_effect=AssertionError("retired private entry must not do IO"))
    monkeypatch.setattr(value.store, "connect", blocked)
    monkeypatch.setattr(value.knowledge.store, "read", blocked)
    monkeypatch.setattr(value.knowledge.store, "change", blocked)
    monkeypatch.setattr(value.knowledge.store, "retrieve", blocked)
    await value.on_event(event)
    blocked.assert_not_called()
    event.send.assert_not_awaited()
    assert event.get_extra("_component_claimed") is True
    assert not value.navigation.sessions


@pytest.mark.asyncio
async def test_cancelled_group_worker_holds_bounded_lease_until_it_stops(
    setup, monkeypatch
):
    value, _, event = setup
    entered, release = threading.Event(), threading.Event()
    original = value.knowledge.store.read

    def delayed(*args, **kwargs):
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(value.knowledge.store, "read", delayed)
    task = asyncio.create_task(value.knowledge.capture(event, "本群知识"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert value.knowledge.worker.locked()
        with pytest.raises(OpenError, match="knowledge_busy"):
            await value.knowledge.capture(event, "本群知识")
    finally:
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert not value.knowledge.worker.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("idle", [False, True])
async def test_group_model_references_never_include_manual_or_other_group_data(
    rpc_scope, monkeypatch, idle
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    scope = KnowledgeScope("group", account="99", group="5")
    edit(value.knowledge.store, scope, title="退款", body="退款 GROUP_VISIBLE")
    edit(value.knowledge.store, scope, "idle" if idle else "mentions", enabled=True)
    for other in (
        KnowledgeScope("admin_manual"),
        KnowledgeScope("group", account="99", group="6"),
        KnowledgeScope("group", account="199", group="5"),
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
