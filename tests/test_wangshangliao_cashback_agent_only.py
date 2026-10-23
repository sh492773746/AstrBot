"""Future-only ordinary reward stopping with preserved debts and scoped writes."""

import pytest
from test_wangshangliao_cashback_qualification import (
    OWNER,
    entry,
    mature,
    scan,
)
from test_wangshangliao_cashback_qualification import (
    scenario as qualification_scenario,
)

from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.cashback_models import GroupRule
from scripts.enable_wsl_agent_only import enable_agent_only

scenario = qualification_scenario


def configure(scenario, at, **updates):
    store, members, _, _ = scenario
    return store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, **updates},
        members,
        set(),
        now=at,
    )


def reprice(scenario, category, request):
    store, *_ = scenario
    row = entry(store)
    return store.mutate(
        OWNER,
        "5",
        "reprice",
        [{"id": row["id"], "version": row["version"]}],
        request,
        "Cashier reviewed private proof",
        category,
        price_confirmation=store.price_preview(
            OWNER, "5", [{"id": row["id"], "version": row["version"]}], category
        ),
    )


def test_new_ordinary_rewards_stopped_without_hidden_invites(scenario):
    store, *_ = scenario
    configure(scenario, 1500, agent_only=True)
    row = scan(scenario)
    assert row["amount"] == 0 and row["pricing_status"] == "no_reward"
    assert row["member_class"] == "ordinary" and row["verification"] == "verified"
    assert store.list_entries(OWNER, "5", "no_reward")["total"] == 1
    assert store.list_entries(OWNER, "5", "verifying")["total"] == 0
    assert store.list_entries(OWNER, "5", "pending")["amount"] == 0
    result = store.invitation_summary("5", "1")
    assert result["total"] == 1 and result["no_reward"] == 1
    assert result["verifying"] == result["pending"] == 0
    summary = store.private_invitation_summaries(
        "1", "bot", "99", ["5"], detail_group="5"
    )[0]
    assert summary["agent_only"] == summary["no_reward"] == 1
    assert summary["verifying"] == summary["hold_pending"] == 0
    assert summary["items"][0]["pricing_status"] == "no_reward"


@pytest.mark.parametrize("operation", ["pay", "exception"])
@pytest.mark.parametrize("corrupted", [False, True])
def test_no_payment_or_exception_bypass(scenario, operation, corrupted):
    store, *_ = scenario
    configure(scenario, 1500, agent_only=True)
    scan(scenario)
    if corrupted:
        with store.connect(True) as db:
            db.execute(
                "UPDATE entries SET member_class='legacy',pricing_status='priced',"
                "state='pending',amount=300 WHERE member='2'"
            )
    row = entry(store)
    with pytest.raises(CashbackError, match="ordinary_reward_stopped"):
        store.mutate(
            OWNER,
            "5",
            operation,
            [{"id": row["id"], "version": row["version"]}],
            "agent-only-bypass-request",
            "Explicit exception",
            actual_payment_confirmed=operation == "exception",
        )
    assert entry(store)["paid_at"] is None


@pytest.mark.parametrize("paid", [False, True])
def test_existing_debt_and_payment_unchanged(scenario, paid):
    store, _, _, clock = scenario
    row = mature(scenario)
    if paid:
        store.mutate(
            OWNER,
            "5",
            "pay",
            [{"id": row["id"], "version": row["version"]}],
            "old-ordinary-payment-request",
        )
    before = entry(store)
    configure(scenario, clock[0] + 1, agent_only=True)
    assert entry(store) == before
    row = scan(scenario, clock[0] + 60)
    assert row["amount"] == 300 and row["pricing_status"] == "priced"
    if not paid:
        result = store.mutate(
            OWNER,
            "5",
            "pay",
            [{"id": row["id"], "version": row["version"]}],
            "preserved-old-debt-request",
        )
        assert result["amount"] == 300
    else:
        result = store.mutate(
            OWNER,
            "5",
            "correct",
            [{"id": row["id"], "version": row["version"]}],
            "preserved-old-correction-request",
            "Revoke mistaken paid registration",
        )
        assert result["amount"] == 300 and entry(store)["state"] == "pending"


def test_delayed_evidence_before_cutoff_keeps_old_price(scenario):
    store, members, evidence, _ = scenario
    configure(scenario, 2030, agent_only=True)
    row = scan(scenario, 2060)
    assert evidence[0]["event_at"] == 2000
    assert row["amount"] == 300 and row["rule_version"] == 1
    assert scan(scenario, 2120)["pricing_status"] == "priced"
    assert store.list_entries(OWNER, "5", "no_reward")["total"] == 0
    assert members["2"]["eligible"]


def test_entry_observed_before_cutoff_keeps_frozen_rule(scenario):
    store, members, _, _ = scenario
    store.snapshot("5", "bot", "99", members, [], now=2000)
    configure(scenario, 2030, agent_only=True)
    row = scan(scenario, 2060)
    assert row["amount"] == 300 and row["rule_version"] == 1


def test_agent_switch_preserves_three_hour_gate_and_rescan(scenario):
    store, *_ = scenario
    configure(scenario, 1500, agent_only=True)
    original = scan(scenario)
    assert (
        reprice(scenario, "agent", "agent-switch-before-three-hours")["amount"] == 100
    )
    row = entry(store)
    assert row["hold_until"] == original["hold_until"]
    assert row["pricing_status"] == "priced" and row["member_class"] == "agent"
    with pytest.raises(CashbackError, match="qualification_required"):
        store.mutate(
            OWNER,
            "5",
            "pay",
            [{"id": row["id"], "version": row["version"]}],
            "agent-early-payment-request",
        )
    assert scan(scenario, 2060)["amount"] == 100
    row = mature(scenario)
    result = store.mutate(
        OWNER,
        "5",
        "pay",
        [{"id": row["id"], "version": row["version"]}],
        "agent-qualified-payment-request",
    )
    assert result["amount"] == 100 and entry(store)["state"] == "paid"


def test_switch_back_to_ordinary_is_zero_and_nonpayable(scenario):
    store, *_ = scenario
    configure(scenario, 1500, agent_only=True)
    scan(scenario)
    reprice(scenario, "agent", "manual-agent-proof-request")
    row = mature(scenario)
    assert row["state"] == "pending"
    assert reprice(scenario, "ordinary", "revoke-agent-proof-request")["amount"] == 0
    assert entry(store)["pricing_status"] == "no_reward"
    assert store.list_entries(OWNER, "5", "pending")["total"] == 0
    assert store.list_entries(OWNER, "5", "no_reward")["total"] == 1
    assert scan(scenario, 12860)["amount"] == 0


def test_no_retroactive_rewards_when_switch_disabled_or_restarted(scenario):
    store, members, evidence, clock = scenario
    configure(scenario, 1500, agent_only=True)
    scan(scenario)
    configure(scenario, 2030, agent_only=False)
    restored = CashbackStore(store.path)
    restored.snapshot("5", "bot", "99", members, evidence, now=2060)
    assert entry(restored)["pricing_status"] == "no_reward"
    assert reprice(scenario, "agent", "after-disable-agent-proof")["amount"] == 100
    assert reprice(scenario, "ordinary", "after-disable-ordinary-proof")["amount"] == 0
    members["3"] = {"name": "Next ordinary member", "peer": "33", "eligible": True}
    clock[0] = 2120
    store.snapshot(
        "5",
        "bot",
        "99",
        members,
        [{**evidence[0], "member": "3", "event_id": "next", "event_at": 2120}],
        now=clock[0],
    )
    with store.connect() as db:
        assert (
            db.execute("SELECT amount FROM entries WHERE member='3'").fetchone()[0]
            == 300
        )


def test_speech_evidence_is_kept_for_later_agent_verification(scenario):
    store, members, evidence, clock = scenario
    configure(scenario, 1500, agent_only=True, require_speech=True)
    scan(scenario)
    for at in range(2060, 12861, 60):
        scan(scenario, at)
    assert entry(store)["pricing_status"] == "no_reward"
    reprice(scenario, "agent", "silent-agent-proof-request")
    row = scan(scenario, 12920)
    assert row["state"] == "verifying"
    clock[0] = 12980
    store.snapshot(
        "5",
        "bot",
        "99",
        members,
        evidence,
        now=clock[0],
        speech=[{"member": "2", "peer": "22", "event_at": 12980}],
    )
    assert entry(store)["state"] == "pending"


def test_manual_attribution_cannot_restore_ordinary_reward(scenario):
    store, members, evidence, _ = scenario
    configure(scenario, 1500, agent_only=True)
    members["3"] = {"name": "First invited member", "peer": "33", "eligible": True}
    store.snapshot(
        "5",
        "bot",
        "99",
        members,
        [{**evidence[0], "member": "3", "event_id": "third"}],
        now=2000,
    )
    row = entry(store)
    assert row["inviter"] == ""
    store.mutate(
        OWNER,
        "5",
        "assign",
        [{"id": row["id"], "version": row["version"]}],
        "assign-nonreward-invite-request",
        "Cashier checked original invitation",
        inviter="1",
        members=members,
    )
    assert entry(store)["amount"] == 0
    assert entry(store)["pricing_status"] == "no_reward"
    assert store.list_entries(OWNER, "5", "no_reward")["total"] == 2


def test_agent_only_rule_is_strict_and_omission_preserves_existing(scenario):
    store, members, _, _ = scenario
    configure(scenario, 1500, agent_only=True)
    rule = dict(store.get_group("5"))
    rule.pop("agent_only")
    store.configure(OWNER, {**rule, "enabled": True}, members, set(), now=1600)
    assert store.get_group("5")["agent_only"] == 1
    payload = {
        "id": "5",
        "bot_id": "bot",
        "contact": "Cashier",
        "rate": 300,
        "enabled": True,
        "version": 0,
    }
    assert GroupRule.model_validate(payload).agent_only is False
    assert GroupRule.model_validate({**payload, "agent_only": True}).agent_only is True
    for value in ("true", "false", 1, 0, None):
        with pytest.raises(ValueError):
            GroupRule.model_validate({**payload, "agent_only": value})


def test_live_rule_activation_is_atomic_backed_up_and_future_only(scenario, tmp_path):
    store, _, _, clock = scenario
    scan(scenario)
    with store.connect() as db:
        before = db.execute("SELECT * FROM entries ORDER BY id").fetchall()
    clock[0] = 2060
    rule = store.get_group("5")
    result = enable_agent_only(
        store.path, "5", rule["version"], "bot", tmp_path / "backup"
    )
    assert result["effective_at"] == 2060 and result["protected_records"] == len(before)
    assert store.get_group("5")["agent_only"] == 1
    with store.connect() as db:
        assert db.execute("SELECT * FROM entries ORDER BY id").fetchall() == before
        assert (
            db.execute(
                "SELECT agent_only FROM reward_rule_history ORDER BY version DESC LIMIT 1"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT actor FROM audit WHERE action='rules_changed' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            == "maintenance:user_requested"
        )
    assert scan(scenario, 2060)["amount"] == 300
    with pytest.raises(ValueError):
        enable_agent_only(store.path, "5", rule["version"], "bot", tmp_path / "retry")


@pytest.mark.parametrize(
    "group,version,bot", [("6", 1, "bot"), ("5", 99, "bot"), ("5", 1, "other")]
)
def test_activation_rejects_unreviewed_scope_without_writes(
    scenario, tmp_path, group, version, bot
):
    store, *_ = scenario
    before = store.get_group("5")
    with pytest.raises(ValueError):
        enable_agent_only(store.path, group, version, bot, tmp_path / "backup")
    assert store.get_group("5") == before
    assert not (tmp_path / "backup").exists()
