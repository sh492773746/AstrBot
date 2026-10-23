"""Apply a user-requested future-only reward rule with a consistent backup."""

import argparse
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import CashbackStore


def enable_agent_only(path, group, expected_version, bot, backup_root):
    """Change one saved rule without changing any existing ledger record.

    Args:
        path: Existing cashier database with the new schema already deployed.
        group: Explicit business group ID approved by the operator.
        expected_version: Previously reviewed optimistic rule version.
        bot: Current collector instance, checked against committed group settings.
        backup_root: Private directory for a transaction-consistent prechange copy.

    Returns:
        Effective timestamp, new rule version and protected row counts.

    Raises:
        ValueError: If the rule scope or version is stale or cannot pay agents.
        RuntimeError: If backup integrity or a protected ledger invariant fails.
    """
    path = Path(path).resolve(strict=True)
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            old = db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
            if (
                not old
                or old["version"] != expected_version
                or old["bot_id"] != bot
                or not old["enabled"]
                or old["agent_rate"] is None
                or old["agent_rate"] <= 0
                or old["agent_only"]
            ):
                raise ValueError("Stale, disabled, unpriced or already changed group")
            before = db.execute("SELECT * FROM entries ORDER BY id").fetchall()
            root = Path(backup_root)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = root / f"cashback-rule-{expected_version}-before.sqlite3"
            if backup.exists():
                raise ValueError("Do not overwrite an existing financial backup")
            with (
                closing(
                    sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                ) as source,
                closing(sqlite3.connect(backup)) as destination,
            ):
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Financial backup integrity failed")
            backup.chmod(0o600)
            now = time.time()
            db.execute(
                "UPDATE groups SET agent_only=1,version=version+1,rate_since=? WHERE id=?",
                (now, group),
            )
            after = dict(
                db.execute("SELECT * FROM groups WHERE id=?", (group,)).fetchone()
            )
            db.execute(
                "INSERT INTO reward_rule_history"
                "(group_id,version,effective_at,enabled,rate,bot_id,account,agent_rate,"
                "qualification_seconds,require_speech,agent_only) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                (
                    group,
                    after["version"],
                    now,
                    old["enabled"],
                    old["rate"],
                    bot,
                    old["account"],
                    old["agent_rate"],
                    old["qualification_seconds"],
                    old["require_speech"],
                ),
            )
            CashbackStore._audit(
                db,
                group,
                None,
                "maintenance:user_requested",
                "rules_changed",
                dict(old),
                after,
                "用户明确要求：仅停止之后新增普通成员返现，保留旧账，不限制入群，代理商仍由结账员核验证明。",
            )
            if before != db.execute("SELECT * FROM entries ORDER BY id").fetchall():
                raise RuntimeError("An existing ledger record changed")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Financial integrity failed")
            db.commit()
            return {
                "group": group,
                "version": after["version"],
                "effective_at": now,
                "effective_beijing": datetime.fromtimestamp(
                    now, ZoneInfo("Asia/Shanghai")
                ).isoformat(timespec="seconds"),
                "protected_records": len(before),
                "protected_paid": sum(row["state"] == "paid" for row in before),
                "backup": str(backup),
            }
        except BaseException:
            db.rollback()
            raise


def main():
    """Require exact instance and group scope from committed production settings."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "data/plugin_data"
    access = json.loads((root / "wangshangliao_cashback/access.json").read_text())
    bot = access["collector_id"]
    with closing(
        sqlite3.connect(
            (root / "wangshangliao_moderation/settings.sqlite3").as_uri() + "?mode=ro",
            uri=True,
        )
    ) as db:
        row = db.execute(
            "SELECT payload FROM policies WHERE instance=?", (bot,)
        ).fetchone()
        if not row or args.group not in json.loads(row[0])["enabled_groups"]:
            raise ValueError("Group is not in the committed cashier collector scope")
    print(
        json.dumps(
            enable_agent_only(
                root / "wangshangliao_cashback/cashback.sqlite3",
                args.group,
                args.version,
                bot,
                args.backup,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
