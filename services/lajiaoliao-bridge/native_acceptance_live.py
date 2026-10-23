"""Explicit one-shot acceptance for the user's two existing test robots.

Default is read-only. Mutations use the normal Dashboard plugin action API,
its native-role checks and durable worker receipts; no second consumer starts.
"""

import argparse
import copy
import hashlib
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import jwt

from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore, atomic_json
from data.plugins.astrbot_plugin_lajiaoliao.test_window import NativeTestPermit

ACTOR = "lajiaoliao_test_20261007"
TARGET = "lajiaoliao_0fa92beedaad4eec"
GROUP = "72567012"
TARGET_UID = "3129316"


def accept_all_members(make_body, execute, state, record):
    """Restore a previously unmuted test group even if verification fails."""
    mute = make_body("mute_all")
    unmute = make_body("unmute_all")
    try:
        entry = execute(mute, duplicate=True)
        entry["server_group_status"] = state()["group_status"]
        record()
        if entry["server_group_status"] != 3:
            raise RuntimeError("group_mute_state_not_confirmed")
    finally:
        # This is the inverse action, not a retry of an uncertain mute request.
        entry = execute(unmute, duplicate=True)
        entry["server_group_status"] = state()["group_status"]
        record()
        if entry["server_group_status"] != 0:
            raise RuntimeError("group_unmute_state_not_confirmed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--kick", action="store_true")
    parser.add_argument("--mute-all-only", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.mute_all_only and args.kick:
        parser.error("--mute-all-only cannot be combined with --kick")
    root = Path(__file__).resolve().parents[2]
    config_path = root / "data/cmd_config.json"
    original = config_path.read_bytes()
    config = json.loads(original.decode("utf-8-sig"))
    actor = next(row for row in config["platform"] if row["id"] == ACTOR)
    target = next(row for row in config["platform"] if row["id"] == TARGET)
    store = SessionStore(root / "data/plugin_data/astrbot_plugin_lajiaoliao")
    actor_row = store._read(actor["session_ref"])
    target_row = store.get(target["session_ref"], actor_row["owner"])
    if (
        actor_row["account_id"] != "9680209"
        or target_row["account_id"] != TARGET_UID
        or actor_row["instance_id"] != ACTOR
        or target_row["instance_id"] != TARGET
        or actor_row["state"] != "bound"
        or target_row["state"] != "bound"
    ):
        raise RuntimeError("test_account_binding_changed")
    authorization = jwt.encode(
        {"username": actor_row["owner"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"], algorithm="HS256",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *arguments):
            return None

    client = urllib.request.build_opener(NoRedirect())
    prefix = "/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao/"

    def api(path, body=None):
        req = urllib.request.Request(
            "http://127.0.0.1:6185" + prefix + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + authorization,
                     "Content-Type": "application/json"},
        )
        try:
            with client.open(req, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError("dashboard_http_" + str(error.code)) from None
        if result.get("status") == "error":
            raise RuntimeError("dashboard_" + str(result.get("message", "rejected")))
        return result

    scope = {"session_ref": actor["session_ref"], "group_id": GROUP}

    def state():
        return api("group-state", {**scope, "evidence": True})

    def members():
        rows = api("members", {**scope, "page": 1})["members"]
        if len(rows) >= 100:
            raise RuntimeError("test_roster_exceeds_scope")
        return {row["userID"]: row for row in rows}

    def pid():
        return subprocess.check_output(
            ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
            text=True,
        ).strip()

    main_pid = pid()
    before_state, before_members = state(), members()
    before = api("business", scope)
    original_settings = copy.deepcopy(before["settings"])
    report = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor_instance": ACTOR, "actor_uid": actor_row["account_id"],
        "target_instance": TARGET, "target_uid": TARGET_UID, "group_id": GROUP,
        "executed": False, "steps": [],
        "mode": "all_members" if args.mute_all_only else "member_actions",
        "before": {"role_level": before_state["role_level"],
                   "announcement": before_state["announcement"],
                   "group_status": before_state.get("group_status"),
                   "members": list(before_members.values()),
                   "settings": original_settings},
    }
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def record():
        atomic_json(args.output, report)

    record()
    if not args.execute:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.confirm_target != TARGET_UID:
        raise RuntimeError("explicit_test_target_required")
    if (
        before_state["role_level"] not in {60, 100}
        or before_members.get(TARGET_UID, {}).get("roleLevel") != 20
        or before_members[TARGET_UID].get("muteEndTime", 0) != 0
        or before["activities"] or before["reminders"]
    ):
        raise RuntimeError("unsafe_live_test_precondition")
    if args.mute_all_only and (
        before_state.get("group_status") != 0
        or set(before_members) != {actor_row["account_id"], TARGET_UID}
    ):
        raise RuntimeError("unsafe_all_members_precondition")
    permit = NativeTestPermit(store)
    changed_settings = None
    needs_unmute = False
    announcement_changed = False
    unmute_body = None
    restore_body = None
    group_unmute_required = False
    old_announcement = before_state["announcement"]
    test_announcement = "LajiaoLiao moderation acceptance test - temporary"

    def save(settings, current):
        return api("business", {
            **scope, "action": "save", "confirm": True,
            "version": current["version"],
            "instance_revision": current["instance_revision"], "settings": settings,
        })

    def execute(body, duplicate=False):
        # Persist intent before even the local request. A lost response is unknown.
        entry = {"request": body, "receipt": {"status": "unknown"}}
        report["steps"].append(entry)
        record()
        receipt = api("platform-action", body)
        entry["receipt"] = receipt
        record()
        if receipt.get("status") != "succeeded":
            raise RuntimeError("action_" + receipt.get("status", "unknown"))
        query = api("platform-action", {
            "session_ref": actor["session_ref"], "instance_id": ACTOR,
            "operation_id": body["operation_id"], "query": True,
        })
        entry["query_matches"] = query == receipt
        if duplicate:
            entry["duplicate_matches"] = api("platform-action", body) == receipt
        record()
        if not entry["query_matches"] or entry.get("duplicate_matches") is False:
            raise RuntimeError("action_receipt_mismatch")
        return entry

    try:
        changed_settings = copy.deepcopy(original_settings)
        actions = (
            ["mute_all", "unmute_all"]
            if args.mute_all_only
            else ["mute", "unmute", "announce"] + (["kick"] if args.kick else [])
        )
        changed_settings["native_actions"] = sorted(set(
            changed_settings["native_actions"] + actions
        ))
        current = save(changed_settings, before)
        report["executed"] = True

        def body(action, **fields):
            return {
                "session_ref": actor["session_ref"], "instance_id": ACTOR,
                "revision": current["instance_revision"],
                "business_version": current["version"],
                "operation_id": "live-" + action + "-" + secrets.token_hex(8),
                "kind": "group", "target_id": GROUP, "confirm": True,
                "action": action, **fields,
            }

        if args.mute_all_only:
            group_unmute_required = True
            accept_all_members(body, execute, state, record)
            group_unmute_required = False
        else:
            mute_body = body("mute", user_id=TARGET_UID, muted_seconds=60)
            unmute_body = body("unmute", user_id=TARGET_UID)
            kick_body = body("kick", user_id=TARGET_UID, confirm_kick=True)
            permit.create(
                actor["session_ref"], actor_row["owner"], target["session_ref"], GROUP,
                [mute_body, unmute_body] + ([kick_body] if args.kick else []), confirm=True,
            )
            needs_unmute = True
            entry = execute(mute_body, duplicate=True)
            mute_end = members()[TARGET_UID].get("muteEndTime", 0)
            entry["server_mute_end"] = mute_end
            record()
            if mute_end <= time.time():
                raise RuntimeError("mute_state_not_confirmed")
            entry = execute(unmute_body)
            entry["server_mute_end"] = members()[TARGET_UID].get("muteEndTime", 0)
            record()
            if entry["server_mute_end"] != 0:
                raise RuntimeError("unmute_state_not_confirmed")
            needs_unmute = False
            announce_body = body(
                "announce", announcement=test_announcement,
                expected_announcement=old_announcement,
            )
            restore_body = body(
                "announce", announcement=old_announcement,
                expected_announcement=test_announcement,
            )
            announcement_changed = True
            entry = execute(announce_body)
            entry["server_announcement"] = state()["announcement"]
            record()
            if entry["server_announcement"] != test_announcement:
                raise RuntimeError("announcement_state_not_confirmed")
            entry = execute(restore_body)
            entry["server_announcement"] = state()["announcement"]
            record()
            if entry["server_announcement"] != old_announcement:
                raise RuntimeError("announcement_restore_not_confirmed")
            announcement_changed = False
            if args.kick:
                entry = execute(kick_body)
                entry["target_absent"] = TARGET_UID not in members()
                record()
                if not entry["target_absent"]:
                    raise RuntimeError("kick_state_not_confirmed")
        time.sleep(2)
        after_state = state()
        report["notifications"] = after_state.get("notifications", [])
        report["protocol_evidence"] = [
            {key: row.get(key) for key in ("message_id", "timestamp", "content_type", "parsed")}
            for row in after_state.get("protocol_evidence", [])
        ]
        report["result"] = "passed"
    except Exception as error:
        report["result"] = "incomplete"
        report["error"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        record()
    finally:
        # Unknown outcomes are queried and inspected, never automatically resent.
        if needs_unmute and unmute_body:
            report["unmute_review_required"] = True
        if announcement_changed and restore_body:
            report["announcement_review_required"] = True
        if group_unmute_required:
            report["group_unmute_review_required"] = True
        if not args.mute_all_only:
            permit.close(actor["session_ref"])
        latest = api("business", scope)
        if changed_settings is not None and latest["settings"] == changed_settings:
            restored = save(original_settings, latest)
            report["settings_restored"] = restored["settings"] == original_settings
        else:
            report["settings_restored"] = latest["settings"] == original_settings
            if not report["settings_restored"]:
                report["concurrent_settings_change_preserved"] = True
        report["config_unchanged"] = (
            hashlib.sha256(config_path.read_bytes()).digest()
            == hashlib.sha256(original).digest()
        )
        report["main_pid_unchanged"] = pid() == main_pid
        report["permit_removed"] = not (
            store.directory(actor["session_ref"]) / "native-test-permit.json"
        ).exists()
        record()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
