"""Offline operator orders, shared budgets and conservative crash recovery."""

import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest

from data.plugins.astrbot_plugin_lajiaoliao.commerce import (
    PERIOD_SECONDS,
    CommerceStore,
    amount_nano,
    cost_nano,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError

NOW = 1_900_000_000
RATE = {
    "input_cny_per_million": "1",
    "cached_input_cny_per_million": "0.1",
    "output_cny_per_million": "5",
    "verified": True,
    "evidence_ref": "synthetic-invoice",
    "input_count_verified": True,
    "output_bound_verified": True,
    "usage_verified": True,
}


def mutation(store, route, action, version=0, **values):
    return store.execute(
        "commerce-" + route,
        {
            "action": action,
            "request_id": secrets.token_hex(16),
            "version": version,
            "confirm": True,
            **values,
        },
        "operator",
        now=NOW,
    )


def prepared(tmp_path, plan="ai", rate=None):
    store = CommerceStore(tmp_path)
    store.bootstrap("operator", [])
    customer = mutation(store, "customers", "create", label="Synthetic customer")[
        "customer_id"
    ]
    mutation(
        store,
        "customers",
        "bind",
        1,
        customer_id=customer,
        instance_id="bot",
        session_ref="ref",
        groups=["g"],
    )
    mutation(
        store,
        "rates",
        "save",
        provider_id="native",
        model="synthetic",
        fingerprint="a" * 64,
        rate=RATE if rate is None else rate,
    )
    period = mutation(
        store,
        "subscriptions",
        "activate",
        2,
        customer_id=customer,
        plan=plan,
        paid_confirmed=True,
        receipt_ref="payment",
    )["period_id"]
    return store, customer, period


def reserve(store, logical="msg", sequence=1, **kwargs):
    return store.reserve(
        "bot",
        logical,
        sequence,
        "chat",
        "native",
        "synthetic",
        "a" * 64,
        kwargs.pop("input_bound", 1000),
        kwargs.pop("output_bound", 1000),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def test_protected_integer_amounts(tmp_path):
    store, _, _ = prepared(tmp_path)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert amount_nano("0.0000000001") == 1
    assert cost_nano(RATE, 1000, 1000, 1000) == 6_100_000
    for value in (True, 0.1, "-1", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            amount_nano(value)


def test_rate_rejects_cache_price_above_reserved_input_price(tmp_path):
    rate = {**RATE, "cached_input_cny_per_million": "3"}
    with pytest.raises(ValueError, match="invalid_commerce_rate"):
        prepared(tmp_path, rate=rate)


def test_call_point_valuation_snapshot_survives_future_constant_change(
    tmp_path, monkeypatch
):
    import data.plugins.astrbot_plugin_lajiaoliao.commerce as commerce

    store, _, _ = prepared(tmp_path)
    held = reserve(store)
    assert held["rate"]["nano_per_micropoint"] == 4
    assert held["rate"]["valuation_version"] == "cny-0.004-v1"
    monkeypatch.setattr(commerce, "NANO_PER_MICROPOINT", 9)
    store.dispatch(held["call_id"], now=NOW)
    result = store.settle(
        held["call_id"],
        {
            "ordinary": 100,
            "cached": 0,
            "candidates": 50,
            "thinking": 0,
        },
        now=NOW,
    )
    with store.connect() as db:
        charged = db.execute("SELECT charged_micro FROM commerce_calls").fetchone()[0]
    assert charged == (result["cost_nano"] + 3) // 4


def test_orders_idempotency_version_and_operator(tmp_path):
    store = CommerceStore(tmp_path)
    store.bootstrap("operator", [])
    body = {
        "action": "create",
        "label": "Customer",
        "request_id": "same-request",
        "version": 0,
        "confirm": True,
    }
    first = store.execute("commerce-customers", body, "operator", now=NOW)
    assert store.execute("commerce-customers", body, "operator", now=NOW) == first
    with pytest.raises(SessionError, match="idempotency"):
        store.execute("commerce-customers", {**body, "label": "Other"}, "operator")
    with pytest.raises(SessionError, match="operator"):
        store.execute("commerce-customers", body, "chat-admin")
    with pytest.raises(SessionError, match="changed"):
        mutation(store, "customers", "pause", 0, customer_id=first["customer_id"])
    with pytest.raises(ValueError, match="confirmation"):
        store.execute("commerce-customers", {**body, "confirm": False}, "operator")


def test_renewal_topup_expiry_and_no_carry(tmp_path):
    store, customer, period = prepared(tmp_path)
    mutation(
        store,
        "topups",
        "add",
        3,
        customer_id=customer,
        product="5000",
        paid_confirmed=True,
        receipt_ref="topup",
    )
    renewed = mutation(
        store,
        "subscriptions",
        "renew",
        4,
        customer_id=customer,
        plan="ai",
        paid_confirmed=True,
        receipt_ref="renew",
    )
    assert renewed["start"] == NOW + PERIOD_SECONDS
    assert renewed["end"] == NOW + 2 * PERIOD_SECONDS
    usage = store.usage(customer, now=NOW)
    old = next(p for p in usage["periods"] if p["id"] == period)
    new = next(p for p in usage["periods"] if p["id"] == renewed["period_id"])
    assert old["total_micro"] == 25_000 * 10**6
    assert new["total_micro"] == 20_000 * 10**6
    reserve(store, now=NOW + PERIOD_SECONDS)
    with pytest.raises(SessionError, match="inactive"):
        reserve(store, "expired", now=NOW + 2 * PERIOD_SECONDS)
    with pytest.raises(SessionError, match="receipt"):
        mutation(
            store,
            "topups",
            "add",
            5,
            customer_id=customer,
            product="10000",
            paid_confirmed=True,
            receipt_ref="topup",
        )


def test_robot_group_binding_limits(tmp_path):
    store, customer, _ = prepared(tmp_path)
    for instance, ref, reason in (
        ("bot", "other", "assigned"),
        ("second", "ref", "assigned"),
        ("second", "second-ref", "robot_limit"),
    ):
        with pytest.raises(SessionError, match=reason):
            mutation(
                store,
                "customers",
                "bind",
                3,
                customer_id=customer,
                instance_id=instance,
                session_ref=ref,
                groups=[],
            )
    with pytest.raises(SessionError, match="group_limit"):
        store.sync_groups("bot", "ref", [str(i) for i in range(11)])


def test_fixed_products_and_no_midperiod_change(tmp_path):
    store, customer, _ = prepared(tmp_path)
    with pytest.raises(ValueError, match="topup"):
        mutation(store, "topups", "add", 3, customer_id=customer, product="999")
    with pytest.raises(SessionError, match="midperiod"):
        mutation(
            store,
            "subscriptions",
            "renew",
            3,
            customer_id=customer,
            plan="standard",
            paid_confirmed=True,
            receipt_ref="other",
        )
    with pytest.raises(ValueError, match="payment"):
        mutation(
            store,
            "topups",
            "add",
            3,
            customer_id=customer,
            product="5000",
            receipt_ref="other",
        )


def test_standard_model_denied_and_new_bots_not_internal(tmp_path):
    store, _, _ = prepared(tmp_path, "standard")
    with pytest.raises(SessionError, match="ai_plan"):
        reserve(store)
    store.bootstrap(
        "operator",
        [
            {
                "instance_id": "new",
                "account_id": "9680209",
                "state": "bound",
                "session_ref": "new",
                "owner": "operator",
            }
        ],
    )
    assert "new" not in store.snapshot()["bindings"]


def test_shared_last_balance_race(tmp_path):
    store, _, period = prepared(tmp_path)
    with store.connect() as db:
        db.execute(
            "UPDATE commerce_periods SET total_nano=6000000,total_micro=1500000 WHERE id=?",
            (period,),
        )

    def attempt(index):
        try:
            return reserve(store, str(index))["call_id"]
        except SessionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(20)))
    assert sum(bool(r) for r in results) == 1
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM commerce_periods WHERE id=?", (period,)
        ).fetchone()
        assert row["held_nano"] == 6_000_000 and row["held_micro"] == 1_500_000
        assert db.execute("SELECT COUNT(*) FROM commerce_alerts").fetchone()[0] == 1


def test_cached_and_thinking_settlement_frozen_rates(tmp_path):
    store, customer, period = prepared(tmp_path)
    call = reserve(store)["call_id"]
    store.dispatch(call, now=NOW)
    mutation(
        store,
        "rates",
        "save",
        1,
        provider_id="native",
        model="synthetic",
        fingerprint="a" * 64,
        rate={**RATE, "output_cny_per_million": "10"},
    )
    result = store.settle(
        call,
        {"ordinary": 800, "cached": 200, "candidates": 100, "thinking": 200},
        now=NOW,
    )
    assert result == {"state": "settled", "cost_nano": 2_320_000}
    assert reserve(store, "next")["rate"]["output_cny_per_million"] == "5"
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM commerce_periods WHERE id=?", (period,)
        ).fetchone()
        assert row["used_nano"] == 2_320_000
    assert store.usage(customer, now=NOW)["calls"]


@pytest.mark.parametrize(
    "usage,state",
    [
        (None, "unknown"),
        ({}, "unknown"),
        ({"ordinary": True, "cached": 0, "candidates": 0, "thinking": 0}, "unknown"),
        ({"ordinary": 1001, "cached": 0, "candidates": 1, "thinking": 0}, "overrun"),
        ({"ordinary": 0, "cached": 0, "candidates": 1001, "thinking": 0}, "overrun"),
    ],
)
def test_unknown_holds_pauses_and_operator_reconciles(tmp_path, usage, state):
    store, customer, period = prepared(tmp_path)
    call = reserve(store)["call_id"]
    store.dispatch(call, now=NOW)
    assert store.settle(call, usage, now=NOW)["state"] == state
    with pytest.raises(SessionError, match="paused"):
        reserve(store, "next")
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT held_nano FROM commerce_periods WHERE id=?", (period,)
            ).fetchone()[0]
            == 6_000_000
        )
    mutation(
        store,
        "reconcile",
        "settle",
        3,
        customer_id=customer,
        call_id=call,
        evidence_ref="operator-bill",
        actual_cny="0.008",
    )
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM commerce_periods WHERE id=?", (period,)
        ).fetchone()
        assert row["held_nano"] == 0 and row["used_nano"] == 8_000_000


def test_crash_recovery_release_only_undispatched(tmp_path):
    store, _, _ = prepared(tmp_path)
    reserve(store, "unsent")
    call = reserve(store, "sent")["call_id"]
    store.dispatch(call, now=NOW)
    restored = CommerceStore(tmp_path)
    restored.recover()
    restored.recover()
    with restored.connect() as db:
        states = dict(db.execute("SELECT logical_id,state FROM commerce_calls"))
        assert states == {"unsent": "released", "sent": "unknown"}


@pytest.mark.parametrize(
    "bounds",
    [
        {"input_bound": 32769},
        {"output_bound": 4097},
        {"output_bound": 0},
        {"input_bound": True},
    ],
)
def test_request_caps(tmp_path, bounds):
    store, _, _ = prepared(tmp_path)
    with pytest.raises(SessionError, match="request_limit"):
        reserve(store, **bounds)


def test_six_call_limit_and_duplicate_request(tmp_path):
    store, _, _ = prepared(tmp_path)
    for index in range(1, 7):
        reserve(store, sequence=index)
    with pytest.raises(SessionError, match="duplicate"):
        reserve(store)
    with pytest.raises(SessionError, match="request_limit"):
        reserve(store, sequence=7)


def test_draft_rate_cannot_reserve_and_confirmation_evidence(tmp_path):
    store, _, _ = prepared(tmp_path)
    mutation(
        store,
        "rates",
        "save",
        1,
        provider_id="native",
        model="synthetic",
        fingerprint="a" * 64,
        rate={**RATE, "verified": False},
    )
    with pytest.raises(SessionError, match="unverified"):
        reserve(store)
    with pytest.raises(ValueError, match="evidence"):
        mutation(
            store,
            "rates",
            "save",
            2,
            provider_id="native",
            model="synthetic",
            fingerprint="a" * 64,
            rate={**RATE, "evidence_ref": ""},
        )
    assert RATE["input_cny_per_million"] == "1"


def test_internal_bootstrap_exact_identity_and_one_time(tmp_path):
    store = CommerceStore(tmp_path)
    rows = [
        {
            "instance_id": "lajiaoliao_test_20261007",
            "account_id": "9680209",
            "session_ref": "internal",
            "state": "bound",
            "owner": "operator",
        },
        {
            "instance_id": "lajiaoliao_0fa92beedaad4eec",
            "account_id": "wrong",
            "session_ref": "other",
            "state": "bound",
            "owner": "operator",
        },
    ]
    store.bootstrap("operator", rows)
    assert list(store.snapshot()["bindings"]) == ["lajiaoliao_test_20261007"]
    assert store.snapshot()["bindings"]["lajiaoliao_test_20261007"]["internal"] == 1
    rows[1]["account_id"] = "3129316"
    store.bootstrap("new-operator", rows)
    assert list(store.snapshot()["bindings"]) == ["lajiaoliao_test_20261007"]
    assert store.snapshot()["operators"] == ["operator"]
