"""Regression coverage for numeric-contact ads and disjoint sustained bursts."""

import json
import time

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import content_rules
from astrbot.builtin_stars.wangshangliao_moderation.flood import FloodWindows
from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore
from tests import test_wangshangliao_flood as flood_fixtures
from tests.test_wangshangliao_flood import handle, ingest

fresh_bot = flood_fixtures.fresh_bot


@pytest.mark.parametrize(
    "text",
    [
        "注册绑定信息，转发朋友圈8条，录屏找12345678审核。"
        "累计充值600加3000电子流水秒反200，达标即秒返现。",
        "累计充值600加3000电子流水秒反200，业务联系12345678",
        "充值流水活跃达标即秒返现，联系：12345678",
        "转发朋友圈8条，审核找：12345678",
        "转发 朋友圈８条，录屏\u200b找１２３４５６７８审核",
    ],
)
def test_numeric_contact_promotions_are_detected_without_literal_keywords(text):
    assert content_rules.classify(text) == "external_promotion"
    assert (
        content_rules.content_category(
            {"text": text},
            {"advertising_enabled": True, "mute_keywords": []},
        )
        == "consecutive_digits"
    )
    assert (
        content_rules.content_category({"text": text}, {"advertising_enabled": False})
        == ""
    )


@pytest.mark.parametrize(
    "text",
    [
        "12345678",
        "录屏找12345678审核",
        "充值流水活跃达标即秒返现",
        "注册绑定信息，联系12345678处理账号问题",
        "报销的录屏找12345678审核，昨天花了600",
        "举报：转发朋友圈8条，录屏找12345678审核",
        "不要相信充值流水达标秒返现，联系12345678的广告",
        "引用 转发朋友圈8条，录屏找12345678审核",
    ],
)
def test_normal_numbers_contacts_and_reports_are_not_new_ad_rules(text):
    assert content_rules.classify(text) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["image", "mixed", "repeated", "video", "forward"])
async def test_sustained_disjoint_bursts_warn_three_times_then_mute(
    fresh_bot, monkeypatch, mode
):
    if mode in {"video", "forward"}:
        fresh_bot.config["moderation"].update(
            advertising_enabled=False,
            video_whitelist_only=True,
            recall_forward=True,
        )
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    for n in range(20):
        mid = str(2000 + n)
        kind = (
            mode
            if mode in {"video", "forward"}
            else "image"
            if mode == "image" or mode == "mixed" and n % 2
            else None
        )
        text = "" if kind else "same text" if mode == "repeated" else f"normal-{n}"
        envelope = await ingest(fresh_bot, mid, text, kind=kind)
        await handle(fresh_bot, mid, envelope)
        if n in {4, 9, 14, 19}:
            await handle(fresh_bot, mid, envelope)
        assert len([c for c in fresh_bot.calls if c[0] == "mute"]) == (
            1 if n == 19 else 0
        )
        if n == 9:
            # Restore the original evidence, not just process-local burst counters.
            fresh_bot._review_store = ReviewStore(fresh_bot._review_store.path)
            fresh_bot.settings.flood_windows = FloodWindows()
            async with fresh_bot.ledger.db.execute(
                "SELECT team,message,payload,received_at FROM inbox ORDER BY received_at"
            ) as cursor:
                rows = [
                    (r[0], r[1], json.loads(r[2]), r[3])
                    for r in await cursor.fetchall()
                ]
            fresh_bot.settings.flood_windows.restore("1", rows, now=clock)
        clock += 2.1 if mode == "repeated" else 0.4

    assert [c for c in fresh_bot.calls if c[0] == "mute"] == [("mute", "2", 1)]
    assert sorted(c[1] for c in fresh_bot.calls if c[0] == "recall") == [
        str(2000 + n) for n in range(20)
    ]
    with fresh_bot._review_store.connect() as db:
        assert db.execute("SELECT warnings FROM moderation_cycles").fetchone() == (3,)
        assert db.execute(
            "SELECT COUNT(*) FROM progressive_warnings WHERE status='accepted'"
        ).fetchone() == (3,)
        assert db.execute("SELECT COUNT(*) FROM flood_batches").fetchone() == (4,)


@pytest.mark.asyncio
async def test_overlapping_old_messages_do_not_count_as_a_second_warning(
    fresh_bot, monkeypatch
):
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    for n in range(5):
        envelope = await ingest(fresh_bot, str(3000 + n), "repeat")
    await handle(fresh_bot, "3004", envelope)
    clock += 20
    envelope = await ingest(fresh_bot, "3005", "repeat")
    assert await handle(fresh_bot, "3005", envelope)
    with fresh_bot._review_store.connect() as db:
        assert db.execute("SELECT warnings FROM moderation_cycles").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM flood_batches").fetchone() == (1,)
    assert sorted(c[1] for c in fresh_bot.calls if c[0] == "recall") == [
        str(3000 + n) for n in range(6)
    ]


@pytest.mark.asyncio
async def test_unknown_warning_receipt_is_not_retried_or_escalated(
    fresh_bot, monkeypatch
):
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    original = fresh_bot.nim.request.side_effect

    async def unknown_trigger(service, command, encoded):
        from astrbot.core.platform.sources.wangshangliao import wire

        properties, _ = wire.read_properties(encoded)
        if properties[11] == b"4004":
            raise TimeoutError()
        return await original(service, command, encoded)

    fresh_bot.nim.request.side_effect = unknown_trigger
    for n in range(20):
        mid = str(4000 + n)
        envelope = await ingest(fresh_bot, mid, "", kind="image")
        await handle(fresh_bot, mid, envelope)
        clock += 0.3
    with fresh_bot._review_store.connect() as db:
        assert db.execute("SELECT warnings FROM moderation_cycles").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM flood_batches").fetchone() == (1,)
        assert db.execute(
            "SELECT status FROM flood_recalls WHERE message='4004'"
        ).fetchone() == ("unknown",)
    assert not [c for c in fresh_bot.calls if c[0] == "mute"]
    assert fresh_bot.nim.request.await_count == 20


@pytest.mark.parametrize("status", ["accepted", "verified", "rejected", "unknown"])
def test_a_submitted_mute_is_never_repeated_by_disjoint_new_bursts(tmp_path, status):
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    identity = ("1", "5", "2", "22", "revision")
    first = [str(5000 + n) for n in range(5)]
    assert store.reserve_flood(
        "first", *identity, first[-1], 1000, first, windows=((5, first),)
    ) == ("first", None, first)
    store.finish_flood("first", status)
    for mid in first:
        store.finish_flood("first", "accepted", message=mid)
    second = [str(5010 + n) for n in range(5)]
    assert store.reserve_flood(
        "next", *identity, second[-1], 1001, second, windows=((5, second),)
    ) == ("first", status, second)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM flood_batches").fetchone() == (1,)


def test_legacy_warning_is_frozen_once_without_absorbing_future_bursts(tmp_path):
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    identity = ("1", "5", "2", "22", "revision")
    first = [str(6000 + n) for n in range(5)]
    store.reserve_flood("legacy", *identity, first[-1], 1000, first)
    store.finish_flood("legacy", "none")
    for mid in first:
        store.finish_flood("legacy", "accepted", message=mid)
    with store.connect() as db:
        db.execute("DELETE FROM flood_warning_evidence")
    for n in range(1, 6):
        messages = first + [str(6004 + extra) for extra in range(1, n + 1)]
        operation, status, pending = store.reserve_flood(
            f"next-{n}",
            *identity,
            messages[-1],
            1000 + n,
            messages,
            windows=((5, messages),),
        )
        assert (operation, status) == (
            (f"next-{n}", None) if n == 5 else ("legacy", "none")
        )
        assert pending == [messages[-1]]
        store.finish_flood(operation, "accepted", message=messages[-1])


def test_warning_windows_expire_without_deleting_original_action_audit(tmp_path):
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    identity = ("1", "5", "2", "22", "revision")
    for operation, stamp in (("old", 1000), ("current", 100000)):
        messages = [str(stamp + n) for n in range(5)]
        store.reserve_flood(
            operation,
            *identity,
            messages[-1],
            stamp,
            messages,
            windows=((5, messages),),
        )
    with store.connect() as db:
        assert db.execute(
            "SELECT operation FROM flood_warning_evidence"
        ).fetchall() == [("current",)]
        assert db.execute("SELECT COUNT(*) FROM flood_batches").fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM flood_recalls").fetchone() == (10,)
