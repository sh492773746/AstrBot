"""Regression cases from the October 21 moderation incidents."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import action_receipts
from astrbot.builtin_stars.wangshangliao_moderation.content_rules import classify
from astrbot.builtin_stars.wangshangliao_moderation.flood import FloodWindows
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.parametrize(
    "offsets,same,expected",
    [
        ([0, 0.5, 1, 2, 3], False, True),
        ([0, 0.5, 1, 2, 3.001], False, False),
        (
            [
                0,
                0.01,
                0.02,
                0.03,
                3.3,
                3.31,
                3.32,
                3.33,
                6.6,
                6.61,
                6.62,
                6.63,
                9.98,
                9.99,
                10,
            ],
            False,
            True,
        ),
        ([0, 7, 15, 22, 30], True, True),
        ([0, 7, 15, 22, 30.001], True, False),
    ],
)
def test_flood_thresholds_keep_both_native_and_local_time_windows(
    offsets, same, expected
):
    window = FloodWindows()
    evidence = None
    for n, offset in enumerate(offsets):
        now = 1792600000 + offset
        evidence = window.observe(
            "1",
            "5",
            str(n),
            {
                "sender": "2",
                "text": "same" if same else str(n),
                "recall_route": {"time": now * 1000},
            },
            now,
        )
    assert bool(evidence["category"]) is expected
    if expected:
        assert evidence["messages"][-1] == str(len(offsets) - 1)


def test_five_message_threshold_does_not_mix_accounts_groups_or_senders():
    window = FloodWindows()
    for account, group, sender in [
        ("1", "5", "2"),
        ("1", "5", "3"),
        ("1", "6", "2"),
        ("9", "5", "2"),
        ("1", "5", "2"),
    ]:
        evidence = window.observe(
            account,
            group,
            str(len(window.windows)) + sender + account + group,
            {"sender": sender, "text": "text", "recall_route": {"time": 1792600000000}},
            1792600000,
        )
        assert not evidence["category"]


@pytest.mark.parametrize(
    "text",
    [
        "免费劈涂上分 我上你打 提现55",
        "免费劈涂上分 我上你打 提现五五分，来合作",
        "手痒想干没米上分d我",
        "没米上分Ｄ我",
        "有微信扫码秒存渠道的老板联系我123456789",
        "有支付宝扫码秒存渠道的朋友私聊我123456789",
        "做项目的来秒结",
    ],
)
def test_confirmed_ad_variants_and_report_exemptions(text):
    assert classify(text) == "external_promotion"
    for prefix in ["举报：", "警惕", "引用：", "转发：", "不要相信"]:
        assert not classify(prefix + text)


@pytest.mark.parametrize(
    "text",
    [
        "微信扫码怎么存款",
        "有微信扫码渠道吗",
        "扫码渠道的老板联系过我",
        "没米上分，先不玩了",
        "免费劈涂怎么画",
        "做项目的来秒结是什么意思",
        "我上你打这句话是什么意思",
        "今天做项目，工资已秒结",
    ],
)
def test_similar_normal_messages_do_not_become_advertisements(text):
    assert not classify(text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,reserved,expected",
    [
        ("recall", False, "rejected"),
        ("recall", True, "unknown"),
        ("mute", False, "rejected"),
        ("mute", True, "unknown"),
    ],
)
async def test_action_audit_uses_native_intent_not_read_request_flags(
    tmp_path, monkeypatch, action, reserved, expected
):
    monkeypatch.setattr(action_receipts, "instance_dir", lambda _: tmp_path)
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    original = ProtocolError("moderation_permission")
    original.submitted = True  # The directory lookup, NOT a mute submission.
    bot = SimpleNamespace(
        account="1",
        config={"id": "fixture"},
        ledger=ledger,
        execute_action=AsyncMock(side_effect=original),
        diagnostics=SimpleNamespace(emit=Mock()),
    )
    request = ActionRequest.bind(
        bot,
        "recall/1/5/100" if action == "recall" else "mute/test",
        action,
        5,
        member=2,
        **({"expected_nim": "22", "message": "100"} if action == "recall" else {}),
    )
    if reserved:
        if action == "recall":
            await ledger.reserve(request.operation, "{}")
            await ledger.finish(request.operation, "unknown")
        else:
            with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
                db.execute(
                    "CREATE TABLE operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
                )
                db.execute(
                    "INSERT INTO operations VALUES(?,'digest',?)",
                    (
                        request.operation,
                        json.dumps({"status": "unknown", "submitted": True}),
                    ),
                )
    try:
        wrapped = action_receipts.ModerationActions(bot, "5")
        with pytest.raises(ProtocolError) as raised:
            await wrapped.execute_action(request)
        assert raised.value is original
        assert raised.value.action_status == expected
        with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
            row = db.execute(
                "SELECT status,submitted,error FROM moderation_action_audit"
            ).fetchone()
        assert row[0] == expected
        assert row[1] == (False if not reserved else True if action == "mute" else None)
        assert row[2] == "moderation_permission"
        bot.execute_action.assert_awaited_once()
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_cancelled_action_preserves_native_unknown_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(action_receipts, "instance_dir", lambda _: tmp_path)
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    bot = SimpleNamespace(
        account="1",
        config={"id": "fixture"},
        ledger=ledger,
        execute_action=AsyncMock(side_effect=asyncio.CancelledError()),
        diagnostics=SimpleNamespace(emit=Mock()),
    )
    request = ActionRequest.bind(
        bot, "recall/1/5/100", "recall", 5, member=2, expected_nim="22", message="100"
    )
    await ledger.reserve(request.operation, "{}")
    await ledger.finish(request.operation, "unknown")
    try:
        with pytest.raises(asyncio.CancelledError):
            await action_receipts.ModerationActions(bot, "5").execute_action(request)
        assert (await ledger.receipt(request.operation))["status"] == "unknown"
    finally:
        await ledger.close()


def test_action_audit_refines_receipts_without_losing_scope_or_submission(tmp_path):
    from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore

    store = ReviewStore(tmp_path / "moderation.sqlite3")

    def save(status, submitted, error="", *, account="1"):
        store.save_action(
            "recall/1/5/100",
            account,
            "5",
            "2",
            "100",
            "recall",
            status,
            submitted,
            error,
            None,
        )
        with sqlite3.connect(store.path) as db:
            return db.execute(
                "SELECT account,status,submitted,error FROM moderation_action_audit"
            ).fetchone()

    assert save("rejected", False, "moderation_permission") == (
        "1",
        "rejected",
        0,
        "moderation_permission",
    )
    assert save("unknown", True, "transport") == ("1", "unknown", 1, "transport")
    assert save("rejected", False, "moderation_identity") == (
        "1",
        "unknown",
        1,
        "transport",
    )
    assert save("accepted", True, account="9") == ("1", "unknown", 1, "transport")
    assert save("accepted", True) == ("1", "accepted", 1, "")
    assert save("unknown", None, "transport") == ("1", "accepted", 1, "")
