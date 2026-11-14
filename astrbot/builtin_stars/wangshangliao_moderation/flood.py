"""Bounded ingress-time rule workers with durable native inbox evidence."""

import asyncio
import hashlib
import json
import math
import sqlite3
import time
import unicodedata
from collections import OrderedDict, deque


class FloodWindows:
    """Count trusted, newly received messages without blocking the event loop."""

    def __init__(self, *, max_members=4096, max_messages=256):
        self.windows = OrderedDict()
        self.max_members = max_members
        self.max_messages = max_messages

    def observe(
        self, account, group, message, payload, received_at, *, revision="", scope=""
    ):
        """Attach a decision to a new envelope, never to a replayed duplicate.

        Args:
            account: Real account whose inbox owns this message.
            group: Business group ID.
            message: Stable upstream message ID.
            payload: Trusted adapter envelope, not a user-supplied dictionary.
            received_at: Local receive time, fixed before queueing.
            revision: Policy revision, preventing cross-policy window accumulation.
            scope: Optional volatile test generation, never shared across windows.

        Returns:
            Evidence containing a fixed decision and bounded counters.
        """
        try:
            stamp = float(payload.get("recall_route", {}).get("time") or 0)
            if stamp > 100000000000:
                stamp /= 1000
        except (ValueError, TypeError, OverflowError):
            stamp = 0
        sender = str(payload.get("sender", ""))
        # Delayed/reconnect history must never inflate a live burst window.
        if (
            not sender
            or group.startswith("private/")
            or not math.isfinite(stamp)
            or not received_at - 10 <= stamp <= received_at + 2
        ):
            return {"category": "", "received_at": received_at, "live": False}
        key = (account, group, sender, revision, scope)
        while self.windows:
            oldest = next(iter(self.windows.values()))
            if oldest and oldest[-1][0] >= received_at - 30:
                break
            self.windows.popitem(last=False)
        window = self.windows.pop(key, deque(maxlen=self.max_messages))
        while window and window[0][0] < received_at - 30:
            window.popleft()
        normalized = unicodedata.normalize(
            "NFKC", str(payload.get("text", ""))
        ).casefold()
        normalized = "".join(
            c for c in normalized if not c.isspace() and unicodedata.category(c) != "Cf"
        )
        digest = hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""
        if not any(item[2] == str(message) for item in window):
            window.append((received_at, stamp, str(message), digest))
        self.windows[key] = window
        while len(self.windows) > self.max_members:
            self.windows.popitem(last=False)
        # Require both the upstream and receive-time windows; out-of-order
        # history cannot turn an old burst into a current one.
        recent = [
            mid
            for seen, sent, mid, _ in window
            if received_at - 10 <= seen <= received_at and stamp - 10 <= sent <= stamp
        ]
        rapid = [
            mid
            for seen, sent, mid, _ in window
            if received_at - 3 <= seen <= received_at and stamp - 3 <= sent <= stamp
        ]
        matching = [
            mid
            for seen, sent, mid, value in window
            if received_at - 30 <= seen <= received_at
            and stamp - 30 <= sent <= stamp
            and value == digest
            and bool(digest)
        ]
        count, repeated = len(recent), len(matching)
        messages = list(
            dict.fromkeys(
                (recent if count >= 15 else rapid if len(rapid) >= 5 else [])
                + (matching if repeated >= 5 else [])
            )
        )
        evidence = {
            "category": "message_flood"
            if len(rapid) >= 5 or count >= 15 or repeated >= 5
            else "",
            "received_at": received_at,
            "live": True,
            "count": count,
            "rapid": len(rapid),
            "repeated": repeated,
            "messages": messages,
            "account": account,
            "group": group,
            "sender": sender,
            "message": str(message),
        }
        if scope:
            evidence["test_window"] = scope
        return evidence

    def restore(self, account, rows, *, now=None):
        """Rebuild only the recent bounded ingress window after a plugin reload.

        Args:
            account: Authenticated inbox account.
            rows: Chronological tuples of group, message, payload, receive time.
            now: Optional clock used by deterministic tests.
        """
        now = time.time() if now is None else now
        for group, message, payload, received_at in rows:
            if not payload.get("_test_rule_window") and now - 30 <= received_at <= now:
                self.observe(
                    account,
                    group,
                    message,
                    payload,
                    received_at,
                    revision=payload.get("_ingress_flood", {}).get("revision", ""),
                )


class FloodRuntime:
    """Consume committed deterministic rules outside the ordinary chat FIFO."""

    def __init__(self, context, *, slots=4, backlog=256, media_runtime=None):
        self.context = context
        self.slots = slots
        self.backlog = backlog
        self.cursors = {}
        self.pending = OrderedDict()
        self.workers = {}
        self.assigned = set()
        self.finished = OrderedDict()
        self.wakeup = asyncio.Event()
        self.active = True
        self.started_at = time.time()
        self.media_runtime = media_runtime
        self.recalls = {}
        self.recall_workers = {}
        self.recall_counts = {}
        self.receipt_checks = {}
        self.receipt_workers = {}
        self.warning_items = {}
        self.warning_pending = {}
        self.warning_workers = {}
        self.warning_started = {}

    async def defer_warning(self, adapter, request, authorize):
        """Queue a bounded, audited notification outside the sanction lane.

        Args:
            adapter: Original audit-wrapped native action port.
            request: Frozen warning intent, never a punishment.
            authorize: Original scope and policy guard.

        Returns:
            Whether the warning is admitted or already has a durable receipt.
        """
        from astrbot.core.platform.sources.wangshangliao.storage import instance_dir

        from .review_store import review_store

        identity = (adapter.config["id"], adapter.account)
        if (
            request.action != "warning"
            or (request.instance, request.account) != identity
            or request.message
        ):
            return False
        key = (*identity, request.operation)
        if key in self.warning_items:
            return True
        store = getattr(adapter, "_review_store", None)
        if store is None:
            store = review_store(
                adapter, instance_dir(identity[0]) / "moderation.sqlite3"
            )
        full = sum(k[:2] == identity for k in self.warning_items) >= self.backlog
        item = (adapter, request, authorize, time.monotonic(), store)
        if not full and self.active:
            self.warning_items[key] = item
        reservation = asyncio.create_task(
            asyncio.to_thread(
                store.reserve_warning,
                request.operation,
                request.account,
                str(request.group),
                str(request.member),
            )
        )
        try:
            reserved = await asyncio.shield(reservation)
            if not reserved:
                self.warning_items.pop(key, None)
                return True
            if full or not self.active or key not in self.warning_items:
                if full:
                    gauges = getattr(adapter.diagnostics, "gauges", {})
                    gauges["warning_queue_full"] = (
                        gauges.get("warning_queue_full", 0) + 1
                    )
                await self.reject_warning(
                    item,
                    "notification_queue_full" if full else "notification_cancelled",
                )
                self.warning_items.pop(key, None)
                return False
            self.warning_pending.setdefault(identity, deque()).append(key)
            self.wakeup.set()
            return True
        except BaseException:
            await asyncio.gather(reservation, return_exceptions=True)
            self.warning_items.pop(key, None)
            await self.reject_warning(item, "notification_cancelled")
            raise

    async def reject_warning(self, item, error):
        """Close an unsubmitted warning and publish a payload-free failure.

        Args:
            item: Frozen adapter, intent, guard, enqueue time and audit store.
            error: Fixed cancellation or capacity diagnostic.
        """
        adapter, request, _, _, store = item
        await asyncio.to_thread(
            store.cancel_warning,
            request.operation,
            request.account,
            str(request.group),
            error,
        )
        adapter.diagnostics.emit(
            "warning_notification",
            "not_submitted",
            request.operation,
            failed=True,
            error=error,
        )

    async def send_warning(self, key):
        """Submit once after independent admission and current authorization.

        Args:
            key: Account-scoped queued operation key.
        """
        import inspect

        from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

        item = self.warning_items[key]
        adapter, request, authorize, queued_at, _ = item
        started = time.monotonic()
        adapter.diagnostics.observe("warning_queue", started - queued_at)

        async def guarded():
            if not self.active or time.monotonic() - queued_at > 30:
                raise ProtocolError("notification_expired")
            checked = authorize()
            if inspect.isawaitable(checked):
                await checked

        try:
            await guarded()
            result = await adapter.execute_action(request, authorize=guarded)
            adapter.diagnostics.emit(
                "warning_notification",
                result.get("status", "unknown"),
                request.operation,
            )
        finally:
            # The native audit owns submitted/unknown outcomes. This only
            # closes an unsubmitted queued receipt, including cancellation.
            try:
                await self.reject_warning_if_queued(item)
            finally:
                self.warning_items.pop(key, None)
                adapter.diagnostics.observe(
                    "warning_execution", time.monotonic() - started
                )
                self.wakeup.set()

    async def reject_warning_if_queued(self, item):
        """Close abandoned queue state without altering native receipts.

        Args:
            item: Frozen adapter, intent, guard, enqueue time and audit store.
        """
        _, request, _, queued_at, store = item
        await asyncio.to_thread(
            store.cancel_warning,
            request.operation,
            request.account,
            str(request.group),
            "notification_expired"
            if time.monotonic() - queued_at > 30
            else "notification_cancelled",
        )

    def defer_recalls(self, adapter, count, work, *, cancel=None):
        """Admit bounded cleanup without occupying the member's sanction lane.

        Args:
            adapter: Original authenticated transport.
            count: Number of already-reserved message recalls.
            work: Deferred callable retaining independent authorization checks.
            cancel: Optional audit closure for a batch cancelled before starting.

        Returns:
            Whether cleanup was admitted; a full queue keeps inline handling.
        """
        identity = (adapter.config["id"], adapter.account)
        if (
            not self.active
            or self.recall_counts.get(identity, 0) + count > self.backlog
        ):
            gauges = getattr(adapter.diagnostics, "gauges", {})
            gauges["flood_recall_queue_full"] = (
                gauges.get("flood_recall_queue_full", 0) + 1
            )
            return False
        self.recalls.setdefault(identity, deque()).append((count, work, cancel))
        self.recall_counts[identity] = self.recall_counts.get(identity, 0) + count
        self.wakeup.set()
        return True

    def owns(self, adapter, group, mid, payload):
        """Identify a current deterministic event handled by this inbox worker.

        Args:
            adapter: Original adapter owning the committed native inbox.
            group: Exact business group ID.
            mid: Exact upstream message ID.
            payload: Trusted event envelope from that inbox.

        Returns:
            Whether the ordinary pipeline can defer moderation to this worker.
            This is routing ownership, never evidence of a successful sanction.
        """
        from astrbot.core.platform.sources.wangshangliao.group_scope import (
            group_revision,
        )

        config = adapter.config
        if (
            not self.active
            or config.get("type") != "wangshangliao"
            or not config.get("enable", True)
            or config.get("_business_active") is False
            or adapter.connection_state != "online"
            or adapter.stopping.is_set()
            or getattr(getattr(adapter, "ledger", None), "db", None) is None
            or payload.get("_dispatch_class") == "command"
            or payload.get("_audit_incomplete")
            or group in getattr(adapter.ledger, "review_groups", set())
        ):
            return False
        now = time.time()
        revision = group_revision(config, group)
        for label, categories, since in (
            ("_ingress_raid", {"raid_flood"}, now - 30),
            ("_ingress_flood", {"message_flood"}, now - 30),
            ("_ingress_content", None, self.started_at),
        ):
            evidence = payload.get(label, {})
            if (
                evidence.get("category")
                and (categories is None or evidence["category"] in categories)
                and since <= evidence.get("received_at", 0) <= now
                and evidence.get("account") == adapter.account
                and evidence.get("group") == group
                and evidence.get("sender") == str(payload.get("sender", ""))
                and evidence.get("message") == mid
                and evidence.get("revision") == revision
                and not (
                    label == "_ingress_flood"
                    and payload.get("_ingress_raid", {}).get("observing")
                )
            ):
                return True
        return False

    async def handoff(self, adapter, group, mid, payload):
        """Acknowledge only non-exempt events actually admitted to a bounded lane.

        Args:
            adapter: Original authenticated inbox owner.
            group: Exact business group.
            mid: Committed upstream message ID.
            payload: Trusted event envelope, not a command-selected target.

        Returns:
            Whether a pending or running worker now owns this exact event.
            False leaves ordinary handling and keyword replies unchanged.
        """
        from .member_exemptions import is_exempt

        if not self.owns(adapter, group, mid, payload):
            return False
        if await asyncio.to_thread(
            is_exempt,
            adapter.config["id"],
            adapter.account,
            group,
            str(payload.get("sender", "")),
            payload.get("recall_route"),
        ):
            return False
        if not self.owns(adapter, group, mid, payload):
            return False
        snapshot = getattr(adapter, "member_snapshots", {}).get(group)
        sender = str(payload.get("sender", ""))
        # Unknown or protected roles keep ordinary handling and replies. This
        # snapshot is only a routing hint; execution still rereads native roles.
        if (
            not snapshot
            or snapshot[0] != adapter.account
            or not snapshot[2].get("complete")
            or not any(
                str(row.get("userId")) == sender
                and str(row.get("nimId")) == payload.get("recall_route", {}).get("peer")
                and row.get("groupRole") == "GROUP_ROLE_MEMBER"
                for row in snapshot[2].get("groupMemberInfo", [])
            )
        ):
            return False
        identity = (adapter.config["id"], adapter.account)
        message_key = (*identity, group, mid)
        if message_key in self.finished:
            return False
        if message_key in self.assigned:
            return True
        if (
            sum(
                len(items)
                for lane, items in self.pending.items()
                if lane[:2] == identity
            )
            >= self.backlog
        ):
            return False
        key = (*identity, group, str(payload.get("sender", "")))
        self.pending.setdefault(key, deque()).append(
            (adapter, mid, payload, adapter.config.get("_business_epoch"))
        )
        self.assigned.add(message_key)
        if payload.get("_ingress_raid", {}).get("category") == "raid_flood":
            self.pending.move_to_end(key, last=False)
        self.wakeup.set()
        return True

    async def step(self):
        """Scan bounded committed inbox rows and start at most four tasks per bot."""
        while self.finished:
            if next(iter(self.finished.values())) >= time.monotonic() - 300:
                break
            self.finished.popitem(last=False)
        for key, task in list(self.workers.items()):
            if task.done():
                if not task.cancelled():
                    task.exception()
                del self.workers[key]
        for identity, task in list(self.warning_workers.items()):
            if task.done():
                if not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        from astrbot.api import logger

                        logger.warning("Wangshangliao warning notification failed")
                del self.warning_workers[identity]
        for adapter in list(self.context.platform_manager.platform_insts):
            config = getattr(adapter, "config", {})
            ledger = getattr(adapter, "ledger", None)
            if (
                not self.active
                or config.get("type") != "wangshangliao"
                or not config.get("enable", True)
                or config.get("_business_active") is False
                or getattr(adapter, "connection_state", "") != "online"
                or (
                    getattr(adapter, "stopping", None) is not None
                    and adapter.stopping.is_set()
                )
                or ledger is None
                or ledger.db is None
            ):
                continue
            identity = (config["id"], adapter.account)
            cleanup = self.recall_workers.get(identity)
            if cleanup is not None and cleanup.done():
                if not cleanup.cancelled():
                    cleanup.exception()
                del self.recall_workers[identity]
            if identity not in self.recall_workers and self.recalls.get(identity):
                count, work, _ = self.recalls[identity].popleft()
                if not self.recalls[identity]:
                    del self.recalls[identity]
                cleanup = asyncio.create_task(work())
                self.recall_workers[identity] = cleanup

                def recalled(_, identity=identity, count=count):
                    self.recall_counts[identity] = max(
                        0, self.recall_counts.get(identity, 0) - count
                    )
                    self.wakeup.set()

                cleanup.add_done_callback(recalled)
            store = getattr(adapter, "_review_store", None)
            reconciliation = self.receipt_workers.get(identity)
            if reconciliation is not None and reconciliation.done():
                del self.receipt_workers[identity]
                reconciliation.result()
            if (
                store is not None
                and identity not in self.receipt_workers
                and time.monotonic() - self.receipt_checks.get(identity, 0) >= 5
            ):
                self.receipt_checks[identity] = time.monotonic()
                self.receipt_workers[identity] = asyncio.create_task(
                    self.reconcile_receipts(
                        adapter, adapter.account, store, ledger.path
                    )
                )
            # Adapter shutdown shares this lock and detaches the connection first.
            async with ledger.ingress_lock:
                if (
                    not self.active
                    or not config.get("enable", True)
                    or config.get("_business_active") is False
                    or getattr(adapter, "connection_state", "") != "online"
                    or (
                        getattr(adapter, "stopping", None) is not None
                        and adapter.stopping.is_set()
                    )
                    or adapter.ledger is not ledger
                    or ledger.db is None
                ):
                    continue
                connection = ledger.db
                # Keep the time index for startup, then seek the physical cursor
                # instead of rescanning five minutes of traffic on every poll.
                table = "inbox NOT INDEXED" if identity in self.cursors else "inbox"
                async with connection.execute(
                    f"SELECT rowid,team,message,payload FROM {table} WHERE account=? "
                    "AND rowid>? AND received_at>=? "
                    "AND state IN ('pending','processing','processed') "
                    "AND json_valid(payload) "
                    "ORDER BY rowid LIMIT 256",
                    (adapter.account, self.cursors.get(identity, 0), time.time() - 300),
                ) as cursor:
                    records = await cursor.fetchall()
            if (
                not self.active
                or not config.get("enable", True)
                or config.get("_business_active") is False
                or getattr(adapter, "connection_state", "") != "online"
                or (
                    getattr(adapter, "stopping", None) is not None
                    and adapter.stopping.is_set()
                )
                or adapter.ledger is not ledger
                or ledger.db is not connection
            ):
                continue
            gauges = getattr(adapter.diagnostics, "gauges", {})
            for rowid, group, mid, encoded in records:
                payload = json.loads(encoded)
                flood = payload.get("_ingress_flood", {})
                content = payload.get("_ingress_content", {})
                raid = payload.get("_ingress_raid", {})
                if not (
                    raid.get("category") == "raid_flood"
                    and raid.get("received_at", 0) >= time.time() - 30
                    or flood.get("category") == "message_flood"
                    and not raid.get("observing")
                    and flood.get("received_at", 0) >= time.time() - 30
                    or content.get("category")
                    and content.get("received_at", 0) >= self.started_at
                ):
                    self.cursors[identity] = rowid
                    continue
                message_key = (*identity, group, mid)
                if message_key in self.assigned or message_key in self.finished:
                    self.cursors[identity] = rowid
                    continue
                key = (*identity, group, str(payload.get("sender", "")))
                if (
                    sum(
                        len(items)
                        for lane, items in self.pending.items()
                        if lane[:2] == identity
                    )
                    >= self.backlog
                ):
                    gauges["rule_queue_full"] = gauges.get("rule_queue_full", 0) + 1
                    adapter.diagnostics.emit(
                        "content_queue" if content.get("category") else "flood_queue",
                        "full",
                        mid,
                        group=group,
                        failed=True,
                    )
                    break
                self.cursors[identity] = rowid
                self.pending.setdefault(key, deque()).append(
                    (adapter, mid, payload, config.get("_business_epoch"))
                )
                self.assigned.add(message_key)
                if raid.get("category") == "raid_flood":
                    self.pending.move_to_end(key, last=False)
            running = sum(k[:2] == identity for k in self.workers)
            for key in list(self.pending):
                if running >= self.slots:
                    break
                if key[:2] != identity or key in self.workers:
                    continue
                item = self.pending[key].popleft()
                if not self.pending[key]:
                    del self.pending[key]
                else:
                    self.pending.move_to_end(key)
                self.workers[key] = asyncio.create_task(self.execute(key, *item))

                def completed(_, message_key=(*identity, key[2], item[1])):
                    self.assigned.discard(message_key)
                    # Task completion is not a sanction receipt. Keep only
                    # bounded routing keys to avoid re-enqueueing its inbox row.
                    self.finished[message_key] = time.monotonic()
                    self.finished.move_to_end(message_key)
                    while len(self.finished) > 4096:
                        self.finished.popitem(last=False)
                    self.wakeup.set()

                self.workers[key].add_done_callback(completed)
                running += 1
            queued = [
                item
                for key, items in self.pending.items()
                if key[:2] == identity
                for item in items
            ]
            # Admit sanctions first, then allow one advisory per busy second.
            # Waiting for the entire account to become idle starves other groups.
            warning_keys = self.warning_pending.get(identity, ())
            now = time.monotonic()
            oldest_warning = (
                self.warning_items.get(warning_keys[0]) if warning_keys else None
            )
            if (
                identity not in self.warning_workers
                and oldest_warning is not None
                and (
                    not running
                    and not queued
                    or now - oldest_warning[3] >= 1
                    and now - self.warning_started.get(identity, -1) >= 1
                )
            ):
                warning_key = self.warning_pending[identity].popleft()
                if not self.warning_pending[identity]:
                    del self.warning_pending[identity]
                if warning_key in self.warning_items:
                    self.warning_started[identity] = now
                    self.warning_workers[identity] = asyncio.create_task(
                        self.send_warning(warning_key)
                    )
            gauges.update(
                warning_pending=len(self.warning_pending.get(identity, ())),
                warning_workers=int(identity in self.warning_workers),
                flood_recall_pending=self.recall_counts.get(identity, 0),
                flood_recall_workers=int(identity in self.recall_workers),
                rule_workers=running,
                rule_pending=len(queued),
                rule_oldest_pending_ms=int(
                    max(
                        (
                            max(
                                0,
                                time.time()
                                - (
                                    payload.get("_ingress_raid")
                                    or payload.get("_ingress_content")
                                    or payload.get("_ingress_flood", {})
                                ).get("received_at", time.time()),
                            )
                            for _, _, payload, _ in queued
                        ),
                        default=0,
                    )
                    * 1000
                ),
            )
        live = {
            (adapter.config.get("id"), getattr(adapter, "account", ""))
            for adapter in self.context.platform_manager.platform_insts
            if getattr(adapter, "config", {}).get("type") == "wangshangliao"
        }
        for key in list(self.cursors):
            if key not in live:
                del self.cursors[key]
        for key in list(self.pending):
            if key[:2] not in live:
                del self.pending[key]
        self.assigned.intersection_update(
            {key for key in self.assigned if key[:2] in live}
        )
        for key in list(self.finished):
            if key[:2] not in live:
                del self.finished[key]
        for identity in list(self.recalls):
            if identity not in live:
                for _, _, cancel in self.recalls.pop(identity):
                    if cancel is not None:
                        await cancel()
                self.recall_counts.pop(identity, None)
        for identity in list(self.receipt_checks):
            if identity not in live:
                del self.receipt_checks[identity]
        for identity, task in list(self.receipt_workers.items()):
            if identity not in live and task.done():
                if not task.cancelled():
                    task.exception()
                del self.receipt_workers[identity]
        available = {
            (adapter.config.get("id"), getattr(adapter, "account", ""))
            for adapter in self.context.platform_manager.platform_insts
            if getattr(adapter, "config", {}).get("enable", True)
            and getattr(adapter, "config", {}).get("_business_active") is not False
            and getattr(adapter, "connection_state", "") == "online"
        }
        for identity in list(self.warning_pending):
            if identity not in available:
                for key in self.warning_pending.pop(identity):
                    item = self.warning_items.pop(key, None)
                    if item is not None:
                        await self.reject_warning(item, "notification_cancelled")

    async def reconcile_receipts(self, adapter, account, store, ledger_path):
        """Own one bounded local receipt scan without holding rule dispatch.

        Args:
            adapter: Original diagnostic owner, never a new native action port.
            account: Frozen account owning both existing ledgers.
            store: Original review store retained until this scan finishes.
            ledger_path: Original transport database opened read-only.
        """
        try:
            await asyncio.to_thread(store.reconcile_recalls, account, ledger_path)
        except Exception as exc:
            busy = isinstance(exc, sqlite3.OperationalError) and (
                getattr(exc, "sqlite_errorcode", 0) & 0xFF
                in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            )
            adapter.diagnostics.emit(
                "recall_reconcile",
                "busy" if busy else "failed",
                failed=not busy,
                error="" if busy else "operation_failed",
            )

    async def execute(self, key, adapter, mid, payload, epoch):
        """Use the normal guarded sanction path, never a model-selected target.

        Args:
            key: Frozen instance, account, group and member lane.
            adapter: Original authenticated adapter.
            mid: Exact committed trigger message ID.
            payload: Original trusted inbox envelope.
            epoch: Plugin activation generation fixed when work was queued.
        """
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
        from astrbot.core.platform.sources.wangshangliao.group_scope import (
            group_revision,
            scoped,
        )
        from astrbot.core.platform.sources.wangshangliao.test_rules import rule_target
        from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

        from .content_rules import content_category, flood_category
        from .policy import handle, is_fresh_message

        instance, account, group, sender = key
        started = time.monotonic()
        received = (
            payload.get("_ingress_raid")
            or payload.get("_ingress_content")
            or payload.get("_ingress_flood", {})
        ).get("received_at", time.time())
        adapter.diagnostics.observe("rule_queue_wait", max(0, time.time() - received))
        raid = payload.get("_ingress_raid", {})
        if raid.get("category") == "raid_flood":
            from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
                runtime,
            )

            worker = getattr(runtime.open_service, "raid_defense", None)
            if worker is not None:
                try:
                    await worker.enforce(
                        adapter, group, mid, payload, recall_queue=self
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    adapter.diagnostics.emit(
                        "raid_response",
                        "blocked",
                        mid,
                        group=group,
                        failed=True,
                        error=str(exc)
                        if isinstance(exc, ProtocolError)
                        else "operation_failed",
                    )
                finally:
                    adapter.diagnostics.observe(
                        "rule_execution", time.monotonic() - started
                    )
            return
        session = str(
            MessageSession(instance, MessageType.GROUP_MESSAGE, f"{account}/{group}")
        )
        content = payload.get("_ingress_content", {})
        revision = content.get("revision") or payload.get("_ingress_flood", {}).get(
            "revision"
        )

        def authorized():
            config = adapter.config
            if (
                not self.active
                or not config.get("enable", True)
                or config.get("_business_active") is False
                or config.get("_business_epoch") != epoch
                or group_revision(config, group) != revision
                or adapter.account != account
                or config.get("id") != instance
                or adapter.connection_state != "online"
                or adapter.stopping.is_set()
                or group in getattr(adapter.ledger, "review_groups", set())
                or sender == account
                or is_managed_account(sender)
                and not rule_target(adapter, group, sender, mid)
                or sender in config.get("_open_managers", {}).get(group, [])
                or sender
                in {
                    str(uid)
                    for uid in self.context.get_config(session).get("admins_id", [])
                }
            ):
                raise ProtocolError("flood_worker_authority_changed")

        try:
            authorized()
            view = scoped(adapter, group)
            category = flood_category(view, group, mid, payload)
            if not category and content:
                policy = view.config.get("moderation", {})
                if (
                    not self.started_at <= content.get("received_at", 0) <= time.time()
                    or not is_fresh_message(payload, time.time())
                    or content.get("account") != account
                    or content.get("group") != group
                    or content.get("sender") != sender
                    or content.get("message") != mid
                    or payload.get("_dispatch_class") == "command"
                    or not policy.get("enabled")
                    or not policy.get("automation_enabled")
                ):
                    return
                category = content_category(
                    payload,
                    {
                        **policy,
                        "custom_rules": view.config.get("_open_groups", {})
                        .get(group, {})
                        .get("custom_rules", []),
                    },
                )
                if (
                    not category
                    and content.get("category") == "image_inspection"
                    and payload.get("kind") == "image"
                    and self.media_runtime is not None
                ):
                    await handle(
                        adapter,
                        group,
                        mid,
                        payload,
                        execution_check=authorized,
                        media_runtime=self.media_runtime,
                    )
                    return
                if category != content.get("category"):
                    return
            if not category:
                return
            consumed = await handle(
                adapter,
                group,
                mid,
                payload,
                assessment={
                    "decision": "violation",
                    "category": category,
                    "message_id": mid,
                },
                execution_check=authorized,
                recall_queue=self,
            )
            from .review_store import ReviewStore

            store = getattr(adapter, "_review_store", None)
            outcome = (
                await asyncio.to_thread(store.content_outcome, account, group, mid)
                if consumed and isinstance(store, ReviewStore)
                else "outcome_unconfirmed"
                if consumed
                else "not_applied"
            )
            adapter.diagnostics.emit(
                "content_response"
                if content and category != "message_flood"
                else "flood_response",
                outcome,
                mid,
                failed=outcome in {"preflight_failed", "execution_rejected", "unknown"},
                group=group,
                duration_ms=int(
                    max(
                        0,
                        time.time()
                        - (content or payload.get("_ingress_flood", {})).get(
                            "received_at", time.time()
                        ),
                    )
                    * 1000
                ),
                aggregate=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            adapter.diagnostics.emit(
                "flood_worker",
                "blocked_or_unknown",
                mid,
                failed=True,
                error=str(exc)
                if isinstance(exc, ProtocolError)
                else "operation_failed",
            )
        finally:
            adapter.diagnostics.observe("rule_execution", time.monotonic() - started)

    async def run(self):
        """Poll only this plugin's native adapters without blocking their readers."""
        try:
            while self.active:
                self.wakeup.clear()
                try:
                    await self.step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    from astrbot.api import logger

                    logger.exception("Wangshangliao flood inbox scan failed")
                try:
                    await asyncio.wait_for(self.wakeup.wait(), 0.1)
                except TimeoutError:
                    pass
        finally:
            await self.close()

    async def close(self):
        """Revoke queued work and quarantine any already-submitted unknown effects."""
        self.active = False
        self.wakeup.set()
        self.pending.clear()
        warning_items = list(self.warning_items.values())
        tasks = [
            *self.workers.values(),
            *self.recall_workers.values(),
            *self.warning_workers.values(),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Cancelling to_thread does not stop its SQLite work. Drain these
        # bounded scans before releasing the old plugin's stores on reload.
        if self.receipt_workers:
            await asyncio.gather(*self.receipt_workers.values(), return_exceptions=True)
        for batches in self.recalls.values():
            for _, _, cancel in batches:
                if cancel is not None:
                    await cancel()
        for item in warning_items:
            await self.reject_warning_if_queued(item)
        self.warning_items.clear()
        self.warning_pending.clear()
        self.warning_workers.clear()
        self.warning_started.clear()
        self.workers.clear()
        self.recalls.clear()
        self.recall_workers.clear()
        self.recall_counts.clear()
        self.receipt_checks.clear()
        self.receipt_workers.clear()
        self.assigned.clear()
        self.finished.clear()
        self.cursors.clear()


async def enforce(adapter, group, mid, payload, *, store, guard, recall_queue=None):
    """Count one violation per burst and recall its authenticated messages.

    Args:
        adapter: Authenticated native adapter with its original inbox.
        group: Enabled business group ID.
        mid: Triggering message ID.
        payload: Trusted ingress envelope.
        store: Instance-owned review repository.
        guard: Frozen account, policy and capability reauthorization callback.
        recall_queue: Optional bounded runtime for independent recall cleanup.

    Returns:
        Independent mute status and aggregate recall status. Unknowns are not retried.
    """
    from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
    from astrbot.core.platform.sources.wangshangliao.automatic import mute_and_escalate
    from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
    from astrbot.core.platform.sources.wangshangliao.progressive import (
        enforce as progress,
    )
    from astrbot.core.platform.sources.wangshangliao.test_rules import rule_target
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    from .content_rules import flood_category

    guard()
    account = adapter.account
    sender = str(payload.get("sender", ""))
    peer = adapter.members.get(group, {}).get(sender, "")
    revision = group_revision(adapter.config, group)
    evidence = payload.get("_ingress_flood", {})
    messages = evidence.get("messages")
    ledger = getattr(adapter, "ledger", None)
    if (
        not flood_category(adapter, group, mid, payload)
        or not adapter.config["moderation"].get("recall_enabled")
        or is_managed_account(sender)
        and not rule_target(adapter, group, sender, mid)
        or not peer
        or ledger is None
        or evidence.get("account") != account
        or evidence.get("group") != group
        or evidence.get("sender") != sender
        or evidence.get("message") != mid
        or not isinstance(messages, list)
        or not 1 <= len(messages) <= 256
        or any(
            not isinstance(message, str)
            or not message.isascii()
            or not message.isdigit()
            or int(message) <= 0
            for message in messages
        )
        or len(set(messages)) != len(messages)
        or mid not in messages
    ):
        raise ProtocolError("flood_evidence_unverified")
    # Serialize only this member's burst using a bounded set of locks.
    locks = getattr(adapter, "_flood_locks", None)
    if locks is None:
        locks = adapter._flood_locks = [asyncio.Lock() for _ in range(64)]
    async with locks[hash((account, group, sender)) % len(locks)]:
        guard()
        marks = ",".join("?" for _ in messages)
        async with ledger.db.execute(
            "SELECT message,payload,received_at FROM inbox "
            f"WHERE account=? AND team=? AND message IN ({marks})",
            (account, group, *messages),
        ) as cursor:
            rows = {
                row[0]: (json.loads(row[1]), row[2]) for row in await cursor.fetchall()
            }
        guard()
        trigger = rows.get(mid)
        if trigger is None or trigger[0].get("_ingress_flood") != evidence:
            raise ProtocolError("flood_evidence_unverified")
        received_at = trigger[1]
        if evidence.get("received_at") != received_at:
            raise ProtocolError("flood_evidence_unverified")
        for message in messages:
            row = rows.get(message)
            if (
                row is None
                or str(row[0].get("sender", "")) != sender
                or not received_at - 30 <= row[1] <= received_at
                or row[0].get("_ingress_flood", {}).get("revision") != revision
                or str(row[0].get("recall_route", {}).get("peer", "")) != peer
                or payload.get("_test_rule_window")
                and (
                    row[0].get("_test_rule_window") != payload["_test_rule_window"]
                    or not rule_target(adapter, group, sender, message)
                )
            ):
                raise ProtocolError("flood_evidence_unverified")
        # Recount only disjoint, authenticated candidates for the next warning.
        # Overlapping old recalls must not turn one extra message into a new burst.
        stamp = float(payload["recall_route"]["time"])
        if stamp > 100000000000:
            stamp /= 1000
        rapid, recent, matching = [], [], []
        normalized = (
            "".join(
                c
                for c in unicodedata.normalize(
                    "NFKC", str(payload.get("text", ""))
                ).casefold()
                if not c.isspace() and unicodedata.category(c) != "Cf"
            )
            if evidence.get("repeated", 0) >= 5
            else ""
        )
        for message, (candidate, seen) in rows.items():
            sent = float(candidate["recall_route"]["time"])
            if sent > 100000000000:
                sent /= 1000
            if not candidate.get("_ingress_flood", {}).get("live"):
                continue
            if received_at - 3 <= seen <= received_at and stamp - 3 <= sent <= stamp:
                rapid.append(message)
            if received_at - 10 <= seen <= received_at and stamp - 10 <= sent <= stamp:
                recent.append(message)
            if (
                normalized
                and received_at - 30 <= seen <= received_at
                and stamp - 30 <= sent <= stamp
                and normalized
                == "".join(
                    c
                    for c in unicodedata.normalize(
                        "NFKC", str(candidate.get("text", ""))
                    ).casefold()
                    if not c.isspace() and unicodedata.category(c) != "Cf"
                )
            ):
                matching.append(message)
        operation = (
            "flood/"
            + hashlib.sha256(
                json.dumps(
                    [account, group, sender, peer, revision, mid], separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
        operation, mute_status, pending = await asyncio.to_thread(
            store.reserve_flood,
            operation,
            account,
            group,
            sender,
            peer,
            str(revision),
            mid,
            received_at,
            messages,
            scope=payload.get("_test_rule_window", ""),
            windows=((5, rapid), (15, recent), (5, matching)),
        )
        guard()
        progression = {}
        if mute_status is None:
            mute_status = "unknown"
            try:
                if adapter.config["moderation"].get("progressive_mute"):
                    progression = await progress(
                        adapter,
                        group,
                        sender,
                        mid,
                        execution_check=guard,
                        operation=operation,
                        recall_after_mute=True,
                    )
                else:
                    progression = {
                        **await mute_and_escalate(
                            adapter,
                            operation,
                            group,
                            sender,
                            mid,
                            execution_check=guard,
                        ),
                        "recall_status": "none",
                    }
                mute_status = (
                    progression.get("status", "unknown")
                    if progression.get("action") == "mute"
                    else "none"
                )
                if mute_status not in {"accepted", "verified", "rejected", "none"}:
                    mute_status = "unknown"
                trigger_status = progression.get("recall_status", "unknown")
                if trigger_status != "none":
                    await asyncio.to_thread(
                        store.finish_flood, operation, trigger_status, message=mid
                    )
                    pending = [message for message in pending if message != mid]
            except Exception:
                adapter.diagnostics.emit(
                    "flood_mute", "failed_or_unknown", mid, failed=True
                )
            finally:
                await asyncio.to_thread(store.finish_flood, operation, mute_status)
        # Independent recalls still suppress spam when the mute receipt is uncertain.
        outcomes = (
            [progression["recall_status"]]
            if progression.get("recall_status") not in {None, "none"}
            else []
        )
        ready = asyncio.Event()
        expected_outcomes = len(outcomes) + len(pending)

        async def recall_pending():
            """Finish reserved recalls after the trigger's initial audit is saved."""
            await ready.wait()
            cleanup_started = time.monotonic()
            try:
                for message in pending:
                    guard()
                    recall_status = "unknown"
                    try:
                        result = await adapter.execute_action(
                            ActionRequest.bind(
                                adapter,
                                f"recall/{account}/{group}/{message}",
                                "recall",
                                int(group),
                                member=int(sender),
                                expected_nim=peer,
                                message=message,
                            ),
                            authorize=guard,
                        )
                        recall_status = result.get("status", "unknown")
                        if recall_status not in {"accepted", "verified", "rejected"}:
                            recall_status = "unknown"
                    except Exception as exc:
                        recall_status = getattr(exc, "action_status", "unknown")
                        adapter.diagnostics.emit(
                            "flood_recall",
                            recall_status,
                            message,
                            failed=True,
                            error=getattr(exc, "action_error", "operation_failed"),
                            group=group,
                            actor=sender,
                        )
                    finally:
                        await asyncio.to_thread(
                            store.finish_flood,
                            operation,
                            recall_status,
                            message=message,
                        )
                    outcomes.append(recall_status)
            except ProtocolError as exc:
                adapter.diagnostics.emit(
                    "flood_cleanup",
                    "blocked",
                    mid,
                    failed=True,
                    error=str(exc),
                    group=group,
                )
            finally:
                adapter.diagnostics.observe(
                    "flood_cleanup", time.monotonic() - cleanup_started
                )
                status = (
                    "accepted"
                    if len(outcomes) == expected_outcomes
                    and outcomes
                    and all(s in {"accepted", "verified"} for s in outcomes)
                    else "unknown"
                )
                await asyncio.to_thread(
                    store.finish,
                    account,
                    group,
                    mid,
                    "muted"
                    if mute_status in {"accepted", "verified"}
                    else "execution_rejected"
                    if mute_status == "rejected"
                    else "unknown"
                    if mute_status == "unknown"
                    else "recalled"
                    if status == "accepted"
                    else "unknown",
                    status,
                )

        if (
            pending
            and recall_queue is not None
            and recall_queue.defer_recalls(adapter, len(pending), recall_pending)
        ):
            return {
                **progression,
                "action": "mute" if mute_status != "none" else "recall",
                "status": mute_status if mute_status != "none" else "pending",
                "mute_status": mute_status,
                "recall_status": "pending",
                "warning_recall_status": progression.get("recall_status", "none"),
                "_recall_ready": ready,
            }
        ready.set()
        await recall_pending()
        recall_status = (
            "accepted"
            if outcomes and all(s in {"accepted", "verified"} for s in outcomes)
            else "rejected"
            if outcomes and all(s == "rejected" for s in outcomes)
            else "unknown"
            if outcomes
            else "none"
        )
        adapter.diagnostics.emit(
            "flood_result",
            mute_status,
            mid,
            action="mute",
            failed=mute_status not in {"accepted", "verified", "none"},
        )
        adapter.diagnostics.emit(
            "flood_result",
            recall_status,
            mid,
            action="recall",
            failed=recall_status not in {"accepted", "verified", "none"},
        )
        return {
            **progression,
            "action": "mute" if mute_status != "none" else "recall",
            "status": mute_status if mute_status != "none" else recall_status,
            "mute_status": mute_status,
            "recall_status": recall_status,
        }
