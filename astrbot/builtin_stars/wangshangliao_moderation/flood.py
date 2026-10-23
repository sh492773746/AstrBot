"""Bounded ingress-time rule workers with durable native inbox evidence."""

import asyncio
import hashlib
import json
import math
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
                (recent if count >= 8 else rapid if len(rapid) >= 3 else [])
                + (matching if repeated >= 5 else [])
            )
        )
        evidence = {
            "category": "message_flood"
            if len(rapid) >= 3 or count >= 8 or repeated >= 5
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

    def __init__(self, context, *, slots=4, backlog=256):
        self.context = context
        self.slots = slots
        self.backlog = backlog
        self.cursors = {}
        self.pending = OrderedDict()
        self.workers = {}
        self.active = True
        self.started_at = time.time()

    async def step(self):
        """Scan bounded committed inbox rows and start at most four tasks per bot."""
        for key, task in list(self.workers.items()):
            if task.done():
                if not task.cancelled():
                    task.exception()
                del self.workers[key]
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
                async with connection.execute(
                    "SELECT rowid,team,message,payload FROM inbox WHERE account=? "
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
                if not (
                    flood.get("category") == "message_flood"
                    and flood.get("received_at", 0) >= time.time() - 30
                    or content.get("category")
                    and content.get("received_at", 0) >= self.started_at
                ):
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
            running = sum(k[:2] == identity for k in self.workers)
            for key in list(self.pending):
                if running >= self.slots:
                    break
                if key[:2] != identity or key in self.workers:
                    continue
                item = self.pending[key].popleft()
                if not self.pending[key]:
                    del self.pending[key]
                self.workers[key] = asyncio.create_task(self.execute(key, *item))
                running += 1
            queued = [
                item
                for key, items in self.pending.items()
                if key[:2] == identity
                for item in items
            ]
            gauges.update(
                rule_workers=running,
                rule_pending=len(queued),
                rule_oldest_pending_ms=int(
                    max(
                        (
                            max(
                                0,
                                time.time()
                                - (
                                    payload.get("_ingress_content")
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

        from .content_rules import classify, flood_category, keyword_category
        from .policy import handle, is_fresh_message

        instance, account, group, sender = key
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
                    or policy.get("advertising_enabled") is False
                ):
                    return
                category = keyword_category(
                    payload.get("text", ""),
                    {
                        **policy,
                        "custom_rules": view.config.get("_open_groups", {})
                        .get(group, {})
                        .get("custom_rules", []),
                    },
                ) or classify(payload.get("text", ""), name=payload.get("name", ""))
                if category != content.get("category"):
                    return
            if not category:
                return
            await handle(
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
            )
            adapter.diagnostics.emit(
                "content_response"
                if content and category != "message_flood"
                else "flood_response",
                "completed",
                mid,
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

    async def run(self):
        """Poll only this plugin's native adapters without blocking their readers."""
        try:
            while self.active:
                try:
                    await self.step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    from astrbot.api import logger

                    logger.exception("Wangshangliao flood inbox scan failed")
                await asyncio.sleep(0.1)
        finally:
            await self.close()

    async def close(self):
        """Revoke queued work and quarantine any already-submitted unknown effects."""
        self.active = False
        self.pending.clear()
        for task in self.workers.values():
            task.cancel()
        if self.workers:
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
        self.workers.clear()
        self.cursors.clear()


async def enforce(adapter, group, mid, payload, *, store, guard):
    """Mute once, then recall only authenticated messages belonging to this burst.

    Args:
        adapter: Authenticated native adapter with its original inbox.
        group: Enabled business group ID.
        mid: Triggering message ID.
        payload: Trusted ingress envelope.
        store: Instance-owned review repository.
        guard: Frozen account, policy and capability reauthorization callback.

    Returns:
        Independent mute status and aggregate recall status. Unknowns are not retried.
    """
    from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
    from astrbot.core.platform.sources.wangshangliao.automatic import mute_and_escalate
    from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
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
        )
        guard()
        if mute_status is None:
            mute_status = "unknown"
            try:
                result = await mute_and_escalate(
                    adapter, operation, group, sender, mid, execution_check=guard
                )
                mute_status = result.get("status", "unknown")
                if mute_status not in {"accepted", "verified", "rejected"}:
                    mute_status = "unknown"
            except Exception:
                adapter.diagnostics.emit(
                    "flood_mute", "failed_or_unknown", mid, failed=True
                )
            finally:
                await asyncio.to_thread(store.finish_flood, operation, mute_status)
        # Independent recalls still suppress spam when the mute receipt is uncertain.
        outcomes = []
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
                    store.finish_flood, operation, recall_status, message=message
                )
            outcomes.append(recall_status)
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
            failed=mute_status not in {"accepted", "verified"},
        )
        adapter.diagnostics.emit(
            "flood_result",
            recall_status,
            mid,
            action="recall",
            failed=recall_status not in {"accepted", "verified", "none"},
        )
        return {"mute_status": mute_status, "recall_status": recall_status}
