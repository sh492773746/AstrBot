"""Offline acceptance of the separately invoked controlled membership test lab."""

import ast
import asyncio
import copy
import hashlib
import json
import socket
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit

import pytest

from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.business_http import (
    _exchange_business as signed_exchange,
)
from astrbot.core.platform.sources.wangshangliao.business_http import request_business
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from scripts import wsl_test_lab as scope

GROUP = "1459282"
MEMBERS = "/v1/group/get-group-members"
GROUPS = "/v1/group/get-group-list"
LOGS = "/v1/group/get-apply-logs"
GRANT = "/v1/group/add-group-manage"
REVOKE = "/v1/group/del-group-manage"


@pytest.fixture(autouse=True)
def offline_only(tmp_path, monkeypatch):
    """Fail closed on network, credential loading and non-fixture SQLite access.

    Args:
        tmp_path: Exclusive root for every database this test may access.
        monkeypatch: Test-scoped dependency replacements.

    Returns:
        Captured SQLite opens for read-only protection assertions.
    """
    opens = []
    connect = sqlite3.connect

    def denied(*args, **kwargs):
        pytest.fail("Live network or credential access is forbidden")

    def local_connect(database, *args, **kwargs):
        raw = str(database)
        path = (
            Path(unquote(urlsplit(raw).path))
            if raw.startswith("file:")
            else Path(database)
        )
        assert path.resolve().is_relative_to(tmp_path.resolve()), raw
        opens.append((raw, kwargs.copy()))
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(scope.aiohttp, "ClientSession", denied)
    monkeypatch.setattr(scope, "Vault", denied)
    monkeypatch.setattr(scope, "Deployment", SimpleNamespace(load=denied))
    monkeypatch.setattr(
        scope, "sqlite3", SimpleNamespace(connect=local_connect, Row=sqlite3.Row)
    )
    return opens


class _Lab:
    """Supply native directory responses without bypassing asynchronous admission."""

    def __init__(self, root):
        self.root = root
        self.now = 1_700_000_000.0
        self.group = GROUP
        self.digest = "a" * 64
        # Only the peer pin comes from the reported receipt; the others are fixtures.
        self.pins = {
            scope.OWNER: "1000000001",
            scope.PEER: "1698050908",
            scope.ORDINARY: "1000000003",
        }
        self.clients = {
            uid: SimpleNamespace(
                fields=[0, 0, 0, int(uid)],
                lock=asyncio.Lock(),
                http=SimpleNamespace(post=Mock(side_effect=AssertionError("Live IO"))),
            )
            for uid in (scope.OWNER, scope.PEER)
        }
        self.members = {uid: self.member(uid) for uid in (scope.OWNER, scope.ORDINARY)}
        self.logs = []
        self.calls = []
        self.write_error = None
        self.read_error = None
        self.after_write = None
        self.sleep_hook = None
        self.member_pages = None
        self.former_owner_role = "GROUP_ROLE_MEMBER"
        self.worker = self.create()

    def member(self, uid):
        return {
            "userId": int(uid),
            "nimId": self.pins.get(uid, "nim-" + uid),
            "groupRole": "GROUP_ROLE_OWNER"
            if uid == scope.OWNER
            else "GROUP_ROLE_MEMBER",
            "accountState": "ACCOUNT_STATE_GOOD",
        }

    def create(self, **overrides):
        return scope.TestLab(
            overrides.get("clients", self.clients),
            overrides.get("pins", self.pins),
            overrides.get("group", self.group),
            overrides.get("control", self.root / "journal"),
            protection=overrides.get(
                "protection", lambda group, first=False: self.digest
            ),
            daily_write_limit=overrides.get("daily_write_limit", 10),
            extra_role_pair_day=overrides.get("extra_role_pair_day", ""),
            extra_role_pair_round=overrides.get("extra_role_pair_round", 1),
        )

    def rows(self, table):
        assert table in {"previews", "attempts", "pacing"}
        with closing(sqlite3.connect(self.worker.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    @property
    def writes(self):
        return [call for call in self.calls if call["route"] in scope.ROUTES.values()]

    async def sleep(self, seconds):
        self.now += seconds
        if seconds > 0 and self.sleep_hook:
            hook, self.sleep_hook = self.sleep_hook, None
            hook()
        await asyncio.sleep(0)

    async def exchange(self, client, route, params, *, authorize, submission):
        """Emulate request completion only after the real caller's async guard.

        Args:
            client: Bound controlled business identity.
            route: Requested native route.
            params: Exact submitted request body.
            authorize: Async scope, snapshot and pacing callback.
            submission: Request-local transport submission evidence.

        Returns:
            Native directory, invitation evidence or mutation receipt.

        Raises:
            BaseException: Injected post-submission transport uncertainty.
        """
        await authorize()
        submission["started"] = True
        call = {
            "actor": str(client.fields[3]),
            "route": route,
            "params": copy.deepcopy(params),
            "start": self.now,
        }
        self.calls.append(call)
        try:
            if route not in scope.ROUTES.values() and self.read_error:
                error, self.read_error = self.read_error, None
                raise error
            if route == GROUPS:
                return {
                    "owner": [
                        {
                            "groupId": int(self.group),
                            "groupCloudId": "991234",
                            "groupName": "Controlled acceptance",
                        }
                    ],
                    "member": [],
                }
            if route == MEMBERS:
                if self.member_pages is not None:
                    return copy.deepcopy(self.member_pages[params.get("cursor", "")])
                return {"groupMemberInfo": copy.deepcopy(list(self.members.values()))}
            if route == LOGS:
                return {"list": copy.deepcopy(self.logs)}
            if route == scope.INVITE:
                assert call["actor"] == scope.OWNER
                assert params == {
                    "groupId": int(self.group),
                    "groupMemberIds": [int(scope.PEER)],
                }
                self.logs.append(
                    {
                        "groupId": int(self.group),
                        "applyId": str(77 + len(self.logs)),
                        "groupApplyType": "INVITATION",
                        "state": "MEMBER_STATE_INVITED",
                        "applyTime": datetime.fromtimestamp(
                            self.now, timezone.utc
                        ).isoformat(),
                        "applicant": {
                            "userId": int(scope.PEER),
                            "nimId": int(self.pins[scope.PEER]),
                        },
                        "inviter": {"userId": int(scope.OWNER)},
                    }
                )
            elif route == scope.ACCEPT:
                assert call["actor"] == scope.PEER
                record = next(r for r in self.logs if r["applyId"] == params["applyId"])
                assert params == {"applyId": record["applyId"], "agree": True}
                record["state"] = "MEMBER_STATE_GOOD"
                self.members[scope.PEER] = self.member(scope.PEER)
            elif route == scope.REMOVE:
                assert call["actor"] == scope.OWNER
                assert params == {
                    "groupId": int(self.group),
                    "groupMemberIds": [int(scope.PEER)],
                }
                self.members.pop(scope.PEER)
            elif route == scope.TRANSFER:
                target = scope.PEER if call["actor"] == scope.OWNER else scope.OWNER
                assert params == {"groupId": int(GROUP), "userId": int(target)}
                assert type(params["groupId"]) is type(params["userId"]) is int
                assert self.members[call["actor"]]["groupRole"] == "GROUP_ROLE_OWNER"
                self.members[call["actor"]]["groupRole"] = self.former_owner_role
                self.members[target]["groupRole"] = "GROUP_ROLE_OWNER"
            elif route in {GRANT, REVOKE}:
                assert call["actor"] == scope.OWNER
                assert type(params["groupId"]) is int
                if route == GRANT:
                    assert params == {
                        "groupId": int(self.group),
                        "groupManagerIds": [int(scope.PEER)],
                    }
                    assert type(params["groupManagerIds"]) is list
                    assert type(params["groupManagerIds"][0]) is int
                else:
                    assert params == {
                        "groupId": int(self.group),
                        "userId": int(scope.PEER),
                    }
                    assert type(params["userId"]) is int
                self.members[scope.PEER]["groupRole"] = (
                    "GROUP_ROLE_ADMIN" if route == GRANT else "GROUP_ROLE_MEMBER"
                )
            else:
                raise AssertionError(f"Unexpected route: {route}")
            if self.after_write:
                hook, self.after_write = self.after_write, None
                hook()
            if self.write_error:
                error, self.write_error = self.write_error, None
                raise error
            return {}
        finally:
            self.now += 0.25
            call["finish"] = self.now


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Bind the script's own clock and asyncio facade without mutating globals.

    Args:
        tmp_path: Private journal directory.
        monkeypatch: Scoped transport and timing replacements.

    Returns:
        Native response harness and the actual TestLab under test.
    """
    native = _Lab(tmp_path)
    monkeypatch.setattr(scope, "time", SimpleNamespace(time=lambda: native.now))
    monkeypatch.setattr(
        scope,
        "asyncio",
        SimpleNamespace(sleep=native.sleep, current_task=asyncio.current_task),
    )
    monkeypatch.setattr(scope, "_exchange_business", native.exchange)
    return native


@pytest.fixture
def protected_root(tmp_path):
    """Create only the local schema read by the protected-state guard.

    Args:
        tmp_path: Isolated application root.

    Returns:
        Paths to fixture configuration, finance and onboarding databases.
    """
    root = tmp_path / "application"
    data = root / "data/plugin_data"
    finance = data / "wangshangliao_cashback/cashback.sqlite3"
    source = data / "wangshangliao_open/open-service.sqlite3"
    finance.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    with closing(sqlite3.connect(finance)) as db, db:
        db.executescript("""
            CREATE TABLE groups(id TEXT PRIMARY KEY,price INTEGER,version INTEGER);
            INSERT INTO groups VALUES('1516387',18,1);
            CREATE TABLE entries(
                id TEXT PRIMARY KEY,group_id TEXT,member TEXT,inviter TEXT,
                amount INTEGER,state TEXT,verification TEXT,eligible INTEGER,
                present INTEGER,ever_left INTEGER,paid_at REAL,paid_by TEXT,
                member_class TEXT);
            INSERT INTO entries VALUES(
                'old','1516387','100','200',18,'paid','confirmed',1,1,0,
                1600000000,'admin','ordinary');
        """)
    with closing(sqlite3.connect(source)) as db, db:
        db.executescript("""
            CREATE TABLE tenants(
                account TEXT,group_id TEXT,active INTEGER,paused INTEGER,
                version INTEGER);
            INSERT INTO tenants VALUES('31846829','1516387',1,0,1);
            CREATE TABLE audit(account TEXT,group_id TEXT);
            CREATE TABLE retired_groups(group_id TEXT);
        """)
    config = root / "data/cmd_config.json"
    config.write_text('{"fixture":true}', encoding="ascii")
    return SimpleNamespace(root=root, finance=finance, source=source, config=config)


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary", [False, True])
@pytest.mark.parametrize("group", [GROUP, "1459283"])
async def test_first_invite_pending_then_explicit_accept_verified(lab, ordinary, group):
    lab.group = group
    if not ordinary:
        lab.members.pop(scope.ORDINARY)
    worker = lab.create()
    assert (scope.OWNER, scope.PEER, scope.ORDINARY) == (
        "31846829",
        "43506527",
        "23691273",
    )
    preview = await worker.preview("invite", first=True)
    saved = lab.rows("previews")[0]
    assert preview["expires"] == lab.now + 600
    assert (
        saved["digest"] == hashlib.sha256(preview["confirmation"].encode()).hexdigest()
    )
    assert preview["confirmation"] not in worker.path.read_bytes().decode("latin-1")
    assert json.loads(saved["payload"])["members"] == preview["members"]
    assert all(isinstance(row, list) for row in preview["members"])
    assert preview["actor"] == scope.OWNER and preview["target"] == scope.PEER
    assert (await worker.execute(preview["preview"], preview["confirmation"]))[
        "state"
    ] == "pending"
    assert scope.PEER not in lab.members
    assert [r["state"] for r in lab.rows("attempts")] == ["pending"]

    worker = lab.create()
    accept = await worker.preview("accept")
    assert accept["invitation"] == {
        "id": "77",
        "state": "MEMBER_STATE_INVITED",
        "attempt": preview["preview"],
    }
    result = await worker.execute(accept["preview"], accept["confirmation"])
    assert result["state"] == "verified"
    assert result["test_only"] is True
    assert result["activation_accepted"] is False
    assert result["phone_recall_confirmed"] is False
    assert [r["state"] for r in lab.rows("attempts")] == ["verified", "verified"]
    assert [(c["actor"], c["route"]) for c in lab.writes] == [
        (scope.OWNER, scope.INVITE),
        (scope.PEER, scope.ACCEPT),
    ]


@pytest.mark.asyncio
async def test_remove_then_invite_and_accept_pacing_survives_restart(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    for action, expected in (
        ("remove", "verified"),
        ("invite", "pending"),
        ("accept", "verified"),
    ):
        worker = lab.create()
        preview = await worker.preview(action)
        result = await worker.execute(preview["preview"], preview["confirmation"])
        assert result["state"] == expected
    assert [c["route"] for c in lab.writes] == [
        scope.REMOVE,
        scope.INVITE,
        scope.ACCEPT,
    ]
    assert all(
        later["start"] >= earlier["finish"] + 3
        for earlier, later in zip(lab.calls, lab.calls[1:])
    )
    assert all(
        later["start"] >= earlier["finish"] + 30
        for earlier, later in zip(lab.writes, lab.writes[1:])
    )
    pacing = lab.rows("pacing")[0]
    assert pacing["request_finished"] == lab.calls[-1]["finish"]
    assert pacing["write_finished"] == lab.writes[-1]["finish"]


@pytest.mark.parametrize(
    "group",
    [
        "1516387",
        "1488006",
        "0",
        "-1",
        "01459282",
        "１４５９２８２",
        str(1 << 63),
        "../1459282",
        "",
        1459282,
    ],
)
def test_formal_and_noncanonical_groups_rejected_without_io(lab, group):
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        lab.create(group=group)
    assert not lab.calls


@pytest.mark.parametrize(
    "change",
    ["missing_client", "extra_client", "missing_pin", "duplicate_pin", "empty_pin"],
)
def test_only_fixed_actors_and_distinct_transport_pins(lab, change):
    clients, pins = lab.clients.copy(), lab.pins.copy()
    if change == "missing_client":
        clients.pop(scope.PEER)
    elif change == "extra_client":
        clients[scope.ORDINARY] = clients[scope.OWNER]
    elif change == "missing_pin":
        pins.pop(scope.ORDINARY)
    elif change == "duplicate_pin":
        pins[scope.PEER] = pins[scope.OWNER]
    else:
        pins[scope.PEER] = ""
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        lab.create(clients=clients, pins=pins)
    assert not lab.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "owner_identity",
        "peer_identity",
        "owner_role",
        "ordinary_role",
        "peer_role",
        "pin",
        "extra_human",
        "extra_bad_human",
        "bad_ordinary",
        "missing_owner",
    ],
)
async def test_live_identity_exact_roster_roles_and_states_are_required(lab, change):
    action = "invite"
    if change.endswith("_identity"):
        uid = scope.OWNER if change == "owner_identity" else scope.PEER
        lab.clients[uid].fields[3] = int(scope.ORDINARY)
    elif change == "owner_role":
        lab.members[scope.OWNER]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif change == "ordinary_role":
        lab.members[scope.ORDINARY]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "peer_role":
        action = "remove"
        lab.members[scope.PEER] = lab.member(scope.PEER)
        lab.members[scope.PEER]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "pin":
        lab.members[scope.ORDINARY]["nimId"] = "someone-else"
    elif change in {"extra_human", "extra_bad_human"}:
        lab.members["999"] = lab.member("999")
        if change == "extra_bad_human":
            lab.members["999"]["accountState"] = "ACCOUNT_STATE_BAN"
    elif change == "bad_ordinary":
        lab.members[scope.ORDINARY]["accountState"] = "ACCOUNT_STATE_BAN"
    else:
        lab.members.pop(scope.OWNER)
    with pytest.raises(ProtocolError, match="test_lab_(identity|native_scope)"):
        await lab.worker.preview(action)
    assert not lab.writes
    assert not lab.rows("previews")


@pytest.mark.asyncio
async def test_native_pagination_cannot_hide_extra_humans(lab):
    lab.member_pages = {
        "": {"groupMemberInfo": list(lab.members.values()), "nextCursor": "tail"},
        "tail": {"groupMemberInfo": [lab.member("999")]},
    }
    with pytest.raises(ProtocolError, match="test_lab_native_scope"):
        await lab.worker.preview("invite")
    assert [c["params"].get("cursor") for c in lab.calls if c["route"] == MEMBERS] == [
        None,
        "tail",
    ]
    assert not lab.writes
    lab.member_pages[""] = {
        "groupMemberInfo": list(lab.members.values()),
        "hasMore": True,
    }
    with pytest.raises(ProtocolError, match="member_pagination_incomplete"):
        await lab.worker.preview("invite")


@pytest.mark.parametrize("table", ["groups", "entries"])
def test_financial_groups_are_denied_read_only(protected_root, offline_only, table):
    with closing(sqlite3.connect(protected_root.finance)) as db, db:
        if table == "groups":
            db.execute("INSERT INTO groups VALUES(?,18,1)", (GROUP,))
        else:
            db.execute("UPDATE entries SET group_id=?", (GROUP,))
    before = protected_root.finance.read_bytes()
    with pytest.raises(ProtocolError, match="test_lab_financial_group"):
        scope.protected_state(protected_root.root, GROUP)
    assert protected_root.finance.read_bytes() == before
    assert all("?mode=ro" in name and flags["uri"] for name, flags in offline_only)


@pytest.mark.parametrize("table", ["tenants", "audit", "retired_groups"])
def test_first_mode_rejects_peer_local_history_but_allows_owner_discovery(
    protected_root, table
):
    with closing(sqlite3.connect(protected_root.source)) as db, db:
        db.execute("INSERT INTO tenants VALUES(?,?,1,0,1)", (scope.OWNER, GROUP))
        db.execute("INSERT INTO audit VALUES(?,?)", (scope.OWNER, GROUP))
    assert scope.protected_state(protected_root.root, GROUP, first=True)
    with closing(sqlite3.connect(protected_root.source)) as db, db:
        if table == "tenants":
            db.execute("INSERT INTO tenants VALUES(?,?,1,0,1)", (scope.PEER, GROUP))
        elif table == "audit":
            db.execute("INSERT INTO audit VALUES(?,?)", (scope.PEER, GROUP))
        else:
            db.execute("INSERT INTO retired_groups VALUES(?)", (GROUP,))
    with pytest.raises(ProtocolError, match="test_lab_not_first_group"):
        scope.protected_state(protected_root.root, GROUP, first=True)
    assert scope.protected_state(protected_root.root, GROUP, first=False)


@pytest.mark.parametrize(
    "field", scope.FINANCIAL_FIELDS.split(",") + ["rules", "formal_binding", "config"]
)
def test_protected_digest_covers_every_financial_field_and_rules(protected_root, field):
    before = scope.protected_state(protected_root.root, GROUP)
    if field == "config":
        protected_root.config.write_text('{"fixture":false}', encoding="ascii")
    elif field == "formal_binding":
        with closing(sqlite3.connect(protected_root.source)) as db, db:
            db.execute("UPDATE tenants SET version=version+1")
    else:
        with closing(sqlite3.connect(protected_root.finance)) as db, db:
            if field == "rules":
                db.execute("UPDATE groups SET version=version+1")
            else:
                assert field in scope.FINANCIAL_FIELDS.split(",")
                db.execute(f"UPDATE entries SET {field}=?", ("changed",))
    assert scope.protected_state(protected_root.root, GROUP) != before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["remove", "accept", "grant", "revoke", "transfer-owner", "restore-owner"]
)
async def test_first_mode_never_kicks_or_accepts(lab, action):
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        await lab.worker.preview(action, first=True)
    assert not lab.calls


@pytest.mark.asyncio
async def test_first_mode_checks_fresh_signed_peer_history(lab, protected_root):
    lab.worker.protection = lambda group, first=False: scope.protected_state(
        protected_root.root, group, first=first
    )
    lab.logs = [
        {"groupId": int(GROUP), "applicant": {"userId": int(scope.OWNER)}},
        {"groupId": 1459283, "applicant": {"userId": int(scope.PEER)}},
    ]
    preview = await lab.worker.preview("invite", first=True)
    lab.logs.append({"groupId": int(GROUP), "applicant": {"userId": int(scope.PEER)}})
    with pytest.raises(ProtocolError, match="test_lab_not_first_group"):
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert lab.rows("previews")[0]["used"] == 0
    assert not lab.rows("attempts")
    assert not lab.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "wrong", "nonstring", "expired"])
async def test_invalid_confirmation_fails_before_any_new_request(lab, change):
    preview = await lab.worker.preview("invite")
    identity, token = preview["preview"], preview["confirmation"]
    if change == "missing":
        identity = "not-a-preview"
    elif change == "wrong":
        token += "-wrong"
    elif change == "nonstring":
        token = None
    else:
        lab.now = preview["expires"]
    count = len(lab.calls)
    with pytest.raises(ProtocolError, match="test_lab_confirmation"):
        await lab.worker.execute(identity, token)
    assert len(lab.calls) == count
    assert not lab.rows("attempts")
    assert lab.rows("previews")[0]["used"] == 0


@pytest.mark.asyncio
async def test_preview_one_use_and_group_bound_across_restart(lab):
    preview = await lab.worker.preview("invite")
    other = lab.create(group="1459283")
    count = len(lab.calls)
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        await other.execute(preview["preview"], preview["confirmation"])
    assert len(lab.calls) == count
    await lab.worker.execute(preview["preview"], preview["confirmation"])
    count = len(lab.calls)
    with pytest.raises(ProtocolError, match="test_lab_confirmation"):
        await lab.create().execute(preview["preview"], preview["confirmation"])
    with pytest.raises(ProtocolError, match="test_lab_attempt_scope"):
        await other.reconcile(preview["preview"])
    assert len(lab.calls) == count
    assert len(lab.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["protected", "roster", "pin"])
async def test_changed_snapshot_does_not_consume_confirmation(lab, change):
    preview = await lab.worker.preview("invite")
    if change == "protected":
        lab.digest = "b" * 64
    elif change == "roster":
        lab.members.pop(scope.ORDINARY)
    else:
        lab.members[scope.ORDINARY]["nimId"] = "changed-pin"
    with pytest.raises(ProtocolError):
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert lab.rows("previews")[0]["used"] == 0
    assert not lab.rows("attempts")
    assert not lab.writes


@pytest.mark.asyncio
async def test_unknown_invite_never_resends_and_reconciles_read_only(lab):
    preview = await lab.worker.preview("invite")
    lab.write_error = TimeoutError("Uncertain submitted request")
    with pytest.raises(TimeoutError) as caught:
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert caught.value.submitted is True
    assert lab.rows("attempts")[0]["state"] == "unknown"
    worker = lab.create()
    with pytest.raises(ProtocolError, match="test_lab_unresolved_action"):
        await worker.preview("invite")
    with pytest.raises(ProtocolError, match="test_lab_confirmation"):
        await worker.execute(preview["preview"], preview["confirmation"])
    assert (await worker.reconcile(preview["preview"]))["state"] == "pending"
    assert [c["route"] for c in lab.writes] == [scope.INVITE]
    accept = await worker.preview("accept")
    assert (await worker.execute(accept["preview"], accept["confirmation"]))[
        "state"
    ] == "verified"
    assert [c["route"] for c in lab.writes] == [scope.INVITE, scope.ACCEPT]


@pytest.mark.asyncio
@pytest.mark.parametrize("nim", [1698050908, "1698050908"], ids=["integer", "string"])
async def test_official_numeric_nim_recovers_accepted_invite_by_reads_only(lab, nim):
    """Recover a saved provider receipt without submitting another invitation.

    Args:
        lab: Isolated native transport, journal and roster with numeric string pins.
        nim: Official integer transport identity or its exact string representation.
    """
    assert lab.worker.pins[scope.PEER] == "1698050908"
    preview = await lab.worker.preview("invite")

    def interrupt_observation():
        lab.logs[0]["applyId"] = "211579722"
        lab.logs[0]["applicant"]["nimId"] = nim
        lab.read_error = TimeoutError(
            "Observation interrupted after provider acceptance"
        )

    lab.after_write = interrupt_observation
    with pytest.raises(TimeoutError):
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    saved = lab.rows("attempts")[0]
    assert saved["state"] == "accepted"
    assert saved["provider_accepted"] == 1
    assert scope.PEER not in lab.members
    count = len(lab.calls)
    for _ in range(2):
        result = await lab.create().reconcile(preview["preview"])
        assert result["state"] == "pending"
        assert result["provider_accepted"] is True
        assert result["invitation"] == {
            "id": "211579722",
            "state": "MEMBER_STATE_INVITED",
        }
    assert lab.calls[count:]
    assert all(call["route"] in scope.READS for call in lab.calls[count:])
    assert [call["route"] for call in lab.writes] == [scope.INVITE]
    assert lab.rows("attempts")[0]["apply_id"] == "211579722"
    assert scope.PEER not in lab.members


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nim",
    [
        pytest.param(1698050908.0, id="equal-valued-float"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(1698050909, id="other-integer"),
        pytest.param("1698050909", id="other-string"),
    ],
)
async def test_numeric_nim_rejects_float_bool_and_other_identity(lab, nim):
    """Keep invalid transport types and different identities out of invite proof.

    Args:
        lab: Isolated native transport, journal and roster with numeric string pins.
        nim: Invalid or mismatching applicant transport identity.
    """
    preview = await lab.worker.preview("invite")
    assert (await lab.worker.execute(preview["preview"], preview["confirmation"]))[
        "state"
    ] == "pending"
    assert type(lab.logs[0]["applicant"]["nimId"]) is int
    lab.logs[0]["applicant"]["nimId"] = nim
    worker = lab.create()
    with pytest.raises(ProtocolError, match="test_lab_invitation_unproven"):
        await worker.reconcile(preview["preview"])
    with pytest.raises(ProtocolError, match="test_lab_invitation_unproven"):
        await worker.preview("accept")
    saved = lab.rows("attempts")[0]
    assert saved["state"] == "pending"
    assert saved["provider_accepted"] == 1
    assert saved["apply_id"] == "77"
    assert [call["route"] for call in lab.writes] == [scope.INVITE]
    assert scope.PEER not in lab.members


@pytest.mark.asyncio
async def test_unknown_remove_cannot_be_verified_from_absence(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview("remove")
    lab.write_error = TimeoutError("Unknown remove receipt")
    with pytest.raises(TimeoutError):
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert scope.PEER not in lab.members
    assert (await lab.create().reconcile(preview["preview"]))["state"] == "unknown"
    with pytest.raises(ProtocolError, match="test_lab_unresolved_action"):
        await lab.create().preview("invite")
    assert [c["route"] for c in lab.writes] == [scope.REMOVE]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["invite", "remove"])
async def test_accepted_receipt_persisted_before_observation_failure(lab, action):
    if action == "remove":
        lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview(action)

    def fail_observe():
        lab.read_error = TimeoutError("Observation interrupted")

    lab.after_write = fail_observe
    with pytest.raises(TimeoutError):
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert lab.rows("attempts")[0]["state"] == "accepted"
    worker = lab.create()
    with pytest.raises(ProtocolError, match="test_lab_unresolved_action"):
        await worker.preview("invite")
    assert (await worker.reconcile(preview["preview"]))["state"] == (
        "pending" if action == "invite" else "verified"
    )
    assert len(lab.writes) == 1


@pytest.mark.asyncio
async def test_provider_receipt_survives_failed_read_and_contradictory_reconcile(lab):
    """Keep a successful provider receipt independent of later roster evidence.

    Args:
        lab: Isolated native transport, journal and roster.
    """
    lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview("remove")

    def fail_observe():
        lab.read_error = TimeoutError("Post-receipt observation interrupted")

    lab.after_write = fail_observe
    with pytest.raises(TimeoutError):
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    saved = lab.rows("attempts")[0]
    assert saved["state"] == "accepted"
    assert saved["provider_accepted"] == 1
    assert saved["error"] == ""

    lab.members[scope.PEER] = lab.member(scope.PEER)
    result = await lab.create().reconcile(preview["preview"])
    assert result["state"] == "unknown"
    assert result["provider_accepted"] is True
    saved = lab.rows("attempts")[0]
    assert saved["state"] == "unknown"
    assert saved["provider_accepted"] == 1

    lab.members.pop(scope.PEER)
    result = await lab.create().reconcile(preview["preview"])
    assert result["state"] == "verified"
    assert result["provider_accepted"] is True
    saved = lab.rows("attempts")[0]
    assert saved["state"] == "verified"
    assert saved["provider_accepted"] == 1
    assert saved["error"] == ""
    assert [call["route"] for call in lab.writes] == [scope.REMOVE]


@pytest.mark.asyncio
async def test_crashed_unknown_remove_with_empty_error_is_not_provider_receipt(lab):
    """Do not infer a missing receipt from an empty error and an absent target.

    Args:
        lab: Isolated native transport, journal and roster.
    """
    lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview("remove")
    payload = lab.rows("previews")[0]["payload"]
    # Reproduce a process exit after intent reservation but before receipt storage.
    with closing(sqlite3.connect(lab.worker.path)) as db, db:
        db.execute("UPDATE previews SET used=1 WHERE id=?", (preview["preview"],))
        db.execute(
            "INSERT INTO attempts(id,group_id,action,created,state,payload) "
            "VALUES(?,?,'remove',?,'unknown',?)",
            (preview["preview"], GROUP, lab.now, payload),
        )
    lab.members.pop(scope.PEER)

    for _ in range(2):
        result = await lab.create().reconcile(preview["preview"])
        assert result["state"] == "unknown"
        assert result["provider_accepted"] is False
        saved = lab.rows("attempts")[0]
        assert saved["state"] == "unknown"
        assert saved["provider_accepted"] == 0
        assert saved["error"] == ""
    with pytest.raises(ProtocolError, match="test_lab_confirmation"):
        await lab.create().execute(preview["preview"], preview["confirmation"])
    with pytest.raises(ProtocolError, match="test_lab_unresolved_action"):
        await lab.create().preview("invite")
    assert not lab.writes


def test_legacy_journal_migration_backfills_only_accepted_receipts(
    lab, tmp_path, protected_root, offline_only
):
    """Add receipt evidence without rewriting legacy rows or financial storage.

    Args:
        lab: Isolated native transport and journal factory.
        tmp_path: Private root for the legacy journal.
        protected_root: Isolated financial and configuration fixtures.
        offline_only: Captured script SQLite opens.
    """
    control = tmp_path / "legacy-journal"
    control.mkdir(mode=0o700)
    path = control / "control.sqlite3"
    protected = {
        file: file.read_bytes()
        for file in (
            protected_root.finance,
            protected_root.source,
            protected_root.config,
        )
    }
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE attempts("
            "id TEXT PRIMARY KEY,group_id TEXT NOT NULL,action TEXT NOT NULL,"
            "created REAL NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,"
            "apply_id TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '')"
        )
        db.executemany(
            "INSERT INTO attempts VALUES(?,?,'remove',?,?,?,'','')",
            [
                ("accepted", GROUP, lab.now - 60, "accepted", '{"old":"receipt"}'),
                ("unknown", GROUP, lab.now - 30, "unknown", '{"old":"uncertain"}'),
            ],
        )
        columns = [row[1] for row in db.execute("PRAGMA table_info(attempts)")]
        assert "provider_accepted" not in columns
        original = db.execute("SELECT * FROM attempts ORDER BY id").fetchall()

    opens_before = len(offline_only)
    for _ in range(2):
        worker = lab.create(control=control)
        with closing(sqlite3.connect(worker.path)) as db:
            migrated_columns = [
                row[1] for row in db.execute("PRAGMA table_info(attempts)")
            ]
            migrated = db.execute("SELECT * FROM attempts ORDER BY id").fetchall()
            assert migrated_columns == columns + ["provider_accepted", "evidence"]
            assert [row[:-2] for row in migrated] == original
            assert all(row[-1] == "{}" for row in migrated)
            assert dict(db.execute("SELECT id,provider_accepted FROM attempts")) == {
                "accepted": 1,
                "unknown": 0,
            }
    assert offline_only[opens_before:]
    assert all(name == str(path) for name, _ in offline_only[opens_before:])
    assert all(file.read_bytes() == before for file, before in protected.items())
    assert not lab.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["invite", "remove"])
@pytest.mark.parametrize("failure", ["pacing_finally", "accepted_update"])
async def test_post_submission_sql_error_never_becomes_non_submission(
    lab, monkeypatch, action, failure
):
    """Preserve uncertain submissions and known receipts after one local SQL error.

    Args:
        lab: Isolated native transport, journal and roster.
        monkeypatch: Scoped replacement for journal connections.
        action: Native membership mutation submitted exactly once.
        failure: Post-IO bookkeeping statement that fails once.
    """
    if action == "remove":
        lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview(action)
    connect = scope.sqlite3.connect
    armed = False
    failures = []

    class FailOnceConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal armed
            statement = (
                "UPDATE pacing SET request_finished="
                if failure == "pacing_finally"
                else "UPDATE attempts SET state='accepted'"
            )
            if armed and sql.startswith(statement):
                armed = False
                failures.append(sql)
                raise sqlite3.OperationalError("Injected post-submission SQL error")
            return super().execute(sql, parameters)

    def connect_with_fault(database, *args, **kwargs):
        kwargs["factory"] = FailOnceConnection
        return connect(database, *args, **kwargs)

    def arm_after_submission():
        nonlocal armed
        armed = True

    monkeypatch.setattr(scope.sqlite3, "connect", connect_with_fault)
    lab.after_write = arm_after_submission
    with pytest.raises(sqlite3.OperationalError) as caught:
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert not hasattr(caught.value, "submitted")
    assert len(failures) == 1
    saved = lab.rows("attempts")[0]
    assert saved["state"] == "unknown"
    assert saved["provider_accepted"] == 1
    assert saved["error"] == "OperationalError"
    assert lab.rows("previews")[0]["used"] == 1

    worker = lab.create()
    count = len(lab.calls)
    with pytest.raises(ProtocolError, match="test_lab_confirmation"):
        await worker.execute(preview["preview"], preview["confirmation"])
    assert len(lab.calls) == count
    with pytest.raises(ProtocolError, match="test_lab_unresolved_action"):
        await worker.preview("invite")
    result = await worker.reconcile(preview["preview"])
    assert result["state"] == ("pending" if action == "invite" else "verified")
    assert result["provider_accepted"] is True
    assert lab.rows("attempts")[0]["state"] != "not_submitted"
    assert len(failures) == 1
    assert [call["route"] for call in lab.writes] == [scope.ROUTES[action]]


@pytest.mark.asyncio
async def test_verified_receipts_are_terminal_after_remove_and_rejoin(lab):
    """Return historical receipts without consulting later platform membership.

    Args:
        lab: Isolated native transport, journal and roster.
    """
    lab.members[scope.PEER] = lab.member(scope.PEER)
    previews = []
    for action in ("remove", "invite", "accept"):
        preview = await lab.create().preview(action)
        await lab.create().execute(preview["preview"], preview["confirmation"])
        previews.append(preview["preview"])
    assert scope.PEER in lab.members
    saved = lab.rows("attempts")
    assert all(row["state"] == "verified" for row in saved)
    count = len(lab.calls)
    lab.read_error = AssertionError("Historical receipts must not read the platform")
    for identity in previews:
        result = await lab.create().reconcile(identity)
        assert result["state"] == "verified"
        assert result["provider_accepted"] is True
        assert result["historical_receipt"] is True
    assert len(lab.calls) == count
    assert lab.rows("attempts") == saved
    assert [call["route"] for call in lab.writes] == [
        scope.REMOVE,
        scope.INVITE,
        scope.ACCEPT,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "old",
        "wrong_inviter",
        "wrong_pin",
        "wrong_target",
        "wrong_group",
        "wrong_type",
        "invalid_time",
        "invalid_id",
        "bad_state",
    ],
)
async def test_accept_requires_exact_unique_fresh_invitation(lab, change):
    preview = await lab.worker.preview("invite")
    await lab.worker.execute(preview["preview"], preview["confirmation"])
    record = lab.logs[0]
    if change == "duplicate":
        lab.logs.append(copy.deepcopy(record))
    elif change == "old":
        record["applyTime"] = datetime.fromtimestamp(
            lab.rows("attempts")[0]["created"] - 60, timezone.utc
        ).isoformat()
    elif change == "wrong_inviter":
        record["inviter"]["userId"] = int(scope.ORDINARY)
    elif change == "wrong_pin":
        record["applicant"]["nimId"] = "other-person"
    elif change == "wrong_target":
        record["applicant"]["userId"] = int(scope.ORDINARY)
    elif change == "wrong_group":
        record["groupId"] = 1516387
    elif change == "wrong_type":
        record["groupApplyType"] = "APPLICATION"
    elif change == "invalid_time":
        record["applyTime"] = "2026-10-15T00:00:00"
    elif change == "invalid_id":
        record["applyId"] = "78"
    else:
        record["state"] = "MEMBER_STATE_REJECTED"
    with pytest.raises(ProtocolError, match="test_lab_invitation_"):
        await lab.create().preview("accept")
    assert [c["route"] for c in lab.writes] == [scope.INVITE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event", ["LEAVE", "KICK", "APPLY", "other_inviter", "same_time"]
)
async def test_later_or_ambiguous_membership_event_supersedes_old_invite(lab, event):
    """Reject retained invitation proof when newer or conflicting evidence exists.

    Args:
        lab: Isolated native transport, journal and roster.
        event: Superseding membership event or equal-time conflict.
    """
    invite = await lab.worker.preview("invite")
    await lab.worker.execute(invite["preview"], invite["confirmation"])
    accept = await lab.create().preview("accept")
    newer = copy.deepcopy(lab.logs[0])
    newer["applyId"] = "88"
    stamp = datetime.fromisoformat(newer["applyTime"]).timestamp()
    newer["applyTime"] = datetime.fromtimestamp(
        stamp if event == "same_time" else stamp + 1, timezone.utc
    ).isoformat()
    if event == "other_inviter":
        newer["inviter"]["userId"] = int(scope.ORDINARY)
    else:
        newer["groupApplyType"] = "LEAVE" if event == "same_time" else event
        newer["inviter"] = None
    lab.logs.insert(0, newer)
    with pytest.raises(ProtocolError, match="test_lab_invitation_superseded"):
        await lab.create().execute(accept["preview"], accept["confirmation"])
    with pytest.raises(ProtocolError, match="test_lab_invitation_superseded"):
        await lab.create().reconcile(invite["preview"])
    saved = {row["id"]: row for row in lab.rows("previews")}
    assert saved[accept["preview"]]["used"] == 0
    assert len(lab.rows("attempts")) == 1
    assert lab.rows("attempts")[0]["state"] == "pending"
    assert [call["route"] for call in lab.writes] == [scope.INVITE]


@pytest.mark.asyncio
async def test_observation_does_not_drop_bad_extra_member(lab):
    preview = await lab.worker.preview("invite")
    await lab.worker.execute(preview["preview"], preview["confirmation"])
    lab.logs[0]["state"] = "MEMBER_STATE_GOOD"
    lab.members[scope.PEER] = lab.member(scope.PEER)
    lab.members["999"] = {**lab.member("999"), "accountState": "ACCOUNT_STATE_BAN"}
    assert (await lab.create().reconcile(preview["preview"]))["state"] == "unknown"
    assert len(lab.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["identity", "protected", "roster", "expired"])
async def test_real_http_lock_wait_revalidates_before_submission(
    lab, monkeypatch, revocation
):
    preview = await lab.worker.preview("invite")
    owner = lab.clients[scope.OWNER]
    waiting = asyncio.Event()

    async def dispatch(client, route, params, *, authorize, submission):
        if route not in scope.ROUTES.values():
            return await lab.exchange(
                client, route, params, authorize=authorize, submission=submission
            )
        waiting.set()
        return await signed_exchange(
            client, route, params, authorize=authorize, submission=submission
        )

    monkeypatch.setattr(scope, "_exchange_business", dispatch)
    await owner.lock.acquire()
    task = asyncio.create_task(
        lab.worker.execute(preview["preview"], preview["confirmation"])
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert not task.done()
        if revocation == "identity":
            owner.fields[3] = int(scope.ORDINARY)
        elif revocation == "protected":
            lab.digest = "b" * 64
        elif revocation == "roster":
            lab.members["999"] = lab.member("999")
        else:
            lab.now = preview["expires"]
        owner.lock.release()
        with pytest.raises(ProtocolError) as caught:
            await asyncio.wait_for(task, timeout=2)
        assert caught.value.submitted is False
    finally:
        if owner.lock.locked():
            owner.lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    owner.http.post.assert_not_called()
    assert owner._authorization_task is None
    assert not lab.writes
    assert lab.rows("attempts")[0]["state"] == "not_submitted"
    assert lab.rows("previews")[0]["used"] == 1


@pytest.mark.asyncio
async def test_write_cooldown_finishes_before_last_snapshot(lab, monkeypatch):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    remove = await lab.worker.preview("remove")
    await lab.worker.execute(remove["preview"], remove["confirmation"])
    preview = await lab.create().preview("invite")
    revoked = []

    def revoke():
        revoked.append(lab.now)
        lab.members["999"] = lab.member("999")

    async def dispatch(client, route, params, *, authorize, submission):
        if route == scope.INVITE:
            lab.sleep_hook = revoke
        return await lab.exchange(
            client, route, params, authorize=authorize, submission=submission
        )

    monkeypatch.setattr(scope, "_exchange_business", dispatch)
    with pytest.raises(ProtocolError, match="test_lab_native_scope"):
        await lab.create().execute(preview["preview"], preview["confirmation"])
    assert revoked
    assert revoked[0] >= lab.writes[0]["finish"] + 30
    assert [c["route"] for c in lab.writes] == [scope.REMOVE]
    assert lab.rows("attempts")[-1]["state"] == "not_submitted"


@pytest.mark.asyncio
async def test_platform_retry_after_is_durable_and_never_automatically_retries(lab):
    preview = await lab.worker.preview("invite")
    lab.write_error = BusinessError("rate_limited", 429, 120)
    with pytest.raises(BusinessError, match="rate_limited"):
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    blocked = lab.rows("pacing")[0]["blocked_until"]
    assert blocked == lab.now + 120
    count = len(lab.calls)
    with pytest.raises(ProtocolError, match="test_lab_platform_cooldown"):
        await lab.create().reconcile(preview["preview"])
    assert len(lab.calls) == count
    lab.now = blocked
    assert (await lab.create().reconcile(preview["preview"]))["state"] == "pending"
    assert len(lab.writes) == 1
    assert lab.rows("attempts")[0]["state"] == "pending"


@pytest.mark.asyncio
async def test_completion_pacing_and_clock_rollback_persist_across_restart(lab):
    await lab.worker.preview("invite")
    finished = lab.calls[-1]["finish"]
    worker = lab.create()
    with worker.locked():
        await worker.exchange(scope.OWNER, GROUPS, {"v": "0"})
    assert lab.calls[-1]["start"] == finished + 3
    lab.now -= 2
    count = len(lab.calls)
    with worker.locked(), pytest.raises(ProtocolError, match="test_lab_clock_changed"):
        await worker.exchange(scope.OWNER, GROUPS, {"v": "0"})
    assert len(lab.calls) == count


@pytest.mark.asyncio
async def test_private_journal_and_actual_flock_exclude_parallel_runners(lab):
    assert stat.S_IMODE(lab.worker.control.stat().st_mode) == 0o700
    assert stat.S_IMODE(lab.worker.path.stat().st_mode) == 0o600
    restarted = lab.create()
    with lab.worker.locked():
        with pytest.raises(ProtocolError, match="test_lab_busy"):
            with restarted.locked():
                pytest.fail("Second process lock was admitted")
        with pytest.raises(ProtocolError, match="test_lab_busy"):
            with lab.worker.locked():
                pytest.fail("Reentrant runner was admitted")
    with restarted.locked():
        assert restarted.fd is not None
    assert stat.S_IMODE((lab.worker.control / "run.lock").stat().st_mode) == 0o600


@pytest.mark.parametrize("unsafe", ["permissions", "directory_symlink", "db_symlink"])
def test_unsafe_journal_paths_rejected_without_changing_target(lab, tmp_path, unsafe):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    control = tmp_path / "unsafe"
    if unsafe == "permissions":
        control.mkdir(mode=0o755)
        control.chmod(0o755)
    elif unsafe == "directory_symlink":
        control.symlink_to(target, target_is_directory=True)
    else:
        control.mkdir(mode=0o700)
        marker = target / "untouched"
        marker.write_bytes(b"not a database")
        (control / "control.sqlite3").symlink_to(marker)
    with pytest.raises(ProtocolError, match="test_lab_private_directory"):
        lab.create(control=control)
    if unsafe == "db_symlink":
        assert marker.read_bytes() == b"not a database"
    assert not lab.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,params",
    [
        (scope.INVITE, {"groupId": int(GROUP), "groupMemberIds": [int(scope.PEER)]}),
        (scope.REMOVE, {"groupId": int(GROUP), "groupMemberIds": [int(scope.PEER)]}),
        (scope.ACCEPT, {"applyId": "77", "agree": True}),
        (GRANT, {"groupId": int(GROUP), "groupManagerIds": [int(scope.PEER)]}),
        (REVOKE, {"groupId": int(GROUP), "userId": int(scope.PEER)}),
        ("/v1/friend/delete-friend", {"userId": int(scope.PEER)}),
        (MEMBERS, {"groupId": 1516387, "v": "0"}),
        (MEMBERS, {"groupId": GROUP, "v": "0"}),
        (MEMBERS, {"groupId": int(GROUP), "v": "0", "unexpected": True}),
        (GROUPS, {"v": "1"}),
        (LOGS, {"groupId": int(GROUP)}),
    ],
)
async def test_unreserved_writes_and_unreviewed_reads_never_submit(lab, route, params):
    with (
        lab.worker.locked(),
        pytest.raises(ProtocolError, match="test_lab_(unreserved|scope)"),
    ):
        await lab.worker.exchange(scope.OWNER, route, params)
    assert not lab.calls
    assert not lab.rows("attempts")


@pytest.mark.asyncio
async def test_production_whitelist_stays_closed_to_invite_accept_and_friend_delete():
    for route in (
        scope.INVITE,
        scope.ACCEPT,
        GRANT,
        REVOKE,
        scope.TRANSFER,
        "/v1/friend/delete-friend",
    ):
        with pytest.raises(BusinessError, match="unsupported_route"):
            await request_business(SimpleNamespace(), route, {})


@pytest.mark.asyncio
async def test_grant_revoke_exact_parameters_receipts_pacing_and_shared_budget(lab):
    """Verify bounded owner-only role changes using the durable shared write budget.

    Args:
        lab: Isolated native transport, journal and roster.
    """
    assert scope.ROUTES["grant"] == GRANT
    assert scope.ROUTES["revoke"] == REVOKE
    lab.members[scope.PEER] = lab.member(scope.PEER)
    unchanged = {
        uid: copy.deepcopy(lab.members[uid]) for uid in (scope.OWNER, scope.ORDINARY)
    }
    previews = []
    for action in ["grant", "revoke"] * 5:
        preview = await lab.create().preview(action)
        assert preview["actor"] == scope.OWNER
        assert preview["target"] == scope.PEER
        assert preview["invitation"] is None
        assert preview["expires"] == lab.now + 600
        result = await lab.create().execute(preview["preview"], preview["confirmation"])
        assert result["state"] == "verified"
        assert result["provider_accepted"] is True
        assert result["invitation"] is None
        assert lab.members[scope.PEER]["groupRole"] == (
            "GROUP_ROLE_ADMIN" if action == "grant" else "GROUP_ROLE_MEMBER"
        )
        assert set(lab.members) == {scope.OWNER, scope.PEER, scope.ORDINARY}
        assert all(lab.members[uid] == row for uid, row in unchanged.items())
        previews.append(preview)
    assert [call["route"] for call in lab.writes] == [GRANT, REVOKE] * 5
    assert all(call["actor"] == scope.OWNER for call in lab.writes)
    assert all(
        later["start"] >= earlier["finish"] + 3
        for earlier, later in zip(lab.calls, lab.calls[1:])
    )
    assert all(
        later["start"] >= earlier["finish"] + 30
        for earlier, later in zip(lab.writes, lab.writes[1:])
    )
    assert not any(call["route"] == LOGS for call in lab.calls)
    count = len(lab.calls)
    for preview in (previews[0], previews[-1]):
        with pytest.raises(ProtocolError, match="test_lab_confirmation"):
            await lab.create().execute(preview["preview"], preview["confirmation"])
    assert len(lab.calls) == count
    extra = await lab.create().preview("grant")
    with pytest.raises(ProtocolError, match="test_lab_daily_write_limit"):
        await lab.create().execute(extra["preview"], extra["confirmation"])
    assert len(lab.writes) == len(lab.rows("attempts")) == 10
    assert all(row["state"] == "verified" for row in lab.rows("attempts"))
    saved = {row["id"]: row for row in lab.rows("previews")}
    assert saved[extra["preview"]]["used"] == 0


@pytest.mark.asyncio
async def test_explicit_extended_rehearsal_keeps_history_and_default_cap(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    for index in range(12):
        worker = lab.create(daily_write_limit=20)
        assert worker.daily_write_limit == 20
        preview = await worker.preview("grant" if index % 2 == 0 else "revoke")
        result = await worker.execute(preview["preview"], preview["confirmation"])
        assert result["state"] == "verified"
    assert len(lab.rows("attempts")) == 12
    original = lab.create()
    preview = await original.preview("grant")
    with pytest.raises(ProtocolError, match="test_lab_daily_write_limit"):
        await original.execute(preview["preview"], preview["confirmation"])
    assert len(lab.rows("attempts")) == 12


@pytest.mark.parametrize("limit", (0, 11, 21, 1000, True, "20"))
def test_rehearsal_cannot_choose_an_unbounded_write_allowance(lab, limit):
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        lab.create(daily_write_limit=limit)


@pytest.mark.asyncio
async def test_approved_extra_role_pair_keeps_history_and_reserves_only_two_writes(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    for index in range(20):
        worker = lab.create(daily_write_limit=20)
        preview = await worker.preview("grant" if index % 2 == 0 else "revoke")
        await worker.execute(preview["preview"], preview["confirmation"])
    baseline = lab.rows("attempts")
    day = datetime.fromtimestamp(lab.now, timezone.utc).date().isoformat()
    worker = lab.create(daily_write_limit=20, extra_role_pair_day=day)
    for action in ("grant", "revoke"):
        preview = await worker.preview(action)
        assert preview["extra_role_pair_day"] == day
        result = await worker.execute(preview["preview"], preview["confirmation"])
        assert result["state"] == "verified"
        assert result["provider_accepted"]
    assert lab.rows("attempts")[:20] == baseline
    assert len(lab.rows("attempts")) == len(lab.writes) == 22
    assert lab.members[scope.PEER]["groupRole"] == "GROUP_ROLE_MEMBER"
    preview = await worker.preview("grant")
    with pytest.raises(ProtocolError, match="test_lab_role_extension_limit"):
        await worker.execute(preview["preview"], preview["confirmation"])
    assert len(lab.writes) == 22
    normal = lab.create(daily_write_limit=20)
    preview = await normal.preview("grant")
    with pytest.raises(ProtocolError, match="test_lab_daily_write_limit"):
        await normal.execute(preview["preview"], preview["confirmation"])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ("invite", "remove", "accept", "transfer-owner"))
async def test_extra_pair_cannot_extend_membership_or_ownership_actions(lab, action):
    day = datetime.fromtimestamp(lab.now, timezone.utc).date().isoformat()
    worker = lab.create(daily_write_limit=20, extra_role_pair_day=day)
    with pytest.raises(ProtocolError, match="test_lab_role_extension_scope"):
        await worker.preview(action)
    assert not lab.calls and not lab.rows("attempts")


@pytest.mark.asyncio
async def test_extra_pair_is_not_a_general_twenty_two_write_budget(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    day = datetime.fromtimestamp(lab.now, timezone.utc).date().isoformat()
    worker = lab.create(daily_write_limit=20, extra_role_pair_day=day)
    preview = await worker.preview("grant")
    with pytest.raises(ProtocolError, match="test_lab_role_extension_limit"):
        await worker.execute(preview["preview"], preview["confirmation"])
    assert not lab.writes and not lab.rows("attempts")


@pytest.mark.asyncio
async def test_second_approved_pair_requires_first_restoration_and_stops_at_twenty_four(
    lab,
):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    day = datetime.fromtimestamp(lab.now, timezone.utc).date().isoformat()
    second = lab.create(
        daily_write_limit=20, extra_role_pair_day=day, extra_role_pair_round=2
    )
    preview = await second.preview("grant")
    with pytest.raises(ProtocolError, match="test_lab_role_extension_limit"):
        await second.execute(preview["preview"], preview["confirmation"])
    for index in range(20):
        worker = lab.create(daily_write_limit=20)
        preview = await worker.preview("grant" if index % 2 == 0 else "revoke")
        await worker.execute(preview["preview"], preview["confirmation"])
    preview = await second.preview("grant")
    with pytest.raises(ProtocolError, match="test_lab_role_extension_limit"):
        await second.execute(preview["preview"], preview["confirmation"])
    first = lab.create(daily_write_limit=20, extra_role_pair_day=day)
    for action in ("grant", "revoke"):
        preview = await first.preview(action)
        await first.execute(preview["preview"], preview["confirmation"])
    baseline = lab.rows("attempts")
    for action in ("grant", "revoke"):
        preview = await second.preview(action)
        assert preview["extra_role_pair_round"] == 2
        result = await second.execute(preview["preview"], preview["confirmation"])
        assert result["state"] == "verified" and result["provider_accepted"]
    assert lab.rows("attempts")[:22] == baseline
    assert len(lab.rows("attempts")) == len(lab.writes) == 24
    assert lab.members[scope.PEER]["groupRole"] == "GROUP_ROLE_MEMBER"
    preview = await second.preview("grant")
    with pytest.raises(ProtocolError, match="test_lab_role_extension_limit"):
        await second.execute(preview["preview"], preview["confirmation"])


@pytest.mark.parametrize("round_number", [0, 3, 100, True, "2"])
def test_role_pair_round_is_not_an_arbitrary_budget(lab, round_number):
    day = datetime.fromtimestamp(lab.now, timezone.utc).date().isoformat()
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        lab.create(
            daily_write_limit=20,
            extra_role_pair_day=day,
            extra_role_pair_round=round_number,
        )


def test_second_pair_requires_an_explicit_current_day(lab):
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        lab.create(daily_write_limit=20, extra_role_pair_round=2)


@pytest.mark.parametrize(
    "overrides",
    (
        {"daily_write_limit": 10},
        {"group": "1459283"},
        {"extra_role_pair_day": "2000-01-01"},
        {"extra_role_pair_day": True},
    ),
)
def test_extra_pair_requires_current_day_fixed_group_and_explicit_twenty(
    lab, overrides
):
    day = datetime.fromtimestamp(lab.now, timezone.utc).date().isoformat()
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        lab.create(
            **{
                "daily_write_limit": 20,
                "extra_role_pair_day": day,
                **overrides,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["grant", "revoke"])
@pytest.mark.parametrize(
    "change",
    [
        "owner_identity",
        "owner_role",
        "ordinary_admin",
        "peer_wrong_role",
        "pin",
        "extra_human",
    ],
)
async def test_role_changes_require_owner_and_exact_native_controlled_roles(
    lab, action, change
):
    """Reject identity, roster and role mismatches before reserving any role write.

    Args:
        lab: Isolated native transport, journal and roster.
        action: Grant or revoke administrator status for the fixed peer.
        change: Unsafe native identity or role evidence.
    """
    lab.members[scope.PEER] = lab.member(scope.PEER)
    if action == "revoke":
        lab.members[scope.PEER]["groupRole"] = "GROUP_ROLE_ADMIN"
    if change == "owner_identity":
        lab.clients[scope.OWNER].fields[3] = int(scope.PEER)
    elif change == "owner_role":
        lab.members[scope.OWNER]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "ordinary_admin":
        lab.members[scope.ORDINARY]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "peer_wrong_role":
        lab.members[scope.PEER]["groupRole"] = (
            "GROUP_ROLE_ADMIN" if action == "grant" else "GROUP_ROLE_MEMBER"
        )
    elif change == "pin":
        lab.members[scope.PEER]["nimId"] = "different-controlled-user"
    else:
        lab.members["999"] = lab.member("999")
    with pytest.raises(ProtocolError, match="test_lab_(identity|native_scope)"):
        await lab.worker.preview(action)
    assert not lab.writes
    assert not lab.rows("previews")
    assert not lab.rows("attempts")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["grant", "revoke"])
@pytest.mark.parametrize("contradiction", ["peer_role", "ordinary_role"])
async def test_role_receipt_requires_exact_roster_before_verification(
    lab, action, contradiction
):
    """Keep a successful role receipt separate from contradictory native roles.

    Args:
        lab: Isolated native transport, journal and roster.
        action: Owner-requested role transition.
        contradiction: Target or unrelated member role that does not match.
    """
    lab.members[scope.PEER] = lab.member(scope.PEER)
    before = "GROUP_ROLE_MEMBER" if action == "grant" else "GROUP_ROLE_ADMIN"
    after = "GROUP_ROLE_ADMIN" if action == "grant" else "GROUP_ROLE_MEMBER"
    lab.members[scope.PEER]["groupRole"] = before
    preview = await lab.worker.preview(action)

    def contradict():
        if contradiction == "peer_role":
            lab.members[scope.PEER]["groupRole"] = before
        else:
            lab.members[scope.ORDINARY]["groupRole"] = "GROUP_ROLE_ADMIN"

    lab.after_write = contradict
    result = await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert result["state"] == "unknown"
    assert result["provider_accepted"] is True
    assert (await lab.create().reconcile(preview["preview"]))["state"] == "unknown"
    lab.members[scope.PEER]["groupRole"] = after
    lab.members[scope.ORDINARY]["groupRole"] = "GROUP_ROLE_MEMBER"
    result = await lab.create().reconcile(preview["preview"])
    assert result["state"] == "verified"
    assert result["provider_accepted"] is True
    assert [call["route"] for call in lab.writes] == [scope.ROUTES[action]]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["grant", "revoke"])
async def test_unknown_role_submission_never_replays_or_infers_receipt(lab, action):
    """Do not treat observed role changes as proof of an uncertain provider receipt.

    Args:
        lab: Isolated native transport, journal and roster.
        action: Owner-requested role transition with uncertain transport outcome.
    """
    lab.members[scope.PEER] = lab.member(scope.PEER)
    if action == "revoke":
        lab.members[scope.PEER]["groupRole"] = "GROUP_ROLE_ADMIN"
    preview = await lab.worker.preview(action)
    lab.write_error = TimeoutError("Uncertain submitted role change")
    with pytest.raises(TimeoutError) as caught:
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert caught.value.submitted is True
    assert lab.members[scope.PEER]["groupRole"] == (
        "GROUP_ROLE_ADMIN" if action == "grant" else "GROUP_ROLE_MEMBER"
    )
    worker = lab.create()
    result = await worker.reconcile(preview["preview"])
    assert result["state"] == "unknown"
    assert result["provider_accepted"] is False
    count = len(lab.calls)
    with pytest.raises(ProtocolError, match="test_lab_confirmation"):
        await worker.execute(preview["preview"], preview["confirmation"])
    assert len(lab.calls) == count
    with pytest.raises(ProtocolError, match="test_lab_unresolved_action"):
        await worker.preview("revoke" if action == "grant" else "grant")
    assert lab.rows("attempts")[0]["state"] == "unknown"
    assert lab.rows("attempts")[0]["provider_accepted"] == 0
    assert [call["route"] for call in lab.writes] == [scope.ROUTES[action]]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["grant", "revoke"])
@pytest.mark.parametrize("change", ["wrong_token", "expired", "protected", "roster"])
async def test_role_confirmation_rejects_invalid_token_or_stale_snapshot(
    lab, action, change
):
    """Apply the same confirmation and protected snapshot rules to role writes.

    Args:
        lab: Isolated native transport, journal and roster.
        action: Owner-requested role transition.
        change: Confirmation or protected snapshot invalidation.
    """
    lab.members[scope.PEER] = lab.member(scope.PEER)
    if action == "revoke":
        lab.members[scope.PEER]["groupRole"] = "GROUP_ROLE_ADMIN"
    preview = await lab.worker.preview(action)
    token = preview["confirmation"]
    if change == "wrong_token":
        token += "-wrong"
    elif change == "expired":
        lab.now = preview["expires"]
    elif change == "protected":
        lab.digest = "b" * 64
    else:
        lab.members["999"] = lab.member("999")
    count = len(lab.calls)
    with pytest.raises(ProtocolError):
        await lab.create().execute(preview["preview"], token)
    if change in {"wrong_token", "expired"}:
        assert len(lab.calls) == count
    assert lab.rows("previews")[0]["used"] == 0
    assert not lab.rows("attempts")
    assert not lab.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("original_role", ["GROUP_ROLE_MEMBER", "GROUP_ROLE_ADMIN"])
@pytest.mark.parametrize("former_role", ["GROUP_ROLE_MEMBER", "GROUP_ROLE_ADMIN"])
async def test_owner_transfer_and_separately_confirmed_return(
    lab, original_role, former_role
):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    lab.members[scope.PEER]["groupRole"] = original_role
    lab.former_owner_role = former_role
    ordinary = copy.deepcopy(lab.members[scope.ORDINARY])
    forward = await lab.worker.preview("transfer-owner")
    assert forward["actor"] == scope.OWNER
    assert forward["target"] == scope.PEER
    assert forward["ownership"] is None
    assert "not just administrator" in forward["warning"]
    assert not lab.writes
    result = await lab.create().execute(forward["preview"], forward["confirmation"])
    assert result["state"] == "verified"
    assert result["evidence"]["restoration_required"] is True
    assert result["evidence"]["roles"][scope.PEER] == "GROUP_ROLE_OWNER"
    assert lab.members[scope.ORDINARY] == ordinary

    reverse = await lab.create().preview("restore-owner")
    assert reverse["actor"] == scope.PEER
    assert reverse["target"] == scope.OWNER
    assert reverse["ownership"]["attempt"] == forward["preview"]
    assert reverse["ownership"]["original_roles"][scope.PEER] == original_role
    assert len(lab.writes) == 1
    result = await lab.create().execute(reverse["preview"], reverse["confirmation"])
    assert result["state"] == "verified"
    assert result["actor"] == scope.PEER
    assert result["target"] == scope.OWNER
    assert result["evidence"]["transfer_attempt"] == forward["preview"]
    assert result["evidence"]["restoration_required"] is False
    assert result["evidence"]["role_restore_required"] is (original_role != former_role)
    assert lab.members[scope.OWNER]["groupRole"] == "GROUP_ROLE_OWNER"
    assert lab.members[scope.ORDINARY] == ordinary
    assert [(r["actor"], r["route"], r["params"]) for r in lab.writes] == [
        (
            scope.OWNER,
            scope.TRANSFER,
            {"groupId": int(GROUP), "userId": int(scope.PEER)},
        ),
        (
            scope.PEER,
            scope.TRANSFER,
            {"groupId": int(GROUP), "userId": int(scope.OWNER)},
        ),
    ]
    assert lab.writes[1]["start"] >= lab.writes[0]["finish"] + 30
    assert all(
        later["start"] >= earlier["finish"] + 3
        for earlier, later in zip(lab.calls, lab.calls[1:])
    )
    receipt = await lab.create().reconcile(reverse["preview"])
    assert receipt["historical_receipt"] is True
    assert receipt["evidence"] == result["evidence"]
    with pytest.raises(ProtocolError, match="test_lab_confirmation"):
        await lab.create().execute(reverse["preview"], reverse["confirmation"])
    assert len(lab.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["grant", "revoke", "transfer-owner", "restore-owner"]
)
async def test_all_role_mutations_are_fixed_to_authorized_test_group(lab, action):
    with pytest.raises(ProtocolError, match="test_lab_scope"):
        await lab.create(group="1459283").preview(action)
    assert not lab.calls
    assert not lab.rows("previews")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["owner_role", "peer_missing", "ordinary_missing", "pin", "extra", "bad"]
)
async def test_owner_transfer_requires_complete_exact_controlled_native_roster(
    lab, change
):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    if change == "owner_role":
        lab.members[scope.OWNER]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "peer_missing":
        lab.members.pop(scope.PEER)
    elif change == "ordinary_missing":
        lab.members.pop(scope.ORDINARY)
    elif change == "pin":
        lab.members[scope.PEER]["nimId"] = "someone-else"
    elif change == "extra":
        lab.members["999"] = lab.member("999")
    else:
        lab.members[scope.PEER]["accountState"] = "ACCOUNT_STATE_BAN"
    with pytest.raises(ProtocolError, match="test_lab_native_scope"):
        await lab.worker.preview("transfer-owner")
    assert not lab.writes
    assert not lab.rows("previews")


@pytest.mark.asyncio
@pytest.mark.parametrize("original_role", ["GROUP_ROLE_MEMBER", "GROUP_ROLE_ADMIN"])
async def test_owner_return_cannot_claim_an_unrecorded_transfer(lab, original_role):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    lab.members[scope.PEER]["groupRole"] = "GROUP_ROLE_OWNER"
    lab.members[scope.OWNER]["groupRole"] = original_role
    with pytest.raises(ProtocolError, match="test_lab_ownership_unproven"):
        await lab.worker.preview("restore-owner")
    assert not lab.writes
    assert not lab.rows("attempts")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_receipt", "role", "pin", "team"])
async def test_owner_return_requires_verified_matching_forward_evidence(lab, change):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    forward = await lab.worker.preview("transfer-owner")
    await lab.worker.execute(forward["preview"], forward["confirmation"])
    with closing(sqlite3.connect(lab.worker.path)) as db, db:
        if change == "missing_receipt":
            db.execute("UPDATE attempts SET provider_accepted=0")
        elif change == "role":
            lab.members[scope.OWNER]["groupRole"] = "GROUP_ROLE_ADMIN"
        else:
            original = json.loads(lab.rows("attempts")[0]["payload"])
            if change == "team":
                original["team"] = "different-native-team"
            else:
                original["members"][0][1] = "different-pin"
            db.execute("UPDATE attempts SET payload=?", (json.dumps(original),))
    with pytest.raises(ProtocolError, match="test_lab_ownership_unproven"):
        await lab.create().preview("restore-owner")
    assert len(lab.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["transfer-owner", "restore-owner"])
@pytest.mark.parametrize(
    "change", ["wrong_token", "expired", "protected", "owner_changed"]
)
async def test_owner_confirmation_bound_to_identity_scope_expiry_and_snapshot(
    lab, direction, change
):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    if direction == "restore-owner":
        forward = await lab.worker.preview("transfer-owner")
        await lab.worker.execute(forward["preview"], forward["confirmation"])
    preview = await lab.create().preview(direction)
    writes = len(lab.writes)
    token = preview["confirmation"]
    if change == "wrong_token":
        token += "-bad"
    elif change == "expired":
        lab.now = preview["expires"]
    elif change == "protected":
        lab.digest = "b" * 64
    else:
        lab.members[preview["actor"]]["groupRole"] = "GROUP_ROLE_ADMIN"
    with pytest.raises(ProtocolError):
        await lab.create().execute(preview["preview"], token)
    assert len(lab.writes) == writes
    assert (
        next(r for r in lab.rows("previews") if r["id"] == preview["preview"])["used"]
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["unknown", "cancelled", "different_owner", "ordinary_promoted"]
)
async def test_owner_unknown_or_contradictory_result_never_replays_or_claims_pass(
    lab, fault
):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview("transfer-owner")
    if fault in {"unknown", "cancelled"}:
        error = TimeoutError if fault == "unknown" else asyncio.CancelledError
        lab.write_error = error("Submitted owner transfer has no reply")
        with pytest.raises(error):
            await lab.worker.execute(preview["preview"], preview["confirmation"])
    else:

        def contradict():
            if fault == "different_owner":
                lab.members[scope.OWNER]["groupRole"] = "GROUP_ROLE_OWNER"
            else:
                lab.members[scope.ORDINARY]["groupRole"] = "GROUP_ROLE_ADMIN"

        lab.after_write = contradict
        assert (await lab.worker.execute(preview["preview"], preview["confirmation"]))[
            "state"
        ] == "unknown"
    assert (await lab.create().reconcile(preview["preview"]))["state"] == "unknown"
    assert len(lab.writes) == 1
    with pytest.raises(ProtocolError):
        await lab.create().preview("restore-owner")
    with pytest.raises(ProtocolError, match="test_lab_confirmation"):
        await lab.create().execute(preview["preview"], preview["confirmation"])
    assert len(lab.writes) == 1


@pytest.mark.asyncio
async def test_transfer_shares_existing_budget_and_needs_room_for_return(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    for index in range(9):
        preview = await lab.create().preview("grant" if index % 2 == 0 else "revoke")
        await lab.create().execute(preview["preview"], preview["confirmation"])
    preview = await lab.create().preview("transfer-owner")
    with pytest.raises(ProtocolError, match="test_lab_daily_write_limit"):
        await lab.create().execute(preview["preview"], preview["confirmation"])
    assert len(lab.writes) == len(lab.rows("attempts")) == 9
    assert lab.members[scope.OWNER]["groupRole"] == "GROUP_ROLE_OWNER"
    assert not any(r["route"] == scope.TRANSFER for r in lab.writes)


@pytest.mark.asyncio
async def test_owner_transfer_does_not_leave_unclosed_history_unnoticed(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview("transfer-owner")
    await lab.worker.execute(preview["preview"], preview["confirmation"])
    # A manual role reset is not a journaled return and must not erase the gap.
    lab.members[scope.OWNER]["groupRole"] = "GROUP_ROLE_OWNER"
    lab.members[scope.PEER]["groupRole"] = "GROUP_ROLE_MEMBER"
    with pytest.raises(ProtocolError, match="test_lab_ownership_unrestored"):
        await lab.create().preview("transfer-owner")
    assert len(lab.rows("attempts")) == len(lab.writes) == 1


@pytest.mark.asyncio
async def test_owner_return_survives_daily_reset_and_keeps_original_history(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview("transfer-owner")
    await lab.worker.execute(preview["preview"], preview["confirmation"])
    lab.now = (int(lab.now // 86400) + 1) * 86400 + 1
    reverse = await lab.create().preview("restore-owner")
    result = await lab.create().execute(reverse["preview"], reverse["confirmation"])
    assert result["state"] == "verified"
    assert result["evidence"]["transfer_attempt"] == preview["preview"]
    assert len(lab.rows("attempts")) == 2
    assert all(r["state"] == "verified" for r in lab.rows("attempts"))


@pytest.mark.asyncio
async def test_owner_old_preview_cannot_survive_an_intervening_owner_cycle(lab):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    first = await lab.worker.preview("transfer-owner")
    stale = await lab.worker.preview("transfer-owner")
    await lab.worker.execute(first["preview"], first["confirmation"])
    reverse = await lab.worker.preview("restore-owner")
    await lab.worker.execute(reverse["preview"], reverse["confirmation"])
    with pytest.raises(ProtocolError, match="test_lab_stale_preview"):
        await lab.create().execute(stale["preview"], stale["confirmation"])
    assert len(lab.writes) == 2
    fresh = await lab.create().preview("transfer-owner")
    assert fresh["previous_ownership"] == reverse["preview"]


@pytest.mark.asyncio
async def test_definite_non_submission_does_not_permanently_block_owner_return(
    lab, monkeypatch
):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    forward = await lab.worker.preview("transfer-owner")
    await lab.worker.execute(forward["preview"], forward["confirmation"])
    reverse = await lab.worker.preview("restore-owner")

    async def dispatch(client, route, params, **kwargs):
        if route == scope.TRANSFER and str(client.fields[3]) == scope.PEER:
            raise ProtocolError("fixture_before_io")
        return await lab.exchange(client, route, params, **kwargs)

    monkeypatch.setattr(scope, "_exchange_business", dispatch)
    with pytest.raises(ProtocolError, match="fixture_before_io"):
        await lab.worker.execute(reverse["preview"], reverse["confirmation"])
    assert lab.rows("attempts")[-1]["state"] == "not_submitted"
    monkeypatch.setattr(scope, "_exchange_business", lab.exchange)
    retry = await lab.create().preview("restore-owner")
    assert retry["ownership"]["attempt"] == forward["preview"]
    assert retry["previous_ownership"] == forward["preview"]
    assert (await lab.create().execute(retry["preview"], retry["confirmation"]))[
        "state"
    ] == "verified"
    assert len(lab.writes) == 2
    assert [r["state"] for r in lab.rows("attempts")] == [
        "verified",
        "not_submitted",
        "verified",
    ]


@pytest.mark.asyncio
async def test_owner_revoked_while_waiting_for_write_cooldown_stops_submission(
    lab, monkeypatch
):
    lab.members[scope.PEER] = lab.member(scope.PEER)
    preview = await lab.worker.preview("grant")
    await lab.worker.execute(preview["preview"], preview["confirmation"])
    preview = await lab.worker.preview("transfer-owner")

    async def dispatch(client, route, params, **kwargs):
        if route == scope.TRANSFER:
            lab.sleep_hook = lambda: lab.members[scope.OWNER].update(
                groupRole="GROUP_ROLE_ADMIN"
            )
        return await lab.exchange(client, route, params, **kwargs)

    monkeypatch.setattr(scope, "_exchange_business", dispatch)
    with pytest.raises(ProtocolError, match="test_lab_native_scope"):
        await lab.worker.execute(preview["preview"], preview["confirmation"])
    assert [r["route"] for r in lab.writes] == [GRANT]
    assert lab.rows("attempts")[-1]["state"] == "not_submitted"
    lab.members[scope.OWNER]["groupRole"] = "GROUP_ROLE_OWNER"
    assert (await lab.create().preview("transfer-owner"))["ownership"] is None


def test_production_wangshangliao_modules_do_not_import_test_lab():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "astrbot/core/platform/sources/wangshangliao",
        root / "astrbot/builtin_stars/wangshangliao_moderation",
    ]
    for base in paths:
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert all("wsl_test_lab" not in item.name for item in node.names)
                elif isinstance(node, ast.ImportFrom):
                    assert "wsl_test_lab" not in (node.module or "")
                    assert all("wsl_test_lab" not in item.name for item in node.names)
