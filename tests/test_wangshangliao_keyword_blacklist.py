"""Fixed solicitation rules use real ingress and durable blacklist authority."""

import asyncio
import copy
import threading
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    content_rules,
    member_exemptions,
)
from astrbot.builtin_stars.wangshangliao_moderation import keyword_blacklist as rules
from astrbot.builtin_stars.wangshangliao_moderation.member_exemptions import (
    set_exemption,
)
from astrbot.core.platform.sources.wangshangliao import blacklist
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests import test_wangshangliao_flood as fixtures
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import drain_recalls, ingest
from tests.test_wangshangliao_sanction_cleanup import process

fresh_bot = fixtures.fresh_bot
TEXT = "888569旺旺➕ 正在开会打🐶 红了发888口令🧧"


@pytest.mark.parametrize(
    "text",
    [
        TEXT,
        "正在开会打狗",
        "红了发8口令",
        "红了发888元口令",
        "８８８５６９ 旺 旺 ＋",
        "正\u200b在 开 会 打 🐶️",
        "红了发８８８口令",
        "123456旺旺+",
    ],
)
def test_fixed_phrases_match_without_a_bare_common_word(text):
    assert rules.matches(text)
    assert content_rules.content_category({"text": text}, {}) == "blacklist_keyword"
    assert (
        content_rules.content_category({"text": text}, {"advertising_enabled": False})
        == ""
    )


@pytest.mark.parametrize(
    "text",
    [
        "旺旺",
        "888",
        "口令",
        "正在开会",
        "开会打",
        "红了发红包",
        "明晚发888口令红包",
        "888569",
        "1388888569",
        "123旺旺+",
        "18885691234567旺旺+",
        "举报：" + TEXT,
        "有人发" + TEXT,
        "引用：" + TEXT,
        "“" + TEXT + "”",
        "不要相信" + TEXT,
    ],
)
def test_reports_and_unrelated_terms_never_authorize_a_blacklist(text):
    assert not rules.matches(text)


@pytest.fixture
def target(fresh_bot, monkeypatch, tmp_path):
    bot = fresh_bot
    monkeypatch.setattr(blacklist, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
    bot.config["moderation"].update(advertising_enabled=True, flood_enabled=True)
    bot.member_snapshots = {
        "5": ("1", time.monotonic(), bot.get_moderation_members.return_value)
    }
    store = blacklist.BlacklistStore(bot.config["id"])
    store.configure("1", "5", 10080, "dashboard/operator", lambda: None)

    async def mute(
        operation,
        action,
        group,
        member,
        text="",
        *,
        minutes,
        action_check,
        blacklist_intent,
        blacklist_fixture,
        blacklist_guard,
    ):
        await action_check()
        blacklist_guard()
        bot.calls.append((action, str(member), minutes))
        return {
            "operation": operation,
            "action": action,
            "group": group,
            "member": member,
            "minutes": minutes,
            "expected_nim": blacklist_intent.peer,
            "submitted": True,
            "status": "accepted",
        }

    bot.execute_moderation = AsyncMock(side_effect=mute)
    return bot, store


@pytest.mark.asyncio
async def test_first_violation_enrolls_then_mutes_then_recalls(target):
    bot, store = target
    runtime = runtime_for(bot)
    try:
        payload = await ingest(bot, "100", TEXT)
        assert payload["_ingress_content"]["category"] == "blacklist_keyword"
        await process(runtime)
        assert bot.calls == [("mute", "2", 10080)]
        entry = store.read("1", "5")[1][0]
        assert entry["member"] == "2" and entry["enforcement_status"] == "accepted"
        assert entry["actor"] == "1"
        assert store.read("1", "6") == (0, [])
        assert store.read("9", "5") == (0, [])
        with store._transaction() as db:
            row = db.execute("SELECT * FROM blacklist_intents").fetchone()
            assert row["trigger"] == "keyword"
            assert row["join_id"] == "keyword/100"
        await drain_recalls(runtime)
        assert bot.calls == [("mute", "2", 10080), ("recall", "100", "22")]
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT category,status,warning FROM content_violations"
            ).fetchone() == ("blacklist_keyword", "muted", "accepted")
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='progressive_actions'"
            ).fetchone()
        await process(runtime)
        assert len(bot.calls) == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "unknown", "rejected"])
async def test_one_keyword_mute_per_enrollment_never_retries_unknown(target, status):
    bot, store = target
    original = bot.execute_moderation.side_effect

    async def outcome(*args, **kwargs):
        result = await original(*args, **kwargs)
        return {**result, "status": status}

    bot.execute_moderation.side_effect = outcome
    runtime = runtime_for(bot)
    try:
        for mid in ("100", "101"):
            await ingest(bot, mid, TEXT)
            await process(runtime)
            await drain_recalls(runtime)
        assert bot.execute_moderation.await_count == 1
        assert len([c for c in bot.calls if c[0] == "recall"]) == 2
        assert store.read("1", "5")[1][0]["cycle"] == 1
        with store._transaction() as db:
            assert (
                db.execute("SELECT status FROM blacklist_intents").fetchall()[0][0]
                == status
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", ["admin", "whitelist", "incomplete", "identity"])
async def test_protected_or_unverified_targets_are_not_enrolled(target, protected):
    bot, store = target
    if protected == "admin":
        bot.get_moderation_members.return_value["groupMemberInfo"][1]["groupRole"] = (
            "GROUP_ROLE_ADMIN"
        )
    elif protected == "whitelist":
        set_exemption(
            bot.config["id"],
            "1",
            "5",
            "2",
            "22",
            enabled=True,
            actor="server/test",
            reason="test",
            guard=lambda: None,
        )
    elif protected == "incomplete":
        bot.get_moderation_members.return_value["complete"] = False
    else:
        bot.get_moderation_members.return_value["groupMemberInfo"][1]["nimId"] = (
            "changed"
        )
    runtime = runtime_for(bot)
    try:
        await ingest(bot, "100", TEXT)
        await process(runtime)
        await drain_recalls(runtime)
        assert not store.read("1", "5")[1]
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "remove", "duration", "depart"])
async def test_changes_before_submission_block_mute_but_keep_independent_recall(
    target, change
):
    bot, store = target

    async def revoked(*args, **kwargs):
        if change == "pause":
            bot.config["_business_active"] = False
        elif change == "remove":
            store.set_member(
                "1", "5", "2", "22", "Target", False, "dashboard/operator", lambda: None
            )
        elif change == "duration":
            store.configure("1", "5", 60, "dashboard/operator", lambda: None)
        else:
            bot.members["5"].clear()
        await kwargs["action_check"]()
        pytest.fail("A revoked mute reached native submission")

    bot.execute_moderation.side_effect = revoked
    runtime = runtime_for(bot)
    try:
        await ingest(bot, "100", TEXT)
        await process(runtime)
        if change == "pause":
            await runtime.close()
        else:
            await drain_recalls(runtime)
        assert not [c for c in bot.calls if c[0] == "mute"]
        assert [c for c in bot.calls if c[0] == "recall"] == (
            [("recall", "100", "22")] if change in {"remove", "duration"} else []
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_live_keyword_rule_remains_deterministic_with_semantic_enabled(target):
    bot, _ = target
    bot.config["moderation"]["semantic"] = {"enabled": True}
    payload = await ingest(bot, "100", TEXT)
    assert payload["_ingress_content"]["category"] == "blacklist_keyword"


@pytest.mark.asyncio
async def test_keyword_blacklist_takes_precedence_over_overlapping_flood(target):
    bot, store = target
    runtime = runtime_for(bot)
    try:
        for n in range(6):
            await ingest(bot, str(100 + n), TEXT)
        await drain_recalls(runtime)
        assert [c for c in bot.calls if c[0] == "mute"] == [("mute", "2", 10080)]
        assert len([c for c in bot.calls if c[0] == "recall"]) == 6
        assert len(store.read("1", "5")[1]) == 1
        with bot._review_store.connect() as db:
            assert {
                r[0] for r in db.execute("SELECT category FROM content_violations")
            } == {"blacklist_keyword"}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["unknown", "cancelled", "full"])
async def test_recall_outcomes_do_not_erase_accepted_blacklist_mute(target, cleanup):
    bot, store = target
    runtime = runtime_for(bot, backlog=1 if cleanup == "full" else 256)
    release = asyncio.Event()
    if cleanup == "unknown":
        bot.nim.request.side_effect = TimeoutError()
    elif cleanup == "full":
        assert runtime.defer_recalls(bot, 1, release.wait)
    try:
        await ingest(bot, "100", TEXT)
        await process(runtime)
        if cleanup == "cancelled":
            await runtime.close()
        elif cleanup == "full":
            assert bot.calls == [("mute", "2", 10080), ("recall", "100", "22")]
            release.set()
            await drain_recalls(runtime)
        else:
            await drain_recalls(runtime)
        expected = {"unknown": "unknown", "cancelled": "rejected", "full": "accepted"}
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations"
            ).fetchone() == ("muted", expected[cleanup])
        assert store.read("1", "5")[1][0]["enforcement_status"] == "accepted"
        assert bot.execute_moderation.await_count == 1
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forgery", ["text", "sender", "group", "missing_ingress", "old"]
)
async def test_missing_stale_or_altered_inbox_evidence_never_enrolls(target, forgery):
    bot, store = target
    payload = await ingest(bot, "100", TEXT)
    if forgery == "text":
        payload["text"] = "正在开会打狗"
    elif forgery == "sender":
        payload["_ingress_content"]["sender"] = "9"
    elif forgery == "group":
        payload["_ingress_content"]["group"] = "6"
    elif forgery == "missing_ingress":
        payload.pop("_ingress_content")
    else:
        await bot.ledger.db.execute(
            "UPDATE inbox SET received_at=?", (time.time() - 301,)
        )
        await bot.ledger.db.commit()
    member = bot.get_moderation_members.return_value["groupMemberInfo"][1]
    with pytest.raises(ProtocolError, match="evidence"):
        await rules.enforce(bot, "5", "100", payload, member, lambda: None)
    assert not store.read("1", "5")[1]
    assert not bot.calls


@pytest.mark.asyncio
async def test_keyword_intent_is_scoped_and_invalid_after_removal(target):
    bot, store = target
    payload = await ingest(bot, "100", TEXT)
    member = bot.get_moderation_members.return_value["groupMemberInfo"][1]
    captured = []

    async def save_only(*args, **kwargs):
        captured.append(kwargs["blacklist_intent"])
        await kwargs["action_check"]()
        return {"status": "rejected", "submitted": False}

    bot.execute_moderation.side_effect = save_only
    await rules.enforce(bot, "5", "100", payload, member, lambda: None)
    intent = captured[0]
    with pytest.raises(ProtocolError, match="invalid"):
        blacklist.authorize_intent(
            intent.instance, "1", intent.operation, "mute", "6", "2", 10080, intent
        )
    with pytest.raises(ProtocolError, match="invalid"):
        forged = replace(intent, trigger="command")
        blacklist.authorize_intent(
            intent.instance, "1", intent.operation, "mute", "5", "2", 10080, forged
        )
    store.set_member(
        "1", "5", "2", "22", "Target", False, "dashboard/operator", lambda: None
    )
    assert store.read("1", "5")[1] == []


@pytest.mark.asyncio
async def test_cancel_during_reservation_is_drained_without_native_submission(
    target, monkeypatch
):
    bot, store = target
    payload = await ingest(bot, "100", TEXT)
    member = bot.get_moderation_members.return_value["groupMemberInfo"][1]
    entered, release = threading.Event(), threading.Event()
    original = blacklist.BlacklistStore.reserve_keyword

    def blocked(self, *args):
        entered.set()
        release.wait(3)
        return original(self, *args)

    monkeypatch.setattr(blacklist.BlacklistStore, "reserve_keyword", blocked)
    task = asyncio.create_task(
        rules.enforce(bot, "5", "100", payload, copy.deepcopy(member), lambda: None)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not bot.calls
        with store._transaction() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM blacklist_intents").fetchone()[0] == 0
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
