"""Durable, minimal-detail notifications independent of moderation execution."""

import json

from telegram.error import BadRequest, Forbidden, RetryAfter

from .store import Rejected


class CommunityLogs:
    """Project only allowlisted event metadata into private administrative sinks."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS cm_delivery(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,destination TEXT NOT NULL,
                kind TEXT NOT NULL,text TEXT NOT NULL,actor TEXT NOT NULL,
                private INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,next REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS cm_projection(
                kind TEXT NOT NULL,id TEXT NOT NULL,state TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS cm_cursors(key TEXT PRIMARY KEY,value REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS cm_delivery_queue ON cm_delivery(status,next);
            UPDATE cm_delivery SET status='review',error='restart_unknown' WHERE status='sending';
        """)
        self.store.db.execute(
            "INSERT OR IGNORE INTO cm_cursors(key,value) VALUES('start',?)",
            (self.store.clock(),),
        )
        self.store.db.execute(
            "INSERT OR IGNORE INTO cm_cursors(key,value) SELECT 'audit',COALESCE(MAX(id),0) FROM audit"
        )

    async def check_channel(self, actor, channel):
        """Require a private channel and explicit actor/bot posting authority.

        Args:
            actor: Group-authorized configuring administrator.
            channel: Stable negative channel ID.

        Returns:
            Telegram channel information.
        """
        if not str(channel).startswith("-100") or not str(channel)[1:].isdigit():
            raise Rejected("请输入私有频道数字 ID")
        info = await self.runtime.bot.get_chat(str(channel))
        if (
            info.type != "channel"
            or getattr(info, "username", None)
            or getattr(info, "active_usernames", None)
        ):
            raise Rejected("日志仅支持专用私有频道")
        member = await self.runtime.bot.get_chat_member(str(channel), int(actor))
        own = await self.runtime.bot.get_chat_member(str(channel), self.runtime.bot.id)
        if member.status not in {"creator", "administrator"}:
            raise Rejected("你必须是日志频道管理员")
        if own.status != "creator" and (
            own.status != "administrator"
            or not getattr(own, "can_post_messages", False)
        ):
            raise Rejected("机器人需要日志频道发帖权限")
        return info

    def enqueue(self, key, chat, destination, kind, text, actor, private=False):
        """Persist one independently deliverable and deduplicated notice.

        Args:
            key: Stable event and recipient identity.
            chat: Source managed group.
            destination: Channel or established private user.
            kind: Fixed metadata category.
            text: Already redacted short message.
            actor: Administrator whose rights must remain valid.
            private: Whether the sink is a private administrator conversation.
        """
        self.store.db.execute(
            "INSERT OR IGNORE INTO cm_delivery(id,chat,destination,kind,text,actor,private) VALUES(?,?,?,?,?,?,?)",
            (
                key,
                str(chat),
                str(destination),
                kind,
                text[:1500],
                str(actor),
                int(private),
            ),
        )

    def project(self):
        """Capture fresh state changes without exporting evidence or reporter IDs.

        Returns:
            None.
        """
        start = self.store.db.execute(
            "SELECT value FROM cm_cursors WHERE key='start'"
        ).fetchone()[0]
        for table, key, timestamp, label in (
            ("mod_ops", "t.id", "at", "群管操作"),
            ("ak_hits", "t.chat || ':' || t.message", "at", "广告检测"),
            ("jv_entries", "t.token", "created", "入群验证"),
        ):
            rows = self.store.db.execute(
                f"SELECT t.*, {key} AS event_key FROM {table} t WHERE {timestamp}>=? AND status NOT IN ('executing','restricting','releasing') AND NOT EXISTS(SELECT 1 FROM cm_projection p WHERE p.kind=? AND p.id={key} AND p.state=t.status) LIMIT 40",
                (start, table),
            ).fetchall()
            for row in rows:
                policy = self.runtime.community.policy(row["chat"])
                log = policy["config"]["log"]
                if log["enabled"]:
                    target = row["uid"] if "uid" in row.keys() else ""
                    message = row["message"] if "message" in row.keys() else None
                    if table == "mod_ops" and row["action"] == "delete":
                        message = json.loads(row["payload"]).get("target")
                    link = (
                        f"\nhttps://t.me/c/{row['chat'].removeprefix('-100')}/{int(message)}"
                        if message and row["chat"].startswith("-100")
                        else ""
                    )
                    self.enqueue(
                        f"{table}:{row['event_key']}:{row['status']}",
                        row["chat"],
                        log["channel"],
                        table,
                        f"{label} · 群 {row['chat']}\n"
                        + (f"成员 {target}\n" if target else "")
                        + f"结果：{row['status']}"
                        + link
                        + "\n详情请在机器人私聊管理入口查看。",
                        policy["actor"],
                    )
                self.store.db.execute(
                    "INSERT INTO cm_projection(kind,id,state) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET state=excluded.state",
                    (table, row["event_key"], row["status"]),
                )
        cursor = self.store.db.execute(
            "SELECT value FROM cm_cursors WHERE key='audit'"
        ).fetchone()[0]
        rows = self.store.db.execute(
            "SELECT * FROM audit WHERE id>? ORDER BY id LIMIT 100", (cursor,)
        ).fetchall()
        categories = {
            "community_config": "群内容或日志配置变更",
            "community_case_closed": "举报处理",
            "community_violation": "违规记录新增",
            "community_revoke": "警告撤销",
            "ad_killer_policy": "广告杀手规则变更",
            "ad_killer_false_positive": "误判标记",
            "join_verify_policy": "入群验证配置变更",
            "join_verify_release": "入群验证放行",
        }
        for row in rows:
            if row["action"] in categories:
                data = json.loads(row["data"])
                chat = str(data.get("chat", ""))
                policy = self.runtime.community.policy(chat)
                log = policy["config"]["log"]
                if log["enabled"]:
                    # Only fixed metadata, never raw audit JSON or private detail.
                    text = f"{categories[row['action']]} · 群 {chat}"
                    if data.get("case"):
                        text += f"\n案件 #{int(data['case'])}"
                    self.enqueue(
                        f"audit:{row['id']}",
                        chat,
                        log["channel"],
                        "audit",
                        text,
                        policy["actor"],
                    )
            self.store.db.execute(
                "UPDATE cm_cursors SET value=? WHERE key='audit'", (row["id"],)
            )

    async def tick(self):
        """Reauthorize and claim one send; never replay uncertain submissions.

        Returns:
            None.
        """
        self.project()
        if not self.runtime.application or not self.runtime.application.running:
            return
        row = self.store.db.execute(
            "SELECT * FROM cm_delivery WHERE status='pending' AND next<=? ORDER BY rowid LIMIT 1",
            (self.store.clock(),),
        ).fetchone()
        if not row:
            return
        try:
            await self.runtime.moderation.check(row["actor"], row["chat"], "view")
            if row["private"]:
                if not self.store.db.execute(
                    "SELECT 1 FROM cm_private WHERE uid=?", (row["destination"],)
                ).fetchone():
                    raise Rejected("private_unavailable")
            else:
                policy = self.runtime.community.policy(row["chat"])
                log = policy["config"]["log"]
                if not log["enabled"] or log["channel"] != row["destination"]:
                    raise Rejected("destination_changed")
                await self.check_channel(row["actor"], row["destination"])
                await self.runtime.moderation.check(row["actor"], row["chat"], "view")
                current = self.runtime.community.policy(row["chat"])["config"]["log"]
                if not current["enabled"] or current["channel"] != row["destination"]:
                    raise Rejected("destination_changed")
        except Exception as exc:
            blocked = isinstance(exc, (Rejected, Forbidden, BadRequest))
            self.store.db.execute(
                "UPDATE cm_delivery SET status=?,next=?,attempts=attempts+1,error=? WHERE id=?",
                (
                    "blocked" if blocked else "pending",
                    self.store.clock() + min(3600, 30 * 2 ** min(row["attempts"], 7)),
                    type(exc).__name__,
                    row["id"],
                ),
            )
            return
        self.store.db.execute(
            "UPDATE cm_delivery SET status='sending',attempts=attempts+1 WHERE id=?",
            (row["id"],),
        )
        try:
            await self.runtime.bot.send_message(
                chat_id=row["destination"], text=row["text"], parse_mode=None
            )
        except RetryAfter as exc:
            wait = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else float(exc.retry_after)
            )
            self.store.db.execute(
                "UPDATE cm_delivery SET status='pending',next=?,error='rate_limit' WHERE id=?",
                (self.store.clock() + max(1, wait), row["id"]),
            )
        except (Forbidden, BadRequest) as exc:
            self.store.db.execute(
                "UPDATE cm_delivery SET status='blocked',error=? WHERE id=?",
                (type(exc).__name__, row["id"]),
            )
        except BaseException as exc:
            self.store.db.execute(
                "UPDATE cm_delivery SET status='review',error=? WHERE id=?",
                (type(exc).__name__, row["id"]),
            )
            if not isinstance(exc, Exception):
                raise
        else:
            self.store.db.execute(
                "UPDATE cm_delivery SET status='sent',error='' WHERE id=?", (row["id"],)
            )
