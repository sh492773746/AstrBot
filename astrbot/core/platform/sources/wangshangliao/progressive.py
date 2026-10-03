"""One verified violation produces separate recall and progressive mute receipts."""

import asyncio
import hashlib
import json
import time
from contextlib import closing

from .automatic import database, mute_and_escalate
from .event import is_managed_account
from .policy import authorize_action
from .wire import ProtocolError


def authorize(adapter, group, member, peer):
    """Recheck both capabilities and the frozen identity before every action."""
    policy = adapter.config.get("moderation", {})
    authorize_action(adapter.config, group, "recall")
    authorize_action(adapter.config, group, "mute")
    if (
        not policy.get("progressive_mute")
        or not policy.get("automation_enabled")
        or not policy.get("recall_enabled")
        or member == adapter.account
        or is_managed_account(member)
        or not peer
        or adapter.members.get(group, {}).get(member) != peer
    ):
        raise ProtocolError("progressive_not_authorized")


async def enforce(adapter, group, member, message):
    """Attempt both actions once; never treat a partial outcome as full success."""
    account = adapter.account
    peer = adapter.members.get(group, {}).get(member)
    authorize(adapter, group, member, peer)
    locks = getattr(adapter, "progressive_locks", None)
    if locks is None:
        locks = adapter.progressive_locks = {}
    # Bound the lock set instead of retaining every member forever.
    slot = (
        int(hashlib.sha256(f"{account}/{group}/{member}".encode()).hexdigest(), 16) % 64
    )
    async with locks.setdefault(slot, asyncio.Lock()):
        authorize(adapter, group, member, peer)
        if adapter.account != account:
            raise ProtocolError("progressive_account_changed")
        operation = (
            "progressive/"
            + hashlib.sha256(
                json.dumps([account, group, member, message]).encode()
            ).hexdigest()
        )
        with closing(database(adapter)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS progressive_actions("
                "operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,member TEXT,"
                "message TEXT,recall_status TEXT,result TEXT,created REAL)"
            )
            previous = db.execute(
                "SELECT result FROM progressive_actions WHERE operation=?", (operation,)
            ).fetchone()
            if previous:
                adapter.diagnostics.emit(
                    "progressive", "duplicate_ignored", message, group=group
                )
                return json.loads(previous[0])
            db.execute(
                "INSERT INTO progressive_actions VALUES(?,?,?,?,?,'unknown',?,?)",
                (
                    operation,
                    account,
                    group,
                    member,
                    message,
                    json.dumps({"status": "unknown", "recall_status": "unknown"}),
                    time.time(),
                ),
            )
        recall_status = "unknown"
        result = {"status": "unknown", "action": "mute"}
        try:
            try:
                recall_status = await adapter.recall_violation(group, message, member)
                if recall_status not in {"accepted", "verified", "rejected"}:
                    recall_status = "unknown"
            except Exception:
                adapter.diagnostics.emit(
                    "progressive_recall", "failed_or_unknown", message, failed=True
                )
            # Recall failure must not silently skip muting; revoked authority must.
            authorize(adapter, group, member, peer)
            if adapter.account != account:
                raise ProtocolError("progressive_account_changed")
            result = await mute_and_escalate(adapter, operation, group, member, message)
            return {**result, "recall_status": recall_status}
        finally:
            with closing(database(adapter)) as db, db:
                db.execute(
                    "UPDATE progressive_actions SET recall_status=?,result=? WHERE operation=?",
                    (
                        recall_status,
                        json.dumps({**result, "recall_status": recall_status}),
                        operation,
                    ),
                )
            for action, status in (
                ("recall", recall_status),
                (result.get("action", "mute"), result.get("status", "unknown")),
            ):
                adapter.diagnostics.emit(
                    "progressive_result",
                    status,
                    message,
                    action=action,
                    group=group,
                    actor=member,
                    failed=status not in {"accepted", "verified"},
                    error="result_unknown" if status == "unknown" else "",
                    aggregate=False,
                )
