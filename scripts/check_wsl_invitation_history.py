"""Read-only checks of join operators, business recruiters and cash transitions."""

import argparse
import json
import sqlite3
import unicodedata
from contextlib import ExitStack, closing
from pathlib import Path


def check_history(cashbook, inboxes, group):
    """Cross-check original routing and audits without enabling or paying anything.

    Args:
        cashbook: Production or fixture cash ledger path, opened read-only.
        inboxes: Available native message ledger paths.
        group: Exact business group ID.

    Returns:
        Counts of independently supported transitions and explicit remaining gates.

    Raises:
        AssertionError: If recorded financial or identity evidence is inconsistent.
    """
    with ExitStack() as stack:
        db = stack.enter_context(
            closing(
                sqlite3.connect(
                    Path(cashbook).resolve().as_uri() + "?mode=ro", uri=True
                )
            )
        )
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rule = db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
        assert rule is not None, "Group has no cash ledger"
        entries = db.execute(
            "SELECT * FROM entries WHERE group_id=? AND eligible=1 ORDER BY id",
            (group,),
        ).fetchall()
        notices = {}
        for path in inboxes:
            inbox = stack.enter_context(
                closing(
                    sqlite3.connect(
                        Path(path).resolve().as_uri() + "?mode=ro", uri=True
                    )
                )
            )
            inbox.row_factory = sqlite3.Row
            inbox.execute("BEGIN")
            columns = {row[1] for row in inbox.execute("PRAGMA table_info(inbox)")}
            if not {"account", "team", "message", "payload", "state"} <= columns:
                continue
            for row in inbox.execute(
                "SELECT account,message,payload FROM inbox WHERE team=? "
                "AND state='invite_notification'",
                (group,),
            ):
                key = (row["account"], row["message"])
                payload = json.loads(row["payload"])
                assert key not in notices or notices[key] == payload, (
                    "Conflicting original invitation envelopes"
                )
                notices[key] = payload
        result = {
            "group": group,
            "verified_native_invitations": 0,
            "verified_business_invitations": 0,
            "registered_attribution_unverified": 0,
            "departures_voided": 0,
            "rejoins_still_void": 0,
            "pending_count": 0,
            "pending_cents": 0,
            "verifying_count": 0,
            "verifying_cents": 0,
            "registered_count": 0,
            "audited_amount_corrections": 0,
            "recorded_amount_corrections": db.execute(
                "SELECT COUNT(*) FROM audit WHERE group_id=? AND action='amount_corrected'",
                (group,),
            ).fetchone()[0],
        }
        for entry in entries:
            native_events = db.execute(
                "SELECT * FROM evidence WHERE group_id=? AND member=? "
                "AND source='nim_notification' ORDER BY observed,event_id",
                (group, entry["member"]),
            ).fetchall()
            native_joins = []
            for event in native_events:
                payload = notices.get((rule["account"], event["event_id"]))
                if payload is None:
                    continue
                assert payload.get("kind") == "team_invite"
                assert entry["peer"] in payload.get("member_peers", [])
                if event["inviter_peer"]:
                    assert payload.get("inviter_peer") == event["inviter_peer"]
                stamp = payload.get("event_at")
                assert type(stamp) in (int, float) and stamp > 0
                stamp = stamp / 1000 if stamp > 100000000000 else stamp
                assert rule["started"] <= stamp <= entry["last_seen"]
                native_joins.append(event["event_id"])
            if native_joins:
                # This proves joining, not who recruited the member.
                result["verified_native_invitations"] += 1
            events = db.execute(
                "SELECT * FROM evidence WHERE group_id=? AND member=? "
                "AND source='business_apply_log' AND account=? "
                "AND inviter=? AND member_peer=? AND inviter_peer=? "
                "AND event_at>=? ORDER BY event_at,event_id",
                (
                    group,
                    entry["member"],
                    rule["account"],
                    entry["inviter"],
                    entry["peer"],
                    entry["inviter_peer"],
                    rule["started"],
                ),
            ).fetchall()
            audits = [
                {
                    **dict(row),
                    "before": json.loads(row["before_json"]),
                    "after": json.loads(row["after_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM audit WHERE group_id=? AND entry_id=? ORDER BY id",
                    (group, entry["id"]),
                )
            ]
            supported = []
            invitation_amount = None
            for event in events:
                assert event["inviter"] == entry["inviter"]
                stamp = event["event_at"]
                assert type(stamp) in (int, float) and stamp > 0
                assert rule["started"] <= stamp <= entry["last_seen"]
                if not supported:
                    historical = db.execute(
                        "SELECT * FROM reward_rule_history WHERE group_id=? "
                        "AND effective_at<=? ORDER BY effective_at DESC,version DESC LIMIT 1",
                        (group, stamp),
                    ).fetchone()
                    assert historical and historical["enabled"]
                    assert historical["account"] == rule["account"]
                    original = next(
                        (
                            audit
                            for audit in audits
                            if audit["action"] == "invitation_verified"
                            and audit["after"].get("inviter") == event["inviter"]
                            and audit["after"].get("source") == "business_apply_log"
                        ),
                        None,
                    )
                    assert original, (
                        "Business evidence has no matching attribution audit"
                    )
                    category = original["after"].get(
                        "member_class", original["after"].get("inviter_class", "legacy")
                    )
                    name = ""
                    digits = 0
                    invitation_amount = historical["rate"]
                    if "member_class" in original["after"]:
                        if category == "agent":
                            invitation_amount = historical["agent_rate"] or 0
                        elif category == "unclassified":
                            invitation_amount = 0
                    elif category != "legacy":
                        name = original["after"]["inviter_name_snapshot"]
                        digits = sum(
                            c.isdecimal() for c in unicodedata.normalize("NFKC", name)
                        )
                        assert original["after"]["name_digit_count"] == digits
                        assert category == (
                            "agent"
                            if digits >= 6
                            else "ordinary"
                            if name
                            else "unclassified"
                        )
                        if category == "agent":
                            invitation_amount = historical["agent_rate"] or 0
                        elif category == "unclassified":
                            invitation_amount = 0
                    price_original = original
                    prior_correction = next(
                        (
                            audit
                            for audit in audits
                            if audit["action"] == "amount_corrected"
                            and audit["id"] < original["id"]
                        ),
                        None,
                    )
                    if category == "legacy" and prior_correction:
                        # A recipient repair must not undo an older audited amount.
                        invitation_amount = prior_correction["after"]["amount"]
                        result["audited_amount_corrections"] += 1
                    assert price_original["after"].get("amount") == invitation_amount
                    expected_amount = invitation_amount
                    amount_at_assignment = invitation_amount
                    for correction in audits:
                        if (
                            correction["action"]
                            == "member_avatar_classification_recovered"
                        ):
                            before, after = correction["before"], correction["after"]
                            assert before["member_class"] in {"legacy", "unclassified"}
                            assert correction["id"] > original["id"]
                            category = after["member_class"]
                            expected_amount = after["amount"]
                            assert category in {"agent", "ordinary"}
                            assert after["rule_version"] == historical["version"]
                            continue
                        if correction["action"] == "inviter_classification_recovered":
                            before, after = correction["before"], correction["after"]
                            assert category == "unclassified" and not name
                            assert before == {
                                "inviter_class": "unclassified",
                                "inviter_name_snapshot": "",
                                "amount": 0,
                            }
                            assert correction["id"] > original["id"]
                            assert after["rule_version"] == historical["version"]
                            name = after["inviter_name_snapshot"]
                            assert name
                            digits = sum(
                                c.isdecimal()
                                for c in unicodedata.normalize("NFKC", name)
                            )
                            category = "agent" if digits >= 6 else "ordinary"
                            assert after["inviter_class"] == category
                            assert after["name_digit_count"] == digits
                            expected_amount = (
                                historical["agent_rate"] or 0
                                if category == "agent"
                                else historical["rate"]
                            )
                            assert after["amount"] == expected_amount
                            continue
                        if correction["action"] == "agent_price_confirmed":
                            before, after = correction["before"], correction["after"]
                            price = db.execute(
                                "SELECT agent_rate FROM reward_rule_history "
                                "WHERE group_id=? AND version=?",
                                (group, after["rule_version"]),
                            ).fetchone()
                            assert (
                                category == "agent"
                                and before["amount"] == expected_amount == 0
                            )
                            assert price and price["agent_rate"] == after["amount"] > 0
                            assert correction["id"] > original["id"]
                            expected_amount = after["amount"]
                            continue
                        if correction["action"] != "amount_corrected":
                            continue
                        if correction["id"] <= original["id"]:
                            continue
                        before, after = correction["before"], correction["after"]
                        assert (
                            correction["id"] > price_original["id"]
                            and correction["created"] >= price_original["created"]
                            and correction["actor"] == "system"
                            and len(correction["reason"].strip()) >= 2
                            and len(correction["trace"]) == 32
                            and all(
                                character in "0123456789abcdef"
                                for character in correction["trace"]
                            )
                        ), "Amount correction has no attributable maintenance audit"
                        assert {
                            "member",
                            "amount",
                            "state",
                            "paid_at",
                            "paid_by",
                            "version",
                        } <= before.keys() & after.keys()
                        assert (
                            before["member"] == entry["member"]
                            and type(before["amount"]) is int
                            and before["amount"] == expected_amount
                            and before["state"] == "pending"
                            and before["paid_at"] is None
                            and before["paid_by"] is None
                            and type(before["version"]) is int
                            and type(after["version"]) is int
                            and 1
                            <= before["version"]
                            < after["version"]
                            <= entry["version"]
                            and after["version"] == before["version"] + 1
                            and type(after["amount"]) is int
                            and after["amount"] > 0
                            and after
                            == {
                                **before,
                                "amount": after["amount"],
                                "version": after["version"],
                            }
                        ), (
                            "Amount correction changes identity/payment or breaks its chain"
                        )
                        expected_amount = after["amount"]
                        if correction["id"] < original["id"]:
                            amount_at_assignment = expected_amount
                        result["audited_amount_corrections"] += 1
                    assert original["after"].get("amount") == amount_at_assignment
                    assert entry["amount"] == expected_amount, (
                        "Stored amount differs from the proven invitation-time price "
                        "and audited corrections"
                    )
                    if "member_class" in original["after"]:
                        assert entry["member_class"] == category
                    elif category != "legacy":
                        assert entry["inviter_class"] == category
                        assert entry["inviter_name_snapshot"] == name
                        assert entry["name_digit_count"] == digits
                assert any(
                    audit["action"] == "invitation_verified"
                    and audit["after"].get("inviter") == event["inviter"]
                    and audit["after"].get("amount") == amount_at_assignment
                    and audit["after"].get("source") == "business_apply_log"
                    for audit in audits
                ), "Business evidence has no matching attribution audit"
                supported.append(event["event_id"])
            if supported:
                result["verified_business_invitations"] += 1
            if native_joins and any(
                audit["action"] in {"member_left", "repeated_join"} for audit in audits
            ):
                assert entry["ever_left"]
                if entry["paid_at"] is None:
                    assert entry["state"] == "void"
                    result["departures_voided"] += 1
                    if (
                        entry["present"]
                        and len(native_joins) >= 2
                        and any(
                            audit["action"] in {"member_rejoined", "repeated_join"}
                            for audit in audits
                        )
                    ):
                        result["rejoins_still_void"] += 1
            if entry["state"] == "pending":
                assert entry["paid_at"] is None and entry["paid_by"] is None
                if (
                    entry["member_class"] != "legacy"
                    and entry["verification"] == "verified"
                    and entry["hold_until"] > 0
                ):
                    assert entry["pricing_status"] == "priced"
                    assert any(
                        audit["action"] == "qualification_completed"
                        and audit["before"].get("hold_until") == entry["hold_until"]
                        and audit["after"].get("observed_at", 0) >= entry["hold_until"]
                        for audit in audits
                    ), "Pending entry has no supported three-hour qualification"
                result["pending_count"] += 1
                result["pending_cents"] += entry["amount"]
            elif entry["state"] == "verifying":
                assert entry["paid_at"] is None and entry["paid_by"] is None
                result["verifying_count"] += 1
                result["verifying_cents"] += entry["amount"]
            elif entry["state"] == "paid":
                assert entry["paid_at"] is not None and entry["paid_by"]
                result["registered_count"] += 1
                if not supported:
                    result["registered_attribution_unverified"] += 1
        result["remaining"] = [
            name
            for name, count in (
                (
                    "business_invitation_attribution",
                    result["verified_business_invitations"],
                ),
                ("live_departure_voiding", result["departures_voided"]),
                ("live_rejoin_no_second_reward", result["rejoins_still_void"]),
            )
            if not count
        ]
        # Paid database rows alone do not prove an authenticated browser workflow.
        result["remaining"].append("access_already_settled_registration")
        if result["registered_attribution_unverified"]:
            result["remaining"].append("historical_paid_recipient_review")
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--group", required=True)
    args = parser.parse_args()
    if not args.group.isascii() or not args.group.isdigit() or int(args.group) <= 0:
        parser.error("--group must be an exact positive business ID")
    print(
        json.dumps(
            check_history(
                args.data / "plugin_data/wangshangliao_cashback/cashback.sqlite3",
                sorted(
                    (args.data / "platform_data/wangshangliao").glob(
                        "*/messages.sqlite3"
                    )
                ),
                args.group,
            ),
            indent=2,
        )
    )
