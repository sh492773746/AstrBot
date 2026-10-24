"""Offline regressions for the native transport audit."""

import asyncio
import json
import socket
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
import pytest_asyncio

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao import approval, moderation, wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    BusinessError,
)
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.test_window import (
    TestWindow as Window,
)


@pytest_asyncio.fixture(autouse=True)
async def offline_audit(monkeypatch, tmp_path):
    """Forbid network and redirect native audit storage to a temporary directory.

    Args:
        monkeypatch: Test-scoped patch restoration.
        tmp_path: Isolated test directory.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("audit_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", forbidden)
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(approval, "instance_dir", lambda _: tmp_path)
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")


def test_private_test_admission_requires_original_transport_peer():
    """Reject a claimed controlled UID arriving from another authenticated peer."""
    receiver = SimpleNamespace(
        account="1",
        nim_account="901",
        enabled_groups={"5"},
        stopping=asyncio.Event(),
        config={"id": "receiver", "_business_active": True, "_business_epoch": 1},
    )
    sender = SimpleNamespace(account="2", nim_account="902", stopping=asyncio.Event())
    window = Window(receiver, sender, [], ["private_ai"], [], 300, 1)
    receiver.test_window = window
    route = "private/2/" + wire.b64(b"999")
    scope = window.admit(
        route, "1", {"sender": "2", "transport_peer": "999", "text": "hello"}
    )
    assert scope == ""
    assert window.remaining == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["closed", "expired"])
async def test_group_test_reply_revocation_fences_submission(
    tmp_path, monkeypatch, revocation
):
    """Prevent a queued controlled-account reply after its window is revoked.

    Args:
        tmp_path: Isolated message ledger directory.
        monkeypatch: Test-scoped managed-account classifier.
        revocation: Explicit close or deadline expiry.
    """
    from astrbot.core.platform.sources.wangshangliao import event as native_event

    monkeypatch.setattr(native_event, "is_managed_account", lambda uid: uid == "2")
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    bot = SimpleNamespace(
        account="1",
        nim_account="901",
        enabled_groups={"5"},
        groups={"5": "55"},
        members={"5": {"1": "901", "2": "902"}},
        stopping=asyncio.Event(),
        config={
            "id": "audit-group-reply",
            "enable": True,
            "_business_active": True,
            "_business_epoch": 1,
            "_business_revision": "r1",
            "enabled_groups": ["5"],
            "reply_groups": {"5": True},
        },
        connection_state="online",
        send_lock=asyncio.Lock(),
        ledger=ledger,
        diagnostics=SimpleNamespace(emit=Mock()),
        business=SimpleNamespace(deployment=SimpleNamespace(key=lambda _: bytes(32))),
        nim=SimpleNamespace(
            request=AsyncMock(
                return_value=(200, wire.properties([(7, b"1000"), (12, b"1")]))
            )
        ),
        meta=lambda: PlatformMetadata("wangshangliao", "audit", id="audit-group-reply"),
    )
    for method in ("send_reply_text", "send_text"):
        setattr(bot, method, getattr(WangshangliaoAdapter, method).__get__(bot))
    sender = SimpleNamespace(account="2", nim_account="902", stopping=asyncio.Event())
    window = Window(bot, sender, ["5"], ["group_ai"], [], 300, 1)
    bot.test_window = window
    reply_admitted = asyncio.Event()
    original_reply = window.reply

    def reserve_reply(*args):
        allowed = original_reply(*args)
        if allowed:
            reply_admitted.set()
        return allowed

    monkeypatch.setattr(window, "reply", reserve_reply)
    assert (
        window.admit("5", "10", {"sender": "2", "text": "hello", "mentions": ["901"]})
        == "group_ai"
    )
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = "1"
    message.session_id = "1/5"
    message.group_id = "5"
    message.message_id = "10"
    message.sender = MessageMember("2", "Controlled")
    message.message_str = "hello"
    message.message = [Plain("hello")]
    message.raw_message = {"_transport_peer": "902"}
    event = WangshangliaoEvent(message, bot, True)
    event.set_extra("wsl_test_scope", "group_ai")
    task = None
    await bot.send_lock.acquire()
    try:
        task = asyncio.create_task(event.send(MessageChain([Plain("audit reply")])))
        await asyncio.wait_for(reply_admitted.wait(), 2)
        if revocation == "closed":
            bot.test_window = None
        else:
            window.expires = 0
        bot.send_lock.release()
        with pytest.raises(wire.ProtocolError, match="test_window_rejected"):
            await asyncio.wait_for(task, 2)
        bot.nim.request.assert_not_awaited()
    finally:
        if bot.send_lock.locked():
            bot.send_lock.release()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await ledger.close()


class SyntheticResponse:
    """Provide a fixed response without a real HTTP session."""

    def __init__(self, status, envelope):
        self.status = status
        self.headers = {}
        self.content = self
        self.envelope = envelope

    async def iter_chunked(self, size):
        yield json.dumps(self.envelope).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,error",
    [
        (403, 0, "http_rejected"),
        (401, 0, "reauth_required"),
        (429, 0, "rate_limited"),
        (200, 401, "reauth_required"),
        (200, 403, "business_rejected"),
        (200, 429, "rate_limited"),
        (200, 1402, "secondary_security_unverified"),
    ],
)
async def test_definite_provider_rejection_has_a_rejected_receipt(
    tmp_path, status, code, error
):
    """Preserve safe rejection evidence instead of quarantining denied mutations.

    Args:
        tmp_path: Isolated action ledger directory.
        status: Synthetic HTTP rejection status.
        code: Synthetic provider rejection code.
        error: Expected sanitized provider rejection.
    """
    post = Mock(return_value=SyntheticResponse(status, {"code": code, "data": {}}))
    deployment = SimpleNamespace(
        metadata=[1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        key=lambda _: bytes(32),
        headers={},
        origin="https://audit.invalid",
    )
    client = BusinessClient(deployment, SimpleNamespace(post=post))
    client.jwt = "synthetic-audit-session"
    client.fields[3] = 1
    native_request = client.request

    async def request(route, params, **kwargs):
        if route == "/v1/group/get-group-list":
            return {
                "owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}],
                "member": [],
            }
        return await native_request(route, params, **kwargs)

    client.request = request
    result = await moderation.execute(
        client, "audit", "1", "audit-denied", "mute_all", 5
    )
    assert post.call_count == 1
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        saved = json.loads(db.execute("SELECT result FROM operations").fetchone()[0])
    assert saved == result
    assert result["status"] == "rejected"
    assert result["submitted"] is True
    assert result["error"] == error
    assert result.get("provider_code") == (code or status)
    assert client.jwt == (
        "" if error == "reauth_required" else "synthetic-audit-session"
    )
    assert (
        await moderation.execute(client, "audit", "1", "audit-denied", "mute_all", 5)
        == result
    )
    assert post.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["timeout", "http_500", "http_503", "malformed", "business_504"]
)
async def test_uncertain_provider_outcome_stays_unknown_without_replay(
    tmp_path, outcome
):
    """Keep uncertain submission evidence durable without retrying.

    Args:
        tmp_path: Isolated action ledger directory.
        outcome: Ambiguous transport or provider outcome.
    """
    response = SyntheticResponse(200, {"code": 0, "data": {}})
    if outcome.startswith("http_"):
        response.status = int(outcome.removeprefix("http_"))
    elif outcome == "malformed":
        response.envelope = {"data": {}}
    elif outcome == "business_504":
        response.envelope = {"code": 504}
    post = Mock(
        side_effect=TimeoutError() if outcome == "timeout" else None,
        return_value=response,
    )
    deployment = SimpleNamespace(
        metadata=[1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        key=lambda _: bytes(32),
        headers={},
        origin="https://audit.invalid",
    )
    client = BusinessClient(deployment, SimpleNamespace(post=post))
    client.jwt = "synthetic-audit-session"
    client.fields[3] = 1
    native_request = client.request

    async def request(route, params, **kwargs):
        if route == "/v1/group/get-group-list":
            return {
                "owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}],
                "member": [],
            }
        return await native_request(route, params, **kwargs)

    client.request = request
    result = await moderation.execute(
        client, "audit", "1", "audit-uncertain", "mute_all", 5
    )
    assert result["status"] == "unknown"
    assert result["submitted"] is True
    assert "provider_code" not in result
    assert (
        await moderation.execute(client, "audit", "1", "audit-uncertain", "mute_all", 5)
        == result
    )
    assert post.call_count == 1
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        saved = json.loads(db.execute("SELECT result FROM operations").fetchone()[0])
    assert saved == result


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "timeout"])
async def test_readback_failure_preserves_accepted_mutation(tmp_path, outcome):
    """Keep an accepted receipt when only the subsequent readback fails.

    Args:
        tmp_path: Isolated action ledger directory.
        outcome: A definitive read rejection or uncertain read timeout.
    """
    failure = BusinessError("http_rejected", 403)
    failure.submitted = True
    failure.provider_rejected = True
    client = SimpleNamespace(
        fields=[0, 0, 0, 1],
        request=AsyncMock(
            side_effect=[
                {"owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
                {"noticeId": "7"},
                failure if outcome == "rejected" else TimeoutError(),
            ]
        ),
    )
    result = await moderation.execute(
        client, "audit", "1", "audit-readback", "announce", 5, text="Audit notice"
    )
    assert result["status"] == "accepted"
    assert result["submitted"] is True
    assert "provider_code" not in result
    assert (
        await moderation.execute(
            client, "audit", "1", "audit-readback", "announce", 5, text="Audit notice"
        )
        == result
    )
    assert client.request.await_count == 3
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        saved = json.loads(db.execute("SELECT result FROM operations").fetchone()[0])
    assert saved == result


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["LEAVE", "KICK_MEMBER", "KICK", "REMOVE"])
@pytest.mark.parametrize("pending_only", [False, True])
@pytest.mark.parametrize("state", ["MEMBER_STATE_KICKED", "MEMBER_STATE_GOOD"])
async def test_full_membership_history_retains_kicked_member_evidence(
    event_type, pending_only, state
):
    """Retain departures in history without treating them as pending invitations.

    Args:
        event_type: Raw upstream departure enum shared with finance consumers.
        pending_only: Whether only pending invitation records were requested.
        state: Departure state or a generic provider completion state.
    """
    record = {
        "applyId": "100",
        "groupId": 5,
        "groupApplyType": event_type,
        "state": state,
        "applyTime": "2026-10-01T12:00:00Z",
        "applicant": {"userId": 2, "nimId": 902},
        "inviter": None,
    }
    bot = SimpleNamespace(
        account="1",
        config={"enable": True, "enabled_groups": ["5"]},
        stopping=asyncio.Event(),
        connection_state="online",
        groups={"5": "55"},
        invitation_read_lock=asyncio.Lock(),
        invitation_read_at={},
        business=SimpleNamespace(
            fields=[0, 0, 0, 1],
            request=AsyncMock(return_value={"list": [record], "lastId": "100"}),
        ),
    )
    result = await WangshangliaoAdapter.get_group_invitation_records(
        bot, "5", pending_only=pending_only
    )
    assert [item["event_type"] for item in result["items"]] == (
        [] if pending_only else [event_type]
    )
    if not pending_only:
        assert result["items"][0]["state"] == state
        assert result["items"][0]["member_id"] == "2"
        assert result["items"][0]["apply_time"] == record["applyTime"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,number",
    [
        ("reauth_required", 401),
        ("account_conflict", 0),
        ("secondary_security_code_required", 1400),
        ("account_clearing", 1401),
        ("secondary_security_unverified", 1402),
        ("secondary_security_code_invalid", 1403),
        ("secondary_security_locked", 1404),
        ("secondary_security_phone_required", 1405),
        ("rate_limited", 429),
    ],
)
async def test_roster_account_errors_reach_connection_state_machine(error, number):
    """Propagate login/rate-limit state instead of silently finishing discovery.

    Args:
        error: Sanitized account-wide provider error.
        number: Corresponding provider response code.
    """
    failure = BusinessError(error, number, retry_after=60)
    bot = SimpleNamespace(
        enabled_groups={"5"},
        diagnostics=SimpleNamespace(emit=Mock()),
        business=SimpleNamespace(
            request=AsyncMock(
                return_value={
                    "owner": [{"groupId": 5, "groupCloudId": 55}],
                    "member": [],
                }
            )
        ),
        refresh_member_mapping=AsyncMock(side_effect=failure),
    )
    with pytest.raises(BusinessError, match=error) as caught:
        await WangshangliaoAdapter.load_groups(bot)
    assert caught.value is failure
    assert caught.value.retry_after == 60
    bot.refresh_member_mapping.assert_awaited_once_with("5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        BusinessError("business_rejected", 403),
        BusinessError("http_rejected", 403),
        wire.ProtocolError("member_identity"),
        BusinessError("transport"),
        TimeoutError(),
    ],
)
async def test_roster_local_failure_does_not_take_other_groups_offline(failure):
    """Keep discovering other groups when one group's roster is unavailable.

    Args:
        failure: Group-local rejection or transient member lookup failure.
    """
    bot = SimpleNamespace(
        enabled_groups={"5", "6"},
        diagnostics=SimpleNamespace(emit=Mock()),
        business=SimpleNamespace(
            request=AsyncMock(
                return_value={
                    "owner": [
                        {"groupId": 5, "groupCloudId": 55},
                        {"groupId": 6, "groupCloudId": 66},
                    ],
                    "member": [],
                }
            )
        ),
        refresh_member_mapping=AsyncMock(side_effect=[failure, {}]),
    )
    await WangshangliaoAdapter.load_groups(bot)
    assert bot.groups == {"5": "55", "6": "66"}
    assert bot.refresh_member_mapping.await_count == 2
    assert [call.args[0] for call in bot.refresh_member_mapping.await_args_list] == [
        "5",
        "6",
    ]
    bot.diagnostics.emit.assert_called_once_with(
        "directory",
        "unavailable",
        "5",
        failed=True,
        error="member_directory_unavailable",
    )


@pytest.mark.asyncio
async def test_dashboard_approval_contention_does_not_block_event_loop(
    tmp_path, monkeypatch
):
    """Keep a pending loop callback runnable while a temporary writer is held.

    Args:
        tmp_path: Isolated approval database directory.
    """
    from astrbot.core.platform.sources.wangshangliao import dashboard as native_page
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime
    from astrbot.core.platform.sources.wangshangliao.storage import Vault

    config = {
        "id": "audit",
        "type": "wangshangliao",
        "account_id": "1",
        "enable": True,
    }
    manager = SimpleNamespace(astrbot_config={"platform": [config]}, platform_insts=[])
    monkeypatch.setattr(runtime, "view", lambda value: value)
    monkeypatch.setattr(Vault, "load", lambda _: {"business": {"uid": 1}})
    from astrbot.core.platform.sources.wangshangliao import policy

    monkeypatch.setattr(policy, "authorize_action", lambda *args: None)
    path = tmp_path / "moderation.sqlite3"
    locked, release = threading.Event(), threading.Event()

    def hold_writer():
        with sqlite3.connect(path) as db:
            db.execute("BEGIN EXCLUSIVE")
            locked.set()
            release.wait(3)

    thread = threading.Thread(target=hold_writer)
    thread.start()
    timer = None
    try:
        assert await asyncio.to_thread(locked.wait, 2)
        timer = threading.Timer(0.25, release.set)
        timer.start()
        loop = asyncio.get_running_loop()
        callback = loop.create_future()
        loop.call_soon(callback.set_result, None)
        started = time.monotonic()
        pending = asyncio.create_task(
            native_page.handle_registration(
                manager,
                {
                    "action": "moderation_preview",
                    "instance_id": "audit",
                    "operation_action": "mute_all",
                    "group": 5,
                },
                "offline-owner",
            )
        )
        await callback
        assert time.monotonic() - started < 0.1
        result = await pending
        assert result["action"] == "mute_all"
    finally:
        release.set()
        if timer is not None:
            timer.cancel()
        await asyncio.to_thread(thread.join, 2)
