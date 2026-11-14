"""Local message statistics and manually reviewed, non-financial activities."""

import asyncio
import hashlib
import html
import json
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter

from .command_router import classify, ownership
from .message_policy import retention
from .store import Rejected, encode

TZ = ZoneInfo("Asia/Shanghai")
LIVE = ("publishing", "active", "review", "finalizing", "delivery_review")


class Activities:
    """Keep activity state, statistics and delivery independent of game wallets."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.last_cleanup = 0
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS ga_config(
                chat TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,
                started REAL NOT NULL,version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS ga_receipts(
                chat TEXT NOT NULL,message INTEGER NOT NULL,uid TEXT NOT NULL,
                name TEXT NOT NULL,at REAL NOT NULL,day TEXT NOT NULL,digest TEXT NOT NULL,
                standard INTEGER NOT NULL,strict INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat,message));
            CREATE INDEX IF NOT EXISTS ga_receipts_actor ON ga_receipts(chat,uid,at);
            CREATE INDEX IF NOT EXISTS ga_receipts_hash ON ga_receipts(chat,uid,day,digest);
            CREATE INDEX IF NOT EXISTS ga_receipts_age ON ga_receipts(at);
            CREATE TABLE IF NOT EXISTS ga_daily(
                chat TEXT NOT NULL,uid TEXT NOT NULL,day TEXT NOT NULL,name TEXT NOT NULL,
                standard INTEGER NOT NULL,strict INTEGER NOT NULL,
                PRIMARY KEY(chat,uid,day));
            CREATE INDEX IF NOT EXISTS ga_daily_rank ON ga_daily(chat,day,standard DESC);
            CREATE TABLE IF NOT EXISTS ga_activities(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,actor TEXT NOT NULL,
                state TEXT NOT NULL,config TEXT NOT NULL,starts REAL NOT NULL DEFAULT 0,
                ends REAL NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL,closed REAL,message INTEGER,error TEXT NOT NULL DEFAULT '');
            CREATE UNIQUE INDEX IF NOT EXISTS ga_one_live ON ga_activities(chat)
                WHERE state IN ('publishing','active','review','finalizing','delivery_review');
            CREATE INDEX IF NOT EXISTS ga_due ON ga_activities(state,ends);
            CREATE TABLE IF NOT EXISTS ga_entries(
                activity TEXT NOT NULL,uid TEXT NOT NULL,name TEXT NOT NULL,
                joined REAL NOT NULL,withdrawn INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(activity,uid));
            CREATE TABLE IF NOT EXISTS ga_candidates(
                activity TEXT NOT NULL,uid TEXT NOT NULL,name TEXT NOT NULL,
                position INTEGER NOT NULL,count INTEGER NOT NULL,reached REAL NOT NULL,
                review TEXT NOT NULL DEFAULT 'pending',reviewer TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',reviewed REAL,
                PRIMARY KEY(activity,uid),UNIQUE(activity,position));
            CREATE TABLE IF NOT EXISTS ga_results(
                activity TEXT NOT NULL,place INTEGER NOT NULL,uid TEXT,name TEXT NOT NULL,
                prize TEXT NOT NULL,fulfilment TEXT NOT NULL DEFAULT 'pending',
                actor TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
                at REAL NOT NULL,PRIMARY KEY(activity,place));
            CREATE TABLE IF NOT EXISTS ga_delivery(
                id TEXT PRIMARY KEY,activity TEXT NOT NULL,kind TEXT NOT NULL,
                status TEXT NOT NULL,next REAL NOT NULL DEFAULT 0,message INTEGER,
                error TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS ga_delivery_due ON ga_delivery(status,next);
            UPDATE ga_delivery SET status='pending' WHERE status='sending' AND kind='refresh';
            UPDATE ga_delivery SET status='review',error='InterruptedSend'
                WHERE status='sending';
            UPDATE ga_activities SET state='delivery_review',error='InterruptedSend'
                WHERE state IN ('publishing','finalizing') AND id IN (
                    SELECT activity FROM ga_delivery WHERE status='review');
        """)
        self.store.db.execute(
            "INSERT OR IGNORE INTO ga_config(chat,started) SELECT chat,0 FROM mod_groups",
        )
        self.store.db.execute(
            "CREATE INDEX IF NOT EXISTS ga_wallet_rank ON group_wallets(chat,balance DESC,uid)"
        )

    def config(self, chat):
        """Read local availability without enabling existing groups.

        Args:
            chat: Group ID.

        Returns:
            Configuration mapping.
        """
        row = self.store.db.execute(
            "SELECT * FROM ga_config WHERE chat=?", (str(chat),)
        ).fetchone()
        return dict(row) if row else {"enabled": 0, "version": 0, "started": 0}

    def register(self, chat):
        """Initialize only a newly discovered group using the free-feature default."""
        self.store.db.execute(
            "INSERT OR IGNORE INTO ga_config(chat,enabled,started) VALUES(?,1,?)",
            (str(chat), self.store.clock()),
        )

    def configure(self, actor, chat, enabled, expected):
        """Change the local feature after fresh authorization.

        Args:
            actor: Verified administrator.
            chat: Exact group.
            enabled: Explicit boolean.
            expected: Configuration version.
        """
        with self.store.tx() as db:
            self.store.require(actor, "moderation", db, chat=chat)
            current = self.config(chat)
            if type(enabled) is not bool or current["version"] != expected:
                raise Rejected("活动配置已变化，请重新打开。")
            db.execute(
                "INSERT INTO ga_config(chat,enabled,started) VALUES(?,?,?) "
                "ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled,version=ga_config.version+1,"
                "started=CASE WHEN ga_config.started=0 THEN excluded.started ELSE ga_config.started END",
                (
                    str(chat),
                    int(enabled),
                    current["started"] or (self.store.clock() if enabled else 0),
                ),
            )
            self.store.audit(
                db, actor, "activity_switch", {"chat": str(chat), "enabled": enabled}
            )

    def observe(self, update):
        """Count eligible new chat text once, without retaining its body.

        Args:
            update: New Telegram message after business routing declined it.
        """
        message = getattr(update, "message", None)
        user, chat = update.effective_user, update.effective_chat
        if (
            not message
            or not user
            or user.is_bot
            or not chat
            or chat.type != "supergroup"
        ):
            return
        if getattr(message, "sender_chat", None) or any(
            getattr(message, key, None)
            for key in (
                "new_chat_members",
                "left_chat_member",
                "new_chat_title",
                "new_chat_photo",
                "pinned_message",
                "dice",
            )
        ):
            return
        if (
            not self.config(chat.id)["enabled"]
            and not self.store.db.execute(
                "SELECT 1 FROM ga_activities WHERE chat=? AND state='active' "
                "AND starts<=? AND ends>? LIMIT 1",
                (str(chat.id), self.store.clock(), self.store.clock()),
            ).fetchone()
        ):
            return
        route = ownership(message, self.runtime.bot.username, self.runtime.bot.id)
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if (
            not route.accepted
            or route.command
            or text.lstrip().startswith("/")
            or classify(text, self.runtime.bot.username)
        ):
            return
        normalized = " ".join(
            "".join(
                c
                for c in unicodedata.normalize("NFKC", text).casefold()
                if unicodedata.category(c) != "Cf"
            ).split()
        )
        meaningful = "".join(c for c in normalized if c.isalnum())
        if not meaningful:
            return
        sent = getattr(message, "date", None)
        at = sent.timestamp() if isinstance(sent, datetime) else self.store.clock()
        now = self.store.clock()
        if at < self.config(chat.id)["started"] or at > now + 5 or now - at > 180:
            return
        day = datetime.fromtimestamp(at, TZ).date().isoformat()
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        name = " ".join((getattr(user, "full_name", "") or str(user.id)).split())[:80]
        with self.store.tx() as db:
            if db.execute(
                "SELECT 1 FROM ga_receipts WHERE chat=? AND message=?",
                (str(chat.id), message.message_id),
            ).fetchone():
                return
            repeated = db.execute(
                "SELECT 1 FROM ga_receipts WHERE chat=? AND uid=? AND day=? AND digest=? LIMIT 1",
                (str(chat.id), str(user.id), day, digest),
            ).fetchone()
            if repeated:
                return
            last = db.execute(
                "SELECT MAX(at) FROM ga_receipts WHERE chat=? AND uid=? AND strict=1",
                (str(chat.id), str(user.id)),
            ).fetchone()[0]
            strict = int(
                len(meaningful) >= 5
                and len(set(meaningful)) >= 3
                and (last is None or at - last >= 30)
            )
            db.execute(
                "INSERT INTO ga_receipts VALUES(?,?,?,?,?,?,?,?,?,0)",
                (
                    str(chat.id),
                    message.message_id,
                    str(user.id),
                    name,
                    at,
                    day,
                    digest,
                    1,
                    strict,
                ),
            )
            db.execute(
                "INSERT INTO ga_daily VALUES(?,?,?,?,1,?) ON CONFLICT(chat,uid,day) "
                "DO UPDATE SET name=excluded.name,standard=standard+1,strict=strict+excluded.strict",
                (str(chat.id), str(user.id), day, name, strict),
            )

    def retract(self, chat, message):
        """Retract a confirmed bot deletion without changing frozen outcomes.

        Args:
            chat: Source group.
            message: Exact deleted message ID.
        """
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM ga_receipts WHERE chat=? AND message=? AND revoked=0",
                (str(chat), message),
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE ga_receipts SET revoked=1 WHERE chat=? AND message=?",
                    (str(chat), message),
                )
                db.execute(
                    "UPDATE ga_daily SET standard=MAX(0,standard-?),strict=MAX(0,strict-?) WHERE chat=? AND uid=? AND day=?",
                    (row["standard"], row["strict"], str(chat), row["uid"], row["day"]),
                )

    def row(self, identity):
        """Fetch one persisted activity.

        Args:
            identity: Activity ID.

        Returns:
            SQLite row.
        """
        row = self.store.db.execute(
            "SELECT * FROM ga_activities WHERE id=?", (identity,)
        ).fetchone()
        if not row:
            raise Rejected("活动不存在或已清理。")
        return row

    def draft(self, actor, chat):
        """Create an actor-bound draft with no side effects outside its group.

        Args:
            actor: Verified administrator.
            chat: Target group.

        Returns:
            Draft identity.
        """
        from .group_authority import versions

        self.store.require(actor, "moderation", chat=chat)
        identity = secrets.token_hex(6)
        cfg = {
            "kind": "rank",
            "title": "",
            "description": "",
            "start": 0,
            "duration": 86400,
            "preset": "standard",
            "minimum": 1,
            "profile": False,
            "link": "",
            "prizes": [],
            "_epoch": list(versions(self.store, chat)),
        }
        self.store.db.execute(
            "INSERT INTO ga_activities(id,chat,actor,state,config,created) VALUES(?,?,?,'draft',?,?)",
            (identity, str(chat), str(actor), encode(cfg), self.store.clock()),
        )
        return identity

    def save_draft(self, actor, identity, config, expected):
        """Save one wizard step with optimistic concurrency.

        Args:
            actor: Draft owner.
            identity: Activity ID.
            config: Complete draft state.
            expected: Draft version.
        """
        row = self.row(identity)
        from .group_authority import versions

        with self.store.tx() as db:
            self.store.require(actor, "moderation", db, chat=row["chat"])
            if (
                row["actor"] != str(actor)
                or row["state"] != "draft"
                or row["version"] != expected
                or json.loads(row["config"]).get("_epoch")
                != list(versions(self.store, row["chat"], db))
                or config.get("_epoch") != json.loads(row["config"]).get("_epoch")
            ):
                raise Rejected("草稿已变化，请重新打开活动设置。")
            db.execute(
                "UPDATE ga_activities SET config=?,version=version+1 WHERE id=?",
                (encode(config), identity),
            )

    def publish(self, actor, identity, expected):
        """Freeze rules and queue an initial panel, without starting early.

        Args:
            actor: Verified draft owner.
            identity: Draft ID.
            expected: Preview version.
        """
        row, now = self.row(identity), self.store.clock()
        cfg = json.loads(row["config"])
        if (
            cfg.get("kind") not in {"rank", "random"}
            or cfg.get("preset") not in {"standard", "strict"}
            or not isinstance(cfg.get("title"), str)
            or not 1 <= len(cfg["title"].strip()) <= 60
            or not isinstance(cfg.get("description"), str)
            or len(cfg["description"]) > 600
            or type(cfg.get("minimum")) is not int
            or not 1 <= cfg["minimum"] <= 100000
            or type(cfg.get("duration")) is not int
            or not 300 <= cfg["duration"] <= 30 * 86400
            or type(cfg.get("profile")) is not bool
            or (
                cfg["profile"]
                and (
                    not isinstance(cfg.get("link"), str)
                    or not re.fullmatch(
                        r"https://t\.me/(?:\+[A-Za-z0-9_-]+|[A-Za-z0-9_]{5,32}|joinchat/[A-Za-z0-9_-]+)",
                        cfg["link"],
                    )
                )
            )
            or not isinstance(cfg.get("prizes"), list)
            or not 1 <= len(cfg["prizes"]) <= 50
            or any(
                not isinstance(p, str) or not 1 <= len(p) <= 200 for p in cfg["prizes"]
            )
            or type(cfg.get("start")) not in {int, float}
            or not 0 <= cfg["start"] <= now + 30 * 86400
            or (cfg["start"] and cfg["start"] < now)
        ):
            raise Rejected("活动信息不完整或时间已过期，请返回逐项设置。")
        with self.store.tx() as db:
            self.store.require(actor, "moderation", db, chat=row["chat"])
            from .group_authority import versions

            if (
                row["actor"] != str(actor)
                or row["state"] != "draft"
                or row["version"] != expected
                or cfg.get("_epoch") != list(versions(self.store, row["chat"], db))
            ):
                raise Rejected("草稿或预览已失效。")
            if not self.config(row["chat"])["enabled"]:
                raise Rejected("请先开启本群排行榜与活动。")
            if db.execute(
                "SELECT 1 FROM ga_activities WHERE chat=? AND state IN ('publishing','active','review','finalizing','delivery_review')",
                (row["chat"],),
            ).fetchone():
                raise Rejected("本群已有未结案活动，请先完成或取消。")
            db.execute(
                "UPDATE ga_activities SET state='publishing',version=version+1 WHERE id=?",
                (identity,),
            )
            db.execute(
                "INSERT INTO ga_delivery(id,activity,kind,status) VALUES(?,?,'panel','pending')",
                (identity + ":panel", identity),
            )
            self.store.audit(
                db,
                actor,
                "activity_publish",
                {"id": identity, "chat": row["chat"], "rules": cfg},
            )

    async def member(self, chat, uid):
        """Fail closed on unknown Telegram membership.

        Args:
            chat: Exact group.
            uid: Candidate.
        """
        try:
            result = await self.runtime.bot.get_chat_member(chat, int(uid))
        except Exception as exc:
            raise Rejected("暂时无法核实成员身份，请稍后重试。") from exc
        if (
            result.status not in {"creator", "administrator", "member", "restricted"}
            or (
                result.status == "restricted"
                and not getattr(result, "is_member", False)
            )
            or getattr(getattr(result, "user", None), "is_bot", False)
        ):
            raise Rejected("仅仍在本群的真人成员可参与。")

    async def enroll(self, identity, uid, name, *, leave=False):
        """Enroll or withdraw before the persisted cutoff.

        Args:
            identity: Activity ID.
            uid: Real user.
            name: Display label.
            leave: Withdraw rather than join.
        """
        row = self.row(identity)
        from .group_authority import versions

        before = versions(self.store, row["chat"]), self.config(row["chat"])["version"]
        await self.member(row["chat"], uid)
        with self.store.tx() as db:
            current = self.row(identity)
            if (
                current["state"] != "active"
                or not current["starts"] <= self.store.clock() < current["ends"]
            ):
                raise Rejected("活动未开始或已截止，报名未改变。")
            if not self.config(row["chat"])["enabled"]:
                raise Rejected("本群已暂停新增活动报名。")
            if (
                before
                != (
                    versions(self.store, row["chat"], db),
                    self.config(row["chat"])["version"],
                )
                or not db.execute(
                    "SELECT enabled FROM mod_groups WHERE chat=?", (row["chat"],)
                ).fetchone()[0]
            ):
                raise Rejected("核验期间群设置已变化，报名未改变，请重试。")
            db.execute(
                "INSERT INTO ga_entries VALUES(?,?,?,?,?) ON CONFLICT(activity,uid) "
                "DO UPDATE SET withdrawn=excluded.withdrawn,name=excluded.name",
                (
                    identity,
                    str(uid),
                    " ".join(str(name).split())[:80],
                    self.store.clock(),
                    int(leave),
                ),
            )
            db.execute(
                "UPDATE ga_activities SET version=version+1 WHERE id=?", (identity,)
            )
            self.refresh_panel(db, identity)

    def refresh_panel(self, db, identity):
        """Coalesce edits to a known panel; never create a replacement message."""
        row = self.row(identity)
        if row["message"]:
            db.execute(
                "INSERT INTO ga_delivery(id,activity,kind,status) VALUES(?,?,'refresh','pending') "
                "ON CONFLICT(id) DO UPDATE SET status='pending',next=0 "
                "WHERE ga_delivery.status NOT IN ('sending','pending')",
                (identity + ":refresh", identity),
            )

    def counts(self, identity):
        """Calculate enrollee counts only within the frozen activity interval.

        Args:
            identity: Activity ID.

        Returns:
            Ordered eligible rows.
        """
        row = self.row(identity)
        cfg = json.loads(row["config"])
        column = "strict" if cfg["preset"] == "strict" else "standard"
        return self.store.db.execute(
            f"SELECT e.uid,e.name,COALESCE(SUM(r.{column}),0) AS count,COALESCE(MAX(r.at),0) AS reached "
            "FROM ga_entries e LEFT JOIN ga_receipts r ON r.chat=? AND r.uid=e.uid "
            f"AND r.at>=? AND r.at<? AND r.revoked=0 AND r.{column}=1 "
            "WHERE e.activity=? AND e.withdrawn=0 GROUP BY e.uid,e.name "
            "HAVING count>=? ORDER BY count DESC,reached,CAST(e.uid AS INTEGER)",
            (row["chat"], row["starts"], row["ends"], identity, cfg["minimum"]),
        ).fetchall()

    def freeze(self, identity):
        """Persist candidate order exactly once, including random backups.

        Args:
            identity: Due activity ID.
        """
        with self.store.tx() as db:
            row = self.row(identity)
            if row["state"] != "active" or row["ends"] > self.store.clock():
                return
            candidates = list(self.counts(identity))
            if json.loads(row["config"])["kind"] == "random":
                secrets.SystemRandom().shuffle(candidates)
            for position, entry in enumerate(candidates, 1):
                db.execute(
                    "INSERT INTO ga_candidates(activity,uid,name,position,count,reached) VALUES(?,?,?,?,?,?)",
                    (
                        identity,
                        entry["uid"],
                        entry["name"],
                        position,
                        entry["count"],
                        entry["reached"],
                    ),
                )
            db.execute(
                "UPDATE ga_activities SET state='review',version=version+1 WHERE id=?",
                (identity,),
            )
            self.refresh_panel(db, identity)
            self.store.audit(
                db,
                "system",
                "activity_frozen",
                {"id": identity, "candidates": len(candidates)},
            )

    async def review(self, actor, identity, target, decision, reason, expected):
        """Review one candidate without reshuffling or changing counts.

        Args:
            actor: Verified administrator.
            identity: Frozen activity.
            target: Candidate UID.
            decision: pass, reject or hold.
            reason: Required rejection explanation.
            expected: Activity revision.
        """
        row = self.row(identity)
        self.store.require(actor, "moderation", chat=row["chat"])
        if decision not in {"pass", "reject", "hold"} or (
            decision == "reject" and not reason.strip()
        ):
            raise Rejected("拒绝必须填写原因。")
        if decision == "pass":
            await self.member(row["chat"], target)
        await self.runtime.tenants.verify(actor, row["chat"], allow_paused=True)
        with self.store.tx() as db:
            self.store.require(actor, "moderation", db, chat=row["chat"])
            current = self.row(identity)
            if current["state"] != "review" or current["version"] != expected:
                raise Rejected("审核状态已变化，请刷新。")
            changed = db.execute(
                "UPDATE ga_candidates SET review=?,reviewer=?,reason=?,reviewed=? WHERE activity=? AND uid=?",
                (
                    decision,
                    str(actor),
                    reason[:600],
                    self.store.clock(),
                    identity,
                    str(target),
                ),
            )
            if not changed.rowcount:
                raise Rejected("候选人不存在。")
            db.execute(
                "UPDATE ga_activities SET version=version+1 WHERE id=?", (identity,)
            )
            self.store.audit(
                db,
                actor,
                "activity_review",
                {
                    "id": identity,
                    "uid": str(target),
                    "decision": decision,
                    "reason": reason[:600],
                },
            )

    def finalize(self, actor, identity, expected):
        """Assign text prizes and queue one immutable result.

        Args:
            actor: Verified administrator.
            identity: Reviewed activity.
            expected: Preview revision.
        """
        with self.store.tx() as db:
            row = self.row(identity)
            self.store.require(actor, "moderation", db, chat=row["chat"])
            if row["state"] != "review" or row["version"] != expected:
                raise Rejected("结果已确认或审核状态已变化，请刷新。")
            prizes, winners = json.loads(row["config"])["prizes"], []
            for candidate in db.execute(
                "SELECT * FROM ga_candidates WHERE activity=? ORDER BY position",
                (identity,),
            ):
                if len(winners) >= len(prizes):
                    break
                if candidate["review"] in {"pending", "hold"}:
                    raise Rejected("前序候选仍待审核，不能跳过后直接确认。")
                if candidate["review"] == "pass":
                    winners.append(candidate)
            for place, prize in enumerate(prizes, 1):
                winner = winners[place - 1] if place <= len(winners) else None
                db.execute(
                    "INSERT INTO ga_results(activity,place,uid,name,prize,at) VALUES(?,?,?,?,?,?)",
                    (
                        identity,
                        place,
                        winner["uid"] if winner else None,
                        winner["name"] if winner else "空缺",
                        prize,
                        self.store.clock(),
                    ),
                )
            db.execute(
                "UPDATE ga_activities SET state='finalizing',version=version+1 WHERE id=?",
                (identity,),
            )
            db.execute(
                "INSERT INTO ga_delivery(id,activity,kind,status) VALUES(?,?,'result','pending')",
                (identity + ":result", identity),
            )
            self.store.audit(
                db,
                actor,
                "activity_finalize",
                {"id": identity, "winners": [w["uid"] for w in winners]},
            )

    def fulfil(self, actor, identity, place, state, reason, expected):
        """Record manual fulfilment without changing prizes or any wallet.

        Args:
            actor: Verified administrator.
            identity: Finalized activity.
            place: Prize position.
            state: Manual fulfilment state.
            reason: Audit explanation.
            expected: Activity revision.
        """
        with self.store.tx() as db:
            row = self.row(identity)
            self.store.require(actor, "moderation", db, chat=row["chat"])
            if (
                row["state"] not in {"closed", "finalizing", "delivery_review"}
                or row["version"] != expected
            ):
                raise Rejected("结果或版本已变化，请刷新。")
            if (
                state not in {"pending", "sent", "waived", "disputed"}
                or not reason.strip()
            ):
                raise Rejected("请选择状态并填写说明。")
            changed = db.execute(
                "UPDATE ga_results SET fulfilment=?,actor=?,reason=?,at=? WHERE activity=? AND place=? AND uid IS NOT NULL",
                (state, str(actor), reason[:600], self.store.clock(), identity, place),
            )
            if not changed.rowcount:
                raise Rejected("该名次没有获奖人。")
            db.execute(
                "UPDATE ga_activities SET version=version+1 WHERE id=?", (identity,)
            )
            self.store.audit(
                db,
                actor,
                "activity_fulfilment",
                {
                    "id": identity,
                    "place": place,
                    "state": state,
                    "reason": reason[:600],
                },
            )

    def cancel(self, actor, identity, reason, expected):
        """Cancel explicitly, without erasing frozen evidence.

        Args:
            actor: Verified administrator.
            identity: Activity ID.
            reason: Human explanation.
            expected: Preview revision.
        """
        with self.store.tx() as db:
            row = self.row(identity)
            self.store.require(actor, "moderation", db, chat=row["chat"])
            if (
                row["version"] != expected
                or row["state"] not in {"draft", "active", "review"}
                or not reason.strip()
            ):
                raise Rejected("当前状态不能取消，请刷新；发送结果不明时须先核查。")
            db.execute(
                "UPDATE ga_activities SET state='cancelled',closed=?,error=?,version=version+1 WHERE id=?",
                (self.store.clock(), reason[:600], identity),
            )
            self.refresh_panel(db, identity)
            self.store.audit(
                db, actor, "activity_cancel", {"id": identity, "reason": reason[:600]}
            )

    def render(self, identity, *, final=False):
        """Render bounded HTML with whole prize entries kept together.

        Args:
            identity: Activity ID.
            final: Render the immutable result instead of the active panel.

        Returns:
            HTML pages and public button markup.
        """
        row = self.row(identity)
        cfg = json.loads(row["config"])
        title = html.escape(cfg["title"])
        if final:
            parts = [f"🏆 <b>{title} · 获奖结果</b>\n奖品由群管理员人工兑现。"]
            for result in self.store.db.execute(
                "SELECT * FROM ga_results WHERE activity=? ORDER BY place", (identity,)
            ):
                name = html.escape(result["name"])
                person = (
                    f'<a href="tg://user?id={result["uid"]}">{name}</a>'
                    if result["uid"]
                    else "空缺"
                )
                parts.append(
                    f"{result['place']}. {person} — <b>{html.escape(result['prize'])}</b>"
                )
            pages, page = [], parts[0]
            for part in parts[1:]:
                block = f"\n<blockquote expandable>{part}</blockquote>"
                if len(page) + len(block) > 3500:
                    pages.append(page)
                    page = f"🏆 <b>{title} · 续页</b>"
                page += block
            return [*pages, page], None
        count = self.store.db.execute(
            "SELECT COUNT(*) FROM ga_entries WHERE activity=? AND withdrawn=0",
            (identity,),
        ).fetchone()[0]
        starts = row["starts"] or cfg.get("start") or self.store.clock()
        ends = row["ends"] or starts + cfg["duration"]
        state = {
            "publishing": "准备发布",
            "active": "报名与统计中",
            "review": "已截止，人工审核中",
            "closed": "已公布",
            "cancelled": "已取消",
            "delivery_review": "投递待核查",
        }.get(row["state"], row["state"])
        rules = (
            f"类型：{'活跃排行奖励' if cfg['kind'] == 'rank' else '达标随机抽奖'}\n"
            f"统计：{'标准' if cfg['preset'] == 'standard' else '严格'} · 最少{cfg['minimum']}条\n"
            f"开始：{datetime.fromtimestamp(starts, TZ):%Y-%m-%d %H:%M:%S}\n"
            f"截止：{datetime.fromtimestamp(ends, TZ):%Y-%m-%d %H:%M:%S}（北京时间）\n"
            "点击报名后，计入活动开始以来的有效发言；命令、下注和重复刷屏不计。\n"
            "同次数按先达到者优先，再按数字UID；随机抽奖不按条数加权。\n"
            + (
                f"主页须挂：{cfg['link']}；管理员按审核时状态核验，不代表开奖时快照。\n"
                if cfg["profile"]
                else ""
            )
            + "\n".join(f"{i}. {p}" for i, p in enumerate(cfg["prizes"], 1))
        )
        # A long prize list belongs to the rules button, not a truncated HTML tag.
        if len(rules) > 2200:
            rules = rules[:1600] + "\n完整奖品请点「规则」。"
        text = f"🎉 <b>{title}</b>\n{html.escape(cfg['description'])}\n<b>{state}</b> · 已报名{count}人\n<blockquote expandable>{html.escape(rules)}</blockquote>"
        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ 报名", callback_data=f"ga:{identity}:join"
                    ),
                    InlineKeyboardButton(
                        "退出报名", callback_data=f"ga:{identity}:leave"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "我的进度", callback_data=f"ga:{identity}:progress"
                    ),
                    InlineKeyboardButton(
                        "📊 排行", callback_data=f"ga:{identity}:rank"
                    ),
                    InlineKeyboardButton(
                        "📖 规则", callback_data=f"ga:{identity}:rules"
                    ),
                ],
            ]
        )
        if row["state"] != "active":
            buttons = InlineKeyboardMarkup([buttons.inline_keyboard[1]])
        return [text], buttons

    async def deliver(self, task):
        """Send once after recording intent; uncertain sends require review.

        Args:
            task: Durable delivery row.
        """
        from .group_authority import GroupAuthority

        row = self.row(task["activity"])
        if task["kind"] == "panel":
            async with GroupAuthority(self.runtime).session(row["actor"], row["chat"]):
                if not self.config(row["chat"])["enabled"]:
                    raise Rejected("本群已关闭新活动发布。")
        pages, markup = self.render(row["id"], final=task["kind"] == "result")
        # Persist each result page separately so retries cannot repeat earlier pages.
        if task["kind"] == "result" and len(pages) > 1:
            with self.store.tx() as db:
                for index in range(1, len(pages)):
                    db.execute(
                        "INSERT OR IGNORE INTO ga_delivery(id,activity,kind,status) VALUES(?,?,?,'pending')",
                        (f"{row['id']}:result:{index}", row["id"], f"page:{index}"),
                    )
        if task["kind"].startswith("page:"):
            pages, markup = self.render(row["id"], final=True)
            text, markup = pages[int(task["kind"].split(":")[1])], None
        else:
            text = pages[0]
        with self.store.tx() as db:
            claimed = db.execute(
                "UPDATE ga_delivery SET status='sending' WHERE id=? AND status='pending'",
                (task["id"],),
            ).rowcount
        if not claimed:
            return
        if task["kind"] == "refresh":
            try:
                await self.runtime.bot.edit_message_text(
                    chat_id=row["chat"],
                    message_id=row["message"],
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            except RetryAfter as exc:
                delay = exc.retry_after
                seconds = (
                    delay.total_seconds()
                    if hasattr(delay, "total_seconds")
                    else float(delay)
                )
                self.store.db.execute(
                    "UPDATE ga_delivery SET status='pending',next=? WHERE id=?",
                    (self.store.clock() + seconds + 1, task["id"]),
                )
                return
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    self.store.db.execute(
                        "UPDATE ga_delivery SET status='failed',error=? WHERE id=?",
                        (type(exc).__name__, task["id"]),
                    )
                    return
            except Exception as exc:
                # Editing a known message is idempotent; a delayed refresh is safe.
                self.store.db.execute(
                    "UPDATE ga_delivery SET status='pending',next=?,error=? WHERE id=?",
                    (self.store.clock() + 30, type(exc).__name__, task["id"]),
                )
                return
            self.store.db.execute(
                "UPDATE ga_delivery SET status='sent',message=?,error='' WHERE id=?",
                (row["message"], task["id"]),
            )
            # A membership/freeze event may have arrived during the edit.
            if row["version"] != self.row(row["id"])["version"]:
                with self.store.tx() as db:
                    self.refresh_panel(db, row["id"])
            return
        try:
            sent = await self.runtime.bot.send_message(
                chat_id=row["chat"], text=text, parse_mode="HTML", reply_markup=markup
            )
            if type(getattr(sent, "message_id", None)) is not int:
                raise RuntimeError("MissingMessageReceipt")
        except RetryAfter as exc:
            delay = exc.retry_after
            delay = (
                delay.total_seconds()
                if hasattr(delay, "total_seconds")
                else float(delay)
            )
            self.store.db.execute(
                "UPDATE ga_delivery SET status='pending',next=?,error='RetryAfter' WHERE id=?",
                (self.store.clock() + delay + 1, task["id"]),
            )
            return
        except (Forbidden, BadRequest) as exc:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE ga_delivery SET status='failed',error=? WHERE id=?",
                    (type(exc).__name__, task["id"]),
                )
                db.execute(
                    "UPDATE ga_activities SET state='delivery_review',error=? WHERE id=?",
                    (type(exc).__name__, row["id"]),
                )
            return
        except Exception as exc:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE ga_delivery SET status='review',error=? WHERE id=?",
                    (type(exc).__name__, task["id"]),
                )
                db.execute(
                    "UPDATE ga_activities SET state='delivery_review',error=? WHERE id=?",
                    (type(exc).__name__, row["id"]),
                )
            return
        with self.store.tx() as db:
            now = self.store.clock()
            db.execute(
                "UPDATE ga_delivery SET status='sent',message=?,error='' WHERE id=?",
                (sent.message_id, task["id"]),
            )
            if task["kind"] == "panel":
                cfg = json.loads(row["config"])
                starts = cfg.get("start") or now
                db.execute(
                    "UPDATE ga_activities SET state='active',starts=?,ends=?,message=?,version=version+1 WHERE id=? AND state='publishing'",
                    (starts, starts + cfg["duration"], sent.message_id, row["id"]),
                )
                self.refresh_panel(db, row["id"])
            else:
                closed = db.execute(
                    "UPDATE ga_activities SET state='closed',closed=?,version=version+1 WHERE id=? "
                    "AND NOT EXISTS(SELECT 1 FROM ga_delivery WHERE activity=? "
                    "AND (kind='result' OR kind LIKE 'page:%') AND status<>'sent')",
                    (now, row["id"], row["id"]),
                ).rowcount
                if closed and row["message"]:
                    self.refresh_panel(db, row["id"])
                    db.execute(
                        "INSERT OR IGNORE INTO gt_delete(chat,message,source,due) VALUES(?,?,0,?)",
                        (
                            row["chat"],
                            row["message"],
                            now + retention(self.runtime, "activity_result"),
                        ),
                    )
                db.execute(
                    "INSERT OR IGNORE INTO gt_delete(chat,message,source,due) VALUES(?,?,0,?)",
                    (
                        row["chat"],
                        sent.message_id,
                        now + retention(self.runtime, "activity_result"),
                    ),
                )

    async def tick(self):
        """Freeze due activities, deliver bounded jobs and clean in batches."""
        for row in self.store.db.execute(
            "SELECT id FROM ga_activities WHERE state='active' AND ends<=? LIMIT 10",
            (self.store.clock(),),
        ).fetchall():
            self.freeze(row["id"])
        for task in self.store.db.execute(
            "SELECT * FROM ga_delivery WHERE status='pending' AND next<=? ORDER BY rowid LIMIT 4",
            (self.store.clock(),),
        ).fetchall():
            try:
                await self.deliver(task)
            except Rejected as exc:
                from .group_authority import VerificationUnavailable

                if isinstance(exc, VerificationUnavailable):
                    self.store.db.execute(
                        "UPDATE ga_delivery SET next=?,error='AuthorityUnavailable' WHERE id=? AND status='pending'",
                        (self.store.clock() + 30, task["id"]),
                    )
                    continue
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE ga_delivery SET status='failed',error=? WHERE id=?",
                        (str(exc)[:200], task["id"]),
                    )
                    db.execute(
                        "UPDATE ga_activities SET state='delivery_review',error='PermissionChanged' WHERE id=?",
                        (task["activity"],),
                    )
        if self.store.clock() - self.last_cleanup >= 60:
            self.last_cleanup = self.store.clock()
            self.cleanup()

    def cleanup(self):
        """Delete bounded expired statistics, never active activity evidence."""
        now = self.store.clock()
        with self.store.tx() as db:
            db.execute(
                "DELETE FROM ga_receipts WHERE rowid IN (SELECT r.rowid FROM ga_receipts r WHERE r.at<? "
                "AND NOT EXISTS(SELECT 1 FROM ga_activities a WHERE a.chat=r.chat "
                "AND a.state IN ('publishing','active','review','finalizing','delivery_review') "
                "AND (a.starts=0 OR r.at>=a.starts)) LIMIT 500)",
                (now - 7 * 86400,),
            )
            day = datetime.fromtimestamp(now - 90 * 86400, TZ).date().isoformat()
            db.execute(
                "DELETE FROM ga_daily WHERE rowid IN (SELECT rowid FROM ga_daily WHERE day<? LIMIT 500)",
                (day,),
            )
            expired = db.execute(
                "SELECT id FROM ga_activities a WHERE closed<? AND state IN ('closed','cancelled') "
                "AND NOT EXISTS(SELECT 1 FROM ga_results r WHERE r.activity=a.id AND r.fulfilment IN ('pending','disputed') AND r.uid IS NOT NULL) LIMIT 10",
                (now - 90 * 86400,),
            ).fetchall()
            for item in expired:
                for table in (
                    "ga_entries",
                    "ga_candidates",
                    "ga_results",
                    "ga_delivery",
                ):
                    db.execute(f"DELETE FROM {table} WHERE activity=?", (item["id"],))
                db.execute("DELETE FROM ga_activities WHERE id=?", (item["id"],))

    async def loop(self):
        """Run only while the existing transport is ready."""
        while True:
            if (
                getattr(self.runtime, "application", None)
                and self.runtime.application.running
            ):
                try:
                    await self.tick()
                except Exception as exc:
                    self.runtime.report("activities", exc)
            await asyncio.sleep(1)

    def statistics(self, chat, start, end, *, strict=False):
        """Query a bounded Beijing day interval.

        Args:
            chat: Exact group.
            start: Inclusive Beijing date.
            end: Exclusive Beijing date.
            strict: Select strict counters.

        Returns:
            Ranked daily aggregates.
        """
        if not 0 < (end - start).days <= 31:
            raise Rejected("统计范围需为1—31天。")
        column = "strict" if strict else "standard"
        return self.store.db.execute(
            f"SELECT uid,MAX(name) AS name,SUM({column}) AS count FROM ga_daily "
            f"WHERE chat=? AND day>=? AND day<? GROUP BY uid HAVING count>0 ORDER BY count DESC,CAST(uid AS INTEGER)",
            (str(chat), start.isoformat(), end.isoformat()),
        ).fetchall()

    def period(self, kind="today"):
        """Resolve today's or this week's Beijing date range.

        Args:
            kind: today or week.

        Returns:
            Inclusive and exclusive dates.
        """
        today = datetime.fromtimestamp(self.store.clock(), TZ).date()
        start = today - timedelta(days=today.weekday()) if kind == "week" else today
        return start, today + timedelta(days=1)
