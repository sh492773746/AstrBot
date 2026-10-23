"""Independent trial/paid cost pools, using isolated SQLite and mock receipts."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import ai_plans as billing
from astrbot.builtin_stars.wangshangliao_moderation.ai_plans import MICRO, AIPlans
from astrbot.builtin_stars.wangshangliao_moderation.group_ai import DEFAULT
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_ai_plans import (
    MODEL,
    NOW,
    PAYLOAD,
    charge,
    grant,
)
from tests.test_wangshangliao_ai_plans import (
    plans as plans,
)
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.fixture
def split(plans, monkeypatch):
    monkeypatch.setattr(billing, "time", SimpleNamespace(time=lambda: NOW))
    plans.configure(2, "客服", "测试客服", "20", "test", trial_monthly_cny="20")
    return plans


def test_free_site_defaults_and_compatible_versioned_settings(split):
    settings = split.settings()
    assert settings["trial_monthly_micro"] == 20 * MICRO
    split.configure(settings["version"], "客服", "账号", "30", "test")
    assert split.settings()["trial_monthly_micro"] == 20 * MICRO
    with pytest.raises(OpenError, match="billing_changed"):
        split.configure(
            settings["version"], "客服", "账号", "30", "test", trial_monthly_cny="1"
        )
    assert split.status()["site"]["limit_micro"] == 30 * MICRO


def test_exhausted_free_pool_does_not_block_paid_administrator(split):
    charge(split, "free", uid="", principal="nim/free", now=NOW)
    with split.store.connect(write=True) as db:
        db.execute("UPDATE ai_charges SET charged_micro=?", (20 * MICRO,))
    with pytest.raises(OpenError, match="trial_budget"):
        charge(split, "free-denied", uid="", principal="nim/another", now=NOW)
    grant(split)
    charge(split, "paid", now=NOW)
    site = split.status()["site"]
    assert site["trial_cost_micro"] == 20 * MICRO
    assert site["paid_cost_micro"] > 0
    assert site["limit_micro"] == (20 * MICRO + 93_800_000)
    with split.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ai_charges").fetchone()[0] == 2
        assert [
            r[0]
            for r in db.execute("SELECT budget_kind FROM ai_charges ORDER BY request")
        ] == ["trial", "paid"]


def test_paid_costs_above_twenty_do_not_consume_trial_pool(split):
    grant(split)
    charge(split, "paid", now=NOW)
    with split.store.connect(write=True) as db:
        db.execute("UPDATE ai_charges SET charged_micro=?", (30 * MICRO,))
    charge(split, "free", uid="", principal="nim/free", now=NOW)
    site = split.status()["site"]
    assert site["paid_cost_micro"] == 30 * MICRO
    assert 0 < site["trial_cost_micro"] < MICRO
    assert site["cost_micro"] == site["paid_cost_micro"] + site["trial_cost_micro"]


def test_group_and_private_use_same_paid_cycle_when_free_pool_is_disabled(split):
    grant(split)
    split.configure(3, "客服", "账号", "20", "test", trial_monthly_cny="0")
    tenant = split.store.tenant("99", "5")
    state = split.snapshot("nim/1001", "1")
    split.bind(
        "99",
        "5",
        "1",
        "nim/1001",
        0,
        tenant["version"],
        state["version"],
        "1",
        guard=lambda: None,
    )
    with split.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO group_ai VALUES('99','5',1,?,'',0,'')",
            (json.dumps({**DEFAULT, "enabled": True}),),
        )
    charge(split, "private", now=NOW)
    charge(
        split,
        "group",
        now=NOW + 61,
        group="5",
        payer_version=1,
        group_version=1,
        guard=lambda: None,
    )
    state = split.snapshot("nim/1001", "1", now=NOW + 61)
    assert state["used"] == 2
    site = split.status()["site"]
    assert site["paid_cost_micro"] == state["cost_micro"]
    assert site["trial_cost_micro"] == 0
    with split.store.connect() as db:
        assert [
            tuple(r)
            for r in db.execute(
                "SELECT DISTINCT pool,period,budget_kind FROM ai_charges"
            )
        ] == [("uid/1", state["period"], "paid")]


@pytest.mark.parametrize("exempt", [False, True])
def test_shared_trial_reservations_are_atomic_and_unsent_refunds(split, exempt):
    quote = split.quote(MODEL, PAYLOAD)
    amount = quote["reserved_micro"]
    split.configure(
        3,
        "客服",
        "账号",
        "20",
        "test",
        trial_monthly_cny=f"{amount // MICRO}.{amount % MICRO:06d}",
    )

    def reserve(index):
        try:
            split.reserve(
                f"nim/{index}", "", str(index), quote, "99", exempt=exempt, now=NOW
            )
            return str(index)
        except OpenError as exc:
            assert str(exc) == "trial_budget"
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = [key for key in pool.map(reserve, range(16)) if key is not None]
    assert len(accepted) == 1
    assert split.status()["site"]["trial_cost_micro"] == amount
    split.finish(accepted[0], "not_sent")
    split.reserve("nim/new", "", "refunded", quote, "99", now=NOW)
    assert split.status()["site"]["trial_cost_micro"] == amount


@pytest.mark.parametrize("changed", ["upgrade", "expiry"])
def test_charge_category_survives_plan_change_and_restart(split, monkeypatch, changed):
    quote = split.quote(MODEL, PAYLOAD)
    if changed == "expiry":
        cycle = grant(split)
    split.reserve("nim/1001", "1", "held", quote, "99", now=NOW)
    if changed == "upgrade":
        grant(split)
    else:
        monkeypatch.setattr(
            billing, "time", SimpleNamespace(time=lambda: cycle["ends"] + 1)
        )
    split.dispatched("held", quote, PAYLOAD)
    reopened = AIPlans(split.store)
    reopened.settle("held", quote, {"prompt_tokens": 100, "completion_tokens": 30})
    reopened.finish("held", "completed")
    with reopened.store.connect() as db:
        row = dict(db.execute("SELECT * FROM ai_charges").fetchone())
    expected = "trial" if changed == "upgrade" else "paid"
    assert row["budget_kind"] == expected
    with reopened.store.connect() as db:
        site = reopened._site_budget(db, NOW, reopened.settings())
    assert site[expected + "_cost_micro"] == row["charged_micro"]
    assert site["paid_cost_micro" if expected == "trial" else "trial_cost_micro"] == 0


def test_upgrade_retains_old_cycle_budget_and_unknown_hold(split, monkeypatch):
    grant(split)
    quote = split.quote(MODEL, PAYLOAD)
    split.reserve("nim/1001", "1", "unknown", quote, "99", now=NOW)
    split.finish("unknown", "unknown")
    monkeypatch.setattr(billing, "time", SimpleNamespace(time=lambda: NOW + 60))
    grant(split, "pro", "335")
    site = split.status()["site"]
    assert site["paid_budget_micro"] == 93_800_000 + 234_500_000
    assert site["paid_cost_micro"] == quote["reserved_micro"]
    assert site["trial_cost_micro"] == 0
    assert site["limit_micro"] == 20 * MICRO + site["paid_budget_micro"]


@pytest.mark.parametrize("month", [10, 12])
def test_beijing_month_reset_does_not_reset_paid_cycle_cost(split, monkeypatch, month):
    last = datetime(2026, month, 31, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(billing, "time", SimpleNamespace(time=last.timestamp))
    grant(split)
    charge(split, "paid", now=last.timestamp())
    charge(split, "free", uid="", principal="nim/free", now=last.timestamp())
    old = split.status()["site"]
    monkeypatch.setattr(
        billing, "time", SimpleNamespace(time=lambda: last.timestamp() + 2)
    )
    state = split.snapshot("nim/1001", "1")
    current = split.status()["site"]
    assert current["month"] != old["month"]
    assert current["cost_micro"] == current["trial_cost_micro"] == 0
    assert state["used"] == 0 and state["cost_micro"] == old["paid_cost_micro"]
    assert current["paid_budget_micro"] == 93_800_000
    charge(split, "next-free", uid="", principal="nim/free", now=last.timestamp() + 6)
    assert split.status()["site"]["trial_cost_micro"] > 0


def test_disabled_free_pool_allows_paid_but_zero_safety_base_blocks_all(split):
    grant(split)
    split.configure(3, "客服", "账号", "20", "test", trial_monthly_cny="0")
    with pytest.raises(OpenError, match="trial_budget"):
        charge(split, "free", uid="", principal="nim/free", now=NOW)
    charge(split, "paid", now=NOW)
    split.configure(4, "客服", "账号", "0", "test")
    assert split.status()["site"]["limit_micro"] == 0
    with pytest.raises(OpenError, match="billing_price_missing"):
        charge(split, "paused", now=NOW + 6)


@pytest.mark.parametrize("field", ["trial_monthly_cny", "monthly_cny"])
def test_budget_lowered_during_dispatch_is_rechecked(split, field):
    quote = split.quote(MODEL, PAYLOAD)
    split.reserve("nim/1001", "1", "held", quote, "99", now=NOW)
    split.configure(
        3,
        "客服",
        "账号",
        "0" if field == "monthly_cny" else "20",
        "test",
        trial_monthly_cny="0" if field == "trial_monthly_cny" else "20",
    )
    with pytest.raises(
        OpenError,
        match="trial_budget"
        if field == "trial_monthly_cny"
        else "billing_price_changed",
    ):
        split.dispatched("held", quote, PAYLOAD)
    split.finish("held", "not_sent")
    assert split.status()["site"]["cost_micro"] == 0
    assert split.snapshot("nim/1001", "1")["used"] == 0


def test_legacy_migration_only_uses_recorded_period_not_current_plan(split):
    charge(split, "free", now=NOW)
    grant(split)
    charge(split, "paid", now=NOW + 6)
    charge(split, "ambiguous", uid="", principal="nim/legacy", now=NOW)
    with split.store.connect(write=True) as db:
        db.execute(
            "UPDATE ai_charges SET period='missing-cycle' WHERE request='ambiguous'"
        )
        db.execute("DROP INDEX ai_charges_month_kind_cost")
        db.execute("ALTER TABLE ai_charges DROP COLUMN budget_kind")
        db.execute("ALTER TABLE ai_plan_settings DROP COLUMN trial_monthly_micro")
        original = [
            dict(r) for r in db.execute("SELECT * FROM ai_charges ORDER BY request")
        ]
        version = split.settings()["version"]
    reopened = AIPlans(split.store)
    assert reopened.settings()["version"] == version + 1
    assert reopened.settings()["trial_monthly_micro"] == 20 * MICRO
    with reopened.store.connect() as db:
        migrated = [
            dict(r) for r in db.execute("SELECT * FROM ai_charges ORDER BY request")
        ]
    assert [r.pop("budget_kind") for r in migrated] == ["legacy", "trial", "paid"]
    assert migrated == original
    site = reopened.status()["site"]
    assert site["legacy_cost_micro"] > 0
    assert site["cost_micro"] == sum(
        site[k] for k in ("trial_cost_micro", "paid_cost_micro", "legacy_cost_micro")
    )
    assert AIPlans(split.store).settings()["version"] == version + 1


def test_trial_budget_private_notices_persist_without_notifying_paid(split):
    charge(split, "free", now=NOW)
    with split.store.connect(write=True) as db:
        db.execute("UPDATE ai_charges SET charged_micro=?", (20 * MICRO,))
    notices = []
    while pending := split.queue_notices(
        "nim/1001", "1", "99", reason="trial_budget", now=NOW
    ):
        notices.extend(pending)
        for notice in pending:
            assert notice["principal"] == "nim/1001" and notice["uid"] == "1"
            assert split.claim_notice(notice["id"])
            split.finish_notice(notice["id"], "failed")
    assert any("付费套餐不占此预算" in n["text"] for n in notices)
    assert any(n["kind"] == "trial-site/2026-10/100" for n in notices)
    reopened = AIPlans(split.store)
    assert not reopened.queue_notices(
        "nim/1001", "1", "99", reason="trial_budget", now=NOW
    )
    grant(reopened)
    assert not any(
        n["kind"].startswith("trial-site/")
        for n in reopened.queue_notices("nim/1001", "1", "99", now=NOW)
    )
    assert "免费试用全站每月共20元" in reopened.describe()


@pytest.mark.asyncio
async def test_budget_rpc_accepts_separate_caps_and_preserves_older_payload(rpc_scope):
    service, _, _ = rpc_scope
    rpc = OpenRPC(service)
    payload = {
        "version": 1,
        "contact_name": "客服",
        "contact": "测试账号",
        "monthly_cny": "25",
        "trial_monthly_cny": "20",
    }
    await rpc.dispatch(
        OpenCall(action="billing_settings", actor="dashboard/test", payload=payload)
    )
    assert service.plans.settings()["monthly_micro"] == 25 * MICRO
    payload.pop("trial_monthly_cny")
    payload.update(version=2, monthly_cny="30")
    await rpc.dispatch(
        OpenCall(action="billing_settings", actor="dashboard/test", payload=payload)
    )
    assert service.plans.settings()["trial_monthly_micro"] == 20 * MICRO
    with pytest.raises(OpenError, match="billing_settings_invalid"):
        await rpc.dispatch(
            OpenCall(
                action="billing_settings",
                actor="dashboard/test",
                payload={**payload, "arbitrary_action": "execute"},
            )
        )
    with service.store.connect() as db:
        assert not db.execute("SELECT * FROM ai_charges").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
