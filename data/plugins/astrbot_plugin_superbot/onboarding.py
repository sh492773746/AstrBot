"""Public onboarding copy and operator-owned navigation."""

import asyncio
import hashlib
import re
from html import escape
from pathlib import Path
from weakref import WeakValueDictionary

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter

ASSETS = Path(__file__).parent / "assets" / "welcome"


def invitation(runtime):
    """Build state-aware copy and links without granting any entitlement."""
    public = runtime.store.get("tenant_policy", {}).get("public", False)
    text = (
        "🎉 **把我加入群聊，免费使用群管理与积分娱乐功能！**\n"
        "群主将我设为管理员，通过「我的群」完成接入并选择功能。"
        if public
        else "🎉 **群管理与积分娱乐功能免费使用**\n当前分批开放，由真实群主申请接入。"
    )
    text += (
        "\n已接入群默认授权AI客服与制图，使用总控统一内容；"
        "仍受功能开关及服务限额约束。广告投递须先配置广告位。"
    )
    username = getattr(runtime.bot, "username", "")
    buttons = []
    if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        buttons.append(
            ("➕ 添加到群聊", {"url": f"https://t.me/{username}?startgroup"})
        )
    buttons.append(("🏪 我的群", {"action": "tenant_home"}))
    return text, buttons


class Onboarding:
    """Send local media once, keeping durable intent before network calls."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.locks = WeakValueDictionary()

    async def private(self, update, keyboard):
        """Welcome a user once without republishing a second menu.

        Args:
            update: Private update.
            keyboard: Existing three-entry reply keyboard.

        Returns:
            Whether this first-welcome operation was claimed.
        """
        if update.effective_chat.type != "private":
            return False
        key = "welcome/private/" + str(update.effective_user.id)
        async with self.locks.setdefault(key, asyncio.Lock()):
            previous = self.store.get(key)
            if previous and previous.get("status") != "failed":
                return False
            name = escape(
                " ".join(
                    (getattr(update.effective_user, "full_name", "") or "朋友").split()
                )[:80]
            )
            username = self.runtime.bot.username
            caption = (
                f"<b>🌊 欢迎使用大海传媒超级机器人</b>\n你好，{name}！\n\n"
                "群管理、积分娱乐、便捷查询与群活动。\n"
                "<blockquote expandable>加入超级群并设为管理员后使用。\n"
                "群主直接管理；其他群管须由群主指定授权。\n"
                "积分仅用于群内娱乐；AI、制图和广告另行配置。</blockquote>\n"
                "「我的群」设置 · 「帮助」看说明 · 「客服」咨询。"
            )
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username or ""):
                caption += f'\n<a href="https://t.me/{username}?startgroup">➕ 把我添加到群聊</a>'
            await self.deliver(
                key, update.effective_chat.id, "private", caption, keyboard
            )
            return True

    async def group(self, chat, member, *, old_status=None, event=None):
        """Show one permission card per join and edit known cards on changes.

        Args:
            chat: Group identity.
            member: Current bot permissions.
            old_status: Previous bot status.
            event: Update identity for duplicate protection.
        """
        key = "welcome/group/" + str(chat.id)
        async with self.locks.setdefault(key, asyncio.Lock()):
            previous = self.store.get(key, {})
            if (
                event is not None
                and event == previous.get("event")
                and previous.get("status") != "failed"
            ):
                return
            if (
                isinstance(event, int)
                and isinstance(previous.get("event"), int)
                and event < previous["event"]
                and self.store.clock() - previous.get("at", 0) < 7 * 86400
            ):
                return
            if member.status in {"left", "kicked"}:
                with self.store.tx() as db:
                    self.store.put(
                        db,
                        key,
                        {
                            "status": "detached",
                            "event": event,
                            "at": self.store.clock(),
                        },
                    )
                return
            rejoined = (
                old_status in {"left", "kicked"} or previous.get("status") == "detached"
            )
            missing = [
                label
                for right, label in (
                    ("can_delete_messages", "删除消息"),
                    ("can_restrict_members", "限制成员"),
                    ("can_pin_messages", "置顶消息"),
                )
                if member.status != "creator"
                and (
                    member.status != "administrator"
                    or not getattr(member, right, False)
                )
            ]
            signature = [member.status, *missing]
            if not rejoined and (
                previous.get("signature") == signature
                and previous.get("status") in {"sent", "review", "sending"}
                or previous.get("status") in {"review", "sending"}
                and not previous.get("message")
            ):
                return
            caption = (
                f"<b>🎉 欢迎使用大海传媒超级机器人</b>\n本群：<b>{escape(str(chat.title or chat.id))}</b>\n\n"
                + (
                    "<b>⚠️ 请设为管理员并补齐权限</b>\n"
                    if missing
                    else "<b>✅ 管理员权限已就绪</b>\n"
                )
                + "<blockquote expandable>需要：发送消息、删除消息、限制成员、置顶消息。\n"
                + (
                    f"尚缺：{'、'.join(missing)}。\n设置路径：群资料 → 管理员 → 添加本机器人。\n"
                    if missing
                    else ""
                )
                + "群主直接管理；其他群管须由群主指定授权。\n"
                "具体功能以本群设置为准，虚拟键盘需主动开启。</blockquote>\n"
                "点下方进入本群管理；普通成员发送「帮助」。"
            )
            username = self.runtime.bot.username
            markup = (
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⚙️ 进入本群管理",
                                url=f"https://t.me/{username}?start=manage_{chat.id}",
                            )
                        ]
                    ]
                )
                if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username or "")
                else None
            )
            await self.deliver(
                key,
                chat.id,
                "group",
                caption,
                markup,
                previous={} if rejoined else previous,
                event=event,
                signature=signature,
            )

    async def deliver(
        self, key, chat, kind, caption, markup, *, previous=None, **metadata
    ):
        """Persist receipts, cache media IDs and avoid replaying uncertain sends.

        Args:
            key: Durable operation identity.
            chat: Destination.
            kind: Local asset name.
            caption: Escaped owned HTML.
            markup: Reply keyboard or inline navigation.
            previous: Known message for editing.
            **metadata: Membership event and permission signature.
        """
        previous = previous or {}
        state = {**previous, **metadata, "status": "sending", "at": self.store.clock()}
        with self.store.tx() as db:
            self.store.put(db, key, state)
        bot, path = self.runtime.bot, ASSETS / f"{kind}-welcome.jpg"
        try:
            message, media = previous.get("message"), previous.get("media", False)
            if message:
                try:
                    await (
                        bot.edit_message_caption if media else bot.edit_message_text
                    )(
                        chat_id=chat,
                        message_id=message,
                        **{"caption" if media else "text": caption},
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                except BadRequest as exc:
                    reason = str(exc).lower()
                    if "message is not modified" not in reason:
                        if not any(
                            word in reason
                            for word in (
                                "message to edit not found",
                                "message_id_invalid",
                            )
                        ):
                            raise
                        message = None
            if not message:
                media = False
                try:
                    asset = path.read_bytes()
                except OSError as exc:
                    asset = None
                    self.runtime.report("welcome_asset", exc)
                if asset:
                    cache = (
                        f"welcome/file/{bot.id}/" + hashlib.sha256(asset).hexdigest()
                    )
                    file_id = self.store.get(cache)
                    try:
                        sent = await bot.send_photo(
                            chat_id=chat,
                            photo=file_id or asset,
                            filename=path.name,
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=markup,
                        )
                        media = True
                    except BadRequest as exc:
                        if not any(
                            word in str(exc).lower()
                            for word in ("file", "photo", "image", "media")
                        ):
                            raise
                        with self.store.tx() as db:
                            self.store.put(db, cache, None)
                if not media:
                    sent = await bot.send_message(
                        chat_id=chat,
                        text=caption,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                message = getattr(sent, "message_id", None)
                if type(message) is not int or message <= 0:
                    raise RuntimeError("MissingWelcomeReceipt")
                if media and getattr(sent, "photo", None):
                    with self.store.tx() as db:
                        self.store.put(db, cache, sent.photo[-1].file_id)
            state.update(status="sent", message=message, media=media)
            state.pop("error", None)
            with self.store.tx() as db:
                self.store.put(db, key, state)
                if kind == "group" and getattr(self.runtime, "group_game", None):
                    from .message_policy import retention

                    db.execute(
                        "INSERT OR IGNORE INTO gt_delete(chat,message,source,due) VALUES(?,?,0,?)",
                        (
                            str(chat),
                            message,
                            self.store.clock() + retention(self.runtime, "help"),
                        ),
                    )
            if kind == "private" and isinstance(
                getattr(self.runtime, "keyboard_versions", None), dict
            ):
                self.runtime.keyboard_versions[str(chat)] = str(markup.to_dict())
        except BaseException as exc:
            state.update(
                status="failed"
                if isinstance(exc, (BadRequest, Forbidden, RetryAfter))
                else "review",
                error=type(exc).__name__,
            )
            with self.store.tx() as db:
                self.store.put(db, key, state)
            if not isinstance(exc, Exception):
                raise
            self.runtime.report("welcome", exc)
