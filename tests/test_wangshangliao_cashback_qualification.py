"""Invitee-avatar pricing and durable newcomer qualification boundaries."""

import json
import sqlite3

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
    avatar_category,
)
from astrbot.builtin_stars.wangshangliao_moderation.cashback_models import GroupRule
from scripts.check_wsl_invitation_history import check_history

OWNER = "owner@example.com"


@pytest.mark.parametrize(
    "member,expected,digits,status",
    [
        ({"avatar_digit_count": 1}, "agent", 1, "detected"),
        ({"avatar_digit_count": 0}, "ordinary", 0, "detected"),
        ({"avatar_digit_count": 6}, "agent", 6, "detected"),
        ({"userAvatar": "123456789012345678"}, "ordinary", 0, "unavailable"),
        ({}, "ordinary", 0, "unavailable"),
    ],
)
def test_invitee_avatar_digit_classification(member, expected, digits, status):
    assert avatar_category(member) == (expected, digits, status)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    clock = [2000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    store = CashbackStore(tmp_path / "cash.sqlite3")
    store.bootstrap([{"email": OWNER, "name": "Owner"}])
    members = {"1": {"name": "邀请人123456", "peer": "11", "eligible": True}}
    store.configure(
        OWNER,
        {
            "id": "5",
            "name": "Group",
            "bot_id": "bot",
            "account": "99",
            "contact": "Contact",
            "enabled": True,
            "rate": 300,
            "agent_rate": 100,
            "qualification_seconds": 10800,
            "version": 0,
        },
        members,
        set(),
        now=1000,
    )
    members["2"] = {
        "name": "普通新成员",
        "peer": "22",
        "eligible": True,
        "avatar_digit_count": 1,
        "avatar_ref": "opaque-avatar-ref",
    }
    evidence = [
        {
            "member": "2",
            "inviter": "1",
            "source": "business_apply_log",
            "event_id": "join",
            "event_at": 2000,
        }
    ]
    return store, members, evidence, clock


def entry(store):
    with store.connect() as db:
        return dict(db.execute("SELECT * FROM entries WHERE member='2'").fetchone())


def scan(scenario, at=None):
    store, members, evidence, clock = scenario
    if at is not None:
        clock[0] = at
    store.snapshot("5", "bot", "99", members, evidence, now=clock[0])
    return entry(store)


def mature(scenario):
    scan(scenario)
    for at in range(2060, 12800, 60):
        scan(scenario, at)
    assert scan(scenario, 12799)["state"] == "verifying"
    return scan(scenario, 12800)


def test_prices_invitee_avatar_not_inviter_name_and_freezes_evidence(scenario):
    store, members, _, _ = scenario
    row = scan(scenario)
    assert row["state"] == "verifying"
    assert row["amount"] == 300 and row["member_class"] == "ordinary"
    assert row["member_avatar_digit_count"] == 0
    assert row["member_avatar_status"] == "manual"
    assert row["hold_until"] == 12800
    members["1"]["name"] = "改名没有数字"
    members["2"]["name"] = "新成员123456789"
    members["2"]["avatar_digit_count"] = 0
    updated = scan(scenario, 2060)
    assert updated["amount"] == 300 and updated["member_class"] == "ordinary"
    assert updated["inviter_name_snapshot"] == "邀请人123456"
    assert updated["hold_until"] == row["hold_until"]
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='invitation_verified'"
            ).fetchone()[0]
            == 1
        )


def test_inviter_name_does_not_control_invitee_avatar_price(scenario):
    _, members, _, _ = scenario
    members["1"].update(name="昵称123456")
    members["2"]["name"] = "被邀请人123456"
    members["2"]["avatar_digit_count"] = 0
    row = scan(scenario)
    assert row["member_class"] == "ordinary"
    assert row["amount"] == 300 and row["member_avatar_digit_count"] == 0


def test_repair_prices_actual_recruiter_and_keeps_qualification_deadline(scenario):
    store, members, evidence, clock = scenario
    members["1"]["name"] = "Approving owner"
    old = scan(scenario)
    with store.connect(True) as db:
        db.execute("UPDATE evidence SET source='nim_notification'")
    members["3"] = {"name": "Recruiter123456", "peer": "33", "eligible": True}
    clock[0] += 60
    store.snapshot(
        "5",
        "bot",
        "99",
        members,
        [{**evidence[0], "inviter": "3", "event_id": "application"}],
        now=clock[0],
    )
    repaired = entry(store)
    assert repaired["inviter"] == "3"
    assert repaired["amount"] == 300
    assert repaired["member_class"] == "ordinary"
    assert repaired["inviter_name_snapshot"] == "Recruiter123456"
    assert repaired["state"] == "verifying"
    assert repaired["hold_until"] == old["hold_until"]
    assert repaired["ever_left"] == 0


@pytest.mark.parametrize("operation", ["pay"])
def test_no_payment_api_bypass_before_qualification(scenario, operation):
    store, *_ = scenario
    row = scan(scenario)
    with pytest.raises(CashbackError, match="qualification_required"):
        store.mutate(
            OWNER,
            "5",
            operation,
            [{"id": row["id"], "version": row["version"]}],
            "qualification-bypass-test",
            "Explicit exception",
        )
    assert entry(store)["paid_at"] is None
    assert store.list_entries(OWNER, "5", "pending")["total"] == 0
    assert store.list_entries(OWNER, "5", "verifying")["total"] == 1


def test_exact_three_hour_transition_and_payment(scenario):
    store, *_ = scenario
    row = mature(scenario)
    assert row["state"] == "pending"
    assert store.invitation_summary("5", "1")["verifying"] == 0
    assert store.invitation_summary("5", "1")["pending"] == 300
    result = store.mutate(
        OWNER,
        "5",
        "pay",
        [{"id": row["id"], "version": row["version"]}],
        "qualified-payment-test",
    )
    assert result["amount"] == 300 and entry(store)["state"] == "paid"


def test_restart_keeps_deadline(scenario):
    store, members, evidence, clock = scenario
    row = scan(scenario)
    restored = CashbackStore(store.path)
    restored.snapshot("5", "bot", "99", members, evidence, now=clock[0] + 60)
    assert entry(restored)["hold_until"] == row["hold_until"]
    assert entry(restored)["state"] == "verifying"


def test_unobserved_gap_restarts_even_when_time_has_elapsed(scenario):
    row = scan(scenario)
    updated = scan(scenario, row["hold_until"] + 1)
    assert updated["state"] == "verifying"
    assert updated["hold_until"] == row["hold_until"] + 1 + 10800


def test_departure_and_rejoin_never_regain_reward(scenario):
    store, members, evidence, clock = scenario
    scan(scenario)
    clock[0] = 2060
    store.snapshot("5", "bot", "99", {"1": members["1"]}, [], now=clock[0])
    assert entry(store)["state"] == "void"
    evidence.append({**evidence[0], "event_id": "rejoin", "event_at": 2120})
    row = scan(scenario, 2120)
    assert row["state"] == "void" and row["ever_left"] == 1


def test_agent_price_requires_manual_cashier_switch(scenario):
    store, members, _, clock = scenario
    rule = store.get_group("5")
    store.configure(
        OWNER,
        {**rule, "enabled": True, "agent_rate": None},
        {"1": members["1"]},
        set(),
        now=1500,
    )
    row = scan(scenario)
    assert row["pricing_status"] == "priced" and row["amount"] == 300
    assert row["member_class"] == "ordinary"
    mature(scenario)
    with pytest.raises(CashbackError, match="agent_rate_unconfigured"):
        store.price_preview(
            OWNER, "5", [{"id": row["id"], "version": entry(store)["version"]}], "agent"
        )
    clock[0] += 1
    rule = store.get_group("5")
    store.configure(
        OWNER,
        {**rule, "enabled": True, "agent_rate": 200},
        members,
        set(),
        now=clock[0],
    )
    current = entry(store)
    result = store.mutate(
        OWNER,
        "5",
        "reprice",
        [{"id": current["id"], "version": current["version"]}],
        "manual-agent-rate-request",
        "私信已核验",
        "agent",
        price_confirmation=store.price_preview(
            OWNER, "5", [{"id": current["id"], "version": current["version"]}], "agent"
        ),
    )
    assert result["amount"] == 200
    assert entry(store)["amount"] == 200 and entry(store)["member_class"] == "agent"
    current = entry(store)
    store.mutate(
        OWNER,
        "5",
        "reprice",
        [{"id": current["id"], "version": current["version"]}],
        "manual-ordinary-rate-request",
        "改回普通成员",
        "ordinary",
        price_confirmation=store.price_preview(
            OWNER,
            "5",
            [{"id": current["id"], "version": current["version"]}],
            "ordinary",
        ),
    )
    assert entry(store)["amount"] == 300
    assert entry(store)["member_class"] == "ordinary"


def test_rate_change_freezes_old_six_and_new_five(scenario):
    store, members, evidence, _ = scenario
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "rate": 600, "agent_rate": 900},
        {"1": members["1"]},
        set(),
        now=1500,
    )
    first = scan(scenario, 2000)
    assert first["amount"] == 600
    members["3"] = {"name": "新成员3", "peer": "33", "eligible": True}
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "rate": 500},
        {"1": members["1"]},
        set(),
        now=2030,
    )
    store.snapshot(
        "5",
        "bot",
        "99",
        members,
        [
            {**evidence[0], "event_id": "old-invite", "event_at": 2000},
            {
                "member": "3",
                "inviter": "1",
                "source": "business_apply_log",
                "event_id": "new-invite",
                "event_at": 2040,
            },
        ],
        now=2060,
    )
    assert entry(store)["amount"] == 600
    with store.connect() as db:
        assert (
            db.execute("SELECT amount FROM entries WHERE member='3'").fetchone()[0]
            == 500
        )


def test_missing_avatar_image_does_not_create_agent_price(scenario):
    _, members, *_ = scenario
    members["2"].pop("avatar_digit_count")
    row = scan(scenario)
    assert row["member_class"] == "ordinary"
    assert row["member_avatar_status"] == "manual"
    assert row["pricing_status"] == "priced" and row["amount"] == 300


@pytest.mark.parametrize("avatar_digit_count", [0, 1])
def test_roster_observed_before_reprice_but_evidence_arrives_later(
    scenario, avatar_digit_count
):
    store, members, _, _ = scenario
    members["2"]["avatar_digit_count"] = avatar_digit_count
    store.snapshot("5", "bot", "99", members, [], now=2000)
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "rate": 600, "agent_rate": 200},
        members,
        set(),
        now=2030,
    )
    row = scan(scenario, 2060)
    assert row["amount"] == 300
    assert row["rule_version"] == 1
    assert scan(scenario, 2120)["amount"] == 300


@pytest.mark.parametrize(
    "avatar_digit_count,category,amount",
    [(1, "ordinary", 300), (0, "ordinary", 300)],
)
def test_avatar_evidence_is_frozen_with_original_price_and_audit(
    scenario, avatar_digit_count, category, amount
):
    store, members, _, _ = scenario
    members["2"]["avatar_digit_count"] = avatar_digit_count
    assert scan(scenario)["member_class"] == category
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "rate": 900, "agent_rate": 800},
        members,
        set(),
        now=2030,
    )
    row = scan(scenario, 2060)
    assert row["member_class"] == category
    assert row["amount"] == amount and row["state"] == "verifying"
    assert row["rule_version"] == 1
    assert scan(scenario, 2120)["amount"] == amount
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='invitation_verified'"
            ).fetchone()[0]
            == 1
        )


def test_avatar_resource_digits_are_not_digit_evidence(scenario):
    _, members, _, _ = scenario
    members["2"].pop("avatar_digit_count")
    members["2"]["avatar_ref"] = "123456789012345678"
    row = scan(scenario)
    assert row["amount"] == 300 and row["member_class"] == "ordinary"
    assert row["member_avatar_status"] == "manual"


def test_agent_backfill_uses_first_saved_price_without_replaying_event(scenario):
    store, members, _, _ = scenario
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "agent_rate": None},
        {"1": members["1"]},
        set(),
        now=1500,
    )
    assert scan(scenario)["amount"] == 300
    for at, rate in ((2030, 200), (2040, 400)):
        store.configure(
            OWNER,
            {**store.get_group("5"), "enabled": True, "agent_rate": rate},
            members,
            set(),
            now=at,
        )
    store.snapshot("5", "bot", "99", members, [], now=2060)
    assert entry(store)["amount"] == 300


def test_delayed_native_agent_evidence_keeps_original_price_across_rescans(scenario):
    store, members, evidence, _ = scenario
    rule = store.get_group("5")
    store.configure(
        OWNER,
        {**rule, "enabled": True, "agent_rate": 200},
        {"1": members["1"]},
        set(),
        now=1990,
    )
    evidence[0]["event_at"] = 1950
    row = scan(scenario)
    assert row["amount"] == 300 and row["member_class"] == "ordinary"
    assert scan(scenario, 2060)["verification"] == "verified"
    assert entry(store)["amount"] == 300


def test_pre_upgrade_evidence_cannot_downgrade_new_entry_qualification(scenario):
    store, members, evidence, _ = scenario
    members["1"]["name"] = "Ordinary"
    members["2"]["avatar_digit_count"] = 0
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "qualification_seconds": 0},
        {"1": members["1"]},
        set(),
        now=1500,
    )
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "qualification_seconds": 10800},
        {"1": members["1"]},
        set(),
        now=2030,
    )
    assert evidence[0]["event_at"] == 2000
    row = scan(scenario, 2060)
    assert row["amount"] == 300 and row["member_class"] == "ordinary"
    assert row["state"] == "verifying" and row["hold_until"] == 12860
    with pytest.raises(CashbackError, match="qualification_required"):
        store.mutate(
            OWNER,
            "5",
            "pay",
            [{"id": row["id"], "version": row["version"]}],
            "pre-upgrade-evidence-bypass",
        )


def test_group_api_cannot_disable_three_hour_gate():
    values = {
        "id": "5",
        "bot_id": "bot",
        "contact": "Contact",
        "rate": 300,
        "enabled": True,
        "version": 0,
    }
    assert GroupRule.model_validate(values).qualification_seconds == 10800
    for seconds in (0, 1, True, 10799):
        with pytest.raises(ValueError):
            GroupRule.model_validate({**values, "qualification_seconds": seconds})


@pytest.mark.parametrize("agent_rate", [None, 100])
@pytest.mark.parametrize("avatar_digit_count", [0, 1])
def test_native_history_checks_classified_and_unpriced_records(
    scenario, tmp_path, agent_rate, avatar_digit_count
):
    store, members, _, _ = scenario
    members["2"]["avatar_digit_count"] = avatar_digit_count
    rule = store.get_group("5")
    store.configure(
        OWNER,
        {**rule, "enabled": True, "agent_rate": agent_rate},
        {"1": members["1"]},
        set(),
        now=1500,
    )
    scan(scenario)
    store.snapshot(
        "5",
        "bot",
        "99",
        members,
        [{**scenario[2][0], "source": "nim_notification"}],
        now=2000,
    )
    inbox = tmp_path / "messages.sqlite3"
    with sqlite3.connect(inbox) as db:
        db.execute(
            "CREATE TABLE inbox(account TEXT,team TEXT,message TEXT,state TEXT,payload TEXT)"
        )
        db.execute(
            "INSERT INTO inbox VALUES('99','5','join','invite_notification',?)",
            (
                json.dumps(
                    {
                        "kind": "team_invite",
                        "inviter_peer": "11",
                        "member_peers": ["22"],
                        "event_at": 2000,
                    }
                ),
            ),
        )
    result = check_history(store.path, [inbox], "5")
    assert result["verified_native_invitations"] == 1
    assert result["verified_business_invitations"] == 1
    assert result["verifying_count"] == 1
    assert result["verifying_cents"] == 300


def test_legacy_migration_preserves_every_entry_and_activates_only_new_joins(scenario):
    store, members, _, clock = scenario
    members["2"]["avatar_digit_count"] = 0
    rule = store.get_group("5")
    store.configure(
        OWNER,
        {**rule, "enabled": True, "qualification_seconds": 0, "agent_rate": None},
        {"1": members["1"]},
        set(),
        now=1500,
    )
    old = scan(scenario)
    store.mutate(
        OWNER,
        "5",
        "pay",
        [{"id": old["id"], "version": old["version"]}],
        "legacy-payment-test",
    )
    paid = entry(store)
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE groups DROP COLUMN qualification_seconds")
    restored = CashbackStore(store.path)
    assert entry(restored) == paid
    assert restored.get_group("5")["qualification_seconds"] == 10800
    assert (
        CashbackStore(store.path).get_group("5")["version"]
        == restored.get_group("5")["version"]
    )
    members["3"] = {"name": "New", "peer": "33", "eligible": True}
    restored.snapshot(
        "5",
        "bot",
        "99",
        members,
        [
            {
                "member": "3",
                "inviter": "1",
                "event_id": "new",
                "source": "nim_notification",
                "event_at": clock[0] + 1,
            }
        ],
        now=clock[0] + 1,
    )
    with restored.connect() as db:
        assert (
            db.execute("SELECT state FROM entries WHERE member='3'").fetchone()[0]
            == "verifying"
        )
