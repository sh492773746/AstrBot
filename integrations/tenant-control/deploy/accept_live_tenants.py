"""Bounded private-message acceptance of real owned tenant Bots."""

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


async def main():
    os.umask(0o077)
    collector = await asyncio.to_thread(
        subprocess.run,
        ["systemctl", "is-active", "--quiet", "telegram-chat-collector.service"],
    )
    if collector.returncode == 0:
        raise RuntimeError("Collector must remain stopped")
    config_path = Path("/opt/telegram-chat-collector-prod/config.json")
    config = json.loads(config_path.read_text())
    data = Path(config.get("data_dir", "var"))
    if not data.is_absolute():
        data = config_path.parent / data
    specs = [(data / "account", config["api_id"], config["api_hash"])]
    specs += [
        (Path(x["session"]), x["api_id"], x["api_hash"])
        for x in config["unified"]["accounts"]
    ]
    with sqlite3.connect(
        "file:/var/lib/astrbot-tenant-state/control.db?mode=ro", uri=True
    ) as db:
        bots = db.execute(
            "SELECT b.id,b.owner_id,b.username,b.status FROM bots b "
            "JOIN trials t ON t.bot_id=b.id "
            "WHERE b.owner_id IN (1000000002,1000000003) AND b.status='active'"
        ).fetchall()
    results = []
    with ExitStack() as locks:
        locks.enter_context(FileLock(str(data / "collector.lock"), timeout=0))
        for session, *_ in specs:
            locks.enter_context(FileLock(f"{session}.session.unified.lock", timeout=0))
        for session, api_id, api_hash in specs:
            client = TelegramClient(
                str(session),
                api_id,
                api_hash,
                proxy=config.get("proxy"),
                receive_updates=False,
                flood_sleep_threshold=0,
                request_retries=1,
                connection_retries=1,
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Session unauthorized")
                me = await client.get_me()
                for bot_id, owner, username, status in bots:
                    peer = await client.get_entity(username)
                    if peer.id != bot_id:
                        raise RuntimeError("Bot identity mismatch")
                    report = {
                        "bot_id": bot_id,
                        "owner": owner,
                        "actor": me.id,
                        "is_owner": owner == me.id,
                        "checks": [],
                    }
                    commands = (
                        ("/start", "/uid", "/chat", "请只回复：验收成功", "/exit")
                        if owner == me.id
                        else ("/start", "⚙️ 管理")
                    )
                    for command in commands:
                        sent = await client.send_message(peer, command, parse_mode=None)
                        responses = []
                        for _ in range(40):
                            messages = await client.get_messages(
                                peer, limit=8, min_id=sent.id
                            )
                            responses = [
                                m
                                for m in messages
                                if m.sender_id == bot_id and not m.out
                            ]
                            if responses:
                                await asyncio.sleep(2)
                                messages = await client.get_messages(
                                    peer, limit=8, min_id=sent.id
                                )
                                responses = [
                                    m
                                    for m in messages
                                    if m.sender_id == bot_id and not m.out
                                ]
                                break
                            await asyncio.sleep(1)
                        record = {
                            "command": command,
                            "responded": bool(responses),
                            "responses": [(m.raw_text or "")[:1600] for m in responses],
                        }
                        report["checks"].append(record)
                        if command == "/start":
                            report["menu"] = [
                                [b.text for b in row]
                                for m in responses
                                for row in (m.buttons or [])
                            ]
                        print(
                            json.dumps(
                                {"bot_id": bot_id, **record}, ensure_ascii=False
                            ),
                            flush=True,
                        )
                        await asyncio.sleep(1)
                    results.append(report)
            finally:
                await client.disconnect()
    dest = Path(__file__).resolve().parents[1] / "var/live-tenant-acceptance.json"
    dest.write_text(
        json.dumps(
            {"at": int(time.time()), "bots": results}, ensure_ascii=False, indent=2
        )
    )
    dest.chmod(0o600)


if __name__ == "__main__":
    asyncio.run(main())
