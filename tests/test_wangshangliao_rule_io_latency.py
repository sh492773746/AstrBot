"""Offline rule transactions must not stall asyncio or retry unknown sends."""

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import advertisement_recall
from tests.test_wangshangliao_business_rules import (
    event_for,
    keyword,
    save_rules,
)
from tests.test_wangshangliao_business_rules import rules as rules

SQLITE_WAIT_SECONDS = 1.3
HEARTBEAT_LIMIT_SECONDS = 0.25


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        (None, "rejected"),
        ("rejected", "rejected"),
        ("unknown", "unknown"),
        ("sending", "unknown"),
        ("accepted", "partial"),
    ],
)
async def test_failed_scheduled_image_uses_native_evidence_not_exception_text(
    scope, receipt, expected
):
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    async def evidence(key):
        if key.endswith("/image") and receipt is not None:
            return {"status": receipt, "server_id": ""}
        return None

    scope.adapter.ledger = SimpleNamespace(receipt=AsyncMock(side_effect=evidence))
    scope.rules.media.send.side_effect = ProtocolError(
        "advertisement_image_scope_changed"
    )
    await scope.rules._send_ad(scope.adapter, scope.cursor)
    with scope.rules.store.connect() as db:
        row = db.execute("SELECT state FROM business_rule_sends").fetchone()
        cursor = db.execute("SELECT blocked FROM business_ad_cursors").fetchone()
    assert row["state"] == expected
    assert (
        cursor["blocked"]
        == {
            "rejected": "",
            "unknown": "delivery_unknown",
            "partial": "partial_delivery",
        }[expected]
    )
    scope.adapter.send_reply_text.assert_not_awaited()
    scope.rules.media.send.assert_awaited_once()


class BeginWait:
    """Observe real SQLite lock waits with an independent writer and heartbeat."""

    def __init__(self, path, monkeypatch):
        self.path = path
        self.monkeypatch = monkeypatch
        self.connect = sqlite3.connect
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.begins = []
        self.writes = []
        self.commits = []
        self.callback_lags = []
        self.timer_lags = []
        self.ready = threading.Event()
        self.attempted = threading.Event()
        self.release = threading.Event()
        self.thread = None
        self.errors = []
        self.armed = False
        self.started = None
        self.elapsed = None
        self.cancel_task = None
        self.mutex = threading.Lock()

    def install(self):
        """Observe only connections to this test's isolated rule store."""
        observer = self

        class Connection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                statement = " ".join(sql.upper().split())
                thread = threading.get_ident()
                waiting = False
                if statement == "BEGIN IMMEDIATE":
                    observer.begins.append(thread)
                    with observer.mutex:
                        if observer.armed:
                            observer.armed = False
                            observer.started = time.monotonic()
                            observer.attempted.set()
                            waiting = True
                elif statement.startswith(("INSERT ", "UPDATE ", "DELETE ")):
                    observer.writes.append(thread)
                try:
                    return super().execute(sql, parameters)
                finally:
                    if waiting:
                        observer.elapsed = time.monotonic() - observer.started

            def commit(self):
                observer.commits.append(threading.get_ident())
                return super().commit()

        def connect(database, *args, **kwargs):
            if str(database) == str(observer.path):
                kwargs["factory"] = Connection
            return observer.connect(database, *args, **kwargs)

        self.monkeypatch.setattr(sqlite3, "connect", connect)

    async def hold(self):
        """Hold a real writer lock until 1.3 seconds after the next BEGIN starts."""

        def lock_writer():
            db = None
            try:
                db = self.connect(self.path, timeout=5)
                db.execute("BEGIN IMMEDIATE")
                self.ready.set()
                if not self.attempted.wait(5):
                    if not self.release.is_set():
                        raise AssertionError("The guarded BEGIN was never attempted")
                    return
                if self.release.wait(0.05):
                    return
                queued = time.monotonic()
                # This callback is queued after SQLite has entered its wait,
                # even when the event loop cannot run a monitoring coroutine.
                self.loop.call_soon_threadsafe(
                    lambda: self.callback_lags.append(time.monotonic() - queued)
                )
                if self.cancel_task is not None:
                    self.loop.call_soon_threadsafe(self.cancel_task.cancel)
                self.release.wait(
                    max(0, self.started + SQLITE_WAIT_SECONDS - time.monotonic())
                )
            except BaseException as exc:
                self.errors.append(exc)
                self.ready.set()
            finally:
                if db is not None:
                    db.rollback()
                    db.close()

        self.armed = True
        self.thread = threading.Thread(target=lock_writer, name="rule-io-test-writer")
        self.thread.start()
        assert await asyncio.to_thread(self.ready.wait, 5), "Writer did not start"
        assert not self.errors, self.errors

    async def finish(self):
        """Release and join the independent writer, including after test failure."""
        if self.started is not None and self.thread is not None:
            await asyncio.to_thread(self.thread.join, SQLITE_WAIT_SECONDS + 1)
        self.release.set()
        self.attempted.set()
        if self.thread is not None:
            await asyncio.to_thread(self.thread.join, 5)
            assert not self.thread.is_alive(), "Writer thread did not stop"
        assert not self.errors, self.errors

    def assert_responsive(self):
        """Require the actual wait and both heartbeat checks to have occurred."""
        assert self.elapsed is not None, "No observed SQLite BEGIN wait"
        assert self.elapsed >= SQLITE_WAIT_SECONDS - 0.1
        assert self.callback_lags, "The independent heartbeat did not run"
        assert self.timer_lags, "The pre-armed timer heartbeat did not run"
        assert max(self.callback_lags) < HEARTBEAT_LIMIT_SECONDS, (
            "A callback queued during SQLite BEGIN was blocked",
            self.callback_lags,
        )
        assert max(self.timer_lags) < HEARTBEAT_LIMIT_SECONDS, (
            "The pre-armed timer was blocked",
            max(self.timer_lags),
        )
        assert len(self.begins) >= 2, "Admission and finalization must both run"
        assert all(thread != self.loop_thread for thread in self.begins)
        assert self.writes
        assert all(thread != self.loop_thread for thread in self.writes)
        assert len(self.commits) >= 2
        assert all(thread != self.loop_thread for thread in self.commits)


async def run_with_heartbeat(awaitable, wait, *, cancel=False):
    """Arm deadlines before running work and flush callbacks after it returns.

    Args:
        awaitable: Rule operation using only test-owned state and mocked native IO.
        wait: Independent SQLite writer and heartbeat observations.
        cancel: Cancel the operation from the writer while admission is waiting.

    Returns:
        The rule operation's result.
    """
    handle = None
    stopped = False

    def heartbeat(expected):
        nonlocal handle
        wait.timer_lags.append(max(0, wait.loop.time() - expected))
        if not stopped:
            expected = wait.loop.time() + 0.02
            handle = wait.loop.call_at(expected, heartbeat, expected)

    expected = wait.loop.time() + 0.02
    handle = wait.loop.call_at(expected, heartbeat, expected)
    task = asyncio.create_task(awaitable)
    if cancel:
        wait.cancel_task = task
    try:
        return await asyncio.wait_for(task, 8)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # A callback delayed by a blocking final transaction may not have fired
        # when the operation returns; drain it before judging responsiveness.
        await asyncio.sleep(0.04)
        stopped = True
        if handle is not None:
            handle.cancel()
        await wait.finish()


@pytest.fixture
def scope(rules, monkeypatch):
    """Seed one isolated keyword and rotating-image schedule with fake native IO."""
    images = ["a" * 32, "b" * 32]
    with rules.store.connect(write=True) as db:
        for image in images:
            db.execute(
                "INSERT INTO business_rule_images "
                "(id,account,group_id,digest,width,height,data,thumbnail,created) "
                "VALUES(?,'99','5',?,1,1,?,'',?)",
                (image, "0" * 64, b"mocked-native-image", time.time()),
            )
    save_rules(
        rules,
        {
            "keywords": [
                keyword(terms=["needle"], text="Offline keyword", cooldown_seconds=0)
            ],
            "ads": [
                {
                    "id": "a",
                    "name": "Offline advertisement",
                    "text": "",
                    "images": images,
                    "times": ["09:00"],
                    "enabled": True,
                }
            ],
        },
    )
    planned = time.time() - 0.1
    rules.started = planned - 60
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_ad_cursors SET next_at=?", (planned,))
    guard = Mock()
    monkeypatch.setattr(rules, "send_guard", guard)
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.rule_replies.is_managed_account",
        lambda uid: False,
    )
    rules.service.capabilities[("99", "5")] = {"checked_at": time.time()}
    adapter = SimpleNamespace(
        account="99",
        nim_account="own-peer",
        connection_state="online",
        config={"id": "offline-rule-io", "_business_active": True, "enable": True},
        send_reply_text=AsyncMock(return_value="accepted"),
    )
    event = event_for(rules.service, text="needle", mid="offline-rule-io")
    event.platform = adapter
    rules.media.send = AsyncMock(return_value="accepted")
    return SimpleNamespace(
        rules=rules,
        adapter=adapter,
        event=event,
        images=images,
        guard=guard,
        cursor={
            "account": "99",
            "group_id": "5",
            "rule_id": "a",
            "next_at": planned,
        },
    )


@pytest.mark.asyncio
async def test_independent_heartbeat_detects_a_blocking_begin(scope, monkeypatch):
    rules = scope.rules
    wait = BeginWait(rules.store.path, monkeypatch)
    wait.install()
    await wait.hold()

    async def blocking_transaction():
        with rules.store.connect(write=True):
            pass

    await run_with_heartbeat(blocking_transaction(), wait)
    assert wait.begins == [wait.loop_thread]
    assert max(wait.callback_lags) >= 1
    assert max(wait.timer_lags) >= 1
    with pytest.raises(AssertionError, match="callback queued during SQLite BEGIN"):
        wait.assert_responsive()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "finalization"])
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
async def test_scheduled_image_transactions_stay_off_loop(
    scope, monkeypatch, phase, outcome
):
    rules = scope.rules
    wait = BeginWait(rules.store.path, monkeypatch)

    async def send_image(adapter, group, image, key, authorize, **kwargs):
        authorize()
        if phase == "finalization":
            await wait.hold()
        return outcome

    rules.media.send.side_effect = send_image
    wait.install()
    if phase == "admission":
        await wait.hold()
    await run_with_heartbeat(rules._send_ad(scope.adapter, scope.cursor), wait)
    await rules._send_ad(scope.adapter, scope.cursor)

    rules.media.send.assert_awaited_once()
    assert rules.media.send.await_args.args[2] == scope.images[0]
    scope.adapter.send_reply_text.assert_not_awaited()
    assert scope.guard.call_count >= 2
    with rules.store.connect() as db:
        sends = db.execute("SELECT state FROM business_rule_sends").fetchall()
        cursor = db.execute(
            "SELECT next_at,blocked FROM business_ad_cursors"
        ).fetchone()
        rotation = db.execute("SELECT position FROM business_ad_rotations").fetchone()
    assert [row["state"] for row in sends] == [outcome]
    assert cursor["next_at"] > time.time()
    assert cursor["blocked"] == ("delivery_unknown" if outcome == "unknown" else "")
    assert rotation["position"] == (1 if outcome == "accepted" else 0)
    wait.assert_responsive()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
async def test_concurrent_image_slot_claims_are_atomic(scope, monkeypatch, outcome):
    rules = scope.rules
    wait = BeginWait(rules.store.path, monkeypatch)
    rules.media.send.return_value = outcome
    wait.install()
    await wait.hold()

    async def send_same_slot():
        await asyncio.gather(
            rules._send_ad(scope.adapter, scope.cursor),
            rules._send_ad(scope.adapter, scope.cursor),
        )

    await run_with_heartbeat(send_same_slot(), wait)
    rules.media.send.assert_awaited_once()
    assert rules.media.send.await_args.args[2] == scope.images[0]
    with rules.store.connect() as db:
        sends = db.execute("SELECT state FROM business_rule_sends").fetchall()
        cursor = db.execute(
            "SELECT next_at,blocked FROM business_ad_cursors"
        ).fetchone()
        position = db.execute("SELECT position FROM business_ad_rotations").fetchone()[
            0
        ]
    assert [row["state"] for row in sends] == [outcome]
    assert cursor["next_at"] > time.time()
    assert cursor["blocked"] == ("delivery_unknown" if outcome == "unknown" else "")
    assert position == (1 if outcome == "accepted" else 0)
    wait.assert_responsive()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "unknown", "partial"])
async def test_losing_slot_claim_cannot_advance_the_senders_cursor(scope, outcome):
    rules = scope.rules
    started, release = asyncio.Event(), asyncio.Event()

    async def deliver(*args, **kwargs):
        started.set()
        await release.wait()
        return outcome

    rules.media.send.side_effect = deliver
    # Partial delivery is normally produced when image succeeds but text fails.
    if outcome == "partial":
        row = rules.read("99", "5")
        row["config"]["ads"][0]["text"] = "Offline caption"
        save_rules(rules, row["config"])
        with rules.store.connect(write=True) as db:
            db.execute(
                "UPDATE business_ad_cursors SET next_at=?",
                (scope.cursor["next_at"],),
            )
        scope.adapter.send_reply_text.return_value = "rejected"

        async def deliver(*args, **kwargs):
            started.set()
            await release.wait()
            return "accepted"

        rules.media.send.side_effect = deliver
    winner = asyncio.create_task(rules._send_ad(scope.adapter, scope.cursor))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await rules._send_ad(scope.adapter, scope.cursor)
        with rules.store.connect() as db:
            cursor = db.execute(
                "SELECT next_at,blocked FROM business_ad_cursors"
            ).fetchone()
        assert tuple(cursor) == (scope.cursor["next_at"], "")
        release.set()
        await asyncio.wait_for(winner, 4)
    finally:
        release.set()
        await asyncio.gather(winner, return_exceptions=True)
    with rules.store.connect() as db:
        cursor = db.execute(
            "SELECT next_at,blocked FROM business_ad_cursors"
        ).fetchone()
        receipt = db.execute("SELECT state FROM business_rule_sends").fetchone()
    assert cursor["next_at"] > time.time()
    assert (
        cursor["blocked"]
        == {
            "accepted": "",
            "unknown": "delivery_unknown",
            "partial": "partial_delivery",
        }[outcome]
    )
    assert receipt["state"] == outcome
    rules.media.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "finalization"])
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
async def test_keyword_transactions_stay_off_loop(scope, monkeypatch, phase, outcome):
    rules = scope.rules
    wait = BeginWait(rules.store.path, monkeypatch)

    async def send_text(group, key, text, **kwargs):
        kwargs["authorize"]()
        if phase == "finalization":
            await wait.hold()
        return outcome

    scope.adapter.send_reply_text.side_effect = send_text
    wait.install()
    if phase == "admission":
        await wait.hold()
    assert await run_with_heartbeat(rules.observe(scope.event), wait)
    assert await rules.observe(scope.event)

    scope.adapter.send_reply_text.assert_awaited_once()
    rules.media.send.assert_not_awaited()
    assert scope.guard.call_count >= 2
    with rules.store.connect() as db:
        sends = db.execute(
            "SELECT operation,state FROM business_rule_sends WHERE kind='keyword'"
        ).fetchall()
    assert [(row["operation"], row["state"]) for row in sends] == [
        ("keyword/99/5/offline-rule-io", outcome)
    ]
    wait.assert_responsive()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "finalization"])
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
async def test_replacement_transactions_stay_off_loop(
    scope, monkeypatch, phase, outcome
):
    rules = scope.rules
    previous = "advertisement/99/5/a/1000"
    operation = "advertisement/99/5/a/1001"
    with rules.store.connect(write=True) as db:
        for key in (previous, operation):
            db.execute(
                "INSERT INTO business_rule_sends VALUES"
                "(?,'99','5','advertisement','a',0,'accepted',0,'')",
                (key,),
            )
        db.execute(
            "INSERT INTO business_ad_heads VALUES('99','5',?,'','not_needed','',0)",
            (previous,),
        )
    row = rules.read("99", "5")
    wait = BeginWait(rules.store.path, monkeypatch)

    async def recall(adapter, group, key, **kwargs):
        kwargs["authorize"]()
        if phase == "finalization":
            await wait.hold()
        return outcome

    native_recall = AsyncMock(side_effect=recall)
    monkeypatch.setattr(advertisement_recall, "recall_advertisement", native_recall)
    wait.install()
    if phase == "admission":
        await wait.hold()
    await run_with_heartbeat(rules.replace_ad(scope.adapter, row, operation), wait)
    await rules.replace_ad(scope.adapter, row, operation)

    native_recall.assert_awaited_once()
    assert native_recall.await_args.args[2] == previous
    rules.media.send.assert_not_awaited()
    scope.adapter.send_reply_text.assert_not_awaited()
    with rules.store.connect() as db:
        head = db.execute("SELECT * FROM business_ad_heads").fetchone()
        audit_count = db.execute(
            "SELECT count(*) FROM audit WHERE action='advertisement_recall'"
        ).fetchone()[0]
    assert (head["operation"], head["previous"], head["state"]) == (
        operation,
        previous,
        outcome,
    )
    assert audit_count == 1
    wait.assert_responsive()


@pytest.mark.asyncio
async def test_cancelled_admission_cannot_leave_a_dispatched_image(scope, monkeypatch):
    rules = scope.rules
    wait = BeginWait(rules.store.path, monkeypatch)
    wait.install()
    await wait.hold()
    with pytest.raises(asyncio.CancelledError):
        await run_with_heartbeat(
            rules._send_ad(scope.adapter, scope.cursor), wait, cancel=True
        )

    rules.media.send.assert_not_awaited()
    scope.adapter.send_reply_text.assert_not_awaited()
    with rules.store.connect() as db:
        sends = db.execute("SELECT state,error FROM business_rule_sends").fetchall()
        cursor = db.execute(
            "SELECT next_at,blocked FROM business_ad_cursors"
        ).fetchone()
        position = db.execute("SELECT position FROM business_ad_rotations").fetchone()[
            0
        ]
    assert [(row["state"], row["error"]) for row in sends] == [
        ("skipped", "advertisement_cancelled_before_send")
    ]
    assert cursor["next_at"] > time.time()
    assert cursor["blocked"] == ""
    assert position == 0
    await rules._send_ad(scope.adapter, scope.cursor)
    rules.media.send.assert_not_awaited()
    with rules.store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM business_rule_sends WHERE state='dispatched'"
            ).fetchone()[0]
            == 0
        )
    wait.assert_responsive()


@pytest.mark.asyncio
async def test_finalization_cancellation_preserves_an_accepted_image(
    scope, monkeypatch
):
    rules = scope.rules
    wait = BeginWait(rules.store.path, monkeypatch)

    async def send_image(adapter, group, image, key, authorize, **kwargs):
        authorize()
        await wait.hold()
        return "accepted"

    rules.media.send.side_effect = send_image
    wait.install()
    with pytest.raises(asyncio.CancelledError):
        await run_with_heartbeat(
            rules._send_ad(scope.adapter, scope.cursor), wait, cancel=True
        )

    def finalized():
        with rules.store.connect() as db:
            send = db.execute("SELECT state FROM business_rule_sends").fetchone()
            cursor = db.execute(
                "SELECT next_at,blocked FROM business_ad_cursors"
            ).fetchone()
            position = db.execute(
                "SELECT position FROM business_ad_rotations"
            ).fetchone()[0]
        return send["state"], dict(cursor), position

    # Drainable finalization may outlive the cancelled delivery coroutine.
    for _ in range(100):
        state, cursor, position = await asyncio.to_thread(finalized)
        if state != "dispatched":
            break
        await asyncio.sleep(0.01)
    assert state == "accepted", "Cancellation lost an already accepted native receipt"
    assert cursor["next_at"] > time.time()
    assert cursor["blocked"] == ""
    assert position == 1
    await rules._send_ad(scope.adapter, scope.cursor)
    rules.media.send.assert_awaited_once()
    scope.adapter.send_reply_text.assert_not_awaited()
    wait.assert_responsive()


def test_tenant_lookup_does_not_decode_other_groups(rules):
    expected = rules.store.tenant("99", "5")
    with rules.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO tenants "
            "(account,group_id,name,team,present,epoch,joined,managers,rules) "
            "VALUES('99','6','Unrelated','1006',1,1,0,'{','{')"
        )
    assert rules.store.tenant("99", "5") == expected
    assert rules.store.tenant("99", "7") is None
    assert rules.store.tenant("88", "5") is None
