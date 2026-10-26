"""Read actual rejoin evidence; exercise reward rules only in isolated ledgers."""

import argparse
import copy
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS, BusinessStore
from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def calculate_cases(directory, roster, note, excluded):
    """Separate bot exclusion, ordinary invitations, duplicates, and reward epochs.

    Args:
        directory: Private disposable ledger directory, never production data.
        roster: Actual complete server roster.
        note: Actual parsed invitation event.
        excluded: All locally bound robot UIDs.

    Returns:
        Expected and observed rewards for explicitly labeled isolated cases.
    """
    stamp = note["timestamp"]
    settings = {
        **copy.deepcopy(DEFAULTS),
        "invitations_enabled": True,
        "invitation_rewards_enabled": True,
        "invitation_points": 1,
    }
    cases = {}
    # Setting a simulated earlier clock does not retroactively enable production.
    actual = BusinessStore(directory / "actual-evidence-bot-exclusion")
    with patch("time.time", return_value=stamp - 1):
        actual.save_settings(GROUP, 0, settings, "synthetic-test-owner", [])
    with patch("time.time", return_value=stamp + 1):
        credited = actual.invitation_credit(GROUP, note, roster, excluded)
    cases["actual_evidence_with_bot_protection"] = {
        "scope": "isolated_actual_evidence_not_production",
        "credited_members": credited,
        "points": 0,
        "pass": credited == 0 and actual.invitation_summary(GROUP) == [],
    }

    # Synthetic ordinary UIDs must never masquerade as live robot participants.
    inviter, member = "synthetic-ordinary-inviter", "synthetic-ordinary-member"
    synthetic_roster = {
        inviter: {"groupID": GROUP, "userID": inviter},
        member: {
            "groupID": GROUP,
            "userID": member,
            "joinSource": 2,
            "inviterUserID": inviter,
            "joinTime": stamp * 1000,
        },
    }
    synthetic_note = {
        "message_id": "synthetic-invitation",
        "timestamp": stamp,
        "inviter_id": inviter,
        "member_ids": [member],
    }
    eligible = BusinessStore(directory / "eligible-ordinary")
    with patch("time.time", return_value=stamp - 1):
        eligible.save_settings(GROUP, 0, settings, "synthetic-test-owner", [inviter])
    with patch("time.time", return_value=stamp + 1):
        first = eligible.invitation_credit(
            GROUP, synthetic_note, synthetic_roster, excluded
        )
        duplicate = eligible.invitation_credit(
            GROUP,
            {**synthetic_note, "message_id": "synthetic-duplicate"},
            synthetic_roster,
            excluded,
        )
    rejoined = copy.deepcopy(synthetic_roster)
    rejoined[member]["joinTime"] = (stamp + 60) * 1000
    with patch("time.time", return_value=stamp + 61):
        rejoin = eligible.invitation_credit(
            GROUP,
            {
                **synthetic_note,
                "message_id": "synthetic-rejoin",
                "timestamp": stamp + 60,
            },
            rejoined,
            excluded,
        )
    restarted = BusinessStore(directory / "eligible-ordinary")
    with patch("time.time", return_value=stamp + 61):
        after_restart = restarted.invitation_credit(
            GROUP,
            {
                **synthetic_note,
                "message_id": "synthetic-after-restart",
                "timestamp": stamp + 60,
            },
            rejoined,
            excluded,
        )
    total = restarted.invitation_totals(GROUP, inviter)
    cases["ordinary_first_duplicate_rejoin_restart"] = {
        "scope": "synthetic_ordinary_members",
        "first_credited": first,
        "duplicate_credited": duplicate,
        "rejoin_credited": rejoin,
        "restart_credited": after_restart,
        "total": total,
        "pass": (first, duplicate, rejoin, after_restart) == (1, 0, 0, 0)
        and restarted.invitation_summary(GROUP)[0]["points"] == 1,
    }
    for name, baseline, enabled_at, rewards in [
        ("existing_member_baseline", [inviter, member], stamp - 1, True),
        ("enabled_after_join_no_backfill", [inviter], stamp + 1, True),
        ("tracking_without_rewards", [inviter], stamp - 1, False),
    ]:
        ledger = BusinessStore(directory / name)
        value = {**settings, "invitation_rewards_enabled": rewards}
        with patch("time.time", return_value=enabled_at):
            ledger.save_settings(GROUP, 0, value, "synthetic-test-owner", baseline)
        with patch("time.time", return_value=stamp + 2):
            credited = ledger.invitation_credit(
                GROUP, synthetic_note, synthetic_roster, excluded
            )
        summary = ledger.invitation_summary(GROUP)
        expected = 1 if name == "tracking_without_rewards" else 0
        cases[name] = {
            "scope": "synthetic_ordinary_members",
            "credited_members": credited,
            "points": sum(row["points"] for row in summary),
            "pass": credited == expected and sum(row["points"] for row in summary) == 0,
        }
    return cases


def main():
    """Inspect the rejoin without writes to production ledgers or live sends.

    Returns:
        None. Persists a restricted, redacted evidence report.

    Raises:
        RuntimeError: Evidence, account scope, or expected rules cannot be confirmed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("existing_report_requires_review")
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = AcceptanceClient()
    actor, target = client.sessions
    if actor["account_id"] != "9680209" or target["account_id"] != "3129316":
        raise RuntimeError("designated_account_binding_changed")
    scope = {"session_ref": client.bots[0]["session_ref"], "group_id": GROUP}
    members = client.api(PREFIX + "members", {**scope, "page": 1})
    roster = {row["userID"]: row for row in members["members"]}
    if members["total"] != len(roster) or set(roster) != {"9680209", "3129316"}:
        raise RuntimeError("complete_designated_roster_required")
    evidence = client.api(PREFIX + "group-state", {**scope, "evidence": True})
    joins = [
        row["parsed"]
        for row in evidence.get("protocol_evidence", [])
        if row.get("parsed", {}).get("action") == "members_invited"
        and "3129316" in row["parsed"].get("member_ids", [])
    ]
    note = max(joins, key=lambda row: row["timestamp"]) if joins else None
    member = roster["3129316"]
    if (
        note is None
        or note.get("group_id") != GROUP
        or note.get("inviter_id") != member.get("inviterUserID")
        or member.get("joinSource") != 2
        or abs(member["joinTime"] / 1000 - note["timestamp"]) > 5
    ):
        raise RuntimeError("current_rejoin_evidence_unverified")
    report = {
        "checked_at": time.time(),
        "group_id": GROUP,
        "real_evidence": {
            "member_id": "3129316",
            "inviter_id": member["inviterUserID"],
            "join_source": member["joinSource"],
            "join_time_ms": member["joinTime"],
            "beijing_join_time": datetime.fromtimestamp(
                member["joinTime"] / 1000, ZoneInfo("Asia/Shanghai")
            ).isoformat(),
            "message_id": note["message_id"],
            "event_timestamp": note["timestamp"],
            "event_roster_match": True,
        },
        "production": [],
        "isolated_cases": {},
        "production_writes": 0,
        "messages_sent": 0,
        "live_positive_reward_acceptance": False,
    }
    data = ROOT / "data/plugin_data/astrbot_plugin_lajiaoliao"
    excluded = {
        row["account_id"]
        for path in data.glob("*/session.json")
        if (row := json.loads(path.read_text())).get("state") == "bound"
    }
    report["real_evidence"]["member_bound_bot"] = "3129316" in excluded
    report["real_evidence"]["inviter_bound_bot"] = member["inviterUserID"] in excluded
    for bot in client.bots:
        business = client.api(
            PREFIX + "business", {"session_ref": bot["session_ref"], "group_id": GROUP}
        )
        directory = data / bot["session_ref"]
        with sqlite3.connect(
            f"file:{directory / 'business.sqlite3'}?mode=ro", uri=True
        ) as db:
            entries = db.execute(
                "SELECT member_id,inviter_id,message_id,stamp,points FROM invitations "
                "WHERE group_id=? AND member_id=?",
                (GROUP, "3129316"),
            ).fetchall()
            baseline = (
                db.execute(
                    "SELECT 1 FROM invitation_members WHERE group_id=? AND member_id=?",
                    (GROUP, "3129316"),
                ).fetchone()
                is not None
            )
        value = business["settings"]
        report["production"].append(
            {
                "instance_id": bot["id"],
                "settings_version": business["version"],
                "tracking_enabled": value["invitations_enabled"],
                "rewards_enabled": value["invitation_rewards_enabled"],
                "points_per_eligible_invitation": value["invitation_points"],
                "target_in_lifetime_baseline": baseline,
                "target_credit_rows": entries,
                "target_total_points": sum(row[4] for row in entries),
                "current_join_credit_points": sum(
                    row[4] for row in entries if row[2] == note["message_id"]
                ),
            }
        )
    sandbox = args.output.parent / ("isolated-reward-cases-" + args.output.stem)
    if sandbox.exists():
        raise RuntimeError("isolated_ledger_already_present")
    sandbox.mkdir(mode=0o700)
    report["isolated_cases"] = calculate_cases(sandbox, roster, note, excluded)
    report["configuration_unchanged"] = (
        ROOT / "data/cmd_config.json"
    ).read_bytes() == client.original_config
    report["passed"] = (
        all(case["pass"] for case in report["isolated_cases"].values())
        and report["configuration_unchanged"]
        and all(row["current_join_credit_points"] == 0 for row in report["production"])
    )
    atomic_json(args.output, report)
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "join": report["real_evidence"],
                "production": report["production"],
                "isolated_cases": report["isolated_cases"],
                "production_writes": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
