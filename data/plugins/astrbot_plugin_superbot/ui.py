"""Private Telegram menus and explicit, expiring human confirmations."""

import json
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button
from telegram import (
    InlineKeyboardMarkup,
    KeyboardButton,
    MessageEntity,
    ReplyKeyboardMarkup,
)
from telegram.error import BadRequest

from .grant_target import resolve as resolve_grant_target
from .moderation_ui import ModerationUI
from .modules import MODULES
from .payments import money
from .points import DEFAULT
from .rich_text import cards, send_html
from .rules import NAMES, PLAY_NAMES, STATUS_NAMES
from .store import Rejected
from .wizard import STEPS
from .wizard import run as run_wizard

AD_STATES = {
    "pending": "待处理",
    "active": "展示中",
    "expired": "已到期",
    "cancelled": "已取消",
    "rejected": "未通过",
    "renewed": "已续期",
    "review": "待核查",
    "sending": "发布中",
    "sent": "等待置顶",
    "pinning": "置顶中",
    "unpinning": "撤销置顶中",
    "editing": "文案更新中",
    "stopped": "已停止",
}
PAYMENT_STATES = {
    "wallet": "广告余额",
    "manual": "人工核款",
    "test": "测试免付款",
    "refunded": "已退回广告余额",
    "manual_pending": "等待人工退款",
    "manual_done": "人工退款已处理",
}

# The four glyphs spell 大海传媒 in the dhcm1 custom emoji pack.  Keep the
# literal fallback because custom emoji availability can vary by Bot API/account.
WELCOME_BRAND = "大海传媒"
WELCOME_BRAND_EMOJI_IDS = (
    "6167905815417070722",  # 大
    "6168055602401517542",  # 海
    "6167764446568522405",  # 传
    "6167986797025436814",  # 媒
)


class UI:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.moderation_ui = ModerationUI(self)

    def keyboard(self, uid):
        labels = [
            "我的群",
            "广告投递",
            "玩法大全",
            "群聊解禁",
            "签到积分",
            "客服",
            "帮助",
            "我的",
        ]
        if self.store.allowed(uid):
            labels.append("⚙️ 管理")
        return ReplyKeyboardMarkup(
            [
                [
                    KeyboardButton(label, api_kwargs={"style": "primary"})
                    for label in labels[i : i + 2]
                ]
                for i in range(0, len(labels), 2)
            ],
            resize_keyboard=True,
            is_persistent=False,
            one_time_keyboard=False,
            input_field_placeholder="认准群聊 @yuan 美元的元",
        )

    async def render(self, update, text, buttons=(), *, fold_sections=None):
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        if fold_sections is None:
            fold_sections = bool(
                getattr(update.effective_chat, "type", None) == "private"
                and self.store.allowed(uid)
            )
        for _, payload in buttons:
            if payload.get("action") in (
                "ad_decide",
                "ad_wallet_pay",
                "form",
                "save_form",
            ):
                key = payload.get("id") or payload.get("data", {}).get("id")
                if key:
                    row = self.store.db.execute(
                        "SELECT version FROM ads WHERE id=?", (key,)
                    ).fetchone()
                    if row:
                        payload.setdefault("version", row[0])
        buttons_flat = [
            Button(label, url=payload["url"])
            if "url" in payload
            else Button(label, callback_data=self.store.callback(uid, chat, payload))
            for label, payload in buttons
        ]
        rows = [buttons_flat[i : i + 2] for i in range(0, len(buttons_flat), 2)]
        markup = InlineKeyboardMarkup(rows) if rows else None
        pages = cards(text, fold_sections=fold_sections)
        for index, (_, formatted) in enumerate(pages):
            options = {"reply_markup": markup if index == len(pages) - 1 else None}
            if index == 0 and update.callback_query:
                try:
                    await send_html(
                        update.callback_query.edit_message_text, formatted, **options
                    )
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
            else:
                await send_html(
                    self.runtime.bot.send_message, formatted, chat_id=chat, **options
                )

    async def home(self, update):
        self.store.clear_dialog(str(update.effective_user.id))
        user = update.effective_user
        name = (
            getattr(user, "full_name", None)
            or getattr(user, "first_name", None)
            or getattr(user, "username", None)
            or "朋友"
        )
        name = " ".join(str(name).split())[:80]
        text = f"你好！我是{WELCOME_BRAND}超级机器人。\n欢迎 {name} 莅临！\n\n需要什么，点下面的菜单；有问题找「客服」。"
        markup = self.keyboard(str(update.effective_user.id))
        try:
            # MessageEntity offsets are UTF-16 units; each Chinese glyph is one
            # unit and the brand starts after the six-character prefix.
            entities = [
                MessageEntity(
                    type="custom_emoji",
                    offset=6 + index,
                    length=1,
                    custom_emoji_id=emoji_id,
                )
                for index, emoji_id in enumerate(WELCOME_BRAND_EMOJI_IDS)
            ]
            entities.insert(
                0,
                MessageEntity(
                    type="bold",
                    offset=0,
                    length=len(text.splitlines()[0].encode("utf-16-le")) // 2,
                ),
            )
            await self.runtime.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                entities=entities,
                reply_markup=markup,
            )
        except BadRequest as exc:
            if not any(
                term in str(exc).lower() for term in ("emoji", "entit", "parse")
            ):
                raise
            # Telegram may reject custom emoji entities for an account without
            # the required entitlement. The welcome message must still work.
            await self.runtime.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=markup,
            )
        from .onboarding import invitation

        text, buttons = invitation(self.runtime)
        await self.render(update, text, buttons, fold_sections=False)

    async def action(self, update, payload, token=""):
        """Render queries or execute confirmed deterministic actions.

        Args:
            update: Authenticated private Telegram update.
            payload: Server-stored button action.
            token: Unique callback identity used as a business operation key.
        """
        uid = str(update.effective_user.id)
        action = payload["action"]
        if action.startswith("support"):
            from .support_menu import action as support_action

            return await support_action(self, update, payload)
        if action.startswith("tenant_"):
            from .tenant_ui import action as tenant_action

            return await tenant_action(self, update, payload, token)
        if action.startswith("ad_") and payload.get("id"):
            existing = self.store.db.execute(
                "SELECT tenant FROM ads WHERE id=?", (payload["id"],)
            ).fetchone()
            if existing and existing["tenant"] != "platform":
                if action == "ad_view":
                    from .tenant_ui import action as tenant_action

                    return await tenant_action(
                        self,
                        update,
                        {"action": "tenant_order", "id": payload["id"]},
                        token,
                    )
                raise Rejected(
                    "此订单属于独立群主，请从我的直付广告处理，旧按钮不可使用。"
                )
        if (
            action.startswith("avatar_")
            and getattr(self.runtime, "tenants", None)
            and not self.runtime.tenants.resource_allowed(uid)
        ):
            raise Rejected("本经营主体未获付费制图授权，请联系平台。")
        if action.startswith("hours_"):
            from .game_hours import action as hours_action

            return await hours_action(self, update, payload, token)
        if action.startswith("avatar_") and getattr(self.runtime, "avatar", None):
            return await self.runtime.avatar.action(update, payload, token)
        if action in {
            "game_delivery",
            "game_notice_view",
            "game_notice_resolve",
            "game_countdown_stop_view",
            "game_countdown_stop",
        }:
            from .game_maintenance import action as maintenance_action

            return await maintenance_action(self, update, payload, token)
        if action in {
            "room",
            "numbers",
            "bet_input",
            "bet_pick",
            "bet_submit",
            "chase_input",
            "chase_submit",
        }:
            self.store.clear_dialog(uid)
            raise Rejected(
                "下注请在已启用的群里发送 jnd 激活，再发送 大1。私聊仅查询，不提交下注；新追号暂不开放。"
            )
        if action.startswith("cm_"):
            from . import community_ui

            return await community_ui.action(self, update, payload, token)
        self.store.db.execute(
            "INSERT INTO user_labels(uid,username) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET username=excluded.username",
            (uid, getattr(update.effective_user, "username", None) or ""),
        )
        modules = self.store.get("modules", {})
        if action.startswith("ap_") or action == "packages":
            from .ad_settings_ui import action as settings_action

            return await settings_action(
                self,
                update,
                {**payload, "action": "ap_list"} if action == "packages" else payload,
            )
        if action in {
            "ad_cancel_preview",
            "ad_cancel_confirm",
            "ad_capacities",
            "ad_capacity",
            "ad_capacity_preview",
            "ad_capacity_save",
            "ad_recovery",
            "ad_recover_view",
            "ad_recover_input",
            "ad_recover_confirm",
            "ad_recover_before",
            "ad_recover_rollback",
            "ad_chain_review",
        }:
            from .ad_maintenance_ui import action as maintenance_action

            self.store.clear_dialog(uid)
            return await maintenance_action(self, update, payload)
        if action in (
            "ads",
            "ad_new",
            "ad_package",
            "ad_submit",
            "ad_browse",
            "ad_renew",
        ) and not modules.get("ads"):
            raise Rejected("广告模块未启用")
        if action == "checkin" and not modules.get("points"):
            raise Rejected("积分获取未启用")
        back = [("返回首页", {"action": "home"})]
        if action.startswith("ad_") or action == "ads":
            self.store.clear_dialog(uid)
            parent = (
                "ad_new"
                if action == "ad_package"
                else "ad_orders"
                if action == "ad_view"
                else "ads"
            )
            back = [("返回上一级", {"action": parent})] if action != "ads" else []
        page = max(0, int(payload.get("page", 0)))
        if action in {"ad_orders", "bets", "chases", "draws"}:
            back = [
                ("上一页", {**payload, "page": max(0, page - 1)}),
                ("下一页", {**payload, "page": page + 1}),
            ] + back
        if action == "home":
            return await self.home(update)
        if action == "deposit":
            if not self.runtime.payments.config.get("usdt_enabled"):
                raise Rejected("充值暂未开放，请稍后再来")
            self.store.dialog(uid, {"form": "deposit"})
            return await self.render(
                update,
                "输入充值整数金额（1—10000 USDT）。\n下一步生成带小数尾数的精确金额，必须按订单金额到账。\nUSDT余额仅用于广告消费，不可用于真实投注，不可兑换模拟下注积分，不支持提现。",
                back,
            )
        if action == "deposit_create":
            order = self.runtime.payments.create(uid, payload["amount"], token)
            return await self.render(
                update,
                f"仅限 USDT-TRC20\n到账金额：{money(order['amount'])} USDT\n收款地址：{order['address']}\n30分钟内完成转账。请保留全部6位小数；手续费由转出方承担，实际到账金额必须完全一致。\n链上确认后自动计入广告余额。超时、金额不符请联系管理员，不要重复转账。",
                [
                    ("收款二维码", {"action": "deposit_qr", "id": order["id"]}),
                    ("查看充值记录", {"action": "deposits"}),
                ],
            )
        if action == "deposit_qr":
            import qrcode

            order = self.store.db.execute(
                "SELECT * FROM deposits WHERE id=? AND uid=?", (payload["id"], uid)
            ).fetchone()
            if (
                not order
                or order["status"] != "pending"
                or order["expires"] <= self.store.clock()
            ):
                raise Rejected("充值单已失效或已到账，请查看充值记录，不要继续付款")
            photo = BytesIO()
            qrcode.make(order["address"]).save(photo, format="PNG")
            photo.seek(0)
            return await self.runtime.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photo,
                caption=f"USDT · TRC20 收款地址二维码\n{order['address']}\n实际到账必须为 {money(order['amount'])} USDT（保留全部6位小数）。\n二维码只填地址，不含金额、币种或网络；请在交易所选择 USDT / TRC20，核对手续费扣除后的到账金额。\n以原充值单有效期为准，过期勿付。余额仅用于广告消费。",
            )
        if action == "deposits":
            rows = self.store.db.execute(
                "SELECT id,amount,status,expires FROM deposits WHERE uid=? ORDER BY created DESC LIMIT 10",
                (uid,),
            ).fetchall()
            text = (
                "\n".join(
                    f"{money(r['amount'])} USDT · {'已到账' if r['status'] == 'credited' else ('已超时，转账需核查' if r['expires'] < self.store.clock() else '等待到账')}"
                    for r in rows
                )
                or "暂无充值记录"
            )
            return await self.render(
                update,
                text,
                [("刷新", {"action": "deposits"}), ("我的", {"action": "account"})],
            )
        if action == "ad_wallet_preview":
            row = self.store.db.execute(
                "SELECT package FROM ads WHERE id=? AND uid=? AND status='pending' AND paid=0",
                (payload["id"], uid),
            ).fetchone()
            if not row:
                raise Rejected("订单不可支付")
            package = json.loads(row[0])
            if package["currency"].upper() != "USDT":
                raise Rejected("该广告位不是 USDT 计价")
            amount = int(Decimal(package["price"]) * 1000000)
            return await self.render(
                update,
                f"从广告余额扣除 {money(amount)} USDT？\n付款后仍需审核；满位或篇幅不足会按就绪时间排队，展示期限从成功发布起算。未发布可自行取消退回广告余额。",
                [
                    (
                        "确认付款",
                        {
                            "action": "ad_wallet_pay",
                            "id": payload["id"],
                            "amount": amount,
                        },
                    ),
                    ("取消", {"action": "account"}),
                ],
            )
        if action == "ad_wallet_pay":
            if "version" not in payload:
                raise Rejected("请重新打开付款预览")
            self.runtime.payments.pay(
                uid, payload["id"], payload["amount"], payload["version"]
            )
            return await self.render(
                update,
                "付款已记录，等待广告内容审核。",
                [("查看订单", {"action": "ad_view", "id": payload["id"]})],
            )
        if action == "unmute_entry":
            if not self.store.allowed(uid, "moderation"):
                return await self.render(
                    update,
                    "需要解禁？请联系所在群的管理员，说明群名和情况。这里不能自行解除禁言。",
                    back,
                )
            rows = self.store.db.execute(
                "SELECT chat,title FROM platform_mod_groups WHERE enabled=1 ORDER BY chat"
            ).fetchall()
            buttons = [
                (
                    r["title"],
                    {"action": "mod_form", "kind": "unmute", "chat": r["chat"]},
                )
                for r in rows
                if self.store.allowed(uid, "manager")
                or self.store.db.execute(
                    "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?", (uid, r["chat"])
                ).fetchone()
            ]
            return await self.render(
                update,
                "选群，再填写需要解禁的成员 UID。"
                if buttons
                else "还没有你能管理的已启用群。",
                buttons + back,
            )
        if action == "help":
            from .onboarding import invitation

            invitation_text, invitation_buttons = invitation(self.runtime)
            buttons = [("我的订单与积分", {"action": "account"})]
            if modules.get("moderation") and hasattr(self.runtime, "community"):
                buttons.append(("群规与说明", {"action": "cm_public_groups"}))
            if modules.get("points"):
                buttons.append(("领取积分", {"action": "points"}))
            return await self.render(
                update,
                (Path(__file__).parent / "docs/player-help.md").read_text()
                + "\n\n"
                + invitation_text,
                buttons + invitation_buttons + back,
                fold_sections=True,
            )
        if action == "grant_accept":
            self.store.accept_grant(uid, payload["token"])
            return await self.home(update)
        if action in {"points", "points_history", "checkin"} and self.store.get(
            "group_points_enabled", False
        ):
            if action == "checkin":
                return await self.render(
                    update,
                    "积分按群独立，请到需要领取积分的群发送 /checkin 签到。",
                    back,
                )
            group = payload.get("points_chat")
            if not group:
                rows = self.store.db.execute(
                    "SELECT g.chat,g.title FROM mod_groups g WHERE EXISTS "
                    "(SELECT 1 FROM group_wallets w WHERE w.chat=g.chat AND w.uid=?) "
                    "OR EXISTS (SELECT 1 FROM mod_members m WHERE m.chat=g.chat AND m.uid=?)",
                    (uid, uid),
                ).fetchall()
                return await self.render(
                    update,
                    "积分按群独立，请选择要查询的群；签到请到对应群发送 /checkin。",
                    [
                        (
                            r["title"],
                            {"action": "points_history", "points_chat": r["chat"]},
                        )
                        for r in rows
                    ]
                    + back,
                )
            rows = self.store.db.execute(
                "SELECT delta,reason FROM group_ledger WHERE chat=? AND uid=? ORDER BY at DESC LIMIT 20",
                (str(group), uid),
            ).fetchall()
            title = self.store.db.execute(
                "SELECT title FROM mod_groups WHERE chat=?", (str(group),)
            ).fetchone()
            return await self.render(
                update,
                f"{title[0] if title else group}\n本群积分：{self.store.balance(uid, group)}\n"
                + ("\n".join(f"{r[0]:+d} {r[1]}" for r in rows) or "还没有积分记录。"),
                [("返回选群", {"action": "points"})] + back,
            )
        if action == "points":
            cfg = self.store.get("points", DEFAULT)
            return await self.render(
                update,
                f"积分获取\n余额：{self.store.balance(uid)}\n签到：{cfg['checkin']}\n聊天：每次 {cfg['chat']}，间隔 {cfg['interval']} 秒，每日最多 {cfg['cap']}。\n仅已配置群的有效发言计分。",
                ([("每日签到", {"action": "checkin"})] if modules.get("points") else [])
                + [
                    ("积分记录", {"action": "points_history"}),
                    ("刷新余额", {"action": "points"}),
                ]
                + back,
            )
        if action == "points_history":
            rows = self.store.db.execute(
                "SELECT delta,reason FROM ledger WHERE uid=? ORDER BY at DESC LIMIT 20",
                (uid,),
            ).fetchall()
            return await self.render(
                update,
                f"积分余额：{self.store.balance(uid)}\n"
                + ("\n".join(f"{r[0]:+d} {r[1]}" for r in rows) or "还没有积分记录。"),
                [("返回积分", {"action": "points"})],
            )
        if action == "checkin":
            changed = self.runtime.points.checkin(uid)
            return await self.render(
                update,
                ("签到成功" if changed else "今天已签到")
                + f"，余额 {self.store.balance(uid)}",
                back,
            )
        if action == "account":
            if self.store.get("group_points_enabled", False):
                rows = self.store.db.execute(
                    "SELECT COALESCE(g.title,w.chat),w.balance FROM group_wallets w "
                    "LEFT JOIN mod_groups g ON g.chat=w.chat WHERE w.uid=? ORDER BY w.chat",
                    (uid,),
                ).fetchall()
                text = f"我的\nUID：{uid}\n积分按群独立，不跨群通用：\n" + (
                    "\n".join(f"{r[0]}：{r[1]}积分" for r in rows) or "暂无群积分。"
                )
            else:
                rows = self.store.db.execute(
                    "SELECT delta,reason FROM ledger WHERE uid=? ORDER BY at DESC LIMIT 12",
                    (uid,),
                ).fetchall()
                text = (
                    f"我的\nUID：{uid}\n积分：{self.store.balance(uid)}\n近期积分记录：\n"
                    + "\n".join(f"{r[0]:+d} {r[1]}" for r in rows)
                )
            if getattr(self.runtime, "payments", None):
                text = (
                    f"广告余额：{money(self.runtime.payments.balance(uid))} USDT\n仅用于广告消费，不可用于真实投注，不可兑换模拟下注积分。\n"
                    + text
                )
            return await self.render(
                update,
                text,
                [
                    ("充值 USDT", {"action": "deposit"}),
                    ("充值记录", {"action": "deposits"}),
                    ("我的广告订单", {"action": "ad_orders"}),
                    ("我的下注", {"action": "bets"}),
                    ("分群积分记录", {"action": "points"}),
                    ("我的追号", {"action": "chases"}),
                ]
                + (
                    [("专属头像", {"action": "avatar_home"})]
                    if getattr(self.runtime, "avatar", None)
                    and self.runtime.avatar.enabled()
                    else []
                )
                + (
                    [
                        ("我的举报", {"action": "cm_mine"}),
                        ("我的警告", {"action": "cm_my_warnings"}),
                    ]
                    if hasattr(self.runtime, "community")
                    else []
                )
                + back,
            )
        if action == "ads":
            return await self.render(
                update,
                "发布广告：选择广告位、提交内容，付款和审核通过后安排发布。",
                [
                    ("群主广告位（直付）", {"action": "tenant_catalog"}),
                    ("我的直付广告", {"action": "tenant_my_ads"}),
                    ("发布广告", {"action": "ad_new"}),
                    ("我的广告", {"action": "ad_orders"}),
                ]
                + back,
            )
        if action == "ad_new":
            packages = self.store.get("packages", {})
            from .ad_state import available

            visible = {}
            for key, p in packages.items():
                try:
                    available(self.store, p, key)
                except Rejected:
                    continue
                visible[key] = p
            packages = visible
            buttons = [
                (
                    f"{p['name']} · {p['price']} {p['currency']}",
                    {"action": "ad_package", "kind": "广告", "key": key},
                )
                for key, p in packages.items()
                if p["enabled"]
            ]
            return await self.render(
                update,
                "请选择广告位。普通按次发布；置顶期限以广告位说明为准。"
                if buttons
                else "管理员尚未开放广告位。",
                buttons + back,
            )
        if action == "ad_package":
            package = self.store.get("packages", {}).get(payload["key"])
            if not package or not package["enabled"]:
                raise Rejected("广告位已停用")
            from .ad_state import available

            available(self.store, package, payload["key"])
            self.store.dialog(
                uid,
                {
                    "form": "ad",
                    "kind": payload.get("kind", "广告"),
                    "key": payload["key"],
                    "package": package,
                },
            )
            return await self.render(
                update,
                f"{package['name']}：{package['price']} {package['currency']}\n目标：{package['target']}\n期限：{package['duration']}\n请发送两部分：第一行联系方式，其余行为广告正文。\n/cancel 取消。",
                back,
            )
        if action == "ad_submit":
            self.runtime.ads.submit(
                uid,
                payload["kind"],
                payload["body"],
                payload["contact"],
                payload["key"],
                payload["package"],
                token,
            )
            return await self.render(
                update,
                f"订单已提交\n收款说明：{payload['package']['payment']}\n付款后在订单中提交凭证；等待管理员核款和内容审核。",
                [("查看订单", {"action": "ad_view", "id": token})] + back,
            )
        if action == "ad_orders":
            rows = self.store.db.execute(
                "SELECT a.id,a.kind,a.status,COALESCE(g.title,c.title,json_extract(a.package,'$.target')) AS group_name,json_extract(a.package,'$.kind') AS placement FROM ads a LEFT JOIN mod_groups g ON g.chat=json_extract(a.package,'$.target') LEFT JOIN ad_channels c ON c.chat=json_extract(a.package,'$.target') WHERE a.uid=? ORDER BY a.at DESC LIMIT 20 OFFSET ?",
                (uid, page * 20),
            ).fetchall()
            return await self.render(
                update,
                f"我的广告 · 第{page + 1}页",
                [
                    (
                        f"{'置顶' if r['placement'] == 'pinned' else '普通'} · {r['group_name']} · {AD_STATES.get(r['status'], '处理中')}",
                        {"action": "ad_view", "id": r["id"]},
                    )
                    for r in rows
                ]
                + back,
            )
        if action == "ad_view":
            row = self.store.db.execute(
                "SELECT * FROM ads WHERE id=? AND uid=?", (payload["id"], uid)
            ).fetchone()
            if not row:
                raise Rejected("订单不存在")
            p = json.loads(row["package"])
            group = self.store.db.execute(
                "SELECT title FROM mod_groups WHERE chat=? UNION ALL SELECT title FROM ad_channels WHERE chat=?",
                (str(p["target"]), str(p["target"])),
            ).fetchone()
            group_name = group[0] if group else p["target"]
            buttons = (
                [("提交付款凭证", {"action": "proof", "id": row["id"]})]
                if row["status"] == "pending"
                else []
            )
            if row["status"] == "active":
                buttons += [("续期置顶", {"action": "ad_renew", "id": row["id"]})]
            if row["status"] == "pending":
                buttons.append(
                    ("取消订单／退款", {"action": "ad_cancel_preview", "id": row["id"]})
                )
            if (
                row["status"] == "pending"
                and not row["paid"]
                and p["currency"].upper() == "USDT"
            ):
                buttons.insert(
                    0,
                    ("广告余额付款", {"action": "ad_wallet_preview", "id": row["id"]}),
                )
            return await self.render(
                update,
                f"投放群／频道：{group_name}\n状态：{AD_STATES.get(row['status'], '处理中')}\n审核：{'已通过' if row['approved'] else '待审核'}，核款：{'已付款' if row['paid'] else '未付款'}\n{row['body']}\n价格：{p['price']} {p['currency']}\n退款：{PAYMENT_STATES.get(row['refund_state'], '无' if not row['refund_state'] else '待核查')}\n付款来源：{PAYMENT_STATES.get(row['payment_source'], '未付款')}\n到期（北京时间）：{datetime.fromtimestamp(row['expires'], ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M') if row['expires'] else '尚未开始'}\n处理说明：{row['error'] or '无'}",
                buttons + back,
            )
        if action == "ad_renew":
            old = self.store.db.execute(
                "SELECT * FROM ads WHERE id=? AND uid=? AND status='active'",
                (payload["id"], uid),
            ).fetchone()
            if not old:
                raise Rejected("没有可续期的有效广告")
            target = json.loads(old["package"])["target"]
            packages = self.store.get("packages", {})
            buttons = [
                (
                    f"确认续期：{p['name']} {p['price']} {p['currency']} / {p['duration']}",
                    {
                        "action": "ad_renew_submit",
                        "id": old["id"],
                        "key": key,
                        "package": p,
                    },
                )
                for key, p in packages.items()
                if p["enabled"] and p["kind"] == "pinned" and p["target"] == target
            ]
            return await self.render(
                update,
                "续期创建独立订单，审核和核款通过后，从原到期时间延长；不重复发布原广告。",
                buttons + back,
            )
        if action == "ad_renew_submit":
            self.runtime.ads.renew(
                uid, payload["id"], payload["key"], payload["package"], token
            )
            return await self.render(
                update,
                f"续期订单已提交：{token}",
                [("查看付款信息", {"action": "ad_view", "id": token})] + back,
            )
        if action == "proof":
            row = self.store.db.execute(
                "SELECT 1 FROM ads WHERE id=? AND uid=? AND status='pending'",
                (payload["id"], uid),
            ).fetchone()
            if not row:
                raise Rejected("订单不可提交凭证")
            self.store.dialog(uid, {"form": "proof", "id": payload["id"]})
            return await self.render(
                update,
                "请发送付款时间、金额和交易流水号，或上传凭证图片。机器人不会仅凭图片自动确认到账。",
                back,
            )
        if action == "ad_browse":
            page = max(0, int(payload.get("page", 0)))
            rows = self.store.db.execute(
                "SELECT id,body,contact FROM ads WHERE kind=? AND (status='published' OR (status='active' AND expires>?)) ORDER BY at DESC LIMIT 5 OFFSET ?",
                (payload["kind"], self.store.clock(), page * 5),
            ).fetchall()
            text = (
                "\n\n".join(f"{r['body'][:400]}\n{r['contact']}" for r in rows)
                or "暂无广告"
            )
            return await self.render(
                update,
                text,
                [
                    (
                        "上一页",
                        {
                            "action": "ad_browse",
                            "kind": payload["kind"],
                            "page": max(0, page - 1),
                        },
                    ),
                    (
                        "下一页",
                        {
                            "action": "ad_browse",
                            "kind": payload["kind"],
                            "page": page + 1,
                        },
                    ),
                ]
                + back,
            )
        if action == "game":
            return await self.render(
                update,
                "🎮 玩法大全\n所有玩法仅在已启用的群内参与，私聊不能下注、开桌或抽奖。\n\n"
                "🚪 加拿大28／积分快三\n在群内点击对应玩法，或发送 jnd／k3 进入30分钟房间。"
                "两房间互斥，发送“取消／退出”离开；下注不续期。\n\n"
                "🤝 双人对赌\n在群内回复对方发送 dd 自定义彩头。\n\n"
                "🎡 积分转盘\n在群内发送「转盘」，支持单抽、5连抽和10连抽，返还含本金。\n\n"
                "🎰老虎机PvP\n在群内发送「老虎机」，发起者选档，同桌同额。"
                "1—10人、50秒开奖；多人赢家获总池90%，抽水10%；单人按牌型倍率返还。\n\n"
                "💣 扫雷接龙\n在群内发送「扫雷」，每人独立选档。"
                "3—10人、50秒报名；轮流拆雷，最后幸存者获总池90%，抽水10%。\n\n"
                "📌 使用方式\n请到所在群发送「玩法大全」。"
                "各玩法须同时满足总开关及本群设置，积分按群独立。",
                [("帮助", {"action": "help"})] + back,
                fold_sections=True,
            )
        if action == "rules":
            text = (Path(__file__).parent / "docs" / "player-help.md").read_text()
            return await self.render(update, text, back, fold_sections=True)
        if action == "bets":
            rows = self.store.db.execute(
                "SELECT issue,play,amount,status,payout,points_chat FROM bets WHERE uid=? ORDER BY at DESC LIMIT 20 OFFSET ?",
                (uid, page * 20),
            ).fetchall()
            return await self.render(
                update,
                f"下注记录 · 第{page + 1}页\n"
                + "\n".join(
                    f"{r[0]} {PLAY_NAMES.get(r[1], '其他玩法')} 投入{r[2]} {STATUS_NAMES.get(r[3], '待核查')} 返还{r[4]}"
                    + (f" · 积分归属群{r[5]}" if r[5] else "")
                    for r in rows
                ),
                back,
            )
        if action == "chases":
            rows = self.store.db.execute(
                "SELECT * FROM chases WHERE uid=? ORDER BY rowid DESC LIMIT 20 OFFSET ?",
                (uid, page * 20),
            ).fetchall()
            return await self.render(
                update,
                "追号记录\n"
                + "\n".join(
                    f"第{page * 20 + i + 1}项 · { {'active': '进行中', 'paused': '已暂停', 'cancelled': '已取消', 'complete': '已完成', 'done': '已完成'}.get(r['status'], '待核查') } · 已完成{r['step']}/{r['periods']}期"
                    for i, r in enumerate(rows)
                ),
                [
                    (
                        f"取消第{page * 20 + i + 1}项",
                        {"action": "chase_cancel_preview", "id": r["id"]},
                    )
                    for i, r in enumerate(rows)
                    if r["status"] in ("active", "paused")
                ]
                + back,
            )
        if action == "chase_cancel_preview":
            return await self.render(
                update,
                "停止未来追号？已提交的下注不撤销。",
                [("确认取消", {"action": "chase_cancel", "id": payload["id"]})] + back,
            )
        if action == "chase_cancel":
            self.runtime.game.cancel_chase(uid, payload["id"])
            return await self.render(
                update, "未来追号已停止，已提交下注继续结算。", back
            )
        if action == "draws":
            rows = self.store.db.execute(
                "SELECT issue,balls,conflict FROM draws ORDER BY issue DESC LIMIT 20 OFFSET ?",
                (page * 20,),
            ).fetchall()
            return await self.render(
                update,
                f"开奖记录 · 第{page + 1}页\n"
                + "\n".join(
                    f"{r[0]} {r[1]}" + (" 待核查" if r[2] else "") for r in rows
                ),
                back,
            )
        if action == "rank":
            rows = self.runtime.game.leaderboard(payload["mode"])
            return await self.render(
                update,
                ("周榜" if payload["mode"] == "week" else "月榜")
                + " · 已结算净收益（北京时间）\n"
                + "\n".join(
                    f"{i + 1}. 用户…{r['uid'][-4:]}：{r['profit']}"
                    for i, r in enumerate(rows)
                ),
                back,
            )
        await self.admin(update, payload, token)

    async def admin(self, update, payload, token):
        uid = str(update.effective_user.id)
        action = payload["action"]
        key = payload.get("id") or payload.get("data", {}).get("id")
        if key and action.startswith("ad_"):
            order = self.store.db.execute(
                "SELECT tenant FROM ads WHERE id=?", (key,)
            ).fetchone()
            if order and order["tenant"] != "platform":
                raise Rejected("独立经营者的订单请从我的群或我的直付广告处理")
        self.store.require(uid, chat=payload.get("chat"))
        self.store.clear_dialog(uid)
        if action.startswith("games_group_"):
            from .game_groups_admin import action as group_games_action

            return await group_games_action(self, update, payload)
        if action.startswith("mines_"):
            from .mines_admin import action as mines_action

            return await mines_action(self, update, payload)
        if action.startswith("k3_"):
            from .k3_admin import action as k3_action

            return await k3_action(self, update, payload)
        if action.startswith("slots_"):
            from .slots_admin import action as slots_action

            return await slots_action(self, update, payload)
        if action.startswith("wheel_"):
            from .wheel_admin import action as wheel_action

            return await wheel_action(self, update, payload)
        if action in {"channels", "channel_add", "channel_preview", "channel_save"}:
            from .channels import action as channel_action

            return await channel_action(self, update, payload)
        if action == "wizard":
            return await run_wizard(
                self, update, payload["form"], payload["values"], payload.get("page", 0)
            )
        if action.startswith("mod_"):
            return await self.moderation_ui.action(update, payload, token)
        back = [("返回管理", {"action": "admin"})]
        if action in {"room_manage", "room_edit", "room_preview", "room_save"}:
            self.store.require(uid, "game")
            self.store.clear_dialog(uid)
            room_id = payload["room"]
            room = self.runtime.game.rooms().get(room_id)
            if room is None:
                raise Rejected("倍率档位不存在")
            back = [("返回倍率设置", {"action": "room_manage", "room": room_id})]
            labels = {"minimum": "最小下注", "maximum": "单笔上限", "total": "单期上限"}
            if action == "room_save":
                values = payload["values"]
                self.runtime.game.configure(
                    uid, room_id, **values, expected=payload["expected"]
                )
                return await self.render(update, "倍率设置已保存。", back)
            if action == "room_manage":
                return await self.render(
                    update,
                    f"{NAMES[room_id]} · {'开放' if room['enabled'] else '关闭'}\n"
                    + "\n".join(
                        f"{label}：{room[key]:,} 积分" for key, label in labels.items()
                    )
                    + "\n各玩法独立限额仍生效；开放倍率不会自动启用模拟28总开关。",
                    [
                        (
                            "关闭该倍率" if room["enabled"] else "开放该倍率",
                            {
                                "action": "room_preview",
                                "room": room_id,
                                "expected": room,
                            },
                        )
                    ]
                    + [
                        (label, {"action": "room_edit", "room": room_id, "field": key})
                        for key, label in labels.items()
                    ]
                    + [("返回倍率列表", {"action": "admin_game"})],
                )
            if action == "room_edit":
                field = payload["field"]
                self.store.dialog(
                    uid,
                    {
                        "form": "room_limit",
                        "room": room_id,
                        "field": field,
                        "expected": room,
                    },
                )
                return await self.render(
                    update,
                    f"{NAMES[room_id]} · {labels[field]}当前为 {room[field]:,} 积分。\n只需发送新的整数；最小下注 ≤ 单笔上限 ≤ 单期上限 ≤ 1,000,000。",
                    back,
                )
            if room != payload["expected"]:
                raise Rejected("倍率设置已变更，请重新打开后操作")
            values = {
                key: room[key] for key in ("enabled", "minimum", "maximum", "total")
            }
            values["enabled"] = not room["enabled"]
            return await self.render(
                update,
                f"确认{'开放' if values['enabled'] else '关闭'} {NAMES[room_id]}？",
                [
                    (
                        "确认",
                        {
                            "action": "room_save",
                            "room": room_id,
                            "expected": room,
                            "values": values,
                        },
                    )
                ]
                + back,
            )
        if action == "admin":
            choices = []
            for scope, label in [
                ("ads", "广告管理"),
                ("points", "积分管理"),
                ("game", "玩法管理"),
            ]:
                if self.store.allowed(uid, scope):
                    choices.append((label, {"action": "admin_" + scope}))
            if self.store.allowed(uid, "manager"):
                choices.append(("全部功能启停", {"action": "modules"}))
                if getattr(self.runtime, "avatar", None):
                    choices.append(("专属头像管理", {"action": "avatar_admin"}))
            if uid == self.store.owner:
                choices += [
                    ("生成授权链接", {"action": "form", "form": "grant"}),
                    ("撤销权限与链接", {"action": "form", "form": "revoke"}),
                ]
            if self.store.allowed(uid, "moderation"):
                choices.append(("群管理", {"action": "mod_home"}))
            return await self.render(
                update,
                "⚙️ 管理中心\n仅显示已授权功能；选择下方业务。\n\n"
                "📋 管理范围\n广告：广告位、审核、核款。\n积分：奖励与调整。\n"
                "玩法：加拿大28、对赌、转盘、老虎机及扫雷接龙。\n全部功能启停：各功能总开关。\n"
                "授权与撤权仅首位超管可操作。",
                choices + [("返回首页", {"action": "home"})],
            )
        if action == "modules":
            self.store.require(uid, "manager")
            enabled = self.store.get("modules", {})
            switches = [
                (m.key, m.label, bool(enabled.get(m.key, False)))
                for m in MODULES
                if m.key != "payments"
            ] + [
                ("wheel", "🎡 积分转盘", bool(enabled.get("wheel", False))),
                ("slots", "🎰老虎机PvP", bool(enabled.get("slots", False))),
                ("mines", "💣 扫雷接龙", bool(enabled.get("mines", False))),
                ("k3", "🎲 积分快三", bool(enabled.get("k3", False))),
                ("duel", "🤝 双人对赌", bool(enabled.get("duel", True))),
                (
                    "fingerprint",
                    "🛡 样本与行为识别",
                    bool(enabled.get("fingerprint", False)),
                ),
            ]
            avatar = getattr(self.runtime, "avatar", None)
            return await self.render(
                update,
                "🔌 全部功能启停\n统一管理各功能总开关，影响所有群。\n\n📋 当前状态\n"
                + "\n".join(
                    f"{label} · {'🟢 已开启' if state else '⚪ 已关闭'}"
                    for _, label, state in switches
                )
                + (
                    f"\n🎨 专属头像 · {'🟢 已开启' if avatar.enabled() else '⚪ 已关闭'}"
                    if avatar
                    else ""
                )
                + "\n\n📖 功能说明\n"
                "当前状态仅代表总开关；管理授权不会自动开放逐群玩法。\n"
                "广告发布：广告订单与投放；广告位、审核和核款在广告管理设置。\n"
                "玩法中心：加拿大28、积分快三、对赌、转盘、老虎机及扫雷的上层开关；各玩法逐群设置在玩法管理。\n"
                "积分获取：签到与聊天奖励；不代表清空余额，也不控制开奖结算。\n"
                "群管理：群内管理业务；各群策略及机器人权限仍须分别配置。\n"
                "积分快三：原生骰子开奖；逐群开关、期次和异常退款在玩法管理。\n"
                "双人对赌：邀请、接受与选择玩法；不扣积分、不担保彩头。\n"
                "专属头像：新头像制作；额度、任务和投递在专属头像管理查看。\n"
                "\n📌 启停边界\n关闭功能停止新业务；已受理下注与已锁定对赌继续结算，广告到期清理继续。\n"
                "对赌同时受玩法中心总开关控制；逐群设置请进入「玩法管理」。\n"
                "关闭玩法中心会暂停未来追号；关闭群管理会关闭定时禁言及广告杀手策略，重新开启总开关不会自动恢复这些策略。\n"
                "广告充值由独立后台配置，不在此直接变更。",
                [
                    (
                        f"设置 {label}",
                        {
                            "action": "module_preview",
                            "key": key,
                            "enabled": not state,
                        },
                    )
                    for key, label, state in switches
                ]
                + (
                    [("设置 🎨 专属头像", {"action": "avatar_toggle_preview"})]
                    if avatar
                    else []
                )
                + back,
            )
        if action == "module_preview":
            self.store.require(uid, "manager")
            names = {m.key: m.label for m in MODULES if m.key != "payments"}
            names["duel"] = "🤝 双人对赌"
            names["fingerprint"] = "🛡 样本与行为识别"
            names["wheel"] = "🎡 积分转盘"
            names["slots"] = "🎰老虎机PvP"
            names["mines"] = "💣 扫雷接龙"
            names["k3"] = "🎲 积分快三"
            if payload["key"] not in names or type(payload["enabled"]) is not bool:
                raise Rejected("无效功能开关")
            return await self.render(
                update,
                "🔌 总开关确认\n\n📋 本次调整\n"
                f"功能：{names[payload['key']]}\n"
                f"调整为：{'🟢 已开启' if payload['enabled'] else '⚪ 已关闭'}\n"
                "范围：所有群\n\n📌 说明\n仅调整总开关，不修改逐群设置；已锁定业务继续结算。",
                [
                    (
                        "确认",
                        {
                            "action": "module_save",
                            "key": payload["key"],
                            "enabled": payload["enabled"],
                            "previous": self.store.get("modules", {}).get(
                                payload["key"], payload["key"] == "duel"
                            ),
                        },
                    )
                ]
                + back,
            )
        if action == "module_save":
            with self.store.tx() as db:
                self.store.require(uid, "manager", db=db)
                enabled = self.store.get("modules", {}, db)
                if (
                    payload["key"]
                    not in (
                        {m.key for m in MODULES if m.key != "payments"}
                        | {"duel", "fingerprint", "wheel", "slots", "mines", "k3"}
                    )
                    or type(payload["enabled"]) is not bool
                ):
                    raise Rejected("无效功能开关")
                if enabled.get(payload["key"], payload["key"] == "duel") != payload.get(
                    "previous"
                ):
                    raise Rejected("功能状态已变化，请重新打开设置")
                enabled[payload["key"]] = payload["enabled"]
                self.store.put(db, "modules", enabled)
                self.store.audit(db, uid, "module_config", enabled)
                if payload["key"] == "game" and not payload["enabled"]:
                    db.execute(
                        "UPDATE chases SET status='paused',error='模块停用' WHERE status='active'"
                    )
                if payload["key"] == "moderation" and not payload["enabled"]:
                    db.execute(
                        "UPDATE mod_schedules SET enabled=0,version=version+1,error='module_disabled' WHERE enabled=1"
                    )
                    db.execute("UPDATE ak_policies SET enabled=0 WHERE enabled=1")
            return await self.admin(update, {"action": "modules"}, token)
        if action.startswith("admin_"):
            scope = action[6:]
            self.store.require(uid, scope)
            if scope == "points":
                config = self.store.get("points", DEFAULT)
                return await self.render(
                    update,
                    f"🎁 积分管理\n奖励：{'开启' if config['enabled'] else '关闭'}\n\n"
                    f"📊 奖励数据\n签到：{config['checkin']} 积分\n聊天：每次 {config['chat']} 积分\n"
                    f"间隔：{config['interval']} 秒 · 每日上限：{config['cap']} 积分\n"
                    f"奖励群：{', '.join(map(str, config['groups'])) or '未配置'}\n\n"
                    "📌 说明\n积分按群独立；调整需指定群、用户和原因。",
                    [
                        ("设置奖励", {"action": "form", "form": "points"}),
                        ("调整积分", {"action": "form", "form": "adjust"}),
                    ]
                    + back,
                    fold_sections=True,
                )
            if scope == "game":
                modules = self.store.get("modules", {})
                return await self.render(
                    update,
                    "🎮 玩法管理\n"
                    f"玩法中心总开关：{'🟢 已开启' if modules.get('game') else '⚪ 已关闭'}\n"
                    "总开关只控制新业务；逐群开关和历史数据不会被覆盖。\n"
                    "先打开「本群玩法启停」查看全部拦截原因，可确认后一次补齐本群配置。\n\n"
                    "🎲 开奖型玩法\n"
                    "加拿大28：倍率、限额、开放时段及采集异常。\n"
                    "积分快三：逐群开关、期次、骰子投递及待核查退款。\n\n"
                    "🎯 互动玩法\n"
                    "双人对赌：逐群启停和进行中挑战，不扣积分。\n"
                    "积分转盘：逐群开关、投入档位、次数与冷却。\n"
                    "老虎机PvP：逐群开关、桌次、托管与投递异常。\n"
                    "扫雷接龙：逐群开关、桌次、托管与异常退款。\n\n"
                    "📌 权限边界\n需要玩法业务权限、对应群授权及 Telegram 群管理员身份。",
                    [
                        ("🎛 本群玩法启停", {"action": "games_group_list"}),
                        ("🎲 加拿大28", {"action": "games_canada"}),
                        ("🎲 积分快三", {"action": "k3_groups"}),
                        ("🤝 双人对赌", {"action": "games_duel"}),
                        ("🎡 积分转盘", {"action": "wheel_groups"}),
                        ("🎰老虎机PvP", {"action": "slots_groups"}),
                        ("💣 扫雷接龙", {"action": "mines_groups"}),
                    ]
                    + back,
                    fold_sections=True,
                )
            if scope == "ads":
                from .ad_settings_ui import action as settings_action

                return await settings_action(self, update, {"action": "ap_home"})
        if action == "games_canada":
            self.store.require(uid, "game")
            status = self.store.get("keno_error", "等待首次采集")
            if not getattr(self.runtime, "keno", None):
                status = "采集未开启，请在插件配置开启 Keno 采集；需要加拿大代理时在后台配置。"
            rooms = self.runtime.game.rooms()
            modules = self.store.get("modules", {})
            latest = self.store.db.execute(
                "SELECT issue,received,conflict FROM draws ORDER BY issue DESC LIMIT 1"
            ).fetchone()
            draw_text = "暂无有效开奖"
            if latest:
                age = max(0, int(self.store.clock() - latest["received"]))
                draw_text = f"第{latest['issue']}期 · " + (
                    "⚠️ 冲突待核查" if latest["conflict"] else f"{age}秒前收到"
                )
            return await self.render(
                update,
                "🎲 加拿大28管理\n"
                f"玩法中心：{'🟢 已开启' if modules.get('game') else '⚪ 已关闭'}\n"
                f"已开放倍率房：{sum(bool(room['enabled']) for room in rooms.values())}/{len(rooms)}\n"
                f"最近开奖：{draw_text}\n"
                f"采集状态：{status or '🟢 最近采集成功'}\n\n"
                "📋 倍率房\n"
                + "\n".join(
                    f"{NAMES[key]} · {'🟢 开放' if room['enabled'] else '⚪ 关闭'}"
                    for key, room in rooms.items()
                )
                + "\n\n📌 配置边界\n"
                "倍率房设置管理单笔与单期限额；开放时间作用于所有群。\n"
                "关闭只停止新下注，已受理订单继续按原快照结算。",
                [
                    (
                        f"设置 {NAMES[key]}",
                        {"action": "room_manage", "room": key},
                    )
                    for key in rooms
                ]
                + [
                    ("开放时间", {"action": "hours_home"}),
                    ("采集与投递异常", {"action": "game_delivery"}),
                    ("返回玩法管理", {"action": "admin_game"}),
                ],
                fold_sections=True,
            )
        if action == "games_duel":
            self.store.require(uid, "game")
            modules = self.store.get("modules", {})
            duel_groups = self.store.db.execute(
                "SELECT g.chat,g.title,COALESCE(d.enabled,0) AS duel_enabled,"
                "(SELECT COUNT(*) FROM duels x WHERE x.chat=g.chat "
                "AND x.status IN ('invited','terms','choosing','pending')) AS active "
                "FROM platform_mod_groups g LEFT JOIN duel_groups d ON d.chat=g.chat "
                "WHERE g.enabled=1 ORDER BY g.title,g.chat"
            ).fetchall()
            page = max(
                0, min(int(payload.get("page", 0)), max(0, (len(duel_groups) - 1) // 8))
            )
            total_groups = len(duel_groups)
            duel_groups = duel_groups[page * 8 : page * 8 + 8]
            group_text = "\n".join(
                f"{i}. {r['title'] or r['chat']} · "
                f"{'🟢 开启' if r['duel_enabled'] else '⚪ 关闭'} · 进行中{r['active']}场"
                for i, r in enumerate(duel_groups, page * 8 + 1)
            )
            return await self.render(
                update,
                "🤝 双人对赌管理\n"
                f"玩法中心：{'🟢 已开启' if modules.get('game') else '⚪ 已关闭'}\n"
                f"对赌总开关：{'🟢 已开启' if modules.get('duel', True) else '⚪ 已关闭'}\n"
                f"已登记群：{total_groups}个 · 当前第{page + 1}页\n\n"
                "📋 逐群状态\n" + (group_text or "暂无已启用群") + "\n\n📌 配置边界\n"
                "逐群开关不会修改总开关；关闭只停止新挑战，已锁定挑战继续结算。\n"
                "对赌不扣积分，机器人不担保双方自定义彩头。",
                [
                    (
                        f"管理 {i}",
                        {
                            "action": "duel_group_preview",
                            "chat": row["chat"],
                            "enabled": not row["duel_enabled"],
                        },
                    )
                    for i, row in enumerate(duel_groups, page * 8 + 1)
                ]
                + ([("上一页", {"action": action, "page": page - 1})] if page else [])
                + (
                    [("下一页", {"action": action, "page": page + 1})]
                    if (page + 1) * 8 < total_groups
                    else []
                )
                + [("返回玩法管理", {"action": "admin_game"})],
                fold_sections=True,
            )
        if action in {"duel_group_preview", "duel_group_save"}:
            self.store.require(uid, "game")
            if update.effective_chat.type != "private":
                raise Rejected("请私聊管理对赌。")
            await self.runtime.moderation.check(uid, payload["chat"], "view")
        if action == "duel_group_preview":
            self.store.require(uid, "game")
            self.store.db.execute(
                "INSERT OR IGNORE INTO duel_groups(chat,enabled) VALUES(?,0)",
                (payload["chat"],),
            )
            row = self.store.db.execute(
                "SELECT g.chat,g.title,COALESCE(d.enabled,0) enabled,COALESCE(d.version,1) version "
                "FROM mod_groups g LEFT JOIN duel_groups d ON d.chat=g.chat WHERE g.chat=?",
                (payload["chat"],),
            ).fetchone()
            if not row:
                raise Rejected("群不存在")
            count = self.store.db.execute(
                "SELECT COUNT(*) FROM duels WHERE chat=? AND status IN ('invited','terms','choosing','pending')",
                (payload["chat"],),
            ).fetchone()[0]
            return await self.render(
                update,
                f"对赌 · {row['title'] or row['chat']}\n"
                f"当前：{'开启' if row['enabled'] else '关闭'}\n"
                f"进行中：{count} 场\n"
                "关闭只停止新邀请和新面板操作；已锁定挑战继续按原规则结算。\n"
                "积分、加拿大28文字下注和开奖结果不受此开关影响。",
                [
                    (
                        f"确认{'开启' if payload['enabled'] else '关闭'}对赌",
                        {
                            **payload,
                            "action": "duel_group_save",
                            "version": row["version"],
                        },
                    ),
                    ("返回玩法管理", {"action": "admin_game"}),
                ],
                fold_sections=True,
            )
        if action == "duel_group_save":
            self.store.require(uid, "game")
            if type(payload.get("enabled")) is not bool:
                raise Rejected("无效开关")
            with self.store.tx() as db:
                row = db.execute(
                    "SELECT version FROM duel_groups WHERE chat=?", (payload["chat"],)
                ).fetchone()
                if not row or row["version"] != payload["version"]:
                    raise Rejected("对赌群配置已变化，请重新打开")
                db.execute(
                    "UPDATE duel_groups SET enabled=?,version=version+1 WHERE chat=?",
                    (int(payload["enabled"]), payload["chat"]),
                )
                self.store.audit(
                    db,
                    uid,
                    "duel_group_config",
                    {"chat": payload["chat"], "enabled": payload["enabled"]},
                )
            return await self.admin(update, {"action": "admin_game"}, token)
        if action == "ad_queue":
            self.store.require(uid, "ads")
            paid = int(bool(payload.get("paid", True)))
            category = payload.get("category", "paid" if paid else "unpaid")
            conditions = {
                "paid": "a.paid=1 AND a.status='pending'",
                "unpaid": "a.paid=0 AND a.status='pending'",
                "active": "a.status IN ('active','published')",
                "review": "a.status='review' OR a.refund_state='manual_pending'",
            }
            if category not in conditions:
                raise Rejected("分类无效")
            page = max(0, int(payload.get("page", 0)))
            back = [
                ("上一页", {**payload, "page": max(0, page - 1)}),
                ("下一页", {**payload, "page": page + 1}),
            ] + back
            rows = self.store.db.execute(
                "SELECT a.id,a.status,a.uid,a.paid,u.username FROM platform_ads a LEFT JOIN user_labels u ON u.uid=a.uid WHERE ("
                + conditions[category]
                + ") ORDER BY a.at DESC LIMIT 20 OFFSET ?",
                (page * 20,),
            ).fetchall()
            return await self.render(
                update,
                {
                    "paid": "已付款待处理",
                    "unpaid": "未付款",
                    "active": "展示中",
                    "review": "待核查",
                }[category]
                + f" · 第{page + 1}页\n"
                + (
                    "\n".join(
                        f"{i}. {'@' + r['username'] if r['username'] else 'UID ' + r['uid']} · "
                        f"{'已付款' if r['paid'] else '未付款'} · {AD_STATES.get(r['status'], r['status'])}\n订单：{r['id']}"
                        for i, r in enumerate(rows, 1)
                    )
                    or "暂无此类订单"
                ),
                [
                    ("已付款订单", {"action": "ad_queue", "paid": True}),
                    ("未付款订单", {"action": "ad_queue", "paid": False}),
                    ("展示中", {"action": "ad_queue", "category": "active"}),
                    ("待核查", {"action": "ad_queue", "category": "review"}),
                ]
                + [
                    (
                        f"查看订单 {i}",
                        {"action": "ad_review", "id": r["id"]},
                    )
                    for i, r in enumerate(rows, 1)
                ]
                + back,
                fold_sections=True,
            )
        if action == "ad_review":
            self.store.require(uid, "ads")
            row = self.store.db.execute(
                "SELECT * FROM ads WHERE id=?", (payload["id"],)
            ).fetchone()
            if not row:
                raise Rejected("订单不存在")
            buttons = []
            if row["status"] in ("active", "published"):
                buttons.append(
                    (
                        "修改已发布文案",
                        {"action": "form", "form": "ad_edit", "id": row["id"]},
                    )
                )
            if row["status"] == "pending":
                buttons += [
                    (
                        "修改广告文案",
                        {"action": "form", "form": "ad_edit", "id": row["id"]},
                    ),
                    (
                        "审核通过",
                        {
                            "action": "ad_decide_preview",
                            "id": row["id"],
                            "decision": "approve",
                        },
                    ),
                    (
                        "✅ 确认已收款",
                        {
                            "action": "ad_paid_preview",
                            "id": row["id"],
                            "version": row["version"],
                        },
                    ),
                    (
                        "🧪 测试免付款发布",
                        {
                            "action": "ad_test_paid_preview",
                            "id": row["id"],
                            "version": row["version"],
                        },
                    ),
                    ("驳回", {"action": "form", "form": "reject", "id": row["id"]}),
                ]
            if row["status"] in ("active", "published"):
                buttons += [
                    ("停止展示", {"action": "form", "form": "stop", "id": row["id"]})
                ]
            elif row["status"] == "pending":
                buttons += [
                    ("取消订单／退款", {"action": "ad_cancel_preview", "id": row["id"]})
                ]
            if row["proof"].startswith("photo:"):
                buttons += [("查看凭证图片", {"action": "proof_view", "id": row["id"]})]
            if row["refund_state"] == "manual_pending":
                buttons += [
                    (
                        "登记人工退款完成",
                        {"action": "form", "form": "manual_refund", "id": row["id"]},
                    )
                ]
            package = json.loads(row["package"])
            return await self.render(
                update,
                f"📋 广告订单\n编号：{row['id']} · 用户：{row['uid']}\n"
                f"状态：{AD_STATES.get(row['status'], row['status'])}\n"
                f"内容审核：{'已通过' if row['approved'] else '待审核'} · 核款：{'已付款' if row['paid'] else '未付款'}\n\n"
                f"📍 广告位\n{package['name']} · {package['price']} {package['currency']}\n"
                f"投放位置：{package['target']} · {'置顶' if package['kind'] == 'pinned' else '普通'}\n"
                f"期限：{'30天' if package['duration'] == '30days' else '自然月'}\n\n"
                f"📝 广告内容\n{row['body']}\n联系方式：{row['contact']}\n\n"
                f"💳 凭证与处理说明\n{row['proof'][:500] or '暂无凭证'}\n{row['error'] or '无异常说明'}\n"
                "凭证不等于已到账；内容审核和确认收款需分别操作。",
                buttons + back,
                fold_sections=True,
            )
        if action == "proof_view":
            self.store.require(uid, "ads")
            row = self.store.db.execute(
                "SELECT proof FROM ads WHERE id=?", (payload["id"],)
            ).fetchone()
            if not row or not row[0].startswith("photo:"):
                raise Rejected("暂无图片凭证")
            return await self.runtime.bot.send_photo(
                chat_id=uid,
                photo=row[0][6:],
                caption="用户提交的凭证，不能代替实际到账核验",
            )
        if action in {"ad_paid_preview", "ad_test_paid_preview"}:
            self.store.require(uid, "ads")
            row = self.store.db.execute(
                "SELECT * FROM ads WHERE id=?", (payload["id"],)
            ).fetchone()
            if not row or row["status"] != "pending":
                raise Rejected("订单不在待处理状态")
            test = action == "ad_test_paid_preview"
            if test and str(uid) != self.store.owner:
                raise Rejected("测试免付款仅首位超管可用")
            package = json.loads(row["package"])
            label = "测试免付款发布" if test else "确认实际收款"
            return await self.render(
                update,
                f"{label}\n订单：{row['id']}\n应收：{package['price']} {package['currency']}\n"
                f"记录：管理员 {uid} · 当前北京时间\n"
                + (
                    "不会伪造到账流水；仅标记为测试免付款。"
                    if test
                    else "将记录确认时间；未填写外部流水号。"
                ),
                [
                    (
                        "确认执行",
                        {
                            "action": "ad_paid",
                            "id": row["id"],
                            "version": row["version"],
                            "test": test,
                        },
                    ),
                    ("返回订单", {"action": "ad_review", "id": row["id"]}),
                ],
                fold_sections=True,
            )
        if action == "ad_paid":
            self.store.require(uid, "ads")
            row = self.store.db.execute(
                "SELECT version FROM ads WHERE id=?", (payload["id"],)
            ).fetchone()
            if not row or row["version"] != payload["version"]:
                raise Rejected("订单已变化，请重新打开")
            if payload.get("test") and str(uid) != self.store.owner:
                raise Rejected("测试免付款仅首位超管可用")
            if payload.get("test"):
                self.runtime.ads.moderate(
                    uid, payload["id"], "paid", f"测试免付款；管理员 {uid} 确认"
                )
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE ads SET payment_source='test' WHERE id=?",
                        (payload["id"],),
                    )
                    self.store.audit(
                        db, uid, "ad_test_payment_override", {"id": payload["id"]}
                    )
            else:
                self.runtime.ads.moderate(uid, payload["id"], "paid")
            return await self.render(
                update,
                "已记录收款状态，审核通过后将进入发布队列。",
                [("查看订单", {"action": "ad_review", "id": payload["id"]})],
            )
        if action == "ad_decide_preview":
            self.store.require(uid, "ads")
            row = self.store.db.execute(
                "SELECT body,paid FROM ads WHERE id=? AND status='pending'",
                (payload["id"],),
            ).fetchone()
            if not row:
                raise Rejected("订单不在待审核状态")
            return await self.render(
                update,
                f"审核文案：\n{row['body']}\n\n{'已付款，确认后将安排发布。' if row['paid'] else '未付款，仅审核内容，不会发布。'}",
                [
                    (
                        "确认审核",
                        {
                            "action": "ad_decide",
                            "id": payload["id"],
                            "decision": "approve",
                            "expected": row["body"],
                        },
                    )
                ]
                + back,
            )
        if action == "ad_decide":
            if "expected" not in payload or "version" not in payload:
                raise Rejected("请重新打开订单，预览当前文案后确认审核")
            self.runtime.ads.moderate(
                uid,
                payload["id"],
                payload["decision"],
                payload.get("reason", ""),
                expected=payload.get("expected"),
                expected_version=payload["version"],
            )
            return await self.render(update, "已记录处理结果。", back)
        if action == "form":
            form = payload["form"]
            if form in ("ad_edit", "paid", "reject", "stop", "manual_refund"):
                if "version" not in payload:
                    raise Rejected("请重新打开订单操作")
            if form == "ad_edit":
                row = self.store.db.execute(
                    "SELECT body FROM ads WHERE id=? AND status IN ('pending','active','published')",
                    (payload["id"],),
                ).fetchone()
                if not row:
                    raise Rejected("当前订单不可修改文案")
                payload = {**payload, "expected": row[0]}
            if form in STEPS:
                return await run_wizard(self, update, form, [])
            scope = FORM_SCOPES[form]
            self.store.require(uid, scope, owner=scope == "owner")
            self.store.dialog(
                uid,
                {
                    "form": form,
                    **{k: v for k, v in payload.items() if k not in ("action", "form")},
                },
            )
            return await self.render(
                update, FORMS[form] + "\n/cancel 取消；提交后还需按钮确认。", back
            )
        if action == "save_form":
            data = payload["data"]
            form = payload["form"]
            if (
                form in ("ad_edit", "paid", "reject", "stop", "manual_refund")
                and "version" not in payload
            ):
                raise Rejected("请重新打开订单操作")
            self.store.require(
                uid, FORM_SCOPES[form], owner=FORM_SCOPES[form] == "owner"
            )
            if form == "points":
                for group in data.get("groups", []):
                    if not self.store.db.execute(
                        "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                        (str(group),),
                    ).fetchone():
                        raise Rejected("奖励群尚未启用管理或已停用，请重新选择")
                self.runtime.points.configure(uid, data)
            elif form == "package":
                if not self.store.db.execute(
                    "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1 UNION ALL SELECT 1 FROM ad_channels WHERE chat=? AND enabled=1",
                    (str(data["package"]["target"]), str(data["package"]["target"])),
                ).fetchone():
                    raise Rejected("发布群已停用或未纳入管理，请重新选择")
                self.runtime.ads.configure(uid, data["key"], data["package"])
            elif form == "room":
                self.runtime.game.configure(uid, **data)
            elif form == "ad_edit":
                state = self.store.db.execute(
                    "SELECT status FROM ads WHERE id=?", (data["id"],)
                ).fetchone()
                published = state and state[0] in ("active", "published")
                if published:
                    await self.runtime.ads.edit_published(
                        uid,
                        data["id"],
                        data["body"],
                        data["expected"],
                        self.runtime.bot,
                        expected_version=payload["version"],
                    )
                else:
                    self.runtime.ads.edit(
                        uid,
                        data["id"],
                        data["body"],
                        data["expected"],
                        expected_version=payload["version"],
                    )
                return await self.render(
                    update,
                    "原群消息已更新，付款、有效期及置顶保持不变。"
                    if published
                    else "文案已保存，请重新审核；付款状态保持不变。",
                    [("返回订单审核", {"action": "ad_review", "id": data["id"]})],
                )
            elif form == "adjust":
                self.runtime.points.adjust(
                    uid,
                    data["target"],
                    data["amount"],
                    data["reason"],
                    "adjust/" + token,
                    chat=data.get("chat"),
                )
            elif form == "revoke":
                self.store.revoke(uid, data["target"])
            elif form == "grant":
                if data.get("target_username"):
                    await resolve_grant_target(
                        self.runtime,
                        "@" + data["target_username"],
                        expected=data["target"],
                    )
                link_token = self.store.grant(
                    uid, data["target"], data["scopes"], data["days"]
                )
                name = self.runtime.bot.username
                return await self.render(
                    update,
                    f"授权链接（十分钟有效，仅目标用户可领取）：\nhttps://t.me/{name}?start=sbg_{link_token}",
                    back,
                )
            elif form in ("paid", "reject", "stop", "manual_refund"):
                self.runtime.ads.moderate(
                    uid,
                    data["id"],
                    form,
                    data["reason"],
                    expected_version=payload["version"],
                )
            return await self.render(
                update, "设置已保存；功能入口可返回首页刷新。", back
            )
        raise Rejected("操作不存在，请返回首页")

    async def input(self, update, dialog):
        if dialog.get("form") == "tenant_input":
            from .tenant_ui import input_text

            return await input_text(self, update, dialog)
        if dialog.get("form") == "wheel_field":
            from .wheel_admin import action as wheel_action

            return await wheel_action(
                self,
                update,
                {
                    **dialog["payload"],
                    "action": "wheel_preview",
                    "value": (update.message.text or "").strip(),
                },
            )
        if dialog.get("form") == "ap_field":
            from .ad_settings_ui import action as settings_action

            return await settings_action(
                self,
                update,
                {
                    **dialog["payload"],
                    "action": "ap_preview",
                    "value": (update.message.text or "").strip(),
                },
            )
        if dialog.get("form") in {"bet", "chase"}:
            self.store.clear_dialog(str(update.effective_user.id))
            raise Rejected("下注请到已启用的群发送 jnd 激活。旧私聊填写已取消。")
        if dialog.get("form") in ("ad_capacity", "ad_recover"):
            text = (update.message.text or "").strip()
            if not text.isascii() or not text.isdigit():
                raise Rejected("请发送正整数")
            from .ad_maintenance_ui import action as maintenance_action

            if dialog["form"] == "ad_capacity":
                return await maintenance_action(
                    self,
                    update,
                    {
                        "action": "ad_capacity_preview",
                        "chat": dialog["chat"],
                        "version": dialog["version"],
                        "capacity": int(text),
                    },
                )
            return await self.render(
                update,
                f"确认已人工核实消息 {text} 的正文与置顶结果符合预期？不会重新发送。",
                [
                    (
                        "确认人工核查",
                        {
                            "action": "ad_recover_confirm",
                            "op": dialog["op"],
                            "message": int(text),
                        },
                    ),
                    ("返回", {"action": "ad_recovery"}),
                ],
            )
        if dialog.get("form") == "channel_add":
            from .channels import action as channel_action
            from .channels import target

            return await channel_action(
                self,
                update,
                {
                    "action": "channel_preview",
                    "chat": target(update.message.text or ""),
                    "enabled": True,
                },
            )
        if dialog.get("form") == "wizard":
            text = (update.message.text or "").strip()
            if not text or "\n" in text or len(text) > 500:
                raise Rejected("请只发送当前这一步的内容（单行，最多500字）")
            return await run_wizard(
                self, update, dialog["kind"], dialog["values"] + [text]
            )
        if dialog.get("form") == "room_limit":
            uid = str(update.effective_user.id)
            self.store.require(uid, "game")
            text = (update.message.text or "").strip()
            if not text.isascii() or not text.isdigit():
                raise Rejected("请只发送一个整数")
            room = dialog["expected"]
            values = {
                key: room[key] for key in ("enabled", "minimum", "maximum", "total")
            }
            values[dialog["field"]] = int(text)
            if (
                not 1
                <= values["minimum"]
                <= values["maximum"]
                <= values["total"]
                <= 1000000
            ):
                raise Rejected(
                    "需满足：1 ≤ 最小下注 ≤ 单笔上限 ≤ 单期上限 ≤ 1,000,000；请重新输入"
                )
            self.store.clear_dialog(uid)
            return await self.render(
                update,
                f"确认保存 {room['name']}？\n最小下注：{values['minimum']:,}\n单笔上限：{values['maximum']:,}\n单期上限：{values['total']:,}\n单位：积分",
                [
                    (
                        "确认保存",
                        {
                            "action": "room_save",
                            "room": dialog["room"],
                            "expected": room,
                            "values": values,
                        },
                    ),
                    ("返回倍率设置", {"action": "room_manage", "room": dialog["room"]}),
                ],
            )
        if dialog.get("form") == "deposit":
            text = (update.message.text or "").strip()
            if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 10000:
                raise Rejected("请输入1—10000的整数金额")
            self.store.clear_dialog(str(update.effective_user.id))
            return await self.render(
                update,
                f"创建约 {text} USDT 的充值单？实际金额会附带小数尾数，全部到账金额均计入广告余额。",
                [
                    ("生成充值单", {"action": "deposit_create", "amount": int(text)}),
                    ("取消", {"action": "account"}),
                ],
            )
        uid = str(update.effective_user.id)
        text = (update.message.text or update.message.caption or "").strip()
        form = dialog["form"]
        if form == "proof":
            proof = (
                "photo:" + update.message.photo[-1].file_id
                if update.message.photo
                else text
            )
            self.runtime.ads.proof(uid, dialog["id"], proof)
            self.store.clear_dialog(uid)
            return await self.render(
                update,
                "凭证已提交，等待人工核款。",
                [("查看订单", {"action": "ad_view", "id": dialog["id"]})],
            )
        if form == "ad":
            contact, sep, body = text.partition("\n")
            if not sep or not 1 <= len(body) <= 2200 or not 1 <= len(contact) <= 200:
                raise Rejected("第一行联系方式，其余行为正文（最多2200字）")
            payload = {
                "action": "ad_submit",
                "kind": dialog["kind"],
                "key": dialog["key"],
                "package": dialog["package"],
                "body": body,
                "contact": contact,
            }
            preview = f"广告预览\n{body}\n联系方式：{contact}\n广告位 {dialog['package']['name']}，{dialog['package']['price']} {dialog['package']['currency']}"
        else:
            self.store.require(
                uid, FORM_SCOPES[form], owner=FORM_SCOPES[form] == "owner"
            )
            data = parse_form(form, text, dialog)
            if form == "grant":
                data.update(await resolve_grant_target(self.runtime, data["target"]))
            payload = {"action": "save_form", "form": form, "data": data}
            if "version" in dialog:
                payload["version"] = dialog["version"]
            preview = "请核对设置：\n" + json.dumps(data, ensure_ascii=False, indent=2)
            if form == "ad_edit":
                preview = "确认以下广告文案：\n\n" + data["body"]
        self.store.clear_dialog(uid)
        await self.render(
            update,
            preview,
            [
                ("确认提交", payload),
                ("取消", {"action": "ads" if form == "ad" else "home"}),
            ],
        )


FORM_SCOPES = {
    "manual_refund": "ads",
    "ad_edit": "ads",
    "points": "points",
    "adjust": "points",
    "package": "ads",
    "paid": "ads",
    "reject": "ads",
    "stop": "ads",
    "room": "game",
    "grant": "owner",
    "revoke": "owner",
}
FORMS = {
    "manual_refund": "请填写已实际完成退款的时间、金额及流水依据。确认仅登记，不会执行链上转账。",
    "ad_edit": "请发送修改后的完整广告正文（最多2200字）。下一步预览确认；已发布广告将直接更新原群消息，待处理订单保存后需重新审核。",
    "points": "请发送：开或关 签到积分 每次聊天积分 间隔秒数（至少15秒） 每日聊天上限 群ID列表（逗号分隔，无则-）\n例：开 10 1 15 20 -100123,-100456",
    "adjust": "请发送：群ID 目标UID 增减积分 原因\n例：-100123 123456 10 活动奖励；扣分使用负数。只调整指定群积分。",
    "room": "请发送：房间编号 开或关 最小下注 单笔上限 单期上限\n房间：room18 special double room27 room28 room32\n例：room18 开 1 200000 200000",
    "grant": "请发送：目标账号 全功能 有效天数\n账号支持数字UID、@用户名、t.me/用户名；用户名需对方先私聊发送 /start。\n全功能包含全部业务管理，但不能授权或撤权；例：@example 全功能 30",
    "revoke": "请发送要收回权限的目标数字 UID，同时撤销该用户未领取链接。",
    "package": "请按顺序发送10行：\n广告位编号（字母数字）\n显示名称\n普通或置顶\n价格\n币种代码\n目标群/频道数字ID\n30天或月\n置顶名额数\n收款说明\n开或关\n例：普通广告位也需填写期限与名额，但不执行置顶。",
    "paid": "请填写实际到账核验说明（金额、到账时间、交易流水号）。",
    "reject": "请填写驳回原因；若已收款需人工跟进退款，本系统不自动退款。",
    "stop": "请填写停止展示原因；不会自动按剩余期限退款。共享广告栏仅移除这一广告，其他广告保留。",
}


def parse_form(form, text, dialog):
    if form == "ad_edit" and 1 <= len(text) <= 2200:
        return {"id": dialog["id"], "body": text, "expected": dialog["expected"]}
    parts = text.split()
    if form == "points" and len(parts) == 6:
        if parts[0] not in ("开", "关"):
            raise Rejected("开关请填写开或关")
        return {
            "enabled": parts[0] == "开",
            "checkin": int(parts[1]),
            "chat": int(parts[2]),
            "interval": int(parts[3]),
            "cap": int(parts[4]),
            "groups": [] if parts[5] == "-" else parts[5].replace("，", ",").split(","),
        }
    if (
        form == "adjust"
        and len(parts) >= 4
        and parts[0].startswith("-")
        and parts[0][1:].isdigit()
    ):
        return {
            "chat": parts[0],
            "target": parts[1],
            "amount": int(parts[2]),
            "reason": " ".join(parts[3:]),
        }
    if form == "room" and len(parts) == 5 and parts[1] in ("开", "关"):
        return {
            "room_id": parts[0],
            "enabled": parts[1] == "开",
            "minimum": int(parts[2]),
            "maximum": int(parts[3]),
            "total": int(parts[4]),
        }
    if form == "grant" and len(parts) == 3:
        scopes = {"广告": "ads", "积分": "points", "游戏": "game", "全功能": "manager"}
        words = parts[1].replace("，", ",").split(",")
        if any(w not in scopes for w in words):
            raise Rejected("权限请填写全功能；兼容广告、积分、游戏的旧授权格式")
        return {
            "target": parts[0],
            "scopes": [scopes[w] for w in words],
            "days": int(parts[2]),
        }
    if form == "revoke" and text.isascii() and text.isdigit():
        return {"target": text}
    if form in ("paid", "reject", "stop", "manual_refund") and 1 <= len(text) <= 300:
        return {"id": dialog["id"], "reason": text}
    if form == "package":
        lines = text.splitlines()
        if (
            len(lines) == 10
            and lines[2] in ("普通", "置顶")
            and lines[6] in ("30天", "月")
            and lines[9] in ("开", "关")
        ):
            return {
                "key": lines[0],
                "package": {
                    "name": lines[1],
                    "kind": "normal" if lines[2] == "普通" else "pinned",
                    "price": lines[3],
                    "currency": lines[4],
                    "target": lines[5],
                    "duration": "30days" if lines[6] == "30天" else "month",
                    "slots": int(lines[7]),
                    "payment": lines[8],
                    "enabled": lines[9] == "开",
                },
            }
    raise Rejected("输入格式不正确，请按表单说明重新填写")
