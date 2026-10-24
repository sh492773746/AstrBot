"""Friend deletion is absent; retained directory codecs are read-only."""

import importlib.util
import struct
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.dashboard import DashboardPage
from astrbot.builtin_stars.wangshangliao_moderation.settings import BusinessSettings
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.nim import NimClient


@pytest.mark.parametrize(
    "text",
    [
        "删除好友",
        "清理好友",
        "好友清理",
        "/删除好友 123",
        "批量删除好友 100",
        "同意好友申请",
        "自动同意好友",
        "拒绝好友申请",
    ],
)
def test_friend_mutations_have_no_conversational_command(text):
    """Reject friendship mutations from every conversational identity.

    Args:
        text: Former cleanup or approval command.
    """
    assert not recognize_command(text)


def test_friend_cleanup_implementation_and_native_deletion_are_removed():
    """Ensure removal extends beyond hiding the dashboard entry."""
    assert (
        importlib.util.find_spec(
            "astrbot.builtin_stars.wangshangliao_moderation.friends"
        )
        is None
    )
    assert not hasattr(NimClient, "delete_friend")
    assert not hasattr(DashboardPage, "friends")
    assert not hasattr(BusinessSettings, "friend_cleanup_hold")
    root = Path(__file__).resolve().parents[1]
    assert not (
        root / "dashboard/plugin-pages/wangshangliao/FriendCleanup.vue"
    ).exists()
    app = (root / "dashboard/plugin-pages/wangshangliao/App.vue").read_text()
    assert "FriendCleanup" not in app
    assert "FriendApproval" in app


def friend_page(rows):
    """Encode bounded read-only SDK fixtures.

    Args:
        rows: Transport peer, timestamp and relation tuples.

    Returns:
        SDK directory response bytes.
    """
    return (
        wire.varint(len(rows))
        + b"".join(
            wire.properties(
                [
                    (4, peer.encode()),
                    (5, flag),
                    (8, b"Fixture"),
                    (11, b"1"),
                    (12, str(updated).encode()),
                ]
            )
            for peer, updated, flag in rows
        )
        + struct.pack("<Q", max((row[1] for row in rows), default=0))
    )


@pytest.mark.asyncio
async def test_friend_directory_paginates_and_excludes_deleted_relations():
    """Validate the retained read codec without a mutation API."""
    client = NimClient.__new__(NimClient)
    first = [(str(i + 1), i + 1, b"1") for i in range(3000)]
    second = [("3001", 3001, b"1"), ("3002", 3002, b"0")]
    client.request = AsyncMock(
        side_effect=[(200, friend_page(first)), (200, friend_page(second))]
    )
    result = await client.get_friends()
    assert len(result) == 3001 and "3002" not in result
    assert client.request.await_args_list[0].args == (12, 4, struct.pack("<Q", 0))
    assert client.request.await_args_list[1].args == (12, 4, struct.pack("<Q", 3000))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        wire.varint(3001),
        friend_page([("1", 1, b"1")])[:-1],
        friend_page([("1", 1, b"1"), ("1", 2, b"1")]),
        friend_page([("1", 0, b"1")]),
        friend_page([("1", 1, b"2")]),
        friend_page([("../shell", 1, b"1")]),
        friend_page([(str(i), 3000 - i, b"1") for i in range(3000)]),
        friend_page([(str(i + 1), 1, b"1") for i in range(3000)]),
    ],
)
async def test_incomplete_or_ambiguous_friend_directory_fails_closed(body):
    """Reject malformed read responses.

    Args:
        body: Ambiguous SDK page bytes.
    """
    client = NimClient.__new__(NimClient)
    client.request = AsyncMock(return_value=(200, body))
    with pytest.raises(wire.ProtocolError):
        await client.get_friends()


@pytest.mark.asyncio
async def test_complete_single_friend_page_does_not_require_server_sort_order():
    """Allow a complete unsorted read-only page."""
    client = NimClient.__new__(NimClient)
    client.request = AsyncMock(
        return_value=(200, friend_page([("1", 2, b"1"), ("2", 1, b"1")]))
    )
    assert set(await client.get_friends()) == {"1", "2"}
    client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_friend_names_only_expose_requested_accounts_and_names():
    """Keep unrequested profiles out of name lookup results."""
    client = NimClient.__new__(NimClient)
    body = wire.varint(1) + wire.properties(
        [(1, b"123"), (3, b"Name"), (9, b"SECRET_PHONE")]
    )
    client.request = AsyncMock(return_value=(200, body))
    assert await client.get_friend_names(["123"]) == {"123": "Name"}
    client.request.assert_awaited_once_with(3, 7, b"\x01\x03123")
    with pytest.raises(wire.ProtocolError):
        await client.get_friend_names(["other"])
