"""Back up and inspect WSL ledgers without exposing credentials or bodies."""

import argparse
import json
import shutil
import sqlite3
import time
from pathlib import Path


def main():
    """Run an explicit local maintenance command with no upstream operations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "backup", "approve-retention"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--instance", help="Exact 64-character instance directory hash")
    parser.add_argument("--healthy-observation-confirmed", action="store_true")
    args = parser.parse_args()
    root = args.data / "platform_data" / "wangshangliao"
    if args.instance and (
        len(args.instance) != 64
        or any(c not in "0123456789abcdef" for c in args.instance)
    ):
        parser.error("Invalid instance hash")
    if args.action == "approve-retention" and not args.instance:
        parser.error("Approval requires --instance")
    if args.action == "backup":
        if not args.destination or args.destination.exists():
            parser.error("Backup destination must be a new directory")
        args.destination.mkdir(mode=0o700, parents=True)
        for config in (args.data / "cmd_config.json",):
            if config.is_file():
                shutil.copy2(config, args.destination / config.name)
                (args.destination / config.name).chmod(0o600)
        for path in root.rglob("*"):
            if not path.is_file() or path.name.endswith(("-wal", "-shm")):
                continue
            target = args.destination / "wangshangliao" / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.suffix == ".sqlite3":
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source:
                    with sqlite3.connect(target) as dest:
                        source.backup(dest)
                        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise RuntimeError("Backup integrity failed")
            else:
                shutil.copy2(path, target)
            target.chmod(0o600)
        print("Backup completed; permissions restricted; SQLite integrity verified")
        return
    for path in sorted(root.glob("*/messages.sqlite3")):
        if args.instance and path.parent.name != args.instance:
            continue
        mode = "rw" if args.action == "approve-retention" else "ro"
        with sqlite3.connect(f"file:{path}?mode={mode}", uri=True) as db:
            counts = dict(db.execute("SELECT state,COUNT(*) FROM inbox GROUP BY state"))
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            maintenance = (
                db.execute(
                    "SELECT installed_at,retention_approved FROM ledger_maintenance WHERE id=1"
                ).fetchone()
                if "ledger_maintenance" in tables
                else None
            )
            if args.action == "approve-retention":
                if (
                    not args.healthy_observation_confirmed
                    or not maintenance
                    or maintenance[0] > time.time() - 86400
                ):
                    raise SystemExit(
                        "Retention denied: migration must be 24h old and healthy observation explicitly confirmed"
                    )
                db.execute(
                    "UPDATE ledger_maintenance SET retention_approved=1 WHERE id=1"
                )
            print(
                json.dumps(
                    {
                        "instance": path.parent.name[:12],
                        "states": counts,
                        "bytes": path.stat().st_size,
                        "maintenance": maintenance,
                        "disk_free": shutil.disk_usage(path.parent).free,
                    }
                )
            )
        cards = path.with_name("cards.sqlite3")
        if cards.exists():
            with sqlite3.connect(f"file:{cards}?mode=ro", uri=True) as db:
                states = {}
                for (data,) in db.execute("SELECT data FROM card_jobs"):
                    state = json.loads(data)["state"]
                    states[state] = states.get(state, 0) + 1
                print(
                    json.dumps(
                        {"instance": path.parent.name[:12], "card_states": states}
                    )
                )


if __name__ == "__main__":
    main()
