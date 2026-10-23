import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import moderation
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
@pytest.mark.parametrize("minutes", [0, -1, 1441, True, 1.5, "3", None])
async def test_invalid_mute_minutes_fail_before_network(tmp_path, monkeypatch, minutes):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, 12]
    with pytest.raises(ProtocolError, match="moderation_minutes"):
        await moderation.execute(
            client, "bot", "12", "op", "mute", 1, 2, minutes=minutes
        )
    client.request.assert_not_awaited()
