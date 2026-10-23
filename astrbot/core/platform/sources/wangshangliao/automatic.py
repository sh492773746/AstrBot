"""Durable automatic mute escalation with server-owned action evidence."""

import inspect
import json
import sqlite3
import time

from .actions import ActionRequest
from .database import AsyncDatabase, account_guard
from .event import is_managed_account
from .policy import authorize_action
from .storage import instance_dir
from .test_rules import advertising_path, rule_target
from .wire import ProtocolError

MUTE_MINUTES = (5, 15, 60)


def database(adapter, *, path=None):
    """Open the automatic sanction ledger for one platform instance.

    Args:
        adapter: Native platform adapter.
        path: Internally frozen fixture ledger, otherwise the existing live ledger.

    Returns:
        SQLite connection with automatic sanction tables initialized.
    """
    path = path or instance_dir(adapter.config["id"]) / "moderation.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
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
    """Reject retired automatic kick requests, including persisted legacy intents.

    Args:
        adapter: Current native platform adapter.
        operation: Exact persisted automatic kick operation.
        group: Target business group ID.
        member: Target business member ID.

    Raises:
        ProtocolError: Always; removal requires the separate manual confirmation path.
    """
    raise ProtocolError("automatic_kick_not_authorized")


async def mute_and_escalate(
    adapter,
    operation: str,
    group: str,
    member: str,
    message: str,
    *,
    execution_check=None,
) -> dict:
    """Apply progressive mutes capped at sixty minutes, never automatic removal.

    Args:
        adapter: Authorized native platform adapter.
        operation: Stable automatic mute operation ID.
        group: Verified business group ID.
        member: Verified ordinary member business ID.
        message: Triggering upstream message ID.
        execution_check: Original rule authority, repeated through submission.

    Returns:
        Mutation outcome and accepted mute count. Unknown outcomes are quarantined.
    """
    peer = adapter.members.get(group, {}).get(member, "")
    if (
        not peer
        or member == adapter.account
        or is_managed_account(member)
        and not rule_target(adapter, group, member, message)
    ):
        raise ProtocolError("moderation_identity")
    progressive = adapter.config.get("moderation", {}).get("progressive_mute", False)
    test_path = advertising_path(adapter, group, member, message)
    native_path = instance_dir(adapter.config["id"]) / "moderation.sqlite3"

    def authorize():
        authorize_action(adapter.config, group, "mute")
        policy = adapter.config.get("moderation", {})
        if (
            not policy.get("automation_enabled")
            or policy.get("progressive_mute", False) != progressive
            or adapter.members.get(group, {}).get(member) != peer
            or is_managed_account(member)
            and not rule_target(adapter, group, member, message)
            or advertising_path(adapter, group, member, message) != test_path
        ):
            raise ProtocolError("automatic_mute_not_authorized")
        if progressive:
            from .progressive import authorize as authorize_progressive

            authorize_progressive(adapter, group, member, peer, message)

    guard = account_guard(adapter, authorize)

    async def check():
        guard()
        if execution_check is not None:
            checked = execution_check()
            if inspect.isawaitable(checked):
                await checked
        guard()

    await check()
    account = adapter.account
    async with AsyncDatabase(
        lambda: database(adapter, path=test_path), write=True, guard=guard
    ) as db:
        # Reconcile interrupted bookkeeping only from existing transport receipts.
        if await db.fetchone(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
        ):
            pending = await db.fetchall(
                "SELECT m.operation,o.result FROM automatic_mutes m JOIN operations o ON o.id=m.operation "
                "WHERE m.account=? AND m.group_id=? AND m.member=? AND m.closed=0 AND m.status='unknown'",
                (account, group, member),
            )
            for pending_id, encoded in pending:
                receipt = json.loads(encoded)
                if (
                    receipt.get("action") == "mute"
                    and str(receipt.get("group")) == group
                    and str(receipt.get("member")) == member
                    and receipt.get("status") in {"accepted", "verified"}
                ):
                    await db.execute(
                        "UPDATE automatic_mutes SET status=? WHERE operation=?",
                        (receipt["status"], pending_id),
                    )
        if await db.fetchone(
            "SELECT 1 FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
            "AND closed=0 AND status='unknown'",
            (account, group, member),
        ):
            return {
                "status": "unknown",
                "action": "mute",
                "reason": "prior_mute_unknown",
            }
        mutes = await db.fetchall(
            "SELECT operation FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
            "AND peer=? AND closed=0 AND status IN ('accepted','verified') ORDER BY created,operation",
            (account, group, member, peer),
        )
        prior = await db.fetchone(
            "SELECT status FROM automatic_mutes WHERE operation=? OR "
            "(account=? AND group_id=? AND member=? AND message=?)",
            (operation, account, group, member, message),
        )
        should_mute = not prior
        minutes = MUTE_MINUTES[min(len(mutes), len(MUTE_MINUTES) - 1)]
        if should_mute:
            await db.execute(
                "INSERT INTO automatic_mutes VALUES(?,?,?,?,?,?,'unknown',?,0)",
                (operation, account, group, member, peer, message, time.time()),
            )
    result = {"status": prior[0] if prior else "accepted", "action": "mute"}
    if should_mute:
        failure = None
        try:
            await check()
            result = await adapter.execute_action(
                ActionRequest.bind(
                    adapter,
                    operation,
                    "mute",
                    int(group),
                    member=int(member),
                    minutes=minutes if progressive else 1,
                ),
                authorize=check,
            )
        except BaseException as exc:
            # The native contract reserves operations before any possible write.
            # No reservation, or an explicit no-submission receipt, is retry-safe
            # for future messages; existing ambiguous operations remain quarantined.
            status = "unknown"
            # Native intents always stay in the original execution ledger,
            # including when test-only progression counts use separate storage.
            async with AsyncDatabase(lambda: database(adapter, path=native_path)) as db:
                receipt = None
                if await db.fetchone(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
                ):
                    receipt = await db.fetchone(
                        "SELECT result FROM operations WHERE id=?", (operation,)
                    )
                if receipt is None:
                    status = "rejected"
                else:
                    evidence = json.loads(receipt[0])
                    if (
                        evidence.get("action") == "mute"
                        and str(evidence.get("group")) == group
                        and str(evidence.get("member")) == member
                        and evidence.get("status") in {"accepted", "verified"}
                    ):
                        status = evidence["status"]
                    elif (
                        evidence.get("status") == "rejected"
                        and evidence.get("submitted") is False
                    ):
                        status = "rejected"
            adapter.diagnostics.emit("automatic_mute", status, message, failed=True)
            result = {"status": status, "action": "mute"}
            if status == "rejected":
                result["submitted"] = False
            if not isinstance(exc, Exception):
                failure = exc
        status = result.get("status", "unknown")
        if status not in {"accepted", "verified", "rejected"}:
            status = "unknown"
        # Persist receipts even after revocation; this is not another action.
        async with AsyncDatabase(
            lambda: database(adapter, path=test_path), write=True
        ) as db:
            await db.execute(
                "UPDATE automatic_mutes SET status=? WHERE operation=?",
                (status, operation),
            )
        result = {**result, "status": status, "action": "mute"}
        if failure is not None:
            raise failure
    async with AsyncDatabase(lambda: database(adapter, path=test_path)) as db:
        mutes = await db.fetchall(
            "SELECT operation FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
            "AND peer=? AND closed=0 AND status IN ('accepted','verified') ORDER BY created,operation",
            (account, group, member, peer),
        )
        count = len(mutes)
        return {
            **result,
            "mute_count": count,
            "reason": "automatic_kick_disabled",
        }
