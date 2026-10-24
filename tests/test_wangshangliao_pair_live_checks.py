"""Offline safety regressions for the explicitly invoked pair acceptance scripts."""

import copy
import hashlib
import importlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
OWNER, PEER, ORDINARY = "31846829", "43506527", "23691273"
GROUP = "1459282"
SENDER, RECEIVER = "wangshangliao_0acde7ec", "wangshangliao_f79c5b81"
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation/"
REGISTRATION = "/api/v1/bot-types/wangshangliao/registration"
TARGET = "private/43506527/MTY5ODA1MDkwOA"


class _Dashboard:
    """Implement detached status reads and revision-CAS on temporary send grants."""

    def __init__(self):
        self.calls = []
        self.send_outcome = "accepted"
        self.tenant = {
            "account": PEER,
            "group_id": GROUP,
            "name": "Controlled fixture",
            "version": 7,
            "epoch": 4,
            "rules": {"advertising": False, "flood": False, "keywords": []},
            "managers": [OWNER],
            "active": 1,
            "paused": 0,
            "present": 1,
            "cleanup_epoch": 2,
            "cleanup_done": 2,
            "admission": "verified",
            "native_role": "GROUP_ROLE_ADMIN",
            "membership_pending": False,
        }
        self.bots = {
            instance: {
                "id": instance,
                "revision": 3,
                "proactive_send": {
                    "enabled": False,
                    "targets": ["dormant-target"],
                    "limit": 10,
                },
            }
            for instance in (SENDER, RECEIVER)
        }

    async def request(self, method, path, *, json=None):
        self.calls.append((method, path, copy.deepcopy(json)))
        if path == PREFIX + "settings":
            if method == "GET":
                data = {"bots": list(self.bots.values())}
            else:
                assert set(json) == {"bot_id", "revision", "proactive_send"}
                bot = self.bots[json["bot_id"]]
                if bot["revision"] != json["revision"]:
                    raise RuntimeError("Revision conflict")
                bot["proactive_send"] = copy.deepcopy(json["proactive_send"])
                bot["revision"] += 1
                data = copy.deepcopy(bot)
        elif path == PREFIX + "open-service":
            action, payload = json["action"], json["payload"]
            if (
                action in {"tenant_preview", "tenant"}
                and type(payload.get("paused")) is not bool
            ):
                raise RuntimeError("tenant_invalid: paused must be bool")
            if action == "status":
                data = {
                    "settings": {"paused": False},
                    "health": {"capacity_paused": False},
                    "accounts": [
                        {"account": OWNER, "instance_id": SENDER, "state": "online"},
                        {"account": PEER, "instance_id": RECEIVER, "state": "online"},
                    ],
                    "groups": [self.tenant],
                }
            elif action == "tenant_preview":
                assert payload["account"] == PEER and payload["group_id"] == GROUP
                assert payload["managers"] == self.tenant["managers"] == [OWNER]
                assert payload["version"] == self.tenant["version"]
                data = {"confirmation": "fixture-confirmation"}
            elif action == "tenant":
                assert payload["account"] == PEER and payload["group_id"] == GROUP
                assert payload["managers"] == self.tenant["managers"] == [OWNER]
                assert payload["version"] == self.tenant["version"]
                assert payload["confirmation"] == "fixture-confirmation"
                self.tenant["rules"] = copy.deepcopy(payload["rules"])
                self.tenant["version"] += 1
                data = copy.deepcopy(self.tenant)
            else:
                raise AssertionError(f"Unexpected open-service action: {action}")
        elif path == "/api/v1/im/messages":
            if self.send_outcome == "timeout":
                raise TimeoutError("Uncertain submitted private request")
            data = {"status": self.send_outcome}
        elif path == REGISTRATION:
            data = {"window": {"active": False}}
        else:
            raise AssertionError(f"Unexpected dashboard path: {path}")
        value = copy.deepcopy(data)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"status": "ok", "data": value},
        )


@pytest_asyncio.fixture
async def checks(tmp_path, monkeypatch):
    """Block real transports and vaults, then construct only isolated script objects.

    Args:
        tmp_path: Exclusive filesystem root for all test data.
        monkeypatch: Scoped replacement of live dependencies.

    Yields:
        Loaded scripts, fake dashboard, real private journal and guarded SQLite opens.
    """

    def denied(*args, **kwargs):
        raise AssertionError("Live IO or vault access is forbidden")

    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)

    import aiohttp
    import httpx

    storage = importlib.import_module(
        "astrbot.core.platform.sources.wangshangliao.storage"
    )
    monkeypatch.setattr(storage.Vault, "__init__", denied)
    monkeypatch.setattr(storage.Vault, "load", denied)
    monkeypatch.setattr(storage.Vault, "save", denied)
    monkeypatch.setattr(httpx.AsyncClient, "request", denied)
    monkeypatch.setattr(httpx.Client, "request", denied)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", denied)
    menu = importlib.import_module("scripts.check_wsl_tree_menu_live")
    pair = importlib.import_module("scripts.check_wsl_pair_live")
    rejoin = importlib.import_module("scripts.check_wsl_pair_rejoin_live")
    names = importlib.import_module("scripts.check_wsl_account_names")
    opens = []
    connect = sqlite3.connect

    def fixture_connect(database, *args, **kwargs):
        raw = str(database)
        path = (
            Path(url2pathname(urlsplit(raw).path))
            if raw.startswith("file:")
            else Path(raw)
        )
        assert path.resolve().is_relative_to(tmp_path.resolve()), raw
        opens.append((raw, kwargs.copy()))
        return connect(database, *args, **kwargs)

    database_api = SimpleNamespace(
        connect=fixture_connect,
        Row=sqlite3.Row,
        OperationalError=sqlite3.OperationalError,
    )
    for module in (pair, rejoin, names):
        monkeypatch.setattr(module, "ROOT", tmp_path / "application")
        monkeypatch.setattr(module, "sqlite3", database_api)
    monkeypatch.setattr(menu, "ROOT", tmp_path / "application")
    monkeypatch.setattr(menu, "rows", denied)
    monkeypatch.setattr(menu, "Vault", denied)
    monkeypatch.setattr(pair, "CONTROL", tmp_path / "pair-control")
    monkeypatch.setattr(rejoin, "CONTROL", tmp_path / "membership-control")

    def fixture_menu_init(self, client, output, **kwargs):
        self.client, self.output, self.target = client, output, TARGET
        self.results = []
        self.remaining, self.window, self.changed = 0, False, False
        self.original = self.proposed = None
        self.native_admin_gate = False

    # Only credential-dependent construction is replaced; API and restore stay real.
    monkeypatch.setattr(menu.MenuCheck, "__init__", fixture_menu_init)
    dashboard = _Dashboard()
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    journal = sqlite3.connect(tmp_path / "pair-journal.sqlite3")
    journal.execute(
        "CREATE TABLE attempts(id TEXT PRIMARY KEY,run TEXT,kind TEXT,state TEXT,created REAL)"
    )
    journal.commit()
    check = pair.PairCheck(dashboard, None, output, journal, ["commands"])
    check.expected = copy.deepcopy(dashboard.tenant)
    check.original_tenant = copy.deepcopy(dashboard.tenant)
    check.before = "protected-fixture"
    digest = ["protected-fixture"]
    monkeypatch.setattr(pair, "protected", lambda: digest[0])
    check.native_path = tmp_path / "native/moderation.sqlite3"
    check.native_path.parent.mkdir()
    check.members = {
        ORDINARY: {
            "userId": ORDINARY,
            "nimId": "1000000003",
            "groupRole": "GROUP_ROLE_MEMBER",
            "accountState": "ACCOUNT_STATE_GOOD",
        }
    }
    try:
        yield SimpleNamespace(
            pair=pair,
            rejoin=rejoin,
            menu=menu,
            names=names,
            check=check,
            client=dashboard,
            digest=digest,
            opens=opens,
            root=tmp_path,
        )
    finally:
        journal.close()


@pytest.fixture
def manager_delivery(checks, monkeypatch):
    """Replace native delivery while retaining the real guards and request journal.

    Args:
        checks: Isolated pair harness and dashboard.
        monkeypatch: Scoped replacement of message delivery and pacing.

    Returns:
        Command history, confirmation evidence and selected failure injection.
    """
    import asyncio

    flow = SimpleNamespace(
        commands=[],
        confirmations=[],
        probes=[],
        pending={},
        fault="",
        injected=False,
        group_list_reply="已启用群（编号十分钟有效）：\n\n1. 测试群",
    )
    checks.check.phases = ["administrators"]
    checks.client.tenant["name"] = "测试群"
    checks.check.expected["name"] = "测试群"
    checks.check.original_tenant["name"] = "测试群"
    capability_path = (
        checks.pair.ROOT / "data/plugin_data/wangshangliao_open/open-service.sqlite3"
    )
    capability_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(capability_path)) as db, db:
        db.execute(
            "CREATE TABLE capabilities(digest TEXT PRIMARY KEY,kind TEXT,account TEXT,"
            "group_id TEXT,epoch INTEGER,version INTEGER,actor TEXT,session TEXT,"
            "target TEXT,operation TEXT,message TEXT,expires REAL,used INTEGER DEFAULT 0)"
        )
    inbox_path = checks.check.native_path.parent / "messages.sqlite3"
    with closing(sqlite3.connect(inbox_path)) as db, db:
        db.execute(
            "CREATE TABLE inbox(account TEXT,message TEXT,team TEXT,payload TEXT)"
        )
    checks.check.members.update(
        {
            uid: {
                "userId": uid,
                "nimId": "1000000001" if uid == OWNER else "1698050908",
                "groupRole": role,
                "accountState": "ACCOUNT_STATE_GOOD",
            }
            for uid, role in (
                (OWNER, "GROUP_ROLE_OWNER"),
                (PEER, "GROUP_ROLE_ADMIN"),
            )
        }
    )
    monkeypatch.setattr(
        checks.check,
        "checked_roster",
        AsyncMock(side_effect=lambda: copy.deepcopy(checks.check.members)),
    )
    monkeypatch.setattr(
        checks.pair,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "sleep": AsyncMock()}),
    )

    async def deliver(self, text, expected):
        receipt = await self.api(
            "POST",
            "/api/v1/im/messages",
            {
                "umo": f"{SENDER}:FriendMessage:{OWNER}/{self.target}",
                "operation_id": f"manager-fixture-{len(flow.commands)}",
                "message": [{"type": "plain", "text": text}],
            },
        )
        flow.commands.append(text)
        message = f"manager-incoming-{len(flow.commands)}"
        if text == "成员列表":
            reply = f"成员快照\n1. Owner ({OWNER})\n2. Ordinary ({ORDINARY})"
        elif text.startswith(("添加管理员 ", "移除管理员 ")):
            action, number = text.split()
            assert number == "2", "Only the controlled ordinary member may change"
            token = f"fixture-manager-confirmation-{len(flow.commands)}"
            flow.pending[token] = (
                action,
                self.expected["version"],
                self.expected["managers"].copy(),
            )
            team = f"private/{OWNER}/fixture"
            with closing(sqlite3.connect(capability_path)) as db, db:
                db.execute(
                    "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        "manager",
                        PEER,
                        GROUP,
                        self.expected["epoch"],
                        self.expected["version"],
                        OWNER,
                        f"{RECEIVER}:FriendMessage:{PEER}/{team}",
                        json.dumps(
                            {"uid": ORDINARY, "peer": self.members[ORDINARY]["nimId"]}
                        ),
                        "add" if action == "添加管理员" else "remove",
                        message,
                        time.time() + 600,
                    ),
                )
            with closing(sqlite3.connect(inbox_path)) as db, db:
                db.execute(
                    "INSERT INTO inbox VALUES(?,?,?,?)",
                    (PEER, message, team, json.dumps({"sender": OWNER, "text": text})),
                )
            reply = f"预览：{action} Ordinary。确认授权 {token}"
            if action == "添加管理员" and flow.fault in {
                "version",
                "foreign_manager",
                "owner_removed",
            }:
                checks.client.tenant["version"] += 1
                if flow.fault == "foreign_manager":
                    checks.client.tenant["managers"] = [OWNER, PEER]
                elif flow.fault == "owner_removed":
                    checks.client.tenant["managers"] = [ORDINARY]
                flow.injected = True
        elif text.startswith("确认授权 "):
            token = text.split()[1]
            action, version, managers = flow.pending.pop(token)
            tenant = checks.client.tenant
            assert tenant["version"] == version and tenant["managers"] == managers
            target = [OWNER, ORDINARY] if action == "添加管理员" else [OWNER]
            flow.confirmations.append(
                {
                    "action": action,
                    "version": version,
                    "before": managers,
                    "after": target,
                }
            )
            if flow.fault == (
                "unknown_add" if action == "添加管理员" else "unknown_remove"
            ):
                flow.injected = True
                raise TimeoutError("Manager confirmation outcome is unknown")
            tenant["managers"] = target
            tenant["version"] += 1
            with closing(sqlite3.connect(capability_path)) as db, db:
                db.execute(
                    "UPDATE capabilities SET used=1 WHERE digest=?",
                    (hashlib.sha256(token.encode()).hexdigest(),),
                )
            if action == "添加管理员" and flow.fault == "accepted_reply_lost":
                flow.injected = True
                raise TimeoutError("Manager accepted but reply delivery is unknown")
            reply = "已保存本群设置。"
        elif text == "管理员列表":
            reply = "本群管理员：" + "、".join(checks.client.tenant["managers"])
        elif text == "规则":
            reply = "广告审核：关闭"
        else:
            raise AssertionError(f"Unexpected manager command: {text}")
        assert any(
            part in reply
            for part in ((expected,) if isinstance(expected, str) else expected)
        ), (text, expected, reply)
        self.results.append(
            {
                "input": text,
                "reply": reply,
                "incoming": message,
                "state": "processed",
                "receipt": receipt,
                "passed": True,
            }
        )

    monkeypatch.setattr(checks.menu.MenuCheck, "send", deliver)

    async def probe(text, label, *, group=True, **kwargs):
        flow.probes.append((text, label, group))
        return {"status": "accepted", "message": label, "text": text, "group": group}

    async def reply(receipt, **kwargs):
        if receipt.get("group"):
            checks.check.ordinary_reply_metadata = {
                "message": receipt["message"],
                "parts": [
                    {
                        "sender": getattr(flow, "group_sender", PEER),
                        "mentions": getattr(
                            flow,
                            "group_mentions",
                            [str(checks.check.members[ORDINARY]["nimId"])],
                        ),
                    }
                ],
            }
            return (
                "@test 无权限"
                if checks.client.tenant["managers"] == [OWNER]
                else getattr(flow, "group_rule_reply", "@test 广告审核：关闭")
            )
        if checks.client.tenant["managers"] == [OWNER]:
            return "无权限"
        if receipt["text"] == "群列表":
            return flow.group_list_reply
        if receipt["text"] == "选择群 1":
            return "当前已选群：测试群"
        return "广告审核：关闭"

    monkeypatch.setattr(checks.check, "probe", AsyncMock(side_effect=probe))
    monkeypatch.setattr(checks.check, "wait_reply", AsyncMock(side_effect=reply))
    return flow


@pytest.mark.parametrize(
    "script,args",
    [
        ("check_wsl_pair_live.py", []),
        (
            "check_wsl_pair_rejoin_live.py",
            [
                "--remove-attempt",
                "removed-fixture",
                "--invite-attempt",
                "invited-fixture",
            ],
        ),
    ],
)
def test_cold_cli_without_apply_has_no_native_imports_credentials_or_network(
    tmp_path, script, args
):
    child = textwrap.dedent("""
        import asyncio, builtins, os, runpy, sys
        from pathlib import Path
        script, root, scratch, *arguments = sys.argv[1:]
        # Bootstrap Windows' internal socket pair before enforcing the IO audit.
        runner = asyncio.Runner()
        runner.get_loop()
        asyncio.run = runner.run
        sys.path.insert(0, root)
        sys.argv = [script, *arguments]
        original = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == "astrbot" or name.startswith("astrbot."):
                raise RuntimeError("Dry run imported native runtime: " + name)
            return original(name, *args, **kwargs)
        def audit(event, args):
            if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
                raise RuntimeError("Dry run attempted network")
            if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(args[0])).resolve()
                if path.is_relative_to(Path(root) / "data") or path.is_relative_to("/opt/backups"):
                    raise RuntimeError("Dry run accessed live data")
                if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                    if not path.is_relative_to(scratch):
                        raise RuntimeError("Dry run attempted an external write")
            if event == "os.mkdir" and not Path(args[0]).resolve().is_relative_to(scratch):
                raise RuntimeError("Dry run attempted an external directory")
        builtins.__import__ = guarded_import
        sys.addaudithook(audit)
        runpy.run_path(script, run_name="__main__")
    """)
    environment = {
        **os.environ,
        "ASTRBOT_ROOT": str(tmp_path),
        "ASTRBOT_DISABLE_METRICS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ASTRBOT_TEST_MODE": "true",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            child,
            str(ROOT / "scripts" / script),
            str(ROOT),
            str(tmp_path),
            *args,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dry_run"] is True
    assert not list(tmp_path.rglob("*.sqlite3"))


@pytest.mark.asyncio
async def test_rejoin_without_apply_returns_before_any_live_snapshot(
    checks, monkeypatch, capsys
):
    def forbidden_snapshot():
        raise AssertionError("Rejoin without --apply reached a live snapshot")

    monkeypatch.setattr(checks.rejoin, "snapshot", forbidden_snapshot, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_wsl_pair_rejoin_live.py",
            "--remove-attempt",
            "removed-fixture",
            "--invite-attempt",
            "invited-fixture",
        ],
    )
    await checks.rejoin.main()
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not checks.client.calls
    assert not checks.opens


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["group", "actor", "manager", "role", "protected"])
async def test_pair_guard_rejects_scope_or_authority_changes_without_writes(
    checks, change
):
    await checks.check.guard()
    if change == "group":
        checks.client.tenant["group_id"] = "1516387"
    elif change == "actor":
        checks.client.tenant["account"] = OWNER
    elif change == "manager":
        checks.client.tenant["managers"] = [OWNER, ORDINARY]
    elif change == "role":
        checks.client.tenant["native_role"] = "GROUP_ROLE_MEMBER"
    else:
        checks.digest[0] = "changed-protected-state"
    with pytest.raises(RuntimeError):
        await checks.check.guard()
    assert all(
        path == PREFIX + "open-service" and body == {"action": "status", "payload": {}}
        for _, path, body in checks.client.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("managers", [[OWNER, ORDINARY], [OWNER, PEER], [ORDINARY], []])
async def test_default_guard_rejects_manager_changes_even_if_expected_matches(
    checks, managers
):
    checks.client.tenant["managers"] = managers.copy()
    checks.check.expected = copy.deepcopy(checks.client.tenant)
    with pytest.raises(RuntimeError):
        await checks.check.guard()
    assert all(
        path == PREFIX + "open-service" and body == {"action": "status", "payload": {}}
        for _, path, body in checks.client.calls
    )
    assert not checks.check.journal.execute("SELECT * FROM attempts").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["inactive", "unselected", "version", "epoch", "foreign", "owner", "extra"],
)
async def test_temporary_manager_guard_requires_exact_tracked_scope(checks, change):
    check = checks.check
    check.phases = ["administrators"]
    check.administrators_active = True
    check.manager_grant = {"version": 7, "epoch": 4, "state": "granted"}
    checks.client.tenant["managers"] = [OWNER, ORDINARY]
    check.expected = copy.deepcopy(checks.client.tenant)
    await check.guard()
    if change == "inactive":
        check.administrators_active = False
    elif change == "unselected":
        check.phases = ["commands"]
    elif change in {"version", "epoch"}:
        checks.client.tenant[change] += 1
    else:
        checks.client.tenant["managers"] = {
            "foreign": [OWNER, PEER],
            "owner": [ORDINARY],
            "extra": [OWNER, ORDINARY, PEER],
        }[change]
    check.expected = copy.deepcopy(checks.client.tenant)
    with pytest.raises(RuntimeError):
        await check.guard()
    assert not check.journal.execute("SELECT * FROM attempts").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [[OWNER, PEER], [ORDINARY], []])
async def test_manager_confirmation_cannot_add_others_or_remove_owner(checks, target):
    check = checks.check
    check.phases = ["administrators"]
    check.administrators_active = True
    check.manager_confirmation = {
        "command": "确认授权 fixture",
        "version": 7,
        "managers": target,
        "operation": "add",
        "digest": "fixture",
    }
    with pytest.raises(RuntimeError, match="captured ordinary manager"):
        await check.command("确认授权 fixture", "已保存", managers=target)
    assert not any(path == "/api/v1/im/messages" for _, path, _ in checks.client.calls)
    assert not check.journal.execute("SELECT * FROM attempts").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
async def test_administrators_phase_adds_and_removes_only_ordinary_with_exact_versions(
    checks, manager_delivery, separator
):
    manager_delivery.group_list_reply = (
        "已启用群（编号十分钟有效）：" + separator + "1. 测试群"
    )
    before = copy.deepcopy(checks.client.tenant)
    await checks.check.administrators()
    await checks.check.finish()
    assert manager_delivery.confirmations == [
        {
            "action": "添加管理员",
            "version": 7,
            "before": [OWNER],
            "after": [OWNER, ORDINARY],
        },
        {
            "action": "移除管理员",
            "version": 8,
            "before": [OWNER, ORDINARY],
            "after": [OWNER],
        },
    ]
    assert checks.client.tenant == {**before, "version": 9}
    assert checks.check.expected == checks.client.tenant
    assert not checks.check.journal.execute(
        "SELECT 1 FROM attempts WHERE state='unknown'"
    ).fetchone()
    assert all(
        body["action"] == "status"
        for _, path, body in checks.client.calls
        if path == PREFIX + "open-service"
    )


@pytest.mark.asyncio
async def test_administrators_accepts_account_label_only_with_exact_native_identity(
    checks, manager_delivery
):
    manager_delivery.group_rule_reply = "@" + ORDINARY + " 广告审核：关闭"
    await checks.check.administrators()
    await checks.check.finish()
    assert any(
        event["case"] == "administrators_passed" for event in checks.check.events
    )
    assert checks.client.tenant["managers"] == [OWNER]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "已启用群（编号十分钟有效）：\n\n",
        "已启用群（编号十分钟有效）：\n\n1. 测试群\n2. 其他群",
        "已启用群（编号十分钟有效）：\n\n1. 测试群\n1. 其他群",
        "已启用群（编号十分钟有效）：\n\n2. 测试群",
    ],
)
async def test_administrators_group_list_format_never_weakens_exact_one_group(
    checks, manager_delivery, reply
):
    manager_delivery.group_list_reply = reply
    with pytest.raises(RuntimeError, match="exactly one authorized group"):
        await checks.check.administrators()
    await checks.check.finish()
    assert [row["action"] for row in manager_delivery.confirmations] == [
        "添加管理员",
        "移除管理员",
    ]
    assert checks.client.tenant["managers"] == [OWNER]
    assert checks.client.tenant["version"] == 9
    assert checks.check.manager_grant is None
    assert manager_delivery.probes == [("群列表", "manager-groups", False)]
    assert not any(
        event["case"] == "administrators_passed" for event in checks.check.events
    )
    assert all(
        event["known_committed"] and event["passed"]
        for event in checks.check.events
        if event["case"] == "manager_confirmation_result"
    )
    assert checks.check.journal.execute(
        "SELECT state FROM attempts WHERE kind='manager' ORDER BY rowid"
    ).fetchall() == [("verified",), ("verified",)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("group_sender", OWNER),
        ("group_mentions", []),
        ("group_mentions", ["unrelated-peer"]),
        ("group_rule_reply", "@someone 广告审核：关闭"),
    ],
)
async def test_administrators_group_reply_requires_exact_body_and_native_identity(
    checks, manager_delivery, field, value
):
    setattr(manager_delivery, field, value)
    with pytest.raises(RuntimeError, match="group rules or native mention"):
        await checks.check.administrators()
    await checks.check.finish()
    assert checks.client.tenant["managers"] == [OWNER]
    assert checks.check.manager_grant is None
    assert [row["action"] for row in manager_delivery.confirmations] == [
        "添加管理员",
        "移除管理员",
    ]
    assert not any(
        event["case"] == "administrators_passed" for event in checks.check.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["version", "foreign_manager", "owner_removed"])
async def test_administrators_preview_conflict_never_confirms_or_overwrites(
    checks, manager_delivery, fault
):
    manager_delivery.fault = fault
    with pytest.raises(RuntimeError):
        await checks.check.administrators()
    assert manager_delivery.injected
    changed = copy.deepcopy(checks.client.tenant)
    before = manager_delivery.commands.copy()
    await checks.check.finish()
    assert manager_delivery.commands == before
    assert checks.client.tenant == changed
    assert not manager_delivery.confirmations


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown_add", "unknown_remove"])
async def test_unknown_manager_confirmation_is_not_replayed_during_cleanup(
    checks, manager_delivery, fault
):
    manager_delivery.fault = fault
    with pytest.raises((TimeoutError, RuntimeError)):
        await checks.check.administrators()
    assert manager_delivery.injected
    managers = [OWNER] if fault == "unknown_add" else [OWNER, ORDINARY]
    assert checks.client.tenant["managers"] == managers
    assert checks.check.journal.execute(
        "SELECT 1 FROM attempts WHERE state='unknown'"
    ).fetchone()
    before = manager_delivery.commands.copy()
    with pytest.raises(RuntimeError, match="Cleanup incomplete"):
        await checks.check.finish()
    assert any(
        event["case"] == "cleanup"
        and any(error.startswith("manager:") for error in event["errors"])
        for event in checks.check.events
    )
    assert manager_delivery.commands == before
    assert len(manager_delivery.confirmations) == (1 if fault == "unknown_add" else 2)
    assert checks.client.tenant["managers"] == managers
    assert checks.check.journal.execute(
        "SELECT 1 FROM attempts WHERE kind='manager' AND state='unknown'"
    ).fetchone()
    with pytest.raises(RuntimeError):
        await checks.check.manager_update(add=False)
    assert manager_delivery.commands == before
    check = checks.check
    confirmation = check.manager_confirmation
    check.administrators_active = True
    with pytest.raises((sqlite3.IntegrityError, RuntimeError)):
        await check.command(
            confirmation["command"], "已保存本群设置", managers=confirmation["managers"]
        )
    check.administrators_active = False
    assert manager_delivery.commands == before


@pytest.mark.asyncio
async def test_known_committed_grant_with_lost_reply_is_revoked_by_new_confirmation(
    checks, manager_delivery
):
    manager_delivery.fault = "accepted_reply_lost"
    with pytest.raises(TimeoutError):
        await checks.check.administrators()
    assert manager_delivery.injected
    assert [row["action"] for row in manager_delivery.confirmations] == [
        "添加管理员",
        "移除管理员",
    ]
    assert checks.client.tenant["managers"] == [OWNER]
    assert checks.client.tenant["version"] == 9
    assert checks.check.manager_grant is None
    assert checks.check.administrators_active is False
    assert checks.check.journal.execute(
        "SELECT state FROM attempts WHERE kind='manager' ORDER BY rowid"
    ).fetchall() == [("accepted_assertion_failed",), ("verified",)]
    await checks.check.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "silent_processed",
        "silent_unprocessed",
        "missing_ingress",
        "unwitnessed",
        "outbox_unknown",
        "independent_reply",
    ],
)
async def test_wait_reply_requires_processed_ingress_and_independent_delivery(
    checks, monkeypatch, case
):
    import asyncio

    integrated = importlib.import_module("scripts.check_wsl_integrated_live")
    monkeypatch.setattr(
        checks.pair,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "sleep": AsyncMock()}),
    )
    incoming = (
        [] if case == "missing_ingress" else [{"team": GROUP, "state": "processed"}]
    )
    if case == "silent_unprocessed":
        incoming[0]["state"] = "received"
    outgoing = (
        [{"state": "accepted", "server_id": "independent-result"}]
        if case in {"unwitnessed", "outbox_unknown", "independent_reply"}
        else []
    )
    if case == "outbox_unknown":
        outgoing[0]["state"] = "unknown"
    checks.check.ordinary = SimpleNamespace(
        replies=(
            {"independent-result": {"sender": PEER, "text": "无权限", "mentions": []}}
            if case in {"outbox_unknown", "independent_reply"}
            else {}
        )
    )

    def rows(account, filename, query, values=()):
        assert account == PEER and filename == "messages.sqlite3"
        if "FROM inbox" in query:
            assert values == (PEER, "ordinary-exact-message")
            return copy.deepcopy(incoming)
        assert "FROM outbox" in query
        assert values == (f"{PEER}/{GROUP}/ordinary-exact-message/%/part/%",)
        return copy.deepcopy(outgoing)

    monkeypatch.setattr(integrated, "rows", rows)
    receipt = {"status": "accepted", "message": "ordinary-exact-message", "group": True}
    if case in {"silent_processed", "independent_reply"}:
        reply = await checks.check.wait_reply(receipt, allow_silent=True)
        assert reply == ("" if case == "silent_processed" else "无权限")
    else:
        with pytest.raises(RuntimeError, match="Ordinary result missing"):
            await checks.check.wait_reply(receipt, allow_silent=True)


@pytest.mark.asyncio
async def test_pair_api_rejects_foreign_routes_umos_and_test_windows(checks):
    invalid = [
        ("POST", PREFIX + "cashback", {"action": "settle"}),
        ("PATCH", PREFIX + "settings", {}),
        (
            "POST",
            "/api/v1/im/messages",
            {
                "umo": f"{SENDER}:FriendMessage:{ORDINARY}/{TARGET}",
                "operation_id": "bad",
            },
        ),
        (
            "POST",
            "/api/v1/im/messages",
            {"umo": f"{SENDER}:GroupMessage:{OWNER}/{GROUP}", "operation_id": "bad"},
        ),
        (
            "POST",
            REGISTRATION,
            {"action": "test_open", "instance_id": SENDER, "sender_instance": RECEIVER},
        ),
        (
            "POST",
            REGISTRATION,
            {
                "action": "test_open",
                "instance_id": RECEIVER,
                "sender_instance": SENDER,
                "groups": ["1516387"],
            },
        ),
    ]
    for method, path, body in invalid:
        with pytest.raises(RuntimeError):
            await checks.check.api(method, path, body)
    assert not checks.client.calls
    assert not checks.check.journal.execute("SELECT * FROM attempts").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_pair_grant_cleanup_uses_current_cas_and_preserves_concurrent_edits(
    checks, concurrent
):
    original = copy.deepcopy(checks.client.bots[SENDER]["proactive_send"])
    await checks.check.grant(SENDER, [TARGET])
    assert checks.client.bots[SENDER]["proactive_send"]["targets"] == [TARGET]
    assert checks.client.bots[RECEIVER]["proactive_send"] == original
    if concurrent:
        checks.client.bots[SENDER]["proactive_send"]["targets"].append("concurrent")
        checks.client.bots[SENDER]["revision"] += 1
        changed = copy.deepcopy(checks.client.bots[SENDER])
        with pytest.raises(RuntimeError, match="Cleanup incomplete"):
            await checks.check.finish()
        assert checks.client.bots[SENDER] == changed
    else:
        await checks.check.finish()
        assert checks.client.bots[SENDER]["proactive_send"] == original
    writes = [
        body
        for method, path, body in checks.client.calls
        if method == "POST" and path == PREFIX + "settings"
    ]
    assert len(writes) == (1 if concurrent else 2)
    if not concurrent:
        assert [body["revision"] for body in writes] == [3, 4]
    assert checks.client.tenant == checks.check.original_tenant


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_rejoin_menu_restore_never_overwrites_a_concurrent_grant(
    checks, concurrent
):
    check = checks.menu.MenuCheck(checks.client, checks.check.output)
    check.original = copy.deepcopy(checks.client.bots[SENDER]["proactive_send"])
    check.proposed = {**check.original, "enabled": True, "targets": [TARGET]}
    check.changed = True
    checks.client.bots[SENDER]["proactive_send"] = copy.deepcopy(check.proposed)
    checks.client.bots[SENDER]["revision"] = 10
    if concurrent:
        checks.client.bots[SENDER]["proactive_send"]["targets"].append("another-writer")
        before = copy.deepcopy(checks.client.bots[SENDER])
        with pytest.raises((AssertionError, RuntimeError)):
            await check.restore()
        assert checks.client.bots[SENDER] == before
        assert check.changed is True
        assert not any(method == "POST" for method, _, _ in checks.client.calls)
    else:
        await check.restore()
        assert check.changed is False
        assert checks.client.bots[SENDER]["proactive_send"] == check.original
        assert checks.client.calls[1][2]["revision"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_rules_restore_is_cas_scoped_and_never_edits_authority(
    checks, concurrent
):
    original = copy.deepcopy(checks.client.tenant)
    assert type(original["paused"]) is int and original["paused"] == 0
    await checks.check.rules({"advertising": True})
    assert checks.client.tenant["managers"] == [OWNER]
    if concurrent:
        checks.client.tenant["rules"]["keywords"] = ["concurrent-rule"]
        checks.client.tenant["version"] += 1
        changed = copy.deepcopy(checks.client.tenant)
        with pytest.raises(RuntimeError, match="Cleanup incomplete"):
            await checks.check.finish()
        assert checks.client.tenant == changed
    else:
        await checks.check.finish()
        assert checks.client.tenant["rules"] == original["rules"]
    changes = [
        (body["action"], body["payload"])
        for method, path, body in checks.client.calls
        if method == "POST"
        and path == PREFIX + "open-service"
        and body["action"] in {"tenant_preview", "tenant"}
    ]
    assert [action for action, _ in changes] == ["tenant_preview", "tenant"] * (
        1 if concurrent else 2
    )
    assert all(
        body["account"] == PEER
        and body["group_id"] == GROUP
        and body["managers"] == [OWNER]
        and type(body["paused"]) is bool
        and body["paused"] is False
        for _, body in changes
    )
    writes = [body for action, body in changes if action == "tenant"]
    assert [body["version"] for body in writes] == ([7] if concurrent else [7, 8])
    for key in ("active", "paused", "present"):
        assert type(checks.client.tenant[key]) is int
        assert checks.client.tenant[key] == original[key]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["tenant_preview", "tenant"])
async def test_dashboard_rejects_native_integer_paused_in_rule_write(checks, action):
    original = copy.deepcopy(checks.client.tenant)
    payload = {
        key: copy.deepcopy(original[key])
        for key in ("account", "group_id", "version", "managers", "paused", "rules")
    }
    payload["rules"]["advertising"] = True
    if action == "tenant":
        payload["confirmation"] = "fixture-confirmation"
    with pytest.raises(RuntimeError, match="tenant_invalid: paused must be bool"):
        await checks.check.api(
            "POST",
            PREFIX + "open-service",
            {"action": action, "payload": payload},
        )
    assert checks.client.calls[-1] == (
        "POST",
        PREFIX + "open-service",
        {"action": action, "payload": payload},
    )
    assert checks.client.tenant == original


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "unknown"])
async def test_unknown_private_send_is_durable_and_same_operation_never_retries(
    checks, outcome
):
    checks.client.send_outcome = outcome
    body = {
        "umo": f"{SENDER}:FriendMessage:{OWNER}/{TARGET}",
        "operation_id": "fixture-private-once",
        "message": [{"type": "plain", "text": "fixture"}],
    }
    with pytest.raises((TimeoutError, RuntimeError)):
        await checks.check.api("POST", "/api/v1/im/messages", body)
    saved = checks.check.journal.execute("SELECT id,state FROM attempts").fetchall()
    assert saved == [("fixture-private-once", "unknown")]
    with pytest.raises((sqlite3.IntegrityError, RuntimeError)):
        await checks.check.api("POST", "/api/v1/im/messages", body)
    assert sum(path == "/api/v1/im/messages" for _, path, _ in checks.client.calls) == 1


def test_rejoin_receipts_are_exact_read_only_and_do_not_touch_finance(checks):
    control = checks.rejoin.CONTROL
    control.mkdir(mode=0o700)
    journal = control / "control.sqlite3"
    source = (
        checks.rejoin.ROOT / "data/plugin_data/wangshangliao_open/open-service.sqlite3"
    )
    source.parent.mkdir(parents=True)
    finance = (
        checks.rejoin.ROOT / "data/plugin_data/wangshangliao_cashback/cashback.sqlite3"
    )
    finance.parent.mkdir(parents=True)
    with closing(sqlite3.connect(finance)) as db, db:
        db.execute("CREATE TABLE untouched(amount INTEGER)")
        db.execute("INSERT INTO untouched VALUES(18)")
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute(
            "CREATE TABLE attempts(id TEXT,group_id TEXT,action TEXT,created REAL,"
            "state TEXT,provider_accepted INTEGER,apply_id TEXT)"
        )
        db.executemany(
            "INSERT INTO attempts VALUES(?,?,?,1,'verified',1,?)",
            [
                ("removed", GROUP, "remove", ""),
                ("invited", GROUP, "invite", "211579722"),
            ],
        )
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE tenants(account TEXT,group_id TEXT,managers TEXT)")
        db.execute(
            "INSERT INTO tenants VALUES(?,?,?)", (PEER, GROUP, json.dumps([OWNER]))
        )
    before = {file: file.read_bytes() for file in (journal, source, finance)}
    attempts = {"remove": "removed", "invite": "invited", "accept": None}
    receipts, tenant = checks.rejoin.evidence(attempts)
    assert [row["action"] for row in receipts] == ["remove", "invite"]
    assert tenant["account"] == PEER and tenant["group_id"] == GROUP
    assert json.loads(tenant["managers"]) == [OWNER]
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute("UPDATE attempts SET group_id='1516387' WHERE id='invited'")
    with pytest.raises((AssertionError, RuntimeError)):
        checks.rejoin.evidence(attempts)
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute(
            "UPDATE attempts SET group_id=?,state='unknown' WHERE id='invited'",
            (GROUP,),
        )
    with pytest.raises((AssertionError, RuntimeError)):
        checks.rejoin.evidence(attempts)
    assert source.read_bytes() == before[source]
    assert finance.read_bytes() == before[finance]
    assert all("?mode=ro" in name and flags.get("uri") for name, flags in checks.opens)
    assert not any(str(finance) in name for name, _ in checks.opens)
    with pytest.raises(RuntimeError, match="Read-only"):
        checks.pair.read_rows(finance, "UPDATE untouched SET amount=0")
    assert finance.read_bytes() == before[finance]


@pytest.mark.asyncio
async def test_unmute_does_not_remove_a_later_foreign_manual_mute(checks):
    check = checks.check
    check.mute_possible = True
    check.manual_mute_message = "own-mute-message"
    team = "private/31846829/fixture"
    origin = f"{RECEIVER}:FriendMessage:{PEER}/{team}"
    own = (
        "command/"
        + hashlib.sha256(
            f"{PEER}/{origin}/{check.manual_mute_message}".encode()
        ).hexdigest()
    )
    with (
        closing(sqlite3.connect(check.native_path.parent / "messages.sqlite3")) as db,
        db,
    ):
        db.execute("CREATE TABLE inbox(account TEXT,message TEXT,team TEXT)")
        db.execute(
            "INSERT INTO inbox VALUES(?,?,?)", (PEER, check.manual_mute_message, team)
        )
    with closing(sqlite3.connect(check.native_path)) as db, db:
        db.execute(
            "CREATE TABLE operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
        )
        db.execute(
            "CREATE TABLE automatic_mutes(operation TEXT,account TEXT,group_id TEXT,"
            "member TEXT,peer TEXT,message TEXT,status TEXT,created REAL,closed INTEGER)"
        )
        for operation, minutes in ((own, 1), ("foreign-later-mute", 60)):
            db.execute(
                "INSERT INTO operations VALUES(?,'fixture',?)",
                (
                    operation,
                    json.dumps(
                        {
                            "status": "accepted",
                            "action": "mute",
                            "group": int(GROUP),
                            "member": int(ORDINARY),
                            "minutes": minutes,
                        }
                    ),
                ),
            )
    before = check.native_path.read_bytes()
    check.member_number = AsyncMock(return_value="3")
    check.command = AsyncMock(return_value="fixture-unmute")
    with pytest.raises(RuntimeError):
        await check.unmute()
    check.command.assert_not_awaited()
    assert check.mute_possible is True
    assert not check.unmuted_operations
    assert check.native_path.read_bytes() == before


@pytest.mark.asyncio
async def test_unmute_without_own_receipt_retains_unresolved_cleanup(checks):
    check = checks.check
    check.mute_possible = True
    check.command = AsyncMock(return_value="fixture-unmute")
    with pytest.raises(RuntimeError):
        await check.unmute()
    assert check.mute_possible is True
    check.command.assert_not_awaited()
    with pytest.raises(RuntimeError, match="Cleanup incomplete"):
        await check.finish()
    assert check.mute_possible is True
    assert any(
        event["case"] == "cleanup"
        and any(error.startswith("unmute:") for error in event["errors"])
        for event in check.events
    )
