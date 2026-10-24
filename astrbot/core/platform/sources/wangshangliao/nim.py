"""Single-reader NIM WebSocket transport with request correlation."""

import asyncio
import json
import re
import struct
import uuid
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from . import wire


class LoginToken(str):
    """Keep a fresh token's server-selected authentication mode with its value."""

    def __new__(cls, value: str, auth_type: int):
        if type(auth_type) is not int or auth_type not in {0, 1, 2}:
            raise wire.ProtocolError("nim_auth_type_invalid")
        token = super().__new__(cls, value)
        token.auth_type = auth_type
        return token


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
        auth_type = getattr(token, "auth_type", 0)
        if type(auth_type) is not int or auth_type not in {0, 1, 2}:
            raise wire.ProtocolError("nim_auth_type_invalid")
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
        if auth_type:
            fields.append((115, str(auth_type).encode()))
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
        """Read exclusively with bounded push backpressure and no message drops."""
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
                    try:
                        await asyncio.wait_for(
                            self.pushes.put((service, command, serial, code, body)), 5
                        )
                    except asyncio.TimeoutError:
                        raise wire.ProtocolError("nim_push_backpressure") from None
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

    async def get_friends(self, *, authorize=None):
        """Read a complete bounded SDK friend directory without mutations.

        Args:
            authorize: Synchronous account/lifecycle guard checked on every page.

        Returns:
            Valid friend relations keyed by authenticated transport account.

        Raises:
            ProtocolError: If a page, cursor or complete roster cannot be verified.
        """
        friends, cursor, seen = {}, 0, set()
        async with asyncio.timeout(20):
            for _ in range(10):
                if authorize:
                    authorize()
                code, body = await self.request(12, 4, struct.pack("<Q", cursor))
                if authorize:
                    authorize()
                if code != 200:
                    raise wire.ProtocolError("friend_directory_rejected")
                count, offset = wire.read_varint(body, 0)
                if count > 3000:
                    raise wire.ProtocolError("friend_directory_limit")
                updates = []
                for _ in range(count):
                    fields, offset = wire.read_properties(body, offset)
                    try:
                        peer = fields[4].decode("ascii")
                        updated = fields[12].decode("ascii")
                        created = fields.get(11, b"0").decode("ascii")
                        alias = fields.get(8, b"").decode("utf-8")
                        flag = fields[5]
                        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", peer):
                            raise wire.ProtocolError("friend_directory_identity")
                        if (
                            not re.fullmatch(r"[0-9]{1,16}", updated)
                            or not re.fullmatch(r"[0-9]{1,16}", created)
                            or int(updated) <= cursor
                        ):
                            raise wire.ProtocolError("friend_directory_timestamp")
                        if flag not in {b"0", b"1"}:
                            raise wire.ProtocolError("friend_directory_relation")
                        if peer in seen or len(alias) > 200:
                            raise wire.ProtocolError("friend_directory_shape")
                    except KeyError:
                        raise wire.ProtocolError(
                            "friend_directory_missing_field"
                        ) from None
                    except UnicodeError:
                        raise wire.ProtocolError("friend_directory_encoding") from None
                    seen.add(peer)
                    updates.append(int(updated))
                    if flag == b"1":
                        friends[peer] = {
                            "peer": peer,
                            "alias": alias,
                            "updated": int(updated),
                            "created": int(created),
                        }
                if offset + 8 != len(body):
                    raise wire.ProtocolError("friend_directory_framing")
                if count == 3000 and updates != sorted(updates):
                    raise wire.ProtocolError("friend_directory_order")
                if len(seen) > 10000:
                    raise wire.ProtocolError("friend_directory_limit")
                if count < 3000:
                    return friends
                if updates[-1] == updates[-2]:
                    raise wire.ProtocolError("friend_directory_ambiguous_boundary")
                cursor = updates[-1]
        raise wire.ProtocolError("friend_directory_incomplete")

    async def get_friend_names(self, peers):
        """Fetch only names for a bounded, already verified friend selection.

        Args:
            peers: At most 150 authenticated transport accounts.

        Returns:
            Names keyed by requested account, without profiles or private fields.

        Raises:
            ProtocolError: If a response contains ambiguous or unrequested users.
        """
        if not peers:
            return {}
        if (
            len(peers) > 150
            or len(set(peers)) != len(peers)
            or any(not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", peer) for peer in peers)
        ):
            raise wire.ProtocolError("friend_selection")
        body = wire.varint(len(peers))
        for peer in peers:
            encoded = peer.encode("ascii")
            body += wire.varint(len(encoded)) + encoded
        code, body = await self.request(3, 7, body)
        if code != 200:
            raise wire.ProtocolError("friend_names_rejected")
        count, offset = wire.read_varint(body, 0)
        if count > len(peers):
            raise wire.ProtocolError("friend_names_shape")
        names = {}
        for _ in range(count):
            fields, offset = wire.read_properties(body, offset)
            try:
                peer = fields[1].decode("ascii")
                name = fields.get(3, b"").decode("utf-8")
                if peer not in peers or peer in names or len(name) > 200:
                    raise ValueError
            except (KeyError, UnicodeError, ValueError):
                raise wire.ProtocolError("friend_names_shape") from None
            names[peer] = name
        if offset != len(body):
            raise wire.ProtocolError("friend_names_shape")
        return names

    async def get_team_muted_members(self, team_id: str) -> set[str]:
        """Fetch the current individual mute list using Web SDK query (8, 27).

        Args:
            team_id: Verified NIM team identity, not a business group number.

        Returns:
            Current muted transport account identities.

        Raises:
            wire.ProtocolError: On rejection, invalid framing or ambiguous data.
        """
        if (
            not isinstance(team_id, str)
            or not team_id.isascii()
            or not team_id.isdigit()
            or len(team_id) > 20
            or not 0 < int(team_id) < 1 << 64
        ):
            raise wire.ProtocolError("mute_list_group")
        # Web SDK 9.21.14: Long team ID -> Long team ID + teamMember PropertyArray.
        try:
            code, body = await self.request(8, 27, struct.pack("<Q", int(team_id)))
        except (TimeoutError, aiohttp.ClientError, OSError):
            raise wire.ProtocolError("mute_list_unavailable") from None
        if code != 200:
            raise wire.ProtocolError("mute_list_rejected")
        if len(body) < 9 or len(body) > wire.LIMIT:
            raise wire.ProtocolError("mute_list_shape")
        if int.from_bytes(body[:8], "little") != int(team_id):
            raise wire.ProtocolError("mute_list_group")
        count, offset = wire.read_varint(body, 8)
        if count > 10000:
            raise wire.ProtocolError("mute_list_limit")
        members = set()
        for _ in range(count):
            fields, offset = wire.read_properties(body, offset)
            try:
                peer = fields[3].decode("ascii")
                if (
                    not peer.isdigit()
                    or not 0 < int(peer) < 1 << 32
                    or peer in members
                    or fields.get(9) != b"1"
                    or fields.get(13) != b"1"
                    or fields.get(1, team_id.encode()) != team_id.encode()
                ):
                    raise ValueError
            except (KeyError, UnicodeError, ValueError):
                raise wire.ProtocolError("mute_list_identity") from None
            members.add(peer)
        if offset != len(body):
            raise wire.ProtocolError("mute_list_incomplete")
        return members

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


def recall_notifications(packet) -> list[dict]:
    """Decode SDK recall notifications, not one-way deletion or message text.

    Args:
        packet: One verified NIM packet, optionally notification-wrapped.

    Returns:
        Bounded group recall metadata with no message content or nickname.

    Raises:
        ProtocolError: If a recall packet has invalid framing or identifiers.
    """
    service, command, _, code, body = packet
    if service == 4 and command in (1, 2, 10, 11):
        if len(body) < 8:
            raise wire.ProtocolError("notification")
        inner = wire.read_packet(body[8:])
        if inner[0] == 4:
            raise wire.ProtocolError("notification")
        return recall_notifications(inner)
    if (service, command) not in ((7, 3), (7, 14), (7, 15)):
        return []
    if code != 200:
        raise wire.ProtocolError("push_rejected")
    try:
        offset = 0
        count = 1
        if command == 15:
            count, offset = wire.read_varint(body, offset)
        if count > 1024:
            raise wire.ProtocolError("recall_notification")
        notices = []
        for _ in range(count):
            fields, offset = wire.read_properties(body, offset)
            if fields.get(1) != b"8":
                continue
            notice = {}
            for key, name in (
                (0, "recalled_at"),
                (2, "team"),
                (3, "author"),
                (6, "notice_id"),
                (11, "message_id"),
                (14, "message_time"),
            ):
                value = fields.get(key, b"").decode("ascii")
                if not value.isdigit() or len(value) > 20 or int(value) <= 0:
                    raise wire.ProtocolError("recall_notification")
                notice[name] = value
            client = fields.get(10, b"").decode("ascii")
            operator = fields.get(16, fields[3]).decode("ascii")
            if (
                not 1 <= len(client) <= 128
                or any(
                    c
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                    for c in client
                )
                or not operator.isdigit()
                or len(operator) > 20
                or int(operator) <= 0
            ):
                raise wire.ProtocolError("recall_notification")
            notice.update(client=client, operator=operator)
            notices.append(notice)
        if command == 15:
            if len(body) - offset != 9 or body[-1] not in (0, 1, 2):
                raise wire.ProtocolError("recall_notification")
        elif offset != len(body):
            raise wire.ProtocolError("recall_notification")
        return notices
    except (wire.ProtocolError, UnicodeDecodeError, KeyError, ValueError):
        raise wire.ProtocolError("recall_notification") from None


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
        for tag in range(29, 39):
            value = fields.get(tag)
            if value is not None and len(value) <= 128:
                try:
                    values[tag] = value.decode("utf-8")
                except UnicodeDecodeError:
                    pass
        if values.get(0) not in ("0", "1") or not all(
            values.get(k) for k in (1, 2, 7, 8, 12)
        ):
            raise wire.ProtocolError("message_fields")
        result.append(values)
    if offset != len(body):
        raise wire.ProtocolError("message_trailing")
    return result, None
