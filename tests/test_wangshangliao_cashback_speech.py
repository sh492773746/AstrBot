"""Group-scoped speech qualification, replay boundaries and payment protection."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.cashback import CashbackService
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.cashback_models import GroupRule
from tests.test_wangshangliao_cashback_qualification import (
    OWNER,
    entry,
    mature,
    scan,
)
from tests.test_wangshangliao_cashback_qualification import scenario as scenario


def enable_speech(scenario, enabled=True):
    store, members, _, clock = scenario
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "require_speech": enabled},
        members,
        set(),
        now=clock[0],
    )


def speak(scenario, stamp=None, peer="22"):
    store, members, evidence, clock = scenario
    store.snapshot(
        "5",
        "bot",
        "99",
        members,
        evidence,
        now=clock[0],
        speech=[{"member": "2", "peer": peer, "event_at": stamp or clock[0]}],
        speech_until=clock[0],
    )
    return entry(store)


@pytest.mark.parametrize("operation", ["pay"])
def test_mature_silent_member_cannot_bypass_speech_gate(scenario, operation):
    store, *_ = scenario
    enable_speech(scenario)
    row = mature(scenario)
    assert row["state"] == "verifying" and row["first_spoke_at"] == 0
    assert store.invitation_summary("5", "1")["pending"] == 0
    assert store.list_entries(OWNER, "5", "pending")["payable_count"] == 0
    summary = store.private_invitation_summaries(
        "1", "bot", "99", ("5",), detail_group="5"
    )[0]
    assert summary["verifying"] == summary["unspoken"] == 1
    assert summary["hold_pending"] == summary["spoken"] == summary["pending"] == 0
    assert not summary["items"][0]["payable"]
    with pytest.raises(CashbackError, match="speech_required"):
        store.mutate(
            OWNER,
            "5",
            operation,
            [{"id": row["id"], "version": row["version"]}],
            "speech-bypass-request",
            "Explicit exception",
        )
    assert entry(store)["state"] == "verifying"


def test_speech_does_not_shorten_three_hour_clock_and_is_idempotent(scenario):
    store, _, _, clock = scenario
    enable_speech(scenario)
    scan(scenario)
    clock[0] += 30
    first = speak(scenario)
    assert first["first_spoke_at"] == clock[0]
    assert first["state"] == "verifying" and first["hold_until"] == 12800
    assert speak(scenario)["version"] == first["version"]
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='member_speech_verified'"
            ).fetchone()[0]
            == 1
        )
    row = mature(scenario)
    assert row["state"] == "pending" and row["amount"] == 300
    assert store.invitation_summary("5", "1")["pending"] == 300
    assert store.list_entries(OWNER, "5")["payable_amount"] == 300
    assert (
        store.private_invitation_summaries("1", "bot", "99", ("5",))[0]["pending"]
        == 300
    )
    assert (
        store.mutate(
            OWNER,
            "5",
            "pay",
            [{"id": row["id"], "version": row["version"]}],
            "speech-qualified-payment",
        )["amount"]
        == 300
    )


def test_speaking_after_three_hours_releases_qualification(scenario):
    _, _, _, clock = scenario
    enable_speech(scenario)
    row = mature(scenario)
    clock[0] += 1
    updated = speak(scenario)
    assert row["state"] == "verifying" and updated["state"] == "pending"
    assert updated["hold_until"] == row["hold_until"]


def test_disabling_speech_restores_normal_qualification(scenario):
    store, *_ = scenario
    enable_speech(scenario)
    mature(scenario)
    enable_speech(scenario, False)
    row = scan(scenario)
    assert row["state"] == "pending" and row["first_spoke_at"] == 0
    summary = store.private_invitation_summaries(
        "1", "bot", "99", ("5",), detail_group="5"
    )[0]
    assert summary["pending"] == 300
    assert "first_spoke_at" not in summary["items"][0]


def test_enabling_speech_preserves_paid_history_but_correction_rechecks(scenario):
    store, *_ = scenario
    row = mature(scenario)
    store.mutate(
        OWNER,
        "5",
        "pay",
        [{"id": row["id"], "version": row["version"]}],
        "old-paid-before-speech",
    )
    paid = entry(store)
    enable_speech(scenario)
    assert entry(store) == paid
    store.mutate(
        OWNER,
        "5",
        "correct",
        [{"id": paid["id"], "version": paid["version"]}],
        "withdraw-paid-speech-test",
        "Wrong registration",
    )
    assert entry(store)["state"] == "verifying"
    assert entry(store)["paid_at"] is None
    assert entry(store)["amount"] == paid["amount"]


@pytest.mark.parametrize("stamp,peer", [(1999, "22"), (5000, "22"), (2030, "999")])
def test_pre_join_future_and_wrong_peer_speech_are_rejected(scenario, stamp, peer):
    enable_speech(scenario)
    scan(scenario)
    scenario[3][0] = 2030
    assert speak(scenario, stamp, peer)["first_spoke_at"] == 0


def test_verified_join_evidence_handles_speech_before_first_roster_scan(scenario):
    enable_speech(scenario)
    scenario[3][0] = 2010
    row = speak(scenario, 2005)
    assert row["first_seen"] == 2010 and row["first_spoke_at"] == 2005
    assert row["hold_until"] == 12810


def test_late_join_evidence_rewinds_only_the_speech_replay_cursor(scenario):
    store, members, evidence, clock = scenario
    enable_speech(scenario)
    clock[0] = 2010
    store.snapshot("5", "bot", "99", members, [], now=clock[0], speech_until=clock[0])
    assert store.get_group("5")["speech_cursor"] == 2010
    clock[0] = 2020
    store.snapshot(
        "5", "bot", "99", members, evidence, now=clock[0], speech_until=clock[0]
    )
    assert store.get_group("5")["speech_cursor"] == 2000
    assert speak(scenario, 2005)["first_spoke_at"] == 2005


def test_speech_survives_restart_and_does_not_requalify_rejoins(scenario):
    store, members, evidence, clock = scenario
    enable_speech(scenario)
    scan(scenario)
    clock[0] += 10
    row = speak(scenario)
    restored = CashbackStore(store.path)
    assert entry(restored)["first_spoke_at"] == row["first_spoke_at"]
    assert restored.get_group("5")["require_speech"] == 1
    absent = dict(members)
    absent.pop("2")
    restored.snapshot("5", "bot", "99", absent, evidence, now=clock[0] + 10)
    restored.snapshot("5", "bot", "99", members, evidence, now=clock[0] + 20)
    assert entry(restored)["ever_left"] == 1
    assert entry(restored)["state"] == "void"


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
def test_speech_switch_api_requires_a_real_boolean(value):
    with pytest.raises(ValidationError):
        GroupRule.model_validate(
            {
                "id": "5",
                "bot_id": "bot",
                "contact": "Contact",
                "rate": 500,
                "enabled": True,
                "version": 1,
                "require_speech": value,
            }
        )


@pytest.mark.asyncio
async def test_speech_ingress_is_account_group_peer_and_human_bound(
    scenario, tmp_path, monkeypatch
):
    store, members, evidence, clock = scenario
    enable_speech(scenario)
    scan(scenario)
    clock[0] = 2100
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[]),
    )
    service = CashbackService(context, tmp_path / "service", "bot")
    service.store = store
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.cashback.is_managed_account",
        lambda uid: uid == "1",
    )
    async with aiosqlite.connect(tmp_path / "messages.sqlite3") as db:
        await db.execute(
            "CREATE TABLE inbox(account,team,message,payload,state,received_at)"
        )
        for number, (account, group, sender, peer, text, state, stamp) in enumerate(
            [
                ("99", "5", "2", "22", "Hello", "done", 2050),
                ("88", "5", "2", "22", "Wrong account", "done", 2050),
                ("99", "6", "2", "22", "Wrong group", "done", 2050),
                ("99", "private/2/22", "2", "22", "Private", "done", 2050),
                ("99", "5", "2", "999", "Wrong peer", "done", 2050),
                ("99", "5", "2", "22", "Fake system text", "invite_notification", 2050),
                ("99", "5", "2", "22", "Unsupported", "unsupported", 2050),
                ("99", "5", "2", "22", "Wrong identity", "rejected_identity", 2050),
                ("99", "5", "2", "22", " \u200b ", "done", 2050),
                ("99", "5", "1", "11", "Managed bot", "done", 2050),
                ("99", "5", "2", "22", "Future", "done", 2101),
            ]
        ):
            await db.execute(
                "INSERT INTO inbox VALUES(?,?,?,?,?,?)",
                (
                    account,
                    group,
                    str(number),
                    json.dumps(
                        {
                            "sender": sender,
                            "text": text,
                            "recall_route": {"peer": peer, "time": str(stamp)},
                        }
                    ),
                    state,
                    2070,
                ),
            )
        await db.commit()
        adapter = SimpleNamespace(
            account="99",
            ledger=SimpleNamespace(db=db),
            config={
                "id": "bot",
                "type": "wangshangliao",
                "enable": True,
                "enabled_groups": ["5"],
                "_business_active": True,
                "_business_epoch": 1,
            },
        )
        records, until = await service.speech(
            adapter, "5", members, store.get_group("5")
        )
        assert records == [{"member": "2", "peer": "22", "event_at": 2050}]
        assert until == 2100
        rule = {**store.get_group("5"), "speech_cursor": 2100}
        assert await service.speech(adapter, "5", members, rule) == ([], 2100)
        context.platform_manager.platform_insts = [adapter]
        service.activities = SimpleNamespace(authorize=lambda *_: None)
        service.roster = AsyncMock(return_value=members)
        service.evidence = AsyncMock(return_value=evidence)
        service._deliver_risk_notices = AsyncMock()
        await service._scan("5")
        assert entry(store)["first_spoke_at"] == 2050
        assert entry(store)["state"] == "verifying"


@pytest.mark.asyncio
async def test_disabled_speech_rule_does_not_read_message_history(scenario):
    store, members, evidence, _ = scenario
    adapter = SimpleNamespace(
        account="99",
        config={
            "type": "wangshangliao",
            "id": "bot",
            "enable": True,
            "enabled_groups": ["5"],
            "_business_active": True,
            "_business_epoch": 1,
        },
    )
    service = CashbackService(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter])),
        store.path.parent / "service",
        "bot",
    )
    service.store = store
    service.activities = SimpleNamespace(authorize=lambda *_: None)
    service.roster = AsyncMock(return_value=members)
    service.evidence = AsyncMock(return_value=evidence)
    service.speech = AsyncMock()
    service._deliver_risk_notices = AsyncMock()
    await service._scan("5")
    service.speech.assert_not_awaited()
