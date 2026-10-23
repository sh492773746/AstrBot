"""Offline session cutover. AstrBot and both consumers must be stopped first."""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from data.plugins.astrbot_plugin_lajiaoliao.migration import (
    prepare_bundle,
    rollback_bundle,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore, atomic_json


def stopped(unit):
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=ActiveState", "--value"],
        capture_output=True, text=True, check=False,
    )
    if result.stdout.strip() not in {"inactive", "failed", ""}:
        raise RuntimeError(f"{unit} must be stopped before cutover")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["activate", "rollback-data"])
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--instance", default="lajiaoliao_test_20261007")
    args = parser.parse_args()
    stopped("astrbot.service")
    stopped("lajiaoliao-bridge-login.service")
    root = Path(__file__).resolve().parents[2]
    config_path = root / "data/cmd_config.json"
    old_raw = config_path.read_bytes()
    config = json.loads(old_raw.decode("utf-8-sig"))
    store = SessionStore(root / "data/plugin_data/astrbot_plugin_lajiaoliao")
    legacy = Path("/var/lib/lajiaoliao-bridge")
    def read(path):
        return json.loads(path.read_text(encoding="utf-8-sig"))
    if args.operation == "activate":
        if (store.root / "activated.json").exists():
            raise RuntimeError("Plugin already activated")
        token = read(legacy / "token.json")
        cursor_path = legacy / "cursor.json"
        cursor = read(cursor_path) if cursor_path.exists() else None
        manifest = prepare_bundle(
            config, token, cursor,
            owner=str(config.get("dashboard", {}).get("username") or ""),
            instance_id=args.instance, output=args.backup,
            defer_binding=True,
        )
        ref = manifest["session_ref"]
        shutil.copytree(args.backup / "sessions" / ref, store.directory(ref))
        replacement = read(args.backup / "bot.after.json")
        for index, bot in enumerate(config["platform"]):
            if bot.get("id") == args.instance:
                config["platform"][index] = replacement
        if config_path.read_bytes() != old_raw:
            raise RuntimeError("Configuration changed during cutover; refusing overwrite")
        atomic_json(config_path, config)
        atomic_json(store.root / "activated.json", {
            "version": 1, "backup": str(args.backup.resolve()),
        })
        print("Plugin activated; imported session requires explicit binding; bot remains disabled.")
    else:
        if (store.root / "commerce.sqlite3").exists():
            from commerce_rollback_guard import require_safe_commerce_rollback

            require_safe_commerce_rollback(root, args.backup / "plugin")
        manifest = read(args.backup / "manifest.json")
        ref = manifest["session_ref"]
        before = read(args.backup / "bot.before.json")
        row = store._read(ref)
        # Unbound imports have no worker and are eligible for rollback too.
        if row["state"] == "authorized":
            store.bind(ref, row["owner"], before["id"])
        exported = args.backup / "rollback-current"
        rollback_bundle(store, ref, before, output=exported)
        legacy.mkdir(parents=True, exist_ok=True, mode=0o700)
        ownership = legacy.stat()
        for name in ("token.json", "cursor.json"):
            if (exported / name).exists():
                atomic_json(legacy / name, read(exported / name))
                os.chown(legacy / name, ownership.st_uid, ownership.st_gid)
            elif name == "cursor.json":
                (legacy / name).unlink(missing_ok=True)
        for index, bot in enumerate(config["platform"]):
            if bot.get("id") == manifest["instance_id"]:
                config["platform"][index] = read(exported / "bot.restore.json")
        if config_path.read_bytes() != old_raw:
            raise RuntimeError("Configuration changed during rollback; refusing overwrite")
        atomic_json(config_path, config)
        (store.root / "activated.json").unlink(missing_ok=True)
        print("Current session restored with bot disabled. Restore matching code/UI before starting legacy services.")


if __name__ == "__main__":
    main()
