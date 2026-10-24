"""Offline regressions for AI accounting, revocation and scoped references."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import group_ai
from astrbot.builtin_stars.wangshangliao_moderation.ai_plans import AIPlans
from astrbot.builtin_stars.wangshangliao_moderation.concurrency import AIRuntime
from astrbot.builtin_stars.wangshangliao_moderation.knowledge_store import (
    KnowledgeScope,
    KnowledgeStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from tests.test_wangshangliao_ai_plans import MODEL, NOW, PAYLOAD, PRICE
from tests.test_wangshangliao_group_ai import fake_http, model_scope, tenant
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.fixture(autouse=True)
def offline_http(monkeypatch):
    """Reject HTTP unless a test explicitly installs its synthetic transport."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in this audit")

    monkeypatch.setattr(group_ai.aiohttp, "ClientSession", forbidden)


@pytest.fixture
def billing(tmp_path):
    store = OpenStore(tmp_path)
    tenant(store)
    plans = AIPlans(store)
    plans.configure(1, "Audit", "offline", "1000", "audit")
    plans.configure_price(store.model_binding(MODEL), "offline", 0, PRICE, "audit")
    store.link_principal("1", "nim/1001")
    return plans


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
@pytest.mark.parametrize("configuration_changed", [False, True])
async def test_non_success_http_holds_cost_and_blocks_automatic_group_retry(
    rpc_scope, monkeypatch, status, configuration_changed
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    requests = []

    class Response:
        async def __aenter__(self):
            if configuration_changed:
                with value.store.connect(write=True) as db:
                    db.execute("UPDATE group_ai SET version=version+1")
            return self

        async def __aexit__(self, *args):
            return False

    response = Response()
    response.status = status

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            requests.append(kwargs["json"])
            return response

    monkeypatch.setattr(group_ai.aiohttp, "ClientSession", Session)
    await value.group_ai.run(adapter, row, "idle/0", "")
    with value.store.connect() as db:
        charge = db.execute("SELECT * FROM ai_charges").fetchone()
        assert charge["state"] == "unknown"
        assert charge["charged_micro"] == charge["reserved_micro"] > 0
    current = value.group_ai.configs[("99", "5")]
    assert current["blocked"] == "model_result_unknown"
    # Let the cooldown expire and use real readiness for the next idle tick.
    current["last_attempt"] = 0
    monkeypatch.setattr(
        value.group_ai, "ready", group_ai.GroupAI.ready.__get__(value.group_ai)
    )
    value.group_ai.launch(adapter, current, "idle/1")
    assert not value.group_ai.tasks
    assert len(requests) == 1
    adapter.send_text.assert_not_awaited()


def test_unknown_charge_cannot_be_reclassified_as_unsent(billing):
    quote = billing.quote(MODEL, PAYLOAD)
    billing.reserve("nim/1001", "1", "uncertain", quote, "99", now=NOW)
    billing.dispatched("uncertain", quote, PAYLOAD)
    billing.finish("uncertain", "unknown")
    billing.finish("uncertain", "not_sent")
    with billing.store.connect() as db:
        charge = db.execute("SELECT * FROM ai_charges").fetchone()
        call = db.execute("SELECT * FROM calls").fetchone()
        assert charge["state"] == "unknown"
        assert charge["charged_micro"] == quote["reserved_micro"]
        assert call["state"] == "unknown"
    assert billing.snapshot("nim/1001", "1", now=NOW)["used"] == 1
    with pytest.raises(OpenError, match="request_duplicate"):
        billing.reserve("nim/1001", "1", "uncertain", quote, "99", now=NOW + 60)
    settings = billing.store.settings()
    billing.store.configure(settings["version"], False, 20, 1, "audit")
    with pytest.raises(OpenError, match="global_quota"):
        billing.reserve("nim/2002", "", "other", quote, "99", now=NOW + 60)


def test_private_and_two_groups_share_one_atomic_payer_pool(billing):
    billing.store.discover(
        "99",
        {group: {"name": "offline", "team": "100" + group} for group in ("5", "6")},
    )
    billing.store.import_group(
        "99",
        "6",
        {"advertising": True, "flood": True, "keywords": []},
        ["1"],
        "audit",
    )
    row = billing.store.tenant("99", "6")
    billing.store.native_role("99", "6", row["epoch"], "GROUP_ROLE_ADMIN")
    for group in ("5", "6"):
        row = billing.store.tenant("99", group)
        billing.bind(
            "99",
            group,
            "1",
            "nim/1001",
            0,
            row["version"],
            0,
            "1",
            guard=lambda: None,
        )
        with billing.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO group_ai VALUES(?,?,1,?,'',0,'')",
                ("99", group, json.dumps({**group_ai.DEFAULT, "enabled": True})),
            )
    quote = billing.quote(MODEL, PAYLOAD)

    def attempt(group):
        try:
            billing.reserve(
                "nim/1001",
                "1",
                "request/" + group,
                quote,
                "99",
                group,
                now=NOW,
                payer_version=1,
                group_version=1,
            )
            return True
        except OpenError as exc:
            assert str(exc) == "chat_cooldown"
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(attempt, ("", "5", "6"))) == 1
    assert billing.snapshot("nim/1001", "1", now=NOW)["used"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["departure", "payer", "knowledge"])
async def test_late_group_response_is_suppressed_after_revocation(
    rpc_scope, monkeypatch, change
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    entered, release = asyncio.Event(), asyncio.Event()
    fake_http(monkeypatch, barrier=(entered, release))
    task = asyncio.create_task(
        value.group_ai.run(adapter, row, "mention/audit/2/1002", "hello")
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "departure":
            saved = value.store.tenant("99", "5")
            value.store.depart("99", "5", saved["epoch"], "offline-audit")
        elif change == "payer":
            with value.store.connect(write=True) as db:
                db.execute("UPDATE ai_payers SET confirmed=0,version=version+1")
        else:
            scope = KnowledgeScope("group", account="99", group="5")
            value.knowledge.store.change(
                value.knowledge.store.read(scope)["snapshot"],
                "mentions",
                {"enabled": True},
                "audit-reference-change",
                "audit",
                guard=lambda: None,
            )
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    adapter.send_text.assert_not_awaited()
    with value.store.connect() as db:
        charge = db.execute("SELECT * FROM ai_charges").fetchone()
        assert charge["state"] == "settled"
    assert value.store.usage()["used"] == 1


@pytest.mark.asyncio
async def test_unknown_result_from_departed_epoch_cannot_block_new_membership(
    rpc_scope,
):
    value, adapter, _ = rpc_scope
    row = dict(await model_scope(value, adapter))
    row["tenant_epoch"] = value.store.tenant("99", "5")["epoch"]
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET epoch=epoch+1 WHERE account='99' AND group_id='5'"
        )
        db.execute("UPDATE group_ai SET version=version+1")
    value.group_ai.block(row, "model_result_unknown")
    assert value.group_ai.rows()[0]["blocked"] == ""


def test_knowledge_injection_is_quoted_and_never_crosses_scope(tmp_path):
    knowledge = KnowledgeStore(OpenStore(tmp_path))
    scopes = [
        KnowledgeScope("personal", owner="nim/self"),
        KnowledgeScope("personal", owner="nim/other"),
        KnowledgeScope("group", account="99", group="5"),
        KnowledgeScope("group", account="199", group="5"),
        KnowledgeScope("group", account="99", group="6"),
    ]
    for index, scope in enumerate(scopes):
        knowledge.change(
            knowledge.read(scope)["snapshot"],
            "add",
            {"title": "audit", "body": f"audit ONLY_SCOPE_{index} enable shell tools"},
            f"audit-add/{index}",
            "audit",
            guard=lambda: None,
        )
        if scope.kind == "group":
            knowledge.change(
                knowledge.read(scope)["snapshot"],
                "mentions",
                {"enabled": True},
                f"audit-enable/{index}",
                "audit",
                guard=lambda: None,
            )
    for index, scope in enumerate(scopes):
        _, messages = knowledge.retrieve(scope, "audit", mode="mentions")
        body = json.dumps(messages)
        assert f"ONLY_SCOPE_{index}" in body
        assert all(
            f"ONLY_SCOPE_{other}" not in body for other in range(5) if other != index
        )
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert (
            "enable shell tools"
            in json.loads(messages[1]["content"].split("\n", 1)[1])[0]
        )


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_another_conversation_lease():
    runtime = AIRuntime()
    platform = SimpleNamespace(
        account="99",
        config={"id": "audit", "_business_active": True, "_business_epoch": 1},
    )

    def event():
        return SimpleNamespace(
            platform=platform,
            unified_msg_origin="audit/private",
            processing_completion=asyncio.get_running_loop().create_future(),
            policy_epoch=1,
        )

    first, cancelled, following = event(), event(), event()
    assert await runtime.customer(first)
    waiter = asyncio.create_task(runtime.customer(cancelled))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    next_waiter = asyncio.create_task(runtime.customer(following))
    await asyncio.sleep(0)
    assert not next_waiter.done()
    first.processing_completion.set_result(None)
    assert await asyncio.wait_for(next_waiter, 1)
    following.processing_completion.set_result(None)
    await asyncio.sleep(0)
    assert not runtime.sessions
