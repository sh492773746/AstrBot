"""Local authenticated acceptance client; never prints credentials or raw errors."""

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import jwt

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao/"
INSTANCES = ("lajiaoliao_test_20261007", "lajiaoliao_0fa92beedaad4eec")
GROUP = "72567012"


def image_disposition(state, message_id):
    """Separate deliberate bound-bot exclusion from genuine Image handoff."""
    for key, status in (
        ("ignored_bot_messages", "ignored_bound_bot"),
        ("received_images", "handed_off"),
    ):
        if any(
            row.get("message_id") == message_id and row.get("status") == status
            for row in state.get(key, [])
        ):
            return status
    return "pending"


class AcceptanceClient:
    def __init__(self, *, test_bindings=True):
        self.original_config = (ROOT / "data/cmd_config.json").read_bytes()
        config = json.loads(self.original_config.decode("utf-8-sig"))
        self.bots = (
            [
                next(row for row in config["platform"] if row["id"] == identifier)
                for identifier in INSTANCES
            ]
            if test_bindings
            else []
        )
        self.sessions = [
            json.loads(
                (
                    ROOT
                    / "data/plugin_data/astrbot_plugin_lajiaoliao"
                    / row["session_ref"]
                    / "session.json"
                ).read_text()
            )
            for row in self.bots
        ]
        if any(
            session["state"] != "bound" or session["instance_id"] != bot["id"]
            for bot, session in zip(self.bots, self.sessions, strict=True)
        ):
            raise RuntimeError("test_binding_changed")
        if test_bindings and self.sessions[0]["owner"] != self.sessions[1]["owner"]:
            raise RuntimeError("test_owner_mismatch")
        self.owner = (
            self.sessions[0]["owner"]
            if test_bindings
            else config["dashboard"]["username"]
        )
        self.authorization = jwt.encode(
            {"username": self.owner, "exp": int(time.time()) + 900},
            config["dashboard"]["jwt_secret"],
            algorithm="HS256",
        )

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None

        self.http = urllib.request.build_opener(NoRedirect())

    def api(self, path, body=None, *, method=None, expected=200):
        request = urllib.request.Request(
            "http://127.0.0.1:6185" + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": "Bearer " + self.authorization,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.http.open(request, timeout=40) as response:
                status, raw = response.status, response.read(2 << 20)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(2 << 20)
        if status != expected:
            reason = ""
            try:
                code = json.loads(raw).get("error", "")
                if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,96}", code):
                    reason = ":" + code
            except (ValueError, AttributeError):
                pass
            raise RuntimeError("dashboard_http_" + str(status) + reason)
        try:
            result = json.loads(raw)
        except ValueError:
            raise RuntimeError("invalid_dashboard_response") from None
        if expected != 200 or path.startswith(PREFIX):
            return result
        if result.get("status") != "ok":
            raise RuntimeError("dashboard_operation_failed")
        return result.get("data")

    def inspect(self, index, kind, target):
        return self.api(
            PREFIX + "message-inspect",
            {
                "session_ref": self.bots[index]["session_ref"],
                "kind": kind,
                "target_id": target,
            },
        )["messages"]
