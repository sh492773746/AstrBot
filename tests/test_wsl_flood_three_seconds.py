"""Regression coverage for the five-message, three-second flood threshold."""

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.flood import FloodWindows


@pytest.mark.parametrize(
    ("received_offsets", "sent_offsets", "expected_rapid", "expected_category"),
    [
        ((0, 1, 2), (0, 1, 2), 3, ""),
        ((0, 1.5, 3), (0, 1.5, 3), 3, ""),
        ((0, 0.5, 1, 2, 3), (0, 0.5, 1, 2, 3), 5, "message_flood"),
        ((0, 0.5, 1, 2, 3.001), (0, 0.5, 1, 2, 3.001), 4, ""),
        ((0, 2, 4), (0, 2, 4), 2, ""),
        ((0, 2.5, 5), (0, 2.5, 5), 2, ""),
        ((0, 1.5, 3.001), (0, 1.5, 3.001), 2, ""),
        ((0, 1, 3), (0, 2, 4), 2, ""),
        ((0, 2, 4), (0, 1, 3), 2, ""),
    ],
)
@pytest.mark.parametrize("kind", ["text", "image", "sticker", "video"])
def test_rapid_flood_requires_both_three_second_windows(
    received_offsets, sent_offsets, expected_rapid, expected_category, kind
):
    windows = FloodWindows()
    base = 1792656000
    for index, (received, sent) in enumerate(zip(received_offsets, sent_offsets)):
        evidence = windows.observe(
            "1",
            "5",
            str(index),
            {
                "sender": "2",
                "text": f"message {index}" if kind == "text" else "",
                "kind": kind,
                "recall_route": {"time": int((base + sent) * 1000)},
            },
            base + received,
        )

    assert evidence["live"] is True
    assert evidence["rapid"] == expected_rapid
    assert evidence["category"] == expected_category
    assert evidence["messages"] == (
        [str(n) for n in range(len(received_offsets))] if expected_category else []
    )


@pytest.mark.parametrize(
    ("offsets", "repeated", "expected_count", "expected_repeated"),
    [
        (tuple(index * 0.7 for index in range(15)), False, 15, 1),
        ((0, 6, 12, 18, 24), True, 2, 5),
    ],
)
def test_existing_ten_and_thirty_second_thresholds_remain_unchanged(
    offsets, repeated, expected_count, expected_repeated
):
    windows = FloodWindows()
    base = 1792656000
    for index, offset in enumerate(offsets):
        evidence = windows.observe(
            "1",
            "5",
            str(index),
            {
                "sender": "2",
                "text": "same message" if repeated else f"message {index}",
                "recall_route": {"time": int((base + offset) * 1000)},
            },
            base + offset,
        )

    assert evidence["category"] == "message_flood"
    assert evidence["count"] == expected_count
    assert evidence["repeated"] == expected_repeated
    assert evidence["messages"] == [str(index) for index in range(len(offsets))]
