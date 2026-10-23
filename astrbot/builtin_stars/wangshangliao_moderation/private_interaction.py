"""Durable first-use guidance and explicit, non-operational private AI sessions."""

import asyncio
import sqlite3
import time

from astrbot.core import logger
from astrbot.core.platform.sources.wangshangliao.database import AsyncDatabase
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .observability import record_event
from .onboarding_delivery import send_onboarding_text
from .private_invitation import PRIVATE_MENU, private_query_guard

CHAT_SECONDS = 15 * 60
FIRST_USE_TEXT = (
    "【旺商聊机器人】\n"
    + PRIVATE_MENU
    + "\n\n群管理（仅授权管理员）：禁言、解禁、公告、定时。\n"
    "AI只提供说明，不执行管理操作。"
)


class PrivateInteraction:
    """Claim onboarding before sending and scope chat state to the real sender."""

    def __init__(self, images):
        self.images = images
        self.active = 0
        self.menu = FIRST_USE_TEXT
        self.group_queue = asyncio.Queue(maxsize=20)
        self.group_task = None
        self.closed = False

    def database(self, instance):
        """Open an additive store without touching invitation or policy data.

        Args:
            instance: Authenticated adapter instance identifier.

        Returns:
            Connection owned by AsyncDatabase.
        """
        root = instance_dir(instance)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "private_interaction.sqlite3"
        db = sqlite3.connect(path, timeout=5)
        path.chmod(0o600)
        db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS onboarding(
                account TEXT NOT NULL, sender TEXT NOT NULL,
                created REAL NOT NULL, outcome TEXT NOT NULL,
                PRIMARY KEY(account,sender));
            CREATE TABLE IF NOT EXISTS chat_sessions(
                account TEXT NOT NULL, sender TEXT NOT NULL,
                expires REAL NOT NULL, fresh INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(account,sender));
            CREATE TABLE IF NOT EXISTS group_onboarding(
                account TEXT NOT NULL,group_id TEXT NOT NULL,epoch INTEGER NOT NULL,
                created REAL NOT NULL,outcome TEXT NOT NULL,
                PRIMARY KEY(account,group_id,epoch));
        """)
        return db

    async def handle(self, event, command):
        """Handle unmatched private text without feeding it to an action agent.

        Args:
            event: Fresh authenticated native private message.
            command: Already recognized deterministic command, or empty text.

        Returns:
            True if the event was consumed; False for commands or active chat.
        """
        if not event.is_private_chat():
            return False
        text = event.message_str.strip()
        control = text in {"/chat", "/exit"}
        if command and not control:
            return False
        adapter = event.platform
        if (
            self.closed
            or not adapter.config.get("enable", True)
            or not adapter.config.get("reply_private", True)
            or not event.eligible
            or event.processing_completion.cancelled()
            or (
                is_managed_account(event.get_sender_id())
                and event.get_extra("wsl_test_scope")
                not in {"private_commands", "private_ai"}
            )
        ):
            event.stop_event()
            return True
        if self.active >= 2:
            event.stop_event()
            record_event(event, "private_onboarding", "busy")
            return True
        self.active += 1
        try:
            frozen = private_query_guard(event, require_verified=False)

            def guard():
                if self.closed:
                    raise ProtocolError("private_welcome_closed")
                frozen()

            account, sender = str(adapter.account), event.actor_identity.principal
            instance = adapter.config["id"]

            def factory():
                return self.database(instance)

            now = time.time()
            from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
                runtime,
            )

            if control and runtime.open_service is not None:
                await runtime.open_service.end_chat(account, sender)
            async with AsyncDatabase(factory, write=True, guard=guard) as db:
                await db.execute("DELETE FROM chat_sessions WHERE expires<=?", (now,))
                if event.actor_identity.verified_uid:
                    await db.execute(
                        "INSERT OR IGNORE INTO onboarding(account,sender,created,outcome) "
                        "SELECT account,?,created,outcome FROM onboarding WHERE account=? AND sender=?",
                        (sender, account, event.actor_identity.verified_uid),
                    )
                if control:
                    await db.execute(
                        "INSERT INTO chat_sessions(account,sender,expires,fresh) "
                        "VALUES(?,?,?,1) ON CONFLICT(account,sender) DO UPDATE SET "
                        "expires=excluded.expires,fresh=1",
                        (account, sender, now + CHAT_SECONDS if text == "/chat" else 0),
                    )
                    claimed, session = False, None
                else:
                    session = await db.fetchone(
                        "SELECT expires,fresh FROM chat_sessions "
                        "WHERE account=? AND sender=? AND expires>?",
                        (account, sender, now),
                    )
                    if session:
                        await db.execute(
                            "UPDATE chat_sessions SET expires=?,fresh=0 "
                            "WHERE account=? AND sender=?",
                            (now + CHAT_SECONDS, account, sender),
                        )
                        claimed = False
                    else:
                        claimed = (
                            await db.execute(
                                "INSERT OR IGNORE INTO onboarding VALUES(?,?,?,'claimed')",
                                (account, sender, now),
                            )
                            == 1
                        )
            guard()
            if session:
                event.set_extra("wsl_dialogue_only", True)
                event.set_extra("wsl_chat_fresh", bool(session[1]))
                # Reuse the native capability revocation hook for this event only.
                # A later explicit management command gets its normal fresh role.
                event.set_extra("_api_key_allow_admin_role", False)
                return False
            event.set_extra("wsl_plain_command", True)
            event.set_extra("wsl_keep_reply", True)
            if event.get_extra("wsl_test_scope"):
                event.set_extra("wsl_command_result", True)
            event.stop_event()
            if control:
                if not event.get_extra("_wsl_menu_exit"):
                    await event.send(
                        event.plain_result(
                            "已进入AI咨询，直接发送问题即可。AI不执行管理操作；"
                            "发送 /exit 结束，15分钟无消息自动结束。"
                            "对话会保留在机器人后台，重新咨询不延续上一次的内容。"
                            if text == "/chat"
                            else "已结束AI咨询。"
                        )
                    )
                return True
            if not claimed:
                return True
            outcome = "unknown"
            try:
                key = (
                    f"{event.session_id}/{event.message_obj.message_id}/"
                    f"{event.reply_index}"
                )
                await event.send(
                    event.plain_result(
                        self.menu + "\n\n下方依次为：①使用说明 ②对话示例。"
                    )
                )
                guard()
                receipt = await adapter.ledger.receipt(key)
                if receipt and receipt["status"] in {"accepted", "verified"}:
                    outcome = await self.images.send(event, guard)
                else:
                    outcome = "text_unconfirmed"
            except asyncio.CancelledError:
                raise
            except Exception:
                record_event(
                    event,
                    "private_onboarding",
                    "delivery_unconfirmed",
                    error="onboarding_delivery_unconfirmed",
                )
            finally:
                # Uncertain delivery remains claimed; never resend after a reload.
                async with AsyncDatabase(factory, write=True) as db:
                    await db.execute(
                        "UPDATE onboarding SET outcome=? WHERE account=? AND sender=?",
                        (outcome, account, sender),
                    )
            record_event(event, "private_onboarding", outcome)
            return True
        except ProtocolError:
            event.stop_event()
            record_event(event, "private_onboarding", "scope_changed")
            return True
        except Exception:
            event.stop_event()
            record_event(
                event,
                "private_onboarding",
                "store_unavailable",
                error="onboarding_store_unavailable",
            )
            return True
        finally:
            self.active -= 1

    async def welcome_friend(self, adapter, member, peer, request_id, authorize):
        """Claim a first welcome only after verified successful friend acceptance.

        Args:
            adapter: Native account that accepted this application.
            member: Business UID in the signed provider acceptance.
            peer: Independent NIM ID in the same verified record.
            request_id: Provider application identifier, never user-supplied text.
            authorize: Active approval worker and configuration guard.

        Returns:
            Delivery outcome, or a safe non-delivery reason.
        """
        account, member = str(adapter.account), str(member)
        sender = "nim/" + peer
        if (
            self.closed
            or not peer.isascii()
            or not peer.isdecimal()
            or not 0 < int(peer) < 1 << 32
            or member == account
            or is_managed_account(member)
            or not adapter.config.get("reply_private", True)
        ):
            return "recipient_unavailable"

        def check():
            if self.closed:
                raise ProtocolError("friend_welcome_closed")
            authorize()

        check()
        if self.active >= 2:
            return "busy"
        self.active += 1
        outcome = "unknown"
        claimed = False
        try:
            instance = adapter.config["id"]
            async with AsyncDatabase(
                lambda: self.database(instance), write=True, guard=check
            ) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO onboarding(account,sender,created,outcome) "
                    "SELECT account,?,created,outcome FROM onboarding WHERE account=? AND sender=?",
                    (sender, account, member),
                )
                claimed = (
                    await db.execute(
                        "INSERT OR IGNORE INTO onboarding VALUES(?,?,?,'claimed')",
                        (account, sender, time.time()),
                    )
                    == 1
                )
            if not claimed:
                return "already_sent"
            key = f"friend-welcome/{account}/{request_id}/{peer}"
            result = await send_onboarding_text(
                adapter,
                member,
                peer,
                key,
                "你好，欢迎使用大海机器人！\n"
                + self.menu
                + "\n\n下方依次为：①使用说明 ②对话示例。",
                check,
            )
            outcome = (
                await self.images.send_friend(adapter, member, peer, key, check)
                if result in {"accepted", "verified"}
                else "text_unconfirmed"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = "delivery_unconfirmed"
        finally:
            try:
                if claimed:
                    async with AsyncDatabase(
                        lambda: self.database(instance), write=True
                    ) as db:
                        await db.execute(
                            "UPDATE onboarding SET outcome=? WHERE account=? AND sender=?",
                            (outcome, account, sender),
                        )
            finally:
                self.active -= 1
        return outcome

    def queue_group(self, adapter, group, team, epoch, text, authorize):
        """Queue fixed activation guidance without blocking native acknowledgements.

        Args:
            adapter: Current authenticated transport.
            group: Newly activated business group.
            team: Independent NIM team verified during activation.
            epoch: Durable membership generation.
            text: Fixed activation instructions and command menu.
            authorize: Live and durable activation authorization guard.

        Returns:
            Whether this bounded queue accepted the welcome.
        """
        if self.closed or self.group_queue.full():
            logger.warning("Wangshangliao group welcome queue is unavailable")
            return False
        self.group_queue.put_nowait((adapter, group, team, epoch, text, authorize))
        if self.group_task is None or self.group_task.done():
            self.group_task = asyncio.create_task(self.deliver_groups())
        return True

    async def deliver_groups(self):
        """Drain a bounded welcome queue, never retrying an uncertain write."""
        while not self.group_queue.empty() and not self.closed:
            args = self.group_queue.get_nowait()
            try:
                for _ in range(10):
                    result = await self.welcome_group(*args)
                    if result != "busy":
                        break
                    await asyncio.sleep(3)
                if result not in {"accepted", "verified", "already_sent"}:
                    logger.warning("Wangshangliao group welcome was not confirmed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Wangshangliao group welcome stopped before completion")
            finally:
                self.group_queue.task_done()

    async def welcome_group(self, adapter, group, team, epoch, text, authorize):
        """Claim one activation generation before sending a menu and two images.

        Args:
            adapter: Native account that activated this group.
            group: Verified business group identifier.
            team: Independent signed NIM team identifier.
            epoch: Durable membership generation, not a runtime instance ID.
            text: Fixed menu text; no caller-generated media or AI content.
            authorize: Revocation-aware guard from the successful activation.

        Returns:
            Last delivery receipt or a safe non-delivery reason.
        """
        if self.active >= 2:
            return "busy"
        account = str(adapter.account)
        instance = adapter.config["id"]
        claimed = False
        outcome = "unknown"

        def check():
            if self.closed:
                raise ProtocolError("group_welcome_closed")
            authorize()

        check()
        self.active += 1
        try:
            async with AsyncDatabase(
                lambda: self.database(instance), write=True, guard=check
            ) as db:
                claimed = (
                    await db.execute(
                        "INSERT OR IGNORE INTO group_onboarding VALUES(?,?,?,?,'claimed')",
                        (account, group, epoch, time.time()),
                    )
                    == 1
                )
            if not claimed:
                return "already_sent"
            key = f"group-welcome/{account}/{group}/{epoch}"
            result = await send_onboarding_text(
                adapter, group, team, key, text, check, private=False
            )
            outcome = (
                await self.images.send_group(adapter, group, team, key, check)
                if result in {"accepted", "verified"}
                else "text_unconfirmed"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = "delivery_unconfirmed"
        finally:
            try:
                if claimed:
                    async with AsyncDatabase(
                        lambda: self.database(instance), write=True
                    ) as db:
                        await db.execute(
                            "UPDATE group_onboarding SET outcome=? "
                            "WHERE account=? AND group_id=? AND epoch=?",
                            (outcome, account, group, epoch),
                        )
            finally:
                self.active -= 1
        return outcome

    async def close(self):
        """Stop queued welcomes without replaying already claimed deliveries."""
        self.closed = True
        if self.group_task is not None:
            self.group_task.cancel()
            await asyncio.gather(self.group_task, return_exceptions=True)
        while not self.group_queue.empty():
            self.group_queue.get_nowait()
            self.group_queue.task_done()
