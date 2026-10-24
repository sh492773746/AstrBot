"""Account-scoped action evidence without changing native execution authority."""

import asyncio
import json
import sqlite3

from astrbot.core.platform.sources.wangshangliao.database import AsyncDatabase
from astrbot.core.platform.sources.wangshangliao.diagnostics import ERROR_LABELS
from astrbot.core.platform.sources.wangshangliao.group_scope import (
    GroupAdapter,
    group_config,
)
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir

from .review_store import review_store

ACTION_ERRORS = ERROR_LABELS | {
    "action_scope_changed",
    "action_automation_changed",
    "recall_scope_changed",
    "recall_identity",
    "moderation_target",
    "content_policy_changed",
    "not_online",
    "progressive_not_authorized",
}


class ModerationActions(GroupAdapter):
    """Retain preflight failures separately from native submission receipts."""

    def __init__(self, adapter, group):
        object.__setattr__(self, "_scoped", isinstance(adapter, GroupAdapter))
        while isinstance(adapter, GroupAdapter):
            adapter = adapter.adapter
        super().__init__(adapter, group)

    @property
    def config(self):
        """Keep one native identity while preserving the caller's policy scope."""
        return (
            group_config(self.adapter.config, self.group)
            if self._scoped
            else self.adapter.config
        )

    async def execute_action(self, request, *, authorize=None):
        """Delegate unchanged authority and retain only bounded action metadata.

        Args:
            request: Existing trusted, immutable native action request.
            authorize: Original business authorization callback.

        Returns:
            The native action result, without manufacturing a success receipt.

        Raises:
            BaseException: The original failure, annotated with audited status.
        """
        account, group = self.account, str(request.group)
        path = getattr(getattr(self, "_review_store", None), "path", None)
        if path is None:
            path = instance_dir(self.config["id"]) / "moderation.sqlite3"
        store = review_store(self, path)
        result, failure = {}, None
        error = ""
        try:
            result = await self.adapter.execute_action(request, authorize=authorize)
            return result
        except BaseException as exc:
            failure = exc
            raw = str(exc)
            error = (
                raw
                if raw in ACTION_ERRORS
                else "transport"
                if isinstance(exc, TimeoutError)
                else "operation_failed"
            )
            # A read request's submission flag does not describe the mutation.
            # Only the owning transport ledger can prove an action was reserved.
            try:
                if request.action in {"recall", "warning"}:
                    ledger = getattr(self, "ledger", None)
                    if ledger is None or ledger.db is None:
                        result = {"status": "unknown"}
                    else:
                        receipt = await ledger.receipt(request.operation)
                        result = receipt or {"status": "rejected", "submitted": False}
                elif path.is_file():
                    async with AsyncDatabase(
                        lambda: sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
                    ) as db:
                        exists = await db.fetchone(
                            "SELECT 1 FROM sqlite_master WHERE name='operations'"
                        )
                        row = (
                            await db.fetchone(
                                "SELECT result FROM operations WHERE id=?",
                                (request.operation,),
                            )
                            if exists
                            else None
                        )
                    result = (
                        json.loads(row[0])
                        if row
                        else {"status": "rejected", "submitted": False}
                    )
                else:
                    result = {"status": "rejected", "submitted": False}
            except Exception:
                result = {"status": "unknown"}
            exc.action_status = (
                result.get("status")
                if result.get("status") in {"accepted", "verified", "rejected"}
                else "unknown"
            )
            exc.action_error = error
            raise
        finally:
            status = result.get("status", "unknown")
            if status not in {"accepted", "verified", "rejected"}:
                status = "unknown"
            submitted = result.get("submitted")
            if type(submitted) is not bool:
                submitted = True if status in {"accepted", "verified"} else None
            if not error and status not in {"accepted", "verified"}:
                raw = result.get("error", "")
                error = (
                    raw
                    if isinstance(raw, str) and raw in ERROR_LABELS
                    else "result_unknown"
                )
            number = (
                getattr(failure, "number", None)
                if failure is not None
                else result.get("provider_code")
            )
            if type(number) is not int or not -(1 << 31) <= number < 1 << 31:
                number = None
            try:
                await asyncio.to_thread(
                    store.save_action,
                    request.operation,
                    account,
                    group,
                    str(request.member),
                    request.message,
                    request.action,
                    status,
                    submitted,
                    error,
                    number,
                )
            except Exception:
                self.diagnostics.emit(
                    "action_audit",
                    "failed",
                    request.operation,
                    failed=True,
                    error="operation_failed",
                    group=group,
                )
            if status not in {"accepted", "verified"}:
                self.diagnostics.emit(
                    "moderation_action",
                    status,
                    request.operation,
                    failed=True,
                    error=error if error in ERROR_LABELS else "validation_failed",
                    action=request.action,
                    group=group,
                    actor=str(request.member),
                    provider_code=number,
                    aggregate=False,
                )
