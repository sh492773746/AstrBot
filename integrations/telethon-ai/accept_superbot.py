"""Bounded Telethon acceptance for Superbot permissions and tenant isolation.

The runner copies authorized sessions into a private temporary directory, uses
read-heavy polling with Telegram-aware backoff, and never prints credentials.
The owner flow is only enabled when an explicit owner session and a separate
unbound test supergroup are supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient, errors

REGISTRY = Path(
    "/opt/astrbot-prod/data/plugin_data/astrbot_plugin_telethon_ai/private/accounts.json"
)
BOT_USERNAME = "dhcmsuperBOT"
BOT_ID = 1000000005
OWNER_UID = 1000000001
LEGACY_GROUP = "wdhihji1"
LEGACY_GROUP_ID = -1001000000003


@dataclass
class Account:
    alias: str
    user_id: int
    api_id: int
    api_hash: str
    session: Path


class ThrottledClient:
    def __init__(self, account: Account, root: Path):
        self.account = account
        copied = root / f"{account.alias}.session"
        shutil.copy2(f"{account.session}.session", copied.with_suffix(".session"))
        copied.chmod(0o600)
        self.client = TelegramClient(
            str(copied.with_suffix("")),
            account.api_id,
            account.api_hash,
            receive_updates=False,
            request_retries=0,
            connection_retries=1,
            flood_sleep_threshold=0,
        )
        self.next_call = 0.0
        self.lock = asyncio.Lock()

    async def call(self, method, *args, retry_read=False, **kwargs):
        async with self.lock:
            delay = self.next_call - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_call = time.monotonic() + 2.0
            try:
                return await method(*args, **kwargs)
            except errors.FloodWaitError as exc:
                if not retry_read:
                    raise
                await asyncio.sleep(max(1, int(exc.seconds)))
                self.next_call = time.monotonic() + 2.0
                return await method(*args, **kwargs)


async def poll_bot(tc: ThrottledClient, chat, sent_id: int, timeout=14):
    deadline = time.monotonic() + timeout
    for wait in (2, 4, 8):
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(wait)
        messages = await tc.call(
            tc.client.get_messages,
            chat,
            limit=20,
            min_id=sent_id,
            retry_read=True,
        )
        replies = [
            message
            for message in messages
            if message.sender_id == BOT_ID and not message.out
        ]
        if replies:
            return replies
    return []


async def exchange(tc: ThrottledClient, chat, text: str):
    sent = await tc.call(tc.client.send_message, chat, text, parse_mode=None)
    replies = await poll_bot(tc, chat, sent.id)
    return sent, replies


def text_report(messages):
    return [
        {
            "id": message.id,
            "text": (message.raw_text or "")[:1000],
            "has_buttons": bool(message.buttons),
        }
        for message in messages
    ]


async def inspect_identity(tc: ThrottledClient, group_name: str):
    me = await tc.call(tc.client.get_me, retry_read=True)
    group = await tc.call(tc.client.get_entity, group_name, retry_read=True)
    permissions = await tc.call(tc.client.get_permissions, group, me, retry_read=True)
    return {
        "user_id": me.id,
        "username": me.username,
        "group_id": getattr(group, "id", None),
        "group_username": getattr(group, "username", None),
        "group_title": getattr(group, "title", None),
        "member": not bool(getattr(group, "left", True)),
        "is_admin": bool(getattr(permissions, "is_admin", False)),
        "is_creator": bool(getattr(permissions, "is_creator", False)),
    }


async def ordinary_flow(tc: ThrottledClient, legacy_group: str):
    identity = await inspect_identity(tc, legacy_group)
    report = {"identity": identity, "private": [], "group": []}
    bot = await tc.call(tc.client.get_entity, BOT_USERNAME, retry_read=True)
    if bot.id != BOT_ID:
        raise RuntimeError("Unexpected Superbot identity")
    for command in (
        "/start",
        "/help",
        "玩法大全",
        "我的",
        "广告投递",
        "管理",
        "群管理",
        "群聊解禁",
    ):
        _, replies = await exchange(tc, bot, command)
        report["private"].append({"command": command, "replies": text_report(replies)})
    if identity["is_admin"] or identity["is_creator"]:
        report["group_status"] = "blocked_not_ordinary_member"
        report["group_reason"] = (
            "The session has Telegram admin/creator privileges; "
            "it was not used to claim ordinary-member denial results."
        )
        return report
    for text in ("玩法大全", "帮助", "/help", "/bindgroup invalid-test-token-000"):
        try:
            _, replies = await exchange(tc, legacy_group, text)
        except errors.UserBannedInChannelError:
            report["group_status"] = "blocked_send_restricted"
            report["group_reason"] = (
                "Telegram denied this session from sending in the group."
            )
            break
        report["group"].append({"command": text, "replies": text_report(replies)})
    else:
        report["group_status"] = "passed"
    return report


async def owner_flow(tc: ThrottledClient, test_group: str):
    identity = await inspect_identity(tc, test_group)
    if identity["user_id"] != OWNER_UID:
        raise RuntimeError("Owner session UID does not match the verified owner")
    if not identity["is_creator"]:
        raise RuntimeError("Owner session is not the test group's creator")
    report = {"identity": identity, "steps": []}
    bot = await tc.call(tc.client.get_entity, BOT_USERNAME, retry_read=True)
    _, messages = await exchange(tc, bot, "我的群")
    report["steps"].append({"action": "tenant_home", "replies": text_report(messages)})
    join = next(
        (message for message in messages if message.buttons),
        None,
    )
    if join is None:
        raise RuntimeError("Missing tenant home buttons")
    join_message = await join.click(text="接入群")
    if join_message is None:
        raise RuntimeError("Missing tenant join response")
    token_match = re.search(
        r"/bindgroup\s+([A-Za-z0-9_-]+)", join_message.raw_text or ""
    )
    if not token_match:
        raise RuntimeError("Missing binding challenge")
    token = token_match.group(1)
    _, replies = await exchange(tc, test_group, f"/bindgroup {token}")
    report["steps"].append({"action": "bindgroup", "replies": text_report(replies)})
    _, messages = await exchange(tc, bot, "我的群")
    report["steps"].append(
        {"action": "tenant_home_after_bind", "replies": text_report(messages)}
    )
    group_message = next(
        (
            message
            for message in messages
            if message.buttons
            and any(
                "群" in (button.text or "") and "接入" not in (button.text or "")
                for row in message.buttons
                for button in row
            )
        ),
        None,
    )
    if group_message is None:
        raise RuntimeError("Missing bound-group management button")
    group_title = identity.get("group_title") or ""
    group_button = next(
        (
            button
            for row in group_message.buttons
            for button in row
            if group_title and group_title in (button.text or "")
        ),
        None,
    )
    if group_button is None:
        group_button = next(
            (
                button
                for row in group_message.buttons
                for button in row
                if "群" in (button.text or "") and "接入" not in (button.text or "")
            ),
            None,
        )
    if group_button is None:
        raise RuntimeError("Cannot locate bound-group button")
    group_page = await group_message.click(data=group_button.data)
    group_step = {"action": "tenant_group", "replies": []}
    if group_page is not None:
        group_step["replies"] = text_report([group_page])
    else:
        group_step["status"] = "unknown_delivery"
    report["steps"].append(group_step)
    if group_page is None or not group_page.buttons:
        raise RuntimeError("Missing bound-group management buttons")
    report["steps"].append(
        {
            "action": "tenant_group_pages",
            "pages": [],
            "platform_control_visible": any(
                "平台" in (button.text or "")
                for row in group_page.buttons
                for button in row
            ),
        }
    )
    for label in (
        "功能启停",
        "积分与奖励",
        "玩法设置",
        "广告位与订单",
        "收款设置",
        "异常记录",
    ):
        button = next(
            (
                candidate
                for row in group_page.buttons
                for candidate in row
                if candidate.text == label
            ),
            None,
        )
        if button is None:
            report["steps"][-1]["pages"].append(
                {"label": label, "status": "missing_button"}
            )
            continue
        page = await group_page.click(data=button.data)
        page_result = {"label": label, "status": "opened", "replies": []}
        if page is not None:
            page_result["replies"] = text_report([page])
        else:
            page_result["status"] = "unknown_delivery"
        report["steps"][-1]["pages"].append(page_result)
    return report


def load_accounts(owner_session: str | None):
    raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    accounts = [
        Account(
            alias=alias,
            user_id=int(spec["user_id"]),
            api_id=int(spec["api_id"]),
            api_hash=spec["api_hash"],
            session=Path(spec["session"]),
        )
        for alias, spec in raw.items()
    ]
    if owner_session:
        base = accounts[0]
        accounts.append(
            Account(
                alias="owner",
                user_id=OWNER_UID,
                api_id=base.api_id,
                api_hash=base.api_hash,
                session=Path(owner_session).with_suffix(""),
            )
        )
    return accounts


async def run(args):
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "legacy_group": args.legacy_group,
        "test_group": args.test_group,
        "owner_flow": "blocked_missing_inputs",
        "accounts": [],
    }
    with tempfile.TemporaryDirectory(prefix="superbot-telethon-") as temp:
        root = Path(temp)
        for account in load_accounts(args.owner_session):
            tc = ThrottledClient(account, root)
            try:
                await tc.client.connect()
                if not await tc.call(tc.client.is_user_authorized, retry_read=True):
                    raise RuntimeError("Session is not authorized")
                if account.alias == "owner":
                    if not args.test_group:
                        report["owner_flow"] = "blocked_missing_test_group"
                        continue
                    report["owner"] = await owner_flow(tc, args.test_group)
                    report["owner_flow"] = "passed"
                else:
                    report["accounts"].append(
                        {
                            "alias": account.alias,
                            "flow": await ordinary_flow(tc, args.legacy_group),
                        }
                    )
            except errors.FloodWaitError as exc:
                report["accounts"].append(
                    {
                        "alias": account.alias,
                        "status": "flood_wait",
                        "seconds": exc.seconds,
                    }
                )
            except Exception as exc:
                report["accounts"].append(
                    {
                        "alias": account.alias,
                        "status": "error",
                        "error": type(exc).__name__,
                        "message": str(exc)[:300],
                    }
                )
            finally:
                await tc.client.disconnect()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-session")
    parser.add_argument("--test-group")
    parser.add_argument("--legacy-group", default=LEGACY_GROUP)
    parser.add_argument(
        "--output",
        default="/root/Projects/agents/telethon-ai-deployment/superbot-acceptance.json",
    )
    args = parser.parse_args()
    result = asyncio.run(run(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    output.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
