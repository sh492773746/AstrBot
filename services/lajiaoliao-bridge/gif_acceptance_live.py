"""Send one tiny animation to an explicitly designated group or owned peer."""

import argparse
import base64
import io
import json
import os
import secrets
import time
from pathlib import Path

from acceptance_client import GROUP, PREFIX, AcceptanceClient, image_disposition
from PIL import Image, ImageDraw

from data.plugins.astrbot_plugin_lajiaoliao.media import validate_bytes
from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def tiny_gif():
    """Generate six palette frames without converting animation to a still."""
    palette = [255, 255, 255, 20, 30, 40, 18, 125, 99, 248, 211, 64]
    spots = {
        1: [(32, 25)],
        2: [(24, 17), (40, 33)],
        3: [(24, 17), (32, 25), (40, 33)],
        4: [(24, 17), (40, 17), (24, 33), (40, 33)],
        5: [(24, 17), (40, 17), (32, 25), (24, 33), (40, 33)],
        6: [(24, 17), (40, 17), (24, 25), (40, 25), (24, 33), (40, 33)],
    }
    frames = []
    for number in range(1, 7):
        frame = Image.new("P", (64, 56), 0)
        frame.putpalette(palette)
        draw = ImageDraw.Draw(frame)
        draw.rectangle((15, 8, 49, 42), fill=3, outline=1, width=2)
        for x, y in spots[number]:
            draw.rectangle((x - 2, y - 2, x + 2, y + 2), fill=1)
        draw.text((19, 44), "DRAW", fill=2)
        frames.append(frame)
    stream = io.BytesIO()
    frames[0].save(
        stream,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=180,
        loop=0,
        optimize=True,
        disposal=1,
    )
    raw = stream.getvalue()
    assert validate_bytes(raw) == "image/gif"
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--sender-index", type=int, choices=(0, 1), default=0)
    parser.add_argument("--with-text", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    asset = args.output / "draw-test.gif"
    raw = tiny_gif()
    asset.write_bytes(raw)
    asset.chmod(0o600)
    report_path = args.output / "gif-send.json"
    if report_path.exists():
        raise RuntimeError("existing_report_requires_review_not_resend")
    report = {
        "format": "GIF",
        "bytes": len(raw),
        "width": 64,
        "height": 56,
        "frames": 6,
        "executed": False,
        "passed": False,
    }
    atomic_json(report_path, report)
    if not args.execute:
        print(json.dumps(report))
        return
    client = AcceptanceClient()
    index = args.sender_index
    peer = 1 - index
    bot, account = client.bots[index], client.sessions[index]
    kind = "private" if args.private else "group"
    target = client.sessions[peer]["account_id"] if args.private else GROUP
    report.update(
        group_id=GROUP,
        sender_instance=bot["id"],
        kind=kind,
        target_id=target,
        mixed_message=args.with_text,
    )
    states = client.api(PREFIX + "management")["instances"]
    for expected in client.bots:
        state = next(row for row in states if row["id"] == expected["id"])
        if (
            not state["enabled"]
            or state["connection_state"] != "online"
            or GROUP not in state["message_policy"]["group_ids"]
            or (args.private and not state["message_policy"]["private_enabled"])
        ):
            raise RuntimeError("test_group_or_instance_not_ready")
        roster = client.api(
            PREFIX + "members",
            {
                "session_ref": expected["session_ref"],
                "group_id": GROUP,
                "page": 1,
            },
        )
        if roster["total"] != 2 or {row["userID"] for row in roster["members"]} != {
            row["account_id"] for row in client.sessions
        }:
            raise RuntimeError("test_group_members_changed")
    window = client.api(
        PREFIX + "test-window",
        {
            "session_ref": bot["session_ref"],
            "action": "open",
            "confirm": True,
            "account_id": account["account_id"],
            "duration": 180,
            "budget": 1,
            "targets": [{"kind": kind, "target_id": target}],
        },
    )
    try:
        operation = "gif-" + secrets.token_hex(8)
        report.update(executed=True, operation_id=operation, status="unknown")
        atomic_json(report_path, report)
        receipt = client.api(
            PREFIX + "test-send",
            {
                "session_ref": bot["session_ref"],
                "window_ref": window["window_ref"],
                "operation_id": operation,
                "kind": kind,
                "target_id": target,
                "image_base64": base64.b64encode(raw).decode(),
                **({"text": "混合消息验收：文字后发送GIF。"} if args.with_text else {}),
            },
        )
        report["receipt"] = receipt
        report["status"] = receipt.get("status", "unknown")
        atomic_json(report_path, report)
        if report["status"] != "sent":
            raise RuntimeError("gif_delivery_not_confirmed")
        receipts = receipt.get("receipts", [])
        if args.with_text and (
            len(receipts) != 2 or any(row.get("status") != "sent" for row in receipts)
        ):
            raise RuntimeError("mixed_message_receipts_not_confirmed")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            peer_rows = client.inspect(
                peer, kind, account["account_id"] if args.private else GROUP
            )
            found = next(
                (
                    row
                    for row in peer_rows
                    if row.get("message_id") == receipt["message_id"]
                ),
                None,
            )
            state = next(
                row
                for row in client.api(PREFIX + "management")["instances"]
                if row["id"] == client.bots[peer]["id"]
            )
            disposition = image_disposition(state, receipt["message_id"])
            expected = (
                "ignored_bound_bot"
                if kind == "group" and state.get("bot_group_mutual_exclusion")
                else "handed_off"
            )
            if found and disposition == expected:
                if args.with_text:
                    text_row = next(
                        (
                            row
                            for row in peer_rows
                            if row.get("message_id") == receipts[0]["message_id"]
                        ),
                        None,
                    )
                    if not text_row:
                        time.sleep(1)
                        continue
                    if (
                        text_row.get("text") != "混合消息验收：文字后发送GIF。"
                        or text_row.get("content_type") != 101
                        or not 0 < text_row.get("seq", 0) < found.get("seq", 0)
                    ):
                        raise RuntimeError("mixed_message_peer_order_mismatch")
                    report["mixed_order_verified"] = True
                picture = found.get("content_shape", {}).get("sourcePicture", {})
                if (
                    found.get("content_type") != 102
                    or picture.get("type") not in {"gif", "image/gif"}
                    or picture.get("size") != len(raw)
                    or picture.get("width") != 64
                    or picture.get("height") != 56
                ):
                    raise RuntimeError("peer_gif_schema_mismatch")
                report.update(
                    passed=True,
                    peer_content_type=102,
                    peer_type=picture["type"],
                    peer_size=picture["size"],
                    astrbot_image_handoff=disposition == "handed_off",
                    peer_event_disposition=disposition,
                    scope="native_wire_and_bound_bot_guard"
                    if disposition == "ignored_bound_bot"
                    else "native_wire_and_image_handoff",
                )
                break
            time.sleep(1)
        if not report["passed"]:
            raise RuntimeError("peer_gif_disposition_not_confirmed")
    finally:
        try:
            client.api(
                PREFIX + "test-window",
                {
                    "session_ref": bot["session_ref"],
                    "action": "close",
                },
            )
            report["window_closed"] = True
        finally:
            report["configuration_unchanged"] = (
                client.original_config
                == (
                    Path(__file__).resolve().parents[2] / "data/cmd_config.json"
                ).read_bytes()
            )
            atomic_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
