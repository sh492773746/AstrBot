"""Create at most one Managed Bot per explicitly confirmed acceptance account."""

import asyncio
import json
import os
import sqlite3
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock
from telethon import TelegramClient
from telethon.errors import UsernameNotOccupiedError
from telethon.tl.functions.bots import CreateBotRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest

ROOT = Path(__file__).resolve().parents[1]
DB = Path("/var/lib/astrbot-tenant-state/control.db")
CONFIG = Path("/opt/telegram-chat-collector-prod/config.json")
OWNERS = {1000000002, 1000000003}
MANAGER = 8941933661
INTENT = ROOT / "var/managed-bot-acceptance.json"


def save(data):
    temp = INTENT.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(INTENT)


def pending(owner):
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT id FROM trials WHERE owner_id=? AND bot_id IS NULL AND expires_at>?",
            (owner, int(time.time())),
        ).fetchone()


async def main():
    os.umask(0o077)
    collector = await asyncio.to_thread(
        subprocess.run,
        ["systemctl", "is-active", "--quiet", "telegram-chat-collector.service"],
    )
    if collector.returncode == 0:
        raise RuntimeError("Collector must remain stopped")
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        heartbeat = db.execute(
            "SELECT value FROM settings WHERE key='worker_heartbeat'"
        ).fetchone()
        if not heartbeat or int(heartbeat[0]) < int(time.time()) - 45:
            raise RuntimeError("Worker is not ready")
    missing = [owner for owner in sorted(OWNERS) if not pending(owner)]
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        bound = {
            r[0]
            for r in db.execute("SELECT owner_id FROM trials WHERE bot_id IS NOT NULL")
        }
    missing = [owner for owner in missing if owner not in bound]
    if missing:
        print("Awaiting real administrator confirmation for:", missing)
    if len(missing) == len(OWNERS):
        return
    config = json.loads(CONFIG.read_text())
    data = Path(config.get("data_dir", "var"))
    if not data.is_absolute():
        data = CONFIG.parent / data
    specs = [(data / "account", config["api_id"], config["api_hash"])]
    specs.extend(
        (Path(item["session"]), item["api_id"], item["api_hash"])
        for item in config["unified"]["accounts"]
    )
    report = json.loads(INTENT.read_text()) if INTENT.exists() else {}
    with ExitStack() as locks:
        locks.enter_context(FileLock(str(data / "collector.lock"), timeout=0))
        for session, *_ in specs:
            locks.enter_context(FileLock(f"{session}.session.unified.lock", timeout=0))
        for session, api_id, api_hash in specs:
            client = TelegramClient(
                str(session),
                int(api_id),
                api_hash,
                proxy=config.get("proxy"),
                receive_updates=False,
                flood_sleep_threshold=0,
                request_retries=0,
                connection_retries=1,
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Session authorization required")
                me = await client.get_me()
                if me.id not in OWNERS or me.bot:
                    raise RuntimeError("Unexpected account")
                if me.id in missing:
                    continue
                if me.id in bound:
                    print(me.id, "already bound; no creation attempted")
                    continue
                manager = await client.get_input_entity("VIP_dhbot")
                if manager.user_id != MANAGER or not pending(me.id):
                    raise RuntimeError("Manager identity or grant changed")
                item = report.get(str(me.id))
                if item is None:
                    item = {
                        "username": f"AstrTrial{str(me.id)[-5:]}{int(time.time())}Bot",
                        "state": "intent",
                        "at": int(time.time()),
                    }
                    report[str(me.id)] = item
                    save(report)
                if item["state"] != "intent":
                    print(
                        me.id, "creation already attempted; inspect without repeating"
                    )
                    continue
                try:
                    await client(ResolveUsernameRequest(item["username"]))
                except UsernameNotOccupiedError:
                    pass
                else:
                    raise RuntimeError(
                        "Chosen username already exists; manual review required"
                    )
                # Record uncertainty before the mutating API call; never retry automatically.
                item["state"] = "requested"
                save(report)
                await client(
                    CreateBotRequest(
                        name="AstrBot Community Trial",
                        username=item["username"],
                        manager_id=manager,
                        via_deeplink=True,
                    )
                )
                item["state"] = "created"
                save(report)
                print(me.id, "Managed Bot created:", item["username"])
            finally:
                await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
