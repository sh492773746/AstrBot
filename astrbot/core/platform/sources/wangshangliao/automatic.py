"""Durable automatic mute escalation with server-owned kick evidence."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing

from .event import is_managed_account
from .policy import authorize_action
from .storage import instance_dir
from .wire import ProtocolError

MUTE_LIMIT = 3
MUTE_MINUTES = (5, 15, 60)


def database(adapter):
    """Open the automatic sanction ledger for one platform instance.

    Args:
        adapter: Native platform adapter.

    Returns:
        SQLite connection with automatic sanction tables initialized.
    """
    root = instance_dir(adapter.config["id"])
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "moderation.sqlite3")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS automatic_mutes(
            operation TEXT PRIMARY KEY, account TEXT NOT NULL, group_id TEXT NOT NULL,
            member TEXT NOT NULL, peer TEXT NOT NULL, message TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, closed INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS automatic_mute_member
            ON automatic_mutes(account,group_id,member,closed);
        CREATE TABLE IF NOT EXISTS automatic_kicks(
            operation TEXT PRIMARY KEY, account TEXT NOT NULL, group_id TEXT NOT NULL,
            member TEXT NOT NULL, peer TEXT NOT NULL, trigger_operation TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS automatic_kick_events(
            operation TEXT PRIMARY KEY, message TEXT NOT NULL);
    """)
    return db


def authorize_kick(adapter, operation: str, group: str, member: str) -> None:
    """Require three accepted mutes and a separate, later violation for removal.

    Args:
        adapter: Current native platform adapter.
        operation: Exact persisted automatic kick operation.
        group: Target business group ID.
        member: Target business member ID.

    Raises:
        ProtocolError: If authorization, identity or three accepted mutes are absent.
    """
    policy = adapter.config.get("moderation", {})
    authorize_action(adapter.config, group, "kick")
    if member == adapter.account or is_managed_account(member):
        raise ProtocolError("moderation_target")
    if (
        not policy.get("automation_enabled")
        or policy.get("manual_kick_only")
        or policy.get("auto_kick", {}).get(group) is not True
    ):
        raise ProtocolError("automatic_kick_not_authorized")
    path = instance_dir(adapter.config["id"]) / "moderation.sqlite3"
    if not path.is_file():
        raise ProtocolError("automatic_kick_evidence")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_kicks'"
        ).fetchone():
            raise ProtocolError("automatic_kick_evidence")
        kick = db.execute(
            "SELECT account,group_id,member,peer,trigger_operation FROM automatic_kicks WHERE operation=?",
            (operation,),
        ).fetchone()
        peer = adapter.members.get(group, {}).get(member)
        if not kick or kick[:4] != (adapter.account, group, member, peer):
            raise ProtocolError("automatic_kick_identity")
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_kick_events'"
        ).fetchone():
            raise ProtocolError("automatic_kick_evidence")
        event = db.execute(
            "SELECT message FROM automatic_kick_events WHERE operation=?", (operation,)
        ).fetchone()
        if (
            not event
            or not event[0]
            or db.execute(
                "SELECT 1 FROM automatic_mutes WHERE account=? AND group_id=? AND member=? AND message=?",
                (adapter.account, group, member, event[0]),
            ).fetchone()
        ):
            raise ProtocolError("automatic_kick_evidence")
        rows = db.execute(
            "SELECT m.operation,o.result FROM automatic_mutes m JOIN operations o ON o.id=m.operation "
            "WHERE m.account=? AND m.group_id=? AND m.member=? AND m.peer=? AND m.closed=0 "
            "AND m.status IN ('accepted','verified')",
            (adapter.account, group, member, peer),
        ).fetchall()
        accepted = []
        for mute_operation, encoded in rows:
            result = json.loads(encoded)
            if (
                result.get("action") == "mute"
                and str(result.get("group")) == group
                and str(result.get("member")) == member
                and result.get("status") in {"accepted", "verified"}
            ):
                accepted.append(mute_operation)
        if len(accepted) < MUTE_LIMIT or kick[4] not in accepted:
            raise ProtocolError("automatic_kick_evidence")


async def mute_and_escalate(
    adapter, operation: str, group: str, member: str, message: str
) -> dict:
    """Mute three times, then remove on the next distinct confirmed violation.

    Args:
        adapter: Authorized native platform adapter.
        operation: Stable automatic mute operation ID.
        group: Verified business group ID.
        member: Verified ordinary member business ID.
        message: Triggering upstream message ID.

    Returns:
        Mutation outcome and accepted mute count. Unknown outcomes are quarantined.
    """
    peer = adapter.members.get(group, {}).get(member, "")
    if not peer or member == adapter.account or is_managed_account(member):
        raise ProtocolError("moderation_identity")
    with closing(database(adapter)) as db, db:
        # Reconcile interrupted bookkeeping only from existing transport receipts.
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone():
            pending = db.execute(
                "SELECT m.operation,o.result FROM automatic_mutes m JOIN operations o ON o.id=m.operation "
                "WHERE m.account=? AND m.group_id=? AND m.member=? AND m.closed=0 AND m.status='unknown'",
                (adapter.account, group, member),
            ).fetchall()
            for pending_id, encoded in pending:
                receipt = json.loads(encoded)
                if (
                    receipt.get("action") == "mute"
                    and str(receipt.get("group")) == group
                    and str(receipt.get("member")) == member
                    and receipt.get("status") in {"accepted", "verified"}
                ):
                    db.execute(
                        "UPDATE automatic_mutes SET status=? WHERE operation=?",
                        (receipt["status"], pending_id),
                    )
        if db.execute(
            "SELECT 1 FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
            "AND closed=0 AND status='unknown'",
            (adapter.account, group, member),
        ).fetchone():
            return {
                "status": "unknown",
                "action": "mute",
                "reason": "prior_mute_unknown",
            }
        mutes = db.execute(
            "SELECT operation FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
            "AND peer=? AND closed=0 AND status IN ('accepted','verified') ORDER BY created,operation",
            (adapter.account, group, member, peer),
        ).fetchall()
        escalation_enabled = adapter.config.get("moderation", {}).get(
            "auto_kick", {}
        ).get(group) is True and not adapter.config.get("moderation", {}).get(
            "manual_kick_only"
        )
        prior = db.execute(
            "SELECT status FROM automatic_mutes WHERE operation=?", (operation,)
        ).fetchone()
        should_mute = not prior and not (
            escalation_enabled and len(mutes) >= MUTE_LIMIT
        )
        # Decide before muting: the third receipt cannot trigger its own removal.
        should_escalate = (
            escalation_enabled
            and len(mutes) >= MUTE_LIMIT
            and not prior
            and bool(message)
            and not db.execute(
                "SELECT 1 FROM automatic_mutes WHERE account=? AND group_id=? AND member=? AND message=?",
                (adapter.account, group, member, message),
            ).fetchone()
        )
        minutes = MUTE_MINUTES[min(len(mutes), len(MUTE_MINUTES) - 1)]
        if should_mute:
            db.execute(
                "INSERT INTO automatic_mutes VALUES(?,?,?,?,?,?,'unknown',?,0)",
                (operation, adapter.account, group, member, peer, message, time.time()),
            )
    result = {"status": prior[0] if prior else "accepted", "action": "mute"}
    if should_mute:
        try:
            result = await adapter.execute_moderation(
                operation,
                "mute",
                int(group),
                int(member),
                **(
                    {"minutes": minutes}
                    if adapter.config.get("moderation", {}).get("progressive_mute")
                    else {}
                ),
            )
        except Exception:
            adapter.diagnostics.emit(
                "automatic_mute", "failed_or_unknown", message, failed=True
            )
            return {"status": "unknown", "action": "mute"}
        status = result.get("status", "unknown")
        if status not in {"accepted", "verified", "rejected"}:
            status = "unknown"
        with closing(database(adapter)) as db, db:
            db.execute(
                "UPDATE automatic_mutes SET status=? WHERE operation=?",
                (status, operation),
            )
        result = {**result, "status": status, "action": "mute"}
    with closing(database(adapter)) as db, db:
        mutes = db.execute(
            "SELECT operation FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
            "AND peer=? AND closed=0 AND status IN ('accepted','verified') ORDER BY created,operation",
            (adapter.account, group, member, peer),
        ).fetchall()
        count = len(mutes)
        if (
            not should_escalate
            or count < MUTE_LIMIT
            or result.get("status") not in {"accepted", "verified"}
        ):
            return {**result, "mute_count": count}
        policy = adapter.config.get("moderation", {})
        if (
            not policy.get("automation_enabled")
            or policy.get("manual_kick_only")
            or policy.get("auto_kick", {}).get(group) is not True
        ):
            return {**result, "mute_count": count, "reason": "escalation_disabled"}
        authorize_action(adapter.config, group, "kick")
        trigger = mutes[MUTE_LIMIT - 1][0]
        key = (
            "automatic-kick/"
            + hashlib.sha256(
                json.dumps([adapter.account, group, member, trigger]).encode()
            ).hexdigest()
        )
        prior_kick = db.execute(
            "SELECT status FROM automatic_kicks WHERE operation=?", (key,)
        ).fetchone()
        if prior_kick:
            if (
                prior_kick[0] == "unknown"
                and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
                ).fetchone()
            ):
                receipt = db.execute(
                    "SELECT result FROM operations WHERE id=?", (key,)
                ).fetchone()
                saved = json.loads(receipt[0]) if receipt else {}
                if (
                    saved.get("action") == "kick"
                    and str(saved.get("group")) == group
                    and str(saved.get("member")) == member
                    and saved.get("status") in {"accepted", "verified"}
                ):
                    prior_kick = (saved["status"],)
                    db.execute(
                        "UPDATE automatic_kicks SET status=? WHERE operation=?",
                        (saved["status"], key),
                    )
                    if saved["status"] == "verified":
                        db.execute(
                            "UPDATE automatic_mutes SET closed=1 WHERE account=? AND group_id=? AND member=? AND peer=?",
                            (adapter.account, group, member, peer),
                        )
            return {
                "status": prior_kick[0],
                "action": "kick",
                "operation": key,
                "mute_count": count,
            }
        db.execute(
            "INSERT INTO automatic_kicks VALUES(?,?,?,?,?,?,'unknown',?)",
            (key, adapter.account, group, member, peer, trigger, time.time()),
        )
        db.execute("INSERT INTO automatic_kick_events VALUES(?,?)", (key, message))
    try:
        result = await adapter.execute_moderation(key, "kick", int(group), int(member))
    except Exception:
        adapter.diagnostics.emit(
            "automatic_kick", "failed_or_unknown", message, failed=True
        )
        return {
            "status": "unknown",
            "action": "kick",
            "operation": key,
            "mute_count": count,
        }
    status = result.get("status", "unknown")
    if status not in {"accepted", "verified", "rejected"}:
        status = "unknown"
    with closing(database(adapter)) as db, db:
        db.execute(
            "UPDATE automatic_kicks SET status=? WHERE operation=?", (status, key)
        )
        if status == "verified":
            db.execute(
                "UPDATE automatic_mutes SET closed=1 WHERE account=? AND group_id=? AND member=? AND peer=?",
                (adapter.account, group, member, peer),
            )
    return {
        **result,
        "status": status,
        "action": "kick",
        "operation": key,
        "mute_count": count,
    }
