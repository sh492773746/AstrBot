"""Offline native login, transport and crash-boundary regression tests."""

import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiohttp import web
from nacl.public import PrivateKey, SealedBox
from nacl.signing import SigningKey

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import (
    WangshangliaoAdapter,
    message_timestamp,
    team_invite_notification,
)
from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    BusinessError,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.event import mentioned
from astrbot.core.platform.sources.wangshangliao.nim import NimClient
from astrbot.core.platform.sources.wangshangliao.registration import RegistrationManager
from astrbot.core.platform.sources.wangshangliao.storage import Ledger, Vault


@pytest.fixture(autouse=True)
def active_plugin_policy(wsl_active_policy):
    """Provide the active plugin policy boundary for transport-only fixtures."""
    return wsl_active_policy


@pytest.mark.parametrize(
    ("server_time", "inner_time", "expected"),
    [
        ("1790949286334", 0, 1790949286334),
        ("1790949286334", 123, 1790949286334),
        ("", 123, 123),
        ("0", 123, 123),
        ("-1", 123, 123),
        ("bad", 123, 123),
        ("1.2", 123, 123),
        ("9" * 100, 123, 123),
        (None, 123, 123),
        (True, 123, 123),
        ("", 0, 0),
    ],
)
def test_message_timestamp_uses_server_time_not_receipt_time(
    server_time, inner_time, expected
):
    assert (
        message_timestamp(SimpleNamespace(created_at=inner_time), {7: server_time})
        == expected
    )


@pytest.mark.parametrize(
    ("outer", "expected"),
    [
        (
            {
                0: "1",
                2: "22",
                8: "5",
                10: '{"id":0,"data":{"ids":["33","44"]}}',
            },
            {
                "kind": "team_invite",
                "inviter_peer": "22",
                "member_peers": ["33", "44"],
            },
        ),
        (
            {
                0: "1",
                2: "22",
                8: "5",
                10: '{"id":0,"data":"{\\"ids\\":[\\"33\\",\\"33\\"]}"}',
            },
            {
                "kind": "team_invite",
                "inviter_peer": "22",
                "member_peers": ["33"],
            },
        ),
        ({0: "1", 2: "22", 8: "5", 10: '{"id":1,"data":{"ids":["33"]}}'}, None),
        ({0: "1", 2: "22", 8: "5", 10: '{"id":0,"data":{"ids":["x"]}}'}, None),
        ({0: "1", 2: "22", 8: "100", 10: '{"id":0,"data":{"ids":["33"]}}'}, None),
    ],
)
def test_team_invite_notification_accepts_only_native_team_invites(outer, expected):
    assert team_invite_notification(outer) == expected


@pytest.fixture
def deployment():
    return Deployment(
        origin="https://example.invalid",
        headers={},
        metadata=[1] * 14,
        signing_seed=base64.b64encode(bytes(32)).decode(),
        body_key=base64.b64encode(bytes(32)).decode(),
        message_key=base64.b64encode(bytes(32)).decode(),
        server_key=base64.b64encode(
            bytes(PrivateKey(bytes([3]) * 32).public_key)
        ).decode(),
        captcha_id="fixture",
        app_key="fixture",
        device_id="fixture",
    )


@pytest.mark.parametrize(
    "text,people,expected",
    [
        ("@DH hi", [], False),
        ("@ DH hi", [], False),
        ("你好，@DH！", [], False),
        ("@DH hi", ["99"], False),
        ("hi", ["99", "12"], True),
        ("@12", [], False),
        ("x@DH.com", [], False),
        ("@DHello", [], False),
        ("@ DHx", [], False),
        ("hi", ["12"], True),
        ("hi", ["99"], False),
        ("hello", [], False),
    ],
)
def test_mentions(text, people, expected):
    assert mentioned(text, "12", people) is expected


@pytest.mark.asyncio
async def test_encrypted_vault_and_crash_ledger(tmp_path):
    vault = Vault("fixture", tmp_path / "vault")
    value = {"token": "private-fixture-value"}
    vault.save(value)
    assert vault.load() == value
    assert b"private-fixture-value" not in (vault.root / "session.sealed").read_bytes()
    with pytest.raises(wire.ProtocolError):
        Vault("other-instance", vault.root).load()
    vault.clear()
    assert vault.load() is None
    ledger = Ledger(tmp_path / "messages.sqlite")
    await ledger.open()
    try:
        await ledger.ingest("a", "g", "m", {"text": "first"})
        await ledger.ingest("a", "g", "m", {"text": "duplicate"})
        await ledger.ingest("other", "g", "m", {"text": "isolated"})
        await ledger.mark("a", "g", "m", "processing")
        nonce, state = await ledger.reserve("send", "text")
        assert state == "new"
        assert await ledger.reserve("send", "text") == (nonce, "sending")
        with pytest.raises(wire.ProtocolError, match="conflict"):
            await ledger.reserve("send", "changed")
    finally:
        await ledger.close()
    await ledger.open()
    try:
        assert await ledger.reserve("send", "text") == (nonce, "unknown")
        async with ledger.db.execute(
            "SELECT account,payload,state FROM inbox ORDER BY account"
        ) as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 2
        assert rows[0][2] == "needs_review"
        assert json.loads(rows[0][1])["text"] == "first"
    finally:
        await ledger.close()


class LoginFixture:
    def __init__(self, deployment, http):
        self.deployment = deployment
        self.sms = False
        self.waiter = None

    async def login(self, params):
        if self.waiter:
            await self.waiter.wait()
        if params["type"] != "LOGIN_TYPE_SMS" and self.sms:
            raise BusinessError("device_required", 1069)
        if params.get("passwd") == "bad":
            raise BusinessError("invalid_credentials")
        return {
            "uid": 12,
            "nimId": "nim-12",
            "nimToken": "secret-fixture",
            "nickname": "Fixture",
        }

    async def request(self, path, params):
        if path == "/v1/user/get-change-device-verify":
            return {
                "phone": {"nationalNumber": "12345678901"},
                "sms": {"key": "secret-sms"},
            }
        return {"key": "new-secret"}

    def export(self):
        return {"uid": 12, "jwt": "secret-fixture"}


@pytest.mark.asyncio
async def test_registration_owner_sms_claim_cancel_expiry(deployment):
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
        code = view["registration_code"]
        base = {"instance_id": "bot", "registration_code": code}
        for bad_owner, bad_instance in [("other", "bot"), ("admin", "other")]:
            with pytest.raises(wire.ProtocolError):
                await manager.action(
                    bad_owner, {**base, "instance_id": bad_instance, "action": "poll"}
                )
        tx = manager.transactions[code]
        tx.client.sms = True
        result = await manager.action(
            "admin",
            {
                **base,
                "action": "login",
                "account": "fixture",
                "password": "fixture",
                "validate_str": "human-fixture",
            },
        )
        assert result["status"] == "sms_required"
        assert "secret" not in json.dumps(result)
        result = await manager.action(
            "admin", {**base, "action": "resend_sms", "validate_str": "human-fixture"}
        )
        assert result["error"] == "sms_cooldown"
        result = await manager.action(
            "admin",
            {
                **base,
                "action": "verify_sms",
                "verification_code": "123456",
                "validate_str": "human-fixture",
            },
        )
        assert result["status"] == "authenticated"
        ref = result["session_ref"]
        assert manager.claim("admin", "bot", ref) is tx
        with pytest.raises(wire.ProtocolError):
            manager.claim("other", "bot", ref)
        assert "secret-fixture" not in json.dumps(result)
        await manager.action("admin", {**base, "action": "cancel"})
        with pytest.raises(wire.ProtocolError):
            manager.claim("admin", "bot", ref)
        view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
        manager.transactions[view["registration_code"]].expires = 0
        with pytest.raises(wire.ProtocolError, match="expired"):
            await manager.action(
                "admin",
                {
                    "action": "poll",
                    "instance_id": "bot",
                    "registration_code": view["registration_code"],
                },
            )
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"action": "login"}, "login_input"),
        ({"action": "request_sms", "account": "invalid"}, "sms_input"),
        ({"action": "verify_sms"}, "sms_challenge"),
        ({"action": "resend_sms"}, "sms_challenge"),
    ],
)
async def test_invalid_login_input_skips_solver(
    deployment, monkeypatch, fields, expected
):
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
        post = AsyncMock(side_effect=AssertionError("Unexpected solver request"))
        monkeypatch.setattr(aiohttp.ClientSession, "post", post)
        result = await manager.action(
            "admin",
            {
                "instance_id": "bot",
                "registration_code": view["registration_code"],
                **fields,
            },
        )
        assert result["error"] == expected
        post.assert_not_called()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_login_uses_builtin_solver_by_default(deployment, monkeypatch):
    monkeypatch.delenv("ASTRBOT_YIDUN_SOLVER_URL", raising=False)
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    solver = AsyncMock(return_value="fixture-validation")
    monkeypatch.setattr(manager.solver, "solve", solver)
    try:
        view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
        result = await manager.action(
            "admin",
            {
                "action": "login",
                "instance_id": "bot",
                "registration_code": view["registration_code"],
                "account": "fixture",
                "password": "fixture",
            },
        )
        assert result["status"] == "authenticated"
        solver.assert_awaited_once()
        assert solver.call_args.args[:2] == (
            deployment.captcha_id,
            deployment.origin.rstrip("/") + "/",
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cancel_invalidates_inflight_login(deployment):
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
    code = view["registration_code"]
    tx = manager.transactions[code]
    gate = tx.client.waiter = asyncio.Event()
    pending = asyncio.create_task(
        manager.action(
            "admin",
            {
                "action": "login",
                "instance_id": "bot",
                "registration_code": code,
                "account": "fixture",
                "password": "fixture",
                "validate_str": "human",
            },
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(wire.ProtocolError, match="conflict"):
        await manager.action(
            "admin",
            {"action": "login", "instance_id": "bot", "registration_code": code},
        )
    await manager.discard(code)
    gate.set()
    with pytest.raises(wire.ProtocolError, match="cancelled"):
        await pending
    assert not tx.saved and not tx.session_ref
    assert tx.http.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", [None, "time", "correlation", "identity", "key", "truncated"]
)
@pytest.mark.parametrize("has_message_key", [True, False])
async def test_real_crypto_login_binding(deployment, corruption, has_message_key):
    if not has_message_key:
        deployment.message_key = Deployment.model_fields["message_key"].default
    async with aiohttp.ClientSession() as http:
        client = BusinessClient(deployment, http)

        async def exchange(route, params, sealed):
            request = wire.Exchange.FromString(
                SealedBox(PrivateKey(bytes([3]) * 32)).decrypt(sealed)
            )
            token = wire.LoginToken(
                seconds=request.seconds + int(corruption == "time"),
                correlation=request.correlation + int(corruption == "correlation"),
                uid=12 + int(corruption == "identity"),
                kind=1,
                mac=bytes(16),
            )
            key = SigningKey(
                bytes([1]) * 32 if corruption == "key" else bytes(32)
            ).verify_key.to_curve25519_public_key()
            cipher = SealedBox(key).encrypt(token.SerializeToString())
            if corruption == "truncated":
                cipher = cipher[:-1]
            return {"token": wire.b64(cipher), "uid": 12, "jwtToken": "fixture-jwt"}

        client.request = exchange
        if corruption:
            with pytest.raises(BusinessError, match="login_binding"):
                await client.login({"type": "LOGIN_TYPE_ACCOUNT_PWD"})
            assert not client.jwt
        else:
            await client.login({"type": "LOGIN_TYPE_ACCOUNT_PWD"})
            assert client.export()["uid"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("burst,consume", [(1, True), (80, True), (80, False)])
async def test_nim_single_reader_interleaved_pushes_and_receipts(burst, consume):
    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for incoming in ws:
            service, command, serial, _, body = wire.read_packet(incoming.data)
            if (service, command) == (2, 3):
                reply = wire.properties([(102, b"connection-fixture")])
            elif (service, command) == (8, 2):
                for index in range(burst):
                    await ws.send_bytes(
                        wire.packet(
                            8,
                            3,
                            44,
                            wire.properties(
                                [
                                    (0, b"1"),
                                    (1, b"team"),
                                    (2, b"sender"),
                                    (7, b"123"),
                                    (8, b"100"),
                                    (12, str(55 + index).encode()),
                                ]
                            ),
                        )
                    )
                reply = wire.properties([(7, b"123"), (12, b"56")])
            else:
                reply = b""
            await ws.send_bytes(wire.packet(service, command, serial, reply))
        return ws

    app = web.Application()
    app.router.add_get("/socket", socket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as http:
            client = NimClient(http)
            consumer = None
            try:
                await client.open_socket(f"ws://127.0.0.1:{port}/socket", [])
                if not consume:
                    with pytest.raises(
                        wire.ProtocolError, match="nim_push_backpressure"
                    ):
                        await client.request(8, 2)
                    assert client.closed.is_set()
                    assert client.pushes.qsize() == 32
                    assert not client.pending
                    return

                async def drain():
                    await asyncio.sleep(0.05)
                    assert client.pushes.qsize() <= 32
                    return [
                        await asyncio.wait_for(client.pushes.get(), 1)
                        for _ in range(burst)
                    ]

                consumer = asyncio.create_task(drain())
                response, heartbeat = await asyncio.gather(
                    client.request(8, 2), client.heartbeat()
                )
                assert response[0] == 200 and heartbeat is None
                incoming = await consumer
                assert all(packet[:2] == (8, 3) for packet in incoming)
                assert [
                    wire.read_properties(packet[4])[0][12] for packet in incoming
                ] == [str(55 + index).encode() for index in range(burst)]
                assert not client.pending
            finally:
                if consumer:
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
                await client.close()
                assert client.closed.is_set()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("known_group", [True, False])
async def test_unopened_group_activation_routes_without_at_or_framework(
    tmp_path, deployment, monkeypatch, known_group
):
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    adapter = WangshangliaoAdapter(
        {"id": "candidate", "account_id": "12", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "native.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.groups = {"34": "1005"} if known_group else {}
    adapter.members = {"34": {"13": "1001"}}
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    candidate = Mock()
    monkeypatch.setattr(
        runtime,
        "open_service",
        SimpleNamespace(
            active=True,
            settings={"paused": False},
            integrated=False,
            admit_candidate=candidate,
        ),
    )
    inner = wire.ApplicationMessage(
        sender={"id": 13},
        target={"id": 34},
        session=2,
        device=1,
        format=0,
        version=2,
        content={"data": "开通"},
    )
    outer = {
        0: "1",
        1: "1005",
        2: "1001",
        7: "123",
        8: "100",
        10: wire.seal_message(bytes(32), inner, 1, 2, 3),
        12: "55",
    }
    try:
        await adapter.ingest(outer)
        candidate.assert_called_once()
        assert candidate.call_args.args[0:2] == (adapter, "34")
        adapter.nim.acknowledge.assert_awaited_once_with(1, "55")
        assert adapter._event_queue.empty()
        async with adapter.ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", ["private", "group", "disabled-group", "known-candidate", "new-candidate"]
)
async def test_ingest_decodes_authenticated_body_only_once(
    tmp_path, deployment, monkeypatch, route
):
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    adapter = WangshangliaoAdapter(
        {
            "id": "decode-once",
            "account_id": "12",
            "enabled_groups": ["34"] if route == "group" else [],
        },
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "decode.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.groups = {} if route == "new-candidate" else {"34": "1005"}
    adapter.members = {"34": {"13": "1001"}}
    adapter.nim_account = "self-nim"
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    candidate = Mock()
    monkeypatch.setattr(
        runtime,
        "open_service",
        SimpleNamespace(
            active=True,
            settings={"paused": False},
            integrated=False,
            admit_candidate=candidate,
            chat_tasks={},
            chat_users=set(),
        ),
    )
    private = route == "private"
    activation = route.endswith("candidate")
    inner = wire.ApplicationMessage(
        sender={"id": 13},
        target={"id": 12 if private else 34},
        session=1 if private else 2,
        device=1,
        format=0,
        content={"data": "开通" if activation else "hello"},
    )
    outer = {
        0: "0" if private else "1",
        1: "self-nim" if private else "1005",
        2: "1001",
        7: "123",
        8: "100",
        10: wire.seal_message(bytes(32), inner, 1, 2, 3),
        12: "55",
    }
    decode = Mock(wraps=wire.open_message)
    monkeypatch.setattr(wire, "open_message", decode)
    try:
        await adapter.ingest(outer)
        decode.assert_called_once_with(bytes(32), outer[10])
        adapter.nim.acknowledge.assert_awaited_once_with(int(outer[0]), "55")
        assert candidate.call_count == int(activation)
        async with adapter.ledger.db.execute(
            "SELECT state,payload FROM inbox"
        ) as cursor:
            rows = await cursor.fetchall()
        if activation:
            assert rows == []
        else:
            assert len(rows) == 1 and rows[0][0] == "pending"
            assert json.loads(rows[0][1])["sender"] == "13"
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("spoof", [False, True])
@pytest.mark.parametrize(
    ("format_id", "kind"),
    [(1, "image"), (3, "video"), (9, "forward"), (2, None), (6, None), (13, None)],
)
async def test_native_images_enter_group_flood_ingress_only_with_trusted_identity(
    tmp_path, deployment, monkeypatch, private, spoof, format_id, kind
):
    import struct

    import lz4.block
    import nacl.bindings

    from astrbot.builtin_stars.wangshangliao_moderation import feature_image
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    monkeypatch.setattr(runtime, "open_service", None)
    observer = Mock()
    monkeypatch.setattr(runtime, "observer", observer)
    adapter = WangshangliaoAdapter(
        {"id": "image-ingress", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.groups = {"34": "1005"}
    adapter.members = {"34": {"13": "1001"}}
    adapter.refresh_member_mapping = AsyncMock()
    adapter.nim_account = "self-nim"
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    adapter.ledger = Ledger(tmp_path / "images.sqlite3")
    await adapter.ledger.open()
    message = feature_image.ImageMessage(
        sender={"id": 13, "name": "ordinary"},
        target={"id": 12 if private else 34},
        session=1 if private else 2,
        device=1,
        format=format_id,
        image={
            "asset": {"uri": next(iter(feature_image.REVIEWED_ASSETS.values()))},
            "width": 1024,
            "height": 1024,
            "size": 1000,
        },
    )
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        lz4.block.compress(message.SerializeToString(), store_size=True),
        struct.pack("<Q", 2),
        struct.pack("<Q", 1),
        bytes(32),
    )
    envelope = wire.Envelope(
        header=message.sender.id << 34
        | message.target.id << 4
        | message.session << 2
        | 1,
        timestamp=3,
        nonce=1,
        aad=2,
        body=sealed[:-16],
        tag=sealed[-16:],
    )
    outer = {
        0: "0" if private else "1",
        1: "self-nim" if private else "1005",
        2: "forged-peer" if spoof else "1001",
        7: str(int(time.time() * 1000)),
        8: "100",
        10: json.dumps({"b": wire.b64(envelope.SerializeToString())}),
        11: "image-client",
        12: "55",
    }
    try:
        await adapter.ingest(outer)
        async with adapter.ledger.db.execute("SELECT state,payload FROM inbox") as c:
            rows = await c.fetchall()
        assert len(rows) == 1
        state, encoded = rows[0]
        if not private and not spoof and kind:
            payload = json.loads(encoded)
            assert state == "pending"
            assert payload["kind"] == kind and payload["text"] == ""
            assert payload["sender"] == "13"
            assert payload["recall_route"]["peer"] == "1001"
            assert payload["mentions"] == []
            observer.assert_called_once()
        elif private and not spoof and kind == "image":
            payload = json.loads(encoded)
            assert state == "pending"
            assert payload["kind"] == "image" and payload["text"] == ""
            assert payload["sender"] == "13" and payload["transport_peer"] == "1001"
            assert payload["image"]["asset"] == message.image.asset.uri
            assert payload["mentions"] == []
            observer.assert_called_once()
        else:
            assert state != "pending"
            observer.assert_not_called()
        adapter.nim.acknowledge.assert_awaited_once_with(int(outer[0]), "55")
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("private", "fault"),
    [
        (True, "target"),
        (True, "session"),
        (True, "self"),
        (True, "cipher"),
        (False, "target"),
        (False, "session"),
        (False, "peer"),
        (False, "cipher"),
    ],
)
async def test_ingest_decode_reuse_keeps_identity_and_cipher_checks(
    tmp_path, deployment, monkeypatch, private, fault
):
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    monkeypatch.setattr(runtime, "open_service", None)
    adapter = WangshangliaoAdapter(
        {"id": "decode-rejected", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "rejected.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.groups = {"34": "1005"}
    adapter.members = {"34": {"13": "1001"}}
    adapter.refresh_member_mapping = AsyncMock()
    adapter.nim_account = "self-nim"
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    inner = wire.ApplicationMessage(
        sender={"id": 12 if fault == "self" else 13},
        target={"id": 99 if fault == "target" else 12 if private else 34},
        session=(2 if private else 1) if fault == "session" else 1 if private else 2,
        device=1,
        format=0,
        content={"data": "hello"},
    )
    outer = {
        0: "0" if private else "1",
        1: "self-nim" if private else "1005",
        2: "forged-peer" if fault == "peer" else "1001",
        8: "100",
        10: wire.seal_message(
            bytes([1]) * 32 if fault == "cipher" else bytes(32), inner, 1, 2, 3
        ),
        12: "55",
    }
    decode = Mock(wraps=wire.open_message)
    monkeypatch.setattr(wire, "open_message", decode)
    try:
        if private and fault != "cipher":
            with pytest.raises(wire.ProtocolError, match="private_message_identity"):
                await adapter.ingest(outer)
            adapter.nim.acknowledge.assert_not_awaited()
        else:
            await adapter.ingest(outer)
            adapter.nim.acknowledge.assert_awaited_once_with(int(outer[0]), "55")
        decode.assert_called_once()
        async with adapter.ledger.db.execute("SELECT state FROM inbox") as cursor:
            rows = await cursor.fetchall()
        assert rows == (
            []
            if private and fault != "cipher"
            else [("unsupported" if fault == "cipher" else "rejected_identity",)]
        )
        assert adapter._event_queue.empty()
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("disposition", ["admitted", "queue-full", "ack-retry"])
async def test_ingest_wakeup_requires_admission_but_recovers_ack_retry(
    tmp_path, deployment, monkeypatch, private, disposition
):
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    monkeypatch.setattr(runtime, "open_service", None)
    observe = Mock(wraps=runtime.observe)
    monkeypatch.setattr(runtime, "observe", observe)
    adapter = WangshangliaoAdapter(
        {"id": "admission-wakeup", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "wakeup.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.groups = {"34": "1005"}
    adapter.members = {"34": {"13": "1001"}}
    adapter.nim_account = "self-nim"
    team = "private/13/" + wire.b64(b"1001") if private else "34"
    rejected = disposition == "queue-full"
    if rejected:
        await adapter.ledger.db.executemany(
            "INSERT INTO inbox(account,team,message,sender,payload,state) "
            "VALUES('12',?,?,?,'{}','pending')",
            [(team, f"old{i}", f"peer{i}") for i in range(256 if private else 4096)],
        )
        await adapter.ledger.db.commit()
    ack_attempts = 0

    async def acknowledge(kind, mid):
        nonlocal ack_attempts
        ack_attempts += 1
        assert not adapter.dispatch_wakeup.is_set()
        # The ACK sees the committed admission result, never speculative state.
        async with adapter.ledger.db.execute(
            "SELECT state FROM inbox WHERE account='12' AND team=? AND message=?",
            (team, mid),
        ) as cursor:
            assert await cursor.fetchone() == ("queue_full" if rejected else "pending",)
        if disposition == "ack-retry" and ack_attempts == 1:
            raise OSError("offline ACK failure")

    adapter.nim = SimpleNamespace(acknowledge=AsyncMock(side_effect=acknowledge))
    inner = wire.ApplicationMessage(
        sender={"id": 13},
        target={"id": 12 if private else 34},
        session=1 if private else 2,
        device=1,
        format=0,
        content={"data": "hello"},
    )
    outer = {
        0: "0" if private else "1",
        1: "self-nim" if private else "1005",
        2: "1001",
        8: "100",
        10: wire.seal_message(bytes(32), inner, 1, 2, 3),
        12: "55",
    }
    try:
        if disposition == "ack-retry":
            with pytest.raises(OSError, match="ACK failure"):
                await adapter.ingest(outer)
            assert not adapter.dispatch_wakeup.is_set()
        await adapter.ingest(outer)
        assert adapter.dispatch_wakeup.is_set() is not rejected
        assert observe.call_count == int(not rejected)
        assert ack_attempts == (2 if disposition == "ack-retry" else 1)
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_removed_member_history_is_rejected_without_reconnect(
    tmp_path, deployment
):
    adapter = WangshangliaoAdapter(
        {"id": "fixture", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "history.sqlite")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(
        deployment=deployment,
        request=AsyncMock(return_value={"groupMemberInfo": []}),
    )
    adapter.groups = {"34": "team"}
    adapter.members = {"34": {}}
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    inner = wire.ApplicationMessage(
        sender=wire.Source(id=13),
        target=wire.Source(id=34),
        session=2,
        device=1,
        format=0,
        version=2,
        content=wire.Content(data="old"),
    )
    outer = {
        0: "1",
        1: "team",
        2: "removed",
        7: "123",
        8: "100",
        10: wire.seal_message(bytes(32), inner, 1, 2, 3),
        12: "55",
    }
    try:
        await adapter.ingest(outer)
        await adapter.ingest(outer)
        async with adapter.ledger.db.execute(
            "SELECT state FROM inbox WHERE message='55'"
        ) as cursor:
            assert await cursor.fetchall() == [("rejected_identity",)]
        await adapter.ledger.ingest("12", "34", "56", {})
        await adapter.ledger.mark("12", "34", "56", "processed")
        await adapter.ingest({**outer, 12: "56"})
        async with adapter.ledger.db.execute(
            "SELECT state FROM inbox WHERE message='56'"
        ) as cursor:
            assert await cursor.fetchone() == ("processed",)
        assert adapter.nim.acknowledge.await_count == 3
        assert adapter._event_queue.empty()
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_inbox_ack_failure_send_unknown_and_serial_pipeline(tmp_path, deployment):
    adapter = WangshangliaoAdapter(
        {"id": "fixture", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.groups = {"34": "team"}
    adapter.members = {"34": {"13": "sender", "12": "self"}}
    adapter.connection_state = "online"
    adapter.nim = SimpleNamespace(
        acknowledge=AsyncMock(side_effect=OSError("lost ack")),
        request=AsyncMock(side_effect=TimeoutError()),
    )
    inner = wire.ApplicationMessage(
        sender=wire.Source(id=13),
        target=wire.Source(id=34),
        session=2,
        device=1,
        format=0,
        version=2,
        content=wire.Content(data="@DH hi"),
    )
    attachment = wire.seal_message(bytes(32), inner, 1, 2, 3)
    outer = {
        0: "1",
        1: "team",
        2: "sender",
        7: "123",
        8: "100",
        10: attachment,
        12: "55",
    }
    try:
        with pytest.raises(OSError):
            await adapter.ingest(outer)
        adapter.nim.acknowledge.side_effect = None
        await adapter.ingest(outer)
        await adapter.ledger.ingest(
            "12",
            "34",
            "56",
            {
                "text": "normal",
                "sender": "13",
                "name": "Fixture",
                "created_at": 0,
                "mentions": [],
            },
        )
        worker = asyncio.create_task(adapter.process_group("34"))
        first = await asyncio.wait_for(adapter._event_queue.get(), 1)
        assert not first.eligible
        assert first.get_extra("wangshangliao_payload")["created_at"] == 123
        assert adapter._event_queue.empty()
        with pytest.raises(TimeoutError):
            await adapter.send_text("34", "key", "reply")
        assert await adapter.send_text("34", "key", "reply") == "unknown"
        assert adapter.nim.request.await_count == 1
        first.processing_completion.set_result(None)
        second = await asyncio.wait_for(adapter._event_queue.get(), 1)
        assert not second.eligible
        assert second.get_extra("_context_only") is True
        second.processing_completion.set_result(None)
        adapter.stopping.set()
        await asyncio.wait_for(worker, 1)
        async with adapter.ledger.db.execute(
            "SELECT state FROM inbox ORDER BY rowid"
        ) as cursor:
            assert await cursor.fetchall() == [("processed",), ("processed",)]
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_bot_save_failure_retry_duplicate_and_logout(
    tmp_path, monkeypatch, deployment
):
    from astrbot.core.platform.sources.wangshangliao import registration, storage
    from astrbot.dashboard.services import config_service
    from astrbot.dashboard.services.platform_service import PlatformService

    manager = RegistrationManager(lambda: deployment, LoginFixture)
    monkeypatch.setattr(registration, "registrations", manager)
    monkeypatch.setattr(storage, "instance_dir", lambda instance: tmp_path / instance)
    config = {"platform": []}
    platform_manager = SimpleNamespace(
        load_platform=AsyncMock(), terminate_platform=AsyncMock(), astrbot_config=config
    )
    service = config_service.BotConfigService(
        SimpleNamespace(astrbot_config=config, platform_manager=platform_manager)
    )
    platform_service = PlatformService(
        SimpleNamespace(platform_manager=platform_manager)
    )
    try:
        view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
        ready = await manager.action(
            "admin",
            {
                "action": "login",
                "instance_id": "bot",
                "registration_code": view["registration_code"],
                "account": "fixture",
                "password": "fixture",
                "validate_str": "human",
            },
        )
        setting = {
            "id": "bot",
            "type": "wangshangliao",
            "enable": True,
            "enabled_groups": ["34"],
            "session_ref": ready["session_ref"],
        }

        def fail(*args, **kwargs):
            raise OSError("fixture disk failure")

        monkeypatch.setattr(config_service, "save_config", fail)
        with pytest.raises(OSError):
            await service.create_bot(setting, "admin")
        assert config["platform"] == []
        assert Vault("bot", tmp_path / "bot").load() is None
        assert manager.claim("admin", "bot", ready["session_ref"])
        monkeypatch.setattr(config_service, "save_config", lambda *a, **kw: None)
        await service.create_bot(setting, "admin")
        await service.create_bot(setting, "admin")
        assert len(config["platform"]) == 1
        assert "session_ref" not in config["platform"][0]
        platform_manager.load_platform.assert_awaited_once()
        assert not manager.transactions
        with pytest.raises(ValueError):
            await service.create_bot(setting, "other")
        with pytest.raises(ValueError, match="fields"):
            await service.update_bot(
                "bot", {**config["platform"][0], "password": "fixture"}
            )
        await service.set_bot_enabled("bot", False)
        assert Vault("bot", tmp_path / "bot").load()
        result = await platform_service.handle_platform_registration(
            "wangshangliao", {"action": "logout", "instance_id": "bot"}, "admin"
        )
        assert result["status"] == "reauth_required"
        assert Vault("bot", tmp_path / "bot").load() is None
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_account", ["34", "12"])
async def test_saved_instance_login_cannot_replace_account(
    tmp_path, monkeypatch, deployment, saved_account
):
    """Preserve account isolation while allowing the original account to reauthenticate."""
    from astrbot.core.platform.sources.wangshangliao import registration, storage
    from astrbot.dashboard.services import config_service

    manager = RegistrationManager(lambda: deployment, LoginFixture)
    monkeypatch.setattr(registration, "registrations", manager)
    monkeypatch.setattr(storage, "instance_dir", lambda instance: tmp_path / instance)
    current = {
        "id": "account-bound-bot",
        "type": "wangshangliao",
        "enable": False,
        "account_id": saved_account,
        "enabled_groups": ["34"],
    }
    config = {"platform": [dict(current)]}
    platform_manager = SimpleNamespace(
        load_platform=AsyncMock(), terminate_platform=AsyncMock(), astrbot_config=config
    )
    service = config_service.BotConfigService(
        SimpleNamespace(astrbot_config=config, platform_manager=platform_manager)
    )
    persist = Mock()
    monkeypatch.setattr(config_service, "save_config", persist)
    vault = Vault(current["id"], tmp_path / current["id"])
    vault.save({"fixture": "original-session"})
    original = (vault.root / "session.sealed").read_bytes()
    try:
        started = await manager.action(
            "admin", {"action": "start", "instance_id": current["id"]}
        )
        ready = await manager.action(
            "admin",
            {
                "action": "login",
                "instance_id": current["id"],
                "registration_code": started["registration_code"],
                "account": "fixture",
                "password": "fixture",
                "validate_str": "human",
            },
        )
        candidate = {**current, "session_ref": ready["session_ref"], "account_id": "12"}
        if saved_account != "12":
            with pytest.raises(
                ValueError, match="account_change_requires_new_instance"
            ):
                await service.update_bot(current["id"], candidate, "admin")
            assert config["platform"] == [current]
            assert (vault.root / "session.sealed").read_bytes() == original
            assert manager.claim("admin", current["id"], ready["session_ref"])
            platform_manager.terminate_platform.assert_not_awaited()
            platform_manager.load_platform.assert_not_awaited()
            persist.assert_not_called()
        else:
            await service.update_bot(current["id"], candidate, "admin")
            assert config["platform"][0]["account_id"] == "12"
            assert "session_ref" not in config["platform"][0]
            assert not manager.transactions
            platform_manager.terminate_platform.assert_awaited_once_with(current["id"])
            platform_manager.load_platform.assert_awaited_once()
            persist.assert_called_once()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_registration_strict_input_and_missing_deployment(monkeypatch, tmp_path):
    from astrbot.core.utils import astrbot_path

    monkeypatch.setattr(astrbot_path, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.delenv("ASTRBOT_WANGSHANGLIAO_CONFIG", raising=False)
    manager = RegistrationManager()
    with pytest.raises(wire.ProtocolError, match="deployment_missing"):
        await manager.action("admin", {"action": "start", "instance_id": "bot"})
    with pytest.raises(wire.ProtocolError, match="registration_input"):
        await manager.action(
            "admin", {"action": "start", "instance_id": "bot", "route": "/unreviewed"}
        )
    assert not manager.transactions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "params"),
    [
        (
            "/v1/user/get-change-device-verify",
            {
                "account": "fixture",
                "passwd": "fixture",
                "type": "LOGIN_TYPE_ACCOUNT_PWD",
            },
        ),
        ("/v1/group/get-group-apply-list", {"groupId": 34, "v": "0"}),
        ("/v1/group/get-apply-logs", {}),
        ("/v1/group/get-apply-logs", {"lastId": "0"}),
        ("/v1/friend/friend-apply-list", {"lastId": 0}),
        (
            "/v1/friend/friend-apply-handler",
            {
                "flId": 123456789012,
                "state": "FRIENDLOG_STATE_AGREE",
                "nickName": "",
                "classId": 0,
                "friendCircle": {"notSeeMe": False, "notSeeHim": True},
            },
        ),
    ],
)
async def test_http_signed_encrypted_exchange(deployment, route, params):
    import nacl.bindings
    import zstandard
    from nacl.signing import VerifyKey

    seen = []

    async def business(request):
        raw_metadata = wire.unb64(request.headers["x-request"])
        VerifyKey(wire.unb64(request.headers["x-seed"])).verify(
            raw_metadata, wire.unb64(request.headers["x-hash"])
        )
        values = {}
        offset = 0
        while offset < len(raw_metadata):
            tag, offset = wire.read_varint(raw_metadata, offset)
            value, offset = wire.read_varint(raw_metadata, offset, 64)
            values[tag >> 3] = value
        plaintext = json.dumps(
            params, ensure_ascii=False, separators=(",", ":")
        ).encode()
        aad = wire.binding(values[6], plaintext, values[1])[1]
        assert wire.open_business(bytes(32), aad, await request.read()) == plaintext
        seen.append(request.path)
        compressed = zstandard.ZstdCompressor().compress(
            b'{"code":0,"data":{"fixture":true}}'
        )
        sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
            compressed, aad, bytes(8), bytes(32)
        )
        return web.Response(body=sealed[-16:] + bytes(16) + sealed[:-16])

    app = web.Application()
    app.router.add_post(route, business)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fixture = deployment.model_copy(
        update={
            "origin": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        }
    )
    try:
        async with aiohttp.ClientSession() as http:
            client = BusinessClient(fixture, http)
            client.jwt = "fixture-session"
            result = await client.request(route, params)
            assert result == {"fixture": True}
            for forbidden in (
                "/arbitrary",
                "/v1/group/group-member-invite",
                "/v1/group/group-manage-apply",
                "/v1/group/del-apply-logs",
            ):
                with pytest.raises(BusinessError, match="unsupported_route"):
                    await client.request(forbidden, {})
        assert seen == [route]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    ["/v1/group/get-group-apply-list", "/v1/group/get-apply-logs"],
)
async def test_invitation_reads_require_authentication(deployment, monkeypatch, route):
    post = AsyncMock(side_effect=AssertionError("Unexpected request"))
    monkeypatch.setattr(aiohttp.ClientSession, "post", post)
    async with aiohttp.ClientSession() as http:
        client = BusinessClient(deployment, http)
        with pytest.raises(BusinessError, match="reauth_required"):
            await client.request(route, {"groupId": 34, "v": "0"})
    post.assert_not_called()


@pytest.mark.asyncio
async def test_real_eventbus_waits_for_model_and_suppresses_ordinary_reply(
    tmp_path, deployment
):
    from unittest.mock import MagicMock

    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.core.event_bus import EventBus

    adapter = WangshangliaoAdapter(
        {"id": "fixture", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite")
    await adapter.ledger.open()
    adapter.groups = {"34": "team"}
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.nim = SimpleNamespace(
        request=AsyncMock(
            return_value=(200, wire.properties([(7, b"100"), (12, b"200")]))
        )
    )
    adapter.connection_state = "online"
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    calls = []

    async def execute(event):
        calls.append(event.message_obj.message_id)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        await event.send(MessageChain([Plain("model fixture")]))
        if len(calls) == 2:
            completed.set()

    manager = MagicMock()
    manager.get_conf_info.return_value = {"id": "fixture"}
    bus = EventBus(
        adapter._event_queue, {"fixture": SimpleNamespace(execute=execute)}, manager
    )
    bus._print_event = lambda *args: None
    for mid, text in [("1", "@DH hello"), ("2", "ordinary")]:
        adapter.nim_account = "912"
        adapter.members = {"34": {"13": "913"}}
        await adapter.ledger.ingest(
            "12",
            "34",
            mid,
            {
                "text": text,
                "sender": "13",
                "name": "Fixture",
                "created_at": 0,
                "mentions": ["912"] if mid == "1" else [],
            },
        )
    dispatcher = asyncio.create_task(bus.dispatch())
    worker = asyncio.create_task(adapter.process_group("34"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert calls == ["1"]
        await adapter.ledger.ingest("12", "34", "1", {})
        release.set()
        await asyncio.wait_for(completed.wait(), 1)
        assert calls == ["1", "2"]
        assert adapter.nim.request.await_count == 1
        key = "12/34/1/0"
        assert await adapter.ledger.receipt(key) == {
            "status": "accepted",
            "server_id": "200",
        }
        with pytest.raises(wire.ProtocolError, match="send_key_conflict"):
            await adapter.send_text("34", key, "model fixture")
        assert adapter.nim.request.await_count == 1
    finally:
        adapter.stopping.set()
        worker.cancel()
        dispatcher.cancel()
        await asyncio.gather(worker, dispatcher, return_exceptions=True)
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("burst", [1, 65])
async def test_native_lifecycle_reconnect_cursor_and_stop(
    tmp_path, monkeypatch, deployment, burst
):
    from astrbot.core.platform.sources.wangshangliao import adapter as module
    from astrbot.core.platform.sources.wangshangliao import storage

    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(storage, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(module.Deployment, "load", lambda: deployment)
    saved = {
        "business": {
            "origin": deployment.origin,
            "uid": 12,
            "kind": 1,
            "device": 1,
            "jwt": "fixture",
            "mac": wire.b64(bytes(16)),
        },
        "nim_id": "self",
        "nim_token": "fixture",
    }
    Vault("fixture", tmp_path).save(saved)
    cursors = []
    reconnected = asyncio.Event()
    instances = []

    class BusinessFixture(BusinessClient):
        async def request(self, route, params, exchange=None):
            if route.endswith("RefreshToken"):
                return {"nimToken": "refreshed-fixture"}
            if route.endswith("get-group-list"):
                return {"owner": [{"groupId": 34, "groupCloudId": "900"}], "member": []}
            return {"groupMemberInfo": [{"userId": 13, "nimId": "sender"}]}

    class DisconnectQueue(asyncio.Queue):
        async def get(self):
            if self.empty():
                raise wire.ProtocolError("fixture_disconnect")
            return await super().get()

    class NimFixture:
        def __init__(self, http):
            self.closed = asyncio.Event()
            self.error = "disconnected"
            self.pushes = (
                DisconnectQueue(maxsize=4)
                if not instances
                else asyncio.Queue(maxsize=4)
            )
            self.acks = []
            instances.append(self)

        async def connect(self, *args):
            inner = wire.ApplicationMessage(
                sender=wire.Source(id=13),
                target=wire.Source(id=34),
                session=2,
                device=1,
                format=0,
                content=wire.Content(data="ordinary"),
            )
            text = wire.seal_message(bytes(32), inner, 5, 6, 7)
            fields = [
                (0, b"1"),
                (1, b"900"),
                (2, b"sender"),
                (7, b"123"),
                (8, b"100"),
                (10, text.encode()),
                (12, b"55"),
            ]
            self.sample = fields
            self.pushes.put_nowait((8, 3, 0, 200, wire.properties(fields)))

        async def request(self, service, command, body):
            fields, _ = wire.read_properties(body)
            cursors.append(fields[2])
            if len(instances) == 1:
                for index in range(1, burst):
                    fields = [
                        (key, str(55 + index).encode() if key == 12 else value)
                        for key, value in self.sample
                    ]
                    await self.pushes.put((8, 3, 0, 200, wire.properties(fields)))
                async with native.ledger.db.execute(
                    "SELECT cursor FROM sync WHERE account='12'"
                ) as cursor:
                    assert await cursor.fetchone() is None
            if len(instances) > 1:
                reconnected.set()
            return 200, (50).to_bytes(8, "little")

        async def acknowledge(self, scene, server_id):
            self.acks.append(server_id)

        async def heartbeat(self):
            pass

        async def close(self):
            self.closed.set()

    monkeypatch.setattr(module, "BusinessClient", BusinessFixture)
    monkeypatch.setattr(module, "NimClient", NimFixture)
    native = module.WangshangliaoAdapter(
        {"id": "fixture", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    task = asyncio.create_task(native.run())
    try:
        await asyncio.wait_for(reconnected.wait(), 3)
        assert cursors == [b"0", b"50"]
        assert not task.done()
        duplicate = module.WangshangliaoAdapter(
            {"id": "duplicate", "account_id": "12", "enabled_groups": []},
            {},
            asyncio.Queue(),
        )
        await duplicate.run()
        assert duplicate.connection_state == "error"
        assert duplicate.last_error.message == "account_already_active"
    finally:
        await native.terminate()
        await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(wire.ProtocolError, match="not_online"):
        await native.send_text("34", "after-stop", "no reply")
    assert native.http.closed
    assert all(client.closed.is_set() for client in instances)
    assert "12" not in module.ACTIVE_ACCOUNTS
    assert all(worker.done() for worker in native.workers.values())
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    try:
        async with ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
            assert (await cursor.fetchone())[0] == burst
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_private_message_reply_dedup_and_identity(tmp_path, deployment):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.api.platform import MessageType

    adapter = WangshangliaoAdapter(
        {"id": "private-fixture", "account_id": "12", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "private.sqlite")
    await adapter.ledger.open()
    dispatcher = asyncio.create_task(adapter.dispatch_pending())
    adapter.business = SimpleNamespace(deployment=deployment)
    adapter.nim_account = "self-nim"
    adapter.connection_state = "online"
    adapter.nim = SimpleNamespace(
        acknowledge=AsyncMock(),
        request=AsyncMock(
            return_value=(200, wire.properties([(7, b"100"), (12, b"200")]))
        ),
    )
    inner = wire.ApplicationMessage(
        sender=wire.Source(id=13),
        target=wire.Source(id=12),
        session=1,
        device=1,
        format=0,
        content=wire.Content(data="你好"),
    )
    outer = {
        0: "0",
        1: "self-nim",
        2: "peer-nim",
        7: "100",
        8: "100",
        10: wire.seal_message(bytes(32), inner, 5, 6, 7),
        12: "55",
    }
    try:
        await adapter.ingest(outer)
        event = await asyncio.wait_for(adapter._event_queue.get(), 1)
        assert event.message_obj.type == MessageType.FRIEND_MESSAGE
        assert event.get_extra("wangshangliao_payload")["created_at"] == 100
        assert event.get_group_id() == ""
        assert event.eligible and "private/13/" in event.session_id
        await event.send(MessageChain([Plain("私信回复")]))
        args = adapter.nim.request.call_args.args
        assert args[:2] == (7, 1)
        fields, _ = wire.read_properties(args[2])
        assert fields[0] == b"0" and fields[1] == b"peer-nim"
        sent = wire.open_message(bytes(32), fields[10].decode())
        assert sent.session == 1 and sent.target.id == 13
        event.processing_completion.set_result(None)
        await adapter.ingest(outer)
        await asyncio.sleep(0.05)
        assert adapter._event_queue.empty()
        adapter.nim.acknowledge.assert_awaited_with(0, "55")
        inner.target.id = 99
        wrong = {**outer, 10: wire.seal_message(bytes(32), inner, 8, 9, 10)}
        with pytest.raises(wire.ProtocolError, match="private_message_identity"):
            await adapter.ingest(wrong)
        await adapter.ingest({**outer, 1: "other-account"})
        assert adapter._event_queue.empty()
    finally:
        adapter.stopping.set()
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
        for task in adapter.workers.values():
            task.cancel()
        await asyncio.gather(*adapter.workers.values(), return_exceptions=True)
        await adapter.ledger.close()


@pytest.mark.parametrize("message_key", [None, "", "invalid"])
def test_legacy_login_deployment(tmp_path, monkeypatch, deployment, message_key):
    payload = deployment.model_dump(mode="json")
    for name in ("signing_seed", "body_key", "server_key"):
        payload[name] = getattr(deployment, name).get_secret_value()
    if message_key is None:
        payload.pop("message_key")
    else:
        payload["message_key"] = message_key
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    monkeypatch.setenv("ASTRBOT_WANGSHANGLIAO_CONFIG", str(path))
    if message_key == "invalid":
        with pytest.raises(wire.ProtocolError, match="deployment_invalid"):
            Deployment.load()
    else:
        loaded = Deployment.load()
        assert not loaded.message_key.get_secret_value()
        assert loaded.key("body_key") == deployment.key("body_key")


def test_imported_deployment_fallback(tmp_path, monkeypatch, deployment):
    from astrbot.core.utils import astrbot_path

    monkeypatch.delenv("ASTRBOT_WANGSHANGLIAO_CONFIG", raising=False)
    monkeypatch.setattr(astrbot_path, "get_astrbot_data_path", lambda: str(tmp_path))
    with pytest.raises(wire.ProtocolError, match="deployment_missing"):
        Deployment.load()
    payload = deployment.model_dump(mode="json")
    for name in ("signing_seed", "body_key", "server_key", "message_key"):
        payload[name] = getattr(deployment, name).get_secret_value()
    root = tmp_path / "platform_data/wangshangliao/deployment"
    vault = Vault("wangshangliao-deployment-v1", root)
    vault.save(payload)
    assert Deployment.load().key("message_key") == deployment.key("message_key")
    assert (
        payload["signing_seed"].encode() not in (root / "session.sealed").read_bytes()
    )
    monkeypatch.setenv("ASTRBOT_WANGSHANGLIAO_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(wire.ProtocolError, match="deployment_invalid"):
        Deployment.load()
    monkeypatch.delenv("ASTRBOT_WANGSHANGLIAO_CONFIG")
    (root / "session.sealed").write_bytes(b"corrupt")
    with pytest.raises(wire.ProtocolError, match="deployment_invalid"):
        Deployment.load()


@pytest.mark.asyncio
async def test_authenticated_group_selection(deployment):
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        started = await manager.action(
            "owner", {"action": "start", "instance_id": "bot"}
        )
        payload = {
            "instance_id": "bot",
            "registration_code": started["registration_code"],
        }
        with pytest.raises(wire.ProtocolError, match="reauth_required"):
            await manager.action("owner", dict(payload, action="groups"))
        tx = manager.transactions[started["registration_code"]]
        tx.state = "authenticated"
        tx.client.request = AsyncMock(
            return_value={
                "owner": [{"groupId": 1, "groupName": "Owned"}],
                "member": [{"groupId": 2, "groupName": "Member"}],
            }
        )
        result = await manager.action("owner", dict(payload, action="groups"))
        assert [g["id"] for g in result["groups"]] == ["1", "2"]
        with pytest.raises(wire.ProtocolError, match="registration_not_found"):
            await manager.action("other", dict(payload, action="groups"))
        tx.client.request.return_value = {"owner": []}
        with pytest.raises(wire.ProtocolError, match="groups_shape"):
            await manager.action("owner", dict(payload, action="groups"))
        assert tx.state == "authenticated"
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,retry,label",
    [
        (200, 401, None, "reauth_required"),
        (401, 0, None, "reauth_required"),
        (429, 0, "12", "rate_limited"),
        (429, 0, "bad", "rate_limited"),
        (503, 0, None, "http_rejected"),
        (200, 1039, None, "phone_not_found"),
    ],
)
async def test_provider_error_classification(deployment, status, code, retry, label):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.json_response(
            {"code": code, "msg": "fixture"},
            status=status,
            headers={"Retry-After": retry} if retry else {},
        )

    app = web.Application()
    app.router.add_post("/v1/user/RefreshToken", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fixture = deployment.model_copy(
        update={
            "origin": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        }
    )
    try:
        async with aiohttp.ClientSession() as http:
            client = BusinessClient(fixture, http)
            client.jwt = "fixture"
            with pytest.raises(BusinessError, match=label) as raised:
                await client.request("/v1/user/RefreshToken", {})
            if code == 1039:
                assert raised.value.number == 1039
            if code == 401 or status == 401:
                assert client.jwt == ""
                with pytest.raises(BusinessError, match="reauth_required"):
                    await client.request("/v1/user/RefreshToken", {})
            else:
                assert client.jwt == "fixture"
            if status == 429:
                assert raised.value.retry_after == (12 if retry == "12" else 30)
        assert len(calls) == 1
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_waiting_request_rechecks_invalidated_credentials(deployment):
    """Reject queued authenticated requests after another request clears credentials."""
    async with aiohttp.ClientSession() as http:
        client = BusinessClient(deployment, http)
        client.jwt = "fixture"
        await client.lock.acquire()
        pending = asyncio.create_task(client.request("/v1/user/RefreshToken", {}))
        try:
            await asyncio.sleep(0)
            assert not pending.done()
            client.jwt, client.mac = "", b""
        finally:
            client.lock.release()
        with pytest.raises(BusinessError, match="reauth_required"):
            await pending


@pytest.mark.asyncio
async def test_friend_request_guard_checked_after_shared_http_lock(deployment):
    from astrbot.core.platform.sources.wangshangliao.business_http import (
        request_business,
    )

    authorized = True

    def guard():
        if not authorized:
            raise wire.ProtocolError("friend_approval_authority_changed")

    async with aiohttp.ClientSession() as http:
        client = BusinessClient(deployment, http)
        client.jwt = "fixture"
        await client.lock.acquire()
        pending = asyncio.create_task(
            request_business(
                client, "/v1/friend/friend-apply-handler", {}, authorize=guard
            )
        )
        try:
            await asyncio.sleep(0)
            assert not pending.done()
            authorized = False
        finally:
            client.lock.release()
        with pytest.raises(wire.ProtocolError, match="authority_changed"):
            await pending
        assert client.last_request == 0


@pytest.mark.asyncio
async def test_saved_groups_without_login_transaction(monkeypatch, deployment):
    from astrbot.core.platform.sources.wangshangliao.business import BusinessClient
    from astrbot.dashboard.services.platform_service import PlatformService

    bot = {"id": "saved-fixture", "type": "wangshangliao", "account_id": "12"}
    service = PlatformService(
        SimpleNamespace(
            platform_manager=SimpleNamespace(
                astrbot_config={"platform": [bot]}, platform_insts=[]
            )
        )
    )
    monkeypatch.setattr(Deployment, "load", lambda: deployment)
    monkeypatch.setattr(Vault, "load", lambda _: {"business": {"uid": 12}})
    monkeypatch.setattr(BusinessClient, "restore", lambda *_: None)
    request = AsyncMock(
        return_value={
            "owner": [],
            "member": [{"groupId": 34, "groupName": "Member group"}],
        }
    )
    monkeypatch.setattr(BusinessClient, "request", request)
    result = await service.handle_platform_registration(
        "wangshangliao",
        {"action": "groups", "instance_id": "saved-fixture"},
        owner="admin",
    )
    assert result["groups"][0]["id"] == "34"
    assert request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ADMIN", "admin"),
        ("OWNER", "owner"),
        ("MEMBER", "member"),
        ("unrecognized", "unknown"),
    ],
)
async def test_directory_role_evidence(raw, expected):
    from astrbot.core.platform.sources.wangshangliao.directory import group_directory

    client = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {"owner": [], "member": [{"groupId": 34, "groupName": "Fixture"}]},
                {"groupMemberInfo": [{"userId": 12, "groupRole": raw}]},
            ]
        )
    )
    result = await group_directory(client, "12")
    assert result["groups"][0]["role"] == expected
    assert result["complete"] is False
    assert result["source"] == "platform"


@pytest.mark.asyncio
async def test_directory_role_lookup_preserves_auth_error():
    from astrbot.core.platform.sources.wangshangliao.directory import group_directory

    client = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {"owner": [], "member": [{"groupId": 34}]},
                BusinessError("reauth_required", 401),
            ]
        )
    )
    with pytest.raises(BusinessError, match="reauth_required"):
        await group_directory(client, "12")


@pytest.mark.asyncio
async def test_directory_owner_requires_identity_match():
    from astrbot.core.platform.sources.wangshangliao.directory import group_directory

    client = SimpleNamespace(
        request=AsyncMock(
            return_value={"owner": [{"groupId": 34, "ownerUserId": 12}], "member": []}
        )
    )
    result = await group_directory(client, "12")
    assert result["groups"][0]["role"] == "owner"
    client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_independent_sms_login_without_password(deployment):
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        view = await manager.action(
            "admin", {"action": "start", "instance_id": "sms-bot"}
        )
        base = {
            "instance_id": "sms-bot",
            "registration_code": view["registration_code"],
        }
        tx = manager.transactions[view["registration_code"]]
        tx.client.request = AsyncMock(return_value={"key": "sms-fixture-key"})
        tx.client.login = AsyncMock(
            return_value={"uid": 12, "nimId": "peer", "nimToken": "fixture"}
        )
        result = await manager.action(
            "admin",
            {
                **base,
                "action": "request_sms",
                "account": "13800000000",
                "validate_str": "human-fixture",
            },
        )
        assert result["status"] == "sms_required"
        params = tx.client.request.call_args.args[1]
        assert "passwd" not in params and "Key" not in params
        assert params["phone"] == {
            "countryCode": 86,
            "nationalNumber": 13800000000,
            "maskedNationalNumber": "",
        }
        assert type(params["phone"]["countryCode"]) is int
        assert type(params["phone"]["nationalNumber"]) is int
        result = await manager.action(
            "admin",
            {
                **base,
                "action": "request_sms",
                "account": "13800000000",
                "validate_str": "human-fixture",
            },
        )
        assert result["error"] == "sms_cooldown"
        result = await manager.action(
            "admin",
            {
                **base,
                "action": "verify_sms",
                "verification_code": "123456",
                "validate_str": "human-fixture",
            },
        )
        assert result["status"] == "authenticated"
        assert tx.client.login.call_args.args[0]["type"] == "LOGIN_TYPE_SMS"
        assert tx.client.login.call_args.args[0]["phone"] == params["phone"]
        assert "sms-fixture-key" not in json.dumps(result)
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected", "provider_code"),
    [
        (BusinessError("business_rejected", 1201), "business_rejected", 1201),
        (BusinessError("transport"), "transport", None),
        (None, "sms_challenge", None),
        ({}, "sms_challenge", None),
        ({"key": ""}, "sms_challenge", None),
        ({"key": "x" * 8193}, "sms_challenge", None),
        ({"key": 123}, "sms_challenge", None),
    ],
)
async def test_rejected_sms_does_not_create_challenge(
    deployment, response, expected, provider_code
):
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        view = await manager.action(
            "admin", {"action": "start", "instance_id": "sms-failure"}
        )
        base = {
            "instance_id": "sms-failure",
            "registration_code": view["registration_code"],
            "validate_str": "private-proof",
        }
        tx = manager.transactions[view["registration_code"]]
        tx.client.request = AsyncMock(
            side_effect=response if isinstance(response, Exception) else None,
            return_value=response,
        )
        tx.client.login = AsyncMock()
        result = await manager.action(
            "admin", dict(base, action="request_sms", account="13800000000")
        )
        assert result["status"] == "failed"
        assert result["error"] == expected
        assert result["error_action"] == "request_sms"
        assert result["provider_code"] == provider_code
        assert 55 <= result["resend_after"] <= 60
        assert not tx.challenge
        assert "13800000000" not in json.dumps(result)
        assert "private-proof" not in json.dumps(result)
        result = await manager.action(
            "admin", dict(base, action="request_sms", account="13800000000")
        )
        assert result["error"] == "sms_cooldown"
        assert result["provider_code"] is None
        assert tx.client.request.await_count == 1
        result = await manager.action(
            "admin", dict(base, action="verify_sms", verification_code="123456")
        )
        assert result["error"] == "sms_challenge"
        tx.client.login.assert_not_awaited()
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [BusinessError("business_rejected", 1202), None, {}, {"key": ""}],
)
async def test_failed_resend_preserves_confirmed_sms_challenge(deployment, response):
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        view = await manager.action(
            "admin", {"action": "start", "instance_id": "sms-resend"}
        )
        base = {
            "instance_id": "sms-resend",
            "registration_code": view["registration_code"],
            "validate_str": "private-proof",
        }
        tx = manager.transactions[view["registration_code"]]
        tx.client.request = AsyncMock(return_value={"key": "confirmed-key"})
        await manager.action(
            "admin", dict(base, action="request_sms", account="13800000000")
        )
        tx.sms_resend_at = 0
        tx.client.request = AsyncMock(
            side_effect=response if isinstance(response, Exception) else None,
            return_value=response,
        )
        result = await manager.action("admin", dict(base, action="resend_sms"))
        assert result["status"] == "sms_required"
        assert result["error_action"] == "resend_sms"
        assert tx.challenge["key"] == "confirmed-key"
        assert 55 <= result["resend_after"] <= 60
        result = await manager.action(
            "admin", dict(base, action="verify_sms", verification_code="123456")
        )
        assert result["status"] == "authenticated"
        assert result["resend_after"] == 0
        assert result["error_action"] == ""
        assert result["provider_code"] is None
        tx.client.request.assert_awaited_once()
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["login", "request_sms"])
@pytest.mark.parametrize("number", [1201, -1201, 0, True, "private-code", 1 << 40])
async def test_login_reports_only_safe_provider_code_and_action(
    deployment, monkeypatch, action, number
):
    from astrbot.core.platform.sources.wangshangliao import diagnostics

    logs = []
    monkeypatch.setattr(
        diagnostics.logger, "log", lambda level, msg, *args: logs.append(msg % args)
    )
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        view = await manager.action(
            "admin", {"action": "start", "instance_id": "code-fixture"}
        )
        tx = manager.transactions[view["registration_code"]]
        failure = BusinessError("business_rejected", number)
        tx.client.login = AsyncMock(side_effect=failure)
        tx.client.request = AsyncMock(side_effect=failure)
        result = await manager.action(
            "admin",
            {
                "instance_id": "code-fixture",
                "registration_code": view["registration_code"],
                "action": action,
                "account": "13800000000",
                "password": "private-password",
                "validate_str": "private-proof",
            },
        )
        safe = number if type(number) is int and 0 < abs(number) < 1 << 31 else None
        assert result["provider_code"] == safe
        assert result["error_action"] == action
        assert f"action={action}" in logs[-1]
        assert (
            "route=sms_anon" in logs[-1]
            if action == "request_sms"
            else ("route=user_login" in logs[-1])
        )
        assert f"provider_code={safe if safe is not None else '-'}" in logs[-1]
        for secret in (
            "13800000000",
            "private-password",
            "private-proof",
            "private-code",
        ):
            assert secret not in "".join(logs)
            assert secret not in json.dumps(result)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sms_provider_retry_after_is_respected(deployment):
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
        tx = manager.transactions[view["registration_code"]]
        tx.client.request = AsyncMock(
            side_effect=BusinessError("rate_limited", 429, 120)
        )
        result = await manager.action(
            "admin",
            {
                "instance_id": "bot",
                "registration_code": view["registration_code"],
                "action": "request_sms",
                "account": "13800000000",
                "validate_str": "private-proof",
            },
        )
        assert result["error"] == "rate_limited"
        assert 115 <= result["resend_after"] <= 120
        assert result["provider_code"] == 429
        assert result["status"] == "failed"
        assert not tx.challenge
        tx.client.request.assert_awaited_once()
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["login", "request_sms", "verify_sms", "resend_sms"])
async def test_missing_verification_is_distinct_from_bad_login_input(
    deployment, monkeypatch, action
):
    monkeypatch.setenv("ASTRBOT_YIDUN_SOLVER_URL", "")
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    try:
        view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
        tx = manager.transactions[view["registration_code"]]
        tx.challenge = {"phone": {"nationalNumber": "13800000000"}, "key": "key"}
        tx.client.request = AsyncMock()
        tx.client.login = AsyncMock()
        result = await manager.action(
            "admin",
            {
                "instance_id": "bot",
                "registration_code": view["registration_code"],
                "action": action,
                "account": "13800000000",
                "password": "private-password",
                "verification_code": "123456",
            },
        )
        assert result["error"] == "captcha_required"
        assert result["error_action"] == action
        tx.client.request.assert_not_awaited()
        tx.client.login.assert_not_awaited()
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["34", "private/13/cGVlcg=="])
async def test_managed_bot_messages_never_dispatch(tmp_path, monkeypatch, group):
    from astrbot.core import astrbot_config

    monkeypatch.setitem(
        astrbot_config,
        "platform",
        [{"type": "wangshangliao", "account_id": "13", "enable": False}],
    )
    adapter = WangshangliaoAdapter(
        {"id": "fixture", "account_id": "12", "enabled_groups": ["34"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "bot-exclusion.sqlite")
    await adapter.ledger.open()
    adapter.connection_state = "online"
    try:
        await adapter.ledger.ingest("12", group, "bot", {"sender": "13"})
        await adapter.ledger.ingest(
            "12",
            group,
            "human",
            {
                "sender": "99",
                "name": "Human",
                "text": "hello",
                "created_at": 0,
                "mentions": [],
            },
        )
        worker = asyncio.create_task(adapter.process_group(group))
        event = await asyncio.wait_for(adapter._event_queue.get(), 2)
        assert event.get_sender_id() == "99"
        async with adapter.ledger.db.execute(
            "SELECT state FROM inbox WHERE message='bot'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == "ignored_bot"
        event.processing_completion.set_result(None)
        adapter.stopping.set()
        await asyncio.wait_for(worker, 2)
        with pytest.raises(wire.ProtocolError, match="managed_bot_peer"):
            adapter.stopping.clear()
            await adapter.send_text("private/13/cGVlcg==", "no-send", "hello")
    finally:
        adapter.stopping.set()
        await adapter.ledger.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["cancel", "expire"])
async def test_solver_completion_does_not_submit_invalid_transaction(
    deployment, monkeypatch, invalidate
):
    monkeypatch.delenv("ASTRBOT_YIDUN_SOLVER_URL", raising=False)
    manager = RegistrationManager(lambda: deployment, LoginFixture)
    started, release = asyncio.Event(), asyncio.Event()

    async def solve(*args):
        started.set()
        await release.wait()
        return "fixture-validation"

    monkeypatch.setattr(manager.solver, "solve", solve)
    try:
        view = await manager.action("admin", {"action": "start", "instance_id": "bot"})
        code = view["registration_code"]
        tx = manager.transactions[code]
        login = AsyncMock()
        monkeypatch.setattr(tx.client, "login", login)
        pending = asyncio.create_task(
            manager.action(
                "admin",
                {
                    "action": "login",
                    "instance_id": "bot",
                    "registration_code": code,
                    "account": "fixture",
                    "password": "fixture",
                },
            )
        )
        await started.wait()
        if invalidate == "cancel":
            await manager.discard(code)
        else:
            tx.expires = 0
        release.set()
        if invalidate == "cancel":
            with pytest.raises(wire.ProtocolError, match="registration_cancelled"):
                await pending
        else:
            result = await pending
            assert result["error"] == "registration_expired"
        login.assert_not_awaited()
        assert not tx.saved
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_reply_and_proactive_settings_survive_save_reload(tmp_path, monkeypatch):
    from astrbot.core.platform.sources.wangshangliao import storage
    from astrbot.dashboard.services import config_service

    monkeypatch.setattr(storage, "instance_dir", lambda instance: tmp_path / instance)
    current = {
        "id": "persist",
        "type": "wangshangliao",
        "account_id": "12",
        "enable": True,
    }
    config = {"platform": [current]}
    manager = SimpleNamespace(load_platform=AsyncMock(), terminate_platform=AsyncMock())
    service = config_service.BotConfigService(
        SimpleNamespace(astrbot_config=config, platform_manager=manager)
    )
    path = tmp_path / "config.json"
    monkeypatch.setattr(
        config_service,
        "save_config",
        lambda *args, **kwargs: path.write_text(json.dumps(config)),
    )
    for enabled in (True, False):
        setting = {
            **service.get_bot("persist")["bot"],
            "enabled_groups": ["34", "35"],
            "reply_private": enabled,
            "reply_groups": {"34": enabled, "35": False},
            "proactive_send": {"enabled": enabled},
            "moderation": {"enabled": enabled, "permissions": {"34": ["mute"]}},
        }
        await service.update_bot("persist", setting, "admin")
        loaded = json.loads(path.read_text())
        fresh = config_service.BotConfigService(
            SimpleNamespace(astrbot_config=loaded, platform_manager=manager)
        )
        expected = {
            **setting,
            "proactive_send": {"enabled": enabled, "targets": []},
            "ai_routes": {"admin_provider_id": "", "groups": {}},
        }
        assert fresh.get_bot("persist")["bot"] == expected
        assert manager.load_platform.call_args.args[0] == {
            key: value
            for key, value in expected.items()
            if not key.startswith("_business_")
        }
