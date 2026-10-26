"""Lajiao Chat adapter backed by the local OpenIM bridge."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict, deque
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.platform import (
    AstrBotMessage,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.message_policy import require_message
from astrbot.core.platform.message_session import MessageSession

from . import runtime
from .event import LajiaoLiaoEvent, chain_to_parts, incoming_to_message
from .media import SLOTS, Images
from .protocol import LajiaoLiaoProtocolError, message_payload
from .storage import SessionError


@register_platform_adapter(
    "lajiaoliao",
    "辣椒聊消息接入",
    default_config_tmpl={
        "type": "lajiaoliao",
        "enable": False,
        "id": "lajiaoliao",
        "session_ref": "",
        "message_policy": {
            "group_mode": "allowlist",
            "group_ids": [],
            "private_enabled": False,
        },
    },
    adapter_display_name="辣椒聊",
    logo_path="logo.ico",
    config_metadata={
        "session_ref": {"description": "会话引用", "type": "string"},
        "message_policy": {
            "description": "消息控制",
            "type": "object",
            "items": {
                "group_mode": {
                    "description": "群聊范围",
                    "type": "string",
                    "options": ["allowlist"],
                },
                "group_ids": {"description": "允许的群 ID", "type": "list"},
                "private_enabled": {"description": "允许私聊", "type": "bool"},
            },
        },
    },
    support_streaming_message=False,
)
class LajiaoLiaoAdapter(Platform):
    """One configured account connected through one local bridge instance."""

    supports_operation_ids = True

    @staticmethod
    def validate_dashboard_config(config, *, owner, current):
        """Validate account ownership before Dashboard configuration persistence.

        Args:
            config: Proposed instance configuration.
            owner: Authenticated Dashboard user.
            current: Previously saved configuration, if any.

        Raises:
            ValueError: If authorization or the immutable binding does not match.
        """
        if runtime.active is None:
            raise ValueError("lajiaoliao_plugin_unavailable")
        if current and current.get("type") != "lajiaoliao":
            raise ValueError("platform_type_change")
        ref = str(config.get("session_ref", ""))
        try:
            runtime.active.store.get(ref, owner)
            runtime.active.store.for_instance(ref, str(config.get("id", "")))
            commerce = getattr(runtime.active, "commerce", None)
            if commerce is not None:
                commerce.validate_config(config)
        except SessionError as exc:
            raise ValueError(str(exc)) from exc
        if current and current.get("session_ref") != ref:
            raise ValueError("account_change_requires_new_instance")

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.session_ref = str(platform_config.get("session_ref", ""))
        self.manager = runtime.active
        if self.manager is None:
            raise LajiaoLiaoProtocolError("plugin_not_initialized")
        binding = self.manager.store.for_instance(
            self.session_ref, str(platform_config["id"])
        )
        self.account_id = binding["account_id"]
        self.client_self_id = self.account_id
        self.base_url = ""
        self.events_url = ""
        self._slots = asyncio.Semaphore(32)
        self._pending_completions: set[asyncio.Future] = set()
        self.member_ids: dict[str, str] = {}
        self.http: aiohttp.ClientSession | None = None
        self.stop_event = asyncio.Event()
        self.connection_state = "stopped"
        self.current_error = ""
        self.last_message_id = ""
        self._seen_message_ids: set[str] = set()
        self._seen_message_order: deque[str] = deque(maxlen=4096)
        self._pending_messages: dict[tuple[str, str, str], asyncio.Future] = {}
        self._revoked_messages: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self.group_nicknames: OrderedDict[tuple[str, str], str] = OrderedDict()
        self.notifications: deque[dict] = deque(maxlen=100)
        self.images = Images(self.manager.store.directory(self.session_ref))
        self._send_lock = asyncio.Lock()
        self.received_images = deque(maxlen=16)
        self.ignored_bot_messages = deque(maxlen=16)

    def require_subscription(self, *, raw=None):
        commerce = getattr(self.manager, "commerce", None)
        if commerce is not None:
            stamp = None
            if raw is not None:
                stamp = raw.get("timestamp", raw.get("send_time"))
                if isinstance(stamp, (int, float)) and stamp > 10**12:
                    stamp /= 1000
                if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
                    stamp = 0
            commerce.require(self.config["id"], message_stamp=stamp)

    def ignore_bot_sender(self, sender_id: str, *, group: bool) -> bool:
        return group and str(sender_id) in self.manager.store.bound_account_ids()

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="lajiaoliao",
            description="辣椒聊 OpenIM 本地桥接",
            id=self.config["id"],
            adapter_display_name="辣椒聊",
            support_streaming_message=False,
            support_proactive_message=True,
        )

    def get_stats(self) -> dict:
        return {
            **super().get_stats(),
            "connection_state": self.connection_state,
            "current_error": self.current_error,
            "account_id": self.account_id,
            "received_images": list(self.received_images),
            "ignored_bot_messages": list(self.ignored_bot_messages),
        }

    async def run(self) -> None:
        delay = 1
        cleanup_task = asyncio.create_task(self._cleanup_media())
        try:
            while not self.stop_event.is_set():
                try:
                    self.manager.store.for_instance(
                        self.session_ref, str(self.config["id"])
                    )
                    self.require_subscription()
                    worker = await self.manager.start(self.session_ref, connect=True)
                    self.http = worker.http
                    self.base_url = worker.endpoint
                    self.events_url = f"{self.base_url}/v1/events"
                    await self._consume_events()
                except asyncio.CancelledError:
                    raise
                except SessionError as exc:
                    self.connection_state = (
                        "subscription_inactive"
                        if str(exc).startswith("commerce_")
                        else "authorization_required"
                    )
                    self.current_error = str(exc)
                    await self.stop_event.wait()
                except Exception as exc:
                    if self.connection_state == "online":
                        delay = 1
                    self.connection_state = "error"
                    self.current_error = type(exc).__name__
                    logger.warning(
                        "LajiaoLiao connection failed: %s", self.current_error
                    )
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    delay = min(delay * 2, 30)
        finally:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
            self.connection_state = "stopped"
            for completion in tuple(self._pending_completions):
                completion.cancel()
            await self.manager.stop(self.session_ref)
            self.http = None

    async def _cleanup_media(self):
        """Sweep abandoned account media with one bounded lifecycle task."""
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(self.images.cleanup)
            except OSError:
                logger.warning(
                    "LajiaoLiao media cleanup deferred after a filesystem error"
                )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                continue

    async def _consume_events(self) -> None:
        if not self.http:
            raise LajiaoLiaoProtocolError("http_session_missing")
        self.connection_state = "connecting"
        async with self.http.ws_connect(self.events_url, heartbeat=20) as ws:
            self.connection_state = "connecting"
            self.current_error = ""
            async for item in ws:
                if item.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(item.data)
                    if payload.get("type") == "ready":
                        state = payload.get("data", {})
                        self.connection_state = str(state.get("status", "connecting"))
                        self.current_error = (
                            ""
                            if self.connection_state == "online"
                            else self.connection_state
                        )
                        if self.connection_state in {
                            "logged_out",
                            "reauth_required",
                            "kicked_offline",
                            "protocol_incompatible",
                        }:
                            await self.stop_event.wait()
                            return
                        user_id = str(state.get("account_id", ""))
                        if self.account_id and user_id and user_id != self.account_id:
                            raise LajiaoLiaoProtocolError("bridge_account_mismatch")
                        if user_id:
                            self.account_id = user_id
                        continue
                    if payload.get("type") == "error":
                        raise LajiaoLiaoProtocolError(
                            str(payload.get("error", "bridge_error"))
                        )
                    if payload.get("type") == "notification":
                        try:
                            self.require_subscription(raw=payload["data"])
                        except SessionError:
                            await ws.send_json(
                                {
                                    "type": "ack",
                                    "message_id": str(payload["data"]["message_id"]),
                                }
                            )
                            continue
                        self.apply_notification(payload["data"])
                        callback = getattr(self.manager, "on_notification", None)
                        if callback is not None:
                            await callback(self.session_ref, payload["data"])
                        await ws.send_json(
                            {
                                "type": "ack",
                                "message_id": str(payload["data"]["message_id"]),
                            }
                        )
                        continue
                    if payload.get("type") != "message":
                        continue
                    raw = payload["data"]
                    try:
                        self.require_subscription(raw=raw)
                        require_message(
                            self.config,
                            raw.get("kind", "private"),
                            str(raw["target_id"]),
                        )
                    except (PermissionError, SessionError):
                        await ws.send_json(
                            {"type": "ack", "message_id": str(raw["message_id"])}
                        )
                        continue
                    if str(raw["message_id"]) in self._seen_message_ids:
                        await ws.send_json(
                            {"type": "ack", "message_id": str(raw["message_id"])}
                        )
                        continue
                    if self.ignore_bot_sender(
                        str(raw.get("sender_id", "")),
                        group=raw.get("kind") == "group" or raw.get("is_group") is True,
                    ):
                        self._remember_message(str(raw["message_id"]))
                        self.ignored_bot_messages.append(
                            {
                                "message_id": str(raw["message_id"]),
                                "sender_id": str(raw["sender_id"]),
                                "target_id": str(raw["target_id"]),
                                "status": "ignored_bound_bot",
                            }
                        )
                        await ws.send_json(
                            {"type": "ack", "message_id": str(raw["message_id"])}
                        )
                        continue
                    image_paths = []
                    try:
                        for component in payload["data"].get("components", []):
                            if component.get("type") == "image":
                                path, _ = await self.images.materialize(
                                    component["url"], inbound=True
                                )
                                image_paths.append(path)
                                component["url"] = str(path)
                    except LajiaoLiaoProtocolError as exc:
                        self.current_error = "image_download_rejected_or_failed"
                        for path in image_paths:
                            path.unlink(missing_ok=True)
                        if exc.delivery_status == "rejected":
                            logger.warning(
                                "LajiaoLiao invalid image rejected before event handoff"
                            )
                            await ws.send_json(
                                {"type": "ack", "message_id": str(raw["message_id"])}
                            )
                            continue
                        # Do not ACK transient failure: reconnect replays this event.
                        raise LajiaoLiaoProtocolError("image_download_failed") from None
                    message = incoming_to_message(
                        payload["data"],
                        self.account_id,
                    )
                    nickname = self.group_nicknames.get(
                        (str(message.group_id), str(message.sender.user_id))
                    )
                    if nickname is not None:
                        message.sender.nickname = nickname
                    key = (
                        "group" if message.group_id else "private",
                        str(message.group_id or message.sender.user_id),
                        str(message.message_id),
                    )
                    if str(message.sender.user_id) != self.account_id:
                        if (
                            str(message.message_id) not in self._seen_message_ids
                            and key not in self._revoked_messages
                        ):
                            await self._slots.acquire()
                            event = self.create_event(message)
                            self._pending_messages[key] = event.processing_completion
                            event.processing_completion.add_done_callback(
                                lambda done, key=key: self._pending_messages.pop(
                                    key, None
                                )
                            )
                            self._pending_completions.add(event.processing_completion)
                            event.processing_completion.add_done_callback(
                                self._event_completed
                            )
                            if image_paths:
                                event.processing_completion.add_done_callback(
                                    lambda done, paths=image_paths: [
                                        path.unlink(missing_ok=True) for path in paths
                                    ]
                                )
                            try:
                                self.commit_event(event)
                            except BaseException:
                                event.processing_completion.cancel()
                                raise
                            self._remember_message(message.message_id)
                            self.last_message_id = str(message.message_id)
                            if image_paths:
                                self.received_images.append(
                                    {
                                        "message_id": str(message.message_id),
                                        "kind": key[0],
                                        "target_id": key[1],
                                        "status": "handed_off",
                                    }
                                )
                    await ws.send_json(
                        {"type": "ack", "message_id": str(message.message_id)}
                    )
                    if not image_paths or key not in self._pending_messages:
                        for path in image_paths:
                            path.unlink(missing_ok=True)
                elif item.type in {
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                }:
                    raise LajiaoLiaoProtocolError("bridge_events_closed")
            raise LajiaoLiaoProtocolError("bridge_events_closed")

    def apply_notification(self, data: dict) -> None:
        """Apply known metadata without dispatching a model message.

        Args:
            data: A validated bridge notification.
        """
        action = data.get("action")
        if action == "message_revoked":
            key = (
                str(data["kind"]),
                str(data["target_id"]),
                str(data["revoked_message_id"]),
            )
            completion = self._pending_messages.get(key)
            self._revoked_messages[key] = None
            self._revoked_messages.move_to_end(key)
            if len(self._revoked_messages) > 4096:
                self._revoked_messages.popitem(last=False)
            if completion is not None and not completion.done():
                completion.cancel()
        elif action == "group_member_updated":
            key = (str(data["group_id"]), str(data["user_id"]))
            self.group_nicknames[key] = str(data["nickname"])
            self.group_nicknames.move_to_end(key)
            if len(self.group_nicknames) > 512:
                self.group_nicknames.popitem(last=False)
        elif action == "unsupported_media":
            self.current_error = "unsupported_or_invalid_media_received"
        elif action not in {
            "group_info_updated",
            "group_announcement_updated",
            "members_kicked",
            "members_invited",
            "member_entered",
            "member_muted",
            "member_unmuted",
            "group_muted",
            "group_unmuted",
        }:
            return
        if not any(
            row["message_id"] == data["message_id"] for row in self.notifications
        ):
            self.notifications.append(dict(data))

    async def platform_action(self, body: dict) -> dict:
        """Execute a fixed action under the current outbound policy.

        Args:
            body: A fixed action request or read-only operation lookup.

        Returns:
            Durable bridge receipt, including unknown or rejected outcomes.

        Raises:
            LajiaoLiaoProtocolError: If the bridge is unavailable or rejects preflight.
        """
        if not self.http or self.connection_state != "online":
            raise LajiaoLiaoProtocolError(
                "instance_offline", delivery_status="rejected"
            )
        self.require_subscription()
        if body.get("query") is True:
            method = self.http.get
            path = f"/v1/actions/{body['operation_id']}"
            kwargs = {}
        else:
            require_message(self.config, body["kind"], body["target_id"])
            method, path, kwargs = self.http.post, "/v1/actions", {"json": body}
        async with method(self.base_url + path, **kwargs) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise LajiaoLiaoProtocolError(
                    str(data.get("error", "action_request_failed")),
                    delivery_status=data.get(
                        "status", "rejected" if response.status < 500 else "unknown"
                    ),
                )
            if data.get("status") not in {"succeeded", "rejected", "unknown"}:
                raise LajiaoLiaoProtocolError("invalid_action_receipt")
            return data

    def _remember_message(self, message_id: str) -> bool:
        message_id = str(message_id)
        if not message_id or message_id in self._seen_message_ids:
            return False
        if len(self._seen_message_order) == self._seen_message_order.maxlen:
            expired = self._seen_message_order.popleft()
            self._seen_message_ids.discard(expired)
        self._seen_message_order.append(message_id)
        self._seen_message_ids.add(message_id)
        return True

    def _event_completed(self, future: asyncio.Future) -> None:
        self._pending_completions.discard(future)
        self._slots.release()
        if not future.cancelled():
            # EventBus already logs processing failures; consume the completion.
            future.exception()

    async def send_parts(
        self,
        target_id: str,
        reply_to: str,
        parts: list[dict[str, Any]],
        kind: str = "private",
    ) -> dict[str, Any]:
        self.require_subscription()
        if not self.http:
            raise LajiaoLiaoProtocolError("bridge_not_started")
        if not parts or len(parts) > 32:
            raise LajiaoLiaoProtocolError(
                "invalid_parts_count", delivery_status="rejected"
            )
        if any(part.get("type") not in {"text", "quote", "image"} for part in parts):
            raise LajiaoLiaoProtocolError(
                "unsupported_message_component", delivery_status="unsupported"
            )
        has_image = any(part.get("type") == "image" for part in parts)
        if has_image and (
            reply_to or any(part.get("type") == "quote" for part in parts)
        ):
            raise LajiaoLiaoProtocolError(
                "image_quote_unverified", delivery_status="unsupported"
            )
        if has_image:
            return await self._send_images(target_id, parts, kind)
        payload = message_payload(target_id, reply_to, parts)
        payload["kind"] = kind
        require_message(self.config, kind, target_id)
        async with self.http.post(f"{self.base_url}/v1/messages", json=payload) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                status = data.get(
                    "status", "rejected" if resp.status < 500 else "unknown"
                )
                raise LajiaoLiaoProtocolError(
                    str(data.get("error", f"bridge_http_{resp.status}")),
                    delivery_status=status
                    if status in {"rejected", "unsupported", "failed", "unknown"}
                    else "unknown",
                )
            if data.get("status") != "sent" or not data.get("message_id"):
                raise LajiaoLiaoProtocolError("missing_send_receipt")
            return data

    async def _send_images(self, target, parts, kind):
        """Preflight the entire chain, then report every ordered native send.

        Args:
            target: Exact private or group target.
            parts: Prevalidated component kinds.
            kind: Private or group scope.

        Returns:
            Aggregate successful receipt with ordered per-message receipts.

        Raises:
            LajiaoLiaoProtocolError: Preserves partial and unknown delivery.
        """
        receipts, files, messages = [], [], []
        send_started = False
        try:
            require_message(self.config, kind, target)
            for part in parts:
                if part["type"] == "text":
                    text = part.get("text")
                    if not isinstance(text, str) or len(text.encode()) > 16384:
                        raise LajiaoLiaoProtocolError(
                            "invalid_text_length", delivery_status="rejected"
                        )
                    if text:
                        if messages and messages[-1][0]["type"] == "text":
                            messages[-1][0]["text"] += text
                            if len(messages[-1][0]["text"].encode()) > 16384:
                                raise LajiaoLiaoProtocolError(
                                    "invalid_text_length", delivery_status="rejected"
                                )
                        else:
                            messages.append([{"type": "text", "text": text}])
                else:
                    path, mime = await self.images.materialize(part.get("url", ""))
                    files.append((path, mime))
                    messages.append([{"type": "image", "path_index": len(files) - 1}])
            # Upload only after every component has passed local preflight.
            for message in messages:
                part = message[0]
                if part["type"] == "image":
                    self.require_subscription()
                    require_message(self.config, kind, target)
                    path, mime = files[part.pop("path_index")]
                    form = aiohttp.FormData()
                    async with SLOTS:
                        with path.open("rb") as stream:
                            form.add_field(
                                "image", stream, filename=path.name, content_type=mime
                            )
                            async with self.http.post(
                                self.base_url + "/v1/media/images", data=form
                            ) as response:
                                result = await response.json(content_type=None)
                                if response.status != 200 or not result.get(
                                    "image_ref"
                                ):
                                    raise LajiaoLiaoProtocolError(
                                        "image_upload_failed", delivery_status="failed"
                                    )
                                part["image_ref"] = result["image_ref"]
            async with self._send_lock:
                for message in messages:
                    self.require_subscription()
                    require_message(self.config, kind, target)
                    send_started = True
                    async with self.http.post(
                        self.base_url + "/v1/messages",
                        json={**message_payload(target, "", message), "kind": kind},
                    ) as response:
                        result = await response.json(content_type=None)
                        if (
                            response.status >= 400
                            or result.get("status") != "sent"
                            or not result.get("message_id")
                        ):
                            status = result.get(
                                "status",
                                "unknown" if response.status >= 500 else "rejected",
                            )
                            if status not in {
                                "failed",
                                "rejected",
                                "unsupported",
                                "unknown",
                            }:
                                status = "unknown"
                            exc = LajiaoLiaoProtocolError(
                                "image_batch_incomplete", delivery_status=status
                            )
                            exc.receipts = receipts + [
                                {
                                    **result,
                                    "status": status,
                                    "part_index": len(receipts),
                                }
                            ]
                            exc.partial = bool(receipts)
                            raise exc
                        receipts.append(result)
            if not receipts:
                raise LajiaoLiaoProtocolError(
                    "empty_message", delivery_status="rejected"
                )
            return {**receipts[-1], "receipts": receipts}
        except LajiaoLiaoProtocolError as exc:
            if not hasattr(exc, "receipts"):
                exc.receipts, exc.partial = receipts, bool(receipts)
            raise
        except PermissionError:
            exc = LajiaoLiaoProtocolError(
                "message_policy_rejected", delivery_status="rejected"
            )
            exc.receipts = receipts + [
                {
                    "status": "rejected",
                    "part_index": len(receipts),
                    "error": "message_policy_rejected",
                }
            ]
            exc.partial = bool(receipts)
            raise exc from None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            exc = LajiaoLiaoProtocolError(
                "image_batch_result_unknown" if send_started else "image_upload_failed",
                delivery_status="unknown" if send_started else "failed",
            )
            exc.receipts = receipts + (
                [{"status": "unknown", "part_index": len(receipts)}]
                if send_started
                else []
            )
            exc.partial = bool(receipts)
            raise exc from None
        finally:
            for path, _ in files:
                path.unlink(missing_ok=True)

    async def send_by_session(
        self,
        session: MessageSession,
        message_chain: MessageChain,
    ) -> None:
        kind, sep, target = session.session_id.partition(":")
        if not sep or kind not in {"group", "private"}:
            raise LajiaoLiaoProtocolError("invalid_session_target")
        parts = chain_to_parts(message_chain, self.member_ids)
        await self.send_parts(target, "", parts, kind=kind)
        await super().send_by_session(session, message_chain)

    async def terminate(self) -> None:
        self.stop_event.set()
        for completion in tuple(self._pending_completions):
            completion.cancel()
        await self.manager.stop(self.session_ref)

    def create_event(self, message: AstrBotMessage) -> LajiaoLiaoEvent:
        return LajiaoLiaoEvent(message, self)
