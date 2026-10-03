"""Narrow private Telegram control bridge on the existing AstrBot adapter."""

import asyncio
import secrets
import sqlite3
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ManagedBotUpdatedHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from . import adapter
from .group_control import GroupHandler
from .tenants import Denied

GROUP = -74


class Control:
    def __init__(self, plugin):
        self.plugin = plugin
        self.application = None
        self.platform = None
        self.pending = {}
        self.active = set()
        self.handlers = [
            GroupHandler(plugin, plugin.config.get("control_platform_id", "VIP_DHBot")),
            CommandHandler(
                [
                    "tgai",
                    "tgaitrial",
                    "tgaiassign",
                    "tgaigroup",
                    "tgaienable",
                    "tgaidisable",
                    "tgaistop",
                    "tgaigrant",
                    "tgairetry",
                    "tgairequests",
                    "tgaiapprove",
                    "tgaireject",
                ],
                self.command,
            ),
            CallbackQueryHandler(self.confirm, pattern=r"^tgai:"),
        ]
        self.standalone = (
            plugin.config.get("controller_mode", "standalone") == "standalone"
        )
        if self.standalone:
            self.handlers.extend(
                [
                    CommandHandler(
                        ["start", "help", "bots", "plans", "create"], self.home
                    ),
                    ManagedBotUpdatedHandler(self.managed),
                    PreCheckoutQueryHandler(self.reject_payment),
                    MessageHandler(filters.ALL, self.ignore),
                ]
            )
        for handler in self.handlers:
            callback = handler.callback

            async def tracked(update, context, callback=callback):
                task = asyncio.current_task()
                self.active.add(task)
                try:
                    if self.plugin.closed:
                        raise ApplicationHandlerStop
                    return await callback(update, context)
                finally:
                    self.active.discard(task)

            handler.callback = tracked

    async def ignore(self, update, context):
        raise ApplicationHandlerStop

    def bind(self):
        platform = self.plugin.context.get_platform_inst(
            self.plugin.config.get("control_platform_id", "VIP_DHBot")
        )
        if platform is None or platform is self.platform:
            return
        required = ("register_application_hook", "unregister_application_hook")
        if self.standalone:
            required += ("suspend_required_plugin",)
        missing = [
            name for name in required if not callable(getattr(platform, name, None))
        ]
        if missing:
            raise RuntimeError(
                "Incompatible AstrBot Telegram adapter; required local interfaces missing: "
                + ", ".join(missing)
                + ". Use the plugin's telethon_ai_service controller platform instead. "
                "Existing Telegram platforms require an operator-reviewed migration."
            )
        self.detach()
        self.platform = platform
        platform.register_application_hook(adapter.NAME + ":control", self.attach)

    def attach(self, application):
        if self.application:
            for handler in self.handlers:
                self.application.remove_handler(handler, GROUP)
            self.application.bot_data.pop(adapter.NAME + ":ready", None)
        if (
            application is not None
            and self.standalone
            and application.bot_data.get("astrbot_plugin_tenant_control:ready")
        ):
            raise ValueError("Legacy controller still active; migration required")
        self.application = application
        if application is not None:
            for handler in self.handlers:
                application.add_handler(handler, GROUP)
            application.bot_data[adapter.NAME + ":ready"] = True

    async def publish_menu(self):
        if self.application is not None and self.standalone:
            await self.application.bot.set_my_commands(
                [
                    BotCommand("start", "服务首页"),
                    BotCommand("bots", "我的服务机器人"),
                    BotCommand("create", "创建服务机器人"),
                    BotCommand("plans", "套餐状态"),
                    BotCommand("groups", "群管理"),
                    BotCommand("tgai", "平台管理（仅超管）"),
                ]
            )

    async def home(self, update, context):
        if (
            self.plugin.closed
            or not update.message
            or not update.effective_user
            or update.effective_chat.type != "private"
        ):
            raise ApplicationHandlerStop
        owner = str(update.effective_user.id)
        command = update.message.text.split()[0].split("@")[0].lstrip("/")
        if command == "plans":
            text = "当前仅开放管理员审核的试用，正式套餐与收费未开启。"
        elif command == "bots":
            rows = self.plugin.tenants.db.execute(
                "SELECT t.bot,e.username,e.state FROM tenants t LEFT JOIN enrollments e ON e.tenant=t.id "
                "WHERE t.owner=?",
                (owner,),
            ).fetchall()
            text = (
                "\n".join(
                    f"@{r['username'] or r['bot']} · {r['state'] or '已登记'}"
                    for r in rows
                )
                or "暂无服务机器人。"
            )
        elif command == "create":
            grant = self.plugin.tenants.pending_grant(owner)
            if grant is None:
                text = "暂无有效试用资格，请联系平台申请。"
            else:
                manager = await context.bot.get_me()
                if not getattr(manager, "can_manage_bots", False):
                    await update.message.reply_text(
                        "总控尚未启用 Bot Management Mode。"
                    )
                    raise ApplicationHandlerStop
                await update.message.reply_text(
                    "创建你的服务入口 Bot。它仅提供账号服务管理，不开放平台后台权限。",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "创建服务机器人",
                                    url=f"https://t.me/newbot/{manager.username}/AIService{secrets.token_hex(5)}Bot?name=AI%20Service",
                                )
                            ]
                        ]
                    ),
                )
                raise ApplicationHandlerStop
        else:
            text = (
                "公共群管与 AI 账号服务\n/groups 群管理\n"
                "群主或管理员添加本 Bot 并授权后，在群内发送 "
                "/bindgroup@Bot用户名，自行私聊确认本群授权。\n"
                "/bots 我的机器人\n/create 创建服务入口\n"
                "/plans 套餐状态\n"
                + self.plugin.config.get("support_text", "请联系平台管理员")
            )
        await update.message.reply_text(text[:4000])
        raise ApplicationHandlerStop

    async def reject_payment(self, update, context):
        await update.pre_checkout_query.answer(
            ok=False, error_message="正式购买尚未开放。"
        )
        raise ApplicationHandlerStop

    async def managed(self, update, context):
        try:
            await self.plugin.enrollment.managed(update, context)
            await context.bot.send_message(
                update.managed_bot.user.id,
                "服务机器人已接入，账号分配及群授权仍待管理员审核。",
            )
        except Exception:
            # Do not log SDK exceptions that might include a Token or upstream URL.
            with self.plugin.tenants.db:
                self.plugin.tenants.db.execute(
                    "UPDATE enrollments SET state='review',error_code='activation_failed' WHERE bot=?",
                    (str(update.managed_bot.bot.id),),
                )
            await context.bot.send_message(
                update.managed_bot.user.id,
                "开通未完成，已保留登记记录；请联系平台核查，勿重复创建。",
            )
        raise ApplicationHandlerStop

    def detach(self):
        if self.platform:
            self.platform.unregister_application_hook(adapter.NAME + ":control")
        self.attach(None)
        self.platform = None

    def authorized(self, update):
        return bool(
            not self.plugin.closed
            and update.effective_chat
            and update.effective_chat.type == "private"
            and update.effective_user
            and str(update.effective_user.id) in self.plugin.config.get("admin_ids", [])
        )

    async def command(self, update, context):
        if not self.authorized(update):
            raise ApplicationHandlerStop
        actor = str(update.effective_user.id)
        command = update.message.text.split()[0].split("@")[0].lstrip("/")
        args = context.args
        try:
            if command == "tgai":
                tenants = self.plugin.tenants.db.execute(
                    "SELECT id FROM tenants"
                ).fetchall()
                lines = [
                    "Telethon AI 试用管理（正式购买关闭）",
                    "/tgaitrial 所有者ID BotID AIClient_名称",
                    "/tgaiassign 租户ID collector或keywords",
                    "/tgaigroup 租户ID -100群ID（仅授权审核后）",
                    "/tgaienable 租户ID /tgaidisable 租户ID",
                    "/tgaistop 紧急暂停全部账号",
                    "/tgaigrant 所有者ID 天数（1至7）",
                    "/tgairetry BotID 重试客户接入",
                    "/tgairequests 查看客户申请",
                    "/tgaiapprove 申请ID /tgaireject 申请ID",
                ]
                for item in tenants:
                    row = self.plugin.tenants.summary(item[0])
                    lines.append(
                        f"{row['id']} owner={row['owner']} "
                        f"用量={row['used']}/{row['budget']} enabled={row['enabled']}"
                    )
                await update.message.reply_text("\n".join(lines)[:4000])
            elif command == "tgaistop":
                for account in adapter.ACCOUNTS:
                    await self.plugin.pause(account)
                await update.message.reply_text("所有 AI 账号已紧急暂停。")
            elif command == "tgairequests":
                rows = self.plugin.tenants.db.execute(
                    "SELECT id,tenant,kind,value FROM requests WHERE state='pending' ORDER BY at LIMIT 20"
                ).fetchall()
                await update.message.reply_text(
                    "\n".join(
                        f"{r['id']} {r['tenant']} {r['kind']} {r['value']}"
                        for r in rows
                    )
                    or "暂无待审核申请。"
                )
            else:
                expected = {
                    "tgaitrial": 3,
                    "tgaiassign": 2,
                    "tgaigroup": 2,
                    "tgaienable": 1,
                    "tgaidisable": 1,
                    "tgaigrant": 2,
                    "tgairetry": 1,
                    "tgaiapprove": 1,
                    "tgaireject": 1,
                }
                if len(args) != expected[command]:
                    raise Denied("参数不正确，请先 /tgai")
                if command == "tgaiassign" and args[1] not in adapter.ACCOUNTS:
                    raise Denied("未知账号")
                # Bind confirmation to actor, chat, command and all arguments.
                now = time.time()
                self.pending = {k: v for k, v in self.pending.items() if v[0] > now}
                if len(self.pending) >= 100:
                    raise Denied("待确认操作过多")
                token = secrets.token_urlsafe(18)
                self.pending[token] = (
                    now + 300,
                    actor,
                    update.effective_chat.id,
                    command,
                    args,
                )
                await update.message.reply_text(
                    f"确认执行：/{command} {' '.join(args)}\n五分钟内有效；不会自动连接账号或入群。",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("确认", callback_data="tgai:" + token)]]
                    ),
                )
        except (Denied, ValueError, KeyError):
            await update.message.reply_text(
                "请求被拒绝：请核对参数和权限，发送 /tgai 查看格式。"
            )
        raise ApplicationHandlerStop

    async def confirm(self, update, context):
        if not self.authorized(update):
            await update.callback_query.answer("无权限", show_alert=True)
            raise ApplicationHandlerStop
        token = update.callback_query.data.removeprefix("tgai:")
        item = self.pending.get(token)
        actor = str(update.effective_user.id)
        if (
            not item
            or item[0] < time.time()
            or item[1:3] != (actor, update.effective_chat.id)
        ):
            await update.callback_query.answer("确认无效或已过期", show_alert=True)
            raise ApplicationHandlerStop
        self.pending.pop(token)
        _, _, _, command, args = item
        store = self.plugin.tenants
        try:
            if command in {"tgaiapprove", "tgaireject"}:
                store.review_request(actor, args[0], command == "tgaiapprove")
                result = "申请已审核，账号连接状态不变。"
            elif command == "tgaigrant":
                store.grant(actor, args[0], int(args[1]))
                result = "试用资格已签发，用户可在总控私聊发送 /create。"
            elif command == "tgairetry":
                await self.plugin.enrollment.retry(args[0])
                result = "客户接入已重试"
            elif command == "tgaitrial":
                # Require an already installed native Bot platform with matching identity.
                platform = self.plugin.context.get_platform_inst(args[2])
                bot = getattr(getattr(platform, "application", None), "bot", None)
                if bot is None or str(bot.id) != args[1]:
                    raise Denied("请先接入对应的客户 Bot 平台")
                profile = self.plugin.context.get_config(
                    f"{args[2]}:FriendMessage:{args[0]}"
                )
                if (
                    profile.get("provider_settings", {}).get("enable", True)
                    or profile.get("admins_id")
                    or not profile.get("disable_builtin_commands")
                    or profile.get("plugin_set") != [adapter.NAME]
                ):
                    raise Denied("客户 Bot 必须使用独立的无 LLM、无超管配置")
                result = store.create_trial(actor, *args)
                self.plugin.bind_customers()
            elif command == "tgaiassign":
                store.assign(actor, *args)
                result = "账号已独占分配"
            elif command == "tgaigroup":
                store.authorize_group(actor, *args)
                result = "已记录授权群；账号不会自动入群"
            else:
                store.set_enabled(actor, args[0], command == "tgaienable")
                result = "租户服务状态已更新；账号连接开关不变"
            await update.callback_query.answer("已完成")
            await update.callback_query.edit_message_text(str(result))
        except (Denied, sqlite3.IntegrityError, ValueError):
            await update.callback_query.answer(
                "操作被拒绝，请核对归属、额度及已有绑定", show_alert=True
            )
        raise ApplicationHandlerStop
