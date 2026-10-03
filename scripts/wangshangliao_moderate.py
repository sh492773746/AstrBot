"""Human-only moderation entry point; explicit confirmation is required."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.moderation import execute
from astrbot.core.platform.sources.wangshangliao.storage import Vault
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


async def main():
    """Preview a fixed action and execute only with explicit confirmation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument(
        "--action",
        choices=["mute", "unmute", "mute_all", "unmute_all", "announce"],
        required=True,
    )
    parser.add_argument("--member", type=int, default=0)
    parser.add_argument("--text", default="")
    parser.add_argument("--operation", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "bot": args.bot,
                "group": args.group,
                "action": args.action,
                "member": args.member,
                "text": args.text,
                "operation": args.operation,
            },
            ensure_ascii=False,
        )
    )
    if not args.confirm:
        return
    config = json.loads(
        (Path(get_astrbot_data_path()) / "cmd_config.json").read_text(
            encoding="utf-8-sig"
        )
    )
    bot = next(
        b
        for b in config["platform"]
        if b.get("id") == args.bot and b.get("type") == "wangshangliao"
    )
    from astrbot.core.platform.sources.wangshangliao.policy import authorize_action

    authorize_action(bot, str(args.group), args.action)
    async with aiohttp.ClientSession() as http:
        client = BusinessClient(Deployment.load(), http)
        client.restore(Vault(args.bot).load()["business"])
        print(
            json.dumps(
                await execute(
                    client,
                    args.bot,
                    str(bot["account_id"]),
                    args.operation,
                    args.action,
                    args.group,
                    args.member,
                    args.text,
                ),
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
