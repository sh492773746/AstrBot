"""Consecutive numeric text gets an independent, protected one-minute mute."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    content_rules,
    member_exemptions,
    numeric_moderation,
    policy,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import drain_recalls, fresh_bot, ingest

bot = fresh_bot


@pytest.mark.parametrize(
    "text,matched",
    [
        ("12345", False),
        ("123456", True),
        ("1234567", True),
        ("000000", True),
        ("编号abc123456def", True),
        ("123456.78", True),
        ("12345.6", False),
        ("123 456", False),
        ("123\n456", False),
        ("123\r\n456", False),
        ("123\u2028456", False),
        ("123-456", False),
        ("123\u200b456", False),
        ("１２３４５６", True),
        ("１２３ ４５６", False),
        ("一二三四五六", False),
        ("举报123456", True),
        ("短数字888", False),
    ],
)
def test_consecutive_digit_boundary_and_group_gate(text, matched):
    payload = {"text": text, "name": "123456789", "sender": "123456789"}
    expected = "consecutive_digits" if matched else ""
    assert content_rules.content_category(payload, {}) == expected
    assert content_rules.content_category(payload, {"advertising_enabled": False}) == ""


def test_numeric_rule_does_not_downgrade_explicit_blacklist_phrase():
    assert (
        content_rules.content_category({"text": "888569旺旺➕"}, {})
        == "blacklist_keyword"
    )


def configure(bot, monkeypatch):
    """Keep real ingress/action dispatch while isolating read-only native queries.

    Args:
        bot: Temporary native action adapter.
        monkeypatch: Per-test patch owner.

    Returns:
        Native mute-list query double.
    """
    bot.business = SimpleNamespace()
    bot.nim_account = "11"
    bot.config["moderation"].update(flood_enabled=False)
    reader = AsyncMock(return_value=set())
    monkeypatch.setattr(numeric_moderation, "muted_member_ids", reader)
    return reader


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [False, True])
async def test_numeric_rule_is_fast_fixed_mute_then_recall_without_warning(
    bot, monkeypatch, tmp_path, semantic
):
    reader = configure(bot, monkeypatch)
    bot.config["moderation"]["semantic"] = {"enabled": semantic}
    runtime = runtime_for(bot)
    try:
        payload = await ingest(bot, "100", "abc123456def")
        assert payload["_ingress_content"]["category"] == "consecutive_digits"
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        await drain_recalls(runtime)
        assert bot.calls == [("mute", "2", 1), ("recall", "100", "22")]
        reader.assert_awaited()
        await policy.handle(bot, "5", "100", payload)
        assert len(bot.calls) == 2
        with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
            row = db.execute(
                "SELECT category,status,warning FROM content_violations"
            ).fetchone()
            assert row == ("consecutive_digits", "muted", "accepted")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
            assert "progressive_warnings" not in tables
            assert "automatic_mutes" not in tables
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_numeric_existing_mute_only_recalls_without_claiming_new_mute(
    bot, monkeypatch, tmp_path, queued
):
    reader = configure(bot, monkeypatch)
    reader.return_value = {"2"}
    runtime = runtime_for(bot)
    try:
        payload = await ingest(bot, "100", "123456")
        await policy.handle(
            bot,
            "5",
            "100",
            payload,
            execution_check=lambda: None,
            recall_queue=runtime if queued else None,
        )
        await drain_recalls(runtime)
        assert bot.calls == [("recall", "100", "22")]
        with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations"
            ).fetchone() == ("recalled", "accepted")
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "unknown", "exception", "cancel"])
async def test_numeric_mute_failures_do_not_retry_or_skip_independent_recall(
    bot, monkeypatch, tmp_path, outcome
):
    configure(bot, monkeypatch)
    payload = await ingest(bot, "100", "123456")

    async def mute(*args, **kwargs):
        bot.calls.append(("mute", "2", 1))
        if outcome == "exception":
            raise TimeoutError()
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return {"status": outcome, "action": "mute"}

    bot.execute_moderation.side_effect = mute
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await policy.handle(bot, "5", "100", payload)
    else:
        assert await policy.handle(bot, "5", "100", payload)
    assert bot.calls == (
        [("mute", "2", 1)]
        if outcome == "cancel"
        else [("mute", "2", 1), ("recall", "100", "22")]
    )
    await policy.handle(bot, "5", "100", payload)
    bot.execute_moderation.assert_awaited_once()
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        row = db.execute("SELECT status,warning FROM content_violations").fetchone()
        assert row[0] != "muted"
        assert row[1] == ("none" if outcome == "cancel" else "accepted")


@pytest.mark.asyncio
async def test_numeric_native_read_failure_still_recalls(bot, monkeypatch):
    reader = configure(bot, monkeypatch)
    reader.side_effect = ProtocolError("mute_list_incomplete")
    payload = await ingest(bot, "100", "123456")
    assert await policy.handle(bot, "5", "100", payload)
    assert bot.calls == [("recall", "100", "22")]
    bot.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "category", "text", "group", "sender"])
async def test_numeric_requires_original_new_ingress_evidence(bot, monkeypatch, change):
    reader = configure(bot, monkeypatch)
    payload = await ingest(bot, "100", "123456")
    if change == "missing":
        payload.pop("_ingress_content")
    elif change == "text":
        payload["text"] = "654321"
    else:
        payload["_ingress_content"][change] = "different"
    assert await policy.handle(bot, "5", "100", payload)
    assert not bot.calls
    reader.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["admin", "white", "disabled", "stale", "permission", "account", "group"]
)
async def test_numeric_protections_remain_in_force(bot, monkeypatch, tmp_path, change):
    configure(bot, monkeypatch)
    runtime = runtime_for(bot)
    payload = await ingest(
        bot,
        "100",
        "123456",
        stamp=int((time.time() - 301) * 1000) if change == "stale" else None,
    )
    if change == "admin":
        bot.get_moderation_members.return_value["groupMemberInfo"][1]["groupRole"] = (
            "GROUP_ROLE_ADMIN"
        )
    elif change == "white":
        monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
        member_exemptions.set_exemption(
            "fixture",
            "1",
            "5",
            "2",
            "22",
            enabled=True,
            actor="server/test",
            reason="Fixture",
            guard=lambda: None,
        )
    elif change == "disabled":
        bot.config["moderation"]["advertising_enabled"] = False
    elif change == "permission":
        bot.config["moderation"]["permissions"] = {}
    elif change == "account":
        bot.account = "9"
    try:
        await runtime.execute(
            ("fixture", "1", "6" if change == "group" else "5", "2"),
            bot,
            "100",
            payload,
            None,
        )
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_numeric_final_http_check_preserves_newer_native_mute(bot, monkeypatch):
    reader = configure(bot, monkeypatch)
    reader.side_effect = [set(), {"2"}]
    payload = await ingest(bot, "100", "123456")

    async def submit(*args, action_check=None, **kwargs):
        bot.business._authorization_task = asyncio.current_task()
        await action_check()
        pytest.fail("Existing native mute must prevent submission")

    bot.execute_moderation.side_effect = submit
    assert await policy.handle(bot, "5", "100", payload)
    assert bot.calls == [("recall", "100", "22")]
    assert reader.await_count == 2


def test_numeric_sanctions_do_not_count_as_legacy_advertising_warning(tmp_path):
    from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore

    store = ReviewStore(tmp_path / "moderation.sqlite3")
    now = time.time()
    assert store.observe("1", "5", "100", "2", "consecutive_digits", now, 1)
    store.finish("1", "5", "100", "muted", "accepted")
    assert store.observe("1", "5", "101", "2", "external_promotion", now, 1)
    _, prior = store.claim("1", "5", "101", "2", 1, now)
    assert prior == (0, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("muted", [False, True])
async def test_native_numeric_checks_mute_list_only_at_final_http_fence(
    bot, monkeypatch, muted
):
    from astrbot.core.platform.sources.wangshangliao.business import BusinessClient

    reader = configure(bot, monkeypatch)
    bot.business = BusinessClient.__new__(BusinessClient)
    bot.business._authorization_task = None
    phases = []

    async def read(*args, **kwargs):
        phases.append(bot.business._authorization_task is asyncio.current_task())
        return {"2"} if muted else set()

    async def submit(*args, action_check=None, **kwargs):
        await action_check()
        bot.business._authorization_task = asyncio.current_task()
        try:
            await action_check()
        finally:
            bot.business._authorization_task = None
        bot.calls.append(("mute", "2", 1))
        return {"status": "accepted", "action": "mute", "submitted": True}

    reader.side_effect = read
    bot.execute_moderation.side_effect = submit
    payload = await ingest(bot, "100", "123456")
    assert await policy.handle(bot, "5", "100", payload)
    assert phases == [True]
    assert bot.calls == (
        [("recall", "100", "22")]
        if muted
        else [("mute", "2", 1), ("recall", "100", "22")]
    )
