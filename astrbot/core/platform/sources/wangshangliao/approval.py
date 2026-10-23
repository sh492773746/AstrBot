"""Persistent, session-bound human confirmations for moderation."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing

from .storage import instance_dir
from .wire import ProtocolError


def approval(instance: str, owner: str, session: dict, payload: dict) -> dict:
    """Create or consume a ten-minute, single-use operation confirmation.

    Args:
        instance: Saved bot ID.
        owner: Authenticated Dashboard user.
        session: Current vault session, binding approval to this login.
        payload: Preview parameters or an approval token for execution.

    Returns:
        An immutable operation preview or the consumed operation.

    Raises:
        ProtocolError: If input, ownership, session, expiry or consumption fails.
    """
    fingerprint = hashlib.sha256(
        json.dumps(session["business"], sort_keys=True).encode()
    ).hexdigest()
    root = instance_dir(instance)
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "moderation.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS approvals (token TEXT PRIMARY KEY, owner TEXT, fingerprint TEXT, expires REAL, operation TEXT, used INTEGER DEFAULT 0)"
        )
        if payload.get("action") == "moderation_preview":
            action = payload.get("operation_action")
            group, member, text = (
                payload.get("group"),
                payload.get("member", 0),
                payload.get("text", ""),
            )
            if (
                action not in {"mute", "unmute", "mute_all", "unmute_all", "announce"}
                or type(group) is not int
                or group <= 0
            ):
                raise ProtocolError("moderation_arguments")
            if action in {"mute", "unmute"} and (
                type(member) is not int or member <= 0
            ):
                raise ProtocolError("moderation_arguments")
            if (
                not isinstance(text, str)
                or len(text) > 2000
                or (action == "announce" and not text.strip())
            ):
                raise ProtocolError("moderation_arguments")
            token = secrets.token_urlsafe(32)
            operation = {
                "operation": secrets.token_hex(16),
                "action": action,
                "group": group,
                "member": member,
                "text": text,
                "account": str(session["business"]["uid"]),
            }
            expires = time.time() + 600
            db.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,0)",
                (token, owner, fingerprint, expires, json.dumps(operation)),
            )
            db.execute(
                "DELETE FROM approvals WHERE expires < ?", (time.time() - 86400,)
            )
            return {"approval_token": token, "expires": expires, **operation}
        if payload.get("action") != "moderation_execute" or set(payload) != {
            "action",
            "instance_id",
            "approval_token",
        }:
            raise ProtocolError("moderation_arguments")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT owner,fingerprint,expires,operation,used FROM approvals WHERE token=?",
            (payload.get("approval_token"),),
        ).fetchone()
        if (
            not row
            or row[0] != owner
            or row[1] != fingerprint
            or row[2] < time.time()
            or row[4]
        ):
            raise ProtocolError("moderation_approval_invalid")
        db.execute(
            "UPDATE approvals SET used=1 WHERE token=?", (payload["approval_token"],)
        )
        return json.loads(row[3])
