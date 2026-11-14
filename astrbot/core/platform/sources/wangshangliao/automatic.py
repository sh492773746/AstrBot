"""Durable automatic mute escalation with server-owned action evidence."""

import asyncio
import inspect
import json
import math
import sqlite3
import time

from .actions import ActionRequest
from .business import BusinessClient
from .database import AsyncDatabase, account_guard
from .diagnostics import failure_evidence
from .directory import muted_member_ids
from .event import is_managed_account
from .policy import authorize_action
from .storage import instance_dir
from .test_rules import advertising_path, rule_target
from .wire import ProtocolError

MUTE_MINUTES = (1, 3, 10)


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
        CREATE INDEX IF NOT EXISTS review_ref_automatic_mutes
            ON automatic_mutes(account,group_id,message);
        CREATE TABLE IF NOT EXISTS automatic_kicks(
            operation TEXT PRIMARY KEY, account TEXT NOT NULL, group_id TEXT NOT NULL,
            member TEXT NOT NULL, peer TEXT NOT NULL, trigger_operation TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS automatic_kick_events(
            operation TEXT PRIMARY KEY, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automatic_mute_durations(
            operation TEXT PRIMARY KEY,minutes INTEGER NOT NULL,accepted_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS automatic_mute_resolutions(
            operation TEXT PRIMARY KEY,evidence TEXT NOT NULL,resolved_at REAL NOT NULL);
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
    """Apply one, three and ten minute mutes, never automatic removal.

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
    native_identity = None

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
        if native_identity is not None and (
            adapter.nim is not native_identity[0]
            or adapter.business is not native_identity[1]
            or str(adapter.nim_account) != native_identity[2]
            or (
                adapter.groups.get(group)
                or getattr(adapter, "group_catalog", {}).get(group, {}).get("team")
            )
            != native_identity[3]
        ):
            raise ProtocolError("automatic_native_identity_changed")
        if progressive:
            from .progressive import authorize as authorize_progressive

            authorize_progressive(adapter, group, member, peer, message)

    guard = account_guard(adapter, authorize)
    protect_native_mute = False
    native_checked = False

    async def check():
        nonlocal native_checked, native_identity
        guard()
        if execution_check is not None:
            checked = execution_check()
            if inspect.isawaitable(checked):
                await checked
        guard()
        if protect_native_mute:
            if native_identity is None:
                native_identity = (
                    adapter.nim,
                    adapter.business,
                    str(adapter.nim_account),
                    adapter.groups.get(group)
                    or getattr(adapter, "group_catalog", {}).get(group, {}).get("team"),
                )
            if (
                native_checked
                and isinstance(getattr(adapter, "business", None), BusinessClient)
                and getattr(adapter.business, "_authorization_task", None)
                is not asyncio.current_task()
            ):
                # Intermediate fences stay local; the final HTTP fence always
                # rereads native state under the client's reentrant lock.
                return
            if member in await muted_member_ids(
                adapter, group, authorize=guard, expected_member=(member, peer)
            ):
                raise ProtocolError("automatic_existing_native_mute")
            if execution_check is not None:
                checked = execution_check()
                if inspect.isawaitable(checked):
                    await checked
            guard()
            native_checked = True

    await check()
    account = adapter.account
    held = {
        "status": "unknown",
        "action": "mute",
        "reason": "prior_mute_unknown",
    }
    observed = None
    # Two passes: inspect, then atomically quarantine the unchanged evidence and
    # reserve a fresh violation. Never hold the SQLite writer during native reads.
    for _ in range(2):
        async with AsyncDatabase(
            lambda: database(adapter, path=test_path), write=True, guard=guard
        ) as db:
            has_receipts = await db.fetchone(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
            )
            # Reconcile interrupted bookkeeping only from transport receipts.
            if has_receipts:
                pending = await db.fetchall(
                    "SELECT m.operation,o.result FROM automatic_mutes m JOIN operations o ON o.id=m.operation "
                    "WHERE m.account=? AND m.group_id=? AND m.member=? AND m.closed=0 AND m.status='unknown'",
                    (account, group, member),
                )
                for pending_id, encoded in pending:
                    try:
                        receipt = json.loads(encoded)
                    except (TypeError, ValueError):
                        continue
                    if (
                        isinstance(receipt, dict)
                        and receipt.get("action") == "mute"
                        and str(receipt.get("group")) == group
                        and str(receipt.get("member")) == member
                        and receipt.get("status") in {"accepted", "verified"}
                    ):
                        await db.execute(
                            "UPDATE automatic_mutes SET status=? WHERE operation=?",
                            (receipt["status"], pending_id),
                        )
            duplicates = await db.fetchall(
                "SELECT status,closed,account,group_id,member,peer FROM automatic_mutes "
                "WHERE operation=? OR (account=? AND group_id=? AND member=? AND message=?) "
                "ORDER BY operation<>?,operation",
                (operation, account, group, member, message, operation),
            )
            if any(row[2:] != (account, group, member, peer) for row in duplicates):
                raise ProtocolError("automatic_mute_operation_conflict")
            prior = duplicates[0] if duplicates else None
            if prior and prior[:2] == ("unknown", 0):
                return held
            receipt_column = "o.result" if has_receipts else "NULL"
            receipt_join = (
                "LEFT JOIN operations o ON o.id=m.operation" if has_receipts else ""
            )
            # Reuse a bound result before reconciling unrelated native unknowns.
            pending = (
                []
                if prior
                else await db.fetchall(
                    "SELECT m.operation,m.peer,m.message,m.created,d.minutes,"
                    f"{receipt_column} FROM automatic_mutes m "
                    "LEFT JOIN automatic_mute_durations d ON d.operation=m.operation "
                    f"{receipt_join} WHERE m.account=? AND m.group_id=? AND m.member=? "
                    "AND m.closed=0 AND m.status='unknown' ORDER BY m.operation LIMIT 17",
                    (account, group, member),
                )
            )
            if observed is not None and not prior:
                if pending != observed[0]:
                    return held
                await check()
                for pending_id, _, _, _, _, encoded in pending:
                    await db.execute(
                        "INSERT INTO automatic_mute_resolutions VALUES(?,?,?)",
                        (
                            pending_id,
                            json.dumps(
                                {
                                    "reason": "expired_unknown_native_unmuted",
                                    "account": account,
                                    "group": group,
                                    "member": member,
                                    "peer": peer,
                                    "source": "native_mute_directory",
                                    "muted": False,
                                    "checked_at": observed[1],
                                    "receipt": json.loads(encoded),
                                }
                            ),
                            time.time(),
                        ),
                    )
                    # closed=2 is quarantined, not accepted, rejected or retryable.
                    await db.execute(
                        "UPDATE automatic_mutes SET closed=2 WHERE operation=?",
                        (pending_id,),
                    )
                pending = []
            if not pending:
                protect_native_mute = bool(
                    await db.fetchone(
                        "SELECT 1 FROM automatic_mutes WHERE account=? AND group_id=? "
                        "AND member=? AND peer=? AND closed=2 AND status='unknown'",
                        (account, group, member, peer),
                    )
                )
                if has_receipts and not protect_native_mute:
                    # Buffered violations must not immediately escalate an active mute.
                    # Use durable native acceptance, not an attempted/unknown sanction.
                    protect_native_mute = bool(
                        await db.fetchone(
                            "SELECT 1 FROM automatic_mutes m "
                            "JOIN automatic_mute_durations d ON d.operation=m.operation "
                            "JOIN operations o ON o.id=m.operation "
                            "WHERE m.account=? AND m.group_id=? AND m.member=? AND m.peer=? "
                            "AND m.closed=0 AND m.status IN ('accepted','verified') "
                            "AND d.minutes IN (1,3,10) AND d.accepted_at>0 "
                            "AND d.accepted_at+d.minutes*60>? "
                            "AND json_valid(o.result) "
                            "AND json_extract(o.result,'$.action')='mute' "
                            "AND json_extract(o.result,'$.status') IN ('accepted','verified') "
                            "AND json_extract(o.result,'$.submitted')=1 LIMIT 1",
                            (account, group, member, peer, time.time()),
                        )
                    )
                mutes = await db.fetchall(
                    "SELECT operation FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
                    "AND peer=? AND closed=0 AND status IN ('accepted','verified') ORDER BY created,operation",
                    (account, group, member, peer),
                )
                should_mute = not prior
                minutes = MUTE_MINUTES[min(len(mutes), len(MUTE_MINUTES) - 1)]
                if should_mute:
                    await db.execute(
                        "INSERT INTO automatic_mutes VALUES(?,?,?,?,?,?,'unknown',?,0)",
                        (operation, account, group, member, peer, message, time.time()),
                    )
                    await db.execute(
                        "INSERT INTO automatic_mute_durations(operation,minutes) VALUES(?,?)",
                        (operation, minutes if progressive else 1),
                    )
        if not pending:
            break
        # A separate fixture ledger cannot prove completion of a native intent.
        if test_path is not None or len(pending) > 16:
            return held
        now = time.time()
        for pending_id, old_peer, old_message, created, duration, encoded in pending:
            if (
                pending_id == operation
                or old_message == message
                or old_peer != peer
                or type(duration) is not int
                or duration not in MUTE_MINUTES
                or not isinstance(created, (int, float))
                or not math.isfinite(created)
                or created <= 0
                or created + duration * 60 > now
            ):
                return held
            try:
                receipt = json.loads(encoded)
            except (TypeError, ValueError):
                return held
            if (
                not isinstance(receipt, dict)
                or receipt.get("operation") != pending_id
                or receipt.get("action") != "mute"
                or str(receipt.get("group")) != group
                or str(receipt.get("member")) != member
                or receipt.get("account", account) != account
                or receipt.get("instance", adapter.config["id"]) != adapter.config["id"]
                or type(receipt.get("minutes")) is not int
                or receipt.get("minutes") != duration
                or receipt.get("expected_nim", peer) != peer
                or receipt.get("status") != "unknown"
                # The pre-submission reservation has no submitted field. Only
                # a finished unknown receipt is eligible; time is not completion.
                or receipt.get("submitted") is not True
            ):
                return held
        try:
            await check()
            native_identity = (
                adapter.nim,
                adapter.business,
                str(adapter.nim_account),
                adapter.groups.get(group)
                or getattr(adapter, "group_catalog", {}).get(group, {}).get("team"),
            )
            muted = await muted_member_ids(
                adapter, group, authorize=guard, expected_member=(member, peer)
            )
            checked_at = time.time()
            await check()
        except Exception:
            guard()
            return held
        if member in muted:
            return held
        native_checked = True
        observed = (pending, checked_at)
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
            result = {
                "status": status,
                "action": "mute",
                **failure_evidence(exc),
            }
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
            if status in {"accepted", "verified"}:
                await db.execute(
                    "UPDATE automatic_mute_durations SET accepted_at=? WHERE operation=?",
                    (time.time(), operation),
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
            "minutes": minutes if progressive else 1,
            "mute_count": count,
            "reason": "automatic_kick_disabled",
        }
