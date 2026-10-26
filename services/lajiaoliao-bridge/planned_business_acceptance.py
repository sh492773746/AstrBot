"""Accept saved plans only in the designated two-robot group, then restore switches."""

import argparse
import copy
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from acceptance_client import GROUP, PREFIX, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--recover-unmute", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    client = AcceptanceClient()
    scope = {"session_ref": client.bots[0]["session_ref"], "group_id": GROUP}
    before = client.api(PREFIX + "business", scope)
    roster = client.api(PREFIX + "members", {**scope, "page": 1})["members"]
    state = client.api(PREFIX + "group-state", scope)
    if (
        {row["userID"] for row in roster}
        != {row["account_id"] for row in client.sessions}
        or (state.get("group_status") != 0 and not args.recover_unmute)
        or state.get("role_level") != 100
        or any(
            row["state"] in {"opening", "open", "drawing", "publishing", "unknown"}
            for row in before["activities"]
        )
        or any(
            row["state"] in {"pending", "running", "unknown"}
            for row in before.get("scheduled", [])
        )
    ):
        raise RuntimeError("unsafe_test_group_plan_precondition")
    original = copy.deepcopy(before["settings"])
    plan = copy.deepcopy(original)
    start = (int(time.time()) // 60 + 2) * 60
    end = start + 60
    timezone = ZoneInfo("Asia/Shanghai")

    def clock(timestamp):
        return datetime.fromtimestamp(timestamp, timezone).strftime("%H:%M")

    plan.update(lottery_enabled=True, reminders_enabled=True)
    plan["lottery"].update(
        prize="No cash acceptance",
        contact="Test robot",
        winners=1,
        minutes=1,
        capacity=2,
        min_invitations=0,
    )
    plan["lottery_plan"] = {"enabled": True, "starts_at": time.time() + 20}
    plan["reminder_plan"] = {
        "enabled": True,
        "clock": clock(start),
        "text": "计划通知验收 " + secrets.token_hex(4),
    }
    plan["maintenance"].update(
        quiet_enabled=True, quiet_start=clock(start), quiet_end=clock(end)
    )
    plan["native_actions"] = list(
        dict.fromkeys([*plan["native_actions"], "mute_all", "unmute_all"])
    )
    report = {
        "mode": "saved_future_plans_no_manual_start",
        "group_id": GROUP,
        "executed": False,
        "passed": False,
        "before": before,
        "plan": plan,
    }
    args.output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_json(args.output, report)
    if not args.execute:
        print("Read-only plan prepared; no changes or sends.")
        return

    def save(value):
        current = client.api(PREFIX + "business", scope)
        return client.api(
            PREFIX + "business",
            {
                **scope,
                "action": "save",
                "confirm": True,
                "settings": value,
                "version": current["version"],
                "instance_revision": current["instance_revision"],
            },
        )

    if args.recover_unmute:
        recovery = copy.deepcopy(original)
        end = (int(time.time()) // 60 + 2) * 60
        recovery["maintenance"].update(
            quiet_enabled=True, quiet_start=clock(end + 300), quiet_end=clock(end)
        )
        recovery["native_actions"] = list(
            dict.fromkeys([*recovery["native_actions"], "mute_all", "unmute_all"])
        )
        save(recovery)
        deadline = end + 25
        while time.time() < deadline:
            if client.api(PREFIX + "group-state", scope).get("group_status") == 0:
                save(original)
                report.update(
                    passed=True, recovered_unmute=True, switches_restored=True
                )
                atomic_json(args.output, report)
                print("Scheduled unmute confirmed; original switches restored.")
                return
            time.sleep(1)
        report["restore_error"] = "scheduled_unmute_requires_review"
        atomic_json(args.output, report)
        raise RuntimeError("scheduled_unmute_not_confirmed")

    saved = None
    try:
        saved = save(plan)
        report["executed"] = True
        report["saved_version"] = saved["version"]
        atomic_json(args.output, report)
        deadline = end + 25
        observed_muted = False
        while time.time() < deadline:
            current = client.api(PREFIX + "business", scope)
            state = client.api(PREFIX + "group-state", scope)
            observed_muted |= state.get("group_status") == 3
            own_activities = [
                row
                for row in current["activities"]
                if row["owner"] == "dashboard:" + client.owner
                and row["version"] == saved["version"]
            ]
            if any(row["state"] == "unknown" for row in own_activities):
                raise RuntimeError("planned_lottery_result_unknown")
            jobs = current.get("scheduled", [])
            if any(row["state"] in {"unknown", "failed", "paused"} for row in jobs):
                raise RuntimeError("planned_task_not_confirmed")
            if time.time() >= end and state.get("group_status") == 0 and observed_muted:
                finished = [row for row in own_activities if row["state"] == "finished"]
                succeeded = [row for row in jobs if row["state"] == "succeeded"]
                if finished and {"quiet", "notification", "lottery"} <= {
                    row["feature"] for row in succeeded
                }:
                    if any(row["enrolled"] or row["winners"] for row in finished):
                        raise RuntimeError("robot_exclusion_not_preserved")
                    report.update(
                        passed=True,
                        muted_then_unmuted=True,
                        jobs=succeeded,
                        lottery=finished,
                        live_positive_human_draw=False,
                    )
                    break
            time.sleep(1)
        if not report["passed"]:
            raise RuntimeError("future_plan_acceptance_timeout")
    finally:
        if saved:
            current = client.api(PREFIX + "business", scope)
            if current["settings"] != plan:
                report["restore_error"] = "settings_changed_externally"
            else:
                final_state = client.api(PREFIX + "group-state", scope)
                if final_state.get("group_status") == 3:
                    report["restore_error"] = (
                        "scheduled_unmute_not_confirmed_keep_recovery_boundary"
                    )
                    recovery = copy.deepcopy(original)
                    recovery["maintenance"].update(plan["maintenance"])
                    recovery["native_actions"] = plan["native_actions"]
                    if end - time.time() < 30:
                        recovery["maintenance"]["quiet_end"] = clock(
                            (int(time.time()) // 60 + 2) * 60
                        )
                    save(recovery)
                else:
                    save(original)
                    report["switches_restored"] = True
        report["final_group_status"] = client.api(PREFIX + "group-state", scope).get(
            "group_status"
        )
        report["global_config_unchanged"] = (
            client.original_config
            == (
                Path(__file__).resolve().parents[2] / "data/cmd_config.json"
            ).read_bytes()
        )
        atomic_json(args.output, report)
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"before", "plan", "jobs", "lottery"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
