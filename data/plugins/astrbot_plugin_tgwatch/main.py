"""Telegram activity administration without an additional bot poller."""

import asyncio
import contextlib
import re
import shlex
from contextvars import ContextVar
from datetime import datetime, time, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

import aiohttp
from telegram import BotCommand, Update
from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup as Keyboard
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.helpers import escape_markdown

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star
from astrbot.core.utils.bot_ingress_limit import BotIngressLimit

TZ = ZoneInfo("Asia/Shanghai")
HANDLER_GROUP = -91
BUTTON_OUTPUT = ContextVar("tgwatch_button_output", default=None)
HELP = """Telegram 工作统计（管理员私聊）
/tgwatch status — 采集及发送状态
/tgwatch groups — 已配置群，按钮启停
/tgwatch dialogs — 选择已配置的公开群或已加入的群
/tgwatch people — 已配置人员，按钮启停
/tgwatch dialogs — 选择群后直接输入 @用户名 开始监控
/tgwatch pause — 🔴 暂停全部
/tgwatch resume — 🟢 恢复全部
/cancel — 取消输入
/tgwatch person name ID 新名字 — 修改显示名
/tgwatch person enable ID — 🟢 启用人员
/tgwatch person disable ID — 🔴 停用人员
/tgwatch records YYYY-MM-DD [人员ID] [群ID] — 查记录
/tgwatch — 菜单
账号登录、验证码和两步验证只能在采集服务本机操作。
发送结果不确定时不会自动重发，请先查看工作群核对。
"""


class TelegramWatch(Star):
    """Bridge the collector API to an existing AstrBot Telegram application."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.session = None
        self.task = None
        self.application = None
        self.handlers = []
        self.state = {}
        self.admins = set()
        self.ingress_limit = BotIngressLimit()
        self.owner = None
        self.panels = {}
        self.last_hour = None
        self.pending = {}
        self.monitor_starts = {}
        self.guard = TypeHandler(Update, self.stop_other_plugins)
        self.menu_application = None
        self.menu_retry_at = 0.0
        self.last_loop_error = None

    async def initialize(self):
        """Load durable delivery state and start a cancellable scheduler task."""
        if not self.config.get("enabled", False):
            return
        self.admins = {int(value) for value in self.config.get("admin_ids", [])}
        if not self.admins or any(value <= 0 for value in self.admins):
            raise ValueError("Configure positive Telegram admin IDs")
        self.owner = int(self.config["admin_ids"][0])
        self.work_chat = int(self.config.get("work_chat_id") or 0)
        if self.work_chat > 0 or not self.config.get("platform_id"):
            raise ValueError("Configure the Telegram platform and work group IDs")
        self.api_url = self.config["api_url"].rstrip("/")
        if not self.api_url.startswith(
            ("http://127.0.0.1:", "http://localhost:", "https://")
        ):
            raise ValueError("Use a loopback or HTTPS collector URL")
        if len(self.config.get("api_token", "")) < 32:
            raise ValueError("Configure the collector API token")
        self.state = await self.get_kv_data("delivery_state", {}) or {}
        self.state.setdefault("reports", {})
        if "work_group_enabled" not in self.state:
            self.state["work_group_enabled"] = (
                {str(self.work_chat): bool(self.config.get("reports_enabled", True))}
                if self.work_chat
                else {}
            )
        self.state.setdefault("work_group_hours", {})
        if not self.state.get("work_delivery_migrated"):
            if self.work_chat:
                for key, value in list(self.state["reports"].items()):
                    if key.startswith(("hour:", "day:")):
                        self.state["reports"][f"{self.work_chat}:{key}"] = value
                        del self.state["reports"][key]
                if self.state.get("last_hour"):
                    self.state["work_group_hours"][str(self.work_chat)] = self.state[
                        "last_hour"
                    ]
            if self.work_chat:
                self.state.setdefault("work_groups", {}).setdefault(
                    str(self.work_chat), str(self.work_chat)
                )
            self.state["work_delivery_migrated"] = True
        if not self.state.get("admins_config_synced"):
            self.admins |= {
                int(value) for value in self.state.get("managed_admins", [])
            }
            await self.save_admins(self.admins)
        self.state["managed_admins"] = sorted(self.admins - {self.owner})
        for report in self.state["reports"].values():
            if report["status"] == "sending":
                report["status"] = "uncertain"
        await self.put_kv_data("delivery_state", self.state)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=65))
        self.last_hour = (
            datetime.fromisoformat(self.state["last_hour"])
            if self.state.get("last_hour")
            else datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
        )
        self.state["last_hour"] = self.last_hour.isoformat()
        await self.put_kv_data("delivery_state", self.state)
        self.task = asyncio.create_task(self.run(), name="tgwatch-scheduler")

    async def save_admins(self, admins):
        """Persist administrator changes to the visible plugin configuration.

        Args:
            admins: Effective administrator IDs including the primary administrator.
        """
        ids = [str(self.owner)] + [
            str(value) for value in sorted(admins - {self.owner})
        ]
        if hasattr(self.config, "save_config_async"):
            await self.config.save_config_async({"admin_ids": ids})
        else:
            self.config["admin_ids"] = ids
        self.state["managed_admins"] = sorted(admins - {self.owner})
        self.state["admins_config_synced"] = True
        await self.put_kv_data("delivery_state", self.state)
        self.admins = admins

    async def api(self, resource, params=None, data=None):
        """Call the collector without leaking secrets or raw upstream errors.

        Args:
            resource: Versioned resource name without a leading slash.
            params: Optional query parameters.
            data: Optional JSON payload for a PUT operation.

        Returns:
            Parsed collector response.

        Raises:
            RuntimeError: If the collector rejects or cannot answer the request.
        """
        try:
            async with self.session.request(
                "PUT" if data is not None else "GET",
                f"{self.api_url}/v1/{resource}",
                params=params,
                json=data,
                headers={"Authorization": f"Bearer {self.config['api_token']}"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    payload = await response.json()
                    messages = {
                        "invalid_group_link": "🔴 群链接格式无效，请发送 t.me/群用户名 或 t.me/+邀请链接。",
                        "group_required": "🔴 请发送群聊链接，不能使用个人或广播频道链接。",
                        "invite_invalid": "🔴 邀请链接无效或已过期。",
                        "public_limit": "🟡 免入群采集最多同时开启 10 个群，请加入该群后重试。",
                        "join_denied": "🔴 无法入群，请检查群权限、账号限制或已加入群数量。",
                        "join_unconfirmed": "🟡 入群结果暂无法确认，请先在已加入群列表核对，不会自动重试。",
                        "invalid_regex": "🔴 正则语法错误，请检查后重新输入。",
                        "regex_timeout": "🔴 正则检查超时，请简化链接规则；本次不显示不完整结果。",
                        "members_unavailable": "采集账号无法查看此群成员，请改用 @用户名、t.me 链接或数字 ID 添加。",
                        "unauthorized": "采集 API 密钥不匹配，请检查插件配置。",
                        "account_unavailable": "采集账号不可用，请查看状态；登录失效需在本机重新登录。",
                        "telegram_rate_limited": "Telegram 限流中，请按状态页提示稍后重试。",
                        "invalid_history_window": "📅 开始时间必须在最近 90 天内，且不能晚于当前时间。",
                        "invalid_history_timezone": "📅 开始时间缺少时区，请重新选择北京时间。",
                        "watch_target_missing": "👥 群或人员配置已不存在，请刷新菜单后重新选择。",
                        "target_capacity": "⚠️ 已达到启用数量上限：最多 30 个群、50 名人员。",
                        "group_not_joined": "👥 采集账号未加入该群。公开群请通过“添加监控群”重新提交公开链接；私密群需先获准加入。",
                        "public_access_required": "🔴 此群当前无法按公开群访问，请检查链接和群权限。",
                        "group_selection_expired": "👥 群列表已变化，请返回菜单刷新后重新选择。",
                        "group_inaccessible": "🔴 采集账号当前无法访问该群。",
                        "user_required": "👤 这个用户名不是个人账号，请输入人员用户名或数字 ID。",
                        "pause_before_remove": "🔴 请先暂停该群监控，再删除。",
                        "invalid_request": f"⚠️ 本次操作参数无效（接口：{resource}）。请刷新菜单后重试，仍失败时提供操作步骤。",
                    }
                    raise RuntimeError(
                        messages.get(
                            payload.get("error"), "采集服务暂不可用，请查看状态后重试。"
                        )
                    )
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise RuntimeError("无法连接采集服务，请检查服务是否运行。") from None

    async def send_text(self, bot, chat_id, text, keyboard=None, *, allow_group=False):
        """Send escaped MarkdownV2 with a bold heading and bounded chunks.

        Args:
            bot: Existing platform bot.
            chat_id: Destination chat ID.
            text: Literal display content, escaped before formatting.
            keyboard: Optional keyboard attached to the final chunk.
            allow_group: Legacy argument; sensitive replies remain private.
        """
        if int(chat_id) <= 0:
            return
        output = BUTTON_OUTPUT.get()
        if output is not None:
            output.append((text, keyboard))
            return
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)] or ["暂无记录"]
        for index, chunk in enumerate(chunks):
            await bot.send_message(
                chat_id=chat_id,
                text=self.markdown(chunk),
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
                reply_markup=keyboard if index == len(chunks) - 1 else None,
            )

    @staticmethod
    def markdown(text):
        """Format literal content safely for Telegram messages and saved reports.

        Args:
            text: Untrusted literal content, including names and message bodies.

        Returns:
            MarkdownV2 with only the first line emphasized.
        """
        heading, separator, body = text.partition("\n")
        return ("*" + escape_markdown(heading, version=2) + "*" if heading else "") + (
            separator + escape_markdown(body, version=2) if separator else ""
        )

    async def stop_other_plugins(self, update, context):
        """Stop unrelated plugins on this dedicated reporting bot.

        Args:
            update: Incoming Telegram update.
            context: Shared callback context.
        """
        membership = getattr(update, "my_chat_member", None)
        if membership and membership.chat.type in ("group", "supergroup"):
            groups = self.state.setdefault("work_groups", {})
            if membership.new_chat_member.status in ("member", "administrator"):
                groups[str(membership.chat.id)] = membership.chat.title or str(
                    membership.chat.id
                )
            else:
                groups.pop(str(membership.chat.id), None)
                self.state.setdefault("work_group_enabled", {}).pop(
                    str(membership.chat.id), None
                )
            await self.put_kv_data("delivery_state", self.state)
        elif not membership:
            chat = getattr(update, "effective_chat", None)
            if chat and chat.type in ("group", "supergroup"):
                groups = self.state.setdefault("work_groups", {})
                title = chat.title or str(chat.id)
                if groups.get(str(chat.id)) != title:
                    groups[str(chat.id)] = title
                    await self.put_kv_data("delivery_state", self.state)
        raise ApplicationHandlerStop

    async def command(self, update, context):
        """Handle only this plugin's private commands and deep links.

        Args:
            update: Telegram update from the shared application.
            context: Existing Telegram callback context.
        """
        if (
            update.effective_chat.type in ("group", "supergroup")
            and update.effective_user.id in self.admins
        ):
            self.state.setdefault("work_groups", {})[str(update.effective_chat.id)] = (
                getattr(update.effective_chat, "title", None)
                or str(update.effective_chat.id)
            )
            await self.put_kv_data("delivery_state", self.state)
            raise ApplicationHandlerStop
        if not self.ingress_limit.allow(update.effective_user.id):
            raise ApplicationHandlerStop
        command = update.effective_message.text.split()[0].split("@")[0]
        is_start = command == "/start"
        if is_start and context.args not in ([], ["tgwatch"]):
            return
        try:
            if (
                update.effective_chat.type != "private"
                or update.effective_user.id not in self.admins
            ):
                await self.send_text(
                    context.bot,
                    update.effective_chat.id,
                    "仅允许已配置的管理员在私聊中使用。",
                )
            else:
                args = (
                    []
                    if is_start
                    else ["cancel"]
                    if command == "/cancel"
                    else shlex.split(update.effective_message.text)[1:]
                )
                self.pending.pop(update.effective_chat.id, None)
                await self.dispatch(context.bot, update.effective_chat.id, args)
        except RuntimeError as exc:
            await self.send_text(context.bot, update.effective_chat.id, str(exc))
        except (ValueError, KeyError, IndexError):
            await self.send_text(
                context.bot,
                update.effective_chat.id,
                "操作失败：请检查命令参数和采集服务状态。用户名无法解析时请使用数字 ID。",
            )
        raise ApplicationHandlerStop

    async def callback(self, update, context):
        """Render authorized button responses by editing the existing private panel.

        Args:
            update: Telegram callback update.
            context: Existing bot context.
        """
        query = update.callback_query
        if not self.ingress_limit.allow(getattr(update.effective_user, "id", None)):
            raise ApplicationHandlerStop
        if not update.effective_user or update.effective_user.id not in self.admins:
            await query.answer("无权访问", show_alert=True)
            raise ApplicationHandlerStop
        if not update.effective_chat or update.effective_chat.type != "private":
            await query.answer(
                "请在机器人私聊菜单中查看详情；群内仅展示排行榜。", show_alert=True
            )
            raise ApplicationHandlerStop
        key = (update.effective_chat.id, query.message.message_id)
        output = []
        token = BUTTON_OUTPUT.set(output)
        try:
            if query.data.startswith("tw:panel:"):
                await query.answer()
                page = int(query.data.split(":")[2])
                pages = self.panels.get(key, [])
                if not 0 <= page < len(pages):
                    await query.answer("页面已过期，请重新打开菜单。", show_alert=True)
                    raise ApplicationHandlerStop
            else:
                try:
                    await self.handle_callback(update, context)
                except ApplicationHandlerStop:
                    pass
                pages = []
                for text, keyboard in output:
                    chunks = [
                        text[i : i + 1800] for i in range(0, len(text), 1800)
                    ] or ["暂无内容"]
                    for chunk in chunks:
                        pages.append((chunk, keyboard))
                if not pages:
                    raise ApplicationHandlerStop
                if len(self.panels) >= 100:
                    self.panels.pop(next(iter(self.panels)))
                self.panels[key] = pages
                page = 0
            text, keyboard = pages[page]
            rows = list(keyboard.inline_keyboard) if keyboard else []
            navigation = []
            if page:
                navigation.append(
                    Button("◀ 上一页", callback_data=f"tw:panel:{page - 1}")
                )
            if page + 1 < len(pages):
                navigation.append(
                    Button("下一页 ▶", callback_data=f"tw:panel:{page + 1}")
                )
            if navigation:
                rows.append(navigation)
            await query.edit_message_text(
                text=self.markdown(text),
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
                reply_markup=Keyboard(rows),
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                self.logger.warning("Telegram Watch panel edit rejected")
                with contextlib.suppress(BadRequest, NetworkError):
                    await query.answer(
                        "页面无法更新，请用 /tgwatch 重新打开。", show_alert=True
                    )
        except NetworkError as exc:
            self.logger.warning(
                "Telegram Watch panel update failed (%s); no automatic retry",
                type(exc).__name__,
            )
            with contextlib.suppress(TelegramError):
                await query.answer(
                    "页面更新暂未确认，请稍后重新打开菜单。", show_alert=True
                )
        finally:
            BUTTON_OUTPUT.reset(token)
        raise ApplicationHandlerStop

    async def handle_callback(self, update, context):
        """Authorize every button click, including previously issued keyboards.

        Args:
            update: Callback update selected by the plugin prefix.
            context: Shared application context.
        """
        query = update.callback_query
        parts = query.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if (
            update.effective_chat is None
            or update.effective_user is None
            or update.effective_user.id not in self.admins
            or (
                action
                in (
                    "admins",
                    "adminadd",
                    "adminremove",
                    "bindwork",
                    "work",
                    "worktoggle",
                    "workselect",
                )
                and update.effective_user.id != self.owner
            )
            or (
                update.effective_chat.type != "private"
                and (
                    update.effective_chat.id != self.work_chat
                    or action
                    not in ("records", "persons", "chats", "status", "violations")
                )
            )
        ):
            await query.answer("无权访问", show_alert=True)
            raise ApplicationHandlerStop
        try:
            await query.answer()
        except (BadRequest, NetworkError):
            self.logger.warning(
                "Telegram Watch callback acknowledgement failed; continuing authorized action"
            )
        chat_id = (
            update.effective_user.id
            if update.effective_chat.type != "private"
            else update.effective_chat.id
        )
        try:
            parts = query.data.split(":")
            action = parts[1]
            if action in ("work", "worktoggle", "workselect"):
                self.pending.pop(chat_id, None)
                if action in ("workselect", "worktoggle"):
                    target = int(parts[2]) if len(parts) > 2 else self.work_chat
                    switches = self.state.setdefault("work_group_enabled", {})
                    enabled = not switches.get(str(target), False)
                    if enabled:
                        group = await context.bot.get_chat(target)
                        member = await context.bot.get_chat_member(
                            target, context.bot.id
                        )
                        if group.type not in ("group", "supergroup") or not (
                            member.status in ("administrator", "creator")
                            or member.status == "member"
                            and group.permissions
                            and group.permissions.can_send_messages
                        ):
                            raise ValueError("Group send permission required")
                        self.state.setdefault("work_groups", {})[str(target)] = (
                            group.title
                        )
                    switches[str(target)] = enabled
                    self.state.setdefault("work_group_hours", {})[str(target)] = (
                        datetime.now(TZ)
                        .replace(minute=0, second=0, microsecond=0)
                        .isoformat()
                    )
                    await self.put_kv_data("delivery_state", self.state)
                    if enabled:
                        try:
                            await context.bot.send_message(
                                chat_id=target,
                                text=self.markdown(
                                    "✅ 工作群启用成功\n\n"
                                    "🟢 本群已开启聊天排行榜\n"
                                    "🕒 每个整点：上一完整小时榜\n"
                                    "📅 每日 01:30：昨日总榜\n\n"
                                    "以上时间均为北京时间。"
                                ),
                                parse_mode="MarkdownV2",
                                disable_web_page_preview=True,
                            )
                        except TelegramError:
                            self.logger.warning(
                                "Work group activation confirmation was not confirmed; no automatic retry"
                            )
                            await query.answer(
                                "已开启发榜，但启用提示未确认送达，请在群里核对。",
                                show_alert=True,
                            )
                discovery_note = ""
                if action == "work":
                    try:
                        candidates = dict(self.state.get("work_groups", {}))
                        for group_id, name in candidates.items():
                            try:
                                member = await context.bot.get_chat_member(
                                    int(group_id), context.bot.id
                                )
                            except (BadRequest, Forbidden):
                                # An inaccessible group is not proof that the bot left.
                                continue
                            if member.status in ("member", "administrator", "creator"):
                                group = await context.bot.get_chat(int(group_id))
                                self.state.setdefault("work_groups", {})[group_id] = (
                                    group.title or name
                                )
                            elif member.status in ("left", "kicked"):
                                self.state.setdefault("work_groups", {}).pop(
                                    group_id, None
                                )
                                self.state.setdefault("work_group_enabled", {}).pop(
                                    group_id, None
                                )
                        await self.put_kv_data("delivery_state", self.state)
                    except (RuntimeError, TelegramError):
                        discovery_note = "\n⚠️ 自动核验暂未完成，显示已记录群；可在目标群发送 /tgwatch 后刷新。"
                groups = self.state.get("work_groups", {})
                rows = [
                    [
                        Button(
                            "➕ 拉机器人入群",
                            url=f"https://t.me/{context.bot.username}?startgroup=tgwatch",
                        )
                    ],
                    [
                        Button("🔄 刷新群列表", callback_data="tw:work"),
                        Button("➕ 输入群 ID 添加", callback_data="tw:bindwork"),
                    ],
                ]
                for group_id, name in groups.items():
                    rows.append(
                        [
                            Button(
                                f"{'🟢' if self.state.get('work_group_enabled', {}).get(group_id, False) else '🔴'} {name}"[
                                    :60
                                ],
                                callback_data=f"tw:workselect:{group_id}",
                            )
                        ]
                    )
                await self.send_text(
                    context.bot,
                    chat_id,
                    f"📣 工作群\n\n🟢 允许向该群定时发榜\n🔴 不向该群发榜\n\n点击群名切换，可同时开启多个群。{discovery_note}",
                    Keyboard(rows),
                )
            elif action in ("persondelete", "personremove"):
                person, group = int(parts[2]), int(parts[3])
                if action == "persondelete":
                    people = (await self.api("people"))["items"]
                    name = next(
                        (p["name"] for p in people if p["id"] == person), str(person)
                    )
                    scope = "本群" if group else "全部群"
                    await self.send_text(
                        context.bot,
                        chat_id,
                        f"🗑 删除监控人员\n\n{name}\n范围：{scope}\n将立即停止对应监控，并永久删除该范围的聊天记录及统计次数。无需先暂停。\n已发送的榜单及历史备份不会自动撤回或清除。",
                        Keyboard(
                            [
                                [
                                    Button(
                                        "🗑 确认删除监控和记录",
                                        callback_data=f"tw:personremove:{person}:{group}",
                                    )
                                ],
                                [
                                    Button(
                                        "取消",
                                        callback_data=f"tw:menu:watches:{group}"
                                        if group
                                        else "tw:menu:people",
                                    )
                                ],
                            ]
                        ),
                    )
                else:
                    await self.api(
                        "remove_person", data={"person": person, "group": group}
                    )
                    # Discard unsent snapshots containing erased records.
                    for report in self.state.get("reports", {}).values():
                        if report["status"] == "pending":
                            report.update(status="missed", reason="person_removed")
                            report.pop("parts", None)
                    await self.put_kv_data("delivery_state", self.state)
                    await self.send_text(
                        context.bot,
                        chat_id,
                        "✅ 已删除对应监控及记录。",
                        Keyboard(
                            [
                                [
                                    Button(
                                        "⬅️ 返回人员列表",
                                        callback_data=f"tw:menu:watches:{group}"
                                        if group
                                        else "tw:menu:people",
                                    )
                                ]
                            ]
                        ),
                    )
            elif action in ("groupdelete", "groupremove"):
                group = int(parts[2])
                target = next(
                    g for g in (await self.api("groups"))["items"] if g["id"] == group
                )
                if target["enabled"]:
                    raise ValueError("Pause group before removal")
                if action == "groupdelete":
                    await self.send_text(
                        context.bot,
                        chat_id,
                        f"🗑 删除监控群：{target['name']}\n\n群已暂停。删除后保留历史记录，可选择让采集账号同时退出群。",
                        Keyboard(
                            [
                                [
                                    Button(
                                        "仅删除监控",
                                        callback_data=f"tw:groupremove:{group}:0",
                                    )
                                ],
                                [
                                    Button(
                                        "删除并退出群",
                                        callback_data=f"tw:groupremove:{group}:1",
                                    )
                                ],
                                [Button("取消", callback_data="tw:menu:groups")],
                            ]
                        ),
                    )
                else:
                    await self.api(
                        "remove_group", data={"group": group, "leave": parts[3] == "1"}
                    )
                    await self.dispatch(context.bot, chat_id, ["groups"])
            elif action == "setup":
                lines = ["🧭 首次配置检查", ""]
                platform = self.context.get_platform_inst(self.config["platform_id"])
                lines.append(
                    "🟢 专用隔离已开启"
                    if platform and platform.config.get("telegram_dedicated_reporting")
                    else "🔴 请在 Telegram 接入设置开启专用汇报隔离"
                )
                lines.append(
                    f"🟢 主管理员：{self.owner} · 管理员共 {len(self.admins)} 人"
                )
                if hasattr(self.context, "get_config"):
                    profile = self.context.get_config(
                        f"{self.config['platform_id']}:FriendMessage:{chat_id}"
                    )
                    lines.append(
                        "🟢 专属会话 AI 已关闭"
                        if not profile.get("provider_settings", {}).get("enable", True)
                        else "🔴 请在专属会话配置关闭 AI"
                    )
                    lines.append(
                        "🟢 通用插件列表已收紧"
                        if profile.get("plugin_set") == ["astrbot_plugin_tgwatch"]
                        else "🔴 请检查专属会话插件列表及平台路由"
                    )

                try:
                    health = await self.api("status")
                    lines.append(
                        "🟢 采集账号在线"
                        if health["state"] == "connected"
                        else "🔴 采集账号未在线，请检查本机登录或连接"
                    )
                    lines.append(f"👥 有效监控项：{health.get('active_watches', 0)}")
                    rules = await self.api("link_rules")
                    lines.append(f"🔗 已配置链接规则：{len(rules['items'])} 人（可选）")
                except RuntimeError as exc:
                    lines.append(str(exc))
                switches = self.state.get("work_group_enabled", {})
                lines.append("\n📣 工作群（绿色定时发榜）")
                for group_id, title in self.state.get("work_groups", {}).items():
                    lines.append(
                        f"{'🟢' if switches.get(group_id, False) else '🔴'} {title}"
                    )
                if not any(switches.values()):
                    lines.append("暂无允许发榜的群，请在工作群中点击群名变为绿色。")
                rows = [
                    [
                        Button("🔄 重新检查", callback_data="tw:setup"),
                        Button("👥 配置监控", callback_data="tw:menu:dialogs"),
                    ]
                ]
                if chat_id == self.owner:
                    rows.append([Button("📣 工作群", callback_data="tw:work")])
                await self.send_text(
                    context.bot, chat_id, "\n".join(lines), Keyboard(rows)
                )
            elif action == "bindwork":
                self.pending[chat_id] = {
                    "mode": "workgroup",
                    "expires": monotonic() + 600,
                }
                await self.send_text(
                    context.bot,
                    chat_id,
                    "🔧 绑定工作群\n\n先把机器人加入目标工作群，再发送群数字 ID（负数）或公开群 @用户名。绑定前会检查机器人发言权限。/cancel 取消。",
                )
            elif action in ("admins", "adminadd", "adminremove"):
                if action == "adminadd":
                    self.logger.info(
                        "Telegram Watch primary administrator opened enrollment"
                    )
                    self.pending[chat_id] = {
                        "mode": "admin",
                        "expires": monotonic() + 600,
                    }
                    await self.send_text(
                        context.bot,
                        chat_id,
                        "👑 添加管理员\n\n请输入对方数字 ID、@用户名或 t.me/用户名。对方需先私聊机器人点击 Start。\n/cancel 取消。",
                        Keyboard(
                            [[Button("↩️ 返回管理员设置", callback_data="tw:admins")]]
                        ),
                    )
                else:
                    if action == "adminremove":
                        target = int(parts[2])
                        if target == self.owner:
                            raise ValueError("Cannot remove primary administrator")
                        updated = self.admins - {target}
                        await self.save_admins(updated)
                        self.pending.pop(target, None)
                    lines = ["👑 管理员设置", "", f"主管理员：{self.owner}"]
                    rows = [[Button("🟢 添加管理员", callback_data="tw:adminadd")]]
                    for user in sorted(self.admins - {self.owner}):
                        lines.append(f"🟢 管理员：{user}")
                        rows.append(
                            [
                                Button(
                                    f"🔴 移除 {user}",
                                    callback_data=f"tw:adminremove:{user}",
                                )
                            ]
                        )
                    rows.append([Button("🏠 返回菜单", callback_data="tw:menu")])
                    await self.send_text(
                        context.bot, chat_id, "\n".join(lines), Keyboard(rows)
                    )
            elif action == "confirmjoin":
                pending = self.pending.get(chat_id, {})
                if (
                    pending.get("mode") != "confirm_join"
                    or pending.get("expires", 0) < monotonic()
                ):
                    await self.send_text(
                        context.bot, chat_id, "⌛ 确认已过期，请重新添加监控群。"
                    )
                else:
                    self.pending.pop(chat_id, None)
                    result = await self.api("join", data={"link": pending["link"]})
                    if result.get("id"):
                        text = f"{result['name']}\n\n🟢 已加入群聊，请选择监控人员。"
                        rows = [
                            [
                                Button(
                                    "📅 选择监控起始时间",
                                    callback_data=f"tw:history:{result['id']}:0",
                                )
                            ]
                        ]
                    else:
                        text = (
                            "🟡 入群申请已提交，批准后刷新列表选择监控人员。"
                            if result["status"] == "pending"
                            else "请刷新群列表确认入群状态。"
                        )
                        rows = [
                            [Button("🔄 刷新群列表", callback_data="tw:menu:dialogs")]
                        ]
                    rows.append([Button("🏠 返回菜单", callback_data="tw:menu")])
                    await self.send_text(context.bot, chat_id, text, Keyboard(rows))
            elif action == "join":
                self.pending[chat_id] = {"mode": "join", "expires": monotonic() + 600}
                await self.send_text(
                    context.bot,
                    chat_id,
                    "➕ 添加监控群\n\n请发送群链接：t.me/群用户名、t.me/+邀请码 或 t.me/joinchat/邀请码。\n公开群优先免入群采集；需要加入时会请你确认。之后选择监控人员，10 分钟内有效。",
                    Keyboard([[Button("✖️ 取消", callback_data="tw:menu")]]),
                )
            elif action == "rules":
                people = (await self.api("people"))["items"]
                page = max(0, int(parts[2]) if len(parts) > 2 else 0)
                rows = [
                    [Button(p["name"][:50], callback_data=f"tw:rule:{p['id']}")]
                    for p in people[page * 10 : (page + 1) * 10]
                ]
                nav = []
                if page:
                    nav.append(Button("◀ 上一页", callback_data=f"tw:rules:{page - 1}"))
                if (page + 1) * 10 < len(people):
                    nav.append(Button("下一页 ▶", callback_data=f"tw:rules:{page + 1}"))
                if nav:
                    rows.append(nav)
                await self.send_text(
                    context.bot,
                    chat_id,
                    "🔗 违规链接规则\n\n选择人员配置允许链接的正则白名单。未配置或关闭规则时不检查。",
                    Keyboard(rows),
                )
            elif action in ("rule", "ruleedit", "ruletoggle"):
                person = int(parts[2])
                rule = next(
                    (
                        r
                        for r in (await self.api("link_rules"))["items"]
                        if r["user_id"] == person
                    ),
                    None,
                )
                if action == "ruleedit":
                    self.pending[chat_id] = {
                        "mode": "rule",
                        "person": person,
                        "expires": monotonic() + 600,
                    }
                    await self.send_text(
                        context.bot,
                        chat_id,
                        "✏️ 设置允许链接\n\n每行一条正则，最多 10 条，每条最多 500 字符。匹配完整链接，任一规则匹配即允许。保存后启用并用于保留期内历史记录。\n示例：\nhttps://t\\.me/dh114514/?\nhttps://example\\.com/.*\n\n/cancel 取消。",
                    )
                else:
                    if action == "ruletoggle":
                        if rule is None:
                            raise ValueError("Configure rules first")
                        rule["enabled"] = not rule["enabled"]
                        await self.api(
                            "link_rules",
                            data={
                                "person": person,
                                "patterns": rule["patterns"],
                                "enabled": bool(rule["enabled"]),
                            },
                        )
                    rows = [
                        [Button("✏️ 设置正则", callback_data=f"tw:ruleedit:{person}")]
                    ]
                    if rule:
                        rows.append(
                            [
                                Button(
                                    "🔴 关闭检查" if rule["enabled"] else "🟢 启用检查",
                                    callback_data=f"tw:ruletoggle:{person}",
                                )
                            ]
                        )
                    rows.append(
                        [
                            Button(
                                "🚫 违规细节", callback_data=f"tw:violations:{person}:0"
                            ),
                            Button("↩️ 选择人员", callback_data="tw:rules:0"),
                        ]
                    )
                    await self.send_text(
                        context.bot,
                        chat_id,
                        "🔗 人员链接规则\n\n"
                        + (
                            f"{rule['name']}｜{'🟢 已启用' if rule['enabled'] else '🔴 已关闭'}\n"
                            + "\n".join(rule["patterns"])
                            if rule
                            else "尚未配置规则，不检查链接。"
                        ),
                        Keyboard(rows),
                    )
            elif action == "violations":
                person, cursor = int(parts[2]), int(parts[3])
                result = await self.api(
                    "violations",
                    {"person": person, **({"before": cursor} if cursor else {})},
                )
                lines = [
                    "🚫 违规链接细节",
                    "",
                    "按当前启用规则检查保留期内记录；每页最多 10 条。",
                ]
                if result["health"].get("incomplete"):
                    lines.append("※ 采集记录可能不完整")
                for item in result["items"]:
                    stamp = datetime.fromisoformat(item["sent"]).astimezone(TZ)
                    lines.append(
                        f"\n{item['name']} · {stamp:%m-%d %H:%M}\n💬 {item['group']} · 消息 {item['message_id']}\n未匹配允许规则：\n"
                        + "\n".join(item["links"])
                    )
                if not result["items"]:
                    lines.append(
                        "\n本批记录未发现违规链接。"
                        if result["rule_count"]
                        else "\n尚无启用的链接规则。"
                    )
                rows = []
                if result["next"]:
                    rows.append(
                        [
                            Button(
                                "下一批 ▶",
                                callback_data=f"tw:violations:{person}:{result['next']}",
                            )
                        ]
                    )
                if cursor:
                    rows.append(
                        [
                            Button(
                                "⏮ 返回首页", callback_data=f"tw:violations:{person}:0"
                            )
                        ]
                    )
                await self.send_text(
                    context.bot,
                    chat_id,
                    "\n".join(lines),
                    Keyboard(rows),
                    allow_group=True,
                )
            elif action == "history":
                self.monitor_starts.pop((chat_id, int(parts[2])), None)
                await self.choose_monitor_start(
                    context.bot, chat_id, int(parts[2]), int(parts[3])
                )
            elif action == "startwatch":
                group, person, choice = int(parts[2]), int(parts[3]), parts[4]
                if choice == "custom":
                    self.pending[chat_id] = {
                        "mode": "history_time",
                        "group": group,
                        "person": person,
                        "expires": monotonic() + 600,
                    }
                    await self.send_text(
                        context.bot,
                        chat_id,
                        "📅 请发送北京时间，例如 9月21日0:13 或 9月21日0:13:25。\n省略年份按今年，省略秒按 00 秒；跨年请写完整年份，如 2026-09-20 09:00:00。最多回溯 90 天，10 分钟内有效。/cancel 取消。",
                    )
                else:
                    now = datetime.now(TZ)
                    data = {"group": group, "person": person, "enabled": True}
                    if choice in ("today", "day"):
                        start = (
                            now.replace(hour=0, minute=0, second=0, microsecond=0)
                            if choice == "today"
                            else now - timedelta(days=1)
                        )
                        data["start"] = start.astimezone(timezone.utc).isoformat()
                    elif choice != "now":
                        raise ValueError("Invalid start choice")
                    await self.apply_monitor_start(
                        context.bot, chat_id, group, person, data.get("start")
                    )
            elif action == "status":
                await self.monitor_status(context.bot, chat_id, int(parts[2]))
            elif action == "menu":
                self.pending.pop(chat_id, None)
                await self.dispatch(context.bot, chat_id, parts[2:])
            elif action in ("addg", "input"):
                group = int(parts[2])
                if action == "addg":
                    await self.api("groups", data={"id": group, "enabled": True})
                groups = (await self.api("groups"))["items"]
                selected = next(g for g in groups if g["id"] == group)
                self.pending[chat_id] = {"group": group, "expires": monotonic() + 600}
                await self.send_text(
                    context.bot,
                    chat_id,
                    f"已选择群：{selected['name']}\n请输入 @用户名、t.me/用户名 或数字 ID，可附加显示名。\n例如：t.me/dh114514 大蛤\n只监控此人在本群的消息；10 分钟内有效。/cancel 取消。",
                    Keyboard(
                        [
                            [
                                Button(
                                    "👥 选择群员", callback_data=f"tw:members:{group}:0"
                                )
                            ],
                            [Button("✖️ 取消", callback_data="tw:menu")],
                        ]
                    ),
                )
            elif action in ("members", "member"):
                group, page = int(parts[2]), int(parts[3])
                result = await self.api("members", {"group": group, "page": page})
                if action == "member":
                    person = next(
                        (
                            p
                            for p in result["items"]
                            if p["id"] == int(parts[4]) and p["selectable"]
                        ),
                        None,
                    )
                    if person is None:
                        raise ValueError("Member list changed; select again")
                    existing = next(
                        (
                            p
                            for p in (await self.api("people"))["items"]
                            if p["id"] == person["id"]
                        ),
                        None,
                    )
                    await self.api(
                        "people",
                        data={
                            "id": person["id"],
                            "name": existing["name"] if existing else person["name"],
                            "enabled": bool(existing["enabled"]) if existing else True,
                        },
                    )
                    self.pending.pop(chat_id, None)
                    await self.choose_monitor_start(
                        context.bot, chat_id, group, person["id"]
                    )
                else:
                    self.pending.pop(chat_id, None)
                    rows = []
                    for person in result["items"]:
                        if person["selectable"]:
                            label = person["name"] + (
                                f" · @{person['username']}"
                                if person.get("username")
                                else f" · {person['id']}"
                            )
                            rows.append(
                                [
                                    Button(
                                        label[:60],
                                        callback_data=f"tw:member:{group}:{page}:{person['id']}",
                                    )
                                ]
                            )
                    navigation = []
                    if page > 0:
                        navigation.append(
                            Button(
                                "◀ 上一页",
                                callback_data=f"tw:members:{group}:{page - 1}",
                            )
                        )
                    if result["has_next"]:
                        navigation.append(
                            Button(
                                "下一页 ▶",
                                callback_data=f"tw:members:{group}:{page + 1}",
                            )
                        )
                    if navigation:
                        rows.append(navigation)
                    rows.append(
                        [
                            Button(
                                "⌨️ 输入用户名或链接", callback_data=f"tw:input:{group}"
                            ),
                            Button(
                                "↩️ 返回监控人员",
                                callback_data=f"tw:menu:watches:{group}",
                            ),
                        ]
                    )
                    await self.send_text(
                        context.bot,
                        chat_id,
                        f"👥 选择群员 · 第 {page + 1} 页\n点击姓名添加此人在本群的监控。\n仅展示账号可获取的成员，机器人、已删除账号和采集账号不可选择。"
                        + (
                            "\n📭 本页暂无可选成员，可翻页或输入用户名。"
                            if not any(p["selectable"] for p in result["items"])
                            else ""
                        ),
                        Keyboard(rows),
                    )
            elif action == "pause":
                self.pending.pop(chat_id, None)
                await self.dispatch(
                    context.bot, chat_id, ["pause" if parts[2] == "1" else "resume"]
                )
            elif action == "watch":
                group, person, enabled = int(parts[2]), int(parts[3]), parts[4] == "1"
                await self.api(
                    "watches",
                    data={"group": group, "person": person, "enabled": enabled},
                )
                await self.dispatch(context.bot, chat_id, ["watches", str(group)])
            elif action == "toggle":
                kind, target_id, enabled = parts[2], int(parts[3]), parts[4] == "1"
                if kind not in ("groups", "people"):
                    raise ValueError("Invalid target kind")
                items = (await self.api(kind))["items"]
                target = next(t for t in items if t["id"] == target_id)
                await self.api(
                    kind,
                    data={"id": target_id, "name": target["name"], "enabled": enabled},
                )
                await self.dispatch(context.bot, chat_id, [kind])
            elif action == "records":
                await self.records(
                    context.bot,
                    chat_id,
                    parts[2],
                    int(parts[3]),
                    int(parts[4]),
                    int(parts[5]),
                )
            elif action in ("persons", "chats"):
                kind = "people" if action == "persons" else "groups"
                items = (await self.api(kind))["items"]
                day, person, group = parts[2], int(parts[3]), int(parts[4])
                items.insert(0, {"id": 0, "name": "全部"})
                for offset in range(0, len(items), 20):
                    buttons = []
                    for item in items[offset : offset + 20]:
                        pid = item["id"] if kind == "people" else person
                        gid = item["id"] if kind == "groups" else group
                        buttons.append(
                            [
                                Button(
                                    item["name"][:40],
                                    callback_data=f"tw:records:{day}:{pid}:{gid}:0",
                                )
                            ]
                        )
                    await self.send_text(
                        context.bot,
                        chat_id,
                        "选择筛选条件",
                        Keyboard(buttons),
                        allow_group=True,
                    )
        except Forbidden:
            await query.answer(
                "请先私聊机器人点击 Start，再重新点击查询。", show_alert=True
            )
        except RuntimeError as exc:
            await self.send_text(context.bot, chat_id, str(exc))
        except (ValueError, KeyError, IndexError, StopIteration):
            await self.send_text(
                context.bot, chat_id, "查询失败，请重新打开菜单或检查采集服务。"
            )
        except (BadRequest, NetworkError) as exc:
            self.logger.warning(
                "Telegram Watch callback reply failed: action=%s error=%s",
                action,
                type(exc).__name__,
            )
            with contextlib.suppress(BadRequest, NetworkError):
                await query.answer(
                    "操作回复发送失败，请稍后重试；已进入输入步骤时可直接发送内容。",
                    show_alert=True,
                )
        raise ApplicationHandlerStop

    async def dispatch(self, bot, chat_id, args):
        """Execute private management actions and show effective monitoring state.

        Args:
            bot: Existing Telegram bot.
            chat_id: Authorized private chat.
            args: Tokenized command arguments.
        """
        if not args or args[0] == "cancel":
            self.pending.pop(chat_id, None)
            day = datetime.now(TZ).strftime("%Y%m%d")
            await self.send_text(
                bot,
                chat_id,
                "🛠 监控管理\n① 添加监控群：发送链接，自动判断免入群采集或确认入群。\n"
                "② 选择监控群：包含已配置的公开群和账号已加入的群。\n"
                "③ 每个群分别配置人员，可单独暂停或恢复。\n"
                "④ 暂停不删除历史，恢复不补算暂停期间。\n"
                "输入 /tgwatch help 查看命令；输入 /cancel 取消当前操作。",
                Keyboard(
                    [
                        [
                            Button("🧭 配置检查", callback_data="tw:setup"),
                            Button("➕ 添加监控群", callback_data="tw:join"),
                            Button("👥 选择监控群", callback_data="tw:menu:dialogs"),
                        ],
                        [
                            Button("👥 群与监控人员", callback_data="tw:menu:groups"),
                            Button("📡 运行状态", callback_data="tw:menu:status"),
                        ],
                        [
                            Button(
                                "📖 查看记录", callback_data=f"tw:records:{day}:0:0:0"
                            )
                        ],
                        [
                            Button("🔗 链接规则", callback_data="tw:rules:0"),
                            Button("🚫 违规细节", callback_data="tw:violations:0:0"),
                        ],
                        [
                            Button("🔴 暂停全部", callback_data="tw:pause:1"),
                            Button("🟢 恢复全部", callback_data="tw:pause:0"),
                        ],
                    ]
                    + (
                        [
                            [
                                Button("👑 管理员设置", callback_data="tw:admins"),
                                Button("📣 工作群设置", callback_data="tw:work"),
                            ]
                        ]
                        if chat_id == self.owner
                        else []
                    )
                ),
            )
        elif args[0] == "status":
            section = args[1] if len(args) > 1 else "menu"
            categories = {
                "account": "📡 采集账号",
                "work": "📣 工作群",
                "groups": "👥 监控群",
                "people": "👤 监控人员",
                "reports": "📊 榜单发送",
                "gaps": "🕳 采集缺口",
            }
            back = [Button("⬅️ 返回运行状态", callback_data="tw:menu:status")]
            if section not in categories:
                buttons = [
                    Button(title, callback_data=f"tw:menu:status:{key}")
                    for key, title in categories.items()
                ]
                await self.send_text(
                    bot,
                    chat_id,
                    "📋 运行状态\n\n请选择要查看的分类：",
                    Keyboard(
                        [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
                        + [[Button("⬅️ 返回主菜单", callback_data="tw:menu")]]
                    ),
                )
                return
            lines = [categories[section], ""]
            rows = []
            if section == "work":
                for group_id, title in self.state.get("work_groups", {}).items():
                    enabled = self.state.get("work_group_enabled", {}).get(
                        group_id, False
                    )
                    lines.append(
                        f"{'🟢' if enabled else '🔴'} {title}｜{'允许定时发榜' if enabled else '不发榜'}"
                    )
                if len(lines) == 2:
                    lines.append("暂无工作群")
                if chat_id == self.owner:
                    rows.append([Button("⚙️ 工作群设置", callback_data="tw:work")])
            elif section == "reports":
                reports = self.state.get("reports", {})
                for status, title in (
                    ("sent", "🟢 已发送"),
                    ("pending", "⏳ 待重试"),
                    ("uncertain", "⚠️ 待核查"),
                    ("missed", "🔴 已过期未发送"),
                ):
                    entries = [
                        (key, value)
                        for key, value in reports.items()
                        if value["status"] == status
                    ]
                    lines.append(f"{title}：{len(entries)} 份")
                    for key, _ in sorted(
                        entries,
                        key=lambda entry: entry[1].get("created", 0),
                        reverse=True,
                    )[:3]:
                        parts = key.split(":", 2)
                        group_name = self.state.get("work_groups", {}).get(
                            parts[0], parts[0]
                        )
                        if len(parts) == 3 and parts[1] in ("hour", "day"):
                            when = datetime.fromisoformat(parts[2]).astimezone(TZ)
                            lines.append(
                                f"  {group_name}｜{'小时榜' if parts[1] == 'hour' else '日榜'}｜{when:%m-%d %H:%M}"
                            )
                    lines.append("")
                lines.append(
                    "每类最多显示最近 3 份；待核查不自动重发，过期小时榜不补发。"
                )
            else:
                health = await self.api("status")
                paused = health.get("paused", False)
                if section == "account":
                    labels = {
                        "connected": "🟢 在线",
                        "starting": "⏳ 启动中",
                        "connecting": "⏳ 连接中",
                        "login_required": "🔴 登录失效，请在采集服务本机重新登录",
                        "flood_wait": "⏳ Telegram 限流等待中",
                        "connection_error": "🔴 连接异常",
                    }
                    lines += [
                        f"账号连接：{labels.get(health['state'], '未知')}",
                        f"监控总开关：{'🔴 已暂停' if paused else '🟢 已开启'}",
                        f"正在监控的群内人员：{health.get('active_watches', 0)}",
                    ]
                    if health.get("retry_at"):
                        when = datetime.fromisoformat(health["retry_at"]).astimezone(TZ)
                        lines.append(f"限流重试时间：{when:%m-%d %H:%M:%S}")
                    rows.append(
                        [
                            Button(
                                "🟢 恢复全部" if paused else "🔴 暂停全部",
                                callback_data=f"tw:pause:{int(not paused)}",
                            )
                        ]
                    )
                elif section == "groups":
                    selected = int(args[2]) if len(args) > 2 else None
                    groups = health["groups"]
                    if selected is None:
                        lines.append("点击群名查看人员数、消息数与同步进度。")
                        rows += [
                            [
                                Button(
                                    f"{'🟢' if g['enabled'] and not paused else '🔴'} {g['name']}"[
                                        :60
                                    ],
                                    callback_data=f"tw:menu:status:groups:{g['id']}",
                                )
                            ]
                            for g in groups
                        ]
                        if not groups:
                            lines.append("暂无监控群")
                    else:
                        group = next((g for g in groups if g["id"] == selected), None)
                        if group is None:
                            lines.append("该群已移除，请返回刷新列表。")
                        else:
                            state = (
                                "🔴 全部暂停"
                                if paused
                                else "🔴 群已暂停"
                                if not group["enabled"]
                                else "⚠️ 采集异常"
                                if group["error"] or group["stale"]
                                else "⏳ 待添加人员"
                                if not group.get("watch_count")
                                else "🟢 监控中"
                            )
                            lines += [
                                group["name"],
                                "",
                                state,
                                "采集方式："
                                + (
                                    "👁 公开群免入群"
                                    if group.get("collection_mode") == "public"
                                    else "👥 已入群监听"
                                ),
                                f"群 ID：{group['id']}",
                                f"监控人员：{group.get('watch_count', 0)} 人",
                                f"今日消息：{group.get('today_count', 0)} 条",
                            ]
                            for field, label in (
                                ("last_sync", "最后同步"),
                                ("last_message", "最近消息"),
                            ):
                                value = group.get(field)
                                lines.append(
                                    f"{label}："
                                    + (
                                        datetime.fromisoformat(value)
                                        .astimezone(TZ)
                                        .strftime("%m-%d %H:%M:%S")
                                        if value
                                        else "暂无"
                                    )
                                )
                        rows.append(
                            [
                                Button(
                                    "⬅️ 返回监控群",
                                    callback_data="tw:menu:status:groups",
                                )
                            ]
                        )
                elif section == "people":
                    page = max(0, int(args[2])) if len(args) > 2 else 0
                    people = health.get("people", [])
                    for person in people[page * 10 : (page + 1) * 10]:
                        lines.append(
                            f"{person['name']}\n{self.person_status(person['id'], health)}\n"
                        )
                    if not people:
                        lines.append("暂无监控人员")
                    nav = []
                    if page:
                        nav.append(
                            Button(
                                "⬅️ 上一页",
                                callback_data=f"tw:menu:status:people:{page - 1}",
                            )
                        )
                    if (page + 1) * 10 < len(people):
                        nav.append(
                            Button(
                                "下一页 ➡️",
                                callback_data=f"tw:menu:status:people:{page + 1}",
                            )
                        )
                    if nav:
                        rows.append(nav)
                elif section == "gaps":
                    gaps = health.get("gaps", [])
                    lines += [
                        f"⚠️ 当前未闭合：{sum(g.get('end') is None for g in gaps)}",
                        f"历史已恢复区间：{sum(g.get('end') is not None for g in gaps)}",
                        "",
                        "恢复不代表从未漏采，断线期间已删除的消息可能无法找回。",
                    ]
            rows += [
                [Button("🔄 刷新本页", callback_data="tw:menu:" + ":".join(args))],
                back,
            ]
            await self.send_text(bot, chat_id, "\n".join(lines), Keyboard(rows))
        elif args[0] in ("groups", "dialogs", "people"):
            kind = args[0]
            items = (await self.api(kind))["items"]
            if kind == "dialogs":
                configured = (await self.api("groups"))["items"]
                items = list(
                    {item["id"]: item for item in [*items, *configured]}.values()
                )
            if not items:
                await self.send_text(
                    bot,
                    chat_id,
                    "暂无项目。请点“添加监控群”发送群链接，再选择监控人员。",
                )
            for offset in range(0, len(items), 12):
                rows, lines = [], []
                for item in items[offset : offset + 12]:
                    if kind == "dialogs":
                        rows.append(
                            [
                                Button(
                                    f"选择 {item['name'][:28]}",
                                    callback_data=f"tw:addg:{item['id']}",
                                )
                            ]
                        )
                        lines.append(f"{item['name']}｜{item['id']}")
                    else:
                        enabled = bool(item["enabled"])
                        label = "🟢 已启用" if enabled else "🔴 已暂停"
                        lines.append(f"{item['name']}｜{label}｜{item['id']}")
                        buttons = []
                        if kind == "groups":
                            buttons.append(
                                Button(
                                    f"{item['name'][:15]} · 人员",
                                    callback_data=f"tw:menu:watches:{item['id']}",
                                )
                            )
                        buttons.append(
                            Button(
                                f"{'🔴 暂停' if enabled else '🟢 恢复'} {item['name'][:15]}",
                                callback_data=f"tw:toggle:{kind}:{item['id']}:{int(not enabled)}",
                            )
                        )
                        if kind == "people":
                            buttons.append(
                                Button(
                                    "🗑 删除",
                                    callback_data=f"tw:persondelete:{item['id']}:0",
                                )
                            )
                        if kind == "groups" and not enabled:
                            buttons.append(
                                Button(
                                    "🗑 删除 / 退出",
                                    callback_data=f"tw:groupdelete:{item['id']}",
                                )
                            )
                        rows.append(buttons)
                await self.send_text(bot, chat_id, "\n".join(lines), Keyboard(rows))
            await self.send_text(
                bot,
                chat_id,
                "可选择已配置的公开群或已加入的群；新群请点“添加监控群”发送链接。"
                if kind == "dialogs"
                else "按群管理监控范围；历史记录不会删除。",
                Keyboard(
                    [
                        [
                            Button("➕ 添加监控群", callback_data="tw:join"),
                            Button("🏠 返回菜单", callback_data="tw:menu"),
                        ]
                    ]
                ),
            )
        elif args[0] == "watches":
            group = int(args[1])
            items = (await self.api("watches", {"group": group}))["items"]
            health = await self.api("status")
            page = int(args[2]) if len(args) > 2 else 0
            page = min(max(0, page), max(0, (len(items) - 1) // 12))
            rows = []
            lines = [f"👥 群内监控人员（仅作用于本群）｜第 {page + 1} 页", ""]
            if health.get("paused"):
                lines.append("🔴 当前已暂停全部监控，恢复总开关后按下列设置生效。")
            for item in items[page * 12 : (page + 1) * 12]:
                effective = (
                    item["enabled"]
                    and item["group_enabled"]
                    and item["person_enabled"]
                    and not health.get("paused")
                )
                reason = (
                    "监控中"
                    if effective
                    else "全部暂停"
                    if health.get("paused")
                    else "群已暂停"
                    if not item["group_enabled"]
                    else "人员全局暂停"
                    if not item["person_enabled"]
                    else "本群已暂停"
                )
                lines.append(
                    f"{item['person_name']}｜{'🟢' if effective else '🔴'} {reason}｜{item['user_id']}"
                )
                rows.append(
                    [
                        Button(
                            f"{'🔴 暂停' if item['enabled'] else '🟢 恢复'} {item['person_name'][:24]}",
                            callback_data=f"tw:watch:{group}:{item['user_id']}:{int(not item['enabled'])}",
                        ),
                        Button(
                            "🗑 删除",
                            callback_data=f"tw:persondelete:{item['user_id']}:{group}",
                        ),
                    ]
                )
                started = (
                    min(
                        v
                        for v in (item.get("started_at"), item.get("history_start"))
                        if v is not None
                    )
                    if item.get("started_at") or item.get("history_start")
                    else None
                )
                if started:
                    lines.append(
                        "🕒 监控起点："
                        + datetime.fromtimestamp(started, TZ).strftime("%Y-%m-%d %H:%M")
                    )
                if item.get("backfill_state"):
                    label = {
                        "pending": "⏳ 历史补采中（暂停时等待恢复）",
                        "completed": "✅ 可访问历史已扫描",
                        "failed": "🔴 补采失败，可重新选择时间重试",
                    }.get(item["backfill_state"], "状态未知")
                    lines.append(
                        f"📥 {label} · 范围内 {item.get('backfill_count', 0)} 条"
                    )
                rows.append(
                    [
                        Button(
                            "📥 补采历史 · " + item["person_name"][:20],
                            callback_data=f"tw:history:{group}:{item['user_id']}",
                        )
                    ]
                )
                if not item["person_enabled"]:
                    rows.append(
                        [
                            Button(
                                "🟢 解除该人员全局暂停",
                                callback_data=f"tw:toggle:people:{item['user_id']}:1",
                            )
                        ]
                    )
            if not items:
                lines.append("尚未添加人员。")
            navigation = []
            if page > 0:
                navigation.append(
                    Button(
                        "上一页", callback_data=f"tw:menu:watches:{group}:{page - 1}"
                    )
                )
            if (page + 1) * 12 < len(items):
                navigation.append(
                    Button(
                        "下一页", callback_data=f"tw:menu:watches:{group}:{page + 1}"
                    )
                )
            if navigation:
                rows.append(navigation)
            rows += [
                [
                    Button("👥 选择群员", callback_data=f"tw:members:{group}:0"),
                    Button("⌨️ 输入添加", callback_data=f"tw:input:{group}"),
                    Button("↩️ 返回群列表", callback_data="tw:menu:groups"),
                ]
            ]
            await self.send_text(bot, chat_id, "\n".join(lines), Keyboard(rows))
        elif args[0] in ("pause", "resume"):
            health = await self.api("control", data={"paused": args[0] == "pause"})
            await self.send_text(
                bot,
                chat_id,
                "🔴 已暂停全部监控；历史查询和定时报表仍可用。"
                if health["paused"]
                else "🟢 已恢复监控；各群和人员原来的暂停设置保留，暂停期间不补采。",
                Keyboard(
                    [
                        [
                            Button("📡 查看状态", callback_data="tw:menu:status"),
                            Button("🏠 返回菜单", callback_data="tw:menu"),
                        ]
                    ]
                ),
            )
        elif args[0] == "person":
            action, value = args[1:3]
            if action == "add":
                await self.send_text(
                    bot,
                    chat_id,
                    "请先选择群，再输入该群要监控的 @用户名。",
                    Keyboard([[Button("💬 选择群", callback_data="tw:menu:dialogs")]]),
                )
                return
            person = next(
                (
                    p
                    for p in (await self.api("people"))["items"]
                    if p["id"] == int(value)
                ),
                None,
            )
            if not person or action not in ("name", "enable", "disable"):
                raise ValueError("Unknown person or action")
            name = " ".join(args[3:]) if action == "name" else person["name"]
            enabled = (
                bool(person["enabled"]) if action == "name" else action == "enable"
            )
            await self.api(
                "people", data={"id": person["id"], "name": name, "enabled": enabled}
            )
            await self.send_text(
                bot,
                chat_id,
                f"{name}｜{'🟢 已启用' if enabled else '🔴 已暂停'}\n人员设置已保存（此操作作用于该人员的全部群）。",
            )
        elif args[0] == "records":
            day = (
                datetime.strptime(args[1], "%Y-%m-%d").strftime("%Y%m%d")
                if len(args) > 1
                else datetime.now(TZ).strftime("%Y%m%d")
            )
            await self.records(
                bot,
                chat_id,
                day,
                int(args[2]) if len(args) > 2 else 0,
                int(args[3]) if len(args) > 3 else 0,
                0,
            )
        else:
            await self.send_text(bot, chat_id, HELP)

    async def input_person(self, update, context):
        """Consume usernames only in an authorized, unexpired private setup flow.

        Args:
            update: A private text message from the shared Telegram application.
            context: Shared bot callback context.
        """
        chat_id = update.effective_chat.id
        pending = self.pending.get(chat_id)
        if not pending or update.effective_chat.type != "private":
            return
        if update.effective_user.id not in self.admins:
            self.pending.pop(chat_id, None)
            raise ApplicationHandlerStop
        if not self.ingress_limit.allow(update.effective_user.id):
            raise ApplicationHandlerStop
        if monotonic() > pending["expires"]:
            self.pending.pop(chat_id, None)
            await self.send_text(
                context.bot, chat_id, "输入已超时，请用 /tgwatch 重新选择群。"
            )
            raise ApplicationHandlerStop
        if pending.get("mode") == "workgroup":
            if update.effective_user.id != self.owner:
                raise ApplicationHandlerStop
            value = update.effective_message.text.strip()
            try:
                group = await context.bot.get_chat(
                    int(value) if value.startswith("-") else value
                )
                if group.type not in ("group", "supergroup"):
                    raise ValueError("Group required")
                member = await context.bot.get_chat_member(group.id, context.bot.id)
                writable = (
                    member.status in ("administrator", "creator")
                    or member.status == "member"
                    and bool(group.permissions and group.permissions.can_send_messages)
                )
                if not writable:
                    raise ValueError("Group send permission required")
                self.state.setdefault("work_groups", {})[str(group.id)] = group.title
                self.state.setdefault("work_group_enabled", {}).setdefault(
                    str(group.id), False
                )
                await self.put_kv_data("delivery_state", self.state)
                self.pending.pop(chat_id, None)
                await self.send_text(
                    context.bot,
                    chat_id,
                    f"🟢 已添加工作群：{group.title}\n点击工作群里的红色群名，变成绿色后会定时发榜。",
                    Keyboard([[Button("📣 工作群设置", callback_data="tw:work")]]),
                )
            except (BadRequest, Forbidden, NetworkError, ValueError):
                await self.send_text(
                    context.bot,
                    chat_id,
                    "🔴 无法绑定，请检查群 ID、机器人入群情况和发言权限。",
                )
            raise ApplicationHandlerStop
        if pending.get("mode") == "admin":
            if update.effective_user.id != self.owner:
                self.pending.pop(chat_id, None)
                raise ApplicationHandlerStop
            try:
                value = update.effective_message.text.strip()
                link = re.fullmatch(
                    r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?", value
                )
                if link:
                    value = "@" + link[1]
                if not re.fullmatch(
                    r"@?[A-Za-z][A-Za-z0-9_]{3,31}|[1-9][0-9]{0,18}", value
                ):
                    raise ValueError("Invalid administrator")
                person = await self.api("resolve", {"user": value})
                updated = self.admins | {int(person["id"])}
                await self.save_admins(updated)
                self.pending.pop(chat_id, None)
                await self.send_text(
                    context.bot,
                    chat_id,
                    f"🟢 管理员已添加\n{person['name']}｜{person['id']}",
                    Keyboard(
                        [
                            [
                                Button("👑 管理员设置", callback_data="tw:admins"),
                                Button("📣 工作群设置", callback_data="tw:work"),
                            ]
                        ]
                    ),
                )
            except (RuntimeError, ValueError) as exc:
                await self.send_text(
                    context.bot,
                    chat_id,
                    str(exc)
                    if isinstance(exc, RuntimeError)
                    else "请输入有效数字 ID、@用户名或 t.me 链接。",
                )
            raise ApplicationHandlerStop
        if pending.get("mode") == "rule":
            try:
                patterns = [
                    line.strip()
                    for line in update.effective_message.text.splitlines()
                    if line.strip()
                ]
                await self.api(
                    "link_rules",
                    data={
                        "person": pending["person"],
                        "patterns": patterns,
                        "enabled": True,
                    },
                )
                self.pending.pop(chat_id, None)
                await self.send_text(
                    context.bot,
                    chat_id,
                    "🟢 链接规则已保存并启用",
                    Keyboard(
                        [
                            [
                                Button(
                                    "🔗 查看规则",
                                    callback_data=f"tw:rule:{pending['person']}",
                                )
                            ]
                        ]
                    ),
                )
            except RuntimeError as exc:
                await self.send_text(
                    context.bot, chat_id, str(exc) + "\n可重新输入，或 /cancel 取消。"
                )
            raise ApplicationHandlerStop
        if pending.get("mode") == "history_time":
            try:
                value = update.effective_message.text.strip()
                now = datetime.now(TZ)
                match = re.fullmatch(
                    r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", value
                )
                if match:
                    start = datetime(
                        now.year,
                        int(match[1]),
                        int(match[2]),
                        int(match[3]),
                        int(match[4]),
                        int(match[5] or 0),
                        tzinfo=TZ,
                    )
                else:
                    start = datetime.strptime(
                        value,
                        "%Y-%m-%d %H:%M:%S"
                        if value.count(":") == 2
                        else "%Y-%m-%d %H:%M",
                    ).replace(tzinfo=TZ)
                if not now - timedelta(days=90) <= start <= now:
                    raise ValueError("Start outside retention window")
                await self.apply_monitor_start(
                    context.bot,
                    chat_id,
                    pending["group"],
                    pending["person"],
                    start.astimezone(timezone.utc).isoformat(),
                )
            except ValueError:
                await self.send_text(
                    context.bot,
                    chat_id,
                    "请输入最近 90 天内且不晚于当前的北京时间，例如 9月21日0:13、9月21日0:13:25 或 2026-09-20 09:00。",
                )
            except RuntimeError as exc:
                await self.send_text(
                    context.bot, chat_id, str(exc) + "\n可重新输入或 /cancel 取消。"
                )
            raise ApplicationHandlerStop
        if pending.get("mode") == "join":
            # Consume once before the RPC so ambiguous failures never auto-retry.
            self.pending.pop(chat_id, None)
            try:
                result = await self.api(
                    "join",
                    data={
                        "link": update.effective_message.text.strip(),
                        "prepare": True,
                    },
                )
                if result["status"] == "join_required":
                    self.pending[chat_id] = {
                        "mode": "confirm_join",
                        "link": update.effective_message.text.strip(),
                        "expires": monotonic() + 600,
                    }
                    text = "➕ 添加监控群\n\n需要采集账号加入该群才能继续。" + (
                        "免入群采集名额已满（10 个）。"
                        if result.get("reason") == "public_limit"
                        else ""
                    )
                    rows = [
                        [Button("✅ 加入该群并监控", callback_data="tw:confirmjoin")]
                    ]
                elif result["status"] == "pending":
                    text = "🟡 已提交入群申请\n等待群管理员审核。审核通过后，点击刷新并选择监控人员。"
                    rows = [
                        [Button("🔄 刷新并选择监控", callback_data="tw:menu:dialogs")]
                    ]
                elif result.get("id"):
                    text = f"{result['name']}\n\n🟢 {'公开群免入群采集' if result['status'] == 'public' else '已在群内' if result['status'] == 'already_joined' else '加入成功'}\n选择群员后才会开始对应监控。"
                    rows = [
                        [
                            Button(
                                "📅 选择监控起始时间",
                                callback_data=f"tw:history:{result['id']}:0",
                            )
                        ]
                    ]
                else:
                    text = "🟢 已在群内\n请刷新列表后选择监控人员。"
                    rows = [
                        [Button("👥 刷新并选择监控", callback_data="tw:menu:dialogs")]
                    ]
                rows.append([Button("🏠 返回菜单", callback_data="tw:menu")])
                await self.send_text(context.bot, chat_id, text, Keyboard(rows))
            except RuntimeError as exc:
                await self.send_text(
                    context.bot,
                    chat_id,
                    str(exc),
                    Keyboard(
                        [
                            [
                                Button(
                                    "🔄 查看已加入群", callback_data="tw:menu:dialogs"
                                ),
                                Button("➕ 重新输入", callback_data="tw:join"),
                            ]
                        ]
                    ),
                )
            raise ApplicationHandlerStop
        try:
            values = shlex.split(update.effective_message.text.strip())
            value = values[0]
            link = re.fullmatch(
                r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?",
                value,
                re.IGNORECASE,
            )
            if link:
                value = "@" + link[1]
            if not re.fullmatch(
                r"@?[A-Za-z][A-Za-z0-9_]{3,31}|[1-9][0-9]{0,18}", value
            ):
                raise ValueError("Invalid username or ID")
            person = await self.api("resolve", {"user": value})
            existing = next(
                (
                    p
                    for p in (await self.api("people"))["items"]
                    if p["id"] == person["id"]
                ),
                None,
            )
            name = " ".join(values[1:]) or (
                existing["name"] if existing else person["name"]
            )
            await self.api(
                "people",
                data={
                    "id": person["id"],
                    "name": name,
                    "enabled": bool(existing["enabled"]) if existing else True,
                },
            )
            self.pending.pop(chat_id, None)
            await self.choose_monitor_start(
                context.bot, chat_id, pending["group"], person["id"]
            )
        except RuntimeError as exc:
            await self.send_text(
                context.bot, chat_id, str(exc) + "\n可重新输入，或 /cancel 取消。"
            )
        except (ValueError, IndexError, KeyError):
            await self.send_text(
                context.bot,
                chat_id,
                "请输入 @用户名、t.me/用户名 或数字 ID，可在后面加显示名，例如：t.me/dh114514 大蛤。\n/cancel 取消。",
            )
        raise ApplicationHandlerStop

    async def apply_monitor_start(self, bot, chat_id, group, person, start):
        """Apply a selected start time or retain it for subsequent member selection.

        Args:
            bot: Bound Telegram bot.
            chat_id: Authorized administrator chat.
            group: Source group ID.
            person: User ID, or zero when choosing the group start first.
            start: UTC timestamp, or None for live monitoring only.
        """
        if person:
            data = {"group": group, "person": person, "enabled": True}
            if start is not None:
                data["start"] = start
            await self.api("watches", data=data)
            self.pending.pop(chat_id, None)
            await self.dispatch(bot, chat_id, ["watches", str(group)])
        else:
            await self.api("groups", data={"id": group, "enabled": True})
            self.monitor_starts[(chat_id, group)] = {
                "start": start,
                "expires": monotonic() + 600,
            }
            self.pending[chat_id] = {"group": group, "expires": monotonic() + 600}
            label = (
                datetime.fromisoformat(start)
                .astimezone(TZ)
                .strftime("%Y-%m-%d %H:%M:%S")
                if start
                else "从现在开始（不补历史）"
            )
            await self.send_text(
                bot,
                chat_id,
                f"🕒 起始时间：{label}\n\n👥 请选择群员，或发送 @用户名、t.me/用户名、数字 ID。\n选择人员后开始补采并持续监控；本次时间选择 10 分钟内有效。",
                Keyboard(
                    [
                        [Button("👥 选择群员", callback_data=f"tw:members:{group}:0")],
                        [
                            Button(
                                "📅 修改起始时间", callback_data=f"tw:history:{group}:0"
                            )
                        ],
                        [Button("🏠 返回菜单", callback_data="tw:menu")],
                    ]
                ),
            )

    async def choose_monitor_start(self, bot, chat_id, group, person):
        """Offer the same start-time choices for typed, selected, and existing users.

        Args:
            bot: Bound Telegram bot.
            chat_id: Authorized private administrator chat.
            group: Source Telegram group ID.
            person: Monitored Telegram user ID.
        """
        selection = self.monitor_starts.get((chat_id, group))
        if person and selection and selection["expires"] > monotonic():
            await self.apply_monitor_start(
                bot, chat_id, group, person, selection["start"]
            )
            return
        await self.send_text(
            bot,
            chat_id,
            "🕒 从什么时候开始监控？\n\n时间按北京时间，最多回溯 90 天。历史补采只保存此人在本群的可访问消息，按原发送时间统计。已删除或不可访问的消息无法恢复；已发送榜单不重发。",
            Keyboard(
                [
                    [
                        Button(
                            "🟢 从现在开始",
                            callback_data=f"tw:startwatch:{group}:{person}:now",
                        )
                    ],
                    [
                        Button(
                            "🌅 今天 00:00 起",
                            callback_data=f"tw:startwatch:{group}:{person}:today",
                        ),
                        Button(
                            "🕘 最近 24 小时",
                            callback_data=f"tw:startwatch:{group}:{person}:day",
                        ),
                    ],
                    [
                        Button(
                            "📅 自定义时间",
                            callback_data=f"tw:startwatch:{group}:{person}:custom",
                        )
                    ],
                    [Button("⬅️ 返回人员", callback_data=f"tw:menu:watches:{group}")],
                ]
            ),
        )

    @staticmethod
    def person_status(person_id, health):
        """Resolve effective person status across account, pause and group switches.

        Args:
            person_id: Telegram user ID.
            health: Collector status snapshot used for this display.

        Returns:
            Chinese status including mixed group availability, without raw errors.
        """
        if health.get("paused"):
            return "🔴 全部监控已暂停"
        person = next(
            (p for p in health.get("people", []) if p["id"] == person_id), None
        )
        if person and not person["enabled"]:
            return "🔴 人员已暂停"
        watches = [w for w in health.get("watches", []) if w["user_id"] == person_id]
        if not watches:
            return "未配置监控群" if "watches" in health else "状态未知"
        active = [
            w
            for w in watches
            if w["enabled"] and w["group_enabled"] and w["person_enabled"]
        ]
        if not active:
            return "🔴 所有监控群已暂停"
        account = health.get("state")
        if account != "connected":
            return {
                "starting": "⏳ 采集账号启动中",
                "connecting": "⏳ 采集账号连接中",
                "login_required": "🔴 采集账号待登录",
                "flood_wait": "⏳ 采集账号限流等待",
                "connection_error": "🔴 采集账号连接中断",
            }.get(account, "状态未知")
        groups = {g["id"]: g for g in health.get("groups", [])}
        unavailable = sum(
            bool(
                groups.get(w["chat_id"], {}).get("error")
                or groups.get(w["chat_id"], {}).get("stale")
                or w["chat_id"] not in groups
            )
            for w in active
        )
        if unavailable == len(active):
            return "⏳ 等待群同步"
        if unavailable:
            return (
                f"🟡 {len(active) - unavailable} 个群监控中 · {unavailable} 个群待同步"
            )
        if any(w.get("backfill_state") == "pending" for w in active):
            return "🟢 监控中 · ⏳ 历史补采中"
        if any(w.get("backfill_state") == "failed" for w in active):
            return "🟡 监控中 · 历史补采失败"
        paused = len(watches) - len(active)
        return f"🟢 {len(active)} 个群监控中" + (
            f" · 🔴 {paused} 个群已暂停" if paused else ""
        )

    async def monitor_status(self, bot, chat_id, page):
        """Show a read-only person status page in private or the authorized work group.

        Args:
            bot: Shared Telegram bot.
            chat_id: Authorized private chat or configured work group.
            page: Zero-based person page.
        """
        health = await self.api("status")
        people = sorted(health.get("people", []), key=lambda p: p["id"])
        pages = max(1, (len(people) + 9) // 10)
        if page < 0 or page >= pages:
            raise ValueError("Invalid status page")
        lines = [
            f"📋 监控状态 · 第 {page + 1}/{pages} 页",
            f"🕒 更新时间：{datetime.now(TZ):%m-%d %H:%M}（北京时间）",
            "",
        ]
        for person in people[page * 10 : (page + 1) * 10]:
            lines.extend([person["name"], self.person_status(person["id"], health), ""])
        if not people:
            lines.append("📭 尚未配置监控人员")
        navigation = []
        if page:
            navigation.append(Button("◀ 上一页", callback_data=f"tw:status:{page - 1}"))
        if page + 1 < pages:
            navigation.append(Button("下一页 ▶", callback_data=f"tw:status:{page + 1}"))
        rows = [navigation] if navigation else []
        day = datetime.now(TZ).strftime("%Y%m%d")
        rows.append(
            [
                Button("🔄 刷新状态", callback_data=f"tw:status:{page}"),
                Button("📖 聊天记录", callback_data=f"tw:records:{day}:0:0:0"),
            ]
        )
        await self.send_text(
            bot, chat_id, "\n".join(lines), Keyboard(rows), allow_group=True
        )

    async def records(self, bot, chat_id, day, person, group, page):
        """Display one bounded record page with date and identity filters.

        Args:
            bot: Existing Telegram bot.
            chat_id: Authorized private chat.
            day: Compact Beijing calendar date.
            person: User filter, zero for all.
            group: Group filter, zero for all.
            page: Zero-based page number.
        """
        date, separator, hour_text = day.partition("h")
        midnight = datetime.strptime(date, "%Y%m%d").replace(tzinfo=TZ)
        hour = int(hour_text) if separator else None
        if hour is not None and not 0 <= hour <= 23:
            raise ValueError("Invalid hour")
        start = midnight + timedelta(hours=hour or 0)
        end = start + (timedelta(hours=1) if hour is not None else timedelta(days=1))
        result = await self.api(
            "messages",
            {
                "start": start.astimezone(timezone.utc).isoformat(),
                "end": end.astimezone(timezone.utc).isoformat(),
                "person": person,
                "group": group,
                "page": page,
            },
        )
        if page < 0:
            raise ValueError("Invalid page")
        lines = [f"📖 聊天记录｜{start:%Y-%m-%d}｜第 {page + 1} 页"]
        if hour is not None:
            lines.append(f"🕒 {start:%H:%M}—{end:%H:%M}（北京时间）")
        if "total" in result:
            lines.append(
                f"💬 已采集 {result['total']} 条"
                + (" · 点击小时筛选记录" if hour is None else "")
            )
        pending_history = any(
            w.get("backfill_state") == "pending"
            and (not person or w["user_id"] == person)
            and (not group or w["chat_id"] == group)
            for w in result["health"].get("watches", [])
        )
        if pending_history:
            lines.append("⏳ 历史记录补充中，当前数量可能不完整。")
        media_labels = {
            "text": "文字",
            "sticker": "贴纸",
            "voice": "语音",
            "photo": "图片",
            "video": "视频",
            "media": "媒体",
            "document": "文件",
            "audio": "音频",
            "animation": "动图",
            "poll": "投票",
            "contact": "联系人",
            "location": "位置",
        }
        if result["health"]["incomplete"]:
            lines.append("⚠️ 存在采集异常或历史缺口，记录可能不完整。")
        for item in result["items"]:
            stamp = datetime.fromisoformat(item["sent"]).astimezone(TZ)
            body = "[已删除]" if item["deleted"] else item["text"]
            if body is None:
                body = "[原文已过保留期]"
            reply = f" 回复消息 #{item['reply_to']}" if item["reply_to"] else ""
            lines.append(
                f"\n{item['person_name']} · 🕒 {stamp:%H:%M:%S}\n💬 {item['group_name']}{reply}\n"
                + (
                    ""
                    if item["media"] == "text"
                    else f"【{media_labels.get(item['media'], '其他消息')}】"
                )
                + body
            )
        if not result["items"]:
            lines.append("📭 此筛选范围内暂无已采集记录。")
        navigation = []
        if page > 0:
            navigation.append(
                Button(
                    "上一页",
                    callback_data=f"tw:records:{day}:{person}:{group}:{page - 1}",
                )
            )
        if result["has_next"]:
            navigation.append(
                Button(
                    "下一页",
                    callback_data=f"tw:records:{day}:{person}:{group}:{page + 1}",
                )
            )
        rows = [navigation] if navigation else []
        if hour is None and "hours" in result:
            counts = {item["hour"]: item["count"] for item in result["hours"]}
            for offset in range(0, 24, 3):
                rows.append(
                    [
                        Button(
                            f"{h:02d}:00 · {counts.get(h, 0)} 条",
                            callback_data=f"tw:records:{date}h{h:02d}:{person}:{group}:0",
                        )
                        for h in range(offset, offset + 3)
                    ]
                )
        elif hour is not None:
            rows.append(
                [
                    Button(
                        "📅 返回全天记录",
                        callback_data=f"tw:records:{date}:{person}:{group}:0",
                    )
                ]
            )
        rows.append(
            [
                Button(
                    "🔄 刷新记录",
                    callback_data=f"tw:records:{day}:{person}:{group}:{page}",
                )
            ]
        )
        rows += [
            [
                Button(
                    "👤 选择人员", callback_data=f"tw:persons:{day}:{person}:{group}"
                ),
                Button("💬 选择群", callback_data=f"tw:chats:{day}:{person}:{group}"),
            ],
            [
                Button(
                    "前一天",
                    callback_data=f"tw:records:{midnight - timedelta(days=1):%Y%m%d}:{person}:{group}:0",
                ),
                Button(
                    "后一天",
                    callback_data=f"tw:records:{midnight + timedelta(days=1):%Y%m%d}:{person}:{group}:0",
                ),
            ],
        ]
        rows.append([Button("📋 监控状态", callback_data="tw:status:0")])
        await self.send_text(
            bot, chat_id, "\n".join(lines), Keyboard(rows), allow_group=True
        )

    async def report(self, bot, kind, start, end, now, destination=None):
        """Persist delivery intent before sending and avoid uncertain retries.

        Args:
            bot: Existing platform bot.
            kind: Hour or day report category.
            start: Inclusive local ranking window start.
            end: Exclusive local ranking window end.
            now: Current local timestamp used for retry deadlines.
            destination: Optional group ID for independent delivery tracking.
        """
        key = f"{kind}:{start.isoformat()}"
        if destination is not None:
            key = f"{destination}:{key}"
        reports = self.state["reports"]
        report = reports.get(key)
        if report and (
            report["status"] in ("sent", "uncertain", "sending", "missed")
            or report.get("retry_at", 0) > now.timestamp()
        ):
            return
        if report is None:
            cumulative_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            result = await self.api(
                "stats",
                {
                    "start": start.astimezone(timezone.utc).isoformat(),
                    "end": end.astimezone(timezone.utc).isoformat(),
                    "cumulative_start": cumulative_start.astimezone(
                        timezone.utc
                    ).isoformat(),
                },
            )
            title = "小时聊天榜" if kind == "hour" else "每日聊天总榜"
            lines = [
                f"📊 {title}｜{start:%Y-%m-%d}",
                f"🕒 北京时间 {start:%m-%d %H:%M}—{end:%m-%d %H:%M}",
                "━━━━━━━━━━━━━━",
            ]
            if result["health"]["incomplete"]:
                lines.append("※ 本榜为部分已采集数据")
            health = result["health"]
            monitored = (
                {watch["user_id"] for watch in health["watches"]}
                if "watches" in health
                else None
            )
            items = {
                item["id"]: item
                for item in result["items"]
                if monitored is None or item["id"] in monitored
            }
            for person in health.get("people", []):
                if monitored is not None and person["id"] not in monitored:
                    continue
                items.setdefault(
                    person["id"],
                    {
                        "id": person["id"],
                        "name": person["name"],
                        "count": 0,
                        "cumulative": 0,
                        "groups": 0,
                    },
                )
            for index, item in enumerate(
                sorted(items.values(), key=lambda row: (-row["count"], row["id"])), 1
            ):
                name = " ".join(item["name"].split())[:24]
                monitoring = self.person_status(item["id"], health)
                lines.append(
                    f"{name}｜第 {index} 名 {['🥇', '🥈', '🥉'][index - 1] if index <= 3 else ''}\n"
                    f"💬 本期 {item['count']} 条 · 📊 当日 {item['cumulative']} 条 · 👥 {item['groups']} 个群\n"
                    f"📡 监控状态：{monitoring}\n"
                )
            if not items:
                lines.append("📭 暂无排名人员")
            text = "\n".join(lines)
            report = {
                "status": "pending",
                "parts": [text[i : i + 1800] for i in range(0, len(text), 1800)],
                "next": 0,
                "created": now.timestamp(),
            }
            reports[key] = report
        keyboard = Keyboard(
            [
                [
                    Button(
                        "📖 聊天记录", callback_data=f"tw:records:{start:%Y%m%d}:0:0:0"
                    ),
                    Button("📋 监控状态", callback_data="tw:status:0"),
                    Button("🚫 违规细节", callback_data="tw:violations:0:0"),
                ]
            ]
        )
        while report["next"] < len(report["parts"]):
            index = report["next"]
            report["status"] = "sending"
            await self.put_kv_data("delivery_state", self.state)
            try:
                await bot.send_message(
                    chat_id=destination if destination is not None else self.work_chat,
                    text=self.markdown(report["parts"][index]),
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                    reply_markup=keyboard
                    if index == len(report["parts"]) - 1
                    else None,
                )
            except RetryAfter as exc:
                delay = (
                    exc.retry_after.total_seconds()
                    if isinstance(exc.retry_after, timedelta)
                    else exc.retry_after
                )
                report.update(status="pending", retry_at=now.timestamp() + delay + 1)
                await self.put_kv_data("delivery_state", self.state)
                return
            except (BadRequest, Forbidden):
                report.update(status="pending", retry_at=now.timestamp() + 300)
                await self.put_kv_data("delivery_state", self.state)
                return
            except Exception:
                report["status"] = "uncertain"
                await self.put_kv_data("delivery_state", self.state)
                return
            report["next"] += 1
            report["status"] = (
                "sent" if report["next"] == len(report["parts"]) else "pending"
            )
            await self.put_kv_data("delivery_state", self.state)

    async def tick(self, bot, now):
        """Dispatch independently to each authorized work group.

        Args:
            bot: Existing Telegram bot.
            now: Current Beijing timestamp.
        """
        switches = self.state.get("work_group_enabled")
        if switches is None:
            await self.tick_group(bot, now)
            return
        for group_id, enabled in list(switches.items()):
            if enabled:
                try:
                    await self.tick_group(bot, now, int(group_id))
                except RuntimeError:
                    self.state["last_report_error"] = "collector_unavailable"

    async def tick_group(self, bot, now, destination=None):
        """Schedule ranking windows without unsolicited health notifications.

        Args:
            bot: Existing initialized Telegram bot.
            now: Current Beijing datetime, injectable for tests.
            destination: Optional group ID with its own hourly cursor.
        """
        if destination is None and (
            not self.work_chat or not self.config.get("reports_enabled", True)
        ):
            return
        hour = now.replace(minute=0, second=0, microsecond=0)
        # Persist skipped windows instead of silently presenting them as delivered.
        last_hour = self.last_hour
        if destination is not None:
            saved = self.state.setdefault("work_group_hours", {}).get(str(destination))
            last_hour = datetime.fromisoformat(saved) if saved else hour
        cursor = max(last_hour, hour - timedelta(days=30))
        changed = False
        while cursor < hour:
            due = cursor + timedelta(hours=1)
            key = f"hour:{cursor.isoformat()}"
            if destination is not None:
                key = f"{destination}:{key}"
            previous = self.state["reports"].get(key, {})
            if due == hour and now - hour < timedelta(minutes=5):
                try:
                    await self.report(bot, "hour", cursor, due, now, destination)
                except RuntimeError:
                    self.state["last_report_error"] = "collector_unavailable"
                if self.state["reports"].get(key, {}).get("status") not in (
                    "sent",
                    "uncertain",
                ):
                    break
            elif previous.get("status") not in ("sent", "uncertain", "sending"):
                self.state["reports"][key] = {
                    "status": "missed",
                    "created": now.timestamp(),
                    "reason": "expired_hour_window",
                }
            last_hour = due
            cursor = due
            changed = True
        if destination is None:
            self.last_hour = last_hour
            self.state["last_hour"] = last_hour.isoformat()
        else:
            self.state["work_group_hours"][str(destination)] = last_hour.isoformat()
        if changed:
            await self.put_kv_data("delivery_state", self.state)
        day = now.date() - timedelta(days=1 if now.time() >= time(1, 30) else 2)
        start = datetime.combine(day, time.min, TZ)
        await self.report(
            bot, "day", start, start + timedelta(days=1), now, destination
        )
        expired = [
            key
            for key, value in self.state["reports"].items()
            if value["status"] in ("sent", "missed")
            and now.timestamp() - value["created"] > 30 * 86400
        ]
        for key in expired:
            del self.state["reports"][key]
        if expired:
            await self.put_kv_data("delivery_state", self.state)

    async def run(self):
        """Attach handlers and maintain one scheduler across adapter rebuilds."""
        while True:
            stage = "adapter_binding"
            try:
                platform = self.context.get_platform_inst(self.config["platform_id"])
                application = getattr(platform, "application", None)
                if application is not None and application is not self.application:
                    if self.application:
                        self.application.remove_handler(self.guard, HANDLER_GROUP + 1)
                        for handler in self.handlers:
                            self.application.remove_handler(handler, HANDLER_GROUP)
                    self.handlers = [
                        CommandHandler(["tgwatch", "start", "cancel"], self.command),
                        CallbackQueryHandler(self.callback, pattern=r"^tw:"),
                        MessageHandler(
                            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
                            self.input_person,
                        ),
                    ]
                    self.application = application
                    application.add_handler(self.guard, HANDLER_GROUP + 1)
                    for handler in self.handlers:
                        application.add_handler(handler, HANDLER_GROUP)
                if application is not None and application.running:
                    if (
                        self.menu_application is not application
                        and monotonic() >= self.menu_retry_at
                    ):
                        try:
                            await application.bot.set_my_commands(
                                [
                                    BotCommand("tgwatch", "工作统计 · 管理与记录"),
                                    BotCommand("start", "打开管理菜单"),
                                    BotCommand("cancel", "取消当前输入"),
                                ]
                            )
                            self.menu_application = application
                            self.menu_retry_at = 0.0
                        except TelegramError as exc:
                            delay = 300
                            if isinstance(exc, RetryAfter):
                                retry = exc.retry_after
                                delay = max(
                                    delay,
                                    retry.total_seconds()
                                    if isinstance(retry, timedelta)
                                    else retry,
                                )
                            self.menu_retry_at = monotonic() + delay
                            self.logger.warning(
                                "Telegram Watch menu registration failed (%s); retry deferred, scheduling continues",
                                type(exc).__name__,
                            )
                    stage = "report_scheduler"
                    await self.tick(application.bot, datetime.now(TZ))
                self.last_loop_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = (stage, type(exc).__name__)
                if failure != self.last_loop_error:
                    self.logger.warning(
                        "Telegram Watch iteration failed: stage=%s error=%s", *failure
                    )
                    self.last_loop_error = failure
            await asyncio.sleep(15)

    async def terminate(self):
        """Remove all registered handlers, stop the task, and close HTTP state."""
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.application:
            self.application.remove_handler(self.guard, HANDLER_GROUP + 1)
            for handler in self.handlers:
                self.application.remove_handler(handler, HANDLER_GROUP)
            self.handlers = []
            self.application = None
        self.pending.clear()
        self.monitor_starts.clear()
        if self.session:
            await self.session.close()
            self.session = None
