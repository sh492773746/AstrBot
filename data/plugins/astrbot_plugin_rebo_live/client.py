"""Authenticated Rebo HTTP and IVS transport with sanitized failures."""

import asyncio
import base64
import hashlib
import json
import time
import uuid

import aiohttp
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.serialization import load_pem_public_key

BASE = "https://api.partdvo.com/api/v2/"


class RemoteError(Exception):
    """Expose only an upstream numeric code, never an upstream payload."""

    def __init__(self, code, business_code=None, endpoint=None):
        self.code = code
        self.business_code = business_code
        self.endpoint = endpoint
        super().__init__(f"服务返回错误码：{code}")


class Client:
    def __init__(self, session, device, tokens):
        self.session = session
        self.device = device
        self.tokens = tokens

    async def request(self, path, data=None, authenticated=True):
        """Call a known service endpoint without logging its payload.

        Args:
            path: Relative API path.
            data: Optional JSON body.
            authenticated: Whether to attach the account token.

        Returns:
            Decoded response data.

        Raises:
            RemoteError: HTTP or application failure.
        """
        headers = {"X-Device-ID": self.device, "X-Request-ID": uuid.uuid4().hex}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.tokens.get("access_token", "")
        async with self.session.request(
            "POST" if data is not None else "GET",
            BASE + path,
            json=data,
            headers=headers,
        ) as response:
            if response.status != 200:
                raise RemoteError(response.status, endpoint=path.split("?", 1)[0])
            payload = await response.json()
            if payload.get("code") != 0:
                raise RemoteError(
                    payload.get("code", "unknown"), endpoint=path.split("?", 1)[0]
                )
            return payload.get("data") or {}

    async def login(self, phone, password):
        """Encrypt the password using the service's RSA public key.

        Args:
            phone: Account phone number.
            password: In-memory plaintext password.
        """
        key = await self.request("user/auth/password/public-key", authenticated=False)
        encrypted = load_pem_public_key(key["public_key"].encode()).encrypt(
            password.encode(), PKCS1v15()
        )
        data = await self.request(
            "user/auth/login/password",
            {
                "phone": phone,
                "phone_region": "CN",
                "password": "enc:" + base64.b64encode(encrypted).decode(),
            },
            authenticated=False,
        )
        self.tokens = {k: data[k] for k in ("access_token", "refresh_token")}

    async def rooms(self):
        """Read all room pages from enabled non-video categories.

        Returns:
            Deduplicated rooms visible to the current account.
        """
        zones = await self.request("live/zones")
        rooms = {}
        for zone in zones.get("zones", []):
            if str(zone.get("id")) == "3":
                continue
            for sub in zone.get("subcategories", []):
                for page in range(1, 51):
                    data = await self.request(
                        f"live/zones/rooms?subcategory_id={sub['id']}&page={page}&page_size=100"
                    )
                    for room in data.get("list", []):
                        rooms[str(room["id"])] = {
                            k: room.get(k)
                            for k in ("id", "title", "room_no", "status", "status_text")
                        }
                        user = room.get("user") or {}
                        streamer = room.get("streamer") or {}
                        rooms[str(room["id"])]["anchor_name"] = (
                            user.get("nickname") or streamer.get("nickname") or ""
                        )
                    if page >= data.get("total_pages", 1):
                        break
        return rooms

    async def connect(self, room):
        """Open an IVS socket without sending an entrance event.

        Args:
            room: Live room ID.

        Returns:
            Open WebSocket, or None while the room is muted.
        """
        info = await self.request(f"room/info?room_id={room}")
        # ``all_muted`` describes the room's ordinary-member mute switch.  It
        # is not a reliable capability check for the logged-in account: room
        # administrators may still be allowed to post.  Always request a chat
        # token and let the platform's SEND_MESSAGE response decide whether
        # this account can speak.  Treating this flag as a hard stop caused a
        # stale "全员禁言" state after an account was promoted.
        token = await self.request(
            "room/generate-chat-token", {"room_id": room, "duration_minutes": 60}
        )
        region = info["region"]
        if not isinstance(region, str) or not all(
            c.isalnum() or c == "-" for c in region
        ):
            raise ValueError("Invalid IVS region")
        return await self.session.ws_connect(
            f"wss://edge.ivschat.{region}.amazonaws.com", protocols=[token["token"]]
        )

    async def send(self, ws, text, request_id, diagnostics=None):
        """Send once and wait for a matching positive or negative receipt.

        Args:
            ws: Connected IVS socket.
            text: Approved room message.
            request_id: Persisted unique attempt ID.
            diagnostics: Optional caller-owned, payload-free diagnostic counters.

        Returns:
            Confirmed message ID.

        Raises:
            RemoteError: Explicit rejection.
            TimeoutError: Unknown delivery outcome.
        """
        diagnostic = diagnostics if diagnostics is not None else {}
        diagnostic.update(stage="write", frames=0, unmatched=0, missing_request=0)
        started = time.monotonic()
        try:
            return await self._send_receipt(ws, text, request_id, diagnostic)
        except BaseException as exc:
            diagnostic["exception"] = type(exc).__name__
            diagnostic["reason"] = (
                "task_cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "receipt_timeout"
                if isinstance(exc, TimeoutError)
                else "connection_error"
                if isinstance(exc, (ConnectionError, aiohttp.ClientError))
                else "invalid_receipt"
                if isinstance(exc, (ValueError, TypeError))
                else "platform_rejected"
                if isinstance(exc, RemoteError)
                else "unexpected_error"
            )
            raise
        finally:
            diagnostic["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            code = getattr(ws, "close_code", None)
            diagnostic["close_code"] = code if type(code) is int else None

    async def _send_receipt(self, ws, text, request_id, diagnostic):
        """Send once and collect bounded metadata while matching the receipt.

        Args:
            ws: Connected socket.
            text: Approved message.
            request_id: Expected correlation identifier.
            diagnostic: Counters only; never store frames or content.
        """
        await ws.send_json(
            {
                "Action": "SEND_MESSAGE",
                "RequestId": request_id,
                "Content": json.dumps(
                    {"type": "chat", "client_event_id": request_id, "content": text},
                    ensure_ascii=False,
                ),
            }
        )
        diagnostic["stage"] = "receipt"
        deadline = time.monotonic() + 15
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Chat receipt deadline exceeded")
            frame = await ws.receive(timeout=max(0.01, deadline - time.monotonic()))
            diagnostic["frames"] += 1
            diagnostic["frame_type"] = int(frame.type)
            if frame.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError("Chat connection closed")
            message = json.loads(frame.data)
            if not isinstance(message, dict):
                raise ValueError("Invalid receipt shape")
            samples = diagnostic.setdefault("samples", [])
            if len(samples) < 8:
                content = message.get("Content")
                try:
                    body = json.loads(content) if isinstance(content, str) else {}
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                sender = message.get("Sender")
                sender = sender if isinstance(sender, dict) else {}
                kind = message.get("Type")
                code = message.get("ErrorCode")
                samples.append(
                    {
                        "type": kind
                        if kind in {"MESSAGE", "ERROR", "EVENT"}
                        else "OTHER",
                        "request_match": message.get("RequestId") == request_id,
                        "request_present": bool(message.get("RequestId")),
                        "event_match": body.get("client_event_id") == request_id,
                        "content_match": content == text or body.get("content") == text,
                        "sender_present": bool(sender.get("UserId")),
                        "sender_hash": hashlib.sha256(
                            str(sender.get("UserId", "")).encode()
                        ).hexdigest()[:12]
                        if sender.get("UserId")
                        else None,
                        "message_id_present": bool(message.get("Id")),
                        "error_code": code if type(code) is int else None,
                    }
                )
            if message.get("RequestId") != request_id:
                diagnostic["unmatched"] += 1
                if not message.get("RequestId"):
                    diagnostic["missing_request"] += 1
                continue
            diagnostic["matched"] = True
            if message.get("Type") == "MESSAGE":
                if not isinstance(message.get("Id"), str) or not message["Id"]:
                    raise ValueError("Missing receipt message ID")
                return message.get("Id")
            if message.get("Type") == "ERROR":
                business_code = None
                try:
                    detail = json.loads(message.get("ErrorMessage", ""))
                    if isinstance(detail, dict) and type(detail.get("code")) is int:
                        business_code = detail["code"]
                except (ValueError, TypeError):
                    pass
                raise RemoteError(message.get("ErrorCode", "rejected"), business_code)
