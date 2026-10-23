"""Acceptance scopes do not bypass historical migration comparisons."""

import json
import sqlite3
from contextlib import closing

import pytest

from scripts import check_wsl_deployment as checks


@pytest.fixture
def migration(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    root = tmp_path / "data" / "plugin_data" / "wangshangliao_moderation"
    root.mkdir(parents=True)
    backup = tmp_path / "backup"
    backup.mkdir()
    transport = {"id": "fixture", "type": "wangshangliao", "account_id": "1"}
    policy = {"enabled_groups": ["5"], "reply_private": True}
    (backup / "cmd_config.json").write_text(
        json.dumps({"platform": [{**transport, **policy}]}), encoding="utf-8"
    )
    with closing(sqlite3.connect(root / "settings.sqlite3")) as db, db:
        db.execute("CREATE TABLE policies(instance,account,payload)")
        db.execute(
            "INSERT INTO policies VALUES(?,?,?)",
            ("fixture", "1", json.dumps(policy)),
        )
    for path in (backup / "history.sqlite3", tmp_path / "data" / "history.sqlite3"):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE invite_seen(account,group_id,member,first_seen,rate)"
            )
            db.execute("INSERT INTO invite_seen VALUES('1','5','2',1000,18)")
    return {"platform": [transport]}, backup, root


def test_migration_preserves_policy_and_historical_comparison(migration):
    config, backup, _ = migration
    checks.verify_migration(config, backup)


@pytest.mark.parametrize("change", ["none", "account", "policy", "missing"])
def test_post_migration_snapshot_checks_persisted_policy(migration, change):
    config, backup, root = migration
    (backup / "cmd_config.json").write_text(json.dumps(config), encoding="utf-8")
    snapshot_root = backup / "plugin_data" / "wangshangliao_moderation"
    snapshot_root.mkdir(parents=True)
    with closing(sqlite3.connect(root / "settings.sqlite3")) as live:
        with closing(sqlite3.connect(snapshot_root / "settings.sqlite3")) as old:
            live.backup(old)
    with closing(sqlite3.connect(root / "settings.sqlite3")) as db, db:
        if change == "account":
            db.execute("UPDATE policies SET account='another'")
        elif change == "policy":
            db.execute("UPDATE policies SET payload='{}'")
        elif change == "missing":
            db.execute("DELETE FROM policies")
        else:
            # Formatting is not a policy change.
            payload = db.execute("SELECT payload FROM policies").fetchone()[0]
            db.execute(
                "UPDATE policies SET payload=?",
                (json.dumps(json.loads(payload), indent=4, sort_keys=True),),
            )
    if change == "none":
        checks.verify_migration(config, backup)
    else:
        with pytest.raises(AssertionError):
            checks.verify_migration(config, backup)


@pytest.mark.parametrize(
    "change", ["account", "policy", "transport", "ledger", "missing"]
)
def test_migration_drift_is_not_silently_accepted(migration, change):
    config, backup, root = migration
    if change in {"account", "policy"}:
        with closing(sqlite3.connect(root / "settings.sqlite3")) as db, db:
            if change == "account":
                db.execute("UPDATE policies SET account='another'")
            else:
                db.execute("UPDATE policies SET payload='{}'")
    elif change == "transport":
        config["platform"][0]["account_id"] = "another"
    else:
        live = checks.ROOT / "data" / "history.sqlite3"
        if change == "missing":
            live.unlink()
        else:
            with closing(sqlite3.connect(live)) as db, db:
                db.execute("UPDATE invite_seen SET rate=0")
    with pytest.raises(AssertionError):
        checks.verify_migration(config, backup)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--runtime-only", "--backup", "/not-a-backup"],
        ["--runtime-only", "--hot-save"],
    ],
)
async def test_invalid_acceptance_scope_fails_before_loading_credentials(
    tmp_path, monkeypatch, arguments
):
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["check_wsl_deployment", *arguments])
    with pytest.raises(SystemExit) as error:
        await checks.main()
    assert error.value.code == 2
