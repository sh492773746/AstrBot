"""Paid display reuse never caches authority, pending qualification or writes."""

import pytest
from test_wangshangliao_cashback_qualification import OWNER, scan
from test_wangshangliao_cashback_qualification import scenario as qualification_scenario

from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
)

scenario = qualification_scenario


@pytest.fixture
def paid_reader(scenario):
    """Create a private paid fixture and an exclusively leased display reader."""
    store, _, _, clock = scenario
    row = scan(scenario)
    store.mutate(
        OWNER,
        "5",
        "exception",
        [{"id": row["id"], "version": row["version"]}],
        "paid-cache-fixture",
        "Controlled external payment fixture",
        actual_payment_confirmed=True,
    )
    reader = CashbackStore(store.path, read_only=True, reuse_reads=True)
    try:
        yield store, reader, clock
    finally:
        reader.close()


@pytest.mark.parametrize("pagination", ["entries", "inviters"])
def test_paid_cache_rechecks_authority_and_returns_independent_objects(
    paid_reader, pagination
):
    _, reader, _ = paid_reader
    first = reader.list_entries(OWNER, "5", "paid", pagination=pagination)
    with reader.connect() as db:
        statements = []
        db.set_trace_callback(statements.append)
    second = reader.list_entries(OWNER, "5", "paid", pagination=pagination)
    assert second == first and second is not first
    assert second["items"][0] is not first["items"][0]
    assert any("FROM users" in sql for sql in statements)
    assert any("FROM groups" in sql for sql in statements)
    assert not any("FROM entries" in sql for sql in statements)
    first["items"][0]["amount"] = 999999
    second["items"][0]["review_reason"] = "forged-client-value"
    if second["sections"]:
        second["sections"][0]["amount"] = 999999
    third = reader.list_entries(OWNER, "5", "paid", pagination=pagination)
    assert third["items"][0]["amount"] == 300
    assert third["items"][0]["review_reason"] != "forged-client-value"
    assert all(section["amount"] == 300 for section in third["sections"])


@pytest.mark.parametrize(
    "change,expected",
    [
        ("UPDATE entries SET amount=900", 900),
        ("UPDATE entries SET state='void'", None),
        (
            "UPDATE cashback_inviter_risk SET blocked_at=2000 "
            "WHERE group_id='5' AND account='99' AND inviter='1'",
            "risk",
        ),
        ("UPDATE evidence SET inviter='3'", "attribution_conflict"),
    ],
)
def test_paid_cache_invalidates_after_any_writer_commit(paid_reader, change, expected):
    store, reader, _ = paid_reader
    first = reader.list_entries(OWNER, "5", "paid")
    assert first["items"][0]["amount"] == 300
    with store.connect(True) as db:
        db.execute(change)
    fresh = reader.list_entries(OWNER, "5", "paid")
    if expected is None:
        assert fresh["total"] == 0 and fresh["items"] == []
    elif expected == "risk":
        assert fresh["items"][0]["risk_blocked"]
    elif isinstance(expected, str):
        assert fresh["items"][0]["review_reason"] == expected
    else:
        assert fresh["items"][0]["amount"] == expected
        assert fresh["amount"] == expected


def test_paid_cache_cannot_survive_revocation(paid_reader):
    store, reader, _ = paid_reader
    reader.list_entries(OWNER, "5", "paid")
    with store.connect(True) as db:
        db.execute("UPDATE users SET active=0")
    with pytest.raises(CashbackError, match="forbidden"):
        reader.list_entries(OWNER, "5", "paid")


def test_paid_cache_rechecks_commit_between_version_and_authority(
    paid_reader, monkeypatch
):
    store, reader, _ = paid_reader
    reader.list_entries(OWNER, "5", "paid")
    original = reader._scope

    def committing_scope(db, actor, group):
        with store.connect(True) as writer:
            writer.execute("UPDATE entries SET amount=900")
        return original(db, actor, group)

    monkeypatch.setattr(reader, "_scope", committing_scope)
    assert reader.list_entries(OWNER, "5", "paid")["amount"] == 900


def test_paid_cache_keeps_pinned_snapshot_then_observes_next_commit(
    paid_reader, monkeypatch
):
    store, reader, _ = paid_reader
    reader.list_entries(OWNER, "5", "paid")
    original = reader._scope

    def pinned_scope(db, actor, group):
        scope = original(db, actor, group)
        with store.connect(True) as writer:
            writer.execute("UPDATE entries SET amount=900")
        return scope

    monkeypatch.setattr(reader, "_scope", pinned_scope)
    assert reader.list_entries(OWNER, "5", "paid")["amount"] == 300
    monkeypatch.setattr(reader, "_scope", original)
    assert reader.list_entries(OWNER, "5", "paid")["amount"] == 900


@pytest.mark.parametrize("advance", [16, 31, -1])
def test_paid_cache_respects_future_proof_time_expiry_and_clock_reversal(
    paid_reader, advance
):
    store, reader, clock = paid_reader
    with store.connect(True) as db:
        db.execute("UPDATE evidence SET event_at=?", (clock[0] + 45,))
    first = reader.list_entries(OWNER, "5", "paid")
    assert first["items"][0]["review_reason"] == "invitation_unconfirmed"
    clock[0] += advance
    cached = reader.list_entries(OWNER, "5", "paid")
    fresh_reader = CashbackStore(store.path, read_only=True)
    try:
        assert cached == fresh_reader.list_entries(OWNER, "5", "paid")
    finally:
        fresh_reader.close()
    if advance > 0:
        assert cached["items"][0]["review_reason"] == ""


def test_paid_cache_does_not_cover_unpaid_qualification_or_mutations(paid_reader):
    store, reader, _ = paid_reader
    item = reader.list_entries(OWNER, "5", "paid")["items"][0]
    with reader.connect() as db:
        statements = []
        db.set_trace_callback(statements.append)
    reader.list_entries(OWNER, "5", "pending")
    assert any("FROM entries" in sql for sql in statements)
    with pytest.raises(CashbackError, match="read_only"):
        with reader.connect(True):
            pytest.fail("Display reuse must not enable writes")
    with store.connect(True) as db:
        db.execute("UPDATE users SET active=0")
    with pytest.raises(CashbackError, match="forbidden"):
        store.mutate(
            OWNER,
            "5",
            "correct",
            [{"id": item["id"], "version": item["version"]}],
            "cache-revocation-request-0001",
            "Fixture",
        )


@pytest.mark.parametrize(
    "query",
    [
        {"search": "missing"},
        {"pagination": "inviters"},
        {"pagination": "inviters", "inviter": "99999"},
        {"period": "custom", "start_at": 1, "end_at": 100},
        {"page": 2},
    ],
)
def test_paid_cache_binds_every_query_filter(paid_reader, query):
    store, reader, _ = paid_reader
    reader.list_entries(OWNER, "5", "paid")
    assert reader.list_entries(OWNER, "5", "paid", **query) == store.list_entries(
        OWNER, "5", "paid", **query
    )


def test_paid_cache_is_removed_when_reader_closes(paid_reader):
    _, reader, _ = paid_reader
    reader.list_entries(OWNER, "5", "paid")
    assert len(reader._paid_pages) == 1
    reader.close()
    assert reader._paid_pages == {}
