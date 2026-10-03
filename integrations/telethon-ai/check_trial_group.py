"""Read-only identity and membership check; no joins or group messages."""

import asyncio
import fcntl
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from filelock import FileLock
from telethon import TelegramClient, utils

REGISTRY = Path(
    "/opt/astrbot-prod/data/plugin_data/astrbot_plugin_telethon_ai/private/accounts.json"
)
OUT = Path("/root/Projects/agents/telethon-ai-deployment/trial-group-check.json")


async def main():
    os.umask(0o077)
    report = []
    for alias, spec in json.loads(REGISTRY.read_text()).items():
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
                me = await client.get_me()
                assert me.id == spec["user_id"]
                group = await client.get_entity("wdhihji1")
                peer_id = utils.get_peer_id(group)
                assert peer_id == -1001000000003, "Group identity changed"
                row = {
                    "account": alias,
                    "user_id": me.id,
                    "group_id": peer_id,
                    "username": group.username,
                    "title": group.title,
                    "megagroup": bool(getattr(group, "megagroup", False)),
                }
                try:
                    permissions = await client.get_permissions(group, me)
                    row.update(
                        is_member=not getattr(group, "left", True),
                        is_admin=permissions.is_admin,
                        is_creator=permissions.is_creator,
                    )
                except Exception as exc:
                    row.update(is_member=False, permission_error=type(exc).__name__)
                report.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                if alias == "keywords" and "--request-binding" in sys.argv:
                    from accept_enrollment import exchange

                    bot = await client.get_entity("AIService94ce058bfdBot")
                    assert bot.id == 8910402926
                    replies = await exchange(client, bot, f"/bind {peer_id}")
                    row["binding_request_replies"] = [m.raw_text for m in replies]
                    print(
                        json.dumps(
                            {"binding_request_replies": row["binding_request_replies"]},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            finally:
                await client.disconnect()
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 90))
