"""Configured rewards and account-scoped integer point movements."""

import hashlib
import re
import unicodedata
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo

from .store import Rejected

DEFAULT = {
    "enabled": False,
    "checkin": 0,
    "chat": 0,
    "interval": 60,
    "cap": 0,
    "groups": [],
}


def validate(config):
    """Validate both platform and owner-scoped reward configuration.

    Args:
        config: Complete reward configuration; no partial unchecked fields.

    Raises:
        Rejected: A field, bound or group identifier is invalid.
    """
    if (
        set(config) != set(DEFAULT)
        or type(config["enabled"]) is not bool
        or any(
            type(config[k]) is not int or not 0 <= config[k] <= 1000000
            for k in ("checkin", "chat", "cap")
        )
    ):
        raise Rejected("奖励数值无效，请检查开关、签到积分、聊天积分和每日上限")
    if type(config["interval"]) is not int or not 15 <= config["interval"] <= 86400:
        raise Rejected("聊天奖励间隔必须为15—86400秒")
    if (
        not isinstance(config["groups"], list)
        or len(config["groups"]) > 100
        or any(not re.fullmatch(r"-[0-9]+", str(g)) for g in config["groups"])
    ):
        raise Rejected("奖励群列表必须为群数字 ID，多个群用逗号分隔")


class Points:
    def __init__(self, store):
        self.store = store

    def configure(self, actor, config):
        with self.store.tx() as db:
            self.store.require(actor, "points", db)
            validate(config)
            self.store.put(db, "points", config)
            self.store.audit(db, actor, "points_config", config)

    def checkin(self, uid, chat=None, db=None):
        with self.store.tx() if db is None else nullcontext(db) as db:
            if (
                self.store.get("group_points_enabled", False, db)
                and not db.execute(
                    "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (str(chat),)
                ).fetchone()
            ):
                raise Rejected("请在已启用群中签到，积分按群独立")
            from .tenants import local_config

            cfg = local_config(
                self.store, chat, "points", self.store.get("points", DEFAULT, db), db
            )
            if not cfg["enabled"] or cfg["checkin"] <= 0:
                raise Rejected("签到奖励尚未启用")
            day = (
                datetime.fromtimestamp(self.store.clock(), ZoneInfo("Asia/Shanghai"))
                .date()
                .isoformat()
            )
            return self.store.credit(
                db, f"checkin/{uid}/{day}", uid, cfg["checkin"], "每日签到", chat=chat
            )

    def chat(self, uid, group, message_id, body):
        now = self.store.clock()
        day = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date().isoformat()
        body = unicodedata.normalize("NFKC", body).strip().lower()
        normalized = "".join(ch for ch in body if ch.isalnum())
        if len(normalized) < 5 or body.startswith("/") or len(set(normalized)) < 3:
            return False
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        with self.store.tx() as db:
            from .tenants import local_config

            cfg = local_config(
                self.store, group, "points", self.store.get("points", DEFAULT, db), db
            )
            if (
                not cfg["enabled"]
                or str(group) not in cfg["groups"]
                or cfg["chat"] <= 0
            ):
                return False
            scoped = self.store.get("group_points_enabled", False, db)
            if (
                scoped
                and not db.execute(
                    "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (str(group),)
                ).fetchone()
            ):
                return False
            rewards = "group_rewards" if scoped else "rewards"
            seen = "group_chat_seen" if scoped else "chat_seen"
            where = "chat=? AND " if scoped else ""
            prefix = (str(group),) if scoped else ()
            row = db.execute(
                f"SELECT last,earned FROM {rewards} WHERE {where}uid=? AND day=?",
                prefix + (str(uid), day),
            ).fetchone()
            last, earned = tuple(row) if row else (0, 0)
            if (
                now - last < cfg["interval"]
                or earned >= cfg["cap"]
                or db.execute(
                    f"SELECT 1 FROM {seen} WHERE {where}uid=? AND day=? AND digest=?",
                    prefix + (str(uid), day, digest),
                ).fetchone()
            ):
                return False
            amount = min(cfg["chat"], cfg["cap"] - earned)
            if not self.store.credit(
                db,
                f"chat/{group}/{message_id}",
                uid,
                amount,
                "有效聊天奖励",
                chat=group,
            ):
                return False
            db.execute(
                f"INSERT INTO {rewards}({'chat,' if scoped else ''}uid,day,last,earned) "
                f"VALUES({'?,' if scoped else ''}?,?,?,?) "
                f"ON CONFLICT({'chat,' if scoped else ''}uid,day) DO UPDATE SET last=excluded.last,earned=excluded.earned",
                prefix + (str(uid), day, now, earned + amount),
            )
            db.execute(
                f"INSERT INTO {seen}({'chat,' if scoped else ''}uid,day,digest) VALUES({'?,' if scoped else ''}?,?,?)",
                prefix + (str(uid), day, digest),
            )
            return True

    def adjust(self, actor, target, amount, reason, op, chat=None):
        if not str(target).isdigit() or not reason.strip() or len(reason) > 200:
            raise Rejected("请提供有效 UID 和调整原因")
        with self.store.tx() as db:
            self.store.require(actor, "points", db, chat=chat)
            if (
                self.store.get("group_points_enabled", False, db)
                and not db.execute(
                    "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (str(chat),)
                ).fetchone()
            ):
                raise Rejected("请指定已启用群的数字ID，旧调整确认已失效")
            if self.store.credit(
                db, op, target, amount, "管理员调整：" + reason, chat=chat
            ):
                self.store.audit(
                    db,
                    actor,
                    "points_adjust",
                    {
                        "uid": str(target),
                        "amount": amount,
                        "reason": reason,
                        "chat": str(chat or ""),
                    },
                )
