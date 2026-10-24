"""Offline friend approval evidence and revocation-safe welcome regressions."""

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import (
    friend_auto,
    onboarding_delivery,
    private_interaction,
)
from astrbot.core.platform.sources.wangshangliao import business_http, wire
from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_friend_auto import application
from tests.test_wangshangliao_friend_auto import approval as approval


@pytest_asyncio.fixture(autouse=True)
async def offline_only(monkeypatch):
    """Prevent synthetic tests from reaching a network transport.

    Args:
        monkeypatch: Test-scoped environment and socket overrides.
    """
    monkeypatch.setenv("ASTRBOT_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")

    def reject_connection(*args, **kwargs):
        raise AssertionError("friend_audit_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", reject_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_connection)


class OfflineResponse:
    """Supply response bytes to the real HTTP boundary without socket access."""

    def __init__(self, value, *, status=200):
        self.status = status
        self.headers = {}
        self.content = self
        self.body = json.dumps(value).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        yield self.body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "http_status,business_code,expected",
    [
        (200, 1001, "unknown"),
        (403, 0, "rejected"),
        (200, 401, "rejected"),
        (200, 429, "rejected"),
        (429, 0, "rejected"),
    ],
)
async def test_approval_outcome_uses_request_local_evidence(
    approval, monkeypatch, http_status, business_code, expected
):
    worker, adapter, _, _, _, clock, _ = approval
    pending = {"code": 0, "data": {"list": [application()]}}
    post = Mock(
        side_effect=[
            OfflineResponse(pending),
            OfflineResponse({"code": business_code}, status=http_status),
        ]
    )
    fields = [0] * 14
    fields[0], fields[3] = 1, 1
    adapter.business = SimpleNamespace(
        protocol_version="3.7.3",
        fields=fields,
        jwt="offline-jwt",
        mac=b"m" * 16,
        lock=asyncio.Lock(),
        last_request=0,
        deployment=SimpleNamespace(
            key=lambda name: b"k" * 32,
            headers={},
            origin="https://offline.invalid",
        ),
        http=SimpleNamespace(post=post),
    )
    monkeypatch.setattr(friend_auto, "request_business", business_http.request_business)

    await worker.step(adapter)
    with worker.database("fixture") as db:
        attempt = db.execute("SELECT state FROM approval_attempts").fetchone()[0]
    assert attempt == expected
    state = await worker.status(adapter)
    assert state["inflight"] == int(expected == "unknown")
    assert state["accepted"] == 0 and state["paused_reason"]
    if http_status == 429 or business_code == 429:
        assert state["retry_after"] == 30
    assert post.call_count == 2

    clock[0] += 600
    restored = friend_auto.FriendAutoApproval(worker.context)
    restored.active = True
    await restored.step(adapter)
    assert post.call_count == 2
    assert (await restored.status(adapter))["paused_reason"] == state["paused_reason"]
    if expected == "unknown":
        post.side_effect = None
        post.return_value = OfflineResponse(pending)
        with pytest.raises(ValueError, match="friend_approval_still_unknown"):
            await restored.reconcile(adapter, state["version"], lambda: None)
        assert post.call_count == 3
        assert (await restored.status(adapter))["inflight"] == 1
        assert all(
            call.args[0].endswith(friend_auto.LIST_ROUTE)
            for call in post.call_args_list[2:]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,submitted,rejected,expected",
    [
        ("approval", False, False, "rejected"),
        ("approval", True, False, "unknown"),
        ("approval", None, None, "unknown"),
        ("readback", False, False, "unknown"),
        ("readback", True, True, "unknown"),
    ],
)
async def test_approval_evidence_never_comes_from_a_later_read(
    approval, phase, submitted, rejected, expected
):
    worker, adapter, _, upstream, calls, clock, _ = approval
    failure = BusinessError("business_rejected", 1001)
    if submitted is not None:
        failure.submitted = submitted
        failure.provider_rejected = rejected
    original = upstream.side_effect

    async def request(client, route, params, *, authorize):
        failing = (
            route == friend_auto.APPROVE_ROUTE
            if phase == "approval"
            else route == friend_auto.LIST_ROUTE and len(calls) == 2
        )
        if failing:
            authorize()
            calls.append((route, params, clock[0]))
            raise failure
        return await original(client, route, params, authorize=authorize)

    upstream.side_effect = request
    await worker.step(adapter)
    with worker.database("fixture") as db:
        assert (
            db.execute("SELECT state FROM approval_attempts").fetchone()[0] == expected
        )
    state = await worker.status(adapter)
    assert state["paused_reason"] and state["accepted"] == 0
    assert state["inflight"] == int(expected == "unknown")
    count = upstream.await_count
    clock[0] += 600
    restored = friend_auto.FriendAutoApproval(worker.context)
    restored.active = True
    await restored.step(adapter)
    assert upstream.await_count == count
    assert sum(route == friend_auto.APPROVE_ROUTE for route, _, _ in calls) == 1


@pytest_asyncio.fixture
async def welcome(tmp_path, monkeypatch):
    """Build a native welcome transport using only temporary durable state.

    Args:
        tmp_path: Isolated SQLite storage root.
        monkeypatch: Test-scoped routing and managed-account overrides.

    Yields:
        Interaction worker, synthetic adapter and image delivery mocks.
    """
    monkeypatch.setattr(
        private_interaction, "instance_dir", lambda instance: tmp_path / instance
    )
    monkeypatch.setattr(private_interaction, "is_managed_account", lambda uid: False)
    ledger = Ledger(tmp_path / "native.sqlite3")
    await ledger.open()

    async def send(service, command, properties):
        parsed, _ = wire.read_properties(properties)
        return 200, wire.properties([(12, b"123"), (7, b"1000"), (11, parsed[11])])

    adapter = SimpleNamespace(
        config={
            "id": "fixture",
            "_business_active": True,
            "_business_epoch": 1,
            "enable": True,
            "reply_private": True,
        },
        account="1",
        nim_account="1001",
        nim=SimpleNamespace(request=AsyncMock(side_effect=send)),
        business=SimpleNamespace(
            deployment=SimpleNamespace(key=lambda name: b"a" * 32)
        ),
        ledger=ledger,
        send_lock=asyncio.Lock(),
        stopping=asyncio.Event(),
        connection_state="online",
    )
    images = SimpleNamespace(
        send_friend=AsyncMock(return_value="accepted"),
        send_group=AsyncMock(return_value="accepted"),
    )
    try:
        yield private_interaction.PrivateInteraction(images), adapter, images
    finally:
        await ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pacing", "reservation"])
async def test_private_welcome_rechecks_reply_policy_before_transport(
    welcome, monkeypatch, stage
):
    worker, adapter, images = welcome

    async def sleep(seconds):
        if stage == "pacing":
            adapter.config["reply_private"] = False
        await asyncio.sleep(0)

    monkeypatch.setattr(onboarding_delivery, "asyncio", SimpleNamespace(sleep=sleep))
    if stage == "reservation":
        original = adapter.ledger.reserve

        async def reserve(*args):
            result = await original(*args)
            adapter.config["reply_private"] = False
            return result

        monkeypatch.setattr(adapter.ledger, "reserve", reserve)

    outcome = await worker.welcome_friend(adapter, 2, "902", 17, lambda: None)
    assert outcome == "delivery_unconfirmed"
    adapter.nim.request.assert_not_awaited()
    images.send_friend.assert_not_awaited()
    if stage == "reservation":
        receipt = await adapter.ledger.receipt("friend-welcome/1/17/902")
        assert receipt["status"] == "rejected"

    adapter.config["reply_private"] = True
    restored = private_interaction.PrivateInteraction(images)
    assert (
        await restored.welcome_friend(adapter, 2, "902", 18, lambda: None)
        == "already_sent"
    )
    adapter.nim.request.assert_not_awaited()
    images.send_friend.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_welcome_is_independent_of_private_reply_policy(
    welcome, monkeypatch
):
    worker, adapter, images = welcome
    adapter.config["reply_private"] = False
    monkeypatch.setattr(
        onboarding_delivery, "asyncio", SimpleNamespace(sleep=AsyncMock())
    )
    assert (
        await worker.welcome_group(
            adapter, "5", "1005", 1, "Offline group menu", lambda: None
        )
        == "accepted"
    )
    adapter.nim.request.assert_awaited_once()
    images.send_group.assert_awaited_once()
    images.send_friend.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["text", "images"])
async def test_unknown_welcome_delivery_is_not_replayed(welcome, monkeypatch, phase):
    worker, adapter, images = welcome
    monkeypatch.setattr(
        onboarding_delivery, "asyncio", SimpleNamespace(sleep=AsyncMock())
    )
    if phase == "text":
        adapter.nim.request.side_effect = TimeoutError
    else:
        images.send_friend.return_value = "unknown"
    assert await worker.welcome_friend(adapter, 2, "902", 17, lambda: None) in {
        "delivery_unconfirmed",
        "unknown",
    }
    restored = private_interaction.PrivateInteraction(images)
    assert (
        await restored.welcome_friend(adapter, 2, "902", 18, lambda: None)
        == "already_sent"
    )
    adapter.nim.request.assert_awaited_once()
    assert images.send_friend.await_count == int(phase == "images")
    receipt = await adapter.ledger.receipt("friend-welcome/1/17/902")
    assert receipt["status"] == ("unknown" if phase == "text" else "accepted")
    assert await adapter.ledger.receipt("friend-welcome/1/18/902") is None


@pytest.mark.asyncio
async def test_separate_interactions_claim_welcome_before_delivery(
    welcome, monkeypatch
):
    worker, adapter, images = welcome
    monkeypatch.setattr(
        onboarding_delivery, "asyncio", SimpleNamespace(sleep=AsyncMock())
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter.nim.request.side_effect

    async def send(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    adapter.nim.request.side_effect = send
    task = asyncio.create_task(
        worker.welcome_friend(adapter, 2, "902", 17, lambda: None)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        other = private_interaction.PrivateInteraction(images)
        assert (
            await other.welcome_friend(adapter, 2, "902", 18, lambda: None)
            == "already_sent"
        )
    finally:
        release.set()
        result = await asyncio.wait_for(task, 2)
    assert result == "accepted"
    adapter.nim.request.assert_awaited_once()
    images.send_friend.assert_awaited_once()
