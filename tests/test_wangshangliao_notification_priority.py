"""Offline notification isolation and game flood-counting regressions."""

import asyncio
import json
import sqlite3
import threading
import time
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.action_receipts import (
    ModerationActions,
)
from astrbot.builtin_stars.wangshangliao_moderation.review_store import (
    ReviewStore,
)
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import fresh_bot, ingest

bot = fresh_bot


def warning(bot, number):
    return ActionRequest.bind(
        bot,
        f"open-warning/1/5/{number}",
        "warning",
        5,
        member=2,
        expected_nim="22",
        text="Offline warning",
    )


def receipt(store, operation):
    with store.connect() as db:
        return db.execute(
            "SELECT status,submitted,error FROM moderation_action_audit "
            "WHERE operation=?",
            (operation,),
        ).fetchone()


@pytest.mark.asyncio
async def test_blocked_warning_does_not_block_same_member_mute(bot):
    bot.config["_open_groups"] = {
        "5": {"advertising": True, "flood": False, "keywords": ["fixture-ad"]}
    }
    bot.config["_open_native_roles"] = {"5": "GROUP_ROLE_ADMIN"}
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_send(*args, **kwargs):
        entered.set()
        await release.wait()
        return "accepted"

    bot.send_text = AsyncMock(side_effect=slow_send)
    runtime = runtime_for(bot, slots=1)
    try:
        await ingest(bot, "100", "fixture-ad")
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert len(runtime.warning_pending.get(("fixture", "1"), ())) == 1, (
            bot.diagnostics.emit.call_args_list
        )
        assert bot.send_text.await_count == 0
        await runtime.step()
        await asyncio.wait_for(entered.wait(), 1)
        started = time.monotonic()
        for mid in ("101", "102", "103"):
            await ingest(bot, mid, "fixture-ad")
            await runtime.step()
            await asyncio.wait_for(asyncio.gather(*runtime.workers.values()), 1)
        assert not release.is_set()
        assert ("mute", "2", 1) in bot.calls
        assert bot.send_text.await_count == 1
        assert bot.get_moderation_members.await_args_list[0].kwargs == {"max_age": 1}
        assert any(
            not call.kwargs for call in bot.get_moderation_members.await_args_list
        )
        print(
            "OFFLINE_BLOCKED_WARNING "
            + json.dumps(
                {
                    "mute_before_notification_release": True,
                    "additional_three_rules_ms": round(
                        (time.monotonic() - started) * 1000, 3
                    ),
                    "real_actions": 0,
                }
            )
        )
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_warning_queue_is_bounded_deduplicated_and_cancelled(bot, tmp_path):
    store = bot._review_store = ReviewStore(tmp_path / "moderation.sqlite3")
    runtime = runtime_for(bot, backlog=1)

    def guarded():
        return None

    adapter = ModerationActions(bot, "5")
    first, second = warning(bot, "100"), warning(bot, "101")
    assert await runtime.defer_warning(adapter, first, guarded)
    assert await runtime.defer_warning(adapter, first, guarded)
    assert not await runtime.defer_warning(adapter, second, guarded)
    assert len(runtime.warning_items) == 1
    assert receipt(store, first.operation) == ("queued", 0, "")
    assert receipt(store, second.operation) == (
        "rejected",
        0,
        "notification_queue_full",
    )
    await runtime.close()
    assert receipt(store, first.operation) == ("rejected", 0, "notification_cancelled")
    assert not runtime.warning_items and not runtime.warning_pending


@pytest.mark.asyncio
async def test_waiting_warning_is_admitted_while_another_member_is_busy(bot, tmp_path):
    bot._review_store = ReviewStore(tmp_path / "moderation.sqlite3")
    runtime = runtime_for(bot)
    release = asyncio.Event()
    worker = asyncio.create_task(release.wait())
    runtime.workers[("fixture", "1", "5", "different-member")] = worker
    bot.send_text = AsyncMock(return_value="accepted")
    request = warning(bot, "100")
    try:
        assert await runtime.defer_warning(
            ModerationActions(bot, "5"), request, lambda: None
        )
        await runtime.step()
        assert not runtime.warning_workers
        key = ("fixture", "1", request.operation)
        adapter, intent, guard, queued, store = runtime.warning_items[key]
        runtime.warning_items[key] = (adapter, intent, guard, queued - 2, store)
        await runtime.step()
        assert not worker.done()
        await asyncio.gather(*runtime.warning_workers.values())
        assert bot.send_text.await_count == 1
        assert not worker.done()
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_busy_account_notifications_are_paced_without_parallel_sends(
    bot, tmp_path
):
    bot._review_store = ReviewStore(tmp_path / "moderation.sqlite3")
    runtime = runtime_for(bot)
    release = asyncio.Event()
    worker = asyncio.create_task(release.wait())
    runtime.workers[("fixture", "1", "5", "different-member")] = worker
    bot.send_text = AsyncMock(return_value="accepted")
    try:
        for number in ("100", "101"):
            request = warning(bot, number)
            assert await runtime.defer_warning(
                ModerationActions(bot, "5"), request, lambda: None
            )
            key = ("fixture", "1", request.operation)
            adapter, intent, guard, queued, store = runtime.warning_items[key]
            runtime.warning_items[key] = (adapter, intent, guard, queued - 2, store)
        await runtime.step()
        await asyncio.gather(*runtime.warning_workers.values())
        await runtime.step()
        assert bot.send_text.await_count == 1
        assert len(runtime.warning_pending[("fixture", "1")]) == 1
        runtime.warning_started[("fixture", "1")] -= 2
        await runtime.step()
        await asyncio.gather(*runtime.warning_workers.values())
        assert bot.send_text.await_count == 2
        assert not worker.done()
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_cancel_during_warning_reservation_leaves_no_queued_receipt(
    bot, tmp_path
):
    store = bot._review_store = ReviewStore(tmp_path / "moderation.sqlite3")
    reserve = store.reserve_warning
    entered, release = threading.Event(), threading.Event()

    def slow_reserve(*args):
        entered.set()
        assert release.wait(2)
        return reserve(*args)

    store.reserve_warning = slow_reserve
    runtime = runtime_for(bot)
    request = warning(bot, "100")
    pending = asyncio.create_task(
        runtime.defer_warning(ModerationActions(bot, "5"), request, lambda: None)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert receipt(store, request.operation) == (
            "rejected",
            0,
            "notification_cancelled",
        )
        assert not runtime.warning_items and not runtime.warning_pending
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
async def test_warning_receipts_are_not_replayed_or_rewritten(bot, tmp_path, outcome):
    store = bot._review_store = ReviewStore(tmp_path / "moderation.sqlite3")
    bot.send_text = AsyncMock(return_value=outcome)
    adapter = ModerationActions(bot, "5")
    request = warning(bot, "100")
    runtime = runtime_for(bot)
    try:
        assert await runtime.defer_warning(adapter, request, lambda: None)
        await runtime.step()
        await asyncio.gather(*runtime.warning_workers.values())
        assert receipt(store, request.operation)[0] == outcome
        assert await runtime.defer_warning(adapter, request, lambda: None)
        await runtime.step()
        assert not runtime.warning_items
        assert bot.send_text.await_count == 1
    finally:
        await runtime.close()
    assert receipt(store, request.operation)[0] == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "revoked", "expired"])
async def test_queued_warning_rechecks_authority_and_age(bot, tmp_path, change):
    store = bot._review_store = ReviewStore(tmp_path / "moderation.sqlite3")
    bot.send_text = AsyncMock(return_value="accepted")
    runtime = runtime_for(bot)

    def guard():
        if change == "revoked":
            raise ProtocolError("moderation_permission")

    request = warning(bot, "100")
    try:
        assert await runtime.defer_warning(ModerationActions(bot, "5"), request, guard)
        if change == "disabled":
            bot.config["enable"] = False
        if change == "expired":
            key = ("fixture", "1", request.operation)
            adapter, request, guard, _, store = runtime.warning_items[key]
            runtime.warning_items[key] = (
                adapter,
                request,
                guard,
                time.monotonic() - 31,
                store,
            )
        await runtime.step()
        await asyncio.gather(*runtime.warning_workers.values(), return_exceptions=True)
        bot.send_text.assert_not_awaited()
        assert receipt(store, request.operation)[0] == "rejected"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_games_count_as_messages_without_same_text_or_image_inspection(bot):
    runtime = runtime_for(bot)
    try:
        for mid in ("100", "101", "102", "103", "104"):
            payload = await ingest(bot, mid, "", kind="game")
        assert payload["_ingress_flood"]["category"] == "message_flood"
        assert len(payload["_ingress_flood"]["messages"]) == 5
        assert payload.get("_ingress_content", {}).get("category") != "image_inspection"
        # Duplicate inbox IDs cannot create another flood observation.
        await ingest(bot, "104", "", kind="game")
        with sqlite3.connect(bot.ledger.path) as db:
            assert db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 5
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == [("recall", "104", "22")]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_game_event_never_enters_keyword_or_ai_reply(bot, tmp_path):
    from tests.test_wangshangliao_integrated_service import service
    from tests.test_wangshangliao_plain_commands import native_event

    value = service(tmp_path)
    value.rule_replies = type(
        "Replies", (), {"observe": AsyncMock(side_effect=AssertionError("keyword"))}
    )()
    value.group_ai = type(
        "AI", (), {"observe": AsyncMock(side_effect=AssertionError("ai"))}
    )()
    payload = await ingest(bot, "100", "", kind="game")
    event = native_event("")
    event.platform = bot
    event.message_obj.message_id = "100"
    event.set_extra("wangshangliao_payload", payload)
    await value._on_event(event)
    assert event.get_extra("_component_claimed")
    value.rule_replies.observe.assert_not_awaited()
    value.group_ai.observe.assert_not_awaited()
