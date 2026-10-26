"""Back up the two designated ledgers and verify a default-off plugin reload."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.business import validate_settings
from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    client = AcceptanceClient()
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = [
        client.api(
            PREFIX + "business", {"session_ref": bot["session_ref"], "group_id": GROUP}
        )
        for bot in client.bots
    ]
    if any(row["settings"].get("automation", {}).get("enabled") for row in before):
        raise RuntimeError("existing_automatic_rules_in_use")
    original_pid = subprocess.check_output(
        ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
        text=True,
    ).strip()
    report = {
        "mode": "read_only_default_off",
        "before_settings": [row["settings"] for row in before],
    }
    atomic_json(args.output, report)
    if args.reload:
        backup = args.output.parent / "business-before"
        backup.mkdir(mode=0o700)
        for bot in client.bots:
            source = (
                ROOT
                / "data/plugin_data/astrbot_plugin_lajiaoliao"
                / bot["session_ref"]
                / "business.sqlite3"
            )
            target = backup / (bot["id"] + ".sqlite3")
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
            copied = sqlite3.connect(target)
            try:
                original.backup(copied)
            finally:
                copied.close()
                original.close()
        client.api("/api/v1/plugins/astrbot_plugin_lajiaoliao/reload", {})
    deadline = time.monotonic() + 45
    while True:
        state = client.api(PREFIX + "management")["instances"]
        selected = [
            row for row in state if row["id"] in {bot["id"] for bot in client.bots}
        ]
        if len(selected) == 2 and all(
            row["connection_state"] == "online" for row in selected
        ):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("test_instances_not_recovered")
        time.sleep(1)
    after = [
        client.api(
            PREFIX + "business", {"session_ref": bot["session_ref"], "group_id": GROUP}
        )
        for bot in client.bots
    ]
    report.update(
        both_online=True,
        reloaded=args.reload,
        settings_preserved=all(
            new["settings"] == validate_settings(old["settings"])
            and new["version"] == old["version"]
            for old, new in zip(before, after, strict=True)
        ),
        default_off=all(
            not row["settings"]["automation"]["enabled"]
            and not row["settings"]["ai_management_enabled"]
            for row in after
        ),
        no_automatic_actions=all(not row["automatic"] for row in after),
        global_config_unchanged=hashlib.sha256(
            (ROOT / "data/cmd_config.json").read_bytes()
        ).digest()
        == hashlib.sha256(client.original_config).digest(),
        main_pid_unchanged=original_pid
        == subprocess.check_output(
            ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
            text=True,
        ).strip(),
    )
    report["passed"] = all(
        report[key]
        for key in (
            "both_online",
            "settings_preserved",
            "default_off",
            "no_automatic_actions",
            "global_config_unchanged",
            "main_pid_unchanged",
        )
    )
    atomic_json(args.output, report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "before_settings"},
            indent=2,
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
