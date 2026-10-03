"""Public navigation and durable, point-free single-message challenge panels."""

import asyncio
import json
import re
from datetime import datetime, timezone
from html import escape
from types import SimpleNamespace

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .duel import ACTIVE, NOTICE
from .game_switches import blocker
from .grant_target import resolve
from .panel_idle import PanelIdle
from .rules import PLAY_NAMES
from .store import Rejected, encode

MENU = [
    ("🎡 积分转盘", "wheel"),
    ("🎰老虎机PvP", "slots"),
    ("💣 扫雷接龙", "mines"),
    ("🎲 积分快三", "k3"),
    ("🎲 加拿大28", "activate"),
    ("🤝 双人对赌", "duel"),
]


class PlayCenter:
    """Keep public panels isolated from personal queries and wallet operations."""

    def __init__(self, text):
        self.text, self.store, self.runtime = text, text.store, text.runtime
        self.idle = PanelIdle(self.store)
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS game_panels(
                id INTEGER PRIMARY KEY,chat TEXT NOT NULL,source INTEGER NOT NULL,
                kind TEXT NOT NULL,duel INTEGER UNIQUE,message INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',rendered TEXT NOT NULL DEFAULT '',
                page INTEGER NOT NULL DEFAULT 0,next REAL NOT NULL DEFAULT 0,
                created REAL NOT NULL,error TEXT NOT NULL DEFAULT '',
                UNIQUE(chat,source));
            CREATE INDEX IF NOT EXISTS game_panels_due ON game_panels(status,next);
            CREATE INDEX IF NOT EXISTS game_panels_retention ON game_panels(status,created);
            CREATE TABLE IF NOT EXISTS game_panel_clicks(
                id TEXT PRIMARY KEY,source INTEGER NOT NULL);
            UPDATE game_panels SET status='review',error='InterruptedSend'
                WHERE status='sending';
        """)
        self.participants = {
            (row["chat"], row[key])
            for row in self.store.db.execute(
                f"SELECT d.chat,d.first,d.second FROM duels d JOIN game_panels p ON p.duel=d.id WHERE d.status IN {ACTIVE}"
            )
            for key in ("first", "second")
        }

    async def message(self, update):
        """Create a navigation or challenge panel from a verified group message.

        Args:
            update: New plain supergroup user message.

        Returns:
            Whether this panel workflow consumed the message.
        """
        word = update.message.text.strip()
        hub = word in {
            "玩法",
            "游戏中心",
            "玩法中心",
            "玩法大全",
            "🎮 玩法大全",
        } or word.lower() in {
            "/games",
            "/games@" + self.runtime.bot.username.lower(),
        }
        match = re.fullmatch(
            r"(?:对赌|dd)(?:\s+|(?=@))(?:@([A-Za-z][A-Za-z0-9_]{3,31})\s+)?(.+)",
            word,
            re.I,
        )
        # Preserve active legacy workflows; only explicit invitation targets start panels.
        reply = getattr(update.message, "reply_to_message", None)
        person = getattr(reply, "from_user", None)
        if not hub and (not match or (not match[1] and not person)):
            return False
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        if not 0 <= self.store.clock() - update.message.date.timestamp() <= 60:
            return True
        if not self.store.get("modules", {}).get("game"):
            return True
        if not hub and not self.store.get("modules", {}).get("duel", True):
            return True
        if (
            not self.store.db.execute(
                "SELECT 1 FROM duel_groups WHERE chat=? AND enabled=1", (chat,)
            ).fetchone()
            and not hub
        ):
            return True
        await self.runtime.community.member(uid, chat, require_moderation=False)
        if hub:
            from .availability import refresh

            await refresh(self.runtime, chat)
        if self.store.db.execute(
            "SELECT 1 FROM game_panels WHERE chat=? AND source=?",
            (chat, update.message.message_id),
        ).fetchone():
            return True
        target, name, terms = None, "", ""
        if not hub:
            terms = match[2].strip()
            if not 1 <= len(terms) <= 100 or re.search(
                r"现金|转账|红包|人民币|元|块|钱|usdt|cny|trx|btc|充值|兑换|财物|¥|￥|\$",
                terms,
                re.I,
            ):
                raise Rejected(
                    "彩头限1—100字非金钱、非财物互动，例如：dd 输的人唱一首歌"
                )
            if match[1]:
                found = await resolve(self.runtime, "@" + match[1])
                target, name = found["target"], "@" + found["target_username"]
            else:
                if person.is_bot or getattr(reply, "sender_chat", None):
                    raise Rejected("请邀请本群普通用户。")
                target = str(person.id)
                name = (
                    "@" + person.username
                    if getattr(person, "username", None)
                    else f"玩家{target}"
                )
            if target == uid:
                raise Rejected("不能邀请自己。")
            member = await self.runtime.community.member(
                target, chat, require_moderation=False
            )
            if getattr(getattr(member, "user", None), "is_bot", False):
                raise Rejected("不能邀请机器人。")
        with self.store.tx() as db:
            if not self.store.get("modules", {}, db).get("game"):
                return True
            if not hub and not self.store.get("modules", {}, db).get("duel", True):
                return True
            if (
                not hub
                and not db.execute(
                    "SELECT 1 FROM duel_groups WHERE chat=? AND enabled=1", (chat,)
                ).fetchone()
            ):
                return True
            if db.execute(
                "SELECT 1 FROM game_panels WHERE chat=? AND source=?",
                (chat, update.message.message_id),
            ).fetchone():
                return True
            if not db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (chat,)
            ).fetchone():
                return True
            duel = None
            if hub:
                if db.execute(
                    "SELECT 1 FROM game_panels WHERE chat=? AND kind='hub' AND created>?",
                    (chat, self.store.clock() - 15),
                ).fetchone():
                    return True
            else:
                if db.execute(
                    f"SELECT 1 FROM duels WHERE chat=? AND status IN {ACTIVE} AND (status='pending' OR expires>?) "
                    "AND (first IN (?,?) OR second IN (?,?))",
                    (chat, self.store.clock(), uid, target, uid, target),
                ).fetchone():
                    raise Rejected("你或对方已有进行中的挑战，请先完成或取消。")
                if db.execute(
                    "SELECT 1 FROM duels WHERE chat=? AND first=? AND created>?",
                    (chat, uid, self.store.clock() - 60),
                ).fetchone():
                    raise Rejected("邀请间隔至少60秒。")
                username = getattr(update.effective_user, "username", None)
                duel = db.execute(
                    "INSERT INTO duels(chat,first,second,first_name,second_name,status,expires,created,terms1,terms2,last_source) "
                    "VALUES(?,?,?,?,?,'invited',?,?,?,?,?)",
                    (
                        chat,
                        uid,
                        target,
                        "@" + username if username else f"玩家{uid}",
                        name,
                        self.store.clock() + 120,
                        self.store.clock(),
                        terms,
                        terms,
                        update.message.message_id,
                    ),
                ).lastrowid
            db.execute(
                "INSERT INTO game_panels(chat,source,kind,duel,created) VALUES(?,?,?,?,?)",
                (
                    chat,
                    update.message.message_id,
                    "hub" if hub else "duel",
                    duel,
                    self.store.clock(),
                ),
            )
        if target:
            self.participants.update(((chat, uid), (chat, target)))
        return True

    async def action(self, update):
        """Authorize public callbacks by actor, group, message and current state.

        Args:
            update: Telegram callback with an untrusted compact payload.
        """
        query = update.callback_query
        pieces = str(query.data).split(":")
        if len(pieces) != 4 or not pieces[1].isdigit() or not pieces[2].isdigit():
            raise Rejected("无效按钮。")
        panel_id, action = int(pieces[1]), pieces[3]
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        if update.effective_chat.type != "supergroup":
            raise Rejected("仅限本群使用。")
        await self.runtime.community.member(uid, chat, require_moderation=False)
        panel = self.store.db.execute(
            "SELECT * FROM game_panels WHERE id=? AND chat=? AND message=?",
            (panel_id, chat, query.message.message_id),
        ).fetchone()
        if not panel or panel["status"] not in {"active", "done"}:
            raise Rejected("面板已失效，请发送“玩法”重新打开。")
        if int(pieces[2]) != (panel["duel"] or 0):
            raise Rejected("这是上一局的按钮，请使用当前面板。")
        if not self.store.get("modules", {}).get("game"):
            raise Rejected("玩法功能已关闭。")
        if panel["kind"] == "hub":
            from .availability import refresh, snapshot

            await refresh(self.runtime, chat)

            if action in {key for _, key in MENU} and not snapshot(
                self.runtime, chat
            ).get(action, False):
                raise Rejected("本群此玩法当前不可用，请重新打开玩法大全。")
            if self.store.clock() - panel["created"] > 86400:
                raise Rejected("面板已过期，请发送“玩法”重新打开。")
            self.idle.touch(chat, panel["message"], "hub")
            if action == "duel":
                if (
                    not self.store.get("modules", {}).get("duel", True)
                    or not self.store.db.execute(
                        "SELECT 1 FROM duel_groups WHERE chat=? AND enabled=1", (chat,)
                    ).fetchone()
                ):
                    local = self.store.db.execute(
                        "SELECT enabled FROM duel_groups WHERE chat=?", (chat,)
                    ).fetchone()
                    raise Rejected(
                        blocker(
                            self.store,
                            chat,
                            "duel",
                            bool(local and local[0]),
                            points=False,
                        )
                    )
                return (
                    "回复对方消息发送：dd 输的人唱一首歌\n或：对赌 @用户名 自定义彩头"
                )
            if action == "wheel":
                await self.runtime.wheel.open(update)
                return "已打开你的积分转盘。"
            if action == "slots":
                await self.runtime.slots.open(update)
                return "已打开你的老虎机开桌面板。"
            if action == "mines":
                await self.runtime.mines.open(update)
                return "已打开你的扫雷开桌面板。"
            if action == "k3":
                if not self.runtime.k3.enabled(chat):
                    raise Rejected(self.runtime.k3.unavailable(chat))
                with self.store.tx() as db:
                    if db.execute(
                        "SELECT 1 FROM game_panel_clicks WHERE id=?", (query.id,)
                    ).fetchone():
                        session = db.execute(
                            "SELECT expires FROM k3_sessions WHERE chat=? AND uid=?",
                            (chat, uid),
                        ).fetchone()
                        remaining = max(
                            0,
                            int(
                                (session["expires"] if session else 0)
                                - self.store.clock()
                            ),
                        )
                        return (
                            "🎲 积分快三已激活 · "
                            f"剩余 {remaining // 60:02d}:{remaining % 60:02d}"
                        )
                    db.execute("INSERT INTO game_panel_clicks VALUES(?,0)", (query.id,))
                    now = self.store.clock()
                    latest = db.execute(
                        "SELECT max(last_message) FROM ("
                        "SELECT last_message FROM gt_sessions WHERE chat=? AND uid=? "
                        "UNION ALL SELECT last_message FROM k3_sessions WHERE chat=? AND uid=?)",
                        (chat, uid, chat, uid),
                    ).fetchone()[0]
                    marker = max(int(latest or 0), int(query.message.message_id))
                    db.execute(
                        "UPDATE gt_sessions SET expires=0,last_message=? WHERE chat=? AND uid=?",
                        (marker, chat, uid),
                    )
                    db.execute(
                        "INSERT INTO k3_sessions(chat,uid,expires,activated,last_message) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(chat,uid) DO UPDATE SET "
                        "expires=excluded.expires,activated=excluded.activated,last_message=excluded.last_message",
                        (chat, uid, now + 1800, now, marker),
                    )
                return (
                    "🎲 积分快三已激活 30 分钟\n"
                    "直接发送：大100 单50\n"
                    "发送“取消”或“退出”可结束当前下注会话。"
                )
            if action == "rules":
                await self.text.group.action(update, {"action": "play"})
                return "已打开本人可见规则。"
            # Old personal-query buttons remain safe until their panel expires.
            if action not in {key for _, key in MENU} | {
                "points",
                "checkin",
                "history",
                "flow",
                "profit",
            }:
                raise Rejected("无效导航。")
            if action in {"points", "checkin"} and not self.store.get(
                "modules", {}
            ).get("points"):
                raise Rejected("积分功能已关闭。")
            with self.store.tx() as db:
                if db.execute(
                    "SELECT 1 FROM game_panel_clicks WHERE id=?", (query.id,)
                ).fetchone():
                    return "已处理，请勿重复。"
                if action == "activate":
                    db.execute("INSERT INTO game_panel_clicks VALUES(?,0)", (query.id,))
                    now = self.store.clock()
                    latest = db.execute(
                        "SELECT max(last_message) FROM ("
                        "SELECT last_message FROM gt_sessions WHERE chat=? AND uid=? "
                        "UNION ALL SELECT last_message FROM k3_sessions WHERE chat=? AND uid=?)",
                        (chat, uid, chat, uid),
                    ).fetchone()[0]
                    marker = max(int(latest or 0), int(query.message.message_id))
                    db.execute(
                        "UPDATE k3_sessions SET expires=0,last_message=? WHERE chat=? AND uid=?",
                        (marker, chat, uid),
                    )
                    db.execute(
                        "INSERT INTO gt_sessions(chat,uid,expires,activated,last_message) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(chat,uid) DO UPDATE SET "
                        "expires=excluded.expires,activated=excluded.activated,last_message=excluded.last_message",
                        (chat, uid, now + 1800, now, marker),
                    )
                    _, feedback = self.text.activation_text(chat, uid)
                    return feedback
            event = SimpleNamespace(
                effective_user=update.effective_user,
                effective_chat=update.effective_chat,
                message=SimpleNamespace(
                    message_id=0,
                    date=datetime.fromtimestamp(self.store.clock(), timezone.utc),
                ),
            )
            await self.text.query_points(event, action, callback_id=query.id)
            return "已查询；结果只包含你在本群的数据，冷却期间不重复发送。"
        duel = self.store.db.execute(
            "SELECT * FROM duels WHERE id=?", (panel["duel"],)
        ).fetchone()
        if uid not in {duel["first"], duel["second"]}:
            raise Rejected("只有本次挑战双方可以操作。")
        for member_uid in (duel["first"], duel["second"]):
            await self.runtime.community.member(
                member_uid, chat, require_moderation=False
            )
        with self.store.tx() as db:
            duel = db.execute(
                "SELECT * FROM duels WHERE id=?", (panel["duel"],)
            ).fetchone()
            if action != "cancel" and (
                not self.store.get("modules", {}, db).get("duel", True)
                or not db.execute(
                    "SELECT 1 FROM duel_groups WHERE chat=? AND enabled=1", (chat,)
                ).fetchone()
            ):
                raise Rejected("本群对赌已停用；已锁定挑战仍会结算。")
            if not self.store.get("modules", {}, db).get("game"):
                raise Rejected("玩法功能已关闭。")
            if not db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (chat,)
            ).fetchone():
                raise Rejected("本群已停用。")
            if (
                duel["status"] in {"invited", "choosing"}
                and duel["expires"] <= self.store.clock()
            ):
                raise Rejected("已超时，等待面板更新。")
            if action == "again":
                if duel["status"] not in {"settled", "expired", "cancelled"}:
                    raise Rejected("本局尚未结束。")
                deletion = db.execute(
                    "SELECT status FROM gt_delete WHERE chat=? AND message=?",
                    (chat, panel["message"]),
                ).fetchone()
                if deletion and deletion["status"] != "pending":
                    raise Rejected("面板正在撤回，请重新发送对赌邀请。")
                if db.execute(
                    f"SELECT 1 FROM duels WHERE chat=? AND status IN {ACTIVE} "
                    "AND (status='pending' OR expires>?) AND (first IN (?,?) OR second IN (?,?))",
                    (
                        chat,
                        self.store.clock(),
                        duel["first"],
                        duel["second"],
                        duel["first"],
                        duel["second"],
                    ),
                ).fetchone():
                    raise Rejected("一方已有其他进行中的挑战。")
                target = duel["second"] if uid == duel["first"] else duel["first"]
                first_name = (
                    duel["first_name"] if uid == duel["first"] else duel["second_name"]
                )
                second_name = (
                    duel["second_name"] if uid == duel["first"] else duel["first_name"]
                )
                new_id = db.execute(
                    "INSERT INTO duels(chat,first,second,first_name,second_name,status,expires,created,terms1,terms2) "
                    "VALUES(?,?,?,?,?,'invited',?,?,?,?)",
                    (
                        chat,
                        uid,
                        target,
                        first_name,
                        second_name,
                        self.store.clock() + 120,
                        self.store.clock(),
                        duel["terms1"],
                        duel["terms1"],
                    ),
                ).lastrowid
                db.execute(
                    "DELETE FROM gt_delete WHERE chat=? AND message=? AND status='pending'",
                    (chat, panel["message"]),
                )
                db.execute(
                    "UPDATE game_panels SET duel=?,status='active',page=0 WHERE id=?",
                    (new_id, panel_id),
                )
                self.participants.update(((chat, uid), (chat, target)))
            elif action == "accept":
                if uid != duel["second"] or duel["status"] != "invited":
                    raise Rejected("仅受邀人可接受当前邀请。")
                room = self.runtime.game.rooms(db)[self.text.group.group_room(chat)]
                if not room["enabled"]:
                    raise Rejected("当前倍率已停用。")
                issue, closes = self.runtime.game.current(db)
                db.execute(
                    "UPDATE duels SET status='choosing',snapshot=?,issue=?,expires=? WHERE id=?",
                    (
                        encode(room),
                        issue,
                        min(self.store.clock() + 120, closes),
                        duel["id"],
                    ),
                )
            elif action == "cancel":
                if duel["status"] not in {"invited", "choosing"}:
                    raise Rejected("已锁定或已结束，不能取消。")
                db.execute(
                    "UPDATE duels SET status='cancelled' WHERE id=?", (duel["id"],)
                )
            elif action.startswith("page"):
                if duel["status"] != "choosing" or not action[4:].isdigit():
                    raise Rejected("不能翻页。")
                db.execute(
                    "UPDATE game_panels SET page=? WHERE id=?",
                    (min(int(action[4:]), 10), panel_id),
                )
            elif action.startswith("pick"):
                if duel["status"] != "choosing" or not action[4:].isdigit():
                    raise Rejected("请先接受邀请；锁定后不能修改。")
                issue, _ = self.runtime.game.current(db)
                if issue != duel["issue"]:
                    raise Rejected("已跨期，本次不能继续选择。")
                room = json.loads(duel["snapshot"])
                options = [
                    p for p in room["odds"] if p in PLAY_NAMES and room["odds"][p] > 0
                ]
                index = int(action[4:])
                if index >= len(options):
                    raise Rejected("玩法无效。")
                side = 1 if uid == duel["first"] else 2
                if duel[f"play{side}"]:
                    raise Rejected("你的玩法已锁定。")
                db.execute(
                    f"UPDATE duels SET play{side}=? WHERE id=?",
                    (options[index], duel["id"]),
                )
                if duel["play2" if side == 1 else "play1"]:
                    db.execute(
                        "UPDATE duels SET status='pending' WHERE id=?", (duel["id"],)
                    )
            else:
                raise Rejected("操作无效，请重新发起新的挑战。")
            db.execute("UPDATE game_panels SET next=0 WHERE id=?", (panel_id,))
        return "已更新，稍候查看面板。"

    def render(self, panel):
        """Build escaped panel HTML and bound compact callback buttons.

        Args:
            panel: Persisted panel row.

        Returns:
            HTML body, inline keyboard, and whether the panel is terminal.
        """
        key = f"{panel['id']}:{panel['duel'] or 0}"
        if panel["kind"] == "hub":
            from .availability import snapshot

            available = snapshot(self.runtime, panel["chat"])
            buttons = [
                Button(label, callback_data=f"pc:{key}:{action}")
                for label, action in MENU
                if available[action]
            ]
            descriptions = {
                "activate": "加拿大28：进入房间后文字下注",
                "k3": "积分快三：三颗原生骰子开奖，与加拿大28房间互斥",
                "duel": "双人对赌：自定义彩头，不扣积分",
                "wheel": "积分转盘：选择投入，按抽中倍率返还",
                "slots": "老虎机PvP：同桌同额；多人赢家获总池90%，单人按倍率返还",
                "mines": "扫雷接龙：各选投入，最后幸存者获总池90%",
            }
            details = "\n".join(
                descriptions[action] for _, action in MENU if available[action]
            )
            return (
                "<b>🎮 大海传媒 · 玩法大全</b>\n\n"
                + (details if buttons else "本群暂无可用玩法，请联系本群管理员。")
                + "\n"
                "<blockquote expandable>积分按当前群独立。\n积分、签到及记录查询使用底部群键盘；点击输入框旁的键盘图标收起或展开。\n"
                "对赌发起：回复对方发送 dd 自定义彩头。\n"
                "选择面板闲置30秒撤回，有效点击重新计时；功能须由本群管理员开启。</blockquote>",
                InlineKeyboardMarkup(
                    [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
                ),
                False,
            )
        duel = self.store.db.execute(
            "SELECT * FROM duels WHERE id=?", (panel["duel"],)
        ).fetchone()
        labels = [
            f'<a href="tg://user?id={duel[uid]}">{escape(duel[name])}</a>'
            for uid, name in (("first", "first_name"), ("second", "second_name"))
        ]
        title = f"<b>🤝 双人挑战 #{duel['id']}</b>"
        body = f"{title}\n👥 {' vs '.join(labels)}\n<blockquote expandable>🎁 {escape(duel['terms1'])}\n{escape(NOTICE)}</blockquote>"
        rows = []
        state = duel["status"]
        if state == "invited":
            body += "\n⏳ 等待对方接受（120秒）；接受即同意上述彩头。"
            rows = [
                [
                    Button("✅ 接受并选玩法", callback_data=f"pc:{key}:accept"),
                    Button("✖️ 拒绝 / 取消", callback_data=f"pc:{key}:cancel"),
                ]
            ]
        elif state in {"choosing", "pending", "settled"}:
            room = json.loads(duel["snapshot"])
            body += f"\n<b>第 {duel['issue']} 期</b>"
            for side in (1, 2):
                play = duel[f"play{side}"]
                body += (
                    f"\n{labels[side - 1]}：{escape(PLAY_NAMES.get(play, '待选玩法'))}"
                )
                if play:
                    body += f" · {room['odds'][play] / 100:g}倍"
            if state == "choosing":
                body += "\n🎯 双方各选一项；点击即锁定，不扣积分。"
                options = [
                    p for p in room["odds"] if p in PLAY_NAMES and room["odds"][p] > 0
                ]
                page = min(panel["page"], (len(options) - 1) // 12)
                buttons = [
                    Button(PLAY_NAMES[p], callback_data=f"pc:{key}:pick{i}")
                    for i, p in enumerate(options)
                    if page * 12 <= i < (page + 1) * 12
                ]
                rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
                nav = []
                if page:
                    nav.append(
                        Button("上一页", callback_data=f"pc:{key}:page{page - 1}")
                    )
                if (page + 1) * 12 < len(options):
                    nav.append(
                        Button("下一页", callback_data=f"pc:{key}:page{page + 1}")
                    )
                nav.append(Button("取消", callback_data=f"pc:{key}:cancel"))
                rows.append(nav)
            elif state == "pending":
                body += "\n<b>🔒 双方已锁定 · 等待开奖</b>"
            else:
                from .duel import outcome

                result = json.loads(duel["result"])
                winner, hits = outcome(
                    room, duel["play1"], duel["play2"], result["balls"]
                )
                body += (
                    "\n🎲 开奖："
                    + " + ".join(map(str, result["balls"]))
                    + f" = {sum(result['balls'])}"
                )
                body += f"\n{labels[0]}：{'✅ 猜中' if hits[0] else '❌ 未中'}\n{labels[1]}：{'✅ 猜中' if hits[1] else '❌ 未中'}"
                body += (
                    "\n<b>🏆 "
                    + (
                        "平局"
                        if not winner
                        else escape(
                            duel["first_name" if winner == 1 else "second_name"]
                        )
                        + " 胜"
                    )
                    + "</b>"
                )
                if not any(hits):
                    body += "\n双方未中，本局作废。"
                body += "\n再来一局：回复对方发送 dd 自定义彩头；需对方重新接受。"
        else:
            body += "\n<b>本局已取消或超时 · 未扣积分</b>"
        if state in {"settled", "cancelled", "expired"}:
            rows = [[Button("🔄 同彩头再来一局", callback_data=f"pc:{key}:again")]]
            body += "\n⏱️ 本面板30秒后撤回；再来一局须双方重新接受与选择。"
        return (
            body,
            InlineKeyboardMarkup(rows),
            state not in {"invited", "choosing", "pending"},
        )

    async def loop(self):
        """Persist uncertain initial sends; retry idempotent edits independently."""
        while True:
            try:
                if self.runtime.application and self.runtime.application.running:
                    await self.tick()
            except Exception as exc:
                self.runtime.report("game_panels", exc)
            await asyncio.sleep(1)

    async def tick(self):
        """Send or edit bounded panels; schedule deletion only after final receipt."""
        self.idle.expire()
        rows = self.store.db.execute(
            "SELECT p.* FROM game_panels p JOIN mod_groups g ON g.chat=p.chat AND g.enabled=1 "
            "WHERE p.status IN ('pending','active') AND p.next<=? ORDER BY p.next,p.id LIMIT 10",
            (self.store.clock(),),
        ).fetchall()
        for panel in rows:
            html, markup, terminal = self.render(panel)
            signature = encode([html, markup.to_dict()])
            now = self.store.clock()
            self.store.db.execute(
                "UPDATE game_panels SET next=? WHERE id=?", (now + 3, panel["id"])
            )
            if signature == panel["rendered"]:
                if panel["kind"] == "hub":
                    self.store.db.execute(
                        "UPDATE game_panels SET status='done' WHERE id=?",
                        (panel["id"],),
                    )
                continue
            initial = panel["message"] is None
            if initial:
                self.store.db.execute(
                    "UPDATE game_panels SET status='sending' WHERE id=? AND status='pending'",
                    (panel["id"],),
                )
            try:
                kwargs = {
                    "chat_id": panel["chat"],
                    "text": html,
                    "parse_mode": "HTML",
                    "reply_markup": markup,
                    "read_timeout": 10,
                    "write_timeout": 10,
                    "connect_timeout": 5,
                }
                if initial:
                    response = await self.runtime.bot.send_message(**kwargs)
                    message = response.message_id
                    if type(message) is not int or message <= 0:
                        raise ValueError("MissingMessageId")
                else:
                    message = panel["message"]
                    try:
                        await self.runtime.bot.edit_message_text(
                            message_id=message, **kwargs
                        )
                    except BadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            raise
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE game_panels SET status=?,message=?,rendered=?,error='' WHERE id=?",
                        (
                            "done" if terminal or panel["kind"] == "hub" else "active",
                            message,
                            signature,
                            panel["id"],
                        ),
                    )
                    if terminal:
                        db.execute(
                            "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                            (
                                panel["chat"],
                                message,
                                panel["source"],
                                self.store.clock() + 30,
                                self.store.clock() + 30,
                            ),
                        )
                    elif initial and panel["kind"] == "hub":
                        self.idle.touch(panel["chat"], message, "hub")
            except RetryAfter as exc:
                delay = (
                    exc.retry_after.total_seconds()
                    if hasattr(exc.retry_after, "total_seconds")
                    else exc.retry_after
                )
                self.store.db.execute(
                    "UPDATE game_panels SET status=?,next=?,error='RateLimit' WHERE id=?",
                    ("pending" if initial else "active", now + delay, panel["id"]),
                )
            except (Forbidden, BadRequest) as exc:
                self.store.db.execute(
                    "UPDATE game_panels SET status='blocked',error=? WHERE id=?",
                    (type(exc).__name__, panel["id"]),
                )
            except NetworkError:
                self.store.db.execute(
                    "UPDATE game_panels SET status=?,next=?,error='NetworkError' WHERE id=?",
                    ("review" if initial else "active", now + 15, panel["id"]),
                )
            except Exception as exc:
                self.store.db.execute(
                    "UPDATE game_panels SET status='review',error=? WHERE id=?",
                    (type(exc).__name__, panel["id"]),
                )
