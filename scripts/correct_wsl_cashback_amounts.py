"""Audit and back up an explicit user-requested correction to five yuan."""

import argparse
import json
import sqlite3
import tempfile
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path

from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    REQUEST_TRACE,
    CashbackStore,
)

TARGET = 500
ACTOR = "maintenance:user_requested"
REASON = (
    "用户更正：之前6元设置有误，已有邀请返现账目统一按每人5元。"
    "保留结账状态和邀请归属；本操作不是补付款或退款。"
)


def correct_amounts(path, groups, *, apply=False, backup_root=None):
    path = Path(path).resolve(strict=True)
    groups = sorted(set(groups))
    if not groups:
        raise ValueError("Explicit group IDs are required")
    if apply and backup_root is None:
        raise ValueError("A backup directory is required before applying changes")
    placeholders = ",".join("?" for _ in groups)
    with closing(
        sqlite3.connect(
            f"{path.as_uri()}?mode={'rw' if apply else 'ro'}",
            uri=True,
            timeout=10,
        )
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            rules = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM groups WHERE id IN ({placeholders}) ORDER BY id",
                    groups,
                )
            ]
            if {rule["id"] for rule in rules} != set(groups):
                raise ValueError("Unknown group ID; no corrections applied")
            original = {
                row["id"]: dict(row)
                for row in db.execute(
                    f"SELECT * FROM entries WHERE group_id IN ({placeholders})",
                    groups,
                )
            }
            entries = [
                row
                for row in original.values()
                if row["eligible"]
                and not row["self_join"]
                and row["amount"] > 0
                and row["amount"] != TARGET
            ]
            histories = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM reward_rule_history WHERE group_id IN ({placeholders}) "
                    "AND rate<>? ORDER BY group_id,version",
                    [*groups, TARGET],
                )
            ]
            changed_rules = [rule for rule in rules if rule["rate"] != TARGET]
            result = {
                "applied": False,
                "groups": groups,
                "target_cents": TARGET,
                "entries_changed": len(entries),
                "states": dict(Counter(row["state"] for row in entries)),
                "previous_amounts": dict(Counter(row["amount"] for row in entries)),
                "historical_rules_changed": len(histories),
                "current_rules_changed": len(changed_rules),
                "backup": None,
            }
            if not apply or not (entries or histories or changed_rules):
                db.rollback()
                return result

            # Hold the writer lock while a separate connection snapshots the
            # committed state, so the backup and corrections share one baseline.
            root = Path(backup_root)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup = (
                Path(tempfile.mkdtemp(prefix="wsl-five-yuan-", dir=root))
                / "cashback.sqlite3"
            )
            with (
                closing(
                    sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
                ) as source,
                closing(sqlite3.connect(backup)) as destination,
            ):
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
            backup.chmod(0o600)
            result["backup"] = str(backup)
            trace = REQUEST_TRACE.set(uuid.uuid4().hex)
            try:
                for row in entries:
                    db.execute(
                        "UPDATE entries SET amount=?,version=version+1 WHERE id=?",
                        (TARGET, row["id"]),
                    )
                    CashbackStore._audit(
                        db,
                        row["group_id"],
                        row["id"],
                        ACTOR,
                        "amount_corrected",
                        {
                            "amount": row["amount"],
                            "state": row["state"],
                            "version": row["version"],
                        },
                        {
                            "amount": TARGET,
                            "state": row["state"],
                            "version": row["version"] + 1,
                        },
                        REASON,
                    )
                for rule in changed_rules:
                    db.execute(
                        "UPDATE groups SET rate=?,version=version+1 WHERE id=?",
                        (TARGET, rule["id"]),
                    )
                for history in histories:
                    db.execute(
                        "UPDATE reward_rule_history SET rate=? WHERE group_id=? AND version=?",
                        (TARGET, history["group_id"], history["version"]),
                    )
                for rule in rules:
                    changed_history = [
                        row for row in histories if row["group_id"] == rule["id"]
                    ]
                    if rule not in changed_rules and not changed_history:
                        continue
                    before = {
                        "rate": rule["rate"],
                        "version": rule["version"],
                        "history": [
                            {
                                key: row[key]
                                for key in (
                                    "version",
                                    "rate",
                                    "agent_rate",
                                    "effective_at",
                                )
                            }
                            for row in changed_history
                        ],
                    }
                    after = {
                        **before,
                        "rate": TARGET,
                        "version": rule["version"] + int(rule in changed_rules),
                        "history": [
                            {**row, "rate": TARGET} for row in before["history"]
                        ],
                    }
                    CashbackStore._audit(
                        db,
                        rule["id"],
                        None,
                        ACTOR,
                        "rules_changed",
                        before,
                        after,
                        REASON,
                    )

                changed_ids = {row["id"] for row in entries}
                for row in db.execute(
                    f"SELECT * FROM entries WHERE group_id IN ({placeholders})",
                    groups,
                ):
                    expected = original.pop(row["id"])
                    if row["id"] in changed_ids:
                        expected = {
                            **expected,
                            "amount": TARGET,
                            "version": expected["version"] + 1,
                        }
                    if dict(row) != expected:
                        raise RuntimeError("A non-monetary ledger field changed")
                if original:
                    raise RuntimeError("A ledger record was removed")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Ledger integrity check failed")
                db.commit()
            finally:
                REQUEST_TRACE.reset(trace)
            result["applied"] = True
            return result
        except BaseException:
            db.rollback()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--group", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()
    result = correct_amounts(
        args.database,
        args.group,
        apply=args.apply,
        backup_root=args.backup_root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
