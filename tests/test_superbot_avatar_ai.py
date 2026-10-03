"""No-network paid-task lifecycle, quota and credential isolation tests."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from telegram.error import BadRequest, RetryAfter, TimedOut
from test_superbot_avatar import avatar, confirmation, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.avatar_ai import ASSETS, MODEL, AIAvatar
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest_asyncio.fixture
async def ai(avatar, monkeypatch):  # noqa: F811
    clock = [1000.0]
    avatar.store.clock = lambda: clock[0]
    avatar.store.db.executescript("""
        CREATE TABLE mod_members(chat TEXT,uid TEXT,seen REAL);
        INSERT INTO mod_members VALUES('-1001','2',1000),('-1001','3',1000);
    """)
    avatar.runtime.bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status="member")
    )
    module = AIAvatar(avatar.runtime)
    monkeypatch.setattr(AIAvatar, "credential", lambda self: "fixture-secret")
    module.clock = clock
    yield module
    await module.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,is_member", [("left", False), ("kicked", False), ("restricted", False)]
)
async def test_paid_avatar_requires_current_membership(ai, status, is_member):
    ai.runtime.bot.get_chat_member.return_value = SimpleNamespace(
        status=status, is_member=is_member
    )
    p, t = confirmation(ai, update())
    with pytest.raises(Rejected, match="当前成员"):
        await ai.action(update(), p, t)
    assert ai.remaining(2) == 2
    assert not ai.store.db.execute("SELECT 1 FROM avatar_paid_budget").fetchone()
    assert (
        ai.store.db.execute(
            "SELECT used FROM callbacks WHERE token=?", (t,)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_group_disabled_during_membership_check_rejects(ai):
    async def disable(*args):
        ai.store.db.execute("UPDATE mod_groups SET enabled=0")
        return SimpleNamespace(status="member")

    ai.runtime.bot.get_chat_member.side_effect = disable
    p, t = confirmation(ai, update())
    with pytest.raises(Rejected):
        await ai.action(update(), p, t)
    assert not ai.store.db.execute("SELECT 1 FROM avatar_ai").fetchone()


@pytest.mark.asyncio
async def test_pending_avatar_does_not_submit_after_departure_or_verification_error(ai):
    calls = []
    await transport(
        ai,
        lambda request: (
            calls.append(request)
            or httpx.Response(
                202, json={"task_id": "imgtask_test", "status": "processing"}
            )
        ),
    )
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    ai.runtime.bot.get_chat_member.side_effect = TimeoutError()
    await ai.tick()
    assert not calls
    assert ai.store.db.execute("SELECT stage FROM avatar_ai").fetchone()[0] == "queued"
    assert ai.remaining(2) == 1
    ai.clock[0] += 61
    ai.runtime.bot.get_chat_member.side_effect = None
    ai.runtime.bot.get_chat_member.return_value = SimpleNamespace(status="left")
    await ai.tick()
    assert not calls
    assert ai.store.db.execute("SELECT stage FROM avatar_ai").fetchone()[0] == "failed"
    assert ai.remaining(2) == 2
    assert (
        ai.store.db.execute("SELECT count(*) FROM avatar_paid_budget").fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["avatar_daily_budget", "avatar_total_budget"])
async def test_budget_failure_is_atomic_and_failed_jobs_do_not_release_budget(ai, key):
    ai.runtime.config[key] = 1
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    ai.store.db.execute("UPDATE avatar_jobs SET status='failed'")
    ai.store.db.execute("UPDATE avatar_ai SET stage='failed'")
    p2, t2 = confirmation(ai, update())
    with pytest.raises(Rejected, match="预算已用完"):
        await ai.action(update(), p2, t2)
    assert ai.remaining(2) == 2
    assert (
        ai.store.db.execute(
            "SELECT used FROM callbacks WHERE token=?", (t2,)
        ).fetchone()[0]
        == 0
    )
    assert (
        ai.store.db.execute("SELECT count(*) FROM avatar_paid_budget").fetchone()[0]
        == 1
    )
    restored = AIAvatar(ai.runtime)
    try:
        assert (
            ai.store.db.execute("SELECT count(*) FROM avatar_paid_budget").fetchone()[0]
            == 1
        )
        with pytest.raises(Rejected, match="预算已用完"):
            await restored.action(update(), p2, t2)
    finally:
        await restored.client.aclose()


@pytest.mark.asyncio
async def test_budget_concurrent_confirmations_cannot_overbook(ai):
    ai.runtime.config["avatar_daily_budget"] = 1
    ai.store.db.execute("INSERT INTO mod_members VALUES('-1001','4',1000)")
    tasks = []
    for uid in (2, 3, 4):
        event = update(uid=uid, chat=uid)
        p, t = confirmation(ai, event)
        tasks.append(ai.action(event, p, t))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(result, Rejected) for result in results) == 2
    assert ai.store.db.execute("SELECT count(*) FROM avatar_jobs").fetchone()[0] == 1
    assert (
        ai.store.db.execute("SELECT count(*) FROM avatar_paid_budget").fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, False, "10", 100001])
async def test_budget_disabled_or_invalid_never_queues(ai, value):
    ai.runtime.config["avatar_daily_budget"] = value
    p, t = confirmation(ai, update())
    with pytest.raises(Rejected):
        await ai.action(update(), p, t)
    assert ai.remaining(2) == 2
    assert not ai.store.db.execute("SELECT 1 FROM avatar_paid_budget").fetchone()
    assert (
        ai.store.db.execute(
            "SELECT used FROM callbacks WHERE token=?", (t,)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_budget_rechecks_before_paid_submission_and_beijing_rollover(ai):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ai.clock[0] = datetime(
        2026, 10, 1, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")
    ).timestamp()
    ai.runtime.config["avatar_daily_budget"] = 1
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    calls = []
    await transport(
        ai,
        lambda request: (
            calls.append(request)
            or httpx.Response(
                202, json={"task_id": "imgtask_test", "status": "processing"}
            )
        ),
    )
    ai.runtime.config["avatar_total_budget"] = 0
    await ai.tick()
    assert not calls
    assert (
        ai.store.db.execute("SELECT error FROM avatar_ai").fetchone()[0]
        == "budget_paused"
    )
    ai.runtime.config["avatar_total_budget"] = 100
    ai.clock[0] += 120
    await ai.tick()
    assert len(calls) == 1
    assert (
        ai.store.db.execute("SELECT day FROM avatar_paid_budget").fetchone()[0]
        == "2026-10-02"
    )
    event = update(uid=3, chat=3)
    p2, t2 = confirmation(ai, event)
    with pytest.raises(Rejected, match="今日"):
        await ai.action(event, p2, t2)


async def transport(ai, handler):
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_queue_confirm_only_no_paid_preview_and_success(ai):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert MODEL.encode() in request.content
            assert "青鱼".encode() in request.content
            assert "下移约画面高度的4%".encode() in request.content
            assert "不得触碰或覆盖底部圆形金边".encode() in request.content
            return httpx.Response(
                202,
                json={"task_id": "imgtask_test", "status": "processing"},
                headers={"Retry-After": "3"},
            )
        if "/tasks/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "image_url": "https://www.sevnx.lol/v1/images/generated-assets/test.png",
                },
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, content=(ASSETS / "ai-example.png").read_bytes())

    await transport(ai, handler)
    event = update()
    await ai.action(event, {"action": "avatar_preview", "name": "青鱼"})
    assert not calls and ai.remaining(2) == 2
    p, t = confirmation(ai, event)
    await ai.action(event, p, t)
    with pytest.raises(Rejected):
        await ai.action(event, p, t)
    assert not calls and ai.remaining(2) == 1
    await ai.tick()
    assert len(calls) == 1
    ai.clock[0] += 4
    await ai.tick()
    assert len(calls) == 3
    await ai.delivery_tick()
    assert ai.store.db.execute("SELECT stage FROM avatar_ai").fetchone()[0] == "done"
    assert ai.runtime.bot.send_document.await_count == 1
    await ai.tick()
    await ai.delivery_tick()
    assert len(calls) == 3
    assert ai.remaining(2) == 1


@pytest.mark.asyncio
async def test_ambiguous_submission_retains_reservation_no_resubmit(ai):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    await transport(ai, handler)
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    await ai.tick()
    await ai.tick()
    assert len(calls) == 1
    assert ai.store.db.execute("SELECT stage FROM avatar_ai").fetchone()[0] == "unknown"
    assert ai.remaining(2) == 1
    p, t = confirmation(ai, update())
    with pytest.raises(Rejected):
        await ai.action(update(), p, t)
    restarted = AIAvatar(ai.runtime)
    assert restarted.remaining(2) == 1
    await restarted.client.aclose()


@pytest.mark.asyncio
async def test_restart_processing_polls_original_id(ai):
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    ai.store.db.execute(
        "UPDATE avatar_ai SET stage='processing',task='imgtask_resume',credential=?",
        (hashlib.sha256(b"fixture-secret").hexdigest(),),
    )
    restarted = AIAvatar(ai.runtime)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path.endswith("/imgtask_resume")
        return httpx.Response(200, json={"status": "processing"})

    await transport(restarted, handler)
    await restarted.tick()
    assert len(calls) == 1 and restarted.remaining(2) == 1
    await restarted.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 422, 429, 524])
async def test_submit_http_outcomes(ai, code):
    await transport(
        ai, lambda request: httpx.Response(code, json={"error": {"message": "fixture"}})
    )
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    await ai.tick()
    assert ai.remaining(2) == (1 if code == 524 else 2)
    assert ai.store.db.execute("SELECT stage FROM avatar_ai").fetchone()[0] == (
        "unknown" if code == 524 else "failed"
    )


@pytest.mark.asyncio
async def test_disabled_queue_no_charge_and_permission_isolation(ai):
    event = update()
    p, t = confirmation(ai, event)
    with pytest.raises(Rejected):
        await ai.action(update(uid=3), p, t)
    await ai.action(event, p, t)
    with ai.store.tx() as db:
        ai.store.put(db, "avatar_enabled", False)
    await transport(ai, lambda request: pytest.fail("Must not submit disabled job"))
    await ai.tick()
    assert ai.remaining(2) == 2


@pytest.mark.asyncio
async def test_untrusted_asset_never_requested_and_key_change_pauses(ai, monkeypatch):
    event = update()
    p, t = confirmation(ai, event)
    await ai.action(event, p, t)
    ai.store.db.execute(
        "UPDATE avatar_ai SET stage='processing',task='imgtask_resume',credential=?",
        (hashlib.sha256(b"fixture-secret").hexdigest(),),
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "www.sevnx.lol"
        return httpx.Response(
            200, json={"status": "completed", "image_url": "http://127.0.0.1/private"}
        )

    await transport(ai, handler)
    await ai.tick()
    assert len(calls) == 1 and ai.remaining(2) == 1
    ai.clock[0] += 61
    monkeypatch.setattr(AIAvatar, "credential", lambda self: "changed")
    await ai.tick()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_task_and_delivery_error_do_not_resubmit(ai):
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    ai.store.db.execute(
        "UPDATE avatar_ai SET stage='processing',task='imgtask_failed',credential=?",
        (hashlib.sha256(b"fixture-secret").hexdigest(),),
    )
    await transport(ai, lambda request: httpx.Response(200, json={"status": "failed"}))
    await ai.tick()
    assert ai.remaining(2) == 2


@pytest.mark.asyncio
async def test_sending_restart_and_atomic_file_recovery(ai):
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    row = ai.store.db.execute("SELECT * FROM avatar_jobs").fetchone()
    (ai.root / (row["id"] + ".png")).write_bytes(
        (ASSETS / "ai-example.png").read_bytes()
    )
    ai.store.db.execute("UPDATE avatar_ai SET stage='submitting'")
    ai.store.db.execute("UPDATE avatar_jobs SET delivery='sending'")
    restarted = AIAvatar(ai.runtime)
    result = ai.store.db.execute("SELECT * FROM avatar_jobs").fetchone()
    assert result["status"] == "ready" and result["delivery"] == "unknown"
    assert restarted.remaining(2) == 1
    await restarted.client.aclose()


@pytest.mark.asyncio
async def test_ephemeral_menu_progress_and_public_image(ai):
    event = update(chat=-1001)
    ai.runtime.bot.send_message.return_value = SimpleNamespace(
        message_id=0, api_kwargs={"ephemeral_message_id": "42"}
    )
    ai.runtime.bot._post = AsyncMock()
    await ai.action(event, {"action": "avatar_home"})
    assert ai.runtime.bot.send_photo.await_args.kwargs["api_kwargs"] == {
        "ephemeral_message_parameters": {"receiver_user_id": 2}
    }
    p, t = confirmation(ai, event)
    await ai.action(event, p, t)
    ai.clock[0] += 10
    await ai.progress_tick()
    args = ai.runtime.bot._post.await_args
    assert args.args[0] == "editEphemeralMessageText"
    assert args.kwargs["data"]["receiver_user_id"] == 2
    assert "00:10" in args.kwargs["data"]["text"]
    row = ai.store.db.execute("SELECT * FROM avatar_jobs").fetchone()
    (ai.root / (row["id"] + ".png")).write_bytes(
        (ASSETS / "ai-example.png").read_bytes()
    )
    ai.store.db.execute("UPDATE avatar_jobs SET status='ready'")
    await ai.deliver(event, row["id"])
    assert "api_kwargs" not in ai.runtime.bot.send_photo.await_args.kwargs
    assert ai.runtime.bot.send_document.await_count == 0


@pytest.mark.asyncio
async def test_progress_private_rate_limit_restart_and_completion(ai):
    ai.runtime.bot.edit_message_text = AsyncMock(side_effect=RetryAfter(20))
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    await ai.progress_tick()
    row = ai.store.db.execute("SELECT * FROM avatar_progress").fetchone()
    assert row["next"] == ai.clock[0] + 20
    await ai.progress_tick()
    assert ai.runtime.bot.edit_message_text.await_count == 1
    ai.clock[0] += 20
    restarted = AIAvatar(ai.runtime)
    try:
        ai.runtime.bot.edit_message_text.side_effect = BadRequest(
            "Message is not modified"
        )
        ai.store.db.execute("UPDATE avatar_ai SET stage='done'")
        ai.store.db.execute("UPDATE avatar_jobs SET delivery='sent'")
        await restarted.progress_tick()
        assert (
            ai.store.db.execute("SELECT status FROM avatar_progress").fetchone()[0]
            == "complete"
        )
        await restarted.progress_tick()
        assert ai.runtime.bot.edit_message_text.await_count == 2
    finally:
        await restarted.client.aclose()


@pytest.mark.asyncio
async def test_progress_timeout_then_expired_never_falls_back_public(ai):
    event = update(chat=-1001)
    ai.runtime.bot.send_message.return_value = SimpleNamespace(
        message_id=0, api_kwargs={"ephemeral_message_id": 42}
    )
    ai.runtime.bot._post = AsyncMock(side_effect=TimedOut())
    ai.runtime.bot.edit_message_text = AsyncMock()
    p, t = confirmation(ai, event)
    await ai.action(event, p, t)
    await ai.progress_tick()
    assert (
        ai.store.db.execute("SELECT next FROM avatar_progress").fetchone()[0]
        == ai.clock[0] + 30
    )
    ai.clock[0] += 30
    ai.runtime.bot._post.side_effect = BadRequest("Message to edit not found")
    await ai.progress_tick()
    assert (
        ai.store.db.execute("SELECT status FROM avatar_progress").fetchone()[0]
        == "stopped"
    )
    assert ai.runtime.bot.send_message.await_count == 1
    assert ai.runtime.bot.edit_message_text.await_count == 0
    assert ai.remaining(2) == 1


@pytest.mark.asyncio
async def test_group_missing_ephemeral_id_does_not_track_public_message(ai):
    event = update(chat=-1001)
    p, t = confirmation(ai, event)
    await ai.action(event, p, t)
    assert (
        ai.store.db.execute("SELECT count(*) FROM avatar_progress").fetchone()[0] == 0
    )
    assert ai.runtime.bot.send_message.await_args.kwargs["api_kwargs"] == {
        "ephemeral_message_parameters": {"receiver_user_id": 2}
    }


@pytest.mark.asyncio
async def test_group_custom_name_requires_matching_ephemeral_reply(ai):
    event = update(chat=-1001)
    ai.runtime.bot.send_message.return_value = SimpleNamespace(
        message_id=0, api_kwargs={"ephemeral_message_id": 42}
    )
    await ai.action(event, {"action": "avatar_custom"})
    reply = SimpleNamespace(message_id=0, api_kwargs={"ephemeral_message_id": 43})
    assert not await ai.message(update(chat=-1001, reply=reply), "")
    reply.api_kwargs["ephemeral_message_id"] = 42
    assert not await ai.message(update(uid=3, chat=-1001, reply=reply), "")
    assert await ai.message(update(chat=-1001, reply=reply), "")
    assert ai.store.db.execute("SELECT count(*) FROM avatar_jobs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_slow_provider_does_not_block_delivery_or_allow_concurrent_duplicate(ai):
    p, t = confirmation(ai, update())
    await ai.action(update(), p, t)
    row = ai.store.db.execute("SELECT * FROM avatar_jobs").fetchone()
    (ai.root / (row["id"] + ".png")).write_bytes(
        (ASSETS / "ai-example.png").read_bytes()
    )
    ai.store.db.execute("UPDATE avatar_jobs SET status='ready'")
    ai.store.db.execute("UPDATE avatar_ai SET stage='done'")
    p, t = confirmation(ai, update(uid=3, chat=3))
    await ai.action(update(uid=3, chat=3), p, t)
    entered, release, sending, finish = (asyncio.Event() for _ in range(4))

    async def slow_advance(row):
        entered.set()
        await release.wait()

    async def slow_send(**kwargs):
        sending.set()
        await finish.wait()
        return SimpleNamespace(message_id=88)

    ai.advance = slow_advance
    ai.runtime.bot.send_document.side_effect = slow_send
    worker = asyncio.create_task(ai.tick())
    delivery = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        delivery = asyncio.create_task(ai.delivery_tick())
        await asyncio.wait_for(sending.wait(), 1)
        assert not worker.done()
        with pytest.raises(Rejected, match="正在发送"):
            await ai.deliver(update(), row["id"], explicit=True)
        await ai.delivery_tick()
        finish.set()
        await delivery
        await ai.delivery_tick()
        assert ai.runtime.bot.send_document.await_count == 1
        assert ai.remaining(2) == 1
    finally:
        release.set()
        finish.set()
        await worker
        if delivery:
            await delivery


@pytest.mark.asyncio
async def test_independent_delivery_unknown_not_replayed_and_disabled_group(ai):
    event = update(chat=-1001)
    p, t = confirmation(ai, event)
    await ai.action(event, p, t)
    row = ai.store.db.execute("SELECT * FROM avatar_jobs").fetchone()
    (ai.root / (row["id"] + ".png")).write_bytes(
        (ASSETS / "ai-example.png").read_bytes()
    )
    ai.store.db.execute("UPDATE avatar_jobs SET status='ready'")
    ai.store.db.execute("UPDATE avatar_ai SET stage='done'")
    ai.runtime.bot.send_photo.side_effect = TimedOut()
    with pytest.raises(TimedOut):
        await ai.delivery_tick()
    await ai.delivery_tick()
    assert ai.runtime.bot.send_photo.await_count == 1
    assert (
        ai.store.db.execute("SELECT delivery FROM avatar_jobs").fetchone()[0]
        == "unknown"
    )
    ai.store.db.execute("UPDATE avatar_jobs SET delivery='pending'")
    ai.store.db.execute("UPDATE mod_groups SET enabled=0")
    await ai.delivery_tick()
    assert ai.runtime.bot.send_photo.await_count == 1
    assert (
        ai.store.db.execute("SELECT delivery FROM avatar_jobs").fetchone()[0]
        == "failed_delivery"
    )
    assert ai.remaining(2) == 1
