"""Bounded, read-only titles for explicitly allowed groups on existing clients."""

import asyncio
import re
import time

from telethon import errors, types, utils


class GroupNames:
    def __init__(self, context):
        self.context = context
        self.cache = {}
        self.lock = asyncio.Lock()

    async def refresh(self, account, group_id, semaphore):
        key = (account["account"], group_id)
        platform = self.context.get_platform_inst(account["platform_id"])
        client = getattr(platform, "client", None)
        if not client or not client.is_connected():
            return
        previous = self.cache.get(key, {})
        if previous.get("retry_at", 0) > time.monotonic():
            return
        async with semaphore:
            self.cache[key] = {**previous, "retry_at": time.monotonic() + 60}
            try:
                entity = await client.get_entity(int(group_id))
                if (
                    not isinstance(entity, (types.Chat, types.Channel))
                    or str(utils.get_peer_id(entity)) != group_id
                    or not entity.title
                ):
                    return
                self.cache[key] = {
                    "name": entity.title[:256],
                    "retry_at": time.monotonic() + 300,
                    "updated": time.time(),
                }
            except errors.FloodWaitError as exc:
                self.cache[key]["retry_at"] = time.monotonic() + max(60, exc.seconds)
            except Exception:
                # Metadata failure cannot alter authorization or break status.
                pass

    def describe(self, account, group_id):
        platform = self.context.get_platform_inst(account["platform_id"])
        client = getattr(platform, "client", None)
        connected = bool(client and client.is_connected())
        cached = self.cache.get((account["account"], group_id), {})
        return {
            "id": group_id,
            "name": cached.get("name"),
            "name_state": (
                "cached"
                if cached.get("name") and not connected
                else "known"
                if cached.get("name")
                else "unavailable"
                if connected
                else "disconnected"
            ),
            "updated": cached.get("updated"),
        }

    async def enrich(self, status):
        async with self.lock:
            jobs, authorized = [], set()
            semaphore = asyncio.Semaphore(4)
            for account in status["accounts"]:
                for value in account["allowed_chats"]:
                    group_id = str(value)
                    key = (account["account"], group_id)
                    authorized.add(key)
                    if re.fullmatch(r"-[1-9][0-9]{0,18}", group_id) and len(jobs) < 32:
                        jobs.append(self.refresh(account, group_id, semaphore))
            # A page refresh must remain responsive even when Telegram is down.
            try:
                async with asyncio.timeout(3):
                    await asyncio.gather(*jobs)
            except TimeoutError:
                pass
            self.cache = {
                key: value for key, value in self.cache.items() if key in authorized
            }
            for account in status["accounts"]:
                account["groups"] = [
                    self.describe(account, str(value))
                    for value in account["allowed_chats"]
                ]
            for tenant in status["tenants"]:
                tenant["group_details"] = []
                for value in tenant["groups"]:
                    group_id = str(value)
                    # Do not borrow a title from another tenant's account.
                    matches = [
                        group
                        for account in status["accounts"]
                        if account.get("tenant") == tenant["id"]
                        for group in account["groups"]
                        if group["id"] == group_id and group["name"]
                    ]
                    tenant["group_details"].append(
                        matches[0]
                        if matches
                        else {
                            "id": group_id,
                            "name": None,
                            "name_state": "unavailable",
                            "updated": None,
                        }
                    )
        return status
