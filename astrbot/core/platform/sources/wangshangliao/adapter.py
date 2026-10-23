"""Native Wangshangliao group text adapter; no external gateway."""

import asyncio
import copy
import inspect
import json
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import aiohttp

from astrbot.api.message_components import At, Plain, Reply
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
from .business import SECURITY_ERRORS, BusinessClient, BusinessError, Deployment
from .diagnostics import Diagnostics
from .directory import member_directory
from .event import WangshangliaoEvent, is_managed_account, mentioned
from .identity import account_nickname
from .invitations import latest_membership_events
from .nim import NimClient, messages, recall_notifications
from .storage import Ledger, Vault, instance_dir
from .text import plain_text, redact_reply

ACTIVE_ACCOUNTS: set[str] = set()


def _text_message(
    instance, account, target, key, text, mentions, private, created_at, *, quote=None
):
    """Build the exact application payload shared by sizing and native sending.

    Args:
        instance: Original platform instance identifier.
        account: Authenticated sender business identifier.
        target: Target business identifier.
        key: Stable per-segment operation identifier.
        text: Segment text.
        mentions: Segment-local UTF-16 mention descriptors.
        private: Whether the target is a private session.
        created_at: Millisecond message timestamp, or a sizing upper bound.
        quote: Verified local inbox reference, never a model-supplied quote body.

    Returns:
        Complete application protobuf, including all transport metadata.
    """
    inner = wire.ApplicationMessage(
        sender=wire.Source(id=int(account)),
        target=wire.Source(id=int(target)),
        created_at=created_at,
        device=1,
        session=1 if private else 2,
        version=2,
        format=0,
        client_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{instance}/{key}")),
        content=wire.Content(data=text),
    )
    if mentions:
        if private:
            raise wire.ProtocolError("group_mentions_only")
        inner.ClearField("content")
        inner.mentions.content.data = text
        for person in mentions:
            inner.mentions.people.add(**person)
    if quote:
        inner.appoint = 1
        inner.reply.client_id = quote["client"]
        inner.reply.server_id = quote["message"]
        inner.reply.content.data = quote["text"]
    return inner


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


def _notification_peer(value) -> str:
    """Return one bounded NIM peer ID, or an empty value when invalid."""
    text = str(value) if type(value) in (str, int) else ""
    if (
        not text.isascii()
        or not text.isdigit()
        or len(text) > 20
        or not 0 < int(text) < 1 << 32
    ):
        return ""
    return text


def team_invite_notification(outer: dict) -> dict | None:
    """Extract a native normal-team invitation notification without routing text.

    A type-5 NIM notification is provider metadata, not a group message.  It is
    kept separately as join evidence. Its sender is the join operator, possibly
    the approving owner, and is not proof of the business recruiter.
    """
    if outer.get(0) != "1" or outer.get(8) != "5":
        return None
    encoded = outer.get(10)
    if not isinstance(encoded, str) or not 0 < len(encoded.encode()) <= 16384:
        return None
    try:
        notice = json.loads(encoded)
        if not isinstance(notice, dict) or notice.get("id") != 0:
            return None
        data = notice.get("data")
        if isinstance(data, str):
            if not 0 < len(data.encode()) <= 16384:
                return None
            data = json.loads(data)
        if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
            return None
        members = [_notification_peer(item) for item in data["ids"]]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    inviter = _notification_peer(outer.get(2))
    if (
        not inviter
        or not members
        or len(members) > 200
        or any(not item for item in members)
    ):
        return None
    return {
        "kind": "team_invite",
        "inviter_peer": inviter,
        "member_peers": list(dict.fromkeys(members)),
    }


@register_platform_adapter("wangshangliao", "旺商聊", support_streaming_message=False)
class WangshangliaoAdapter(Platform):
    """Own one account's transport, ledger and sequential group workers."""

    supports_operation_ids = True

    @property
    def config(self):
        """Expose detached effective policy, never a second writable store."""
        from .runtime_policy import runtime

        return runtime.view(self._transport_config)

    @config.setter
    def config(self, value):
        self._transport_config = value

    @property
    def enabled_groups(self):
        """Keep observation scope available while plugin execution is suspended."""
        return set(self.config.get("enabled_groups", []))

    def __init__(self, platform_config, platform_settings, event_queue):
        from pathlib import Path

        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        from .runtime_policy import runtime

        runtime.restore_observation(
            platform_config,
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "wangshangliao_moderation"
            / "settings.sqlite3",
        )
        super().__init__(platform_config, event_queue)
        self.diagnostics = Diagnostics(platform_config["id"])
        self.account = str(platform_config.get("account_id", ""))
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
        self.group_catalog = {}
        self.members = {}
        self.group_directory_state = {}
        self.member_refresh_locks = {}
        self.member_refresh_at = {}
        self.member_snapshots = {}
        self.invitation_read_lock = asyncio.Lock()
        self.invitation_read_at = {}
        self.workers = {}
        self.worker_classes = {}
        self.dispatch_wakeup = asyncio.Event()
        self.dispatch_task = None
        self.maintenance_task = None
        self.metrics_task = None
        self.reply_recall_task = None
        self.events = set()
        self.connection_state = "stopped"
        self.runner = None
        self.heartbeat_task = None
        self.unsupported_pushes = 0
        self.receive_health = {
            "decoded_messages": 0,
            "unhandled_packets": 0,
            "invalid_packets": 0,
            "decode_failures": 0,
            "unsupported_formats": 0,
        }
        self.decode_reasons = {}
        self.decode_shapes = {}
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
            "nickname": account_nickname(self),
            "account_id": self.account,
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
            "business_auth": {
                "state": (
                    "expired"
                    if getattr(self.business, "auth_error", "") == "reauth_required"
                    else "security_required"
                    if getattr(self.business, "security_required", False)
                    else "credential_present"
                    if getattr(self.business, "jwt", "")
                    else "unavailable"
                ),
                "protocol_version": getattr(self.business, "protocol_version", ""),
                "device_type": (
                    self.business.fields[1]
                    if self.business and hasattr(self.business, "fields")
                    else None
                ),
            },
            "unsupported_pushes": self.unsupported_pushes,
            "receive_health": {
                **self.receive_health,
                "decode_reasons": dict(self.decode_reasons),
                "decode_shapes": dict(self.decode_shapes),
            },
            "current_error": self.current_error,
            "last_connected_at": self.last_connected_at,
            "next_retry_at": self.next_retry_at,
            "group_directory_state": dict(self.group_directory_state),
            "business_active": self.config.get("_business_active", False),
            "business_revision": self.config.get("_business_revision", ""),
            "enabled_groups": sorted(self.enabled_groups),
            "performance": self.diagnostics.snapshot(),
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
            # A cold start must not replay commands accepted before suspension.
            await self.ledger.db.execute(
                "UPDATE inbox SET state='business_suspended',completed_at=? "
                "WHERE account=? AND state='pending'",
                (time.time(), self.account),
            )
            await self.ledger.db.commit()
            self.dispatch_task = asyncio.create_task(self.dispatch_pending())
            self.maintenance_task = asyncio.create_task(self.maintain_ledger())
            self.metrics_task = asyncio.create_task(self.sample_runtime())
            from .reply_recall import run as recall_replies

            self.reply_recall_task = asyncio.create_task(recall_replies(self))
            delay = 1
            while not self.stopping.is_set():
                self.connection_state = "reconnecting"
                self.next_retry_at = None
                consumer = None
                retry_wait = min(30, delay * (1 + secrets.randbelow(251) / 1000))
                try:
                    token = await self.business.refresh_nim_token()
                    saved["nim_token"] = token
                    self.vault.save(saved)
                    await self.load_groups()
                    self.nim = NimClient(self.http)
                    await self.nim.connect(deployment, saved["nim_id"], token)
                    async with self.ledger.db.execute(
                        "SELECT cursor FROM sync WHERE account=?", (self.account,)
                    ) as cursor:
                        row = await cursor.fetchone()
                    async with self.ledger.db.execute(
                        "SELECT MAX(CAST(recalled_at AS INTEGER)) "
                        "FROM observed_recalls WHERE account=?",
                        (self.account,),
                    ) as cursor:
                        recall_cursor = (await cursor.fetchone())[0]
                    consumer = asyncio.create_task(self.consume_nim_pushes(self.nim))
                    # Sync responses may precede queued pushes; persist their watermark only
                    # after the single push consumer has committed all earlier events.
                    code, body = await self.nim.request(
                        5,
                        1,
                        wire.properties(
                            [
                                (2, (row[0] if row else "0").encode()),
                                (7, (row[0] if row else "0").encode()),
                                (17, str(recall_cursor or 0).encode()),
                            ]
                        ),
                    )
                    if code != 200 or len(body) != 8:
                        raise wire.ProtocolError("sync_boundary")
                    try:
                        await asyncio.wait_for(
                            self.nim.pushes.put((5, 1, 0, 200, body)), 5
                        )
                    except asyncio.TimeoutError:
                        raise wire.ProtocolError("nim_push_backpressure") from None
                    self.heartbeat_task = asyncio.create_task(self.keep_alive())
                    self.diagnostics.emit("connection", "online")
                    self.connection_state = "online"
                    self.current_error = ""
                    self.last_connected_at = time.time()
                    self.status = PlatformStatus.RUNNING
                    delay = 1
                    self.dispatch_wakeup.set()
                    await consumer
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    label = (
                        str(exc)
                        if isinstance(exc, wire.ProtocolError)
                        else "connection_failed"
                    )
                    self.current_error = label
                    if label in {
                        "reauth_required",
                        "account_conflict",
                        "nim_kicked",
                        "secondary_security_code_required",
                        "secondary_security_unverified",
                        "secondary_security_code_invalid",
                        "secondary_security_locked",
                        "secondary_security_phone_required",
                        "account_clearing",
                    }:
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
                    if consumer:
                        consumer.cancel()
                        await asyncio.gather(consumer, return_exceptions=True)
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
                self.metrics_task,
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

    async def consume_nim_pushes(self, nim) -> None:
        """Persist pushes while initial synchronization is awaiting its receipt.

        Args:
            nim: Current connection's single-reader transport.

        Raises:
            ProtocolError: If transport or verified-message processing fails.
        """
        while not self.stopping.is_set() and not nim.closed.is_set():
            try:
                packet = await asyncio.wait_for(nim.pushes.get(), 15)
            except asyncio.TimeoutError:
                continue
            try:
                recalls = recall_notifications(packet)
                for notice in recalls:
                    if self.nim is not nim:
                        raise wire.ProtocolError("nim_disconnected")
                    group = next(
                        (
                            gid
                            for gid, item in self.group_catalog.items()
                            if str(item["team"]) == notice["team"]
                        ),
                        "",
                    )
                    if group and await self.ledger.observe_recall(
                        self.account, group, notice
                    ):
                        self.diagnostics.emit(
                            "recall", "observed", notice["notice_id"], group=group
                        )
                incoming, boundary = messages(packet)
            except wire.ProtocolError as exc:
                if str(exc) in {
                    "message_fields",
                    "message_trailing",
                    "notification",
                    "message_count",
                    "recall_notification",
                }:
                    self.unsupported_pushes += 1
                    self.receive_health["invalid_packets"] += 1
                    self.diagnostics.emit(
                        "receive", "unsupported", failed=True, error=str(exc)
                    )
                    continue
                raise
            if not incoming and not recalls and boundary is None:
                self.receive_health["unhandled_packets"] += 1
            for message in incoming:
                try:
                    await self.ingest(message)
                except wire.ProtocolError as exc:
                    if str(exc) != "message_decode":
                        raise
                    self.unsupported_pushes += 1
                    self.receive_health["decode_failures"] += 1
                    self.diagnostics.emit(
                        "receive",
                        "unsupported",
                        message.get(12, ""),
                        failed=True,
                        error="message_decode",
                    )
                    await nim.acknowledge(int(message[0]), message[12])
            if boundary is not None:
                await self.ledger.db.execute(
                    "INSERT INTO sync VALUES(?,?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor",
                    (self.account, str(boundary)),
                )
                await self.ledger.db.commit()
        if nim.closed.is_set():
            raise wire.ProtocolError(nim.error)

    async def load_groups(self) -> None:
        """Discover all groups but fetch member identities only in policy scope."""
        data = await self.business.request("/v1/group/get-group-list", {"v": "0"})
        if not isinstance(data, dict) or any(
            not isinstance(data.get(key), list) for key in ("owner", "member")
        ):
            raise wire.ProtocolError("groups_shape")
        groups = {}
        group_names = {}
        catalog = {}
        for entry in data.get("owner", []) + data.get("member", []):
            group_id, team = str(entry["groupId"]), str(entry["groupCloudId"])
            name = " ".join(
                str(entry.get("groupName") or entry.get("name") or "").split()
            )[:128]
            if group_id in catalog and catalog[group_id]["team"] != team:
                raise wire.ProtocolError("group_identity")
            catalog[group_id] = {"id": group_id, "name": name or group_id, "team": team}
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
        self.group_catalog = catalog
        self.members = {}
        self.group_directory_state = {}
        for group_id in groups:
            try:
                await self.refresh_member_mapping(group_id)
            except (wire.ProtocolError, aiohttp.ClientError, TimeoutError) as exc:
                if str(exc) in {
                    "reauth_required",
                    "account_conflict",
                    "rate_limited",
                    *SECURITY_ERRORS.values(),
                }:
                    raise
                self.diagnostics.emit(
                    "directory",
                    "unavailable",
                    group_id,
                    failed=True,
                    error="member_directory_unavailable",
                )

    async def refresh_member_mapping(
        self, group: str, *, bounded: bool = False, max_age: float = 0
    ) -> dict | None:
        """Publish a complete roster atomically, coalescing ingress refreshes.

        Args:
            group: Enabled business group ID.
            bounded: Limit mismatch-triggered requests to one per thirty seconds.
            max_age: Maximum age of a complete snapshot; zero forces a refresh.

        Returns:
            Validated roster, or None when a bounded refresh is throttled.

        Raises:
            ProtocolError: If the roster cannot establish consistent identities.
        """
        requested_at = time.monotonic()
        lock = self.member_refresh_locks.setdefault(group, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self.member_snapshots.get(group)
            if (
                cached
                and cached[0] == self.account
                and (
                    cached[1] >= requested_at
                    or (max_age > 0 and now - cached[1] < max_age)
                )
            ):
                return copy.deepcopy(cached[2])
            if bounded and now - self.member_refresh_at.get(group, float("-inf")) < 30:
                return None
            self.member_refresh_at[group] = now
            try:
                fetch_started = time.monotonic()
                page = await member_directory(self.business, group)
                self.diagnostics.observe(
                    "member_directory", time.monotonic() - fetch_started
                )
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
                self.member_snapshots.pop(group, None)
                self.group_directory_state[group] = {
                    "complete": False,
                    "error": "member_directory_unavailable",
                    "updated_at": time.time(),
                }
                raise
            self.members[group] = mapping
            self.member_snapshots[group] = (
                self.account,
                time.monotonic(),
                copy.deepcopy(page),
            )
            self.group_directory_state[group] = {
                "complete": True,
                "error": "",
                "updated_at": time.time(),
            }
            return page

    async def ingest(self, outer: dict) -> None:
        """Authenticate custom group text, commit its inbox, then ACK its NIM ID."""
        inner = None
        if outer.get(8) == "100":
            try:
                inner = wire.open_message(
                    self.business.deployment.key("message_key"), outer.get(10, "")
                )
                self.receive_health["decoded_messages"] += 1
            except wire.ProtocolError as exc:
                if str(exc) != "message_decode":
                    raise
                # Persist only routing metadata, never unverified content. This
                # prevents poison history from repeatedly reconnecting the socket.
                target = "unsupported/" + outer[0] + "/" + outer[1]
                await self.ledger.ingest(
                    self.account, target, outer[12], {}, state="unsupported"
                )
                self.unsupported_pushes += 1
                self.receive_health["decode_failures"] += 1
                reason = getattr(exc, "reason", "unknown")
                if reason not in {
                    "wrapper",
                    "encoding",
                    "envelope",
                    "packet_version",
                    "packet_signature",
                    "packet_message",
                    "authentication",
                    "compression",
                    "schema",
                    "routing",
                }:
                    reason = "unknown"
                self.decode_reasons[reason] = self.decode_reasons.get(reason, 0) + 1
                shape = getattr(exc, "shape", "")
                if shape:
                    if (
                        shape not in self.decode_shapes
                        and len(self.decode_shapes) >= 16
                    ):
                        shape = "other"
                    self.decode_shapes[shape] = self.decode_shapes.get(shape, 0) + 1
                self.diagnostics.emit(
                    "receive",
                    "unsupported",
                    outer[12],
                    failed=True,
                    error="message_decode",
                    action=reason,
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
                self.receive_health["unsupported_formats"] += 1
                await self.nim.acknowledge(0, outer[12])
                return
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
            image = (
                wire.image_metadata(inner)
                if inner.format == 1
                and peer.isascii()
                and peer.isdecimal()
                and not peer.startswith("0")
                and 0 < int(peer) < 1 << 32
                else None
            )
            supported = (
                bool(image)
                or inner.format == 0
                and (inner.HasField("content") or inner.mentions.HasField("content"))
            )
            payload = {
                "text": ""
                if image
                else inner.content.data
                if inner.HasField("content")
                else inner.mentions.content.data,
                "sender": str(inner.sender.id),
                "transport_peer": peer,
                "name": inner.sender.name,
                "created_at": message_timestamp(inner, outer),
                "mentions": [],
            }
            if image:
                payload.update(kind="image", image=image)
            from .runtime_policy import runtime

            service = runtime.open_service
            if supported and not image and service is not None:
                principal = "nim/" + peer
                control = payload["text"].strip()
                if (
                    control in {"/chat", "/exit"}
                    and (self.account, principal) in service.chat_tasks
                ):
                    await service.end_chat(self.account, principal)
                elif principal in service.chat_users and not (
                    runtime.recognize(control) or service.recognize_command(control)
                ):
                    payload["_ai_busy"] = True
            if not supported:
                self.receive_health["unsupported_formats"] += 1
                payload = {"format": inner.format, "reason": "unsupported_format"}
            state = (
                "unsupported"
                if not supported
                else "business_suspended"
                if not self.config.get("_business_active")
                else "pending"
            )
            from .runtime_policy import runtime

            admitted = False

            def observe_private(envelope, received_at):
                nonlocal admitted
                runtime.observe(
                    self._transport_config,
                    self.account,
                    session,
                    outer[12],
                    envelope,
                    received_at,
                )
                admitted = True

            created = await self.ledger.ingest(
                self.account,
                session,
                outer[12],
                payload,
                state=state,
                observe=observe_private,
            )
            await self.nim.acknowledge(0, outer[12])
            # A duplicate may be committed work whose previous ACK failed.
            if state == "pending" and (admitted or not created):
                self.dispatch_wakeup.set()
            return
        if outer[0] != "1":
            return
        from .runtime_policy import runtime

        service = runtime.open_service
        quote_test = getattr(service, "quote_rehearsal", None)
        if inner is not None and quote_test is not None:
            if await quote_test.receive(self, outer, inner):
                await self.nim.acknowledge(1, outer[12])
                return
        if service is not None and outer.get(8) == "5":
            if await service.receive_membership(self, outer):
                await self.nim.acknowledge(1, outer[12])
                return
        group = next(
            (group for group, team in self.groups.items() if team == outer[1]), None
        )
        if group is None:
            from .runtime_policy import runtime

            service = runtime.open_service
            if service is not None and outer.get(8) == "100":
                try:
                    if inner.session == 2 and inner.format in (0, 1):
                        service.admit_candidate(
                            self, str(inner.target.id), outer, inner
                        )
                except Exception:
                    self.diagnostics.emit("open_activation", "unconfirmed", outer[12])
                await self.nim.acknowledge(1, outer[12])
            return
        if (
            service is not None
            and group not in self.enabled_groups
            and outer.get(8) == "100"
        ):
            if (
                inner.session == 2
                and str(inner.target.id) == group
                and inner.format == 0
                and inner.HasField("content")
                and inner.content.data.strip() == "开通"
            ):
                service.admit_candidate(self, group, outer, inner)
                await self.nim.acknowledge(1, outer[12])
                return
        notification = team_invite_notification(outer)
        if notification is not None:
            notification["business_revision"] = self.config.get(
                "_business_revision", ""
            )
            notification["business_suspended"] = not self.config.get(
                "_business_active", False
            )
            stamp = outer.get(7, "")
            if (
                isinstance(stamp, str)
                and stamp.isascii()
                and stamp.isdigit()
                and len(stamp) <= 19
            ):
                notification["event_at"] = int(stamp)
            await self.ledger.ingest(
                self.account,
                group,
                outer[12],
                notification,
                state="invite_notification",
            )
            blacklist = getattr(service, "member_blacklist", None)
            if blacklist is not None:
                await blacklist.receive(self, group, outer[12], notification)
            await self.nim.acknowledge(1, outer[12])
            return
        state = "unsupported"
        payload = {}
        if outer[8] == "100":
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
            elif (
                inner.format in (1, 3, 9)
                or inner.format == 0
                and (inner.HasField("content") or inner.mentions.HasField("content"))
            ):
                state = "pending"
                # Official MessageFormat: image=1, video=3, merged forward=9.
                media = {1: "image", 3: "video", 9: "forward"}.get(inner.format)
                payload = {
                    "text": ""
                    if media
                    else inner.content.data
                    if inner.HasField("content")
                    else inner.mentions.content.data,
                    "sender": str(inner.sender.id),
                    "name": inner.sender.name,
                    "created_at": message_timestamp(inner, outer),
                    "mentions": []
                    if media
                    else [str(person.uid) for person in inner.mentions.people],
                    "mention_spans": [
                        {
                            "uid": str(person.uid),
                            "nick": person.nick,
                            "start": person.start,
                            "end": person.end,
                        }
                        for person in (() if media else inner.mentions.people)
                    ],
                    "recall_route": {
                        "client": outer.get(11, ""),
                        "time": outer.get(7, ""),
                        "peer": outer[2],
                    },
                }
                if media:
                    payload["kind"] = media
                if inner.HasField("reply"):
                    payload["reply"] = {
                        "server": str(inner.reply.server_id),
                        "client": str(inner.reply.client_id),
                    }
        from .runtime_policy import runtime

        if state == "pending" and not self.config.get("_business_active"):
            state = "business_suspended"
        admitted = False

        def observe_group(envelope, received_at):
            nonlocal admitted
            runtime.observe(
                self._transport_config,
                self.account,
                group,
                outer[12],
                envelope,
                received_at,
            )
            admitted = True

        created = await self.ledger.ingest(
            self.account,
            group,
            outer[12],
            payload,
            state=state,
            observe=observe_group,
        )
        await self.nim.acknowledge(1, outer[12])
        if state == "pending" and (admitted or not created):
            self.dispatch_wakeup.set()

    async def dispatch_pending(self) -> None:
        """Keep ordered member lanes with four slots reserved for fixed commands."""
        while not self.stopping.is_set():
            self.dispatch_wakeup.clear()
            failed = False
            for lane, task in list(self.workers.items()):
                if task.done():
                    if not task.cancelled() and task.exception():
                        self.record_error("dispatch_processing_failed")
                        self.diagnostics.emit(
                            "dispatch", "processing_failed", failed=True
                        )
                        failed = True
                    del self.workers[lane]
                    self.worker_classes.pop(lane, None)
            if failed:
                try:
                    await asyncio.wait_for(self.stopping.wait(), 1)
                    return
                except asyncio.TimeoutError:
                    pass
            try:
                if self.connection_state == "online" and self.config.get(
                    "_business_active"
                ):
                    groups = list(self.enabled_groups.intersection(self.groups))
                    scans = []
                    args = [self.account, *groups]
                    for priority, (kind, limit) in enumerate(
                        (("control", 2), ("command", 4), ("message", 12))
                    ):
                        slots = limit - sum(
                            value == kind for value in self.worker_classes.values()
                        )
                        if slots <= 0:
                            continue
                        excluded = [
                            (team, sender.removeprefix("control/"))
                            for (
                                team,
                                sender,
                            ), worker_kind in self.worker_classes.items()
                            if (worker_kind == "control") == (kind == "control")
                        ]
                        head = "control_row" if kind == "control" else "first_row"
                        sql = (
                            "SELECT i.team,i.sender,i.dispatch_class,"
                            f"i.rowid AS first_row,{priority} AS priority "
                            f"FROM heads h JOIN inbox i ON i.rowid=h.{head} "
                            "WHERE i.dispatch_class=?"
                        )
                        args.append(kind)
                        if excluded:
                            sql += (
                                " AND (i.team,i.sender) NOT IN ("
                                + ",".join("(?,?)" for _ in excluded)
                                + ")"
                            )
                            args.extend(value for lane in excluded for value in lane)
                        sql += " ORDER BY i.rowid LIMIT ?"
                        args.append(slots)
                        scans.append("SELECT * FROM (" + sql + ")")
                    if scans:
                        # Share one pending scan. Ordinary heads include controls,
                        # so a command cannot pass its sender's older control.
                        sql = (
                            "WITH heads AS (SELECT team,sender,MIN(rowid) AS first_row,"
                            "MIN(CASE WHEN dispatch_class='control' THEN rowid END) "
                            "AS control_row FROM inbox "
                            "WHERE account=? AND state='pending' "
                            "AND (team LIKE 'private/%' OR team IN ("
                            + ",".join("?" for _ in groups)
                            + ")) GROUP BY team,sender), ready AS ("
                            + " UNION ALL ".join(scans)
                            + ") SELECT team,sender,dispatch_class FROM ready "
                            "ORDER BY priority,first_row"
                        )
                        async with self.ledger.db.execute(sql, args) as cursor:
                            ready = await cursor.fetchall()
                        for team, sender, kind in ready:
                            task = asyncio.create_task(
                                self.process_group(
                                    team,
                                    single=True,
                                    sender=sender,
                                    control=kind == "control",
                                )
                            )
                            lane = (
                                team,
                                "control/" + sender if kind == "control" else sender,
                            )
                            self.workers[lane] = task
                            self.worker_classes[lane] = kind
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

    async def sample_runtime(self):
        """Sample loop delay and durable backlog without logging message content."""
        ticks = 0
        last_rejected = 0
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                await asyncio.wait_for(self.stopping.wait(), 1)
                return
            except TimeoutError:
                self.diagnostics.observe(
                    "event_loop", max(0, time.monotonic() - started - 1)
                )
            self.diagnostics.gauges["workers"] = len(self.workers)
            self.diagnostics.gauges["admission_rejected"] = self.ledger.rejected_count
            self.diagnostics.gauges["review_holds"] = len(self.ledger.review_groups)
            if self.ledger.rejected_count != last_rejected:
                self.diagnostics.emit("admission", "queue_rejected", failed=True)
                last_rejected = self.ledger.rejected_count
            ticks += 1
            if ticks % 10 == 0:
                try:
                    async with self.ledger.db.execute(
                        "SELECT COUNT(*),MIN(received_at) FROM inbox WHERE account=? AND state='pending'",
                        (self.account,),
                    ) as cursor:
                        count, oldest = await cursor.fetchone()
                    self.diagnostics.gauges.update(
                        pending=count,
                        oldest_pending_ms=int(max(0, time.time() - oldest) * 1000)
                        if oldest
                        else 0,
                    )
                except Exception:
                    self.diagnostics.emit("metrics", "sample_failed", failed=True)

    async def process_group(
        self,
        group: str,
        *,
        single: bool = False,
        sender: str | None = None,
        control: bool = False,
    ) -> None:
        """Process one ordered member lane, leaving other members independent.

        Args:
            group: Group or private session ID.
            single: Process just one item for fair scheduling.
            sender: Frozen member lane; None retains the maintenance drain API.
        """
        remaining = 1 if single else float("inf")
        while not self.stopping.is_set() and remaining > 0:
            remaining -= 1
            async with self.ledger.db.execute(
                "SELECT message,payload,received_at FROM inbox WHERE account=? AND team=? "
                "AND state='pending'"
                + (
                    " AND dispatch_class='control'"
                    if control
                    else " AND dispatch_class<>'control'"
                )
                + (" AND sender=?" if sender is not None else "")
                + " ORDER BY rowid LIMIT 1",
                (self.account, group, sender)
                if sender is not None
                else (self.account, group),
            ) as cursor:
                row = await cursor.fetchone()
            if (
                row is None
                or self.connection_state != "online"
                or not self.config.get("_business_active")
                or (
                    not group.startswith("private/")
                    and group not in self.enabled_groups
                )
            ):
                return
            mid, encoded, received_at = row
            processing_started = time.monotonic()
            if received_at is not None:
                self.diagnostics.observe(
                    "queue_wait", max(0, time.time() - received_at)
                )
            try:
                payload = json.loads(encoded)
                if group in self.ledger.review_groups:
                    payload["_audit_incomplete"] = True
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
            message.raw_message = {
                "message_id": mid,
                "_transport_peer": (
                    payload.get("transport_peer", "")
                    if private
                    else payload.get("recall_route", {}).get("peer", "")
                ),
            }
            if private and not message.raw_message["_transport_peer"]:
                try:
                    message.raw_message["_transport_peer"] = wire.unb64(
                        group.split("/", 2)[2], 1024
                    ).decode()
                except (ValueError, UnicodeError, wire.ProtocolError):
                    pass
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
            event.set_extra("wsl_received_at", received_at)
            if test_scope == "group_rules":
                event.set_extra("_context_only", True)
            self.events.add(event)
            self.diagnostics.emit("process", "started", mid)
            try:
                await self._event_queue.put(event)
                await event.processing_completion
                await self.ledger.mark(self.account, group, mid, "processed")
                self.diagnostics.emit("process", "completed", mid)
                self.diagnostics.observe(
                    "event_total", time.monotonic() - processing_started
                )
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

    async def get_cached_members(self, group: str) -> dict:
        """Return a detached complete directory for non-executing reads.

        Args:
            group: Currently selected group.

        Returns:
            A complete snapshot at most fifteen seconds old.

        Raises:
            ProtocolError: When disconnected, unselected or incomplete.
        """
        if self.connection_state != "online" or group not in self.enabled_groups:
            raise wire.ProtocolError("moderation_identity")
        return await self.refresh_member_mapping(group, max_age=15)

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
        """Read account-visible invitation and membership transition events.

        Args:
            group: Enabled business group ID.
            member: Optional exact invited business account ID.
            pending_only: Limit to pending invitations; otherwise retain
                INVITATION, APPLY and departure events (LEAVE, KICK_MEMBER,
                KICK, REMOVE) without collapsing history or normalizing enums.
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
                event_type = row.get("groupApplyType")
                if (
                    not isinstance(event_type, str)
                    or event_type
                    not in {
                        "INVITATION",
                        "LEAVE",
                        "APPLY",
                        "KICK_MEMBER",
                        "KICK",
                        "REMOVE",
                    }
                    or (pending_only and event_type != "INVITATION")
                ):
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
                    "event_type": event_type,
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
                if not pending_only:
                    stamp = row.get("applyTime")
                    if (
                        not item["member_id"]
                        or not isinstance(stamp, str)
                        or not 0 < len(stamp) <= 64
                    ):
                        raise wire.ProtocolError("invitation_response")
                    try:
                        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        if observed.tzinfo is None or observed > datetime.now(
                            timezone.utc
                        ):
                            raise ValueError
                    except (ValueError, OverflowError):
                        raise wire.ProtocolError("invitation_response") from None
                    item["apply_time"] = stamp
                if member and item["member_id"] != member:
                    continue
                item["status"] = (
                    "attributed"
                    if item["member_id"] and item["inviter_id"]
                    else "unattributed"
                )
                items.append(item)
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
            nim, team_id, business = self.nim, self.groups[group], self.business
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
                or self.business is not business
                or self.groups.get(group) != team_id
                or str(self.business.fields[3]) != self.account
            ):
                raise wire.ProtocolError("invitation_scope")
            response = await business.request("/v1/group/get-apply-logs", {})
            evidence = latest_membership_events(response, group)
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or not self.config.get("enable", True)
                or group not in self.config.get("enabled_groups", [])
                or self.nim is not nim
                or self.business is not business
                or self.groups.get(group) != team_id
                or str(business.fields[3]) != self.account
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
                or self.business is not business
                or self.groups.get(group) != team_id
                or str(self.business.fields[3]) != self.account
            ):
                raise wire.ProtocolError("invitation_scope")
            peers = {row["nimId"]: row for row in roster_members}
            users = {row["userId"]: row for row in roster_members}
            items = []
            for row in selected:
                current = evidence.get(row["userId"])
                event = current["row"] if current else {}
                person = event.get("inviter")
                person = person if isinstance(person, dict) else {}
                inviter = users.get(str(person.get("userId", "")))
                inviter_peer = str(person.get("nimId") or "")
                status = "unattributed"
                if current and current["ambiguous"]:
                    status = "evidence_conflict"
                elif (
                    event.get("groupApplyType") == "INVITATION"
                    and event.get("state") == "MEMBER_STATE_GOOD"
                ):
                    if str(event["applicant"].get("nimId") or "") != row["nimId"]:
                        status = "evidence_conflict"
                    elif inviter and peers.get(inviter_peer) is inviter:
                        status = (
                            "attributed"
                            if inviter["userId"] != row["userId"]
                            else "evidence_conflict"
                        )
                    elif inviter:
                        status = "evidence_conflict"
                    elif person.get("userId"):
                        status = "inviter_not_in_roster"
                if status != "attributed":
                    inviter, inviter_peer = None, ""
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
                        "status": status,
                        "apply_id": str(event.get("applyId", "")),
                        "apply_time": str(event.get("applyTime", ""))[:64],
                        "nim_conflict": bool(
                            mapping.get(row["nimId"])
                            and mapping[row["nimId"]] != inviter_peer
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
                "source": "business_current_member_inviter",
                "observed_at": int(time.time()),
                "scope": "current_members",
                "member_count": len(roster_members),
                "queried_count": len(selected),
                "complete": bool(member)
                or requested is not None
                or len(selected) == len(roster_members),
                "items": items,
            }

    async def execute_action(self, request, *, authorize=None) -> dict:
        """Execute the versioned business-to-transport action contract.

        Args:
            request: Immutable versioned action intent.
            authorize: Optional trusted business reauthorization callback.

        Returns:
            Versioned receipt with the original account and operation binding.
        """
        from .actions import dispatch

        return await dispatch(self, request, authorize=authorize)

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
        action_check=None,
        blacklist_intent=None,
        blacklist_fixture=None,
        blacklist_guard=None,
    ) -> dict:
        """Serialize group toggles and pause schedules before manual changes.

        Args:
            operation: Idempotent operation identifier.
            action: Authorized operation name.
            group: Target group ID.
            member: Optional member ID.
            text: Optional operation text.
            minutes: Explicit member mute duration; internal default is one minute.
            scheduled_check: Internal asynchronous schedule authorization callback.
            action_check: Versioned action identity and business authority callback.
            blacklist_intent: Durable canonical-join authority for blacklist actions.

        Returns:
            Upstream operation evidence.
        """
        if action not in {"mute_all", "unmute_all"}:
            return await self._execute_moderation(
                operation,
                action,
                group,
                member,
                text,
                minutes=minutes,
                action_check=action_check,
                blacklist_intent=blacklist_intent,
                blacklist_fixture=blacklist_fixture,
                blacklist_guard=blacklist_guard,
            )
        if blacklist_intent is not None or operation.startswith("blacklist/"):
            raise wire.ProtocolError("blacklist_intent_invalid")
        from .database import AsyncDatabase, account_guard
        from .policy import authorize_action
        from .schedule_store import database, group_lock

        account = self.account
        guard = account_guard(
            self, lambda: authorize_action(self.config, str(group), action)
        )
        async with group_lock(self, str(group)):
            guard()
            if action_check:
                checked = action_check()
                if inspect.isawaitable(checked):
                    await checked
            authorize_action(self.config, str(group), action)
            if scheduled_check:
                checked = scheduled_check()
                if inspect.isawaitable(checked):
                    await checked
            else:
                async with AsyncDatabase(
                    lambda: database(self), write=True, guard=guard
                ) as db:
                    await db.execute(
                        "UPDATE confirmations SET used=1 WHERE account=? AND group_id=?",
                        (account, str(group)),
                    )
                    await db.execute(
                        "UPDATE schedules SET status='paused',version=version+1,error=? "
                        "WHERE account=? AND group_id=? AND status<>'deleted'",
                        ("manual_override", account, str(group)),
                    )
            guard()
            authorize_action(self.config, str(group), action)
            return await self._execute_moderation(
                operation,
                action,
                group,
                member,
                text,
                scheduled_check=scheduled_check,
                action_check=action_check,
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
        action_check=None,
        blacklist_intent=None,
        blacklist_fixture=None,
        blacklist_guard=None,
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
            action_check: Recheck the versioned action before submission.
            blacklist_intent: Stored blacklist capability, independently rechecked.

        Returns:
            Accepted, verified or unknown operation result.

        Raises:
            ProtocolError: If the connection, identity or platform role is invalid.
        """
        from .actions import AUTOMATIC_KICK_PREFIXES
        from .blacklist import authorize_intent, protected_target
        from .database import AsyncDatabase, account_guard
        from .group_scope import group_revision
        from .moderation import execute
        from .policy import authorize_action

        guard = account_guard(self)
        blacklist_action = (
            operation.startswith("blacklist/") or blacklist_intent is not None
        )
        if blacklist_action:
            client, account = self.business, self.account
            revision = group_revision(self.config, str(group))
            await asyncio.to_thread(
                authorize_intent,
                self.config["id"],
                self.account,
                operation,
                action,
                group,
                member,
                minutes,
                blacklist_intent,
            )
            guard()

        def require_manual_kick():
            if (
                action == "kick"
                and self.config.get("moderation", {}).get("manual_kick_only")
                and not operation.startswith("confirmed-kick/")
            ):
                raise wire.ProtocolError("private_confirmation_required")

        require_manual_kick()
        if action == "kick" and operation.startswith(AUTOMATIC_KICK_PREFIXES):
            raise wire.ProtocolError("automatic_kick_not_authorized")
        progressive_mute = action == "mute" and operation.startswith("progressive/")
        expected_peer = self.members.get(str(group), {}).get(str(member))
        if blacklist_action and (
            expected_peer != blacklist_intent.peer
            or str(member) == self.account
            or await asyncio.to_thread(
                protected_target,
                self,
                str(group),
                str(member),
                expected_peer,
                fixture=blacklist_fixture,
                intent=blacklist_intent,
            )
        ):
            raise wire.ProtocolError("blacklist_target_changed")
        if progressive_mute:
            from .progressive import authorize as authorize_progressive
            from .test_rules import rule_message

            authorize_progressive(
                self,
                str(group),
                str(member),
                expected_peer,
                rule_message(self, str(group), str(member), operation),
            )
        if action == "kick" and (
            self.config.get("moderation", {}).get("content_rules_since")
            or self.config.get("moderation", {}).get("manual_kick_only")
        ):
            path = instance_dir(self.config["id"]) / "moderation.sqlite3"
            if not operation.startswith("confirmed-kick/") or not path.exists():
                raise wire.ProtocolError("private_confirmation_required")
            async with AsyncDatabase(lambda: sqlite3.connect(path), guard=guard) as db:
                row = await db.fetchone(
                    "SELECT account,group_id,member,used FROM kick_confirmations WHERE token=?",
                    (operation.removeprefix("confirmed-kick/"),),
                )
            if not row or row != (self.account, str(group), str(member), 1):
                raise wire.ProtocolError("private_confirmation_required")
        authorize_action(self.config, str(group), action)
        await self.get_moderation_members(str(group))
        guard()
        if self.stopping.is_set():
            raise wire.ProtocolError("not_online")
        authorize_action(self.config, str(group), action)

        async def authorize():
            guard()
            if action_check:
                checked = action_check()
                if inspect.isawaitable(checked):
                    await checked
            if scheduled_check:
                checked = scheduled_check()
                if inspect.isawaitable(checked):
                    await checked
            guard()
            if blacklist_action:
                await asyncio.to_thread(
                    authorize_intent,
                    self.config["id"],
                    self.account,
                    operation,
                    action,
                    group,
                    member,
                    minutes,
                    blacklist_intent,
                )
                guard()
                if (
                    self.members.get(str(group), {}).get(str(member)) != expected_peer
                    or str(member) == self.account
                    or await asyncio.to_thread(
                        protected_target,
                        self,
                        str(group),
                        str(member),
                        expected_peer,
                        fixture=blacklist_fixture,
                        intent=blacklist_intent,
                    )
                ):
                    raise wire.ProtocolError("blacklist_target_changed")
            require_manual_kick()
            authorize_action(
                self.config if not self.stopping.is_set() else {"enable": False},
                str(group),
                action,
            )
            if progressive_mute:
                authorize_progressive(
                    self,
                    str(group),
                    str(member),
                    expected_peer,
                    rule_message(self, str(group), str(member), operation),
                )

        parameters = {}
        if blacklist_action:

            def submission_guard():
                guard()
                if blacklist_guard is not None:
                    blacklist_guard()
                if self.business is not client or str(client.fields[3]) != account:
                    raise wire.ProtocolError("moderation_identity")
                if group_revision(self.config, str(group)) != revision:
                    raise wire.ProtocolError("action_scope_changed")
                authorize_action(
                    self.config if not self.stopping.is_set() else {"enable": False},
                    str(group),
                    action,
                )

            parameters["blacklist_intent"] = blacklist_intent
            parameters["blacklist_fixture"] = blacklist_fixture
            parameters["submission_guard"] = submission_guard
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
            expected_nim=expected_peer
            if progressive_mute or blacklist_action
            else None,
            authorize=authorize,
            **parameters,
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

        async def check():
            if authorize is not None:
                checked = authorize()
                if inspect.isawaitable(checked):
                    await checked
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or self.account != account
                or self.business is not business
            ):
                raise wire.ProtocolError("not_online")
            authorize_action(self.config, str(group), "rename")

        async with self.send_lock:
            await check()
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

        async def check():
            if authorize is not None:
                checked = authorize()
                if inspect.isawaitable(checked):
                    await checked
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or self.account != account
                or self.business is not business
            ):
                raise wire.ProtocolError("not_online")
            authorize_action(self.config, str(group), "cleanup")

        async with self.send_lock:
            await check()
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

    async def recall_violation(
        self, group: str, mid: str, sender: str, *, authorize=None
    ) -> str:
        """Recall a stored violation once, without retrying ambiguous results.

        Args:
            group: Enabled business group ID.
            mid: Stored server message ID.
            sender: Expected business sender ID.
            authorize: Optional trusted business authority check.

        Returns:
            Accepted, rejected, or unknown transport status.

        Raises:
            ProtocolError: If authorization, roles or stored routing are invalid.
        """
        from .database import account_guard
        from .policy import authorize_action

        guard = account_guard(self)
        account, nim, ledger = self.account, self.nim, self.ledger
        peer = self.members.get(group, {}).get(sender)
        team = self.groups.get(group)

        async def check():
            guard()
            authorize_action(self.config, group, "recall")
            if authorize is not None:
                checked = authorize()
                if inspect.isawaitable(checked):
                    await checked
            guard()
            authorize_action(self.config, group, "recall")
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or self.nim is not nim
                or self.ledger is not ledger
                or not peer
                or self.members.get(group, {}).get(sender) != peer
                or not team
                or self.groups.get(group) != team
            ):
                raise wire.ProtocolError("recall_scope_changed")

        await check()
        directory = await self.get_moderation_members(group)
        await check()
        roster = directory["groupMemberInfo"]
        roles = {str(m["userId"]): m.get("groupRole") for m in roster}
        if (
            not directory.get("complete")
            or roles.get(account) not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
            or roles.get(sender) != "GROUP_ROLE_MEMBER"
        ):
            raise wire.ProtocolError("moderation_permission")
        async with ledger.db.execute(
            "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
            (account, group, mid),
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
            or route.get("peer") != peer
        ):
            raise wire.ProtocolError("recall_identity")
        async with self.send_lock:
            await check()
            key = f"recall/{account}/{group}/{mid}"
            _, state = await ledger.reserve(key, json.dumps(route, sort_keys=True))
            if state != "new":
                return state
            try:
                await check()
            except wire.ProtocolError:
                await ledger.finish(key, "rejected")
                raise
            try:
                code, _ = await nim.request(
                    7,
                    13,
                    wire.properties(
                        [
                            (0, str(route["time"]).encode()),
                            (1, b"8"),
                            (2, team.encode()),
                            (3, route["peer"].encode()),
                            (10, route["client"].encode()),
                            (11, mid.encode()),
                            (16, route["peer"].encode()),
                        ]
                    ),
                )
                result = "accepted" if code == 200 else "rejected"
            except BaseException:
                await ledger.finish(key, "unknown")
                raise
            await ledger.finish(key, result)
            self.diagnostics.emit("recall", result, mid)
            return result

    async def quote_message(self, group, message):
        """Resolve an exact group quote from authenticated local receive evidence.

        Args:
            group: Business group in this account's inbox namespace.
            message: Original NIM server message ID.

        Returns:
            Bounded quote payload and native routing metadata.

        Raises:
            ProtocolError: If the reference is absent, recalled or no longer trusted.
        """
        message = str(message)
        if (
            group.startswith("private/")
            or not 1 <= len(message) <= 20
            or not message.isascii()
            or not message.isdigit()
            or not 0 < int(message) < 1 << 64
        ):
            raise wire.ProtocolError("quote_identity")
        async with self.ledger.db.execute(
            "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
            (self.account, group, message),
        ) as cursor:
            row = await cursor.fetchone()
        async with self.ledger.db.execute(
            "SELECT 1 FROM observed_recalls "
            "WHERE account=? AND group_id=? AND message_id=? LIMIT 1",
            (self.account, group, message),
        ) as cursor:
            recalled = await cursor.fetchone()
        data = json.loads(row[0]) if row else {}
        route = data.get("recall_route", {})
        peer = self.members.get(group, {}).get(data.get("sender"))
        receipt = await self.ledger.receipt(f"recall/{self.account}/{group}/{message}")
        if (
            not isinstance(data.get("text"), str)
            or not data["text"].strip()
            or data.get("_audit_incomplete")
            or recalled
            or not peer
            or route.get("peer") != peer
            or not isinstance(route.get("client"), str)
            or not 0 < len(route["client"].encode()) <= 128
            or not str(route.get("time", "")).isascii()
            or not 1 <= len(str(route.get("time", ""))) <= 19
            or not str(route.get("time", "")).isdigit()
            or not 0 < int(route["time"]) < 1 << 63
            or receipt
            and receipt["status"] in {"accepted", "verified", "unknown", "sending"}
        ):
            raise wire.ProtocolError("quote_unavailable")
        return {
            "message": message,
            "client": route["client"],
            "time": str(route["time"]),
            "peer": peer,
            "text": data["text"].encode()[:1024].decode("utf-8", errors="ignore"),
        }

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
        authorize=None,
        reply_to=None,
    ) -> str:
        """Persist intent before transport; uncertain sends are query-only.

        Args:
            group: Verified group or private route.
            key: Stable operation identifier.
            text: Bounded plain text.
            mentions: Native UTF-16 mention descriptors.
            test_mid: Optional developer-window correlation.
            proactive: Require a saved proactive-send grant.
            auto_recall: Persist a twelve-second self-recall after acceptance.
            authorize: Optional trusted business check before submission.
            reply_to: Exact received group message to quote.

        Returns:
            Durable native send status.
        """
        from .database import account_guard

        guard = account_guard(self)
        account, nim, business, ledger = (
            self.account,
            self.nim,
            self.business,
            self.ledger,
        )
        nim_account = self.nim_account
        own_peer = self.members.get(group, {}).get(account)
        team = self.groups.get(group)
        quote = (
            await self.quote_message(group, reply_to) if reply_to is not None else None
        )

        async def check():
            guard()
            if quote is not None and await self.quote_message(group, reply_to) != quote:
                raise wire.ProtocolError("quote_changed")
            guard()
            if authorize is not None:
                checked = authorize()
                if inspect.isawaitable(checked):
                    await checked
            guard()
            if (
                self.nim is not nim
                or self.business is not business
                or self.ledger is not ledger
                or self.nim_account != nim_account
                or (not group.startswith("private/") and self.groups.get(group) != team)
                or (
                    auto_recall is True
                    and not group.startswith("private/")
                    and self.members.get(group, {}).get(account) != own_peer
                )
            ):
                raise wire.ProtocolError("send_scope_changed")

        async with self.send_lock:
            await check()
            private = group.startswith("private/")
            if (
                not self.config.get("_business_active")
                or not self.config.get("enable", True)
                or (not private and group not in self.enabled_groups)
            ):
                raise wire.ProtocolError("business_service_inactive")
            if proactive and (
                not self.config.get("enable", True)
                or not self.config.get("proactive_send", {}).get("enabled", False)
                or group not in self.config.get("proactive_send", {}).get("targets", [])
            ):
                raise wire.ProtocolError("proactive_not_authorized")
            if (
                self.stopping.is_set()
                or self.connection_state != "online"
                or not self.config.get("_business_active")
                or (not private and group not in self.enabled_groups)
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
                async with ledger.db.execute(
                    "SELECT 1 FROM inbox WHERE account=? AND team=? LIMIT 1",
                    (account, group),
                ) as cursor:
                    if await cursor.fetchone() is None:
                        raise wire.ProtocolError("private_peer_unknown")
                _, target, encoded_peer = group.split("/", 2)
                transport_target = wire.unb64(encoded_peer, 1024).decode()
            else:
                target, transport_target = group, team
            if not text or len(text.encode()) > 4096:
                raise wire.ProtocolError("text_limit")
            inner = _text_message(
                self.config["id"],
                account,
                target,
                key,
                text,
                mentions,
                private,
                int(time.time() * 1000),
                quote=quote,
            )
            if inner.ByteSize() > 4096:
                raise wire.ProtocolError("message_limit")
            recall_reply = auto_recall is True and not private
            fingerprint = json.dumps(
                [group, text, mentions]
                + ([True] if recall_reply else [])
                + ([quote] if quote is not None else []),
                ensure_ascii=True,
            )
            nonce, state = await ledger.reserve(key, fingerprint)
            if state != "new":
                self.diagnostics.emit("send", "existing_intent", key)
                return state
            self.diagnostics.emit("send", "reserved", key)
            client_id = inner.client_id
            try:
                attachment = wire.seal_message(
                    business.deployment.key("message_key"),
                    inner,
                    int(nonce),
                    secrets.randbits(64) or 1,
                    int(time.time()),
                )
                await check()
            except BaseException:
                await ledger.finish(key, "rejected")
                raise
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
                or not self.config.get("_business_active")
                or not self.config.get("enable", True)
                or (not private and group not in self.enabled_groups)
            ):
                await ledger.finish(key, "rejected")
                return "rejected"
            try:
                fields = [
                    (0, b"0" if private else b"1"),
                    (1, transport_target.encode()),
                    (8, b"100"),
                    (10, attachment.encode()),
                    (11, client_id.encode()),
                ]
                if quote is not None:
                    # Official NIM SDK messageTag fields 29-38 identify the reply
                    # and its root. A new thread may use the same original.
                    values = (
                        quote["peer"],
                        transport_target,
                        quote["time"],
                        quote["message"],
                        quote["client"],
                    )
                    fields.extend(
                        (base + index, value.encode())
                        for base in (29, 34)
                        for index, value in enumerate(values)
                    )
                code, body = await nim.request(
                    7 if private else 8,
                    1 if private else 2,
                    wire.properties(fields),
                )
                if code != 200:
                    await ledger.finish(key, "rejected")
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
                        or not nim_account
                        or own_peer != str(nim_account)
                    ):
                        raise wire.ProtocolError("reply_recall_identity")
                    recall = (
                        account,
                        group,
                        transport_target,
                        str(nim_account),
                        client_id,
                        receipt[7].decode(),
                        time.time() + 12,
                    )
                await ledger.finish(
                    key, "accepted", receipt[12].decode(), recall=recall
                )
                self.diagnostics.emit("send", "accepted", key)
                return "accepted"
            except BaseException:
                await ledger.finish(key, "unknown")
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
        authorize=None,
        reply_to=None,
    ):
        """Send a bounded logical reply with stable, non-overlapping segments.

        Args:
            group: Verified native target.
            key: Stable logical operation identifier.
            text: Formatted reply text.
            mentions: UTF-16 mention ranges.
            test_mid: Correlated developer-window inbound message.
            proactive: Require explicit target authorization.
            auto_recall: Recall each accepted group segment after twelve seconds.
            authorize: Synchronous or asynchronous original business authority.
            reply_to: Received group message quoted by the first segment only.

        Returns:
            Durable logical operation status.
        """
        from .database import account_guard

        guard = account_guard(self)
        ledger, nim, business = self.ledger, self.nim, self.business
        team = self.groups.get(group)
        nim_account = self.nim_account
        own_peer = self.members.get(group, {}).get(self.account)
        quote = (
            await self.quote_message(group, reply_to) if reply_to is not None else None
        )

        async def check():
            guard()
            if quote is not None and await self.quote_message(group, reply_to) != quote:
                raise wire.ProtocolError("quote_changed")
            guard()
            if authorize is not None:
                checked = authorize()
                if inspect.isawaitable(checked):
                    await checked
            guard()
            if (
                self.ledger is not ledger
                or self.nim is not nim
                or self.business is not business
                or self.nim_account != nim_account
                or (not group.startswith("private/") and self.groups.get(group) != team)
                or (
                    auto_recall is True
                    and not group.startswith("private/")
                    and self.members.get(group, {}).get(self.account) != own_peer
                )
            ):
                raise wire.ProtocolError("reply_scope_changed")

        fingerprint = json.dumps(
            [group, text, mentions]
            + ([True] if auto_recall is True else [])
            + ([quote] if quote is not None else []),
            ensure_ascii=True,
        )
        async with self.send_lock:
            await check()
            _, state = await ledger.reserve(key, fingerprint)
        if state != "new":
            return "unknown" if state == "sending" else state
        offset = 0
        accepted_parts = 0
        child_key = None
        try:
            if not text:
                raise wire.ProtocolError("text_limit")
            private = group.startswith("private/")
            target = group.split("/", 2)[1] if private else group
            encoded = text.encode("utf-16-le")
            previous = 0
            for mention in mentions or []:
                if not (
                    type(mention.get("start")) is int
                    and type(mention.get("end")) is int
                    and previous
                    <= mention["start"]
                    < mention["end"]
                    <= len(encoded) // 2
                    and encoded[mention["start"] * 2 : mention["end"] * 2]
                    == f"@{mention['nick']} ".encode("utf-16-le")
                ):
                    raise wire.ProtocolError("mention_range")
                previous = mention["end"]
            while text:
                await check()
                # Binary search the complete protobuf, not just UTF-8 content.
                # The timestamp upper bound also covers future varint growth.
                unit_offsets = [0]
                for char in text[:4096]:
                    unit_offsets.append(
                        unit_offsets[-1] + (2 if ord(char) > 0xFFFF else 1)
                    )
                low, high = 0, len(unit_offsets) - 1
                part_key = f"{key}/part/{offset}"
                while low < high:
                    candidate = (low + high + 1) // 2
                    candidate_units = unit_offsets[candidate]
                    candidate_mentions = [
                        {
                            **m,
                            "start": m["start"] - offset,
                            "end": m["end"] - offset,
                        }
                        for m in mentions or []
                        if offset <= m["start"] < offset + candidate_units
                    ]
                    inner = _text_message(
                        self.config["id"],
                        self.account,
                        target,
                        part_key,
                        text[:candidate],
                        candidate_mentions,
                        private,
                        (1 << 63) - 1,
                        quote=quote if not accepted_parts else None,
                    )
                    if inner.ByteSize() <= 4096:
                        low = candidate
                    else:
                        high = candidate - 1
                end = low
                units = unit_offsets[end]
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
                child_key = part_key
                result = await self.send_text(
                    group,
                    part_key,
                    text[:end],
                    mentions=part_mentions or None,
                    test_mid=test_mid,
                    proactive=proactive,
                    authorize=check,
                    **({"auto_recall": True} if auto_recall is True else {}),
                    **(
                        {"reply_to": reply_to}
                        if quote is not None and not accepted_parts
                        else {}
                    ),
                )
                if result not in {"accepted", "verified"}:
                    # A partially delivered logical message is never retryable.
                    result = "unknown" if accepted_parts else result
                    await ledger.finish(key, result)
                    return result
                accepted_parts += 1
                text = text[end:]
                offset += units
            child = await ledger.receipt(f"{key}/part/0")
            await ledger.finish(
                key, "accepted", child.get("server_id", "") if child else ""
            )
            return "accepted"
        except BaseException:
            child = await ledger.receipt(child_key) if child_key else None
            status = (
                "unknown"
                if accepted_parts or (child and child["status"] != "rejected")
                else "rejected"
            )
            await ledger.finish(key, status)
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
        replies = [part for part in parts if isinstance(part, Reply)]
        if len(replies) > 1:
            raise wire.ProtocolError("quote_identity")
        reply_to = (
            str(replies[0].id)
            if replies and session.message_type is MessageType.GROUP_MESSAGE
            else None
        )
        parts = [part for part in parts if not isinstance(part, Reply)]
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
            target,
            key,
            text,
            mentions=people or None,
            proactive=True,
            **({"reply_to": reply_to} if reply_to is not None else {}),
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
