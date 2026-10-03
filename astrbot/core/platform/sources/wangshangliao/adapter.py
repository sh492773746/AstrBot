"""Native Wangshangliao group text adapter; no external gateway."""

import asyncio
import json
import secrets
import sqlite3
import time
import uuid
from collections import deque

import aiohttp

from astrbot.api.message_components import At, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.platform import PlatformStatus

from . import wire
from .business import BusinessClient, BusinessError, Deployment
from .diagnostics import Diagnostics
from .directory import member_directory
from .event import WangshangliaoEvent, is_managed_account, mentioned
from .nim import NimClient, messages
from .storage import Ledger, Vault, instance_dir
from .text import plain_text, redact_reply

ACTIVE_ACCOUNTS: set[str] = set()


def message_timestamp(inner, outer: dict) -> int:
    """Prefer the NIM message time; mobile payloads may omit their inner time."""
    stamp = outer.get(7, "")
    if (
        isinstance(stamp, str)
        and stamp.isascii()
        and stamp.isdigit()
        and len(stamp) <= 19
        and 0 < int(stamp) < 1 << 63
    ):
        return int(stamp)
    return inner.created_at


@register_platform_adapter("wangshangliao", "旺商聊", support_streaming_message=False)
class WangshangliaoAdapter(Platform):
    """Own one account's transport, ledger and sequential group workers."""

    supports_operation_ids = True

    def __init__(self, platform_config, platform_settings, event_queue):
        super().__init__(platform_config, event_queue)
        self.diagnostics = Diagnostics(platform_config["id"])
        self.account = str(platform_config.get("account_id", ""))
        self.enabled_groups = set(platform_config.get("enabled_groups", []))
        self.vault = Vault(platform_config["id"])
        self.ledger = Ledger(instance_dir(platform_config["id"]) / "messages.sqlite3")
        self.stopping = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.nim = None
        self.business = None
        self.http = None
        self.nim_account = ""
        self.groups = {}
        self.group_names = {}
        self.members = {}
        self.group_directory_state = {}
        self.member_refresh_locks = {}
        self.member_refresh_at = {}
        self.invitation_read_lock = asyncio.Lock()
        self.invitation_read_at = {}
        self.workers = {}
        self.dispatch_wakeup = asyncio.Event()
        self.dispatch_task = None
        self.maintenance_task = None
        self.reply_recall_task = None
        self.events = set()
        self.connection_state = "stopped"
        self.runner = None
        self.heartbeat_task = None
        self.unsupported_pushes = 0
        self.current_error = ""
        self.last_connected_at = None
        self.next_retry_at = None
        self.test_window = None

    def meta(self) -> PlatformMetadata:
        """Expose the saved instance ID, never an account-global platform ID."""
        return PlatformMetadata(
            "wangshangliao",
            "旺商聊",
            self.config["id"],
            support_streaming_message=False,
            support_proactive_message=True,
        )

    def get_stats(self) -> dict:
        """Expose authentication and connection state separately."""
        return {
            **super().get_stats(),
            "status": {
                "online": "running",
                "rate_limited": "pending",
                "account_conflict": "error",
                "reconnecting": "pending",
                "reauth_required": "error",
                "error": "error",
                "stopped": "stopped",
            }[self.connection_state],
            "connection_state": self.connection_state,
            "unsupported_pushes": self.unsupported_pushes,
            "current_error": self.current_error,
            "last_connected_at": self.last_connected_at,
            "next_retry_at": self.next_retry_at,
            "group_directory_state": dict(self.group_directory_state),
        }

    async def run(self) -> None:
        """Restore a saved session and reconnect transport without password retries."""
        self.runner = asyncio.current_task()
        claimed = False
        try:
            if not self.account or self.account in ACTIVE_ACCOUNTS:
                raise wire.ProtocolError("account_already_active")
            ACTIVE_ACCOUNTS.add(self.account)
            claimed = True
            deployment = Deployment.load()
            if not deployment.message_key.get_secret_value():
                raise wire.ProtocolError("message_key_missing")
            saved = self.vault.load()
            if not saved or str(saved["business"]["uid"]) != self.account:
                raise wire.ProtocolError("reauth_required")
            self.http = aiohttp.ClientSession()
            self.business = BusinessClient(deployment, self.http)
            self.business.restore(saved["business"])
            self.diagnostics.emit("session", "restored")
            self.nim_account = saved["nim_id"]
            await self.ledger.open()
            self.dispatch_task = asyncio.create_task(self.dispatch_pending())
            self.maintenance_task = asyncio.create_task(self.maintain_ledger())
            from .reply_recall import run as recall_replies

            self.reply_recall_task = asyncio.create_task(recall_replies(self))
            delay = 1
            while not self.stopping.is_set():
                self.connection_state = "reconnecting"
                self.next_retry_at = None
                retry_wait = min(30, delay * (1 + secrets.randbelow(251) / 1000))
                try:
                    refreshed = await self.business.request("/v1/user/RefreshToken", {})
                    token = refreshed.get("nimToken")
                    if not isinstance(token, str) or not token:
                        raise wire.ProtocolError("refresh_shape")
                    saved["nim_token"] = token
                    self.vault.save(saved)
                    await self.load_groups()
                    self.nim = NimClient(self.http)
                    await self.nim.connect(deployment, saved["nim_id"], token)
                    async with self.ledger.db.execute(
                        "SELECT cursor FROM sync WHERE account=?", (self.account,)
                    ) as cursor:
                        row = await cursor.fetchone()
                    # Sync responses may precede queued pushes; persist their watermark only
                    # after the single push consumer has committed all earlier events.
                    code, body = await self.nim.request(
                        5,
                        1,
                        wire.properties(
                            [
                                (2, (row[0] if row else "0").encode()),
                                (7, (row[0] if row else "0").encode()),
                            ]
                        ),
                    )
                    if code != 200 or len(body) != 8:
                        raise wire.ProtocolError("sync_boundary")
                    self.nim.pushes.put_nowait((5, 1, 0, 200, body))
                    self.heartbeat_task = asyncio.create_task(self.keep_alive())
                    self.diagnostics.emit("connection", "online")
                    self.connection_state = "online"
                    self.current_error = ""
                    self.last_connected_at = time.time()
                    self.status = PlatformStatus.RUNNING
                    delay = 1
                    self.dispatch_wakeup.set()
                    while not self.stopping.is_set() and not self.nim.closed.is_set():
                        try:
                            packet = await asyncio.wait_for(self.nim.pushes.get(), 15)
                        except asyncio.TimeoutError:
                            continue
                        try:
                            incoming, boundary = messages(packet)
                        except wire.ProtocolError as exc:
                            # A malformed or unsupported push must not tear down a healthy
                            # socket; request/ACK traffic can continue after diagnostics.
                            if str(exc) in {
                                "message_fields",
                                "message_trailing",
                                "notification",
                                "message_count",
                            }:
                                self.unsupported_pushes += 1
                                self.diagnostics.emit(
                                    "receive",
                                    "unsupported",
                                    failed=True,
                                    error=str(exc),
                                )
                                continue
                            raise
                        if not incoming and boundary is None:
                            self.unsupported_pushes += 1
                        for message in incoming:
                            try:
                                await self.ingest(message)
                            except wire.ProtocolError as exc:
                                if str(exc) != "message_decode":
                                    raise
                                self.unsupported_pushes += 1
                                self.diagnostics.emit(
                                    "receive",
                                    "unsupported",
                                    message.get(12, ""),
                                    failed=True,
                                    error="message_decode",
                                )
                                await self.nim.acknowledge(int(message[0]), message[12])
                        if boundary is not None:
                            await self.ledger.db.execute(
                                "INSERT INTO sync VALUES(?,?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor",
                                (self.account, str(boundary)),
                            )
                            await self.ledger.db.commit()
                    if self.nim.closed.is_set():
                        raise wire.ProtocolError(self.nim.error)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    label = (
                        str(exc)
                        if isinstance(exc, wire.ProtocolError)
                        else "connection_failed"
                    )
                    self.current_error = label
                    if label in {"reauth_required", "account_conflict", "nim_kicked"}:
                        raise wire.ProtocolError(label) from None
                    self.diagnostics.emit(
                        "connection",
                        "rate_limited"
                        if label == "rate_limited"
                        else "retry_scheduled",
                        failed=True,
                        error=label,
                    )
                    self.connection_state = "reconnecting"
                    if isinstance(exc, BusinessError) and label == "rate_limited":
                        self.connection_state = "rate_limited"
                        retry_wait = max(retry_wait, exc.retry_after)
                finally:
                    if self.heartbeat_task:
                        self.heartbeat_task.cancel()
                        await asyncio.gather(
                            self.heartbeat_task, return_exceptions=True
                        )
                    if self.nim:
                        await self.nim.close()
                try:
                    self.next_retry_at = time.time() + retry_wait
                    await asyncio.wait_for(self.stopping.wait(), retry_wait)
                except asyncio.TimeoutError:
                    delay = min(delay * 2, 30)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.current_error = (
                str(exc) if isinstance(exc, wire.ProtocolError) else "startup_failed"
            )
            self.next_retry_at = None
            self.connection_state = (
                "reauth_required"
                if str(exc) == "reauth_required"
                else "account_conflict"
                if str(exc) == "account_conflict"
                else "error"
            )
            self.diagnostics.emit(
                "connection",
                self.connection_state,
                failed=True,
                terminal=True,
                error=self.current_error,
            )
            self.record_error(
                str(exc) if isinstance(exc, wire.ProtocolError) else "startup_failed"
            )
        finally:
            self.stopping.set()
            for task in (
                self.dispatch_task,
                self.maintenance_task,
                self.reply_recall_task,
            ):
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            pipeline_tasks = []
            for event in self.events:
                event.processing_completion.cancel()
                task = getattr(event, "processing_task", None)
                if isinstance(task, asyncio.Task):
                    task.cancel()
                    pipeline_tasks.append(task)
            await asyncio.gather(*pipeline_tasks, return_exceptions=True)
            for task in self.workers.values():
                task.cancel()
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
            if self.nim:
                await self.nim.close()
            if self.http:
                await self.http.close()
            await self.ledger.close()
            self.diagnostics.emit("connection", "stopped")
            if claimed:
                ACTIVE_ACCOUNTS.discard(self.account)

    async def keep_alive(self) -> None:
        """Maintain heartbeat independently of push traffic and model processing."""
        try:
            while not self.stopping.is_set():
                await asyncio.sleep(15)
                await self.nim.heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.nim.close()

    async def load_groups(self) -> None:
        """Load explicit business-to-NIM identities for enabled groups only."""
        data = await self.business.request("/v1/group/get-group-list", {"v": "0"})
        if not isinstance(data, dict) or any(
            not isinstance(data.get(key), list) for key in ("owner", "member")
        ):
            raise wire.ProtocolError("groups_shape")
        groups = {}
        group_names = {}
        for entry in data.get("owner", []) + data.get("member", []):
            group_id, team = str(entry["groupId"]), str(entry["groupCloudId"])
            if group_id in self.enabled_groups:
                if (
                    (group_id in groups and groups[group_id] != team)
                    or not team.isascii()
                    or not team.isdigit()
                ):
                    raise wire.ProtocolError("group_identity")
                groups[group_id] = team
                name = " ".join(
                    str(entry.get("groupName") or entry.get("name") or "").split()
                )[:128]
                if name:
                    group_names.setdefault(group_id, name)
        self.groups = groups
        self.group_names = group_names
        self.members = {}
        self.group_directory_state = {}
        for group_id in groups:
            try:
                await self.refresh_member_mapping(group_id)
            except (wire.ProtocolError, aiohttp.ClientError, TimeoutError) as exc:
                if str(exc) in {"reauth_required", "account_conflict"}:
                    raise
                self.diagnostics.emit(
                    "directory",
                    "unavailable",
                    group_id,
                    failed=True,
                    error="member_directory_unavailable",
                )

    async def refresh_member_mapping(
        self, group: str, *, bounded: bool = False
    ) -> dict | None:
        """Publish a complete roster atomically, coalescing ingress refreshes.

        Args:
            group: Enabled business group ID.
            bounded: Limit mismatch-triggered requests to one per thirty seconds.

        Returns:
            Validated roster, or None when a bounded refresh is throttled.

        Raises:
            ProtocolError: If the roster cannot establish consistent identities.
        """
        lock = self.member_refresh_locks.setdefault(group, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            if bounded and now - self.member_refresh_at.get(group, float("-inf")) < 30:
                return None
            self.member_refresh_at[group] = now
            try:
                page = await member_directory(self.business, group)
                mapping = {
                    row["userId"]: row["nimId"] for row in page["groupMemberInfo"]
                }
                if not page.get("complete") or (
                    self.nim_account
                    and self.account in mapping
                    and mapping[self.account] != self.nim_account
                ):
                    raise wire.ProtocolError("member_identity")
            except (wire.ProtocolError, aiohttp.ClientError, TimeoutError):
                self.group_directory_state[group] = {
                    "complete": False,
                    "error": "member_directory_unavailable",
                    "updated_at": time.time(),
                }
                raise
            self.members[group] = mapping
            self.group_directory_state[group] = {
                "complete": True,
                "error": "",
                "updated_at": time.time(),
            }
            return page

    async def ingest(self, outer: dict) -> None:
        """Authenticate custom group text, commit its inbox, then ACK its NIM ID."""
        if outer.get(8) == "100":
            try:
                wire.open_message(
                    self.business.deployment.key("message_key"), outer.get(10, "")
                )
            except wire.ProtocolError as exc:
                if str(exc) != "message_decode":
                    raise
                # Persist only routing metadata, never unverified content. This
                # prevents poison history from repeatedly reconnecting the socket.
                target = "unsupported/" + outer[0] + "/" + outer[1]
                await self.ledger.ingest(self.account, target, outer[12], {})
                await self.ledger.mark(self.account, target, outer[12], "unsupported")
                self.unsupported_pushes += 1
                self.diagnostics.emit(
                    "receive",
                    "unsupported",
                    outer[12],
                    failed=True,
                    error="message_decode",
                )
                await self.nim.acknowledge(int(outer[0]), outer[12])
                return
        self.diagnostics.emit("receive", "received", str(outer.get(12, "")))
        if outer[0] == "0":
            if (
                not self.nim_account
                or outer[1] != self.nim_account
                or outer[2] == self.nim_account
            ):
                return
            if outer[8] != "100":
                self.unsupported_pushes += 1
                await self.nim.acknowledge(0, outer[12])
                return
            inner = wire.open_message(
                self.business.deployment.key("message_key"), outer.get(10, "")
            )
            if (
                inner.session != 1
                or str(inner.target.id) != self.account
                or str(inner.sender.id) == self.account
            ):
                raise wire.ProtocolError("private_message_identity")
            # Keep both authenticated application identity and transport peer in the
            # session key. Never infer a NIM ID from a business user ID.
            peer = outer[2]
            if not peer or len(peer.encode()) > 1024:
                raise wire.ProtocolError("private_peer")
            session = f"private/{inner.sender.id}/{wire.b64(peer.encode())}"
            supported = inner.format == 0 and (
                inner.HasField("content") or inner.mentions.HasField("content")
            )
            payload = {
                "text": inner.content.data
                if inner.HasField("content")
                else inner.mentions.content.data,
                "sender": str(inner.sender.id),
                "name": inner.sender.name,
                "created_at": message_timestamp(inner, outer),
                "mentions": [],
            }
            await self.ledger.ingest(self.account, session, outer[12], payload)
            if not supported:
                async with self.ledger.db.execute(
                    "UPDATE inbox SET payload=? WHERE account=? AND team=? AND message=?",
                    (
                        json.dumps(
                            {"format": inner.format, "reason": "unsupported_format"}
                        ),
                        self.account,
                        session,
                        outer[12],
                    ),
                ):
                    pass
                await self.ledger.mark(self.account, session, outer[12], "unsupported")
            await self.nim.acknowledge(0, outer[12])
            self.dispatch_wakeup.set()
            return
        if outer[0] != "1":
            return
        group = next(
            (group for group, team in self.groups.items() if team == outer[1]), None
        )
        if group is None:
            return
        state = "unsupported"
        payload = {}
        if outer[8] == "100":
            inner = wire.open_message(
                self.business.deployment.key("message_key"), outer.get(10, "")
            )
            if (
                inner.session == 2
                and str(inner.target.id) == group
                and self.members.get(group, {}).get(str(inner.sender.id)) != outer[2]
            ):
                try:
                    await self.refresh_member_mapping(group, bounded=True)
                except (wire.ProtocolError, aiohttp.ClientError, TimeoutError):
                    pass
            if (
                inner.session != 2
                or str(inner.target.id) != group
                or self.members.get(group, {}).get(str(inner.sender.id)) != outer[2]
            ):
                # Removed members may still occur in reconnect history. Reject the
                # message without turning a stale mapping into a reconnect loop.
                await self.ledger.db.execute(
                    "INSERT OR IGNORE INTO inbox(account,team,message,payload,state,received_at,completed_at) VALUES(?,?,?,?,'rejected_identity',?,?)",
                    (self.account, group, outer[12], "{}", time.time(), time.time()),
                )
                await self.ledger.db.commit()
                self.diagnostics.emit(
                    "receive",
                    "rejected_identity",
                    outer[12],
                    failed=True,
                    error="message_identity",
                )
                await self.nim.acknowledge(1, outer[12])
                return
            if str(inner.sender.id) == self.account:
                state = "self"
            elif inner.format == 0 and (
                inner.HasField("content") or inner.mentions.HasField("content")
            ):
                state = "pending"
                payload = {
                    "text": inner.content.data
                    if inner.HasField("content")
                    else inner.mentions.content.data,
                    "sender": str(inner.sender.id),
                    "name": inner.sender.name,
                    "created_at": message_timestamp(inner, outer),
                    "mentions": [str(person.uid) for person in inner.mentions.people],
                    "mention_spans": [
                        {
                            "uid": str(person.uid),
                            "nick": person.nick,
                            "start": person.start,
                            "end": person.end,
                        }
                        for person in inner.mentions.people
                    ],
                    "recall_route": {
                        "client": outer.get(11, ""),
                        "time": outer.get(7, ""),
                        "peer": outer[2],
                    },
                }
        await self.ledger.ingest(self.account, group, outer[12], payload)
        if state != "pending":
            await self.ledger.mark(self.account, group, outer[12], state)
        await self.nim.acknowledge(1, outer[12])
        self.dispatch_wakeup.set()

    async def dispatch_pending(self) -> None:
        """Schedule durable sessions fairly without unbounded resident tasks."""
        ready = deque()
        while not self.stopping.is_set():
            self.dispatch_wakeup.clear()
            failed = False
            for team, task in list(self.workers.items()):
                if task.done():
                    if not task.cancelled() and task.exception():
                        self.record_error("dispatch_processing_failed")
                        self.diagnostics.emit(
                            "dispatch", "processing_failed", failed=True
                        )
                        failed = True
                    del self.workers[team]
            if failed:
                try:
                    await asyncio.wait_for(self.stopping.wait(), 1)
                    return
                except asyncio.TimeoutError:
                    pass
            try:
                if self.connection_state == "online":
                    excluded = list(ready) + list(self.workers)
                    groups = list(self.enabled_groups.intersection(self.groups))
                    slots = 1024 - len(ready)
                    if slots:
                        sql = (
                            "SELECT team FROM inbox WHERE account=? AND state='pending' "
                            "AND (team LIKE 'private/%' OR team IN ("
                            + ",".join("?" for _ in groups)
                            + "))"
                        )
                        args = [self.account, *groups]
                        if excluded:
                            sql += (
                                " AND team NOT IN ("
                                + ",".join("?" for _ in excluded)
                                + ")"
                            )
                            args.extend(excluded)
                        sql += " GROUP BY team ORDER BY MIN(rowid) LIMIT ?"
                        async with self.ledger.db.execute(
                            sql, [*args, slots]
                        ) as cursor:
                            ready.extend(row[0] for row in await cursor.fetchall())
                    while ready and len(self.workers) < 16:
                        team = ready.popleft()
                        task = asyncio.create_task(
                            self.process_group(team, single=True)
                        )
                        self.workers[team] = task
                        task.add_done_callback(lambda _: self.dispatch_wakeup.set())
            except Exception:
                self.record_error("dispatch_scan_failed")
                self.diagnostics.emit("dispatch", "scan_failed", failed=True)
            try:
                await asyncio.wait_for(self.dispatch_wakeup.wait(), 30)
            except asyncio.TimeoutError:
                pass

    async def maintain_ledger(self) -> None:
        """Clean eligible bodies only after an explicit healthy rollout approval."""
        while not self.stopping.is_set():
            try:
                if self.connection_state == "online":
                    while not self.stopping.is_set() and await self.ledger.prune():
                        try:
                            await asyncio.wait_for(self.stopping.wait(), 0.05)
                        except asyncio.TimeoutError:
                            pass
            except Exception:
                self.record_error("ledger_maintenance_failed")
                self.diagnostics.emit("maintenance", "prune_failed", failed=True)
            try:
                await asyncio.wait_for(self.stopping.wait(), 3600)
            except asyncio.TimeoutError:
                pass

    async def process_group(self, group: str, *, single: bool = False) -> None:
        """Await completion, including sends, before dispatching the next group event."""
        remaining = 1 if single else float("inf")
        while not self.stopping.is_set() and remaining > 0:
            remaining -= 1
            async with self.ledger.db.execute(
                "SELECT message,payload FROM inbox WHERE account=? AND team=? AND state='pending' ORDER BY rowid LIMIT 1",
                (self.account, group),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or self.connection_state != "online":
                return
            mid, encoded = row
            try:
                payload = json.loads(encoded)
                if (
                    isinstance(payload, dict)
                    and not self.test_window
                    and is_managed_account(str(payload.get("sender", "")))
                ):
                    await self.ledger.mark(self.account, group, mid, "ignored_bot")
                    self.diagnostics.emit("process", "ignored_bot", mid)
                    continue
                if (
                    not isinstance(payload, dict)
                    or not all(
                        key in payload
                        for key in ("sender", "name", "text", "created_at", "mentions")
                    )
                    or not isinstance(payload["text"], str)
                    or not isinstance(payload["created_at"], (int, float))
                ):
                    raise ValueError("invalid_inbox_payload")
            except (ValueError, TypeError):
                await self.ledger.mark(self.account, group, mid, "needs_review")
                self.diagnostics.emit("process", "invalid_payload", mid, failed=True)
                continue
            test_scope = (
                self.test_window.admit(group, mid, payload) if self.test_window else ""
            )
            if is_managed_account(str(payload.get("sender", ""))) and not test_scope:
                await self.ledger.mark(self.account, group, mid, "ignored_bot")
                self.diagnostics.emit("process", "ignored_bot", mid)
                continue
            await self.ledger.mark(self.account, group, mid, "processing")
            message = AstrBotMessage()
            private = group.startswith("private/")
            message.type = (
                MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
            )
            message.self_id = self.account
            message.session_id = f"{self.account}/{group}"
            message.group_id = "" if private else group
            message.message_id = mid
            message.sender = MessageMember(payload["sender"], payload["name"])
            message.message_str = payload["text"]
            message.timestamp = payload["created_at"] // 1000
            message.raw_message = {"message_id": mid}
            eligible = private or mentioned(
                payload["text"], str(self.nim_account or ""), payload["mentions"]
            )
            message.message = (
                [At(qq=self.account)] if eligible and not private else []
            ) + [Plain(payload["text"])]
            if not private:
                reverse = {}
                for business_id, peer in self.members.get(group, {}).items():
                    reverse.setdefault(str(peer), []).append(business_id)
                mapped = [At(qq=self.account)] if eligible else []
                for peer in dict.fromkeys(payload.get("mentions", [])):
                    if str(peer) == str(self.nim_account):
                        continue
                    identities = reverse.get(str(peer), [])
                    if len(identities) == 1:
                        mapped.append(At(qq=identities[0]))
                # Only a transport-verified leading self-mention may be removed.
                encoded_text = payload["text"].encode("utf-16-le")
                for span in payload.get("mention_spans", []):
                    end = span.get("end", 0)
                    if (
                        span.get("uid") == str(self.nim_account)
                        and span.get("start") == 0
                        and type(end) is int
                        and 0 < end * 2 <= len(encoded_text)
                    ):
                        prefix = encoded_text[: end * 2].decode(
                            "utf-16-le", errors="replace"
                        )
                        remainder = (
                            encoded_text[end * 2 :]
                            .decode("utf-16-le", errors="replace")
                            .lstrip()
                        )
                        if prefix.rstrip() == "@" + span.get("nick", ""):
                            message.message_str = remainder
                            break
                message.message = mapped + [Plain(message.message_str)]
            event = WangshangliaoEvent(message, self, eligible)
            event.set_extra("wangshangliao_payload", payload)
            event.set_extra("wsl_test_scope", test_scope)
            if test_scope == "group_rules":
                event.set_extra("_context_only", True)
            self.events.add(event)
            self.diagnostics.emit("process", "started", mid)
            try:
                await self._event_queue.put(event)
                await event.processing_completion
                await self.ledger.mark(self.account, group, mid, "processed")
                self.diagnostics.emit("process", "completed", mid)
            except asyncio.CancelledError:
                event.processing_completion.cancel()
                await self.ledger.mark(self.account, group, mid, "needs_review")
                raise
            except Exception:
                event.processing_completion.cancel()
                await self.ledger.mark(self.account, group, mid, "needs_review")
                self.record_error("event_requires_review")
                self.diagnostics.emit(
                    "process", "needs_review", mid, failed=True, terminal=True
                )
            finally:
                self.events.discard(event)

    async def get_moderation_members(self, group: str) -> dict:
        """Fetch authenticated members for moderation identity checks.

        Args:
            group: Enabled business group ID.

        Returns:
            Platform member directory.

        Raises:
            ProtocolError: If the group is not enabled or the connection is offline.
        """
        if self.connection_state != "online" or group not in self.config.get(
            "enabled_groups", []
        ):
            raise wire.ProtocolError("moderation_identity")
        return await self.refresh_member_mapping(group)

    async def get_moderation_result(self, operation: str, group: str) -> dict:
        """Read an instance-scoped operation without creating storage.

        Args:
            operation: Exact operation identifier.
            group: Expected business group ID.

        Returns:
            Matching saved result, or an empty dictionary if unavailable.
        """
        path = instance_dir(self.config["id"]) / "moderation.sqlite3"
        if not path.is_file():
            return {}
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
            ).fetchone():
                return {}
            row = db.execute(
                "SELECT result FROM operations WHERE id=?", (operation,)
            ).fetchone()
        result = json.loads(row[0]) if row else {}
        return result if str(result.get("group")) == str(group) else {}

    async def get_group_invitation_records(
        self,
        group: str,
        member: str = "",
        *,
        pending_only: bool = True,
        last_id: str = "",
    ) -> dict:
        """Read account-visible invitation logs, including not-yet-joined users.

        Args:
            group: Enabled business group ID.
            member: Optional exact invited business account ID.
            pending_only: Return only the platform's applied/invited states.
            last_id: Optional platform cursor from an earlier log response.

        Returns:
            Sanitized, group-scoped records and the upstream continuation cursor.
            Account visibility is not proof of a complete group invitation list.

        Raises:
            wire.ProtocolError: If scope, cursor or provider data is invalid.
        """
        if (
            type(pending_only) is not bool
            or not isinstance(last_id, str)
            or (
                last_id
                and (
                    not last_id.isascii() or not last_id.isdigit() or len(last_id) > 20
                )
            )
        ):
            raise wire.ProtocolError("invitation_arguments")
        async with self.invitation_read_lock:
            business = self.business
            if (
                not self.config.get("enable", True)
                or self.stopping.is_set()
                or self.connection_state != "online"
                or group not in self.config.get("enabled_groups", [])
                or group not in self.groups
                or not business
                or str(business.fields[3]) != self.account
            ):
                raise wire.ProtocolError("invitation_scope")
            key = f"records/{group}"
            now = time.monotonic()
            if now - self.invitation_read_at.get(key, float("-inf")) < 30:
                raise wire.ProtocolError("invitation_read_cooldown")
            self.invitation_read_at[key] = now
            # This route rejects groupId/v; filter the account log locally instead.
            response = await business.request(
                "/v1/group/get-apply-logs",
                {"lastId": last_id} if last_id else {},
            )
            if (
                not self.config.get("enable", True)
                or self.stopping.is_set()
                or self.connection_state != "online"
                or group not in self.config.get("enabled_groups", [])
                or group not in self.groups
                or self.business is not business
                or str(business.fields[3]) != self.account
            ):
                raise wire.ProtocolError("invitation_scope")
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("list"), list)
                or len(response["list"]) > 4096
            ):
                raise wire.ProtocolError("invitation_response")
            cursor = response.get("lastId", "")
            if (
                not isinstance(cursor, str)
                or len(cursor) > 20
                or (cursor and (not cursor.isascii() or not cursor.isdigit()))
            ):
                raise wire.ProtocolError("invitation_response")
            items = []
            seen = set()
            for row in response["list"]:
                if not isinstance(row, dict):
                    raise wire.ProtocolError("invitation_response")
                if str(row.get("groupId")) != group:
                    continue
                if row.get("groupApplyType") != "INVITATION":
                    continue
                apply_id = row.get("applyId")
                state = row.get("state")
                if (
                    not isinstance(apply_id, str)
                    or not apply_id.isascii()
                    or not apply_id.isdigit()
                    or len(apply_id) > 20
                    or int(apply_id) <= 0
                    or apply_id in seen
                    or not isinstance(state, str)
                    or not state
                    or len(state) > 80
                ):
                    raise wire.ProtocolError("invitation_response")
                seen.add(apply_id)
                pending = (
                    True
                    if state in {"MEMBER_STATE_APPLIED", "MEMBER_STATE_INVITED"}
                    else (
                        False
                        if state in {"MEMBER_STATE_GOOD", "MEMBER_STATE_MANAGER_REJECT"}
                        else None
                    )
                )
                if pending_only and pending is not True:
                    continue
                item = {
                    "apply_id": apply_id,
                    "state": state,
                    "pending": pending,
                    "apply_time": str(row.get("applyTime", ""))[:64],
                }
                for role, field in (("member", "applicant"), ("inviter", "inviter")):
                    person = row.get(field)
                    if person is not None and not isinstance(person, dict):
                        raise wire.ProtocolError("invitation_response")
                    person = person or {}
                    for source, target in (
                        ("userId", "id"),
                        ("accountId", "account_id"),
                        ("nimId", "nim_id"),
                    ):
                        value = person.get(source)
                        if value is None:
                            item[f"{role}_{target}"] = ""
                        else:
                            identity = str(value)
                            if (
                                type(value) not in (int, str)
                                or not identity.isascii()
                                or not identity.isdigit()
                                or len(identity) > 20
                                or int(identity) <= 0
                            ):
                                raise wire.ProtocolError("invitation_response")
                            item[f"{role}_{target}"] = identity
                    item[f"{role}_name"] = str(person.get("nick") or "")[:256]
                if member and item["member_id"] != member:
                    continue
                item["status"] = (
                    "attributed"
                    if item["member_id"] and item["inviter_id"]
                    else "unattributed"
                )
                items.append(item)
                if len(items) > 200:
                    raise wire.ProtocolError("invitation_response")
            return {
                "group_id": group,
                "viewer_id": self.account,
                "source": "business_apply_logs",
                "scope": "account_visible_records",
                "observed_at": int(time.time()),
                "pending_only": pending_only,
                "complete": False,
                "last_id": cursor,
                "items": items,
            }

    async def get_group_inviters(
        self, group: str, member: str = "", *, members: list[str] | None = None
    ) -> dict:
        """Read invitation attribution for current, authenticated group members.

        Args:
            group: Enabled business group ID.
            member: Optional exact business member ID to query.
            members: Optional bounded batch of exact joined business IDs.

        Returns:
            Bounded member/inviter identities and explicit unresolved statuses.
            This is a current-roster snapshot, not a credited recruitment ledger.

        Raises:
            wire.ProtocolError: If scope, lifecycle, identity or query is invalid.
        """
        if members is not None and (
            member
            or not isinstance(members, list)
            or not 1 <= len(members) <= 200
            or any(
                not isinstance(uid, str)
                or not uid.isascii()
                or not uid.isdigit()
                or len(uid) > 20
                or int(uid) <= 0
                for uid in members
            )
            or len(set(members)) != len(members)
        ):
            raise wire.ProtocolError("invitation_arguments")
        requested = set(members) if members is not None else None
        async with self.invitation_read_lock:
            if (
                not self.config.get("enable", True)
                or self.stopping.is_set()
                or self.connection_state != "online"
                or group not in self.config.get("enabled_groups", [])
                or group not in self.groups
                or not self.business
                or str(self.business.fields[3]) != self.account
                or not self.nim
            ):
                raise wire.ProtocolError("invitation_scope")
            now = time.monotonic()
            if now - self.invitation_read_at.get(group, float("-inf")) < 30:
                raise wire.ProtocolError("invitation_read_cooldown")
            self.invitation_read_at[group] = now
            nim, team_id = self.nim, self.groups[group]
            roster = await self.refresh_member_mapping(group)
            if not roster or not roster.get("complete"):
                raise wire.ProtocolError("invitation_roster_incomplete")
            roster_members = roster["groupMemberInfo"]
            selected = (
                [row for row in roster_members if row["userId"] in requested]
                if requested is not None
                else [row for row in roster_members if row["userId"] == member]
                if member
                else roster_members[:200]
            )
            if (member and len(selected) != 1) or (
                requested is not None and len(selected) != len(requested)
            ):
                raise wire.ProtocolError("invitation_member_not_found")
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or not self.config.get("enable", True)
                or group not in self.config.get("enabled_groups", [])
                or self.nim is not nim
                or self.groups.get(group) != team_id
                or str(self.business.fields[3]) != self.account
            ):
                raise wire.ProtocolError("invitation_scope")
            mapping = (
                await nim.get_team_inviters(team_id, [row["nimId"] for row in selected])
                if selected
                else {}
            )
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or group not in self.config.get("enabled_groups", [])
                or not self.config.get("enable", True)
                or self.nim is not nim
                or self.groups.get(group) != team_id
                or str(self.business.fields[3]) != self.account
            ):
                raise wire.ProtocolError("invitation_scope")
            peers = {row["nimId"]: row for row in roster_members}
            items = []
            for row in selected:
                inviter_peer = mapping.get(row["nimId"], "")
                inviter = peers.get(inviter_peer) if inviter_peer else None
                items.append(
                    {
                        "member_id": row["userId"],
                        "member_name": str(
                            row.get("userNick") or row.get("groupMemberNick") or ""
                        )[:256],
                        "member_nim_id": row["nimId"],
                        "inviter_id": inviter["userId"] if inviter else "",
                        "inviter_name": str(
                            inviter.get("userNick")
                            or inviter.get("groupMemberNick")
                            or ""
                        )[:256]
                        if inviter
                        else "",
                        "inviter_nim_id": inviter_peer,
                        "status": "attributed"
                        if inviter
                        else (
                            "inviter_not_in_roster" if inviter_peer else "unattributed"
                        ),
                    }
                )
                if requested is not None:
                    items[-1]["member_state"] = str(row.get("accountState", ""))
                    items[-1]["inviter_state"] = (
                        str(inviter.get("accountState", "")) if inviter else ""
                    )
            return {
                "group_id": group,
                "source": "nim_team_member_inviter",
                "observed_at": int(time.time()),
                "scope": "current_members",
                "member_count": len(roster_members),
                "queried_count": len(selected),
                "complete": bool(member)
                or requested is not None
                or len(selected) == len(roster_members),
                "items": items,
            }

    async def execute_moderation(
        self,
        operation: str,
        action: str,
        group: int,
        member: int = 0,
        text: str = "",
        *,
        minutes: int = 1,
        scheduled_check=None,
    ) -> dict:
        """Serialize group toggles and pause schedules before manual changes.

        Args:
            operation: Idempotent operation identifier.
            action: Authorized operation name.
            group: Target group ID.
            member: Optional member ID.
            text: Optional operation text.
            minutes: Explicit member mute duration; internal default is one minute.
            scheduled_check: Internal synchronous schedule authorization callback.

        Returns:
            Upstream operation evidence.
        """
        if action not in {"mute_all", "unmute_all"}:
            return await self._execute_moderation(
                operation, action, group, member, text, minutes=minutes
            )
        from .policy import authorize_action
        from .schedule_store import group_lock, pause

        async with group_lock(self, str(group)):
            authorize_action(self.config, str(group), action)
            if scheduled_check:
                scheduled_check()
            else:
                pause(self, str(group), "manual_override")
            return await self._execute_moderation(
                operation, action, group, member, text, scheduled_check=scheduled_check
            )

    async def _execute_moderation(
        self,
        operation: str,
        action: str,
        group: int,
        member: int = 0,
        text: str = "",
        *,
        minutes: int = 1,
        scheduled_check=None,
    ) -> dict:
        """Execute a fixed action using the persistent operation ledger.

        Args:
            operation: Stable operation ID.
            action: Fixed supported action.
            group: Business group ID.
            member: Business target member ID.
            text: Announcement content.
            minutes: Integer member mute duration in minutes.
            scheduled_check: Recheck a fixed schedule immediately before submission.

        Returns:
            Accepted, verified or unknown operation result.

        Raises:
            ProtocolError: If the connection, identity or platform role is invalid.
        """
        from .moderation import execute
        from .policy import authorize_action

        def require_manual_kick():
            if (
                action == "kick"
                and self.config.get("moderation", {}).get("manual_kick_only")
                and not operation.startswith("confirmed-kick/")
            ):
                raise wire.ProtocolError("private_confirmation_required")

        require_manual_kick()
        automatic_kick = action == "kick" and operation.startswith("automatic-kick/")
        progressive_mute = action == "mute" and operation.startswith("progressive/")
        expected_peer = self.members.get(str(group), {}).get(str(member))
        if progressive_mute:
            from .progressive import authorize as authorize_progressive

            authorize_progressive(self, str(group), str(member), expected_peer)
        if automatic_kick:
            from .automatic import authorize_kick

            authorize_kick(self, operation, str(group), str(member))
        elif action == "kick" and (
            self.config.get("moderation", {}).get("content_rules_since")
            or self.config.get("moderation", {}).get("manual_kick_only")
        ):
            path = instance_dir(self.config["id"]) / "moderation.sqlite3"
            if not operation.startswith("confirmed-kick/") or not path.exists():
                raise wire.ProtocolError("private_confirmation_required")
            with sqlite3.connect(path) as db:
                row = db.execute(
                    "SELECT account,group_id,member,used FROM kick_confirmations WHERE token=?",
                    (operation.removeprefix("confirmed-kick/"),),
                ).fetchone()
            if not row or row != (self.account, str(group), str(member), 1):
                raise wire.ProtocolError("private_confirmation_required")
        authorize_action(self.config, str(group), action)
        await self.get_moderation_members(str(group))
        if self.stopping.is_set():
            raise wire.ProtocolError("not_online")
        authorize_action(self.config, str(group), action)
        if automatic_kick:
            authorize_kick(self, operation, str(group), str(member))
        return await execute(
            self.business,
            self.config["id"],
            self.account,
            operation,
            action,
            group,
            member,
            text,
            minutes=minutes,
            expected_nim=expected_peer if automatic_kick or progressive_mute else None,
            authorize=lambda: (
                require_manual_kick(),
                scheduled_check() if scheduled_check else None,
                authorize_action(
                    self.config if not self.stopping.is_set() else {"enable": False},
                    str(group),
                    action,
                ),
                authorize_kick(self, operation, str(group), str(member))
                if automatic_kick
                else None,
                authorize_progressive(self, str(group), str(member), expected_peer)
                if progressive_mute
                else None,
            ),
        )

    async def rename_member(
        self,
        operation: str,
        group: int,
        member: int,
        name: str,
        expected_card: str,
        expected_nim: str,
        *,
        authorize=None,
    ) -> dict:
        """Rename one verified ordinary member using a frozen preview.

        Args:
            operation: Stable persisted operation ID.
            group: Enabled business group ID.
            member: Verified business member ID.
            name: Proposed group card.
            expected_card: Card captured during preview.
            expected_nim: Transport identity captured during preview.
            authorize: Optional job cancellation and lifecycle check.

        Returns:
            Persisted accepted, verified or unknown outcome.

        Raises:
            ProtocolError: If the preview, lifecycle or authorization changed.
        """
        from .moderation import execute
        from .policy import authorize_action

        account, business = self.account, self.business

        def check():
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or self.account != account
                or self.business is not business
            ):
                raise wire.ProtocolError("not_online")
            authorize_action(self.config, str(group), "rename")
            if authorize is not None:
                authorize()

        async with self.send_lock:
            check()
            return await execute(
                business,
                self.config["id"],
                account,
                operation,
                "rename",
                group,
                member,
                name,
                authorize=check,
                expected_card=expected_card,
                expected_nim=expected_nim,
            )

    async def cleanup_member(
        self, operation, group, member, expected_nim, *, authorize=None
    ):
        """Remove a currently banned or cancelled ordinary member.

        Args:
            operation: Persisted operation ID.
            group: Explicitly authorized group ID.
            member: Previewed business member ID.
            expected_nim: Previewed transport identity.
            authorize: Job cancellation check invoked immediately before mutation.

        Returns:
            Persisted platform outcome, with readback when available.

        Raises:
            ProtocolError: If authorization, identity or lifecycle changed.
        """
        from .moderation import execute
        from .policy import authorize_action

        account, business = self.account, self.business

        def check():
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or self.account != account
                or self.business is not business
            ):
                raise wire.ProtocolError("not_online")
            authorize_action(self.config, str(group), "cleanup")
            if authorize is not None:
                authorize()

        async with self.send_lock:
            check()
            return await execute(
                business,
                self.config["id"],
                account,
                operation,
                "cleanup",
                group,
                member,
                authorize=check,
                expected_nim=expected_nim,
            )

    async def recall_violation(self, group: str, mid: str, sender: str) -> str:
        """Recall a stored violation once, without retrying ambiguous results.

        Args:
            group: Enabled business group ID.
            mid: Stored server message ID.
            sender: Expected business sender ID.

        Returns:
            Accepted, rejected, or unknown transport status.

        Raises:
            ProtocolError: If authorization, roles or stored routing are invalid.
        """
        from .policy import authorize_action

        authorize_action(self.config, group, "recall")
        roster = (await self.get_moderation_members(group))["groupMemberInfo"]
        roles = {str(m["userId"]): m.get("groupRole") for m in roster}
        if (
            roles.get(self.account) not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
            or roles.get(sender) != "GROUP_ROLE_MEMBER"
        ):
            raise wire.ProtocolError("moderation_permission")
        async with self.ledger.db.execute(
            "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
            (self.account, group, mid),
        ) as cursor:
            row = await cursor.fetchone()
        data = json.loads(row[0]) if row else {}
        route = data.get("recall_route", {})
        if (
            data.get("sender") != sender
            or not route.get("client")
            or not str(route.get("time", "")).isdigit()
            or int(route["time"]) <= 0
            or not mid.isdigit()
            or int(mid) <= 0
            or route.get("peer") != self.members.get(group, {}).get(sender)
        ):
            raise wire.ProtocolError("recall_identity")
        async with self.send_lock:
            authorize_action(self.config, group, "recall")
            if self.stopping.is_set() or self.connection_state != "online":
                raise wire.ProtocolError("not_online")
            key = f"recall/{self.account}/{group}/{mid}"
            _, state = await self.ledger.reserve(key, json.dumps(route, sort_keys=True))
            if state != "new":
                return state
            try:
                authorize_action(self.config, group, "recall")
                if self.stopping.is_set() or self.connection_state != "online":
                    raise wire.ProtocolError("not_online")
            except wire.ProtocolError:
                await self.ledger.finish(key, "rejected")
                raise
            try:
                code, _ = await self.nim.request(
                    7,
                    13,
                    wire.properties(
                        [
                            (0, str(route["time"]).encode()),
                            (1, b"8"),
                            (2, self.groups[group].encode()),
                            (3, route["peer"].encode()),
                            (10, route["client"].encode()),
                            (11, mid.encode()),
                            (16, route["peer"].encode()),
                        ]
                    ),
                )
                result = "accepted" if code == 200 else "rejected"
            except BaseException:
                await self.ledger.finish(key, "unknown")
                raise
            await self.ledger.finish(key, result)
            self.diagnostics.emit("recall", result, mid)
            return result

    async def send_text(
        self,
        group: str,
        key: str,
        text: str,
        *,
        mentions=None,
        test_mid=None,
        proactive=False,
        auto_recall=False,
    ) -> str:
        """Persist intent before transport; uncertain sends are query-only."""
        async with self.send_lock:
            private = group.startswith("private/")
            if proactive and (
                not self.config.get("enable", True)
                or not self.config.get("proactive_send", {}).get("enabled", False)
                or group not in self.config.get("proactive_send", {}).get("targets", [])
            ):
                raise wire.ProtocolError("proactive_not_authorized")
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or (not private and group not in self.groups)
            ):
                raise wire.ProtocolError("not_online")
            if private:
                test_reply = bool(
                    test_mid
                    and self.test_window
                    and self.test_window.reply(
                        group, test_mid, key.rsplit("/part/", 1)[0]
                    )
                )
                if (
                    is_managed_account(group.split("/", 2)[1])
                    and not test_reply
                    and not (
                        (window := getattr(self, "test_command_window", None))
                        and window.send_command(self, group, text, key, consume=True)
                    )
                ):
                    raise wire.ProtocolError("managed_bot_peer")
                async with self.ledger.db.execute(
                    "SELECT 1 FROM inbox WHERE account=? AND team=? LIMIT 1",
                    (self.account, group),
                ) as cursor:
                    if await cursor.fetchone() is None:
                        raise wire.ProtocolError("private_peer_unknown")
                _, target, encoded_peer = group.split("/", 2)
                transport_target = wire.unb64(encoded_peer, 1024).decode()
            else:
                target, transport_target = group, self.groups[group]
            if not text or len(text.encode()) > 4096:
                raise wire.ProtocolError("text_limit")
            recall_reply = auto_recall is True and not private
            fingerprint = json.dumps(
                [group, text, mentions] + ([True] if recall_reply else []),
                ensure_ascii=True,
            )
            nonce, state = await self.ledger.reserve(key, fingerprint)
            if state != "new":
                self.diagnostics.emit("send", "existing_intent", key)
                return state
            self.diagnostics.emit("send", "reserved", key)
            client_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{self.config['id']}/{key}")
            )
            inner = wire.ApplicationMessage(
                sender=wire.Source(id=int(self.account)),
                target=wire.Source(id=int(target)),
                created_at=int(time.time() * 1000),
                device=1,
                session=1 if private else 2,
                version=2,
                format=0,
                client_id=client_id,
                content=wire.Content(data=text),
            )
            if mentions:
                if private:
                    raise wire.ProtocolError("group_mentions_only")
                inner.ClearField("content")
                inner.mentions.content.data = text
                for person in mentions:
                    inner.mentions.people.add(**person)
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or (
                    proactive
                    and (
                        not self.config.get("enable", True)
                        or not self.config.get("proactive_send", {}).get(
                            "enabled", False
                        )
                        or group
                        not in self.config.get("proactive_send", {}).get("targets", [])
                    )
                )
            ):
                await self.ledger.finish(key, "rejected")
                return "rejected"
            try:
                attachment = wire.seal_message(
                    self.business.deployment.key("message_key"),
                    inner,
                    int(nonce),
                    secrets.randbits(64) or 1,
                    int(time.time()),
                )
                code, body = await self.nim.request(
                    7 if private else 8,
                    1 if private else 2,
                    wire.properties(
                        [
                            (0, b"0" if private else b"1"),
                            (1, transport_target.encode()),
                            (8, b"100"),
                            (10, attachment.encode()),
                            (11, client_id.encode()),
                        ]
                    ),
                )
                if code != 200:
                    await self.ledger.finish(key, "rejected")
                    self.diagnostics.emit("send", "rejected", key, failed=True)
                    return "rejected"
                receipt, end = wire.read_properties(body)
                if (
                    end != len(body)
                    or not receipt.get(12)
                    or not receipt.get(7, b"").isdigit()
                    or receipt.get(11, client_id.encode()) != client_id.encode()
                ):
                    raise wire.ProtocolError("receipt_shape")
                recall = None
                if recall_reply:
                    if (
                        not receipt[12].isdigit()
                        or not self.nim_account
                        or self.members.get(group, {}).get(self.account)
                        != str(self.nim_account)
                    ):
                        raise wire.ProtocolError("reply_recall_identity")
                    recall = (
                        self.account,
                        group,
                        transport_target,
                        str(self.nim_account),
                        client_id,
                        receipt[7].decode(),
                        time.time() + 20,
                    )
                await self.ledger.finish(
                    key, "accepted", receipt[12].decode(), recall=recall
                )
                self.diagnostics.emit("send", "accepted", key)
                return "accepted"
            except BaseException:
                await self.ledger.finish(key, "unknown")
                self.diagnostics.emit(
                    "send", "unknown", key, failed=True, terminal=True
                )
                raise

    async def send_reply_text(
        self,
        group,
        key,
        text,
        *,
        mentions=None,
        test_mid=None,
        proactive=False,
        auto_recall=False,
    ):
        """Send a bounded logical reply with stable, non-overlapping segments.

        Args:
            group: Verified native target.
            key: Stable logical operation identifier.
            text: Formatted reply text.
            mentions: UTF-16 mention ranges.
            test_mid: Correlated developer-window inbound message.
            proactive: Require explicit target authorization.
            auto_recall: Recall each accepted group segment after twenty seconds.

        Returns:
            Durable logical operation status.
        """
        fingerprint = json.dumps([group, text, mentions], ensure_ascii=True)
        async with self.send_lock:
            _, state = await self.ledger.reserve(key, fingerprint)
        if state != "new":
            return "unknown" if state == "sending" else state
        offset = 0
        try:
            while text:
                end = 0
                size = 0
                units = 0
                for char in text:
                    if size + len(char.encode()) > 4096:
                        break
                    end += 1
                    size += len(char.encode())
                    units += len(char.encode("utf-16-le")) // 2
                for mention in mentions or []:
                    if mention["start"] < offset + units < mention["end"]:
                        units = mention["start"] - offset
                        end = len(
                            text.encode("utf-16-le")[: units * 2].decode("utf-16-le")
                        )
                if not end:
                    raise wire.ProtocolError("mention_text_limit")
                part_mentions = [
                    {**m, "start": m["start"] - offset, "end": m["end"] - offset}
                    for m in mentions or []
                    if offset <= m["start"] and m["end"] <= offset + units
                ]
                result = await self.send_text(
                    group,
                    f"{key}/part/{offset}",
                    text[:end],
                    mentions=part_mentions or None,
                    test_mid=test_mid,
                    proactive=proactive,
                    **({"auto_recall": True} if auto_recall is True else {}),
                )
                if result not in {"accepted", "verified"}:
                    await self.ledger.finish(key, result)
                    return result
                text = text[end:]
                offset += units
            child = await self.ledger.receipt(f"{key}/part/0")
            await self.ledger.finish(
                key, "accepted", child.get("server_id", "") if child else ""
            )
            return "accepted"
        except BaseException:
            await self.ledger.finish(key, "unknown")
            raise

    async def send_by_session(
        self, session: MessageSession, message_chain, *, operation_id: str | None = None
    ) -> dict:
        """Send an explicitly authorized text message to one known session.

        Args:
            session: Target platform session.
            message_chain: AstrBot message chain containing text components.

        Raises:
            ProtocolError: If proactive sending is disabled or the target is invalid.
        """
        if not self.config.get("proactive_send", {}).get("enabled", False):
            raise wire.ProtocolError("proactive_not_authorized")
        if session.platform_id != self.config["id"]:
            raise wire.ProtocolError("session_platform")
        if session.message_type is MessageType.GROUP_MESSAGE:
            target = session.session_id.split("/", 1)[-1]
            if target not in self.groups or target not in self.config.get(
                "enabled_groups", []
            ):
                raise wire.ProtocolError("session_target")
        elif session.message_type is MessageType.FRIEND_MESSAGE:
            target = session.session_id.removeprefix(f"{self.account}/")
            if not target.startswith("private/") or len(target.split("/")) != 3:
                raise wire.ProtocolError("session_target")
        else:
            raise wire.ProtocolError("session_type")
        parts = getattr(message_chain, "chain", [])
        if not parts or any(not isinstance(part, (Plain, At)) for part in parts):
            raise wire.ProtocolError("text_only")
        structured_mentions = any(isinstance(part, At) for part in parts)
        text = ""
        people = []
        for part in parts:
            if isinstance(part, Plain):
                text += redact_reply(
                    part.text if structured_mentions else plain_text(part.text),
                    preserve_whitespace=structured_mentions,
                )
                continue
            if session.message_type is not MessageType.GROUP_MESSAGE:
                raise wire.ProtocolError("group_mentions_only")
            uid = str(part.qq)
            peer = self.members.get(target, {}).get(uid, "")
            if not peer.isascii() or not peer.isdigit() or not 0 < int(peer) < 1 << 32:
                raise wire.ProtocolError("mention_identity")
            nick = str(part.name or uid)
            start = len(text.encode("utf-16-le")) // 2
            text += f"@{nick} "
            people.append(
                {
                    "uid": int(peer),
                    "nick": nick,
                    "start": start,
                    "end": len(text.encode("utf-16-le")) // 2,
                }
            )
        if not text.strip():
            raise wire.ProtocolError("text_empty")
        key = "proactive/" + (operation_id or uuid.uuid4().hex)
        result = await self.send_reply_text(
            target, key, text, mentions=people or None, proactive=True
        )
        return {"operation_id": key.removeprefix("proactive/"), "status": result}

    async def terminate(self) -> None:
        """Stop event processing and transport before releasing account ownership."""
        self.stopping.set()
        if (
            self.runner
            and self.runner is not asyncio.current_task()
            and not self.runner.done()
        ):
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        self.connection_state = "stopped"
        self.status = PlatformStatus.STOPPED
