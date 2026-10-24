"""Offline witness freshness and revocation checks against a real native ledger."""

import asyncio
import copy
import json
import socket
import threading
import time
from contextlib import aclosing, closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio

ACCOUNT = "23691273"
GROUP = "1459282"


@pytest_asyncio.fixture
async def witness(tmp_path, monkeypatch):
    """Reuse the native probe fixture only after isolating core imports.

    Args:
        tmp_path: Temporary root for configuration and ledger initialization.
        monkeypatch: Restores environment and upstream IO guards after each test.

    Yields:
        Shared probe scope with a real ledger and the production reply observer.
    """
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    monkeypatch.setenv("ASTRBOT_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(
            socket.socket,
            method,
            Mock(side_effect=AssertionError("Network IO is forbidden")),
        )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        Mock(side_effect=AssertionError("DNS IO is forbidden")),
    )

    from astrbot.core.platform.sources.wangshangliao import storage
    from tests import test_wangshangliao_three_live_probe as shared

    async with aclosing(shared.scope.__wrapped__(tmp_path, monkeypatch)) as scopes:
        scope = await anext(scopes)
        scope.sender.diagnostics = SimpleNamespace(emit=Mock())
        scope.reply_window = shared.reply_window
        scope.probe = shared.test_probe
        scope.wire = shared.wire
        scope.storage = storage
        try:
            yield scope
        finally:
            tasks = tuple(getattr(scope.service, "candidate_tasks", ()))
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


@pytest_asyncio.fixture
async def sqlite_checkpoint(witness, monkeypatch):
    """Pause real SQL on test-owned connections without replacing database IO."""
    original_connect = witness.storage.sqlite3.connect
    loop = asyncio.get_running_loop()
    checkpoint = SimpleNamespace(
        prefix="",
        started=asyncio.Event(),
        resume=threading.Event(),
        paused=threading.Event(),
        timed_out=threading.Event(),
        durability=[],
        connect=original_connect,
    )

    def trace_callback(connection, dedicated):
        def trace(sql):
            statement = sql.lstrip().upper()
            if dedicated and statement.startswith("INSERT OR IGNORE INTO INBOX"):
                checkpoint.durability.append(
                    (
                        connection.execute("PRAGMA journal_mode").fetchone()[0],
                        connection.execute("PRAGMA synchronous").fetchone()[0],
                    )
                )
            if (
                checkpoint.prefix
                and statement.startswith(checkpoint.prefix)
                and not checkpoint.paused.is_set()
            ):
                checkpoint.paused.set()
                loop.call_soon_threadsafe(checkpoint.started.set)
                if not checkpoint.resume.wait(5):
                    checkpoint.timed_out.set()

        return trace

    def connect(path, *args, **kwargs):
        assert path == witness.sender.ledger.path
        connection = original_connect(path, *args, **kwargs)
        assert connection is not witness.sender.ledger.db._conn
        connection.set_trace_callback(trace_callback(connection, True))
        return connection

    monkeypatch.setattr(witness.storage.sqlite3, "connect", connect)
    await witness.sender.ledger.db.set_trace_callback(
        trace_callback(witness.sender.ledger.db._conn, False)
    )
    try:
        yield checkpoint
    finally:
        checkpoint.resume.set()
        await witness.sender.ledger.db.set_trace_callback(None)


def revoke_window(scope, window, change):
    """Revoke only the original window without altering its ledger.

    Args:
        scope: Isolated native probe fixtures.
        window: Original real TestWindow.
        change: Original authority or frozen witness metadata to invalidate.
    """
    if change == "since_changed":
        window._witness_since += 1
        assert window.active()
        return
    if change == "id_changed":
        window._witness_id += "-replacement"
        assert window.active()
        return
    if change == "operation_changed":
        window._witness_operation += "_replacement"
        assert window.active()
        return
    if change == "closed":
        scope.receiver.test_window = None
    elif change == "replaced":
        scope.receiver.test_window = type(window)(
            scope.receiver,
            scope.sender,
            list(window.groups),
            list(window.scopes),
            list(window.keywords),
            60,
            10,
        )
        assert scope.receiver.test_window.active()
    else:
        assert change == "expired"
        window.expires = 0
    assert not window.active()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "closed",
        "replaced",
        "expired",
        "since_changed",
        "id_changed",
        "operation_changed",
    ],
)
@pytest.mark.parametrize(
    "checkpoint", ["ingress_lock", "sqlite_begin", "sqlite_lookup", "sqlite_insert"]
)
async def test_queued_witness_cannot_commit_after_original_window_revocation(
    witness, sqlite_checkpoint, change, checkpoint
):
    window, inner, outer = witness.reply_window(witness)
    ledger = witness.sender.ledger
    tasks = ()
    if checkpoint == "ingress_lock":
        await ledger.ingress_lock.acquire()
    else:
        sqlite_checkpoint.prefix = {
            "sqlite_begin": "BEGIN IMMEDIATE",
            "sqlite_lookup": "SELECT 1 FROM INBOX",
            "sqlite_insert": "INSERT OR IGNORE INTO INBOX",
        }[checkpoint]
    try:
        assert witness.probe.observe_reply(
            witness.service, witness.sender, GROUP, outer, inner
        )
        tasks = tuple(witness.service.candidate_tasks)
        assert len(tasks) == 1
        if checkpoint == "ingress_lock":
            await asyncio.sleep(0)
            assert not tasks[0].done()
            assert len(ledger.ingress_lock._waiters or ()) == 1
        else:
            await asyncio.wait_for(sqlite_checkpoint.started.wait(), 2)
            assert not tasks[0].done()
        revoke_window(witness, window, change)
    finally:
        if checkpoint == "ingress_lock":
            ledger.ingress_lock.release()
        else:
            sqlite_checkpoint.resume.set()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert not sqlite_checkpoint.timed_out.is_set()
    async with ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
        assert (await cursor.fetchone())[0] == 0
    assert not witness.service.candidate_tasks
    witness.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "observed"),
    [
        ("pending", False),
        ("pending", True),
        ("ignored_bot", True),
        ("processed", False),
    ],
)
async def test_guarded_ingress_cannot_expand_to_business_observers(
    witness, state, observed
):
    ledger = witness.sender.ledger
    authorize, observe = Mock(), Mock()
    with pytest.raises(ValueError, match="authorized_ingress_requires_ignored_bot"):
        await ledger.ingest(
            ACCOUNT,
            GROUP,
            "700",
            {"sender": "43506527", "text": "fixture"},
            state=state,
            observe=observe if observed else None,
            authorize=authorize,
        )
    authorize.assert_not_called()
    observe.assert_not_called()
    async with ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["closed", "replaced", "expired", "cancelled"])
@pytest.mark.parametrize("writer", ["mark", "reserve", "finish"])
async def test_insert_revocation_rolls_back_only_witness_not_other_writers(
    witness, sqlite_checkpoint, change, writer
):
    window, inner, outer = witness.reply_window(witness)
    ledger = witness.sender.ledger
    assert await ledger.ingest(ACCOUNT, GROUP, "normal", {"sender": "ordinary"})
    if writer == "finish":
        await ledger.reserve("unrelated", "normal intent")
    sqlite_checkpoint.prefix = "INSERT OR IGNORE INTO INBOX"
    tasks = ()
    writing = None
    try:
        assert witness.probe.observe_reply(
            witness.service, witness.sender, GROUP, outer, inner
        )
        tasks = tuple(witness.service.candidate_tasks)
        await asyncio.wait_for(sqlite_checkpoint.started.wait(), 2)
        if writer == "mark":
            operation = ledger.mark(ACCOUNT, GROUP, "normal", "processed")
        elif writer == "reserve":
            operation = ledger.reserve("unrelated", "normal intent")
        else:
            operation = ledger.finish("unrelated", "accepted", "900")
        writing = asyncio.create_task(operation)
        await asyncio.sleep(0.02)
        assert not writing.done()
        if change == "cancelled":
            tasks[0].cancel()
            await asyncio.sleep(0)
            assert not tasks[0].done()
        else:
            revoke_window(witness, window, change)
        sqlite_checkpoint.resume.set()
        if change == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tasks[0], 5)
        else:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
        await asyncio.wait_for(writing, 5)
    finally:
        sqlite_checkpoint.resume.set()
        if tasks or writing:
            await asyncio.gather(
                *tasks, *([writing] if writing else []), return_exceptions=True
            )
    assert not sqlite_checkpoint.timed_out.is_set()
    assert sqlite_checkpoint.durability == [("wal", 2)]
    assert not ledger.db.in_transaction
    with closing(sqlite_checkpoint.connect(ledger.path)) as reader:
        rows = reader.execute(
            "SELECT message,state FROM inbox ORDER BY message"
        ).fetchall()
        assert rows == [("normal", "processed" if writer == "mark" else "pending")]
        if writer != "mark":
            expected = ("accepted", "900") if writer == "finish" else ("sending", "")
            assert (
                reader.execute(
                    "SELECT state,server_id FROM outbox WHERE key='unrelated'"
                ).fetchone()
                == expected
            )
    witness.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["closed", "replaced", "expired", "cancelled"])
async def test_waiting_witness_cannot_commit_or_rollback_shared_uncommitted_outbox(
    witness, sqlite_checkpoint, change
):
    window, inner, outer = witness.reply_window(witness)
    ledger = witness.sender.ledger
    await ledger.db.execute(
        "INSERT INTO outbox VALUES('unrelated','fixture','900','sending','')"
    )
    assert ledger.db.in_transaction
    sqlite_checkpoint.prefix = "BEGIN IMMEDIATE"
    tasks = ()
    try:
        assert witness.probe.observe_reply(
            witness.service, witness.sender, GROUP, outer, inner
        )
        tasks = tuple(witness.service.candidate_tasks)
        await asyncio.wait_for(sqlite_checkpoint.started.wait(), 2)
        sqlite_checkpoint.resume.set()
        await asyncio.sleep(0.02)
        assert not tasks[0].done()
        if change == "cancelled":
            tasks[0].cancel()
            await asyncio.sleep(0)
            assert not tasks[0].done()
        else:
            revoke_window(witness, window, change)
        assert ledger.db.in_transaction
        async with ledger.db.execute("SELECT COUNT(*) FROM outbox") as cursor:
            assert (await cursor.fetchone())[0] == 1
        with closing(sqlite_checkpoint.connect(ledger.path)) as reader:
            assert reader.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        await ledger.db.commit()
        if change == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(tasks[0], 5)
        else:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
    finally:
        sqlite_checkpoint.resume.set()
        await ledger.db.rollback()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    assert not sqlite_checkpoint.timed_out.is_set()
    assert not sqlite_checkpoint.durability
    with closing(sqlite_checkpoint.connect(ledger.path)) as reader:
        assert reader.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
        assert reader.execute("SELECT key FROM outbox").fetchall() == [("unrelated",)]


@pytest.mark.asyncio
async def test_guarded_evidence_commit_is_durable_and_deduplicated(
    witness, sqlite_checkpoint
):
    window, _, _ = witness.reply_window(witness)
    ledger = witness.sender.ledger
    checks = []

    def authorize():
        checks.append(threading.get_ident())
        if not window.active():
            raise witness.wire.ProtocolError("fixture_authority_revoked")

    payload = {
        "sender": "43506527",
        "text": "first",
        "created_at": window._witness_since,
    }
    assert await ledger.ingest(
        ACCOUNT, GROUP, "700", payload, state="ignored_bot", authorize=authorize
    )
    assert not await ledger.ingest(
        ACCOUNT,
        GROUP,
        "700",
        {"text": "duplicate"},
        state="ignored_bot",
        authorize=authorize,
    )
    assert threading.get_ident() in checks
    assert any(thread != threading.get_ident() for thread in checks)
    assert sqlite_checkpoint.durability == [("wal", 2)]
    assert not ledger.db.in_transaction
    with closing(sqlite_checkpoint.connect(ledger.path)) as reader:
        rows = reader.execute("SELECT state,payload,completed_at FROM inbox").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "ignored_bot"
        assert json.loads(rows[0][1]) == payload
        assert rows[0][2] is not None


@pytest.mark.asyncio
async def test_ledger_default_ingress_still_commits_and_deduplicates(witness):
    ledger = witness.sender.ledger
    original = {"sender": "43506527", "text": "original"}
    assert await ledger.ingest(ACCOUNT, GROUP, "700", original, state="ignored_bot")
    assert not await ledger.ingest(
        ACCOUNT,
        GROUP,
        "700",
        {"sender": "43506527", "text": "replacement"},
        state="ignored_bot",
    )
    async with ledger.db.execute(
        "SELECT state,payload FROM inbox WHERE account=? AND team=? AND message=?",
        (ACCOUNT, GROUP, "700"),
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "ignored_bot"
    assert json.loads(rows[0][1]) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["outer", "inner"])
async def test_reply_from_before_window_is_rejected_even_with_fresh_receive_time(
    witness, source
):
    window, inner, outer = witness.reply_window(witness)
    stale = window._witness_since - 1
    if source == "outer":
        outer[7] = str(stale)
        inner.created_at = int(time.time() * 1000)
    else:
        outer.pop(7, None)
        inner.created_at = stale
    assert not witness.probe.observe_reply(
        witness.service, witness.sender, GROUP, outer, inner
    )
    assert not witness.service.candidate_tasks
    async with witness.sender.ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing_timestamp",
        "zero_timestamp",
        "future_timestamp",
        "missing_since",
        "zero_since",
        "string_since",
        "boolean_since",
        "missing_window_id",
        "empty_window_id",
        "missing_operation",
        "empty_operation",
    ],
)
async def test_reply_requires_native_time_and_window_metadata(
    witness, monkeypatch, fault
):
    now_ms = int(time.time()) * 1000
    monkeypatch.setattr(time, "time", lambda: now_ms / 1000)
    window, inner, outer = witness.reply_window(witness)
    if fault == "missing_timestamp":
        outer.pop(7, None)
        inner.ClearField("created_at")
    elif fault == "zero_timestamp":
        outer[7] = "0"
        inner.created_at = 0
    elif fault == "future_timestamp":
        outer[7] = str(now_ms + 5001)
        inner.created_at = now_ms
    elif fault == "missing_since":
        del window._witness_since
    elif fault == "zero_since":
        window._witness_since = 0
    elif fault == "string_since":
        window._witness_since = str(window._witness_since)
    elif fault == "boolean_since":
        window._witness_since = True
    elif fault == "missing_window_id":
        del window._witness_id
    elif fault == "empty_window_id":
        window._witness_id = ""
    elif fault == "missing_operation":
        del window._witness_operation
    else:
        window._witness_operation = ""
    assert not witness.probe.observe_reply(
        witness.service, witness.sender, GROUP, outer, inner
    )
    assert not witness.service.candidate_tasks
    async with witness.sender.ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["outer_preferred", "inner_fallback", "window_start", "future_boundary"]
)
async def test_native_timestamp_and_original_probe_correlation_are_committed(
    witness, monkeypatch, source
):
    now_ms = int(time.time()) * 1000
    monkeypatch.setattr(time, "time", lambda: now_ms / 1000)
    window, inner, outer = witness.reply_window(witness)
    if source == "outer_preferred":
        stamp = now_ms + 1000
        outer[7] = str(stamp)
        inner.created_at = window._witness_since - 10000
    elif source == "inner_fallback":
        stamp = now_ms + 500
        outer.pop(7, None)
        inner.created_at = stamp
    elif source == "window_start":
        stamp = window._witness_since
        outer[7] = str(stamp)
        inner.created_at = 0
    else:
        stamp = now_ms + 5000
        outer[7] = str(stamp)
        inner.created_at = now_ms
    assert witness.probe.observe_reply(
        witness.service, witness.sender, GROUP, outer, inner
    )
    tasks = tuple(witness.service.candidate_tasks)
    assert len(tasks) == 1
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    async with witness.sender.ledger.db.execute(
        "SELECT account,team,message,state,payload,received_at FROM inbox"
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1
    account, group, message, state, encoded, received_at = rows[0]
    assert (account, group, message, state) == (
        ACCOUNT,
        GROUP,
        str(outer[12]),
        "ignored_bot",
    )
    payload = json.loads(encoded)
    assert payload["created_at"] == stamp
    assert payload["_test_witness"] is True
    assert payload["_test_window"] == window._witness_id
    assert payload["_test_probe"] == window._witness_operation
    assert payload["sender"] == "43506527"
    assert payload["text"] == inner.content.data
    assert received_at == now_ms / 1000
    assert "_ingress_flood" not in payload
    assert "enabled_groups" not in witness.sender.config
    witness.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_native_send_stamps_window_before_submission_but_replay_does_not(
    witness,
):
    window = witness.window
    assert not getattr(window, "_witness_since", None)
    assert not getattr(window, "_witness_id", None)
    before = int(time.time() * 1000)
    submitted = []

    async def submit(service, command, encoded):
        assert (service, command) == (8, 2)
        assert before <= window._witness_since <= int(time.time() * 1000)
        assert isinstance(window._witness_id, str) and window._witness_id
        assert window._witness_operation == witness.payload["operation_id"]
        submitted.append(
            (window._witness_since, window._witness_id, window._witness_operation)
        )
        return 200, witness.wire.properties([(12, b"987654")])

    witness.sender.nim.request.side_effect = submit
    first = await witness.probe.send_probe(
        witness.service, "dashboard/test", witness.payload
    )
    original = (
        window._witness_since,
        window._witness_id,
        window._witness_operation,
    )
    witness.sender._test_probe_sent = 0
    second = await witness.probe.send_probe(
        witness.service, "dashboard/test", witness.payload
    )
    assert first["status"] == second["status"] == "accepted"
    assert first["message_id"] == second["message_id"] == "987654"
    assert submitted == [original]
    assert (
        window._witness_since,
        window._witness_id,
        window._witness_operation,
    ) == original
    witness.sender.nim.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_historical_probe_receipt_cannot_arm_replacement_window(witness):
    original = await witness.probe.send_probe(
        witness.service, "dashboard/test", witness.payload
    )
    assert original["status"] == "accepted"
    replacement = copy.copy(witness.window)
    replacement.outbound_keys = set()
    for name in ("_witness_since", "_witness_id", "_witness_operation"):
        if hasattr(replacement, name):
            delattr(replacement, name)
    witness.receiver.test_window = replacement
    witness.sender._test_probe_sent = 0
    receipt = await witness.probe.send_probe(
        witness.service, "dashboard/test", witness.payload
    )
    assert receipt["status"] == "historical"
    assert receipt["prior_status"] == "accepted"
    assert not getattr(replacement, "_witness_since", None)
    assert not getattr(replacement, "_witness_id", None)
    assert not getattr(replacement, "_witness_operation", None)
    witness.sender.nim.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_window_and_fresh_reservation_get_distinct_witness_identity(
    witness,
):
    first = await witness.probe.send_probe(
        witness.service, "dashboard/test", witness.payload
    )
    assert first["status"] == "accepted"
    original_id = witness.window._witness_id
    replacement = copy.copy(witness.window)
    replacement.outbound_keys = set()
    for name in ("_witness_since", "_witness_id", "_witness_operation"):
        delattr(replacement, name)
    witness.receiver.test_window = replacement
    witness.sender._test_probe_sent = 0
    payload = {**witness.payload, "operation_id": "WSL_TEST_new_window"}
    before = int(time.time() * 1000)
    second = await witness.probe.send_probe(witness.service, "dashboard/test", payload)
    assert second["status"] == "accepted"
    assert before <= replacement._witness_since <= int(time.time() * 1000)
    assert replacement._witness_id
    assert replacement._witness_id != original_id
    assert replacement._witness_operation == payload["operation_id"]
    assert witness.sender.nim.request.await_count == 2
