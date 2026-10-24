"""Offline guards for the explicitly bounded native acceptance harness."""

import copy
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts import check_wsl_integrated_live as checks


@pytest.fixture
def harness(monkeypatch):
    test = object.__new__(checks.Acceptance)
    row = {"version": 1, "active": 0, "paused": 0}
    test.store = SimpleNamespace(tenant=Mock(return_value=copy.deepcopy(row)))
    test.expected = {checks.OWNER: copy.deepcopy(row)}
    test.replies = {"100": {"text": "fixed command reply"}}
    test.send = AsyncMock(return_value={"status": "accepted", "message": "9"})
    test.results = {"cases": [], "cleanup": {}}
    test.save = Mock()
    monkeypatch.setattr(checks.asyncio, "sleep", AsyncMock())
    return test


@pytest.mark.asyncio
async def test_logical_and_segment_receipts_count_one_physical_reply(
    harness, monkeypatch
):
    def rows(account, filename, statement, values):
        assert account == checks.OWNER and filename == "messages.sqlite3"
        if "FROM inbox" in statement:
            return [{"team": checks.GROUP, "state": "processed"}]
        assert values == (f"{checks.OWNER}/{checks.GROUP}/9/%",)
        return [
            {"state": "accepted", "server_id": "100"},
            {"state": "accepted", "server_id": "100"},
        ]

    monkeypatch.setattr(checks, "rows", rows)
    reply = await harness.command("管理 群列表", "group-list", "fixed command")
    assert reply == "fixed command reply"
    harness.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,delivered", [("unknown", True), ("rejected", True), ("accepted", False)]
)
async def test_unconfirmed_reply_fails_without_resubmitting(
    harness, monkeypatch, state, delivered
):
    if not delivered:
        harness.replies.clear()

    def rows(_account, _filename, statement, _values):
        if "FROM inbox" in statement:
            return [{"team": checks.GROUP, "state": "processed"}]
        return [{"state": state, "server_id": "100"}]

    monkeypatch.setattr(checks, "rows", rows)
    with pytest.raises(AssertionError, match="reply_missing"):
        await harness.command("管理 规则", "unconfirmed", "fixed command")
    harness.send.assert_awaited_once()
    assert not harness.results["cases"]


@pytest.mark.asyncio
async def test_silent_group_event_requires_no_outgoing_reply(harness, monkeypatch):
    monkeypatch.setattr(
        checks,
        "rows",
        lambda _account, _filename, statement, _values: (
            [{"team": checks.GROUP, "state": "processed"}]
            if "FROM inbox" in statement
            else []
        ),
    )
    assert (
        await harness.command("ordinary text", "group-silent", None, group=True) == ""
    )
    harness.send.assert_awaited_once_with(
        "ordinary text", "group-silent", account=checks.OWNER, group=True
    )


@pytest.mark.asyncio
async def test_prior_unknown_sender_intent_never_reaches_native_transport():
    test = object.__new__(checks.Acceptance)
    test.sender = SimpleNamespace(fields={3: checks.ORDINARY})
    test.last_sent = 0
    test.marker = "fixture"
    test.team = "55"
    test.ledger = SimpleNamespace(reserve=AsyncMock(return_value=(1, "unknown")))
    test.nim = SimpleNamespace(request=AsyncMock())
    with pytest.raises(AssertionError, match="must never be retried"):
        await test.send("test", "unknown", group=True, burst=True)
    test.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["1", checks.ORDINARY])
async def test_send_refuses_any_target_account_outside_the_two_test_bots(account):
    test = object.__new__(checks.Acceptance)
    with pytest.raises(AssertionError):
        await test.send("test", "wrong-account", account=account, burst=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_member", [False, True])
async def test_concurrent_tenant_change_is_preserved_and_sender_resources_close(
    harness, monkeypatch, tmp_path, restore_member
):
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    path = tmp_path / "data/plugin_data/wangshangliao_cashback/cashback.sqlite3"
    path.parent.mkdir(parents=True)
    baseline = tmp_path / "cashback-before.sqlite3"
    schema = "CREATE TABLE entries(id,group_id,member,inviter,amount,paid_at,paid_by,member_class)"
    for database in (path, baseline):
        with sqlite3.connect(database) as db:
            db.execute(schema)
    harness.output = tmp_path
    harness.mute_possible = harness.group_mute_possible = False
    harness.card_changed = harness.schedule_created = harness.activity_created = False
    harness.native_before = {}
    harness.before = copy.deepcopy(harness.expected)
    harness.other_tenants = []
    harness.store.tenant.return_value = {"version": 2, "active": 0, "paused": 0}
    if restore_member:
        harness.expected = {
            checks.PEER: {
                "version": 1,
                "active": 0,
                "paused": 0,
                "present": 0,
                "managers": [],
            }
        }
        harness.before = copy.deepcopy(harness.expected)
        harness.store.tenant.return_value.update(present=1, managers=["other-manager"])
        pending = tmp_path / "membership.sqlite3"
        with sqlite3.connect(pending) as db:
            db.execute("CREATE TABLE attempts(action TEXT,state TEXT)")
            db.execute("INSERT INTO attempts VALUES('invite','unknown')")
        harness.membership_removed = True
        harness.membership_inviter = SimpleNamespace(
            path=pending,
            finish_invitation=AsyncMock(return_value={"state": "verified"}),
        )
        harness.business = {checks.PEER: object()}
    harness.store.tenants = lambda: []
    harness.patch = AsyncMock()
    harness.state = AsyncMock(
        return_value={
            "groups": [
                {
                    "account": checks.PEER if restore_member else checks.OWNER,
                    "group_id": checks.GROUP,
                    "active": 0,
                    "paused": 0,
                }
            ]
        }
    )
    harness.tasks = []
    harness.nim = SimpleNamespace(close=AsyncMock())
    harness.ledger = SimpleNamespace(close=AsyncMock())
    with pytest.raises(AssertionError):
        await harness.cleanup()
    harness.patch.assert_not_awaited()
    assert harness.store.tenant.return_value["version"] == 2
    if restore_member:
        assert harness.store.tenant.return_value["managers"] == ["other-manager"]
        assert (
            "membership_restore:AssertionError" in harness.results["cleanup"]["errors"]
        )
    harness.nim.close.assert_awaited_once()
    harness.ledger.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_retired_membership_refuses_before_io_or_history_changes(
    harness, monkeypatch
):
    """Reject direct legacy calls without touching transport or durable state.

    Args:
        harness: Lightweight acceptance object with existing test state.
        monkeypatch: Scoped fail-closed IO replacements.
    """
    before = copy.deepcopy((harness.expected, harness.results))
    file_io = Mock(side_effect=AssertionError("Legacy membership attempted file IO"))
    database = Mock(side_effect=AssertionError("Legacy membership accessed history"))
    native = AsyncMock(side_effect=AssertionError("Legacy membership attempted IO"))
    harness.patch = harness.state = harness.command = harness.send = native
    harness.save = file_io
    harness.store.connect = database
    harness.business = {
        account: SimpleNamespace(request=native) for account in checks.INSTANCES
    }
    harness.nim = SimpleNamespace(
        get_friends=native, get_team_inviters=native, request=native
    )
    monkeypatch.setattr(checks.sqlite3, "connect", database)
    monkeypatch.setattr(checks, "rows", database)
    monkeypatch.setattr(checks, "member_directory", native)
    monkeypatch.setattr(checks, "execute", native)
    for method in ("read_text", "write_text", "mkdir"):
        monkeypatch.setattr(checks.Path, method, file_io)

    with pytest.raises(RuntimeError, match="Retired ordinary-inviter workflow") as exc:
        await harness.membership()

    assert "scripts.wsl_test_lab" in str(exc.value)
    assert (harness.expected, harness.results) == before
    harness.store.tenant.assert_not_called()
    file_io.assert_not_called()
    database.assert_not_called()
    native.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        ["--apply", "--phase", "membership"],
        ["--apply", "--full"],
        ["--apply", "--phase", "management", "--phase", "membership"],
        ["--apply", "--full", "--phase", "management"],
    ],
)
async def test_retired_cli_phases_fail_before_config_network_or_history(
    monkeypatch, capsys, arguments
):
    """Reject legacy CLI selections before reading config or constructing IO owners.

    Args:
        monkeypatch: Scoped argv and fail-closed IO replacements.
        capsys: Captured parser diagnostics.
        arguments: Explicit legacy live workflow selection.
    """
    file_io = Mock(side_effect=AssertionError("Legacy CLI attempted file IO"))
    database = Mock(side_effect=AssertionError("Legacy CLI accessed history"))
    network = Mock(
        side_effect=AssertionError("Legacy CLI constructed a network client")
    )
    acceptance = Mock(side_effect=AssertionError("Legacy CLI constructed acceptance"))
    token = Mock(side_effect=AssertionError("Legacy CLI accessed authentication"))
    monkeypatch.setattr(sys, "argv", ["check_wsl_integrated_live", *arguments])
    for method in ("read_text", "write_text", "mkdir"):
        monkeypatch.setattr(checks.Path, method, file_io)
    monkeypatch.setattr(checks.sqlite3, "connect", database)
    monkeypatch.setattr(checks.httpx, "AsyncClient", network)
    monkeypatch.setattr(checks.aiohttp, "ClientSession", network)
    monkeypatch.setattr(checks, "Acceptance", acceptance)
    monkeypatch.setattr(checks.jwt, "encode", token)

    with pytest.raises(SystemExit) as exc:
        await checks.main()

    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "historical full/membership workflow is retired" in error
    assert "scripts.wsl_test_lab" in error
    file_io.assert_not_called()
    database.assert_not_called()
    network.assert_not_called()
    acceptance.assert_not_called()
    token.assert_not_called()
