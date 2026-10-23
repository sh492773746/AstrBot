"""Only syntactically valid, unambiguous fragments reach atomic acceptance."""

# ruff: noqa: F811

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, request, text_service  # noqa: F401

from data.plugins.astrbot_plugin_superbot.bet_text import parse_partial


@pytest.mark.parametrize(
    "text,items,skipped",
    [
        ("大10xs0", [("big", 10)], 1),
        ("dd100 28/10 01/01", [("big_odd", 100), ("number_1", 1)], 1),
        ("大10 27//10 小单20", [("big", 10), ("small_odd", 20)], 1),
        ("大10xs-2bz3", [("big", 10), ("triple", 3)], 1),
        ("大10xs1.5bz3", [("big", 10), ("triple", 3)], 1),
        ("大10027/10 小10", [("small", 10)], 1),
        ("大 10｜dd 20\n01/ 01", [("big", 10), ("big_odd", 20), ("number_1", 1)], 0),
    ],
)
def test_partial_boundaries(text, items, skipped):
    assert parse_partial(text) == (items, skipped)


@pytest.mark.asyncio
async def test_partial_receipt_debits_only_valid_and_deduplicates(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, "dd100 28/10 01/01", 10)
    assert s.store.balance("2") == 899
    assert request(s)["result"] == "accepted"
    assert "已跳过 1 段" in request(s)["text"]
    assert s.store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 2
    await accept(s, "dd100 28/10 01/01", 10)
    assert s.store.balance("2") == 899
