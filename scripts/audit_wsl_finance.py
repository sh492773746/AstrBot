"""Create consistent read-only finance evidence without repairing ledger rows."""

import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FINANCIAL_FIELDS = (
    "id,group_id,member,inviter,amount,state,verification,eligible,"
    "present,ever_left,paid_at,paid_by,member_class"
)


def inspect(db):
    """List review candidates without deciding or changing their qualification.

    Args:
        db: Query-only connection to the consistent financial snapshot.

    Returns:
        Stable protected-state fingerprints and non-authoritative review items.
    """
    db.row_factory = sqlite3.Row
    entries = [
        tuple(row)
        for row in db.execute(f"SELECT {FINANCIAL_FIELDS} FROM entries ORDER BY id")
    ]
    review = []
    for row in db.execute(
        "SELECT e.id,e.group_id,e.member,e.inviter,e.state,e.verification,"
        "e.eligible,e.first_seen,e.paid_at,g.started FROM entries e "
        "JOIN groups g ON g.id=e.group_id ORDER BY e.group_id,e.id"
    ):
        reasons = []
        if row["paid_at"] and not row["inviter"]:
            reasons.append("historical_payment_without_recipient")
        if (
            row["eligible"]
            and not row["paid_at"]
            and row["first_seen"] < row["started"]
        ):
            reasons.append("debt_predates_current_collection_baseline")
        if row["verification"] == "conflict":
            reasons.append("attribution_conflict_requires_evidence_review")
        if reasons:
            review.append(
                {
                    key: row[key]
                    for key in (
                        "id",
                        "group_id",
                        "member",
                        "inviter",
                        "state",
                        "verification",
                    )
                }
                | {"reasons": reasons}
            )
    return {
        "read_only": True,
        "automatic_repairs": False,
        "qualification_decisions": False,
        "entry_count": len(entries),
        "financial_sha256": hashlib.sha256(json.dumps(entries).encode()).hexdigest(),
        "review_count": len(review),
        "review_candidates": review,
        "group_rules": [
            dict(row)
            for row in db.execute(
                "SELECT id,enabled,rate,agent_rate,qualification_seconds,"
                "require_speech,agent_only FROM groups ORDER BY id"
            )
        ],
        "limitations": [
            "Candidates are not proven errors and must not be automatically repaired.",
            "Offline membership changes may lack reliable historical evidence.",
            "This audit does not send platform requests or register payments.",
        ],
    }


def main():
    """Back up SQLite with its backup API and audit only the copied snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if output.is_relative_to(root / "data"):
        parser.error("Evidence must be outside live application data.")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = {"created_at": time.time(), "snapshots": {}}
    for folder, name in (
        ("wangshangliao_cashback", "cashback.sqlite3"),
        ("wangshangliao_open", "open-service.sqlite3"),
        ("wangshangliao_moderation", "settings.sqlite3"),
        ("wangshangliao_moderation", "group_management.sqlite3"),
    ):
        source = root / "data/plugin_data" / folder / name
        target = output / name
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live,
            closing(sqlite3.connect(target)) as backup,
        ):
            live.backup(backup)
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        target.chmod(0o600)
        evidence["snapshots"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
    config = (root / "data/cmd_config.json").read_bytes()
    config_backup = output / "cmd_config.json"
    config_backup.touch(mode=0o600)
    config_backup.write_bytes(config)
    evidence["config_sha256"] = hashlib.sha256(config).hexdigest()
    with closing(
        sqlite3.connect((output / "cashback.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.execute("PRAGMA query_only=ON")
        evidence["finance"] = inspect(db)
    with closing(
        sqlite3.connect(
            (output / "open-service.sqlite3").as_uri() + "?mode=ro", uri=True
        )
    ) as db:
        db.row_factory = sqlite3.Row
        evidence["formal_groups"] = [
            dict(row)
            for row in db.execute(
                "SELECT account,group_id,active,paused,version FROM tenants "
                "WHERE group_id IN ('1516387','1488006') ORDER BY account,group_id"
            )
        ]
    report = output / "audit.json"
    report.touch(mode=0o600)
    report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "report": str(report),
                "entries": evidence["finance"]["entry_count"],
                "financial_sha256": evidence["finance"]["financial_sha256"],
                "review_candidates": evidence["finance"]["review_count"],
                "read_only": True,
            }
        )
    )


if __name__ == "__main__":
    main()
