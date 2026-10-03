"""Telegram-native three-dice point game."""

import asyncio
import json
import re
from html import escape

from telegram.error import BadRequest, RetryAfter

from .game_switches import blocker
from .rich_text import send_html
from .store import Rejected, encode

ALIASES = {
    "大单": "big_odd",
    "dd": "big_odd",
    "dad": "big_odd",
    "大双": "big_even",
    "ds": "big_even",
    "das": "big_even",
    "小单": "small_odd",
    "xd": "small_odd",
    "xid": "small_odd",
    "小双": "small_even",
    "xs": "small_even",
    "xis": "small_even",
    "大": "big",
    "da": "big",
    "小": "small",
    "x": "small",
    "xiao": "small",
    "单": "odd",
    "d": "odd",
    "dan": "odd",
    "双": "even",
    "s": "even",
    "shuang": "even",
    "豹子": "triple",
    "bz": "triple",
    "对子": "pair",
    "dz": "pair",
    "顺子": "straight",
    "sz": "straight",
}
PLAY_NAMES = {
    "big_odd": "大单",
    "big_even": "大双",
    "small_odd": "小单",
    "small_even": "小双",
    "big": "大",
    "small": "小",
    "odd": "单",
    "even": "双",
    "triple": "豹子",
    "pair": "对子",
    "straight": "顺子",
}
ODDS = {
    # Multipliers include principal and target about 95% theoretical return.
    # The four combinations do not have equal probability under three dice.
    "big_odd": 342,
    "big_even": 456,
    "small_odd": 456,
    "small_even": 342,
    "big": 195,
    "small": 195,
    "odd": 195,
    "even": 195,
    "triple": 3420,
    "straight": 855,
    "pair": 228,
    **{f"triple{i}{i}{i}": 20520 for i in range(1, 7)},
    **{
        f"sum{i}": v
        for i, v in zip(
            range(3, 19),
            (
                20520,
                6840,
                3420,
                2052,
                1368,
                977,
                820,
                760,
                760,
                820,
                977,
                1368,
                2052,
                3420,
                6840,
                20520,
            ),
        )
    },
}
CAPS = {
    key: (
        1000
        if key
        in {
            "big",
            "small",
            "odd",
            "even",
            "big_odd",
            "big_even",
            "small_odd",
            "small_even",
        }
        else 100
        if key in {"triple", "straight", "pair"}
        else 20
    )
    for key in ODDS
}
TOKEN = re.compile(
    r"(大单|大双|小单|小双|豹子|对子|顺子|shuang|xiao|"
    r"dad|das|xid|xis|dd|ds|xd|xs|dan|da|bz|dz|sz|大|小|单|双|x|d|s)"
    r"\s*([0-9]+)"
    r"|((?:和值\s*(?:1[0-8]|[3-9])|三同\s*(?:111|222|333|444|555|666)))\s+([0-9]+)"
    r"|([0-9]{1,3})\s*/\s*([0-9]+)",
    re.I,
)


def parse_partial(text):
    """Parse K3 items, skipping only well-formed out-of-range sum pairs."""
    if len(text) > 512:
        raise Rejected("快三下注最多512字符。")
    found, skipped, end = [], 0, 0
    for match in TOKEN.finditer(text):
        if text[end : match.start()].strip(" \t\r\n|"):
            raise Rejected("快三格式有误，请按“和值10 20”格式发送。")
        ordinary, stake, numbered, numbered_stake, sum_number, sum_stake = (
            match.groups()
        )
        if sum_number is not None:
            amount = int(sum_stake)
            if amount < 1:
                raise Rejected("下注金额必须是正整数。")
            value = int(sum_number)
            if not 3 <= value <= 18:
                skipped += 1
                end = match.end()
                continue
            found.append((f"sum{value}", amount))
            end = match.end()
            continue
        label, amount = (ordinary, stake) if ordinary else (numbered, numbered_stake)
        amount = int(amount)
        if amount < 1:
            raise Rejected("下注金额必须是正整数。")
        label = re.sub(r"\s+", "", label)
        key = ALIASES.get(label.lower())
        if label.startswith("和值"):
            key = "sum" + label[2:]
        elif label.startswith("三同"):
            key = "triple" + label[2:]
        if not key or key not in ODDS:
            raise Rejected("快三玩法无效。")
        found.append((key, amount))
        end = match.end()
    if not found or text[end:].strip(" \t\r\n|"):
        raise Rejected("请发送有效的快三玩法和金额。")
    if len(found) > 20:
        raise Rejected("每条快三最多20项。")
    return found, skipped


def parse(text):
    """Parse a complete K3 message and return its valid items."""
    return parse_partial(text)[0]


def play_name(play):
    """Return the concise player-facing name for one canonical K3 play."""
    if play.startswith("sum"):
        return "和值" + play[3:]
    if play.startswith("triple") and play != "triple":
        return "三同" + play[6:]
    return PLAY_NAMES.get(play, play)


class K3:
    """Own group-local rounds and sequential Telegram dice settlement."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.locks = {}
        self.network_limit = asyncio.Semaphore(4)
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS k3_groups(chat TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS k3_sessions(chat TEXT NOT NULL,uid TEXT NOT NULL,expires REAL NOT NULL,activated REAL NOT NULL,last_message INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(chat,uid));
            CREATE TABLE IF NOT EXISTS k3_rounds(
                chat TEXT NOT NULL,issue INTEGER NOT NULL,status TEXT NOT NULL,
                closes REAL NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
                message INTEGER,PRIMARY KEY(chat,issue));
            CREATE TABLE IF NOT EXISTS k3_bets(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,issue INTEGER NOT NULL,uid TEXT NOT NULL,
                play TEXT NOT NULL,amount INTEGER NOT NULL,payout INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS k3_dice(
                chat TEXT NOT NULL,issue INTEGER NOT NULL,seq INTEGER NOT NULL,
                message INTEGER,value INTEGER NOT NULL,status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,next_retry REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',PRIMARY KEY(chat,issue,seq));
            CREATE INDEX IF NOT EXISTS k3_round_due ON k3_rounds(status,closes);
            CREATE INDEX IF NOT EXISTS k3_bet_user ON k3_bets(chat,uid,created);
            CREATE INDEX IF NOT EXISTS k3_bet_round ON k3_bets(chat,issue,uid,play);
            CREATE TABLE IF NOT EXISTS k3_requests(
                chat TEXT NOT NULL,source INTEGER NOT NULL,uid TEXT NOT NULL,
                issue INTEGER NOT NULL,total INTEGER NOT NULL,balance INTEGER NOT NULL,
                PRIMARY KEY(chat,source));
            CREATE TABLE IF NOT EXISTS k3_results(
                chat TEXT NOT NULL,issue INTEGER NOT NULL,due REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',message INTEGER,error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat,issue));
            CREATE INDEX IF NOT EXISTS k3_result_due ON k3_results(status,due);
            CREATE TABLE IF NOT EXISTS k3_rule_snapshots(
                chat TEXT NOT NULL,issue INTEGER NOT NULL,value TEXT NOT NULL,PRIMARY KEY(chat,issue));
            CREATE TABLE IF NOT EXISTS k3_result_pages(
                chat TEXT NOT NULL,issue INTEGER NOT NULL,page INTEGER NOT NULL,
                text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                message INTEGER,error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat,issue,page));
            UPDATE k3_result_pages SET status='review',error='InterruptedSend' WHERE status='sending';
            UPDATE k3_results SET status='review',error='InterruptedSend' WHERE status='sending';
        """)
        columns = {
            row["name"] for row in self.store.db.execute("PRAGMA table_info(k3_dice)")
        }
        for statement in (
            (
                "attempts",
                "ALTER TABLE k3_dice ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "next_retry",
                "ALTER TABLE k3_dice ADD COLUMN next_retry REAL NOT NULL DEFAULT 0",
            ),
            (
                "error",
                "ALTER TABLE k3_dice ADD COLUMN error TEXT NOT NULL DEFAULT ''",
            ),
        ):
            if statement[0] not in columns:
                self.store.db.execute(statement[1])
        # A process interruption cannot prove that Telegram did not send the dice.
        self.store.db.execute(
            "UPDATE k3_dice SET status='review',error='InterruptedUnknown' "
            "WHERE status='sending'"
        )
        self.store.db.execute(
            "UPDATE k3_rounds SET status='review' WHERE status='closing' AND EXISTS("
            "SELECT 1 FROM k3_dice d WHERE d.chat=k3_rounds.chat "
            "AND d.issue=k3_rounds.issue AND d.status='review')"
        )

    def enabled(self, chat):
        modules = self.store.get("modules", {})
        row = self.store.db.execute(
            "SELECT enabled FROM k3_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        return bool(modules.get("game") and modules.get("k3") and row and row[0])

    def unavailable(self, chat):
        """Return the specific admission gate used by text and button entry points."""
        row = self.store.db.execute(
            "SELECT enabled FROM k3_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        return blocker(self.store, chat, "k3", bool(row and row[0]), points=False)

    def parse(self, text):
        return parse(text)

    def parse_partial(self, text):
        return parse_partial(text)

    def _panel_text(self, row, bets=None, *, paged=False):
        bets = (
            bets
            if bets is not None
            else self.store.db.execute(
                "SELECT b.uid,sum(b.amount) total,sum(b.payout) payout,"
                "coalesce(p.name,'') name,coalesce(u.username,'') username FROM k3_bets b "
                "LEFT JOIN gg_players p ON p.uid=b.uid "
                "LEFT JOIN user_labels u ON u.uid=b.uid "
                "WHERE b.chat=? AND b.issue=? GROUP BY b.uid ORDER BY b.uid",
                (row["chat"], row["issue"]),
            ).fetchall()
        )
        state = row["state"]
        try:
            state = __import__("json").loads(state or "{}")
        except Exception:
            state = {}
        status = {
            "open": "收集中",
            "closing": "封盘开奖中",
            "settled": "已结算",
            "review": "待核查",
            "refunded": "已退款",
        }.get(row["status"], row["status"])
        remain = (
            max(0, int(row["closes"] - self.store.clock()))
            if row["status"] == "open"
            else 0
        )
        lines = [f"<b>🎲 积分快三 · 第{row['issue']}期</b>", f"状态：<b>{status}</b>"]
        if row["status"] == "open":
            lines.append(f"距封盘：{remain}秒")
        if state.get("values"):
            lines.append(
                f"🎯 开奖结果：<b>{' + '.join(map(str, state['values']))} "
                f"= {state.get('sum')}</b>"
            )
        lines.append(
            f"参与人数：{len(bets)} · 总下注：{sum(r['total'] for r in bets)}积分"
        )
        if bets and row["status"] == "settled":
            details = []
            plays = {}
            for bet in self.store.db.execute(
                "SELECT uid,play,status,SUM(amount) amount,SUM(payout) payout "
                "FROM k3_bets WHERE chat=? AND issue=? GROUP BY uid,play,status ORDER BY MIN(rowid)",
                (row["chat"], row["issue"]),
            ):
                outcome = {
                    "won": "✅ 中奖",
                    "lost": "❌ 未中",
                    "refunded": "↩️ 退款",
                }.get(bet["status"], "⚠️ 待核查")
                plays.setdefault(str(bet["uid"]), []).append(
                    f"{escape(play_name(bet['play']))} · 投入{bet['amount']} · "
                    f"{outcome} · 返还<b>{bet['payout']}</b>"
                )
            ranked = sorted(
                bets,
                key=lambda item: (item["payout"] - item["total"], item["payout"]),
                reverse=True,
            )
            for index, player in enumerate(ranked, 1):
                uid = str(player["uid"])
                username = player["username"] if "username" in player.keys() else ""
                name = escape(
                    "@" + username
                    if username
                    else player["name"] or f"用户…{uid[-4:]}",
                    quote=False,
                )
                mention = f'<a href="tg://user?id={uid}">{name}</a>'
                net = player["payout"] - player["total"]
                outcome = (
                    f"净赢 <b>+{net}</b>"
                    if net > 0
                    else f"净输 <b>{-net}</b>"
                    if net < 0
                    else "<b>持平</b>"
                )
                icon = "🏆" if net > 0 else "▫️"
                details.append(
                    f"<blockquote expandable>{icon} <b>第{index}名</b> · {mention}\n"
                    f"投入 {player['total']} · 返还 <b>{player['payout']}</b> · {outcome}\n"
                    + "\n".join(plays.get(uid, ()))
                    + "</blockquote>"
                )
            winners = [
                player for player in ranked if player["payout"] - player["total"] > 0
            ]
            lines.append(
                f"🏆 本期净赢 <b>{len(winners)}</b> 人"
                if winners
                else "🏆 本期无人净赢"
            )
            lines.append("<b>本期输赢明细</b>")
            header = "\n".join(lines)
            pages, current = [], header
            for detail in details:
                # Keep every player's merged play breakdown on a single page.
                if (
                    len((current + "\n" + detail).encode("utf-16-le")) // 2 > 3700
                    and current != header
                ):
                    pages.append(current)
                    current = header
                current += "\n" + detail
            pages.append(current)
            if len(pages) > 1:
                pages = [
                    text.replace(header, header + f"\n第 {index}/{len(pages)} 页", 1)
                    for index, text in enumerate(pages, 1)
                ]
            return pages if paged else "\n\n".join(pages)
        elif bets:
            participant_labels = []
            for player in bets:
                uid = str(player["uid"])
                username = player["username"] if "username" in player.keys() else ""
                label = escape(
                    "@" + username
                    if username
                    else player["name"] or f"用户…{uid[-4:]}",
                    quote=False,
                )
                participant_labels.append(f'<a href="tg://user?id={uid}">{label}</a>')
            lines.append("参与者：" + "、".join(participant_labels))
        return ["\n".join(lines)] if paged else "\n".join(lines)

    async def panel(self, row):
        """Create or update the single public round panel; failures are reviewable."""
        row = self.store.db.execute(
            "SELECT * FROM k3_rounds WHERE chat=? AND issue=?",
            (row["chat"], row["issue"]),
        ).fetchone()
        if not row:
            return None
        if row["status"] == "settled":
            # Results have their own durable notification; never rewrite the betting panel.
            return row["message"]
        text = self._panel_text(row)
        while True:
            try:
                async with self.network_limit:
                    if row["message"]:
                        await send_html(
                            self.runtime.bot.edit_message_text,
                            chat_id=row["chat"],
                            message_id=row["message"],
                            text=text,
                        )
                        return row["message"]
                    sent = await send_html(
                        self.runtime.bot.send_message,
                        chat_id=row["chat"],
                        text=text,
                    )
                if not getattr(sent, "message_id", None):
                    raise ValueError("MissingMessageId")
                self.store.db.execute(
                    "UPDATE k3_rounds SET message=? WHERE chat=? AND issue=?",
                    (sent.message_id, row["chat"], row["issue"]),
                )
                return sent.message_id
            except RetryAfter as exc:
                await asyncio.sleep(max(1, int(exc.retry_after)))
                current = self.store.db.execute(
                    "SELECT status FROM k3_rounds WHERE chat=? AND issue=?",
                    (row["chat"], row["issue"]),
                ).fetchone()
                if not current or current["status"] not in {"open", "closing"}:
                    return None
            except BadRequest as exc:
                if row["message"] and "message is not modified" in str(exc).lower():
                    return row["message"]
                self.store.db.execute(
                    "UPDATE k3_rounds SET status='review',version=version+1 "
                    "WHERE chat=? AND issue=? AND status IN ('open','closing')",
                    (row["chat"], row["issue"]),
                )
                return None
            except Exception:
                self.store.db.execute(
                    "UPDATE k3_rounds SET status='review',version=version+1 "
                    "WHERE chat=? AND issue=? AND status IN ('open','closing')",
                    (row["chat"], row["issue"]),
                )
                return None

    def refund_round(self, chat, issue, actor, reason):
        """Refund every pending stake exactly once for an unresolved round."""
        if not str(reason).strip():
            raise Rejected("必须填写退款原因。")
        with self.store.tx() as db:
            self.store.require(actor, "game", db, chat=chat)
            row = db.execute(
                "SELECT status FROM k3_rounds WHERE chat=? AND issue=?", (chat, issue)
            ).fetchone()
            if not row:
                raise Rejected("期次不存在。")
            if row["status"] != "review":
                raise Rejected("仅待核查期次可以执行异常退款。")
            bets = db.execute(
                "SELECT id,uid,amount,status FROM k3_bets WHERE chat=? AND issue=?",
                (chat, issue),
            ).fetchall()
            for bet in bets:
                if bet["status"] == "pending":
                    self.store.credit(
                        db,
                        f"k3:{chat}:{issue}:{bet['id']}:refund",
                        bet["uid"],
                        bet["amount"],
                        "k3_refund",
                        chat=chat,
                    )
                    db.execute(
                        "UPDATE k3_bets SET status='refunded' WHERE id=?", (bet["id"],)
                    )
            db.execute(
                "UPDATE k3_rounds SET status='refunded',version=version+1 WHERE chat=? AND issue=?",
                (chat, issue),
            )
            self.store.audit(
                db,
                actor,
                "k3_refund",
                {"chat": chat, "issue": issue, "reason": str(reason)[:200]},
            )

    def activate(self, chat, uid, message_id, now):
        with self.store.tx() as db:
            latest = db.execute(
                "SELECT max(last_message) FROM ("
                "SELECT last_message FROM gt_sessions WHERE chat=? AND uid=? "
                "UNION ALL SELECT last_message FROM k3_sessions WHERE chat=? AND uid=?)",
                (chat, uid, chat, uid),
            ).fetchone()[0]
            if latest is not None and latest >= message_id:
                return False
            db.execute(
                "UPDATE gt_sessions SET expires=0,last_message=? WHERE chat=? AND uid=?",
                (message_id, chat, uid),
            )
            db.execute(
                "INSERT INTO k3_sessions(chat,uid,expires,activated,last_message) VALUES(?,?,?,?,?) "
                "ON CONFLICT(chat,uid) DO UPDATE SET expires=excluded.expires,activated=excluded.activated,last_message=excluded.last_message",
                (chat, uid, now + 1800, now, message_id),
            )
            return True

    def deactivate(self, chat, uid, message_id, now):
        self.store.db.execute(
            "UPDATE k3_sessions SET expires=0,last_message=? WHERE chat=? AND uid=? AND last_message<?",
            (message_id, chat, uid, message_id),
        )

    def place(self, chat, uid, source, items, now):
        if not self.enabled(chat):
            raise Rejected(self.unavailable(chat))
        with self.store.tx() as db:
            prior = db.execute(
                "SELECT * FROM k3_requests WHERE chat=? AND source=?", (chat, source)
            ).fetchone()
            if prior:
                if prior["uid"] != uid:
                    raise Rejected("消息身份不一致。")
                return prior["issue"], prior["total"], prior["balance"]
            session = db.execute(
                "SELECT * FROM k3_sessions WHERE chat=? AND uid=?", (chat, uid)
            ).fetchone()
            if (
                not session
                or session["expires"] <= now
                or source <= session["last_message"]
            ):
                raise Rejected("请先发送 快三 激活。")
            row = db.execute(
                "SELECT * FROM k3_rounds WHERE chat=? AND status NOT IN ('settled','refunded')",
                (chat,),
            ).fetchone()
            if row and (row["status"] != "open" or row["closes"] <= now):
                raise Rejected("本期已封盘或正在核查，未下注。")
            from .tenants import local_config

            frozen = (
                db.execute(
                    "SELECT value FROM k3_rule_snapshots WHERE chat=? AND issue=?",
                    (chat, row["issue"]),
                ).fetchone()
                if row
                else None
            )
            config = local_config(
                self.store,
                chat,
                "k3_limits",
                {"ordinary": 1000, "special": 100, "number": 20, "total": 2000},
                db,
            )
            rules = (
                json.loads(frozen[0])
                if frozen
                else {"caps": dict(CAPS), "total": 2000, "odds": dict(ODDS)}
            )
            if not row:
                rules["caps"] = {
                    play: min(
                        cap,
                        config[
                            "ordinary"
                            if cap == 1000
                            else "special"
                            if cap == 100
                            else "number"
                        ],
                    )
                    for play, cap in CAPS.items()
                }
                rules["total"] = min(2000, config["total"])
            caps, maximum = rules["caps"], rules["total"]
            totals = {}
            existing = (
                db.execute(
                    "SELECT play,sum(amount) AS amount FROM k3_bets "
                    "WHERE chat=? AND issue=? AND uid=? GROUP BY play",
                    (chat, row["issue"], uid),
                ).fetchall()
                if row
                else []
            )
            for bet in existing:
                totals[bet["play"]] = bet["amount"]
            previous = dict(totals)
            requested = {}
            for play, amount in items:
                if play not in ODDS or type(amount) is not int or amount < 1:
                    raise Rejected("快三玩法或金额无效。")
                totals[play] = totals.get(play, 0) + amount
                requested[play] = requested.get(play, 0) + amount
            total = sum(amount for _, amount in items)
            if not items:
                raise Rejected("没有有效下注项目，本条未下注、未扣分。")
            violations = []
            for play, amount in requested.items():
                if totals[play] > caps[play]:
                    used = previous.get(play, 0)
                    violations.append(
                        f"【{play_name(play)}】超限\n"
                        f"本期已下注 {used}积分 ＋ 本条再投 {amount}积分"
                        f" ＝ {totals[play]}积分\n"
                        f"该玩法本期累计上限：{caps[play]}积分；"
                        f"本期最多还能投：{max(0, caps[play] - used)}积分。"
                    )
            if sum(totals.values()) > maximum:
                used = sum(previous.values())
                violations.append(
                    "【本期所有玩法合计】超限\n"
                    f"本期已下注 {used}积分 ＋ 本条再投 {total}积分"
                    f" ＝ {sum(totals.values())}积分\n"
                    f"本期合计上限：{maximum}积分；本期合计最多还能投：{max(0, maximum - used)}积分。"
                )
            if violations:
                raise Rejected(
                    "⚠️ 本条下注未受理\n\n"
                    + "\n\n".join(violations)
                    + "\n\n限额只统计你在本群、本期的下注；同一玩法分多次发送也会累计。\n"
                    "以上单项额度与本期合计额度须同时满足。\n"
                    "本条消息中的所有项目均未下注、未扣分；此前已成功的下注不受影响。\n"
                    "请调整金额后重新发送整条下注消息。"
                )
            balance = self.store.balance(uid, chat)
            if balance < total:
                raise Rejected("积分不足，未下注。")
            if not row:
                issue = (
                    db.execute(
                        "SELECT coalesce(max(issue),0) FROM k3_rounds WHERE chat=?",
                        (chat,),
                    ).fetchone()[0]
                    or 0
                ) + 1
                db.execute(
                    "INSERT INTO k3_rounds VALUES(?,?,?,?,?,?,?)",
                    (chat, issue, "open", now + 50, "{}", 1, None),
                )
                db.execute(
                    "INSERT INTO k3_rule_snapshots(chat,issue,value) VALUES(?,?,?)",
                    (chat, issue, encode(rules)),
                )
                row = db.execute(
                    "SELECT * FROM k3_rounds WHERE chat=? AND issue=?", (chat, issue)
                ).fetchone()
            for index, (play, amount) in enumerate(items):
                op = f"k3:{chat}:{source}:{index}"
                self.store.credit(db, op, uid, -amount, "k3_stake", chat=chat)
                db.execute(
                    "INSERT INTO k3_bets VALUES(?,?,?,?,?,?,0,'pending',?)",
                    (op, chat, row["issue"], uid, play, amount, now),
                )
            remaining = self.store.balance(uid, chat)
            db.execute(
                "INSERT INTO k3_requests VALUES(?,?,?,?,?,?)",
                (chat, source, uid, row["issue"], total, remaining),
            )
            return row["issue"], total, remaining

    def settle(self, row, values):
        if len(values) != 3 or any(
            type(v) is not int or not 1 <= v <= 6 for v in values
        ):
            raise Rejected("骰子回执不完整，不能结算。")
        a, b, c = values
        total = a + b + c
        triple = a == b == c
        pair = len({a, b, c}) == 2
        straight = sorted((a, b, c)) in ([1, 2, 3], [2, 3, 4], [3, 4, 5], [4, 5, 6])
        with self.store.tx() as db:
            current = db.execute(
                "SELECT status FROM k3_rounds WHERE chat=? AND issue=?",
                (row["chat"], row["issue"]),
            ).fetchone()
            if current and current[0] == "settled":
                return
            evidence = db.execute(
                "SELECT seq,value,status,message FROM k3_dice WHERE chat=? AND issue=? ORDER BY seq",
                (row["chat"], row["issue"]),
            ).fetchall()
            if (
                not current
                or current[0] != "closing"
                or len(evidence) != 3
                or [item["seq"] for item in evidence] != [0, 1, 2]
                or [item["value"] for item in evidence] != list(values)
                or any(
                    item["status"] != "sent" or not item["message"] for item in evidence
                )
            ):
                raise Rejected("只能使用本期完整的三颗骰子回执结算。")
            db.execute(
                "UPDATE k3_rounds SET status='settled',state=?,version=version+1 WHERE chat=? AND issue=?",
                (encode({"values": values, "sum": total}), row["chat"], row["issue"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO k3_results(chat,issue,due) VALUES(?,?,?)",
                (row["chat"], row["issue"], self.store.clock() + 8),
            )
            bets = db.execute(
                "SELECT * FROM k3_bets WHERE chat=? AND issue=? AND status='pending'",
                (row["chat"], row["issue"]),
            ).fetchall()
            cumulative = {}
            frozen = db.execute(
                "SELECT value FROM k3_rule_snapshots WHERE chat=? AND issue=?",
                (row["chat"], row["issue"]),
            ).fetchone()
            odds = json.loads(frozen[0])["odds"] if frozen else ODDS
            for bet in bets:
                key = bet["play"]
                win = (
                    (key == "big" and not triple and 11 <= total <= 18)
                    or (key == "small" and not triple and 3 <= total <= 10)
                    or (key == "odd" and not triple and total % 2)
                    or (key == "even" and not triple and not total % 2)
                    or (
                        key == "big_odd"
                        and not triple
                        and 11 <= total <= 18
                        and total % 2
                    )
                    or (
                        key == "big_even"
                        and not triple
                        and 11 <= total <= 18
                        and not total % 2
                    )
                    or (
                        key == "small_odd"
                        and not triple
                        and 3 <= total <= 10
                        and total % 2
                    )
                    or (
                        key == "small_even"
                        and not triple
                        and 3 <= total <= 10
                        and not total % 2
                    )
                    or (key == "triple" and triple)
                    or (key == "pair" and pair)
                    or (key == "straight" and straight)
                    or (key == f"sum{total}")
                    or (key == f"triple{a}{b}{c}" and triple)
                )
                # Allocate rounded aggregate returns across receipts without losing
                # fractional returns when one play was submitted in several messages.
                identity = (bet["uid"], key)
                previous = cumulative.get(identity, 0)
                cumulative[identity] = previous + bet["amount"]
                payout = (
                    (
                        (previous + bet["amount"]) * odds[key] // 100
                        - previous * odds[key] // 100
                    )
                    if win
                    else 0
                )
                self.store.credit(
                    db,
                    f"{bet['id']}:payout",
                    bet["uid"],
                    payout,
                    "k3_payout",
                    chat=row["chat"],
                ) if payout else None
                db.execute(
                    "UPDATE k3_bets SET payout=?,status=? WHERE id=?",
                    (payout, "won" if win else "lost", bet["id"]),
                )

    async def loop(self):
        while True:
            try:
                rows = self.store.db.execute(
                    "SELECT * FROM k3_rounds WHERE "
                    "(status='open' AND closes<=?) OR "
                    "(status='closing' AND (NOT EXISTS("
                    "SELECT 1 FROM k3_dice d WHERE d.chat=k3_rounds.chat "
                    "AND d.issue=k3_rounds.issue AND d.status!='sent') OR EXISTS("
                    "SELECT 1 FROM k3_dice d WHERE d.chat=k3_rounds.chat "
                    "AND d.issue=k3_rounds.issue AND d.status='retry' AND d.next_retry<=?)))",
                    (self.store.clock(), self.store.clock()),
                ).fetchall()
                if rows:
                    await asyncio.gather(
                        *(self.draw(row) for row in rows), return_exceptions=True
                    )
                notices = self.store.db.execute(
                    "SELECT chat,issue FROM k3_results WHERE status='pending' AND due<=? ORDER BY due LIMIT 4",
                    (self.store.clock(),),
                ).fetchall()
                await asyncio.gather(
                    *(self.deliver_result(r["chat"], r["issue"]) for r in notices),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.runtime.report("k3_loop", exc)
            await asyncio.sleep(1)

    async def draw(self, row):
        lock = self.locks.setdefault((row["chat"], row["issue"]), asyncio.Lock())
        async with lock:
            row = self.store.db.execute(
                "SELECT * FROM k3_rounds WHERE chat=? AND issue=?",
                (row["chat"], row["issue"]),
            ).fetchone()
            if (
                not row
                or row["status"] not in {"open", "closing"}
                or self.store.clock() < row["closes"]
            ):
                return
            if row["status"] == "open":
                self.store.db.execute(
                    "UPDATE k3_rounds SET status='closing' WHERE chat=? AND issue=? "
                    "AND status='open'",
                    (row["chat"], row["issue"]),
                )
                if not await self.panel(row):
                    return
            evidence = self.store.db.execute(
                "SELECT * FROM k3_dice WHERE chat=? AND issue=? ORDER BY seq",
                (row["chat"], row["issue"]),
            ).fetchall()
            if any(item["status"] in {"sending", "review"} for item in evidence):
                self.store.db.execute(
                    "UPDATE k3_rounds SET status='review' WHERE chat=? AND issue=?",
                    (row["chat"], row["issue"]),
                )
                return
            values = [item["value"] for item in evidence if item["status"] == "sent"]
            if [item["seq"] for item in evidence if item["status"] == "sent"] != list(
                range(len(values))
            ):
                self.store.db.execute(
                    "UPDATE k3_rounds SET status='review' WHERE chat=? AND issue=?",
                    (row["chat"], row["issue"]),
                )
                return
            for seq in range(len(values), 3):
                try:
                    prior = self.store.db.execute(
                        "SELECT * FROM k3_dice WHERE chat=? AND issue=? AND seq=?",
                        (row["chat"], row["issue"], seq),
                    ).fetchone()
                    if prior and (
                        prior["status"] != "retry"
                        or prior["next_retry"] > self.store.clock()
                    ):
                        return
                    if prior:
                        self.store.db.execute(
                            "UPDATE k3_dice SET status='sending',attempts=attempts+1,"
                            "error='' WHERE chat=? AND issue=? AND seq=?",
                            (row["chat"], row["issue"], seq),
                        )
                    else:
                        self.store.db.execute(
                            "INSERT INTO k3_dice(chat,issue,seq,message,value,status,"
                            "attempts,next_retry,error) VALUES(?,?,?,NULL,0,'sending',1,0,'')",
                            (row["chat"], row["issue"], seq),
                        )
                    async with self.network_limit:
                        sent = await self.runtime.bot.send_dice(
                            chat_id=row["chat"], emoji="🎲"
                        )
                    value = sent.dice.value
                    if (
                        type(value) is not int
                        or not 1 <= value <= 6
                        or not sent.message_id
                    ):
                        raise ValueError("Invalid dice receipt")
                    self.store.db.execute(
                        "UPDATE k3_dice SET message=?,value=?,status='sent',"
                        "next_retry=0,error='' WHERE chat=? AND issue=? AND seq=?",
                        (
                            sent.message_id,
                            value,
                            row["chat"],
                            row["issue"],
                            seq,
                        ),
                    )
                    values.append(value)
                except RetryAfter as exc:
                    retry_after = max(1, int(exc.retry_after))
                    self.store.db.execute(
                        "UPDATE k3_dice SET status='retry',next_retry=?,error='RetryAfter' "
                        "WHERE chat=? AND issue=? AND seq=?",
                        (
                            self.store.clock() + retry_after,
                            row["chat"],
                            row["issue"],
                            seq,
                        ),
                    )
                    return
                except Exception as exc:
                    self.store.db.execute(
                        "UPDATE k3_dice SET status='review',error=? "
                        "WHERE chat=? AND issue=? AND seq=?",
                        (type(exc).__name__, row["chat"], row["issue"], seq),
                    )
                    self.store.db.execute(
                        "UPDATE k3_rounds SET status='review',version=version+1 "
                        "WHERE chat=? AND issue=?",
                        (row["chat"], row["issue"]),
                    )
                    return
            self.settle(row, values)

    async def deliver_result(self, chat, issue):
        """Claim and send one result after the persisted animation safety delay.

        Args:
            chat: Exact group of the settled round.
            issue: Persisted round number.
        """
        row = self.store.db.execute(
            "SELECT * FROM k3_rounds WHERE chat=? AND issue=? AND status='settled'",
            (chat, issue),
        ).fetchone()
        if (
            not row
            or not self.store.db.execute(
                "UPDATE k3_results SET status='sending' WHERE chat=? AND issue=? AND status='pending' AND due<=?",
                (chat, issue, self.store.clock()),
            ).rowcount
        ):
            return
        try:
            if not self.store.db.execute(
                "SELECT 1 FROM k3_result_pages WHERE chat=? AND issue=?", (chat, issue)
            ).fetchone():
                with self.store.tx() as db:
                    for index, text in enumerate(self._panel_text(row, paged=True), 1):
                        db.execute(
                            "INSERT INTO k3_result_pages(chat,issue,page,text) VALUES(?,?,?,?)",
                            (chat, issue, index, text),
                        )
            for page in self.store.db.execute(
                "SELECT * FROM k3_result_pages WHERE chat=? AND issue=? ORDER BY page",
                (chat, issue),
            ).fetchall():
                if page["status"] == "sent":
                    continue
                if not self.store.db.execute(
                    "UPDATE k3_result_pages SET status='sending' WHERE chat=? AND issue=? AND page=? AND status='pending'",
                    (chat, issue, page["page"]),
                ).rowcount:
                    raise ValueError("UncertainResultPage")
                async with self.network_limit:
                    sent = await send_html(
                        self.runtime.bot.send_message,
                        chat_id=chat,
                        text=page["text"],
                    )
                if (
                    type(getattr(sent, "message_id", None)) is not int
                    or sent.message_id <= 0
                ):
                    raise ValueError("MissingMessageId")
                now = self.store.clock()
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE k3_result_pages SET status='sent',message=?,error='' "
                        "WHERE chat=? AND issue=? AND page=?",
                        (sent.message_id, chat, issue, page["page"]),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                        (chat, sent.message_id, sent.message_id, now + 60, now + 60),
                    )
            now = self.store.clock()
            with self.store.tx() as db:
                db.execute(
                    "UPDATE k3_results SET status='sent',message=(SELECT message FROM k3_result_pages "
                    "WHERE chat=? AND issue=? ORDER BY page LIMIT 1),error='' WHERE chat=? AND issue=?",
                    (chat, issue, chat, issue),
                )
                messages = [row["message"]] + [
                    r[0]
                    for r in db.execute(
                        "SELECT message FROM k3_dice WHERE chat=? AND issue=? AND status='sent'",
                        (chat, issue),
                    )
                ]
                for message in messages:
                    if message:
                        db.execute(
                            "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                            (chat, message, message, now + 60, now + 60),
                        )
        except asyncio.CancelledError:
            # The claim survives shutdown; constructor makes it review-only.
            raise
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else exc.retry_after
            )
            self.store.db.execute(
                "UPDATE k3_result_pages SET status='pending',error='RetryAfter' "
                "WHERE chat=? AND issue=? AND status='sending'",
                (chat, issue),
            )
            self.store.db.execute(
                "UPDATE k3_results SET status='pending',due=?,error='RetryAfter' WHERE chat=? AND issue=?",
                (self.store.clock() + max(1, delay), chat, issue),
            )
        except Exception as exc:
            self.store.db.execute(
                "UPDATE k3_result_pages SET status='review',error=? "
                "WHERE chat=? AND issue=? AND status='sending'",
                (type(exc).__name__, chat, issue),
            )
            self.store.db.execute(
                "UPDATE k3_results SET status='review',error=? WHERE chat=? AND issue=?",
                (type(exc).__name__, chat, issue),
            )
