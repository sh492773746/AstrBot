"""One-shot authorized managed Bot acceptance; never joins or posts to groups."""

import asyncio
import fcntl
import json
import os
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse

from filelock import FileLock
from telethon import TelegramClient, functions

OUT = Path("/root/Projects/agents/telethon-ai-deployment/live-enrollment.json")
REGISTRY = Path(
    "/opt/astrbot-prod/data/plugin_data/astrbot_plugin_telethon_ai/private/accounts.json"
)


async def exchange(client, bot, command):
    sent = await client.send_message(bot, command, parse_mode=None)
    for _ in range(25):
        await asyncio.sleep(1)
        replies = [
            m
            for m in await client.get_messages(bot, limit=10, min_id=sent.id)
            if m.sender_id == bot.id and not m.out
        ]
        if replies:
            return replies
    raise RuntimeError("No private Bot reply")


async def verify_outsider():
    spec = json.loads(REGISTRY.read_text())["collector"]
    state = json.loads(OUT.read_text())
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
            assert (await client.get_me()).id == 1000000002
            bot = await client.get_entity(state["username"])
            assert bot.id == state["bot_id"]
            state["outsider_replies"] = {}
            for command in ("/start", "/service", "/resume", "/bind -1001000000003"):
                replies = await exchange(client, bot, command)
                texts = [m.raw_text for m in replies]
                assert all(text == "此服务仅限绑定的所有者操作。" for text in texts)
                state["outsider_replies"][command] = texts
                print(
                    json.dumps({"outsider_command": command, "denied": True}),
                    flush=True,
                )
            state["state"] = "owner_and_outsider_checked"
            OUT.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        finally:
            await client.disconnect()


async def main():
    os.umask(0o077)
    spec = json.loads(REGISTRY.read_text())["keywords"]
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
            assert await client.is_user_authorized()
            assert (await client.get_me()).id == 1000000003
            manager = await client.get_entity("VIP_dhbot")
            assert manager.id == 8941933661
            if OUT.exists():
                state = json.loads(OUT.read_text())
                if not state.get("bot_id"):
                    raise RuntimeError(
                        "Prior creation outcome uncertain; inspect before retry"
                    )
                bot = await client.get_entity(state["username"])
                assert bot.id == state["bot_id"]
            else:
                replies = await exchange(client, manager, "/create")
                links = [
                    button.url
                    for msg in replies
                    for row in (msg.buttons or [])
                    for button in row
                    if button.url
                ]
                urls = [urlparse(link) for link in links]
                valid = [
                    url
                    for url in urls
                    if url.scheme == "https"
                    and url.netloc == "t.me"
                    and url.path.startswith("/newbot/VIP_dhbot/")
                ]
                assert len(valid) == 1, "Missing approved creation link"
                username = valid[0].path.split("/")[-1]
                assert await client(functions.bots.CheckUsernameRequest(username))
                state = {"owner": 1000000003, "username": username, "state": "creating"}
                with OUT.open("x") as file:
                    json.dump(state, file)
                bot = await client(
                    functions.bots.CreateBotRequest(
                        name="AI Service Acceptance",
                        username=username,
                        manager_id=await client.get_input_entity(manager),
                    )
                )
                state.update(bot_id=bot.id, state="created")
                OUT.write_text(json.dumps(state, indent=2))
            print(json.dumps(state), flush=True)
            await asyncio.sleep(15)
            state["owner_replies"] = {}
            for command in ("/start", "/status", "/help"):
                replies = await exchange(client, bot, command)
                state["owner_replies"][command] = [m.raw_text for m in replies]
                print(
                    json.dumps(
                        {
                            "command": command,
                            "replies": state["owner_replies"][command],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            replies = await exchange(client, manager, "/bots")
            state["listing"] = [m.raw_text for m in replies]
            assert any(state["username"] in text for text in state["listing"])
            state["state"] = "owner_commands_checked"
            OUT.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        finally:
            await client.disconnect()
    await verify_outsider()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 150))
