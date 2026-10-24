"""Offline regressions for read-only shared-service stability observation."""

import copy
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import url2pathname

import httpx
import pytest

from scripts import check_wsl_account_names as account_names
from scripts import check_wsl_shared_soak as shared_soak

SERVICES = {
    "astrbot.service": (2 * 1024**3, 256),
}


@pytest.fixture
def observation(tmp_path, monkeypatch):
    """Run the real sampler against synthetic state and temporary read-only DBs.

    Args:
        tmp_path: Exclusive directory for all fixture data and evidence.
        monkeypatch: Test-scoped replacements for external IO and virtual time.

    Yields:
        Mutable service/API state and the isolated sampler's call evidence.
    """
    root = tmp_path / "root"
    data = root / "data"
    data.mkdir(parents=True)
    config = {
        "dashboard": {
            "username": "offline-monitor",
            "jwt_secret": "synthetic-monitor-secret-not-a-production-key",
        },
        "platform": [
            {"id": "wsl-a", "type": "wangshangliao", "enable": True},
            {"id": "wsl-b", "type": "wangshangliao", "enable": True},
            {"id": "pepper", "type": "lajiaoliao", "enable": True},
            {"id": "local", "type": "webchat"},
            {"id": "disabled", "type": "wangshangliao", "enable": False},
        ],
    }
    config_path = data / "cmd_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    databases = {
        "cashback": data / "plugin_data/wangshangliao_cashback/cashback.sqlite3",
        "tenants": data / "plugin_data/wangshangliao_open/open-service.sqlite3",
        "friend_a": data / "platform_data/wangshangliao/wsl-a/friend_approval.sqlite3",
        "friend_b": data / "platform_data/wangshangliao/wsl-b/friend_approval.sqlite3",
    }
    for database in databases.values():
        database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(databases["cashback"])) as db, db:
        db.executescript("""
            CREATE TABLE entries(
                id,group_id,member,inviter,amount,state,verification,eligible,
                present,ever_left,paid_at,paid_by,member_class);
            INSERT INTO entries VALUES(
                1,'1516387','301','302',12345,'pending','verified',1,
                1,0,0,'','member');
            CREATE TABLE groups(
                id,enabled,rate,agent_rate,qualification_seconds,
                require_speech,agent_only);
            INSERT INTO groups VALUES('1516387',1,100,150,86400,1,0);
        """)
    with closing(sqlite3.connect(databases["tenants"])) as db, db:
        db.execute(
            "CREATE TABLE tenants(account,group_id,active,paused,version,managers,rules)"
        )
        db.executemany(
            "INSERT INTO tenants VALUES(?,?,1,1,7,?,?)",
            [
                (account, group, '["manager"]', '{"welcome":false}')
                for account in ("101", "202")
                for group in ("1516387", "1488006")
            ],
        )
        db.execute("INSERT INTO tenants VALUES('101','sandbox',1,0,1,'[]','{}')")
    for name, account, enabled, paused in (
        ("friend_a", "101", 1, ""),
        ("friend_b", "202", 0, "business_rejected"),
    ):
        with closing(sqlite3.connect(databases[name])) as db, db:
            db.execute("CREATE TABLE approval_settings(account,enabled,paused,version)")
            db.execute(
                "INSERT INTO approval_settings VALUES(?,?,?,2)",
                (account, enabled, paused),
            )

    state = {
        "mode": "integrated",
        "ai_budget": {"confirmed": False},
        "accounts": [
            {"instance_id": "wsl-a", "state": "online"},
            {"instance_id": "wsl-b", "state": "online"},
        ],
        "queue_health": {"pending": 0},
    }
    platforms = [
        {
            "id": item["id"],
            "type": item["type"],
            "status": "stopped" if item.get("enable") is False else "running",
            "connection_state": (
                "offline"
                if item["type"] == "webchat" or item.get("enable") is False
                else "online"
            ),
        }
        for item in config["platform"]
    ]
    processes = {
        service: {
            "MainPID": str(1234 + index),
            "MemoryCurrent": str(limits[0] // 2),
            "TasksCurrent": "16",
            "NRestarts": "0",
            "ActiveState": "active",
        }
        for index, (service, limits) in enumerate(SERVICES.items())
    }
    cashier = {"status": 401, "body": {"status": "error"}}
    requests, clients, connections, hooks = [], [], [], []
    clock = [0.0]
    started = 1_700_000_000
    output_root = tmp_path / "backups"
    output_root.mkdir()
    output = output_root / "wsl-shared-soak-offline"

    def path(*parts):
        result = Path(*parts)
        if result == Path("/opt/backups"):
            return output_root
        assert result.is_relative_to(tmp_path)
        return result

    def connect(database, *, uri):
        parsed = urlsplit(database)
        assert uri is True and parsed.scheme == "file"
        assert parse_qs(parsed.query) == {"mode": ["ro"]}
        assert Path(url2pathname(parsed.path)).is_relative_to(root)
        connection = sqlite3.connect(database, uri=True)
        connections.append(connection)
        return connection

    readonly_connect = Mock(side_effect=connect)

    def response(request):
        requests.append(request)
        assert "cookie" not in request.headers
        if request.url.path.endswith("/cashier"):
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == "Bearer offline-monitor-token"
        if (
            str(request.url)
            == "http://127.0.0.1:6185" + shared_soak.PREFIX + "open-service"
        ):
            assert request.method == "POST"
            assert json.loads(request.content) == {"action": "status", "payload": {}}
            return httpx.Response(200, json={"data": copy.deepcopy(state)})
        if str(request.url) == "http://127.0.0.1:6185/api/v1/bots/stats":
            assert request.method == "GET"
            return httpx.Response(
                200, json={"data": {"platforms": copy.deepcopy(platforms)}}
            )
        assert str(request.url) == "http://127.0.0.1:6185" + shared_soak.PREFIX + "cashier"
        assert request.method == "POST" and json.loads(request.content) == {"action": "me", "payload": {}}
        return httpx.Response(cashier["status"], json=copy.deepcopy(cashier["body"]))

    def client(**kwargs):
        assert kwargs == {
            "base_url": "http://127.0.0.1:6185",
            "trust_env": False,
            "timeout": 20,
            "headers": {"Authorization": "Bearer offline-monitor-token"},
        }
        result = httpx.AsyncClient(transport=httpx.MockTransport(response), **kwargs)
        clients.append(result)
        return result

    def check_output(command, *, text):
        assert text is True
        assert command == [
            "systemctl",
            "show",
            command[2],
            "-p",
            "MainPID",
            "-p",
            "MemoryCurrent",
            "-p",
            "TasksCurrent",
            "-p",
            "NRestarts",
            "-p",
            "ActiveState",
        ]
        assert command[2] in SERVICES
        return "\n".join(
            f"{key}={value}" for key, value in processes[command[2]].items()
        )

    async def sleep(seconds):
        assert 0 < seconds <= 30
        clock[0] += seconds
        for hook in hooks:
            hook()

    sleep_mock = AsyncMock(side_effect=sleep)
    thread_mock = AsyncMock(side_effect=lambda function, *a, **kw: function(*a, **kw))
    process_mock = Mock(side_effect=check_output)
    client_mock = Mock(side_effect=client)
    token_mock = Mock(return_value="offline-monitor-token")
    snapshot_mock = Mock(wraps=account_names.snapshot)
    monkeypatch.setattr(shared_soak, "ROOT", root)
    monkeypatch.setattr(account_names, "ROOT", root)
    monkeypatch.setattr(shared_soak, "Path", path)
    monkeypatch.setattr(shared_soak, "snapshot", snapshot_mock)
    for module in (shared_soak, account_names):
        monkeypatch.setattr(
            module, "sqlite3", SimpleNamespace(connect=readonly_connect)
        )
    monkeypatch.setattr(shared_soak, "httpx", SimpleNamespace(AsyncClient=client_mock))
    monkeypatch.setattr(shared_soak, "jwt", SimpleNamespace(encode=token_mock))
    monkeypatch.setattr(
        shared_soak, "subprocess", SimpleNamespace(check_output=process_mock)
    )
    monkeypatch.setattr(
        shared_soak, "asyncio", SimpleNamespace(sleep=sleep_mock, to_thread=thread_mock)
    )
    monkeypatch.setattr(
        shared_soak,
        "time",
        SimpleNamespace(
            time=lambda: started + clock[0],
            monotonic=lambda: clock[0],
            gmtime=lambda: (),
            strftime=lambda *_: "wsl-shared-soak-offline",
        ),
    )
    monkeypatch.setattr(
        shared_soak,
        "shutil",
        SimpleNamespace(disk_usage=Mock(return_value=SimpleNamespace(free=2**40))),
    )
    monkeypatch.setattr(sys, "argv", ["check_wsl_shared_soak.py", "--seconds", "60"])
    try:
        yield SimpleNamespace(
            root=root,
            config=config,
            config_path=config_path,
            databases=databases,
            state=state,
            platforms=platforms,
            processes=processes,
            cashier=cashier,
            hooks=hooks,
            clock=clock,
            started=started,
            output=output,
            evidence=output / "evidence.json",
            requests=requests,
            clients=clients,
            sleep=sleep_mock,
            thread=thread_mock,
            systemctl=process_mock,
            client=client_mock,
            token=token_mock,
            snapshot=snapshot_mock,
            connect=readonly_connect,
        )
    finally:
        for connection in connections:
            connection.close()


async def assert_rejects_second_sample(observation):
    """Check that drift aborts rather than publishing successful final evidence.

    Args:
        observation: Fixture whose first virtual sleep introduces unsafe drift.
    """
    with pytest.raises(AssertionError):
        await shared_soak.main()
    evidence = json.loads(observation.evidence.read_text("utf-8"))
    assert evidence["complete"] is False
    assert evidence["elapsed_seconds"] == 0
    assert len(evidence["samples"]) == 1
    assert observation.clock[0] == 30
    observation.sleep.assert_awaited_once_with(30)
    assert all(client.is_closed for client in observation.clients)


@pytest.mark.asyncio
async def test_three_configured_accounts_are_monitored_without_weakening_membership(
    observation,
):
    observation.config["platform"].append(
        {"id": "wsl-c", "type": "wangshangliao", "enable": True}
    )
    observation.config_path.write_text(json.dumps(observation.config))
    observation.state["accounts"].append({"instance_id": "wsl-c", "state": "online"})
    observation.platforms.append(
        {
            "id": "wsl-c",
            "type": "wangshangliao",
            "status": "running",
            "connection_state": "online",
        }
    )
    await shared_soak.main()
    assert json.loads(observation.evidence.read_text())["complete"] is True


@pytest.mark.asyncio
async def test_stable_observation_is_read_only_bounded_and_writes_private_evidence(
    observation,
):
    protected = [observation.config_path, *observation.databases.values()]
    fingerprints = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected
    }
    with (
        patch.object(Path, "mkdir", autospec=True, side_effect=Path.mkdir) as mkdir,
        patch.object(Path, "chmod", autospec=True, side_effect=Path.chmod) as chmod,
    ):
        await shared_soak.main()
    mkdir.assert_any_call(observation.output, mode=0o700)
    chmod.assert_any_call(observation.evidence, 0o600)
    evidence = json.loads(observation.evidence.read_text("utf-8"))
    assert evidence["required_seconds"] == evidence["elapsed_seconds"] == 60
    assert evidence["complete"] is True
    assert [sample["at"] for sample in evidence["samples"]] == [
        observation.started + elapsed for elapsed in (0, 30, 60)
    ]
    for sample in evidence["samples"]:
        assert sample["services"] == observation.processes
        assert sample["process"] == observation.processes["astrbot.service"]
        assert sample["cashier_anonymous_status"] == 401
        assert sample["platform_stats"]["platforms"] == observation.platforms
        assert sample["queue_health"] == {"pending": 0}
        assert sample["disk_free_bytes"] == 2**40
    if os.name == "posix":
        assert observation.output.stat().st_mode & 0o777 == 0o700
        assert observation.evidence.stat().st_mode & 0o777 == 0o600
    assert [call.args for call in observation.sleep.await_args_list] == [(30,), (30,)]
    assert observation.systemctl.call_count == observation.thread.await_count == 3
    assert observation.snapshot.call_count == 4
    assert observation.connect.call_count == 16
    assert len(observation.requests) == 9
    observation.client.assert_called_once()
    observation.token.assert_called_once_with(
        {"username": "offline-monitor", "exp": observation.started + 7200},
        observation.config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    assert all(client.is_closed for client in observation.clients)
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected
    } == fingerprints
    assert "offline-monitor-token" not in observation.evidence.read_text("utf-8")
    assert observation.config["dashboard"]["jwt_secret"] not in (
        observation.evidence.read_text("utf-8")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("service", SERVICES)
async def test_service_without_initial_pid_is_rejected(observation, service):
    observation.processes[service]["MainPID"] = "0"
    with pytest.raises(AssertionError):
        await shared_soak.main()
    assert not observation.evidence.exists()
    observation.sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize(
    "field,value",
    [
        ("MainPID", "9999"),
        ("MainPID", "0"),
        ("NRestarts", "1"),
        ("ActiveState", "inactive"),
        ("MemoryCurrent", None),
        ("TasksCurrent", None),
    ],
)
async def test_each_service_rejects_pid_restart_memory_task_or_state_drift(
    observation, service, field, value
):
    if value is None:
        limits = SERVICES[service]
        value = str(limits[0] if field == "MemoryCurrent" else limits[1] + 1)
    observation.hooks.append(
        lambda: observation.processes[service].__setitem__(field, value)
    )
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", SERVICES)
async def test_service_allows_last_memory_byte_and_inclusive_task_limit(
    observation, service
):
    memory, tasks = SERVICES[service]
    observation.processes[service]["MemoryCurrent"] = str(memory - 1)
    observation.processes[service]["TasksCurrent"] = str(tasks)
    await shared_soak.main()
    assert json.loads(observation.evidence.read_text("utf-8"))["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("instance", ["wsl-a", "wsl-b", "pepper", "local"])
async def test_no_enabled_instance_may_disappear_from_stats(observation, instance):
    observation.hooks.append(
        lambda: observation.platforms.__setitem__(
            slice(None),
            [row for row in observation.platforms if row["id"] != instance],
        )
    )
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "instance,field,value",
    [
        ("wsl-a", "status", "stopped"),
        ("wsl-b", "status", "stopped"),
        ("pepper", "status", "stopped"),
        ("local", "status", "stopped"),
        ("wsl-a", "connection_state", "offline"),
        ("wsl-b", "connection_state", "offline"),
        ("pepper", "connection_state", "offline"),
    ],
)
async def test_enabled_platforms_must_be_running_and_native_accounts_online(
    observation, instance, field, value
):
    row = next(row for row in observation.platforms if row["id"] == instance)
    observation.hooks.append(lambda: row.__setitem__(field, value))
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (200, {"status": "error"}),
        (403, {"status": "error"}),
        (302, {"status": "error"}),
        (401, {}),
        (401, {"status": "ok"}),
    ],
)
async def test_cashier_anonymous_requires_both_401_and_error(
    observation, status, body
):
    observation.hooks.append(
        lambda: observation.cashier.update(status=status, body=body)
    )
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "ai_enabled",
        "not_integrated",
        "missing_account",
        "extra_account",
        "offline_account",
    ],
)
async def test_public_ai_gate_mode_and_two_account_health_remain_protected(
    observation, change
):
    def drift():
        if change == "ai_enabled":
            observation.state["ai_budget"]["confirmed"] = True
        elif change == "not_integrated":
            observation.state["mode"] = "isolated"
        elif change == "missing_account":
            observation.state["accounts"].pop()
        elif change == "extra_account":
            observation.state["accounts"].append({"state": "online"})
        else:
            observation.state["accounts"][0]["state"] = "offline"

    observation.hooks.append(drift)
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database,sql",
    [
        ("cashback", "UPDATE entries SET amount=54321"),
        ("cashback", "UPDATE entries SET state='paid'"),
        (
            "cashback",
            "INSERT INTO entries SELECT 2,group_id,member,inviter,amount,state,"
            "verification,eligible,present,ever_left,paid_at,paid_by,member_class FROM entries",
        ),
        ("cashback", "DELETE FROM entries"),
        ("cashback", "UPDATE groups SET enabled=0"),
        ("cashback", "UPDATE groups SET rate=999"),
        ("cashback", "UPDATE groups SET agent_rate=999"),
        ("cashback", "UPDATE groups SET qualification_seconds=1"),
        ("cashback", "UPDATE groups SET require_speech=0"),
        ("cashback", "UPDATE groups SET agent_only=1"),
        ("tenants", "UPDATE tenants SET active=0 WHERE group_id='1516387'"),
        ("tenants", "UPDATE tenants SET version=8 WHERE group_id='1516387'"),
        ("tenants", "UPDATE tenants SET managers='[]' WHERE group_id='1516387'"),
        ("tenants", "UPDATE tenants SET rules='{}' WHERE group_id='1516387'"),
        ("tenants", "DELETE FROM tenants WHERE group_id='1516387'"),
    ],
)
async def test_financial_fingerprint_group_rules_and_formal_settings_are_immutable(
    observation, database, sql
):
    def drift():
        with closing(sqlite3.connect(observation.databases[database])) as db, db:
            db.execute(sql)

    observation.hooks.append(drift)
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["101", "202"])
@pytest.mark.parametrize("group", ["1516387", "1488006"])
async def test_all_four_formal_bindings_preserve_their_pause(
    observation, account, group
):
    def drift():
        with closing(sqlite3.connect(observation.databases["tenants"])) as db, db:
            db.execute(
                "UPDATE tenants SET paused=0 WHERE account=? AND group_id=?",
                (account, group),
            )

    observation.hooks.append(drift)
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
async def test_saved_cmd_config_must_remain_byte_identical(observation):
    observation.hooks.append(
        lambda: observation.config_path.write_text(
            json.dumps(observation.config, indent=2), encoding="utf-8"
        )
    )
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
@pytest.mark.parametrize("database", ["friend_a", "friend_b"])
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE approval_settings SET account='303'",
        "UPDATE approval_settings SET enabled=1-enabled",
        "UPDATE approval_settings SET paused='friend_approval_unknown'",
        "UPDATE approval_settings SET version=version+1",
        "DELETE FROM approval_settings",
    ],
)
async def test_each_friends_account_toggle_hold_version_and_identity_are_immutable(
    observation, database, sql
):
    def drift():
        with closing(sqlite3.connect(observation.databases[database])) as db, db:
            db.execute(sql)

    observation.hooks.append(drift)
    await assert_rejects_second_sample(observation)


@pytest.mark.asyncio
async def test_existing_exceptional_friend_pause_cannot_be_cleared(observation):
    def drift():
        with closing(sqlite3.connect(observation.databases["friend_b"])) as db, db:
            db.execute("UPDATE approval_settings SET paused=''")

    observation.hooks.append(drift)
    await assert_rejects_second_sample(observation)
