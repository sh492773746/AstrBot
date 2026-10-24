"""Subscription accounting uses isolated SQLite and simulated model receipts only."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.ai_plans import AIPlans, money
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_group_ai import tenant

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")

PRICE = {
    "input_per_million": "0.15",
    "output_per_million": "0.6",
    "multiplier": "0.25",
    "credit_cny": "1",
    "input_ceiling": 100000,
    "output_ceiling": 4096,
    "all_output_billed_in_limit": True,
    "evidence": "offline bounded pricing fixture",
}
MODEL = {
    "model": "offline",
    "base_url": "https://offline.example/v1",
    "api_key": "fixture",
}
PAYLOAD = {
    "model": "offline",
    "max_completion_tokens": 512,
    "messages": [{"role": "user", "content": "hello"}],
}
NOW = datetime(2026, 10, 16, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def setup_billing(service):
    service.plans.configure(1, "服务客服", "测试账号", "1000", "test")
    model = (
        service.resolve_model({"provider_id": "existing/model"})
        if service.integrated
        else service.resolve_model()
    )
    service.plans.configure_price(
        service.store.model_binding(model), "existing/model", 0, PRICE, "test"
    )


@pytest.fixture
def plans(tmp_path):
    store = OpenStore(tmp_path)
    tenant(store)
    value = AIPlans(store)
    value.configure(1, "客服", "测试客服", "1000", "test")
    value.configure_price(store.model_binding(MODEL), "offline", 0, PRICE, "test")
    store.link_principal("1", "nim/1001")
    return value


def grant(plans, tier="plus", amount="134"):
    state = plans.snapshot("nim/1001", "1")
    payload = {
        "account": "99",
        "group_id": "5",
        "uid": "1",
        "principal": "nim/1001",
        "version": state["version"],
        "tier": tier,
        "received_cny": amount,
    }
    preview = plans.preview("dashboard/test", payload, guard=lambda: None)
    plans.grant("dashboard/test", payload, preview["confirmation"], guard=lambda: None)
    return preview


def charge(plans, key="r", *, now=None, uid="1", principal="nim/1001", **kwargs):
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve(principal, uid, key, quote, "99", now=now, **kwargs)
    plans.dispatched(key, quote, PAYLOAD)
    plans.settle(
        key, quote, {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}
    )
    plans.finish(key, "completed")
    return quote


def test_defaults_cost_math_and_persistent_trial(plans):
    assert money("0.000001") == 1
    assert AIPlans.cost(PRICE, 1_000_000, 1_000_000) == 187500
    state = plans.snapshot("nim/1001", "1", now=NOW)
    assert state["daily"] == 20 and state["budget_micro"] == 3_000_000
    charge(plans, now=NOW)
    reopened = AIPlans(OpenStore(plans.store.path.parent))
    assert reopened.snapshot("nim/1001", "1", now=NOW)["used"] == 1
    assert reopened.store.settings()["global_limit"] == 100000


def test_plus_pro_upgrade_renewal_and_idempotent_confirmation(plans):
    first = grant(plans)
    state = plans.snapshot("nim/1001", "1")
    assert state["tier"] == "plus" and state["daily"] == 2000
    assert state["budget_micro"] == 93_800_000
    second = grant(plans)
    assert second["starts"] == first["ends"] and not second["upgrade"]
    third = grant(plans, "pro", "335")
    assert third["upgrade"] and third["budget_micro"] == 234_500_000
    assert plans.snapshot("nim/1001", "1")["daily"] == 5000
    assert plans.snapshot("nim/1001", "1", now=third["ends"] + 1)["daily"] == 2000
    assert (
        plans.snapshot("nim/1001", "1", now=third["ends"] + 30 * 86400 + 1)["daily"]
        == 20
    )


def test_trial_daily_limit_crossday_and_month(plans):
    for index in range(20):
        charge(plans, str(index), now=NOW + index * 6)
    with pytest.raises(OpenError, match="personal_quota"):
        charge(plans, "full", now=NOW + 200)
    assert plans.snapshot("nim/1001", "1", now=NOW + 86400)["remaining"] == 20
    assert plans.snapshot("nim/1001", "1", now=NOW + 32 * 86400)["cost_micro"] == 0


def test_shared_group_pool_requires_self_consent_and_combines_private(plans):
    row = plans.store.tenant("99", "5")
    from astrbot.builtin_stars.wangshangliao_moderation.group_ai import DEFAULT

    with plans.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO group_ai VALUES('99','5',1,?,'',0,'')",
            (json.dumps({**DEFAULT, "enabled": True}),),
        )
    with pytest.raises(OpenError, match="billing_payer"):
        plans.group_pool("99", "5")
    state = plans.snapshot("nim/1001", "1", now=NOW)
    plans.bind(
        "99",
        "5",
        "1",
        "nim/1001",
        0,
        row["version"],
        state["version"],
        "1",
        guard=lambda: None,
    )
    charge(plans, "private", now=NOW)
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve(
        "nim/1001",
        "1",
        "group",
        quote,
        "99",
        "5",
        now=NOW + 61,
        payer_version=1,
        group_version=1,
        guard=lambda: None,
    )
    assert plans.snapshot("nim/1001", "1", now=NOW + 61)["used"] == 2
    with plans.store.connect(write=True) as db:
        db.execute("UPDATE tenants SET managers='[\"2\"]'")
    with pytest.raises(OpenError, match="permission"):
        plans.group_pool("99", "5")


def test_atomic_budget_and_inflight_reservation(plans):
    quote = plans.quote(MODEL, PAYLOAD)

    def attempt(index):
        try:
            plans.reserve("nim/1001", "1", str(index), quote, "99", now=NOW)
            return 1
        except OpenError:
            return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(20))) == 1
    with plans.store.connect() as db:
        request = db.execute("SELECT request FROM ai_charges").fetchone()[0]
    plans.finish(request, "not_sent")
    assert plans.snapshot("nim/1001", "1", now=NOW)["used"] == 0


def test_no_price_no_site_budget_no_http_admission(plans):
    with pytest.raises(OpenError, match="billing_price_missing"):
        plans.quote({**MODEL, "api_key": "changed"}, PAYLOAD)
    settings = plans.settings()
    plans.configure(settings["version"], "客服", "账号", "0", "test")
    with pytest.raises(OpenError, match="billing_price_missing"):
        charge(plans, now=NOW)
    assert plans.store.usage()["used"] == 0


def test_unknown_and_missing_usage_keep_hold_and_block_model(plans):
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "1", "unknown", quote, "99", now=NOW)
    plans.dispatched("unknown", quote, PAYLOAD)
    with pytest.raises(OpenError, match="billing_usage_unverified"):
        plans.settle("unknown", quote, None)
    plans.finish("unknown", "unknown")
    state = plans.snapshot("nim/1001", "1", now=NOW)
    assert state["cost_micro"] == quote["reserved_micro"] and state["used"] == 1
    with pytest.raises(OpenError, match="billing_price_missing"):
        plans.quote(MODEL, PAYLOAD)


def test_actual_overrun_is_recorded_not_capped_to_reservation(plans):
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "1", "over", quote, "99", now=NOW)
    plans.dispatched("over", quote, PAYLOAD)
    with pytest.raises(OpenError):
        plans.settle(
            "over", quote, {"prompt_tokens": 200000, "completion_tokens": 100000}
        )
    state = plans.snapshot("nim/1001", "1", now=NOW)
    assert state["cost_micro"] > quote["reserved_micro"]


def test_legacy_personal_usage_preserved_not_old_group_attributed(plans):
    plans.store.reserve("nim/1001", "old", now=NOW)
    plans.store.reserve("group/99/5", "old-group", now=NOW)
    assert plans.snapshot("nim/1001", "1", now=NOW)["used"] == 1


def test_notifications_persist_dedup_and_do_not_rollover(plans):
    for index in range(20):
        charge(plans, str(index), now=NOW + index * 6)
    notices = plans.queue_notices("nim/1001", "1", "99", now=NOW + 200)
    assert len(notices) == 1 and "私" not in notices[0]["kind"]
    assert plans.claim_notice(notices[0]["id"])
    plans.finish_notice(notices[0]["id"], "failed")
    reopened = AIPlans(OpenStore(plans.store.path.parent))
    assert not reopened.queue_notices("nim/1001", "1", "99", now=NOW + 201)


def test_plan_confirmation_changed_contact_or_pool_fails(plans):
    payload = {
        "account": "99",
        "group_id": "5",
        "uid": "1",
        "principal": "nim/1001",
        "version": 0,
        "tier": "plus",
        "received_cny": "134",
    }
    preview = plans.preview("test", payload, guard=lambda: None)
    plans.configure(2, "new", "contact", "1000", "test")
    with pytest.raises(OpenError, match="billing_changed"):
        plans.grant("test", payload, preview["confirmation"], guard=lambda: None)


@pytest.mark.parametrize("value", [True, "nan", "-1", "inf", "0.0000001", {}, "1e100"])
def test_money_rejects_non_decimal_or_unbounded(value):
    with pytest.raises(OpenError):
        money(value)


@pytest.mark.parametrize(
    "tier,amount,limit", [("plus", "134", 2000), ("pro", "335", 5000)]
)
def test_paid_limit_exemption_still_has_budget(plans, tier, amount, limit):
    grant(plans, tier, amount)
    state = plans.snapshot("nim/1001", "1")
    now = time.time()
    with plans.store.connect(write=True) as db:
        db.executemany(
            "INSERT INTO calls VALUES(?,?,?,0,?,'completed')",
            [(f"old-{i}", "nim/1001", state["day"], now - 10) for i in range(limit)],
        )
    with pytest.raises(OpenError, match="personal_quota"):
        charge(plans, now=now)
    charge(plans, "exempt", now=now, exempt=True)
    state = plans.snapshot("nim/1001", "1")
    with plans.store.connect(write=True) as db:
        db.execute("UPDATE ai_charges SET charged_micro=?", (state["budget_micro"],))
    with pytest.raises(OpenError, match="plan_budget"):
        charge(plans, "money-full", now=now + 6, exempt=True)


def test_expiry_renewal_does_not_reset_same_day_usage(plans):
    preview = grant(plans)
    when = preview["ends"] - 60
    charge(plans, now=when)
    state = plans.snapshot("nim/1001", "1", now=when + 61)
    assert state["tier"] == "trial" and state["used"] == 1
    assert state["remaining"] == 19


def test_price_change_payload_change_and_unsent_refund(plans):
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "1", "unsent", quote, "99", now=NOW)
    with pytest.raises(OpenError, match="payload_changed"):
        plans.dispatched("unsent", quote, {**PAYLOAD, "messages": []})
    plans.finish("unsent", "not_sent")
    assert plans.snapshot("nim/1001", "1", now=NOW)["cost_micro"] == 0
    plans.configure_price(plans.store.model_binding(MODEL), "offline", 1, PRICE, "test")
    with pytest.raises(OpenError, match="price_missing"):
        plans.reserve("nim/1001", "1", "stale", quote, "99", now=NOW)


def test_cross_account_sharing_and_identity_conflict_rejection(plans):
    charge(plans, "bot-a", now=NOW)
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "1", "bot-b", quote, "88", now=NOW + 6)
    assert plans.snapshot("nim/1001", "1", now=NOW + 6)["used"] == 2
    with pytest.raises(OpenError, match="identity_unverified"):
        plans.snapshot("nim/forged", "1", now=NOW + 6)


def test_site_budget_is_atomic_across_different_users(plans):
    reserve = plans.quote(MODEL, PAYLOAD)["reserved_micro"]
    plans.configure(2, "客服", "账号", f"{reserve / 1_000_000:.6f}", "test")
    quote = plans.quote(MODEL, PAYLOAD)

    def attempt(index):
        try:
            plans.reserve(f"nim/{index}", "", str(index), quote, "99", now=NOW)
            return True
        except OpenError as error:
            assert str(error) == "site_budget"
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(10))) == 1


def test_unknown_restart_and_retention_preserve_holds(plans):
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "1", "unconfirmed", quote, "99", now=NOW)
    reopened = AIPlans(OpenStore(plans.store.path.parent))
    reopened.prune(now=NOW + 91 * 86400)
    with reopened.store.connect() as db:
        row = db.execute(
            "SELECT * FROM ai_charges WHERE request='unconfirmed'"
        ).fetchone()
        assert (
            row["state"] == "unknown"
            and row["charged_micro"] == quote["reserved_micro"]
        )


def test_confirmation_expiry_actor_and_replay(plans):
    state = plans.snapshot("nim/1001", "1")
    payload = {
        "account": "99",
        "group_id": "5",
        "uid": "1",
        "principal": "nim/1001",
        "version": state["version"],
        "tier": "plus",
        "received_cny": "134",
    }
    preview = plans.preview("admin", payload, guard=lambda: None)
    with pytest.raises(OpenError, match="confirmation_invalid"):
        plans.grant("other-admin", payload, preview["confirmation"], guard=lambda: None)
    plans.grant("admin", payload, preview["confirmation"], guard=lambda: None)
    with pytest.raises(OpenError, match="confirmation_invalid"):
        plans.grant("admin", payload, preview["confirmation"], guard=lambda: None)
    assert len(plans.status()["subscriptions"]) == 1
    payload["version"] = plans.snapshot("nim/1001", "1")["version"]
    expired = plans.preview("admin", payload, guard=lambda: None)
    with plans.store.connect(write=True) as db:
        db.execute("UPDATE ai_plan_previews SET expires=?", (time.time() - 1,))
    with pytest.raises(OpenError, match="confirmation_invalid"):
        plans.grant("admin", payload, expired["confirmation"], guard=lambda: None)
    assert len(plans.status()["subscriptions"]) == 1


def test_cost_threshold_notices_are_once_per_period(plans):
    charge(plans, now=NOW)
    with plans.store.connect(write=True) as db:
        db.execute("UPDATE ai_charges SET charged_micro=2400000")
    notices = plans.queue_notices("nim/1001", "1", "99", now=NOW + 6)
    assert len(notices) == 1 and notices[0]["kind"].endswith("/80")
    assert plans.claim_notice(notices[0]["id"])
    plans.finish_notice(notices[0]["id"], "accepted")
    with plans.store.connect(write=True) as db:
        db.execute("UPDATE ai_charges SET charged_micro=3000000")
    notices = plans.queue_notices("nim/1001", "1", "99", now=NOW + 12)
    assert len(notices) == 1 and notices[0]["kind"].endswith("/100")


def test_upgrade_preserves_prepaid_future_renewals(plans):
    grant(plans)
    renewal = grant(plans)
    upgrade = grant(plans, "pro", "335")
    future = plans.snapshot("nim/1001", "1", now=upgrade["ends"] + 1)
    assert len(upgrade["deferred_periods"]) == 1
    assert future["tier"] == "plus"
    assert future["budget_micro"] == renewal["budget_micro"]
    assert future["expires"] == pytest.approx(upgrade["ends"] + 30 * 86400)


def test_dispatch_rechecks_price_inside_transaction(plans, monkeypatch):
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "1", "race", quote, "99", now=NOW)
    original = plans.assert_quote

    def change_after_precheck(*args, **kwargs):
        original(*args, **kwargs)
        plans.configure_price(
            plans.store.model_binding(MODEL), "offline", 1, PRICE, "test"
        )

    monkeypatch.setattr(plans, "assert_quote", change_after_precheck)
    with pytest.raises(OpenError, match="price_changed"):
        plans.dispatched("race", quote, PAYLOAD)
    plans.finish("race", "not_sent")
    assert plans.snapshot("nim/1001", "1", now=NOW)["used"] == 0


def test_usage_overflow_still_persists_anomaly_and_blocks_price(plans):
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "1", "enormous", quote, "99", now=NOW)
    plans.dispatched("enormous", quote, PAYLOAD)
    with pytest.raises(OpenError, match="usage_unverified"):
        plans.settle(
            "enormous", quote, {"prompt_tokens": 10**30, "completion_tokens": 1}
        )
    with pytest.raises(OpenError, match="price_missing"):
        plans.quote(MODEL, PAYLOAD)
    with plans.store.connect() as db:
        row = db.execute("SELECT * FROM ai_charges WHERE request='enormous'").fetchone()
        assert row["state"] == "unknown" and "reported_cost_micro" in row["usage"]
    assert plans.snapshot("nim/1001", "1", now=NOW)["cost_micro"] > 3_000_000
    assert plans.status()["unknown"]


def test_completed_model_call_cannot_be_refunded_as_unsent(plans):
    charge(plans, "completed", now=NOW)
    plans.finish("completed", "not_sent")
    assert plans.snapshot("nim/1001", "1", now=NOW)["used"] == 1
    with plans.store.connect() as db:
        assert db.execute("SELECT state FROM ai_charges").fetchone()[0] == "settled"
        assert db.execute("SELECT state FROM calls").fetchone()[0] != "not_sent"


@pytest.mark.asyncio
async def test_native_receive_menu_cannot_hijack_shared_pool_consent(setup, tmp_path):
    """Construct identities from the durable receiving pipeline, not manual fields."""
    import asyncio

    from astrbot.builtin_stars.wangshangliao_moderation.settings import BusinessSettings
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
        PolicySnapshot,
    )
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger

    value, _, template = setup
    ingress = BusinessSettings(value.context, root=tmp_path / "ingress")
    adapter = template.platform
    ledger = Ledger(tmp_path / "receiving.sqlite3")
    await ledger.open()
    adapter.ledger = ledger
    adapter._event_queue = asyncio.Queue()
    adapter.events = set()
    adapter.test_window = None
    adapter.enabled_groups = {"5", "6"}
    adapter.config["_business_active"] = True
    adapter.config["_business_revision"] = "offline-native-receive"
    adapter.record_error = Mock()
    adapter.diagnostics = Mock()
    adapter.members["5"]["3"] = "903"
    sequence = 0

    async def receive(text, sender="2", peer="902"):
        nonlocal sequence
        sequence += 1
        value.command_at = {k: v - 4 for k, v in value.command_at.items()}
        snapshot = PolicySnapshot(
            "1",
            adapter.config["_business_revision"],
            json.dumps(adapter.config),
            time.time(),
        )
        await ledger.ingest(
            "1",
            "5",
            str(sequence),
            {
                "sender": sender,
                "name": "fixture",
                "text": text,
                "created_at": time.time() * 1000,
                "mentions": [],
                "recall_route": {"peer": peer, "time": str(time.time())},
            },
            observe=lambda body, at: ingress.observe(
                snapshot, "1", "5", str(sequence), body, at
            ),
        )
        worker = asyncio.create_task(
            WangshangliaoAdapter.process_group(adapter, "5", single=True)
        )
        event = None
        try:
            event = await asyncio.wait_for(adapter._event_queue.get(), 5)
            event.send = AsyncMock()
            assert event.actor_identity.peer == peer
            await value.on_event(event)
            return "\n".join(
                call.args[0].chain[0].text for call in event.send.await_args_list
            )
        finally:
            if event is not None and not event.processing_completion.done():
                event.processing_completion.set_result(None)
            if event is None:
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    try:
        assert "回复 1" in await receive("确认本群扣费")
        await receive("1", "3", "903")
        assert value.plans.payer("1", "5") is None
        assert "已确认" in await receive("1")
        payer = value.plans.group_pool("1", "5")
        assert payer["uid"] == "2" and payer["principal"] == "nim/902"
        await receive("确认本群扣费")
        value.plans.configure(1, "客服", "测试", "0", "test")
        assert "变化" in await receive("1")
        assert value.plans.payer("1", "5")["version"] == payer["version"]
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_group_quota_notices_use_private_channel_only(setup, monkeypatch):
    value, _, event = setup
    plans = value.plans
    value.store.link_principal("2", "nim/902")
    state = plans.snapshot("nim/902", "2")
    with value.store.connect(write=True) as db:
        db.executemany(
            "INSERT INTO calls VALUES(?,?,?,0,?,'completed')",
            [
                (f"used-{i}", "nim/902", state["day"], time.time() - 100)
                for i in range(20)
            ],
        )
    pm = AsyncMock(side_effect=RuntimeError("private_send_failed"))
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.open_service.send_onboarding_text",
        pm,
    )
    event.send = AsyncMock()
    await value.notify_quota(
        event.platform, "nim/902", "2", reason="personal_quota", event=event
    )
    assert pm.await_count == 1
    event.send.assert_not_awaited()
    await value.notify_quota(
        event.platform, "nim/902", "2", reason="personal_quota", event=event
    )
    assert pm.await_count == 1
