"""Account labels never replace stable transport or permission identities."""

import copy
from types import SimpleNamespace

import pytest

from astrbot.core.platform.sources.wangshangliao.identity import account_nickname


@pytest.mark.parametrize(
    "change,expected",
    [
        ("none", "New Name"),
        ("other_account", "Old Name"),
        ("other_member", "Old Name"),
        ("other_peer", "Old Name"),
        ("partial", "Old Name"),
        ("group_alias", "Old Name"),
        ("older", "Newest Name"),
        ("changed_account", ""),
        ("malformed_name", "Old Name"),
        ("controls", "New Name"),
    ],
)
def test_nickname_requires_current_self_identity_and_complete_snapshot(
    change, expected
):
    """Prefer fresh account nicknames without treating group aliases as names.

    Args:
        change: Identity, freshness or name variation to test.
        expected: Expected bounded plain-text nickname.
    """
    config = {"id": "stable-instance", "account_id": "1", "nickname": "Old Name"}
    row = {"userId": "1", "nimId": "101", "userNick": "New Name"}
    roster = {"complete": True, "groupMemberInfo": [row]}
    adapter = SimpleNamespace(
        config=config,
        account="1",
        nim_account="101",
        member_snapshots={"5": ("1", 10, roster)},
    )
    if change == "other_account":
        adapter.member_snapshots["5"] = ("2", 10, roster)
    elif change == "other_member":
        row["userId"] = "2"
    elif change == "other_peer":
        row["nimId"] = "102"
    elif change == "partial":
        roster["complete"] = False
    elif change == "group_alias":
        row["userNick"] = ""
        row["groupMemberNick"] = "Not an account nickname"
    elif change == "older":
        adapter.member_snapshots["6"] = (
            "1",
            20,
            {
                "complete": True,
                "groupMemberInfo": [{**row, "userNick": "Newest Name"}],
            },
        )
    elif change == "changed_account":
        adapter.account = "2"
    elif change == "malformed_name":
        row["userNick"] = {"token": "must-not-render"}
    elif change == "controls":
        row["userNick"] = "\x00\u202e New   Name \n"
    before = copy.deepcopy(vars(adapter))
    assert account_nickname(adapter) == expected
    assert vars(adapter) == before


@pytest.mark.parametrize(
    "nickname,expected",
    [("", ""), (None, ""), ("x" * 200, "x" * 128), (" Saved ", "Saved")],
)
def test_disconnected_account_uses_bounded_saved_nickname(nickname, expected):
    """Keep disconnected labels available without credential or network access.

    Args:
        nickname: Saved public nickname.
        expected: Expected normalized display value.
    """
    assert account_nickname(None, {"account_id": "1", "nickname": nickname}) == expected
