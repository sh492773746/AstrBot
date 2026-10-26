"""Read-only AI rollout snapshots; never submits a message or modifies a profile."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from acceptance_client import GROUP, INSTANCES, PREFIX, ROOT, AcceptanceClient
from reload_release import consumers, main_pid, metadata_snapshot, verify_bundle

from data.plugins.astrbot_plugin_lajiaoliao.ai import (
    AI_DEFAULTS,
    CHAT_PERSONA,
    MANAGEMENT_PERSONA,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def configuration_fingerprint():
    paths = [
        ROOT / "data/cmd_config.json",
        *sorted((ROOT / "data/config").glob("*.json")),
    ]
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def snapshot(client):
    config = json.loads(client.original_config.decode("utf-8-sig"))
    return {
        "stamp": time.time(),
        "main_pid": main_pid(),
        "configuration": configuration_fingerprint(),
        "metadata": metadata_snapshot(config),
        "consumers": len(consumers(main_pid())),
        "binary": verify_bundle(),
        "platforms": [
            {key: row.get(key) for key in ("id", "type", "status", "connection_state")}
            for row in client.api("/api/v1/bots/stats")["platforms"]
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("snapshot", "verify"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--before", type=Path)
    args = parser.parse_args()
    client = AcceptanceClient()
    report = snapshot(client)
    if args.operation == "verify":
        if not args.before:
            parser.error("--before is required for verification")
        before = json.loads(args.before.read_text())
        if report["configuration"] != before["configuration"]:
            raise RuntimeError("configuration_changed_review_required")
        if report["metadata"] != before["metadata"]:
            raise RuntimeError("binding_or_business_settings_changed")
        if report["consumers"] != 2:
            raise RuntimeError("unexpected_consumer_count")
        current_platforms = {row["id"]: row for row in report["platforms"]}
        for previous in before["platforms"]:
            current = current_platforms.get(previous["id"])
            if (
                current is None
                or current["status"] != previous["status"]
                or (
                    previous["connection_state"] == "online"
                    and current["connection_state"] != "online"
                )
            ):
                raise RuntimeError("platform_recovery_incomplete")
        instances = client.api(PREFIX + "management")["instances"]
        if not all(
            next(row for row in instances if row["id"] == identifier)[
                "connection_state"
            ]
            == "online"
            for identifier in INSTANCES
        ):
            raise RuntimeError("test_instances_not_online")
        report["ai_instances"] = []
        for bot in client.bots:
            body = {"session_ref": bot["session_ref"], "group_id": GROUP}
            settings = client.api(PREFIX + "ai-config", body)
            usage = client.api(PREFIX + "ai-usage", body)
            if settings["settings"] != AI_DEFAULTS:
                raise RuntimeError("unexpected_initial_ai_settings")
            if settings["group"]["settings"] != {"daily_limit": 500, "notify_uid": ""}:
                raise RuntimeError("unexpected_initial_group_ai_settings")
            persona_ids = {p["id"] for p in settings["personas"]}
            if not {CHAT_PERSONA, MANAGEMENT_PERSONA} <= persona_ids:
                raise RuntimeError("required_persona_missing")
            report["ai_instances"].append(
                {
                    "instance_id": bot["id"],
                    "user_limit": 20,
                    "management_limit": 100,
                    "group_limit": 500,
                    "recipients_unassigned": True,
                    "group_used": usage["group_used"],
                    "concurrency": settings["concurrency"],
                }
            )
        report["main_pid_changed"] = report["main_pid"] != before["main_pid"]
        report["passed"] = True
        report["real_messages_sent"] = 0
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(args.output, report)
    print("Read-only AI rollout " + args.operation + " passed.")


if __name__ == "__main__":
    main()
