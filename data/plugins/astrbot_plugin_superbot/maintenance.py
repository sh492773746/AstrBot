"""Safe database maintenance; writes require explicit apply and a fresh backup."""

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def cleanup(db, now, *, apply=False, limit=500):
    """Preview or prune one bounded batch of disposable records.

    Args:
        db: SQLite connection; the caller owns the write transaction.
        now: Fixed cutoff timestamp for this maintenance run.
        apply: Execute changes only when explicitly enabled.
        limit: Maximum affected rows per table, from one through five hundred.

    Returns:
        Per-table eligible and affected counts without message bodies or secrets.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Cleanup batch must be between 1 and 500")
    tables = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    day = (
        datetime.fromtimestamp(now - 7 * 86400, ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    rules = [
        ("callbacks", "expires<?", now, None),
        ("dialogs", "expires<?", now, None),
        ("group_chat_seen", "day<?", day, None),
        ("ak_seen", "at<?", now - 7 * 86400, None),
        ("cm_tickets", "expires<?", now - 86400, None),
        ("keno_poll_runs", "finished<?", now - 7 * 86400, None),
        ("gt_delivery_timing", "sent_at<?", now - 30 * 86400, None),
    ]
    if {"game_panels", "gt_delete"} <= tables:
        rules.append(
            (
                "game_panels",
                "status='done' AND created<? AND rendered<>'' AND EXISTS("
                "SELECT 1 FROM gt_delete d WHERE d.chat=game_panels.chat "
                "AND d.message=game_panels.message AND d.status IN ('deleted','archived'))",
                now - 7 * 86400,
                "rendered=''",
            )
        )
    result = {}
    for table, predicate, cutoff, assignment in rules:
        if table not in tables:
            continue
        eligible = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {predicate}", (cutoff,)
        ).fetchone()[0]
        affected = 0
        if apply and eligible:
            statement = (
                f"UPDATE {table} SET {assignment}"
                if assignment
                else f"DELETE FROM {table}"
            )
            affected = db.execute(
                statement + f" WHERE rowid IN (SELECT rowid FROM {table} "
                f"WHERE {predicate} LIMIT ?)",
                (cutoff, limit),
            ).rowcount
        result[table] = {"eligible": eligible, "affected": affected}
    return result


def main():
    """Maintain an explicitly selected database without Telegram or payment access."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["status", "backup", "verify", "cleanup", "optimize"]
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = args.database.resolve(strict=True)
    if args.apply and args.action not in {"cleanup", "optimize"}:
        parser.error("--apply is valid only for cleanup or optimize")
    if args.action in {"cleanup", "optimize"}:
        mode = "rw" if args.apply else "ro"
        with sqlite3.connect(
            path.as_uri() + f"?mode={mode}", uri=True, timeout=5
        ) as db:
            db.execute("PRAGMA foreign_keys=ON")
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"audit", "settings", "wallets", "ledger", "bets", "ads"} <= tables:
                raise SystemExit(
                    "Not a recognized Superbot database; maintenance refused"
                )
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SystemExit("Integrity check failed; maintenance refused")
            if args.apply:
                target = args.destination
                if target is None or target.exists():
                    raise SystemExit(
                        "Provide a new explicit backup path with --destination"
                    )
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.touch(mode=0o600, exist_ok=False)
                with sqlite3.connect(target) as backup:
                    db.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise SystemExit("Backup integrity check failed")
            now = time.time()
            result = {"action": args.action, "applied": args.apply, "at": now}
            if args.action == "cleanup":
                totals = {}
                for _ in range(20 if args.apply else 1):
                    with db:
                        batch = cleanup(db, now, apply=args.apply)
                        changed = {
                            table: counts["affected"]
                            for table, counts in batch.items()
                            if counts["affected"]
                        }
                        if args.apply and changed:
                            db.execute(
                                "INSERT INTO audit(at,actor,action,data) VALUES(?,?,?,?)",
                                (
                                    now,
                                    "maintenance",
                                    "transient_cleanup",
                                    json.dumps(changed),
                                ),
                            )
                    for table, counts in batch.items():
                        totals[table] = totals.get(table, 0) + counts["affected"]
                    if not args.apply or not any(c["affected"] for c in batch.values()):
                        break
                result.update(affected=totals, remaining=cleanup(db, now))
            elif args.apply:
                db.execute("PRAGMA analysis_limit=1000")
                db.execute("PRAGMA optimize=0x10002")
                result["checkpoint"] = list(
                    db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                )
            result["integrity"] = db.execute("PRAGMA quick_check").fetchone()[0]
            result["page_count"] = db.execute("PRAGMA page_count").fetchone()[0]
            result["free_pages"] = db.execute("PRAGMA freelist_count").fetchone()[0]
            print(json.dumps(result, ensure_ascii=False))
        return
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if args.action == "backup":
            target = args.destination
            if target is None or target.exists():
                raise SystemExit("Provide a new explicit backup path")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.touch(mode=0o600, exist_ok=False)
            with sqlite3.connect(target) as backup:
                db.backup(backup)
                assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            print("Consistent backup completed and verified")
            return
        result = {
            "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
            "bytes": path.stat().st_size,
        }
        for table in ("ads", "bets", "chases", "notices"):
            result[table] = dict(
                db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status")
            )
        scoped = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_wallets'"
        ).fetchone()
        result["ledger_rows"] = db.execute(
            "SELECT count(*) FROM group_ledger"
            if scoped
            else "SELECT count(*) FROM ledger"
        ).fetchone()[0]
        result["wallet_mismatches"] = db.execute(
            "SELECT COUNT(*) FROM group_wallets w WHERE balance<>(SELECT COALESCE(SUM(delta),0) FROM group_ledger l WHERE l.uid=w.uid AND l.chat=w.chat)"
            if scoped
            else "SELECT COUNT(*) FROM wallets w WHERE balance<>(SELECT COALESCE(SUM(delta),0) FROM ledger l WHERE l.uid=w.uid)"
        ).fetchone()[0]
        result["conflicting_draws"] = db.execute(
            "SELECT count(*) FROM draws WHERE conflict=1"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in ("gg_dispatch", "gg_round_notices", "game_backfill"):
            if table in tables:
                result[table] = dict(
                    db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status")
                )
        print(json.dumps(result, ensure_ascii=False))
        if result["integrity"] != "ok" or result["wallet_mismatches"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
