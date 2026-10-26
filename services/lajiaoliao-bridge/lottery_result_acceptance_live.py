"""Opt-in quote/result/GIF display test; no enrollment or winner ledger changes."""

import argparse
import base64
import json
import os
import secrets
import time
from pathlib import Path

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient, image_disposition

from data.plugins.astrbot_plugin_lajiaoliao.lottery import result_gif, result_text
from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_path = args.output / "lottery-results.json"
    if report_path.exists():
        raise RuntimeError("existing_report_requires_review_not_resend")
    gif = result_gif()["url"].removeprefix("base64://")
    report = {
        "group_id": GROUP,
        "executed": False,
        "passed": False,
        "purpose": "display_test_only_no_enrollment_no_actual_prize",
        "gif_bytes": len(base64.b64decode(gif)),
        "operations": [],
    }
    atomic_json(report_path, report)
    if not args.execute:
        print(json.dumps(report))
        return
    client = AcceptanceClient()
    before = {}
    for index, bot in enumerate(client.bots):
        ref = bot["session_ref"]
        before[ref] = client.api(
            PREFIX + "business", {"session_ref": ref, "group_id": GROUP}
        )
    windows = {}
    try:
        states = client.api(PREFIX + "management")["instances"]
        for index, budget in ((0, 4), (1, 2)):
            bot = client.bots[index]
            state = next(row for row in states if row["id"] == bot["id"])
            if (
                not state["enabled"]
                or state["connection_state"] != "online"
                or GROUP not in state["message_policy"]["group_ids"]
            ):
                raise RuntimeError("designated_test_instance_not_ready")
            if any(
                row["state"] in {"open", "opening", "publishing", "drawing", "unknown"}
                for row in before[bot["session_ref"]]["activities"]
            ):
                raise RuntimeError("active_lottery_requires_review")
            windows[index] = client.api(
                PREFIX + "test-window",
                {
                    "session_ref": bot["session_ref"],
                    "action": "open",
                    "confirm": True,
                    "account_id": client.sessions[index]["account_id"],
                    "duration": 300,
                    "budget": budget,
                    "targets": [{"kind": "group", "target_id": GROUP}],
                },
            )["window_ref"]

        def send(index, stage, **payload):
            operation = "draw-display-" + secrets.token_hex(8)
            item = {"stage": stage, "operation_id": operation, "status": "unknown"}
            report["executed"] = True
            report["operations"].append(item)
            atomic_json(report_path, report)
            receipt = client.api(
                PREFIX + "test-send",
                {
                    "session_ref": client.bots[index]["session_ref"],
                    "window_ref": windows[index],
                    "operation_id": operation,
                    "kind": "group",
                    "target_id": GROUP,
                    **payload,
                },
            )
            item.update(
                status=receipt.get("status", "unknown"),
                message_id=receipt.get("message_id", ""),
            )
            atomic_json(report_path, report)
            if item["status"] != "sent" or not item["message_id"]:
                raise RuntimeError("display_send_not_confirmed_no_retry")
            return item["message_id"]

        def wait_peer(index, message_id):
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                found = next(
                    (
                        row
                        for row in client.inspect(index, "group", GROUP)
                        if row.get("message_id") == message_id
                    ),
                    None,
                )
                if found:
                    return found
                time.sleep(1)
            raise RuntimeError("display_peer_not_confirmed")

        for won, label in ((True, "中奖"), (False, "未中奖")):
            source = send(
                1,
                label + "_source",
                text=f"【抽奖展示测试·{label}】模拟报名消息，不实际报名或领奖",
            )
            wait_peer(0, source)
            text = f"【展示测试，无实际奖品】{result_text('测试纪念品', won)}"
            quote = send(0, label + "_quote", text=text, reply_to=source)
            image = send(0, label + "_gif", image_base64=gif)
            received_quote = wait_peer(1, quote)
            received_image = wait_peer(1, image)
            picture = received_image.get("content_shape", {}).get("sourcePicture", {})
            if (
                received_quote.get("content_type") != 114
                or received_quote.get("quoted_message_id") != source
                or received_quote.get("text") != text
                or received_image.get("content_type") != 102
                or picture.get("size") != report["gif_bytes"]
                or picture.get("type") not in {"gif", "image/gif"}
                or not 0 < received_quote.get("seq", 0) < received_image.get("seq", 0)
            ):
                raise RuntimeError("display_peer_schema_mismatch")
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                state = next(
                    row
                    for row in client.api(PREFIX + "management")["instances"]
                    if row["id"] == client.bots[1]["id"]
                )
                disposition = image_disposition(state, image)
                expected = (
                    "ignored_bound_bot"
                    if state.get("bot_group_mutual_exclusion")
                    else "handed_off"
                )
                if disposition == expected:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("display_gif_disposition_not_confirmed")
            report[label] = {
                "quote_verified": True,
                "gif_verified": True,
                "ordered": True,
                "astrbot_image_handoff": disposition == "handed_off",
                "peer_event_disposition": disposition,
            }
            atomic_json(report_path, report)
        report["passed"] = True
    finally:
        for index in windows:
            try:
                client.api(
                    PREFIX + "test-window",
                    {
                        "session_ref": client.bots[index]["session_ref"],
                        "action": "close",
                    },
                )
                report.setdefault("closed_windows", []).append(index)
            except Exception:
                report.setdefault("close_errors", []).append(index)
        unchanged = True
        for bot in client.bots:
            ref = bot["session_ref"]
            current = client.api(
                PREFIX + "business", {"session_ref": ref, "group_id": GROUP}
            )
            unchanged = unchanged and all(
                current[key] == before[ref][key]
                for key in ("settings", "version", "activities")
            )
        report["business_unchanged"] = unchanged
        report["configuration_unchanged"] = (
            client.original_config == (ROOT / "data/cmd_config.json").read_bytes()
        )
        report["passed"] = (
            report["passed"]
            and unchanged
            and report["configuration_unchanged"]
            and len(report.get("closed_windows", [])) == 2
        )
        atomic_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
