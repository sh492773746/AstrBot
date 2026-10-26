"""Read-only native evidence for the explicitly designated test instance."""

import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import jwt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reload", action="store_true", help="Reload only this plugin before reading"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--include-owned-evidence",
        action="store_true",
        help="Read the same test group's notifications from other owned existing instances",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    config_path = root / "data/cmd_config.json"
    original = config_path.read_bytes()
    config = json.loads(original.decode("utf-8-sig"))
    bot = next(
        row for row in config["platform"] if row["id"] == "lajiaoliao_0fa92beedaad4eec"
    )
    ref = bot["session_ref"]
    session = json.loads(
        (
            root / "data/plugin_data/astrbot_plugin_lajiaoliao" / ref / "session.json"
        ).read_text()
    )
    authorization = jwt.encode(
        {"username": session["owner"], "exp": int(time.time()) + 300},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *arguments):
            return None

    client = urllib.request.build_opener(NoRedirect())

    def api(path, body=None):
        request = urllib.request.Request(
            "http://127.0.0.1:6185" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + authorization,
                "Content-Type": "application/json",
            },
        )
        try:
            with client.open(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            # Never print raw exceptions, response bodies or authentication headers.
            raise RuntimeError(
                "Dashboard request rejected: HTTP " + str(exc.code)
            ) from None
        if path.startswith("/api/v1/plugins/extensions/"):
            return result
        if result.get("status") != "ok":
            raise RuntimeError("Dashboard operation did not complete")
        return result.get("data")

    main_pid = subprocess.check_output(
        ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
        text=True,
    ).strip()
    prefix = "/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao/"
    if args.reload:
        api("/api/v1/plugins/astrbot_plugin_lajiaoliao/reload", {})
    deadline = time.monotonic() + 45
    while True:
        management = api(prefix + "management")
        if all(
            row["connection_state"] == "online"
            for row in management["instances"]
            if row["enabled"]
        ):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Enabled LajiaoLiao instances did not recover in time")
        time.sleep(1)
    body = {"session_ref": ref, "group_id": "72567012"}
    state = api(prefix + "group-state", {**body, "evidence": True})
    directory = api(prefix + "members", {**body, "page": 1})
    business = api(prefix + "business", body)
    evidence = [
        {
            key: row.get(key)
            for key in ("message_id", "timestamp", "content_type", "parsed")
        }
        for row in state.get("protocol_evidence", [])
    ]
    result = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "instance_id": bot["id"],
        "group_id": "72567012",
        "role_level": state.get("role_level"),
        "nickname": state.get("nickname"),
        "announcement": state.get("announcement"),
        "group_status": state.get("group_status"),
        "members": [
            {
                key: row.get(key)
                for key in (
                    "userID",
                    "roleLevel",
                    "inviterUserID",
                    "joinSource",
                    "joinTime",
                    "muteEndTime",
                )
            }
            for row in directory.get("members", [])
        ],
        "evidence": evidence,
        "consumer_notifications": [
            {
                key: row.get(key)
                for key in (
                    "message_id",
                    "timestamp",
                    "action",
                    "group_id",
                    "member_ids",
                    "inviter_id",
                    "operator_id",
                )
            }
            for row in state.get("notifications", [])
        ],
        "business_version": business["version"],
        "native_actions": business["settings"]["native_actions"],
        "invitations_enabled": business["settings"]["invitations_enabled"],
        "instances": [
            {"id": row["id"], "connection_state": row["connection_state"]}
            for row in management["instances"]
        ],
        "config_unchanged": hashlib.sha256(config_path.read_bytes()).digest()
        == hashlib.sha256(original).digest(),
        "main_pid_unchanged": main_pid
        == subprocess.check_output(
            ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
            text=True,
        ).strip(),
    }
    if args.include_owned_evidence:
        result["peer_group_evidence"] = []
        for instance in management["instances"]:
            if instance["id"] == bot["id"] or not instance["enabled"]:
                continue
            if "72567012" not in instance["message_policy"].get("group_ids", []):
                continue
            peer = api(
                prefix + "group-state",
                {
                    "session_ref": instance["session_ref"],
                    "group_id": "72567012",
                    "evidence": True,
                },
            )
            result["peer_group_evidence"].append(
                {
                    "instance_id": instance["id"],
                    "consumer_notifications": [
                        {
                            key: row.get(key)
                            for key in (
                                "message_id",
                                "timestamp",
                                "action",
                                "group_id",
                                "member_ids",
                                "inviter_id",
                                "operator_id",
                            )
                        }
                        for row in peer.get("notifications", [])
                    ],
                    "evidence": [
                        {
                            key: row.get(key)
                            for key in (
                                "message_id",
                                "timestamp",
                                "content_type",
                                "parsed",
                            )
                        }
                        for row in peer.get("protocol_evidence", [])
                    ],
                }
            )
    if not result["config_unchanged"] or not result["main_pid_unchanged"]:
        raise RuntimeError("Deployment verification changed unexpectedly")
    if args.output:
        from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
