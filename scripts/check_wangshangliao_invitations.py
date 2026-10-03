"""Query read-only invitation attribution through the running local Dashboard."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    """Read a configured group's member attribution or invitation records.

    Returns:
        Zero for a completed read, or one for unavailable platform evidence.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--member", default="")
    parser.add_argument(
        "--records", action="store_true", help="Read account-visible invitation logs"
    )
    parser.add_argument(
        "--all-states", action="store_true", help="Include non-pending log records"
    )
    parser.add_argument(
        "--last-id", default="", help="Platform log continuation cursor"
    )
    parser.add_argument("--port", type=int, default=6185)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid local Dashboard port")
    if (args.all_states or args.last_id) and not args.records:
        parser.error("--all-states and --last-id require --records")
    if args.last_id and (
        not args.last_id.isascii()
        or not args.last_id.isdigit()
        or len(args.last_id) > 20
    ):
        parser.error("Invalid platform continuation cursor")
    config = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    bot = next(
        (
            item
            for item in config.get("platform", [])
            if item.get("id") == args.bot
            and item.get("type") == "wangshangliao"
            and item.get("enable")
            and args.group in item.get("enabled_groups", [])
        ),
        None,
    )
    if not bot:
        parser.error("The bot/group must be enabled in the local configuration")
    if args.member and (
        not args.member.isascii()
        or not args.member.isdigit()
        or len(args.member) > 20
        or int(args.member) <= 0
    ):
        parser.error("Member must be an exact business account ID")
    token = jwt.encode(
        {
            "username": config["dashboard"]["username"],
            "exp": int(time.time()) + 60,
        },
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        timeout=40, follow_redirects=False, trust_env=False
    ) as client:
        response = await client.get(
            f"http://127.0.0.1:{args.port}/api/v1/plugins/extensions/"
            "wangshangliao_moderation/invitations",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "bot_id": args.bot,
                "group_id": args.group,
                "member_id": args.member,
                "source": "records" if args.records else "members",
                "pending_only": "false" if args.all_states else "true",
                "last_id": args.last_id,
            },
        )
    try:
        result = response.json()
    except ValueError:
        print(
            json.dumps({"status": "unavailable", "http_status": response.status_code})
        )
        return 1
    if response.status_code != 200 or result.get("status") != "ok":
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "http_status": response.status_code,
                    "message": str(result.get("message", "Query unavailable"))[:500],
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result["data"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
