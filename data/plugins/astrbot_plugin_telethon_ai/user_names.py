"""Display-only usernames fetched by numeric ID through the existing control Bot."""

import asyncio
import re
import time

from telegram.error import RetryAfter


def username(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", value)
        else None
    )


class UserNames:
    def __init__(self):
        self.cache = {}
        self.lock = asyncio.Lock()

    async def fetch(self, user_id, bot, semaphore):
        previous = self.cache.get(user_id, {})
        if not bot or previous.get("retry_at", 0) > time.monotonic():
            return
        async with semaphore:
            self.cache[user_id] = {**previous, "retry_at": time.monotonic() + 60}
            try:
                chat = await bot.get_chat(int(user_id))
                if str(chat.id) != user_id or chat.type != "private":
                    return
                self.cache[user_id] = {
                    "username": username(chat.username),
                    "retry_at": time.monotonic() + 600,
                }
            except RetryAfter as exc:
                delay = exc.retry_after
                if hasattr(delay, "total_seconds"):
                    delay = delay.total_seconds()
                self.cache[user_id]["retry_at"] = time.monotonic() + max(60, delay)
            except Exception:
                pass

    async def enrich(self, status, bot):
        async with self.lock:
            ids = {str(t["owner"]) for t in status["tenants"]}
            ids.update(str(a["user_id"]) for a in status["accounts"])
            needed = {str(t["owner"]) for t in status["tenants"]}
            needed.update(
                str(a["user_id"]) for a in status["accounts"] if not a.get("username")
            )
            semaphore = asyncio.Semaphore(4)
            try:
                async with asyncio.timeout(3):
                    await asyncio.gather(
                        *[
                            self.fetch(value, bot, semaphore)
                            for value in sorted(needed)[:32]
                            if value.isascii() and value.isdigit() and int(value) > 0
                        ]
                    )
            except TimeoutError:
                pass
            self.cache = {key: value for key, value in self.cache.items() if key in ids}
            for tenant in status["tenants"]:
                tenant["owner_username"] = self.cache.get(str(tenant["owner"]), {}).get(
                    "username"
                )
                tenant["bot_username"] = username(tenant.get("bot_username"))
            for account in status["accounts"]:
                account["username"] = username(
                    account.get("username")
                ) or self.cache.get(str(account["user_id"]), {}).get("username")
        return status
