"""Manual group invitation acceptance remains exact, durable and independent of login."""

import copy
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from scripts import wsl_group_invitation as scope


@pytest.fixture
def invitation():
    return {
        "applyId": "123",
        "groupId": 456,
        "groupAccount": "2633333",
        "groupCloudId": "789",
        "groupName": "Neutral test group",
        "groupApplyType": "INVITATION",
        "state": "MEMBER_STATE_INVITED",
        "applyTime": datetime.now(timezone.utc).isoformat(),
        "applicant": {"userId": int(scope.ACCOUNT), "nimId": 999},
    }


def test_exact_group_recipient_and_latest_event(invitation):
    row = scope.select_invitation({"list": [invitation]}, "2633333", "999")
    assert row == invitation
    for field, value in [
        ("groupAccount", "456"),
        ("groupApplyType", "APPLY"),
        ("state", "MEMBER_STATE_REJECT"),
    ]:
        changed = {**invitation, field: value}
        with pytest.raises(ProtocolError):
            scope.select_invitation({"list": [changed]}, "2633333", "999")
    with pytest.raises(ProtocolError):
        scope.select_invitation({"list": [invitation]}, "2633333", "1000")
    duplicate = {**invitation, "applyId": "124"}
    with pytest.raises(ProtocolError):
        scope.select_invitation({"list": [invitation, duplicate]}, "2633333", "999")


def test_business_id_does_not_substitute_for_display_number(invitation):
    with pytest.raises(ProtocolError):
        scope.select_invitation({"list": [invitation]}, "456", "999")
    another = {**invitation, "groupId": 457, "applyId": "125"}
    with pytest.raises(ProtocolError):
        scope.select_invitation({"list": [invitation, another]}, "2633333", "999")


@pytest.mark.asyncio
async def test_accept_once_then_verify_without_login(invitation, tmp_path, monkeypatch):
    joined = []
    calls = []
    clock = [100.0]

    async def sleep(seconds):
        clock[0] += seconds

    async def request(route, params, *, authorize):
        authorize()
        calls.append((route, copy.deepcopy(params), clock[0]))
        if route.endswith("get-group-list"):
            return {"owner": [], "member": copy.deepcopy(joined)}
        return {"list": [copy.deepcopy(invitation)]}

    async def exchange(client, route, params, *, authorize, submission):
        authorize()
        submission["started"] = True
        calls.append((route, copy.deepcopy(params), clock[0]))
        joined.append(copy.deepcopy(invitation))
        return {}

    monkeypatch.setattr(scope.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(scope.asyncio, "sleep", sleep)
    upstream = AsyncMock(side_effect=exchange)
    monkeypatch.setattr(scope, "_exchange_business", upstream)
    client = SimpleNamespace(
        fields=[0, 0, 0, int(scope.ACCOUNT)], request=AsyncMock(side_effect=request)
    )
    worker = scope.ManualInvitation(client, "999", tmp_path)
    assert (await worker.accept("2633333", "123"))["state"] == "verified"
    assert (await worker.accept("2633333", "123"))["state"] == "already_joined"
    upstream.assert_awaited_once()
    assert calls[2][:2] == (scope.ACCEPT, {"applyId": "123", "agree": True})
    assert all(b[2] - a[2] >= 3 for a, b in zip(calls, calls[1:]))
    with sqlite3.connect(tmp_path / "attempts.sqlite3") as db:
        assert db.execute("SELECT state FROM attempts").fetchone() == ("verified",)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_unknown_and_rejected_writes_never_replay(
    invitation, tmp_path, monkeypatch, rejected
):
    client = SimpleNamespace(fields=[0, 0, 0, int(scope.ACCOUNT)])
    worker = scope.ManualInvitation(client, "999", tmp_path)
    worker.inspect = AsyncMock(return_value=(None, invitation))
    worker.last_read = -100

    async def fail(client, route, params, *, authorize, submission):
        authorize()
        submission.update(started=True, rejected=rejected)
        raise BusinessError("rate_limited" if rejected else "transport_timeout")

    upstream = AsyncMock(side_effect=fail)
    monkeypatch.setattr(scope, "_exchange_business", upstream)
    with pytest.raises(BusinessError):
        await worker.accept("2633333", "123")
    restarted = scope.ManualInvitation(client, "999", tmp_path)
    restarted.inspect = AsyncMock(return_value=(None, invitation))
    with pytest.raises(ProtocolError, match="no_replay"):
        await restarted.accept("2633333", "123")
    upstream.assert_awaited_once()
    with sqlite3.connect(tmp_path / "attempts.sqlite3") as db:
        assert db.execute("SELECT state FROM attempts").fetchone() == (
            "rejected" if rejected else "unknown",
        )


@pytest.mark.asyncio
async def test_wrong_account_or_confirmation_never_mutates(
    invitation, tmp_path, monkeypatch
):
    upstream = AsyncMock()
    monkeypatch.setattr(scope, "_exchange_business", upstream)
    client = SimpleNamespace(fields=[0, 0, 0, 123], request=AsyncMock())
    worker = scope.ManualInvitation(client, "999", tmp_path)
    with pytest.raises(ProtocolError, match="account_binding"):
        await worker.accept("2633333", "123")
    client.request.assert_not_awaited()
    worker.inspect = AsyncMock(return_value=(None, invitation))
    with pytest.raises(ProtocolError, match="invitation_changed"):
        await worker.accept("2633333", "124")
    upstream.assert_not_awaited()
    assert not (tmp_path / "attempts.sqlite3").exists()
