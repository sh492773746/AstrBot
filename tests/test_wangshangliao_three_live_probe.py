"""Offline guards for native acceptance through three existing online accounts."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import test_probe
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


@pytest_asyncio.fixture
async def scope(tmp_path, monkeypatch):
    """Build exact native fixtures without sockets, credentials or live databases."""
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    sender = SimpleNamespace(
        account="23691273",
        nim_account="111",
        config={"id": "fixture", "_business_epoch": 4},
        connection_state="online",
        groups={"1459282": "555"},
        send_lock=asyncio.Lock(),
        ledger=ledger,
        business=SimpleNamespace(deployment=SimpleNamespace(key=lambda name: b"x")),
        nim=SimpleNamespace(
            request=AsyncMock(return_value=(200, wire.properties([(12, b"987654")])))
        ),
    )
    receiver = SimpleNamespace(
        account="43506527",
        nim_account="222",
        connection_state="online",
        groups={"1459282": "555"},
        business=object(),
    )
    active = [True]
    window = SimpleNamespace(
        sender=sender,
        receiver=receiver,
        groups=["1459282"],
        scopes=["group_rules"],
        keywords=["WSL_TEST_CANARY"],
        active=lambda: active[0],
        remaining=10,
        outbound_remaining=10,
        outbound_keys=set(),
    )
    receiver.test_window = window
    service = SimpleNamespace(
        integrated=True,
        active=True,
        settings={"paused": False},
        adapters=lambda: [sender, receiver],
    )
    members = [
        {"userId": "31846829", "nimId": "333", "groupRole": "GROUP_ROLE_OWNER"},
        {"userId": "43506527", "nimId": "222", "groupRole": "GROUP_ROLE_ADMIN"},
        {"userId": "23691273", "nimId": "111", "groupRole": "GROUP_ROLE_MEMBER"},
    ]
    directory = AsyncMock(return_value={"complete": True, "groupMemberInfo": members})
    monkeypatch.setattr(test_probe, "member_directory", directory)
    monkeypatch.setattr(test_probe.wire, "seal_message", lambda *args: "encoded")
    try:
        yield SimpleNamespace(
            service=service,
            sender=sender,
            receiver=receiver,
            window=window,
            active=active,
            members=members,
            directory=directory,
            payload={"text": "WSL_TEST_CANARY", "operation_id": "WSL_TEST_case"},
        )
    finally:
        await ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": "WSL_TEST_CANARY", "operation_id": "plain"},
        {"text": "", "operation_id": "WSL_TEST_case"},
        {"text": "中" * 1024, "operation_id": "WSL_TEST_case"},
        {
            "text": "WSL_TEST_CANARY",
            "operation_id": "WSL_TEST_case",
            "group": "1516387",
        },
        {
            "text": "WSL_TEST_CANARY",
            "operation_id": "WSL_TEST_case",
            "method": "delete",
        },
    ],
)
async def test_probe_rejects_generic_routes_and_invalid_payload(scope, payload):
    with pytest.raises(OpenError, match="test_probe_arguments"):
        await test_probe.send_probe(scope.service, "dashboard/test", payload)
    scope.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_uses_fresh_receiver_roster_without_enabling_sender_group(scope):
    scope.sender.groups.clear()
    result = await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    assert result["status"] == "accepted"
    assert scope.sender.groups == {}
    scope.directory.assert_awaited_once()
    scope.sender.nim.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_changed_native_team_after_roster_lookup_is_never_used(scope):
    scope.sender.groups.clear()

    async def changed(*args):
        scope.receiver.groups["1459282"] = "99999999"
        return {"complete": True, "groupMemberInfo": scope.members}

    scope.directory.side_effect = changed
    with pytest.raises(OpenError, match="test_probe_window"):
        await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    scope.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_replaced_window_reports_prior_receipt_as_historical(scope):
    first = await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    assert first["status"] == "accepted"
    scope.window.outbound_keys.clear()
    scope.sender._test_probe_sent = 0
    second = await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    assert second["status"] == "historical"
    assert second["prior_status"] == "accepted"
    scope.sender.nim.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_roster_is_rechecked_after_waiting_for_the_send_lock(scope):
    await scope.sender.send_lock.acquire()
    task = asyncio.create_task(
        test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    )
    await asyncio.sleep(0)
    scope.directory.assert_not_awaited()
    scope.members.append(
        {"userId": "999", "nimId": "999", "groupRole": "GROUP_ROLE_MEMBER"}
    )
    scope.sender.send_lock.release()
    with pytest.raises(OpenError, match="test_probe_roster"):
        await task
    scope.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_reuses_native_connection_and_does_not_repeat(scope):
    original = scope.sender.nim
    first = await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    scope.sender._test_probe_sent = 0
    second = await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    assert first["status"] == second["status"] == "accepted"
    assert first["message_id"] == second["message_id"] == "987654"
    assert scope.sender.nim is original
    assert scope.window.outbound_remaining == 9
    scope.sender.nim.request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["chat", "expired", "group", "text", "roster", "budget"]
)
async def test_probe_is_confined_to_dashboard_window_and_exact_roster(scope, fault):
    actor = "dashboard/test"
    if fault == "chat":
        actor = "nim/333"
    elif fault == "expired":
        scope.active[0] = False
    elif fault == "group":
        scope.window.groups = ["1516387"]
    elif fault == "text":
        scope.payload["text"] = "Arbitrary non-test message"
    elif fault == "roster":
        scope.members.append(
            {"userId": "999", "nimId": "444", "groupRole": "GROUP_ROLE_MEMBER"}
        )
    elif fault == "budget":
        scope.window.outbound_remaining = 0
    with pytest.raises(OpenError):
        await test_probe.send_probe(scope.service, actor, scope.payload)
    scope.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_unknown_result_is_persisted_without_replay(scope):
    scope.sender.nim.request.side_effect = TimeoutError("Unknown after submission")
    with pytest.raises(TimeoutError):
        await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    scope.sender._test_probe_sent = 0
    receipt = await test_probe.send_probe(
        scope.service, "dashboard/test", scope.payload
    )
    assert receipt["status"] == "unknown" and not receipt["message_id"]
    scope.sender.nim.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_rechecks_window_after_directory_wait(scope):
    async def changed(*args):
        scope.active[0] = False
        return {"complete": True, "groupMemberInfo": scope.members}

    scope.directory.side_effect = changed
    with pytest.raises(OpenError, match="test_probe_window"):
        await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    scope.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["stopped", "paused", "capacity", "suspended", "membership"],
)
async def test_probe_rechecks_service_stop_after_directory_wait(scope, change):
    """A fixed test send must stop when control revokes it during an HTTP wait."""

    async def changed(*args):
        if change == "stopped":
            scope.service.active = False
        elif change == "paused":
            scope.service.settings["paused"] = True
        elif change == "capacity":
            scope.service.capacity_paused = True
        elif change == "suspended":
            scope.service.suspended_groups = {("43506527", "1459282")}
        else:
            scope.service.membership_receiving = {("43506527", "1459282"): 1}
        return {"complete": True, "groupMemberInfo": scope.members}

    scope.directory.side_effect = changed
    with pytest.raises(OpenError, match="test_probe_window"):
        await test_probe.send_probe(scope.service, "dashboard/test", scope.payload)
    scope.sender.nim.request.assert_not_awaited()
    assert scope.window.outbound_remaining == 10
    assert await scope.sender.ledger.receipt("test-probe/WSL_TEST_case") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [None, 1, "", "dashboard/"])
async def test_probe_rejects_missing_dashboard_identity(scope, actor):
    """Invalid control identities must fail before directory or send operations."""
    with pytest.raises(OpenError, match="test_probe_arguments"):
        await test_probe.send_probe(scope.service, actor, scope.payload)
    scope.directory.assert_not_awaited()
    scope.sender.nim.request.assert_not_awaited()


def reply_window(scope):
    from astrbot.core.platform.sources.wangshangliao.test_window import (
        TestWindow as Window,
    )

    scope.sender.stopping = asyncio.Event()
    scope.receiver.stopping = asyncio.Event()
    scope.receiver.enabled_groups = ["1459282"]
    scope.receiver.config = {"id": "receiver", "_business_epoch": 1}
    scope.receiver.nim_account = "222"
    scope.receiver.members = {"1459282": {"23691273": "111"}}
    scope.service.active = True
    scope.service.settings = {"paused": False}
    scope.service.candidate_tasks = set()
    window = Window(
        scope.receiver,
        scope.sender,
        ["1459282"],
        ["group_commands"],
        [],
        60,
        10,
    )
    scope.receiver.test_window = window
    window._witness_since = int(time.time() * 1000)
    window._witness_id = "fixed-test-window"
    window._witness_operation = "WSL_TEST_native_probe"
    inner = test_probe._text_message(
        "receiver",
        "43506527",
        "1459282",
        "reply",
        "测试回复",
        None,
        False,
        window._witness_since,
    )
    outer = {0: "1", 1: "555", 2: "222", 8: "100", 12: "12345"}
    return window, inner, outer


@pytest.mark.asyncio
async def test_inactive_tenant_observes_native_reply_without_dispatch_or_enabling(
    scope,
):
    _, inner, outer = reply_window(scope)
    assert test_probe.observe_reply(
        scope.service, scope.sender, "1459282", outer, inner
    )
    await asyncio.gather(*scope.service.candidate_tasks)
    async with scope.sender.ledger.db.execute(
        "SELECT state,payload FROM inbox WHERE account=? AND team=? AND message=?",
        ("23691273", "1459282", "12345"),
    ) as cursor:
        row = await cursor.fetchone()
    assert row[0] == "ignored_bot"
    assert '"_test_witness": true' in row[1]
    assert "enabled_groups" not in scope.sender.config
    scope.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ("closed", "expired", "peer", "team", "member", "group", "budget")
)
async def test_native_witness_does_not_capture_outside_the_fixed_window(scope, change):
    window, inner, outer = reply_window(scope)
    group = "1459282"
    if change == "closed":
        scope.receiver.test_window = None
    elif change == "expired":
        window.expires = 0
    elif change == "peer":
        outer[2] = "999"
    elif change == "team":
        outer[1] = "999"
    elif change == "member":
        inner.sender.id = 31846829
    elif change == "group":
        group = "1516387"
    else:
        window._witness_messages = {str(n) for n in range(20)}
    assert not test_probe.observe_reply(
        scope.service, scope.sender, group, outer, inner
    )
    assert not scope.service.candidate_tasks
