"""Control business handlers called by the AstrBot tenant-control plugin."""

import json
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import quote

from cryptography.fernet import Fernet
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonCommands,
    Update,
)
from telegram.ext import ContextTypes

from .store import Store

logger = logging.getLogger(__name__)
LABELS = {"first": "首月社区版", "month": "社区版续月", "year": "社区版年付"}
COMMANDS = {
    "start": "home",
    "plans": "plan_picker",
    "help": "home",
    "buy": "buy",
    "renew": "renew",
    "create": "create",
    "bots": "my_bots",
    "dashboard": "dashboard",
    "manage": "dashboard",
    "custom": "custom",
    "support": "support_command",
    "paysupport": "support_command",
    "adminprice": "admin_price",
    "adminquote": "admin_quote",
    "adminrefund": "admin_refund",
    "adminpause": "admin_switch",
    "adminresume": "admin_switch",
    "adminstatus": "admin_status",
    "adminbackup": "admin_operation",
    "adminrestore": "admin_operation",
    "adminupgrade": "admin_operation",
    "adminops": "admin_operations",
    "adminconfirm": "admin_confirm",
    "admin": "admin_menu",
    "configstatus": "config_status",
    "admintrial": "admin_trial",
}
PUBLIC_COMMANDS = [
    BotCommand("start", "主菜单"),
    BotCommand("plans", "套餐与价格"),
    BotCommand("bots", "我的机器人与续费"),
    BotCommand("manage", "Telegram 机器人管理"),
    BotCommand("create", "创建已付款机器人"),
    BotCommand("custom", "申请专属定制"),
    BotCommand("support", "联系客服"),
    BotCommand("paysupport", "支付与退款支持"),
    BotCommand("help", "帮助"),
]


async def publish_menu(bot):
    """Publish only customer commands, scoped to private chats."""
    await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def keyboard(rows):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"tc:{data}")
                for label, data in row
            ]
            for row in rows
        ]
    )


class Control:
    """Handle Stars receipts and managed-bot identity as separate operations."""

    def __init__(
        self, store: Store, cipher: Fernet, admin_ids: set[int], support: str = ""
    ):
        self.store = store
        self.cipher = cipher
        self.admin_ids = admin_ids
        self.support = support
        self.purchase_allowed = False
        self.trials_allowed = False
        self.defaults = None
        self.status_provider = None

    async def config_status(self, update, context):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        await update.message.reply_text(
            self.status_provider() if self.status_provider else "配置状态暂不可用。"
        )

    @staticmethod
    def _private(update: Update) -> bool:
        return bool(update.effective_chat and update.effective_chat.type == "private")

    async def home(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show real commands and prices, not an unconfigured checkout."""
        if not self._private(update) or not update.message:
            return
        prices = [
            f"{LABELS[key]}：{self.store.price(key)} Stars"
            if self.store.price(key)
            else f"{LABELS[key]}：暂未开放"
            for key in ("first", "month", "year")
        ]
        rows = [
            [("购买机器人", "plans"), ("我的机器人 / 续费", "bots")],
            [("创建已付款机器人", "create"), ("专属定制", "custom")],
            [("联系客服", "support"), ("支付支持", "paysupport")],
        ]
        if update.effective_user and update.effective_user.id in self.admin_ids:
            rows.append([("平台运维", "admin")])
        await update.message.reply_text(
            "机器人托管中心\n\n"
            + "\n".join(prices)
            + "\n\n每期主动付款，不自动扣款。专属定制单独报价。",
            reply_markup=keyboard(rows),
        )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Treat callback data as untrusted; business handlers recheck ownership."""
        query = update.callback_query
        if not query:
            return
        if (
            not self._private(update)
            or not query.message
            or not hasattr(query.message, "reply_text")
            or query.from_user.id != update.effective_chat.id
        ):
            await query.answer("请在与机器人的私聊中操作。", show_alert=True)
            return
        data = query.data or ""
        parts = data.removeprefix("tc:").split(":")
        action, args = parts[0], parts[1:]
        routes = {
            "home": "home",
            "plans": "plan_picker",
            "bots": "my_bots",
            "create": "create",
            "custom": "custom",
            "support": "support_command",
            "paysupport": "support_command",
            "buy": "buy",
            "renew": "renew",
            "dashboard": "dashboard",
            "manage": "dashboard",
            "confirm": "admin_confirm",
            "admin": "admin_menu",
            "ops": "admin_operations",
            "status": "admin_status",
            "configstatus": "config_status",
        }
        valid = data.startswith("tc:") and action in routes
        if action in {"buy"}:
            valid = valid and len(args) == 1 and args[0] in LABELS
        elif action == "renew":
            valid = (
                valid
                and len(args) == 2
                and args[0].isdigit()
                and args[1] in {"month", "year"}
            )
        elif action in {"dashboard", "manage"}:
            valid = valid and len(args) == 1 and args[0].isdigit()
        elif action == "confirm":
            valid = valid and len(args) == 1 and len(args[0]) == 32
        else:
            valid = valid and not args
        if not valid:
            await query.answer("按钮已失效，请重新打开主菜单。", show_alert=True)
            return
        await query.answer()
        # The button's message was sent by the bot, not by the customer.
        request = SimpleNamespace(
            message=query.message,
            effective_message=query.message,
            effective_chat=update.effective_chat,
            effective_user=query.from_user,
        )
        await getattr(self, routes[action])(
            request, SimpleNamespace(bot=context.bot, args=args)
        )

    async def plan_picker(self, update, context):
        if not self._private(update) or not update.message:
            return
        rows = [
            [(f"{LABELS[tier]} · {self.store.price(tier)} Stars", f"buy:{tier}")]
            for tier in LABELS
            if self.store.price(tier)
        ]
        available = bool(rows)
        rows.append([("返回主菜单", "home")])
        await update.message.reply_text(
            "请选择套餐，付款前请核对发票金额。"
            if available
            else "套餐暂未开放，请稍后再来。",
            reply_markup=keyboard(rows),
        )

    async def support_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self._private(update) and update.message:
            await update.message.reply_text(self.support or "请联系平台管理员。")

    async def _invoice(self, bot, chat_id: int, order) -> None:
        if not self.purchase_allowed:
            raise ValueError("Purchases are disabled")
        await bot.send_invoice(
            chat_id=chat_id,
            title=LABELS.get(order["tier"], "机器人专属定制"),
            description=order["description"] or "每期主动购买；新机器人在付款后创建。",
            payload=order["id"],
            currency="XTR",
            prices=[LabeledPrice("服务", order["stars"])],
        )

    async def buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or not update.message or not update.effective_user:
            return
        if not self.purchase_allowed or not self.store.worker_ready():
            await update.message.reply_text("开通服务暂未就绪，请稍后再试。")
            return
        tier = context.args[0].lower() if context.args else ""
        if tier not in {"first", "month", "year"}:
            await self.home(update, context)
            return
        try:
            order = self.store.create_order(update.effective_user.id, tier)
        except ValueError as error:
            await update.message.reply_text(str(error))
            return
        try:
            await self._invoice(context.bot, update.effective_user.id, order)
        except Exception:
            logger.exception("Invoice delivery failed for order %s", order["id"])
            await update.message.reply_text(
                "订单已保存，发票发送失败；请稍后联系管理员。"
            )

    async def renew(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or not update.message or not update.effective_user:
            return
        if not self.purchase_allowed or not self.store.worker_ready():
            await update.message.reply_text("续费服务暂未就绪，请稍后再试。")
            return
        if len(context.args) != 2 or context.args[1] not in {"month", "year"}:
            await update.message.reply_text("用法：/renew 机器人ID month|year")
            return
        try:
            order = self.store.create_order(
                update.effective_user.id, context.args[1], int(context.args[0])
            )
        except (ValueError, OverflowError) as error:
            await update.message.reply_text(str(error))
            return
        await self._invoice(context.bot, update.effective_user.id, order)

    async def checkout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        checkout = update.pre_checkout_query
        if not checkout:
            return
        valid = (
            self.purchase_allowed
            and self.store.worker_ready()
            and checkout.currency == "XTR"
            and self.store.precheckout(
                checkout.invoice_payload, checkout.from_user.id, checkout.total_amount
            )
        )
        await checkout.answer(
            ok=valid,
            error_message=None if valid else "订单已失效或金额不符，请重新购买。",
        )

    async def payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.successful_payment:
            return
        payment = update.message.successful_payment
        if payment.currency != "XTR":
            logger.error("Unexpected payment currency for %s", payment.invoice_payload)
            return
        try:
            order = self.store.settle(
                payment.invoice_payload,
                update.effective_user.id,
                payment.total_amount,
                payment.telegram_payment_charge_id,
            )
        except ValueError:
            logger.exception("Payment requires operator reconciliation")
            await update.message.reply_text("收款待人工核对；请勿重复支付。")
            return
        if order["bot_id"] is None:
            await self._creation_link(update, context, order["id"])
        elif order["tier"] == "custom":
            await update.message.reply_text("定制订单已付款，运营人员将安排验收。")
        else:
            await update.message.reply_text("续费成功，租户实例正在恢复。")

    async def _creation_link(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order_id: str,
        trial: bool = False,
    ):
        manager = await context.bot.get_me()
        suggested = f"Community{order_id[:10]}Bot"
        link = (
            f"https://t.me/newbot/{manager.username}/{suggested}"
            f"?name={quote('社区助手')}"
        )
        await update.effective_message.reply_text(
            (
                "测试资格已确认（未付款）。有效期从签发时起算，创建后不重新计时。"
                "请点击创建你自己的机器人；创建后会自动绑定并部署。"
                if trial
                else "付款已确认。请点击创建你自己的机器人；创建后会自动绑定并部署。"
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("创建机器人", url=link)]]
            ),
        )

    async def create(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or not update.message:
            return
        order = self.store.db.execute(
            "SELECT id FROM orders WHERE owner_id=? AND bot_id IS NULL "
            "AND status='paid' AND tier IN ('first','month','year') "
            "ORDER BY created_at LIMIT 1",
            (update.effective_user.id,),
        ).fetchone()
        if order:
            await self._creation_link(update, context, order["id"])
        else:
            trial = self.store.pending_trial(update.effective_user.id)
            if self.trials_allowed and trial:
                if not self.store.worker_ready():
                    await update.message.reply_text(
                        "测试资格已签发，部署环境尚未就绪，暂不能创建。"
                    )
                    return
                await self._creation_link(update, context, trial["id"], trial=True)
            else:
                await update.message.reply_text(
                    "暂无待创建的已付款机器人或有效测试资格。"
                )

    async def managed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        item = update.managed_bot
        if not item:
            return
        user_id, bot_id = item.user.id, item.bot.id
        existing = self.store.db.execute(
            "SELECT owner_id FROM bots WHERE id=?", (bot_id,)
        ).fetchone()
        pending = self.store.db.execute(
            "SELECT 1 FROM orders WHERE owner_id=? AND bot_id IS NULL "
            "AND status='paid' AND tier IN ('first','month','year') LIMIT 1",
            (user_id,),
        ).fetchone()
        trial_ready = bool(
            self.trials_allowed
            and self.store.worker_ready()
            and self.store.pending_trial(user_id)
        )
        if (existing and existing["owner_id"] != user_id) or (
            not existing and not pending and not trial_ready
        ):
            logger.warning("Unmatched managed-bot update for %s", bot_id)
            return
        try:
            token = await context.bot.get_managed_bot_token(bot_id)
            created, _ = self.store.attach_bot(
                user_id,
                bot_id,
                item.bot.username or "",
                self.cipher.encrypt(token.encode()),
                defaults=self.defaults,
                allow_trial=trial_ready,
            )
        except Exception:
            logger.exception("Managed-bot binding failed for bot %s", bot_id)
            return
        await context.bot.send_message(
            user_id,
            "机器人已绑定，正在部署。" if created else "机器人凭据已更新，正在同步。",
        )

    async def my_bots(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or not update.message:
            return
        rows = self.store.bots_for(update.effective_user.id)
        if not rows:
            await update.message.reply_text(
                "暂无机器人。",
                reply_markup=keyboard(
                    [[("查看套餐", "plans"), ("返回主菜单", "home")]]
                ),
            )
            return
        for row in rows:
            bot_id = row["id"]
            await update.message.reply_text(
                f"@{row['username']} · {bot_id}\n状态：{row['status']}\n到期："
                f"{datetime.fromtimestamp(row['expires_at'], timezone.utc):%Y-%m-%d %H:%M} UTC",
                reply_markup=keyboard(
                    [
                        [("Telegram 管理", f"manage:{bot_id}")],
                        [
                            ("续费一个月", f"renew:{bot_id}:month"),
                            ("续费一年", f"renew:{bot_id}:year"),
                        ],
                        [("返回主菜单", "home")],
                    ]
                ),
            )

    async def dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or not update.message:
            return
        try:
            bot_id = int(context.args[0])
        except (IndexError, ValueError):
            await self.my_bots(update, context)
            return
        row = self.store.db.execute(
            "SELECT id,username,status,expires_at FROM bots "
            "WHERE id=? AND owner_id=? AND status='active'",
            (bot_id, update.effective_user.id),
        ).fetchone()
        if not row:
            await update.message.reply_text("机器人尚未就绪或不属于你。")
            return
        await update.message.reply_text(
            f"@{row['username']}\n状态：{row['status']}\n"
            "进入机器人私聊的管理中心，配置群绑定和已批准功能。\n"
            "平台托管、续费及专属定制在本总控处理。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "打开机器人管理",
                            url=f"https://t.me/{row['username']}?start=manage",
                        )
                    ],
                    [InlineKeyboardButton("返回我的机器人", callback_data="tc:bots")],
                ]
            ),
        )

    async def custom(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or not update.message:
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "专属定制单独报价，验收后部署到你的机器人。\n"
                "提交需求：/custom 机器人ID 需求描述\n"
                "例如：/custom 123456 新增会员到期提醒",
                reply_markup=keyboard(
                    [
                        [("查看我的机器人", "bots"), ("联系客服", "support")],
                        [("返回主菜单", "home")],
                    ]
                ),
            )
            return
        try:
            ticket = self.store.request_custom(
                update.effective_user.id,
                int(context.args[0]),
                " ".join(context.args[1:]),
            )
        except (IndexError, ValueError) as error:
            await update.message.reply_text(f"用法：/custom 机器人ID 需求描述\n{error}")
            return
        await update.message.reply_text(f"定制需求 #{ticket} 已登记，待单独报价。")
        for admin in self.admin_ids:
            await context.bot.send_message(admin, f"新定制需求 #{ticket}，请查看后台。")

    async def admin_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        try:
            self.store.set_price(
                context.args[0], int(context.args[1]), update.effective_user.id
            )
        except (IndexError, ValueError) as error:
            await update.message.reply_text(
                f"用法：/adminprice first|month|year Stars\n{error}"
            )
            return
        await update.message.reply_text("Stars 价格已更新，新订单立即生效。")

    async def admin_quote(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        if not self.purchase_allowed:
            await update.message.reply_text("当前未开放购买或付费报价。")
            return
        try:
            order = self.store.create_quote(
                int(context.args[0]),
                int(context.args[1]),
                int(context.args[2]),
                " ".join(context.args[3:]),
                update.effective_user.id,
            )
        except (IndexError, ValueError) as error:
            await update.message.reply_text(
                f"用法：/adminquote 客户ID 机器人ID Stars 需求摘要\n{error}"
            )
            return
        await self._invoice(context.bot, order["owner_id"], order)
        await update.message.reply_text(f"报价已发送：{order['id']}")

    async def admin_refund(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        await self._confirm_request(update, context, "refund")

    async def _refund(self, update, context):
        try:
            order_id = context.args[0]
            order = self.store.refundable(order_id)
            await context.bot.refund_star_payment(order["owner_id"], order["charge_id"])
            self.store.refund(order_id, update.effective_user.id)
        except (IndexError, ValueError):
            await update.message.reply_text(
                "仅可自动退未交付的订单，其他情况人工核查。"
            )
            return
        except Exception:
            logger.exception("Stars refund needs reconciliation")
            await update.message.reply_text("退款状态待核查；请勿重复操作。")
            return
        await update.message.reply_text("退款成功并已记入审计。")
        return True

    async def admin_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        action = "resume" if update.message.text.startswith("/adminresume") else "pause"
        await self._confirm_request(update, context, action)

    async def admin_operation(self, update, context):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        command = update.message.text.split()[0].split("@")[0].lstrip("/")
        await self._confirm_request(
            update,
            context,
            {
                "adminbackup": "backup",
                "adminrestore": "restore",
                "adminupgrade": "upgrade",
            }[command],
        )

    async def _confirm_request(self, update, context, action):
        try:
            ref = self.store.propose_operation(
                update.effective_user.id, action, context.args
            )
        except ValueError as error:
            await update.message.reply_text(str(error))
            return
        await update.message.reply_text(
            f"确认操作：{action}\n目标：{' '.join(context.args)}\n"
            "确认有效期 5 分钟；恢复快照会覆盖当前租户数据。",
            reply_markup=keyboard(
                [
                    [("确认执行", f"confirm:{ref}")],
                    [("返回主菜单（不执行）", "home")],
                ]
            ),
        )

    async def admin_confirm(self, update, context):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        try:
            candidate = self.store.db.execute(
                "SELECT action FROM operations WHERE id=? AND actor=?",
                (context.args[0], update.effective_user.id),
            ).fetchone()
            if candidate and candidate["action"] == "trial" and not self.trials_allowed:
                await update.message.reply_text("测试开通已关闭，确认未执行。")
                return
            item = self.store.confirm_operation(
                context.args[0], update.effective_user.id
            )
        except (IndexError, ValueError):
            await update.message.reply_text("确认已失效、已使用或不属于你。")
            return
        if item["action"] == "trial":
            owner, days = json.loads(item["argument"])
            await update.message.reply_text(
                f"已为 {owner} 签发 {days} 天测试资格，未创建付款记录。"
                "请让该用户私聊发送 /create；部署环境就绪后才能创建。"
            )
            return
        if item["action"] == "refund":
            refunded = await self._refund(
                update, SimpleNamespace(bot=context.bot, args=[item["argument"]])
            )
            if refunded:
                self.store.db.execute(
                    "UPDATE operations SET state='done' WHERE id=?", (item["id"],)
                )
        await update.message.reply_text("操作已确认。使用 /adminops 查看任务状态。")

    async def admin_trial(self, update, context):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        if not self.trials_allowed:
            await update.message.reply_text("测试开通未启用。")
            return
        await self._confirm_request(update, context, "trial")

    async def admin_operations(self, update, context):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        rows = self.store.db.execute(
            "SELECT * FROM operations WHERE actor=? ORDER BY created_at DESC LIMIT 15",
            (update.effective_user.id,),
        ).fetchall()
        await update.message.reply_text(
            "\n".join(
                f"{r['action']} Bot {r['bot_id']} · {r['state']} · {r['result']}"
                for r in rows
            )
            or "暂无运维任务。"
        )

    async def admin_menu(self, update, context):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        await update.message.reply_text(
            "平台运维\n"
            "/adminpause BotID 暂停\n/adminresume BotID 恢复\n"
            "/adminbackup BotID 备份\n/adminrestore BotID 快照ID 恢复快照\n"
            "/adminupgrade BotID 升级社区插件（生成回滚快照）\n"
            "/adminrefund 订单ID 退款\n"
            "以上操作提交后均需再次确认；未运行 Worker 时任务会等待。\n"
            "/adminquote 客户ID BotID Stars 需求摘要\n"
            "/adminprice first|month|year Stars\n"
            "/admintrial 用户数字ID 天数（1–7，独立测试资格，需确认）",
            reply_markup=keyboard(
                [
                    [("实例概况", "status"), ("运维任务", "ops")],
                    [("生效配置", "configstatus")],
                    [("返回主菜单", "home")],
                ]
            ),
        )

    async def admin_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._private(update) or update.effective_user.id not in self.admin_ids:
            return
        counts = self.store.db.execute(
            "SELECT status,COUNT(*) AS count FROM bots GROUP BY status"
        ).fetchall()
        orders = self.store.db.execute(
            "SELECT COUNT(*) FROM orders WHERE status IN ('paid','provisioning')"
        ).fetchone()[0]
        requests = self.store.db.execute(
            "SELECT id,owner_id,bot_id,body FROM requests WHERE status='new' "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
        await update.message.reply_text(
            "实例："
            + ", ".join(f"{r['status']} {r['count']}" for r in counts)
            + f"\n待交付/待核查订单：{orders}\n待报价："
            + (
                "\n".join(
                    f"#{r['id']} 客户 {r['owner_id']} Bot {r['bot_id']}: {r['body'][:120]}"
                    for r in requests
                )
                or "无"
            )
        )

    async def reminders(self, context: ContextTypes.DEFAULT_TYPE):
        """Send once per threshold after successful delivery, then persist."""
        now = int(time.time())
        rows = self.store.db.execute(
            "SELECT id,owner_id,expires_at FROM bots WHERE "
            "(status='active' AND expires_at<=?) OR "
            "(status='suspended' AND expires_at>?)",
            (now + 7 * 86400, now - 30 * 86400),
        ).fetchall()
        for row in rows:
            remaining = row["expires_at"] - now
            kind = "expired" if remaining <= 0 else "1d" if remaining <= 86400 else "7d"
            if self.store.db.execute(
                "SELECT 1 FROM notices WHERE bot_id=? AND kind=?",
                (row["id"], kind),
            ).fetchone():
                continue
            try:
                await context.bot.send_message(
                    row["owner_id"],
                    f"机器人 {row['id']} "
                    + ("已到期暂停。" if remaining <= 0 else "即将到期：")
                    + f"{datetime.fromtimestamp(row['expires_at'], timezone.utc):%Y-%m-%d %H:%M} UTC。"
                    + "请使用 /renew 机器人ID month|year 续费。",
                )
                self.store.db.execute(
                    "INSERT OR IGNORE INTO notices(bot_id,kind,sent_at) VALUES(?,?,?)",
                    (row["id"], kind, now),
                )
            except Exception:
                logger.exception("Reminder failed for bot %s", row["id"])
