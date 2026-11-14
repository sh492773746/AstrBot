"""Deterministic newcomer defense, durable receipts and guarded ingress tests."""

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import raid_defense
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.raid_defense import RaidDefense
from astrbot.core.platform.sources.wangshangliao import raid
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest, dispatch
from astrbot.core.platform.sources.wangshangliao.raid import RaidStore
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(raid, "instance_dir", lambda _: tmp_path)
    return RaidStore("fixture")


def joined(store, uid="2", *, group="5", at=1001, join_id=None):
    """Create one isolated, already corroborated membership round."""
    store.configure("1", group, True, now=1000)
    return store.join(
        "1",
        group,
        uid,
        uid + "0",
        join_id or uid,
        at,
        guard=lambda: None,
        now=at,
    )


def reserve(store, uid="2", *, kind="burst", trigger="100", at=1002, group="5"):
    """Reserve one test intent using a deterministic independent clock."""
    return store.reserve(
        "1",
        group,
        uid,
        kind=kind,
        trigger=trigger,
        guard=lambda: None,
        now=at,
    )


def test_default_off_and_no_historical_or_duplicate_joins(store):
    assert not store.snapshot("1", "5", now=1000)["state"]
    store.configure("1", "5", True, now=1000)
    assert (
        store.join("1", "5", "2", "20", "1", 999, guard=lambda: None, now=1001) is None
    )
    member = joined(store)
    assert member["observed_until"] == 1601
    assert joined(store) is None
    assert (
        store.join("1", "5", "2", "20", "9", 1000, guard=lambda: None, now=1001) is None
    )
    assert store.snapshot("1", "5", now=1001)["members"][0]["join_id"] == "2"


@pytest.mark.parametrize("offset,active", [(59, True), (60, True), (60.001, False)])
def test_two_distinct_members_within_sixty_seconds(store, offset, active):
    joined(store)
    joined(store, "3")
    reserve(store, at=1002)
    second = reserve(store, "3", trigger="101", at=1002 + offset)
    assert second
    state = store.snapshot("1", "5", now=1063)["state"]
    assert bool(state["protection_until"]) is active
    if active:
        assert state["protection_until"] == 1002 + offset + 900


def test_same_uid_does_not_activate_and_groups_are_isolated(store):
    joined(store)
    reserve(store)
    reserve(store, trigger="101", at=1013)
    joined(store, "3", group="6")
    reserve(store, "3", group="6", trigger="102")
    assert store.snapshot("1", "5", now=1014)["state"]["protection_until"] == 0
    assert store.snapshot("1", "6", now=1014)["state"]["protection_until"] == 0


def test_protection_holds_only_subsequent_arrivals_and_never_extends(store):
    joined(store)
    joined(store, "3")
    reserve(store)
    reserve(store, "3", trigger="101", at=1003)
    period = store.snapshot("1", "5", now=1003)["state"]
    assert reserve(store, kind="hold", trigger="2", at=1004) is None
    joined(store, "4", at=1004)
    hold = reserve(store, "4", kind="hold", trigger="4", at=1004)
    assert hold.minutes == 1
    reserve(store, "3", trigger="102", at=1014)
    assert store.snapshot("1", "5", now=1014)["state"] == period
    joined(store, "5", at=1904)
    assert reserve(store, "5", kind="hold", trigger="5", at=1904) is None


def test_rejoin_restart_and_ceil_remaining_without_resetting_full_mute(store):
    joined(store)
    intent = reserve(store)
    store.finish(intent, "accepted", now=1003)
    restarted = RaidStore("fixture")
    member = joined(restarted, at=1090, join_id="99")
    assert member["mute_until"] == 1603
    assert member["violations"] == 1
    restoration = reserve(restarted, kind="restore", trigger="99", at=1090)
    assert restoration.minutes == 9
    assert restoration.deadline == 1603
    restarted.authorize(restoration, now=1091)
    restarted.finish(restoration, "accepted", now=1092)
    assert restarted.snapshot("1", "5", now=1092)["members"][0]["mute_until"] == 1603
    with pytest.raises(ProtocolError):
        restarted.authorize(intent, now=1093)
    assert reserve(restarted, kind="restore", trigger="expired", at=1603) is None


@pytest.mark.parametrize("status", ["unknown", "rejected", "skipped"])
def test_only_success_creates_debt_and_unknown_is_not_retried(store, status):
    joined(store)
    intent = reserve(store)
    store.finish(intent, status, now=1003)
    assert store.snapshot("1", "5", now=1003)["members"][0]["mute_until"] == 0
    assert reserve(store) is None
    newer = reserve(store, trigger="101", at=1013)
    assert bool(newer) is (status != "unknown")


def test_manual_unmute_revokes_remaining_and_inflight_capabilities(store):
    joined(store)
    intent = reserve(store)
    store.release("1", "5", "2")
    store.finish(intent, "accepted", now=1003)
    assert store.snapshot("1", "5", now=1003)["members"][0]["mute_until"] == 0
    with pytest.raises(ProtocolError):
        store.authorize(intent, now=1004)


def test_replayed_old_unmute_does_not_erase_a_newer_mute(store):
    joined(store)
    first = reserve(store)
    store.finish(first, "accepted", now=1003)
    store.release("1", "5", "2", operation="manual-release-1")
    next_intent = reserve(store, trigger="new-burst", at=1014)
    store.finish(next_intent, "accepted", now=1015)
    store.release("1", "5", "2", operation="manual-release-1")
    assert store.snapshot("1", "5", now=1016)["members"][0]["mute_until"] == 1615


@pytest.mark.parametrize("change", ["disable", "peer", "join", "minutes", "member"])
def test_final_capability_fence(store, change):
    joined(store)
    intent = reserve(store)
    if change == "disable":
        store.configure("1", "5", False, now=1003)
    elif change == "peer":
        store.join("1", "5", "2", "21", "99", 1003, guard=lambda: None, now=1003)
    elif change == "join":
        joined(store, at=1003, join_id="99")
    else:
        intent = replace(intent, **{change: 1 if change == "minutes" else "3"})
    with pytest.raises(ProtocolError):
        store.authorize(intent, now=1004)


def test_cleanup_protects_unknown_and_retains_separate_audit(store):
    joined(store)
    joined(store, "3")
    unknown = reserve(store)
    accepted = reserve(store, "3", trigger="101")
    store.finish(unknown, "unknown", now=1003)
    store.finish(accepted, "accepted", now=1003)
    store.prune(now=1003 + 600 + 86401)
    with store.connect() as db:
        assert [row["member"] for row in db.execute("SELECT * FROM raid_members")] == [
            "2"
        ]
        assert db.execute("SELECT COUNT(*) FROM raid_intents").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM raid_audit").fetchone()[0] == 2


def test_switch_changes_revoke_pending_but_preserve_accepted_debt(store):
    joined(store)
    intent = reserve(store)
    store.finish(intent, "accepted", now=1003)
    store.configure("1", "5", False, now=1004)
    store.configure("1", "5", True, now=1005)
    assert reserve(store, trigger="old_observation", at=1006) is None
    member = store.join("1", "5", "2", "20", "99", 1006, guard=lambda: None, now=1006)
    assert member["mute_until"] == 1603
    assert reserve(store, kind="restore", trigger="99", at=1007).minutes == 10


def test_batch_recall_reservation_is_atomic_and_preserves_unknowns(store):
    assert store.reserve_recalls(
        "1", "5", "2", ["1", "2", "2"], guard=lambda: None
    ) == ["1", "2"]
    store.recall("1", "5", "2", "1", status="accepted")
    assert store.reserve_recalls(
        "1", "5", "2", ["1", "2", "3"], guard=lambda: None
    ) == ["3"]
    calls = 0

    def revoked():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProtocolError("raid_policy_changed")

    with pytest.raises(ProtocolError, match="raid_policy_changed"):
        store.reserve_recalls("1", "5", "2", ["4", "5"], guard=revoked)
    with store.connect() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT message,status FROM raid_recalls ORDER BY message"
            )
        ] == [("1", "accepted"), ("2", "unknown"), ("3", "unknown")]


@pytest_asyncio.fixture
async def defense(store):
    ledger = Ledger(store.path.parent / "messages.sqlite3")
    await ledger.open()
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "fixture",
            "enable": True,
            "_business_active": True,
            "_business_revision": "1",
            "enabled_groups": ["5"],
            "moderation": {"enabled": True, "permissions": {"5": ["mute", "recall"]}},
        },
        stopping=asyncio.Event(),
        connection_state="online",
        ledger=ledger,
        business=SimpleNamespace(request=AsyncMock()),
        diagnostics=SimpleNamespace(emit=Mock()),
        execute_action=AsyncMock(return_value={"status": "accepted"}),
    )
    service = SimpleNamespace(
        active=True,
        settings={"paused": False},
        capacity_paused=False,
        suspended_groups=set(),
        membership_receiving={},
        reconciling_groups={},
        tenants={
            ("1", "5"): {
                "account": "1",
                "group_id": "5",
                "version": 1,
                "active": True,
                "present": True,
                "paused": False,
                "native_role": "GROUP_ROLE_ADMIN",
                "rules": {"raid_defense_enabled": True},
            }
        },
        roster=AsyncMock(
            return_value={
                "2": {"nimId": "20", "groupRole": "GROUP_ROLE_MEMBER"},
                "3": {"nimId": "30", "groupRole": "GROUP_ROLE_MEMBER"},
            }
        ),
        adapters=lambda: [adapter],
    )
    runtime = RaidDefense(service)
    runtime.protected = AsyncMock(return_value=False)
    now = time.time() - 2
    store.configure("1", "5", True, now=now - 1)
    store.join("1", "5", "2", "20", "2", now, guard=lambda: None, now=now)
    await runtime.refresh()
    try:
        yield runtime, adapter, service
    finally:
        await runtime.close()
        await ledger.close()


def envelope(uid="2", *, at=None, kind="text"):
    """Build an authenticated synthetic envelope without storing media bytes."""
    return {
        "sender": uid,
        "kind": kind,
        "recall_route": {
            "peer": uid + "0",
            "time": str(int((at or time.time()) * 1000)),
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "image", "video"])
@pytest.mark.parametrize("span,trigger", [(3, True), (3.001, False)])
async def test_three_message_boundary_and_duplicate_ids(defense, kind, span, trigger):
    runtime, adapter, _ = defense
    now = time.time()
    for mid, offset in [("1", 0), ("1", 0), ("2", 1.5), ("3", span)]:
        result = runtime.observe(
            adapter, "5", mid, envelope(at=now + offset, kind=kind), now + offset
        )
    assert bool(result["category"]) is trigger
    assert len(result["messages"]) == (3 if trigger else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("span,trigger", [(10, True), (10.001, False)])
async def test_media_window_is_independent_of_rapid_window(defense, span, trigger):
    runtime, adapter, _ = defense
    now = time.time()
    for index, offset in enumerate([0, 2.5, 5, 7.5, span]):
        result = runtime.observe(
            adapter,
            "5",
            str(index),
            envelope(at=now + offset, kind="image"),
            now + offset,
        )
    assert bool(result["category"]) is trigger


@pytest.mark.asyncio
async def test_observation_expires_at_ten_minutes_and_first_seen_is_not_join(defense):
    runtime, adapter, _ = defense
    now = time.time()
    assert not runtime.observe(adapter, "5", "1", envelope("3"), now)
    until = runtime.members[("1", "5")]["2"]["observed_until"]
    for index in range(3):
        result = runtime.observe(adapter, "5", str(index), envelope(at=until), until)
    assert result == {}
    assert not runtime.observe(adapter, "6", "4", envelope(), now)


@pytest.mark.asyncio
async def test_ingress_requires_fresh_native_timestamps(defense):
    runtime, adapter, _ = defense
    now = time.time()
    assert not runtime.observe(adapter, "5", "1", envelope(at=now - 11), now)
    assert not runtime.observe(adapter, "5", "1", {"sender": "2", "text": "入群"}, now)
    adapter.stopping.set()
    assert not runtime.observe(adapter, "5", "1", envelope(), now)


@pytest.mark.asyncio
async def test_real_ledger_batch_recall_is_independent_of_mute_receipt(
    defense, monkeypatch
):
    runtime, adapter, _ = defense
    runtime.mute = AsyncMock(return_value="unknown")
    for mid in ("1", "2", "3"):
        await adapter.ledger.ingest(
            "1",
            "5",
            mid,
            envelope(kind="image"),
            observe=lambda payload, received_at, mid=mid: payload.update(
                _ingress_raid=runtime.observe(adapter, "5", mid, payload, received_at)
            ),
        )
    async with adapter.ledger.db.execute(
        "SELECT payload FROM inbox WHERE message='3'"
    ) as cursor:
        payload = json.loads((await cursor.fetchone())[0])
    await runtime.enforce(adapter, "5", "3", payload)
    runtime.mute.assert_awaited_once()
    assert adapter.execute_action.await_count == 3
    assert all(
        call.args[0].action == "recall"
        for call in adapter.execute_action.await_args_list
    )
    await runtime.enforce(adapter, "5", "3", payload)
    assert adapter.execute_action.await_count == 3
    snapshot = RaidStore("fixture").snapshot("1", "5")
    assert len(snapshot["recalls"]) == 3
    assert {row["status"] for row in snapshot["recalls"]} == {"accepted"}


@pytest.mark.asyncio
async def test_covered_bursts_deduplicate_cleanup_without_roster_reads(defense):
    runtime, adapter, service = defense
    store = RaidStore("fixture")
    intent = store.reserve(
        "1", "5", "2", kind="burst", trigger="previous", guard=lambda: None
    )
    store.finish(intent, "accepted")
    await runtime.reload(adapter, "5")
    work = []
    queue = SimpleNamespace(
        defer_recalls=lambda adapter, count, callback, **kwargs: (
            work.append((count, callback)) is None
        )
    )
    for number in range(1, 25):
        mid = str(number)
        await adapter.ledger.ingest(
            "1",
            "5",
            mid,
            envelope(kind="image"),
            observe=lambda payload, received_at, mid=mid: payload.update(
                _ingress_raid=runtime.observe(adapter, "5", mid, payload, received_at)
            ),
        )
        if number < 3:
            continue
        async with adapter.ledger.db.execute(
            "SELECT payload FROM inbox WHERE message=?", (mid,)
        ) as cursor:
            payload = json.loads((await cursor.fetchone())[0])
        await runtime.enforce(adapter, "5", mid, payload, recall_queue=queue)
    assert sum(count for count, _ in work) == 24
    assert len(work) == 22
    service.roster.assert_not_awaited()
    adapter.execute_action.assert_not_awaited()
    for _, callback in work:
        await callback()
    assert adapter.execute_action.await_count == 24
    assert {row["status"] for row in store.snapshot("1", "5")["recalls"]} == {
        "accepted"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [False, True])
async def test_slow_cleanup_never_holds_the_member_mute_lock(defense, full):
    runtime, adapter, _ = defense
    runtime.mute = AsyncMock(return_value="accepted")
    work = []
    queue = SimpleNamespace(
        defer_recalls=lambda adapter, count, callback, **kwargs: (
            False if full else work.append(callback) is None
        )
    )
    for mid in ("1", "2", "3", "4"):
        await adapter.ledger.ingest(
            "1",
            "5",
            mid,
            envelope(kind="image"),
            observe=lambda payload, received_at, mid=mid: payload.update(
                _ingress_raid=runtime.observe(adapter, "5", mid, payload, received_at)
            ),
        )
    async with adapter.ledger.db.execute(
        "SELECT message,payload FROM inbox WHERE message IN ('3','4')"
    ) as cursor:
        payloads = {
            mid: json.loads(encoded) for mid, encoded in await cursor.fetchall()
        }
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_recall(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"status": "accepted"}

    adapter.execute_action.side_effect = slow_recall
    first = asyncio.create_task(
        runtime.enforce(adapter, "5", "3", payloads["3"], recall_queue=queue)
    )
    cleanup = None
    second = None
    try:
        if not full:
            await asyncio.wait_for(first, 1)
            cleanup = asyncio.create_task(work[0]())
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(
            runtime.enforce(adapter, "5", "4", payloads["4"], recall_queue=queue)
        )
        for _ in range(100):
            if runtime.mute.await_count == 2:
                break
            await asyncio.sleep(0.005)
        assert runtime.mute.await_count == 2
        assert not release.is_set()
        release.set()
        await asyncio.gather(first, second)
        if cleanup:
            await cleanup
        if not full:
            for callback in work[1:]:
                await callback()
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (first, second, cleanup) if task),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_deferred_cleanup_rechecks_revocation_and_never_replays(defense):
    runtime, adapter, service = defense
    runtime.mute = AsyncMock(return_value="accepted")
    work = []
    queue = SimpleNamespace(
        defer_recalls=lambda adapter, count, callback, **kwargs: (
            work.append(callback) is None
        )
    )
    for mid in ("1", "2", "3"):
        await adapter.ledger.ingest(
            "1",
            "5",
            mid,
            envelope(),
            observe=lambda payload, received_at, mid=mid: payload.update(
                _ingress_raid=runtime.observe(adapter, "5", mid, payload, received_at)
            ),
        )
    async with adapter.ledger.db.execute(
        "SELECT payload FROM inbox WHERE message='3'"
    ) as cursor:
        payload = json.loads((await cursor.fetchone())[0])
    await runtime.enforce(adapter, "5", "3", payload, recall_queue=queue)
    service.tenants[("1", "5")]["paused"] = True
    await work[0]()
    adapter.execute_action.assert_not_awaited()
    assert {
        row["status"] for row in RaidStore("fixture").snapshot("1", "5")["recalls"]
    } == {"rejected"}


@pytest.mark.asyncio
async def test_shutdown_closes_only_unstarted_raid_cleanup(defense):
    from tests.test_wangshangliao_content_fastlane import runtime_for

    runtime, adapter, _ = defense
    runtime.mute = AsyncMock(return_value="accepted")
    queue = runtime_for(adapter)
    for mid in ("1", "2", "3"):
        await adapter.ledger.ingest(
            "1",
            "5",
            mid,
            envelope(),
            observe=lambda payload, received_at, mid=mid: payload.update(
                _ingress_raid=runtime.observe(adapter, "5", mid, payload, received_at)
            ),
        )
    async with adapter.ledger.db.execute(
        "SELECT payload FROM inbox WHERE message='3'"
    ) as cursor:
        payload = json.loads((await cursor.fetchone())[0])
    await runtime.enforce(adapter, "5", "3", payload, recall_queue=queue)
    assert queue.recall_counts[("fixture", "1")] == 3
    await queue.close()
    adapter.execute_action.assert_not_awaited()
    assert {
        row["status"] for row in RaidStore("fixture").snapshot("1", "5")["recalls"]
    } == {"rejected"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["admin", "identity", "incomplete"])
async def test_new_mute_requires_complete_current_identity(defense, change):
    runtime, adapter, _ = defense
    row = {"userId": "2", "nimId": "20", "groupRole": "GROUP_ROLE_MEMBER"}
    if change == "admin":
        row["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "identity":
        row["nimId"] = "21"
    adapter.get_moderation_members = AsyncMock(
        return_value={"complete": change != "incomplete", "groupMemberInfo": [row]}
    )
    assert (
        await runtime.mute(
            adapter, "5", "2", kind="burst", trigger="3", guard=lambda: None
        )
        == "rejected"
    )
    adapter.execute_action.assert_not_awaited()
    adapter.get_moderation_members.assert_awaited_once_with("5", max_age=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["paused", "disabled", "blacklist", "admin", "identity"]
)
async def test_enforcement_revoked_before_any_native_action(defense, case):
    runtime, adapter, service = defense
    runtime.mute = AsyncMock()
    now = time.time()
    payload = envelope()
    for mid in ("1", "2", "3"):
        decision = runtime.observe(adapter, "5", mid, payload, now)
    payload["_ingress_raid"] = decision
    if case == "paused":
        service.tenants[("1", "5")]["paused"] = True
    elif case == "disabled":
        service.tenants[("1", "5")]["rules"]["raid_defense_enabled"] = False
    elif case == "blacklist":
        runtime.protected.return_value = True
    elif case == "admin":
        service.roster.return_value["2"]["groupRole"] = "GROUP_ROLE_ADMIN"
    else:
        service.roster.return_value["2"]["nimId"] = "21"
    with pytest.raises(ProtocolError):
        await runtime.enforce(adapter, "5", "3", payload)
    runtime.mute.assert_not_awaited()
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_restores_recent_window_after_restart(defense):
    runtime, adapter, service = defense
    for mid in ("1", "2"):
        await adapter.ledger.ingest("1", "5", mid, envelope())
    restarted = RaidDefense(service)
    try:
        await restarted.refresh()
        result = restarted.observe(adapter, "5", "3", envelope(), time.time())
        assert result["category"] == "raid_flood"
        assert set(result["messages"]) == {"1", "2", "3"}
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_invented_prefix_and_missing_callback_cannot_authorize_mute(defense):
    _, adapter, _ = defense
    request = ActionRequest.bind(
        adapter, "raid/invented", "mute", 5, member=2, minutes=10
    )
    with pytest.raises(ProtocolError, match="raid_authorization_required"):
        await dispatch(adapter, request)
    with pytest.raises(ProtocolError, match="raid_intent_invalid"):
        await dispatch(adapter, request, authorize=lambda: None)


@pytest.mark.asyncio
async def test_existing_mute_is_never_shortened(defense, monkeypatch):
    runtime, adapter, _ = defense
    monkeypatch.setattr(raid_defense, "muted_member_ids", AsyncMock(return_value={"2"}))
    adapter.business.request.return_value = {
        "list": [
            {
                "groupId": "5",
                "applyId": "2",
                "applyTime": datetime.fromtimestamp(
                    time.time() - 2, timezone.utc
                ).isoformat(),
                "applicant": {"userId": "2", "nimId": "20"},
                "state": "MEMBER_STATE_GOOD",
                "groupApplyType": "INVITATION",
            }
        ]
    }
    result = await runtime.mute(
        adapter, "5", "2", kind="burst", trigger="3", guard=lambda: None
    )
    assert result == "skipped"
    adapter.execute_action.assert_not_awaited()
    assert RaidStore("fixture").snapshot("1", "5")["members"][0]["mute_until"] == 0


@pytest.mark.asyncio
async def test_lagging_membership_evidence_is_rechecked_without_guessing(defense):
    runtime, adapter, _ = defense
    now = time.time() - 0.1
    notice = {"event_at": int(now * 1000), "member_peers": ["30"]}
    await adapter.ledger.ingest("1", "5", "join", notice, state="invite_notification")
    runtime.mute = AsyncMock()
    adapter.business.request.return_value = {"list": []}
    await runtime.process_joins(adapter, "5")
    assert "3" not in runtime.members[("1", "5")]
    assert ("1", "5", "join") not in runtime.seen
    adapter.business.request.return_value = {
        "list": [
            {
                "groupId": "5",
                "applyId": "30",
                "applyTime": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "applicant": {"userId": "3", "nimId": "30"},
                "state": "MEMBER_STATE_GOOD",
                "groupApplyType": "APPLY",
            }
        ]
    }
    await runtime.process_joins(adapter, "5")
    assert runtime.members[("1", "5")]["3"]["join_id"] == "30"
    assert ("1", "5", "join") in runtime.seen
    assert runtime.mute.await_args.kwargs["kind"] == "hold"
    await runtime.process_joins(adapter, "5")
    runtime.mute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["application", "historical", "peer", "admin"])
async def test_unproven_admissions_never_start_observation(defense, case):
    runtime, adapter, service = defense
    now = time.time() - 0.1
    await adapter.ledger.ingest(
        "1",
        "5",
        "join",
        {"event_at": int(now * 1000), "member_peers": ["30"]},
        state="invite_notification",
    )
    runtime.mute = AsyncMock()
    if case == "historical":
        now -= 180
    elif case == "admin":
        service.roster.return_value["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
    adapter.business.request.return_value = {
        "list": [
            {
                "groupId": "5",
                "applyId": "30",
                "applyTime": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "applicant": {"userId": "3", "nimId": "31" if case == "peer" else "30"},
                "state": "MEMBER_STATE_APPLY"
                if case == "application"
                else "MEMBER_STATE_GOOD",
                "groupApplyType": "APPLY",
            }
        ]
    }
    await runtime.process_joins(adapter, "5")
    assert "3" not in runtime.members[("1", "5")]
    runtime.mute.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_and_automatic_exemptions_prevent_newcomer_actions(
    defense, monkeypatch
):
    runtime, adapter, _ = defense
    monkeypatch.setattr(raid_defense, "is_exempt", Mock(return_value=True))
    assert await RaidDefense.protected(runtime, adapter, "5", "2", "20")
    adapter.config["_open_managers"] = {"5": ["3"]}
    assert await RaidDefense.protected(runtime, adapter, "5", "3", "30")


@pytest.mark.asyncio
async def test_unknown_native_result_is_never_resubmitted(defense, monkeypatch):
    runtime, adapter, _ = defense
    monkeypatch.setattr(raid_defense, "muted_member_ids", AsyncMock(return_value=set()))
    adapter.business.request.return_value = {
        "list": [
            {
                "groupId": "5",
                "applyId": "2",
                "applyTime": datetime.fromtimestamp(
                    time.time() - 2, timezone.utc
                ).isoformat(),
                "applicant": {"userId": "2", "nimId": "20"},
                "state": "MEMBER_STATE_GOOD",
                "groupApplyType": "INVITATION",
            }
        ]
    }
    adapter.execute_action.return_value = {"status": "unknown"}
    assert (
        await runtime.mute(
            adapter, "5", "2", kind="burst", trigger="3", guard=lambda: None
        )
        == "unknown"
    )
    assert (
        await runtime.mute(
            adapter, "5", "2", kind="burst", trigger="4", guard=lambda: None
        )
        == "covered_or_unresolved"
    )
    adapter.execute_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_submission_rejection_is_not_reported_as_unknown(
    defense, monkeypatch
):
    runtime, adapter, _ = defense
    monkeypatch.setattr(raid_defense, "muted_member_ids", AsyncMock(return_value=set()))
    adapter.business.request.return_value = {
        "list": [
            {
                "groupId": "5",
                "applyId": "2",
                "applyTime": datetime.fromtimestamp(
                    time.time() - 2, timezone.utc
                ).isoformat(),
                "applicant": {"userId": "2", "nimId": "20"},
                "state": "MEMBER_STATE_GOOD",
                "groupApplyType": "INVITATION",
            }
        ]
    }
    error = ProtocolError("raid_policy_changed")
    error.submitted = False
    adapter.execute_action.side_effect = error
    result = await runtime.mute(
        adapter, "5", "2", kind="burst", trigger="3", guard=lambda: None
    )
    assert result == "rejected"
    assert (
        RaidStore("fixture").snapshot("1", "5")["outcomes"][0]["status"] == "rejected"
    )


@pytest.mark.asyncio
async def test_fixed_live_gate_rejects_an_owner_without_creating_a_permit(defense):
    from astrbot.builtin_stars.wangshangliao_moderation.test_raid import preflight

    _, adapter, service = defense
    adapter.account = "43506527"
    service.roster.return_value = {
        "31846829": {"nimId": "318", "groupRole": "GROUP_ROLE_OWNER"},
        "23691273": {"nimId": "236", "groupRole": "GROUP_ROLE_MEMBER"},
    }
    result = await preflight(service, "dashboard/operator")
    assert result["eligible"] is False
    assert result["permit_created"] is False
    assert result["live_acceptance_passed"] is False
    assert result["targets"][0]["role"] == "GROUP_ROLE_OWNER"
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_drains_native_submission_instead_of_cancelling_it(
    defense, monkeypatch
):
    runtime, adapter, _ = defense
    monkeypatch.setattr(raid_defense, "muted_member_ids", AsyncMock(return_value=set()))
    entered, release = asyncio.Event(), asyncio.Event()

    async def submit(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"status": "accepted"}

    adapter.execute_action.side_effect = submit
    worker = asyncio.create_task(
        runtime.mute(
            adapter,
            "5",
            "2",
            kind="burst",
            trigger="3",
            guard=lambda: None,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    assert not closing.done() and not worker.cancelled()
    release.set()
    assert await asyncio.wait_for(worker, 2) == "accepted"
    await asyncio.wait_for(closing, 2)
    assert (
        RaidStore("fixture").snapshot("1", "5")["outcomes"][0]["status"] == "accepted"
    )


@pytest.mark.asyncio
async def test_end_requires_current_personal_preview_and_does_not_unmute(defense):
    runtime, adapter, service = defense
    store = RaidStore("fixture")
    now = time.time()
    store.join("1", "5", "3", "30", "3", now - 0.1, guard=lambda: None, now=now)
    for uid in ("2", "3"):
        store.reserve("1", "5", uid, kind="burst", trigger=uid, guard=lambda: None)
    guard = Mock()
    rpc = SimpleNamespace(control_guard=Mock(return_value=guard))
    call = SimpleNamespace(
        actor="dashboard/operator",
        payload={
            "account": "1",
            "group_id": "5",
            "tenant_version": 1,
            "operation": "preview",
        },
    )
    preview = await runtime.control(rpc, call)
    assert store.snapshot("1", "5")["state"]["protection_until"] > time.time()
    call.payload.update(operation="commit", token=preview["token"])
    assert await runtime.control(rpc, call) == {"ended": True}
    assert store.snapshot("1", "5")["state"]["protection_until"] == 0
    adapter.execute_action.assert_not_awaited()
    with pytest.raises(OpenError):
        await runtime.control(rpc, call)
    assert guard.call_count >= 2
    assert service.tenants[("1", "5")]["rules"]["raid_defense_enabled"] is True


@pytest.mark.asyncio
async def test_join_workers_are_bounded_coalesced_and_fair_between_accounts(defense):
    runtime, adapter, service = defense
    second = SimpleNamespace(account="4", connection_state="online")
    service.adapters = lambda: [adapter, second]
    service.tenants = {
        (account, str(group)): {"account": account, "group_id": str(group)}
        for account in ("1", "4")
        for group in range(10, 14)
    }
    runtime.enabled = lambda *args: True
    runtime.process_joins = AsyncMock()
    runtime.tick()
    assert len(runtime.tasks) == 4
    runtime.tick()
    assert len(runtime.tasks) == 4
    await asyncio.gather(*runtime.tasks.values())
    runtime.tick()
    assert set(runtime.tasks) == {("4", str(group)) for group in range(10, 14)}
    await asyncio.gather(*runtime.tasks.values())
    assert runtime.process_joins.await_count == 8
