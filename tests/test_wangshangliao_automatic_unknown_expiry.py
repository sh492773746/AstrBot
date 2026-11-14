"""Expired unknown actions need independent native evidence, never a retry."""

import asyncio
import json
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import automatic
from astrbot.core.platform.sources.wangshangliao.business import BusinessClient
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.fixtures.wsl_actions import bind_action_port


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(automatic, "instance_dir", lambda _: tmp_path)
    instance = SimpleNamespace(
        account="1",
        nim_account="11",
        config={
            "id": "unknown-expiry",
            "enabled_groups": ["5", "6"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "progressive_mute": True,
                "recall_enabled": True,
                "permissions": {"5": ["mute", "recall"], "6": ["mute", "recall"]},
            },
        },
        members={"5": {"2": "22", "3": "33"}, "6": {"2": "22"}},
        groups={"5": "55", "6": "66"},
        nim=SimpleNamespace(get_team_muted_members=AsyncMock(return_value=set())),
        business=SimpleNamespace(
            request=AsyncMock(
                return_value={
                    "groupMemberInfo": [
                        {"userId": "1", "nimId": "11", "groupRole": "GROUP_ROLE_ADMIN"},
                        {
                            "userId": "2",
                            "nimId": "22",
                            "groupRole": "GROUP_ROLE_MEMBER",
                        },
                        {
                            "userId": "3",
                            "nimId": "33",
                            "groupRole": "GROUP_ROLE_MEMBER",
                        },
                    ]
                }
            )
        ),
        diagnostics=SimpleNamespace(emit=Mock()),
        native_actions=[],
    )

    async def execute(
        operation, action, group, member, text="", *, minutes=1, action_check=None
    ):
        if action_check:
            await action_check()
        instance.native_actions.append((operation, action, group, member, minutes))
        result = {
            "operation": operation,
            "action": action,
            "group": group,
            "member": member,
            "minutes": minutes,
            "status": "accepted",
            "submitted": True,
        }
        with closing(automatic.database(instance)) as db, db:
            db.execute(
                "INSERT INTO operations VALUES(?,'fixture',?)",
                (operation, json.dumps(result)),
            )
        return result

    instance.execute_moderation = AsyncMock(side_effect=execute)
    return bind_action_port(instance)


@pytest.fixture
def seed(adapter):
    def insert(
        operation="old",
        *,
        age=5 * 86400,
        duration=3,
        account="1",
        group="5",
        member="2",
        peer="22",
        message="old-message",
        receipt_change=None,
    ):
        receipt = {
            "operation": operation,
            "action": "mute",
            "group": int(group),
            "member": int(member),
            "minutes": duration,
            "status": "unknown",
            "submitted": True,
        }
        receipt.update(receipt_change or {})
        encoded = json.dumps(receipt)
        with closing(automatic.database(adapter)) as db, db:
            db.execute(
                "INSERT INTO automatic_mutes VALUES(?,?,?,?,?,?,'unknown',?,0)",
                (operation, account, group, member, peer, message, time.time() - age),
            )
            db.execute(
                "INSERT INTO automatic_mute_durations VALUES(?,?,0)",
                (operation, duration),
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations("
                "id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
            )
            db.execute(
                "INSERT INTO operations VALUES(?,'fixture',?)", (operation, encoded)
            )
        return encoded

    return insert


def old_state(adapter, operation="old"):
    with closing(automatic.database(adapter)) as db:
        return db.execute(
            "SELECT status,closed FROM automatic_mutes WHERE operation=?", (operation,)
        ).fetchone()


@pytest.mark.asyncio
async def test_recent_unknown_stays_held_even_with_a_clear_native_list(adapter, seed):
    seed(age=120)
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    adapter.nim.get_team_muted_members.assert_not_awaited()
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted", [None, False, "true"])
async def test_stale_reservation_is_not_completion_evidence(adapter, seed, submitted):
    seed(receipt_change={"submitted": submitted})
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    adapter.nim.get_team_muted_members.assert_not_awaited()
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_expired_finished_unknown_allows_only_new_violation(adapter, seed):
    encoded = seed()
    with closing(automatic.database(adapter)) as db, db:
        db.execute(
            "INSERT INTO automatic_mutes VALUES("
            "'accepted-before','1','5','2','22','first','accepted',1,0)"
        )
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["status"] == "accepted"
    assert result["mute_count"] == 2
    assert result["minutes"] == 3
    assert adapter.native_actions == [("fresh", "mute", 5, 2, 3)]
    assert old_state(adapter) == ("unknown", 2)
    with closing(automatic.database(adapter)) as db:
        assert db.execute(
            "SELECT result FROM operations WHERE id='old'"
        ).fetchone() == (encoded,)
        evidence, resolved_at = db.execute(
            "SELECT evidence,resolved_at FROM automatic_mute_resolutions WHERE operation='old'"
        ).fetchone()
    evidence = json.loads(evidence)
    assert evidence["reason"] == "expired_unknown_native_unmuted"
    assert evidence["source"] == "native_mute_directory"
    assert evidence["muted"] is False
    assert evidence["receipt"] == json.loads(encoded)
    assert (
        evidence["account"],
        evidence["group"],
        evidence["member"],
        evidence["peer"],
    ) == (
        "1",
        "5",
        "2",
        "22",
    )
    assert evidence["checked_at"] <= resolved_at
    await automatic.mute_and_escalate(adapter, "old", "5", "2", "different-message")
    await automatic.mute_and_escalate(adapter, "new-id", "5", "2", "old-message")
    await automatic.mute_and_escalate(adapter, "fresh", "5", "2", "new-message")
    assert adapter.native_actions == [("fresh", "mute", 5, 2, 3)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,message", [("old", "new-message"), ("different", "old-message")]
)
async def test_stale_unknown_replay_never_resolves_or_retries(
    adapter, seed, operation, message
):
    seed()
    result = await automatic.mute_and_escalate(adapter, operation, "5", "2", message)
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    adapter.nim.get_team_muted_members.assert_not_awaited()
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_fresh_native_mute_is_never_shortened(adapter, seed):
    seed()
    adapter.nim.get_team_muted_members.return_value = {"22"}
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ProtocolError("mute_list_rejected"), TimeoutError()]
)
async def test_native_read_failure_stays_held(adapter, seed, failure):
    seed()
    adapter.nim.get_team_muted_members.side_effect = failure
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_missing_or_ambiguous_roster_cannot_prove_absence(adapter, seed):
    seed()
    adapter.business.request.return_value = {"groupMemberInfo": []}
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"operation": "other"},
        {"action": "unmute"},
        {"group": 6},
        {"member": 3},
        {"account": "9"},
        {"instance": "other"},
        {"minutes": 10},
        {"minutes": True},
        {"expected_nim": "other"},
    ],
)
async def test_receipt_must_match_the_original_intent(adapter, seed, change):
    seed(receipt_change=change)
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    adapter.nim.get_team_muted_members.assert_not_awaited()
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["receipt", "duration", "submitted"])
async def test_missing_evidence_is_not_inferred_from_age(adapter, seed, missing):
    seed()
    with closing(automatic.database(adapter)) as db, db:
        if missing == "receipt":
            db.execute("DELETE FROM operations")
        elif missing == "duration":
            db.execute("DELETE FROM automatic_mute_durations")
        else:
            receipt = json.loads(
                db.execute("SELECT result FROM operations WHERE id='old'").fetchone()[0]
            )
            del receipt["submitted"]
            db.execute("UPDATE operations SET result=?", (json.dumps(receipt),))
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["{", "null", "[]"])
async def test_malformed_receipt_stays_unknown(adapter, seed, value):
    seed()
    with closing(automatic.database(adapter)) as db, db:
        db.execute("UPDATE operations SET result=?", (value,))
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope", [{"account": "9"}, {"group": "6"}, {"member": "3", "peer": "33"}]
)
async def test_unrelated_scope_is_not_resolved_or_blocking(adapter, seed, scope):
    seed(**scope)
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["status"] == "accepted"
    assert old_state(adapter) == ("unknown", 0)
    adapter.nim.get_team_muted_members.assert_not_awaited()
    assert adapter.native_actions == [("fresh", "mute", 5, 2, 1)]


@pytest.mark.asyncio
async def test_rebound_native_identity_does_not_release_an_old_unknown(adapter, seed):
    seed(peer="previous-peer")
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    adapter.nim.get_team_muted_members.assert_not_awaited()
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"])
async def test_native_protected_role_cannot_be_quarantined(adapter, seed, role):
    seed()
    adapter.business.request.return_value["groupMemberInfo"][1]["groupRole"] = role
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["grants", "automation", "peer", "account", "rule"])
async def test_revocation_during_native_read_never_resolves_or_submits(
    adapter, seed, change
):
    seed()
    revoked = False

    def execution_check():
        if revoked:
            raise ProtocolError("rule_revoked")

    async def query(_):
        nonlocal revoked
        if change == "grants":
            adapter.config["moderation"]["permissions"] = {}
        elif change == "automation":
            adapter.config["moderation"]["automation_enabled"] = False
        elif change == "peer":
            adapter.members["5"]["2"] = "changed"
        elif change == "account":
            adapter.account = "another"
        else:
            revoked = True
        return set()

    adapter.nim.get_team_muted_members.side_effect = query
    if change == "rule":
        result = await automatic.mute_and_escalate(
            adapter, "fresh", "5", "2", "new-message", execution_check=execution_check
        )
        assert result["reason"] == "prior_mute_unknown"
    else:
        with pytest.raises(ProtocolError):
            await automatic.mute_and_escalate(
                adapter,
                "fresh",
                "5",
                "2",
                "new-message",
                execution_check=execution_check,
            )
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_native_mute_appearing_after_resolution_stops_submission(adapter, seed):
    seed()
    adapter.nim.get_team_muted_members.side_effect = [set(), {"22"}]
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["status"] == "rejected"
    assert result["submitted"] is False
    assert old_state(adapter) == ("unknown", 2)
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_final_action_callback_rechecks_native_mutes(adapter, seed):
    seed()
    original = adapter.execute_moderation.side_effect

    async def execute(*args, **kwargs):
        adapter.nim.get_team_muted_members.return_value = {"22"}
        return await original(*args, **kwargs)

    adapter.execute_moderation.side_effect = execute
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["status"] == "rejected"
    assert result["submitted"] is False
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_restart_preserves_native_mute_protection_after_quarantine(adapter, seed):
    seed()
    await automatic.mute_and_escalate(adapter, "fresh", "5", "2", "new-message")
    adapter.nim.get_team_muted_members.return_value = {"22"}
    restarted = bind_action_port(SimpleNamespace(**vars(adapter)))
    result = await automatic.mute_and_escalate(
        restarted, "fresh-after-restart", "5", "2", "newer-message"
    )
    assert result["status"] == "rejected"
    assert result["submitted"] is False
    assert adapter.native_actions == [("fresh", "mute", 5, 2, 1)]
    assert old_state(adapter) == ("unknown", 2)


@pytest.mark.asyncio
async def test_resolution_native_read_does_not_hold_the_sqlite_writer(adapter, seed):
    seed()

    async def query(_):
        with closing(automatic.database(adapter)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO automatic_kicks VALUES("
                "'unrelated','1','6','3','33','none','unknown',0)"
            )
        return set()

    adapter.nim.get_team_muted_members.side_effect = query
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["status"] == "accepted"


@pytest.mark.asyncio
async def test_changed_evidence_during_native_read_is_not_quarantined(adapter, seed):
    seed()

    async def query(_):
        with closing(automatic.database(adapter)) as db, db:
            db.execute(
                "UPDATE automatic_mute_durations SET minutes=10 WHERE operation='old'"
            )
        return set()

    adapter.nim.get_team_muted_members.side_effect = query
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_final_http_fence_rereads_without_redundant_intermediate_io(
    adapter, seed
):
    seed()
    business = Mock(spec=BusinessClient)
    business.request = adapter.business.request
    business._authorization_task = None
    adapter.business = business
    original = adapter.execute_moderation.side_effect

    async def execute(*args, **kwargs):
        adapter.nim.get_team_muted_members.return_value = {"22"}
        business._authorization_task = asyncio.current_task()
        try:
            return await original(*args, **kwargs)
        finally:
            business._authorization_task = None

    adapter.execute_moderation.side_effect = execute
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["status"] == "rejected"
    assert result["submitted"] is False
    assert adapter.nim.get_team_muted_members.await_count == 2
    assert old_state(adapter) == ("unknown", 2)
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_revocation_at_final_action_callback_never_submits(adapter, seed):
    seed()
    original = adapter.execute_moderation.side_effect

    async def execute(*args, **kwargs):
        adapter.config["moderation"]["permissions"] = {}
        return await original(*args, **kwargs)

    adapter.execute_moderation.side_effect = execute
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["status"] == "rejected"
    assert result["submitted"] is False
    assert old_state(adapter) == ("unknown", 2)
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["nim", "business", "team", "native_account"])
async def test_native_binding_changed_after_quarantine_never_submits(
    adapter, seed, change
):
    seed()
    original = adapter.execute_moderation.side_effect

    async def execute(*args, **kwargs):
        if change == "nim":
            adapter.nim = SimpleNamespace(
                get_team_muted_members=AsyncMock(return_value=set())
            )
        elif change == "business":
            adapter.business = SimpleNamespace(request=adapter.business.request)
        elif change == "team":
            adapter.groups["5"] = "999"
        else:
            adapter.nim_account = "33"
        return await original(*args, **kwargs)

    adapter.execute_moderation.side_effect = execute
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["status"] == "rejected"
    assert result["submitted"] is False
    assert old_state(adapter) == ("unknown", 2)
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_cancelled_native_read_keeps_the_unknown_open(adapter, seed):
    seed()
    adapter.nim.get_team_muted_members.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await automatic.mute_and_escalate(adapter, "fresh", "5", "2", "new-message")
    assert old_state(adapter) == ("unknown", 0)
    assert not adapter.native_actions


@pytest.mark.asyncio
async def test_resolution_batch_is_bounded(adapter, seed):
    for index in range(17):
        seed(f"old-{index}", message=f"old-message-{index}")
    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "new-message"
    )
    assert result["reason"] == "prior_mute_unknown"
    assert old_state(adapter, "old-0") == ("unknown", 0)
    adapter.nim.get_team_muted_members.assert_not_awaited()
    assert not adapter.native_actions
