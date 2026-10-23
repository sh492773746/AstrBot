"""Expiring price evidence stops admission without changing settled charges."""

import json
from contextlib import contextmanager

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.ai_plans import AIPlans
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from tests.test_wangshangliao_ai_plans import MODEL, PAYLOAD, PRICE

NOW = 1_800_000_000


@pytest.fixture
def billing(tmp_path, monkeypatch):
    """Create a clock-controlled accounting store without a model connection.

    Args:
        tmp_path: Disposable SQLite root.
        monkeypatch: Test clock replacement.

    Returns:
        Accounting authority and mutable wall-clock value.
    """
    clock = [NOW]
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.ai_plans.time.time",
        lambda: clock[0],
    )
    plans = AIPlans(OpenStore(tmp_path))
    plans.configure(1, "Fixture", "Offline fixture", "10", "test")
    return plans, clock


def configure(plans, **extra):
    """Register one fixture price with optional expiry.

    Args:
        plans: Isolated accounting store.
        extra: Fields appended to the known fixture price.
    """
    plans.configure_price(
        plans.store.model_binding(MODEL),
        "offline",
        0,
        {**PRICE, **extra},
        "test",
    )


def test_legacy_price_remains_compatible(billing):
    plans, _ = billing
    configure(plans)
    assert plans.quote(MODEL, PAYLOAD)["reserved_micro"] > 0


@pytest.mark.parametrize("expiry", [NOW, NOW - 1, True, "1800000100", 4_102_444_801])
def test_invalid_or_already_expired_evidence_is_rejected(billing, expiry):
    plans, _ = billing
    with pytest.raises(OpenError, match="billing_price_invalid"):
        configure(plans, valid_until=expiry)
    with plans.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ai_prices").fetchone()[0] == 0


def test_unknown_price_fields_remain_rejected(billing):
    plans, _ = billing
    with pytest.raises(OpenError, match="billing_price_invalid"):
        configure(plans, valid_until=NOW + 60, arbitrary_url="https://invalid.test")


def test_expiry_stops_new_quotes_and_previously_prepared_dispatch(billing):
    plans, clock = billing
    configure(plans, valid_until=NOW + 60)
    quote = plans.quote(MODEL, PAYLOAD)
    plans.assert_quote(quote, PAYLOAD)
    clock[0] = NOW + 60
    with pytest.raises(OpenError, match="billing_price_expired"):
        plans.quote(MODEL, PAYLOAD)
    with pytest.raises(OpenError, match="billing_price_expired"):
        plans.assert_quote(quote, PAYLOAD)
    with plans.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ai_charges").fetchone()[0] == 0


def test_expiry_before_reservation_does_not_consume_quota(billing):
    plans, clock = billing
    configure(plans, valid_until=NOW + 60)
    quote = plans.quote(MODEL, PAYLOAD)
    clock[0] = NOW + 60
    with pytest.raises(OpenError, match="billing_price_expired"):
        plans.reserve("nim/1001", "", "never-reserved", quote, "99")
    with plans.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


def test_expiry_while_waiting_for_dispatch_transaction(billing, monkeypatch):
    plans, clock = billing
    configure(plans, valid_until=NOW + 60)
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "", "unsent", quote, "99")
    original_connect = plans.store.connect

    @contextmanager
    def delayed_connect(*args, **kwargs):
        """Advance the fixture clock only after dispatch's preliminary read.

        Args:
            args: Positional connection options.
            kwargs: Read/write transaction options.

        Yields:
            Original SQLite transaction with the advanced fixture clock.
        """
        with original_connect(*args, **kwargs) as db:
            if kwargs.get("write"):
                clock[0] = NOW + 60
            yield db

    monkeypatch.setattr(plans.store, "connect", delayed_connect)
    with pytest.raises(OpenError, match="billing_price_expired"):
        plans.dispatched("unsent", quote, PAYLOAD)
    plans.finish("unsent", "not_sent")
    with original_connect() as db:
        row = db.execute(
            "SELECT state,charged_micro FROM ai_charges WHERE request='unsent'"
        ).fetchone()
        assert tuple(row) == ("not_sent", 0)


def test_dispatched_receipt_uses_its_original_price_after_expiry(billing):
    plans, clock = billing
    configure(plans, valid_until=NOW + 60)
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "", "expiry-fixture", quote, "99")
    plans.dispatched("expiry-fixture", quote, PAYLOAD)
    clock[0] = NOW + 61
    plans.settle(
        "expiry-fixture",
        quote,
        {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
    )
    plans.finish("expiry-fixture", "completed")
    with plans.store.connect() as db:
        row = db.execute(
            "SELECT state,usage,charged_micro FROM ai_charges WHERE request=?",
            ("expiry-fixture",),
        ).fetchone()
        assert row["state"] == "settled"
        assert json.loads(row["usage"])["total_tokens"] == 130
        assert row["charged_micro"] == plans.cost(PRICE, 100, 30)
