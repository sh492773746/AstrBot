"""Persistent, group-isolated six-cell elimination games."""

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
from .slots import STAKES, group_stakes
from .store import Rejected, encode

VERSION = "mines-2"
RULES = (
    "每人自选投入，3—10人，50秒报名；不足3人退款。\n"
    "报名期间可退出，退出后本桌不能再次加入。\n"
    "随机座次，六格一雷，轮流选格；安全后换人，踩雷淘汰并重置棋盘。\n"
    "每人15秒，超时随机代选；最后幸存者获得总池90%，抽水10%。\n"
    "投入不同，胜率不加权；低投入也可赢得扣费后的全部奖池。\n"
    "仅群积分，无数字推理；故障暂停，连续5分钟无法恢复则全员退回原投入。"
)


class Mines:
    """Own atomic game transitions and serialized public panel delivery."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.idle = PanelIdle(self.store)
        self.locks = {}
        self.jobs = {}
        self.cleanup_at = 0
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS mines_groups(
                chat TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS mines_menus(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,uid TEXT NOT NULL,
                source TEXT NOT NULL,message INTEGER,status TEXT NOT NULL,
                created REAL NOT NULL,UNIQUE(chat,source,uid));
            CREATE TABLE IF NOT EXISTS mines_tables(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,stake INTEGER NOT NULL,
                status TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,
                message INTEGER,delivery TEXT NOT NULL DEFAULT 'pending',
                deadline REAL NOT NULL DEFAULT 0,next REAL NOT NULL DEFAULT 0,
                fault REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                retry_at REAL NOT NULL DEFAULT 0,
                created REAL NOT NULL,menu TEXT NOT NULL UNIQUE);
            CREATE UNIQUE INDEX IF NOT EXISTS mines_one_active_group
                ON mines_tables(chat) WHERE status IN ('open','active');
            CREATE INDEX IF NOT EXISTS mines_due ON mines_tables(next);
            CREATE INDEX IF NOT EXISTS mines_group_history ON mines_tables(chat,created);
            CREATE TABLE IF NOT EXISTS mines_moves(
                table_id TEXT NOT NULL,version INTEGER NOT NULL,uid TEXT NOT NULL,
                action TEXT NOT NULL,at REAL NOT NULL,data TEXT NOT NULL,
                PRIMARY KEY(table_id,version));
            CREATE TABLE IF NOT EXISTS mines_effects(
                table_id TEXT PRIMARY KEY,chat TEXT NOT NULL,uid TEXT NOT NULL,
                label TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                message INTEGER,next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS mines_effect_due ON mines_effects(status,next);
            CREATE INDEX IF NOT EXISTS mines_menu_created ON mines_menus(created);
            UPDATE mines_effects SET status='review',error='InterruptedSend' WHERE status='sending';
            UPDATE mines_menus SET status='review' WHERE status='sending';
            UPDATE mines_tables SET delivery='review',error='InterruptedSend'
                WHERE delivery='sending';
            CREATE TABLE IF NOT EXISTS mines_events(
                id TEXT PRIMARY KEY,table_id TEXT NOT NULL,chat TEXT NOT NULL,
                uid TEXT NOT NULL,label TEXT NOT NULL,kind TEXT NOT NULL,
                caption TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                message INTEGER,next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS mines_event_due ON mines_events(status,next);
            UPDATE mines_events SET status='review',error='InterruptedSend' WHERE status='sending';
        """)
        # A restart never catches up missed turns or rerolls saved boards.
        self.store.db.execute(
            "UPDATE mines_tables SET deadline=0,next=0,fault=CASE WHEN fault=0 THEN ? ELSE fault END "
            "WHERE status='active'",
            (self.store.clock(),),
        )

    def check(self, chat):
        """Validate group-local admission switches.

        Args:
            chat: Registered group ID.

        Returns:
            Current group configuration version.
        """
        row = self.store.db.execute(
            "SELECT m.version FROM mines_groups m JOIN mod_groups g ON g.chat=m.chat "
            "WHERE m.chat=? AND m.enabled=1 AND g.enabled=1",
            (str(chat),),
        ).fetchone()
        reason = blocker(self.store, chat, "mines", bool(row))
        if reason:
            raise Rejected(reason)
        return row["version"]

    def transition(
        self, identity, action, uid="", label="", version=None, cell=None, stake=None
    ):
        """Execute one synchronous state transition and all associated credits.

        Args:
            identity: Persisted table ID.
            action: Join, leave, start, pick, timeout, or cancel.
            uid: Verified actor ID, absent for system transitions.
            label: Escaped only at rendering time.
            version: Expected table version for player or timeout operations.
            cell: Zero-based chosen cell.
            stake: Selected personal stake for new-version admission.

        Returns:
            A concise callback response.
        """
        now = self.store.clock()
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM mines_tables WHERE id=?", (identity,)
            ).fetchone()
            if not row:
                raise Rejected("桌次不存在。")
            if row["status"] in {"settled", "cancelled"}:
                return "本桌已结束，未重复扣分或派发。"
            if (
                version is not None
                and version != row["version"]
                and action not in {"join", "leave"}
            ):
                raise Rejected("桌次已更新，请使用最新按钮。")
            state = json.loads(row["state"])
            board_before = json.loads(
                encode(
                    {
                        key: state.get(key)
                        for key in (
                            "board",
                            "mine",
                            "opened",
                            "turn",
                            "seats",
                            "auto_cell",
                        )
                    }
                )
            )
            people = state["people"]
            status, deadline = row["status"], row["deadline"]
            answer = "已更新桌面板。"
            if action in {"join", "leave"}:
                if status != "open" or not deadline or deadline <= now or row["fault"]:
                    raise Rejected("当前不在报名期，请查看桌面板。")
                self.check(row["chat"])
                person = next((p for p in people if p["uid"] == str(uid)), None)
                if action == "join":
                    if state.get("rules") == "mines-2":
                        if type(stake) is not int or stake not in state.get(
                            "stakes", STAKES
                        ):
                            raise Rejected("请选择本桌积分档位加入，旧加入按钮已停用。")
                    else:
                        stake = row["stake"]
                    if person:
                        raise Rejected("已报名或已退出，本桌不能重复加入。")
                    if sum(p["status"] == "alive" for p in people) >= 10:
                        raise Rejected("本桌人数已满。")
                    self.store.credit(
                        db,
                        f"mines:{identity}:{uid}:stake",
                        uid,
                        -stake,
                        "mines_stake",
                        chat=row["chat"],
                    )
                    people.append(
                        {
                            "uid": str(uid),
                            "label": label[:60],
                            "status": "alive",
                            "payout": 0,
                            "stake": stake,
                        }
                    )
                else:
                    if not person or person["status"] != "alive":
                        raise Rejected("你没有本桌待退投入。")
                    self.store.credit(
                        db,
                        f"mines:{identity}:{uid}:refund",
                        uid,
                        person.get("stake", row["stake"]),
                        "mines_refund",
                        chat=row["chat"],
                    )
                    person.update(
                        status="withdrawn", payout=person.get("stake", row["stake"])
                    )
            elif action == "start":
                if status != "open" or not deadline or deadline > now:
                    raise Rejected("尚未到报名截止时间。")
                self.check(row["chat"])
                alive = [p["uid"] for p in people if p["status"] == "alive"]
                if len(alive) < 3:
                    action = "cancel"
                    state["notice"] = "人数不足3人，全额退款。"
                else:
                    secrets.SystemRandom().shuffle(alive)
                    state.update(
                        seats=alive,
                        turn=alive[0],
                        mine=secrets.randbelow(6),
                        opened=[],
                        eliminated=[],
                        board=1,
                        auto_cell=secrets.randbelow(6),
                        notice="随机座次已确定。",
                    )
                    status, deadline = "active", 0
            elif action in {"pick", "timeout"}:
                if status != "active" or not deadline or row["fault"]:
                    raise Rejected("当前回合暂停，等待面板恢复。")
                if action == "timeout":
                    if now < deadline:
                        raise Rejected("尚未超时。")
                    uid = state["turn"]
                    cell = state["auto_cell"]
                elif str(uid) != state["turn"] or now >= deadline:
                    raise Rejected("未轮到你或本回合已超时。")
                if (
                    type(cell) is not int
                    or cell not in range(6)
                    or cell in state["opened"]
                ):
                    raise Rejected("格子无效或已翻开。")
                hit = cell == state["mine"]
                state["notice"] = (
                    f"{'超时代选' if action == 'timeout' else '选格'}：{cell + 1}号，{'踩雷淘汰' if hit else '安全'}。"
                )
                state["last_uid"] = str(uid)
                if hit:
                    next(p for p in people if p["uid"] == str(uid))["status"] = "out"
                    state["eliminated"].append(str(uid))
                    if state.get("rules") == "mines-2":
                        loser = next(p for p in people if p["uid"] == str(uid))
                        db.execute(
                            "INSERT OR IGNORE INTO mines_events(id,table_id,chat,uid,label,kind,caption) VALUES(?,?,?,?,?,'explosion',?)",
                            (
                                f"{identity}:out:{uid}",
                                identity,
                                row["chat"],
                                str(uid),
                                loser["label"],
                                "💥 踩雷淘汰，本次不再参与选格。",
                            ),
                        )
                alive = [p["uid"] for p in people if p["status"] == "alive"]
                if len(alive) == 1:
                    winner = next(p for p in people if p["uid"] == alive[0])
                    pool = sum(
                        p.get("stake", row["stake"])
                        for p in people
                        if p["status"] != "withdrawn"
                    )
                    fee = pool // 10 if state.get("rules") == "mines-2" else 0
                    self.store.credit(
                        db,
                        f"mines:{identity}:payout",
                        winner["uid"],
                        pool - fee,
                        "mines_payout",
                        chat=row["chat"],
                    )
                    winner["payout"] = pool - fee
                    state.update(pool=pool, fee=fee)
                    state["winner"] = winner["uid"]
                    loser = next(p for p in people if p["uid"] == str(uid))
                    if state.get("rules") == "mines-2":
                        db.execute(
                            "INSERT OR IGNORE INTO mines_events(id,table_id,chat,uid,label,kind,caption) VALUES(?,?,?,?,?,'trophy',?)",
                            (
                                f"{identity}:winner",
                                identity,
                                row["chat"],
                                winner["uid"],
                                winner["label"],
                                f"🏆 最后幸存者！\n总池 {pool} · 抽水 {fee}\n到账 {pool - fee}积分 · 净赢 {pool - fee - winner['stake']:+d}积分",
                            ),
                        )
                    else:
                        db.execute(
                            "INSERT OR IGNORE INTO mines_effects(table_id,chat,uid,label) VALUES(?,?,?,?)",
                            (identity, row["chat"], str(uid), loser["label"]),
                        )
                    status, deadline = "settled", 0
                else:
                    seats = state["seats"]
                    start = seats.index(str(uid))
                    state["turn"] = next(
                        seats[(start + offset) % len(seats)]
                        for offset in range(1, len(seats) + 1)
                        if seats[(start + offset) % len(seats)] in alive
                    )
                    if hit:
                        state.update(
                            mine=secrets.randbelow(6),
                            opened=[],
                            board=state["board"] + 1,
                        )
                    else:
                        state["opened"].append(cell)
                    state["auto_cell"] = secrets.choice(
                        [n for n in range(6) if n not in state["opened"]]
                    )
                    deadline = 0
            elif action != "cancel":
                raise Rejected("无效操作。")
            if action == "cancel":
                for person in people:
                    if person["status"] != "withdrawn":
                        self.store.credit(
                            db,
                            f"mines:{identity}:{person['uid']}:refund",
                            person["uid"],
                            person.get("stake", row["stake"]),
                            "mines_refund",
                            chat=row["chat"],
                        )
                        person.update(
                            status="refunded", payout=person.get("stake", row["stake"])
                        )
                status, deadline = "cancelled", 0
                state.setdefault("notice", "桌次取消，全额退款。")
            if (
                state.get("rules") == "mines-2"
                and action in {"start", "pick", "timeout"}
                and status == "active"
                and row["message"]
            ):
                # Each new turn gets a new panel. Its timer starts after delivery.
                state["previous_panel"] = row["message"]
                db.execute(
                    "UPDATE mines_tables SET message=NULL,delivery='pending' WHERE id=?",
                    (identity,),
                )
            db.execute(
                "UPDATE mines_tables SET status=?,state=?,version=version+1,deadline=?,next=0 WHERE id=?",
                (status, encode(state), deadline, identity),
            )
            db.execute(
                "INSERT INTO mines_moves VALUES(?,?,?,?,?,?)",
                (
                    identity,
                    row["version"] + 1,
                    str(uid),
                    action,
                    now,
                    encode(
                        {
                            "cell": cell,
                            "status": status,
                            "notice": state.get("notice", ""),
                            "board_before": board_before,
                            "board_after": {
                                key: state.get(key)
                                for key in (
                                    "board",
                                    "mine",
                                    "opened",
                                    "turn",
                                    "seats",
                                    "auto_cell",
                                )
                            },
                        }
                    ),
                ),
            )
            self.store.audit(
                db,
                uid or "system",
                "mines_transition",
                {
                    "table": identity,
                    "action": action,
                    "version": row["version"] + 1,
                    "rules": state.get("rules", "mines-1"),
                    "pool": state.get("pool", 0),
                    "fee": state.get("fee", 0),
                    "winner": state.get("winner"),
                },
            )
            return answer

    def render(self, row):
        """Build bounded escaped text and version-bound buttons.

        Args:
            row: Persisted table row.

        Returns:
            HTML and inline keyboard.
        """
        state = json.loads(row["state"])
        people = [p for p in state["people"] if p["status"] != "withdrawn"]
        names = {
            p["uid"]: f'<a href="tg://user?id={p["uid"]}">{escape(p["label"])}</a>'
            for p in people
        }
        prefix = f"mn:t:{row['id']}:{row['version']}:"
        modern = state.get("rules") == "mines-2"
        pool = sum(p.get("stake", row["stake"]) for p in people)
        text = f"<b>💣 扫雷接龙 · 第{row['id']}桌</b>\n奖池 <b>{pool}积分</b> · " + (
            "抽水10%\n" if modern else "旧版同额 · 不抽水\n"
        )
        rows = []
        left = (
            max(0, int(row["deadline"] - self.store.clock()))
            if row["deadline"]
            else (50 if row["status"] == "open" else 15)
        )
        if row["status"] == "open":
            text += f"👥 报名 {len(people)}/10人 · ⏳ {left}秒\n不足3人退款。\n"
            rows = [
                [
                    Button("加入", callback_data=prefix + "join"),
                    Button("退出退款", callback_data=prefix + "leave"),
                ]
            ]
            if modern:
                text += "每人自选投入，胜率不加权；胜者独得总池90%。\n"
                buttons = [
                    Button(f"{s}积分加入", callback_data=prefix + f"join{s}")
                    for s in state.get("stakes", STAKES)
                ]
                rows = [
                    buttons[:2],
                    buttons[2:],
                    [Button("退出退款", callback_data=prefix + "leave")],
                ]
        elif row["status"] == "active":
            text += f"🟢 存活 {sum(p['status'] == 'alive' for p in people)}人 · 第{state['board']}盘\n轮到 {names[state['turn']]} · ⏳ {left}秒\n"
            text += (
                names.get(state.get("last_uid"), "")
                + " "
                + escape(state.get("notice", ""))
            ).strip() + "\n"
            buttons = [
                Button(
                    "✅" if n in state["opened"] else f"❔{n + 1}",
                    callback_data=prefix + f"pick{n}",
                )
                for n in range(6)
            ]
            rows = [buttons[:3], buttons[3:]]
        elif row["status"] == "settled":
            text += f"🏆 <b>胜者</b> {names[state['winner']]} · 应得奖池已到账\n"
            text += f"总池 {pool} · 抽水 {state.get('fee', 0)} · 到账 <b>{pool - state.get('fee', 0)}积分</b>\n"
            order = [state["winner"]] + list(reversed(state["eliminated"]))
            people.sort(key=lambda p: order.index(p["uid"]))
        else:
            text += "↩️ <b>本桌取消，原投入已全额退还。</b>\n"
        if row["fault"] and row["status"] in {"open", "active"}:
            text += "⏸ 网络恢复中，本回合将在面板恢复后重新计时。\n"
        details = []
        for i, p in enumerate(people, 1):
            if row["status"] in {"settled", "cancelled"}:
                details.append(
                    f"{i} · {names[p['uid']]}：投入{p.get('stake', row['stake'])} · 返还{p['payout']} · 净增减{p['payout'] - p.get('stake', row['stake']):+d}"
                )
            else:
                details.append(
                    f"{names[p['uid']]} · 投入{p.get('stake', row['stake'])} · {'存活' if p['status'] == 'alive' else '已淘汰'}"
                )
        text += (
            "<blockquote expandable>"
            + ("\n".join(details) or "暂无报名成员")
            + "</blockquote>"
        )
        if row["status"] in {"settled", "cancelled"}:
            text += "\n⏱ 本条显示成功60秒后撤回。"
        return text, InlineKeyboardMarkup(rows)

    async def open(self, update):
        """Send a user-bound disposable selector, without debiting points.

        Args:
            update: Group text or catalog callback.
        """
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        if update.effective_chat.type != "supergroup" or update.effective_user.is_bot:
            raise Rejected("扫雷接龙仅在超级群参与。")
        message = update.effective_message
        if not update.callback_query and (
            getattr(message, "sender_chat", None)
            or getattr(message, "forward_origin", None)
            or getattr(message, "forward_date", None)
            or not 0 <= self.store.clock() - message.date.timestamp() <= 60
        ):
            raise Rejected("请发送新的普通文字消息。")
        self.check(chat)
        reason = blocker(self.store, chat, "mines", True, rollout=True)
        if reason:
            raise Rejected(reason)
        await self.runtime.community.member(uid, chat, require_moderation=False)
        self.check(chat)
        reason = blocker(self.store, chat, "mines", True, rollout=True)
        if reason:
            raise Rejected(reason)
        identity = secrets.token_hex(6)
        source = (
            ("q:" + update.callback_query.id)
            if update.callback_query
            else ("m:" + str(message.message_id))
        )
        cursor = self.store.db.execute(
            "INSERT OR IGNORE INTO mines_menus VALUES(?,?,?,?,NULL,'sending',?)",
            (identity, chat, uid, source, self.store.clock()),
        )
        if not cursor.rowcount:
            return
        buttons = [
            Button(f"💣 {stake}积分", callback_data=f"mn:m:{identity}:0:{stake}")
            for stake in group_stakes(self.store, chat, "mines")
        ]
        try:
            sent = await self.runtime.bot.send_message(
                chat_id=chat,
                text="<b>💣 扫雷接龙</b>\n选择同桌投入，点击即扣分开桌。\n"
                "<blockquote expandable>" + RULES + "</blockquote>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [buttons[:2], buttons[2:4], buttons[4:]]
                ),
                read_timeout=10,
                write_timeout=10,
            )
            if type(sent.message_id) is not int or sent.message_id <= 0:
                raise ValueError("MissingMessageId")
            self.store.db.execute(
                "UPDATE mines_menus SET message=?,status='active' WHERE id=?",
                (sent.message_id, identity),
            )
            self.idle.touch(chat, sent.message_id, "mines")
        except BaseException:
            self.store.db.execute(
                "UPDATE mines_menus SET status='review' WHERE id=?", (identity,)
            )
            raise

    async def action(self, update):
        """Verify network membership before claiming local atomic operations.

        Args:
            update: Telegram callback.

        Returns:
            Private callback notification.
        """
        query = update.callback_query
        parts = str(query.data).split(":")
        if len(parts) != 5 or parts[1] not in {"m", "t"} or not parts[3].isdigit():
            raise Rejected("无效扫雷按钮。")
        _, kind, identity, version, action = parts
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        if update.effective_chat.type != "supergroup" or update.effective_user.is_bot:
            raise Rejected("请在本群使用个人身份操作。")
        await self.runtime.community.member(uid, chat, require_moderation=False)
        label = (
            "@" + update.effective_user.username
            if update.effective_user.username
            else update.effective_user.full_name
        )
        if kind == "m":
            if chat not in self.store.get("games_v3_groups", []):
                raise Rejected(blocker(self.store, chat, "mines", True, rollout=True))
            config_version = self.check(chat)
            with self.store.tx() as db:
                menu = db.execute(
                    "SELECT * FROM mines_menus WHERE id=?", (identity,)
                ).fetchone()
                if not menu or (menu["chat"], menu["uid"], menu["message"]) != (
                    chat,
                    uid,
                    query.message.message_id,
                ):
                    raise Rejected("请发送“扫雷”打开自己的面板。")
                prior = db.execute(
                    "SELECT id FROM mines_tables WHERE menu=?", (identity,)
                ).fetchone()
                if prior:
                    return "此面板已开桌，未重复扣分。"
                if menu["status"] != "active":
                    raise Rejected("面板已失效。")
                self.idle.touch(chat, query.message.message_id, "mines")
                if not action.isdigit() or int(action) not in group_stakes(
                    self.store, chat, "mines", db
                ):
                    raise Rejected("档位无效。")
                if db.execute(
                    "SELECT 1 FROM mines_tables WHERE chat=? AND status IN ('open','active')",
                    (chat,),
                ).fetchone():
                    raise Rejected("本群已有扫雷桌，请在桌面板加入。")
                stake = int(action)
                table = secrets.token_hex(6)
                self.store.credit(
                    db,
                    f"mines:{table}:{uid}:stake",
                    uid,
                    -stake,
                    "mines_stake",
                    chat=chat,
                )
                state = {
                    "rules": VERSION,
                    "fee_percent": 10,
                    "config_version": config_version,
                    "stakes": group_stakes(self.store, chat, "mines", db),
                    "people": [
                        {
                            "uid": uid,
                            "label": label[:60],
                            "status": "alive",
                            "payout": 0,
                            "stake": stake,
                        }
                    ],
                }
                db.execute(
                    "INSERT INTO mines_tables(id,chat,stake,status,version,state,created,menu) VALUES(?,?,?,'open',1,?,?,?)",
                    (table, chat, stake, encode(state), self.store.clock(), identity),
                )
                self.store.audit(
                    db,
                    uid,
                    "mines_create",
                    {"table": table, "chat": chat, "stake": stake},
                )
            return "已托管积分，正在发布报名桌。"
        async with self.locks.setdefault(identity, asyncio.Lock()):
            row = self.store.db.execute(
                "SELECT * FROM mines_tables WHERE id=?", (identity,)
            ).fetchone()
            if not row or (row["chat"], row["message"]) != (
                chat,
                query.message.message_id,
            ):
                raise Rejected("这不是本群对应桌次。")
            if action in {"join", "leave"}:
                return self.transition(identity, action, uid, label, int(version))
            if action.startswith("join") and action[4:].isdigit():
                return self.transition(
                    identity, "join", uid, label, int(version), stake=int(action[4:])
                )
            if action.startswith("pick") and action[4:].isdigit():
                return self.transition(
                    identity, "pick", uid, label, int(version), int(action[4:])
                )
            raise Rejected("无效操作。")

    async def tick(self, identity):
        """Advance deadlines and acknowledge exact versions after delivery.

        Args:
            identity: Persisted table to process under its own asynchronous lock.
        """
        async with self.locks.setdefault(identity, asyncio.Lock()):
            row = self.store.db.execute(
                "SELECT * FROM mines_tables WHERE id=?", (identity,)
            ).fetchone()
            now = self.store.clock()
            if not row or row["next"] > now:
                return
            live = row["status"] in {"open", "active"}
            if live:
                cancel = row["delivery"] == "review" or (
                    row["fault"] and now - row["fault"] >= 300
                )
                if row["status"] == "open":
                    try:
                        self.check(row["chat"])
                    except Rejected:
                        cancel = True
                if cancel:
                    self.transition(identity, "cancel")
                elif row["deadline"] and row["deadline"] <= now:
                    self.transition(
                        identity,
                        "start" if row["status"] == "open" else "timeout",
                        version=row["version"],
                    )
            row = self.store.db.execute(
                "SELECT * FROM mines_tables WHERE id=?", (identity,)
            ).fetchone()
            if row["retry_at"] > now:
                self.store.db.execute(
                    "UPDATE mines_tables SET next=? WHERE id=?",
                    (
                        min(row["retry_at"], row["fault"] + 300)
                        if row["status"] in {"open", "active"} and row["fault"]
                        else row["retry_at"],
                        identity,
                    ),
                )
                return
            if row["delivery"] == "review" or (
                not row["message"] and row["status"] == "cancelled"
            ):
                self.store.db.execute(
                    "UPDATE mines_tables SET next=1e20 WHERE id=?", (identity,)
                )
                return
            text, markup = self.render(row)
            self.store.db.execute(
                "UPDATE mines_tables SET next=? WHERE id=?", (now + 3, identity)
            )
            sending = row["message"] is None
            started = self.store.clock()
            try:
                if sending:
                    self.store.db.execute(
                        "UPDATE mines_tables SET delivery='sending' WHERE id=?",
                        (identity,),
                    )
                    sent = await self.runtime.bot.send_message(
                        chat_id=row["chat"],
                        text=text,
                        parse_mode="HTML",
                        reply_markup=markup,
                        read_timeout=10,
                        write_timeout=10,
                    )
                    if type(sent.message_id) is not int or sent.message_id <= 0:
                        raise ValueError("MissingMessageId")
                    self.store.db.execute(
                        "UPDATE mines_tables SET message=?,delivery='sent' WHERE id=?",
                        (sent.message_id, identity),
                    )
                    message_id = sent.message_id
                else:
                    message_id = row["message"]
                    try:
                        await self.runtime.bot.edit_message_text(
                            chat_id=row["chat"],
                            message_id=message_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=markup,
                            read_timeout=2
                            if row["deadline"] and not row["fault"]
                            else 10,
                            write_timeout=10,
                        )
                    except BadRequest as exc:
                        if "not modified" not in str(exc).lower():
                            raise
                now = self.store.clock()
                duration = 50 if row["status"] == "open" else 15
                deadline = row["deadline"] or now + duration
                # A slow successful refresh must not consume a player's turn.
                if row["status"] == "active" and row["deadline"]:
                    deadline += max(0, now - started)
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE mines_tables SET deadline=?,fault=0,error='',next=? WHERE id=?",
                        (
                            deadline,
                            now + 3 if row["status"] in {"open", "active"} else 1e20,
                            identity,
                        ),
                    )
                    previous = json.loads(row["state"]).get("previous_panel")
                    if sending and previous and previous != message_id:
                        db.execute(
                            "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                            (row["chat"], previous, previous, now + 60, now + 60),
                        )
                    if row["status"] in {"settled", "cancelled"}:
                        db.execute(
                            "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                            (row["chat"], message_id, message_id, now + 60, now + 60),
                        )
            except asyncio.CancelledError:
                # Interrupted sends remain claimed; constructor marks them for review.
                raise
            except Exception as exc:
                now = self.store.clock()
                fault = row["fault"] or now
                delay = 3
                if isinstance(exc, RetryAfter):
                    delay = (
                        exc.retry_after.total_seconds()
                        if hasattr(exc.retry_after, "total_seconds")
                        else exc.retry_after
                    )
                missing = isinstance(exc, BadRequest) and any(
                    term in str(exc).lower()
                    for term in ("message to edit not found", "message_id_invalid")
                )
                limited = isinstance(exc, RetryAfter)
                self.store.db.execute(
                    "UPDATE mines_tables SET deadline=0,fault=?,error=?,next=?,delivery=?,retry_at=? WHERE id=?",
                    (
                        fault,
                        type(exc).__name__,
                        now + min(max(delay, 3), 300),
                        ("pending" if limited else "review")
                        if sending
                        else row["delivery"],
                        now + delay if limited else 0,
                        identity,
                    ),
                )
                if (sending and not limited) or missing:
                    self.transition(identity, "cancel")
                    self.store.db.execute(
                        "UPDATE mines_tables SET next=1e20 WHERE id=?", (identity,)
                    )
                elif isinstance(exc, Forbidden):
                    self.runtime.report("mines_delivery", exc)
                if row["status"] in {"settled", "cancelled"} and now - fault >= 300:
                    self.store.db.execute(
                        "UPDATE mines_tables SET next=1e20,delivery='review' WHERE id=?",
                        (identity,),
                    )

    async def effect(self, identity, *, modern=False):
        """Deliver a persisted elimination or winner animation once.

        Args:
            identity: Legacy table identifier or modern event identifier.
            modern: Whether to use the independently keyed event queue.
        """
        table, key = ("mines_events", "id") if modern else ("mines_effects", "table_id")
        row = self.store.db.execute(
            f"SELECT * FROM {table} WHERE {key}=?", (identity,)
        ).fetchone()
        if not row or row["status"] != "pending":
            return
        if not self.store.db.execute(
            f"UPDATE {table} SET status='sending' WHERE {key}=? AND status='pending'",
            (identity,),
        ).rowcount:
            return
        try:
            trophy = modern and row["kind"] == "trophy"
            asset = Path(__file__).parent / (
                "assets/mines-trophy-v1.gif"
                if trophy
                else "assets/mines-explosion-v1.gif"
            )
            cache_key = "mines_trophy_v1" if trophy else "mines_explosion_v1"
            cached = self.store.get(cache_key)
            caption = (
                f"<b>{'🏆' if trophy else '💥'} 扫雷接龙 · 第{row['table_id']}桌</b>\n"
                f'<a href="tg://user?id={row["uid"]}">{escape(row["label"])}</a>\n'
                + (
                    escape(row["caption"])
                    if modern
                    else "最后踩雷淘汰！\n本桌已结算，以桌面板排名和返还为准。"
                )
            )
            sent = await self.runtime.bot.send_animation(
                chat_id=row["chat"],
                animation=cached or InputFile(asset.read_bytes(), filename=asset.name),
                caption=caption,
                parse_mode="HTML",
                read_timeout=10,
                write_timeout=10,
            )
            if type(sent.message_id) is not int or sent.message_id <= 0:
                raise ValueError("MissingMessageId")
            now = self.store.clock()
            with self.store.tx() as db:
                db.execute(
                    f"UPDATE {table} SET status='sent',message=?,error='' WHERE {key}=?",
                    (sent.message_id, identity),
                )
                animation = getattr(sent, "animation", None)
                if animation:
                    self.store.put(db, cache_key, animation.file_id)
                db.execute(
                    "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next) VALUES(?,?,?,?,?)",
                    (row["chat"], sent.message_id, sent.message_id, now + 60, now + 60),
                )
        except asyncio.CancelledError:
            raise
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else exc.retry_after
            )
            self.store.db.execute(
                f"UPDATE {table} SET status='pending',next=?,error='RateLimit' WHERE {key}=?",
                (self.store.clock() + delay, identity),
            )
        except Exception as exc:
            self.store.db.execute(
                f"UPDATE {table} SET status='review',error=? WHERE {key}=?",
                (type(exc).__name__, identity),
            )

    async def loop(self):
        """Schedule independent bounded workers and drain them before shutdown."""
        try:
            while True:
                try:
                    for key, task in list(self.jobs.items()):
                        if task.done():
                            del self.jobs[key]
                            if not task.cancelled() and task.exception():
                                self.runtime.report("mines_worker", task.exception())
                    now = self.store.clock()
                    if now >= self.cleanup_at:
                        # Keep source identity tombstones and all business evidence.
                        self.store.db.execute(
                            "UPDATE mines_menus SET status='expired' WHERE id IN "
                            "(SELECT id FROM mines_menus m WHERE created<? AND status='active' "
                            "AND NOT EXISTS(SELECT 1 FROM mines_tables t WHERE t.menu=m.id) LIMIT 100)",
                            (now - 86400,),
                        )
                        self.cleanup_at = now + 60
                    rows = self.store.db.execute(
                        "SELECT id FROM mines_tables WHERE next<=? ORDER BY next LIMIT 20",
                        (now,),
                    ).fetchall()
                    for row in rows:
                        key = ("table", row[0])
                        if (
                            key not in self.jobs
                            and sum(k[0] == "table" for k in self.jobs) < 4
                        ):
                            self.jobs[key] = asyncio.create_task(self.tick(row[0]))
                    effects = self.store.db.execute(
                        "SELECT table_id FROM mines_effects WHERE status='pending' AND next<=? ORDER BY next LIMIT 3",
                        (now,),
                    ).fetchall()
                    for row in effects:
                        key = ("effect", row[0])
                        if key not in self.jobs and not any(
                            k[0] == "effect" for k in self.jobs
                        ):
                            self.jobs[key] = asyncio.create_task(self.effect(row[0]))
                    events = self.store.db.execute(
                        "SELECT id FROM mines_events WHERE status='pending' AND next<=? ORDER BY rowid LIMIT 3",
                        (now,),
                    ).fetchall()
                    for row in events:
                        key = ("event", row[0])
                        if key not in self.jobs and not any(
                            k[0] in {"effect", "event"} for k in self.jobs
                        ):
                            self.jobs[key] = asyncio.create_task(
                                self.effect(row[0], modern=True)
                            )
                    self.locks = {
                        key: lock
                        for key, lock in self.locks.items()
                        if lock.locked() or getattr(lock, "_waiters", None)
                    }
                except Exception as exc:
                    self.runtime.report("mines_loop", exc)
                await asyncio.sleep(0.25)
        finally:
            for task in self.jobs.values():
                task.cancel()
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)
            self.jobs.clear()
