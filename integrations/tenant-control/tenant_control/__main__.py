"""Infrastructure commands; the control bot itself runs only as an AstrBot plugin."""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet

from .store import Store


def main() -> None:
    """Launch an explicitly configured role without touching production AstrBot."""
    parser = argparse.ArgumentParser(description="AstrBot tenant control")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(
            os.getenv(
                "CONTROL_DB_PATH",
                str(Path(__file__).resolve().parent.parent / "var/control.db"),
            )
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("build-plugin")
    bundle.add_argument("output", type=Path)
    commands.add_parser("worker")
    commands.add_parser("gateway")
    commands.add_parser("doctor")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("bot_id", type=int)
    backup = commands.add_parser("backup")
    backup.add_argument("bot_id", type=int)
    extension = commands.add_parser("install-extension")
    extension.add_argument("bot_id", type=int)
    extension.add_argument("order_id")
    extension.add_argument("archive", type=Path)
    stage = commands.add_parser("stage-extension")
    stage.add_argument("bot_id", type=int)
    stage.add_argument("order_id")
    stage.add_argument("archive", type=Path)
    approve = commands.add_parser("approve-extension")
    approve.add_argument("bot_id", type=int)
    approve.add_argument("order_id")
    approve.add_argument("archive", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("bot_id", type=int)
    restore.add_argument("snapshot", help="Numeric directory name under tenant/backups")
    upgrade = commands.add_parser("upgrade-plugin")
    upgrade.add_argument("bot_id", type=int)
    upgrade.add_argument("--custom-approved", action="store_true")
    args = parser.parse_args()
    if args.command == "build-plugin":
        from .bundle import build_plugin

        print(build_plugin(args.output))
        return
    db = Store(args.db)
    try:
        if args.command == "doctor":
            for tier in ("first", "month", "year"):
                print(f"{tier}: {'configured' if db.price(tier) else 'disabled'}")
            print("controller: AstrBot plugin; token is owned by its Telegram platform")
            print(
                f"shared DB: {'explicit' if os.getenv('CONTROL_DB_PATH') else 'CLI default'}"
            )
            print(
                f"encryption: {'configured' if os.getenv('CONTROL_FERNET_KEY') else 'missing'}"
            )
            print(
                "deployment: "
                + (
                    "configured"
                    if all(
                        os.getenv(name)
                        for name in (
                            "TENANT_ROOT",
                            "ASTRBOT_IMAGE",
                            "TENANT_PROVIDER_TEMPLATE",
                        )
                    )
                    else "disabled"
                )
            )
            return
        if args.command == "gateway":
            from .gateway import run_gateway

            run_gateway(args.db)
            return
        key = os.getenv("CONTROL_FERNET_KEY", "")
        credential_dir = os.getenv("CREDENTIALS_DIRECTORY")
        if credential_dir and (Path(credential_dir) / "worker.json").is_file():
            key = json.loads((Path(credential_dir) / "worker.json").read_text())[
                "CONTROL_FERNET_KEY"
            ]
        if not key:
            parser.error("Set CONTROL_FERNET_KEY in a private environment file")
        cipher = Fernet(key.encode())
        from .worker import Worker

        values = {
            name: os.getenv(name, "")
            for name in ("TENANT_ROOT", "ASTRBOT_IMAGE", "TENANT_PROVIDER_TEMPLATE")
        }
        if not all(values.values()):
            parser.error(
                "TENANT_ROOT, ASTRBOT_IMAGE and TENANT_PROVIDER_TEMPLATE required"
            )
        worker = Worker(
            db,
            cipher,
            Path(values["TENANT_ROOT"]),
            Path(os.getenv("ASTRBOT_SOURCE_ROOT", "/opt/astrbot-prod")),
            values["ASTRBOT_IMAGE"],
            Path(values["TENANT_PROVIDER_TEMPLATE"]),
        )
        if args.command == "reconcile":
            worker.reconcile(args.bot_id)
        elif args.command == "backup":
            print(worker.backup(args.bot_id))
        elif args.command == "install-extension":
            worker.install_extension(args.bot_id, args.order_id, args.archive)
        elif args.command == "stage-extension":
            print(worker.stage_extension(args.bot_id, args.order_id, args.archive))
        elif args.command == "approve-extension":
            worker.approve_extension(args.bot_id, args.order_id, args.archive)
        elif args.command == "restore":
            worker.restore(args.bot_id, args.snapshot)
        elif args.command == "upgrade-plugin":
            print(worker.upgrade_plugin(args.bot_id, args.custom_approved))
        else:
            with db.tx() as tx:
                tx.execute(
                    "UPDATE jobs SET state='needs_review',error='Worker interrupted' "
                    "WHERE state='running'"
                )
                tx.execute(
                    "UPDATE operations SET state='needs_review',result='Worker interrupted' "
                    "WHERE state='running'"
                )
            while True:
                if worker.proxy_ready():
                    db.db.execute(
                        "INSERT INTO settings(key,value) VALUES('worker_heartbeat',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(int(time.time())),),
                    )
                if not worker.run_once():
                    time.sleep(10)
    except (ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Control operation failed: {type(error).__name__}\n")
    finally:
        db.close()


if __name__ == "__main__":
    main()
