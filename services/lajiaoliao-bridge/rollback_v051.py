"""Convert only stopped Lajiao business settings for the protected v0.5.1 code backup."""

import argparse
import copy
import json
import os
import sqlite3
import time
from pathlib import Path


def compatible_settings(value, defaults, grants, validator):
    result = {
        key: copy.deepcopy(value.get(key, fallback))
        for key, fallback in defaults.items()
    }
    result["automation"] = {
        key: copy.deepcopy(value.get("automation", {}).get(key, fallback))
        for key, fallback in defaults["automation"].items()
    }
    result["lottery"] = {
        key: copy.deepcopy(value.get("lottery", {}).get(key, fallback))
        for key, fallback in defaults["lottery"].items()
    }
    result["lottery"]["min_invitations"] = 0
    result["native_actions"] = [
        grant for grant in result["native_actions"] if grant in grants
    ]
    # Never re-enable unknown or newly scheduled effects under the old runtime.
    result["automation"]["enabled"] = False
    result["lottery_enabled"] = False
    result["reminders_enabled"] = False
    result["invitations_enabled"] = False
    return validator(result)


def convert_database(path, defaults, grants, validator):
    """Preserve original JSON and all effect tables; transform only group settings."""
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """CREATE TABLE IF NOT EXISTS rollback_settings_v060(
               group_id TEXT,version INTEGER,stamp REAL,value TEXT,
               PRIMARY KEY(group_id,version))"""
        )
        rows = db.execute("SELECT group_id,version,value FROM settings").fetchall()
        for group, version, raw in rows:
            value = compatible_settings(json.loads(raw), defaults, grants, validator)
            db.execute(
                "INSERT OR IGNORE INTO rollback_settings_v060 VALUES(?,?,?,?)",
                (group, version, time.time(), raw),
            )
            db.execute(
                "UPDATE settings SET version=?,value=? WHERE group_id=?",
                (version + 1, json.dumps(value), group),
            )
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "scheduled_jobs" in tables:
            db.execute("UPDATE scheduled_jobs SET state='paused' WHERE state='pending'")
            db.execute(
                "UPDATE scheduled_jobs SET state='unknown' WHERE state='running'"
            )
        if "activities" in tables:
            db.execute("UPDATE activities SET state='cancelled' WHERE state='open'")
            db.execute(
                "UPDATE activities SET state='unknown' WHERE state IN ('opening','drawing','publishing')"
            )
        if "reminders" in tables:
            db.execute("UPDATE reminders SET state='paused' WHERE state='active'")
        return len(rows)


def main():
    from acceptance_client import AcceptanceClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    backup = args.backup.resolve()
    for path in (backup, *(backup / "plugin").rglob("*")):
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise RuntimeError("private_code_backup_required")
    metadata = (backup / "plugin/metadata.yaml").read_text()
    if "v0.5.1" not in metadata or not (backup / "manifest.json").is_file():
        raise RuntimeError("protected_v051_backup_required")
    root = Path(__file__).resolve().parents[2]
    if (root / "data/plugin_data/astrbot_plugin_lajiaoliao/commerce.sqlite3").exists():
        from commerce_rollback_guard import require_safe_commerce_rollback

        require_safe_commerce_rollback(root, backup / "plugin")
    client = AcceptanceClient(test_bindings=False)
    config = json.loads(client.original_config.decode("utf-8-sig"))
    if any(
        row.get("type") == "lajiaoliao" and row.get("enable")
        for row in config["platform"]
    ):
        raise RuntimeError("disable_lajiaoliao_instances_first")
    plugins = client.api("/api/v1/plugins")
    if any(
        row.get("name") == "astrbot_plugin_lajiaoliao" and row.get("activated")
        for row in plugins
    ):
        raise RuntimeError("deactivate_lajiaoliao_plugin_first")
    binary = str(
        Path(__file__).resolve().parents[2]
        / "data/plugins/astrbot_plugin_lajiaoliao/bin/lajiaoliao-bridge"
    )
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if os.readlink(process / "exe").startswith(binary):
                raise RuntimeError("lajiaoliao_worker_still_running")
        except (FileNotFoundError, PermissionError):
            continue
    if not args.execute:
        print("Rollback preconditions verified; no data changed.")
        return
    import data.plugins.astrbot_plugin_lajiaoliao as package

    package.__path__ = [str(backup / "plugin")]
    from data.plugins.astrbot_plugin_lajiaoliao.business import (
        DEFAULTS,
        NATIVE_ACTIONS,
        validate_settings,
    )

    changed = 0
    seen = set()
    for bot in config["platform"]:
        if bot.get("type") != "lajiaoliao" or bot.get("session_ref") in seen:
            continue
        ref = str(bot["session_ref"])
        if len(ref) != 32 or any(char not in "0123456789abcdef" for char in ref):
            raise RuntimeError("invalid_session_ref")
        seen.add(ref)
        path = (
            Path(__file__).resolve().parents[2]
            / "data/plugin_data/astrbot_plugin_lajiaoliao"
            / ref
            / "business.sqlite3"
        )
        if path.is_file():
            snapshot = path.with_name(
                "business-before-v051-" + str(time.time_ns()) + ".sqlite3"
            )
            with sqlite3.connect(path) as old, sqlite3.connect(snapshot) as saved:
                old.backup(saved)
            snapshot.chmod(0o600)
            changed += convert_database(
                path, DEFAULTS, NATIVE_ACTIONS, validate_settings
            )
    print(
        "Converted "
        + str(changed)
        + " group settings; credentials, cursors and unknown receipts retained. Restore protected code before reactivating."
    )


if __name__ == "__main__":
    main()
