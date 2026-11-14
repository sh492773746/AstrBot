import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import moderation
from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.asyncio
async def test_revocation_while_reserving_transport_operation_stops_send(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    with closing(moderation.operation_database("bot")):
        pass
    blocker = sqlite3.connect(tmp_path / "moderation.sqlite3")
    entered = asyncio.Event()
    authorized = True
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if not authorized:
            raise ProtocolError("revoked")
        if checks == 1:
            blocker.execute("BEGIN IMMEDIATE")
            entered.set()

    client = AsyncMock()
    client.fields = [0, 0, 0, "1"]
    client.request.return_value = {
        "owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}]
    }
    work = asyncio.create_task(
        moderation.execute(
            client, "bot", "1", "contended", "mute_all", 5, authorize=guard
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0.03)
        assert not work.done()
        authorized = False
        blocker.rollback()
        with pytest.raises(ProtocolError, match="revoked"):
            await asyncio.wait_for(work, 3)
        client.request.assert_awaited_once_with("/v1/group/get-group-list", {"v": "0"})
        result = blocker.execute(
            "SELECT result FROM operations WHERE id='contended'"
        ).fetchone()
        assert result is not None
        evidence = json.loads(result[0])
        assert evidence["status"] == "rejected"
        assert evidence["submitted"] is False
    finally:
        blocker.rollback()
        blocker.close()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_revocation_after_remote_lookup_prevents_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    client.request.return_value = {
        "owner": [{"groupId": 1, "me": {"role": "GROUP_ROLE_ADMIN"}}]
    }

    def revoked():
        raise ProtocolError("moderation_permission")

    with pytest.raises(ProtocolError, match="moderation_permission"):
        await moderation.execute(
            client, "bot", "12", "revoked", "mute_all", 1, authorize=revoked
        )
    assert client.request.await_count == 1


@pytest.mark.asyncio
async def test_moderation_dedup_and_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    client.request.side_effect = [
        {"owner": [{"groupId": 1, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
        {
            "groupMemberInfo": [
                {"userId": 2, "nimId": 22, "groupRole": "GROUP_ROLE_MEMBER"}
            ]
        },
        {},
    ]
    result = await moderation.execute(client, "bot", "12", "op", "mute", 1, 2)
    assert result["status"] == "accepted"
    assert await moderation.execute(client, "bot", "12", "op", "mute", 1, 2) == result
    assert client.request.call_count == 3
    with pytest.raises(ProtocolError, match="conflict"):
        await moderation.execute(client, "bot", "12", "op", "unmute", 1, 2)


@pytest.mark.asyncio
async def test_moderation_rejects_nonadmin(tmp_path, monkeypatch):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    client.request.return_value = {
        "member": [{"groupId": 1, "me": {"role": "GROUP_ROLE_MEMBER"}}]
    }
    with pytest.raises(ProtocolError, match="permission"):
        await moderation.execute(client, "bot", "12", "op", "mute_all", 1)
    assert client.request.call_count == 1


@pytest.mark.asyncio
async def test_moderation_unknown_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    client.request.side_effect = [
        {"owner": [{"groupId": 1, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
        TimeoutError(),
    ]
    result = await moderation.execute(client, "bot", "12", "op", "mute_all", 1)
    assert result["status"] == "unknown"
    assert await moderation.execute(client, "bot", "12", "op", "mute_all", 1) == result
    assert client.request.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,reason,kind,code",
    [
        (TimeoutError("secret payload"), "transport_timeout", "TimeoutError", None),
        (
            BusinessError("business_rejected", -1),
            "business_rejected",
            "BusinessError",
            -1,
        ),
        (BusinessError("response_shape"), "response_shape", "BusinessError", None),
        (RuntimeError("jwt=secret\nraw body"), "operation_failed", "Exception", None),
    ],
)
async def test_unknown_retains_safe_cause_without_replay(
    tmp_path, monkeypatch, failure, reason, kind, code
):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    client.request.side_effect = [
        {"owner": [{"groupId": 1, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
        failure,
    ]
    result = await moderation.execute(client, "bot", "12", "cause", "mute_all", 1)
    assert result["status"] == "unknown" and result["submitted"] is True
    assert result["error"] == reason and result["error_type"] == kind
    assert result.get("provider_code") == code
    assert result["error_phase"] == "submission"
    assert result["finished_at"] >= result["started_at"]
    assert "secret" not in json.dumps(result) and "raw body" not in json.dumps(result)
    with closing(moderation.operation_database("bot")) as db:
        assert (
            json.loads(
                db.execute("SELECT result FROM operations WHERE id='cause'").fetchone()[
                    0
                ]
            )
            == result
        )
    assert (
        await moderation.execute(client, "bot", "12", "cause", "mute_all", 1) == result
    )
    assert client.request.await_count == 2


@pytest.mark.asyncio
async def test_pre_submission_failure_is_saved_with_cause(tmp_path, monkeypatch):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    failure = ProtocolError("blacklist_target_changed")
    failure.submitted = False
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    client.request.side_effect = [
        {"owner": [{"groupId": 1, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
        failure,
    ]
    with pytest.raises(ProtocolError, match="blacklist_target_changed"):
        await moderation.execute(client, "bot", "12", "departed", "mute_all", 1)
    with closing(moderation.operation_database("bot")) as db:
        result = json.loads(
            db.execute("SELECT result FROM operations WHERE id='departed'").fetchone()[
                0
            ]
        )
    assert result["status"] == "rejected" and result["submitted"] is False
    assert result["error"] == "blacklist_target_changed"


@pytest.mark.asyncio
async def test_complete_directory_absence_is_not_reported_as_bad_command(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    client.request.side_effect = [
        {"owner": [{"groupId": 1, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
        {"groupMemberInfo": []},
    ]
    with pytest.raises(ProtocolError, match="moderation_member_absent"):
        await moderation.execute(client, "bot", "12", "absent", "mute", 1, 2)
    assert client.request.await_count == 2
    with closing(moderation.operation_database("bot")) as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [1, 3, 30, 1440])
async def test_mute_minutes_reach_native_request_and_idempotency(
    tmp_path, monkeypatch, minutes
):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    client.request.side_effect = [
        {"owner": [{"groupId": 1, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
        {
            "groupMemberInfo": [
                {"userId": 2, "nimId": 22, "groupRole": "GROUP_ROLE_MEMBER"}
            ]
        },
        {},
    ]
    result = await moderation.execute(
        client, "bot", "12", "op", "mute", 1, 2, minutes=minutes
    )
    assert result["status"] == "accepted"
    assert result["minutes"] == minutes
    client.request.assert_awaited_with(
        "/v1/group/set-member-mute",
        {"groupId": 1, "userId": 2, "min": minutes},
        authorize=None,
    )
    assert (
        await moderation.execute(
            client, "bot", "12", "op", "mute", 1, 2, minutes=minutes
        )
        == result
    )
    with pytest.raises(ProtocolError, match="conflict"):
        await moderation.execute(
            client,
            "bot",
            "12",
            "op",
            "mute",
            1,
            2,
            minutes=2 if minutes == 1 else 1,
        )
    assert client.request.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, -1, 1441, 4320, True, 1.5, "3", None])
async def test_invalid_mute_minutes_fail_before_network(tmp_path, monkeypatch, minutes):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    with pytest.raises(ProtocolError, match="moderation_minutes"):
        await moderation.execute(
            client, "bot", "12", "op", "mute", 1, 2, minutes=minutes
        )
    client.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["callback", "marker", "prefix", "identity", "kick", "duration", "type"]
)
async def test_native_delegation_cannot_be_inferred_from_prefix(
    tmp_path, monkeypatch, change
):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, "12"]
    options = {
        "minutes": 4320,
        "authorize": AsyncMock(),
        "expected_nim": "22",
        "delegated_mute": True,
    }
    operation, action = "delegate-mute/3/test", "mute"
    if change == "callback":
        options["authorize"] = None
    elif change == "marker":
        options.update(delegated_mute=False, minutes=128)
    elif change == "prefix":
        operation = "ordinary"
    elif change == "identity":
        options["expected_nim"] = None
    elif change == "kick":
        action = "kick"
    elif change == "type":
        options["delegated_mute"] = 1
    else:
        options["minutes"] = 4321
    with pytest.raises(ProtocolError, match="delegated_mute_authority_invalid"):
        await moderation.execute(
            client, "bot", "12", operation, action, 1, 2, **options
        )
    client.request.assert_not_awaited()
