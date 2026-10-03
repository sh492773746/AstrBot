"""Transactional, group-isolated point wheel with durable one-shot rounds."""

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest

from .game_switches import blocker
from .panel_idle import PanelIdle
from .store import Rejected, encode

VERSION = "wheel-1"
ANIMATION_ASSET = "wheel-savnx-v1.gif"
ANIMATION_CACHE = "wheel_animation_savnx_v1"
PRIZES = ((8, 7950), (12, 1450), (18, 400), (25, 120), (40, 70), (100, 10))
DEFAULT = {"enabled": False, "stakes": [50, 100, 500, 1000], "limit": 20, "cooldown": 3}
RULES = (
    "支持单抽、5连抽和10连抽；连抽按钮显示总投入，每抽沿用对应单抽档位。\n"
    "每抽独立使用相同概率，占用一次每日额度；余额或次数不足整批不执行。\n"
    "倍率含本金：投入100，抽中0.8倍返还80，净减少20。\n"
    "0.8倍 79.5% · 1.2倍 14.5% · 1.8倍 4%\n"
    "2.5倍 1.2% · 4倍 0.7% · 10倍 0.1%\n"
    "理论平均返还95%，不保证个人收益；扇区大小不代表中奖概率。\n"
    "指针动画仅作展示，以文字结果为准。点击档位即抽奖，无二次确认。\n"
    "面板仅本人可用，闲置30秒撤回；有效点击续计，最长10分钟。\n"
    "积分按群独立；每日次数按北京时间零点重置。"
)


class Wheel:
    """Own atomic settlements and user-bound Telegram animation panels."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.idle = PanelIdle(self.store)
        self.locks = {}
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS wheel_groups(
                chat TEXT PRIMARY KEY,config TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS wheel_panels(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,uid TEXT NOT NULL,source TEXT NOT NULL,
                message INTEGER,status TEXT NOT NULL,expires REAL NOT NULL,round INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chat,source,uid));
            CREATE TABLE IF NOT EXISTS wheel_orders(
                panel TEXT NOT NULL,round INTEGER NOT NULL,chat TEXT NOT NULL,uid TEXT NOT NULL,
                stake INTEGER NOT NULL,multiplier INTEGER NOT NULL,payout INTEGER NOT NULL,
                balance INTEGER NOT NULL,day TEXT NOT NULL,at REAL NOT NULL,
                version TEXT NOT NULL,config TEXT NOT NULL,PRIMARY KEY(panel,round));
            CREATE INDEX IF NOT EXISTS wheel_user_day ON wheel_orders(chat,uid,day);
            CREATE INDEX IF NOT EXISTS wheel_user_time ON wheel_orders(chat,uid,at);
            CREATE INDEX IF NOT EXISTS wheel_panel_expiry ON wheel_panels(status,expires);
            UPDATE wheel_panels SET status='review' WHERE status='sending';
        """)
        if "draws" not in {
            r[1] for r in self.store.db.execute("PRAGMA table_info(wheel_orders)")
        }:
            self.store.db.execute(
                "ALTER TABLE wheel_orders ADD COLUMN draws INTEGER NOT NULL DEFAULT 1"
            )

    def config(self, chat):
        """Read defaults without implicitly enabling a group.

        Args:
            chat: Group identifier.
        """
        row = self.store.db.execute(
            "SELECT config,version FROM wheel_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        return (
            (json.loads(row["config"]), row["version"]) if row else (dict(DEFAULT), 0)
        )

    def check(self, chat):
        """Recheck local switches inside settlement transactions.

        Args:
            chat: Registered group identifier.
        """
        config, version = self.config(chat)
        reason = blocker(self.store, chat, "wheel", config["enabled"])
        if reason:
            raise Rejected(reason)
        return config, version

    def settle(self, identity, round_id, uid, chat, message, stake, version, draws=1):
        """Claim, draw and book both ledger legs in one transaction.

        Args:
            identity: Panel identifier.
            round_id: One-shot round number from the button.
            uid: Actual Telegram actor.
            chat: Actual Telegram group.
            message: Actual panel message.
            stake: Requested whole-point stake.
            version: Configuration version verified before network validation.
            draws: Number of independent draws, one, five or ten.
        """
        now = self.store.clock()
        day = (
            datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()
        )
        with self.store.tx() as db:
            panel = db.execute(
                "SELECT * FROM wheel_panels WHERE id=?", (identity,)
            ).fetchone()
            if not panel or (panel["uid"], panel["chat"], panel["message"]) != (
                str(uid),
                str(chat),
                message,
            ):
                raise Rejected("这不是你的转盘，请发送“转盘”打开自己的面板。")
            prior = db.execute(
                "SELECT * FROM wheel_orders WHERE panel=? AND round=?",
                (identity, round_id),
            ).fetchone()
            if prior:
                return dict(prior), False
            config, current_version = self.check(chat)
            if version != current_version:
                raise Rejected("本群规则已调整，请刷新后重试。")
            if (
                panel["status"] != "active"
                or panel["expires"] <= now
                or panel["round"] != round_id
            ):
                raise Rejected("按钮已失效，请刷新或重新发送“转盘”。")
            if type(stake) is not int or stake not in config["stakes"]:
                raise Rejected("投入档位已调整，请刷新。")
            if type(draws) is not int or draws not in {1, 5, 10}:
                raise Rejected("只支持单抽、5连抽或10连抽。")
            count = db.execute(
                "SELECT coalesce(sum(draws),0) FROM wheel_orders WHERE chat=? AND uid=? AND day=?",
                (str(chat), str(uid), day),
            ).fetchone()[0]
            if count + draws > config["limit"]:
                raise Rejected(
                    f"今日抽奖次数不足，剩余{max(0, config['limit'] - count)}次；未扣分。"
                )
            last = db.execute(
                "SELECT at FROM wheel_orders WHERE chat=? AND uid=? ORDER BY at DESC LIMIT 1",
                (str(chat), str(uid)),
            ).fetchone()
            if last and now - last["at"] < config["cooldown"]:
                raise Rejected(f"请间隔{config['cooldown']}秒再抽奖。")
            operation = f"wheel:{identity}:{round_id}"
            self.store.credit(
                db,
                operation + ":stake",
                uid,
                -stake * draws,
                "wheel_stake",
                chat=str(chat),
            )
            results = []
            for _ in range(draws):
                ticket = secrets.randbelow(10000)
                cumulative = 0
                for multiplier, weight in PRIZES:
                    cumulative += weight
                    if ticket < cumulative:
                        break
                results.append(multiplier)
            payout = sum(stake * value // 10 for value in results)
            self.store.credit(
                db, operation + ":payout", uid, payout, "wheel_payout", chat=str(chat)
            )
            balance = self.store.balance(uid, chat=str(chat))
            db.execute(
                "INSERT INTO wheel_orders(panel,round,chat,uid,stake,multiplier,payout,balance,day,at,version,config,draws) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    round_id,
                    str(chat),
                    str(uid),
                    stake * draws,
                    multiplier if draws == 1 else 0,
                    payout,
                    balance,
                    day,
                    now,
                    VERSION,
                    encode(
                        {
                            "version": current_version,
                            **config,
                            "unit_stake": stake,
                            "results": results,
                        }
                    ),
                    draws,
                ),
            )
            db.execute("UPDATE wheel_panels SET round=round+1 WHERE id=?", (identity,))
            self.store.audit(
                db,
                uid,
                "wheel_settle",
                {"panel": identity, "round": round_id, "chat": str(chat)},
            )
            return dict(
                db.execute(
                    "SELECT * FROM wheel_orders WHERE panel=? AND round=?",
                    (identity, round_id),
                ).fetchone()
            ), True

    def render(self, identity, user, rules=False):
        """Render the persisted result without drawing again.

        Args:
            identity: Stored panel identifier.
            user: Telegram user for escaped mention.
            rules: Whether to expand the compact rule caption.
        """
        panel = self.store.db.execute(
            "SELECT * FROM wheel_panels WHERE id=?", (identity,)
        ).fetchone()
        config, _ = self.config(panel["chat"])
        day = (
            datetime.fromtimestamp(self.store.clock(), timezone(timedelta(hours=8)))
            .date()
            .isoformat()
        )
        count = self.store.db.execute(
            "SELECT coalesce(sum(draws),0) FROM wheel_orders WHERE chat=? AND uid=? AND day=?",
            (panel["chat"], panel["uid"], day),
        ).fetchone()[0]
        label = "@" + user.username if user.username else user.full_name
        text = f'<b>🎡 积分转盘</b> · <a href="tg://user?id={panel["uid"]}">{escape(label)}</a>\n'
        order = self.store.db.execute(
            "SELECT * FROM wheel_orders WHERE panel=? ORDER BY round DESC LIMIT 1",
            (identity,),
        ).fetchone()
        if order:
            result_title = f"抽中 <b>{order['multiplier'] / 10:g}倍</b>"
            if order["draws"] > 1:
                saved = json.loads(order["config"])
                result_title = (
                    f"<b>{order['draws']}连抽</b> · 每抽{saved['unit_stake']}积分\n"
                    "<blockquote expandable>"
                    + " · ".join(f"{value / 10:g}倍" for value in saved["results"])
                    + "</blockquote>"
                )
            text += (
                f"\n🎯 {result_title}\n"
                f"投入 {order['stake']} · 返还 <b>{order['payout']}</b>\n"
                f"本次净增减 <b>{order['payout'] - order['stake']:+d}</b> 积分\n"
            )
        text += f"\n💰 当前积分：<b>{self.store.balance(panel['uid'], panel['chat'])}</b>\n🎟 今日剩余：{max(0, config['limit'] - count)}/{config['limit']} 次\n"
        text += "🎞 指针仅作展示，以文字开奖结果为准。\n"
        text += (
            "<blockquote expandable>"
            + (
                RULES
                if rules
                else "点击档位立即扣除积分并抽奖，无二次确认。\n倍率包含本金；闲置30秒撤回，有效点击续计，最长10分钟。\n概率公示请点「规则」。"
            )
            + "</blockquote>"
        )
        active = panel["expires"] > self.store.clock() and panel["status"] in {
            "active",
            "sending",
        }
        prefix = f"wh:{identity}:{panel['round']}:"
        buttons = (
            [
                Button(f"🎡 {stake} 积分", callback_data=prefix + str(stake))
                for stake in config["stakes"]
            ]
            if active
            else []
        )
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        if active:
            for stake in config["stakes"]:
                rows.append(
                    [
                        Button(
                            f"5连抽 · {stake * 5}积分",
                            callback_data=prefix + f"{stake}x5",
                        ),
                        Button(
                            f"10连抽 · {stake * 10}积分",
                            callback_data=prefix + f"{stake}x10",
                        ),
                    ]
                )
            rows.append(
                [
                    Button("🔄 刷新", callback_data=prefix + "refresh"),
                    Button("📖 规则", callback_data=prefix + "rules"),
                ]
            )
        else:
            text += "\n⌛ 面板已过期，请重新发送“转盘”。"
        return text, InlineKeyboardMarkup(rows)

    async def open(self, update):
        """Send one claimed animation; ambiguous sends are never retried.

        Args:
            update: Verified group message or hub callback.
        """
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        if update.effective_chat.type != "supergroup":
            raise Rejected("请在已开启的群内发送“转盘”。")
        if getattr(update.effective_user, "is_bot", False):
            raise Rejected("仅普通用户可打开转盘。")
        if (
            not update.callback_query
            and not 0 <= self.store.clock() - update.message.date.timestamp() <= 60
        ):
            raise Rejected("消息已过期，请重新发送“转盘”。")
        self.check(chat)
        await self.runtime.community.member(uid, chat, require_moderation=False)
        self.check(chat)
        source = (
            ("callback:" + update.callback_query.id)
            if update.callback_query
            else ("message:" + str(update.message.message_id))
        )
        identity = secrets.token_hex(8)
        with self.store.tx() as db:
            if not db.execute(
                "INSERT OR IGNORE INTO wheel_panels(id,chat,uid,source,status,expires) VALUES(?,?,?,?,'sending',?)",
                (identity, chat, uid, source, self.store.clock() + 600),
            ).rowcount:
                return
        try:
            caption, markup = self.render(identity, update.effective_user)
            asset = self.store.get(ANIMATION_CACHE)
            animation = (
                asset
                or (Path(__file__).parent / "assets" / ANIMATION_ASSET).read_bytes()
            )
            sent = await self.runtime.bot.send_animation(
                chat_id=chat,
                animation=animation,
                filename=ANIMATION_ASSET if not asset else None,
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
            with self.store.tx() as db:
                db.execute(
                    "UPDATE wheel_panels SET message=?,status='active' WHERE id=?",
                    (sent.message_id, identity),
                )
                self.store.put(db, ANIMATION_CACHE, sent.animation.file_id)
                self.idle.touch(chat, sent.message_id, "wheel")
        except BaseException:
            self.store.db.execute(
                "UPDATE wheel_panels SET status='review' WHERE id=? AND status='sending'",
                (identity,),
            )
            raise

    async def action(self, update):
        """Validate identity and serialize edits while settlement stays synchronous.

        Args:
            update: Telegram callback update.
        """
        query = update.callback_query
        pieces = str(query.data).split(":")
        if len(pieces) != 4 or not pieces[2].isdigit():
            raise Rejected("无效转盘按钮。")
        _, identity, round_id, action = pieces
        panel = self.store.db.execute(
            "SELECT * FROM wheel_panels WHERE id=?", (identity,)
        ).fetchone()
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        if not panel or (panel["chat"], panel["uid"], panel["message"]) != (
            chat,
            uid,
            query.message.message_id,
        ):
            raise Rejected("这不是你的转盘，请发送“转盘”打开自己的面板。")
        lock = self.locks.setdefault(identity, asyncio.Lock())
        try:
            async with lock:
                _, version = self.check(chat)
                await self.runtime.community.member(uid, chat, require_moderation=False)
                self.check(chat)
                self.idle.touch(chat, query.message.message_id, "wheel")
                answer = "已刷新"
                if action not in {"refresh", "rules"}:
                    parts = action.split("x")
                    if len(parts) > 2 or not all(part.isdigit() for part in parts):
                        raise Rejected("无效投入档位。")
                    order, fresh = self.settle(
                        identity,
                        int(round_id),
                        uid,
                        chat,
                        query.message.message_id,
                        int(parts[0]),
                        version,
                        int(parts[1]) if len(parts) == 2 else 1,
                    )
                    answer = (
                        "抽奖成功" if fresh else "该轮已结算，未重复扣分"
                    ) + f"：{order['draws']}次，合计返还{order['payout']}积分"
                caption, markup = self.render(
                    identity, update.effective_user, action == "rules"
                )
                try:
                    await self.runtime.bot.edit_message_caption(
                        chat_id=chat,
                        message_id=query.message.message_id,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                except BadRequest as exc:
                    if "not modified" not in str(exc).lower():
                        raise
                return answer
        finally:
            if not lock.locked() and not getattr(lock, "_waiters", None):
                self.locks.pop(identity, None)
