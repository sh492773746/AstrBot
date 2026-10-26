"""Run bounded feature acceptance on the designated pair, restoring all settings."""

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = args.output / "full-feature.json"
    if path.exists():
        raise RuntimeError("existing_report_requires_review_not_resend")
    client = AcceptanceClient()
    scopes = [
        {"session_ref": bot["session_ref"], "group_id": GROUP} for bot in client.bots
    ]
    original = [client.api(PREFIX + "business", scope) for scope in scopes]
    routes = client.api("/api/v1/config-routes")["routing"]
    before_ai = [client.api(PREFIX + "ai-config", scope) for scope in scopes]
    states = client.api(PREFIX + "management")["instances"]
    uids = {session["account_id"] for session in client.sessions}
    for index, scope in enumerate(scopes):
        members = client.api(PREFIX + "members", {**scope, "page": 1})
        state = next(row for row in states if row["id"] == client.bots[index]["id"])
        group = client.api(PREFIX + "group-state", scope)
        window = client.api(
            PREFIX + "test-window",
            {
                "session_ref": scope["session_ref"],
                "action": "status",
            },
        )
        if (
            members["total"] != 2
            or {row["userID"] for row in members["members"]} != uids
            or group["group_status"] != 0
            or not state["enabled"]
            or state["connection_state"] != "online"
            or not state["bot_group_mutual_exclusion"]
            or window["open"]
            or any(
                row["state"] in {"opening", "open", "drawing", "publishing", "unknown"}
                for row in original[index]["activities"]
            )
            or any(
                row["state"] in {"pending", "running", "unknown"}
                for row in original[index]["scheduled"]
            )
        ):
            raise RuntimeError("unsafe_full_acceptance_precondition")
    report = {
        "started_at": time.time(),
        "group_id": GROUP,
        "instances": [bot["id"] for bot in client.bots],
        "executed": False,
        "passed": False,
        "before_settings": [row["settings"] for row in original],
        "steps": [],
        "blocked_live": [
            "ordinary_member_recall",
            "ordinary_member_card_sync",
            "authoritative_unavailable_account_cleanup",
            "positive_human_ranking_enrollment_invitation",
            "ordinary_member_automatic_punishment",
            "real_model_persona_quota_and_cost",
        ],
        "separate_acceptance_required": [
            "two_hour_soak",
            "targeted_worker_recovery",
            "cross_platform_live_latency",
        ],
        "safety": {
            "bound_bot_and_admin_protection": True,
            "no_real_customer_or_commercial_rate_activation": True,
            "no_global_model_or_other_platform_changes": True,
        },
    }
    completed = {}
    if args.resume:
        previous = json.loads(args.resume.read_text())
        if (
            previous["before_settings"] != report["before_settings"]
            or previous.get("cleanup_errors")
            or not previous.get("global_config_unchanged")
            or not previous.get("routes_restored")
            or time.time() - previous["finished_at"] > 3600
        ):
            raise RuntimeError("resume_baseline_changed_requires_review")
        completed = {
            (step["name"], step.get("mode", "default")): step
            for step in previous["steps"]
            if step.get("returncode") == 0
            and step.get("state") == "passed"
            and (step["name"] != "image_acceptance_live.py" or "mode" in step)
        }
        report["prior_report"] = str(args.resume.resolve())
    atomic_json(path, report)
    if not args.execute:
        print("Read-only preflight passed; no switches changed or messages sent.")
        return

    expected_settings = {}

    def record():
        atomic_json(path, report)

    def save(index, settings):
        current = client.api(PREFIX + "business", scopes[index])
        result = client.api(
            PREFIX + "business",
            {
                **scopes[index],
                "action": "save",
                "confirm": True,
                "settings": settings,
                "version": current["version"],
                "instance_revision": current["instance_revision"],
            },
        )
        if result["settings"] != settings:
            raise RuntimeError("saved_switches_differ")
        expected_settings[index] = copy.deepcopy(settings)
        return result

    def run_tool(name, *arguments):
        mode = "private" if "--private" in arguments else "default"
        if (name, mode) in completed:
            report["steps"].append(
                {
                    **completed[(name, mode)],
                    "evidence": str(args.resume.resolve()),
                    "reused_confirmed_receipt": True,
                }
            )
            record()
            print("Reused confirmed " + name + "; no duplicate send", flush=True)
            return
        step = {"name": name, "mode": mode, "state": "running"}
        report["steps"].append(step)
        record()
        print("Starting " + name, flush=True)
        process = subprocess.run(
            [sys.executable, str(Path(__file__).parent / name), *map(str, arguments)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        log = args.output / (name.removesuffix(".py") + "-" + mode + ".log")
        log.write_text(process.stdout)
        log.chmod(0o600)
        step.update(
            returncode=process.returncode,
            state="passed" if process.returncode == 0 else "failed",
        )
        record()
        if process.returncode:
            raise RuntimeError("acceptance_tool_failed:" + name)
        print("Passed " + name, flush=True)

    try:
        report["executed"] = True
        for index, scope in enumerate(scopes):
            settings = copy.deepcopy(original[index]["settings"])
            settings.update(
                group_ai_enabled=True,
                commands_enabled=True,
                rankings_enabled=True,
                lottery_enabled=True,
                reminders_enabled=True,
                review_enabled=True,
                ai_management_enabled=True,
                invitations_enabled=True,
                invitation_rewards_enabled=True,
                review_provider_id="GeminiNativeLajiao/gemini-3.8-flash",
            )
            if settings["review_provider_id"] not in {
                row["id"] for row in original[index]["review_models"]
            }:
                raise RuntimeError("verified_current_review_provider_missing")
            settings["native_actions"] = ["mute", "unmute", "reply_recall"]
            settings["maintenance"]["reply_recall_enabled"] = True
            settings["automation"].update(
                enabled=True,
                keyword_enabled=True,
                flood_enabled=True,
                ai_enabled=True,
                ai_rules="Flag only the explicit FULL_ACCEPTANCE_SPAM marker.",
            )
            settings["keywords"] = ["FULL_ACCEPTANCE_SPAM"]
            settings["lottery"].update(
                notify_results=True,
                prize="Acceptance example - no cash",
                winners=1,
                minutes=1,
                capacity=2,
                contact="Test robot",
            )
            current = save(index, settings)
            report["steps"].append(
                {
                    "name": "enable_supported_switches",
                    "instance": client.bots[index]["id"],
                    "state": "passed",
                    "settings": settings,
                    "version": current["version"],
                }
            )
            record()
            for feature in ("member_recall", "member_cards", "account_cleanup"):
                proposed = copy.deepcopy(settings)
                if feature == "member_recall":
                    proposed["automation"]["punishment_mode"] = "recall_progressive"
                    proposed["automation"]["flood_recall"] = True
                    proposed["native_actions"].append("revoke_others")
                elif feature == "member_cards":
                    proposed["maintenance"]["cards_enabled"] = True
                    proposed["native_actions"].append("rename_others")
                else:
                    proposed["maintenance"]["cleanup_enabled"] = True
                    proposed["native_actions"].append("cleanup")
                if feature != "account_cleanup":
                    enabled = save(index, proposed)
                    if enabled["settings"] != proposed:
                        raise RuntimeError("verified_native_switch_not_saved")
                    current = save(index, settings)
                    report["steps"].append(
                        {
                            "name": feature + "_configuration",
                            "state": "passed",
                            "instance": client.bots[index]["id"],
                            "live_execution": "native_verified_workflow_pending",
                            "restored": True,
                        }
                    )
                    record()
                    continue
                rejected = client.api(
                    PREFIX + "business",
                    {
                        **scope,
                        "action": "save",
                        "confirm": True,
                        "settings": proposed,
                        "version": current["version"],
                        "instance_revision": current["instance_revision"],
                    },
                    expected=409,
                )
                if rejected.get("message") != "native_capability_unverified":
                    raise RuntimeError("unexpected_capability_gate")
                if client.api(PREFIX + "business", scope)["settings"] != settings:
                    raise RuntimeError("rejected_switch_changed_settings")
                report["steps"].append(
                    {
                        "name": feature + "_gate",
                        "state": "passed",
                        "instance": client.bots[index]["id"],
                        "live_execution": "blocked_unverified",
                    }
                )
                record()
            conflict = client.api(
                PREFIX + "business",
                {
                    **scope,
                    "action": "save",
                    "confirm": True,
                    "settings": settings,
                    "version": current["version"] - 1,
                    "instance_revision": current["instance_revision"],
                },
                expected=409,
            )
            if conflict.get("message") != "business_changed_refresh_required":
                raise RuntimeError("unexpected_version_conflict")

        run_tool(
            "bot_exclusion_acceptance_live.py",
            "--execute",
            "--output",
            args.output / "mutual-exclusion",
        )
        run_tool(
            "image_acceptance_live.py", "--output", args.output / "images-group.json"
        )
        run_tool("gif_acceptance_live.py", "--execute", "--output", args.output / "gif")
        run_tool(
            "lottery_result_acceptance_live.py",
            "--execute",
            "--output",
            args.output / "lottery-display",
        )
        run_tool(
            "image_acceptance_live.py",
            "--private",
            "--output",
            args.output / "images-private.json",
        )
        run_tool(
            "mutual_management_acceptance.py",
            "--execute",
            "--confirm-accounts",
            ",".join(row["account_id"] for row in client.sessions),
            "--output",
            args.output / "private-managers.json",
        )
        run_tool(
            "private_bot_exclusion_acceptance.py",
            "--execute",
            "--output",
            args.output / "private-mutual.json",
        )
        run_tool(
            "planned_business_acceptance.py",
            "--execute",
            "--output",
            args.output / "future-plans.json",
        )
        for index, scope in enumerate(scopes):
            current = client.api(PREFIX + "business", scope)
            if any(
                row.get("user_id", row.get("uid")) in uids
                for row in current["rankings"]
            ):
                raise RuntimeError("bound_bot_counted_in_ranking")
            if any(
                row["enrolled"] or row["winners"]
                for row in current["activities"]
                if row["starts"] >= report["started_at"]
            ):
                raise RuntimeError("bound_bot_enrolled_or_awarded")
        report["passed"] = True
    except Exception as error:
        report["error"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
        record()
    finally:
        for index, settings in expected_settings.items():
            try:
                current = client.api(PREFIX + "business", scopes[index])
                if current["settings"] != settings:
                    raise RuntimeError("settings_changed_externally_requires_review")
                save(index, original[index]["settings"])
                report.setdefault("switches_restored", {})[client.bots[index]["id"]] = (
                    True
                )
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(
                    client.bots[index]["id"] + ":" + str(error)
                )
        report["global_config_unchanged"] = (
            hashlib.sha256(client.original_config).digest()
            == hashlib.sha256((ROOT / "data/cmd_config.json").read_bytes()).digest()
        )
        report["routes_restored"] = (
            client.api("/api/v1/config-routes")["routing"] == routes
        )
        report["ai_config_unchanged"] = all(
            client.api(PREFIX + "ai-config", scope)["settings"]
            == before_ai[index]["settings"]
            for index, scope in enumerate(scopes)
        )
        report["final_group_status"] = client.api(PREFIX + "group-state", scopes[0])[
            "group_status"
        ]
        report["final_instances"] = [
            {
                key: row[key]
                for key in (
                    "id",
                    "enabled",
                    "connection_state",
                    "bot_group_mutual_exclusion",
                )
            }
            for row in client.api(PREFIX + "management")["instances"]
        ]
        if (
            report.get("cleanup_errors")
            or not report["global_config_unchanged"]
            or not report["routes_restored"]
            or not report["ai_config_unchanged"]
            or report["final_group_status"] != 0
        ):
            report["passed"] = False
        report["finished_at"] = time.time()
        record()
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"before_settings", "steps"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
