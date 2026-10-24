"""Test-only group operations cannot reach production groups or friendship deletion."""

import copy
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.business_http import request_business
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from scripts import wsl_test_membership as scope


@pytest.fixture
def membership(tmp_path, monkeypatch):
    """Create signed-IO mocks and an isolated durable test action ledger.

    Args:
        tmp_path: Private test output directory.
        monkeypatch: Scoped dependency overrides.

    Returns:
        Owner and inviter workers, native roster, request recorder and fake clock.
    """
    clock = [1000.0]
    members = {
        uid: {
            "userId": uid,
            "nimId": "nim-" + uid,
            "groupRole": "GROUP_ROLE_OWNER"
            if uid == scope.OWNER
            else "GROUP_ROLE_MEMBER",
            "accountState": "ACCOUNT_STATE_GOOD",
        }
        for uid in (scope.OWNER, scope.PEER, scope.ORDINARY)
    }
    target = copy.deepcopy(members[scope.PEER])
    calls = []

    async def sleep(seconds):
        clock[0] += seconds

    async def exchange(client, route, params, *, authorize):
        authorize()
        calls.append((route, copy.deepcopy(params), clock[0]))
        if route == scope.REMOVE:
            members.pop(scope.PEER)
            return {}
        if route == scope.INVITE:
            members[scope.PEER] = copy.deepcopy(target)
            return {"members": [copy.deepcopy(target)]}
        return {"groupMemberInfo": list(copy.deepcopy(members).values())}

    monkeypatch.setattr(
        scope,
        "time",
        SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(
        scope, "asyncio", SimpleNamespace(Lock=scope.asyncio.Lock, sleep=sleep)
    )
    upstream = AsyncMock(side_effect=exchange)
    monkeypatch.setattr(scope, "request_business", upstream)
    monkeypatch.setattr(scope, "_exchange_business", upstream)
    owner = scope.MembershipTest(
        SimpleNamespace(fields=[0, 0, 0, int(scope.OWNER)]),
        scope.OWNER,
        target["nimId"],
        tmp_path,
    )
    inviter = scope.MembershipTest(
        SimpleNamespace(fields=[0, 0, 0, int(scope.ORDINARY)]),
        scope.ORDINARY,
        target["nimId"],
        tmp_path,
    )
    return owner, inviter, members, upstream, calls, clock


@pytest.mark.asyncio
async def test_realistic_single_target_kick_invite_are_paced_and_one_shot(membership):
    """Verify exact parameters, durable cooldown and no repeat writes.

    Args:
        membership: Isolated native scope.
    """
    owner, inviter, members, upstream, calls, _ = membership
    assert (await owner.change("kick"))["state"] == "verified"
    assert scope.PEER not in members
    assert (await inviter.change("invite"))["state"] == "verified"
    assert scope.PEER in members
    writes = [c for c in calls if c[0] in {scope.REMOVE, scope.INVITE}]
    assert [c[0] for c in writes] == [scope.REMOVE, scope.INVITE]
    assert writes[1][2] - writes[0][2] >= 30
    assert all(b[2] - a[2] >= 3 for a, b in zip(calls, calls[1:]))
    count = upstream.await_count
    for worker, action in ((owner, "kick"), (inviter, "invite")):
        with pytest.raises(ProtocolError, match="no_replay"):
            await worker.change(action)
    assert upstream.await_count == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,group,member",
    [
        ("kick", "1516387", scope.PEER),
        ("invite", "1488006", scope.PEER),
        ("kick", scope.GROUP, scope.OWNER),
        ("kick", scope.GROUP, scope.ORDINARY),
        ("kick", scope.GROUP, [scope.PEER]),
        ("delete_friend", scope.GROUP, scope.PEER),
        ("shell", scope.GROUP, scope.PEER),
        ("invite", "../production", scope.PEER),
    ],
)
async def test_out_of_scope_actions_never_send_native_requests(
    membership, action, group, member
):
    """Reject production groups, humans, owners, batches and arbitrary actions.

    Args:
        membership: Isolated native scope.
        action: Requested operation.
        group: Requested business group.
        member: Requested target.
    """
    owner, _, _, upstream, _, _ = membership
    with pytest.raises(ProtocolError, match="scope"):
        await owner.change(action, group=group, member=member)
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_cannot_kick_and_unreserved_invite_is_rejected(membership):
    """Prevent uncontrolled writes even through the test client's request method.

    Args:
        membership: Isolated native scope.
    """
    _, inviter, _, upstream, _, _ = membership
    with pytest.raises(ProtocolError):
        await inviter.change("kick")
    with pytest.raises(ProtocolError, match="unreserved"):
        await inviter.request(
            scope.INVITE,
            {"groupId": int(scope.GROUP), "groupMemberIds": [int(scope.PEER)]},
        )
    upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role", "nim", "extra_member", "account", "banned"])
async def test_live_native_scope_changes_stop_before_mutation(membership, change):
    """Recheck native identities and roles before removal.

    Args:
        membership: Isolated native scope.
        change: Stale or unauthorized scope condition.
    """
    owner, _, members, _, calls, _ = membership
    if change == "role":
        members[scope.PEER]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "nim":
        members[scope.PEER]["nimId"] = "another-account"
    elif change == "extra_member":
        members["9"] = {**members[scope.ORDINARY], "userId": "9", "nimId": "nim-9"}
    elif change == "account":
        owner.client.fields[3] = int(scope.ORDINARY)
    else:
        members[scope.PEER]["accountState"] = "ACCOUNT_STATE_BAN"
    with pytest.raises(ProtocolError):
        await owner.change("kick")
    assert not any(c[0] in {scope.REMOVE, scope.INVITE} for c in calls)


@pytest.mark.asyncio
async def test_unknown_write_survives_restart_without_replay(membership):
    """Retain an uncertain intent instead of repeating it after a restart.

    Args:
        membership: Isolated native scope.
    """
    owner, _, _, upstream, calls, _ = membership
    normal = upstream.side_effect

    async def exchange(client, route, params, *, authorize):
        if route == scope.REMOVE:
            authorize()
            raise TimeoutError()
        return await normal(client, route, params, authorize=authorize)

    upstream.side_effect = exchange
    with pytest.raises(TimeoutError):
        await owner.change("kick")
    restarted = scope.MembershipTest(
        owner.client, scope.OWNER, owner.target_nim, owner.path.parent
    )
    count = upstream.await_count
    with pytest.raises(ProtocolError, match="no_replay"):
        await restarted.change("kick")
    assert upstream.await_count == count
    with sqlite3.connect(owner.path) as db:
        assert db.execute("SELECT state FROM attempts").fetchall() == [("unknown",)]


@pytest.mark.asyncio
async def test_invitation_route_is_not_added_to_production_whitelist():
    """Keep the test-only invitation route inaccessible to normal business clients."""
    for route in (scope.INVITE, scope.ACCEPT):
        with pytest.raises(BusinessError, match="unsupported_route"):
            await request_business(SimpleNamespace(), route, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "",
        "group",
        "target",
        "nim",
        "inviter",
        "duplicate",
        "state",
        "time",
        "recipient",
        "timeout",
    ],
)
async def test_pending_invitation_requires_exact_signed_evidence_and_one_acceptance(
    membership, change
):
    """Handle real pending-invite semantics without replaying the invitation.

    Args:
        membership: Isolated actor, recipient and native roster.
        change: Forged or stale invitation evidence, or an uncertain acceptance.
    """
    _, inviter, members, upstream, calls, clock = membership
    target = members.pop(scope.PEER)
    clock[0] = 1031
    with sqlite3.connect(inviter.path) as db:
        db.execute(
            "INSERT INTO attempts VALUES('kick',?,'verified',1000)", (scope.OWNER,)
        )
        db.execute(
            "INSERT INTO attempts VALUES('invite',?,'unknown',1001)", (scope.ORDINARY,)
        )
    record = {
        "groupId": int(scope.GROUP),
        "applyId": "77",
        "groupApplyType": "INVITATION",
        "state": "MEMBER_STATE_INVITED",
        "applyTime": datetime.fromtimestamp(1001, timezone.utc).isoformat(),
        "applicant": {"userId": int(scope.PEER), "nimId": inviter.target_nim},
        "inviter": {"userId": int(scope.ORDINARY)},
    }
    recipient = SimpleNamespace(fields=[0, 0, 0, int(scope.PEER)])
    if change == "group":
        record["groupId"] = 1516387
    elif change == "target":
        record["applicant"]["userId"] = int(scope.OWNER)
    elif change == "nim":
        record["applicant"]["nimId"] = "other-user"
    elif change == "inviter":
        record["inviter"]["userId"] = int(scope.OWNER)
    elif change == "state":
        record["state"] = "MEMBER_STATE_REJECTED"
    elif change == "time":
        record["applyTime"] = datetime.fromtimestamp(99999, timezone.utc).isoformat()
    elif change == "recipient":
        recipient.fields[3] = int(scope.OWNER)
    normal = upstream.side_effect

    async def exchange(client, route, params, *, authorize):
        if route == "/v1/group/get-apply-logs":
            authorize()
            calls.append((route, copy.deepcopy(params), clock[0]))
            return {
                "list": [copy.deepcopy(record)] * (2 if change == "duplicate" else 1)
            }
        if route == scope.ACCEPT:
            authorize()
            calls.append((route, copy.deepcopy(params), clock[0]))
            assert str(client.fields[3]) == scope.PEER
            assert params == {"applyId": "77", "agree": True}
            if change == "timeout":
                raise TimeoutError()
            members[scope.PEER] = target
            record["state"] = "MEMBER_STATE_GOOD"
            return {}
        return await normal(client, route, params, authorize=authorize)

    upstream.side_effect = exchange
    if not change:
        assert (await inviter.finish_invitation(recipient))["state"] == "verified"
        count = len(calls)
        assert (await inviter.finish_invitation(recipient))["state"] == "verified"
        assert len(calls) == count
        assert all(b[2] - a[2] >= 3 for a, b in zip(calls, calls[1:]))
    elif change == "timeout":
        with pytest.raises(TimeoutError):
            await inviter.finish_invitation(recipient)
        with pytest.raises(ProtocolError, match="no_replay"):
            await inviter.finish_invitation(recipient)
    else:
        with pytest.raises(ProtocolError):
            await inviter.finish_invitation(recipient)
    assert sum(c[0] == scope.ACCEPT for c in calls) == int(change in {"", "timeout"})
    assert not any(c[0] in {scope.REMOVE, scope.INVITE} for c in calls)
