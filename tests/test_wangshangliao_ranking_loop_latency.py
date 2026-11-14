"""Detached ranking CPU batches, live scope fences and legacy-result parity."""

import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import rankings
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 11, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    monkeypatch.setattr(rankings.time, "time", lambda: clock[0])
    monkeypatch.setattr(rankings, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(rankings, "is_managed_account", lambda uid: uid == "9")
    monkeypatch.setattr(rankings, "flood_fixture", lambda *_: None)
    path = tmp_path / "messages.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "CREATE TABLE inbox(account,team,message,payload,state,received_at,pruned_at);"
            "CREATE TABLE observed_recalls(account,group_id,message_id,received_at);"
            "CREATE TABLE outbox(key,state);"
        )
    config = {
        "id": "ranking-cpu",
        "enable": True,
        "enabled_groups": ["5"],
        "_business_epoch": 1,
        "_business_revision": "policy-1",
        "_business_active": True,
        "_open_revisions": {"5": "tenant-1"},
        "_open_paused": False,
        "moderation": {"enabled": True, "automation_enabled": True},
    }

    class Adapter:
        @property
        def config(self):
            return copy.deepcopy(config)

    adapter = Adapter()
    adapter.account = "1"
    adapter.nim_account = "901"
    adapter.ledger = SimpleNamespace(path=path, db=object())
    return SimpleNamespace(
        clock=clock,
        config=config,
        adapter=adapter,
        path=path,
        root=tmp_path,
        runtime=rankings.DailyRankings(),
    )


def row(case, mid, *, sender="10", text="Valid speech", stamp=None, **changes):
    """Build one isolated inbox row with independently replaceable payload fields."""
    stamp = case.clock[0] - 100 if stamp is None else stamp
    payload = {
        "sender": sender,
        "name": f"Member {sender}",
        "text": text,
        "created_at": stamp,
    }
    payload.update(changes.pop("payload", {}))
    return (
        changes.get("account", "1"),
        changes.get("group", "5"),
        mid,
        changes.get("encoded", json.dumps(payload, ensure_ascii=False)),
        changes.get("state", "processed"),
        changes.get("received_at", stamp),
        None,
    )


def insert(case, rows):
    """Populate only the temporary fixture database."""
    with closing(sqlite3.connect(case.path)) as db, db:
        db.executemany("INSERT INTO inbox VALUES(?,?,?,?,?,?,?)", rows)


def legacy_ranked(
    rows, account, nim_account, lower, upper, violations=(), fixture=False
):
    """Freeze the pre-offload filtering and chronological reduction as an oracle.

    Args:
        rows: Detached message, JSON payload and state tuples in source order.
        account: Collector identity.
        nim_account: Transport identity for leading mentions.
        lower: Inclusive source-day timestamp.
        upper: Exclusive settlement cutoff.
        violations: Recalled or otherwise disqualified message IDs.
        fixture: Whether the exact managed-account fixture message is admitted.

    Returns:
        Ranked UID, name, count and last-counted timestamp tuples.
    """
    messages = []
    for mid, encoded, state in rows:
        if state not in {"pending", "processing", "processed"} or mid in violations:
            continue
        try:
            payload = json.loads(encoded)
            user = str(payload["sender"])
            text = payload["text"]
            stamp = float(
                payload.get("recall_route", {}).get("time")
                or payload.get("created_at")
                or 0
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if (
            not user.isascii()
            or not user.isdigit()
            or len(user) > 20
            or int(user) <= 0
            or user == account
            or user == "9"
            and not (fixture and mid == "fixture-yes")
            or not isinstance(text, str)
            or len(text) > 16384
            or rankings.recognize_command(text)
            or text.strip().startswith("/")
        ):
            continue
        if stamp > 100000000000:
            stamp /= 1000
        if not lower <= stamp < upper:
            continue
        encoded_text = text.encode("utf-16-le", errors="replace")
        for span in payload.get("mention_spans", []):
            stop = span.get("end", 0)
            if (
                span.get("uid") == str(nim_account)
                and span.get("start") == 0
                and type(stop) is int
                and 0 < stop * 2 <= len(encoded_text)
                and encoded_text[: stop * 2]
                .decode("utf-16-le", errors="replace")
                .rstrip()
                == "@" + span.get("nick", "")
            ):
                text = encoded_text[stop * 2 :].decode("utf-16-le", errors="replace")
                break
        normalized = "".join(
            char
            for char in unicodedata.normalize("NFKC", text).casefold()
            if not char.isspace() and unicodedata.category(char) != "Cf"
        )
        if rankings.recognize_command(text) or not any(
            char.isalnum() for char in normalized
        ):
            continue
        name = rankings._display_name(
            payload.get("name"),
            user,
            str(payload.get("recall_route", {}).get("peer") or ""),
        )
        digest = hashlib.sha256(normalized.encode()).digest()
        messages.append((stamp, str(mid), user, name, digest))
    counts, names, last_counted, repeated, reached = {}, {}, {}, {}, {}
    for stamp, _mid, user, name, digest in sorted(messages):
        if name:
            names[user] = name
        previous = repeated.get((user, digest), float("-inf"))
        repeated[(user, digest)] = stamp
        if stamp - previous < 300:
            continue
        if stamp - last_counted.get(user, float("-inf")) < 30:
            continue
        counts[user] = counts.get(user, 0) + 1
        last_counted[user] = reached[user] = stamp
    return [
        (user, names.get(user, ""), count, last_counted[user])
        for user, count in sorted(
            counts.items(), key=lambda pair: (-pair[1], reached[pair[0]], pair[0])
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_day", [False, True])
@pytest.mark.parametrize("settled", [False, True])
@pytest.mark.parametrize("include_activity", [False, True])
async def test_result_matches_legacy_filters_and_reduction(
    case, monkeypatch, previous_day, settled, include_activity
):
    fixture = SimpleNamespace(operation="fixture", ranking=True)
    monkeypatch.setattr(rankings, "flood_fixture", lambda *_: fixture)
    monkeypatch.setattr(
        rankings,
        "rule_target",
        lambda _a, _g, uid, mid: (uid, mid) == ("9", "fixture-yes"),
    )
    start = datetime.fromtimestamp(case.clock[0], ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=int(previous_day))
    lower = start.timestamp()
    end = lower + 86400
    rows = [
        row(case, "one", stamp=lower + 100, text="First"),
        row(case, "fast", stamp=lower + 110, text="Different but too fast"),
        row(case, "two", stamp=lower + 130, text="Second"),
        row(case, "repeat", stamp=lower + 160, text="ＦＩＲＳＴ"),
        row(case, "repeat-again", stamp=lower + 400, text="f i r s t"),
        row(case, "three", stamp=lower + 700, text="First"),
        row(
            case,
            "rename-without-count",
            stamp=lower + 710,
            text="Third name",
            payload={"name": "New\x00\u200b name"},
        ),
        row(case, "tie-b", sender="11", stamp=lower + 90),
        row(case, "tie-a", sender="12", stamp=lower + 90),
        row(case, "fixture-no", sender="9", stamp=lower + 800),
        row(case, "fixture-yes", sender="9", stamp=lower + 900),
        row(
            case,
            "managed-invalid-mention",
            sender="9",
            stamp=lower + 900,
            payload={"mention_spans": None},
        ),
        row(case, "self", sender="1", stamp=lower + 900),
        row(case, "state", stamp=lower + 900, state="needs_review"),
        row(case, "json", stamp=lower + 900, encoded="{"),
        row(case, "list", stamp=lower + 900, encoded="[]"),
        row(case, "route", stamp=lower + 900, payload={"recall_route": []}),
        row(case, "missing", stamp=lower + 900, encoded="{}"),
        row(case, "uid", sender="０", stamp=lower + 900),
        row(case, "zero", sender="0", stamp=lower + 900),
        row(case, "long-uid", sender="1" * 21, stamp=lower + 900),
        row(case, "object-text", text={}, stamp=lower + 900),
        row(case, "long-text", text="x" * 16385, stamp=lower + 900),
        row(case, "command", text="排名", stamp=lower + 900),
        row(case, "slash", text=" /unknown", stamp=lower + 900),
        row(case, "empty", text="\u200b \u2028 !!!", stamp=lower + 900),
        row(
            case,
            "mention-command",
            text="@Bot 排名",
            stamp=lower + 900,
            payload={
                "mention_spans": [{"uid": "901", "nick": "Bot", "start": 0, "end": 4}]
            },
        ),
        row(
            case,
            "wrong-mention",
            sender="13",
            text="@Bot 排名",
            stamp=lower + 900,
            payload={
                "mention_spans": [{"uid": "other", "nick": "Bot", "start": 0, "end": 4}]
            },
        ),
        row(
            case,
            "milliseconds",
            sender="14",
            stamp=lower + 900,
            payload={"recall_route": {"time": (lower + 900) * 1000, "peer": "914"}},
        ),
        row(
            case,
            "nan",
            stamp=lower + 900,
            payload={"created_at": "nan"},
        ),
        row(case, "old-time", stamp=lower + 900, payload={"created_at": lower - 1}),
        row(case, "future-time", stamp=lower + 900, payload={"created_at": end + 1}),
        row(case, "other-account", stamp=lower + 900, account="2"),
        row(case, "other-group", stamp=lower + 900, group="6"),
        row(case, "recent", sender="15", stamp=case.clock[0] - 5),
        row(case, "public-edge", sender="16", stamp=case.clock[0] + 0.5),
        row(case, "too-future", sender="17", stamp=case.clock[0] + 2),
    ]
    for index in range(80):
        rows.append(
            row(
                case,
                f"mixed-{index}",
                sender=str(100 + index % 7),
                text=f"Distinct {index}",
                stamp=lower + 1000 + index * 13,
                payload={"name": "100" if index % 3 else None},
            )
        )
    violations = {"violation", "flood", "audit", "observed", "outbox"}
    rows.extend(
        row(case, mid, sender="18", stamp=lower + 1000 + index * 31)
        for index, mid in enumerate(sorted(violations))
    )
    rows.reverse()
    insert(case, rows)
    with closing(sqlite3.connect(case.path)) as db, db:
        db.execute(
            "INSERT INTO observed_recalls VALUES('1','5','observed',?)", (lower + 1,)
        )
        db.execute("INSERT INTO outbox VALUES('recall/1/5/outbox','unknown')")
    with closing(sqlite3.connect(case.root / "moderation.sqlite3")) as db, db:
        db.executescript(
            "CREATE TABLE content_violations(account,group_id,message,observed,status);"
            "CREATE TABLE flood_batches(operation,account,group_id,last_seen);"
            "CREATE TABLE flood_recalls(message,batch,status);"
            "CREATE TABLE moderation_action_audit(account,group_id,message,action,status,created);"
        )
        db.execute(
            "INSERT INTO content_violations VALUES('1','5','violation',?,'execution_rejected')",
            (lower + 1,),
        )
        db.execute("INSERT INTO flood_batches VALUES('batch','1','5',?)", (lower + 1,))
        db.execute("INSERT INTO flood_recalls VALUES('flood','batch','unknown')")
        db.execute(
            "INSERT INTO moderation_action_audit VALUES('1','5','audit','recall','accepted',?)",
            (lower + 1,),
        )
    selected = [r[2:5] for r in rows if r[:2] == ("1", "5") and lower <= r[5] < end]
    expected = legacy_ranked(
        selected,
        "1",
        "901",
        lower,
        min(end, case.clock[0] - 30 if settled else case.clock[0] + 1),
        violations,
        fixture=True,
    )
    assert next(entry for entry in expected if entry[0] == "10") == (
        "10",
        "New name",
        3,
        lower + 700,
    )
    assert all(entry[0] != "18" for entry in expected)
    result = await case.runtime.snapshot(
        case.adapter,
        "5",
        previous_day=previous_day,
        settled=settled,
        include_activity=include_activity,
    )
    assert result == (
        start.strftime("%Y-%m-%d"),
        end,
        expected if include_activity else [entry[:3] for entry in expected],
    )


@pytest.mark.asyncio
async def test_cpu_helpers_are_bounded_workers_and_caches_stay_on_loop(
    case, monkeypatch
):
    loop_thread = threading.get_ident()
    calls = []

    class LoopCache(OrderedDict):
        def __setitem__(self, key, value):
            assert threading.get_ident() == loop_thread
            return super().__setitem__(key, value)

    class Runtime(rankings.DailyRankings):
        def __setattr__(self, name, value):
            if name in {"cache", "settled_cache"}:
                assert threading.get_ident() == loop_thread
                value = LoopCache(value)
            super().__setattr__(name, value)

    case.runtime = Runtime()
    insert(
        case,
        [
            row(case, str(i), sender=str(i + 10), stamp=case.clock[0] - 86400 - 100)
            for i in range(65)
        ],
    )
    for name in ("_decode_batch", "_message_batch", "_digest_batch", "_reduce_batch"):
        original = getattr(rankings, name)

        def checked(*args, _name=name, _original=original):
            assert threading.get_ident() != loop_thread
            if _name in {"_decode_batch", "_message_batch"}:
                assert len(args[0]) <= 32
            elif _name == "_digest_batch":
                assert len(args[1]) <= 32
            calls.append(_name)
            return _original(*args)

        monkeypatch.setattr(rankings, name, checked)

    def managed(_uid):
        assert threading.get_ident() == loop_thread
        return False

    monkeypatch.setattr(rankings, "is_managed_account", managed)
    result = await case.runtime.snapshot(case.adapter, "5", previous_day=True)
    assert len(result[2]) == 65
    assert set(calls) == {
        "_decode_batch",
        "_message_batch",
        "_digest_batch",
        "_reduce_batch",
    }
    assert case.runtime.cache and case.runtime.settled_cache


def gate_worker(monkeypatch, name):
    """Hold one detached worker until the loop-side test explicitly releases it."""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = getattr(rankings, name)

    def blocked(*args):
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("Ranking test worker was not released")
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(rankings, name, blocked)
    return entered, release, finished


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "helper", ["_decode_batch", "_message_batch", "_digest_batch", "_reduce_batch"]
)
async def test_cancellation_never_publishes_a_late_worker_result(
    case, monkeypatch, helper
):
    insert(case, [row(case, "one", stamp=case.clock[0] - 86400 - 100)])
    entered, release, finished = gate_worker(monkeypatch, helper)
    task = asyncio.create_task(
        case.runtime.snapshot(case.adapter, "5", previous_day=True)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # The loop services this callback while the CPU worker remains blocked.
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not case.runtime.cache and not case.runtime.settled_cache
        assert not case.runtime.normalized_cache
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not case.runtime.cache and not case.runtime.settled_cache
    assert not case.runtime.normalized_cache
    assert (await case.runtime.snapshot(case.adapter, "5", previous_day=True))[2] == [
        ("10", "Member 10", 1)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["_message_batch", "_reduce_batch"])
@pytest.mark.parametrize(
    "change",
    [
        "account",
        "group",
        "disable",
        "epoch",
        "revision",
        "inactive",
        "tenant",
        "paused",
        "automation",
        "nim",
        "ledger",
        "connection",
        "reopen",
        "path",
        "inode",
        "day",
    ],
)
async def test_fresh_scope_rejects_mutation_during_worker(
    case, monkeypatch, helper, change
):
    insert(case, [row(case, "one", stamp=case.clock[0] - 86400 - 100)])
    entered, release, finished = gate_worker(monkeypatch, helper)
    task = asyncio.create_task(
        case.runtime.snapshot(case.adapter, "5", previous_day=True)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if change == "account":
            case.adapter.account = "2"
        elif change == "group":
            case.config["enabled_groups"] = []
        elif change == "disable":
            case.config["enable"] = False
        elif change == "epoch":
            case.config["_business_epoch"] += 1
        elif change == "revision":
            case.config["_business_revision"] = "policy-2"
        elif change == "inactive":
            case.config["_business_active"] = False
        elif change == "tenant":
            case.config["_open_revisions"]["5"] = "tenant-2"
        elif change == "paused":
            case.config["_open_paused"] = True
        elif change == "automation":
            case.config["moderation"]["automation_enabled"] = False
        elif change == "nim":
            case.adapter.nim_account = "902"
        elif change == "ledger":
            case.adapter.ledger = SimpleNamespace(path=case.path)
        elif change == "connection":
            case.adapter.ledger.db = None
        elif change == "reopen":
            case.adapter.ledger.db = object()
        elif change == "path":
            case.adapter.ledger.path = case.root / "other.sqlite3"
        elif change == "inode":
            original_stat = rankings.Path.stat

            def replaced_stat(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path == case.path:
                    return os.stat_result(
                        (result.st_mode, result.st_ino + 1, *result[2:])
                    )
                return result

            # SQLite still owns the file here; Windows forbids replacing it.
            monkeypatch.setattr(rankings.Path, "stat", replaced_stat)
        elif change == "day":
            case.clock[0] += 86400
        release.set()
        with pytest.raises(ProtocolError, match="ranking_scope"):
            await task
        assert not case.runtime.cache and not case.runtime.settled_cache
        assert not case.runtime.normalized_cache
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fixture_revoked_during_cpu_work_is_rechecked_on_loop(case, monkeypatch):
    fixture = SimpleNamespace(operation="fixture", ranking=True)
    current = [fixture]
    monkeypatch.setattr(rankings, "flood_fixture", lambda *_: current[0])
    insert(case, [row(case, "one")])
    entered, release, finished = gate_worker(monkeypatch, "_message_batch")
    task = asyncio.create_task(case.runtime.snapshot(case.adapter, "5"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        current[0] = None
        release.set()
        with pytest.raises(ProtocolError, match="ranking_scope"):
            await task
        assert not case.runtime.cache and not case.runtime.settled_cache
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiting_for_scan_lock_does_not_reuse_old_scope(case):
    insert(case, [row(case, "one")])
    async with case.runtime.lock:
        task = asyncio.create_task(case.runtime.snapshot(case.adapter, "5"))
        await asyncio.sleep(0)
        case.config["_business_epoch"] += 1
    with pytest.raises(ProtocolError, match="ranking_scope"):
        await task
    assert not case.runtime.cache and not case.runtime.settled_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_settled", [False, True])
async def test_revocation_during_database_close_cannot_publish_cache(
    case, monkeypatch, reuse_settled
):
    insert(case, [row(case, "one", stamp=case.clock[0] - 86400 - 100)])
    if reuse_settled:
        await case.runtime.snapshot(case.adapter, "5", previous_day=True)
        case.runtime.cache.clear()
    previous = dict(case.runtime.settled_cache)
    connect = rankings.aiosqlite.connect

    @asynccontextmanager
    async def revoke_on_close(*args, **kwargs):
        async with connect(*args, **kwargs) as db:
            yield db
        case.config["_business_epoch"] += 1

    monkeypatch.setattr(rankings.aiosqlite, "connect", revoke_on_close)
    with pytest.raises(ProtocolError, match="ranking_scope"):
        await case.runtime.snapshot(case.adapter, "5", previous_day=True)
    assert not case.runtime.cache
    assert case.runtime.settled_cache == previous


@pytest.mark.asyncio
async def test_policy_change_invalidates_warm_cache_without_caching_a_config(case):
    insert(case, [row(case, "one")])
    assert len((await case.runtime.snapshot(case.adapter, "5"))[2]) == 1
    insert(case, [row(case, "two", sender="11")])
    case.config["_business_revision"] = "policy-2"
    assert len((await case.runtime.snapshot(case.adapter, "5"))[2]) == 2
    case.config["enabled_groups"] = []
    with pytest.raises(ProtocolError, match="ranking_scope"):
        await case.runtime.snapshot(case.adapter, "5")


@pytest.mark.asyncio
async def test_normalized_cache_rechecks_recalls_settlement_and_managed_accounts(
    case, monkeypatch
):
    insert(
        case,
        [
            row(case, "first", stamp=case.clock[0] - 100),
            row(case, "recent", sender="11", stamp=case.clock[0] - 5),
            row(case, "managed", sender="9", stamp=case.clock[0] - 100),
        ],
    )
    first = await case.runtime.snapshot(case.adapter, "5", settled=True)
    assert first[2] == [("10", "Member 10", 1)]
    assert case.runtime.normalized_cache
    with closing(sqlite3.connect(case.path)) as db, db:
        db.execute(
            "INSERT INTO observed_recalls VALUES('1','5','first',?)", (case.clock[0],)
        )
    case.runtime.cache.clear()
    case.clock[0] += 60
    monkeypatch.setattr(rankings, "is_managed_account", lambda uid: False)
    monkeypatch.setattr(
        rankings,
        "_decode_batch",
        lambda *_: pytest.fail("Unchanged payloads must not be decoded twice"),
    )
    monkeypatch.setattr(
        rankings,
        "_message_batch",
        lambda *_: pytest.fail("Unchanged payloads must not be normalized twice"),
    )
    result = await case.runtime.snapshot(case.adapter, "5", settled=True)
    assert result[2] == [
        ("9", "Member 9", 1),
        ("11", "Member 11", 1),
    ]


@pytest.mark.asyncio
async def test_normalized_cache_is_bounded_and_invalidates_pruned_payload(
    case, monkeypatch
):
    monkeypatch.setattr(rankings, "NORMALIZED_CACHE_BATCHES", 2)
    insert(
        case,
        [
            row(case, str(i), sender=str(i + 10), stamp=case.clock[0] - 100)
            for i in range(100)
        ],
    )
    assert len((await case.runtime.snapshot(case.adapter, "5"))[2]) == 100
    assert len(case.runtime.normalized_cache) == 2
    with closing(sqlite3.connect(case.path)) as db, db:
        db.execute("UPDATE inbox SET payload='{}',pruned_at=1 WHERE message='99'")
    case.runtime.cache.clear()
    assert len((await case.runtime.snapshot(case.adapter, "5"))[2]) == 99


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["ledger", "connection"])
async def test_reopened_ledger_cannot_reuse_warm_cache(case, change):
    insert(case, [row(case, "one")])
    assert len((await case.runtime.snapshot(case.adapter, "5"))[2]) == 1
    insert(case, [row(case, "two", sender="11")])
    if change == "ledger":
        case.adapter.ledger = SimpleNamespace(path=case.path, db=object())
    else:
        case.adapter.ledger.db = object()
    assert len((await case.runtime.snapshot(case.adapter, "5"))[2]) == 2


async def measure(operation):
    """Measure a one-millisecond heartbeat around a synchronous or async operation."""
    gaps = []
    stopping = asyncio.Event()

    async def heartbeat():
        previous = time.perf_counter()
        while not stopping.is_set():
            await asyncio.sleep(0.001)
            current = time.perf_counter()
            gaps.append((current - previous) * 1000)
            previous = current

    pulse = asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(0.005)
        started = time.perf_counter()
        result = operation()
        if hasattr(result, "__await__"):
            result = await result
        elapsed = (time.perf_counter() - started) * 1000
        await asyncio.sleep(0.005)
    finally:
        stopping.set()
        await pulse
    return result, {"elapsed_ms": round(elapsed, 2), "max_gap_ms": round(max(gaps), 2)}


@pytest.mark.asyncio
@pytest.mark.parametrize("count,mixed", [(500, False), (1000, True)])
async def test_synthetic_latency_benchmark_and_result_parity(case, count, mixed):
    long_text = ("\uff28\uff45\uff4c\uff4c\uff4f\uff0c\u4e16\u754c " * 2000)[:16384]
    rows = [
        row(
            case,
            str(index),
            sender=str(1000 + index % 50),
            text=(
                f"Distinct ordinary sentence {index}"
                if mixed and index % 4
                else long_text
            ),
            stamp=case.clock[0] - 10000 + index * 7,
            state="needs_review" if mixed and index % 11 == 0 else "processed",
        )
        for index in range(count)
    ]
    insert(case, rows)
    start = (
        datetime.fromtimestamp(case.clock[0], ZoneInfo("Asia/Shanghai"))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    expected, before = await measure(
        lambda: legacy_ranked(
            [r[2:5] for r in rows], "1", "901", start, case.clock[0] - 30
        )
    )
    result, after = await measure(
        lambda: case.runtime.snapshot(
            case.adapter, "5", settled=True, include_activity=True
        )
    )
    assert result[2] == expected
    print(
        json.dumps(
            {
                "rows": count,
                "workload": "mixed" if mixed else "16384_fullwidth_cjk",
                "legacy_cpu_on_loop": before,
                "bounded_workers_with_sql": after,
            }
        )
    )
