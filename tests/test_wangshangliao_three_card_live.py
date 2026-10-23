"""Offline native card acceptance: real jobs/action-port/SQLite, fake upstream IO."""

import asyncio
import copy
import json
import socket
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio

from scripts import check_wsl_three_card_live as script


class Clock:
    """Advance only the script's explicit pacing; production core remains real."""

    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class Upstream:
    """Emulate only external account status and native business HTTP."""

    def __init__(self, clock):
        self.clock = clock
        self.fields = [0, 0, 0, int(script.ADMIN)]
        self.accounts = [
            {"account": uid, "instance_id": instance, "state": "online"}
            for uid, instance in script.BINDINGS.items()
        ]
        self.pins = {uid: "nim-" + uid for uid in script.BINDINGS}
        self.members = [
            {
                "userId": uid,
                "nimId": self.pins[uid],
                "groupRole": script.ROLES[uid],
                "accountState": "ACCOUNT_STATE_GOOD",
                "groupMemberNick": "Original-" + uid,
            }
            for uid in script.BINDINGS
        ]
        self.calls = []
        self.writes = []
        self.mode = "normal"
        self.group_role = "GROUP_ROLE_ADMIN"
        self.after_authorize = None

    async def status(self):
        self.calls.append(("dashboard", self.clock()))
        return {"accounts": copy.deepcopy(self.accounts)}

    async def request(self, route, params, *, authorize=None):
        if authorize is not None:
            await authorize()
        if self.after_authorize is not None:
            await self.after_authorize(route)
        self.calls.append((route, self.clock()))
        if route == script.LIST:
            return {
                "owner": [],
                "member": [
                    {"groupId": int(script.GROUP), "me": {"role": self.group_role}}
                ],
            }
        if route == script.MEMBERS:
            return {"groupMemberInfo": copy.deepcopy(self.members)}
        assert route == script.RENAME
        self.writes.append((copy.deepcopy(params), self.clock()))
        target = next(m for m in self.members if m["userId"] == script.MEMBER)
        if self.mode == "unknown":
            target["groupMemberNick"] = params["nick"]
            raise TimeoutError("upstream secrets must never reach evidence")
        if self.mode == "accepted_no_change":
            return {}
        target["groupMemberNick"] = params["nick"]
        return {}


@pytest_asyncio.fixture
async def scope(tmp_path, monkeypatch):
    """Keep all real runtime stores in the test root and forbid network access."""
    evidence = script.Evidence(tmp_path / "control")
    monkeypatch.setenv("ASTRBOT_ROOT", str(evidence.root / "runtime"))
    monkeypatch.setenv("ASTRBOT_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    monkeypatch.delenv("ASTRBOT_WSL_OPEN", raising=False)
    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(
            socket.socket, method, Mock(side_effect=AssertionError("No network"))
        )
    monkeypatch.setattr(
        socket, "getaddrinfo", Mock(side_effect=AssertionError("No DNS"))
    )
    from astrbot.core import astrbot_config

    # Isolate the process registry without weakening either managed-target guard.
    monkeypatch.setitem(astrbot_config, "platform", [])
    clock = Clock()
    upstream = Upstream(clock)
    pace = script.PacedIO(clock=clock, sleep=clock.sleep)
    transport = script.NativeTransport(
        upstream, upstream.status, upstream.pins, evidence, pace
    )
    check = script.CardCheck(transport, evidence)
    try:
        yield SimpleNamespace(
            evidence=evidence,
            clock=clock,
            upstream=upstream,
            transport=transport,
            check=check,
            config=astrbot_config,
        )
    finally:
        await check.jobs.close()
        evidence.close()


def test_default_is_credential_free_plan(monkeypatch, capsys):
    monkeypatch.setattr(
        script, "Evidence", Mock(side_effect=AssertionError("No writes"))
    )
    monkeypatch.setattr(script, "live_check", Mock(side_effect=AssertionError("No IO")))
    assert script.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["target"] == script.MEMBER
    assert plan["maximum_native_writes"] == 2


@pytest.mark.asyncio
async def test_real_jobs_action_port_native_receipts_and_restore(scope):
    baseline = copy.deepcopy(scope.upstream.members)
    report = await scope.check.run(lambda job: True)
    assert report["state"] == "verified_restored"
    assert report["role_writes"] == report["extra_nim_connections"] == 0
    assert "not chat" in report["boundary"]
    assert scope.upstream.members == baseline
    assert len(scope.upstream.writes) == 2
    first, second = scope.upstream.writes
    assert first[0]["nick"] == report["proposed"]
    assert second[0]["nick"] == report["original"]
    assert second[1] - first[1] >= 30
    times = [timestamp for _, timestamp in scope.upstream.calls]
    assert all(b - a >= 3 for a, b in zip(times, times[1:]))
    for phase in ("change", "restore"):
        receipt = next(r for r in report["receipts"] if r["phase"] == phase)
        assert receipt["native"]["submitted"] is True
        assert receipt["native"]["status"] == "verified"
        assert receipt["job"]["items"][0]["state"] == "verified"
    assert scope.evidence.db.execute(
        "SELECT state FROM attempts ORDER BY created"
    ).fetchall() == [("verified",), ("verified",)]
    for name in ("cards.sqlite3", "moderation.sqlite3"):
        path = scope.check.storage / name
        assert path.is_relative_to(scope.evidence.root)
        assert path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(path) as db:
            table = "card_jobs" if name == "cards.sqlite3" else "operations"
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 2
    assert (scope.evidence.root / "evidence.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_preview_declined_never_writes(scope):
    report = await scope.check.run(lambda job: False)
    assert report["state"] == "not_confirmed"
    assert len(report["previews"]) == 1
    assert not scope.upstream.writes
    assert not scope.evidence.db.execute("SELECT * FROM attempts").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "binding",
        "offline",
        "extra_account",
        "role",
        "group_role",
        "nim",
        "extra_member",
        "bad_state",
        "empty_original",
        "old_canary",
    ],
)
async def test_preconditions_fail_closed_without_role_repair(scope, change):
    upstream = scope.upstream
    if change == "binding":
        upstream.accounts[-1]["instance_id"] = "retired-binding"
    elif change == "offline":
        upstream.accounts[-1]["state"] = "offline"
    elif change == "extra_account":
        upstream.accounts.append(dict(upstream.accounts[0]))
    elif change == "role":
        upstream.members[1]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif change == "group_role":
        upstream.group_role = "GROUP_ROLE_MEMBER"
    elif change == "nim":
        upstream.members[-1]["nimId"] = "changed"
    elif change == "extra_member":
        upstream.members.append(
            {**upstream.members[0], "userId": "9999", "nimId": "nim-fourth"}
        )
    elif change == "bad_state":
        upstream.members[-1]["accountState"] = "ACCOUNT_STATE_BAN"
    elif change == "empty_original":
        upstream.members[-1]["groupMemberNick"] = ""
    else:
        upstream.members[-1]["groupMemberNick"] = "WSL_TEST_OLD_UNRESOLVED"
    with pytest.raises(RuntimeError):
        await scope.check.run(lambda job: True)
    assert not upstream.writes
    assert not scope.evidence.db.execute("SELECT * FROM attempts").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unknown", "accepted_no_change"])
async def test_ambiguous_write_never_retries_or_restores(scope, mode):
    scope.upstream.mode = mode
    with pytest.raises(RuntimeError, match="write_not_verified"):
        await scope.check.run(lambda job: True)
    assert len(scope.upstream.writes) == 1
    assert len(scope.evidence.report["previews"]) == 1
    assert scope.evidence.db.execute("SELECT state FROM attempts").fetchall() == [
        ("unknown",)
    ]
    assert "upstream secrets" not in (scope.evidence.root / "evidence.json").read_text()
    control = scope.evidence.root.parent
    scope.evidence.close()
    with pytest.raises(RuntimeError, match="prior_run_requires_manual_review"):
        script.Evidence(control)


@pytest.mark.asyncio
async def test_provider_rejection_is_recorded_without_request_or_credentials(scope):
    from astrbot.core.platform.sources.wangshangliao.business import BusinessError

    original = scope.upstream.request

    async def rejected(route, params, *, authorize=None):
        if route != script.RENAME:
            return await original(route, params, authorize=authorize)
        error = BusinessError("business_rejected", 1001)
        error.submitted = True
        raise error

    scope.upstream.request = rejected
    with pytest.raises(RuntimeError, match="write_not_verified"):
        await scope.check.run(lambda job: True)
    assert scope.evidence.report["provider_errors"] == [
        {
            "route": script.RENAME,
            "code": "business_rejected",
            "number": 1001,
            "submitted": True,
        }
    ]
    assert len(scope.evidence.report["receipts"]) == 1
    assert scope.evidence.report["receipts"][0]["native"]["status"] == "unknown"
    assert scope.evidence.db.execute("SELECT state FROM attempts").fetchall() == [
        ("unknown",)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["card", "role", "binding", "expired"])
async def test_changes_after_preview_prevent_native_write(scope, change):
    def confirm(job):
        if change == "card":
            scope.upstream.members[-1]["groupMemberNick"] = "Changed-by-someone-else"
        elif change == "role":
            scope.upstream.members[1]["groupRole"] = "GROUP_ROLE_MEMBER"
        elif change == "binding":
            scope.upstream.accounts[-1]["instance_id"] = "replacement"
        else:
            # Expire the real persisted preview rather than mocking CardJobs.start.
            with sqlite3.connect(scope.check.storage / "cards.sqlite3") as db:
                stale = copy.deepcopy(job)
                stale["expires"] = 0
                db.execute(
                    "UPDATE card_jobs SET data=? WHERE id=?",
                    (json.dumps(stale), job["id"]),
                )
        return True

    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    with pytest.raises((ProtocolError, RuntimeError)):
        await scope.check.run(confirm)
    assert not scope.upstream.writes


@pytest.mark.asyncio
async def test_production_managed_protection_is_not_bypassed(scope):
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    scope.config["platform"].append(
        {
            "id": script.BINDINGS[script.MEMBER],
            "type": "wangshangliao",
            "account_id": script.MEMBER,
        }
    )
    with pytest.raises(ProtocolError, match="moderation_target"):
        await scope.check.run(lambda job: True)
    assert not scope.upstream.writes
    with pytest.raises(RuntimeError, match="fresh_isolated_root"):
        script.CardCheck(scope.transport, scope.evidence)


@pytest.mark.asyncio
async def test_native_route_allowlist_and_unarmed_write(scope):
    for route, params in (
        (script.RENAME, {"groupId": int(script.GROUP), "userId": int(script.MEMBER)}),
        (script.MEMBERS, {"groupId": 1516387, "v": "0"}),
        ("/v1/group/set-member-role", {"groupId": int(script.GROUP)}),
        (script.LIST, {"v": "0", "extra": "unsafe"}),
    ):
        with pytest.raises(RuntimeError, match="native_route_forbidden"):
            await scope.transport.request(route, params)
    assert not scope.upstream.calls


def test_journal_quota_counts_unknown_and_forbids_operation_replay(tmp_path):
    evidence = script.Evidence(tmp_path / "control")
    try:
        evidence.reserve("one", "change")
        with pytest.raises(sqlite3.IntegrityError):
            evidence.reserve("one", "change")
        evidence.reserve("two", "restore")
        with pytest.raises(RuntimeError, match="two_write_limit"):
            evidence.reserve("three", "restore")
    finally:
        evidence.close()


def test_rolling_daily_cap_is_not_a_success_counter(tmp_path):
    evidence = script.Evidence(tmp_path / "control")
    try:
        with evidence.db:
            evidence.db.executemany(
                "INSERT INTO attempts VALUES(?,?,?,'unknown',?)",
                [
                    (f"old-{i}", "other-run", "change", script.time.time())
                    for i in range(20)
                ],
            )
        with pytest.raises(RuntimeError, match="rolling_daily_card_cap"):
            evidence.reserve("one", "change")
    finally:
        evidence.close()


@pytest.mark.asyncio
async def test_verified_receipt_without_independent_readback_cannot_restore(scope):
    reads = 0

    async def changed_after_receipt(route):
        nonlocal reads
        if route == script.MEMBERS and len(scope.upstream.writes) == 1:
            reads += 1
            if reads == 2:
                scope.upstream.members[-1]["groupMemberNick"] = "External-change"

    scope.upstream.after_authorize = changed_after_receipt
    with pytest.raises(RuntimeError, match="independent_native_readback_failed"):
        await scope.check.run(lambda job: True)
    assert len(scope.upstream.writes) == 1
    assert scope.evidence.report["receipts"][0]["native"]["status"] == "verified"
    assert scope.evidence.db.execute("SELECT state FROM attempts").fetchall() == [
        ("unknown",)
    ]
    assert len(scope.evidence.report["previews"]) == 1


@pytest.mark.asyncio
async def test_failed_restore_is_not_replayed_or_claimed_restored(scope):
    async def fail_restore(route):
        if route == script.RENAME and len(scope.upstream.writes) == 1:
            scope.upstream.mode = "unknown"

    scope.upstream.after_authorize = fail_restore
    with pytest.raises(RuntimeError, match="write_not_verified"):
        await scope.check.run(lambda job: True)
    assert len(scope.upstream.writes) == 2
    assert scope.evidence.report["state"] == "stopped_manual_review_no_replay"
    assert scope.evidence.db.execute(
        "SELECT state FROM attempts ORDER BY created"
    ).fetchall() == [("verified",), ("unknown",)]
    assert scope.evidence.db.execute(
        "SELECT state FROM runs WHERE id=?", (scope.evidence.run,)
    ).fetchone() == ("incomplete",)


@pytest.mark.asyncio
async def test_cancelled_submission_keeps_unknown_and_never_restores(scope):
    entered = asyncio.Event()

    async def wait_in_upstream(route):
        if route == script.RENAME:
            entered.set()
            await asyncio.Event().wait()

    scope.upstream.after_authorize = wait_in_upstream
    task = asyncio.create_task(scope.check.run(lambda job: True))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert len(scope.evidence.report["previews"]) == 1
        assert scope.evidence.db.execute("SELECT state FROM attempts").fetchall() == [
            ("unknown",)
        ]
        assert not scope.check.jobs.tasks
        with sqlite3.connect(scope.check.storage / "moderation.sqlite3") as db:
            result = json.loads(
                db.execute("SELECT result FROM operations").fetchone()[0]
            )
        assert result["status"] == "unknown"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
