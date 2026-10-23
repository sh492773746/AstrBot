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


async def enforce(adapter, group, member, message, *, execution_check=None):
    """Attempt both actions once; never treat a partial outcome as full success."""
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
        operation = (
            "progressive/"
            + hashlib.sha256(
                json.dumps([account, group, member, message]).encode()
            ).hexdigest()
        )
        warnings, previous = await asyncio.to_thread(
            store.reserve, operation, account, group, member, message
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
        try:
            # Database contention can outlive a grant or account change.
            await check()
            try:
                recall_status = "unknown"
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
                recall_status = receipt["status"]
                if recall_status not in {"accepted", "verified", "rejected"}:
                    recall_status = "unknown"
            except Exception:
                adapter.diagnostics.emit(
                    "progressive_recall", "failed_or_unknown", message, failed=True
                )
            # Recall failure must not silently skip muting; revoked authority must.
            await check()
            if warnings < 3:
                await asyncio.to_thread(
                    store.warning, account, group, member, message, recall_status
                )
                result = {
                    "status": recall_status,
                    "action": "recall",
                    "warning_number": warnings + 1,
                }
                return {**result, "recall_status": recall_status}
            result = await mute_and_escalate(
                adapter, operation, group, member, message, execution_check=check
            )
            return {**result, "recall_status": recall_status}
        finally:
            await asyncio.to_thread(store.finish, operation, recall_status, result)
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
