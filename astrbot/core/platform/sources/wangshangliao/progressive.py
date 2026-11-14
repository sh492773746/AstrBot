"""One verified violation produces separate recall and progressive mute receipts."""

import asyncio
import hashlib
import inspect
import json

from . import automatic
from .actions import ActionRequest
from .automatic import mute_and_escalate
from .event import is_managed_account
from .policy import authorize_action
from .progression_store import ProgressionStore
from .test_rules import advertising_path, rule_target
from .wire import ProtocolError


def authorize(adapter, group, member, peer, message=""):
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
        and not rule_target(adapter, group, member, message)
        or not peer
        or adapter.members.get(group, {}).get(member) != peer
    ):
        raise ProtocolError("progressive_not_authorized")


async def enforce(
    adapter,
    group,
    member,
    message,
    *,
    execution_check=None,
    operation=None,
    recall_after_mute=False,
    recall_queue=None,
    recall_complete=None,
):
    """Submit an earned mute before recalls while keeping independent receipts.

    Args:
        adapter: Original authenticated platform adapter.
        group: Exact business group identity.
        member: Verified ordinary member identity.
        message: Original upstream message identity.
        execution_check: Caller-owned authorization repeated before effects.
        operation: Optional stable burst operation identity.
        recall_after_mute: Leave recalls to the burst caller after a mute.
        recall_queue: Optional bounded cleanup queue after an earned mute.
        recall_complete: Async callback receiving the recall status and action result.

    Returns:
        Independent mute and recall results, or a recall-only warning result.
    """
    account = adapter.account
    test_path = advertising_path(adapter, group, member, message)
    path = (
        test_path or automatic.instance_dir(adapter.config["id"]) / "moderation.sqlite3"
    )
    store = getattr(adapter, "_progression_store", None)
    if store is None or store.path != path:
        store = adapter._progression_store = ProgressionStore(path)
    peer = adapter.members.get(group, {}).get(member)

    async def check():
        authorize(adapter, group, member, peer, message)
        if advertising_path(adapter, group, member, message) != test_path:
            raise ProtocolError("test_advertising_scope")
        if adapter.account != account:
            raise ProtocolError("progressive_account_changed")
        if execution_check is not None:
            checked = execution_check()
            if inspect.isawaitable(checked):
                await checked
        authorize(adapter, group, member, peer, message)
        if advertising_path(adapter, group, member, message) != test_path:
            raise ProtocolError("test_advertising_scope")
        if adapter.account != account:
            raise ProtocolError("progressive_account_changed")

    await check()
    locks = getattr(adapter, "progressive_locks", None)
    if locks is None:
        locks = adapter.progressive_locks = {}
    # Bound the lock set instead of retaining every member forever.
    slot = (
        int(hashlib.sha256(f"{account}/{group}/{member}".encode()).hexdigest(), 16) % 64
    )
    async with locks.setdefault(slot, asyncio.Lock()):
        await check()
        operation = operation or (
            "progressive/"
            + hashlib.sha256(
                json.dumps([account, group, member, message]).encode()
            ).hexdigest()
        )
        warnings, previous = await asyncio.to_thread(
            store.reserve, operation, account, group, member, message, peer
        )
        if previous is not None:
            adapter.diagnostics.emit(
                "progressive", "duplicate_ignored", message, group=group
            )
            return previous
        recall_status = "rejected"
        result = {
            "status": "rejected",
            "submitted": False,
            "action": "recall" if warnings < 3 else "mute",
        }
        deferred = False

        async def recall():
            """Submit one independently authorized recall, never retrying unknowns."""
            status = "rejected"
            try:
                await check()
                status = "unknown"
                receipt = await adapter.execute_action(
                    ActionRequest.bind(
                        adapter,
                        f"recall/{account}/{group}/{message}",
                        "recall",
                        int(group),
                        member=int(member),
                        expected_nim=peer,
                        message=message,
                    ),
                    authorize=check,
                )
                status = receipt["status"]
                if status not in {"accepted", "verified", "rejected"}:
                    status = "unknown"
            except Exception as exc:
                status = getattr(exc, "action_status", status)
                adapter.diagnostics.emit(
                    "progressive_recall", "failed_or_unknown", message, failed=True
                )
            return status

        try:
            # Database contention can outlive a grant or account change.
            await check()
            if warnings >= 3:
                recall_status = "none"
                result = await mute_and_escalate(
                    adapter, operation, group, member, message, execution_check=check
                )
                if recall_after_mute:
                    return {**result, "recall_status": recall_status}
            if recall_queue is not None and (warnings >= 3 or warnings < 0):
                if warnings < 0:
                    result = {
                        "status": "pending",
                        "action": "recall",
                        "cycle_wait": True,
                    }
                ready = asyncio.Event()
                receipt_result = dict(result)

                async def finish_recall(status):
                    """Save cleanup separately from the original mute receipt."""
                    completed = {
                        **receipt_result,
                        **(
                            {"status": status}
                            if receipt_result.get("action") == "recall"
                            else {}
                        ),
                    }
                    await asyncio.to_thread(
                        store.finish, operation, status, completed, peer
                    )
                    if recall_complete is not None:
                        await recall_complete(status, completed)
                    adapter.diagnostics.emit(
                        "progressive_result",
                        status,
                        message,
                        action="recall",
                        group=group,
                        actor=member,
                        failed=status not in {"accepted", "verified"},
                        error="result_unknown" if status == "unknown" else "",
                        aggregate=False,
                    )

                async def cleanup():
                    """Wait for the initial audit before refining its receipt."""
                    status = "rejected"
                    try:
                        await ready.wait()
                        status = "unknown"
                        status = await recall()
                    finally:
                        await ready.wait()
                        await finish_recall(status)

                async def cancel_cleanup():
                    """Close never-started cleanup without submitting a recall."""
                    await ready.wait()
                    await finish_recall("rejected")

                # Persist before handing off; no cancellable work may sit
                # between queue admission and returning the audit barrier.
                await asyncio.to_thread(
                    store.finish, operation, "pending", result, peer
                )
                if recall_queue.defer_recalls(
                    adapter, 1, cleanup, cancel=cancel_cleanup
                ):
                    recall_status = "pending"
                    deferred = True
                    return {
                        **result,
                        "recall_status": recall_status,
                        "_recall_ready": ready,
                    }
            # Recalls and notifications cannot delay an already earned mute.
            recall_status = await recall()
            await check()
            if warnings < 3:
                if warnings >= 0:
                    await asyncio.to_thread(
                        store.warning,
                        account,
                        group,
                        member,
                        message,
                        recall_status,
                        peer,
                    )
                result = {
                    "status": recall_status,
                    "action": "recall",
                    **(
                        {"warning_number": warnings + 1}
                        if warnings >= 0
                        else {"cycle_wait": True}
                    ),
                }
                return {**result, "recall_status": recall_status}
            return {**result, "recall_status": recall_status}
        finally:
            if not deferred:
                await asyncio.to_thread(
                    store.finish, operation, recall_status, result, peer
                )
            outcomes = (
                (
                    ("mute", result.get("status", "unknown")),
                    ("recall", recall_status),
                )
                if warnings >= 3
                else (("recall", recall_status),)
            )
            for action, status in outcomes:
                adapter.diagnostics.emit(
                    "progressive_result",
                    status,
                    message,
                    action=action,
                    group=group,
                    actor=member,
                    failed=status not in {"accepted", "verified", "none", "pending"},
                    error="result_unknown" if status == "unknown" else "",
                    aggregate=False,
                )
