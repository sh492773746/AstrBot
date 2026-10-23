"""Save and restore verified native switches only in the two-robot test group."""

import argparse
import copy
import json
from pathlib import Path

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    """Validate real configuration APIs without sanctions or live member changes.

    Returns:
        None. Writes private configuration and restoration evidence.

    Raises:
        RuntimeError: Test scope, capability declarations, or restoration changed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("existing_evidence_requires_review")
    client = AcceptanceClient()
    scope = {"session_ref": client.bots[0]["session_ref"], "group_id": GROUP}
    roster = client.api(PREFIX + "members", {**scope, "page": 1})
    if roster["total"] != 2 or {row["userID"] for row in roster["members"]} != {
        row["account_id"] for row in client.sessions
    }:
        raise RuntimeError("designated_two_robot_roster_required")
    matrix = client.api(PREFIX + "capabilities")
    features = {row["name"]: row for row in matrix["features"]}
    if (
        not all(
            features[name].get("execution_verified") is True
            for name in ("revoke_others", "rename_others")
        )
        or features["cleanup"].get("execution_verified") is not False
    ):
        raise RuntimeError("execution_declarations_incorrect")
    before = client.api(PREFIX + "business", scope)
    original = copy.deepcopy(before["settings"])
    report = {"passed": False, "executed": False, "steps": [], "messages_sent": 0}
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(args.output, report)
    if not args.execute:
        print("Read-only execution capability preflight passed.")
        return
    expected_settings = original

    def save(value, current):
        return client.api(
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

    try:
        current = before
        for name in ("cards", "progressive_recall", "flood_recall"):
            value = copy.deepcopy(original)
            if name == "cards":
                value["maintenance"]["cards_enabled"] = True
                value["native_actions"] = sorted(
                    set(value["native_actions"] + ["rename_others"])
                )
            else:
                value["review_enabled"] = True
                value["native_actions"] = sorted(
                    set(value["native_actions"] + ["mute", "revoke_others"])
                )
                value["automation"]["enabled"] = True
                value["automation"]["punishment_mode"] = (
                    "recall_progressive" if name == "progressive_recall" else "mute"
                )
                value["automation"]["flood_recall"] = name == "flood_recall"
            previous = current
            current = save(value, current)
            expected_settings = value
            report["executed"] = True
            atomic_json(args.output, report)
            if current["settings"] != value:
                raise RuntimeError("verified_feature_not_saved")
            read = client.api(PREFIX + "business", scope)
            if read["settings"] != value:
                raise RuntimeError("verified_feature_not_persisted")
            conflict = client.api(
                PREFIX + "business",
                {
                    **scope,
                    "action": "save",
                    "confirm": True,
                    "settings": original,
                    "version": previous["version"],
                    "instance_revision": previous["instance_revision"],
                },
                expected=409,
            )
            if conflict["message"] != "business_changed_refresh_required":
                raise RuntimeError("version_conflict_not_enforced")
            report["steps"].append(
                {
                    "feature": name,
                    "saved": True,
                    "read_matches": True,
                    "old_version_rejected": True,
                    "actual_sanctions": 0,
                }
            )
            current = save(original, current)
            expected_settings = original
            atomic_json(args.output, report)
        cleanup = copy.deepcopy(original)
        cleanup["maintenance"]["cleanup_enabled"] = True
        cleanup["native_actions"] = sorted(set(cleanup["native_actions"] + ["cleanup"]))
        rejected = client.api(
            PREFIX + "business",
            {
                **scope,
                "action": "save",
                "confirm": True,
                "settings": cleanup,
                "version": current["version"],
                "instance_revision": current["instance_revision"],
            },
            expected=409,
        )
        report["cleanup_denied"] = rejected["message"] == "native_capability_unverified"
        report["passed"] = report["cleanup_denied"] and len(report["steps"]) == 3
    finally:
        latest = client.api(PREFIX + "business", scope)
        if latest["settings"] != original:
            if latest["settings"] != expected_settings:
                report["restore_error"] = "concurrent_change_do_not_overwrite"
                report["passed"] = False
                atomic_json(args.output, report)
                raise RuntimeError("configuration_changed_during_test")
            latest = save(original, latest)
        report["settings_restored"] = latest["settings"] == original
        report["global_config_unchanged"] = (
            ROOT / "data/cmd_config.json"
        ).read_bytes() == client.original_config
        report["passed"] = (
            report["passed"]
            and report["settings_restored"]
            and report["global_config_unchanged"]
        )
        atomic_json(args.output, report)
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
