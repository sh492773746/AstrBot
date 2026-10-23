"""Server-scoped private trials never enable group AI or bypass billing."""

import asyncio
import copy
import time

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.consultation_access import (
    consultation_access,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_admin_guidance import admin_profile
from tests.test_wangshangliao_ai_plans import MODEL, PAYLOAD, charge
from tests.test_wangshangliao_ai_plans import plans as plans
from tests.test_wangshangliao_conversation_profiles import configure
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_public_hardening import chat_event


async def prepare(scope, *, model=False):
    """Prepare an isolated rules-only manager with optional simulated billing."""
    value, adapter, _ = scope
    configure(value)
    admin_profile(value)
    if model:
        await model_scope(value, adapter)
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET rules_only=1 WHERE account='99' AND group_id='5'"
        )
        db.execute(
            "UPDATE tenants SET managers='[]' WHERE account='99' AND group_id='6'"
        )
        if model:
            db.execute("DELETE FROM group_ai")
            db.execute("DELETE FROM ai_payers")
    await value.refresh()
    return {
        "account": "99",
        "group_id": "5",
        "uid": "1",
        "tenant_version": value.store.tenant("99", "5")["version"],
        "version": 0,
        "uses": 20,
    }


async def invoke(value, payload, *, commit=False, actor="dashboard/test"):
    """Use the same authenticated dispatcher as the production dashboard."""
    return await OpenRPC(value).dispatch(
        OpenCall(
            action="guidance_trial" if commit else "guidance_trial_preview",
            actor=actor,
            payload=payload,
        )
    )


async def grant(scope, *, model=False):
    """Commit a twenty-call grant using an exact one-use preview."""
    payload = await prepare(scope, model=model)
    value = scope[0]
    preview = await invoke(value, payload)
    await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    return payload


@pytest.mark.asyncio
async def test_trial_preview_commit_preserves_rules_and_all_existing_permissions(
    rpc_scope,
):
    value, adapter, _ = rpc_scope
    payload = await prepare(rpc_scope)
    before = copy.deepcopy(value.store.tenants())
    preview = await invoke(value, payload)
    with value.store.connect() as db:
        assert not db.execute("SELECT * FROM guidance_trials").fetchall()
    result = await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    assert result == {"saved": True, "version": 1, "uses": 20}
    assert value.store.tenants() == before
    event = chat_event(adapter)
    await value.verify_actor(event)
    access = consultation_access(value, event)
    assert access["rules_only"] and access["kind"] == "admin_guidance"
    assert access["guidance_trial"]["quota"] == 20
    items = value.navigation.items_for(event)
    assert not any(item.label == "群聊与热群AI" for item in items)
    quick_ai = next(item for item in items if item.label == "AI与额度")
    assert {"/chat", "/exit", "我的套餐"} <= {item.command for item in quick_ai.children}
    items = value.navigation.items_for(event, full=True)
    ai = next(item for item in items if item.label == "私聊AI咨询（不执行操作）")
    assert {item.command for item in ai.children} == {"/chat", "/exit", "我的套餐"}
    assert "剩余20次" in await value.plan_command(event, "我的额度")
    with pytest.raises(OpenError, match="guidance_trial_changed"):
        await invoke(
            value, {**payload, "confirmation": preview["confirmation"]}, commit=True
        )
    with value.store.connect() as db:
        for table in (
            "calls",
            "ai_charges",
            "ai_subscriptions",
            "ai_payers",
            "group_ai",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    reopened = OpenStore(value.store.path.parent)
    with reopened.connect() as db:
        assert db.execute("SELECT quota,active FROM guidance_trials").fetchone()[:] == (
            20,
            1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["actor", "version", "role", "manager", "peer", "paused", "not_rules"]
)
async def test_trial_grant_cannot_bypass_authority(rpc_scope, change):
    value, _, rows = rpc_scope
    payload = await prepare(rpc_scope)
    actor = "dashboard/test"
    if change == "actor":
        actor = "chat/1"
    elif change == "version":
        payload["tenant_version"] += 1
    elif change == "role":
        rows[1]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif change == "peer":
        payload["uid"] = "2"
    else:
        with value.store.connect(write=True) as db:
            db.execute(
                {
                    "manager": "UPDATE tenants SET managers='[]' WHERE account='99'",
                    "paused": "UPDATE tenants SET paused=1 WHERE account='99'",
                    "not_rules": "UPDATE tenants SET rules_only=0 WHERE account='99'",
                }[change]
            )
    with pytest.raises(OpenError):
        await invoke(value, payload, actor=actor)
    with value.store.connect() as db:
        assert not db.execute("SELECT * FROM guidance_trials").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("uses", [True, 1, 21, 2000, "20"])
async def test_trial_accepts_only_bounded_literal_count(rpc_scope, uses):
    payload = await prepare(rpc_scope)
    with pytest.raises((OpenError, ValidationError)):
        await invoke(rpc_scope[0], {**payload, "uses": uses})


@pytest.mark.asyncio
async def test_changed_preview_and_revocation_do_not_reset_grant(rpc_scope):
    value, adapter, _ = rpc_scope
    payload = await grant(rpc_scope)
    revoke = {**payload, "version": 1, "uses": 0}
    preview = await invoke(value, revoke)
    with pytest.raises(OpenError, match="confirmation_invalid"):
        await invoke(
            value,
            {**revoke, "uses": 20, "confirmation": preview["confirmation"]},
            commit=True,
        )
    await invoke(
        value, {**revoke, "confirmation": preview["confirmation"]}, commit=True
    )
    event = chat_event(adapter)
    await value.verify_actor(event)
    assert not consultation_access(value, event).get("guidance_trial")
    with value.store.connect() as db:
        assert db.execute("SELECT version,active FROM guidance_trials").fetchone()[
            :
        ] == (2, 0)
        created = db.execute("SELECT created FROM guidance_trials").fetchone()[0]
    restore = {**payload, "version": 2}
    preview = await invoke(value, restore)
    await invoke(
        value, {**restore, "confirmation": preview["confirmation"]}, commit=True
    )
    with value.store.connect() as db:
        assert (
            db.execute("SELECT created FROM guidance_trials").fetchone()[0] == created
        )


def test_twenty_total_calls_survive_next_day_and_unsent_is_refunded(plans):
    created = time.time() - 1000
    with plans.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO guidance_trials VALUES('99','5','1','nim/1001',1,1,20,1,?)",
            (created,),
        )
        trial = dict(db.execute("SELECT * FROM guidance_trials").fetchone())
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve(
        "nim/1001", "1", "unsent", quote, "99", guidance_trial=trial, now=created + 1
    )
    plans.finish("unsent", "not_sent")
    for index in range(20):
        charge(plans, str(index), guidance_trial=trial, now=created + 10 + index * 6)
    plans.prune(now=created + 91 * 86400)
    with pytest.raises(OpenError, match="guidance_trial_exhausted"):
        charge(plans, "next-day", guidance_trial=trial, now=created + 86400)
    with plans.store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM ai_charges WHERE state<>'not_sent'"
            ).fetchone()[0]
            == 20
        )


@pytest.mark.asyncio
async def test_trial_routes_only_to_tool_free_guidance_and_counts_once(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    await grant(rpc_scope, model=True)
    requests = fake_http(monkeypatch, content="先私信选择本人授权群，再查看规则。")
    event = chat_event(adapter)
    await value.chat(event)
    assert len(requests) == 1
    body = requests[0][1]["json"]
    assert "累计20次模型请求" in body["messages"][0]["content"]
    assert not {"tools", "tool_choice", "functions"} & body.keys()
    assert "剩余19次" in await value.plan_command(event, "我的额度")
    with value.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ai_charges").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM group_ai").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_revoking_trial_suppresses_inflight_reply(rpc_scope, monkeypatch):
    value, adapter, _ = rpc_scope
    payload = await grant(rpc_scope, model=True)
    barrier = (asyncio.Event(), asyncio.Event())
    requests = fake_http(monkeypatch, content="stale answer", barrier=barrier)
    event = chat_event(adapter)
    task = asyncio.create_task(value.chat(event))
    await asyncio.wait_for(barrier[0].wait(), 5)
    revoke = {**payload, "version": 1, "uses": 0}
    preview = await invoke(value, revoke)
    await invoke(
        value, {**revoke, "confirmation": preview["confirmation"]}, commit=True
    )
    barrier[1].set()
    await task
    assert len(requests) == 1
    event.send.assert_not_awaited()
    assert not value.chat_count and not value.chat_users


@pytest.mark.asyncio
async def test_exhausted_trial_never_dispatches_another_model_request(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    await grant(rpc_scope, model=True)
    with value.store.connect() as db:
        trial = dict(db.execute("SELECT * FROM guidance_trials").fetchone())
    with value.store.connect(write=True) as db:
        for index in range(20):
            db.execute(
                "INSERT INTO ai_charges(request,pool,period,day,month,account,group_id,"
                "uid,principal,binding,price_version,reserved_micro,charged_micro,state,created) "
                "VALUES(?,?,'trial/offline','2000-01-01','2000-01','99','','1','nim/1001',"
                "'offline',1,0,0,'unknown',?)",
                (f"used-{index}", "uid/1", trial["created"] + index),
            )
    requests = fake_http(monkeypatch)
    event = chat_event(adapter)
    await value.chat(event)
    assert not requests
    assert "20次免费试用已用完" in event.send.call_args.args[0]


@pytest.mark.asyncio
async def test_changed_tenant_invalidates_pending_trial_confirmation(rpc_scope):
    value, _, _ = rpc_scope
    payload = await prepare(rpc_scope)
    preview = await invoke(value, payload)
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET version=version+1 WHERE account='99' AND group_id='5'"
        )
    with pytest.raises(OpenError, match="tenant_changed"):
        await invoke(
            value, {**payload, "confirmation": preview["confirmation"]}, commit=True
        )
    with value.store.connect() as db:
        assert not db.execute("SELECT * FROM guidance_trials").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["wrong_peer", "unverified", "other_account"])
async def test_trial_is_bound_to_trusted_identity(rpc_scope, identity):
    value, adapter, _ = rpc_scope
    await grant(rpc_scope)
    event = chat_event(adapter)
    await value.verify_actor(event)
    if identity == "wrong_peer":
        event.actor_identity = ActorIdentity("99", "1", "2001", "1")
    elif identity == "unverified":
        event.actor_identity = ActorIdentity("99", "1", "1001")
    else:
        adapter.account = "199"
        event.actor_identity = ActorIdentity("199", "1", "1001", "1")
    assert not consultation_access(value, event).get("guidance_trial")
