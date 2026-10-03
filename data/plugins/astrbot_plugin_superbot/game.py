"""Atomic betting, canonical draws, restart-safe settlement and chase plans."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .rich_text import cards
from .rules import (
    COVERAGE,
    NEW_CAPS,
    NEW_ODDS,
    PLAY_NAMES,
    STATUS_NAMES,
    VERSION,
    canada_balls,
    evaluate,
    room_defaults,
)
from .store import Rejected, encode


class Game:
    def __init__(self, store):
        self.store = store
        self.recovered = False
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS game_backfill(
                issue INTEGER PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,next REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '');
            UPDATE game_backfill SET status='pending' WHERE status='running';
            CREATE TABLE IF NOT EXISTS draw_timing(
                issue INTEGER PRIMARY KEY,first_seen REAL,last_verified REAL NOT NULL,
                origin TEXT NOT NULL,settled_at REAL);
            INSERT OR IGNORE INTO draw_timing(issue,first_seen,last_verified,origin)
                SELECT issue,NULL,received,'legacy' FROM draws;
            CREATE TABLE IF NOT EXISTS keno_poll_runs(
                id INTEGER PRIMARY KEY,started REAL NOT NULL,finished REAL NOT NULL,
                fetch_ms REAL NOT NULL,process_ms REAL NOT NULL,
                latest_issue INTEGER,status TEXT NOT NULL,error TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS keno_poll_age ON keno_poll_runs(finished);
        """)
        if "format" not in {
            row["name"] for row in self.store.db.execute("PRAGMA table_info(notices)")
        }:
            self.store.db.execute("ALTER TABLE notices ADD COLUMN format TEXT")
        # Rewrite only unsent settlement templates, never already delivered notices.
        for bet in self.store.db.execute(
            "SELECT b.* FROM bets b JOIN notices n ON n.id='settle/'||b.id "
            "WHERE n.status='pending' AND n.format IS NULL AND b.id NOT LIKE 'gg:%'"
        ).fetchall():
            self.store.db.execute(
                "UPDATE notices SET text=? WHERE id=? AND status='pending'",
                (
                    f"加拿大28开奖啦\n第 {bet['issue']} 期\n"
                    f"{PLAY_NAMES.get(bet['play'], '其他玩法')} · 投注{bet['amount']}积分 · "
                    f"{STATUS_NAMES.get(bet['status'], '待核查')}\n返还{bet['payout']}积分（含本金）",
                    "settle/" + bet["id"],
                ),
            )

    def rooms(self, db=None, chat=None):
        rooms = self.store.get("rooms", room_defaults(), db)
        for room in rooms.values():
            if chat is not None:
                from .tenants import local_config

                overrides = local_config(
                    self.store, chat, "canada_limits:" + room["id"], {}, db
                )
                for field in ("minimum", "maximum", "total"):
                    if field in overrides:
                        room[field] = (
                            max(room[field], overrides[field])
                            if field == "minimum"
                            else min(room[field], overrides[field])
                        )
            if room["id"] not in {"double", "room28"}:
                continue
            for play, odds in NEW_ODDS.items():
                room["odds"].setdefault(play, odds)
            room["single_limits"] = dict(NEW_CAPS)
            room["additional_rules_version"] = "positions-edges-20260925-v1"
        return rooms

    def configure(
        self, actor, room_id, enabled, minimum, maximum, total, expected=None
    ):
        with self.store.tx() as db:
            self.store.require(actor, "game", db)
            rooms = self.rooms(db)
            if expected is not None and rooms.get(room_id) != expected:
                raise Rejected("房间设置已变更，请重新打开房间后操作")
            if (
                room_id not in rooms
                or type(enabled) is not bool
                or any(type(n) is not int for n in (minimum, maximum, total))
                or not 1 <= minimum <= maximum <= total <= 1000000
            ):
                raise Rejected("房间参数无效")
            rooms[room_id].update(
                enabled=enabled, minimum=minimum, maximum=maximum, total=total
            )
            self.store.put(db, "rooms", rooms)
            self.store.audit(db, actor, "room_config", rooms[room_id])

    def current(self, db=None):
        from .game_hours import description, is_open

        if not is_open(self.store, db):
            raise Rejected("加拿大28休息中。\n" + description(self.store, db))
        db = db or self.store.db
        row = db.execute("SELECT * FROM draws ORDER BY issue DESC LIMIT 1").fetchone()
        if not row:
            raise Rejected("暂无可靠开奖数据，暂不可下注；等待采集恢复。")
        if row["conflict"]:
            raise Rejected(
                f"第 {row['issue']} 期开奖结果存在冲突，暂不可下注；等待核查。"
            )
        if self.store.get("keno_error", "", db):
            raise Rejected("开奖采集异常，暂不可下注；当前封盘状态无法可靠确认。")
        now = self.store.clock()
        closes = row["at"] + 210 - 20
        if now < row["at"]:
            raise Rejected("开奖时间异常：最近开奖时间晚于当前时间，暂不可下注。")
        if now - row["received"] > 60:
            raise Rejected(
                f"开奖数据已过期：距上次有效采集 {int(now - row['received'])} 秒（上限60秒）。"
                "暂不可下注，当前封盘状态无法可靠确认。"
            )
        if now >= row["at"] + 210:
            raise Rejected(
                f"下一期开奖尚未更新：最近开奖第 {row['issue']} 期，"
                f"已超出预计周期 {int(now - row['at'] - 210)} 秒。暂不可下注，等待新数据。"
            )
        if now >= closes:
            raise Rejected(
                f"第 {row['issue'] + 1} 期已封盘，等待开奖（预计约 {max(1, int(row['at'] + 210 - now))} 秒）；"
                "开奖数据正常，下一期开放后可参与。"
            )
        return row["issue"] + 1, closes

    def _place(self, db, uid, room_id, issue, plays, amount, op, chat=None):
        from .tenants import local_config, platform_group

        if (
            chat
            and not platform_group(self.store, chat, db)
            and not local_config(self.store, chat, "canada_enabled", False, db)
        ):
            raise Rejected("本群加拿大28尚未开启，未下注、未扣分。")
        if not self.store.get("modules", {}, db).get("game"):
            raise Rejected("模拟28尚未启用")
        room = self.rooms(db, chat=chat).get(room_id)
        if (
            not room
            or not room["enabled"]
            or type(amount) is not int
            or not room["minimum"] <= amount <= room["maximum"]
            or not 1 <= len(plays) <= 20
            or len(set(plays)) != len(plays)
            or any(p not in room["odds"] for p in plays)
        ):
            raise Rejected("房间未开放或下注参数不符合规则")
        first = db.execute("SELECT * FROM bets WHERE id=?", (op + "/0",)).fetchone()
        if first:
            rows = db.execute(
                "SELECT play,amount,uid,room,issue,points_chat FROM bets WHERE id GLOB ? ORDER BY id",
                (op + "/*",),
            ).fetchall()
            expected = sorted(
                (p, amount, str(uid), room_id, issue, str(chat or "")) for p in plays
            )
            if sorted(tuple(r) for r in rows) != expected:
                raise Rejected("重复操作参数冲突")
            return
        current, _ = self.current(db)
        if issue != current:
            raise Rejected("期次已变化，请重新预览下注")
        existing = db.execute(
            "SELECT room,play,amount FROM bets WHERE uid=? AND issue=? AND status<>'cancelled'"
            + (
                " AND points_chat=?"
                if self.store.get("group_points_enabled", False, db)
                else ""
            ),
            (str(uid), issue, str(chat))
            if self.store.get("group_points_enabled", False, db)
            else (str(uid), issue),
        ).fetchall()
        mask, total, amounts = 0, 0, {}
        for old in existing:
            mask |= COVERAGE.get(old["play"], 0)
            if old["room"] == room_id:
                total += old["amount"]
                amounts[old["play"]] = amounts.get(old["play"], 0) + old["amount"]
        for play in plays:
            mask |= COVERAGE.get(play, 0)
            amounts[play] = amounts.get(play, 0) + amount
            total += amount
            if play in NEW_CAPS:
                if amount > NEW_CAPS[play]:
                    raise Rejected(f"该新增玩法单笔最多{NEW_CAPS[play]}积分")
            elif amounts[play] > min(room["limits"][play], room["maximum"]):
                raise Rejected("该玩法本期累计达到限额")
        if mask == 15 or total > room["total"]:
            raise Rejected("本期总额超限或形成全覆盖下注（含其他房间）")
        self.store.credit(
            db, "bet/" + op, uid, -amount * len(plays), "模拟28下注", chat=chat
        )
        for index, play in enumerate(plays):
            db.execute(
                "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,at,points_chat) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    op + f"/{index}",
                    str(uid),
                    room_id,
                    issue,
                    play,
                    amount,
                    encode(room),
                    self.store.clock(),
                    str(chat or ""),
                ),
            )
        self.store.audit(
            db,
            uid,
            "bet_accepted",
            {
                "operation": op,
                "issue": issue,
                "room": room_id,
                "plays": plays,
                "amount": amount,
                "version": VERSION,
                "chat": str(chat or ""),
            },
        )

    def place(self, uid, room, issue, plays, amount, op, chat=None):
        with self.store.tx() as db:
            self._place(db, uid, room, issue, plays, amount, op, chat=chat)

    def ingest(self, draws, *, historical=False):
        """Preserve canonical facts and stop on conflicts without rewriting history.

        Args:
            draws: Validated source records with issue, at, raw, evidence.
            historical: Preserve live collection health while importing history.
        """
        if not draws:
            raise Rejected("开奖源没有返回有效期次")
        now = self.store.clock()
        prepared = []
        for item in sorted(draws, key=lambda x: x["issue"]):
            balls = canada_balls(item["raw"])
            if (
                type(item["issue"]) is not int
                or item["issue"] <= 0
                or not 0 < item["at"] <= now + 5
            ):
                raise Rejected("开奖期号或时间无效")
            prepared.append((item, balls))
        conflict = False
        with self.store.tx() as db:
            for item, balls in prepared:
                old = db.execute(
                    "SELECT * FROM draws WHERE issue=?", (item["issue"],)
                ).fetchone()
                if old:
                    if (
                        json.loads(old["balls"]) != balls
                        or sorted(json.loads(old["raw"])) != sorted(item["raw"])
                        or old["at"] != item["at"]
                    ):
                        db.execute(
                            "UPDATE draws SET conflict=1 WHERE issue=?",
                            (item["issue"],),
                        )
                        self.store.audit(db, "system", "draw_conflict", item)
                        conflict = True
                    else:
                        db.execute(
                            "UPDATE draws SET received=? WHERE issue=?",
                            (now, item["issue"]),
                        )
                        db.execute(
                            "UPDATE draw_timing SET last_verified=? WHERE issue=?",
                            (now, item["issue"]),
                        )
                else:
                    neighbors = db.execute(
                        "SELECT issue,at FROM draws WHERE issue=(SELECT MAX(issue) FROM draws WHERE issue<?) OR issue=(SELECT MIN(issue) FROM draws WHERE issue>?)",
                        (item["issue"], item["issue"]),
                    ).fetchall()
                    if any(
                        (n["issue"] < item["issue"] and n["at"] >= item["at"])
                        or (n["issue"] > item["issue"] and n["at"] <= item["at"])
                        for n in neighbors
                    ):
                        raise Rejected("开奖时间与期号顺序冲突")
                    db.execute(
                        "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(?,?,?,?,?,?)",
                        (
                            item["issue"],
                            item["at"],
                            encode(item["raw"]),
                            encode(balls),
                            encode(item["evidence"]),
                            now,
                        ),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO draw_timing(issue,first_seen,last_verified,origin) VALUES(?,?,?,?)",
                        (
                            item["issue"],
                            now,
                            now,
                            "backfill"
                            if historical
                            else "live"
                            if now - item["at"] <= 120
                            else "catchup",
                        ),
                    )
            unresolved = db.execute(
                "SELECT 1 FROM draws WHERE conflict=1 LIMIT 1"
            ).fetchone()
            if conflict or unresolved or not historical:
                self.store.put(
                    db,
                    "keno_error",
                    "开奖证据冲突，需维护核查" if conflict or unresolved else "",
                )
        self.settle()

    def settle(self):
        self.store.db.execute(
            "INSERT OR IGNORE INTO game_backfill(issue) SELECT DISTINCT b.issue FROM bets b LEFT JOIN draws d ON d.issue=b.issue WHERE b.status='pending' AND d.issue IS NULL AND b.issue<(SELECT MAX(issue) FROM draws)"
        )
        self.store.db.execute(
            "UPDATE game_backfill SET status='complete',error='' WHERE EXISTS(SELECT 1 FROM draws d WHERE d.issue=game_backfill.issue AND d.conflict=0)"
        )
        pending = self.store.db.execute(
            "SELECT b.id FROM bets b JOIN draws d ON d.issue=b.issue WHERE b.status='pending' AND d.conflict=0 LIMIT 500"
        ).fetchall()
        for item in pending:
            with self.store.tx() as db:
                bet = db.execute(
                    "SELECT * FROM bets WHERE id=? AND status='pending'", (item[0],)
                ).fetchone()
                if not bet:
                    continue
                draw = db.execute(
                    "SELECT * FROM draws WHERE issue=? AND conflict=0", (bet["issue"],)
                ).fetchone()
                if not draw:
                    continue
                status, payout = evaluate(
                    json.loads(bet["snapshot"]),
                    bet["play"],
                    bet["amount"],
                    json.loads(draw["balls"]),
                )
                self.store.credit(
                    db,
                    "settle/" + bet["id"],
                    bet["uid"],
                    payout,
                    "模拟28结算",
                    chat=bet["points_chat"],
                )
                db.execute(
                    "UPDATE bets SET status=?,payout=?,settled=? WHERE id=?",
                    (status, payout, self.store.clock(), bet["id"]),
                )
                db.execute(
                    "UPDATE draw_timing SET settled_at=? WHERE issue=? AND NOT EXISTS("
                    "SELECT 1 FROM bets WHERE issue=? AND status='pending')",
                    (self.store.clock(), bet["issue"], bet["issue"]),
                )
                if not bet["id"].startswith("gg:"):
                    text = f"加拿大28开奖啦\n第 {bet['issue']} 期\n{PLAY_NAMES.get(bet['play'], '其他玩法')} · 投注{bet['amount']}积分 · {STATUS_NAMES.get(status, '待核查')}\n返还{payout}积分（含本金）"
                    db.execute(
                        "INSERT OR IGNORE INTO notices(id,chat,text,format) VALUES(?,?,?,'HTML')",
                        ("settle/" + bet["id"], bet["uid"], cards(text)[0][1]),
                    )

    def create_chase(
        self, uid, room, issue, plays, amount, periods, mode, stop_win, op
    ):
        if (
            type(periods) is not int
            or not 1 <= periods <= 99
            or mode not in ("flat", "double")
            or type(stop_win) is not bool
            or (
                mode == "double"
                and (periods > 40 or amount * (2 ** (periods - 1)) > 1000000)
            )
        ):
            raise Rejected("追号参数无效或翻倍后超限；期数1—99，平倍或翻倍")
        with self.store.tx() as db:
            prior = db.execute("SELECT * FROM chases WHERE id=?", (op,)).fetchone()
            if prior:
                if (
                    prior["uid"],
                    prior["room"],
                    prior["plays"],
                    prior["amount"],
                    prior["periods"],
                    prior["mode"],
                    prior["stop_win"],
                ) != (
                    str(uid),
                    room,
                    encode(plays),
                    amount,
                    periods,
                    mode,
                    int(stop_win),
                ):
                    raise Rejected("追号操作冲突")
                return
            self._place(db, uid, room, issue, plays, amount, "chase/" + op + "/0")
            db.execute(
                "INSERT INTO chases(id,uid,room,plays,amount,periods,mode,stop_win,next_issue,step,status) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                (
                    op,
                    str(uid),
                    room,
                    encode(plays),
                    amount,
                    periods,
                    mode,
                    int(stop_win),
                    issue + 1,
                    "complete" if periods == 1 else "active",
                ),
            )

    def cancel_chase(self, uid, key):
        with self.store.tx() as db:
            row = db.execute("SELECT uid FROM chases WHERE id=?", (key,)).fetchone()
            if not row or row[0] != str(uid):
                raise Rejected("追号不存在或无权限")
            db.execute(
                "UPDATE chases SET status='cancelled' WHERE id=? AND status IN ('active','paused')",
                (key,),
            )
            self.store.audit(db, uid, "chase_cancel", {"id": key})

    def tick_chases(self):
        try:
            issue, _ = self.current()
        except Rejected:
            return
        if not self.recovered:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE chases SET status='paused',error='重启后不补投当前或错过期次' WHERE status='active' AND next_issue<=?",
                    (issue,),
                )
            self.recovered = True
        rows = self.store.db.execute(
            "SELECT id FROM chases WHERE status='active' AND next_issue<=?", (issue,)
        ).fetchall()
        for row in rows:
            try:
                with self.store.tx() as db:
                    chase = db.execute(
                        "SELECT * FROM chases WHERE id=? AND status='active'", (row[0],)
                    ).fetchone()
                    if not chase:
                        continue
                    if chase["next_issue"] != issue:
                        raise Rejected("错过期次，已暂停，不补投")
                    prev = db.execute(
                        "SELECT status FROM bets WHERE id GLOB ?",
                        ("chase/" + row[0] + f"/{chase['step'] - 1}/*",),
                    ).fetchall()
                    if any(r[0] == "pending" for r in prev):
                        continue
                    if chase["stop_win"] and any(
                        r[0] in ("win", "refund") for r in prev
                    ):
                        db.execute(
                            "UPDATE chases SET status='complete',error='中奖即停' WHERE id=?",
                            (row[0],),
                        )
                        continue
                    amount = chase["amount"] * (
                        2 ** chase["step"] if chase["mode"] == "double" else 1
                    )
                    self._place(
                        db,
                        chase["uid"],
                        chase["room"],
                        issue,
                        json.loads(chase["plays"]),
                        amount,
                        "chase/" + row[0] + f"/{chase['step']}",
                        chat=chase["points_chat"],
                    )
                    db.execute(
                        "UPDATE chases SET step=step+1,next_issue=next_issue+1,status=? WHERE id=?",
                        (
                            "complete"
                            if chase["step"] + 1 >= chase["periods"]
                            else "active",
                            row[0],
                        ),
                    )
            except Rejected as exc:
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE chases SET status='paused',error=? WHERE id=? AND status='active'",
                        (str(exc), row[0]),
                    )

    def leaderboard(self, mode):
        now = datetime.fromtimestamp(self.store.clock(), ZoneInfo("Asia/Shanghai"))
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = (
            start - timedelta(days=start.weekday())
            if mode == "week"
            else start.replace(day=1)
        )
        return self.store.db.execute(
            "SELECT uid,SUM(payout-amount) profit,COUNT(*) bets FROM bets WHERE status IN ('win','lose','refund') AND settled>=? GROUP BY uid HAVING profit>0 ORDER BY profit DESC,uid LIMIT 20",
            (start.timestamp(),),
        ).fetchall()
