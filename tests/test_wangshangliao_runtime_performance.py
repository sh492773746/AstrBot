"""Offline concurrency, ingress evidence and bounded dispatch acceptance."""

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.concurrency import AIRuntime
from astrbot.builtin_stars.wangshangliao_moderation.flood import FloodWindows
from astrbot.core.platform.sources.wangshangliao import adapter as native
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


def envelope(sender, stamp, text="hello"):
    return {
        "sender": sender,
        "name": sender,
        "text": text,
        "mentions": [],
        "created_at": int(stamp * 1000),
        "recall_route": {"time": str(int(stamp * 1000)), "peer": sender},
    }


@pytest.mark.asyncio
async def test_ingress_observed_once_durable_before_processing(tmp_path):
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    seen = []

    def observe(payload, received_at):
        seen.append(received_at)
        payload["_ingress_flood"] = {"category": "message_flood"}

    try:
        results = await asyncio.gather(
            *(
                ledger.ingest(
                    "1", "5", "m", envelope("2", time.time()), observe=observe
                )
                for _ in range(20)
            )
        )
        assert sum(results) == len(seen) == 1
        async with ledger.db.execute("SELECT sender,payload FROM inbox") as cursor:
            rows = await cursor.fetchall()
        assert rows[0][0] == "2"
        assert json.loads(rows[0][1])["_ingress_flood"]["category"] == "message_flood"
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_duplicate_notification_cannot_resurrect_completed_message(tmp_path):
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    try:
        assert await ledger.ingest(
            "1", "5", "m", {"kind": "team_invite"}, state="invite_notification"
        )
        assert not await ledger.ingest(
            "1", "5", "m", {"text": "replayed"}, state="pending"
        )
        async with ledger.db.execute(
            "SELECT state,payload,completed_at FROM inbox"
        ) as cursor:
            state, body, completed = await cursor.fetchone()
        assert state == "invite_notification" and json.loads(body) == {
            "kind": "team_invite"
        }
        assert completed is not None
    finally:
        await ledger.close()


def test_diagnostics_percentiles_are_bounded_and_contain_no_identifiers():
    from astrbot.core.platform.sources.wangshangliao.diagnostics import Diagnostics

    diagnostics = Diagnostics("test")
    for i in range(1000):
        diagnostics.observe("queue_wait", i / 1000)
    diagnostics.observe("arbitrary_user_supplied_label", 1)
    snapshot = diagnostics.snapshot()
    assert set(snapshot["timings"]) == {"queue_wait"}
    assert snapshot["timings"]["queue_wait"]["count"] == 1000
    assert snapshot["timings"]["queue_wait"]["samples"] == 512
    assert snapshot["timings"]["queue_wait"]["p95_ms"] < 1000


def test_ingress_window_ignores_reconnect_history_and_survives_restore():
    windows = FloodWindows(max_members=2)
    rows = []
    base = 1_790_000_000
    for i in range(2):
        stamp = base + i
        payload = envelope("2", stamp, str(i))
        rows.append(("5", str(i), payload, stamp))
        assert not windows.observe("1", "5", str(i), payload, stamp)["category"]
    recovered = FloodWindows()
    recovered.restore("1", rows, now=base + 2)
    assert (
        recovered.observe("1", "5", "2", envelope("2", base + 2), base + 2)["category"]
        == "message_flood"
    )
    assert not recovered.observe("1", "5", "old", envelope("2", base), base + 20)[
        "live"
    ]
    assert not recovered.observe(
        "1", "5", "future", envelope("2", base + 1000), base + 20
    )["live"]
    for uid in ("3", "4", "5"):
        windows.observe("1", "5", uid, envelope(uid, base + 10), base + 10)
    assert len(windows.windows) == 2


@pytest.mark.asyncio
async def test_complete_member_snapshots_coalesce_without_weakening_force(
    tmp_path, monkeypatch, wsl_active_policy
):
    bot = native.WangshangliaoAdapter(
        {"id": "cache", "account_id": "1", "enabled_groups": ["5"]}, {}, asyncio.Queue()
    )
    bot.connection_state = "online"
    bot.business = object()
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*_):
        entered.set()
        await release.wait()
        return {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "22"}]}

    fetcher = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(native, "member_directory", fetcher)
    jobs = [asyncio.create_task(bot.get_moderation_members("5")) for _ in range(4)]
    await entered.wait()
    release.set()
    snapshots = await asyncio.gather(*jobs)
    assert fetcher.await_count == 1
    snapshots[0]["groupMemberInfo"].clear()
    assert (await bot.get_cached_members("5"))["groupMemberInfo"]
    assert fetcher.await_count == 1
    await bot.get_moderation_members("5")
    assert fetcher.await_count == 2
    fetcher.side_effect = native.wire.ProtocolError("incomplete")
    with pytest.raises(native.wire.ProtocolError):
        await bot.get_moderation_members("5")
    assert bot.group_directory_state["5"]["complete"] is False
    assert bot.members["5"] == {"2": "22"}
    assert "5" not in bot.member_snapshots


@pytest.mark.asyncio
async def test_customer_limit_session_serialization_and_revocation():
    runtime = AIRuntime()
    platform = SimpleNamespace(
        account="1",
        config={"id": "bot", "_business_active": True, "_business_epoch": 1},
    )

    def event(session):
        return SimpleNamespace(
            platform=platform,
            unified_msg_origin=session,
            policy_epoch=1,
            processing_completion=asyncio.get_running_loop().create_future(),
        )

    first, same, other, third = (event(s) for s in ("a", "a", "b", "c"))
    assert await runtime.customer(first)
    waiting = asyncio.create_task(runtime.customer(same))
    assert await runtime.customer(other)
    blocked = asyncio.create_task(runtime.customer(third))
    await asyncio.sleep(0)
    assert not waiting.done() and not blocked.done()
    platform.config["_business_epoch"] = 2
    first.processing_completion.set_result(None)
    other.processing_completion.set_result(None)
    assert not await waiting
    assert not await blocked
    assert not runtime.sessions
    assert runtime.customers[("bot", "1")]._value == 2


@pytest.mark.asyncio
async def test_same_member_order_other_member_progress(tmp_path, wsl_active_policy):
    bot = native.WangshangliaoAdapter(
        {"id": "lanes", "account_id": "1", "enabled_groups": ["5"]}, {}, asyncio.Queue()
    )
    bot.ledger = Ledger(tmp_path / "messages.sqlite3")
    await bot.ledger.open()
    bot.groups = {"5": "55"}
    bot.connection_state = "online"
    bot.diagnostics.emit = Mock()
    blocked, fast, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    all_finished = asyncio.Event()
    finished = []

    async def process(group, *, single, sender, control=False):
        async with bot.ledger.db.execute(
            "SELECT message FROM inbox WHERE team=? AND sender=? AND state='pending' ORDER BY rowid LIMIT 1",
            (group, sender),
        ) as cursor:
            mid = (await cursor.fetchone())[0]
        if sender == "2":
            blocked.set()
            await release.wait()
        else:
            fast.set()
        finished.append(mid)
        await bot.ledger.mark("1", group, mid, "processed")
        if len(finished) == 3:
            all_finished.set()

    bot.process_group = process
    for mid, sender in (("slow1", "2"), ("slow2", "2"), ("fast", "3")):
        await bot.ledger.ingest("1", "5", mid, envelope(sender, time.time()))
    dispatch = asyncio.create_task(bot.dispatch_pending())
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        await asyncio.wait_for(fast.wait(), 2)
        assert finished == ["fast"]
        release.set()
        await asyncio.wait_for(all_finished.wait(), 2)
        assert finished == ["fast", "slow1", "slow2"]
    finally:
        bot.stopping.set()
        dispatch.cancel()
        await asyncio.gather(dispatch, *bot.workers.values(), return_exceptions=True)
        await bot.ledger.close()


@pytest.mark.asyncio
async def test_review_database_lock_does_not_block_event_loop(tmp_path):
    from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore

    store = ReviewStore(tmp_path / "moderation.sqlite3")
    await asyncio.to_thread(store.initialize)
    with store.connect() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        writing = asyncio.create_task(
            asyncio.to_thread(
                store.observe, "1", "5", "m", "2", "message_flood", time.time(), 0
            )
        )
        start = time.monotonic()
        await asyncio.sleep(0.05)
        assert time.monotonic() - start < 0.5
        assert not writing.done()
        blocker.rollback()
    assert await writing


@pytest.mark.asyncio
async def test_review_concurrency_is_four_and_timeout_falls_back(monkeypatch):
    from astrbot.builtin_stars.wangshangliao_moderation import semantic

    platform = SimpleNamespace(config={}, diagnostics=SimpleNamespace(emit=Mock()))
    release, full = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def review(_, event):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            full.set()
        try:
            await release.wait()
            return {"decision": "allow"}
        finally:
            active -= 1

    monkeypatch.setattr(semantic, "_assess", review)
    event = SimpleNamespace(
        platform=platform,
        get_group_id=lambda: "5",
        message_obj=SimpleNamespace(message_id="m"),
    )
    tasks = [asyncio.create_task(semantic.assess(None, event)) for _ in range(8)]
    try:
        await asyncio.wait_for(full.wait(), 1)
        assert peak == 4
        await asyncio.sleep(0)
        assert sum(task.done() for task in tasks) == 4
        release.set()
        results = await asyncio.gather(*tasks)
        assert sum(r["decision"] == "allow" for r in results) == 4
        assert (
            sum(
                r["decision"] == "fallback" and r["reason"] == "review_busy"
                for r in results
            )
            == 4
        )
        release.clear()
        platform.config = {"moderation": {"semantic": {"timeout_seconds": 0.01}}}
        assert (await semantic.assess(None, event))["decision"] == "fallback"
        assert platform._review_slots._value == 4
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_transport_soak(tmp_path, wsl_active_policy):
    """Exercise the real durable dispatcher against an immediate fake upstream."""
    duration = float(os.environ.get("WSL_SOAK_SECONDS", "2"))
    count = int(duration * 20)
    queue = asyncio.Queue()
    groups = [str(10 + i) for i in range(10)]
    bot = native.WangshangliaoAdapter(
        {"id": "soak", "account_id": "1", "enabled_groups": groups}, {}, queue
    )
    bot.ledger = Ledger(tmp_path / "messages.sqlite3")
    await bot.ledger.open()
    bot.groups = {g: g + "0" for g in groups}
    bot.connection_state = "online"
    bot.diagnostics.emit = Mock()
    elapsed, lag, seen = [], [], set()
    started = time.monotonic()

    async def consume():
        for _ in range(count):
            event = await queue.get()
            mid = event.message_obj.message_id
            assert mid not in seen
            seen.add(mid)
            elapsed.append(time.time() - event.get_extra("wsl_received_at"))
            event.processing_completion.set_result(None)

    async def tick():
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.02)
            lag.append(max(0, time.monotonic() - before - 0.02))

    tasks = [asyncio.create_task(bot.dispatch_pending()), asyncio.create_task(tick())]
    consumer = asyncio.create_task(consume())
    try:
        for i in range(count):
            await asyncio.sleep(max(0, started + i / 20 - time.monotonic()))
            await bot.ledger.ingest(
                "1",
                groups[i % 10],
                str(i),
                envelope(str(100 + i % 30), time.time(), "排名"),
            )
            bot.dispatch_wakeup.set()
        await asyncio.wait_for(consumer, 15)
        p95 = sorted(elapsed)[int(len(elapsed) * 0.95)]
        lag95 = sorted(lag)[int(len(lag) * 0.95)]
        print(
            json.dumps(
                {
                    "mode": "native_transport_fake_pipeline",
                    "seconds": duration,
                    "messages": count,
                    "unique": len(seen),
                    "dispatch_p50_ms": sorted(elapsed)[len(elapsed) // 2] * 1000,
                    "dispatch_p95_ms": p95 * 1000,
                    "loop_p95_ms": lag95 * 1000,
                }
            )
        )
        assert len(seen) == count and p95 < 1 and lag95 < 0.1
    finally:
        bot.stopping.set()
        for task in [*tasks, consumer, *bot.workers.values()]:
            task.cancel()
        await asyncio.gather(
            *tasks, consumer, *bot.workers.values(), return_exceptions=True
        )
        await bot.ledger.close()


@pytest.mark.asyncio
async def test_commands_have_reserved_capacity_under_slow_review_load(
    tmp_path, wsl_active_policy
):
    bot = native.WangshangliaoAdapter(
        {"id": "priority", "account_id": "1", "enabled_groups": ["5"]},
        {},
        asyncio.Queue(),
    )
    bot.ledger = Ledger(tmp_path / "messages.sqlite3")
    await bot.ledger.open()
    bot.groups = {"5": "55"}
    bot.connection_state = "online"
    release, saturated, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = 0

    async def process(group, *, single, sender, control=False):
        nonlocal active
        async with bot.ledger.db.execute(
            "SELECT message,dispatch_class FROM inbox WHERE team=? AND sender=? "
            "AND state='pending' ORDER BY rowid LIMIT 1",
            (group, sender),
        ) as cursor:
            mid, kind = await cursor.fetchone()
        if kind == "message":
            active += 1
            if active == 12:
                saturated.set()
            await release.wait()
            active -= 1
        else:
            answered.set()
        await bot.ledger.mark("1", group, mid, "processed")

    bot.process_group = process
    for n in range(20):
        await bot.ledger.ingest("1", "5", str(n), envelope(str(100 + n), time.time()))
    dispatch = asyncio.create_task(bot.dispatch_pending())
    try:
        await asyncio.wait_for(saturated.wait(), 2)
        await bot.ledger.ingest(
            "1",
            "5",
            "rank",
            {**envelope("999", time.time(), "排名"), "_dispatch_class": "command"},
        )
        bot.dispatch_wakeup.set()
        await asyncio.wait_for(answered.wait(), 1)
        assert active == 12 and len(bot.workers) <= 16
    finally:
        bot.stopping.set()
        dispatch.cancel()
        for worker in bot.workers.values():
            worker.cancel()
        await asyncio.gather(dispatch, *bot.workers.values(), return_exceptions=True)
        await bot.ledger.close()


@pytest.mark.asyncio
async def test_dispatch_single_scan_preserves_priority_and_reserved_slots(
    tmp_path, wsl_active_policy
):
    bot = native.WangshangliaoAdapter(
        {"id": "single-scan", "account_id": "1", "enabled_groups": ["5"]},
        {},
        asyncio.Queue(),
    )
    bot.ledger = Ledger(tmp_path / "messages.sqlite3")
    await bot.ledger.open()
    bot.groups = {"5": "55"}
    bot.connection_state = "online"
    started, release = asyncio.Event(), asyncio.Event()
    selected, queries = [], []
    # Insert ordinary messages first so class priority cannot follow row order.
    rows = (
        [("5", f"m{i}", f"m{i}", "message") for i in range(20)]
        + [("5", f"cmd{i}", f"cmd{i}", "command") for i in range(6)]
        + [
            (f"private/{i}/peer{i}", f"ctrl{i}", f"peer{i}", "control")
            for i in range(3)
        ]
    )
    await bot.ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,sender,dispatch_class,payload,state) "
        "VALUES('1',?,?,?,?,'{}','pending')",
        rows,
    )
    await bot.ledger.db.commit()
    await bot.ledger.db.set_trace_callback(
        lambda sql: queries.append(sql) if sql.lstrip().startswith("WITH ") else None
    )

    async def process(group, *, single, sender, control=False):
        lane = (group, "control/" + sender if control else sender)
        selected.append((bot.worker_classes[lane], group, sender))
        if len(selected) == 18:
            started.set()
        await release.wait()

    bot.process_group = process
    dispatch = asyncio.create_task(bot.dispatch_pending())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert [row[0] for row in selected] == (
            ["control"] * 2 + ["command"] * 4 + ["message"] * 12
        )
        assert [row[2] for row in selected] == (
            ["peer0", "peer1"]
            + [f"cmd{i}" for i in range(4)]
            + [f"m{i}" for i in range(12)]
        )
        assert len(bot.workers) == 18
        assert len(queries) == 1
    finally:
        bot.stopping.set()
        for task in [dispatch, *bot.workers.values()]:
            task.cancel()
        await asyncio.gather(dispatch, *bot.workers.values(), return_exceptions=True)
        await bot.ledger.close()


@pytest.mark.asyncio
async def test_dispatch_combined_heads_keep_active_lanes_and_sender_order(
    tmp_path, wsl_active_policy
):
    bot = native.WangshangliaoAdapter(
        {"id": "head-order", "account_id": "1", "enabled_groups": ["5"]},
        {},
        asyncio.Queue(),
    )
    bot.ledger = Ledger(tmp_path / "messages.sqlite3")
    await bot.ledger.open()
    bot.groups = {"5": "55", "6": "66"}
    bot.connection_state = "online"
    started, release = asyncio.Event(), asyncio.Event()
    selected = []
    private = "private/7/peer"
    other_private = "private/8/other"
    rows = [
        ("1", "5", "busy", "busy", "command"),
        ("1", private, "busy-private", "peer", "message"),
        ("1", private, "interrupt", "peer", "control"),
        ("1", other_private, "ordinary-before-control", "other", "message"),
        ("1", other_private, "busy-control", "other", "control"),
        ("1", "5", "older-message", "fifo", "message"),
        ("1", "5", "younger-command", "fifo", "command"),
        ("1", "5", "older-command", "fifo2", "command"),
        ("1", "5", "younger-message", "fifo2", "message"),
        ("1", "6", "disabled", "disabled", "command"),
        ("2", "5", "other-account", "other-account", "command"),
    ]
    await bot.ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,sender,dispatch_class,payload,state) "
        "VALUES(?,?,?,?,?,'{}','pending')",
        rows,
    )
    await bot.ledger.db.commit()
    for lane, kind in [
        (("5", "busy"), "command"),
        ((private, "peer"), "message"),
        ((other_private, "control/other"), "control"),
    ]:
        bot.workers[lane] = asyncio.create_task(release.wait())
        bot.worker_classes[lane] = kind

    async def process(group, *, single, sender, control=False):
        lane = (group, "control/" + sender if control else sender)
        selected.append((bot.worker_classes[lane], group, sender))
        if len(selected) == 4:
            started.set()
        await release.wait()

    bot.process_group = process
    dispatch = asyncio.create_task(bot.dispatch_pending())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert selected == [
            ("control", private, "peer"),
            ("command", "5", "fifo2"),
            ("message", other_private, "other"),
            ("message", "5", "fifo"),
        ]
        assert len(bot.workers) == 7
    finally:
        bot.stopping.set()
        for task in [dispatch, *bot.workers.values()]:
            task.cancel()
        await asyncio.gather(dispatch, *bot.workers.values(), return_exceptions=True)
        await bot.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary_kind", ["message", "command"])
async def test_dispatch_control_head_blocks_later_ordinary_until_completed(
    tmp_path, wsl_active_policy, ordinary_kind
):
    bot = native.WangshangliaoAdapter(
        {"id": "control-head", "account_id": "1", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    bot.ledger = Ledger(tmp_path / "messages.sqlite3")
    await bot.ledger.open()
    bot.connection_state = "online"
    control_started, ordinary_started, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    private = "private/7/peer"
    await bot.ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,sender,dispatch_class,payload,state) "
        "VALUES('1',?,?,'peer',?,'{}','pending')",
        [(private, "exit", "control"), (private, "later", ordinary_kind)],
    )
    await bot.ledger.db.commit()

    async def process(group, *, single, sender, control=False):
        if control:
            control_started.set()
            await release.wait()
            await bot.ledger.mark("1", group, "exit", "processed")
        else:
            ordinary_started.set()
            await release.wait()
            await bot.ledger.mark("1", group, "later", "processed")

    bot.process_group = process
    dispatch = asyncio.create_task(bot.dispatch_pending())
    try:
        await asyncio.wait_for(control_started.wait(), 2)
        # Wake another scan while the control lane remains occupied.
        bot.dispatch_wakeup.set()
        await asyncio.sleep(0.03)
        assert not ordinary_started.is_set()
        release.set()
        await asyncio.wait_for(ordinary_started.wait(), 2)
    finally:
        bot.stopping.set()
        for task in [dispatch, *bot.workers.values()]:
            task.cancel()
        await asyncio.gather(dispatch, *bot.workers.values(), return_exceptions=True)
        await bot.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saturated",
    [
        ("control",),
        ("control", "command"),
        ("command", "message"),
        ("control", "command", "message"),
    ],
)
async def test_dispatch_combined_scan_respects_existing_class_capacity(
    tmp_path, wsl_active_policy, saturated
):
    bot = native.WangshangliaoAdapter(
        {"id": "capacity-scan", "account_id": "1", "enabled_groups": ["5"]},
        {},
        asyncio.Queue(),
    )
    bot.ledger = Ledger(tmp_path / "messages.sqlite3")
    await bot.ledger.open()
    bot.groups = {"5": "55"}
    bot.connection_state = "online"
    started, release = asyncio.Event(), asyncio.Event()
    selected, queries = [], []
    limits = {"control": 2, "command": 4, "message": 12}
    await bot.ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,sender,dispatch_class,payload,state) "
        "VALUES('1',?,?,?,?,'{}','pending')",
        [
            ("private/7/peer", "exit", "peer", "control"),
            ("5", "cmd", "cmd", "command"),
            ("5", "normal", "normal", "message"),
        ],
    )
    await bot.ledger.db.commit()
    for kind in saturated:
        for i in range(limits[kind]):
            lane = ("5", f"control/{kind}{i}" if kind == "control" else f"{kind}{i}")
            bot.workers[lane] = asyncio.create_task(release.wait())
            bot.worker_classes[lane] = kind
    await bot.ledger.db.set_trace_callback(
        lambda sql: queries.append(sql) if sql.lstrip().startswith("WITH ") else None
    )

    async def process(group, *, single, sender, control=False):
        lane = (group, "control/" + sender if control else sender)
        selected.append(bot.worker_classes[lane])
        if len(selected) == 3 - len(saturated):
            started.set()
        await release.wait()

    bot.process_group = process
    dispatch = asyncio.create_task(bot.dispatch_pending())
    try:
        if len(saturated) == 3:
            await asyncio.sleep(0)
        else:
            await asyncio.wait_for(started.wait(), 2)
        assert selected == [kind for kind in limits if kind not in saturated]
        assert len(queries) == int(len(saturated) < 3)
        assert all(
            list(bot.worker_classes.values()).count(kind) <= limit
            for kind, limit in limits.items()
        )
    finally:
        bot.stopping.set()
        for task in [dispatch, *bot.workers.values()]:
            task.cancel()
        await asyncio.gather(dispatch, *bot.workers.values(), return_exceptions=True)
        await bot.ledger.close()
