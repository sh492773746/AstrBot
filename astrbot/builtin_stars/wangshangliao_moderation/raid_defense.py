"""Bounded newcomer anti-raid decisions independent of OCR and normal warnings."""

import asyncio
import json
import math
import secrets
import time
from collections import OrderedDict, deque

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.blacklist import BlacklistStore
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.directory import muted_member_ids
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.invitations import (
    latest_membership_events,
)
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.raid import RaidStore
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .member_exemptions import is_exempt
from .member_whitelist import Scope
from .open_store import OpenError


class RaidDefense:
    """Observe in memory and corroborate admissions outside the ACK/chat lanes."""

    def __init__(self, service):
        self.service = service
        self.states = {}
        self.members = {}
        self.windows = OrderedDict()
        self.tasks = {}
        self.poll_at = {}
        self.join_cursors = {}
        self.seen = OrderedDict()
        self.previews = {}
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.active = True
        self.pruned_at = {}
        self.submissions = set()

    def enabled(self, adapter, group):
        """Check current saved opt-in and runtime fences without doing IO.

        Args:
            adapter: Original authenticated transport.
            group: Exact business group.

        Returns:
            Whether automatic defense is currently authorized.
        """
        tenant = self.service.tenants.get((str(adapter.account), group), {})
        config = adapter.config
        return bool(
            self.active
            and self.service.active
            and not self.service.settings["paused"]
            and not self.service.capacity_paused
            and tenant.get("active")
            and tenant.get("present")
            and not tenant.get("paused")
            and tenant.get("rules", {}).get("raid_defense_enabled") is True
            and tenant.get("native_role") in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
            and (str(adapter.account), group) not in self.service.suspended_groups
            and not self.service.membership_receiving.get((str(adapter.account), group))
            and not self.service.reconciling_groups.get((str(adapter.account), group))
            and config.get("enable", True)
            and config.get("_business_active") is not False
            and group in config.get("enabled_groups", [])
            and not adapter.stopping.is_set()
        )

    async def refresh(self):
        """Restore working observations and revoke generations only on switch changes."""
        for adapter in self.service.adapters():
            account = str(adapter.account)
            store = RaidStore(adapter.config["id"])
            for tenant in list(self.service.tenants.values()):
                if tenant["account"] != account:
                    continue
                group, key = tenant["group_id"], (account, tenant["group_id"])
                enabled = tenant["rules"].get("raid_defense_enabled") is True
                if not enabled and key not in self.states:
                    continue
                snapshot = await asyncio.to_thread(
                    store.configure,
                    account,
                    group,
                    enabled,
                    guard=account_guard(adapter),
                )
                self.states[key] = snapshot["state"]
                self.members[key] = {row["member"]: row for row in snapshot["members"]}
                if enabled and snapshot["members"]:
                    async with adapter.ledger.ingress_lock:
                        async with adapter.ledger.db.execute(
                            "SELECT message,payload,received_at FROM inbox WHERE account=? "
                            "AND team=? AND received_at>=? "
                            "AND state IN ('pending','processing','processed') "
                            "ORDER BY received_at DESC LIMIT 4096",
                            (account, group, time.time() - 10),
                        ) as cursor:
                            recent = await cursor.fetchall()
                    for mid, encoded, received_at in reversed(recent):
                        self.observe(
                            adapter, group, mid, json.loads(encoded), received_at
                        )

    async def maintain(self):
        """Prune expired metadata in worker threads without touching pending IO."""
        for adapter in self.service.adapters():
            account = str(adapter.account)
            if not any(key[0] == account for key in self.states):
                continue
            if time.monotonic() - self.pruned_at.get(account, -3600) >= 3600:
                await asyncio.to_thread(RaidStore(adapter.config["id"]).prune)
                self.pruned_at[account] = time.monotonic()
        now = time.time()
        for key, members in self.members.items():
            self.members[key] = {
                uid: row
                for uid, row in members.items()
                if max(row["observed_until"], row["mute_until"]) > now
            }
        self.previews = {key: row for key, row in self.previews.items() if row[0] > now}

    def observe(self, adapter, group, message, payload, received_at):
        """Attach bounded evidence to a newly committed authenticated envelope.

        Args:
            adapter: Original receiving instance.
            group: Exact group resolved by the adapter.
            message: Stable native message identity.
            payload: Trusted envelope, not a user-provided object.
            received_at: Fixed local ingress time.

        Returns:
            Independent decision and exact batch identities, or an empty dict.
        """
        account, sender = str(adapter.account), str(payload.get("sender", ""))
        if (
            not self.enabled(adapter, group)
            or sender == account
            or is_managed_account(sender)
        ):
            return {}
        state = self.states.get((account, group), {})
        if not state.get("enabled"):
            return {}
        try:
            stamp = float(payload.get("recall_route", {}).get("time", 0)) / 1000
        except (TypeError, ValueError, OverflowError):
            return {}
        if not math.isfinite(stamp) or not received_at - 10 <= stamp <= received_at + 2:
            return {}
        key = (account, group, sender, state["generation"])
        window = self.windows.pop(key, deque(maxlen=64))
        while window and window[0][0] < received_at - 10:
            window.popleft()
        if not any(row[2] == str(message) for row in window):
            window.append(
                (
                    received_at,
                    stamp,
                    str(message),
                    payload.get("kind") in {"image", "video"},
                )
            )
        self.windows[key] = window
        while len(self.windows) > 4096:
            self.windows.popitem(last=False)
        member = self.members.get((account, group), {}).get(sender)
        if (
            not member
            or member["observed_until"] <= received_at
            or member["joined_at"] < state["enabled_at"]
            or member["peer"] != str(payload.get("recall_route", {}).get("peer", ""))
        ):
            return {}
        rapid, media = [], []
        for seen, sent, mid, is_media in window:
            if sent < member["joined_at"] or seen < member["joined_at"]:
                continue
            if received_at - 3 <= seen <= received_at and stamp - 3 <= sent <= stamp:
                rapid.append(mid)
            if (
                is_media
                and received_at - 10 <= seen <= received_at
                and stamp - 10 <= sent <= stamp
            ):
                media.append(mid)
        return {
            "observing": True,
            "category": "raid_flood" if len(rapid) >= 3 or len(media) >= 5 else "",
            "messages": list(
                dict.fromkeys(
                    (rapid if len(rapid) >= 3 else [])
                    + (media if len(media) >= 5 else [])
                )
            ),
            "received_at": received_at,
            "account": account,
            "group": group,
            "sender": sender,
            "message": str(message),
            "join_id": member["join_id"],
            "generation": state["generation"],
            "revision": group_revision(adapter.config, group),
        }

    def wake(self, adapter, group):
        """Schedule join corroboration without network work in the reader.

        Args:
            adapter: Original receiving instance.
            group: Exact notification group.
        """
        self.poll_at.pop((str(adapter.account), group), None)
        self.tick(urgent=(str(adapter.account), group))

    def tick(self, *, urgent=None):
        """Start at most four coalesced group workers, never one per incoming message."""
        for key, task in list(self.tasks.items()):
            if task.done():
                if not task.cancelled():
                    task.exception()
                del self.tasks[key]
        adapters = {
            str(adapter.account): adapter for adapter in self.service.adapters()
        }
        for tenant in sorted(
            self.service.tenants.values(),
            key=lambda row: (
                (row["account"], row["group_id"]) != urgent,
                self.poll_at.get((row["account"], row["group_id"]), -3),
            ),
        ):
            group, key = tenant["group_id"], (tenant["account"], tenant["group_id"])
            adapter = adapters.get(tenant["account"])
            if (
                adapter is None
                or not self.enabled(adapter, group)
                or adapter.connection_state != "online"
                or key in self.tasks
                or len(self.tasks) >= 4
                or time.monotonic() - self.poll_at.get(key, -3) < 3
            ):
                continue
            self.poll_at[key] = time.monotonic()
            self.tasks[key] = asyncio.create_task(self.process_joins(adapter, group))

    async def protected(self, adapter, group, uid, peer):
        """Keep explicit blacklist and all trusted exemptions ahead of short mutes.

        Args:
            adapter: Original receiving instance.
            group: Exact group.
            uid: Verified member.
            peer: Frozen transport identity.

        Returns:
            Whether defense must leave this target untouched.
        """
        if (
            uid == str(adapter.account)
            or is_managed_account(uid)
            or uid in adapter.config.get("_open_managers", {}).get(group, [])
        ):
            return True
        if await asyncio.to_thread(
            is_exempt, adapter.config["id"], str(adapter.account), group, uid, peer
        ):
            return True
        store = await asyncio.to_thread(BlacklistStore, adapter.config["id"])
        _, entries = await asyncio.to_thread(store.read, str(adapter.account), group)
        return any(str(row["member"]) == uid for row in entries)

    async def process_joins(self, adapter, group):
        """Corroborate only fresh actual admissions and restore their remaining mutes.

        Args:
            adapter: Original receiving transport.
            group: One enabled group.
        """
        account, store = str(adapter.account), RaidStore(adapter.config["id"])
        state = self.states.get((account, group), {})
        if not self.enabled(adapter, group) or not state:
            return
        revision = group_revision(adapter.config, group)
        frozen = account_guard(adapter)

        def guard():
            frozen()
            if (
                not self.enabled(adapter, group)
                or group_revision(adapter.config, group) != revision
            ):
                raise ProtocolError("raid_policy_changed")

        try:
            ledger = adapter.ledger
            async with ledger.ingress_lock:
                parameters = (
                    account,
                    group,
                    max(state["enabled_at"], time.time() - 120),
                    json.dumps(
                        [
                            mid
                            for seen_account, seen_group, mid in self.seen
                            if (seen_account, seen_group) == (account, group)
                        ]
                    ),
                )
                start = self.join_cursors.get((account, group), 0)
                # Rotate past unresolved pages, then wrap to recheck lagging logs.
                for after in (start, 0) if start else (0,):
                    async with ledger.db.execute(
                        "SELECT rowid,message,payload FROM inbox WHERE account=? AND team=? "
                        "AND state='invite_notification' AND received_at>=? "
                        "AND message NOT IN (SELECT value FROM json_each(?)) "
                        "AND rowid>? ORDER BY rowid LIMIT 64",
                        (*parameters, after),
                    ) as cursor:
                        rows = await cursor.fetchall()
                    if rows:
                        break
                notices = [
                    (mid, json.loads(encoded))
                    for _, mid, encoded in rows
                    if (account, group, mid) not in self.seen
                ]
            guard()
            if rows:
                self.join_cursors[(account, group)] = rows[-1][0]
            if not notices:
                return
            roster = await self.service.roster(adapter, group)
            events = latest_membership_events(
                await adapter.business.request("/v1/group/get-apply-logs", {}), group
            )
            guard()
            for mid, notice in notices:
                event_at = notice.get("event_at", 0) / 1000
                if (
                    not max(state["enabled_at"], time.time() - 120)
                    <= event_at
                    <= time.time()
                ):
                    self.seen[(account, group, mid)] = True
                    while len(self.seen) > 4096:
                        self.seen.popitem(last=False)
                    continue
                peers = set(notice.get("member_peers", []))
                resolved = set()
                for uid, evidence in events.items():
                    event, row = evidence["row"], roster.get(uid, {})
                    peer = str(row.get("nimId", ""))
                    if (
                        evidence["ambiguous"]
                        or event.get("state") != "MEMBER_STATE_GOOD"
                        or event.get("groupApplyType") not in {"INVITATION", "APPLY"}
                        or abs(evidence["timestamp"] - event_at) > 120
                        or peer not in peers
                        or str(event["applicant"].get("nimId", "")) != peer
                        or row.get("groupRole") != "GROUP_ROLE_MEMBER"
                        or await self.protected(adapter, group, uid, peer)
                    ):
                        continue
                    resolved.add(peer)
                    member = await asyncio.to_thread(
                        store.join,
                        account,
                        group,
                        uid,
                        peer,
                        str(event["applyId"]),
                        evidence["timestamp"],
                        guard=guard,
                    )
                    if member is None:
                        continue
                    self.members.setdefault((account, group), {})[uid] = member
                    await self.mute(
                        adapter,
                        group,
                        uid,
                        kind="restore"
                        if member["mute_until"] > time.time()
                        else "hold",
                        trigger=member["join_id"],
                        guard=guard,
                    )
                    # Messages can precede the read-side admission corroboration.
                    # Revisit only fresh, authenticated ingress, never old history.
                    async with ledger.ingress_lock:
                        async with ledger.db.execute(
                            "SELECT message,payload,received_at FROM inbox WHERE account=? AND team=? "
                            "AND sender=? AND received_at>=? AND state IN ('pending','processing','processed') "
                            "ORDER BY received_at LIMIT 64",
                            (
                                account,
                                group,
                                uid,
                                max(member["joined_at"], time.time() - 10),
                            ),
                        ) as cursor:
                            rows = await cursor.fetchall()
                    for message, encoded, received_at in rows:
                        payload = json.loads(encoded)
                        decision = self.observe(
                            adapter, group, message, payload, received_at
                        )
                        if decision.get("category"):
                            await self.enforce(
                                adapter,
                                group,
                                message,
                                {**payload, "_ingress_raid": decision},
                            )
                            break
                # Provider event logs can lag a delivered native notification.
                # Keep unresolved notices eligible for the bounded freshness window.
                if peers and peers.issubset(resolved):
                    self.seen[(account, group, mid)] = True
                while len(self.seen) > 4096:
                    self.seen.popitem(last=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            adapter.diagnostics.emit(
                "raid_membership",
                "unavailable",
                group=group,
                failed=True,
                error=str(exc)
                if isinstance(exc, ProtocolError)
                else "operation_failed",
            )

    async def mute(self, adapter, group, uid, *, kind, trigger, guard):
        """Submit one stored short-mute intent through the native protected boundary.

        Args:
            adapter: Original transport.
            group: Exact group.
            uid: Verified member.
            kind: Burst, hold or remaining-time restoration.
            trigger: Native message or admission identity.
            guard: Caller-owned current policy fence.

        Returns:
            Independent mute outcome.
        """
        account, store = str(adapter.account), RaidStore(adapter.config["id"])
        guard()
        intent = await asyncio.to_thread(
            store.reserve, account, group, uid, kind=kind, trigger=trigger, guard=guard
        )
        if intent is None:
            return "covered_or_unresolved"
        status = "unknown"
        attempted = False

        async def authorize():
            guard()
            await asyncio.to_thread(store.authorize, intent)
            if await self.protected(adapter, group, uid, intent.peer):
                raise ProtocolError("raid_protected")
            if (
                getattr(adapter.business, "_authorization_task", None)
                is asyncio.current_task()
            ):
                events = latest_membership_events(
                    await adapter.business.request("/v1/group/get-apply-logs", {}),
                    group,
                )
                current = events.get(uid)
                if (
                    not current
                    or current["ambiguous"]
                    or str(current["row"]["applyId"]) != intent.join_id
                    or current["row"].get("state") != "MEMBER_STATE_GOOD"
                ):
                    raise ProtocolError("raid_join_changed")
                if uid in await muted_member_ids(
                    adapter, group, authorize=guard, expected_member=(uid, intent.peer)
                ):
                    raise ProtocolError("raid_existing_mute")
            guard()

        try:
            reader = getattr(adapter, "get_moderation_members", None)
            if callable(reader):
                directory = await reader(group, max_age=1)
                if not directory.get("complete"):
                    raise ProtocolError("raid_protected")
                roster = {
                    str(row["userId"]): row
                    for row in directory.get("groupMemberInfo", [])
                }
            else:
                roster = await self.service.roster(adapter, group)
            if (
                roster.get(uid, {}).get("groupRole") != "GROUP_ROLE_MEMBER"
                or str(roster.get(uid, {}).get("nimId", "")) != intent.peer
            ):
                raise ProtocolError("raid_protected")
            await authorize()
            if uid in await muted_member_ids(
                adapter, group, authorize=guard, expected_member=(uid, intent.peer)
            ):
                status = "skipped"
            else:
                attempted = True
                self.submissions.add(asyncio.current_task())
                result = await adapter.execute_action(
                    ActionRequest.bind(
                        adapter,
                        intent.operation,
                        "mute",
                        int(group),
                        member=int(uid),
                        minutes=intent.minutes,
                        expected_nim=intent.peer,
                        raid_intent=intent,
                    ),
                    authorize=authorize,
                )
                status = result.get("status", "unknown")
        except asyncio.CancelledError:
            if not attempted:
                status = "rejected"
            raise
        except Exception as exc:
            status = (
                "skipped"
                if str(exc) == "raid_existing_mute"
                else "rejected"
                if getattr(exc, "submitted", None) is False
                else (
                    getattr(exc, "action_status", "unknown")
                    if attempted
                    else "rejected"
                )
            )
            adapter.diagnostics.emit(
                "raid_mute",
                status,
                group=group,
                actor=uid,
                failed=True,
                error=str(exc)
                if isinstance(exc, ProtocolError)
                else "operation_failed",
            )
        finally:
            try:
                await asyncio.to_thread(store.finish, intent, status)
                await self.reload(adapter, group)
            finally:
                self.submissions.discard(asyncio.current_task())
        return status

    async def enforce(self, adapter, group, mid, payload, *, recall_queue=None):
        """Verify the original inbox batch before independent mute and recalls.

        Args:
            adapter: Original transport.
            group: Enabled group.
            mid: Authentic trigger identity.
            payload: Trusted envelope with ingress-only raid evidence.
            recall_queue: Existing bounded cleanup lane, if supplied by its owner.
        """
        evidence = payload.get("_ingress_raid", {})
        uid, account = str(payload.get("sender", "")), str(adapter.account)
        member = self.members.get((account, group), {}).get(uid, {})
        revision = group_revision(adapter.config, group)
        frozen = account_guard(adapter)

        def guard():
            frozen()
            current_role = (
                getattr(self.service, "member_evidence", {})
                .get((account, group), {})
                .get(uid, {})
                .get("groupRole")
            )
            if (
                not self.enabled(adapter, group)
                or group_revision(adapter.config, group) != revision
                or self.members.get((account, group), {}).get(uid, {}).get("join_id")
                != member.get("join_id")
                or member.get("observed_until", 0) <= time.time()
                or current_role in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
            ):
                raise ProtocolError("raid_policy_changed")
            authorize_action(adapter.config, group, "mute")
            authorize_action(adapter.config, group, "recall")

        async with self.locks[hash((account, group, uid)) % len(self.locks)]:
            guard()
            ids = evidence.get("messages", [])
            if (
                evidence.get("category") != "raid_flood"
                or not isinstance(ids, list)
                or not 1 <= len(ids) <= 64
                or mid not in ids
                or len(set(ids)) != len(ids)
                or (
                    evidence.get("account"),
                    evidence.get("group"),
                    evidence.get("sender"),
                    evidence.get("message"),
                    evidence.get("revision"),
                    evidence.get("join_id"),
                    evidence.get("generation"),
                )
                != (
                    account,
                    group,
                    uid,
                    mid,
                    revision,
                    member.get("join_id"),
                    self.states.get((account, group), {}).get("generation"),
                )
                or evidence.get("received_at", 0) < time.time() - 30
                or await self.protected(adapter, group, uid, member.get("peer", ""))
            ):
                raise ProtocolError("raid_evidence_unverified")
            ledger = adapter.ledger
            async with ledger.ingress_lock:
                async with ledger.db.execute(
                    "SELECT message,payload,received_at FROM inbox WHERE account=? AND team=? AND message IN ("
                    + ",".join("?" for _ in ids)
                    + ")",
                    (account, group, *ids),
                ) as cursor:
                    rows = {
                        message: (json.loads(encoded), stamp)
                        for message, encoded, stamp in await cursor.fetchall()
                    }
            guard()
            trigger = rows.get(mid)
            if trigger is None or trigger[1] != evidence["received_at"]:
                raise ProtocolError("raid_evidence_unverified")
            sent_at = float(trigger[0].get("recall_route", {}).get("time", 0)) / 1000
            rapid, media = 0, 0
            for message in ids:
                original, seen = rows.get(message, ({}, 0))
                stamp = float(original.get("recall_route", {}).get("time", 0)) / 1000
                if (
                    original.get("sender") != uid
                    or original.get("recall_route", {}).get("peer") != member["peer"]
                    or not max(member["joined_at"], trigger[1] - 10)
                    <= seen
                    <= trigger[1]
                    or not max(member["joined_at"], sent_at - 10) <= stamp <= sent_at
                ):
                    raise ProtocolError("raid_evidence_unverified")
                rapid += seen >= trigger[1] - 3 and stamp >= sent_at - 3
                media += original.get("kind") in {"image", "video"}
            if rapid < 3 and media < 5:
                raise ProtocolError("raid_evidence_unverified")
            guard()
            outcome = await self.mute(
                adapter, group, uid, kind="burst", trigger=mid, guard=guard
            )
            store = RaidStore(adapter.config["id"])
            pending = await asyncio.to_thread(
                store.reserve_recalls, account, group, uid, ids, guard=guard
            )

            async def recall_authorize():
                guard()
                if await self.protected(adapter, group, uid, member["peer"]):
                    raise ProtocolError("raid_protected")
                guard()

            async def cancel_cleanup():
                """Close reservations that never reached a native submission."""
                for message in pending:
                    await asyncio.to_thread(
                        store.recall, account, group, uid, message, status="rejected"
                    )

            async def cleanup():
                """Drain only newly reserved identities outside the sanction lock."""
                started = time.monotonic()
                remaining = set(pending)
                statuses = []
                try:
                    for message in pending:
                        status = "rejected"
                        try:
                            await recall_authorize()
                            self.submissions.add(asyncio.current_task())
                            status = "unknown"
                            result = await adapter.execute_action(
                                ActionRequest.bind(
                                    adapter,
                                    f"recall/{account}/{group}/{message}",
                                    "recall",
                                    int(group),
                                    member=int(uid),
                                    expected_nim=member["peer"],
                                    message=message,
                                ),
                                authorize=recall_authorize,
                            )
                            status = result.get("status", "unknown")
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            status = (
                                "rejected"
                                if status == "rejected"
                                or getattr(exc, "submitted", None) is False
                                else getattr(exc, "action_status", "unknown")
                            )
                        finally:
                            try:
                                await asyncio.to_thread(
                                    store.recall,
                                    account,
                                    group,
                                    uid,
                                    message,
                                    status=status,
                                )
                                remaining.discard(message)
                                statuses.append(status)
                            finally:
                                self.submissions.discard(asyncio.current_task())
                finally:
                    for message in remaining:
                        await asyncio.to_thread(
                            store.recall,
                            account,
                            group,
                            uid,
                            message,
                            status="rejected",
                        )
                    result = (
                        "accepted"
                        if len(statuses) == len(pending)
                        and all(s in {"accepted", "verified"} for s in statuses)
                        else "rejected"
                        if all(s == "rejected" for s in statuses)
                        else "unknown"
                    )
                    adapter.diagnostics.emit(
                        "raid_cleanup",
                        result,
                        mid,
                        group=group,
                        action="recall",
                        failed=result not in {"accepted", "verified"},
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )

            if recall_queue is None:
                recall_queue = getattr(
                    getattr(self.service, "plugin", None), "flood_runtime", None
                )
            deferred = bool(
                pending
                and recall_queue is not None
                and recall_queue.defer_recalls(
                    adapter, len(pending), cleanup, cancel=cancel_cleanup
                )
            )
            adapter.diagnostics.emit(
                "raid_response",
                outcome,
                mid,
                group=group,
                failed=outcome
                not in {"accepted", "verified", "skipped", "covered_or_unresolved"},
                duration_ms=int(max(0, time.time() - evidence["received_at"]) * 1000),
            )
        if pending and not deferred:
            # Full queues retain cleanup, but never retain the member mute lock.
            await cleanup()

    async def reload(self, adapter, group):
        """Refresh metadata after a durable join or sanction transition.

        Args:
            adapter: Original instance.
            group: Exact group.
        """
        snapshot = await asyncio.to_thread(
            RaidStore(adapter.config["id"]).snapshot, str(adapter.account), group
        )
        self.states[(str(adapter.account), group)] = snapshot["state"]
        self.members[(str(adapter.account), group)] = {
            row["member"]: row for row in snapshot["members"]
        }

    async def control(self, rpc, call):
        """Read status or personally confirm ending one exact protection period.

        Args:
            rpc: Authenticated in-process control plane.
            call: Dashboard-only allowlisted request.

        Returns:
            Status, a short-lived preview or the confirmed end result.
        """
        payload = dict(call.payload)
        operation = payload.pop("operation", "")
        token = payload.pop("token", "")
        if not isinstance(token, str) or len(token) > 128:
            raise OpenError("raid_arguments")
        scope = Scope.model_validate(payload)
        if not call.actor.startswith("dashboard/") or operation not in {
            "read",
            "preview",
            "commit",
        }:
            raise OpenError("raid_arguments")
        adapter = next(
            (a for a in self.service.adapters() if str(a.account) == scope.account),
            None,
        )
        tenant = self.service.tenants.get((scope.account, scope.group_id))
        if adapter is None or not tenant or tenant["version"] != scope.tenant_version:
            raise OpenError("tenant_changed")
        store = RaidStore(adapter.config["id"])
        snapshot = await asyncio.to_thread(
            store.snapshot, scope.account, scope.group_id
        )
        if operation == "read":
            return {
                **snapshot,
                "enabled": tenant["rules"].get("raid_defense_enabled", False),
                "operational": self.enabled(adapter, scope.group_id),
            }
        roster = await self.service.roster(adapter, scope.group_id)
        guard = rpc.control_guard(adapter, tenant, roster)
        guard()
        period = snapshot["state"].get("protection_id", "")
        if snapshot["state"].get("protection_until", 0) <= time.time():
            raise OpenError("raid_protection_changed")
        binding = (
            call.actor,
            scope.account,
            scope.group_id,
            scope.tenant_version,
            period,
        )
        self.previews = {
            key: row for key, row in self.previews.items() if row[0] > time.time()
        }
        if operation == "preview":
            if len(self.previews) >= 100:
                raise OpenError("service_busy")
            token = secrets.token_urlsafe(24)
            self.previews[token] = (time.time() + 300, binding)
            return {"token": token, "expires_in": 300}
        preview = self.previews.pop(token, None)
        if not preview or preview[1] != binding:
            raise OpenError("confirmation_invalid")
        try:
            await asyncio.to_thread(
                store.end, scope.account, scope.group_id, period, guard=guard
            )
        except ProtocolError as exc:
            if str(exc) == "raid_protection_changed":
                raise OpenError("raid_protection_changed") from exc
            raise
        await self.reload(adapter, scope.group_id)
        return {"ended": True}

    async def release(self, adapter, group, member, *, operation):
        """Remove only defense-owned remaining mute debt after a successful unmute.

        Args:
            adapter: Original account.
            group: Exact group.
            member: Explicitly released member UID.
            operation: Accepted unmute identity, never replayed against a newer mute.
        """
        if (str(adapter.account), group) not in self.states:
            return
        await asyncio.to_thread(
            RaidStore(adapter.config["id"]).release,
            str(adapter.account),
            group,
            member,
            operation=operation,
        )
        await self.reload(adapter, group)

    async def close(self):
        """Revoke workers and previews before the owning plugin shuts down."""
        self.active = False
        self.previews.clear()
        draining = set(self.tasks.values()) | self.submissions
        for task in draining:
            if task not in self.submissions:
                task.cancel()
        if draining:
            await asyncio.gather(*draining, return_exceptions=True)
        self.tasks.clear()
        self.join_cursors.clear()
        self.windows.clear()
