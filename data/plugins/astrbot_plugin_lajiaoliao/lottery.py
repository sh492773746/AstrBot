"""Durable, ordered per-person lottery notifications; never retry uncertainty."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from .storage import SessionError

GIF_SHA256 = "75accd59c812d798aff97ff5ce327ee34ba6d65aade8010fbd297363c08d8110"


def result_gif():
    raw = (Path(__file__).parent / "assets/lottery-v1.gif").read_bytes()
    if hashlib.sha256(raw).hexdigest() != GIF_SHA256:
        raise SessionError("lottery_gif_integrity_failed")
    return {"type": "image", "url": "base64://" + base64.b64encode(raw).decode()}


def result_text(prize, won):
    return f"您的奖品是 {prize}" if won else "抱歉，您未中奖"


class LotteryLedger:
    def initialize_lottery(self, db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS enrollment_messages(
                activity_id TEXT, member_id TEXT, message_id TEXT NOT NULL,
                PRIMARY KEY(activity_id,member_id));
            CREATE TABLE IF NOT EXISTS lottery_delivery(
                activity_id TEXT, member_id TEXT, stage TEXT, message_id TEXT,
                text TEXT, state TEXT, receipt TEXT,
                PRIMARY KEY(activity_id,member_id,stage));
        """)
        db.execute("UPDATE lottery_delivery SET state='unknown' WHERE state='sending'")

    def freeze_notifications(self, db, identifier, people, winners, params):
        if not params.get("notify_results", False):
            return
        winning_ids = {person["member_id"] for person in winners}
        for person in people:
            member = person["member_id"]
            original = db.execute(
                "SELECT message_id FROM enrollment_messages WHERE activity_id=? AND member_id=?",
                (identifier, member),
            ).fetchone()
            message = original["message_id"] if original else ""
            for stage in ("quote", "gif"):
                state = (
                    "pending"
                    if message
                    else ("rejected" if stage == "quote" else "skipped")
                )
                db.execute(
                    "INSERT INTO lottery_delivery VALUES(?,?,?,?,?,?,?)",
                    (
                        identifier,
                        member,
                        stage,
                        message,
                        result_text(params["prize"], member in winning_ids)
                        if stage == "quote"
                        else "",
                        state,
                        json.dumps({"status": state, "error": "signup_message_missing"})
                        if not message
                        else "",
                    ),
                )

    def notification_rows(self, group, identifier):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT d.* FROM lottery_delivery d JOIN activities a ON a.id=d.activity_id
                    WHERE a.group_id=? AND a.id=?
                    ORDER BY d.member_id,CASE d.stage WHEN 'quote' THEN 0 ELSE 1 END""",
                    (group, identifier),
                )
            ]

    def reserve_notification(self, group, identifier, member, stage):
        if stage not in {"quote", "gif"}:
            raise ValueError("invalid_notification_stage")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT 1 FROM activities WHERE id=? AND group_id=? AND state='publishing'",
                (identifier, group),
            ).fetchone()
            if not active:
                raise SessionError("activity_state_changed")
            if (
                stage == "gif"
                and not db.execute(
                    """SELECT 1 FROM lottery_delivery WHERE activity_id=? AND member_id=?
                AND stage='quote' AND state='sent'""",
                    (identifier, member),
                ).fetchone()
            ):
                return False
            return (
                db.execute(
                    """UPDATE lottery_delivery SET state='sending' WHERE activity_id=?
                AND member_id=? AND stage=? AND state='pending'""",
                    (identifier, member, stage),
                ).rowcount
                == 1
            )

    def finish_notification(self, group, identifier, member, stage, receipt):
        status = receipt.get("status", "unknown")
        if status not in {"sent", "rejected", "unsupported", "failed", "unknown"}:
            status = "unknown"
        safe = {"status": status}
        for key in ("message_id", "error"):
            value = receipt.get(key, "")
            if isinstance(value, str) and re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", value
            ):
                safe[key] = value
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM activities WHERE id=? AND group_id=? AND state='publishing'",
                (identifier, group),
            ).fetchone():
                raise SessionError("activity_state_changed")
            if (
                db.execute(
                    """UPDATE lottery_delivery SET state=?,receipt=? WHERE activity_id=?
                AND member_id=? AND stage=? AND state='sending'""",
                    (status, json.dumps(safe), identifier, member, stage),
                ).rowcount
                != 1
            ):
                raise SessionError("notification_state_changed")
            if stage == "quote" and status != "sent":
                db.execute(
                    """UPDATE lottery_delivery SET state='skipped' WHERE activity_id=?
                    AND member_id=? AND stage='gif' AND state='pending'""",
                    (identifier, member),
                )
            self.log(
                db,
                group,
                "lottery_notification",
                identifier + ":" + member + ":" + stage,
                status,
                safe.get("error", ""),
            )

    @staticmethod
    def notification_summary(db, identifier):
        return {
            row["state"]: row["count"]
            for row in db.execute(
                """SELECT state,COUNT(*) AS count FROM lottery_delivery
                WHERE activity_id=? GROUP BY state""",
                (identifier,),
            )
        }
