"""Delayed membership evidence must not silently finish blacklist admissions."""

import asyncio
import copy
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.blacklist_messages import contain
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_member_blacklist import native_join as native_join
from tests.test_wangshangliao_member_blacklist import scope as scope

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["event", "member", "state", "peer"])
async def test_incomplete_join_evidence_stays_pending_until_corroborated(
    native_join, missing
):
    value, event, rows, store, current = native_join
    adapter = event.platform
    original = copy.deepcopy(current)
    target = copy.deepcopy(rows["3"])
    if missing == "event":
        adapter.business.request.return_value = {"list": []}
    elif missing == "member":
        rows.pop("3")
    elif missing == "state":
        current["state"] = "MEMBER_STATE_WAIT"
    else:
        rows["3"]["nimId"] = "999"
    await value.member_blacklist.process(adapter)
    adapter.execute_action.assert_not_awaited()
    assert [row["message"] for row in store.pending("1")] == ["notice-101"]

    current.clear()
    current.update(original)
    rows["3"] = target
    adapter.business.request.return_value = {"list": [current]}
    await value.member_blacklist.process(adapter)
    adapter.execute_action.assert_awaited_once()
    assert not store.pending("1")


@pytest.mark.asyncio
async def test_later_notice_does_not_disappear_against_a_stale_completed_round(
    native_join,
):
    value, event, _, store, current = native_join
    adapter = event.platform
    await value.member_blacklist.process(adapter)
    assert adapter.execute_action.await_count == 1
    stamp = time.time()
    store.observe(
        "1",
        "5",
        "notice-102",
        ["903"],
        stamp,
        guard=lambda: None,
        scope=value.member_blacklist.notice_scope(adapter, value.tenants[("1", "5")]),
    )
    await value.member_blacklist.process(adapter)
    assert adapter.execute_action.await_count == 1
    assert [row["message"] for row in store.pending("1")] == ["notice-102"]

    current.update(
        applyId="102",
        applyTime=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
    )
    await value.member_blacklist.process(adapter)
    assert adapter.execute_action.await_count == 2
    assert not store.pending("1")


@pytest.mark.asyncio
async def test_unresolved_evidence_expires_to_review_not_processed(
    native_join, monkeypatch
):
    value, event, _, store, _ = native_join
    event.platform.business.request.return_value = {"list": []}
    stamp = time.time()
    monkeypatch.setattr(time, "time", lambda: stamp + 121)
    await value.member_blacklist.process(event.platform)
    event.platform.execute_action.assert_not_awaited()
    assert not store.pending("1")
    with store._transaction() as db:
        state = db.execute(
            "SELECT state FROM blacklist_notices WHERE message='notice-101'"
        ).fetchone()[0]
    assert state == "review_event_missing"


@pytest.mark.asyncio
async def test_pending_admission_survives_worker_recreation(native_join):
    value, event, _, store, current = native_join
    adapter = event.platform
    adapter.business.request.return_value = {"list": []}
    await value.member_blacklist.process(adapter)
    await value.member_blacklist.close()
    value.member_blacklist = type(value.member_blacklist)(value)
    adapter.business.request.return_value = {"list": [current]}
    await value.member_blacklist.process(adapter)
    adapter.execute_action.assert_awaited_once()
    assert not store.pending("1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("unknown", "review_action_unknown"),
        ("rejected", "review_action_rejected"),
        ("accepted", "review_action_unknown"),
    ],
)
async def test_missing_or_failed_native_receipt_is_never_reported_processed(
    native_join, status, state
):
    value, event, _, store, current = native_join
    event.platform.execute_action.return_value = {"status": status}
    await value.member_blacklist.process(event.platform)
    with store._transaction() as db:
        assert (
            db.execute(
                "SELECT state FROM blacklist_notices WHERE message='notice-101'"
            ).fetchone()[0]
            == state
        )
    stamp = datetime.fromisoformat(current["applyTime"]).timestamp()
    store.observe(
        "1",
        "5",
        "duplicate-notice",
        ["903"],
        stamp,
        guard=lambda: None,
        scope=value.member_blacklist.notice_scope(
            event.platform, value.tenants[("1", "5")]
        ),
    )
    await value.member_blacklist.process(event.platform)
    event.platform.execute_action.assert_awaited_once()
    with store._transaction() as db:
        assert (
            db.execute(
                "SELECT state FROM blacklist_notices WHERE message='duplicate-notice'"
            ).fetchone()[0]
            == state
        )


@pytest.mark.asyncio
async def test_rejoin_preflight_does_not_repeat_directory_and_log_requests(
    native_join,
):
    value, event, _, _, _ = native_join
    adapter = event.platform

    async def execute(request, *, authorize):
        await authorize()
        return {
            "status": "accepted",
            "submitted": True,
            "operation": request.operation,
            "action": "mute",
            "group": request.group,
            "member": request.member,
            "minutes": request.minutes,
            "expected_nim": request.expected_nim,
        }

    adapter.execute_action = AsyncMock(side_effect=execute)
    value.roster.reset_mock()
    await value.member_blacklist.process(adapter)
    assert value.roster.await_count == 2
    assert adapter.business.request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_pending_join_batch_coalesces_initial_platform_queries(
    native_join, unavailable
):
    value, event, _, store, _ = native_join
    adapter = event.platform
    adapter.business.request.return_value = {"list": []}
    if unavailable:
        from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

        adapter.business.request.side_effect = ProtocolError("transport")
    for index in range(19):
        store.observe(
            "1",
            "5",
            f"waiting-{index}",
            ["903"],
            time.time(),
            guard=lambda: None,
            scope=value.member_blacklist.notice_scope(
                adapter, value.tenants[("1", "5")]
            ),
        )
    value.roster.reset_mock()
    await value.member_blacklist.process(adapter)
    assert len(store.pending("1")) == 20
    assert value.roster.await_count == 1
    assert adapter.business.request.await_count == 1
    adapter.execute_action.assert_not_awaited()


@pytest.fixture
def blacklisted_message(native_join):
    """Bind an original ordinary member message to its isolated blacklist row."""
    value, event, rows, store, current = native_join
    adapter = event.platform
    adapter.diagnostics.emit = Mock()
    adapter.config["_business_active"] = True
    adapter.config["moderation"]["permissions"]["5"].append("recall")
    adapter.config["moderation"]["automation_enabled"] = True
    event.message_obj.sender.user_id = "3"
    event.actor_identity = ActorIdentity("1", "3", "903", "3")
    event.message_obj.message_id = "9001"
    event.message_str = "ordinary message"
    payload = dict(event.get_extra("wangshangliao_payload") or {})
    payload.update(
        sender="3",
        text=event.message_str,
        recall_route={
            "peer": "903",
            "client": "client-9001",
            "time": str(int(time.time() * 1000)),
        },
        _open_revision=group_revision(adapter.config, "5"),
    )
    event.set_extra("wangshangliao_payload", payload)
    value.member_blacklist.tick = Mock()
    return value, event, rows, store, current


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "image", "video", "forward"])
async def test_blacklist_messages_are_claimed_before_ocr_and_ai(
    blacklisted_message, kind
):
    value, event, _, _, _ = blacklisted_message
    payload = event.get_extra("wangshangliao_payload")
    if kind != "text":
        payload["kind"] = kind
        payload["text"] = ""
    value.plugin.media_moderation = Mock()
    value.rule_replies.observe = AsyncMock()
    value.group_ai.observe = AsyncMock()
    await value.on_event(event)
    request = event.platform.execute_action.await_args.args[0]
    assert request.action == "recall"
    assert request.expected_nim == "903"
    assert request.message == "9001"
    assert request.blacklist_intent is None
    value.rule_replies.observe.assert_not_awaited()
    value.group_ai.observe.assert_not_awaited()
    assert event.get_extra("_component_claimed") is True
    value.member_blacklist.tick.assert_called_once_with(urgent=("1", "5"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["removed", "paused", "demoted_creator", "promoted", "peer"]
)
async def test_blacklist_message_recall_rechecks_authority(blacklisted_message, change):
    value, event, rows, store, _ = blacklisted_message
    adapter = event.platform
    writes = []

    async def execute(request, *, authorize):
        if change == "removed":
            store.set_member(
                "1", "5", "3", "903", "Target", False, "dashboard/test", lambda: None
            )
        elif change == "paused":
            value.settings["paused"] = True
        elif change == "demoted_creator":
            value.member_evidence[("1", "5")]["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
        elif change == "promoted":
            value.member_evidence[("1", "5")]["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
        else:
            value.member_evidence[("1", "5")]["3"]["nimId"] = "999"
        await authorize()
        writes.append(request)
        return {"status": "accepted"}

    adapter.execute_action.side_effect = execute
    assert await contain(value, event)
    assert writes == []
    assert adapter.diagnostics.emit.call_args.args[:2] == (
        "blacklist_message",
        "needs_review",
    )


@pytest.mark.asyncio
async def test_native_directory_refresh_can_revoke_the_blacklist_creator(
    blacklisted_message,
):
    value, event, rows, _, _ = blacklisted_message
    writes = []

    async def execute(request, *, authorize):
        changed = copy.deepcopy(rows)
        changed["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
        event.platform.member_snapshots = {
            "5": (
                "1",
                time.monotonic(),
                {"complete": True, "groupMemberInfo": list(changed.values())},
            )
        }
        await authorize()
        writes.append(request)
        return {"status": "accepted"}

    event.platform.execute_action.side_effect = execute
    assert await contain(value, event)
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["peer", "account", "audit", "group", "revision"])
async def test_spoofed_or_incomplete_blacklist_message_never_executes(
    blacklisted_message, change
):
    value, event, _, _, _ = blacklisted_message
    payload = event.get_extra("wangshangliao_payload")
    if change == "peer":
        payload["recall_route"]["peer"] = "999"
    elif change == "account":
        event.actor_identity = ActorIdentity("9", "3", "903", "3")
    elif change == "audit":
        payload["_audit_incomplete"] = True
    elif change == "group":
        event.message_obj.group_id = "6"
    else:
        payload["_open_revision"] = "stale"
    value.roster.reset_mock()
    assert not await contain(value, event)
    value.roster.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_unlisted_1000_message_lookup_has_no_network_work(
    blacklisted_message,
):
    value, event, _, store, _ = blacklisted_message
    store.set_member(
        "1", "5", "3", "903", "Target", False, "dashboard/test", lambda: None
    )
    value.roster.reset_mock()
    event.platform.business.request.reset_mock()
    samples = []
    for _ in range(1000):
        started = time.perf_counter()
        assert not await contain(value, event)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    print(
        f"\nBlacklist lookup: samples=1000 p95={samples[949]:.3f}ms "
        f"p99={samples[989]:.3f}ms max={samples[-1]:.3f}ms"
    )
    value.roster.assert_not_awaited()
    event.platform.business.request.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_unlisted_message_does_not_query_membership_or_execute(
    blacklisted_message,
):
    value, event, _, store, _ = blacklisted_message
    store.set_member(
        "1", "5", "3", "903", "Target", False, "dashboard/test", lambda: None
    )
    value.roster.reset_mock()
    event.platform.business.request.reset_mock()
    assert not await contain(value, event)
    value.roster.assert_not_awaited()
    event.platform.business.request.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_blacklist_recall_waits_for_existing_mute_without_cancelling_it(
    blacklisted_message,
):
    value, event, _, _, _ = blacklisted_message
    release, entered = asyncio.Event(), asyncio.Event()
    order = []

    async def mute():
        entered.set()
        await release.wait()
        order.append("mute")

    task = asyncio.create_task(mute())
    value.member_blacklist.tasks[("1", "5")] = task

    async def execute(request, *, authorize):
        await authorize()
        order.append("recall")
        return {"status": "accepted"}

    event.platform.execute_action.side_effect = execute
    handling = asyncio.create_task(contain(value, event))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.02)
        assert not handling.done()
        assert not task.cancelled()
        event.platform.execute_action.assert_not_awaited()
        release.set()
        assert await asyncio.wait_for(handling, 2)
        assert order == ["mute", "recall"]
    finally:
        release.set()
        await asyncio.gather(task, handling, return_exceptions=True)
