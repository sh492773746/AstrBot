"""Position order, independent payouts and per-item caps for additive plays."""

# ruff: noqa: F811

import itertools
import json

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, request, text_service  # noqa: F401

from data.plugins.astrbot_plugin_superbot.bet_text import parse_partial
from data.plugins.astrbot_plugin_superbot.rules import (
    NEW_CAPS,
    NEW_ODDS,
    evaluate,
    room_defaults,
)


def test_mixed_parser_and_position_number_syntax():
    assert parse_partial("a大100 B单100 c小单10 a9点20 小边10大边20边30中40") == (
        [
            ("pos_a_big", 100),
            ("pos_b_odd", 100),
            ("pos_c_small_odd", 10),
            ("pos_a_number_9", 20),
            ("edge_small", 10),
            ("edge_big", 20),
            ("edge", 30),
            ("middle", 40),
        ],
        0,
    )


@pytest.mark.parametrize("room_id", ["double", "room28"])
def test_all_thousand_draws_all_new_plays(room_id):
    room = room_defaults()[room_id]
    for balls in itertools.product(range(10), repeat=3):
        total = sum(balls)
        expected = {
            "edge_small": total <= 9,
            "edge_big": total >= 18,
            "edge": total <= 9 or total >= 18,
            "middle": 10 <= total <= 17,
        }
        for pos, digit in zip("abc", balls):
            big, odd = digit >= 5, bool(digit % 2)
            kinds = {
                "big": big,
                "small": not big,
                "odd": odd,
                "even": not odd,
                "big_odd": big and odd,
                "big_even": big and not odd,
                "small_odd": not big and odd,
                "small_even": not big and not odd,
            }
            expected.update({f"pos_{pos}_{k}": v for k, v in kinds.items()})
            expected.update({f"pos_{pos}_number_{n}": digit == n for n in range(10)})
        for play, win in expected.items():
            assert evaluate(room, play, 100, list(balls)) == (
                ("win", NEW_ODDS[play]) if win else ("lose", 0)
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected",
    [
        ("a大2001", "rejected"),
        ("小边501", "rejected"),
        ("a9点101", "rejected"),
        ("a大单1001", "rejected"),
        ("a大500a大500", "accepted"),
    ],
)
async def test_single_limits(text_service, text, expected):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, text, 10)
    assert request(s)["result"] == expected
    assert s.store.balance("2") == (0 if expected == "accepted" else 1000)


@pytest.mark.asyncio
async def test_repeat_single_cap_does_not_become_period_cap(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, "小边500小边500", 10)
    assert request(s)["result"] == "accepted"
    snapshot = json.loads(
        s.store.db.execute("SELECT snapshot FROM bets LIMIT 1").fetchone()[0]
    )
    assert snapshot["single_limits"] == NEW_CAPS
    assert snapshot["odds"]["edge_small"] == 440
    assert s.store.balance("2") == 0


def test_existing_custom_odds_and_original_rules_unchanged(text_service):
    s = text_service
    rooms = room_defaults()
    rooms["room28"]["odds"]["big"] = 281
    for room in rooms.values():
        for p in NEW_ODDS:
            room["odds"].pop(p, None)
    with s.store.tx() as db:
        s.store.put(db, "rooms", rooms)
    augmented = s.runtime.game.rooms()
    assert augmented["room28"]["odds"]["big"] == 281
    for room_id, old in rooms.items():
        for play, odds in old["odds"].items():
            assert augmented[room_id]["odds"][play] == odds
            for balls in ([0, 1, 9], [8, 9, 0], [4, 4, 5], [4, 5, 5], [3, 3, 3]):
                assert evaluate(augmented[room_id], play, 100, balls) == evaluate(
                    old, play, 100, balls
                )
