"""Bounded live acceptance against VIP_dhbot using authorized retired sessions."""

import asyncio
import json
import os
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock
from telethon import TelegramClient
from telethon.errors import DataInvalidError
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

ROOT = Path("/opt/astrbot-prod")
CONFIG = Path("/opt/telegram-chat-collector-prod/config.json")
OUTPUT = ROOT / "integrations/tenant-control/var/telegram-acceptance.json"
BOT_ID = 8941933661
ADMIN = 1000000001


async def replies(client, peer, after, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = await client.get_messages(peer, limit=12, min_id=after)
        result = [m for m in messages if m.sender_id == BOT_ID and not m.out]
        if result:
            return result
        await asyncio.sleep(1)
    return []


async def account(spec, seen):
    label, session, api_id, api_hash, proxy = spec
    client = TelegramClient(
        str(session),
        int(api_id),
        api_hash,
        proxy=proxy,
        receive_updates=False,
        flood_sleep_threshold=0,
        connection_retries=1,
        request_retries=1,
    )
    result = {"account": label, "checks": []}
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Session is not authorized")
        me = await client.get_me()
        if not me or me.bot or me.id in seen:
            raise RuntimeError("Expected two distinct user accounts")
        seen.add(me.id)
        result["role"] = "admin" if me.id == ADMIN else "customer"
        peer = await client.get_entity("VIP_dhbot")
        if peer.id != BOT_ID or not peer.bot:
            raise RuntimeError("Unexpected target identity")

        async def command(text, expected=None, denied=False):
            sent = await client.send_message(peer, text, parse_mode=None)
            received = await replies(client, peer, sent.id, 4 if denied else 15)
            combined = "\n".join(m.raw_text or "" for m in received)
            passed = not received if denied else bool(received and expected in combined)
            result["checks"].append(
                {
                    "command": text,
                    "passed": passed,
                    "responses": [m.raw_text for m in received],
                }
            )
            if not passed:
                raise RuntimeError("Command acceptance failed: " + text.split()[0])
            await asyncio.sleep(1)
            return received

        home = (await command("/start", "机器人托管中心"))[0]
        rows = [[button.text for button in row] for row in home.buttons or []]
        result["keyboard"] = rows
        if not rows:
            raise RuntimeError("Home keyboard is missing")
        await command("/plans", "套餐暂未开放")
        await command("/bots", "暂无机器人")
        await command("/configstatus", "实例：astrbot-prod", denied=me.id != ADMIN)
        await command("/admin", "平台", denied=me.id != ADMIN)
        if me.id != ADMIN:
            await command(f"/admintrial {me.id} 1", denied=True)

        # A real callback goes through Telegram and the backend, not a direct handler call.
        last = (await client.get_messages(peer, limit=1))[0].id
        answer = await client(
            GetBotCallbackAnswerRequest(
                peer=peer,
                msg_id=home.id,
                data=b"tc:bots",
            )
        )
        received = await replies(client, peer, last)
        passed = any("暂无机器人" in (m.raw_text or "") for m in received)
        result["checks"].append({"callback": "tc:bots", "passed": passed})
        if not passed:
            raise RuntimeError("Menu callback failed")

        if me.id != ADMIN:
            for payload in (b"tc:admin", b"tc:configstatus"):
                last = (await client.get_messages(peer, limit=1))[0].id
                try:
                    answer = await client(
                        GetBotCallbackAnswerRequest(
                            peer=peer,
                            msg_id=home.id,
                            data=payload,
                        )
                    )
                except DataInvalidError:
                    result["checks"].append(
                        {
                            "forged_callback": payload.decode(),
                            "passed": True,
                            "rejected_by": "Telegram; backend not exercised",
                        }
                    )
                    continue
                received = await replies(client, peer, last, timeout=4)
                passed = not received
                result["checks"].append(
                    {
                        "forged_callback": payload.decode(),
                        "passed": passed,
                        "answer": answer.message,
                    }
                )
                if not passed:
                    raise RuntimeError("Unauthorized admin callback responded")
        # This response distinguishes an intentional denial from a stalled receiver.
        await command("/bots", "暂无机器人")
        result["passed"] = True
    except Exception as error:
        result["passed"] = False
        result["error_type"] = type(error).__name__
    finally:
        await client.disconnect()
    return result


async def main():
    os.umask(0o077)
    active = await asyncio.to_thread(
        subprocess.run,
        ["systemctl", "is-active", "--quiet", "telegram-chat-collector.service"],
        check=False,
    )
    if active.returncode == 0:
        raise RuntimeError("Collector must stay stopped")
    config = json.loads(CONFIG.read_text())
    data = Path(config.get("data_dir", "var")).expanduser()
    if not data.is_absolute():
        data = CONFIG.parent / data
    specs = [
        (
            "collector",
            data / "account",
            config["api_id"],
            config["api_hash"],
            config.get("proxy"),
        )
    ]
    specs.extend(
        (
            item["id"],
            Path(item["session"]).expanduser(),
            item["api_id"],
            item["api_hash"],
            config.get("proxy"),
        )
        for item in config["unified"]["accounts"]
    )
    if len(specs) != 2 or any(not Path(f"{s}.session").is_file() for _, s, *_ in specs):
        raise RuntimeError("Expected two existing sessions")
    report = {"at": int(time.time()), "bot": "VIP_dhbot", "accounts": []}
    with ExitStack() as locks:
        locks.enter_context(FileLock(str(data / "collector.lock"), timeout=0))
        for _, session, *_ in specs:
            locks.enter_context(FileLock(f"{session}.session.unified.lock", timeout=0))
        seen = set()
        for spec in specs:
            result = await account(spec, seen)
            report["accounts"].append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    OUTPUT.chmod(0o600)
    if not all(item["passed"] for item in report["accounts"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
