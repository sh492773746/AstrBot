"""Validate and reload only LajiaoLiao; never restore credential snapshots."""

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

from acceptance_client import PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

PLUGIN = ROOT / "data/plugins/astrbot_plugin_lajiaoliao"
DATA = ROOT / "data/plugin_data/astrbot_plugin_lajiaoliao"


def main_pid():
    return int(
        subprocess.check_output(
            ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
            text=True,
        ).strip()
    )


def consumers(parent):
    workers = []
    for process in Path("/proc").glob("[0-9]*"):
        try:
            executable = os.readlink(process / "exe").removesuffix(" (deleted)")
            if executable != str(PLUGIN / "bin/lajiaoliao-bridge"):
                continue
            fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) != parent:
                raise RuntimeError("orphan_lajiaoliao_worker")
            workers.append(int(process.name))
        except FileNotFoundError:
            continue
    return workers


def metadata_snapshot(config, *, check_work=True):
    snapshot = {}
    now = time.time()
    if check_work:
        for path in (
            *DATA.glob("*/session.json"),
            *(DATA / ".reauth").glob("*/session.json"),
        ):
            row = json.loads(path.read_text())
            if row.get("state") == "committing" or (
                row.get("state") in {"draft", "ready"}
                and row.get("expires_at", 0) > now
            ):
                raise RuntimeError("authorization_in_progress_requires_later_reload")
    for bot in config["platform"]:
        if bot.get("type") != "lajiaoliao":
            continue
        ref = bot["session_ref"]
        if (
            not isinstance(ref, str)
            or len(ref) != 32
            or any(c not in "0123456789abcdef" for c in ref)
        ):
            raise RuntimeError("invalid_session_reference")
        directory = DATA / ref
        session = json.loads((directory / "session.json").read_text())
        if session["state"] != "bound" or session["instance_id"] != bot["id"]:
            raise RuntimeError("binding_changed")
        database = directory / "business.sqlite3"
        settings = []
        if database.exists():
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                settings = db.execute(
                    "SELECT group_id,version,value FROM settings ORDER BY group_id"
                ).fetchall()
                if (
                    check_work
                    and db.execute(
                        "SELECT 1 FROM activities WHERE state IN ('opening','open','publishing') LIMIT 1"
                    ).fetchone()
                ):
                    raise RuntimeError("active_lottery_requires_later_reload")
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if (
                    check_work
                    and "scheduled_jobs" in tables
                    and db.execute(
                        "SELECT 1 FROM scheduled_jobs WHERE state='running' OR "
                        "(state='pending' AND due BETWEEN ? AND ?) LIMIT 1",
                        (now - 30, now + 90),
                    ).fetchone()
                ):
                    raise RuntimeError(
                        "nearby_scheduled_boundary_requires_later_reload"
                    )
        snapshot[bot["id"]] = hashlib.sha256(
            json.dumps(
                {
                    "binding": [
                        session["owner"],
                        session["account_id"],
                        session["instance_id"],
                        session["state"],
                    ],
                    "settings": settings,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
    return snapshot


def verify_bundle():
    from astrbot import __version__

    info = json.loads((PLUGIN / "build-info.json").read_text())
    dependencies_raw = (PLUGIN / "runtime-dependencies.json").read_bytes()
    dependencies = json.loads(dependencies_raw)
    if (
        __version__ != "4.28.1"
        or dependencies["astrbot"] != __version__
        or dependencies["python"]
        != f"{sys.version_info.major}.{sys.version_info.minor}"
        or info["arch"] != platform.machine()
        or info.get("runtime_dependencies_sha256")
        != hashlib.sha256(dependencies_raw).hexdigest()
    ):
        raise RuntimeError("unsupported_runtime_build")
    if any(
        version(name) != expected
        for name, expected in dependencies["distribution_versions"].items()
    ):
        raise RuntimeError("locked_runtime_dependency_mismatch")
    expected = (PLUGIN / "bin/lajiaoliao-bridge.sha256").read_text().strip()
    actual = hashlib.sha256((PLUGIN / "bin/lajiaoliao-bridge").read_bytes()).hexdigest()
    if info["protocol_version"] != 2 or expected != actual or info["sha256"] != actual:
        raise RuntimeError("protocol_binary_checksum_mismatch")
    compatibility = json.loads(
        (Path(__file__).parent / "releases/core-compatibility.json").read_text()
    )
    if compatibility["astrbot_version"] != "4.28.1":
        raise RuntimeError("unsupported_astrbot_build")
    for name, digest in compatibility["core_files"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("core_extension_changed")
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = AcceptanceClient(test_bindings=False)
    config = json.loads(client.original_config.decode("utf-8-sig"))
    expected = {
        bot["id"]
        for bot in config["platform"]
        if bot.get("type") == "lajiaoliao" and bot.get("enable")
    }
    parent = main_pid()
    before = metadata_snapshot(config)
    report = {
        "completed": False,
        "executed": False,
        "binary_sha256": verify_bundle(),
        "enabled_instances": len(expected),
        "before_consumers": len(consumers(parent)),
        "credentials_or_cursors_restored": False,
    }
    atomic_json(args.output, report)
    if not args.execute:
        print("Release preflight passed; no reload or configuration change.")
        return
    started = time.monotonic()
    try:
        if (ROOT / "data/cmd_config.json").read_bytes() != client.original_config:
            raise RuntimeError("configuration_changed")
        report["executed"] = True
        atomic_json(args.output, report)
        client.api("/api/v1/plugins/astrbot_plugin_lajiaoliao/reload", {})
        while time.monotonic() - started < 60:
            rows = client.api(PREFIX + "management")["instances"]
            actual = {
                row["id"]
                for row in rows
                if row["enabled"] and row["connection_state"] == "online"
            }
            if expected <= actual and len(consumers(parent)) == len(expected):
                break
            time.sleep(1)
        else:
            raise RuntimeError("release_recovery_incomplete")
        if (
            main_pid() != parent
            or (ROOT / "data/cmd_config.json").read_bytes() != client.original_config
        ):
            raise RuntimeError("main_process_or_configuration_changed")
        if metadata_snapshot(config, check_work=False) != before:
            raise RuntimeError("binding_or_business_settings_changed")
        report.update(
            completed=True,
            recovery_seconds=round(time.monotonic() - started, 3),
            main_pid_unchanged=True,
            configuration_unchanged=True,
            binding_and_settings_unchanged=True,
            final_consumers=len(consumers(parent)),
        )
    except Exception as error:
        report["error"] = type(error).__name__
        raise
    finally:
        atomic_json(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
