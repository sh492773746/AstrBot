"""Operator-only interactive account login; never request login codes in a Bot."""

import argparse
import asyncio
import getpass
import json
import os
import re

from filelock import FileLock
from telethon import TelegramClient

from . import registry


async def main(alias):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", alias):
        raise ValueError("Use an ASCII account alias")
    os.umask(0o077)
    root = registry.private_root()
    with FileLock(str(root / "registry.lock"), timeout=0):
        records = registry.load()
        if alias in records:
            raise ValueError("Account already registered; no overwrite")
        session = root / ("session_" + alias)
        if session.with_suffix(".session").exists():
            raise ValueError("Session already exists; operator recovery required")
        api_id = int((await asyncio.to_thread(input, "Telegram API ID: ")).strip())
        api_hash = getpass.getpass("Telegram API hash: ").strip()
        phone = getpass.getpass("Account phone (+country code): ").strip()
        client = TelegramClient(
            str(session),
            api_id,
            api_hash,
            receive_updates=False,
            flood_sleep_threshold=0,
        )
        try:
            await client.start(
                phone=phone,
                code_callback=lambda: getpass.getpass("Telegram login code: "),
                password=lambda: getpass.getpass("Telegram 2FA password: "),
            )
            me = await client.get_me()
            if me.bot or any(str(r["user_id"]) == str(me.id) for r in records.values()):
                raise ValueError("Expected an unregistered user account")
            records[alias] = {
                "user_id": me.id,
                "session": str(session),
                "api_id": api_id,
                "api_hash": api_hash,
            }
            target = root / "accounts.json"
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(records, indent=2))
            temp.chmod(0o600)
            temp.replace(target)
            print(
                "Account registered. Reload the plugin; its platform remains disabled."
            )
        finally:
            await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("alias")
    args = parser.parse_args()
    asyncio.run(main(args.alias))
