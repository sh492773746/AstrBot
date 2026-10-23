"""Two opt-in group probes: quoted bot messages must ACK without model handoff."""

import argparse
import json
import os
import secrets
import time
from pathlib import Path

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = args.output / "bot-exclusion.json"
    if path.exists():
        raise RuntimeError("existing_report_requires_review_not_resend")
    report = {"executed": False, "passed": False, "group_id": GROUP, "probes": []}
    atomic_json(path, report)
    if not args.execute:
        print(json.dumps(report))
        return
    client = AcceptanceClient()
    windows = {}
    states = client.api(PREFIX + "management")["instances"]
    for bot in client.bots:
        state = next(row for row in states if row["id"] == bot["id"])
        if (
            not state["enabled"]
            or state["connection_state"] != "online"
            or not state.get("bot_group_mutual_exclusion")
            or GROUP not in state["message_policy"]["group_ids"]
        ):
            raise RuntimeError("test_instances_or_bot_exclusion_not_ready")
    try:
        for index, bot in enumerate(client.bots):
            peer_index = 1 - index
            original = next(
                row
                for row in reversed(client.inspect(index, "group", GROUP))
                if row.get("sender_id") == client.sessions[peer_index]["account_id"]
                and row.get("content_type") in {101, 114}
            )
            window = client.api(
                PREFIX + "test-window",
                {
                    "session_ref": bot["session_ref"],
                    "action": "open",
                    "confirm": True,
                    "account_id": client.sessions[index]["account_id"],
                    "targets": [{"kind": "group", "target_id": GROUP}],
                    "duration": 180,
                    "budget": 1,
                },
            )
            windows[index] = window["window_ref"]
            probe = {
                "sender_instance": bot["id"],
                "receiver_instance": client.bots[peer_index]["id"],
                "operation_id": "bot-guard-" + secrets.token_hex(8),
                "status": "unknown",
            }
            report["executed"] = True
            report["probes"].append(probe)
            atomic_json(path, report)
            receipt = client.api(
                PREFIX + "test-send",
                {
                    "session_ref": bot["session_ref"],
                    "window_ref": windows[index],
                    "operation_id": probe["operation_id"],
                    "kind": "group",
                    "target_id": GROUP,
                    "reply_to": original["message_id"],
                    "text": "/机器人互斥验收 " + secrets.token_hex(4),
                },
            )
            probe.update(
                status=receipt.get("status", "unknown"),
                message_id=receipt.get("message_id", ""),
            )
            atomic_json(path, report)
            if probe["status"] != "sent" or not probe["message_id"]:
                raise RuntimeError("probe_send_not_confirmed_no_retry")
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                state = next(
                    row
                    for row in client.api(PREFIX + "management")["instances"]
                    if row["id"] == client.bots[peer_index]["id"]
                )
                ignored = any(
                    row["message_id"] == probe["message_id"]
                    and row["status"] == "ignored_bound_bot"
                    for row in state.get("ignored_bot_messages", [])
                )
                received = next(
                    (
                        row
                        for row in client.inspect(peer_index, "group", GROUP)
                        if row.get("message_id") == probe["message_id"]
                    ),
                    None,
                )
                if ignored and received:
                    if (
                        received.get("content_type") != 114
                        or received.get("quoted_message_id") != original["message_id"]
                    ):
                        raise RuntimeError("quote_probe_not_verified")
                    probe.update(quote_verified=True, dropped_before_pipeline=True)
                    break
                time.sleep(1)
            else:
                raise RuntimeError("bot_message_not_mutually_excluded")
            atomic_json(path, report)
        # A strong quote/prefix would wake a model without the ingress guard.
        time.sleep(15)
        ids = {item["message_id"] for item in report["probes"]}
        replies = [
            row
            for row in client.inspect(0, "group", GROUP)
            if row.get("message_id") not in ids and row.get("quoted_message_id") in ids
        ]
        report["unexpected_quote_replies"] = len(replies)
        report["passed"] = not replies
    finally:
        for index in windows:
            client.api(
                PREFIX + "test-window",
                {
                    "session_ref": client.bots[index]["session_ref"],
                    "action": "close",
                },
            )
        report["windows_closed"] = len(windows)
        report["configuration_unchanged"] = (
            client.original_config == (ROOT / "data/cmd_config.json").read_bytes()
        )
        report["passed"] = report["passed"] and report["configuration_unchanged"]
        atomic_json(path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
