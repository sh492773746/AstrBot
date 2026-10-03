"""Local login, service launch, and consistent SQLite backups."""

import argparse
import asyncio
import contextlib
import getpass
import json
import logging
import os
import secrets
import signal
import sqlite3
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import web
from filelock import FileLock
from telethon import TelegramClient, errors

from .api import create_app
from .runtime import Collector
from .store import Store


async def execute(args) -> None:
    """Run one CLI operation with exclusive session ownership.

    Args:
        args: Parsed command-line arguments.

    Raises:
        ValueError: If configuration is invalid.
    """
    if args.command == "restore":
        destination = args.output.expanduser().resolve()
        if destination.exists():
            raise ValueError("Restore destination must be new")
        with zipfile.ZipFile(args.archive) as bundle:
            config = json.loads(bundle.read("collector-config.json"))
            config["data_dir"] = "var"
            destination.mkdir(parents=True, mode=0o700)
            (destination / "var").mkdir(mode=0o700)
            (destination / "config.json").write_text(json.dumps(config, indent=2))
            (destination / "var/activity.sqlite3").write_bytes(
                bundle.read("activity.sqlite3")
            )
            with sqlite3.connect(destination / "var/activity.sqlite3") as database:
                if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Restore database integrity check failed")
            for name in bundle.namelist():
                if (
                    name
                    in (
                        "plugin-config.json",
                        "plugin-preferences.json",
                        "telegram-platform.json",
                        "config-routes.json",
                    )
                    or name.startswith("profiles/abconf_")
                    and Path(name).name == name.removeprefix("profiles/")
                ):
                    target = destination / "astrbot-restore" / name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.write_bytes(bundle.read(name))
        print(
            "Restored to new directory. Re-login locally; import AstrBot settings separately. Existing services were not changed."
        )
        return
    config_path = args.config.expanduser().resolve()
    if args.command == "setup":
        if config_path.exists():
            raise ValueError(
                "Configuration already exists; edit it instead of overwriting"
            )
        api_id = int(input("Telegram API ID: ").strip())
        api_hash = getpass.getpass("Telegram API hash: ").strip()
        if (
            api_id <= 0
            or len(api_hash) != 32
            or any(c not in "0123456789abcdefABCDEF" for c in api_hash)
        ):
            raise ValueError("Invalid developer credentials")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with config_path.open("x") as output:
            json.dump(
                {
                    "api_id": api_id,
                    "api_hash": api_hash,
                    "api_token": secrets.token_urlsafe(48),
                    "data_dir": "var",
                    "port": 6190,
                    "proxy": None,
                },
                output,
                indent=2,
            )
        config_path.chmod(0o600)
        print(
            "Private configuration created. Next run login, then run. API key stays in the local configuration."
        )
        return
    config = json.loads(config_path.read_text())
    data = Path(config.get("data_dir", "var")).expanduser()
    if not data.is_absolute():
        data = config_path.parent / data
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "maintenance":
        from .maintenance import backup_bundle

        backup_bundle(config_path, args.astrbot_root)
        print("Scheduled backup completed; newest 14 archives retained.")
        return
    if args.command == "doctor":
        print(
            "Developer credentials: "
            + (
                "configured"
                if config.get("api_id") and len(config.get("api_hash", "")) == 32
                else "missing"
            )
        )
        print(
            "API secret: "
            + ("configured" if len(config.get("api_token", "")) >= 32 else "missing")
        )
        print(
            "Login session: "
            + (
                "present (authorization checked by running service)"
                if (data / "account.session").exists()
                else "missing; run login"
            )
        )
        print(
            "Database: "
            + ("present" if (data / "activity.sqlite3").exists() else "not created yet")
        )
        return
    if args.command == "backup":
        destination = args.output.expanduser().resolve()
        if destination.exists():
            raise ValueError("Backup destination already exists")
        source_path = data / "activity.sqlite3"
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        try:
            with sqlite3.connect(destination) as target:
                source.backup(target)
        finally:
            source.close()
        print("Consistent database backup created.")
        return
    if (
        int(config["api_id"]) <= 0
        or not config["api_hash"]
        or config["api_hash"].startswith("REPLACE")
    ):
        raise ValueError("Set Telegram API credentials in the local config")
    # Login and the daemon must never open the same session concurrently.
    with FileLock(data / "collector.lock", timeout=0):
        client = TelegramClient(
            str(data / "account"),
            int(config["api_id"]),
            config["api_hash"],
            proxy=config.get("proxy"),
            flood_sleep_threshold=0,
            # A DC migration consumes an attempt before the request can succeed.
            request_retries=3,
            raise_last_call_error=True,
            connection_retries=3,
            sequential_updates=True,
        )
        if args.command == "login":
            try:
                await client.start(
                    phone=lambda: input("Phone number: "),
                    code_callback=lambda: getpass.getpass("Telegram login code: "),
                    password=lambda: getpass.getpass(
                        "Two-step verification password: "
                    ),
                )
                print("Login session saved locally.")
            finally:
                await client.disconnect()
            return
        handler = RotatingFileHandler(
            data / "collector.log", maxBytes=2 * 1024 * 1024, backupCount=5
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("collector").addHandler(handler)
        logging.getLogger("collector").setLevel(logging.INFO)
        store = Store(data / "activity.sqlite3")
        service = Collector(client, store)
        app = create_app(service, config["api_token"])
        runner = web.AppRunner(app, access_log=None)
        task = None
        try:
            await runner.setup()
            await web.TCPSite(
                runner, "127.0.0.1", int(config.get("port", 6190))
            ).start()
            task = asyncio.create_task(service.run())
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)
            print(
                "Collector API listening on loopback; use /v1/status for health.",
                flush=True,
            )
            await stop.wait()
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await client.disconnect()
            await runner.cleanup()
            store.db.close()


def main() -> None:
    """Parse CLI arguments and keep credentials out of error output."""
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("output", type=Path)
    commands.add_parser("setup")
    commands.add_parser("doctor")
    maintenance = commands.add_parser("maintenance")
    maintenance.add_argument("--astrbot-root", type=Path)
    commands.add_parser("login")
    commands.add_parser("run")
    backup = commands.add_parser("backup")
    backup.add_argument("output", type=Path)
    args = parser.parse_args()
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        hints = {
            "ApiIdInvalidError": "Telegram rejected api_id/api_hash. Check the developer credentials.",
            "PhoneNumberInvalidError": "Use an international phone number including its country code.",
            "PhoneNumberBannedError": "Telegram rejected this phone number as banned.",
            "PhoneNumberFloodError": "Too many login attempts. Wait before requesting another code.",
            "PhoneCodeInvalidError": "The login code is invalid. Retry with the latest code.",
            "PhoneCodeExpiredError": "The login code expired. Restart the login command.",
            "PasswordHashInvalidError": "The two-step verification password is incorrect.",
            "PhoneMigrateError": "Telegram data-center migration did not complete. Retry login later.",
            "NetworkMigrateError": "Telegram data-center migration did not complete. Retry login later.",
            "UserMigrateError": "Telegram data-center migration did not complete. Retry login later.",
            "Timeout": "Another process owns this session. Stop the collector before logging in.",
            "TimeoutError": "Telegram connection timed out. Check the network or proxy.",
            "ConnectionError": "Unable to connect to Telegram. Check the network or proxy.",
            "FileNotFoundError": "Configuration or database file is missing. Check --config and data_dir.",
            "JSONDecodeError": "The configuration file is not valid JSON.",
            "ValueError": "Invalid configuration or input. Check api_id, api_hash, proxy and phone format.",
        }
        hint = hints.get(
            type(exc).__name__, "Check configuration, network and account state."
        )
        if isinstance(exc, errors.FloodWaitError):
            hint = f"Telegram requires a wait of {exc.seconds} seconds before retrying."
        parser.exit(1, f"Operation failed ({type(exc).__name__}). {hint}\n")


if __name__ == "__main__":
    main()
