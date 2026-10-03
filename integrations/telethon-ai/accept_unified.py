"""Bounded non-admin private-message acceptance; never joins or posts to groups."""

import asyncio
import fcntl
import json
import os
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock
from telethon import TelegramClient


async def main():
    os.umask(0o077)
    path = Path(
        "/opt/astrbot-prod/data/plugin_data/astrbot_plugin_telethon_ai/private/accounts.json"
    )
    specs = json.loads(path.read_text())
    report = []
    for alias, spec in specs.items():
        with ExitStack() as locks:
            guard = locks.enter_context(
                (Path(spec["collector_lock_dir"]) / "collector.lock").open("a")
            )
            fcntl.flock(guard.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            locks.enter_context(
                FileLock(spec["session"] + ".session.unified.lock", timeout=0)
            )
            client = TelegramClient(
                spec["session"],
                spec["api_id"],
                spec["api_hash"],
                proxy=spec.get("proxy"),
                receive_updates=False,
                request_retries=0,
                connection_retries=1,
                flood_sleep_threshold=0,
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Session unauthorized")
                me = await client.get_me()
                assert me.id == spec["user_id"]
                bot = await client.get_entity("VIP_dhbot")
                assert bot.id == 8941933661
                for command in ("/start", "/bots", "/plans", "/create", "/tgai"):
                    sent = await client.send_message(bot, command, parse_mode=None)
                    replies = []
                    for _ in range(8 if command == "/tgai" else 20):
                        await asyncio.sleep(1)
                        replies = [
                            m.raw_text
                            for m in await client.get_messages(
                                bot, limit=10, min_id=sent.id
                            )
                            if m.sender_id == bot.id and not m.out
                        ]
                        if replies:
                            break
                    record = {"account": alias, "command": command, "replies": replies}
                    print(json.dumps(record, ensure_ascii=False), flush=True)
                    report.append(record)
                    if command == "/tgai":
                        assert not replies, "Non-admin received control data"
                    else:
                        assert replies, "Missing response"
            finally:
                await client.disconnect()
    dest = Path("/root/Projects/agents/telethon-ai-deployment/unified-acceptance.json")
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    dest.chmod(0o600)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=180))
