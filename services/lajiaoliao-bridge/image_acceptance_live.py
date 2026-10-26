"""Send bounded PNG/JPEG probes only to the already designated test group."""

import argparse
import base64
import copy
import io
import json
import secrets
import time
from pathlib import Path

from acceptance_client import GROUP, PREFIX, AcceptanceClient, image_disposition
from acceptance_routes import ScopedTestRoutes
from PIL import Image, ImageDraw

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    """Verify group image transport without changing grants or model routing.

    Returns:
        None. Writes a private evidence file with no URLs or credentials.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private", action="store_true")
    args = parser.parse_args()
    client = AcceptanceClient()
    report = {
        "scope": "two_bound_test_bots_group_72567012",
        "probes": [],
        "private": "pending_scoped_model_disabled_test",
    }
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    routes = None
    profile = None
    atomic_json(args.output, report)
    try:
        if args.private:
            original_routes = client.api("/api/v1/config-routes")["routing"]
            exact = [
                bot["id"]
                + ":FriendMessage:private:"
                + client.sessions[1 - index]["account_id"]
                for index, bot in enumerate(client.bots)
            ]
            defaults = copy.deepcopy(
                client.api("/api/v1/config-profiles/schema")["config"]
            )
            defaults.update(
                dashboard={},
                platform=[],
                provider=[],
                provider_sources=[],
                admins_id=[],
                plugin_set=["astrbot_plugin_lajiaoliao"],
            )
            defaults["provider_settings"]["enable"] = False
            profile = client.api(
                "/api/v1/config-profiles",
                {
                    "name": "lajiaoliao-image-private-" + secrets.token_hex(8),
                    "config": defaults,
                },
            )["conf_id"]
            report["temporary_profile"] = profile
            atomic_json(args.output, report)
            from acceptance_client import ROOT

            (ROOT / "data/config" / ("abconf_" + profile + ".json")).chmod(0o600)
            routes = ScopedTestRoutes(client.api, exact, original_routes)
            routes.install(profile)
            report["private_routes_installed"] = True
            atomic_json(args.output, report)
        for index, (bot, session) in enumerate(
            zip(client.bots, client.sessions, strict=True)
        ):
            peer = 1 - index
            kind = "private" if args.private else "group"
            target = client.sessions[peer]["account_id"] if args.private else GROUP
            window = client.api(
                PREFIX + "test-window",
                {
                    "session_ref": bot["session_ref"],
                    "action": "open",
                    "confirm": True,
                    "account_id": session["account_id"],
                    "duration": 180,
                    "budget": 2,
                    "targets": [{"kind": kind, "target_id": target}],
                },
            )
            for format in ("PNG", "JPEG"):
                image = Image.new("RGB", (192, 96), "white")
                draw = ImageDraw.Draw(image)
                draw.rectangle((8, 8, 90, 86), fill=(20, 140, 90))
                draw.text((98, 40), format, fill="black")
                stream = io.BytesIO()
                image.save(stream, format=format)
                receipt = client.api(
                    PREFIX + "test-send",
                    {
                        "session_ref": bot["session_ref"],
                        "window_ref": window["window_ref"],
                        "operation_id": "image-" + secrets.token_hex(8),
                        "kind": kind,
                        "target_id": target,
                        "text": "图片接入验收 " + format,
                        "image_base64": base64.b64encode(stream.getvalue()).decode(),
                    },
                )
                if receipt.get("status") != "sent":
                    report["probes"].append(
                        {
                            "format": format,
                            "sender": index,
                            "status": receipt.get("status", "unknown"),
                        }
                    )
                    raise RuntimeError("image_delivery_not_confirmed")
                deadline = time.monotonic() + 20
                found = None
                while time.monotonic() < deadline:
                    rows = client.inspect(
                        peer, kind, session["account_id"] if args.private else GROUP
                    )
                    found = next(
                        (
                            row
                            for row in rows
                            if row.get("message_id") == receipt["message_id"]
                        ),
                        None,
                    )
                    if found:
                        break
                    time.sleep(1)
                if not found or found.get("content_type") != 102:
                    raise RuntimeError("peer_native_image_not_found")
                disposition = "pending"
                expected = "handed_off"
                while time.monotonic() < deadline:
                    states = client.api(PREFIX + "management")["instances"]
                    state = next(
                        item for item in states if item["id"] == client.bots[peer]["id"]
                    )
                    disposition = image_disposition(state, receipt["message_id"])
                    expected = (
                        "ignored_bound_bot"
                        if kind == "group" and state.get("bot_group_mutual_exclusion")
                        else "handed_off"
                    )
                    if disposition == expected:
                        break
                    time.sleep(1)
                if disposition != expected:
                    raise RuntimeError("peer_image_disposition_not_confirmed")
                shape = found.get("shape", found.get("content_shape", {}))
                report["probes"].append(
                    {
                        "sender": index,
                        "format": format,
                        "status": "sent",
                        "message_id": receipt["message_id"],
                        "peer_content_type": found["content_type"],
                        "astrbot_image_handoff": disposition == "handed_off",
                        "peer_event_disposition": disposition,
                        "width": shape.get("sourcePicture", {}).get("width"),
                        "height": shape.get("sourcePicture", {}).get("height"),
                    }
                )
                atomic_json(args.output, report)
            client.api(
                PREFIX + "test-window",
                {"session_ref": bot["session_ref"], "action": "close"},
            )
        report["passed"] = len(report["probes"]) == 4
        if args.private:
            report["scope"] = "two_bound_test_bots_private_model_disabled"
            report["private"] = "verified_png_jpeg_both_directions"
    finally:
        cleanup_errors = []
        for bot in client.bots:
            try:
                client.api(
                    PREFIX + "test-window",
                    {"session_ref": bot["session_ref"], "action": "close"},
                )
            except Exception:
                cleanup_errors.append("window_close_failed")
        if routes:
            time.sleep(5)
            try:
                routes.restore()
                report["private_routes_restored"] = True
            except Exception:
                cleanup_errors.append("private_routes_restore_failed")
        if profile and (not routes or report.get("private_routes_restored")):
            try:
                client.api("/api/v1/config-profiles/" + profile, method="DELETE")
                report["temporary_profile_removed"] = True
            except Exception:
                cleanup_errors.append("temporary_profile_delete_failed")
        if cleanup_errors:
            report.update(cleanup_errors=cleanup_errors, passed=False)
        atomic_json(args.output, report)
    print(json.dumps(report, indent=2))
    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
