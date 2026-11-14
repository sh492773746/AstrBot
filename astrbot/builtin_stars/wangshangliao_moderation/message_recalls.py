"""Preview-bound, serial manual recall of persisted flood windows."""

import asyncio
import hashlib
import json
import secrets
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from astrbot.api import logger
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action

from .flood import FloodWindows
from .member_whitelist import member_name
from .open_store import OpenError

MAX_SCAN = 3000
MAX_MESSAGES = 500
MAX_SELECTION_BYTES = 16 * 1024 * 1024
PACE_SECONDS = 3.0
PACE_JITTER_SECONDS = 2.0


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,9}$")


class Read(Scope):
    operation: Literal["read"]


class Search(Scope):
    operation: Literal["search"]
    query: str = Field(min_length=1, max_length=80)


class Preview(Scope):
    operation: Literal["preview"]
    version: int = Field(ge=1)
    member: str = Field(pattern=r"^[1-9][0-9]{0,9}$")
    minutes: Literal[5, 15, 30, 60]
    end_at: float | None = Field(default=None, gt=0)


class Commit(Scope):
    operation: Literal["commit"]
    confirmation: str = Field(min_length=16, max_length=128)


class Cancel(Scope):
    operation: Literal["cancel"]
    job: str = Field(pattern=r"^[a-f0-9]{32}$")


REQUEST = TypeAdapter(
    Annotated[
        Read | Search | Preview | Commit | Cancel, Field(discriminator="operation")
    ]
)


def fingerprint(payload):
    """Bind a preview to the exact persisted envelope.

    Args:
        payload: Authenticated stored message envelope.

    Returns:
        Stable SHA-256 digest, without copying message content into the audit.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MessageRecalls:
    """Own bounded dashboard jobs without changing exemptions or sanctions."""

    def __init__(self, service):
        self.service = service
        self.tasks = {}
        self.cancelled = set()
        self.next_send_at = {}
        self.lock = asyncio.Lock()
        # A process restart never grants permission to replay an unfinished job.
        with service.store.connect(write=True) as db:
            db.execute(
                "UPDATE bulk_recalls SET state='interrupted',error='recall_interrupted',"
                "updated=? WHERE state IN ('queued','running')",
                (time.time(),),
            )
            for row in db.execute(
                "SELECT account,MAX(updated) AS updated FROM bulk_recalls GROUP BY account"
            ):
                remaining = min(
                    PACE_SECONDS + PACE_JITTER_SECONDS,
                    max(
                        0,
                        PACE_SECONDS
                        + PACE_JITTER_SECONDS
                        - (time.time() - row["updated"]),
                    ),
                )
                self.next_send_at[row["account"]] = time.monotonic() + remaining

    def snapshot(self, account, group, job=""):
        """Read bounded progress without exposing the stored authorization manifest.

        Args:
            account: Selected robot account.
            group: Selected business group.
            job: Optional exact job ID.

        Returns:
            Latest scope-bound job, or None.
        """
        with self.service.store.connect() as db:
            row = db.execute(
                "SELECT * FROM bulk_recalls WHERE account=? AND group_id=? "
                + ("AND id=? " if job else "")
                + "ORDER BY created DESC LIMIT 1",
                (account, group, job) if job else (account, group),
            ).fetchone()
        if row is None:
            return None
        manifest, results = json.loads(row["manifest"]), json.loads(row["results"])
        # An interrupted submission is ambiguous even if its final receipt was lost.
        results = {
            mid: "unknown"
            if status == "submitting" and row["state"] not in {"queued", "running"}
            else status
            for mid, status in results.items()
        }
        return {
            "id": row["id"],
            "state": row["state"],
            "error": row["error"],
            "member": manifest["member"],
            "name": manifest["name"],
            "departed": manifest.get("departed", False),
            "total": len(manifest["messages"]),
            "counts": {
                state: sum(status == state for status in results.values())
                for state in (
                    "accepted",
                    "verified",
                    "skipped",
                    "unknown",
                    "rejected",
                    "submitting",
                )
            },
            "remaining": len(manifest["messages"])
            - sum(status != "pending" for status in results.values()),
            "updated": row["updated"],
        }

    async def receipt(self, adapter, group, mid):
        """Check both local submissions and independently observed recalls.

        Args:
            adapter: Frozen native receiver.
            group: Selected business group.
            mid: Stored server message ID.

        Returns:
            Existing state, or an empty string if never submitted.
        """
        ledger = adapter.ledger
        async with ledger.db.execute(
            "SELECT 1 FROM observed_recalls WHERE account=? AND group_id=? "
            "AND message_id=? LIMIT 1",
            (adapter.account, group, mid),
        ) as cursor:
            if await cursor.fetchone():
                return "verified"
        result = await ledger.receipt(f"recall/{adapter.account}/{group}/{mid}")
        if result:
            return result.get("status", "unknown")

        def stored():
            with self.service.store.connect() as db:
                row = db.execute(
                    "SELECT state FROM bulk_recall_receipts "
                    "WHERE account=? AND group_id=? AND message=?",
                    (adapter.account, group, mid),
                ).fetchone()
            return row["state"] if row else ""

        return await asyncio.to_thread(stored)

    @staticmethod
    def delegated_candidates(rows, member, peer, start, end):
        """Select exact messages for one accepted delegated mute, newest first.

        Args:
            rows: Bounded native inbox rows from this account and group.
            member: Verified target business UID.
            peer: Verified original transport identity.
            start: Inclusive start of the thirty-minute window.
            end: Frozen command timestamp, never extended while processing.

        Returns:
            Native message fingerprints without copying their bodies.

        Raises:
            OpenError: If more than the safe batch limit would be selected.
        """
        selected = {}
        for mid, raw, received in rows:
            try:
                payload = json.loads(raw)
                route = payload.get("recall_route", {})
                stamp = route.get("time", "")
                kind = payload.get("kind") or "text"
                if (
                    not isinstance(mid, str)
                    or not mid.isascii()
                    or not mid.isdigit()
                    or not 0 < int(mid) < 1 << 64
                    or not start <= received <= end
                    or payload.get("sender") != member
                    or kind
                    not in {
                        "text",
                        "image",
                        "sticker",
                        "video",
                        "audio",
                        "file",
                        "forward",
                    }
                    or payload.get("_test_rule_window")
                    or not isinstance(route.get("client"), str)
                    or not 1 <= len(route["client"]) <= 256
                    or route.get("peer") != peer
                    or not isinstance(stamp, str)
                    or not stamp.isascii()
                    or not stamp.isdigit()
                    or not start <= int(stamp) / 1000 <= end
                ):
                    continue
                selected[mid] = {
                    "id": mid,
                    "received_at": received,
                    "kind": kind,
                    "fingerprint": fingerprint(payload),
                }
            except (ValueError, TypeError, AttributeError):
                continue
        if len(selected) > MAX_MESSAGES:
            raise OpenError("recall_selection_limit")
        return sorted(
            selected.values(),
            key=lambda item: (item["received_at"], item["id"]),
            reverse=True,
        )

    async def delegate(
        self, adapter, group, member, peer, operation, actor, end, authorize
    ):
        """Queue a fixed-window recall only after a delegated mute was accepted.

        Args:
            adapter: Original native receiver.
            group: Exact authorized group.
            member: Verified ordinary target UID.
            peer: Target's original transport identity.
            operation: Durable originating mute operation.
            actor: Verified command sender UID.
            end: Frozen command timestamp in seconds.
            authorize: Trusted callback rechecking delegation and target protection.

        Returns:
            Existing or newly persisted job progress, or None for no candidates.

        Raises:
            OpenError: On an active job, stale authority or excessive selection.
        """
        account = str(adapter.account)
        job = hashlib.sha256(operation.encode()).hexdigest()[:32]
        async with self.lock:
            current = await asyncio.to_thread(self.snapshot, account, group, job)
            if current is not None:
                return current
            if account in self.tasks:
                raise OpenError("recall_job_busy")
            await authorize()
            now = time.time()
            if not now - 1800 <= end <= now + 2:
                raise OpenError("recall_time_range_invalid")
            end = min(end, now)
            ledger = adapter.ledger
            async with ledger.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM "
                "(SELECT payload FROM inbox WHERE account=? AND team=? AND sender=? "
                "AND received_at BETWEEN ? AND ? LIMIT ?)",
                (account, group, member, end - 1800, end, MAX_SCAN + 1),
            ) as cursor:
                count, size = await cursor.fetchone()
            if count > MAX_SCAN or size > MAX_SELECTION_BYTES:
                raise OpenError("recall_selection_limit")
            async with ledger.db.execute(
                "SELECT message,payload,received_at FROM inbox "
                "WHERE account=? AND team=? AND sender=? "
                "AND received_at BETWEEN ? AND ? "
                "ORDER BY received_at DESC,message DESC LIMIT ?",
                (account, group, member, end - 1800, end, MAX_SCAN + 1),
            ) as cursor:
                rows = await cursor.fetchall()
            if len(rows) > MAX_SCAN:
                raise OpenError("recall_selection_limit")
            messages = await asyncio.to_thread(
                self.delegated_candidates, rows, member, peer, end - 1800, end
            )
            if not messages:
                return None
            await authorize()
            if adapter.ledger is not ledger:
                raise OpenError("operation_revoked")
            tenant = dict(self.service.tenants[(account, group)])
            manifest = {
                "tenant": tenant,
                "member": member,
                "peer": peer,
                "name": member_name(
                    self.service.member_evidence.get((account, group), {}).get(
                        member, {}
                    )
                ),
                "messages": messages,
                "settings_version": self.service.settings["version"],
                "group_revision": group_revision(adapter.config, group),
                "epoch": adapter.config.get("_business_epoch"),
                "revision": adapter.config.get("_business_revision"),
                "instance": adapter.config["id"],
                "delegate": actor,
                "mute_operation": operation,
                "window_start": end - 1800,
                "window_end": end,
            }
            principal = f"delegate/{actor}"

            def reserve():
                with self.service.store.connect(write=True) as db:
                    db.execute(
                        "INSERT INTO bulk_recalls VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            job,
                            account,
                            group,
                            principal,
                            time.time(),
                            time.time(),
                            "queued",
                            json.dumps(manifest),
                            json.dumps({item["id"]: "pending" for item in messages}),
                            "",
                        ),
                    )
                    self.service.store.log(
                        db,
                        principal,
                        account,
                        group,
                        "delegated_recall_queued",
                        {
                            "job": job,
                            "member": member,
                            "count": len(messages),
                            "minutes": 30,
                            "mute_operation": operation,
                        },
                    )

            reserving = asyncio.create_task(asyncio.to_thread(reserve))
            try:
                await asyncio.shield(reserving)
            except asyncio.CancelledError:
                await reserving
                await asyncio.to_thread(
                    self.save,
                    job,
                    "interrupted",
                    {item["id"]: "pending" for item in messages},
                    "recall_interrupted",
                )
                raise
            task = asyncio.create_task(
                self.run(
                    adapter,
                    job,
                    principal,
                    manifest,
                    None,
                    None,
                    delegated_authorize=authorize,
                )
            )
            self.tasks[account] = task

            def finished(done):
                if self.tasks.get(account) is done:
                    self.tasks.pop(account, None)
                if not done.cancelled() and done.exception() is not None:
                    logger.error(
                        "Delegated recall job failed before receipt persistence"
                    )

            task.add_done_callback(finished)
        return await asyncio.to_thread(self.snapshot, account, group, job)

    async def select(self, adapter, group, member, peer, minutes, end_at=None):
        """Select only exact messages belonging to authenticated flood windows.

        Args:
            adapter: Frozen native receiver and ledger.
            group: Exact business group.
            member: Verified ordinary member UID.
            peer: Verified native peer ID.
            minutes: Bounded lookback interval.
            end_at: Optional historical end time within the last 24 hours.

        Returns:
            Exact candidate manifest and number already recalled.

        Raises:
            OpenError: If selection is too large or a prior result is ambiguous.
        """
        now = time.time()
        end = now if end_at is None else end_at
        if not now - 86400 + minutes * 60 <= end <= now + 2:
            raise OpenError("recall_time_range_invalid")
        start = end - minutes * 60
        async with adapter.ledger.db.execute(
            "SELECT message,payload,received_at FROM inbox "
            "WHERE account=? AND team=? AND received_at BETWEEN ? AND ? "
            "AND sender=? ORDER BY received_at,message LIMIT ?",
            (adapter.account, group, start - 30, end, member, MAX_SCAN + 1),
        ) as cursor:
            rows = await cursor.fetchall()
        if len(rows) > MAX_SCAN:
            raise OpenError("recall_selection_limit")
        candidates = await asyncio.to_thread(
            self.candidates, rows, adapter.account, group, member, peer, start
        )
        remaining, skipped = [], 0
        for item in candidates:
            receipt = await self.receipt(adapter, group, item["id"])
            if receipt in {"accepted", "verified"}:
                skipped += 1
                continue
            if receipt:
                raise OpenError("recall_previous_unconfirmed")
            remaining.append(item)
        return remaining, skipped

    async def historical_members(self, adapter, group, query):
        """Resolve absent senders only through original inboxes and verified bindings.

        Args:
            adapter: Authenticated receiver owning the ledger.
            group: Exact business group.
            query: Explicit UID or stored name to search.

        Returns:
            Bounded historical member records, not current membership evidence.
        """
        async with adapter.ledger.db.execute(
            "SELECT sender,payload,MAX(received_at) FROM inbox "
            "WHERE account=? AND team=? AND received_at BETWEEN ? AND ? AND "
            "(sender=? OR instr(lower(CASE WHEN json_valid(payload) "
            "THEN json_extract(payload,'$.name') ELSE '' END),?)>0) "
            "GROUP BY sender ORDER BY MAX(received_at) DESC LIMIT 30",
            (
                adapter.account,
                group,
                time.time() - 86400,
                time.time() + 2,
                query,
                query.lower(),
            ),
        ) as cursor:
            rows = await cursor.fetchall()

        def resolve():
            result = {}
            with self.service.store.connect() as db:
                for uid, raw, _ in rows:
                    try:
                        payload = json.loads(raw)
                        peer = payload.get("recall_route", {}).get("peer")
                        if (
                            not isinstance(uid, str)
                            or not uid.isascii()
                            or not uid.isdigit()
                            or not 0 < int(uid) < 1 << 30
                            or uid == adapter.account
                            or is_managed_account(uid)
                            or payload.get("sender") != uid
                            or not isinstance(peer, str)
                            or not peer.isascii()
                            or not peer.isdigit()
                            or not 0 < int(peer) < 1 << 32
                            or not db.execute(
                                "SELECT 1 FROM actor_bindings WHERE account=? "
                                "AND uid=? AND peer=? AND conflicted=0",
                                (adapter.account, uid, peer),
                            ).fetchone()
                        ):
                            continue
                        result[uid] = {
                            "userId": uid,
                            "nimId": peer,
                            "userNick": payload.get("name") or "群成员",
                            "departed": True,
                        }
                    except (ValueError, TypeError, AttributeError):
                        continue
            return result

        return await asyncio.to_thread(resolve)

    def target_guard(self, adapter, group, member, peer, departed):
        """Keep a preview's current-or-departed identity fixed during execution.

        Args:
            adapter: Original receiver.
            group: Exact group.
            member: Verified UID.
            peer: Verified original native peer.
            departed: Whether the confirmed selection was absent from the roster.

        Raises:
            OpenError: On rejoin, promotion, conflicting binding or changed identity.
        """
        latest = self.service.member_evidence.get((adapter.account, group), {})
        if member == adapter.account or is_managed_account(member) or not peer:
            raise OpenError("recall_member_changed")
        if departed:
            with self.service.store.connect() as db:
                binding = db.execute(
                    "SELECT 1 FROM actor_bindings WHERE account=? AND uid=? "
                    "AND peer=? AND conflicted=0",
                    (adapter.account, member, peer),
                ).fetchone()
            if (
                not binding
                or member in latest
                or any(str(row.get("nimId", "")) == peer for row in latest.values())
            ):
                raise OpenError("recall_member_changed")
        elif (
            latest.get(member, {}).get("groupRole") != "GROUP_ROLE_MEMBER"
            or str(latest.get(member, {}).get("nimId", "")) != peer
        ):
            raise OpenError("recall_member_changed")

    @staticmethod
    def candidates(rows, account, group, member, peer, start):
        """Recheck historical windows off the message-processing event loop.

        Args:
            rows: Bounded, chronological durable inbox rows.
            account: Exact receiver account.
            group: Exact business group.
            member: Verified business UID.
            peer: Verified native peer.
            start: Requested lookback boundary.

        Returns:
            Exact chronological message fingerprints.

        Raises:
            OpenError: If a single selection exceeds the safe batch limit.
        """
        stored = {}
        windows = FloodWindows(max_members=1)
        proofs = {}
        for mid, raw, received in rows:
            try:
                payload = json.loads(raw)
                route = payload.get("recall_route", {})
                evidence = payload.get("_ingress_flood", {})
                if (
                    not isinstance(mid, str)
                    or not mid.isascii()
                    or not mid.isdigit()
                    or int(mid) <= 0
                    or payload.get("sender") != member
                    or payload.get("_test_rule_window")
                    or not isinstance(route, dict)
                    or not isinstance(route.get("client"), str)
                    or not route["client"]
                    or str(route.get("peer", "")) != peer
                    or not str(route.get("time", "")).isascii()
                    or not str(route.get("time", "")).isdigit()
                    or int(route["time"]) <= 0
                    or not isinstance(evidence, dict)
                    or not isinstance(evidence.get("revision"), str)
                ):
                    continue
                stored[mid] = (payload, received)
                proofs[mid] = windows.observe(
                    account,
                    group,
                    mid,
                    payload,
                    received,
                    revision=evidence.get("revision", ""),
                )
            except (ValueError, TypeError, AttributeError):
                continue
        selected = set()
        for mid, (payload, received) in stored.items():
            evidence = payload.get("_ingress_flood", {})
            messages = evidence.get("messages")
            if (
                received < start
                or evidence.get("live") is not True
                or evidence.get("category") != "message_flood"
                or not any(
                    type(evidence.get(key)) is int and evidence[key] >= minimum
                    for key, minimum in (("rapid", 5), ("count", 15), ("repeated", 5))
                )
                or (
                    evidence.get("account"),
                    evidence.get("group"),
                    evidence.get("sender"),
                    evidence.get("message"),
                    evidence.get("received_at"),
                )
                != (account, group, member, mid, received)
                or not evidence.get("revision")
                or not isinstance(messages, list)
                or not 1 <= len(messages) <= 256
                or any(not isinstance(message, str) for message in messages)
                or len(set(messages)) != len(messages)
                or mid not in messages
                or proofs[mid].get("category") != "message_flood"
                or set(messages) != set(proofs[mid].get("messages", []))
            ):
                continue
            if any(
                message not in stored
                or not received - 30 <= stored[message][1] <= received
                or stored[message][0].get("_ingress_flood", {}).get("revision")
                != evidence["revision"]
                for message in messages
            ):
                continue
            selected.update(
                message for message in messages if stored[message][1] >= start
            )
        if len(selected) > MAX_MESSAGES:
            raise OpenError("recall_selection_limit")
        candidates = []
        for mid in sorted(selected, key=lambda message: (stored[message][1], message)):
            payload, received = stored[mid]
            candidates.append(
                {
                    "id": mid,
                    "received_at": received,
                    "kind": str(payload.get("kind", ""))[:32],
                    "fingerprint": fingerprint(payload),
                }
            )
        return candidates

    async def handle(self, rpc, call):
        """Handle only authenticated, scope-bound dashboard operations.

        Args:
            rpc: Existing allowlisted control plane.
            call: Server-authenticated dashboard request.

        Returns:
            Search, preview, or durable job progress.

        Raises:
            OpenError: On stale authority, conflicting work or invalid confirmation.
        """
        if not call.actor.startswith("dashboard/"):
            raise OpenError("server_required")
        request = REQUEST.validate_python(call.payload)
        account, group = request.account, request.group_id
        if any(int(uid) >= 1 << 30 for uid in (account, group)):
            raise OpenError("tenant_invalid")
        service = self.service
        tenant = await asyncio.to_thread(service.store.tenant, account, group)
        if not tenant:
            raise OpenError("tenant_changed")
        # Progress and stop remain available after a pause or account disconnect.
        if request.operation in {"read", "cancel"}:
            current = await asyncio.to_thread(
                self.snapshot, account, group, getattr(request, "job", "")
            )
            if request.operation == "cancel":
                if current is None:
                    raise OpenError("recall_job_missing")
                if current["state"] in {"queued", "running"}:
                    self.cancelled.add(request.job)
            return {"version": tenant["version"], "job": current}
        adapter = next((a for a in service.adapters() if a.account == account), None)
        if adapter is None or adapter.connection_state != "online":
            raise OpenError("account_unavailable")
        frozen = service.write_guard(adapter)
        roster = await service.roster(adapter, group)
        frozen()
        authority = rpc.control_guard(adapter, tenant, {account: roster[account]})

        def guard(member="", peer="", departed=False):
            frozen()
            authority()
            current = service.tenants.get((account, group), {})
            if service.settings["paused"] or current.get("paused"):
                raise OpenError("service_paused")
            authorize_action(adapter.config, group, "recall")
            if member:
                self.target_guard(adapter, group, member, peer, departed)

        guard()
        if request.operation == "search":
            query = request.query.strip().casefold()
            if not query:
                raise OpenError("recall_member_required")
            historical = await self.historical_members(adapter, group, query)
            guard()
            rows = {**historical, **roster}
            peers = {str(row.get("nimId", "")) for row in roster.values()}
            return {
                "version": tenant["version"],
                "members": [
                    {
                        "id": uid,
                        "name": member_name(row),
                        "departed": row.get("departed", False),
                    }
                    for uid, row in rows.items()
                    if (
                        row.get("groupRole") == "GROUP_ROLE_MEMBER"
                        or row.get("departed") is True
                        and str(row.get("nimId", "")) not in peers
                    )
                    and uid != account
                    and not is_managed_account(uid)
                    and (uid == query or query in member_name(row).casefold())
                ][:30],
            }
        if request.operation == "preview":
            if request.version != tenant["version"]:
                raise OpenError("tenant_changed")
            member = request.member
            departed = member not in roster
            target = roster.get(member, {})
            if departed:
                historical = await self.historical_members(adapter, group, member)
                target = historical.get(member, {})
            peer = str(target.get("nimId", ""))
            guard(member, peer, departed)
            messages, skipped = await self.select(
                adapter, group, member, peer, request.minutes, request.end_at
            )
            guard(member, peer, departed)
            if not messages:
                raise OpenError("recall_no_flood_messages")
            manifest = {
                "tenant": tenant,
                "member": member,
                "peer": peer,
                "name": member_name(target),
                "departed": departed,
                "messages": messages,
                "settings_version": service.settings["version"],
                "group_revision": group_revision(adapter.config, group),
                "epoch": adapter.config.get("_business_epoch"),
                "revision": adapter.config.get("_business_revision"),
                "instance": adapter.config["id"],
            }
            token = secrets.token_urlsafe(24)

            def reserve():
                with service.store.connect(
                    write=True, guard=lambda: guard(member, peer, departed)
                ) as db:
                    db.execute(
                        "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                        (
                            hashlib.sha256(token.encode()).hexdigest(),
                            "bulk_recall",
                            account,
                            group,
                            tenant["epoch"],
                            tenant["version"],
                            call.actor,
                            "",
                            json.dumps(manifest),
                            "",
                            "",
                            time.time() + 300,
                        ),
                    )

            await asyncio.to_thread(reserve)
            return {
                "confirmation": token,
                "expires_in": 300,
                "member": member,
                "name": manifest["name"],
                "departed": departed,
                "messages": [
                    {key: item[key] for key in ("id", "received_at", "kind")}
                    for item in messages
                ],
                "skipped": skipped,
            }

        async with self.lock:
            if account in self.tasks:
                raise OpenError("recall_job_busy")
            digest = hashlib.sha256(request.confirmation.encode()).hexdigest()
            job = secrets.token_hex(16)

            def commit():
                with service.store.connect(write=True, guard=guard) as db:
                    preview = db.execute(
                        "SELECT * FROM capabilities WHERE digest=?", (digest,)
                    ).fetchone()
                    if (
                        not preview
                        or preview["used"]
                        or preview["expires"] <= time.time()
                        or (
                            preview["kind"],
                            preview["account"],
                            preview["group_id"],
                            preview["actor"],
                            preview["version"],
                            preview["epoch"],
                        )
                        != (
                            "bulk_recall",
                            account,
                            group,
                            call.actor,
                            tenant["version"],
                            tenant["epoch"],
                        )
                    ):
                        raise OpenError("confirmation_invalid")
                    manifest = json.loads(preview["target"])
                    guard(
                        manifest["member"],
                        manifest["peer"],
                        manifest.get("departed", False),
                    )
                    if (
                        manifest["tenant"] != tenant
                        or manifest["settings_version"] != service.settings["version"]
                        or manifest["group_revision"]
                        != group_revision(adapter.config, group)
                        or manifest["epoch"] != adapter.config.get("_business_epoch")
                        or manifest["revision"]
                        != adapter.config.get("_business_revision")
                        or manifest["instance"] != adapter.config["id"]
                    ):
                        raise OpenError("tenant_changed")
                    db.execute(
                        "UPDATE capabilities SET used=1 WHERE digest=?", (digest,)
                    )
                    db.execute(
                        "INSERT INTO bulk_recalls VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            job,
                            account,
                            group,
                            call.actor,
                            time.time(),
                            time.time(),
                            "queued",
                            json.dumps(manifest),
                            json.dumps(
                                {item["id"]: "pending" for item in manifest["messages"]}
                            ),
                            "",
                        ),
                    )
                    service.store.log(
                        db,
                        call.actor,
                        account,
                        group,
                        "bulk_recall_confirmed",
                        {
                            "job": job,
                            "member": manifest["member"],
                            "count": len(manifest["messages"]),
                        },
                    )
                    return manifest

            reserving = asyncio.create_task(asyncio.to_thread(commit))
            try:
                manifest = await asyncio.shield(reserving)
            except asyncio.CancelledError:
                try:
                    await reserving
                except Exception:
                    pass
                else:
                    await asyncio.to_thread(
                        self.save,
                        job,
                        "interrupted",
                        {
                            item["id"]: "pending"
                            for item in reserving.result()["messages"]
                        },
                        "recall_interrupted",
                    )
                raise

            task = asyncio.create_task(
                self.run(
                    adapter,
                    job,
                    call.actor,
                    manifest,
                    rpc,
                    roster,
                )
            )
            self.tasks[account] = task

            def finished(done):
                self.tasks.pop(account, None)
                if not done.cancelled() and done.exception() is not None:
                    logger.error("Bulk recall job failed before receipt persistence")

            task.add_done_callback(finished)
        return {"job": await asyncio.to_thread(self.snapshot, account, group, job)}

    def save(self, job, state, results, error=""):
        """Durably record intent before I/O and each independent receipt.

        Args:
            job: Exact previously confirmed job.
            state: Job lifecycle state.
            results: Per-message states.
            error: Sanitized failure code.
        """
        with self.service.store.connect(write=True) as db:
            row = db.execute(
                "SELECT account,group_id,results FROM bulk_recalls WHERE id=?", (job,)
            ).fetchone()
            if row is None:
                raise OpenError("recall_job_missing")
            previous = json.loads(row["results"])
            for mid, receipt in results.items():
                if receipt == previous.get(mid) or receipt in {"pending", "skipped"}:
                    continue
                existing = db.execute(
                    "SELECT job FROM bulk_recall_receipts "
                    "WHERE account=? AND group_id=? AND message=?",
                    (row["account"], row["group_id"], mid),
                ).fetchone()
                if existing is not None and existing["job"] != job:
                    raise OpenError("recall_previous_unconfirmed")
                db.execute(
                    "INSERT INTO bulk_recall_receipts VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(account,group_id,message) DO UPDATE SET "
                    "state=excluded.state,updated=excluded.updated",
                    (row["account"], row["group_id"], mid, job, receipt, time.time()),
                )
            db.execute(
                "UPDATE bulk_recalls SET state=?,results=?,error=?,updated=? WHERE id=?",
                (state, json.dumps(results), error, time.time(), job),
            )

    async def run(
        self, adapter, job, actor, manifest, rpc, roster, *, delegated_authorize=None
    ):
        """Give owned background work fresh leases, not the expired HTTP lease.

        Args:
            adapter: Original native receiver.
            job: Durable job ID.
            actor: Authenticated dashboard principal.
            manifest: Confirmed immutable selection.
            rpc: Existing allowlisted control plane.
            roster: Exact member bindings from confirmation.
            delegated_authorize: Optional originating command's fixed target guard.
        """
        from astrbot.core.star.component_runtime import current_execution

        token = current_execution.set(None)
        try:
            await self.execute(
                adapter,
                job,
                actor,
                manifest,
                rpc,
                roster,
                delegated_authorize=delegated_authorize,
            )
        finally:
            current_execution.reset(token)

    async def execute(
        self, adapter, job, actor, manifest, rpc, roster, *, delegated_authorize=None
    ):
        """Execute a bounded job serially, stopping on any ambiguous receipt.

        Args:
            adapter: Original native receiver.
            job: Confirmed durable job identifier.
            actor: Authenticated dashboard principal.
            manifest: Immutable preview content.
            rpc: Existing control plane for fresh per-job authority.
            roster: Exact member bindings authorized by the preview.
            delegated_authorize: Fresh command grant check for a delegated job only.
        """
        group = manifest["tenant"]["group_id"]
        results = {item["id"]: "pending" for item in manifest["messages"]}
        state, error, current = "completed", "", ""
        ledger = adapter.ledger
        try:
            authority = (
                self.service.write_guard(adapter)
                if delegated_authorize is not None
                else rpc.control_guard(
                    adapter,
                    manifest["tenant"],
                    {adapter.account: roster[adapter.account]},
                )
            )

            def guard():
                authority()
                authorize_action(adapter.config, group, "recall")
                self.target_guard(
                    adapter,
                    group,
                    manifest["member"],
                    manifest["peer"],
                    manifest.get("departed", False),
                )
                if (
                    self.service.settings["paused"]
                    or self.service.tenants.get((adapter.account, group), {}).get(
                        "paused"
                    )
                    or adapter.connection_state != "online"
                    or manifest["settings_version"] != self.service.settings["version"]
                    or manifest["group_revision"]
                    != group_revision(adapter.config, group)
                    or manifest["epoch"] != adapter.config.get("_business_epoch")
                    or manifest["revision"] != adapter.config.get("_business_revision")
                    or manifest["instance"] != adapter.config["id"]
                ):
                    raise OpenError("operation_revoked")

            for item in manifest["messages"]:
                delay = self.next_send_at.get(adapter.account, 0) - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                if job in self.cancelled:
                    state = "cancelled"
                    break
                guard()
                if adapter.ledger is not ledger:
                    raise OpenError("operation_revoked")
                await self.service.roster(adapter, group)
                guard()
                mid = item["id"]

                async def authorize():
                    guard()
                    if job in self.cancelled or adapter.ledger is not ledger:
                        raise OpenError("operation_revoked")
                    if delegated_authorize is not None:
                        await delegated_authorize()
                        guard()
                    async with ledger.db.execute(
                        "SELECT payload FROM inbox "
                        "WHERE account=? AND team=? AND message=?",
                        (adapter.account, group, mid),
                    ) as cursor:
                        row = await cursor.fetchone()
                    guard()
                    if (
                        not row
                        or fingerprint(json.loads(row[0])) != item["fingerprint"]
                    ):
                        raise OpenError("recall_evidence_changed")

                await authorize()
                receipt = await self.receipt(adapter, group, mid)
                if receipt in {"accepted", "verified"}:
                    results[mid] = "skipped"
                elif receipt:
                    raise OpenError("recall_previous_unconfirmed")
                else:
                    current = mid
                    results[mid] = "submitting"
                    await asyncio.to_thread(self.save, job, "running", results)
                    await authorize()

                    async def submit():
                        return await adapter.execute_action(
                            ActionRequest.bind(
                                adapter,
                                f"recall/{adapter.account}/{group}/{mid}",
                                "recall",
                                int(group),
                                member=int(manifest["member"]),
                                expected_nim=manifest["peer"],
                                message=mid,
                                recall_departed=manifest.get("departed", False),
                            ),
                            authorize=authorize,
                        )

                    try:
                        result = await asyncio.wait_for(
                            self.service.governance.run(
                                "control",
                                adapter,
                                submit,
                                group=group,
                                guard=guard,
                            ),
                            timeout=30,
                        )
                    finally:
                        # Pace from the receipt, including uncertain failures.
                        self.next_send_at[adapter.account] = (
                            time.monotonic()
                            + PACE_SECONDS
                            + secrets.randbelow(round(PACE_JITTER_SECONDS * 1000) + 1)
                            / 1000
                        )
                    status = result.get("status", "unknown")
                    results[mid] = (
                        status
                        if status in {"accepted", "verified", "rejected"}
                        else "unknown"
                    )
                    current = ""
                    if results[mid] in {"unknown", "rejected"}:
                        state, error = "stopped", "recall_receipt_unconfirmed"
                await asyncio.to_thread(self.save, job, "running", results)
                if state == "stopped":
                    break
        except asyncio.CancelledError:
            state, error = "interrupted", "recall_interrupted"
        except Exception:
            state, error = "stopped", "recall_authority_or_transport_changed"
        finally:
            if current:
                results[current] = "unknown"
            await asyncio.to_thread(self.save, job, state, results, error)
            self.cancelled.discard(job)

            def audit():
                with self.service.store.connect(write=True) as db:
                    self.service.store.log(
                        db,
                        actor,
                        adapter.account,
                        group,
                        "bulk_recall_finished",
                        {
                            "job": job,
                            "state": state,
                            "error": error,
                            "results": results,
                        },
                    )

            await asyncio.to_thread(audit)

    async def close(self):
        """Stop workers without retrying uncertain in-flight actions."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self.service.store.connect(write=True) as db:
            db.execute(
                "UPDATE bulk_recalls SET state='interrupted',error='recall_interrupted',"
                "updated=? WHERE state IN ('queued','running')",
                (time.time(),),
            )
