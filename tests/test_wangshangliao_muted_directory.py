"""Validate bounded native mute snapshots and business identity cross-checks."""

import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.directory import muted_member_ids
from astrbot.core.platform.sources.wangshangliao.nim import NimClient


def snapshot(rows, *, team=905):
    """Encode the Web SDK current mute-list response.

    Args:
        rows: Ordered native member properties.
        team: Transport team identity.

    Returns:
        One bounded SDK member-list response.
    """
    return (
        struct.pack("<Q", team)
        + wire.varint(len(rows))
        + b"".join(wire.properties(row) for row in rows)
    )


@pytest.mark.asyncio
async def test_native_query_reads_current_mutes_not_local_history():
    client = NimClient(None)
    rows = [
        [(3, b"902"), (9, b"1"), (13, b"1"), (10, b"1792000000000")],
    ]
    client.request = AsyncMock(return_value=(200, snapshot(rows)))
    assert await client.get_team_muted_members("905") == {"902"}
    client.request.assert_awaited_once_with(8, 27, struct.pack("<Q", 905))


@pytest.mark.asyncio
async def test_native_query_accepts_a_valid_empty_mute_list():
    client = NimClient(None)
    client.request = AsyncMock(return_value=(200, snapshot([])))
    assert await client.get_team_muted_members("905") == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"",
        snapshot([], team=906),
        snapshot([]) + b"x",
        snapshot([[(3, b"901")]]),
        snapshot([[(3, b"901"), (9, b"1"), (13, b"0")]]),
        snapshot([[(3, b"901"), (9, b"0"), (13, b"1")]]),
        snapshot([[(3, b"901"), (9, b"2"), (13, b"1")]]),
        snapshot([[(3, b"not-an-account"), (9, b"1"), (13, b"1")]]),
        snapshot([[(3, b"901"), (9, b"1"), (13, b"1"), (1, b"906")]]),
        snapshot([[(3, b"901"), (9, b"1"), (13, b"1")]] * 2),
        snapshot([[(3, b"901"), (10, b"1"), (11, b"1")]]),
        struct.pack("<Q", 905) + wire.varint(10001),
    ],
)
async def test_native_query_rejects_partial_or_ambiguous_data(body):
    client = NimClient(None)
    client.request = AsyncMock(return_value=(200, body))
    with pytest.raises(wire.ProtocolError):
        await client.get_team_muted_members("905")


@pytest.mark.asyncio
async def test_native_rejection_is_not_an_empty_list():
    client = NimClient(None)
    client.request = AsyncMock(return_value=(403, b""))
    with pytest.raises(wire.ProtocolError, match="mute_list_rejected"):
        await client.get_team_muted_members("905")


@pytest.mark.asyncio
async def test_mute_read_uses_authenticated_catalog_without_the_refresh_lock():
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        groups={},
        group_catalog={"5": {"team": "905"}},
        nim=SimpleNamespace(get_team_muted_members=AsyncMock(return_value={"902"})),
        business=SimpleNamespace(
            request=AsyncMock(
                return_value={
                    "groupMemberInfo": [
                        {"userId": "1", "nimId": "901"},
                        {"userId": "2", "nimId": "902"},
                    ]
                }
            )
        ),
        get_moderation_members=AsyncMock(side_effect=AssertionError("refresh lock")),
    )
    assert await muted_member_ids(adapter, "5") == {"2"}
    adapter.get_moderation_members.assert_not_awaited()
    adapter.nim.get_team_muted_members.assert_awaited_once_with("905")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["self_absent", "extra", "reconnect", "revoke", "mapping"]
)
async def test_business_mapping_and_connection_are_rechecked(change):
    nim = SimpleNamespace(get_team_muted_members=AsyncMock())
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        groups={"5": "905"},
        nim=nim,
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "1", "nimId": "901"},
                    {"userId": "2", "nimId": "902"},
                ],
            }
        ),
    )
    revoked = False
    adapter.business = SimpleNamespace(
        request=AsyncMock(return_value=adapter.get_moderation_members.return_value)
    )
    if change == "self_absent":
        adapter.nim_account = "999"

    def guard():
        if revoked:
            raise wire.ProtocolError("revoked")

    async def query(_):
        nonlocal revoked
        if change == "extra":
            return {"903"}
        if change == "reconnect":
            adapter.nim = object()
        if change == "revoke":
            revoked = True
        if change == "mapping":
            adapter.groups["5"] = "906"
        return {"902"}

    nim.get_team_muted_members.side_effect = query
    with pytest.raises(wire.ProtocolError):
        await muted_member_ids(adapter, "5", authorize=guard)
