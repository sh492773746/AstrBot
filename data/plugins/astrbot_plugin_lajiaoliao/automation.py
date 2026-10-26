"""Bounded automatic mute rules and an account-scoped, no-retry ledger."""

import copy
import hashlib
import json
import math
import time

from .storage import SessionError

AUTOMATION_DEFAULTS = {
    "enabled": False,
    "keyword_enabled": True,
    "flood_enabled": True,
    "ai_enabled": False,
    "ai_rules": "",
    "ai_min_confidence": 0.95,
    "flood_limit": 8,
    "flood_window_seconds": 10,
    "duplicate_limit": 5,
    "duplicate_window_seconds": 30,
    "violation_window_hours": 24,
    "mute_minutes": [5, 15, 60],
    "cooldown_seconds": 60,
    "punishment_mode": "mute",
    "flood_recall": False,
}


def validate_automation(value):
    if not isinstance(value, dict) or set(value) - set(AUTOMATION_DEFAULTS):
        raise ValueError("invalid_automation_rules")
    value = {**copy.deepcopy(AUTOMATION_DEFAULTS), **value}
    for key in (
        "enabled",
        "keyword_enabled",
        "flood_enabled",
        "ai_enabled",
        "flood_recall",
    ):
        if type(value[key]) is not bool:
            raise ValueError("invalid_automation_switch")
    if value["punishment_mode"] not in {"mute", "recall_progressive"}:
        raise ValueError("invalid_punishment_mode")
    policy = value["ai_rules"]
    if (
        not isinstance(policy, str)
        or len(policy) > 2000
        or any(ord(c) < 32 and c not in "\n\t" for c in policy)
    ):
        raise ValueError("invalid_ai_rules")
    if value["enabled"] and value["ai_enabled"] and not policy.strip():
        raise ValueError("automation_requires_ai_rules")
    for key, low, high in (
        ("flood_limit", 2, 32),
        ("flood_window_seconds", 1, 60),
        ("duplicate_limit", 2, 32),
        ("duplicate_window_seconds", 1, 120),
        ("violation_window_hours", 1, 168),
        ("cooldown_seconds", 60, 3600),
    ):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError("invalid_automation_number")
    confidence = value["ai_min_confidence"]
    if (
        type(confidence) not in {int, float}
        or not math.isfinite(confidence)
        or not 0.9 <= confidence <= 1
    ):
        raise ValueError("invalid_automation_confidence")
    minutes = value["mute_minutes"]
    if (
        not isinstance(minutes, list)
        or len(minutes) != 3
        or any(type(item) is not int or not 1 <= item <= 60 for item in minutes)
        or minutes != sorted(minutes)
    ):
        raise ValueError("invalid_automation_duration")
    return copy.deepcopy(value)


class AutomationLedger:
    """Storage methods share BusinessStore's connection and transaction boundary."""

    @staticmethod
    def initialize_automation(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS automatic_moderation(
                group_id TEXT, message_id TEXT, user_id TEXT, stamp REAL,
                reason TEXT, decision TEXT, operation_id TEXT UNIQUE,
                status TEXT, muted_seconds INTEGER, version INTEGER,
                PRIMARY KEY(group_id,message_id));
            CREATE INDEX IF NOT EXISTS automatic_member
                ON automatic_moderation(group_id,user_id,stamp);
        """)

    def automatic_events(self, group):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM automatic_moderation WHERE group_id=? ORDER BY (status='unknown') DESC,stamp DESC LIMIT 100",
                (group,),
            ).fetchall()
        return [dict(row) for row in rows]

    def automatic_rejection(self, group, message, error):
        with self.connect() as db:
            self.log(db, group, "automation", message, "rejected", error)

    def reserve_automatic(self, group, message, uid, stamp, reason, version):
        now = time.time()
        if (
            reason not in {"keyword", "flood", "ai_violation"}
            or not message
            or len(message) > 128
        ):
            raise ValueError("invalid_automatic_evidence")
        if not 0 <= now - stamp <= 60:
            return None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            if not current or current["version"] != version:
                raise SessionError("business_changed_refresh_required")
            settings = json.loads(current["value"])
            rules = validate_automation(settings.get("automation", {}))
            enabled = {
                "keyword": "keyword_enabled",
                "flood": "flood_enabled",
                "ai_violation": "ai_enabled",
            }
            if (
                not settings["review_enabled"]
                or not rules["enabled"]
                or not rules[enabled[reason]]
                or "mute" not in settings["native_actions"]
            ):
                return None
            if db.execute(
                "SELECT 1 FROM automatic_moderation WHERE group_id=? AND message_id=?",
                (group, message),
            ).fetchone():
                return None
            db.execute(
                "DELETE FROM automatic_moderation WHERE stamp<? AND status!='unknown'",
                (now - 31 * 86400,),
            )
            if (
                db.execute("SELECT COUNT(*) FROM automatic_moderation").fetchone()[0]
                >= 4096
            ):
                raise SessionError("automation_ledger_full")
            unknown = db.execute(
                "SELECT 1 FROM automatic_moderation WHERE group_id=? AND user_id=? AND status='unknown'",
                (group, uid),
            ).fetchone()
            cooldown = db.execute(
                "SELECT 1 FROM automatic_moderation WHERE group_id=? AND user_id=? AND decision='violation' AND stamp>?",
                (group, uid, now - rules["cooldown_seconds"]),
            ).fetchone()
            count = db.execute(
                "SELECT COUNT(*) FROM automatic_moderation WHERE group_id=? AND user_id=? AND decision='violation' AND status IN ('succeeded','unknown') AND stamp>?",
                (group, uid, now - rules["violation_window_hours"] * 3600),
            ).fetchone()[0]
            seconds = rules["mute_minutes"][min(count, 2)] * 60
            if rules["punishment_mode"] == "recall_progressive":
                seconds = (
                    0 if count < 3 else rules["mute_minutes"][min(count - 3, 2)] * 60
                )
            operation = (
                "auto-"
                + hashlib.sha256(
                    json.dumps([group, message, uid]).encode()
                ).hexdigest()[:32]
            )
            status = "rejected" if unknown or cooldown else "unknown"
            decision = (
                "blocked_unknown"
                if unknown
                else ("cooldown" if cooldown else "violation")
            )
            db.execute(
                "INSERT INTO automatic_moderation VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    group,
                    message,
                    uid,
                    now,
                    reason,
                    decision,
                    operation,
                    status,
                    seconds,
                    version,
                ),
            )
            self.log(
                db,
                group,
                "automation",
                operation,
                decision,
                f"user={uid}; reason={reason}; seconds={seconds}",
            )
            return (
                {"operation_id": operation, "muted_seconds": seconds}
                if decision == "violation"
                else None
            )

    def finish_automatic(self, group, operation, status):
        if status not in {"succeeded", "failed", "rejected", "unsupported", "unknown"}:
            status = "unknown"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automatic_moderation WHERE group_id=? AND operation_id=?",
                (group, operation),
            ).fetchone()
            if not row:
                raise SessionError("automatic_operation_not_found")
            if row["status"] == "unknown":
                db.execute(
                    "UPDATE automatic_moderation SET status=? WHERE operation_id=?",
                    (status, operation),
                )
                self.log(db, group, "automation_receipt", operation, status)
            return status if row["status"] == "unknown" else row["status"]
