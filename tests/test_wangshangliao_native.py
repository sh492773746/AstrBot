"""Offline native login, transport and crash-boundary regression tests."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from nacl.public import PrivateKey, SealedBox
from nacl.signing import SigningKey

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import (
    WangshangliaoAdapter,
    message_timestamp,
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
def test_message_timestamp_uses_server_time_not_receipt_time(server_time, inner_time, expected):
    assert message_timestamp(SimpleNamespace(created_at=inner_time), {7: server_time}) == expected


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
async def test_nim_single_reader_interleaved_pushes_and_receipts():
    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for incoming in ws:
            service, command, serial, _, body = wire.read_packet(incoming.data)
            if (service, command) == (2, 3):
                reply = wire.properties([(102, b"connection-fixture")])
            elif (service, command) == (8, 2):
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
                                (12, b"55"),
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
            try:
                await client.open_socket(f"ws://127.0.0.1:{port}/socket", [])
                response, heartbeat = await asyncio.gather(
                    client.request(8, 2), client.heartbeat()
                )
                assert response[0] == 200 and heartbeat is None
                assert (await asyncio.wait_for(client.pushes.get(), 1))[:2] == (8, 3)
                assert not client.pending
            finally:
                await client.close()
                assert client.closed.is_set()
    finally:
        await runner.cleanup()


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
async def test_native_lifecycle_reconnect_cursor_and_stop(
    tmp_path, monkeypatch, deployment
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
            self.pushes = DisconnectQueue() if not instances else asyncio.Queue()
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
            self.pushes.put_nowait((8, 3, 0, 200, wire.properties(fields)))

        async def request(self, service, command, body):
            fields, _ = wire.read_properties(body)
            cursors.append(fields[2])
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
            assert (await cursor.fetchone())[0] == 1
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
        assert params["phone"]["nationalNumber"] == "13800000000"
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
        assert "sms-fixture-key" not in json.dumps(result)
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
        {"id": "fixture", "account_id": "12"}, {}, asyncio.Queue()
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
        assert fresh.get_bot("persist")["bot"] == setting
        assert manager.load_platform.call_args.args[0] == setting
