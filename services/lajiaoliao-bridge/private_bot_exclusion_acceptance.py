"""Verify private bot AI exclusion with one spare quota slot, restoring limits."""

import argparse
import copy
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from acceptance_client import PREFIX, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise RuntimeError("existing_report_requires_review_not_resend")
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = AcceptanceClient()
    scopes = [{"session_ref": bot["session_ref"]} for bot in client.bots]
    original = [client.api(PREFIX + "ai-config", scope) for scope in scopes]
    before = [client.api(PREFIX + "ai-usage", scope) for scope in scopes]
    report = {"executed": False, "passed": False, "probes": []}
    atomic_json(args.output, report)
    if not args.execute:
        print("Read-only private exclusion preflight prepared.")
        return
    settings, windows = {}, {}

    def save(index, value):
        current = client.api(PREFIX + "ai-config", scopes[index])
        return client.api(
            PREFIX + "ai-config",
            {
                **scopes[index],
                "action": "save",
                "confirm": True,
                "version": current["version"],
                "instance_revision": current["instance_revision"],
                "settings": value,
            },
        )

    try:
        for index, scope in enumerate(scopes):
            status = client.api(PREFIX + "test-window", {**scope, "action": "status"})
            if status["open"]:
                raise RuntimeError("test_window_in_use")
            uid = client.sessions[1 - index]["account_id"]
            used = next(
                (
                    row["used"]
                    for row in before[index]["usage"]
                    if row["pool"] == "user" and row["subject"] == uid
                ),
                0,
            )
            value = copy.deepcopy(original[index]["settings"])
            value["user_daily_limit"] = max(value["user_daily_limit"], used + 1)
            save(index, value)
            settings[index] = value
        for index, bot in enumerate(client.bots):
            peer = 1 - index
            uid = client.sessions[peer]["account_id"]
            window = client.api(
                PREFIX + "test-window",
                {
                    "session_ref": bot["session_ref"],
                    "action": "open",
                    "confirm": True,
                    "account_id": client.sessions[index]["account_id"],
                    "targets": [{"kind": "private", "target_id": uid}],
                    "duration": 180,
                    "budget": 1,
                },
            )
            windows[index] = window["window_ref"]
            probe = {
                "sender": bot["id"],
                "operation_id": "private-guard-" + secrets.token_hex(8),
                "status": "unknown",
            }
            report["probes"].append(probe)
            report["executed"] = True
            atomic_json(args.output, report)
            result = client.api(
                PREFIX + "test-send",
                {
                    "session_ref": bot["session_ref"],
                    "window_ref": windows[index],
                    "operation_id": probe["operation_id"],
                    "kind": "private",
                    "target_id": uid,
                    "text": "机器人私聊互斥验收 "
                    + secrets.token_hex(4)
                    + "，请只回复早上好。",
                },
            )
            probe.update(
                status=result.get("status"), message_id=result.get("message_id")
            )
            atomic_json(args.output, report)
            if probe["status"] != "sent":
                raise RuntimeError("private_probe_unknown_do_not_resend")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if any(
                    row["message_id"] == probe["message_id"]
                    for row in client.inspect(
                        peer, "private", client.sessions[index]["account_id"]
                    )
                ):
                    probe["wire_received"] = True
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("private_probe_not_received")
        time.sleep(15)
        after = [client.api(PREFIX + "ai-usage", scope) for scope in scopes]
        report["model_calls_before"] = [row["metrics"]["model_calls"] for row in before]
        report["model_calls_after"] = [row["metrics"]["model_calls"] for row in after]
        report["usage_unchanged"] = all(
            {
                (row["pool"], row["subject"]): row["used"]
                for row in before[index]["usage"]
            }
            == {
                (row["pool"], row["subject"]): row["used"]
                for row in after[index]["usage"]
            }
            for index in range(2)
        )
        report["no_model_calls"] = (
            report["model_calls_before"] == report["model_calls_after"]
        )
        report["passed"] = report["usage_unchanged"] and report["no_model_calls"]
        if not report["passed"]:
            raise RuntimeError("private_bound_bot_triggered_ai")
    finally:
        for index, value in settings.items():
            current = client.api(PREFIX + "ai-config", scopes[index])
            if current["settings"] != value:
                report.setdefault("cleanup_errors", []).append(
                    "ai_settings_changed_externally"
                )
            else:
                save(index, original[index]["settings"])
        for index in windows:
            client.api(PREFIX + "test-window", {**scopes[index], "action": "close"})
        report["limits_restored"] = all(
            client.api(PREFIX + "ai-config", scope)["settings"]
            == original[index]["settings"]
            for index, scope in enumerate(scopes)
        )
        report["configuration_unchanged"] = (
            hashlib.sha256(client.original_config).digest()
            == hashlib.sha256(
                (
                    Path(__file__).resolve().parents[2] / "data/cmd_config.json"
                ).read_bytes()
            ).digest()
        )
        if (
            report.get("cleanup_errors")
            or not report["limits_restored"]
            or not report["configuration_unchanged"]
        ):
            report["passed"] = False
        atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
