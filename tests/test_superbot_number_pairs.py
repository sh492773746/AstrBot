"""Fixed-position numeric pairs never shift invalid stakes into another play."""

# ruff: noqa: F811

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, request, text_service  # noqa: F401

from data.plugins.astrbot_plugin_superbot.bet_text import parse_partial
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.parametrize(
    "text,items,skipped",
    [
        ("9 90 10 90 25 90", [("number_9", 90), ("number_10", 90), ("number_25", 90)], 0),
        ("9 90\n28 10\n25 90 7", [("number_9", 90), ("number_25", 90)], 2),
        ("01 01 00 10", [("number_1", 1), ("number_0", 10)], 0),
        ("9 0 10 90", [("number_10", 90)], 1),
        ("9 9999999999999 10 90", [("number_10", 90)], 1),
    ],
)
def test_fixed_numeric_pairs(text, items, skipped):
    assert parse_partial(text) == (items, skipped)


def test_numeric_pair_limits():
    with pytest.raises(Rejected):
        parse_partial("9 1 " * 51)
    with pytest.raises(Rejected):
        parse_partial("28 90 9 0")


@pytest.mark.asyncio
async def test_numeric_pairs_require_own_group_activation(text_service):
    s = text_service
    await accept(s, "9 90 10 90 25 90", 10)
    assert request(s)["result"] == "room_required"
    await accept(s, "jnd", 1)
    await accept(s, "9 90 10 90 25 90", 11)
    assert request(s, 11)["result"] == "accepted"
    assert s.store.balance("2") == 730
    await accept(s, "9 90 10 90 25 90", 11)
    assert s.store.balance("2") == 730
    await accept(s, "9 90", 12, uid=3)
    assert request(s, 12)["result"] == "room_required"
    s.clock[0] += 1801
    await accept(s, "9 90", 13)
    assert request(s, 13)["result"] == "room_required"
