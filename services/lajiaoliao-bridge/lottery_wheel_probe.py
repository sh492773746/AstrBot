"""Probe GIF rejection and a static wheel PNG in the designated test group."""

import argparse
import base64
import io
import json
import math
import secrets
import time
from pathlib import Path

from acceptance_client import GROUP, PREFIX, AcceptanceClient
from PIL import Image, ImageDraw, ImageFont

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

SENDER = "lajiaoliao_test_20261007"
PEER = "lajiaoliao_0fa92beedaad4eec"


def image_font(size):
    for name in (
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def wheel_png():
    size, center = 640, 320
    image = Image.new("RGB", (size, size), "#f4f6f2")
    draw = ImageDraw.Draw(image)
    palette = ["#0c8068", "#e1a53b", "#d85a4a", "#3589a8"] * 2
    for index, color in enumerate(palette):
        draw.pieslice(
            (36, 36, 604, 604),
            index * 45,
            (index + 1) * 45,
            fill=color,
            outline="white",
            width=5,
        )
    draw.ellipse(
        (center - 98, center - 98, center + 98, center + 98),
        fill="#ffffff",
        outline="#183b32",
        width=7,
    )
    label_font = image_font(31)
    for index in range(8):
        angle = (index * 45 + 22.5) * 3.141592653589793 / 180
        x = center + int(208 * math.cos(angle))
        y = center + int(208 * math.sin(angle))
        text = f"{index + 1:02}"
        box = draw.textbbox((0, 0), text, font=label_font)
        draw.text(
            (x - (box[2] - box[0]) / 2, y - (box[3] - box[1]) / 2),
            text,
            fill="white",
            font=label_font,
        )
    draw.polygon([(center - 24, 48), (center + 24, 48), (center, 102)], fill="#183b32")
    font = image_font(30)
    text = "转盘图"
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (center - (box[2] - box[0]) / 2, center - (box[3] - box[1]) / 2),
        text,
        fill="#183b32",
        font=font,
    )
    result = io.BytesIO()
    image.save(result, format="PNG", optimize=True)
    return result.getvalue()


def animated_gif():
    frames = []
    for color in ("#e1a53b", "#0c8068"):
        frame = Image.new("RGB", (64, 64), color)
        ImageDraw.Draw(frame).ellipse((12, 12, 52, 52), fill="#ffffff")
        frames.append(frame)
    result = io.BytesIO()
    frames[0].save(
        result,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=120,
        loop=0,
    )
    return result.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = AcceptanceClient()
    sender_index = next(i for i, bot in enumerate(client.bots) if bot["id"] == SENDER)
    peer_index = next(i for i, bot in enumerate(client.bots) if bot["id"] == PEER)
    sender = client.bots[sender_index]
    sender_session = client.sessions[sender_index]
    before = client.inspect(peer_index, "group", GROUP)
    baseline_ids = {row.get("message_id") for row in before}
    report = {
        "scope": {"instance": SENDER, "peer": PEER, "group_id": GROUP},
        "gif": {"format": "animated GIF", "submitted_to_native_transport": False},
        "wheel": {
            "format": "static PNG",
            "purpose": "transport probe only; not a draw result",
        },
        "messages_sent": 0,
    }
    atomic_json(args.output, report)
    window = None
    try:
        states = client.api(PREFIX + "management")["instances"]
        selected = {row["id"]: row for row in states if row["id"] in {SENDER, PEER}}
        if len(selected) != 2 or any(
            row["connection_state"] != "online" for row in selected.values()
        ):
            raise RuntimeError("designated_instances_not_online")
        window = client.api(
            PREFIX + "test-window",
            {
                "session_ref": sender["session_ref"],
                "action": "open",
                "confirm": True,
                "account_id": sender_session["account_id"],
                "duration": 180,
                "budget": 2,
                "targets": [{"kind": "group", "target_id": GROUP}],
            },
        )
        gif_result = client.api(
            PREFIX + "test-send",
            {
                "session_ref": sender["session_ref"],
                "window_ref": window["window_ref"],
                "operation_id": "gif-reject-" + secrets.token_hex(8),
                "kind": "group",
                "target_id": GROUP,
                "text": "GIF格式能力探测：预期本地拒绝，不会实际发送",
                "image_base64": base64.b64encode(animated_gif()).decode(),
            },
            expected=400,
        )
        report["gif"].update(
            status="rejected_before_native_submit",
            error_code=gif_result.get("message", gif_result.get("error", "")),
            native_receipt_count=0,
        )
        time.sleep(2)
        gif_messages = [
            row
            for row in client.inspect(peer_index, "group", GROUP)
            if row.get("message_id") not in baseline_ids
            and "GIF格式能力探测" in str(row.get("text", ""))
        ]
        report["gif"]["appeared_in_group"] = bool(gif_messages)
        if gif_messages:
            raise RuntimeError("gif_rejection_not_confirmed")
        wheel = base64.b64encode(wheel_png()).decode()
        result = client.api(
            PREFIX + "test-send",
            {
                "session_ref": sender["session_ref"],
                "window_ref": window["window_ref"],
                "operation_id": "wheel-png-" + secrets.token_hex(8),
                "kind": "group",
                "target_id": GROUP,
                "text": "静态转盘图发送测试（仅验证图片链路，不代表抽奖或开奖结果）",
                "image_base64": wheel,
            },
        )
        if result.get("status") != "sent":
            raise RuntimeError("wheel_png_send_not_confirmed")
        report["messages_sent"] += 1
        receipts = result.get("receipts", [])
        if not receipts:
            raise RuntimeError("wheel_png_receipts_missing")
        peer_rows = []
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            peer_rows = client.inspect(peer_index, "group", GROUP)
            found = [
                row
                for row in peer_rows
                if row.get("message_id")
                in {receipt.get("message_id") for receipt in receipts}
            ]
            image_row = next(
                (row for row in found if row.get("content_type") == 102), None
            )
            if image_row:
                break
            time.sleep(1)
        else:
            raise RuntimeError("wheel_png_native_102_not_confirmed")
        handed_off = False
        while time.monotonic() < deadline:
            rows = client.api(PREFIX + "management")["instances"]
            current = next(row for row in rows if row["id"] == PEER)
            handed_off = any(
                row.get("message_id") == image_row["message_id"]
                and row.get("status") == "handed_off"
                for row in current.get("received_images", [])
            )
            if handed_off:
                break
            time.sleep(1)
        if not handed_off:
            raise RuntimeError("wheel_png_astrbot_handoff_not_confirmed")
        report["wheel"].update(
            status="sent",
            message_id=image_row["message_id"],
            native_content_type=image_row["content_type"],
            astrbot_image_handoff=True,
            text_receipts=[
                {
                    key: receipt[key]
                    for key in ("status", "message_id")
                    if key in receipt
                }
                for receipt in receipts
            ],
        )
        report["passed"] = True
    finally:
        try:
            client.api(
                PREFIX + "test-window",
                {"session_ref": sender["session_ref"], "action": "close"},
            )
            report["test_window_closed"] = True
        except Exception:
            report["test_window_closed"] = False
        report["messages_sent"] = min(report["messages_sent"], 1)
        atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("passed") or not report.get("test_window_closed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
