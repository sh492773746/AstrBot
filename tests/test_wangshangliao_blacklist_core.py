"""Offline blacklist persistence, capability binding and native execution tests."""

import asyncio
import inspect
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import (
    adapter as adapter_module,
)
from astrbot.core.platform.sources.wangshangliao import (
    blacklist,
    moderation,
)
from astrbot.core.platform.sources.wangshangliao import (
    event as event_module,
)
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest, dispatch
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.blacklist import (
    BlacklistStore,
    authorize_intent,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Keep every ledger and native client in an isolated offline fixture.

    Args:
        tmp_path: Test-owned temporary root.
        monkeypatch: Scoped module replacements.

    Returns:
        Isolated per-instance blacklist store.
    """
    import aiohttp

    for module in (blacklist, moderation, adapter_module):
        monkeypatch.setattr(
            module, "instance_dir", lambda instance: tmp_path / instance
        )
    for module in (event_module, moderation, adapter_module):
        monkeypatch.setattr(module, "is_managed_account", lambda uid: uid == "23691273")
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "_request",
        AsyncMock(
            side_effect=AssertionError("Network access forbidden in offline tests")
        ),
    )
    value = BlacklistStore("bot")
    value.set_member(
        "1",
        "5",
        "2",
        "22",
        "Member",
        True,
        {"uid": "9", "peer": "99"},
        lambda: None,
    )
    return value


def reserve(store, join_id="101"):
    """Reserve a new canonical membership event in the fixture's active cycle.

    Args:
        store: Isolated blacklist store.
        join_id: Native application-log identity.

    Returns:
        Reserved immutable capability, or None for duplicate work.
    """
    return store.reserve("1", "5", "2", "22", join_id, time.time(), lambda: None)


def proof(intent):
    """Build the complete native receipt used by successful mute tests.

    Args:
        intent: Stored blacklist capability.

    Returns:
        Fully bound accepted mute receipt.
    """
    return {
        "status": "accepted",
        "operation": intent.operation,
        "action": intent.action,
        "group": int(intent.group),
        "member": int(intent.member),
        "expected_nim": intent.peer,
        "minutes": intent.minutes,
        "submitted": True,
    }


def authorize(intent):
    """Check a capability using the exact production function signature.

    Args:
        intent: Stored blacklist capability.
    """
    authorize_intent(
        intent.instance,
        intent.account,
        intent.operation,
        intent.action,
        intent.group,
        intent.member,
        intent.minutes,
        intent,
    )


def test_repeat_add_retains_cycle_created_count_and_original_manager(store):
    intent = reserve(store)
    store.done(intent, proof(intent))
    before = store.read("1", "5")
    after = store.set_member(
        "1",
        "5",
        "2",
        "changed-peer",
        "Changed",
        True,
        "8",
        lambda: None,
        expected_version=before[0],
    )
    assert after == before
    assert after[1][0]["kicks"] == 0
    assert after[1][0]["actor"] == "9"
    assert after[1][0]["actor_peer"] == "99"
    assert BlacklistStore("bot").read("1", "5") == before


def test_removal_ends_cycle_keeps_audit_and_invalidates_old_capability(store):
    intent = reserve(store)
    version, old = store.read("1", "5")
    assert store.set_member(
        "1",
        "5",
        "2",
        "22",
        "Member",
        False,
        "9",
        lambda: None,
        version,
    ) == (version + 1, [])
    with pytest.raises(ProtocolError, match="stale"):
        authorize(intent)
    store.done(intent, proof(intent))
    _, entries = store.set_member(
        "1",
        "5",
        "2",
        "22",
        "Member",
        True,
        "9",
        lambda: None,
    )
    assert entries[0]["cycle"] == old[0]["cycle"] + 1
    assert entries[0]["id"] != old[0]["id"]
    assert entries[0]["kicks"] == 0
    assert reserve(store) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "SELECT status,kicks FROM blacklist_entries WHERE id=?",
            (intent.entry,),
        ).fetchone() == ("removed", 0)
        assert db.execute("SELECT COUNT(*) FROM blacklist_audit").fetchone()[0] >= 5
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM blacklist_audit")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE blacklist_audit SET kind='forged'")


def test_optimistic_version_conflict_and_group_account_instance_isolation(store):
    with pytest.raises(ProtocolError, match="version_changed"):
        store.set_member(
            "1",
            "5",
            "3",
            "33",
            "Other",
            True,
            "9",
            lambda: None,
            expected_version=0,
        )
    assert store.read("1", "6") == (0, [])
    assert store.read("3", "5") == (0, [])
    assert BlacklistStore("other").read("1", "5") == (0, [])
    assert len(store.read("1", "5")[1]) == 1


def test_duration_is_scoped_durable_versioned_and_idempotent(store):
    assert store.settings("1", "5") == {
        "version": 1,
        "mute_minutes": 41760,
        "policy_version": 0,
    }
    configured = store.configure(
        "1", "5", 60, ActorIdentity("1", "9", "99", "9"), lambda: None, 1
    )
    assert configured == {"version": 2, "mute_minutes": 60, "policy_version": 1}
    assert BlacklistStore("bot").settings("1", "5") == configured
    assert (
        store.configure("1", "5", 60, "dashboard/offline-admin", lambda: None, 2)
        == configured
    )
    assert store.settings("1", "6")["mute_minutes"] == 41760
    assert store.settings("3", "5")["mute_minutes"] == 41760
    assert BlacklistStore("other").settings("1", "5")["mute_minutes"] == 41760
    assert len(store.read("1", "5")[1]) == 1
    with sqlite3.connect(store.path) as db:
        audits = db.execute(
            "SELECT payload FROM blacklist_audit WHERE kind='configure'"
        ).fetchall()
    assert len(audits) == 1
    assert json.loads(audits[0][0])["actor_peer"] == "99"


@pytest.mark.parametrize("minutes", [True, False, 0, -1, 41761, 60.0, "60", None])
def test_invalid_duration_never_changes_policy_or_issues_an_intent(store, minutes):
    before = store.settings("1", "5")
    with pytest.raises(ProtocolError, match="minutes"):
        store.configure("1", "5", minutes, "9", lambda: None)
    assert store.settings("1", "5") == before


def test_duration_change_checks_preview_and_commit_guard(store):
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        return calls < 2

    with pytest.raises(ProtocolError, match="authority_changed"):
        store.configure("1", "5", 30, "9", guard, 1)
    assert store.settings("1", "5")["mute_minutes"] == 41760
    with pytest.raises(ProtocolError, match="version_changed"):
        store.configure("1", "5", 30, "9", lambda: None, 0)
    for actor in (
        "dashboard/",
        "dashboard/bad\nactor",
        "not-a-principal",
        ActorIdentity("3", "9", "99", "9"),
        ActorIdentity("1", "9", "99"),
    ):
        with pytest.raises(ProtocolError):
            store.configure("1", "5", 30, actor, lambda: None)
    assert store.settings("1", "5")["version"] == 1


def test_duration_change_and_aba_restore_cannot_reauthorize_reserved_work(store):
    old = reserve(store)
    store.configure("1", "5", 30, "9", lambda: None, 1)
    with pytest.raises(ProtocolError, match="stale"):
        authorize(old)
    store.configure("1", "5", 41760, "9", lambda: None, 2)
    with pytest.raises(ProtocolError, match="stale"):
        authorize(old)
    # Configuration never resolves an uncertain native submission.
    assert reserve(store, "102") is None
    store.done(old, {"status": "rejected", "submitted": False})
    fresh = reserve(store, "102")
    assert fresh.policy_version == 2
    authorize(fresh)


def test_delayed_old_join_never_inherits_a_new_duration(store):
    store.configure("1", "5", 1, "9", lambda: None)
    joined_at = time.time()
    store.configure("1", "5", 41760, "9", lambda: None)
    assert store.reserve("1", "5", "2", "22", "101", joined_at, lambda: None) is None
    # Ignoring old evidence creates no unknown intent and doesn't block a new join.
    current = reserve(BlacklistStore("bot"), "102")
    assert current.minutes == 41760
    authorize(current)


@pytest.mark.parametrize("minutes", [1, 60, 1441, 2880, 41760])
def test_configured_duration_is_exactly_bound_to_native_capability(store, minutes):
    store.configure("1", "5", minutes, "9", lambda: None)
    intent = reserve(store)
    assert (intent.action, intent.minutes) == ("mute", minutes)
    authorize(intent)
    with pytest.raises(ProtocolError):
        authorize(replace(intent, minutes=minutes + 1))


@pytest.mark.parametrize("legacy_action", ["kick", "mute"])
def test_upgrade_rejects_old_intents_without_rewriting_audit_or_counters(
    store, legacy_action
):
    intent = reserve(store)
    legacy = replace(
        intent,
        action=legacy_action,
        minutes=1 if legacy_action == "kick" else 41760,
        policy_version=-1,
    )
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE blacklist_intents SET action=?,minutes=?,policy_version=-1 WHERE id=?",
            (legacy.action, legacy.minutes, legacy.id),
        )
        db.execute("UPDATE blacklist_entries SET kicks=2 WHERE id=?", (legacy.entry,))
        audit_before = db.execute("SELECT * FROM blacklist_audit").fetchall()
    with pytest.raises(ProtocolError):
        authorize(legacy)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT * FROM blacklist_audit").fetchall() == audit_before
    assert store.read("1", "5")[1][0]["kicks"] == 2
    assert reserve(store, "102") is None


def test_additive_schema_upgrade_never_replays_legacy_mute_intents(store):
    intent = reserve(store)
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE blacklist_intents DROP COLUMN policy_version")
        before = db.execute("SELECT * FROM blacklist_audit").fetchall()
    assert store.settings("1", "5")["mute_minutes"] == 41760
    with pytest.raises(ProtocolError, match="stale"):
        authorize(intent)
    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "SELECT policy_version FROM blacklist_intents WHERE id=?", (intent.id,)
        ).fetchone() == (-1,)
        assert db.execute("SELECT * FROM blacklist_audit").fetchall() == before
    assert reserve(store, "102") is None


def test_guard_rechecked_at_commit_rolls_back_both_state_and_audit(store):
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProtocolError("revoked")

    with pytest.raises(ProtocolError, match="revoked"):
        store.set_member("1", "5", "3", "33", "Other", True, "9", guard)
    assert calls == 2
    assert store.read("1", "5")[0] == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM blacklist_audit").fetchone()[0] == 1


def test_guards_cannot_be_async_or_false(store):
    async def guard():
        return None

    for check in (guard, lambda: False):
        with pytest.raises(ProtocolError, match="guard_async|authority_changed"):
            store.set_member("1", "5", "3", "33", "Other", True, "9", check)


def test_creating_manager_and_dashboard_principals_are_retained_without_grants(store):
    actor = ActorIdentity("1", "9", "99", "9")
    _, entries = store.set_member(
        "1",
        "5",
        "3",
        "33",
        "Other",
        True,
        actor,
        lambda: None,
    )
    created = next(entry for entry in entries if entry["member"] == "3")
    assert (created["actor"], created["actor_peer"]) == ("9", "99")
    _, entries = store.set_member(
        "1",
        "5",
        "4",
        "44",
        "Dashboard entry",
        True,
        "dashboard/offline-admin",
        lambda: None,
    )
    created = next(entry for entry in entries if entry["member"] == "4")
    assert (created["actor"], created["actor_peer"]) == ("dashboard/offline-admin", "")
    for actor in (ActorIdentity("3", "9", "99", "9"), ActorIdentity("1", "9", "99")):
        with pytest.raises(ProtocolError):
            store.set_member("1", "5", "6", "66", "Invalid", True, actor, lambda: None)


@pytest.mark.asyncio
async def test_writer_contention_rechecks_guard_without_blocking_event_loop(store):
    blocker = sqlite3.connect(store.path)
    blocker.execute("BEGIN IMMEDIATE")
    started = threading.Event()
    permitted = True

    def work():
        started.set()
        return store.reserve(
            "1",
            "5",
            "2",
            "22",
            "101",
            time.time(),
            lambda: permitted,
        )

    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.03)
        assert not task.done()
        permitted = False
        blocker.rollback()
        with pytest.raises(ProtocolError, match="authority_changed"):
            await asyncio.wait_for(task, 3)
        assert blocker.execute("SELECT COUNT(*) FROM blacklist_intents").fetchone() == (
            0,
        )
    finally:
        blocker.rollback()
        blocker.close()
        await asyncio.gather(task, return_exceptions=True)


def test_parallel_stores_reserve_only_one_member_round(store):
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(
            workers.map(
                lambda _: reserve(BlacklistStore("bot")),
                range(6),
            )
        )
    assert sum(result is not None for result in results) == 1
    assert reserve(BlacklistStore("bot"), "102") is None


def test_canonical_join_must_be_newer_than_blacklist_and_have_exact_peer(store):
    created = store.read("1", "5")[1][0]["created"]
    assert (
        store.reserve(
            "1",
            "5",
            "2",
            "22",
            "100",
            created - 1,
            lambda: None,
        )
        is None
    )
    assert (
        store.reserve(
            "1",
            "5",
            "2",
            "changed",
            "101",
            time.time(),
            lambda: None,
        )
        is None
    )
    for join_id in ("notice/native-message", "", "00101", "0", True, "join/101"):
        with pytest.raises(ProtocolError):
            store.reserve(
                "1",
                "5",
                "2",
                "22",
                join_id,
                time.time(),
                lambda: None,
            )

    # The native application-log reader accepts up to 20 decimal digits.
    intent = reserve(store, "18446744073709551615")
    assert intent.join_id == "18446744073709551615"
    authorize(intent)


def test_pending_notices_are_durable_bounded_and_never_rearmed(store):
    now = time.time()
    assert store.observe("1", "5", "native-message", ["22", "22"], now, lambda: None)
    assert not store.observe(
        "1",
        "5",
        "native-message",
        ["33"],
        now,
        lambda: None,
    )
    assert BlacklistStore("bot").pending("1") == [
        {
            "account": "1",
            "group": "5",
            "message": "native-message",
            "peers": ["22"],
            "event_at": now,
            "state": "pending",
            "scope": None,
        }
    ]
    assert store.pending("3") == []
    for peers in ([], ["22"] * 201, [None]):
        with pytest.raises(ProtocolError, match="notice"):
            store.observe("1", "5", "bad-message", peers, now, lambda: None)
    store.finish_notice("1", "5", "native-message", "done")
    store.finish_notice("1", "5", "native-message", "unknown")
    assert not store.observe(
        "1",
        "5",
        "native-message",
        ["22"],
        now,
        lambda: None,
    )
    assert store.pending("1") == []
    with pytest.raises(ProtocolError):
        store.finish_notice("1", "5", "native-message", "pending")


def test_every_rejoin_mutes_without_ever_incrementing_legacy_kicks(store):
    for index in range(4):
        intent = reserve(store, str(101 + index))
        assert (intent.action, intent.minutes) == ("mute", 41760)
        authorize(intent)
        store.done(intent, proof(intent))
        store.done(intent, proof(intent))
        assert store.read("1", "5")[1][0]["kicks"] == 0
        assert reserve(store, str(101 + index)) is None
    intent = reserve(store, "105")
    assert (intent.action, intent.minutes) == ("mute", 41760)
    authorize(intent)
    for forged in (
        replace(intent, action="kick", minutes=1),
        replace(intent, minutes=1440),
    ):
        with pytest.raises(ProtocolError):
            authorize(forged)


@pytest.mark.parametrize(
    "result",
    [
        {"status": "accepted"},
        {"status": "unknown"},
        {"status": "verified"},
        {"status": "verified", "absence_verified": True, "readback_complete": True},
        {"status": []},
        {},
    ],
)
def test_unproven_outcome_blocks_future_rounds_even_after_restart_and_readd(
    store, result
):
    intent = reserve(store)
    store.done(intent, result)
    assert store.read("1", "5")[1][0]["kicks"] == 0
    assert reserve(BlacklistStore("bot"), "102") is None
    store.set_member("1", "5", "2", "22", "Member", False, "9", lambda: None)
    store.set_member("1", "5", "2", "22", "Member", True, "9", lambda: None)
    assert reserve(BlacklistStore("bot"), "103") is None
    with pytest.raises(ProtocolError, match="stale"):
        authorize(intent)


def test_rejected_round_does_not_count_or_block_later_distinct_join(store):
    intent = reserve(store)
    store.done(intent, {"status": "rejected", "submitted": False})
    assert store.read("1", "5")[1][0]["kicks"] == 0
    assert reserve(store) is None
    next_intent = reserve(store, "102")
    assert next_intent.action == "mute"
    assert next_intent.id != intent.id


def test_done_also_rejects_boolean_counterfeit_cycle(store):
    intent = reserve(store)
    with pytest.raises(ProtocolError, match="invalid"):
        store.done(replace(intent, cycle=True), proof(intent))
    assert store.read("1", "5")[1][0]["kicks"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"instance": "other"},
        {"account": "3"},
        {"group": "6"},
        {"member": "3"},
        {"peer": "33"},
        {"entry": "forged"},
        {"id": "forged"},
        {"cycle": 2},
        {"version": 2},
        {"join_id": "102"},
        {"action": "kick", "minutes": 1},
        {"minutes": 1440},
        {"policy_version": 1},
        {"policy_version": True},
        {"cycle": True},
        {"version": True},
        {"minutes": True},
    ],
)
def test_all_capability_fields_are_bound_to_persistent_state(store, change):
    intent = reserve(store)
    with pytest.raises(FrozenInstanceError):
        intent.peer = "33"
    with pytest.raises(ProtocolError):
        authorize(replace(intent, **change))
    assert store.read("1", "5")[1][0]["kicks"] == 0


@pytest.fixture
def native(store):
    """Expose real action and adapter methods over an in-memory native client.

    Args:
        store: Isolated durable blacklist fixture.

    Returns:
        Adapter and mutable remote state for admission/readback race tests.
    """
    state = SimpleNamespace(
        peer="22",
        role="GROUP_ROLE_MEMBER",
        bot_role="GROUP_ROLE_ADMIN",
        present=True,
        complete=True,
        mutations=[],
        before_submit=None,
        readback="absent",
        failure=None,
    )
    client = SimpleNamespace(fields=[0, 0, 0, "1"], lock=asyncio.Lock())

    async def request(route, parameters, *, authorize=None):
        if route == "/v1/group/get-group-list":
            return {"owner": [{"groupId": 5, "me": {"role": state.bot_role}}]}
        if route == "/v1/group/get-group-members":
            rows = (
                [{"userId": 2, "nimId": state.peer, "groupRole": state.role}]
                if state.present
                else []
            )
            return {"groupMemberInfo": rows}
        assert route in {
            "/v1/group/remove-group-member",
            "/v1/group/set-member-mute",
        }
        # Model the last native HTTP-lock admission, not just an earlier callback.
        async with client.lock:
            if state.before_submit is not None:
                result = state.before_submit()
                if inspect.isawaitable(result):
                    await result
            try:
                if authorize is not None:
                    result = authorize()
                    if inspect.isawaitable(result):
                        await result
            except BaseException as exc:
                exc.submitted = False
                raise
            state.mutations.append((route, dict(parameters)))
            if state.failure is not None:
                raise state.failure
            if route == "/v1/group/remove-group-member":
                if state.readback == "absent":
                    state.present = False
                elif state.readback == "partial":
                    state.present = False
                    state.complete = False
            return {}

    client.request = AsyncMock(side_effect=request)
    value = SimpleNamespace(
        account="1",
        config={
            "id": "bot",
            "enable": True,
            "_business_active": True,
            "_business_epoch": "epoch-1",
            "_business_revision": "revision-1",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "manual_kick_only": True,
                "content_rules_since": 1,
                "permissions": {"5": ["mute", "kick"]},
            },
        },
        members={"5": {"2": "22"}},
        business=client,
        stopping=asyncio.Event(),
        get_moderation_members=AsyncMock(),
    )
    for name in ("execute_action", "execute_moderation", "_execute_moderation"):
        setattr(value, name, getattr(WangshangliaoAdapter, name).__get__(value))
    return value, state


def action_request(native, intent):
    """Bind a stored intent to the real runtime-generation action contract.

    Args:
        native: Isolated adapter.
        intent: Stored blacklist capability.

    Returns:
        Versioned request preserving the exact stored intent.
    """
    return ActionRequest.bind(
        native,
        intent.operation,
        intent.action,
        int(intent.group),
        member=int(intent.member),
        minutes=intent.minutes,
        expected_nim=intent.peer,
        blacklist_intent=intent,
    )


@pytest.mark.asyncio
async def test_end_to_end_stored_intent_bypasses_only_manual_confirmation(
    store, native
):
    adapter, state = native
    intent = reserve(store)

    def check_reservations():
        with sqlite3.connect(store.path) as db:
            assert db.execute(
                "SELECT status,finished FROM blacklist_intents WHERE id=?",
                (intent.id,),
            ).fetchone() == ("unknown", 0)
        with sqlite3.connect(store.path.parent / "moderation.sqlite3") as db:
            result = json.loads(
                db.execute(
                    "SELECT result FROM operations WHERE id=?",
                    (intent.operation,),
                ).fetchone()[0]
            )
        assert result["status"] == "unknown"

    state.before_submit = lambda: asyncio.to_thread(check_reservations)
    result = await dispatch(adapter, action_request(adapter, intent))
    assert result["status"] == "accepted"
    assert result["minutes"] == 41760
    assert result["expected_nim"] == "22"
    store.done(intent, result)
    assert store.read("1", "5")[1][0]["kicks"] == 0
    assert state.mutations == [
        ("/v1/group/set-member-mute", {"groupId": 5, "userId": 2, "min": 41760}),
    ]
    with pytest.raises(ProtocolError):
        await dispatch(adapter, action_request(adapter, intent))
    assert len(state.mutations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dispatch", "adapter", "native"])
async def test_prefix_and_callback_alone_never_grant_blacklist_authority(
    store,
    native,
    boundary,
):
    adapter, state = native
    callback = AsyncMock()
    with pytest.raises(ProtocolError):
        if boundary == "dispatch":
            await dispatch(
                adapter,
                ActionRequest.bind(
                    adapter,
                    "blacklist/forged",
                    "kick",
                    5,
                    member=2,
                    expected_nim="22",
                ),
                authorize=callback,
            )
        elif boundary == "adapter":
            await adapter.execute_moderation(
                "blacklist/forged",
                "kick",
                5,
                2,
                action_check=callback,
            )
        else:
            await moderation.execute(
                adapter.business,
                "bot",
                "1",
                "blacklist/forged",
                "kick",
                5,
                2,
                expected_nim="22",
                authorize=callback,
            )
    assert state.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dispatch", "adapter", "native"])
async def test_intent_cannot_be_laundered_to_another_operation_or_member(
    store,
    native,
    boundary,
):
    adapter, state = native
    intent = reserve(store)
    with pytest.raises(ProtocolError):
        if boundary == "dispatch":
            request = replace(
                action_request(adapter, intent), operation="confirmed-kick/x"
            )
            await dispatch(adapter, request, authorize=AsyncMock())
        elif boundary == "adapter":
            await adapter.execute_moderation(
                intent.operation,
                "kick",
                5,
                3,
                blacklist_intent=intent,
            )
        else:
            await moderation.execute(
                adapter.business,
                "bot",
                "1",
                intent.operation,
                "kick",
                5,
                2,
                expected_nim="different",
                blacklist_intent=intent,
            )
    assert state.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "remove",
        "cycle",
        "peer",
        "role",
        "bot_role",
        "managed",
        "account",
        "epoch",
        "duration",
    ],
)
async def test_last_native_submission_rechecks_durable_and_current_authority(
    store,
    native,
    monkeypatch,
    change,
):
    adapter, state = native
    intent = reserve(store)

    async def revoke():
        if change in {"remove", "cycle"}:
            await asyncio.to_thread(
                store.set_member,
                "1",
                "5",
                "2",
                "22",
                "Member",
                False,
                "9",
                lambda: None,
            )
            if change == "cycle":
                await asyncio.to_thread(
                    store.set_member,
                    "1",
                    "5",
                    "2",
                    "22",
                    "Member",
                    True,
                    "9",
                    lambda: None,
                )
        elif change == "peer":
            state.peer = "changed"
        elif change == "role":
            state.role = "GROUP_ROLE_ADMIN"
        elif change == "bot_role":
            state.bot_role = "GROUP_ROLE_MEMBER"
        elif change == "managed":
            monkeypatch.setattr(moderation, "is_managed_account", lambda _: True)
        elif change == "account":
            adapter.business.fields[3] = "3"
        elif change == "duration":
            await asyncio.to_thread(
                store.configure, "1", "5", 60, "dashboard/offline-admin", lambda: None
            )
        else:
            adapter.config["_business_epoch"] = "new"

    state.before_submit = revoke
    with pytest.raises(ProtocolError):
        await dispatch(adapter, action_request(adapter, intent))
    assert state.mutations == []
    with sqlite3.connect(store.path.parent / "moderation.sqlite3") as db:
        evidence = json.loads(
            db.execute(
                "SELECT result FROM operations WHERE id=?",
                (intent.operation,),
            ).fetchone()[0]
        )
    assert evidence["status"] == "rejected"
    assert evidence["submitted"] is False
    store.done(intent, evidence)
    assert store.read("1", "5")[1] == [] or store.read("1", "5")[1][0]["kicks"] == 0


@pytest.mark.asyncio
async def test_epoch_fence_runs_after_the_last_native_database_wait(
    store,
    native,
    monkeypatch,
):
    adapter, state = native
    intent = reserve(store)
    entered, release = threading.Event(), threading.Event()
    original = moderation.authorize_intent
    calls = 0

    def contended(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(3)
        original(*args)

    monkeypatch.setattr(moderation, "authorize_intent", contended)
    task = asyncio.create_task(dispatch(adapter, action_request(adapter, intent)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        adapter.config["_business_epoch"] = "revoked-during-last-db-wait"
        release.set()
        with pytest.raises(ProtocolError, match="authority_changed"):
            await asyncio.wait_for(task, 3)
        assert state.mutations == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_callback_cannot_revoke_then_bypass_persistent_authority(
    store,
    native,
):
    adapter, state = native
    intent = reserve(store)
    checks = 0

    def admit():
        state.native_admitted = True

    async def callback():
        nonlocal checks
        if getattr(state, "native_admitted", False):
            checks += 1
            if checks == 2:
                await asyncio.to_thread(
                    store.set_member,
                    "1",
                    "5",
                    "2",
                    "22",
                    "Member",
                    False,
                    "9",
                    lambda: None,
                )

    state.before_submit = admit
    with pytest.raises(ProtocolError, match="stale"):
        await moderation.execute(
            adapter.business,
            "bot",
            "1",
            intent.operation,
            "mute",
            5,
            2,
            minutes=intent.minutes,
            expected_nim="22",
            blacklist_intent=intent,
            authorize=callback,
        )
    assert checks == 2
    assert state.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", ["present", "partial"])
async def test_accepted_mute_keeps_member_and_never_uses_kick_readback(
    store,
    native,
    monkeypatch,
    readback,
):
    adapter, state = native
    state.readback = readback
    original = moderation.member_directory

    async def directory(*args, **kwargs):
        page = await original(*args, **kwargs)
        page["complete"] = state.complete
        return page

    monkeypatch.setattr(moderation, "member_directory", directory)
    intent = reserve(store)
    result = await dispatch(adapter, action_request(adapter, intent))
    assert result["status"] == "accepted"
    assert result.get("absence_verified") is not True
    store.done(intent, result)
    assert store.read("1", "5")[1][0]["kicks"] == 0
    assert reserve(BlacklistStore("bot"), "102").action == "mute"


@pytest.mark.asyncio
async def test_unknown_native_outcome_is_persisted_and_never_submitted_twice(
    store, native
):
    adapter, state = native
    state.failure = TimeoutError()
    intent = reserve(store)
    result = await dispatch(adapter, action_request(adapter, intent))
    assert result["status"] == "unknown"
    assert len(state.mutations) == 1
    assert await dispatch(adapter, action_request(adapter, intent)) == result
    assert len(state.mutations) == 1
    store.done(intent, result)
    assert reserve(BlacklistStore("bot"), "102") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [1, 1440, 1441, 2880, 41760])
async def test_configured_blacklist_mute_reaches_native_but_ordinary_cap_stays_one_day(
    store,
    native,
    minutes,
):
    adapter, state = native
    store.configure("1", "5", minutes, "9", lambda: None)
    intent = reserve(store)
    result = await dispatch(adapter, action_request(adapter, intent))
    assert result["status"] == "accepted"
    assert result["minutes"] == minutes
    assert state.present is True
    assert state.mutations == [
        ("/v1/group/set-member-mute", {"groupId": 5, "userId": 2, "min": minutes}),
    ]
    for minutes in (1441, 41759, 41760, 41761):
        with pytest.raises(ProtocolError):
            await dispatch(
                adapter,
                ActionRequest.bind(
                    adapter, "normal", "mute", 5, member=2, minutes=minutes
                ),
                authorize=AsyncMock(),
            )
        with pytest.raises(ProtocolError, match="minutes"):
            await moderation.execute(
                adapter.business,
                "bot",
                "1",
                "normal",
                "mute",
                5,
                2,
                minutes=minutes,
                authorize=AsyncMock(),
            )
    assert len(state.mutations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "message/5/old",
        "content/5/old",
        "progressive/old",
        "ai/old",
        "normal",
    ],
)
async def test_existing_manual_kick_only_gate_stays_closed(store, native, operation):
    adapter, state = native
    with pytest.raises(ProtocolError, match="private_confirmation_required"):
        await adapter.execute_moderation(
            operation,
            "kick",
            5,
            2,
            action_check=AsyncMock(),
        )
    assert state.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["self", "managed", "admin", "owner"])
async def test_ordinary_member_mutations_cannot_target_protected_identities(
    store,
    native,
    monkeypatch,
    target,
):
    adapter, state = native
    member = 2
    if target == "self":
        member = 1
    elif target == "managed":
        monkeypatch.setattr(moderation, "is_managed_account", lambda _: True)
    else:
        state.role = "GROUP_ROLE_ADMIN" if target == "admin" else "GROUP_ROLE_OWNER"
    with pytest.raises(ProtocolError, match="target"):
        await moderation.execute(
            adapter.business,
            "bot",
            "1",
            "ordinary",
            "mute",
            5,
            member,
            authorize=AsyncMock(),
        )
    assert state.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["dispatch", "adapter", "native"])
async def test_managed_23691273_has_no_blacklist_exemption(
    store,
    native,
    monkeypatch,
    boundary,
):
    adapter, state = native
    store.set_member(
        "1",
        "5",
        "23691273",
        "55",
        "Managed",
        True,
        "9",
        lambda: None,
    )
    intent = store.reserve(
        "1",
        "5",
        "23691273",
        "55",
        "105",
        time.time(),
        lambda: None,
    )
    adapter.members["5"]["23691273"] = "55"
    monkeypatch.setattr(
        moderation,
        "member_directory",
        AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {
                        "userId": "23691273",
                        "nimId": "55",
                        "groupRole": "GROUP_ROLE_MEMBER",
                    }
                ],
            }
        ),
    )
    with pytest.raises(ProtocolError, match="target"):
        if boundary == "dispatch":
            await dispatch(adapter, action_request(adapter, intent))
        elif boundary == "adapter":
            await adapter.execute_moderation(
                intent.operation,
                "mute",
                5,
                23691273,
                minutes=intent.minutes,
                blacklist_intent=intent,
            )
        else:
            await moderation.execute(
                adapter.business,
                "bot",
                "1",
                intent.operation,
                "mute",
                5,
                23691273,
                minutes=intent.minutes,
                expected_nim="55",
                blacklist_intent=intent,
            )
    assert state.mutations == []
