"""Live Telegram visibility checks for private group selectors."""

import asyncio


async def visible_groups(runtime, uid, rows, *, concurrency=4):
    async def check(row):
        try:
            actor, bot = await asyncio.gather(
                runtime.bot.get_chat_member(row["chat"], int(uid)),
                runtime.bot.get_chat_member(row["chat"], runtime.bot.id),
            )
        except Exception:
            return None
        if actor.status in {"creator", "administrator"} and bot.status in {
            "creator",
            "administrator",
        }:
            try:
                await runtime.tenants.verify_designation(uid, row["chat"], actor.status)
            except Exception:
                return None
            return row
        return None

    selected = []
    for start in range(0, len(rows), concurrency):
        selected.extend(
            row
            for row in await asyncio.gather(
                *(check(row) for row in rows[start : start + concurrency])
            )
            if row is not None
        )
    return selected
