"""Durable, group-isolated fruit-slot pool games and Telegram delivery."""

import asyncio
import json
import secrets
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup, InputFile
from telegram.error import BadRequest, Forbidden, RetryAfter

from .game_switches import blocker
from .panel_idle import PanelIdle
from .store import Rejected, encode

STAKES = (100, 300, 800, 1500, 2000)


def group_stakes(store, chat, game="slots", db=None):
    """Resolve group operating tiers without changing table snapshots.

    Args:
        store: Instance store.
        chat: Group ID.
        game: Slots or mines.
        db: Optional active transaction.

    Returns:
        Platform-bounded stake list for a new table.
    """
    from .tenants import local_config

    return local_config(store, chat, game + "_stakes", list(STAKES), db)


SYMBOLS = ("🍒", "🍋", "🍊", "🍇")
NAMES = {3: "三连", 2: "对子", 1: "散牌"}
VERSION = "slots-5"
MULTIPLIERS = {1: 0, 2: 10, 3: 62}
RULES = (
    "1—10人，发起者选定档位，全桌同额；点击加入即扣分托管，50秒后锁定。\n"
    "到时单人也开奖；锁定前可退出，退出后本桌不能再次加入。\n"
    "三列独立抽取，樱桃／柠檬／橙子／葡萄各25%。\n"
    "三连＞对子＞散牌；同牌型再比公开幸运点1—1,000,000，高者胜。\n"
    "多人：唯一胜者获得总池90%，抽水10%；每人投入相同。\n"
    "只有最高牌型及幸运点均相同才免费加赛，最多追加5轮仍平局全桌退款。\n"
    "单人：散牌0倍、对子1倍、三连6.2倍，含本金，不另抽水。\n"
    "单人理论平均返还95%，不保证个人收益；GIF展示已保存结果。\n"
    "积分按群独立。"
)


def rank(result):
    """Return the largest matching-symbol count.

    Args:
        result: Three independently drawn symbol indices.
    """
    return max(result.count(symbol) for symbol in result)


class Slots:
    """Own escrow, deterministic recovery and bounded public outbox work."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.idle = PanelIdle(self.store)
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS slots_groups(
                chat TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS slots_menus(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,uid TEXT NOT NULL,
                source TEXT NOT NULL,message INTEGER,status TEXT NOT NULL,
                expires REAL NOT NULL,UNIQUE(chat,uid,source));
            CREATE TABLE IF NOT EXISTS slots_tables(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,creator TEXT NOT NULL,
                menu TEXT NOT NULL UNIQUE,stake INTEGER NOT NULL,status TEXT NOT NULL,
                created REAL NOT NULL,deadline REAL NOT NULL,snapshot TEXT NOT NULL,
                fee INTEGER NOT NULL DEFAULT 0,pool INTEGER NOT NULL DEFAULT 0,
                message INTEGER,rendered TEXT NOT NULL DEFAULT '',
                next_edit REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                next_settle REAL NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS slots_one_open
                ON slots_tables(chat) WHERE status IN ('open','locked');
            CREATE INDEX IF NOT EXISTS slots_due ON slots_tables(status,deadline);
            CREATE INDEX IF NOT EXISTS slots_panel_due ON slots_tables(next_edit) WHERE message IS NOT NULL;
            CREATE INDEX IF NOT EXISTS slots_group_history ON slots_tables(chat,created);
            CREATE TABLE IF NOT EXISTS slots_players(
                table_id TEXT NOT NULL,uid TEXT NOT NULL,label TEXT NOT NULL,
                joined REAL NOT NULL,ordinal INTEGER NOT NULL,status TEXT NOT NULL,
                stake INTEGER NOT NULL DEFAULT 0,
                result TEXT,payout INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(table_id,uid),UNIQUE(table_id,ordinal));
            CREATE TABLE IF NOT EXISTS slots_clicks(
                id TEXT PRIMARY KEY,table_id TEXT NOT NULL,uid TEXT NOT NULL,
                action TEXT NOT NULL,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS slots_outbox(
                id INTEGER PRIMARY KEY,table_id TEXT NOT NULL,seq INTEGER NOT NULL,
                kind TEXT NOT NULL,uid TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',message INTEGER,
                next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                UNIQUE(table_id,seq));
            CREATE INDEX IF NOT EXISTS slots_outbox_due ON slots_outbox(status,next);
            CREATE INDEX IF NOT EXISTS slots_player_user ON slots_players(uid,table_id);
            CREATE TABLE IF NOT EXISTS slots_pacing(chat TEXT PRIMARY KEY,next REAL NOT NULL);
            UPDATE slots_menus SET status='review' WHERE status='sending';
            UPDATE slots_outbox SET status='review',error='InterruptedSend' WHERE status='sending';
            CREATE TABLE IF NOT EXISTS slots_v3_rounds(
                table_id TEXT NOT NULL,round INTEGER NOT NULL,uid TEXT NOT NULL,
                result TEXT NOT NULL,advanced INTEGER NOT NULL,
                seq INTEGER NOT NULL,PRIMARY KEY(table_id,round,uid),
                UNIQUE(table_id,seq));
        """)
        if "luck" not in {
            r["name"]
            for r in self.store.db.execute("PRAGMA table_info(slots_v3_rounds)")
        }:
            self.store.db.execute("ALTER TABLE slots_v3_rounds ADD COLUMN luck INTEGER")
        if "stake" not in {
            r["name"] for r in self.store.db.execute("PRAGMA table_info(slots_players)")
        }:
            self.store.db.execute(
                "ALTER TABLE slots_players ADD COLUMN stake INTEGER NOT NULL DEFAULT 0"
            )
        self.store.db.execute(
            "UPDATE slots_players SET stake=(SELECT stake FROM slots_tables WHERE id=table_id) WHERE stake=0"
        )
        # Suppress only unsent v3 summaries; uncertain/sent deliveries remain evidence.
        self.store.db.execute(
            "UPDATE slots_outbox SET status='cancelled' WHERE kind='summary' AND status='pending' "
            "AND table_id IN (SELECT id FROM slots_tables WHERE json_extract(snapshot,'$.version')='slots-3')"
        )
        # An ambiguous first panel can never accept members: refund its escrow.
        for row in self.store.db.execute(
            "SELECT t.id FROM slots_tables t JOIN slots_outbox o ON o.table_id=t.id "
            "WHERE t.status='open' AND o.kind='panel' AND o.status='review'"
        ).fetchall():
            self.finish(row["id"], cancel=True)

    def check(self, chat):
        """Require all switches without creating a default-enabled group.

        Args:
            chat: Exact registered Telegram group ID.
        """
        row = self.store.db.execute(
            "SELECT version FROM slots_groups WHERE chat=? AND enabled=1", (str(chat),)
        ).fetchone()
        reason = blocker(self.store, chat, "slots", bool(row))
        if reason:
            raise Rejected(reason)
        return row["version"]

    def create(self, menu_id, uid, chat, message, stake, label, version):
        """Create one table and escrow the owner in the same transaction.

        Args:
            menu_id: Owner-bound one-shot menu ID.
            uid: Actual actor.
            chat: Actual group.
            message: Verified menu message ID.
            stake: Fixed public stake tier.
            label: Escaped later, never interpreted as markup.
            version: Group version preceding member verification.
        """
        if str(chat) not in self.store.get("games_v3_groups", []):
            raise Rejected(blocker(self.store, chat, "slots", True, rollout=True))
        now = self.store.clock()
        with self.store.tx() as db:
            menu = db.execute(
                "SELECT * FROM slots_menus WHERE id=?", (menu_id,)
            ).fetchone()
            if not menu or (menu["uid"], menu["chat"], menu["message"]) != (
                str(uid),
                str(chat),
                message,
            ):
                raise Rejected("请自己打开老虎机面板。")
            self.idle.touch(chat, message, "slots")
            prior = db.execute(
                "SELECT id FROM slots_tables WHERE menu=?", (menu_id,)
            ).fetchone()
            if prior:
                return prior["id"]
            if self.check(chat) != version:
                raise Rejected("群设置已变化，请重新打开。")
            if menu["status"] != "active" or menu["expires"] <= now:
                raise Rejected("面板已过期，请重新打开老虎机。")
            if type(stake) is not int or stake not in group_stakes(
                self.store, chat, db=db
            ):
                raise Rejected("无效档位。")
            if db.execute(
                "SELECT 1 FROM slots_tables WHERE chat=? AND status IN ('open','locked')",
                (chat,),
            ).fetchone():
                raise Rejected("本群已有报名桌，请点击该桌加入。")
            identity = secrets.token_hex(6)
            db.execute(
                "INSERT INTO slots_tables(id,chat,creator,menu,stake,status,created,deadline,snapshot) "
                "VALUES(?,?,?,?,?,'open',?,?,?)",
                (
                    identity,
                    chat,
                    uid,
                    menu_id,
                    stake,
                    now,
                    now + 50,
                    encode(
                        {
                            "version": VERSION,
                            "group_version": version,
                            "stakes": group_stakes(self.store, chat, db=db),
                            "fee_percent": 10
                            if VERSION in {"slots-3", "slots-4", "slots-5"}
                            else 0,
                            "max_extra_rounds": 5,
                            "luck_max": 1_000_000 if VERSION == "slots-5" else 0,
                            "multipliers": MULTIPLIERS,
                            "symbols": list(SYMBOLS),
                            "weights": [1, 1, 1, 1],
                        }
                    ),
                ),
            )
            self.store.credit(
                db,
                f"slots:{identity}:{uid}:stake",
                uid,
                -stake,
                "slots_stake",
                chat=chat,
            )
            db.execute(
                "INSERT INTO slots_players(table_id,uid,label,joined,ordinal,status,stake,result,payout) VALUES(?,?,?,?,?,'active',?,?,0)",
                (identity, uid, label, now, 1, stake, None),
            )
            db.execute(
                "INSERT INTO slots_outbox(table_id,seq,kind) VALUES(?,0,'panel')",
                (identity,),
            )
            self.store.audit(
                db,
                uid,
                "slots_create",
                {"table": identity, "chat": chat, "stake": stake},
            )
            return identity

    def participate(self, identity, uid, chat, message, label, action, click, version):
        """Join or refund one member without replaying stale clicks.

        Args:
            identity: Table ID.
            uid: Actual actor.
            chat: Actual group.
            message: Bound public panel.
            label: Current display label.
            action: Join or leave.
            click: Telegram callback identity.
            version: Version checked before live membership verification.
        """
        now = self.store.clock()
        with self.store.tx() as db:
            table = db.execute(
                "SELECT * FROM slots_tables WHERE id=?", (identity,)
            ).fetchone()
            if not table or (table["chat"], table["message"]) != (chat, message):
                raise Rejected("无效桌次按钮。")
            if db.execute("SELECT 1 FROM slots_clicks WHERE id=?", (click,)).fetchone():
                return "此操作已处理，未重复扣分。"
            player = db.execute(
                "SELECT * FROM slots_players WHERE table_id=? AND uid=?",
                (identity, uid),
            ).fetchone()
            if table["status"] != "open" or now >= table["deadline"]:
                raise Rejected("报名已结束，请查看结算。")
            if self.check(chat) != version:
                raise Rejected("群设置已变化，请刷新后重试。")
            legacy = json.loads(table["snapshot"])["version"] == "slots-1"
            equal_stake = legacy or json.loads(table["snapshot"])["version"] in {
                "slots-4",
                "slots-5",
            }
            if action == "join" and equal_stake:
                action = str(table["stake"])
            if action.isdigit():
                stake = int(action)
                if stake not in json.loads(table["snapshot"]).get("stakes", STAKES):
                    raise Rejected("无效积分档位。")
                if equal_stake and stake != table["stake"]:
                    raise Rejected(
                        f"本桌每人须投入{table['stake']}积分，与发起者相同；未扣分。"
                    )
                if player:
                    return (
                        "你已报名。"
                        if player["status"] == "active"
                        else "已退出，本桌不能再次加入。"
                    )
                if (
                    db.execute(
                        "SELECT count(*) FROM slots_players WHERE table_id=? AND status='active'",
                        (identity,),
                    ).fetchone()[0]
                    >= 10
                ):
                    raise Rejected("本桌已满10人。")
                ordinal = db.execute(
                    "SELECT coalesce(max(ordinal),0)+1 FROM slots_players WHERE table_id=?",
                    (identity,),
                ).fetchone()[0]
                self.store.credit(
                    db,
                    f"slots:{identity}:{uid}:stake",
                    uid,
                    -stake,
                    "slots_stake",
                    chat=chat,
                )
                db.execute(
                    "INSERT INTO slots_players(table_id,uid,label,joined,ordinal,status,stake,result,payout) VALUES(?,?,?,?,?,'active',?,?,0)",
                    (identity, uid, label, now, ordinal, stake, None),
                )
                answer = f"报名成功，已托管{stake}积分。"
            elif action == "leave":
                if not player or player["status"] != "active":
                    return "你当前未报名，未扣分。"
                self.store.credit(
                    db,
                    f"slots:{identity}:{uid}:refund",
                    uid,
                    player["stake"],
                    "slots_refund",
                    chat=chat,
                )
                db.execute(
                    "UPDATE slots_players SET status='withdrawn',payout=? WHERE table_id=? AND uid=?",
                    (player["stake"], identity, uid),
                )
                answer = "已退出，积分全额退还。"
            else:
                raise Rejected("无效操作。")
            db.execute(
                "INSERT INTO slots_clicks VALUES(?,?,?,?,?)",
                (click, identity, uid, action, now),
            )
            db.execute("UPDATE slots_tables SET next_edit=0 WHERE id=?", (identity,))
            self.store.audit(
                db, uid, "slots_" + action, {"table": identity, "chat": chat}
            )
            return answer

    def finish(self, identity, cancel=False):
        """Persist immutable draws before atomically settling or refunding.

        Args:
            identity: Persisted table ID.
            cancel: Explicit switch closure or failed initial publication.
        """
        current = self.store.db.execute(
            "SELECT snapshot FROM slots_tables WHERE id=?", (identity,)
        ).fetchone()
        if current and json.loads(current["snapshot"])["version"] in {
            "slots-3",
            "slots-4",
            "slots-5",
        }:
            from .slots_pool import finish

            return finish(self, identity, cancel)
        with self.store.tx() as db:
            table = db.execute(
                "SELECT * FROM slots_tables WHERE id=?", (identity,)
            ).fetchone()
            if not table or table["status"] not in {"open", "locked"}:
                return
            if table["status"] == "locked":
                cancel = False
            elif not cancel and self.store.clock() < table["deadline"]:
                return
            people = db.execute(
                "SELECT * FROM slots_players WHERE table_id=? AND status='active' ORDER BY ordinal",
                (identity,),
            ).fetchall()
            if cancel or len(people) < 2:
                for person in people:
                    self.store.credit(
                        db,
                        f"slots:{identity}:{person['uid']}:refund",
                        person["uid"],
                        person["stake"],
                        "slots_refund",
                        chat=table["chat"],
                    )
                db.execute(
                    "UPDATE slots_players SET status='refunded',payout=stake WHERE table_id=? AND status='active'",
                    (identity,),
                )
                db.execute(
                    "UPDATE slots_tables SET status='cancelled',next_edit=0,error=? WHERE id=?",
                    (
                        "DisabledOrPanelUnavailable" if cancel else "NotEnoughPlayers",
                        identity,
                    ),
                )
                db.execute(
                    "UPDATE slots_outbox SET status='cancelled' WHERE table_id=? AND status='pending'",
                    (identity,),
                )
                self.store.audit(
                    db,
                    "system",
                    "slots_refund",
                    {"table": identity, "chat": table["chat"]},
                )
                return
            if table["status"] == "open":
                for person in people:
                    result = [secrets.randbelow(4) for _ in range(3)]
                    db.execute(
                        "UPDATE slots_players SET result=? WHERE table_id=? AND uid=?",
                        (encode(result), identity, person["uid"]),
                    )
                db.execute(
                    "UPDATE slots_tables SET status='locked',next_edit=0 WHERE id=?",
                    (identity,),
                )
                self.store.audit(
                    db,
                    "system",
                    "slots_locked",
                    {"table": identity, "chat": table["chat"]},
                )
        # A failed credit leaves the immutable outcomes locked for safe recovery.
        with self.store.tx() as db:
            table = db.execute(
                "SELECT * FROM slots_tables WHERE id=?", (identity,)
            ).fetchone()
            if table["status"] == "locked":
                people = db.execute(
                    "SELECT * FROM slots_players WHERE table_id=? AND status='active' ORDER BY ordinal",
                    (identity,),
                ).fetchall()
                results = [json.loads(p["result"]) for p in people]
                best = max(map(rank, results))
                winners = [
                    i for i, result in enumerate(results) if rank(result) == best
                ]
                snapshot = json.loads(table["snapshot"])
                legacy = snapshot["version"] == "slots-1"
                pool = sum(p["stake"] for p in people)
                fee = pool // 10 if legacy else 0
                share, remainder = divmod(pool - fee, len(winners))
                for i, (person, result) in enumerate(zip(people, results)):
                    payout = (
                        (share + (winners.index(i) < remainder) if i in winners else 0)
                        if legacy
                        else person["stake"]
                        * snapshot["multipliers"][str(rank(result))]
                        // 10
                    )
                    if payout:
                        self.store.credit(
                            db,
                            f"slots:{identity}:{person['uid']}:payout",
                            person["uid"],
                            int(payout),
                            "slots_payout",
                            chat=table["chat"],
                        )
                    db.execute(
                        "UPDATE slots_players SET status='settled',result=?,payout=? WHERE table_id=? AND uid=?",
                        (encode(result), payout, identity, person["uid"]),
                    )
                    db.execute(
                        "INSERT INTO slots_outbox(table_id,seq,kind,uid) VALUES(?,?,'animation',?)",
                        (identity, i + 1, person["uid"]),
                    )
                db.execute(
                    "INSERT INTO slots_outbox(table_id,seq,kind) VALUES(?,?,'summary')",
                    (identity, len(people) + 1),
                )
                db.execute(
                    "UPDATE slots_tables SET status='settled',pool=?,fee=?,next_edit=0,error='' WHERE id=?",
                    (pool, fee, identity),
                )
            self.store.audit(
                db, "system", "slots_finish", {"table": identity, "chat": table["chat"]}
            )

    def render(self, identity, summary=False):
        """Render a public table or final summary from saved values only.

        Args:
            identity: Persisted table.
            summary: Whether this is the separate, temporary summary message.
        """
        t = self.store.db.execute(
            "SELECT * FROM slots_tables WHERE id=?", (identity,)
        ).fetchone()
        if json.loads(t["snapshot"])["version"] in {"slots-3", "slots-4", "slots-5"}:
            from .slots_pool import render

            return render(self, t, summary)
        people = self.store.db.execute(
            "SELECT * FROM slots_players WHERE table_id=? AND status!='withdrawn' ORDER BY ordinal",
            (identity,),
        ).fetchall()
        title = f"<b>🎰 第{identity}桌 · 老虎机PvP</b>\n"
        rows = []
        snapshot = json.loads(t["snapshot"])
        legacy = snapshot["version"] == "slots-1"
        if t["status"] == "open":
            left = max(0, int(t["deadline"] - self.store.clock()))
            body = f"\n每人自主选择投入 · 已报名 <b>{len(people)}/10人</b>\n⏳ 剩余 <b>{left}秒</b>\n"
            body += (
                "<blockquote expandable>"
                + "\n".join(
                    f'<a href="tg://user?id={p["uid"]}">{escape(p["label"])}</a> · {p["stake"]}积分'
                    for p in people
                )
                + "</blockquote>\n不足2人全额退款；散牌0倍、对子1倍、三连6.2倍。"
            )
            rows = [
                [
                    Button("🎟 100积分", callback_data=f"sl:t:{identity}:100"),
                    Button("🎟 300积分", callback_data=f"sl:t:{identity}:300"),
                ],
                [
                    Button("🎟 800积分", callback_data=f"sl:t:{identity}:800"),
                    Button("🎟 1500积分", callback_data=f"sl:t:{identity}:1500"),
                    Button("🎟 2000积分", callback_data=f"sl:t:{identity}:2000"),
                ],
                [
                    Button("↩️ 退出退款", callback_data=f"sl:t:{identity}:leave"),
                ],
                [Button("📖 规则", callback_data=f"sl:t:{identity}:rules")],
            ]
            if legacy:
                body = body.replace(
                    "每人自主选择投入", f"旧版同档 {t['stake']}积分"
                ).replace(
                    "散牌0倍、对子1倍、三连6.2倍。",
                    "旧版奖池结算：最高牌型平分90%奖池。",
                )
                rows = [
                    [
                        Button("加入原档位", callback_data=f"sl:t:{identity}:join"),
                        Button("退出退款", callback_data=f"sl:t:{identity}:leave"),
                    ]
                ]
        elif t["status"] == "locked":
            body = "\n🔒 <b>报名已锁定，正在结算。</b>\n水果结果已保存，不会重新抽取。"
        elif t["status"] == "cancelled":
            body = "\n↩️ <b>本桌已取消，托管积分全额退还。</b>\n" + (
                "人数不足2人。"
                if t["error"] == "NotEnoughPlayers"
                else "功能关闭或开桌面板未能确认送达。"
            )
        else:
            body = f"\n🏁 <b>已结算</b> · {len(people)}人\n各自按投入和牌型返还，不互相分奖池。\n"
            if legacy:
                body = f"\n🏁 <b>旧版奖池 · 已结算</b>\n奖池 {t['pool']} · 扣除 {t['fee']} · 派发 {t['pool'] - t['fee']}积分\n"
            else:
                body += f"总投入 {sum(p['stake'] for p in people)} · 总返还 <b>{sum(p['payout'] for p in people)}积分</b>\n不额外抽水。"
            details = []
            ranked = sorted(people, key=lambda p: -rank(json.loads(p["result"])))
            previous, place = None, 0
            for index, p in enumerate(ranked, 1):
                result = json.loads(p["result"])
                tier = rank(result)
                if tier != previous:
                    place = index
                previous = tier
                details.append(
                    f'<b>第{place}名</b> · <a href="tg://user?id={p["uid"]}">{escape(p["label"])}</a>\n'
                    + "".join(SYMBOLS[n] for n in result)
                    + f" · {NAMES[tier]}\n投入 {p['stake']} · 返还 {p['payout']} · 净增减 {p['payout'] - p['stake']:+d}"
                    + (
                        ""
                        if legacy
                        else f"\n倍率 {snapshot['multipliers'][str(tier)] / 10:g}倍（含本金）"
                    )
                )
            body += (
                "\n\n🏅 <b>本桌排名</b> · 按牌型，同牌型并列\n"
                "<blockquote expandable>" + "\n\n".join(details) + "</blockquote>"
            )
            if summary:
                body += "\n⏱ 本条及已送达开奖动画将在60秒后撤回。"
        return title + body, InlineKeyboardMarkup(rows)

    async def open(self, update):
        """Publish one owner-bound stake selector without debiting points.

        Args:
            update: New group message or public hub callback.
        """
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        if update.effective_chat.type != "supergroup" or update.effective_user.is_bot:
            raise Rejected("请在已启用的超级群使用老虎机。")
        if not update.callback_query and (
            getattr(update.message, "forward_origin", None)
            or getattr(update.message, "sender_chat", None)
            or not 0 <= self.store.clock() - update.message.date.timestamp() <= 60
        ):
            raise Rejected("请发送新的普通文字消息打开老虎机。")
        self.check(chat)
        reason = blocker(self.store, chat, "slots", True, rollout=True)
        if reason:
            raise Rejected(reason)
        await self.runtime.community.member(uid, chat, require_moderation=False)
        self.check(chat)
        reason = blocker(self.store, chat, "slots", True, rollout=True)
        if reason:
            raise Rejected(reason)
        source = (
            "c:" + update.callback_query.id
            if update.callback_query
            else "m:" + str(update.message.message_id)
        )
        identity = secrets.token_hex(6)
        if not self.store.db.execute(
            "INSERT OR IGNORE INTO slots_menus VALUES(?,?,?,?,NULL,'sending',?)",
            (identity, chat, uid, source, self.store.clock() + 600),
        ).rowcount:
            return
        buttons = [
            Button(f"🎰 {s}积分", callback_data=f"sl:m:{identity}:{s}")
            for s in group_stakes(self.store, chat)
        ]
        try:
            sent = await self.runtime.bot.send_message(
                chat_id=chat,
                parse_mode="HTML",
                text="<b>🎰 老虎机PvP</b>\n选择档位即开桌并扣分托管。\n<blockquote expandable>"
                + RULES
                + "</blockquote>",
                reply_markup=InlineKeyboardMarkup(
                    [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
                ),
            )
            if type(sent.message_id) is not int or sent.message_id <= 0:
                raise ValueError("MissingMessageId")
            self.store.db.execute(
                "UPDATE slots_menus SET message=?,status='active' WHERE id=?",
                (sent.message_id, identity),
            )
            self.idle.touch(chat, sent.message_id, "slots")
        except BaseException:
            self.store.db.execute(
                "UPDATE slots_menus SET status='review' WHERE id=?", (identity,)
            )
            raise

    async def action(self, update):
        """Validate Telegram identity before a single atomic operation.

        Args:
            update: Slot callback query.
        """
        query = update.callback_query
        parts = str(query.data).split(":")
        if len(parts) != 4 or parts[1] not in {"m", "t"}:
            raise Rejected("无效按钮。")
        _, kind, identity, action = parts
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        if update.effective_chat.type != "supergroup" or update.effective_user.is_bot:
            raise Rejected("仅群内成员可操作。")
        if kind == "t" and action == "rules":
            table = self.store.db.execute(
                "SELECT chat,message,snapshot FROM slots_tables WHERE id=?", (identity,)
            ).fetchone()
            if not table or (table["chat"], table["message"]) != (
                chat,
                query.message.message_id,
            ):
                raise Rejected("无效桌次。")
            if json.loads(table["snapshot"]).get("version") == "slots-1":
                return (
                    "旧版奖池：同桌同额，费用10%；三连优先于对子、散牌，同分平分奖池。"
                )
            if json.loads(table["snapshot"]).get("version") == "slots-5":
                return RULES
            if json.loads(table["snapshot"]).get("version") in {"slots-3", "slots-4"}:
                return "旧版奖池：三连＞对子＞散牌，同牌型不比水果。最高同牌型免费加赛，最多追加5轮仍平局全退；唯一胜者获总池90%。单人散牌0倍、对子1倍、三连6.2倍。"
            return "50秒报名，2—10人；每人独立选择投入。散牌0倍、对子1倍、三连6.2倍，不抽水。不足2人退款。"
        version = self.check(chat)
        await self.runtime.community.member(uid, chat, require_moderation=False)
        label = (
            "@" + update.effective_user.username
            if update.effective_user.username
            else update.effective_user.full_name
        )[:60]
        if kind == "m":
            if not action.isdigit():
                raise Rejected("无效档位。")
            table = self.create(
                identity,
                uid,
                chat,
                query.message.message_id,
                int(action),
                label,
                version,
            )
            return f"第{table}桌已受理；请查看群内桌面板，同一按钮不会重复扣分。"
        return self.participate(
            identity,
            uid,
            chat,
            query.message.message_id,
            label,
            action,
            query.id,
            version,
        )

    async def loop(self):
        """Settle on time independently of slow Telegram deliveries."""
        while True:
            try:
                for row in self.store.db.execute(
                    "SELECT id,chat FROM slots_tables WHERE status IN ('open','locked') AND next_settle<=?",
                    (self.store.clock(),),
                ).fetchall():
                    try:
                        self.check(row["chat"])
                        cancel = False
                    except Rejected:
                        cancel = True
                    try:
                        self.finish(row["id"], cancel)
                    except Exception as exc:
                        self.store.db.execute(
                            "UPDATE slots_tables SET error=?,next_settle=? WHERE id=?",
                            (type(exc).__name__, self.store.clock() + 30, row["id"]),
                        )
                        self.runtime.report("slots_settle", exc)
            except Exception as exc:
                self.runtime.report("slots_loop", exc)
            await asyncio.sleep(1)

    async def delivery_loop(self):
        """Run a single paced outbox, leaving escrow and settlement independent."""
        while True:
            try:
                await self.deliver()
                await self.edit_panels()
            except Exception as exc:
                self.runtime.report("slots_delivery", exc)
            await asyncio.sleep(1)

    async def deliver(self):
        """Claim one safe send, recording unknown outcomes without retries."""
        now = self.store.clock()
        row = self.store.db.execute(
            "SELECT o.*,t.chat FROM slots_outbox o JOIN slots_tables t ON t.id=o.table_id "
            "LEFT JOIN slots_pacing p ON p.chat=t.chat "
            "WHERE o.status='pending' AND o.next<=? AND coalesce(p.next,0)<=? "
            "AND NOT EXISTS(SELECT 1 FROM slots_outbox prev WHERE prev.table_id=o.table_id "
            "AND prev.seq<o.seq AND prev.status IN ('pending','sending')) ORDER BY o.id LIMIT 1",
            (now, now),
        ).fetchone()
        if not row:
            return
        self.store.db.execute(
            "UPDATE slots_outbox SET status='sending' WHERE id=? AND status='pending'",
            (row["id"],),
        )
        try:
            kwargs = {
                "chat_id": row["chat"],
                "parse_mode": "HTML",
                "read_timeout": 15,
                "write_timeout": 20,
                "connect_timeout": 5,
            }
            if row["kind"] == "animation":
                p = self.store.db.execute(
                    "SELECT * FROM slots_players WHERE table_id=? AND uid=?",
                    (row["table_id"], row["uid"]),
                ).fetchone()
                result = json.loads(p["result"])
                outcome = self.store.db.execute(
                    "SELECT round,result,luck FROM slots_v3_rounds WHERE table_id=? AND seq=?",
                    (row["table_id"], row["seq"]),
                ).fetchone()
                if outcome:
                    result = json.loads(outcome["result"])
                key = "".join(map(str, result))
                cached = self.store.get("slots_animation_gif_v3_" + key)
                asset = cached or InputFile(
                    (
                        Path(__file__).parent / "assets" / "slots" / (key + ".gif")
                    ).read_bytes(),
                    filename=f"slots-{key}.gif",
                )
                caption = f'<b>🎰 第{row["table_id"]}桌</b>\n<a href="tg://user?id={p["uid"]}">{escape(p["label"])}</a>\n'
                caption += (
                    "".join(SYMBOLS[n] for n in result)
                    + f" · <b>{NAMES[rank(result)]}</b>\n等待全桌汇总"
                )
                table = self.store.db.execute(
                    "SELECT snapshot FROM slots_tables WHERE id=?", (row["table_id"],)
                ).fetchone()
                snapshot = json.loads(table["snapshot"])
                if snapshot["version"] in {"slots-3", "slots-4", "slots-5"}:
                    caption += f"\n{'初轮' if outcome['round'] == 0 else '免费加赛' + str(outcome['round'])} · 投入 {p['stake']}\n最终到账与净输赢见原桌面板。"
                    if snapshot["version"] in {"slots-4", "slots-5"}:
                        caption = caption.replace("见原桌面板", "见稍后的结算通知")
                    if outcome["luck"] is not None:
                        caption += (
                            f"\n🍀 幸运点 <b>{outcome['luck']:,}</b>（同牌型比点数）"
                        )
                elif snapshot["version"] != "slots-1":
                    caption += f"\n投入 {p['stake']} · {snapshot['multipliers'][str(rank(result))] / 10:g}倍\n返还 <b>{p['payout']}</b> · 净输赢 <b>{p['payout'] - p['stake']:+d}</b>"
                sent = await self.runtime.bot.send_animation(
                    animation=asset, caption=caption, **kwargs
                )
            else:
                text, markup = self.render(row["table_id"], row["kind"] == "summary")
                if (
                    row["kind"] == "summary"
                    and self.store.db.execute(
                        "SELECT 1 FROM slots_outbox WHERE table_id=? AND kind='animation' AND status IN ('review','blocked')",
                        (row["table_id"],),
                    ).fetchone()
                ):
                    text += "\n⚠️ 部分动画未能确认送达；已保存结果与积分结算不受影响。"
                sent = await self.runtime.bot.send_message(
                    text=text, reply_markup=markup, **kwargs
                )
            if type(sent.message_id) is not int or sent.message_id <= 0:
                raise ValueError("MissingMessageId")
            with self.store.tx() as db:
                db.execute(
                    "UPDATE slots_outbox SET status='sent',message=?,error='' WHERE id=?",
                    (sent.message_id, row["id"]),
                )
                db.execute(
                    "INSERT INTO slots_pacing VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET next=excluded.next",
                    (row["chat"], self.store.clock() + 1),
                )
                if row["kind"] == "panel":
                    db.execute(
                        "UPDATE slots_tables SET message=?,next_edit=0 WHERE id=?",
                        (sent.message_id, row["table_id"]),
                    )
                elif row["kind"] == "animation" and snapshot["version"] in {
                    "slots-4",
                    "slots-5",
                }:
                    # Delay the final notification after every confirmed animation.
                    db.execute(
                        "UPDATE slots_outbox SET next=? WHERE table_id=? AND kind='summary' AND status='pending'",
                        (self.store.clock() + 8, row["table_id"]),
                    )
                    if getattr(sent, "animation", None):
                        self.store.put(
                            db, "slots_animation_gif_v3_" + key, sent.animation.file_id
                        )
                elif row["kind"] == "animation":
                    if getattr(sent, "animation", None):
                        self.store.put(
                            db, "slots_animation_gif_v3_" + key, sent.animation.file_id
                        )
                    else:
                        db.execute(
                            "UPDATE slots_outbox SET error='AnimationReturnedAsDocument' WHERE id=?",
                            (row["id"],),
                        )
                else:
                    due = self.store.clock() + 60
                    table = db.execute(
                        "SELECT message,snapshot FROM slots_tables WHERE id=?",
                        (row["table_id"],),
                    ).fetchone()
                    if (
                        json.loads(table["snapshot"])["version"]
                        in {"slots-4", "slots-5"}
                        and table["message"]
                    ):
                        db.execute(
                            "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                            (row["chat"], table["message"], table["message"], due, due),
                        )
                    for msg in db.execute(
                        "SELECT message FROM slots_outbox WHERE table_id=? AND kind!='panel' AND status='sent'",
                        (row["table_id"],),
                    ).fetchall():
                        db.execute(
                            "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                            (row["chat"], msg["message"], msg["message"], due, due),
                        )
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else exc.retry_after
            )
            self.store.db.execute(
                "UPDATE slots_outbox SET status='pending',next=?,error='RetryAfter' WHERE id=?",
                (self.store.clock() + delay + 1, row["id"]),
            )
            self.store.db.execute(
                "INSERT INTO slots_pacing VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET next=excluded.next",
                (row["chat"], self.store.clock() + delay + 1),
            )
        except BaseException as exc:
            self.store.db.execute(
                "UPDATE slots_outbox SET status=?,error=? WHERE id=?",
                (
                    "blocked" if isinstance(exc, (BadRequest, Forbidden)) else "review",
                    type(exc).__name__,
                    row["id"],
                ),
            )
            if row["kind"] == "panel":
                self.finish(row["table_id"], cancel=True)
            raise

    async def edit_panels(self):
        """Refresh countdowns and final summaries; edits are safe to retry."""
        now = self.store.clock()
        rows = self.store.db.execute(
            "SELECT * FROM slots_tables WHERE message IS NOT NULL AND next_edit<=? ORDER BY next_edit LIMIT 4",
            (now,),
        ).fetchall()
        for row in rows:
            if row["status"] in {"settled", "cancelled"} and json.loads(
                row["snapshot"]
            )["version"] in {"slots-4", "slots-5"}:
                self.store.db.execute(
                    "UPDATE slots_tables SET next_edit=1e20 WHERE id=?", (row["id"],)
                )
                continue
            modern_final = (
                row["status"] in {"settled", "cancelled"}
                and json.loads(row["snapshot"])["version"] == "slots-3"
            )
            if (
                modern_final
                and self.store.db.execute(
                    "SELECT 1 FROM slots_outbox WHERE table_id=? AND kind='animation' AND status IN ('pending','sending')",
                    (row["id"],),
                ).fetchone()
            ):
                continue
            text, markup = self.render(row["id"])
            self.store.db.execute(
                "UPDATE slots_tables SET next_edit=? WHERE id=?", (now + 5, row["id"])
            )
            try:
                if text != row["rendered"]:
                    await self.runtime.bot.edit_message_text(
                        chat_id=row["chat"],
                        message_id=row["message"],
                        text=text,
                        parse_mode="HTML",
                        reply_markup=markup,
                        read_timeout=10,
                        connect_timeout=5,
                    )
                self.store.db.execute(
                    "UPDATE slots_tables SET rendered=?,next_edit=? WHERE id=?",
                    (text, now + 5 if row["status"] == "open" else 1e20, row["id"]),
                )
                if modern_final:
                    messages = [row["message"]] + [
                        r[0]
                        for r in self.store.db.execute(
                            "SELECT message FROM slots_outbox WHERE table_id=? AND kind='animation' AND status='sent' AND message IS NOT NULL",
                            (row["id"],),
                        )
                    ]
                    for message in messages:
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                            (
                                row["chat"],
                                message,
                                message,
                                self.store.clock() + 60,
                                self.store.clock() + 60,
                            ),
                        )
            except RetryAfter as exc:
                delay = (
                    exc.retry_after.total_seconds()
                    if hasattr(exc.retry_after, "total_seconds")
                    else exc.retry_after
                )
                self.store.db.execute(
                    "UPDATE slots_tables SET next_edit=? WHERE id=?",
                    (now + delay + 1, row["id"]),
                )
            except BadRequest as exc:
                self.store.db.execute(
                    "UPDATE slots_tables SET next_edit=?,error=? WHERE id=?",
                    (1e20, type(exc).__name__, row["id"]),
                )
            except Forbidden as exc:
                self.store.db.execute(
                    "UPDATE slots_tables SET next_edit=?,error=? WHERE id=?",
                    (1e20, type(exc).__name__, row["id"]),
                )
