"""Single-reader NIM WebSocket transport with request correlation."""

import asyncio
import json
import uuid
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import wire


class NimClient:
    """Keep pushes separate from correlated request replies."""

    def __init__(self, http: aiohttp.ClientSession):
        self.http = http
        self.socket = None
        self.reader = None
        self.pending: dict[tuple[int, int, int], asyncio.Future] = {}
        self.pushes: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.serial = 0
        self.closed = asyncio.Event()
        self.error = "disconnected"

    async def connect(self, deployment, account: str, token: str) -> None:
        """Discover a secure endpoint and authenticate without sending tokens to LBS."""
        try:
            async with self.http.get(
                "https://lbs.netease.im/lbs/webconf.jsp",
                params={
                    "id": account,
                    "k": deployment.app_key,
                    "sv": "92114",
                    "pv": "1",
                    "networkType": "0",
                    "hostEnv": "BROWSER",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise wire.ProtocolError("discovery_limit")
                common = json.loads(content)["common"]
            endpoints = []
            for key in ("link", "link.default"):
                for host in list(reversed(common.get(key, [])))[:16]:
                    url = urlsplit(host if "://" in host else f"https://{host}")
                    if (
                        url.scheme != "https"
                        or not url.hostname
                        or url.username
                        or url.password
                        or url.path not in ("", "/")
                        or url.query
                        or url.fragment
                    ):
                        raise wire.ProtocolError("discovery_endpoint")
                    endpoint = urlunsplit(("wss", url.netloc, "/websocket", "", ""))
                    if endpoint not in endpoints:
                        endpoints.append(endpoint)
        except Exception:
            raise wire.ProtocolError("discovery_failed") from None
        fields = [
            (3, b"16"),
            (4, b"Linux"),
            (6, b"92114"),
            (8, b"1"),
            (9, b"1"),
            (13, deployment.device_id.encode()),
            (18, deployment.app_key.encode()),
            (19, account.encode()),
            (24, b"AstrBot"),
            (26, str(uuid.uuid4()).encode()),
            (38, b""),
            (40, b"9.21.14"),
            (41, b"0"),
            (42, b"Native/9.21.14"),
            (112, b"0"),
            (1000, token.encode()),
        ]
        for endpoint in endpoints[:3]:
            try:
                await self.open_socket(endpoint, fields)
                return
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                wire.ProtocolError,
            ) as exc:
                await self.close()
                if (
                    isinstance(exc, wire.ProtocolError)
                    and str(exc) == "nim_auth_rejected"
                ):
                    raise
        raise wire.ProtocolError("nim_connection_failed")

    async def open_socket(self, endpoint: str, fields: list[tuple[int, bytes]]) -> None:
        """Open an endpoint and verify the full login response.

        Args:
            endpoint: Secure production endpoint or a loopback test server.
            fields: Ordered SDK authentication fields.

        Raises:
            ProtocolError: If the response has no unique connection identity.
        """
        parsed = urlsplit(endpoint)
        if parsed.scheme != "wss" and not (
            parsed.scheme == "ws" and parsed.hostname in ("127.0.0.1", "::1")
        ):
            raise wire.ProtocolError("nim_endpoint")
        self.closed.clear()
        self.pushes = asyncio.Queue(maxsize=32)
        self.socket = await self.http.ws_connect(
            endpoint,
            max_msg_size=wire.LIMIT,
            timeout=aiohttp.ClientWSTimeout(ws_close=5),
        )
        self.reader = asyncio.create_task(self.receive())
        code, body = await self.request(2, 3, wire.properties(fields), timeout=10)
        if code != 200:
            raise wire.ProtocolError("nim_auth_rejected")
        properties, offset = wire.read_properties(body)
        if not properties.get(102):
            raise wire.ProtocolError("nim_auth_shape")
        if offset < len(body):
            count, offset = wire.read_varint(body, offset)
            if count > 128:
                raise wire.ProtocolError("nim_auth_shape")
            for _ in range(count):
                _, offset = wire.read_properties(body, offset)
            if offset < len(body):
                _, offset = wire.read_properties(body, offset)
        if offset != len(body):
            raise wire.ProtocolError("nim_auth_shape")
        await self.heartbeat()

    async def receive(self) -> None:
        """Read the socket exclusively, failing closed on push queue overflow."""
        try:
            async for message in self.socket:
                if message.type != aiohttp.WSMsgType.BINARY:
                    if message.type in (aiohttp.WSMsgType.PING, aiohttp.WSMsgType.PONG):
                        continue
                    raise wire.ProtocolError("nim_disconnected")
                service, command, serial, code, body = wire.read_packet(message.data)
                if (service, command) == (2, 5):
                    # The reference decoder does not establish a reason-field layout.
                    # Stop rather than repeatedly contest an unknown server kick.
                    raise wire.ProtocolError("nim_kicked")
                waiter = self.pending.get((service, command, serial))
                if waiter is not None and not waiter.done():
                    waiter.set_result((code, body))
                else:
                    self.pushes.put_nowait((service, command, serial, code, body))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = (
                str(exc) if isinstance(exc, wire.ProtocolError) else "nim_disconnected"
            )
        finally:
            self.closed.set()
            for waiter in self.pending.values():
                if not waiter.done():
                    waiter.set_exception(wire.ProtocolError(self.error))

    async def request(
        self, service: int, command: int, body: bytes = b"", timeout: float = 10
    ):
        """Correlate a request by service, command and monotonically increasing serial."""
        if self.socket is None or self.socket.closed or self.closed.is_set():
            raise wire.ProtocolError("nim_disconnected")
        self.serial += 1
        if self.serial > 65535:
            raise wire.ProtocolError("nim_serial_exhausted")
        key = (service, command, self.serial)
        waiter = asyncio.get_running_loop().create_future()
        self.pending[key] = waiter
        try:
            await self.socket.send_bytes(wire.packet(*key, body))
            return await asyncio.wait_for(waiter, timeout)
        finally:
            self.pending.pop(key, None)
            if not waiter.done():
                waiter.cancel()

    async def heartbeat(self) -> None:
        """Require an empty, correlated successful heartbeat response."""
        code, body = await self.request(1, 2, timeout=5)
        if code != 200 or body:
            raise wire.ProtocolError("heartbeat")

    async def get_team_inviters(
        self, team_id: str, accounts: list[str]
    ) -> dict[str, str]:
        """Read inviter accounts with the SDK's bounded team query (8, 33).

        Args:
            team_id: Verified NIM team identity, not a business group number.
            accounts: One to two hundred verified NIM member accounts.

        Returns:
            Platform-reported member account to inviter account mapping.

        Raises:
            wire.ProtocolError: If input, response identity or framing is invalid.
        """
        if (
            not isinstance(team_id, str)
            or not team_id.isascii()
            or not team_id.isdigit()
            or len(team_id) > 20
            or not 0 < int(team_id) < 1 << 64
            or not isinstance(accounts, list)
            or not 1 <= len(accounts) <= 200
            or any(
                not isinstance(account, str)
                or not account
                or len(account.encode()) > 1024
                or any(ord(char) < 32 for char in account)
                for account in accounts
            )
            or len(set(accounts)) != len(accounts)
        ):
            raise wire.ProtocolError("invitation_arguments")
        # SDK 9.21.14 encodes Long + StrArray and returns a StrStrMap.
        body = int(team_id).to_bytes(8, "little") + wire.varint(len(accounts))
        for account in accounts:
            encoded = account.encode()
            body += wire.varint(len(encoded)) + encoded
        code, reply = await self.request(8, 33, body)
        if code != 200:
            raise wire.ProtocolError("invitation_query_rejected")
        if len(reply) > wire.LIMIT:
            raise wire.ProtocolError("invitation_response")
        count, offset = wire.read_varint(reply, 0)
        if count > len(accounts):
            raise wire.ProtocolError("invitation_response")
        result = {}
        for _ in range(count):
            pair = []
            for _ in range(2):
                size, offset = wire.read_varint(reply, offset)
                if size > 1024 or offset + size > len(reply):
                    raise wire.ProtocolError("invitation_response")
                try:
                    value = reply[offset : offset + size].decode("utf-8")
                except UnicodeError:
                    raise wire.ProtocolError("invitation_response") from None
                if any(ord(char) < 32 for char in value):
                    raise wire.ProtocolError("invitation_response")
                pair.append(value)
                offset += size
            account, inviter = pair
            if account not in accounts or account in result:
                raise wire.ProtocolError("invitation_response")
            result[account] = inviter
        if offset != len(reply):
            raise wire.ProtocolError("invitation_response")
        return result

    async def acknowledge(self, scene: int, server_id: str) -> None:
        """Flush a transport ACK only after the caller has persisted the message."""
        self.serial += 1
        if self.serial > 65535:
            raise wire.ProtocolError("nim_serial_exhausted")
        route = bytes((8, 3)) if scene == 1 else bytes((7, 2))
        body = route + wire.varint(1) + int(server_id).to_bytes(8, "little")
        await self.socket.send_bytes(wire.packet(4, 5, self.serial, body))

    async def close(self) -> None:
        """Cancel the single reader and release the socket."""
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        if self.socket:
            await self.socket.close()
        self.closed.set()


def messages(packet) -> tuple[list[dict], int | None]:
    """Decode group/private pushes, returning explicit sync boundaries."""
    service, command, _, code, body = packet
    if code != 200:
        raise wire.ProtocolError("push_rejected")
    if service == 4 and command in (1, 2, 10, 11):
        if len(body) < 8:
            raise wire.ProtocolError("notification")
        inner = wire.read_packet(body[8:])
        if inner[0] == 4:
            raise wire.ProtocolError("notification")
        return messages(inner)
    if (service, command) == (5, 1):
        if len(body) != 8:
            raise wire.ProtocolError("sync_boundary")
        return [], int.from_bytes(body, "little")
    offset = 0
    if (service, command) in ((7, 2), (7, 101), (8, 3), (8, 102)):
        count = 1
    elif (service, command) in ((4, 4), (4, 9), (8, 4)):
        count, offset = wire.read_varint(body, 0)
    else:
        return [], None
    if count > 1024:
        raise wire.ProtocolError("message_count")
    result = []
    for _ in range(count):
        fields, offset = wire.read_properties(body, offset)
        values = {
            key: value.decode("utf-8")
            for key, value in fields.items()
            if key in (0, 1, 2, 6, 7, 8, 9, 10, 11, 12)
        }
        if values.get(0) not in ("0", "1") or not all(
            values.get(k) for k in (1, 2, 7, 8, 12)
        ):
            raise wire.ProtocolError("message_fields")
        result.append(values)
    if offset != len(body):
        raise wire.ProtocolError("message_trailing")
    return result, None
