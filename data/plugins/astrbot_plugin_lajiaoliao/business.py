"""Account-scoped text businesses; unsupported native actions never execute."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from astrbot.core.platform.instance_control import instance_lock
from astrbot.core.platform.message_policy import require_message
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .ai import AILedger
from .automation import AUTOMATION_DEFAULTS, AutomationLedger, validate_automation
from .lottery import LotteryLedger, result_gif
from .maintenance import (
    LOTTERY_PLAN_DEFAULTS,
    MAINTENANCE_DEFAULTS,
    REMINDER_PLAN_DEFAULTS,
    MaintenanceLedger,
    ScheduleBoundaryExpired,
    require_current_boundary,
    validate_maintenance,
    validate_plans,
)
from .storage import SessionError
from .test_window import GroupManagerTestPermit, NativeTestPermit

ZONE = ZoneInfo("Asia/Shanghai")
DEFAULTS = {
    "group_ai_enabled": False,
    "commands_enabled": True,
    "rankings_enabled": False,
    "lottery_enabled": False,
    "reminders_enabled": False,
    "review_enabled": False,
    "ai_management_enabled": False,
    "automation": copy.deepcopy(AUTOMATION_DEFAULTS),
    "native_actions": [],
    "manager_user_ids": [],
    "invitations_enabled": False,
    "invitation_rewards_enabled": False,
    "invitation_points": 1,
    "keywords": [],
    "review_provider_id": "",
    "maintenance": copy.deepcopy(MAINTENANCE_DEFAULTS),
    "lottery_plan": copy.deepcopy(LOTTERY_PLAN_DEFAULTS),
    "reminder_plan": copy.deepcopy(REMINDER_PLAN_DEFAULTS),
    "lottery": {
        "prize": "",
        "winners": 3,
        "minutes": 10,
        "capacity": 15,
        "min_invitations": 0,
        "contact": "",
        "notify_results": False,
    },
}

NATIVE_ACTIONS = {
    "mute": "成员禁言",
    "unmute": "成员解禁",
    "kick": "人工踢人",
    "announce": "群公告",
    "mute_all": "全员禁言",
    "unmute_all": "解除全员禁言",
    "revoke_others": "撤回普通成员消息",
    "rename_others": "自动同步普通成员名片",
    "cleanup": "清理已核验不可用账号",
    "reply_recall": "撤回自己的群功能回复",
}
UNSUPPORTED = {
    "audio": "语音：本轮不接入",
    "file": "文件：本轮不接入",
    "mention": "@：本轮不新增",
    "image_quote": "引用图片：待单独验证",
}


def validate_settings(value: dict) -> dict:
    """Validate text-only settings without granting upstream capabilities.

    Args:
        value: Complete group business settings.

    Returns:
        Normalized private copy.

    Raises:
        ValueError: If settings are malformed or rely on unverified capabilities.
    """
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("invalid_business_settings")
    value = {
        **copy.deepcopy(DEFAULTS),
        "group_ai_enabled": value.get("group_ai_enabled", True),
        "invitation_rewards_enabled": value.get("invitations_enabled", False),
        **value,
    }
    managers = value["manager_user_ids"]
    if (
        not isinstance(managers, list)
        or len(managers) > 100
        or any(
            not isinstance(uid, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", uid)
            for uid in managers
        )
        or len(managers) != len(set(managers))
    ):
        raise ValueError("invalid_group_managers")
    for field in (
        "group_ai_enabled",
        "commands_enabled",
        "rankings_enabled",
        "lottery_enabled",
        "reminders_enabled",
        "review_enabled",
        "invitations_enabled",
        "invitation_rewards_enabled",
        "ai_management_enabled",
    ):
        if type(value[field]) is not bool:
            raise ValueError("invalid_business_switch")
    actions = value["native_actions"]
    if (
        not isinstance(actions, list)
        or any(not isinstance(a, str) or a not in NATIVE_ACTIONS for a in actions)
        or len(actions) != len(set(actions))
        or type(value["invitation_points"]) is not int
        or not 0 <= value["invitation_points"] <= 10000
    ):
        raise ValueError("invalid_action_grants")
    words = value["keywords"]
    if (
        not isinstance(words, list)
        or len(words) > 100
        or any(
            not isinstance(word, str) or not 1 <= len(word.strip()) <= 64
            for word in words
        )
    ):
        raise ValueError("invalid_business_keywords")
    if (
        not isinstance(value["review_provider_id"], str)
        or len(value["review_provider_id"]) > 256
    ):
        raise ValueError("invalid_review_provider")
    value["automation"] = validate_automation(value["automation"])
    value["maintenance"] = validate_maintenance(value["maintenance"])
    value["lottery_plan"], value["reminder_plan"] = validate_plans(
        value["lottery_plan"], value["reminder_plan"]
    )
    if value["invitation_rewards_enabled"] and not value["invitations_enabled"]:
        raise ValueError("invitation_tracking_required")
    maintenance = value["maintenance"]
    required = []
    if maintenance["quiet_enabled"]:
        required += ["mute_all", "unmute_all"]
    if maintenance["cards_enabled"]:
        required.append("rename_others")
    if maintenance["cleanup_enabled"]:
        required.append("cleanup")
    if maintenance["reply_recall_enabled"]:
        required.append("reply_recall")
    if (
        value["automation"]["punishment_mode"] == "recall_progressive"
        or value["automation"]["flood_recall"]
    ):
        required.append("revoke_others")
    if any(action not in actions for action in required):
        raise ValueError("maintenance_action_grant_required")
    if value["automation"]["enabled"] and (
        not value["review_enabled"] or "mute" not in actions
    ):
        raise ValueError("automation_requires_review_and_mute")
    if (
        value["automation"]["enabled"]
        and value["automation"]["ai_enabled"]
        and not value["review_provider_id"]
    ):
        raise ValueError("automation_requires_review_provider")
    lottery = value["lottery"]
    if not isinstance(lottery, dict) or set(lottery) - set(DEFAULTS["lottery"]):
        raise ValueError("invalid_lottery_settings")
    lottery = value["lottery"] = {"notify_results": False, **lottery}
    if (
        set(lottery) != set(DEFAULTS["lottery"])
        or type(lottery["notify_results"]) is not bool
    ):
        raise ValueError("invalid_lottery_settings")
    for field, maximum, minimum in (
        ("winners", 100, 1),
        ("minutes", 10080, 1),
        ("capacity", 10000, 0),
        ("min_invitations", 10000, 0),
    ):
        if type(lottery[field]) is not int or not minimum <= lottery[field] <= maximum:
            raise ValueError(
                "invitation_threshold_unverified"
                if field == "min_invitations"
                else "invalid_lottery_number"
            )
    if lottery["capacity"] and lottery["winners"] > lottery["capacity"]:
        raise ValueError("invalid_lottery_capacity")
    if lottery["notify_results"] and not 1 <= lottery["capacity"] <= 100:
        raise ValueError("lottery_notification_capacity")
    for field in ("prize", "contact"):
        if (
            not isinstance(lottery[field], str)
            or len(lottery[field]) > 128
            or any(unicodedata.category(c).startswith("C") for c in lottery[field])
        ):
            raise ValueError("invalid_lottery_text")
    result = copy.deepcopy(value)
    result["keywords"] = list(dict.fromkeys(word.strip() for word in words))
    if lottery["min_invitations"] and not value["invitations_enabled"]:
        raise ValueError("invitation_tracking_required")
    return result


def next_daily(clock: str, now: float) -> float:
    """Find the next future Beijing-time daily boundary.

    Args:
        clock: HH:MM, 24-hour time.
        now: Current Unix time.

    Returns:
        The next strictly future Unix timestamp.

    Raises:
        ValueError: If the clock is invalid.
    """
    if not isinstance(clock, str) or not re.fullmatch(
        r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock
    ):
        raise ValueError("invalid_schedule_clock")
    hour, minute = map(int, clock.split(":"))
    local = datetime.fromtimestamp(now, ZONE)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


class BusinessStore(AILedger, AutomationLedger, MaintenanceLedger, LotteryLedger):
    """One SQLite database per authorized account, without credential reads."""

    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "business.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(
                    group_id TEXT PRIMARY KEY, version INTEGER NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(
                    group_id TEXT, message_id TEXT, member_id TEXT, name TEXT,
                    stamp REAL, day TEXT, digest TEXT, counted INTEGER, revoked INTEGER DEFAULT 0,
                    PRIMARY KEY(group_id,message_id));
                CREATE INDEX IF NOT EXISTS ranking_lookup ON observations(group_id,day,counted,revoked);
                CREATE INDEX IF NOT EXISTS member_recent ON observations(group_id,member_id,stamp);
                CREATE INDEX IF NOT EXISTS observation_expiry ON observations(stamp);
                CREATE TABLE IF NOT EXISTS activities(
                    id TEXT PRIMARY KEY, group_id TEXT, owner TEXT, umo TEXT, version INTEGER,
                    state TEXT, starts REAL, ends REAL, parameters TEXT, winners TEXT, receipt TEXT);
                CREATE INDEX IF NOT EXISTS activity_group ON activities(group_id,starts);
                CREATE TABLE IF NOT EXISTS enrollment(
                    activity_id TEXT, member_id TEXT, name TEXT, stamp REAL,
                    PRIMARY KEY(activity_id,member_id));
                CREATE TABLE IF NOT EXISTS reminders(
                    id TEXT PRIMARY KEY, group_id TEXT, owner TEXT, umo TEXT, clock TEXT,
                    text TEXT, state TEXT, next_run REAL, revision INTEGER, receipt TEXT);
                CREATE TABLE IF NOT EXISTS drafts(
                    id TEXT PRIMARY KEY, group_id TEXT, owner TEXT, umo TEXT,
                    expires REAL, version INTEGER, value TEXT);
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT,
                    stamp REAL, kind TEXT, subject TEXT, status TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS invitation_epoch(
                    group_id TEXT PRIMARY KEY, stamp REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS invitation_members(
                    group_id TEXT, member_id TEXT, PRIMARY KEY(group_id,member_id));
                CREATE TABLE IF NOT EXISTS invitations(
                    group_id TEXT, member_id TEXT, inviter_id TEXT, message_id TEXT,
                    stamp REAL, points INTEGER,
                    PRIMARY KEY(group_id,member_id));
                CREATE TABLE IF NOT EXISTS reward_epoch(
                    group_id TEXT PRIMARY KEY, stamp REAL NOT NULL);
            """)
            self.initialize_automation(db)
            self.initialize_maintenance(db)
            self.initialize_lottery(db)
            self.initialize_ai(db)
            # A crash after reserving an effect must not repeat it at startup.
            db.execute("UPDATE reminders SET state='unknown' WHERE state='sending'")
            db.execute(
                "UPDATE activities SET state='unknown' WHERE state IN ('opening','publishing','drawing')"
            )
        for suffix in ("-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if path.exists():
                path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def settings(self, group):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM settings WHERE group_id=?", (group,)
            ).fetchone()
        return {
            "version": row["version"] if row else 0,
            "settings": validate_settings(json.loads(row["value"]))
            if row
            else copy.deepcopy(DEFAULTS),
        }

    def seen(self, group, message):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM observations WHERE group_id=? AND message_id=?",
                    (group, message),
                ).fetchone()
                is not None
            )

    def save_settings(self, group, version, value, actor, baseline=None):
        value = validate_settings(value)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT version FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            if type(version) is not int or version != (
                current["version"] if current else 0
            ):
                raise SessionError("business_changed_refresh_required")
            active = db.execute(
                "SELECT parameters FROM activities WHERE group_id=? AND state IN ('opening','open','drawing','publishing','unknown')",
                (group,),
            ).fetchone()
            old = db.execute(
                "SELECT value FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            old_value = validate_settings(json.loads(old["value"])) if old else DEFAULTS
            if value["invitations_enabled"] and not old_value["invitations_enabled"]:
                if not isinstance(baseline, list):
                    raise SessionError("invitation_baseline_required")
                db.execute(
                    "INSERT INTO invitation_epoch VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET stamp=excluded.stamp",
                    (group, time.time()),
                )
                db.executemany(
                    "INSERT OR IGNORE INTO invitation_members VALUES(?,?)",
                    [(group, uid) for uid in baseline],
                )
            if active and value["lottery"] != old_value["lottery"]:
                raise SessionError("lottery_settings_frozen")
            if (
                value["invitation_rewards_enabled"]
                and not old_value["invitation_rewards_enabled"]
            ):
                db.execute(
                    "INSERT INTO reward_epoch VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET stamp=excluded.stamp",
                    (group, time.time()),
                )
            db.execute(
                "INSERT INTO settings VALUES(?,?,?) ON CONFLICT(group_id) DO UPDATE SET version=excluded.version,value=excluded.value",
                (group, version + 1, json.dumps(value)),
            )
            self.configure_plans(db, group, old_value, value, actor)
            if not value["lottery_enabled"]:
                db.execute(
                    "UPDATE activities SET state='cancelled' WHERE group_id=? AND state='open'",
                    (group,),
                )
            else:
                db.execute(
                    "UPDATE activities SET version=? WHERE group_id=? AND state='open'",
                    (version + 1, group),
                )
            if not value["reminders_enabled"]:
                db.execute(
                    "UPDATE reminders SET state='paused' WHERE group_id=? AND state='active'",
                    (group,),
                )
            else:
                db.execute(
                    "UPDATE reminders SET revision=? WHERE group_id=? AND state='active'",
                    (version + 1, group),
                )
            db.execute("DELETE FROM drafts WHERE group_id=?", (group,))
            for uid in set(old_value["manager_user_ids"]) - set(
                value["manager_user_ids"]
            ):
                db.execute(
                    "UPDATE reminders SET state='paused' WHERE group_id=? AND owner=? AND state='active'",
                    (group, uid),
                )
                db.execute(
                    "UPDATE activities SET state='cancelled' WHERE group_id=? AND owner=? AND state='open'",
                    (group, uid),
                )
            self.log(
                db, group, "settings", actor, "saved", "version=" + str(version + 1)
            )
        return {"version": version + 1, "settings": value}

    def managed_groups(self, uid):
        with self.connect() as db:
            return [
                row["group_id"]
                for row in db.execute(
                    "SELECT group_id FROM settings WHERE EXISTS ("
                    "SELECT 1 FROM json_each(settings.value, '$.manager_user_ids') "
                    "WHERE type='text' AND value=?)",
                    (str(uid),),
                )
            ]

    def invitation_credit(self, group, note, roster, excluded):
        """Credit only fresh server-proven invitations, once per member lifetime."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            value = validate_settings(json.loads(row["value"])) if row else DEFAULTS
            epoch = db.execute(
                "SELECT stamp FROM invitation_epoch WHERE group_id=?", (group,)
            ).fetchone()
            stamp = note.get("timestamp", 0)
            if (
                not value["invitations_enabled"]
                or not epoch
                or not isinstance(stamp, (int, float))
                or not epoch["stamp"] <= stamp <= time.time() + 5
                or time.time() - stamp
                > (86400 if note.get("reconciled") is True else 300)
            ):
                return 0
            count = 0
            for uid in note.get("member_ids", []):
                member = roster.get(uid)
                if not member or uid in excluded:
                    continue
                inviter = member.get("inviterUserID", "")
                joined_ms = member.get("joinTime", 0)
                if type(joined_ms) not in {int, float}:
                    continue
                joined = joined_ms / 1000
                if (
                    member.get("groupID") != group
                    or member.get("userID") != uid
                    or member.get("joinSource") != 2
                    or inviter not in roster
                    or inviter == uid
                    or inviter in excluded
                    or not epoch["stamp"] <= joined <= time.time() + 5
                    or abs(joined - stamp) > 300
                    or (note.get("inviter_id") and note["inviter_id"] != inviter)
                ):
                    continue
                fresh = db.execute(
                    "INSERT OR IGNORE INTO invitation_members VALUES(?,?)", (group, uid)
                ).rowcount
                if not fresh:
                    continue
                db.execute(
                    "INSERT INTO invitations VALUES(?,?,?,?,?,?)",
                    (
                        group,
                        uid,
                        inviter,
                        note["message_id"],
                        stamp,
                        value["invitation_points"]
                        if value["invitation_rewards_enabled"]
                        and (
                            not (
                                reward := db.execute(
                                    "SELECT stamp FROM reward_epoch WHERE group_id=?",
                                    (group,),
                                ).fetchone()
                            )
                            or stamp >= reward["stamp"]
                        )
                        else 0,
                    ),
                )
                self.log(db, group, "invitation", uid, "credited", "inviter=" + inviter)
                db.execute(
                    "DELETE FROM pending_invitations WHERE group_id=? AND member_id=?",
                    (group, uid),
                )
                count += 1
            return count

    def invitation_summary(self, group, inviter=""):
        with self.connect() as db:
            if inviter:
                return [
                    dict(row)
                    for row in db.execute(
                        "SELECT member_id,stamp,points FROM invitations WHERE group_id=? AND inviter_id=? ORDER BY stamp DESC LIMIT 100",
                        (group, inviter),
                    )
                ]
            return [
                dict(row)
                for row in db.execute(
                    "SELECT inviter_id,COUNT(*) AS count,SUM(points) AS points FROM invitations WHERE group_id=? GROUP BY inviter_id ORDER BY points DESC,inviter_id LIMIT 100",
                    (group,),
                )
            ]

    def invitation_totals(self, group, inviter):
        with self.connect() as db:
            return dict(
                db.execute(
                    "SELECT COUNT(*) AS count,COALESCE(SUM(points),0) AS points FROM invitations WHERE group_id=? AND inviter_id=?",
                    (group, inviter),
                ).fetchone()
            )

    def prepare_native(self, group, owner, umo, version, payload):
        identifier = secrets.token_hex(16)
        with self.connect() as db:
            db.execute("DELETE FROM drafts WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    group,
                    owner,
                    umo,
                    time.time() + 600,
                    version,
                    json.dumps({"kind": "native", "payload": payload}),
                ),
            )
        return identifier

    def claim_native(self, group, owner, umo, identifier, version, kick):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM drafts WHERE id=?", (identifier,)
            ).fetchone()
            if (
                not row
                or row["group_id"] != group
                or row["owner"] != owner
                or row["umo"] != umo
                or row["expires"] < time.time()
                or row["version"] != version
            ):
                raise SessionError("native_preview_invalid")
            value = json.loads(row["value"])
            if (
                value.get("kind") != "native"
                or (value["payload"]["action"] == "kick") != kick
            ):
                raise SessionError("native_preview_invalid")
            # Consume before mutation; uncertain sends can only be queried.
            db.execute("DELETE FROM drafts WHERE id=?", (identifier,))
            return value["payload"]

    @staticmethod
    def supersede(db, group):
        """Stop known old-version jobs without altering in-flight/unknown effects."""
        db.execute(
            "UPDATE activities SET state='cancelled' WHERE group_id=? AND state='open'",
            (group,),
        )
        db.execute(
            "UPDATE reminders SET state='paused' WHERE group_id=? AND state='active'",
            (group,),
        )

    @staticmethod
    def log(db, group, kind, subject, status, detail=""):
        db.execute(
            "INSERT INTO audit(group_id,stamp,kind,subject,status,detail) VALUES(?,?,?,?,?,?)",
            (group, time.time(), kind, subject, status, detail[:512]),
        )
        db.execute(
            "DELETE FROM audit WHERE id IN (SELECT id FROM audit ORDER BY id DESC LIMIT -1 OFFSET 10000)"
        )

    def audit(self, group, before=0):
        if type(before) is not int or before < 0:
            raise ValueError("invalid_audit_cursor")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM audit WHERE group_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 100",
                (group, before, before),
            ).fetchall()
        return [dict(row) for row in rows]

    def observe(self, group, message, member, name, stamp, text, counted, review):
        now = time.time()
        if not 0 < stamp <= now + 5 or now - stamp > 300 or len(text) > 16384:
            return
        day = datetime.fromtimestamp(stamp, ZONE).strftime("%Y-%m-%d")
        normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM observations WHERE group_id=? AND message_id=?",
                (group, message),
            ).fetchone():
                return False
            last = db.execute(
                "SELECT stamp FROM observations WHERE group_id=? AND member_id=? AND counted=1 ORDER BY stamp DESC LIMIT 1",
                (group, member),
            ).fetchone()
            duplicate = db.execute(
                "SELECT 1 FROM observations WHERE group_id=? AND member_id=? AND digest=? AND stamp>?",
                (group, member, digest, stamp - 300),
            ).fetchone()
            counted = bool(
                counted
                and not review
                and normalized
                and any(c.isalnum() for c in normalized)
                and not duplicate
                and (not last or stamp - last["stamp"] >= 30)
            )
            db.execute(
                "INSERT INTO observations(group_id,message_id,member_id,name,stamp,day,digest,counted) VALUES(?,?,?,?,?,?,?,?)",
                (group, message, member, name[:128], stamp, day, digest, int(counted)),
            )
            if review:
                self.log(db, group, "review", message, "review_only", review)
            # Ranking evidence is bounded in time; no chat bodies are retained.
            db.execute("DELETE FROM observations WHERE stamp<?", (now - 31 * 86400,))
        return True

    def revoked(self, group, messages):
        with self.connect() as db:
            db.executemany(
                "UPDATE observations SET revoked=1 WHERE group_id=? AND message_id=?",
                [(group, message) for message in messages],
            )

    def ranking(self, group, page=1):
        if type(page) is not int or not 1 <= page <= 500:
            raise ValueError("invalid_ranking_page")
        day = datetime.now(ZONE).strftime("%Y-%m-%d")
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT member_id,MAX(name) AS name,COUNT(*) AS count,MAX(stamp) AS reached
                FROM observations WHERE group_id=? AND day=? AND counted=1 AND revoked=0
                GROUP BY member_id ORDER BY count DESC,reached,member_id LIMIT 20 OFFSET ?
            """,
                (group, day, (page - 1) * 20),
            ).fetchall()
        return [dict(row) for row in rows]

    def activities(self, group):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM activities WHERE group_id=? ORDER BY starts DESC LIMIT 20",
                (group,),
            ).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["parameters"] = json.loads(value["parameters"])
                value["winners"] = json.loads(value["winners"] or "[]")
                value["enrolled"] = db.execute(
                    "SELECT COUNT(*) FROM enrollment WHERE activity_id=?", (row["id"],)
                ).fetchone()[0]
                value["notifications"] = self.notification_summary(db, row["id"])
                result.append(value)
        return result

    def prepare_activity(self, group, owner, umo, version):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT version,value FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            if not row or row["version"] != version:
                raise SessionError("business_changed_refresh_required")
            settings = json.loads(row["value"])
            params = settings["lottery"]
            if (
                not settings["lottery_enabled"]
                or not params["prize"]
                or not params["contact"]
            ):
                raise SessionError("lottery_disabled_or_incomplete")
            if db.execute(
                "SELECT 1 FROM activities WHERE group_id=? AND state IN ('opening','open','drawing','publishing','unknown')",
                (group,),
            ).fetchone():
                raise SessionError("lottery_already_active")
            identifier = secrets.token_hex(16)
            db.execute(
                "INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    group,
                    owner,
                    umo,
                    version,
                    "opening",
                    now,
                    now + params["minutes"] * 60,
                    json.dumps(params),
                    "[]",
                    "",
                ),
            )
            self.log(db, group, "lottery", identifier, "opening")
        return identifier, params

    def activity_receipt(self, identifier, stage, receipt):
        status = receipt.get("status")
        state = (
            ("open" if stage == "opening" else "finished")
            if status == "sent"
            else (
                "failed"
                if status in {"rejected", "unsupported", "failed"}
                else "unknown"
            )
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT group_id,state FROM activities WHERE id=?", (identifier,)
            ).fetchone()
            if row is None or row["state"] != stage:
                raise SessionError("activity_state_changed")
            db.execute(
                "UPDATE activities SET state=?,receipt=? WHERE id=?",
                (state, json.dumps(receipt), identifier),
            )
            self.log(db, row["group_id"], "lottery", identifier, state)
        return state

    def enroll(self, group, member, name, stamp, message_id=""):
        if not isinstance(message_id, str) or (
            message_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", message_id)
        ):
            raise ValueError("invalid_signup_message_id")
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = db.execute(
                "SELECT version,value FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            if not settings or not json.loads(settings["value"])["lottery_enabled"]:
                raise SessionError("lottery_disabled_or_incomplete")
            row = db.execute(
                "SELECT * FROM activities WHERE group_id=? AND state='open' ORDER BY starts DESC LIMIT 1",
                (group,),
            ).fetchone()
            if row is None:
                raise SessionError("lottery_not_open")
            if row["version"] != settings["version"]:
                raise SessionError("business_changed_refresh_required")
            if (
                not row["starts"] <= stamp <= now + 5
                or stamp > row["ends"]
                or now > row["ends"]
                or now - stamp > 300
            ):
                raise SessionError("lottery_registration_expired")
            count = db.execute(
                "SELECT COUNT(*) FROM enrollment WHERE activity_id=?", (row["id"],)
            ).fetchone()[0]
            if db.execute(
                "SELECT 1 FROM enrollment WHERE activity_id=? AND member_id=?",
                (row["id"], member),
            ).fetchone():
                return "already_enrolled"
            params = json.loads(row["parameters"])
            invitations = db.execute(
                "SELECT COUNT(*) FROM invitations WHERE group_id=? AND inviter_id=?",
                (group, member),
            ).fetchone()[0]
            if invitations < params.get("min_invitations", 0):
                raise SessionError("lottery_invitation_threshold")
            if count >= 10000 or params["capacity"] and count >= params["capacity"]:
                raise SessionError("lottery_capacity_reached")
            db.execute(
                "INSERT INTO enrollment VALUES(?,?,?,?)",
                (row["id"], member, name[:128], stamp),
            )
            if message_id:
                db.execute(
                    "INSERT INTO enrollment_messages VALUES(?,?,?)",
                    (row["id"], member, message_id),
                )
            self.log(db, group, "enrollment", member, "enrolled")
        return "enrolled"

    def draw(self, identifier, eligible):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (identifier,)
            ).fetchone()
            if not row or row["state"] != "open":
                raise SessionError("lottery_not_open")
            people = [
                dict(item)
                for item in db.execute(
                    "SELECT member_id,name FROM enrollment WHERE activity_id=?",
                    (identifier,),
                )
                if item["member_id"] in eligible
            ]
            params = json.loads(row["parameters"])
            if params.get("min_invitations", 0):
                people = [
                    person
                    for person in people
                    if db.execute(
                        "SELECT COUNT(*) FROM invitations WHERE group_id=? AND inviter_id=?",
                        (row["group_id"], person["member_id"]),
                    ).fetchone()[0]
                    >= params["min_invitations"]
                ]
            winners = secrets.SystemRandom().sample(
                people, min(params["winners"], len(people))
            )
            # Persist the fixed draw before publishing. Never redraw on send failure.
            db.execute(
                "UPDATE activities SET state='publishing',winners=? WHERE id=?",
                (json.dumps(winners), identifier),
            )
            self.freeze_notifications(db, identifier, people, winners, params)
            self.log(
                db,
                row["group_id"],
                "lottery",
                identifier,
                "publishing",
                "eligible=" + str(len(people)),
            )
        return winners, len(people), params

    def cancel_activity(self, group, identifier):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM activities WHERE id=? AND group_id=?",
                (identifier, group),
            ).fetchone()
            if not row or row["state"] != "open":
                raise SessionError("activity_result_requires_review")
            db.execute(
                "UPDATE activities SET state='cancelled' WHERE id=?", (identifier,)
            )
            self.log(db, group, "lottery", identifier, "cancelled")

    def preview_settings(self, group, owner, umo, value, version):
        value = validate_settings(value)
        identifier = secrets.token_hex(8)
        with self.connect() as db:
            db.execute(
                "DELETE FROM drafts WHERE expires<? OR (owner=? AND umo=?)",
                (time.time(), owner, umo),
            )
            db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    group,
                    owner,
                    umo,
                    time.time() + 600,
                    version,
                    json.dumps({"kind": "settings", "proposal": value}),
                ),
            )
        return identifier

    def confirm_settings(self, group, owner, umo, identifier):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            draft = db.execute(
                "SELECT * FROM drafts WHERE id=?", (identifier,)
            ).fetchone()
            current = db.execute(
                "SELECT version,value FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            version = current["version"] if current else 0
            if (
                not draft
                or draft["group_id"] != group
                or draft["owner"] != owner
                or draft["umo"] != umo
                or draft["expires"] < time.time()
                or draft["version"] != version
            ):
                raise SessionError("business_preview_invalid")
            value = json.loads(draft["value"])
            if value.get("kind") != "settings":
                raise SessionError("business_preview_invalid")
            proposal = validate_settings(value["proposal"])
            previous = (
                validate_settings(json.loads(current["value"])) if current else DEFAULTS
            )
            if proposal["invitations_enabled"] and not previous["invitations_enabled"]:
                raise SessionError("invitation_baseline_required")
            if proposal["native_actions"] != previous["native_actions"]:
                raise SessionError("dashboard_action_grants_required")
            if proposal["manager_user_ids"] != previous["manager_user_ids"]:
                raise SessionError("dashboard_manager_grants_required")
            if (
                proposal["automation"]["enabled"] != previous["automation"]["enabled"]
                or proposal["ai_management_enabled"]
                != previous["ai_management_enabled"]
                or proposal["maintenance"] != previous["maintenance"]
                or proposal["lottery_plan"] != previous["lottery_plan"]
                or proposal["reminder_plan"] != previous["reminder_plan"]
                or proposal["automation"]["punishment_mode"]
                != previous["automation"]["punishment_mode"]
                or proposal["automation"]["flood_recall"]
                != previous["automation"]["flood_recall"]
                or proposal["invitation_rewards_enabled"]
                != previous["invitation_rewards_enabled"]
                or proposal["lottery"]["notify_results"]
                != previous["lottery"]["notify_results"]
            ):
                raise SessionError("dashboard_automation_grants_required")
            active = db.execute(
                "SELECT 1 FROM activities WHERE group_id=? AND state IN ('opening','open','publishing','unknown')",
                (group,),
            ).fetchone()
            if active and proposal["lottery"] != previous["lottery"]:
                raise SessionError("lottery_settings_frozen")
            db.execute(
                "INSERT INTO settings VALUES(?,?,?) ON CONFLICT(group_id) DO UPDATE SET version=excluded.version,value=excluded.value",
                (group, version + 1, json.dumps(proposal)),
            )
            if not proposal["lottery_enabled"]:
                db.execute(
                    "UPDATE activities SET state='cancelled' WHERE group_id=? AND state='open'",
                    (group,),
                )
            else:
                db.execute(
                    "UPDATE activities SET version=? WHERE group_id=? AND state='open'",
                    (version + 1, group),
                )
            if not proposal["reminders_enabled"]:
                db.execute(
                    "UPDATE reminders SET state='paused' WHERE group_id=? AND state='active'",
                    (group,),
                )
            else:
                db.execute(
                    "UPDATE reminders SET revision=? WHERE group_id=? AND state='active'",
                    (version + 1, group),
                )
            db.execute("DELETE FROM drafts WHERE id=?", (identifier,))
            self.log(
                db, group, "settings", owner, "confirmed", "version=" + str(version + 1)
            )
        return version + 1

    def preview_reminder(self, group, owner, umo, clock, text, version):
        next_daily(clock, time.time())
        if (
            not isinstance(text, str)
            or not 1 <= len(text) <= 1000
            or any(unicodedata.category(c).startswith("C") for c in text)
        ):
            raise ValueError("invalid_reminder_text")
        identifier = secrets.token_hex(8)
        with self.connect() as db:
            db.execute(
                "DELETE FROM drafts WHERE expires<? OR (owner=? AND umo=?)",
                (time.time(), owner, umo),
            )
            db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    group,
                    owner,
                    umo,
                    time.time() + 600,
                    version,
                    json.dumps({"kind": "reminder", "clock": clock, "text": text}),
                ),
            )
        return identifier

    def confirm_reminder(self, group, owner, umo, identifier, version):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM drafts WHERE id=?", (identifier,)
            ).fetchone()
            current = db.execute(
                "SELECT version,value FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            if (
                not row
                or not current
                or current["version"] != version
                or not json.loads(current["value"])["reminders_enabled"]
                or row["group_id"] != group
                or row["owner"] != owner
                or row["umo"] != umo
                or row["expires"] < time.time()
                or row["version"] != version
            ):
                raise SessionError("schedule_preview_invalid")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM reminders WHERE state!='deleted'"
                ).fetchone()[0]
                >= 32
            ):
                raise SessionError("schedule_limit_reached")
            value = json.loads(row["value"])
            if value.get("kind") != "reminder":
                raise SessionError("schedule_preview_invalid")
            db.execute(
                "INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    group,
                    owner,
                    umo,
                    value["clock"],
                    value["text"],
                    "active",
                    next_daily(value["clock"], time.time()),
                    version,
                    "",
                ),
            )
            db.execute("DELETE FROM drafts WHERE id=?", (identifier,))
            self.log(db, group, "reminder", identifier, "active")
        return identifier

    def reminders(self, group):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM reminders WHERE group_id=? AND state!='deleted' ORDER BY next_run",
                    (group,),
                )
            ]

    def pause_reminder(self, group, identifier, state="paused"):
        if state not in {"paused", "deleted"}:
            raise ValueError("invalid_schedule_action")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM reminders WHERE id=? AND group_id=?",
                (identifier, group),
            ).fetchone()
            if not row or row["state"] in {"sending", "unknown"}:
                raise SessionError("schedule_result_requires_review")
            db.execute(
                "UPDATE reminders SET state=? WHERE id=? AND group_id=?",
                (state, identifier, group),
            )
            self.log(db, group, "reminder", identifier, state)

    def resume_reminder(self, group, identifier, owner, umo, version):
        """Resume a known paused task at its next future boundary, never catch up."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM reminders WHERE id=? AND group_id=?",
                (identifier, group),
            ).fetchone()
            config = db.execute(
                "SELECT * FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            if (
                not row
                or row["state"] != "paused"
                or row["owner"] != owner
                or row["umo"] != umo
                or not config
                or config["version"] != version
                or not json.loads(config["value"])["reminders_enabled"]
            ):
                raise SessionError("schedule_result_requires_review")
            db.execute(
                "UPDATE reminders SET state='active',next_run=?,revision=?,receipt='' WHERE id=?",
                (next_daily(row["clock"], time.time()), version, identifier),
            )
            self.log(db, group, "reminder", identifier, "resumed")

    def suspend_subscription(self):
        """Pause known pending work; preserve all unknown side-effect receipts."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE reminders SET state='paused' WHERE state='active'")
            db.execute("UPDATE activities SET state='cancelled' WHERE state='open'")
            db.execute("UPDATE scheduled_jobs SET state='paused' WHERE state='pending'")
            self.log(db, "", "subscription", "", "paused")

    def due(self, now):
        with self.connect() as db:
            reminders = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM reminders WHERE state='active' AND next_run<=? LIMIT 32",
                    (now,),
                )
            ]
            activities = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM activities WHERE state='open' AND ends<=? LIMIT 100",
                    (now,),
                )
            ]
        return reminders, activities

    def reserve_reminder(self, identifier, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM reminders WHERE id=? AND state='active'", (identifier,)
            ).fetchone()
            if not row or row["next_run"] > now:
                return False
            if now - row["next_run"] > 60:
                db.execute(
                    "UPDATE reminders SET state='paused' WHERE id=?", (identifier,)
                )
                self.log(db, row["group_id"], "reminder", identifier, "missed_boundary")
                return False
            db.execute("UPDATE reminders SET state='sending' WHERE id=?", (identifier,))
            self.log(db, row["group_id"], "reminder", identifier, "sending")
        return True

    def reminder_receipt(self, identifier, receipt):
        status = receipt.get("status")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM reminders WHERE id=? AND state='sending'", (identifier,)
            ).fetchone()
            if not row:
                raise SessionError("schedule_state_changed")
            state = (
                "active"
                if status == "sent"
                else "paused"
                if status in {"failed", "rejected", "unsupported"}
                else "unknown"
            )
            db.execute(
                "UPDATE reminders SET state=?,next_run=?,receipt=? WHERE id=?",
                (
                    state,
                    next_daily(row["clock"], time.time()),
                    json.dumps(receipt),
                    identifier,
                ),
            )
            self.log(db, row["group_id"], "reminder", identifier, state)


class Businesses:
    """Bounded account locks, database workers and one persistent task poller."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.stores = {}
        self.locks = {}
        self.store_locks = {}
        self.database_slots = asyncio.Semaphore(4)
        self.database_admission = asyncio.Semaphore(64)
        self.database_tasks = set()
        self.database_peak = 0
        self.review_slots = asyncio.Semaphore(2)
        self.windows = OrderedDict()
        self.contexts = OrderedDict()
        self.notification_heads = OrderedDict()
        self.settings_cache = OrderedDict()
        self.settings_locks = WeakValueDictionary()
        self.settings_generation = 0
        self.retry_after = {}
        self.stopping = False
        self.poller = None
        self.account_pollers = {}
        self.poll_cursor = 0
        self.maintenance_after = {}
        self.maintenance_group_cursors = {}

    async def db(self, ref, method, *args, **kwargs):
        """Run account-scoped database work outside the AstrBot event loop.

        Args:
            ref: Authorized account reference.
            method: Internal fixed store method, not a public API parameter.
            args: Store method arguments.

        Returns:
            The store operation result.
        """
        if self.stopping:
            raise SessionError("plugin_stopping")
        # Admission happens in the caller, before creating a shielded worker.
        await self.database_admission.acquire()
        if self.stopping:
            self.database_admission.release()
            raise SessionError("plugin_stopping")
        started = False

        async def operate():
            nonlocal started
            async with self.database_slots:
                if self.stopping:
                    raise SessionError("plugin_stopping")
                started = True
                async with self.store_locks.setdefault(ref, asyncio.Lock()):
                    if self.stopping:
                        raise SessionError("plugin_stopping")
                    if ref not in self.stores:
                        from .commerce import NAMESPACE, CommerceStore

                        self.stores[ref] = await asyncio.to_thread(
                            CommerceStore if ref == NAMESPACE else BusinessStore,
                            self.plugin.store.root
                            if ref == NAMESPACE
                            else self.plugin.store.directory(ref),
                        )
                if self.stopping:
                    raise SessionError("plugin_stopping")
                result = await asyncio.to_thread(
                    getattr(self.stores[ref], method), *args, **kwargs
                )
                if method in {"save_settings", "confirm_settings"}:
                    self.settings_cache.pop((ref, args[0]), None)
                    self.settings_generation += 1
                if (
                    method in {"save_ai_config", "save_ai_pair"}
                    and getattr(self.plugin, "ai", None) is not None
                ):
                    self.plugin.ai.cache.clear()
                    self.plugin.ai.cache_generation += 1
                return result

        task = asyncio.create_task(operate())
        self.database_tasks.add(task)
        self.database_peak = max(self.database_peak, len(self.database_tasks))

        def finished(task):
            self.database_tasks.discard(task)
            self.database_admission.release()
            if not task.cancelled():
                task.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancel queued work; a running thread retains its slot until completion.
            if not started:
                task.cancel()
            raise

    async def settings(self, ref, group):
        """Cache read-only observation switches; local saves invalidate immediately."""
        key = (ref, group)
        row = self.settings_cache.get(key)
        if row is None or row[0] <= time.monotonic():
            async with self.settings_locks.setdefault(key, asyncio.Lock()):
                row = self.settings_cache.get(key)
                if row is None or row[0] <= time.monotonic():
                    while True:
                        generation = self.settings_generation
                        value = await self.db(ref, "settings", group)
                        if generation == self.settings_generation:
                            break
                    row = (time.monotonic() + 5, value)
                    self.settings_cache[key] = row
        self.settings_cache.move_to_end(key)
        if len(self.settings_cache) > 512:
            self.settings_cache.popitem(last=False)
        return row[1]

    def instance(self, ref, group):
        row = self.plugin.store._read(ref)
        config = next(
            (
                c
                for c in self.plugin.context.get_config().get("platform", [])
                if c.get("session_ref") == ref
                and c.get("id") == row["instance_id"]
                and c.get("type") == "lajiaoliao"
            ),
            None,
        )
        if not config or not config.get("enable") or self.stopping:
            raise SessionError("instance_offline")
        commerce = getattr(self.plugin, "commerce", None)
        if commerce is not None:
            commerce.require(config["id"])
        require_message(config, "group", group)
        instance = self.plugin.context.get_platform_inst(config["id"])
        if instance is None or instance.connection_state != "online":
            raise SessionError("instance_offline")
        return instance

    def administrator(self, owner, umo):
        config = self.plugin.context.get_config(umo=umo)
        plugins = config.get("plugin_set", ["*"])
        return ("*" in plugins or "astrbot_plugin_lajiaoliao" in plugins) and str(
            owner
        ) in {str(uid) for uid in config.get("admins_id", [])}

    def plugin_enabled(self, umo):
        plugins = self.plugin.context.get_config(umo=umo).get("plugin_set", ["*"])
        return "*" in plugins or "astrbot_plugin_lajiaoliao" in plugins

    def group_origin(self, ref, group):
        row = self.plugin.store._read(ref)
        return str(
            MessageSession(
                row["instance_id"], MessageType.GROUP_MESSAGE, "group:" + group
            )
        )

    async def accessible_groups(self, ref, uid, umo):
        """Return only this account's allowed groups explicitly delegated to uid."""
        if not self.plugin_enabled(umo):
            return []
        row = self.plugin.store._read(ref)
        config = next(
            (
                cfg
                for cfg in self.plugin.context.get_config().get("platform", [])
                if cfg.get("id") == row["instance_id"]
                and cfg.get("session_ref") == ref
                and cfg.get("type") == "lajiaoliao"
            ),
            {},
        )
        allowed = config.get("message_policy", {}).get("group_ids", [])
        is_bot = str(uid) in self.plugin.store.bound_account_ids()
        delegated = (
            None
            if self.administrator(uid, umo)
            else set(await self.db(ref, "managed_groups", str(uid)))
        )
        return [
            group
            for group in allowed
            if (delegated is None or group in delegated)
            and self.plugin_enabled(self.group_origin(ref, group))
            and (
                delegated is None
                or not is_bot
                or GroupManagerTestPermit(self.plugin.store).allows(ref, group, uid)
            )
        ]

    async def group_administrator(self, ref, group, uid, umo):
        if not self.plugin_enabled(umo) or not self.plugin_enabled(
            self.group_origin(ref, group)
        ):
            return False
        if self.administrator(uid, umo):
            return True
        if str(uid) in self.plugin.store.bound_account_ids():
            if not GroupManagerTestPermit(self.plugin.store).allows(ref, group, uid):
                return False
        settings = await self.settings(ref, group)
        return str(uid) in settings["settings"]["manager_user_ids"]

    async def identity(self, ref, group, uid, umo):
        if self.administrator(uid, umo):
            return "astrbot_admin"
        if group and await self.group_administrator(ref, group, uid, umo):
            return "group_manager"
        return "member"

    async def reconcile(self, ref, group, instance):
        notes = [
            note
            for note in getattr(instance, "notifications", ())
            if note.get("group_id") == group
        ]
        if not notes:
            return
        key = (ref, group)
        head = notes[-1]["message_id"]
        if self.notification_heads.get(key) == head:
            return
        messages = [
            note["revoked_message_id"]
            for note in notes
            if note.get("action") == "message_revoked"
        ]
        if messages:
            await self.db(ref, "revoked", group, messages)
        self.notification_heads[key] = head
        self.notification_heads.move_to_end(key)
        if len(self.notification_heads) > 512:
            self.notification_heads.popitem(last=False)

    async def roster(self, ref, group):
        """Read a complete bounded directory, preserving upstream member IDs.

        Args:
            ref: Account reference.
            group: Exact OpenIM group ID.

        Returns:
            A user-ID mapping from the verified current group directory.

        Raises:
            SessionError: If membership cannot be completely verified.
        """
        from urllib.parse import quote

        self.instance(ref, group)
        people = {}
        for page in range(1, 21):
            data = await self.plugin.manager.request(
                ref, "GET", f"/v1/groups/{quote(group, safe='')}/members?page={page}"
            )
            rows = data.get("members")
            if not isinstance(rows, list):
                raise SessionError("group_directory_unverified")
            for row in rows:
                uid = str(row.get("userID", ""))
                if not uid or str(row.get("groupID", "")) != group or uid in people:
                    raise SessionError("group_directory_unverified")
                people[uid] = row
            if len(rows) < 100:
                self.instance(ref, group)
                return people
        raise SessionError("group_directory_incomplete")

    async def notification(self, ref, note):
        """Handoff fresh invitation evidence before transport ACK."""
        if note.get("action") not in {"members_invited", "member_entered"}:
            return
        group = note.get("group_id", "")
        try:
            self.instance(ref, group)
        except (SessionError, PermissionError):
            return
        settings = await self.settings(ref, group)
        if not settings["settings"]["invitations_enabled"]:
            return
        stamp = note.get("timestamp", 0)
        if not isinstance(stamp, (int, float)) or not 0 <= time.time() - stamp <= 300:
            return
        await self.db(ref, "pending_invitation", group, note)
        async with self.locks.setdefault(ref, asyncio.Lock()):
            roster = await self.roster(ref, group)
            excluded = {
                row["account_id"]
                for row in self.plugin.store._all()
                if row["state"] == "bound"
            }
            await self.db(ref, "invitation_credit", group, note, roster, excluded)

    async def validate_capabilities(self, ref, settings):
        """Reject unverified executable toggles using the packaged evidence matrix.

        Args:
            ref: Bound account.
            settings: Validated proposed settings.

        Raises:
            SessionError: When required live evidence is absent.
        """
        from .capabilities import execution_verified, load_capabilities

        evidence = load_capabilities()
        if settings["lottery"]["notify_results"]:
            features = {item["name"]: item for item in evidence["features"]}
            image, quote = features.get("image", {}), features.get("quote", {})
            if (
                not quote.get("live_group_send")
                or not image.get("live_group_send")
                or "GIF" not in image.get("formats_send", [])
            ):
                raise SessionError("lottery_notification_unverified")
            result_gif()
        verified = {
            item["name"] for item in evidence["features"] if execution_verified(item)
        }
        rules, maintenance = settings["automation"], settings["maintenance"]
        requested = []
        if rules["enabled"] and (
            rules["punishment_mode"] == "recall_progressive" or rules["flood_recall"]
        ):
            requested.append("revoke_others")
        if maintenance["cards_enabled"]:
            requested.append("rename_others")
        if maintenance["cleanup_enabled"]:
            requested.append("cleanup")
        if any(action not in verified for action in requested):
            raise SessionError("native_capability_unverified")

    async def task_authorized(self, ref, group, owner, umo=""):
        """Check the original principal without translating web users to chat UIDs.

        Args:
            ref: Account reference.
            group: Group scope.
            owner: Explicit Dashboard principal or legacy chat UID.
            umo: Legacy chat authorization origin.

        Returns:
            Whether the current principal still owns the allowed operation.
        """
        if owner.startswith("dashboard:"):
            self.plugin.store.get(ref, owner.removeprefix("dashboard:"))
            return self.plugin_enabled(self.group_origin(ref, group))
        return await self.group_administrator(ref, group, owner, umo)

    async def native_payload(self, ref, group, body):
        """Recheck explicit group grants and protect administrators and robots."""
        action = body["action"]
        grants = {
            "revoke_member_message": "revoke_others",
            "set_member_group_nickname": "rename_others",
            "cleanup_unavailable_member": "cleanup",
        }
        grant = grants.get(action, action)
        settings = await self.db(ref, "settings", group)
        local_test = NativeTestPermit(self.plugin.store).allows(ref, group, body)
        if body.get("business_version") != settings["version"]:
            raise SessionError("business_changed_refresh_required")
        if grant not in settings["settings"]["native_actions"]:
            raise SessionError("native_action_not_authorized")
        if "role_level" in body or (
            action not in grants
            and any(
                key in body for key in ("message_id", "nickname", "expected_nickname")
            )
        ):
            raise ValueError("unexpected_action_fields")
        roster = await self.roster(ref, group)
        account = self.plugin.store._read(ref)["account_id"]
        if roster.get(account, {}).get("roleLevel") not in {60, 100}:
            raise SessionError("native_admin_required")
        payload = {
            key: body[key] for key in ("operation_id", "action", "kind", "target_id")
        }
        member_action = action in {"mute", "unmute", "kick", *grants}
        uid = body.get("user_id")
        instance = self.instance(ref, group)
        group_origin = str(
            MessageSession(
                instance.config["id"], MessageType.GROUP_MESSAGE, "group:" + group
            )
        )

        def protected_admins():
            return {
                str(member)
                for cfg in (
                    self.plugin.context.get_config(),
                    self.plugin.context.get_config(umo=group_origin),
                )
                for member in cfg.get("admins_id", [])
            } | set(settings["settings"]["manager_user_ids"])

        if member_action:
            if (
                not isinstance(uid, str)
                or not 1 <= len(uid) <= 128
                or uid != uid.strip()
            ):
                raise ValueError("invalid_target_member")
            protected_bots = {
                row["account_id"]
                for row in self.plugin.store._all()
                if row["state"] == "bound"
            }
            test_permit = NativeTestPermit(self.plugin.store)
            if uid in protected_admins() or (
                uid in protected_bots and not test_permit.allows(ref, group, body)
            ):
                raise SessionError("protected_member")
            if uid not in roster or roster[uid].get("roleLevel") != 20:
                raise SessionError("protected_or_unverified_member")
            payload["user_id"] = uid
        elif uid is not None:
            raise ValueError("unexpected_action_fields")
        if action == "mute":
            seconds = body.get("muted_seconds")
            if type(seconds) is not int or not 60 <= seconds <= 86400 or seconds % 60:
                raise ValueError("invalid_mute_duration")
            payload["muted_seconds"] = seconds
        elif "muted_seconds" in body:
            raise ValueError("unexpected_action_fields")
        if action == "kick":
            if body.get("confirm_kick") is not True:
                raise SessionError("explicit_kick_confirmation_required")
            payload["confirm_kick"] = True
        elif "confirm_kick" in body:
            raise ValueError("unexpected_action_fields")
        if action in grants:
            await self.validate_capabilities(ref, settings["settings"])
            # A capability remains gated even if a caller bypasses its feature switch.
            from .capabilities import execution_verified, load_capabilities

            matrix = load_capabilities()
            if not local_test and not any(
                item["name"] == grant and execution_verified(item)
                for item in matrix["features"]
            ):
                raise SessionError("native_capability_unverified")
            if uid in protected_bots and not local_test:
                raise SessionError("protected_member")
            if action == "revoke_member_message":
                message = body.get("message_id", "")
                if not isinstance(message, str) or not 1 <= len(message) <= 128:
                    raise ValueError("invalid_message_id")
                payload["message_id"] = message
            elif action == "set_member_group_nickname":
                previous, nickname = body.get("expected_nickname"), body.get("nickname")
                if (
                    not isinstance(previous, str)
                    or not isinstance(nickname, str)
                    or not 1 <= len(nickname) <= 15
                ):
                    raise ValueError("invalid_nickname")
                if roster[uid].get("nickname") != previous:
                    raise SessionError("nickname_changed_refresh_required")
                payload.update(nickname=nickname, expected_nickname=previous)
            else:
                from urllib.parse import urlencode

                availability = await self.plugin.manager.request(
                    ref,
                    "GET",
                    "/v1/groups/member-availability?"
                    + urlencode({"group_id": group, "user_id": uid}),
                )
                if availability.get("verified") is not True or availability.get(
                    "availability"
                ) not in {"banned", "deactivated"}:
                    raise SessionError("account_availability_unverified")
                payload.update(action="kick", confirm_kick=True)
        if action == "announce":
            text, previous = body.get("announcement"), body.get("expected_announcement")
            if (
                not isinstance(text, str)
                or len(text) > 2000
                or any(ord(c) < 32 and c not in "\n\t" for c in text)
                or not isinstance(previous, str)
                or len(previous.encode()) > 16384
            ):
                raise ValueError("invalid_announcement")
            payload.update(announcement=text, expected_announcement=previous)
        elif "announcement" in body or "expected_announcement" in body:
            raise ValueError("unexpected_action_fields")
        latest = await self.db(ref, "settings", group)
        if latest["version"] != settings["version"]:
            raise SessionError("business_changed_refresh_required")
        if member_action and uid in (
            protected_admins() | set(latest["settings"]["manager_user_ids"])
        ):
            raise SessionError("protected_member")
        if (
            member_action
            and uid in protected_bots
            and not test_permit.allows(ref, group, body)
        ):
            raise SessionError("protected_member")
        self.instance(ref, group)
        return payload

    async def apply_automatic(
        self, ref, group, uid, message, stamp, reason, version, *, batch_digest=""
    ):
        """Execute fixed automatic actions without lifting bot protections."""
        row = self.plugin.store._read(ref)
        plan = None
        submitted = False
        try:
            async with instance_lock(row["instance_id"]):
                async with self.locks.setdefault(ref, asyncio.Lock()):
                    if not self.plugin_enabled(self.group_origin(ref, group)):
                        raise SessionError("plugin_disabled")
                    if uid in {
                        r["account_id"]
                        for r in self.plugin.store._all()
                        if r["state"] == "bound"
                    }:
                        raise SessionError("protected_member")
                    current = await self.db(ref, "settings", group)
                    rules = current["settings"]["automation"]
                    await self.validate_capabilities(ref, current["settings"])
                    # Verify roles/grants before reserving a level, and again before sending.
                    body = {
                        "operation_id": "automatic-preflight",
                        "action": "mute",
                        "kind": "group",
                        "target_id": group,
                        "user_id": uid,
                        "muted_seconds": 300,
                        "business_version": version,
                    }
                    await self.native_payload(ref, group, body)
                    plan = await self.db(
                        ref,
                        "reserve_automatic",
                        group,
                        message,
                        uid,
                        stamp,
                        reason,
                        version,
                    )
                    if not plan:
                        return True
                    body.update(plan)
                    if not body["muted_seconds"]:
                        body["muted_seconds"] = 60
                    payload = await self.native_payload(ref, group, body)
                    current = await self.db(ref, "settings", group)
                    if (
                        current["version"] != version
                        or not current["settings"]["automation"]["enabled"]
                    ):
                        raise SessionError("business_changed_refresh_required")
                    protected = (
                        {
                            str(member)
                            for cfg in (
                                self.plugin.context.get_config(),
                                self.plugin.context.get_config(
                                    umo=self.group_origin(ref, group)
                                ),
                            )
                            for member in cfg.get("admins_id", [])
                        }
                        | set(current["settings"]["manager_user_ids"])
                        | {
                            r["account_id"]
                            for r in self.plugin.store._all()
                            if r["state"] == "bound"
                        }
                    )
                    if uid in protected:
                        raise SessionError("protected_member")
                    instance = self.instance(ref, group)
                    if ("group", group, message) in getattr(
                        instance, "_revoked_messages", {}
                    ):
                        raise SessionError("message_already_revoked")
                    if not 0 <= time.time() - stamp <= 60:
                        raise SessionError("automatic_evidence_expired")
                    if rules["punishment_mode"] == "recall_progressive" or (
                        reason == "flood" and rules["flood_recall"]
                    ):
                        messages = [{"message_id": message, "stamp": stamp}]
                        if reason == "flood" and rules["flood_recall"]:
                            messages = await self.db(
                                ref,
                                "recent_member_messages",
                                group,
                                uid,
                                time.time()
                                - min(
                                    60,
                                    rules["duplicate_window_seconds"]
                                    if batch_digest
                                    else rules["flood_window_seconds"],
                                ),
                                batch_digest,
                            )
                        successful_recalls = 0
                        for original in messages:
                            if not 0 <= time.time() - original["stamp"] <= 60:
                                continue
                            if ("group", group, original["message_id"]) in getattr(
                                instance, "_revoked_messages", {}
                            ):
                                continue
                            recall = {
                                "operation_id": plan["operation_id"],
                                "action": "revoke_member_message",
                                "kind": "group",
                                "target_id": group,
                                "user_id": uid,
                                "message_id": original["message_id"],
                                "business_version": version,
                            }
                            validated = await self.native_payload(ref, group, recall)
                            if not 0 <= time.time() - original["stamp"] <= 60:
                                raise SessionError("automatic_evidence_expired")
                            effect = await self.db(
                                ref,
                                "reserve_effect",
                                group,
                                "recall",
                                original["message_id"],
                                uid,
                                validated,
                            )
                            if not effect:
                                raise SessionError("recall_requires_review")
                            validated["operation_id"] = effect
                            submitted = True
                            receipt = await instance.platform_action(validated)
                            await self.db(ref, "finish_effect", effect, receipt)
                            if receipt.get("status") != "succeeded":
                                await self.db(
                                    ref,
                                    "finish_automatic",
                                    group,
                                    plan["operation_id"],
                                    receipt.get("status", "unknown"),
                                )
                                return True
                            successful_recalls += 1
                        if not successful_recalls:
                            raise SessionError("recall_evidence_unavailable")
                        if plan["muted_seconds"] == 0:
                            await self.db(
                                ref,
                                "finish_automatic",
                                group,
                                plan["operation_id"],
                                "succeeded",
                            )
                            return True
                        body["muted_seconds"] = plan["muted_seconds"]
                        payload = await self.native_payload(ref, group, body)
                    if not 0 <= time.time() - stamp <= 60:
                        raise SessionError("automatic_evidence_expired")
                    submitted = True
                    receipt = await instance.platform_action(payload)
                    await self.db(
                        ref,
                        "finish_automatic",
                        group,
                        plan["operation_id"],
                        receipt.get("status", "unknown"),
                    )
                    return True
        except asyncio.CancelledError:
            # The durable unknown reservation prevents an automatic retry.
            raise
        except Exception as exc:
            if plan:
                status = (
                    getattr(exc, "delivery_status", "unknown")
                    if submitted
                    else "rejected"
                )
                await self.db(
                    ref, "finish_automatic", group, plan["operation_id"], status
                )
            else:
                await self.db(
                    ref, "automatic_rejection", group, message, type(exc).__name__
                )
            return submitted

    async def send(self, ref, group, text):
        """Send once using the current core policy and preserve uncertainty.

        Args:
            ref: Account reference.
            group: Exact group target.
            text: Textual notification.

        Returns:
            Explicit transport outcome, never a fabricated success.
        """
        try:
            instance = self.instance(ref, group)
            return await instance.send_parts(
                group, "", [{"type": "text", "text": text}], "group"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = getattr(exc, "delivery_status", "unknown")
            if isinstance(exc, (PermissionError, SessionError)):
                status = "rejected"
            return {
                "status": status
                if status in {"rejected", "unsupported", "failed"}
                else "unknown",
                "error": type(exc).__name__,
            }

    async def start_lottery(self, ref, group, owner, umo, *, job_id="", job_due=None):
        async with self.locks.setdefault(ref, asyncio.Lock()):
            self.instance(ref, group)
            if job_id and not await self.db(ref, "job_current", job_id):
                raise SessionError("schedule_state_changed")
            if not await self.task_authorized(ref, group, owner, umo):
                raise SessionError("chat_admin_required")
            await self.roster(ref, group)
            settings = await self.db(ref, "settings", group)
            if job_id and not await self.db(ref, "job_current", job_id):
                raise SessionError("schedule_state_changed")
            require_current_boundary(job_due)
            identifier, params = await self.db(
                ref, "prepare_activity", group, owner, umo, settings["version"]
            )
            if not await self.task_authorized(ref, group, owner, umo):
                receipt = {"status": "rejected"}
            else:
                try:
                    require_current_boundary(job_due)
                except ScheduleBoundaryExpired:
                    await self.db(
                        ref,
                        "activity_receipt",
                        identifier,
                        "opening",
                        {"status": "rejected", "error": "schedule_boundary_expired"},
                    )
                    raise
                receipt = await self.send(
                    ref,
                    group,
                    f"【抽奖开始】\n奖品：{params['prize']}\n中奖人数：{params['winners']}\n倒计时：{params['minutes']}分钟\n领奖联系人：{params['contact']}\n参加抽奖",
                )
            state = await self.db(
                ref, "activity_receipt", identifier, "opening", receipt
            )
            return identifier, state

    async def draw_lottery(self, ref, group, identifier):
        async with self.locks.setdefault(ref, asyncio.Lock()):
            self.instance(ref, group)
            settings = await self.db(ref, "settings", group)
            rows = await self.db(ref, "activities", group)
            row = next((row for row in rows if row["id"] == identifier), None)
            if not row or row["state"] != "open":
                raise SessionError("lottery_not_open")
            if (
                not settings["settings"]["lottery_enabled"]
                or row["version"] != settings["version"]
                or not await self.task_authorized(ref, group, row["owner"], row["umo"])
            ):
                await self.db(ref, "cancel_activity", group, identifier)
                return "cancelled"
            roster = await self.roster(ref, group)
            bots = {
                r["account_id"]
                for r in self.plugin.store._all()
                if r["state"] == "bound"
            }
            eligible = {
                uid
                for uid, member in roster.items()
                if uid not in bots and member.get("roleLevel") in (20, 60, 100)
            }
            # Recheck after directory IO; a saved policy change cancels the draw.
            latest = await self.db(ref, "settings", group)
            if latest["version"] != row["version"] or not await self.task_authorized(
                ref, group, row["owner"], row["umo"]
            ):
                await self.db(ref, "cancel_activity", group, identifier)
                return "cancelled"
            winners, count, params = await self.db(ref, "draw", identifier, eligible)
            if params.get("notify_results", False):
                await self.notify_lottery(ref, group, row)
            listing = (
                "\n".join(
                    f"{index}. {winner['name']}（{winner['member_id']}）"
                    for index, winner in enumerate(winners, 1)
                )
                or "暂无符合条件的报名成员"
            )
            text = f"【抽奖结果】\n{listing}\n奖品：{params['prize']}\n领奖联系人：{params['contact']}\n有效参与：{count}人\n中奖率：{len(winners) / count * 100 if count else 0:.2f}%"
            latest = await self.db(ref, "settings", group)
            receipt = (
                {"status": "rejected"}
                if latest["version"] != row["version"]
                or not await self.task_authorized(ref, group, row["owner"], row["umo"])
                else await self.send(ref, group, text)
            )
            notifications = await self.db(ref, "activities", group)
            delivery = next(
                item["notifications"]
                for item in notifications
                if item["id"] == identifier
            )
            if delivery:
                receipt = {
                    **receipt,
                    "summary_status": receipt.get("status", "unknown"),
                    "notifications": delivery,
                }
                if any(delivery.get(s) for s in ("unknown", "sending", "pending")):
                    receipt["status"] = "unknown"
                elif receipt.get("status") == "sent" and any(
                    delivery.get(s)
                    for s in ("failed", "rejected", "unsupported", "skipped")
                ):
                    receipt["status"] = "failed"
            return await self.db(
                ref, "activity_receipt", identifier, "publishing", receipt
            )

    async def notify_lottery(self, ref, group, activity):
        """Quote a frozen result, then send the GIF only after a confirmed quote."""
        identifier = activity["id"]
        rows = await self.db(ref, "notification_rows", group, identifier)
        deadline = time.monotonic() + 300
        for row in rows:
            if row["state"] != "pending":
                continue
            if not await self.db(
                ref,
                "reserve_notification",
                group,
                identifier,
                row["member_id"],
                row["stage"],
            ):
                continue
            submitted = False
            try:
                if time.monotonic() >= deadline:
                    raise SessionError("lottery_notification_window_expired")
                current = await self.db(ref, "settings", group)
                if (
                    current["version"] != activity["version"]
                    or not current["settings"]["lottery_enabled"]
                    or not await self.task_authorized(
                        ref, group, activity["owner"], activity["umo"]
                    )
                ):
                    raise SessionError("lottery_authorization_changed")
                instance = self.instance(ref, group)
                if row["stage"] == "quote":
                    parts = [{"type": "text", "text": row["text"]}]
                    reply_to = row["message_id"]
                else:
                    parts, reply_to = [result_gif()], ""
                submitted = True
                async with asyncio.timeout(20):
                    receipt = await instance.send_parts(group, reply_to, parts, "group")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = getattr(exc, "delivery_status", "unknown")
                if not submitted or isinstance(exc, (PermissionError, SessionError)):
                    status = "rejected"
                receipt = {"status": status, "error": type(exc).__name__}
            await self.db(
                ref,
                "finish_notification",
                group,
                identifier,
                row["member_id"],
                row["stage"],
                receipt,
            )

    async def poll(self):
        while not self.stopping:
            await asyncio.sleep(2)
            from .commerce import NAMESPACE

            refs = tuple(ref for ref in self.stores if ref != NAMESPACE)
            if not refs:
                continue
            offset = self.poll_cursor % len(refs)
            self.poll_cursor += 1
            for ref in refs[offset:] + refs[:offset]:
                if ref in self.account_pollers or len(self.account_pollers) >= 4:
                    continue
                if time.monotonic() < self.retry_after.get(ref, 0):
                    continue
                task = asyncio.create_task(self.poll_account(ref))
                self.account_pollers[ref] = task
                task.add_done_callback(
                    lambda done, ref=ref: self.account_pollers.pop(ref, None)
                )

    async def poll_account(self, ref):
        """Poll one account without blocking scheduled work on other accounts.

        Args:
            ref: Account with an initialized private business ledger.
        """
        try:
            commerce = getattr(self.plugin, "commerce", None)
            if commerce is not None:
                commerce.require(self.plugin.store._read(ref)["instance_id"])
            if getattr(self.plugin, "ai", None) is not None:
                await self.plugin.ai.notify(ref)
            await self.poll_scheduled(ref)
            if time.monotonic() >= self.maintenance_after.get(ref, 0):
                await self.poll_maintenance(ref)
                self.maintenance_after[ref] = time.monotonic() + 300
            reminders, activities = await self.db(ref, "due", time.time())
            for job in reminders:
                async with self.locks.setdefault(ref, asyncio.Lock()):
                    settings = await self.db(ref, "settings", job["group_id"])
                    if (
                        not settings["settings"]["reminders_enabled"]
                        or settings["version"] != job["revision"]
                        or not await self.group_administrator(
                            ref, job["group_id"], job["owner"], job["umo"]
                        )
                    ):
                        await self.db(ref, "pause_reminder", job["group_id"], job["id"])
                        continue
                    try:
                        self.instance(ref, job["group_id"])
                    except (SessionError, PermissionError):
                        await self.db(ref, "pause_reminder", job["group_id"], job["id"])
                        continue
                    if await self.db(ref, "reserve_reminder", job["id"], time.time()):
                        latest = await self.db(ref, "settings", job["group_id"])
                        receipt = (
                            {"status": "rejected"}
                            if latest["version"] != job["revision"]
                            or not await self.group_administrator(
                                ref, job["group_id"], job["owner"], job["umo"]
                            )
                            else await self.send(ref, job["group_id"], job["text"])
                        )
                        await self.db(ref, "reminder_receipt", job["id"], receipt)
            for activity in activities:
                await self.draw_lottery(ref, activity["group_id"], activity["id"])
            self.retry_after.pop(ref, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.retry_after[ref] = time.monotonic() + 30
            # Isolate account failures; no retry of reserved unknown effects.
            self.plugin.logger.warning(
                "LajiaoLiao business poll deferred for an unavailable account"
            )

    async def poll_scheduled(self, ref):
        """Execute due saved plans once; a crash leaves a visible unknown receipt.

        Args:
            ref: Bound account reference.
        """
        for pending in await self.db(ref, "scheduled", "", time.time()):
            group = pending["group_id"]
            job = None
            submitted = False
            receipt = {"status": "rejected"}
            try:
                self.instance(ref, group)
                if not await self.task_authorized(ref, group, pending["owner"]):
                    raise SessionError("task_owner_not_authorized")
                job = await self.db(ref, "reserve_job", pending["id"], time.time())
                if not job:
                    continue
                if not await self.db(ref, "job_current", job["id"]):
                    raise SessionError("schedule_state_changed")
                require_current_boundary(job["due"])
                if job["payload"]["action"] == "lottery":
                    submitted = True
                    _, state = await self.start_lottery(
                        ref,
                        group,
                        job["owner"],
                        "",
                        job_id=job["id"],
                        job_due=job["due"],
                    )
                    receipt = {
                        "status": "succeeded"
                        if state == "open"
                        else "unknown"
                        if state == "unknown"
                        else "failed"
                    }
                else:
                    async with instance_lock(
                        self.plugin.store._read(ref)["instance_id"]
                    ):
                        async with self.locks.setdefault(ref, asyncio.Lock()):
                            current = await self.db(ref, "settings", group)
                            if not await self.db(ref, "job_current", job["id"]):
                                raise SessionError("schedule_state_changed")
                            if not await self.task_authorized(ref, group, job["owner"]):
                                raise SessionError("task_owner_not_authorized")
                            if job["feature"] == "reply_recall":
                                if (
                                    not current["settings"]["maintenance"][
                                        "reply_recall_enabled"
                                    ]
                                    or "reply_recall"
                                    not in current["settings"]["native_actions"]
                                ):
                                    raise SessionError("feature_disabled")
                                payload = {
                                    "operation_id": "task-" + job["id"],
                                    "action": "revoke_self_message",
                                    "kind": job["payload"]["kind"],
                                    "target_id": job["payload"]["target_id"],
                                    "message_id": job["payload"]["message_id"],
                                }
                                instance = self.instance(ref, group)
                                require_current_boundary(job["due"])
                                submitted = True
                                receipt = await instance.platform_action(payload)
                            elif job["feature"] == "quiet":
                                if not current["settings"]["maintenance"][
                                    "quiet_enabled"
                                ]:
                                    raise SessionError("feature_disabled")
                                payload = await self.native_payload(
                                    ref,
                                    group,
                                    {
                                        "operation_id": "task-" + job["id"],
                                        "action": job["payload"]["action"],
                                        "kind": "group",
                                        "target_id": group,
                                        "business_version": current["version"],
                                    },
                                )
                                require_current_boundary(job["due"])
                                submitted = True
                                receipt = await self.instance(
                                    ref, group
                                ).platform_action(payload)
                            else:
                                if (
                                    not current["settings"]["reminders_enabled"]
                                    or not current["settings"]["reminder_plan"][
                                        "enabled"
                                    ]
                                ):
                                    raise SessionError("feature_disabled")
                                require_current_boundary(job["due"])
                                submitted = True
                                receipt = await self.send(
                                    ref, group, job["payload"]["text"]
                                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                receipt = {
                    "status": getattr(exc, "delivery_status", "unknown")
                    if submitted
                    else "paused"
                    if isinstance(exc, ScheduleBoundaryExpired)
                    else "rejected",
                    "error": type(exc).__name__,
                }
            if job:
                await self.db(ref, "finish_job", job["id"], receipt)

    async def poll_maintenance(self, ref):
        """Reconcile bounded complete rosters; never interpret names as status.

        Args:
            ref: Bound account reference.
        """
        row = self.plugin.store._read(ref)
        config = next(
            (
                c
                for c in self.plugin.context.get_config().get("platform", [])
                if c.get("id") == row["instance_id"] and c.get("session_ref") == ref
            ),
            {},
        )
        if not config.get("enable"):
            return
        groups = list(
            dict.fromkeys(config.get("message_policy", {}).get("group_ids", []))
        )
        if not groups:
            return
        start = self.maintenance_group_cursors.get(ref, 0) % len(groups)
        selected = [
            groups[(start + index) % len(groups)]
            for index in range(min(100, len(groups)))
        ]
        self.maintenance_group_cursors[ref] = (start + len(selected)) % len(groups)
        for group in selected:
            current = await self.db(ref, "settings", group)
            settings = current["settings"]
            rules = settings["maintenance"]
            if not (
                rules["cards_enabled"]
                or rules["cleanup_enabled"]
                or settings["manager_user_ids"]
                or settings["invitations_enabled"]
            ):
                continue
            async with instance_lock(row["instance_id"]):
                async with self.locks.setdefault(ref, asyncio.Lock()):
                    self.instance(ref, group)
                    if not self.plugin_enabled(self.group_origin(ref, group)):
                        continue
                    roster = await self.roster(ref, group)
                    await self.db(ref, "revoke_departed_managers", group, list(roster))
                    self.settings_cache.pop((ref, group), None)
                    self.settings_generation += 1
                    current = await self.db(ref, "settings", group)
                    settings, rules = (
                        current["settings"],
                        current["settings"]["maintenance"],
                    )
                    excluded = {
                        r["account_id"]
                        for r in self.plugin.store._all()
                        if r["state"] == "bound"
                    }
                    if settings["invitations_enabled"]:
                        for note in await self.db(ref, "pending_notes", group):
                            await self.db(
                                ref, "invitation_credit", group, note, roster, excluded
                            )
                        await self.db(
                            ref, "recover_invitations", group, roster, excluded
                        )
                    if not (rules["cards_enabled"] or rules["cleanup_enabled"]):
                        continue
                    await self.validate_capabilities(ref, settings)
                    candidates = sorted(
                        uid
                        for uid, member in roster.items()
                        if uid not in excluded
                        and uid not in settings["manager_user_ids"]
                        and member.get("roleLevel") == 20
                    )
                    ordered = await self.db(
                        ref, "scan_batch", group, "members", candidates, 20
                    )
                    for uid in ordered:
                        member = roster[uid]
                        action = None
                        if rules["cleanup_enabled"]:
                            from urllib.parse import urlencode

                            state = await self.plugin.manager.request(
                                ref,
                                "GET",
                                "/v1/groups/member-availability?"
                                + urlencode({"group_id": group, "user_id": uid}),
                            )
                            if state.get("verified") and state.get("availability") in {
                                "banned",
                                "deactivated",
                            }:
                                action = {"action": "cleanup_unavailable_member"}
                        if not action and rules["cards_enabled"]:
                            nickname = str(member.get("nickname", ""))
                            target = await self.db(
                                ref, "card_plan", group, uid, nickname, rules
                            )
                            if target and not any(
                                other != uid and person.get("nickname") == target
                                for other, person in roster.items()
                            ):
                                action = {
                                    "action": "set_member_group_nickname",
                                    "nickname": target,
                                    "expected_nickname": nickname,
                                }
                        if not action:
                            continue
                        body = {
                            **action,
                            "operation_id": "maintenance-preflight",
                            "kind": "group",
                            "target_id": group,
                            "user_id": uid,
                            "business_version": current["version"],
                        }
                        try:
                            payload = await self.native_payload(ref, group, body)
                            identifier = await self.db(
                                ref,
                                "reserve_effect",
                                group,
                                action["action"],
                                json.dumps(
                                    [current["version"], action], sort_keys=True
                                ),
                                uid,
                                payload,
                            )
                            if not identifier:
                                continue
                            payload["operation_id"] = identifier
                            receipt = await self.instance(ref, group).platform_action(
                                payload
                            )
                            if (
                                receipt.get("status") == "succeeded"
                                and action["action"] == "set_member_group_nickname"
                            ):
                                fresh = await self.roster(ref, group)
                                if (
                                    fresh.get(uid, {}).get("nickname")
                                    == action["nickname"]
                                ):
                                    await self.db(
                                        ref,
                                        "card_applied",
                                        group,
                                        uid,
                                        action["nickname"],
                                    )
                                else:
                                    receipt = {
                                        "status": "unknown",
                                        "error": "nickname_readback_not_confirmed",
                                    }
                            await self.db(ref, "finish_effect", identifier, receipt)
                        except asyncio.CancelledError:
                            raise
                        except (SessionError, PermissionError, ValueError):
                            continue

    async def close(self):
        self.stopping = True
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
        tasks = tuple(self.account_pollers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.account_pollers.clear()
        if self.database_tasks:
            _, pending = await asyncio.wait(tuple(self.database_tasks), timeout=6)
            if pending:
                self.plugin.logger.warning(
                    "LajiaoLiao database shutdown exceeded the bounded wait; pending effects require review"
                )
        self.contexts.clear()
        self.windows.clear()
        self.settings_cache.clear()
        self.maintenance_group_cursors.clear()
