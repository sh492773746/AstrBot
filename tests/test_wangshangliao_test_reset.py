"""Local-only reset regressions with no production files or platform requests."""

import json
import os
import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("fcntl", reason="POSIX-only maintenance CLI")

from scripts import reset_wsl_test_group as module


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    """Prevent test resets from making any real platform request.

    Args:
        monkeypatch: Test-scoped patch manager.
    """
    monkeypatch.setattr(
        socket.socket,
        "connect",
        Mock(side_effect=AssertionError("Network forbidden in local reset tests")),
    )


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Create an isolated, departed test tenant and adjacent protected namespaces.

    Args:
        tmp_path: Test-owned private temporary directory.
        monkeypatch: Test-scoped patch manager.

    Returns:
        Reset utility and its fixture paths.
    """
    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    original_stat = Path.stat

    def private_parent_stat(path, *, follow_symlinks=True):
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path == tmp_path:
            # Simulate only the root-owned external archive parent in this fixture.
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", private_parent_stat)
    root = tmp_path / "application"
    data = root / "data"
    native = data / "platform_data/wangshangliao" / ("a" * 64)
    native.mkdir(parents=True)
    (data / "cmd_config.json").write_text(
        json.dumps(
            {
                "wangshangliao_open": {"mode": "integrated"},
                "platform": [
                    {
                        "id": "wangshangliao_peer",
                        "type": "wangshangliao",
                        "account_id": module.ACCOUNT,
                    },
                    {
                        "id": "wangshangliao_owner",
                        "type": "wangshangliao",
                        "account_id": module.MANAGER,
                    },
                ],
            }
        )
    )
    (native.parent / "account-roots.json").write_text(
        json.dumps({module.ACCOUNT: "a" * 64, module.MANAGER: "b" * 64})
    )
    for name in (module.OPEN, module.FINANCE, module.SETTINGS, module.GRANTS):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / module.OPEN)) as db, db:
        db.executescript("""
            CREATE TABLE tenants(
                account TEXT,group_id TEXT,team TEXT,managers TEXT,
                version INTEGER,epoch INTEGER,present INTEGER,active INTEGER,
                admission TEXT,cleanup_epoch INTEGER,cleanup_done INTEGER);
            CREATE TABLE actor_bindings(
                account TEXT,uid TEXT,peer TEXT,conflicted INTEGER);
            CREATE TABLE audit(
                id INTEGER PRIMARY KEY,account TEXT,group_id TEXT,
                action TEXT,detail TEXT);
            CREATE TABLE retired_groups(group_id TEXT);
            CREATE TABLE membership_notices(
                account TEXT,team TEXT,message TEXT,state TEXT);
            CREATE TABLE capabilities(account TEXT,group_id TEXT,digest TEXT);
            CREATE TABLE ai_charges(
                account TEXT,group_id TEXT,charged_micro INTEGER);
        """)
        db.executemany(
            "INSERT INTO tenants VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    module.ACCOUNT,
                    module.GROUP,
                    "64031167010",
                    json.dumps([module.MANAGER]),
                    201,
                    19,
                    0,
                    0,
                    "departed",
                    19,
                    19,
                ),
                (
                    module.MANAGER,
                    module.GROUP,
                    "64031167010",
                    "[]",
                    206,
                    1,
                    1,
                    0,
                    "legacy",
                    0,
                    0,
                ),
                (
                    module.ACCOUNT,
                    "1516387",
                    "formal-team",
                    json.dumps([module.MANAGER]),
                    99,
                    1,
                    1,
                    0,
                    "legacy",
                    0,
                    0,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO actor_bindings VALUES(?,?,?,0)",
            [
                (module.ACCOUNT, module.ACCOUNT, "10002"),
                (module.ACCOUNT, module.MANAGER, "10001"),
            ],
        )
        db.execute(
            "INSERT INTO audit VALUES(1,?,?,?,?)",
            (
                module.ACCOUNT,
                module.GROUP,
                "departure_confirmed",
                json.dumps({"epoch": 19}),
            ),
        )
        db.execute("INSERT INTO retired_groups VALUES('1143980')")
        db.executemany(
            "INSERT INTO membership_notices VALUES(?,?,?,?)",
            [
                (module.ACCOUNT, "64031167010", "departed", "departure_verified"),
                (module.MANAGER, "64031167010", "owner", "verified"),
            ],
        )
        db.executemany(
            "INSERT INTO capabilities VALUES(?,?,?)",
            [
                (module.ACCOUNT, module.GROUP, "test-token"),
                (module.MANAGER, module.GROUP, "owner-token"),
                (module.ACCOUNT, "1516387", "formal-token"),
            ],
        )
    with closing(sqlite3.connect(root / module.FINANCE)) as db, db:
        columns = ",".join(
            f'"{key}" TEXT' for key in module.FINANCIAL_FIELDS.split(",")
        )
        db.execute(f"CREATE TABLE entries({columns})")
        db.execute("CREATE TABLE groups(id TEXT,enabled INTEGER,rate INTEGER)")
        db.execute("INSERT INTO groups VALUES('1516387',0,500)")
        db.execute(
            "INSERT INTO entries(id,group_id,member,amount) VALUES('debt','1516387',?,500)",
            (module.GROUP,),
        )
    with closing(sqlite3.connect(root / module.SETTINGS)) as db, db:
        db.execute(
            "CREATE TABLE policies(instance TEXT,account TEXT,version INTEGER,payload TEXT)"
        )
        db.execute(
            "INSERT INTO policies VALUES(?,?,1,?)",
            (
                "wangshangliao_peer",
                module.ACCOUNT,
                json.dumps(
                    {
                        "enabled_groups": [module.GROUP, "1516387"],
                        "moderation": {
                            "permissions": {module.GROUP: ["mute"], "1516387": []},
                            "mute_keywords": [module.GROUP],
                        },
                    }
                ),
            ),
        )
    with closing(sqlite3.connect(root / module.GRANTS)) as db, db:
        db.execute("CREATE TABLE grants(account TEXT,group_id TEXT,users TEXT)")
        db.executemany(
            "INSERT INTO grants VALUES(?,?,?)",
            [
                (module.ACCOUNT, module.GROUP, json.dumps([module.MANAGER])),
                (module.MANAGER, module.GROUP, '["another"]'),
            ],
        )
    with closing(sqlite3.connect(root / module.FRAMEWORK)) as db, db:
        db.execute("CREATE TABLE conversations(user_id TEXT,content TEXT)")
        db.executemany(
            "INSERT INTO conversations VALUES(?,?)",
            [
                (
                    f"wangshangliao_peer:GroupMessage:{module.ACCOUNT}/{module.GROUP}",
                    "test-content",
                ),
                (
                    f"wangshangliao_owner:GroupMessage:{module.MANAGER}/{module.GROUP}",
                    "owner-content",
                ),
                (
                    f"telegram:GroupMessage:{module.ACCOUNT}/{module.GROUP}",
                    "unrelated-platform",
                ),
                (
                    f"wangshangliao_peer:GroupMessage:{module.ACCOUNT}/{module.GROUP}1",
                    "different-group",
                ),
            ],
        )
    with closing(sqlite3.connect(native / "messages.sqlite3")) as db, db:
        db.execute("CREATE TABLE inbox(account TEXT,team TEXT,payload TEXT)")
        db.executemany(
            "INSERT INTO inbox VALUES(?,?,?)",
            [
                (module.ACCOUNT, module.GROUP, "test-content"),
                (module.ACCOUNT, "unsupported/1/64031167010", "test-notice"),
                (module.ACCOUNT, "64031167010", "bare-transport-number-keep"),
                (module.MANAGER, module.GROUP, "owner-content"),
                (module.ACCOUNT, "1516387", module.GROUP),
            ],
        )
    with closing(sqlite3.connect(native / "moderation.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE progressive_warnings(account TEXT,group_id TEXT,status TEXT)"
        )
        db.execute(
            "INSERT INTO progressive_warnings VALUES(?,?,'warned')",
            (module.ACCOUNT, module.GROUP),
        )
        db.execute("CREATE TABLE operations(id TEXT,result TEXT)")
        db.execute("INSERT INTO operations VALUES('unknown-action','unknown')")
    with closing(sqlite3.connect(native / "schedules.sqlite3")) as db, db:
        db.execute("CREATE TABLE schedules(account TEXT,group_id TEXT,status TEXT)")
        db.execute(
            "INSERT INTO schedules VALUES(?,?,'paused')",
            (module.ACCOUNT, module.GROUP),
        )
    with closing(sqlite3.connect(native / "cards.sqlite3")) as db, db:
        db.execute("CREATE TABLE card_seen(identity TEXT)")
        db.executemany(
            "INSERT INTO card_seen VALUES(?)",
            [
                (json.dumps([module.ACCOUNT, module.GROUP, "member", "nim"]),),
                (json.dumps([module.MANAGER, module.GROUP, "member", "nim"]),),
            ],
        )
    with closing(sqlite3.connect(native / "activities.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE invite_credits(account TEXT,group_id TEXT,amount INTEGER)"
        )
    monkeypatch.setattr(module, "offline_guard", Mock())
    return SimpleNamespace(
        root=root,
        native=native,
        output=tmp_path / "archive",
        reset=module.TestGroupReset(root),
    )


def rows(lab, name, table):
    """Read fixture data for exact preservation assertions.

    Args:
        lab: Fixture installation.
        name: Relative database path.
        table: Fixed test table name.

    Returns:
        Ordered row tuples.
    """
    with closing(sqlite3.connect(lab.root / name)) as db:
        return db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()


def test_non_root_operator_cannot_apply_reset(lab, monkeypatch):
    preview = lab.reset.preview()
    before = rows(lab, module.OPEN, "tenants")
    monkeypatch.setattr(module.os, "geteuid", lambda: 1001)
    with pytest.raises(module.ResetError, match="apply_requires_root_maintenance_operator"):
        lab.reset.apply(preview["confirmation_sha256"], lab.output)
    assert rows(lab, module.OPEN, "tenants") == before
    assert not lab.output.exists()


@pytest.mark.parametrize("field,value", [(4, 1001), (0, 0o40777)])
def test_unsafe_external_archive_parent_is_rejected(lab, monkeypatch, field, value):
    preview = lab.reset.preview()
    original_stat = Path.stat

    def unsafe_parent_stat(path, *, follow_symlinks=True):
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if path == lab.output.parent:
            fields = list(result)
            fields[field] = value
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", unsafe_parent_stat)
    with pytest.raises(
        module.ResetError, match="new_private_external_backup_directory_required"
    ):
        lab.reset.apply(preview["confirmation_sha256"], lab.output)
    assert not lab.output.exists()


def test_default_preview_is_read_only_and_not_genuine_first_activation(lab):
    before = {name: path.read_bytes() for name, path in lab.reset.paths.items()}
    preview = lab.reset.preview()
    assert preview["reset_existing_group"] is True
    assert preview["genuinely_new_group"] is False
    assert preview["departure_confirmed"] is True
    assert preview["instance"] == "wangshangliao_peer"
    assert not lab.output.exists()
    assert {name: path.read_bytes() for name, path in lab.reset.paths.items()} == before
    assert "test-content" not in json.dumps(preview)
    assert "test-token" not in json.dumps(preview)


def test_apply_backs_up_and_preserves_finance_audits_other_bot_and_raw_transport(lab):
    finance = rows(lab, module.FINANCE, "entries")
    audit = rows(lab, module.OPEN, "audit")
    retired = rows(lab, module.OPEN, "retired_groups")
    preview = lab.reset.preview()
    result = lab.reset.apply(preview["confirmation_sha256"], lab.output)
    assert result["status"] == "reset_complete"
    assert result["genuinely_new_group"] is False
    assert rows(lab, module.FINANCE, "entries") == finance
    assert rows(lab, module.OPEN, "audit") == audit
    assert rows(lab, module.OPEN, "retired_groups") == retired
    tenants = rows(lab, module.OPEN, "tenants")
    assert [(r[0], r[1]) for r in tenants] == [
        (module.MANAGER, module.GROUP),
        (module.ACCOUNT, "1516387"),
    ]
    assert rows(lab, module.OPEN, "capabilities") == [
        (module.MANAGER, module.GROUP, "owner-token"),
        (module.ACCOUNT, "1516387", "formal-token"),
    ]
    assert rows(lab, module.OPEN, "membership_notices") == [
        (module.ACCOUNT, "64031167010", "departed", "departure_verified"),
        (module.MANAGER, "64031167010", "owner", "verified"),
    ]
    inbox = rows(
        lab, str((lab.native / "messages.sqlite3").relative_to(lab.root)), "inbox"
    )
    assert [r[2] for r in inbox] == [
        "bare-transport-number-keep",
        "owner-content",
        module.GROUP,
    ]
    assert len(rows(lab, module.FRAMEWORK, "conversations")) == 3
    policy = json.loads(rows(lab, module.SETTINGS, "policies")[0][3])
    assert policy["enabled_groups"] == ["1516387"]
    assert policy["moderation"]["permissions"] == {"1516387": []}
    assert policy["moderation"]["mute_keywords"] == [module.GROUP]
    assert (lab.output / "receipt.json").stat().st_mode & 0o777 == 0o400
    assert (lab.output / "completion.json").stat().st_mode & 0o777 == 0o400
    for name, snapshot in result["snapshots"].items():
        with closing(sqlite3.connect(lab.output / snapshot["file"])) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            if name == module.OPEN:
                assert (
                    db.execute(
                        "SELECT count(*) FROM tenants WHERE account=? AND group_id=?",
                        (module.ACCOUNT, module.GROUP),
                    ).fetchone()[0]
                    == 1
                )
    with closing(sqlite3.connect(lab.root / module.OPEN)) as db:
        for sql in (
            "DELETE FROM test_reset_audit",
            "UPDATE test_reset_audit SET sha256='changed'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(sql)


@pytest.mark.parametrize("table", ["entries", "audit", "live_revisions"])
def test_any_financial_namespace_for_test_group_blocks_even_preview(lab, table):
    with closing(sqlite3.connect(lab.root / module.FINANCE)) as db, db:
        if table == "entries":
            db.execute(
                "INSERT INTO entries(id,group_id) VALUES('test',?)", (module.GROUP,)
            )
        else:
            db.execute(f"CREATE TABLE {table}(group_id TEXT)")
            db.execute(f"INSERT INTO {table} VALUES(?)", (module.GROUP,))
    with pytest.raises(module.ResetError, match="financial_data"):
        lab.reset.preview()


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            "UPDATE tenants SET team='999' WHERE account='43506527' "
            "AND group_id='1459282'",
            "identity_conflict",
        ),
        ("UPDATE actor_bindings SET conflicted=1", "identity_unverified"),
        ("INSERT INTO retired_groups VALUES('1459282')", "identity_conflict"),
        (
            "INSERT INTO ai_charges VALUES('43506527','1459282',1)",
            "model_cost_data",
        ),
    ],
)
def test_unverified_identity_retired_group_or_cost_data_refused(lab, mutation, error):
    with closing(sqlite3.connect(lab.root / module.OPEN)) as db, db:
        db.execute(mutation)
    with pytest.raises(module.ResetError, match=error):
        lab.reset.preview()


def test_stale_preview_and_missing_departure_refuse_without_mutations(lab):
    preview = lab.reset.preview()
    with closing(sqlite3.connect(lab.root / module.OPEN)) as db, db:
        db.execute(
            "UPDATE tenants SET version=version+1 WHERE account=? AND group_id=?",
            (module.ACCOUNT, module.GROUP),
        )
    with pytest.raises(module.ResetError, match="preview_changed"):
        lab.reset.apply(preview["confirmation_sha256"], lab.output)
    assert len(rows(lab, module.OPEN, "tenants")) == 3
    with closing(sqlite3.connect(lab.root / module.OPEN)) as db, db:
        db.execute(
            "UPDATE tenants SET present=1 WHERE account=? AND group_id=?",
            (module.ACCOUNT, module.GROUP),
        )
    preview = lab.reset.preview()
    with pytest.raises(module.ResetError, match="confirm_departure"):
        lab.reset.apply(preview["confirmation_sha256"], lab.output.parent / "second")
    assert len(rows(lab, module.OPEN, "tenants")) == 3


@pytest.mark.parametrize(
    "state", ["pending", "directory_verified", "membership_verified"]
)
def test_reset_preserves_exact_verified_generation_fences_only_in_target_scope(
    lab, state
):
    with closing(sqlite3.connect(lab.root / module.OPEN)) as db, db:
        db.executemany(
            "INSERT INTO membership_notices VALUES(?,?,?,?)",
            [
                (module.ACCOUNT, "64031167010", "working", state),
                (
                    module.ACCOUNT,
                    "64031167010",
                    "older-departure",
                    "departure_verified",
                ),
                (module.ACCOUNT, "another-team", "formal", state),
            ],
        )
    before = rows(lab, module.OPEN, "membership_notices")
    preview = lab.reset.preview()
    assert preview["preserved_departure_fences"] == 2
    lab.reset.apply(preview["confirmation_sha256"], lab.output)
    assert rows(lab, module.OPEN, "membership_notices") == [
        row for row in before if row[2] != "working"
    ]


def test_running_writer_or_trigger_changing_protected_rows_aborts(lab, monkeypatch):
    offline = module.offline_guard
    monkeypatch.setattr(
        module,
        "offline_guard",
        Mock(side_effect=module.ResetError("writer_running")),
    )
    with pytest.raises(module.ResetError, match="writer_running"):
        lab.reset.apply(lab.reset.preview()["confirmation_sha256"], lab.output)
    assert not lab.output.exists()
    monkeypatch.setattr(module, "offline_guard", offline)
    with closing(sqlite3.connect(lab.root / module.OPEN)) as db, db:
        db.execute(
            "CREATE TRIGGER unsafe_cleanup AFTER DELETE ON tenants "
            "BEGIN DELETE FROM capabilities; END"
        )
    preview = lab.reset.preview()
    before = rows(lab, module.OPEN, "capabilities")
    with pytest.raises(module.ResetError, match="integrity"):
        lab.reset.apply(preview["confirmation_sha256"], lab.output)
    assert rows(lab, module.OPEN, "capabilities") == before
    assert len(rows(lab, module.OPEN, "tenants")) == 3
    assert json.loads((lab.output / "completion.json").read_text())["committed"] == []


def test_symlinked_namespaces_and_shared_account_roots_rejected(lab):
    bindings = lab.root / "data/platform_data/wangshangliao/account-roots.json"
    bindings.write_text(
        json.dumps({module.ACCOUNT: "a" * 64, module.MANAGER: "a" * 64})
    )
    with pytest.raises(module.ResetError, match="namespace_not_unique"):
        module.TestGroupReset(lab.root)
    bindings.write_text(
        json.dumps({module.ACCOUNT: "a" * 64, module.MANAGER: "b" * 64})
    )
    path = lab.root / module.GRANTS
    renamed = path.with_name("other.sqlite3")
    path.rename(renamed)
    path.symlink_to(renamed)
    with pytest.raises(module.ResetError, match="symlink"):
        module.TestGroupReset(lab.root)


def test_concurrent_sqlite_writer_prevents_all_reset_mutations(lab):
    preview = lab.reset.preview()
    with closing(sqlite3.connect(lab.root / module.GRANTS)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            lab.reset.apply(preview["confirmation_sha256"], lab.output)
    assert len(rows(lab, module.OPEN, "tenants")) == 3
    assert len(rows(lab, module.GRANTS, "grants")) == 2
    assert not (lab.output / "receipt.json").exists()


def test_account_root_rebinding_after_construction_is_rejected(lab):
    path = lab.root / "data/platform_data/wangshangliao/account-roots.json"
    path.write_text(json.dumps({module.ACCOUNT: "b" * 64, module.MANAGER: "a" * 64}))
    with pytest.raises(module.ResetError):
        lab.reset.preview()
    assert len(rows(lab, module.OPEN, "tenants")) == 3


def test_audit_corruption_is_not_accepted_as_a_new_reset_chain(lab):
    with closing(sqlite3.connect(lab.root / module.OPEN)) as db, db:
        db.execute(
            "CREATE TABLE test_reset_audit("
            "id INTEGER PRIMARY KEY,previous TEXT,receipt TEXT,sha256 TEXT)"
        )
        db.execute("INSERT INTO test_reset_audit VALUES(1,'','changed','invalid')")
    with pytest.raises(module.ResetError, match="audit_chain_invalid"):
        lab.reset.preview()


def test_real_offline_guard_refuses_active_or_queued_services(tmp_path, monkeypatch):
    show = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout="ActiveState=active\nMainPID=123\nControlPID=0\nJob=\n",
        )
    )
    monkeypatch.setattr(module.subprocess, "run", show)
    with pytest.raises(module.ResetError, match="stop_service"):
        module.offline_guard(tmp_path, [])
    show.return_value.stdout = "ActiveState=inactive\nMainPID=0\nControlPID=0\nJob=42\n"
    with pytest.raises(module.ResetError, match="stop_service"):
        module.offline_guard(tmp_path, [])


@pytest.mark.parametrize(
    "command,blocked",
    [
        (b"/bin/bash\0-c\0from scripts.wsl_test_lab import TestLab\n\0", False),
        (b"/usr/bin/python3.12\0-m\0scripts.wsl_test_lab\0", True),
        (b"/opt/runtime/bin/python\0main.py\0", True),
        (
            b"/usr/bin/python3\0-m\0"
            b"astrbot.builtin_stars.wangshangliao_moderation.cashback_standalone\0",
            True,
        ),
    ],
)
def test_offline_guard_distinguishes_shell_source_from_python_runner(
    tmp_path, monkeypatch, command, blocked
):
    """Inspect actual process entrypoints, not script words embedded in a shell."""
    process = tmp_path / str(os.getpid() + 100000)
    process.mkdir()
    (process / "fd").mkdir()
    (process / "cmdline").write_bytes(command)
    (process / "cwd").symlink_to(tmp_path, target_is_directory=True)
    original = Path.iterdir
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda path: iter([process]) if path == Path("/proc") else original(path),
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="ActiveState=inactive\nMainPID=0\nControlPID=0\nJob=0\n",
        ),
    )
    if blocked:
        with pytest.raises(module.ResetError, match="test_runner_still_running"):
            module.offline_guard(tmp_path, [])
    else:
        module.offline_guard(tmp_path, [])


def test_cli_rejects_unknown_version_and_unconfirmed_apply(monkeypatch):
    for args in (
        ["reset_wsl_test_group", "--format-version", "2"],
        ["reset_wsl_test_group", "--apply"],
        ["reset_wsl_test_group", "--confirmation-sha256", "f" * 64],
    ):
        monkeypatch.setattr("sys.argv", args)
        with pytest.raises(SystemExit) as failure:
            module.main()
        assert failure.value.code == 2
