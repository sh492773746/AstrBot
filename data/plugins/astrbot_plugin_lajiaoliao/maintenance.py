"""Persistent, default-off maintenance and authorized scheduled businesses."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import regex

from .storage import SessionError

ZONE = ZoneInfo("Asia/Shanghai")
MAINTENANCE_DEFAULTS = {
    "quiet_enabled": False,
    "quiet_start": "23:00",
    "quiet_end": "07:00",
    "cards_enabled": False,
    "name_characters": 2,
    "prefix": "",
    "suffix": "",
    "cleanup_enabled": False,
    "reply_recall_enabled": False,
}
LOTTERY_PLAN_DEFAULTS = {"enabled": False, "starts_at": 0}
REMINDER_PLAN_DEFAULTS = {"enabled": False, "clock": "09:00", "text": ""}


class ScheduleBoundaryExpired(SessionError):
    delivery_status = "paused"


def require_current_boundary(due):
    if due is not None and time.time() - due > 30:
        raise ScheduleBoundaryExpired("schedule_boundary_expired")


def validate_maintenance(value):
    """Validate independent default-off maintenance rules.

    Args:
        value: Group maintenance configuration.

    Returns:
        A normalized private copy.

    Raises:
        ValueError: For invalid rules.
    """
    if not isinstance(value, dict) or set(value) - set(MAINTENANCE_DEFAULTS):
        raise ValueError("invalid_maintenance_settings")
    value = {**MAINTENANCE_DEFAULTS, **value}
    for key in (
        "quiet_enabled",
        "cards_enabled",
        "cleanup_enabled",
        "reply_recall_enabled",
    ):
        if type(value[key]) is not bool:
            raise ValueError("invalid_maintenance_switch")
    for key in ("quiet_start", "quiet_end"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", value[key]
        ):
            raise ValueError("invalid_schedule_clock")
    if value["quiet_start"] == value["quiet_end"]:
        raise ValueError("quiet_boundaries_equal")
    if (
        type(value["name_characters"]) is not int
        or not 1 <= value["name_characters"] <= 15
    ):
        raise ValueError("invalid_card_length")
    for key in ("prefix", "suffix"):
        if (
            not isinstance(value[key], str)
            or len(value[key]) > 10
            or any(unicodedata.category(c).startswith("C") for c in value[key])
        ):
            raise ValueError("invalid_card_text")
    if len(value["prefix"] + value["suffix"]) + value["name_characters"] > 15:
        raise ValueError("invalid_card_length")
    return copy.deepcopy(value)


def validate_plans(lottery, reminder):
    """Validate saved plans without interpreting Dashboard identity as a UID.

    Args:
        lottery: Single future activity configuration.
        reminder: Daily notification configuration.

    Returns:
        Normalized lottery and reminder settings.

    Raises:
        ValueError: For invalid settings.
    """
    if not isinstance(lottery, dict) or set(lottery) - set(LOTTERY_PLAN_DEFAULTS):
        raise ValueError("invalid_lottery_plan")
    if not isinstance(reminder, dict) or set(reminder) - set(REMINDER_PLAN_DEFAULTS):
        raise ValueError("invalid_reminder_plan")
    lottery, reminder = (
        {**LOTTERY_PLAN_DEFAULTS, **lottery},
        {**REMINDER_PLAN_DEFAULTS, **reminder},
    )
    if type(lottery["enabled"]) is not bool or type(reminder["enabled"]) is not bool:
        raise ValueError("invalid_schedule_switch")
    stamp = lottery["starts_at"]
    if type(stamp) not in {int, float} or not 0 <= stamp <= 4102444800:
        raise ValueError("invalid_lottery_start")
    if not isinstance(reminder["clock"], str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", reminder["clock"]
    ):
        raise ValueError("invalid_schedule_clock")
    text = reminder["text"]
    if (
        not isinstance(text, str)
        or len(text) > 2000
        or any(ord(c) < 32 and c not in "\n\t" for c in text)
    ):
        raise ValueError("invalid_reminder_text")
    if reminder["enabled"] and not text.strip():
        raise ValueError("empty_reminder_text")
    return copy.deepcopy(lottery), copy.deepcopy(reminder)


def next_boundary(clock, now):
    """Return a strictly future Beijing-time boundary.

    Args:
        clock: Validated HH:MM.
        now: Unix timestamp.

    Returns:
        Future Unix timestamp.
    """
    hour, minute = map(int, clock.split(":"))
    target = datetime.fromtimestamp(now, ZONE).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


class MaintenanceLedger:
    """Mix-in operating on the account's existing SQLite transaction boundary."""

    @staticmethod
    def initialize_maintenance(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS feature_plans(
                group_id TEXT, feature TEXT, revision INTEGER, value TEXT,
                owner TEXT, PRIMARY KEY(group_id,feature));
            CREATE TABLE IF NOT EXISTS scheduled_jobs(
                id TEXT PRIMARY KEY, group_id TEXT, feature TEXT, revision INTEGER,
                owner TEXT, due REAL, state TEXT, payload TEXT, receipt TEXT);
            CREATE INDEX IF NOT EXISTS jobs_due ON scheduled_jobs(state,due);
            CREATE TABLE IF NOT EXISTS maintenance_effects(
                id TEXT PRIMARY KEY, group_id TEXT, feature TEXT, user_id TEXT,
                stamp REAL, state TEXT, payload TEXT, receipt TEXT);
            CREATE INDEX IF NOT EXISTS effect_member ON maintenance_effects(group_id,feature,user_id,state);
            CREATE TABLE IF NOT EXISTS original_cards(
                group_id TEXT, user_id TEXT, original TEXT, last_applied TEXT,
                PRIMARY KEY(group_id,user_id));
            CREATE TABLE IF NOT EXISTS pending_invitations(
                group_id TEXT, member_id TEXT, stamp REAL, note TEXT,
                PRIMARY KEY(group_id,member_id));
            CREATE TABLE IF NOT EXISTS maintenance_scan(
                group_id TEXT, feature TEXT, cursor TEXT,
                PRIMARY KEY(group_id,feature));
        """)
        db.execute("UPDATE scheduled_jobs SET state='unknown' WHERE state='running'")

    def configure_plans(self, db, group, old, value, actor):
        now = time.time()
        db.execute(
            "DELETE FROM scheduled_jobs WHERE due<? AND state IN ('succeeded','failed','cancelled')",
            (now - 31 * 86400,),
        )
        if db.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0] >= 4096:
            raise SessionError("schedule_limit_reached")
        features = {
            "quiet": {
                k: value["maintenance"][k]
                for k in ("quiet_enabled", "quiet_start", "quiet_end")
            },
            "lottery": {
                "plan": value["lottery_plan"],
                "params": value["lottery"],
                "enabled": value["lottery_enabled"],
            },
            "notification": {
                "plan": value["reminder_plan"],
                "enabled": value["reminders_enabled"],
            },
        }
        for feature, spec in features.items():
            previous = db.execute(
                "SELECT * FROM feature_plans WHERE group_id=? AND feature=?",
                (group, feature),
            ).fetchone()
            if previous and json.loads(previous["value"]) == spec:
                continue
            enabled = (
                spec["quiet_enabled"]
                if feature == "quiet"
                else spec["enabled"] and spec["plan"]["enabled"]
            )
            if enabled and not actor.startswith("dashboard:"):
                raise SessionError("dashboard_schedule_grants_required")
            if feature == "lottery" and enabled and spec["plan"]["starts_at"] <= now:
                raise ValueError("lottery_start_must_be_future")
            if previous:
                db.execute(
                    "UPDATE scheduled_jobs SET state='cancelled' WHERE group_id=? AND feature=? AND state='pending'",
                    (group, feature),
                )
            revision = previous["revision"] + 1 if previous else 1
            db.execute(
                "INSERT INTO feature_plans VALUES(?,?,?,?,?) ON CONFLICT(group_id,feature) DO UPDATE SET revision=excluded.revision,value=excluded.value,owner=excluded.owner",
                (group, feature, revision, json.dumps(spec), actor),
            )
            if not enabled:
                continue
            tasks = []
            if feature == "quiet":
                tasks = [
                    (next_boundary(spec["quiet_start"], now), {"action": "mute_all"}),
                    (next_boundary(spec["quiet_end"], now), {"action": "unmute_all"}),
                ]
            elif feature == "lottery":
                if not spec["params"]["prize"] or not spec["params"]["contact"]:
                    raise ValueError("lottery_disabled_or_incomplete")
                tasks = [(spec["plan"]["starts_at"], {"action": "lottery"})]
            else:
                tasks = [
                    (
                        next_boundary(spec["plan"]["clock"], now),
                        {"action": "notification", "text": spec["plan"]["text"]},
                    )
                ]
            for due, payload in tasks:
                db.execute(
                    "INSERT INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        secrets.token_hex(16),
                        group,
                        feature,
                        revision,
                        actor,
                        due,
                        "pending",
                        json.dumps(payload),
                        "",
                    ),
                )

    def scheduled(self, group="", due=None):
        with self.connect() as db:
            if due is not None:
                rows = db.execute(
                    "SELECT * FROM scheduled_jobs WHERE state='pending' AND due<=? ORDER BY due LIMIT 20",
                    (due,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM scheduled_jobs WHERE group_id=? ORDER BY due DESC LIMIT 100",
                    (group,),
                ).fetchall()
            return [
                {**dict(row), "payload": json.loads(row["payload"])} for row in rows
            ]

    def reserve_job(self, identifier, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM scheduled_jobs WHERE id=?", (identifier,)
            ).fetchone()
            if not row or row["state"] != "pending" or row["due"] > now:
                return None
            plan = db.execute(
                "SELECT * FROM feature_plans WHERE group_id=? AND feature=?",
                (row["group_id"], row["feature"]),
            ).fetchone()
            if (
                row["feature"] != "reply_recall"
                and (not plan or plan["revision"] != row["revision"])
            ) or now - row["due"] > 30:
                db.execute(
                    "UPDATE scheduled_jobs SET state='paused' WHERE id=?", (identifier,)
                )
                self.log(
                    db,
                    row["group_id"],
                    "schedule",
                    identifier,
                    "paused",
                    "expired_or_changed",
                )
                return None
            db.execute(
                "UPDATE scheduled_jobs SET state='running' WHERE id=?", (identifier,)
            )
            return {**dict(row), "payload": json.loads(row["payload"])}

    def job_current(self, identifier):
        """Check a reserved job's feature revision immediately before execution.

        Args:
            identifier: Persistent job ID.

        Returns:
            Whether the job still belongs to the current feature configuration.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM scheduled_jobs WHERE id=?", (identifier,)
            ).fetchone()
            if not row or row["state"] != "running":
                return False
            if row["feature"] == "reply_recall":
                return True
            plan = db.execute(
                "SELECT revision FROM feature_plans WHERE group_id=? AND feature=?",
                (row["group_id"], row["feature"]),
            ).fetchone()
            return bool(plan and plan["revision"] == row["revision"])

    def finish_job(self, identifier, receipt):
        status = receipt.get("status", "unknown")
        state = (
            "succeeded"
            if status in {"succeeded", "sent"}
            else "paused"
            if status == "paused"
            else (
                "failed"
                if status in {"failed", "rejected", "unsupported"}
                else "unknown"
            )
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM scheduled_jobs WHERE id=?", (identifier,)
            ).fetchone()
            if not row or row["state"] != "running":
                raise SessionError("schedule_state_changed")
            db.execute(
                "UPDATE scheduled_jobs SET state=?,receipt=? WHERE id=?",
                (state, json.dumps(receipt), identifier),
            )
            self.log(db, row["group_id"], "schedule", identifier, state)
            plan = db.execute(
                "SELECT * FROM feature_plans WHERE group_id=? AND feature=?",
                (row["group_id"], row["feature"]),
            ).fetchone()
            if (
                state == "succeeded"
                and plan
                and plan["revision"] == row["revision"]
                and row["feature"] not in {"lottery", "reply_recall"}
            ):
                spec = json.loads(plan["value"])
                payload = json.loads(row["payload"])
                clock = (
                    (
                        spec["quiet_start"]
                        if payload["action"] == "mute_all"
                        else spec["quiet_end"]
                    )
                    if row["feature"] == "quiet"
                    else spec["plan"]["clock"]
                )
                db.execute(
                    "INSERT INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        secrets.token_hex(16),
                        row["group_id"],
                        row["feature"],
                        row["revision"],
                        row["owner"],
                        next_boundary(clock, time.time()),
                        "pending",
                        row["payload"],
                        "",
                    ),
                )

    def reserve_effect(self, group, feature, key, uid, payload):
        identifier = (
            "effect-"
            + hashlib.sha256(
                json.dumps([group, feature, key, uid], sort_keys=True).encode()
            ).hexdigest()[:32]
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM maintenance_effects WHERE stamp<? AND state!='unknown'",
                (time.time() - 31 * 86400,),
            )
            if (
                db.execute("SELECT COUNT(*) FROM maintenance_effects").fetchone()[0]
                >= 4096
            ):
                raise SessionError("maintenance_ledger_full")
            if db.execute(
                "SELECT 1 FROM maintenance_effects WHERE group_id=? AND feature=? AND user_id=? AND state='unknown'",
                (group, feature, uid),
            ).fetchone():
                return None
            if not db.execute(
                "INSERT OR IGNORE INTO maintenance_effects VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    group,
                    feature,
                    uid,
                    time.time(),
                    "unknown",
                    json.dumps(payload),
                    "",
                ),
            ).rowcount:
                return None
        return identifier

    def finish_effect(self, identifier, receipt):
        state = receipt.get("status", "unknown")
        if state not in {"succeeded", "failed", "rejected", "unsupported", "unknown"}:
            state = "unknown"
        with self.connect() as db:
            row = db.execute(
                "SELECT group_id FROM maintenance_effects WHERE id=?", (identifier,)
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE maintenance_effects SET state=?,receipt=? WHERE id=? AND state='unknown'",
                    (state, json.dumps(receipt), identifier),
                )
                self.log(db, row["group_id"], "maintenance", identifier, state)

    def effects(self, group):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,feature,user_id,stamp,state FROM maintenance_effects WHERE group_id=? ORDER BY stamp DESC LIMIT 100",
                    (group,),
                )
            ]

    def recent_member_messages(self, group, uid, since, digest=""):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT message_id,stamp FROM observations WHERE group_id=? AND member_id=? AND stamp>=? AND revoked=0 AND (?='' OR digest=?) ORDER BY stamp LIMIT 32",
                    (group, uid, since, digest, digest),
                )
            ]

    def card_plan(self, group, uid, nickname, rules):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM original_cards WHERE group_id=? AND user_id=?",
                (group, uid),
            ).fetchone()
            original = (
                old["original"] if old and old["last_applied"] == nickname else nickname
            )
            target = (
                rules["prefix"]
                + "".join(regex.findall(r"\X", original)[: rules["name_characters"]])
                + rules["suffix"]
            )
            if not target or len(target) > 15 or target == nickname:
                return None
            if old and old["last_applied"] == nickname:
                db.execute(
                    "UPDATE original_cards SET original=? WHERE group_id=? AND user_id=?",
                    (original, group, uid),
                )
            else:
                db.execute(
                    "INSERT INTO original_cards VALUES(?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET original=excluded.original,last_applied=excluded.last_applied",
                    (group, uid, original, nickname),
                )
            return target

    def card_applied(self, group, uid, target):
        with self.connect() as db:
            db.execute(
                "UPDATE original_cards SET last_applied=? WHERE group_id=? AND user_id=?",
                (target, group, uid),
            )

    def pending_invitation(self, group, note):
        with self.connect() as db:
            db.execute(
                "DELETE FROM pending_invitations WHERE stamp<?", (time.time() - 86400,)
            )
            if (
                db.execute("SELECT COUNT(*) FROM pending_invitations").fetchone()[0]
                >= 4096
            ):
                raise SessionError("invitation_pending_full")
            for uid in note.get("member_ids", [])[:100]:
                db.execute(
                    "INSERT OR IGNORE INTO pending_invitations VALUES(?,?,?,?)",
                    (group, uid, note.get("timestamp", 0), json.dumps(note)),
                )

    def pending_notes(self, group):
        with self.connect() as db:
            db.execute(
                "DELETE FROM pending_invitations WHERE stamp<?", (time.time() - 86400,)
            )
            return [
                json.loads(row["note"])
                for row in db.execute(
                    "SELECT note FROM pending_invitations WHERE group_id=? ORDER BY stamp LIMIT 100",
                    (group,),
                )
            ]

    def scan_batch(self, group, feature, candidates, limit):
        """Rotate bounded reconciliation work without starving later members."""
        identifiers = sorted(candidates)
        if not identifiers:
            return []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT cursor FROM maintenance_scan WHERE group_id=? AND feature=?",
                (group, feature),
            ).fetchone()
            if (
                row is None
                and db.execute("SELECT COUNT(*) FROM maintenance_scan").fetchone()[0]
                >= 4096
            ):
                raise SessionError("maintenance_ledger_full")
            cursor = row["cursor"] if row else ""
            batch = [uid for uid in identifiers if uid > cursor]
            batch.extend(uid for uid in identifiers if uid <= cursor)
            batch = batch[:limit]
            db.execute(
                "INSERT INTO maintenance_scan VALUES(?,?,?) ON CONFLICT(group_id,feature) DO UPDATE SET cursor=excluded.cursor",
                (group, feature, batch[-1]),
            )
        return batch

    def recover_invitations(self, group, roster, excluded):
        """Supplement fresh membership evidence without awarding the baseline.

        Args:
            group: Verified group.
            roster: Complete current membership snapshot.
            excluded: Bound robot IDs.
        """
        with self.connect() as db:
            epoch = db.execute(
                "SELECT stamp FROM invitation_epoch WHERE group_id=?", (group,)
            ).fetchone()
            if not epoch:
                return
        for uid in self.scan_batch(group, "invitations", roster, 2000):
            member = roster[uid]
            joined = member.get("joinTime", 0)
            if (
                type(joined) not in {int, float}
                or not epoch["stamp"] <= joined / 1000 <= time.time()
            ):
                continue
            self.invitation_credit(
                group,
                {
                    "timestamp": joined / 1000,
                    "member_ids": [uid],
                    "inviter_id": member.get("inviterUserID", ""),
                    "message_id": "roster-" + uid + "-" + str(joined),
                    "reconciled": True,
                },
                roster,
                excluded,
            )

    def revoke_departed_managers(self, group, present):
        """Revoke delegation only after a verified complete membership snapshot.

        Args:
            group: Group scope.
            present: Verified member IDs.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            if not row:
                return
            value = json.loads(row["value"])
            previous = value.get("manager_user_ids", [])
            remaining = [uid for uid in previous if uid in present]
            if remaining != previous:
                value["manager_user_ids"] = remaining
                version = row["version"] + 1
                db.execute(
                    "UPDATE settings SET version=?,value=? WHERE group_id=?",
                    (version, json.dumps(value), group),
                )
                db.execute(
                    "UPDATE activities SET version=? WHERE group_id=? AND state='open'",
                    (version, group),
                )
                db.execute(
                    "UPDATE reminders SET revision=? WHERE group_id=? AND state='active'",
                    (version, group),
                )
                self.log(
                    db,
                    group,
                    "manager_reconciliation",
                    "",
                    "revoked",
                    "count=" + str(len(previous) - len(remaining)),
                )
                for uid in set(previous) - set(remaining):
                    db.execute(
                        "UPDATE reminders SET state='paused' WHERE group_id=? AND owner=? AND state='active'",
                        (group, uid),
                    )
                    db.execute(
                        "UPDATE activities SET state='cancelled' WHERE group_id=? AND owner=? AND state='open'",
                        (group, uid),
                    )

    def schedule_reply(self, group, message, kind, target, owner):
        """Reserve an eligible functional reply for a single future recall.

        Args:
            group: Authorization group scope.
            message: Confirmed native message ID.
            kind: Message target kind.
            target: Exact message target.
            owner: Dashboard principal tied to the bound session.
        """
        identifier = (
            "reply-"
            + hashlib.sha256(
                json.dumps([group, kind, target, message]).encode()
            ).hexdigest()[:32]
        )
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE group_id=?", (group,)
            ).fetchone()
            if not row or not json.loads(row["value"]).get("maintenance", {}).get(
                "reply_recall_enabled"
            ):
                return
            if "reply_recall" not in json.loads(row["value"]).get("native_actions", []):
                return
            if (
                db.execute(
                    "SELECT COUNT(*) FROM scheduled_jobs WHERE state IN ('pending','running','unknown')"
                ).fetchone()[0]
                >= 4096
            ):
                raise SessionError("schedule_limit_reached")
            db.execute(
                "INSERT OR IGNORE INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    group,
                    "reply_recall",
                    0,
                    owner,
                    time.time() + 20,
                    "pending",
                    json.dumps(
                        {
                            "action": "reply_recall",
                            "kind": kind,
                            "target_id": target,
                            "message_id": message,
                        }
                    ),
                    "",
                ),
            )
