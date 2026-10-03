"""Shared Telegram group management, without an AstrBot AI event path."""

import asyncio
import json
import re
import time
from datetime import datetime, timezone

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import BaseHandler

from astrbot.api import logger

from . import adapter
from .group_store import GroupStore
from .tenants import Denied

ADMIN = {"administrator", "creator"}
PERMISSIONS = tuple(ChatPermissions.all_permissions().to_dict())


class GroupHandler(BaseHandler):
    def __init__(self, plugin, platform_id):
        self.plugin = plugin
        self.platform_id = platform_id
        super().__init__(self.dispatch)

    def check_update(self, update):
        if getattr(self.plugin, "closed", False):
            return False
        chat = getattr(update, "effective_chat", None)
        if getattr(update, "my_chat_member", None):
            return True
        if chat and chat.type in {"group", "supergroup"}:
            return True
        query = getattr(update, "callback_query", None)
        if query and (query.data or "").startswith("gc:"):
            return True
        msg = getattr(update, "message", None)
        text = (getattr(msg, "text", None) or "").strip()
        if text == "群管理" or text.split("@", 1)[0].split(" ", 1)[0] in {
            "/groups",
            "/cancel",
        }:
            return True
        groups = getattr(self.plugin, "group_control", None)
        user = getattr(update, "effective_user", None)
        if (
            groups
            and chat
            and chat.type == "private"
            and user
            and text
            and not text.startswith("/")
        ):
            return bool(
                groups.store.db.execute(
                    "SELECT 1 FROM confirmations WHERE platform=? AND actor=? AND used=0 "
                    "AND expires>? AND json_extract(payload,'$.action')='input'",
                    (self.platform_id, str(user.id), time.time()),
                ).fetchone()
            )
        return False

    async def dispatch(self, update, context):
        from telegram.ext import ApplicationHandlerStop

        await self.plugin.group_control.dispatch(self.platform_id, update, context)
        raise ApplicationHandlerStop


class GroupControl:
    def __init__(self, plugin):
        self.plugin = plugin
        self.store = GroupStore(adapter.state_path().with_name("group_control.db"))
        self.active = set()
        self.task = None
        self.closed = False
        self.watch_offset = 0

    def service(self, platform_id, bot, *, active=False):
        platform = self.plugin.context.get_platform_inst(platform_id)
        if (
            self.closed
            or self.plugin.closed
            or platform is None
            or platform.config.get("type") != "telethon_ai_service"
            or platform.config.get("telegram_required_plugin") != adapter.NAME
            or platform.config.get("telegram_dedicated_reporting") is not True
            or platform.application.bot is not bot
            or str(bot.id) != platform.config["telegram_token"].split(":", 1)[0]
            or not platform.application.bot_data.get(adapter.NAME + ":ready")
            or platform.config.get("service_role")
            != (
                "controller"
                if platform_id
                == self.plugin.config.get("control_platform_id", "VIP_DHBot")
                else "customer"
            )
        ):
            raise Denied("Service identity invalid")
        if platform_id == self.plugin.config.get("control_platform_id", "VIP_DHBot"):
            admins = self.plugin.config.get("admin_ids", [])
            if not admins or not str(admins[0]).isdigit():
                raise Denied("Controller owner is not configured")
            return {
                "owner": str(admins[0]),
                "tenant": None,
                "group_limit": None,
                "expires": None,
                "public": True,
            }
        row = self.plugin.tenants.db.execute(
            "SELECT * FROM tenants WHERE platform=? AND bot=?",
            (platform_id, str(bot.id)),
        ).fetchone()
        if row is None or (active and row["expires"] <= time.time()):
            raise Denied("Clone expired or not registered")
        manager = self.plugin.context.astrbot_config_mgr
        conf_id = manager.ucr.get_conf_id_for_umop(f"{platform_id}:FriendMessage:0")
        conf = manager.confs.get(conf_id)
        if (
            conf_id != manager.ucr.umop_to_conf_id.get(f"{platform_id}::")
            or manager.ucr.get_conf_id_for_umop(f"{platform_id}:GroupMessage:0")
            != conf_id
            or not conf
            or conf.get("admins_id") != []
            or conf.get("disable_builtin_commands") is not True
            or conf.get("plugin_set") != [adapter.NAME]
            or conf.get("provider_settings", {}).get("enable") is not False
            or conf.get("kb_names") != []
            or conf.get("dashboard", {}).get("enable") is not False
        ):
            raise Denied("Clone profile isolation invalid")
        return {**dict(row), "tenant": row["id"], "public": False}

    async def member(self, bot, chat, user):
        async with asyncio.timeout(8):
            return await bot.get_chat_member(int(chat), int(user))

    async def authorize(
        self, platform, bot, chat, actor, *, owner=False, active=True, right=None
    ):
        """Recheck ownership, actual member roles, Bot rights and current expiry.

        Raises:
            Denied: The binding, actor, Bot privilege, or entitlement is invalid.
        """
        scope = self.service(platform, bot, active=active)
        binding = self.store.binding(platform, chat)
        if (
            binding["bot"] != str(bot.id)
            or bool(binding["public"]) != scope["public"]
            or (not scope["public"] and binding["owner"] != scope["owner"])
            or binding["tenant"] != scope["tenant"]
            or (owner and not scope["public"] and str(actor) != scope["owner"])
        ):
            raise Denied("Group ownership mismatch")
        actor_member, bot_member = await asyncio.gather(
            self.member(bot, chat, actor), self.member(bot, chat, bot.id)
        )
        if actor_member.status not in ADMIN or bot_member.status not in ADMIN:
            raise Denied("Current Telegram administrator required")
        if right and not getattr(bot_member, right, False):
            raise Denied("Bot administrator privilege missing")
        scope = self.service(platform, bot, active=active)
        current = self.store.binding(platform, chat)
        if any(
            current[key] != binding[key] for key in ("owner", "bot", "tenant", "public")
        ):
            raise Denied("Binding changed during authorization")
        if scope["public"]:
            with self.store.db:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO access_hints VALUES(?,?,?)",
                    (platform, str(chat), str(actor)),
                )
        return current

    def button(self, platform, bot, actor, chat, label, action, **payload):
        token = self.store.token(
            platform, bot.id, actor, chat, action=action, **payload
        )
        return InlineKeyboardButton(label, callback_data="gc:" + token)

    async def menu(self, platform, bot, actor, chat=None):
        scope = self.service(platform, bot)
        if chat is None:
            if scope["public"]:
                items = self.store.db.execute(
                    "SELECT b.chat,b.title FROM bindings b JOIN access_hints a "
                    "ON b.platform=a.platform AND b.chat=a.chat "
                    "WHERE b.platform=? AND b.public=1 AND a.actor=? ORDER BY b.chat LIMIT 50",
                    (platform, str(actor)),
                ).fetchall()
            else:
                items = self.store.db.execute(
                    "SELECT chat,title FROM bindings WHERE platform=? ORDER BY chat",
                    (platform,),
                ).fetchall()
            buttons = []
            for row in items:
                try:
                    if scope["public"] or str(actor) != scope["owner"]:
                        await self.authorize(
                            platform, bot, row["chat"], actor, active=False
                        )
                except Exception:
                    continue
                buttons.append(
                    [
                        self.button(
                            platform, bot, actor, row["chat"], row["title"][:40], "menu"
                        )
                    ]
                )
            text = "群管理\n" + (
                "请选择群。"
                if buttons
                else f"暂无可管理的群。群内发送 /bindgroup@{bot.username} 发起绑定。\n"
                f"已绑定群的新管理员可在群内发送 /groups@{bot.username} 打开本群菜单。"
            )
        else:
            verified = True
            try:
                binding = await self.authorize(platform, bot, chat, actor, active=False)
            except (Denied, BadRequest, Forbidden, RetryAfter, TimeoutError):
                binding = self.store.binding(platform, chat)
                if (
                    binding["public"]
                    or str(actor) != scope["owner"]
                    or binding["owner"] != scope["owner"]
                ):
                    raise
                verified = False
            expired = scope["expires"] is not None and scope["expires"] <= time.time()
            auto = binding["automatic"] or "未指定"
            text = (
                f"群管理 · {binding['title']}\n群号：{chat}\n"
                f"状态：{'服务已到期' if expired else '有效'}\n自动执行：{auto}\n"
                f"欢迎：{'开启' if binding['welcome_enabled'] else '关闭'}\n"
                f"关键词：{'开启' if binding['keywords_enabled'] else '关闭'}\n"
                f"权限：{'当前已核验' if verified else '未核验，仅保留所有者解绑'}\n"
                f"自动功能状态：{binding['disabled_reason'] or '未暂停'}"
            )
            buttons = [
                [
                    self.button(platform, bot, actor, chat, "查看群规", "rules"),
                    self.button(platform, bot, actor, chat, "操作记录", "history"),
                ]
            ]
            if not expired and verified:
                buttons += [
                    [
                        self.button(
                            platform,
                            bot,
                            actor,
                            chat,
                            "编辑群规",
                            "prompt",
                            field="rules",
                        ),
                        self.button(
                            platform,
                            bot,
                            actor,
                            chat,
                            "编辑欢迎语",
                            "prompt",
                            field="welcome",
                        ),
                    ],
                    [
                        self.button(
                            platform,
                            bot,
                            actor,
                            chat,
                            "欢迎开关",
                            "toggle",
                            field="welcome_enabled",
                        ),
                        self.button(
                            platform,
                            bot,
                            actor,
                            chat,
                            "关键词开关",
                            "toggle",
                            field="keywords_enabled",
                        ),
                    ],
                    [
                        self.button(
                            platform,
                            bot,
                            actor,
                            chat,
                            "添加关键词",
                            "prompt",
                            field="keyword",
                        ),
                        self.button(
                            platform, bot, actor, chat, "查看关键词", "keywords"
                        ),
                    ],
                ]
                if scope["public"] or str(actor) == scope["owner"]:
                    buttons.append(
                        [
                            self.button(
                                platform,
                                bot,
                                actor,
                                chat,
                                "设为自动执行 Bot",
                                "auto_prompt",
                            )
                        ]
                    )
            if (scope["public"] and verified) or (
                not scope["public"] and str(actor) == scope["owner"]
            ):
                buttons.append(
                    [
                        self.button(
                            platform, bot, actor, chat, "解绑此 Bot", "unbind_prompt"
                        )
                    ]
                )
            buttons.append(
                [self.button(platform, bot, actor, chat, "返回群列表", "list")]
            )
        await bot.send_message(
            int(actor),
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=None,
            disable_web_page_preview=True,
        )

    async def dispatch(self, platform, update, context):
        task = asyncio.current_task()
        self.active.add(task)
        try:
            await self.handle(platform, update, context)
        except asyncio.CancelledError:
            raise
        except (Denied, BadRequest, Forbidden, RetryAfter, TimeoutError):
            message = getattr(update, "effective_message", None)
            if message:
                try:
                    await message.reply_text(
                        "操作未完成：请检查服务期限、当前群管理员身份、Bot 权限及绑定。"
                        "确认按钮仅限本人在五分钟内使用一次。",
                        parse_mode=None,
                    )
                except Exception:
                    pass
        except Exception:
            logger.error("Group operation rejected; internal details withheld")
        finally:
            self.active.discard(task)

    async def handle(self, platform, update, context):
        bot = context.bot
        self.service(platform, bot)
        changed = getattr(update, "my_chat_member", None)
        if changed:
            try:
                binding = self.store.binding(platform, changed.chat.id)
            except Denied:
                return
            if changed.new_chat_member.status not in ADMIN:
                self.store.disable(platform, binding["chat"], "Bot 已被移除或降权")
            return
        chat, actor = update.effective_chat, update.effective_user
        message = getattr(update, "message", None)
        if not chat or not actor or actor.is_bot:
            return
        if message and getattr(message, "sender_chat", None):
            return
        if chat.type == "private":
            if str(chat.id) != str(actor.id):
                return
            if update.callback_query:
                await self.callback(platform, bot, actor.id, update.callback_query)
                return
            text = (getattr(message, "text", None) or "").strip()
            head = text.split()[0] if text else ""
            command, _, target = head.partition("@")
            if target and target.lower() != bot.username.lower():
                return
            if text == "群管理" or command == "/groups":
                await self.menu(platform, bot, actor.id)
                return
            pending = self.store.db.execute(
                "SELECT token FROM confirmations WHERE platform=? AND bot=? AND actor=? "
                "AND used=0 AND expires>? AND json_extract(payload,'$.action')='input' "
                "ORDER BY expires DESC LIMIT 1",
                (platform, str(bot.id), str(actor.id), time.time()),
            ).fetchone()
            if pending:
                group, payload = self.store.consume(
                    pending["token"], platform, bot.id, actor.id
                )
                if command == "/cancel":
                    await message.reply_text("已取消。")
                    return
                await self.authorize(platform, bot, group, actor.id)
                field = payload["field"]
                if field == "keyword":
                    parts = text.split("|", 2)
                    if len(parts) != 3 or parts[0].strip() not in {"精确", "包含"}:
                        raise Denied("Expected keyword format")
                    self.store.add_keyword(
                        actor.id,
                        platform,
                        group,
                        "exact" if parts[0].strip() == "精确" else "contains",
                        parts[1],
                        parts[2].strip(),
                    )
                else:
                    self.store.set_config(actor.id, platform, group, field, text)
                await message.reply_text("已保存；保存内容不会自动开启功能。")
                await self.menu(platform, bot, actor.id, group)
            return
        if chat.type not in {"group", "supergroup"} or not message:
            return
        text = (message.text or "").strip()
        head, _, args = text.partition(" ")
        command, _, target = head.partition("@")
        if text.startswith("/") and (
            not target or target.lower() != bot.username.lower()
        ):
            return
        if command == "/bindgroup":
            scope = self.service(platform, bot, active=True)
            user, myself = await asyncio.gather(
                self.member(bot, chat.id, actor.id), self.member(bot, chat.id, bot.id)
            )
            if user.status not in ADMIN or myself.status not in ADMIN:
                raise Denied("Administrator required")
            existing = self.store.db.execute(
                "SELECT owner FROM claims WHERE chat=?", (str(chat.id),)
            ).fetchone()
            if (
                not scope["public"]
                and existing
                and existing["owner"] not in {"", scope["owner"]}
            ):
                raise Denied("Group belongs to another owner")
            confirmer = str(actor.id) if scope["public"] else scope["owner"]
            token = self.store.token(
                platform,
                bot.id,
                confirmer,
                chat.id,
                action="bind",
                public=scope["public"],
            )
            await bot.send_message(
                int(confirmer),
                f"确认绑定群：{chat.title}\n群号：{chat.id}\n"
                + (
                    "仅开通本群公共群管，不授予平台管理员权限。\n"
                    if scope["public"]
                    else ""
                )
                + "绑定不授权 Telethon AI，不自动开启欢迎或关键词。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("确认绑定", callback_data="gc:" + token)]]
                ),
                parse_mode=None,
            )
            await message.reply_text(
                "请在机器人私聊中确认本群授权；尚未绑定或开启自动功能。"
                if scope["public"]
                else "绑定请求已发送给机器人所有者，请在私聊确认。"
            )
            return
        try:
            binding = self.store.binding(platform, chat.id)
        except Denied:
            return
        scope = self.service(platform, bot)
        if (
            binding["bot"] != str(bot.id)
            or bool(binding["public"]) != scope["public"]
            or binding["tenant"] != scope["tenant"]
            or (not scope["public"] and binding["owner"] != scope["owner"])
        ):
            raise Denied("Binding identity mismatch")
        if command == "/groups":
            await self.authorize(platform, bot, chat.id, actor.id, active=False)
            await self.menu(platform, bot, actor.id, chat.id)
            await message.reply_text("群管理菜单已发送到你的机器人私聊。")
            return
        if command in {"/rules", "/help"}:
            await message.reply_text(
                binding["rules"] or "尚未设置群规。"
                if command == "/rules"
                else f"/rules@{bot.username} 群规\n/groups@{bot.username} 私聊管理\n"
                "管理员可回复目标消息使用 /del、/mute 时长、/unmute；命令须带 @Bot用户名。",
                parse_mode=None,
                disable_web_page_preview=True,
            )
            return
        if command in {"/del", "/mute", "/unmute"}:
            await self.moderate(
                platform, bot, actor.id, chat.id, message, command, args.strip()
            )
            return
        if text.startswith("/"):
            return
        if binding["automatic"] != platform or binding["disabled_reason"]:
            return
        await self.automatic(platform, bot, binding, message)

    async def callback(self, platform, bot, actor, query):
        await query.answer()
        chat, payload = self.store.consume(
            query.data.removeprefix("gc:"), platform, bot.id, actor
        )
        action = payload["action"]
        if action == "bind":
            scope = self.service(platform, bot, active=True)
            if bool(payload.get("public")) != scope["public"]:
                raise Denied("Binding authorization mode changed")
            if not scope["public"] and str(actor) != scope["owner"]:
                raise Denied("Only the owner can bind")
            owner, myself, group = await asyncio.gather(
                self.member(bot, chat, actor),
                self.member(bot, chat, bot.id),
                bot.get_chat(int(chat)),
            )
            if (
                owner.status not in ADMIN
                or myself.status not in ADMIN
                or group.type not in {"group", "supergroup"}
                or str(group.id) != str(chat)
            ):
                raise Denied("Current administrators required")
            scope = self.service(platform, bot, active=True)
            if not scope["public"] and str(actor) != scope["owner"]:
                raise Denied("Owner changed during confirmation")
            binding_owner = str(actor)
            if scope["public"]:
                previous = self.store.db.execute(
                    "SELECT owner,public FROM bindings WHERE platform=? AND chat=?",
                    (platform, str(chat)),
                ).fetchone()
                if previous:
                    if not previous["public"]:
                        raise Denied("Legacy platform binding needs operator review")
                    binding_owner = previous["owner"]
            self.store.bind(
                platform,
                bot.id,
                binding_owner,
                scope["tenant"],
                chat,
                group.title,
                scope["group_limit"],
                public=scope["public"],
            )
            with self.store.db:
                if scope["public"]:
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO access_hints VALUES(?,?,?)",
                        (platform, str(chat), str(actor)),
                    )
                self.store.audit(
                    actor,
                    platform,
                    chat,
                    "group_access_confirmed",
                    public=scope["public"],
                )
            await self.menu(platform, bot, actor, chat)
            return
        if action == "list":
            await self.menu(platform, bot, actor)
            return
        if action == "menu":
            await self.menu(platform, bot, actor, chat)
            return
        active = action not in {
            "rules",
            "history",
            "keywords",
            "unbind_prompt",
            "unbind",
        }
        if action in {"unbind", "unbind_prompt"}:
            scope = self.service(platform, bot)
            binding = self.store.binding(platform, chat)
            if scope["public"] and binding["public"]:
                await self.authorize(platform, bot, chat, actor, active=False)
            elif str(actor) != scope["owner"] or binding["owner"] != scope["owner"]:
                raise Denied("Only the owner can release a binding")
            if action == "unbind_prompt":
                await bot.send_message(
                    int(actor),
                    "确认解除此 Bot 的本地群管绑定？不修改群权限或 Telethon AI 授权。",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                self.button(
                                    platform, bot, actor, chat, "确认解绑", "unbind"
                                )
                            ]
                        ]
                    ),
                    parse_mode=None,
                )
            else:
                self.store.unbind(actor, platform, chat)
                await self.menu(platform, bot, actor)
            return
        binding = await self.authorize(
            platform,
            bot,
            chat,
            actor,
            owner=action in {"auto", "auto_prompt"},
            active=active,
        )
        if action == "auto_prompt":
            await bot.send_message(
                int(actor),
                "请确认：切换本群自动执行 Bot。",
                reply_markup=InlineKeyboardMarkup(
                    [[self.button(platform, bot, actor, chat, "确认", "auto")]]
                ),
                parse_mode=None,
            )
        elif action == "auto":
            self.store.set_automatic(actor, platform, chat)
            await self.menu(platform, bot, actor, chat)
        elif action == "toggle":
            field = payload["field"]
            if field not in {"welcome_enabled", "keywords_enabled"}:
                raise Denied("Unknown switch")
            if (
                field == "welcome_enabled"
                and not binding["welcome"]
                and not binding[field]
            ):
                raise Denied("Welcome text is empty")
            self.store.set_config(
                actor, platform, chat, field, not bool(binding[field])
            )
            await self.menu(platform, bot, actor, chat)
        elif action == "prompt":
            field = payload["field"]
            if field not in {"rules", "welcome", "keyword"}:
                raise Denied("Unknown input")
            with self.store.db:
                self.store.db.execute(
                    "UPDATE confirmations SET used=1 WHERE platform=? AND actor=? "
                    "AND json_extract(payload,'$.action')='input'",
                    (platform, str(actor)),
                )
            self.store.token(platform, bot.id, actor, chat, action="input", field=field)
            await bot.send_message(
                int(actor),
                "请发送：精确|关键词|回复内容 或 包含|关键词|回复内容。发送 /cancel 取消。"
                if field == "keyword"
                else "请发送新文本（最多 3000 字）。发送 /cancel 取消。",
                parse_mode=None,
            )
        elif action == "rules":
            await bot.send_message(
                int(actor), binding["rules"] or "尚未设置群规。", parse_mode=None
            )
        elif action == "history":
            rows = self.store.db.execute(
                "SELECT at,action,actor,details FROM audit WHERE platform=? AND chat=? ORDER BY id DESC LIMIT 10",
                (platform, str(chat)),
            ).fetchall()
            text = "\n".join(
                f"{datetime.fromtimestamp(r['at'], timezone.utc).isoformat(timespec='seconds')} "
                f"{r['action']} · 用户 {r['actor']}\n{r['details']}"
                for r in rows
            )
            await bot.send_message(
                int(actor), text[:3900] or "暂无记录。", parse_mode=None
            )
        elif action == "keywords":
            rows = self.store.db.execute(
                "SELECT id,mode,phrase FROM keywords WHERE platform=? AND chat=? ORDER BY id",
                (platform, str(chat)),
            ).fetchall()
            buttons = [
                [
                    self.button(
                        platform,
                        bot,
                        actor,
                        chat,
                        ("精确" if r["mode"] == "exact" else "包含")
                        + " · "
                        + r["phrase"][:25],
                        "keyword_delete_prompt",
                        keyword=r["id"],
                    )
                ]
                for r in rows
            ]
            await bot.send_message(
                int(actor),
                "关键词列表" if rows else "暂无关键词。",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=None,
            )
        elif action == "keyword_delete_prompt":
            await bot.send_message(
                int(actor),
                "确认删除此关键词？",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            self.button(
                                platform,
                                bot,
                                actor,
                                chat,
                                "删除",
                                "keyword_delete",
                                keyword=payload["keyword"],
                            )
                        ]
                    ]
                ),
                parse_mode=None,
            )
        elif action == "keyword_delete":
            with self.store.db:
                cursor = self.store.db.execute(
                    "DELETE FROM keywords WHERE id=? AND platform=? AND chat=?",
                    (payload["keyword"], platform, str(chat)),
                )
                if cursor.rowcount != 1:
                    raise Denied("Keyword not found")
                self.store.audit(
                    actor, platform, chat, "keyword_deleted", keyword=payload["keyword"]
                )
            await self.menu(platform, bot, actor, chat)
        else:
            raise Denied("Unknown group action")

    async def automatic(self, platform, bot, binding, message):
        try:
            scope = self.service(platform, bot, active=True)
            member = await self.member(bot, binding["chat"], bot.id)
            if member.status not in ADMIN:
                raise Denied("Bot is not an administrator")
            if (
                bool(binding["public"]) != scope["public"]
                or binding["tenant"] != scope["tenant"]
                or binding["bot"] != str(bot.id)
                or (not scope["public"] and binding["owner"] != scope["owner"])
            ):
                raise Denied("Group ownership changed")
        except Exception:
            self.store.disable(platform, binding["chat"], "服务失效或 Bot 权限核验失败")
            return
        self.service(platform, bot, active=True)
        binding = self.store.binding(platform, binding["chat"])
        if binding["automatic"] != platform or binding["disabled_reason"]:
            return
        text = None
        if message.new_chat_members:
            if binding["welcome_enabled"] and any(
                not u.is_bot for u in message.new_chat_members
            ):
                text = binding["welcome"]
        elif binding["keywords_enabled"] and message.text:
            text = self.store.match(platform, binding["chat"], message.text.strip())
        if not text:
            return
        key = f"auto:{platform}:{binding['chat']}:{message.message_id}"
        if not self.store.claim_operation(
            key, platform, binding["chat"], "system", "automatic_reply"
        ):
            return
        if not self.store.admit_reply(platform, binding["chat"]):
            self.store.settle(key, "failed", reason="reply_cooldown")
            return
        try:
            await bot.send_message(
                int(binding["chat"]),
                text,
                parse_mode=None,
                disable_web_page_preview=True,
                reply_to_message_id=message.message_id,
            )
        except (BadRequest, Forbidden, RetryAfter):
            self.store.settle(key, "failed", reason="telegram_rejected")
        except BaseException:
            self.store.settle(key, "uncertain", reason="delivery_unconfirmed")
            self.store.disable(platform, binding["chat"], "回复投递不确定，需人工复核")
            raise
        else:
            self.store.settle(key, "sent")

    @staticmethod
    def snapshot(member):
        return json.dumps(
            {
                "status": member.status,
                "until": int(member.until_date.timestamp())
                if getattr(member, "until_date", None)
                else 0,
                "is_member": getattr(member, "is_member", None),
                "permissions": {p: getattr(member, p, None) for p in PERMISSIONS},
            },
            sort_keys=True,
        )

    async def moderate(self, platform, bot, actor, chat, message, command, args):
        right = "can_delete_messages" if command == "/del" else "can_restrict_members"
        await self.authorize(platform, bot, chat, actor, right=right)
        reply = message.reply_to_message
        target = reply.from_user if reply else None
        if (
            not target
            or target.is_bot
            or reply.sender_chat
            or str(target.id) == str(actor)
        ):
            raise Denied("Reply to a member's message")
        member = await self.member(bot, chat, target.id)
        if member.status in ADMIN:
            raise Denied("Cannot punish an administrator")
        unresolved = self.store.db.execute(
            "SELECT 1 FROM operations WHERE platform=? AND chat=? AND state='uncertain' "
            "AND kind IN ('/mute','/unmute','/del') AND json_extract(details,'$.target')=?",
            (platform, str(chat), str(target.id)),
        ).fetchone()
        if unresolved:
            raise Denied("Target has an uncertain operation requiring review")
        duration = 0
        prior = None
        baseline = None
        if command == "/mute":
            parsed = re.fullmatch(r"([1-9][0-9]{0,5})([mhd])", args)
            if not parsed or member.status != "member":
                raise Denied("Invalid duration or already restricted member")
            duration = int(parsed[1]) * {"m": 60, "h": 3600, "d": 86400}[parsed[2]]
            if not 60 <= duration <= 7 * 86400:
                raise Denied("Mute must last one minute to seven days")
            group = await bot.get_chat(int(chat))
            if group.type != "supergroup" or not group.permissions:
                raise Denied("Supergroup permissions are unavailable")
            baseline = group.permissions.to_dict()
        elif command == "/unmute":
            prior = self.store.db.execute(
                "SELECT * FROM mutes WHERE platform=? AND chat=? AND target=?",
                (platform, str(chat), str(target.id)),
            ).fetchone()
            if (
                prior is None
                or prior["until"] <= time.time()
                or prior["snapshot"] != self.snapshot(member)
            ):
                raise Denied("Restriction changed or was not applied by this Bot")
        await self.authorize(platform, bot, chat, actor, right=right)
        latest = await self.member(bot, chat, target.id)
        if self.snapshot(latest) != self.snapshot(member) or latest.status in ADMIN:
            raise Denied("Target permissions changed during authorization")
        self.service(platform, bot, active=True)
        key = f"mod:{platform}:{chat}:{message.message_id}:{command}"
        if not self.store.claim_operation(
            key, platform, chat, actor, command, target=str(target.id)
        ):
            return
        try:
            if command == "/del":
                result = await bot.delete_message(int(chat), reply.message_id)
                if result is False:
                    raise RuntimeError("Deletion was not acknowledged")
            elif command == "/mute":
                until = int(time.time()) + duration
                result = await bot.restrict_chat_member(
                    int(chat),
                    target.id,
                    ChatPermissions(**dict.fromkeys(PERMISSIONS, False)),
                    until_date=until,
                    use_independent_chat_permissions=True,
                )
                if result is False:
                    raise RuntimeError("Restriction was not acknowledged")
                actual = await self.member(bot, chat, target.id)
                if (
                    actual.status != "restricted"
                    or abs(actual.until_date.timestamp() - until) > 1
                ):
                    raise RuntimeError("Restriction could not be verified")
                with self.store.db:
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO mutes VALUES(?,?,?,?,?,?)",
                        (
                            platform,
                            str(chat),
                            str(target.id),
                            self.snapshot(actual),
                            json.dumps(baseline),
                            until,
                        ),
                    )
            else:
                current_group = await bot.get_chat(int(chat))
                if current_group.type != "supergroup" or not current_group.permissions:
                    raise Denied("Group permissions unavailable")
                actual = await self.member(bot, chat, target.id)
                if self.snapshot(actual) != prior["snapshot"]:
                    raise Denied("Restriction changed before restore")
                self.service(platform, bot, active=True)
                # Telegram requires all flags true to remove an individual
                # restriction. The member then remains subject to group defaults.
                result = await bot.restrict_chat_member(
                    int(chat),
                    target.id,
                    ChatPermissions.all_permissions(),
                    until_date=0,
                    use_independent_chat_permissions=True,
                )
                if result is False:
                    raise RuntimeError("Restore was not acknowledged")
                restored = await self.member(bot, chat, target.id)
                if restored.status != "member":
                    raise RuntimeError("Restriction removal could not be verified")
                with self.store.db:
                    self.store.db.execute(
                        "DELETE FROM mutes WHERE platform=? AND chat=? AND target=?",
                        (platform, str(chat), str(target.id)),
                    )
        except (BadRequest, Forbidden, RetryAfter, Denied):
            self.store.settle(key, "failed", reason="operation_rejected")
            raise
        except BaseException:
            self.store.settle(key, "uncertain", reason="result_unconfirmed")
            self.store.disable(platform, chat, "处罚结果不确定，需人工复核")
            raise
        else:
            self.store.settle(key, "sent", target=str(target.id))
            await message.reply_text("操作已执行。", parse_mode=None)

    async def watch(self):
        while not self.closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Group health check failed; details withheld")
            await asyncio.sleep(30)

    async def tick(self):
        rows = self.store.db.execute(
            "SELECT b.* FROM bindings b JOIN claims c USING(chat) WHERE c.automatic=b.platform "
            "ORDER BY b.platform,b.chat LIMIT 32 OFFSET ?",
            (self.watch_offset,),
        ).fetchall()
        self.watch_offset = self.watch_offset + 32 if len(rows) == 32 else 0
        for binding in rows:
            platform = self.plugin.context.get_platform_inst(binding["platform"])
            try:
                if platform is None:
                    raise Denied("Platform is stopped")
                self.service(binding["platform"], platform.application.bot, active=True)
                member = await self.member(
                    platform.application.bot,
                    binding["chat"],
                    platform.application.bot.id,
                )
                if member.status not in ADMIN:
                    raise Denied("Bot administrator permission lost")
            except Exception:
                self.store.disable(
                    binding["platform"],
                    binding["chat"],
                    "自动执行 Bot 停用、到期或权限核验失败",
                )
            current = self.store.binding(binding["platform"], binding["chat"])
            if not current["disabled_reason"]:
                continue
            controller = self.plugin.control.application
            if controller is None:
                continue
            key = f"notice:{current['platform']}:{current['chat']}:{current['generation']}"
            if not self.store.claim_operation(
                key, current["platform"], current["chat"], "system", "owner_notice"
            ):
                continue
            try:
                await controller.bot.send_message(
                    int(
                        self.plugin.config["admin_ids"][0]
                        if current["public"]
                        else current["owner"]
                    ),
                    f"群管自动功能已停止\n群：{current['title']}\n群号：{current['chat']}\n"
                    f"Bot：{current['platform']}\n原因：{current['disabled_reason']}\n"
                    "不会自动切换其他 Bot。请在该机器人私聊 /groups 查看。",
                    parse_mode=None,
                )
            except (BadRequest, Forbidden, RetryAfter):
                self.store.settle(key, "failed", reason="notification_rejected")
            except BaseException:
                self.store.settle(key, "uncertain", reason="notification_unconfirmed")
                raise
            else:
                self.store.settle(key, "sent")

    def status(self):
        bindings = [
            dict(row)
            for row in self.store.db.execute(
                "SELECT b.platform,b.chat,b.bot,b.owner,b.title,b.tenant,b.welcome_enabled,"
                "b.keywords_enabled,b.disabled_reason,b.public,c.automatic FROM bindings b JOIN claims c USING(chat) "
                "ORDER BY b.platform,b.chat LIMIT 100"
            )
        ]
        for binding in bindings:
            tenant = (
                self.plugin.tenants.db.execute(
                    "SELECT expires FROM tenants WHERE id=?", (binding["tenant"],)
                ).fetchone()
                if binding["tenant"]
                else None
            )
            binding["expired"] = bool(
                binding["tenant"] and (not tenant or tenant["expires"] <= time.time())
            )
        return {
            "bindings": bindings,
            "uncertain": self.store.db.execute(
                "SELECT COUNT(*) FROM operations WHERE state='uncertain'"
            ).fetchone()[0],
        }

    async def close(self):
        if self.closed:
            return
        self.closed = True
        tasks = self.active - {asyncio.current_task()}
        if self.task:
            tasks.add(self.task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
