"""Offline moderation audit regressions, using temporary databases exclusively."""

import asyncio
import copy
import inspect
import json
import socket
import time
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import (
    activities,
    activity_store,
    cards,
    content_rules,
    policy,
    rankings,
    schedules,
    semantic,
)
from astrbot.builtin_stars.wangshangliao_moderation.command_menu import (
    CommandMenu,
    Session,
)
from astrbot.builtin_stars.wangshangliao_moderation.flood import (
    FloodRuntime,
    FloodWindows,
)
from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao import (
    automatic,
    progressive,
    schedule_store,
)
from astrbot.core.platform.sources.wangshangliao.actions import dispatch
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture(autouse=True)
def offline_paths(tmp_path, monkeypatch):
    """Isolate storage and fail immediately on an attempted network connection."""
    for module in (
        activities,
        activity_store,
        automatic,
        cards,
        content_rules,
        policy,
        rankings,
        schedule_store,
        semantic,
    ):
        monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Network connections are forbidden in this audit")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def member(uid, role="GROUP_ROLE_MEMBER"):
    """Build a uniquely mapped synthetic member, never a production identity."""
    return {
        "userId": str(uid),
        "nimId": str(100 + int(uid)),
        "groupRole": role,
        "accountState": "ACCOUNT_STATE_GOOD",
        "groupMemberNick": "Member " + str(uid),
    }


@pytest_asyncio.fixture
async def bot(tmp_path):
    """Provide real temporary inbox storage behind a fake action boundary."""
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    roster = [member("1", "GROUP_ROLE_ADMIN"), member("2"), member("3"), member("9")]
    adapter = SimpleNamespace(
        account="1",
        nim_account="101",
        config={
            "id": "moderation-audit-20261019",
            "type": "wangshangliao",
            "enable": True,
            "_business_active": True,
            "_business_epoch": 1,
            "_business_revision": "audit-1",
            "enabled_groups": ["5"],
            "proactive_send": {"enabled": True, "targets": ["5"]},
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "recall_enabled": True,
                "progressive_mute": True,
                "content_rules_since": time.time() - 60,
                "permissions": {
                    "5": ["mute", "recall", "rename", "mute_all", "unmute_all"]
                },
                "semantic": {"enabled": True, "provider_id": "offline"},
            },
        },
        stopping=asyncio.Event(),
        connection_state="online",
        groups={"5": "105"},
        members={"5": {m["userId"]: m["nimId"] for m in roster}},
        ledger=ledger,
        diagnostics=SimpleNamespace(emit=Mock()),
        get_moderation_members=AsyncMock(
            return_value={"complete": True, "groupMemberInfo": roster}
        ),
        send_reply_text=AsyncMock(return_value="accepted"),
    )

    async def execute(request, *, authorize=None):
        if authorize is not None:
            checked = authorize()
            if inspect.isawaitable(checked):
                await checked
        return {"status": "accepted", "action": request.action}

    adapter.execute_action = AsyncMock(side_effect=execute)
    try:
        yield adapter
    finally:
        await ledger.close()


async def ingest_burst(bot, count=5):
    """Commit trusted flood evidence together with each temporary inbox row."""
    windows = FloodWindows()
    payload = None
    for index in range(count):
        mid = str(1000 + index)
        payload = {
            "sender": "2",
            "text": "same repeated message",
            "recall_route": {"time": str(time.time() * 1000), "peer": "102"},
        }

        def observe(envelope, received_at):
            evidence = windows.observe("1", "5", mid, envelope, received_at)
            evidence["revision"] = bot.config["_business_revision"]
            envelope["_ingress_flood"] = evidence

        await bot.ledger.ingest("1", "5", mid, payload, observe=observe)
    async with bot.ledger.db.execute(
        "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
        ("1", "5", mid),
    ) as cursor:
        payload = json.loads((await cursor.fetchone())[0])
    return mid, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError, ProtocolError])
async def test_flood_mute_exception_does_not_abandon_reserved_recalls(
    bot, monkeypatch, failure
):
    mid, payload = await ingest_burst(bot)
    mute = AsyncMock(side_effect=failure("offline synthetic failure"))
    monkeypatch.setattr(automatic, "mute_and_escalate", mute)
    assert await policy.handle(
        bot,
        "5",
        mid,
        payload,
        assessment={
            "decision": "violation",
            "category": "message_flood",
            "message_id": mid,
        },
    )
    mute.assert_awaited_once()
    assert [call.args[0].message for call in bot.execute_action.await_args_list] == [
        str(1000 + index) for index in range(5)
    ]
    assert await policy.handle(
        bot,
        "5",
        mid,
        payload,
        assessment={
            "decision": "violation",
            "category": "message_flood",
            "message_id": mid,
        },
    )
    assert mute.await_count == 1 and bot.execute_action.await_count == 5
    with bot._review_store.connect() as db:
        assert db.execute("SELECT mute_status FROM flood_batches").fetchone() == (
            "unknown",
        )
        assert db.execute(
            "SELECT COUNT(*) FROM flood_recalls WHERE status='accepted'"
        ).fetchone() == (5,)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ProtocolError, asyncio.CancelledError])
async def test_flood_does_not_continue_after_revocation_or_cancellation(
    bot, monkeypatch, failure
):
    mid, payload = await ingest_burst(bot)

    async def revoke(*args, **kwargs):
        bot.config["_business_active"] = False
        raise failure("synthetic revocation")

    monkeypatch.setattr(automatic, "mute_and_escalate", revoke)
    operation = policy.handle(
        bot,
        "5",
        mid,
        payload,
        assessment={
            "decision": "violation",
            "category": "message_flood",
            "message_id": mid,
        },
    )
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        await operation
    bot.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_flood_pending_is_bounded_and_close_cancels_worker(bot):
    await ingest_burst(bot, 12)
    runtime = FloodRuntime(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[bot])),
        slots=1,
        backlog=2,
    )
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    runtime.execute = blocked
    try:
        await runtime.step()
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(5):
            await runtime.step()
            assert len(runtime.workers) <= 1
            assert sum(map(len, runtime.pending.values())) <= 2
        workers = list(runtime.workers.values())
    finally:
        await runtime.close()
    assert all(task.cancelled() for task in workers)
    assert not runtime.pending and not runtime.workers and not runtime.cursors


def revoke_semantic(bot, change):
    """Apply a single real policy or identity change at an awaited boundary."""
    if change == "account":
        bot.account = "4"
    elif change == "epoch":
        bot.config["_business_epoch"] += 1
    elif change == "active":
        bot.config["_business_active"] = False
    elif change == "native":
        bot.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    elif change == "semantic":
        bot.config["moderation"]["semantic"]["enabled"] = False
    else:
        bot.config["moderation"]["permissions"]["5"] = []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "epoch", "active", "native", "semantic", "grant"]
)
@pytest.mark.parametrize("phase", ["roster", "provider", "response"])
async def test_semantic_rechecks_authority_across_awaits(
    bot, monkeypatch, change, phase
):
    bot.config.update(
        _open_groups={"5": {}}, _open_native_roles={"5": "GROUP_ROLE_ADMIN"}
    )
    payload = {
        "sender": "2",
        "text": "a synthetic current message",
        "recall_route": {"time": str(time.time() * 1000)},
    }
    await bot.ledger.ingest("1", "5", "current", payload)
    event = SimpleNamespace(
        platform=bot,
        get_group_id=lambda: "5",
        unified_msg_origin="audit:GroupMessage:1/5",
        message_obj=SimpleNamespace(message_id="current"),
        get_extra=lambda _: payload,
    )
    result = {
        "decision": "allow",
        "category": "none",
        "message_id": "current",
        "evidence": [],
        "reason": "offline synthetic result",
    }

    async def generate(**kwargs):
        if phase == "response":
            revoke_semantic(bot, change)
        return SimpleNamespace(completion_text=json.dumps(result))

    context = SimpleNamespace(llm_generate=AsyncMock(side_effect=generate))
    if phase == "roster":
        roster = copy.deepcopy(bot.get_moderation_members.return_value)

        async def directory(group):
            revoke_semantic(bot, change)
            return roster

        bot.get_moderation_members.side_effect = directory
    elif phase == "provider":
        bot.config["moderation"]["semantic"]["provider_id"] = ""

        async def provider(origin):
            revoke_semantic(bot, change)
            return SimpleNamespace(meta=lambda: SimpleNamespace(id="offline"))

        context.get_using_provider_async = AsyncMock(side_effect=provider)
    decision = await semantic.assess(context, event)
    assert decision == {"decision": "skip"}
    assert context.llm_generate.await_count == (1 if phase == "response" else 0)
    store = getattr(bot, "_review_store", None)
    if store is not None:
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM semantic_reviews").fetchone() == (
                0,
            )


@pytest.mark.asyncio
async def test_semantic_overload_has_no_unbounded_semaphore_waiters(bot, monkeypatch):
    entered, release = 0, asyncio.Event()

    async def blocked(*args, **kwargs):
        nonlocal entered
        entered += 1
        await release.wait()
        return {"decision": "allow"}

    monkeypatch.setattr(semantic, "_assess", blocked)
    event = SimpleNamespace(
        platform=bot,
        get_group_id=lambda: "5",
        message_obj=SimpleNamespace(message_id="offline"),
    )
    workers = [asyncio.create_task(semantic.assess(None, event)) for _ in range(4)]
    try:
        for _ in range(20):
            if entered == 4:
                break
            await asyncio.sleep(0)
        assert entered == 4
        result = await asyncio.wait_for(semantic.assess(None, event), 0.2)
        assert result["decision"] == "fallback" and result["reason"] == "review_busy"
        assert entered == 4
    finally:
        release.set()
        await asyncio.gather(*workers)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preflight", "submission"])
async def test_departed_lottery_creator_cannot_publish_scheduled_draw(bot, phase):
    bot.config.update(
        _open_integrated=True,
        _open_groups={"5": {}},
        _open_managers={"5": ["9"]},
        _open_native_roles={"5": "GROUP_ROLE_ADMIN"},
    )
    bot.get_moderation_members.return_value["groupMemberInfo"] = [
        member("1", "GROUP_ROLE_ADMIN"),
        member("2"),
    ]
    submitted = []
    if phase == "submission":
        bot.get_moderation_members.return_value["groupMemberInfo"].append(member("9"))

        async def send(*args, authorize, **kwargs):
            bot.get_moderation_members.return_value["groupMemberInfo"] = [
                member("1", "GROUP_ROLE_ADMIN"),
                member("2"),
            ]
            checked = authorize()
            if inspect.isawaitable(checked):
                await checked
            submitted.append(args)
            return "accepted"

        bot.send_reply_text.side_effect = send
    with closing(activity_store.database(bot)) as db, db:
        db.execute(
            "INSERT INTO lotteries(id,command,account,group_id,owner,session,owner_name,"
            "prize,winners,duration,capacity,invite_gate,contact,status,ends,created)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "offline-lottery",
                "offline-command",
                "1",
                "5",
                "9",
                "private:9",
                "Creator",
                "Synthetic prize",
                1,
                60,
                15,
                0,
                "Creator",
                "open",
                time.time() - 1,
                time.time() - 61,
            ),
        )
        db.execute(
            "INSERT INTO lottery_entries VALUES(?,?,?,?,?)",
            ("offline-lottery", "2", "102", "Entrant", time.time() - 2),
        )
    service = activities.GroupActivities(SimpleNamespace(get_config=lambda _: {}))
    await service.tick(bot)
    assert submitted == []
    if phase == "preflight":
        bot.send_reply_text.assert_not_awaited()
    with closing(activity_store.database(bot)) as db:
        row = db.execute("SELECT status,results,total FROM lotteries").fetchone()
        if phase == "preflight":
            assert tuple(row) == ("blocked", "[]", 0)
        else:
            assert row["status"] == "result_unknown"
            assert len(json.loads(row["results"])) == row["total"] == 1
    await service.tick(bot)
    assert bot.send_reply_text.await_count == (1 if phase == "submission" else 0)


@pytest.mark.asyncio
async def test_schedule_checks_creator_membership_at_submission(bot):
    key = (bot.config["id"], bot.account, "5")
    management = SimpleNamespace(
        key=lambda adapter, group: (adapter.config["id"], adapter.account, group),
        assignments={key: ("9",)},
        versions={key: 1},
        allowed=lambda event, group: True,
    )
    scheduler = schedules.GroupSchedules(SimpleNamespace(get_config=lambda _: {}))
    scheduler.management = management
    event = SimpleNamespace(
        platform=bot,
        get_sender_id=lambda: "9",
        unified_msg_origin="audit:private:9",
        is_private_chat=lambda: True,
        is_admin=lambda: False,
        message_obj=SimpleNamespace(message_id="preview"),
    )
    bot.config.update(
        _open_groups={"5": {}}, _open_native_roles={"5": "GROUP_ROLE_ADMIN"}
    )
    token = (await scheduler.command(event, "5", "定时禁言", "23:00 08:00")).split()[-1]
    event.message_obj.message_id = "confirmation"
    assert "已启用" in await scheduler.command(event, "5", "确认定时", token)
    await scheduler.tick(bot)
    with closing(schedule_store.database(bot)) as db, db:
        db.execute("UPDATE schedules SET next_at=?", (time.time() - 1,))
    submitted = []

    async def native(operation, action, group, *, scheduled_check):
        bot.get_moderation_members.return_value["groupMemberInfo"] = [
            member("1", "GROUP_ROLE_ADMIN"),
            member("2"),
        ]
        await scheduled_check()
        submitted.append(action)
        return {"status": "accepted"}

    bot.execute_moderation = AsyncMock(side_effect=native)
    bot.execute_action = lambda request, **kwargs: dispatch(bot, request, **kwargs)
    await scheduler.tick(bot)
    assert submitted == []
    with closing(schedule_store.database(bot)) as db:
        assert db.execute("SELECT status,error FROM schedules").fetchone() == (
            "paused",
            "schedule_owner_revoked",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["peer", "account", "origin", "claimed"])
async def test_menu_confirmation_cannot_be_taken_over(bot, scope):
    service = SimpleNamespace(
        integrated=True,
        recognize_command=recognize_command,
        admit_command=lambda event: True,
        menu_command=AsyncMock(return_value="offline result"),
    )
    menu = CommandMenu(service)
    actor = ActorIdentity("1", "9", "109", "9")
    event = SimpleNamespace(
        platform=bot,
        actor_identity=actor,
        get_sender_id=lambda: "9",
        get_group_id=lambda: "5",
        unified_msg_origin="audit:GroupMessage:1/5",
        message_obj=SimpleNamespace(message_id="confirmation"),
    )
    original = menu.key(event)
    menu.sessions[original] = Session(
        time.monotonic() + 600, confirm="关闭防刷屏", preview_message="preview"
    )
    if scope == "peer":
        event.actor_identity = replace(
            actor, peer="119", claimed_uid="19", verified_uid="19"
        )
        event.get_sender_id = lambda: "19"
    elif scope == "account":
        event.platform = SimpleNamespace(account="4")
        event.actor_identity = replace(actor, account="4")
    elif scope == "origin":
        event.unified_msg_origin = "audit:GroupMessage:1/6"
        event.get_group_id = lambda: "6"
    else:
        event.actor_identity = replace(actor, claimed_uid="19")
    assert not await menu.handle(event, "1")
    service.menu_command.assert_not_awaited()
    assert menu.sessions[original].confirm == "关闭防刷屏"


@pytest.mark.asyncio
async def test_card_stop_preserves_an_inflight_receipt_and_skips_remaining_items(bot):
    jobs = cards.CardJobs()
    job = await jobs.preview(bot, "5", "dashboard/offline")
    entered, release = asyncio.Event(), asyncio.Event()

    async def sent(request, *, authorize):
        await authorize()
        entered.set()
        await release.wait()
        return {"status": "accepted", "action": request.action}

    bot.execute_action.side_effect = sent
    try:
        await jobs.start(bot, job["id"], job["owner"])
        await asyncio.wait_for(entered.wait(), 2)
        await jobs.stop(bot, job["id"], job["owner"])
        release.set()
        await asyncio.gather(*list(jobs.tasks))
        stopped = await jobs.status(bot, job["id"], job["owner"])
        assert stopped["state"] == "stopped" and stopped["stop_requested"]
        assert [item["state"] for item in stopped["items"]] == [
            "accepted",
            "pending",
            "pending",
        ]
        assert bot.execute_action.await_count == 1
    finally:
        release.set()
        await jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["accepted", "verified", "rejected", "unknown"])
async def test_progression_counts_only_accepted_mute_receipts(bot, receipt):
    for index in range(3):
        result = await progressive.enforce(bot, "5", "2", str(2000 + index))
        assert result["action"] == "recall"
    bot.execute_action.reset_mock()

    async def action(request, *, authorize):
        await authorize()
        return {"status": receipt, "action": "mute"}

    bot.execute_action.side_effect = action
    first = await automatic.mute_and_escalate(bot, "offline/1", "5", "2", "2003")
    second = await automatic.mute_and_escalate(bot, "offline/2", "5", "2", "2004")
    requests = [call.args[0] for call in bot.execute_action.await_args_list]
    assert first["mute_count"] == (1 if receipt in {"accepted", "verified"} else 0)
    assert [request.minutes for request in requests] == (
        [5, 15]
        if receipt in {"accepted", "verified"}
        else [5, 5]
        if receipt == "rejected"
        else [5]
    )
    assert second["status"] == receipt


@pytest.mark.parametrize("receipt", ["accepted", "verified", "rejected", "unknown"])
def test_warning_unknown_is_not_a_successful_prior_warning(tmp_path, receipt):
    store = ReviewStore(tmp_path / "warnings.sqlite3")
    store.observe("1", "5", "1", "2", "external_promotion", 100, 1)
    store.claim("1", "5", "1", "2", 1, 100)
    store.finish("1", "5", "1", "warned", receipt)
    store.observe("1", "5", "2", "2", "external_promotion", 101, 1)
    _, prior = store.claim("1", "5", "2", "2", 1, 101)
    assert prior == (1, int(receipt in {"accepted", "verified"}))


@pytest.mark.asyncio
async def test_rankings_exclude_the_successfully_recalled_warning_stage(bot, tmp_path):
    now = time.time()
    for mid, text, stamp in (
        ("3001", "synthetic advertisement", now - 100),
        ("3002", "ordinary discussion", now - 50),
    ):
        await bot.ledger.ingest(
            "1",
            "5",
            mid,
            {"sender": "2", "text": text, "recall_route": {"time": stamp * 1000}},
        )
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    store.observe("1", "5", "3001", "2", "external_promotion", now, now - 200)
    store.finish("1", "5", "3001", "recalled", "accepted")
    result = await rankings.DailyRankings().read(bot, "5", "9")
    assert "1. 2 - 1 条" in result
    assert "2 条" not in result
