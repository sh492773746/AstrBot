"""Automatic duplicate results never need unrelated native mute reconciliation."""

import asyncio
import json
import socket
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import automatic
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_automatic_unknown_expiry import (
    adapter as expiry_adapter,
)
from tests.test_wangshangliao_automatic_unknown_expiry import seed as expiry_seed

adapter = expiry_adapter
seed = expiry_seed


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """Forbid external connections and subprocesses in these transport doubles."""

    def forbidden(*args, **kwargs):
        raise AssertionError("automatic_dedup_external_io_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=forbidden)
    )


def ledger_snapshot(adapter):
    """Read all automatic and native evidence for exact before/after comparison."""
    with closing(automatic.database(adapter)) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "automatic_mutes",
                "automatic_mute_durations",
                "automatic_mute_resolutions",
                "operations",
            )
        }


def finish_seed(adapter, operation, status, closed=0):
    """Set the automatic bookkeeping without changing the seeded native receipt."""
    with closing(automatic.database(adapter)) as db, db:
        db.execute(
            "UPDATE automatic_mutes SET status=?,closed=? WHERE operation=?",
            (status, closed, operation),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("progressive", [False, True])
@pytest.mark.parametrize("match", ["operation", "message", "operation_other_message"])
@pytest.mark.parametrize(
    "status,closed",
    [
        ("accepted", 0),
        ("verified", 0),
        ("rejected", 0),
        ("unknown", 2),
        ("unknown", 1),
        ("accepted", 1),
    ],
)
async def test_processed_duplicate_precedes_unrelated_unknown(
    adapter, seed, progressive, match, status, closed
):
    adapter.config["moderation"]["progressive_mute"] = progressive
    seed(
        "processed",
        message="processed-message",
        duration=1,
        receipt_change={"status": status},
    )
    finish_seed(adapter, "processed", status, closed)
    operation = "alias" if match == "message" else "processed"
    message = (
        "different-message"
        if match == "operation_other_message"
        else "processed-message"
    )
    # Preserve the existing duplicate shape, including its count-based minutes.
    expected = await automatic.mute_and_escalate(adapter, operation, "5", "2", message)
    count = int(status in {"accepted", "verified"} and closed == 0)
    assert expected == {
        "status": status,
        "action": "mute",
        "minutes": 3 if progressive and count else 1,
        "mute_count": count,
        "reason": "automatic_kick_disabled",
    }
    seed()
    before = ledger_snapshot(adapter)
    adapter.business.request.side_effect = TimeoutError
    adapter.nim.get_team_muted_members.return_value = {"22"}

    result = await automatic.mute_and_escalate(adapter, operation, "5", "2", message)

    assert result == expected
    assert ledger_snapshot(adapter) == before
    adapter.business.request.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [30, 5 * 86400])
@pytest.mark.parametrize("match", ["operation", "message"])
async def test_open_unknown_duplicate_remains_held_without_reads(
    adapter, seed, age, match
):
    seed("processed", message="processed-message", age=age)
    seed()
    before = ledger_snapshot(adapter)

    result = await automatic.mute_and_escalate(
        adapter,
        "processed" if match == "operation" else "alias",
        "5",
        "2",
        "processed-message",
    )

    assert result == {
        "status": "unknown",
        "action": "mute",
        "reason": "prior_mute_unknown",
    }
    assert ledger_snapshot(adapter) == before
    adapter.business.request.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        {"account": "9"},
        {"group": "6"},
        {"member": "3", "peer": "33"},
        {"peer": "previous-peer"},
    ],
)
@pytest.mark.parametrize("matching_message", [False, True])
async def test_operation_scope_conflict_cannot_reuse_another_result(
    adapter, seed, scope, matching_message
):
    seed(
        "processed",
        message="different-message",
        receipt_change={"status": "accepted"},
        **scope,
    )
    finish_seed(adapter, "processed", "accepted")
    if matching_message:
        seed(
            "same-message",
            message="current-message",
            receipt_change={"status": "verified"},
        )
        finish_seed(adapter, "same-message", "verified")
    seed()
    before = ledger_snapshot(adapter)

    with pytest.raises(ProtocolError, match="automatic_mute_operation_conflict"):
        await automatic.mute_and_escalate(
            adapter, "processed", "5", "2", "current-message"
        )

    assert ledger_snapshot(adapter) == before
    adapter.business.request.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_message_with_rebound_peer_is_not_a_duplicate(adapter, seed):
    seed(
        "processed",
        message="processed-message",
        peer="previous-peer",
        receipt_change={"status": "accepted"},
    )
    finish_seed(adapter, "processed", "accepted")
    before = ledger_snapshot(adapter)

    with pytest.raises(ProtocolError, match="automatic_mute_operation_conflict"):
        await automatic.mute_and_escalate(
            adapter, "alias", "5", "2", "processed-message"
        )

    assert ledger_snapshot(adapter) == before
    adapter.business.request.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope", [{"account": "9"}, {"group": "6"}, {"member": "3", "peer": "33"}]
)
async def test_same_message_in_another_scope_does_not_suppress_new_intent(
    adapter, seed, scope
):
    encoded = seed("other", message="current-message", **scope)

    result = await automatic.mute_and_escalate(
        adapter, "fresh", "5", "2", "current-message"
    )

    assert result["status"] == "accepted"
    assert result["mute_count"] == 1
    assert adapter.native_actions == [("fresh", "mute", 5, 2, 1)]
    with closing(automatic.database(adapter)) as db:
        assert db.execute(
            "SELECT result FROM operations WHERE id='other'"
        ).fetchone() == (encoded,)
        assert db.execute(
            "SELECT status,closed FROM automatic_mutes WHERE operation='other'"
        ).fetchone() == ("unknown", 0)
    adapter.business.request.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "verified", "rejected", "unknown"])
async def test_exact_operation_wins_over_same_message_alias(adapter, seed, status):
    seed("alias", message="processed-message", receipt_change={"status": "accepted"})
    finish_seed(adapter, "alias", "accepted")
    seed(
        "processed",
        message="processed-message",
        receipt_change={"status": status},
    )
    finish_seed(adapter, "processed", status)
    seed()
    before = ledger_snapshot(adapter)

    result = await automatic.mute_and_escalate(
        adapter, "processed", "5", "2", "processed-message"
    )

    if status == "unknown":
        assert result == {
            "status": "unknown",
            "action": "mute",
            "reason": "prior_mute_unknown",
        }
    else:
        assert result == {
            "status": status,
            "action": "mute",
            "minutes": 10 if status in {"accepted", "verified"} else 3,
            "mute_count": 2 if status in {"accepted", "verified"} else 1,
            "reason": "automatic_kick_disabled",
        }
    assert ledger_snapshot(adapter) == before
    adapter.business.request.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_appearing_during_native_read_precedes_quarantine(
    adapter, seed
):
    encoded = seed()

    async def another_worker_finished(team):
        seed(
            "processed",
            message="processed-message",
            receipt_change={"status": "accepted"},
        )
        return set()

    adapter.nim.get_team_muted_members.side_effect = another_worker_finished

    result = await automatic.mute_and_escalate(
        adapter, "processed", "5", "2", "processed-message"
    )

    assert result == {
        "status": "accepted",
        "action": "mute",
        "minutes": 3,
        "mute_count": 1,
        "reason": "automatic_kick_disabled",
    }
    after = ledger_snapshot(adapter)
    assert next(row for row in after["operations"] if row[0] == "old")[2] == encoded
    assert next(row for row in after["automatic_mutes"] if row[0] == "old")[6] == (
        "unknown"
    )
    assert next(row for row in after["automatic_mutes"] if row[0] == "old")[8] == 0
    assert after["automatic_mute_resolutions"] == []
    assert adapter.business.request.await_count == 1
    assert adapter.nim.get_team_muted_members.await_count == 1
    adapter.execute_moderation.assert_not_awaited()
    assert not adapter.native_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "verified"])
async def test_interrupted_duplicate_reconciles_only_from_saved_receipt(
    adapter, seed, status
):
    encoded = seed(
        "processed",
        message="processed-message",
        receipt_change={"status": status},
    )
    seed()
    before = ledger_snapshot(adapter)

    result = await automatic.mute_and_escalate(
        adapter, "processed", "5", "2", "processed-message"
    )

    assert result["status"] == status
    assert result["mute_count"] == 1
    after = ledger_snapshot(adapter)
    assert after["operations"] == before["operations"]
    assert after["automatic_mute_durations"] == before["automatic_mute_durations"]
    assert after["automatic_mute_resolutions"] == []
    assert next(row for row in after["automatic_mutes"] if row[0] == "old") == next(
        row for row in before["automatic_mutes"] if row[0] == "old"
    )
    assert json.loads(encoded)["status"] == status
    adapter.business.request.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_waiting_for_ledger_still_obeys_revocation(
    adapter, seed, tmp_path
):
    seed("processed", receipt_change={"status": "accepted"})
    finish_seed(adapter, "processed", "accepted")
    seed()
    before = ledger_snapshot(adapter)
    with closing(sqlite3.connect(tmp_path / "moderation.sqlite3")) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        work = asyncio.create_task(
            automatic.mute_and_escalate(adapter, "processed", "5", "2", "old-message")
        )
        try:
            await asyncio.sleep(0.03)
            assert not work.done()
            adapter.config["moderation"]["permissions"] = {}
            blocker.rollback()
            with pytest.raises(ProtocolError):
                await asyncio.wait_for(work, 3)
        finally:
            blocker.rollback()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)

    assert ledger_snapshot(adapter) == before
    adapter.business.request.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()
