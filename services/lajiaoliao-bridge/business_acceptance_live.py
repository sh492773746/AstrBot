"""Explicit business acceptance using only the user's two bound test robots.

Production bot exclusions remain intact. A temporary core profile grants only
the designated robot's private management conversation authority. No consumer
or login session is created. Default execution is read-only.
"""

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jwt

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

ACTOR = "lajiaoliao_test_20261007"
MEMBER = "lajiaoliao_0fa92beedaad4eec"
GROUP = "72567012"
ACTOR_UID = "9680209"
MEMBER_UID = "3129316"
PREFIX = "/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao/"
ROUTES = (
    f"{ACTOR}:FriendMessage:private:{MEMBER_UID}",
    f"{MEMBER}:FriendMessage:private:{ACTOR_UID}",
)


def isolated_profile(defaults):
    """Do not copy dashboard credentials, providers or platform connections."""
    profile = copy.deepcopy(defaults)
    profile.update(dashboard={}, platform=[], provider=[], provider_sources=[])
    profile["admins_id"] = [MEMBER_UID]
    profile["plugin_set"] = ["astrbot_plugin_lajiaoliao"]
    profile["provider_settings"]["enable"] = False
    profile["platform_settings"]["enable_id_white_list"] = False
    profile["platform_settings"]["friend_message_needs_wake_prefix"] = False
    profile["wake_prefix"] = ["/"]
    return profile


def next_test_clock(now):
    """Leave one full minute for preview and confirmation."""
    due = (int(now) // 60 + 2) * 60
    clock = datetime.fromtimestamp(due, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    return clock, due


def verify_private_route_priority(routes):
    for umo in ROUTES:
        for pattern in routes:
            parts = pattern.split(":", 2)
            actual = umo.split(":", 2)
            if len(parts) == 3 and all(
                not p or fnmatch.fnmatchcase(value, p)
                for p, value in zip(parts, actual, strict=True)
            ):
                if pattern != umo:
                    raise RuntimeError("test_private_route_shadowed")
                break


def main():
    from acceptance_routes import ScopedTestRoutes

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-account", default="")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--reload-after-schedule",
        action="store_true",
        help="Reload only LajiaoLiao after confirming the temporary task",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    config_file = root / "data/cmd_config.json"
    original_config = config_file.read_bytes()
    config = json.loads(original_config.decode("utf-8-sig"))
    actor = next(row for row in config["platform"] if row["id"] == ACTOR)
    member = next(row for row in config["platform"] if row["id"] == MEMBER)
    data_root = root / "data/plugin_data/astrbot_plugin_lajiaoliao"
    actor_dir = data_root / actor["session_ref"]
    member_dir = data_root / member["session_ref"]
    actor_session = json.loads((actor_dir / "session.json").read_text())
    member_session = json.loads((member_dir / "session.json").read_text())
    if (
        actor_session["owner"] != member_session["owner"]
        or actor_session["account_id"] != ACTOR_UID
        or member_session["account_id"] != MEMBER_UID
        or actor_session["instance_id"] != ACTOR
        or member_session["instance_id"] != MEMBER
        or actor_session["state"] != "bound"
        or member_session["state"] != "bound"
    ):
        raise RuntimeError("test_account_binding_changed")
    authorization = jwt.encode(
        {"username": actor_session["owner"], "exp": int(time.time()) + 900},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *arguments):
            return None

    client = urllib.request.build_opener(NoRedirect())

    def api(path, body=None, *, method=None):
        request = urllib.request.Request(
            "http://127.0.0.1:6185" + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": "Bearer " + authorization,
                "Content-Type": "application/json",
            },
        )
        try:
            with client.open(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError("dashboard_http_" + str(error.code)) from None
        if path.startswith(PREFIX):
            if result.get("status") == "error":
                raise RuntimeError("plugin_request_rejected")
            return result
        if result.get("status") != "ok":
            raise RuntimeError("dashboard_request_rejected")
        return result.get("data")

    scope = {"session_ref": actor["session_ref"], "group_id": GROUP}

    def business():
        return api(PREFIX + "business", scope)

    def group_state():
        return api(PREFIX + "group-state", {**scope, "evidence": True})

    def pid():
        return subprocess.check_output(
            ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
            text=True,
        ).strip()

    state = group_state()
    roster = api(PREFIX + "members", {**scope, "page": 1})["members"]
    before = business()
    routes_before = api("/api/v1/config-routes")["routing"]
    instances = api(PREFIX + "management")["instances"]
    report = {
        "beijing_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "actor": ACTOR,
        "member": MEMBER,
        "group_id": GROUP,
        "executed": False,
        "steps": [],
        "before_settings": before["settings"],
        "before_routes": {umo: routes_before.get(umo) for umo in ROUTES},
        "robot_exclusions_retained": True,
        "acceptance_limits": "Bot exclusion, empty draw, real timed delivery; not human ranking or enrollment.",
    }
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def record():
        atomic_json(args.output, report)

    record()
    if not args.execute:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.confirm_account != MEMBER_UID:
        raise RuntimeError("explicit_test_account_required")
    if (
        {row["userID"] for row in roster} != {ACTOR_UID, MEMBER_UID}
        or state.get("group_status") != 0
        or state.get("role_level") != 100
        or any(
            row["state"] in {"opening", "open", "drawing", "publishing", "unknown"}
            for row in before["activities"]
        )
        or before["reminders"]
        or any(
            not row["enabled"] or row["connection_state"] != "online"
            for row in instances
            if row["id"] in {ACTOR, MEMBER}
        )
    ):
        raise RuntimeError("unsafe_business_test_precondition")
    routing = ScopedTestRoutes(api, ROUTES, routes_before)
    window_before = api(
        PREFIX + "test-window",
        {
            "session_ref": member["session_ref"],
            "action": "status",
        },
    )
    if window_before["open"]:
        raise RuntimeError("existing_test_window_in_use")
    backup = args.output.parent / ("business-baseline-" + secrets.token_hex(4))
    backup.mkdir(mode=0o700)
    for instance, directory in ((ACTOR, actor_dir), (MEMBER, member_dir)):
        destination = backup / (instance + ".sqlite3")
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        source_db = sqlite3.connect(
            (directory / "business.sqlite3").resolve().as_uri() + "?mode=ro",
            uri=True,
        )
        destination_db = sqlite3.connect(destination)
        try:
            source_db.backup(destination_db)
        finally:
            source_db.close()
            destination_db.close()
    report["baseline_directory"] = str(backup)
    original_settings = copy.deepcopy(before["settings"])
    temp_settings = None
    profile_id = None
    profile_name = "lajiaoliao-private-acceptance-" + secrets.token_hex(8)
    report["temporary_profile_name"] = profile_name
    record()
    main_pid = pid()
    window = None
    reminder_id = None

    def wait_for(label, predicate, *, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.5)
        raise RuntimeError(label + "_not_confirmed")

    def save_settings(value, current):
        return api(
            PREFIX + "business",
            {
                **scope,
                "action": "save",
                "confirm": True,
                "version": current["version"],
                "instance_revision": current["instance_revision"],
                "settings": value,
            },
        )

    def send(text, *, kind="private"):
        entry = {
            "text": text,
            "kind": kind,
            "operation_id": "business-live-" + secrets.token_hex(8),
            "receipt": {"status": "unknown"},
        }
        report["steps"].append(entry)
        record()
        entry["receipt"] = api(
            PREFIX + "test-send",
            {
                "session_ref": member["session_ref"],
                "window_ref": window["window_ref"],
                "operation_id": entry["operation_id"],
                "kind": kind,
                "target_id": ACTOR_UID if kind == "private" else GROUP,
                "text": text,
            },
        )
        record()
        if entry["receipt"].get("status") != "sent":
            raise RuntimeError("test_send_result_not_sent")
        return entry

    def actor_private_cursor():
        cursor = json.loads((actor_dir / "cursor.json").read_text())
        ids = sorted([ACTOR_UID, MEMBER_UID])
        return cursor.get("sequences", {}).get("si_" + "_".join(ids), 0)

    def command(text):
        previous = actor_private_cursor()
        entry = send("/" + text)
        wait_for("private_command_handoff", lambda: actor_private_cursor() > previous)
        time.sleep(1)
        return entry

    def read_drafts(clock, text):
        with sqlite3.connect(
            (actor_dir / "business.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
        ) as db:
            rows = db.execute(
                "SELECT id,value FROM drafts WHERE group_id=? AND owner=? AND umo=? AND expires>?",
                (GROUP, MEMBER_UID, ROUTES[0], time.time()),
            ).fetchall()
        for identifier, raw in rows:
            value = json.loads(raw)
            if value == {"kind": "reminder", "clock": clock, "text": text}:
                return identifier
        return None

    try:
        defaults = api("/api/v1/config-profiles/schema")["config"]
        profile_id = api(
            "/api/v1/config-profiles",
            {
                "name": profile_name,
                "config": isolated_profile(defaults),
            },
        )["conf_id"]
        report["temporary_profile"] = profile_id
        record()
        profile_file = root / "data/config" / ("abconf_" + profile_id + ".json")
        if profile_file.is_file():
            profile_file.chmod(0o600)
        routing.install(profile_id)
        verify_private_route_priority(api("/api/v1/config-routes")["routing"])
        report["executed"] = True
        temp_settings = copy.deepcopy(original_settings)
        temp_settings.update(
            rankings_enabled=True, lottery_enabled=True, reminders_enabled=True
        )
        temp_settings["lottery"].update(
            prize="Acceptance test - no cash prize",
            contact="Test robot",
            winners=1,
            minutes=1,
            capacity=2,
            min_invitations=0,
        )
        save_settings(temp_settings, before)
        window = api(
            PREFIX + "test-window",
            {
                "session_ref": member["session_ref"],
                "action": "open",
                "account_id": MEMBER_UID,
                "confirm": True,
                "duration": 300,
                "budget": 10,
                "targets": [
                    {"kind": "private", "target_id": ACTOR_UID},
                    {"kind": "group", "target_id": GROUP},
                ],
            },
        )
        command("群列表")
        command("选择群 1")
        rank_entry = send(
            "LajiaoLiao acceptance: bound robot must not rank.", kind="group"
        )
        time.sleep(2)
        report["rankings"] = business()["rankings"]
        if any(row["member_id"] == MEMBER_UID for row in report["rankings"]):
            raise RuntimeError("robot_counted_in_rankings")
        report["ranking_probe_message"] = rank_entry["receipt"]["message_id"]
        command("开启抽奖")
        activity = wait_for(
            "lottery_open",
            lambda: next(
                (
                    row
                    for row in business()["activities"]
                    if row["state"] == "open"
                    and row["owner"] == MEMBER_UID
                    and row["umo"] == ROUTES[0]
                ),
                None,
            ),
        )
        report["activity_id"] = activity["id"]
        report["lottery_open_receipt"] = json.loads(activity["receipt"])
        send("/参加抽奖", kind="group")
        time.sleep(2)
        if (
            next(
                row for row in business()["activities"] if row["id"] == activity["id"]
            )["enrolled"]
            != 0
        ):
            raise RuntimeError("robot_enrolled_in_lottery")
        command("立即开奖")
        finished = wait_for(
            "lottery_finished",
            lambda: next(
                (
                    row
                    for row in business()["activities"]
                    if row["id"] == activity["id"] and row["state"] == "finished"
                ),
                None,
            ),
        )
        if finished["enrolled"] or finished["winners"]:
            raise RuntimeError("unexpected_lottery_participant")
        report["lottery"] = finished
        clock, due = next_test_clock(time.time())
        text = "LajiaoLiao timed acceptance " + secrets.token_hex(4)
        command("定时通知 " + clock + " " + text)
        reminder_id = wait_for("reminder_preview", lambda: read_drafts(clock, text))
        command("确认定时 " + reminder_id)
        task = wait_for(
            "reminder_active",
            lambda: next(
                (
                    row
                    for row in business()["reminders"]
                    if row["id"] == reminder_id and row["state"] == "active"
                ),
                None,
            ),
        )
        if task["next_run"] != due:
            raise RuntimeError("scheduled_boundary_changed")
        report["scheduled_beijing_time"] = datetime.fromtimestamp(
            due, ZoneInfo("Asia/Shanghai")
        ).isoformat()
        record()
        if args.reload_after_schedule:
            if due - time.time() < 40:
                raise RuntimeError("not_enough_time_for_safe_plugin_reload")
            reload_started = time.monotonic()
            api("/api/v1/plugins/astrbot_plugin_lajiaoliao/reload", {})

            def recovered():
                rows = api(PREFIX + "management")["instances"]
                selected = [row for row in rows if row["id"] in {ACTOR, MEMBER}]
                return len(selected) == 2 and all(
                    row["enabled"] and row["connection_state"] == "online"
                    for row in selected
                )

            wait_for("plugin_reload", recovered, timeout=30)
            resumed = next(
                (row for row in business()["reminders"] if row["id"] == reminder_id),
                None,
            )
            if (
                not resumed
                or resumed["state"] != "active"
                or resumed["next_run"] != due
            ):
                raise RuntimeError("scheduled_task_not_restored_after_reload")
            report["plugin_reload"] = {
                "task_restored": True,
                "main_pid_unchanged": pid() == main_pid,
                "both_instances_online": True,
                "recovery_seconds": round(time.monotonic() - reload_started, 3),
            }
            record()
        task = wait_for(
            "scheduled_delivery",
            lambda: next(
                (
                    row
                    for row in business()["reminders"]
                    if row["id"] == reminder_id and row["receipt"]
                ),
                None,
            ),
            timeout=max(10, due - time.time() + 20),
        )
        receipt = json.loads(task["receipt"])
        report["reminder_receipt"] = receipt
        record()
        if task["state"] != "active" or receipt.get("status") != "sent":
            raise RuntimeError("scheduled_delivery_not_sent")
        # Inspect only this robot's recent own messages, without replaying cursors.
        own_messages = group_state()["messages"]
        scheduled = next((row for row in own_messages if row["text"] == text), None)
        if scheduled is None or scheduled["message_id"] != receipt.get("message_id"):
            raise RuntimeError("scheduled_message_not_confirmed")
        report["scheduled_message"] = scheduled
        report["group_messages"] = [
            row
            for row in own_messages
            if row["message_id"]
            in {
                receipt["message_id"],
                report["lottery_open_receipt"].get("message_id"),
                json.loads(finished["receipt"]).get("message_id"),
            }
        ]
        if len(report["group_messages"]) != 3:
            raise RuntimeError("business_messages_not_confirmed")
        report["result"] = "passed_scoped_robot_workflow"
    except Exception as error:
        report["result"] = "incomplete"
        report["error"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
        record()
    finally:

        def cleanup(label, operation):
            try:
                operation()
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(
                    label + ":" + type(error).__name__
                )
            finally:
                record()

        def delete_reminder():
            if not reminder_id:
                return
            current = business()
            task = next(
                (row for row in current["reminders"] if row["id"] == reminder_id), None
            )
            if task and task["state"] not in {"sending", "unknown"}:
                api(
                    PREFIX + "business",
                    {
                        **scope,
                        "action": "delete-reminder",
                        "confirm": True,
                        "task_id": reminder_id,
                        "version": current["version"],
                        "instance_revision": current["instance_revision"],
                    },
                )
                report["reminder_deleted"] = True
            elif task:
                report["reminder_review_required"] = True

        def restore_settings():
            latest = business()
            if temp_settings is not None and latest["settings"] == temp_settings:
                report["settings_restored"] = (
                    save_settings(original_settings, latest)["settings"]
                    == original_settings
                )
            else:
                report["settings_restored"] = latest["settings"] == original_settings

        def close_window():
            if window:
                api(
                    PREFIX + "test-window",
                    {
                        "session_ref": member["session_ref"],
                        "action": "close",
                    },
                )
                report["test_window_closed"] = True

        cleanup("reminder", delete_reminder)
        cleanup("settings", restore_settings)
        cleanup("window", close_window)
        # Give final command responses time to arrive under the model-disabled route.
        time.sleep(3)

        def restore_routes():
            identifier = profile_id
            if identifier is None:
                candidates = [
                    row["id"]
                    for row in api("/api/v1/config-profiles")["info_list"]
                    if row.get("name") == profile_name
                ]
                if not candidates:
                    report["temporary_profile_removed"] = True
                    return
                if len(candidates) != 1:
                    raise RuntimeError("temporary_profile_identity_uncertain")
                identifier = candidates[0]
            routing.restore()
            remaining_routes = api("/api/v1/config-routes")["routing"]
            report["private_routes_restored"] = list(remaining_routes.items()) == list(
                routes_before.items()
            )
            if identifier not in remaining_routes.values():
                api("/api/v1/config-profiles/" + identifier, method="DELETE")
                report["temporary_profile_removed"] = True

        cleanup("private_routes", restore_routes)
        report["config_unchanged"] = (
            hashlib.sha256(config_file.read_bytes()).digest()
            == hashlib.sha256(original_config).digest()
        )
        report["main_pid_unchanged"] = pid() == main_pid

        def read_after():
            report["after_business"] = business()
            report["after_instances"] = api(PREFIX + "management")["instances"]

        cleanup("after_status", read_after)
        if (
            report.get("cleanup_errors")
            or report.get("reminder_review_required")
            or (
                report.get("result") == "passed_scoped_robot_workflow"
                and not all(
                    report.get(key) is True
                    for key in (
                        "reminder_deleted",
                        "settings_restored",
                        "test_window_closed",
                        "private_routes_restored",
                        "temporary_profile_removed",
                        "config_unchanged",
                        "main_pid_unchanged",
                    )
                )
            )
        ):
            report["result"] = "incomplete_cleanup"
        record()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["result"] != "passed_scoped_robot_workflow":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
