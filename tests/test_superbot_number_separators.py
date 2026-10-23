"""Explicit number/amount separators never permit fractional amounts."""

# ruff: noqa: F811

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, request, text_service  # noqa: F401

from data.plugins.astrbot_plugin_superbot.bet_text import (
    is_candidate,
    parse,
    parse_partial,
)
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.parametrize("separator", [".", "押"])
@pytest.mark.parametrize("number", range(28))
def test_all_numbers(separator, number):
    text = f"{number:02d}{separator}013"
    assert is_candidate(text)
    assert parse(text) == [(f"number_{number}", 13)]


@pytest.mark.parametrize(
    "text", ["大1.5", "15.1.3", "15押1.3", "28.13", "15.0", "15押-13"]
)
def test_invalid_or_fractional_amounts(text):
    with pytest.raises(Rejected):
        parse(text)


def test_mixed_partial_order():
    assert parse_partial("dd100 15.13 28押10 01押02") == (
        [("big_odd", 100), ("number_15", 13), ("number_1", 2)],
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["15.13", "15押13"])
async def test_accept_and_duplicate(text_service, text):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, text, 10)
    assert request(s)["result"] == "accepted"
    assert s.store.balance("2") == 987
    assert tuple(s.store.db.execute("SELECT play,amount FROM bets").fetchone()) == (
        "number_15",
        13,
    )
    await accept(s, text, 10)
    assert s.store.balance("2") == 987
