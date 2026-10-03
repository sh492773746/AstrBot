"""Customer Bot menus, bound to the actual adapter and authenticated owner."""

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, TypeHandler

from astrbot.api import logger

from . import adapter
from .group_control import GroupHandler
from .tenants import Denied


def isolated_profile(conf):
    return bool(
        conf
        and conf.get("admins_id") == []
        and conf.get("disable_builtin_commands") is True
        and conf.get("plugin_set") == [adapter.NAME]
        and conf.get("provider_settings", {}).get("enable") is False
        and conf.get("kb_names") == []
        and conf.get("dashboard", {}).get("enable") is False
    )


class Customer:
    def __init__(self, plugin, platform_id):
        self.plugin = plugin
        self.platform_id = platform_id
        self.platform = None
        self.application = None
        self.handler = TypeHandler(Update, self.dispatch)
        self.group_handler = GroupHandler(plugin, platform_id)

    async def dispatch(self, update, context):
        # A failed menu handler must never fall through to AstrBot or another plugin.
        try:
            if getattr(
                self.plugin, "group_control", None
            ) and self.group_handler.check_update(update):
                await self.plugin.group_control.dispatch(
                    self.platform_id, update, context
                )
                raise ApplicationHandlerStop
            await self.message(update, context)
        except ApplicationHandlerStop:
            raise
        except Exception:
            logger.error("Customer service update rejected after handler failure")
        raise ApplicationHandlerStop

    def bind(self):
        platform = self.plugin.context.get_platform_inst(self.platform_id)
        if platform is self.platform:
            return
        self.detach()
        self.platform = platform
        if platform:
            platform.register_application_hook("telethon-ai:customer", self.attach)

    def attach(self, application):
        if self.application:
            self.application.bot_data.pop(adapter.NAME + ":ready", None)
            self.application.remove_handler(self.handler, -80)
        self.application = application
        if application:
            application.add_handler(self.handler, -80)
            if (
                self.platform.required_plugin == adapter.NAME
                and self.platform.config.get("telegram_dedicated_reporting") is True
            ):
                application.bot_data[adapter.NAME + ":ready"] = True

    def detach(self):
        if self.platform:
            self.platform.unregister_application_hook("telethon-ai:customer")
        self.attach(None)
        self.platform = None

    async def message(self, update, context):
        # Always consume this service Bot's messages; never fall through to LLM.
        if (
            self.plugin.closed
            or not update.effective_chat
            or update.effective_chat.type != "private"
            or not update.effective_user
            or not update.message
            or not update.message.text
        ):
            raise ApplicationHandlerStop
        try:
            row = self.plugin.tenants.owned(
                self.platform_id, context.bot.id, update.effective_user.id
            )
            manager = self.plugin.context.astrbot_config_mgr
            route = f"{self.platform_id}:FriendMessage:{update.effective_user.id}"
            conf_id = manager.ucr.get_conf_id_for_umop(route)
            if (
                self.platform is None
                or self.platform.required_plugin != adapter.NAME
                or self.platform.config.get("telegram_dedicated_reporting") is not True
                or self.application is not self.platform.application
                or not self.application.bot_data.get(adapter.NAME + ":ready")
                or conf_id != manager.ucr.umop_to_conf_id.get(f"{self.platform_id}::")
                or conf_id not in manager.confs
                or not isolated_profile(manager.confs[conf_id])
                or self.plugin.context.get_config(route) is not manager.confs[conf_id]
            ):
                await update.message.reply_text("服务配置异常，请联系平台管理员。")
                raise ApplicationHandlerStop
            command = update.message.text.strip()
            if not command:
                raise ApplicationHandlerStop
            if command.split()[0].startswith("/"):
                head, _, target = command.split()[0].partition("@")
                if target and target.lower() != context.bot.username.lower():
                    raise ApplicationHandlerStop
                command = head
            if command in {"/pause", "暂停服务"}:
                self.plugin.tenants.set_enabled(
                    str(update.effective_user.id), row["id"], False
                )
                text = "服务已暂停。恢复服务请联系平台管理员。"
            elif command in {"/resume", "申请恢复", "/renew", "申请续期"}:
                kind = "resume" if command in {"/resume", "申请恢复"} else "renew"
                ticket = self.plugin.tenants.request(
                    self.platform_id, context.bot.id, update.effective_user.id, kind
                )
                text = f"申请已提交：{ticket}。审核后生效，不会自动扣款。"
            elif update.message.text.startswith("/bind "):
                value = update.message.text.split(maxsplit=1)[1].strip()
                ticket = self.plugin.tenants.request(
                    self.platform_id,
                    context.bot.id,
                    update.effective_user.id,
                    "group",
                    value,
                )
                text = f"群授权申请已提交：{ticket}。平台将核验群管理权与授权范围，不会自动入群。"
            elif command in {"/start", "/service", "我的服务", "查看额度"}:
                info = self.plugin.tenants.summary(row["id"])
                text = (
                    f"AI 账号服务\n状态：{'已启用' if info['enabled'] else '已暂停'}\n"
                    f"请求额度：{info['used']}/{info['budget']}\n"
                    f"账号数：{len(info['accounts'])}\n"
                    f"授权群：{len(info['groups'])}/{info['group_limit']}\n"
                    "试用期间暂不开放付款。"
                )
            else:
                text = "发送 /bind -100群ID 申请群授权。\n" + self.plugin.config.get(
                    "support_text", "请联系平台管理员"
                )
            await update.message.reply_text(
                text,
                reply_markup=ReplyKeyboardMarkup(
                    [
                        [KeyboardButton("我的服务"), KeyboardButton("查看额度")],
                        [KeyboardButton("暂停服务"), KeyboardButton("申请恢复")],
                        [KeyboardButton("申请续期"), KeyboardButton("联系支持")],
                        [KeyboardButton("群管理")],
                    ],
                    resize_keyboard=True,
                ),
            )
        except Denied:
            await update.message.reply_text("此服务仅限绑定的所有者操作。")
        raise ApplicationHandlerStop
