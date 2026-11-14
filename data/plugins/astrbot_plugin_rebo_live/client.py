"""Authenticated Rebo HTTP and IVS transport with sanitized failures."""

import asyncio
import base64
import hashlib
import json
import time
import uuid
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pyhpke import AEADId, CipherSuite, KDFId, KEMId

BASE = "https://api.partdvo.com/api/v2/"


class RemoteError(Exception):
    """Expose only an upstream numeric code, never an upstream payload."""

    def __init__(self, code, business_code=None, endpoint=None):
        self.code = code
        self.business_code = business_code
        self.endpoint = endpoint
        detail = f" / 业务码{business_code}" if business_code is not None else ""
        stage = f"（{endpoint}）" if endpoint else ""
        super().__init__(f"服务返回错误码：{code}{detail}{stage}")

    @property
    def review_reason(self):
        """Return an allowlisted reason without exposing upstream payloads."""
        return {
            2010392001: "内容触发平台敏感词校验",
            2010122004: "当前账号暂不可发言",
        }.get(self.business_code, "平台未提供可展示的具体审核原因")


class Client:
    def __init__(self, session, device, tokens):
        self.session = session
        self.device = device
        self.tokens = tokens
        self.hpke_key = None
        self.hpke_until = 0
        self.hpke_lock = asyncio.Lock()
        self.hpke_suite = CipherSuite.new(
            KEMId.DHKEM_X25519_HKDF_SHA256,
            KDFId.HKDF_SHA256,
            AEADId.AES256_GCM,
        )

    async def request(self, path, data=None, authenticated=True):
        """Call the service using its v1 HPKE envelope without automatic retries.

        Args:
            path: Relative API path.
            data: Optional JSON body.
            authenticated: Whether to attach the account token.

        Returns:
            Decoded response data.

        Raises:
            RemoteError: HTTP or application failure.
        """
        headers = {
            "X-Device-ID": self.device,
            "X-Device": "pc",
            "X-Request-ID": uuid.uuid4().hex,
            "Accept": "application/json",
        }
        if authenticated:
            headers["Authorization"] = "Bearer " + self.tokens.get("access_token", "")
        endpoint = path.split("?", 1)[0]
        # Only the HPKE bootstrap key is fetched in plaintext, as in the
        # official client. Never downgrade credentials or tokens to plaintext.
        async with self.hpke_lock:
            if self.hpke_key is None or time.monotonic() >= self.hpke_until:
                async with self.session.get(
                    BASE + "user/hpke/public-key",
                    headers={"X-Device-ID": self.device, "Accept": "application/json"},
                ) as response:
                    payload = await response.json()
                    if response.status != 200 or payload.get("code") != 0:
                        raise RemoteError(
                            response.status,
                            business_code=payload.get("code"),
                            endpoint="user/hpke/public-key",
                        )
                    key = (payload.get("data") or {}).get("public_key", "")
                    if (
                        not isinstance(key, str)
                        or "." in key
                        and not key.startswith("ae71.")
                    ):
                        raise RemoteError(
                            "unsupported_encryption", endpoint="user/hpke/public-key"
                        )
                    try:
                        key = key.removeprefix("ae71.")
                        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
                        if len(raw) != 32:
                            raise ValueError("Invalid key length")
                        self.hpke_key = self.hpke_suite.kem.deserialize_public_key(raw)
                    except Exception:
                        raise RemoteError(
                            "invalid_key", endpoint="user/hpke/public-key"
                        ) from None
                    self.hpke_until = time.monotonic() + 300
        method = "POST" if data is not None else "GET"
        url = urlsplit(BASE + path)
        timestamp = str(int(time.time()))
        enc, sender = self.hpke_suite.create_sender_context(
            self.hpke_key, info=b"live-api-v1"
        )
        ciphertext = sender.seal(
            json.dumps(
                {"q": url.query, "b": data}, ensure_ascii=False, separators=(",", ":")
            ).encode(),
            f"{method}\n{url.path}\n{timestamp}".encode(),
        )
        headers.update(
            {
                "X-Enc": "v1",
                "X-Enc-Ts": timestamp,
                "X-Enc-Enc": base64.urlsafe_b64encode(enc).rstrip(b"=").decode(),
            }
        )
        target = url._replace(query="", fragment="").geturl()
        if data is None:
            target += "?e=" + base64.urlsafe_b64encode(ciphertext).rstrip(b"=").decode()
        else:
            headers["Content-Type"] = "application/octet-stream"
        async with self.session.request(
            method,
            target,
            data=ciphertext if data is not None else None,
            headers=headers,
        ) as response:
            if response.headers.get("X-Enc", "").lower() == "v1":
                try:
                    nonce = response.headers["X-Enc-Nonce"]
                    nonce = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4))
                    secret = sender.export(b"live-api-v1 response", 32)
                    key = HKDF(
                        algorithm=hashes.SHA256(),
                        length=32,
                        salt=enc + nonce,
                        info=b"key",
                    ).derive(secret)
                    iv = HKDF(
                        algorithm=hashes.SHA256(),
                        length=12,
                        salt=enc + nonce,
                        info=b"nonce",
                    ).derive(secret)
                    payload = json.loads(
                        AESGCM(key).decrypt(iv, await response.read(), None)
                    )
                except Exception:
                    raise RemoteError(
                        "invalid_encrypted_response", endpoint=endpoint
                    ) from None
            else:
                try:
                    payload = await response.json()
                except Exception:
                    raise RemoteError("invalid_response", endpoint=endpoint) from None
            if not isinstance(payload, dict):
                raise RemoteError("invalid_response", endpoint=endpoint)
            business_code = payload.get("code")
            if type(business_code) is not int:
                business_code = None
            if business_code in {2001031, 2001032}:
                self.hpke_key = None
            if response.status != 200:
                raise RemoteError(
                    response.status, business_code=business_code, endpoint=endpoint
                )
            if payload.get("code") != 0:
                raise RemoteError(
                    business_code if business_code is not None else "unknown",
                    endpoint=endpoint,
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
                diagnostic["error_code"] = (
                    message.get("ErrorCode")
                    if type(message.get("ErrorCode")) is int
                    else None
                )
                diagnostic["business_code"] = business_code
                diagnostic["review_reason"] = RemoteError(
                    message.get("ErrorCode"), business_code
                ).review_reason
                raise RemoteError(message.get("ErrorCode", "rejected"), business_code)
