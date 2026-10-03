import asyncio
import importlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pkg = "data.plugins.astrbot_plugin_telethon_ai"
policy = importlib.import_module(pkg + ".policy")
adapter = importlib.import_module(pkg + ".adapter")
main = importlib.import_module(pkg + ".main")


def config():
    return dict(
        adapter.DEFAULT,
        id="TelethonAI_collector",
        account="collector",
        disclosure_confirmed=True,
        allowed_chats=["-100123"],
        allowed_senders=["1000000001"],
        daily_limit=2,
        cooldown_seconds=10,
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("disclosure_confirmed", False),
        ("allowed_chats", []),
        ("allowed_chats", ["123"]),
        ("allowed_senders", []),
        ("allowed_senders", ["@name"]),
        ("cooldown_seconds", 0),
        ("cooldown_seconds", True),
    ],
)
def test_invalid_policy(key, value):
    c = config()
    c[key] = value
    with pytest.raises(ValueError):
        policy.validate(c)


def test_durable_gate(tmp_path):
    path = tmp_path / "gate.db"
    g = policy.Gate(path)
    assert g.admit("a", -1, 1, config(), now=100)
    assert not g.admit("a", -1, 1, config(), now=120)
    assert not g.admit("a", -1, 2, config(), now=105)
    assert g.admit("a", -1, 2, config(), now=120)
    assert g.admit("a", -1, 3, config(), now=150)
    assert g.admit("b", -1, 3, config(), now=150)
    g.pause("a")
    g.close()
    g = policy.Gate(path)
    assert g.paused("a")
    assert not g.admit("a", -1, 4, config(), now=86401)
    g.pause("a", False)
    assert g.admit("a", -1, 4, config(), now=86401)
    g.close()


def test_legacy_daily_cap_does_not_limit_account_but_cooldown_crosses_utc_day(tmp_path):
    g = policy.Gate(tmp_path / "gate.db")
    try:
        cfg = config()
        cfg["daily_limit"] = 1
        policy.validate(cfg)
        for message in range(105):
            assert g.admit("a", -1, message, cfg, now=100 + message * 10)
        assert g.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 105
        assert g.admit("a", -1, 200, cfg, now=86399)
        assert not g.admit("a", -1, 201, cfg, now=86400)
        assert g.admit("a", -1, 201, cfg, now=86409)
        cfg.pop("daily_limit")
        policy.validate(cfg)
        assert g.admit("a", -1, 202, cfg, now=86419)
    finally:
        g.close()


def test_tenant_authorization_still_limits_account_without_daily_cap(tmp_path):
    from data.plugins.astrbot_plugin_telethon_ai.tenants import Denied, Tenants

    store = Tenants(tmp_path / "tenants.db")
    g = policy.Gate(tmp_path / "gate.db")
    try:
        tenant = store.create_trial("admin", "1", "22", "AIClient_a", budget=2)
        other = store.create_trial("admin", "2", "33", "AIClient_b", budget=3)
        for tid, alias, chat in [(tenant, "a", "-1001"), (other, "b", "-1002")]:
            store.assign("admin", tid, alias)
            store.authorize_group("admin", tid, chat)
            store.set_enabled("admin", tid, True)
        cfg = config()
        cfg["daily_limit"] = 1
        for mid in range(2):
            assert g.admit("a", "-1001", mid, cfg, now=100 + mid * 10)
            reservation, _ = store.reserve("a", "-1001", mid)
            store.finish(reservation, "sent")
        assert g.admit("a", "-1001", 3, cfg, now=130)
        with pytest.raises(Denied, match="Quota"):
            store.reserve("a", "-1001", 3)
        with pytest.raises(Denied, match="entitlement"):
            store.reserve("a", "-1002", 4)
        reservation, bound = store.reserve("b", "-1002", 4)
        assert bound == other
        assert store.can_send(reservation)
        store.set_enabled("admin", other, False)
        assert not store.can_send(reservation)
        store.finish(reservation, "released")
        with pytest.raises(Denied, match="entitlement"):
            store.reserve("b", "-1002", 5)
        with store.db:
            store.db.execute("UPDATE tenants SET expires=0 WHERE id=?", (tenant,))
        with pytest.raises(Denied, match="entitlement"):
            store.reserve("a", "-1001", 5)
    finally:
        g.close()
        store.close()


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.Metric.upload", AsyncMock()
    )
    monkeypatch.setattr(adapter, "state_path", lambda: tmp_path / "gate.db")
    monkeypatch.setattr(
        adapter,
        "CONTEXT",
        SimpleNamespace(
            get_config=lambda umo: {
                "admins_id": [],
                "disable_builtin_commands": True,
                "plugin_set": [adapter.NAME],
                "provider_settings": {"enable": True},
                "agent_runner": {"runner_type": "local"},
                "kb_names": [],
            }
        ),
    )
    a = adapter.TelethonAdapter(config(), {}, asyncio.Queue())
    monkeypatch.setattr(
        adapter,
        "PIPELINE",
        SimpleNamespace(
            dispatch=AsyncMock(side_effect=lambda event: a.commit_event(event))
        ),
    )
    a.self_id = adapter.ACCOUNTS["collector"]
    a.username = "AI_account"
    a.stopping = False
    a.started = 0
    a.client = SimpleNamespace(send_message=AsyncMock(), disconnect=AsyncMock())
    tenant = a.tenants.create_trial("admin", "1000000001", "12345", "AIClient_test")
    a.tenants.assign("admin", tenant, "collector")
    a.tenants.authorize_group("admin", tenant, "-100123")
    a.tenants.set_enabled("admin", tenant, True)
    yield a
    a.gate.close()
    a.tenants.close()


def incoming(**changes):
    raw = SimpleNamespace(
        out=False,
        fwd_from=None,
        media=None,
        raw_text="@AI_account hello",
        date=datetime.now(timezone.utc),
        id=12,
        is_reply=False,
    )
    msg = SimpleNamespace(
        message=raw,
        is_group=True,
        sender_id=1000000001,
        chat_id=-100123,
        get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)),
        get_input_chat=AsyncMock(return_value="bound-peer"),
    )
    for k, v in changes.items():
        setattr(msg, k, v)
    return msg


@pytest.mark.asyncio
async def test_admitted_event_and_bound_single_reply(transport):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    await transport.receive(incoming())
    event = transport._event_queue.get_nowait()
    assert event.unified_msg_origin.startswith("TelethonAI_collector:GroupMessage:")
    assert event.message_obj.raw_message is None
    chain = MessageChain([Plain(text="hello")])
    await event.send(chain)
    await event.send(chain)
    transport.client.send_message.assert_awaited_once_with(
        "bound-peer",
        "[AI] hello",
        reply_to=12,
        parse_mode=None,
        link_preview=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"sender_id": 1000000002},
        {"sender_id": 1000000003},
        {"sender_id": 111},
        {"chat_id": -999},
        {"is_group": False},
    ],
)
async def test_inbound_denied(transport, changes):
    await transport.receive(incoming(**changes))
    assert transport._event_queue.empty()


@pytest.mark.asyncio
async def test_not_mentioned_and_stale(transport):
    msg = incoming()
    msg.message.raw_text = "hello"
    await transport.receive(msg)
    transport.started = datetime.now(timezone.utc).timestamp() + 100
    await transport.receive(incoming())
    assert transport._event_queue.empty()


@pytest.mark.asyncio
async def test_pause_rechecked_after_generation(transport):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    await transport.receive(incoming())
    event = transport._event_queue.get_nowait()
    transport.gate.pause("collector")
    await event.send(MessageChain([Plain(text="late reply")]))
    transport.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_proactive_transport(transport):
    with pytest.raises(PermissionError):
        await transport.send_by_session(None, None)


@pytest.mark.asyncio
async def test_send_error_pauses_and_does_not_retry(transport):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    transport.client.send_message.side_effect = OSError("secret-like internal error")
    await transport.receive(incoming())
    event = transport._event_queue.get_nowait()
    with pytest.raises(RuntimeError, match="gateway paused"):
        await event.send(MessageChain([Plain(text="hi")]))
    assert transport.gate.paused("collector")
    assert transport.client.send_message.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_send_retains_uncertain_charge_and_pauses(transport):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    transport.client.send_message.side_effect = asyncio.CancelledError()
    await transport.receive(incoming())
    event = transport._event_queue.get_nowait()
    with pytest.raises(asyncio.CancelledError):
        await event.send(MessageChain([Plain(text="hi")]))
    assert transport.gate.paused("collector")
    assert transport.client.send_message.await_count == 1
    assert (
        transport.tenants.db.execute(
            "SELECT state FROM reservations WHERE id=?", (event.reservation,)
        ).fetchone()[0]
        == "uncertain"
    )


def test_admin_is_private_and_platform_bound():
    plugin = object.__new__(main.TelethonAI)
    plugin.closed = False
    plugin.config = {"control_platform_id": "VIP_DHBot", "admin_ids": ["1000000001"]}
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        get_platform_id=lambda: "VIP_DHBot",
        get_sender_id=lambda: "1000000001",
    )
    assert plugin.is_admin(event)
    event.get_sender_id = lambda: "123"
    assert not plugin.is_admin(event)
    event.get_sender_id = lambda: "1000000001"
    event.is_private_chat = lambda: False
    assert not plugin.is_admin(event)


def test_customer_service_preserves_native_prompt():
    import ast

    path = Path(
        "/var/lib/astrbot-tenants/8852060111/data/plugins/astrbot_plugin_superbot/main.py"
    )
    tree = ast.parse(path.read_text())
    guard = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat_guard"
    )
    branches = [
        n
        for n in guard.body
        if isinstance(n, ast.If) and "chat_only_tenant" in ast.unparse(n.test)
    ]
    assert len(branches) == 1
    assert len(branches[0].body) == 1 and isinstance(branches[0].body[0], ast.Return)


@pytest.mark.asyncio
async def test_two_session_leases_share_collector_guard(tmp_path, monkeypatch):
    import telethon
    from filelock import FileLock, Timeout

    monkeypatch.setattr(adapter, "state_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(
        adapter,
        "credentials",
        lambda account: (
            {"session": str(tmp_path / account), "api_id": 1, "api_hash": "test"},
            None,
            tmp_path,
        ),
    )
    clients = []

    class Client:
        def __init__(self, session, *args, **kwargs):
            self.account = Path(session).name
            self.done = asyncio.Event()
            self.ready = asyncio.Event()
            clients.append(self)

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=adapter.ACCOUNTS[self.account], username="test")

        def add_event_handler(self, *args):
            self.ready.set()

        async def run_until_disconnected(self):
            await self.done.wait()

        async def disconnect(self):
            self.done.set()

    monkeypatch.setattr(telethon, "TelegramClient", Client)
    a = adapter.TelethonAdapter(config(), {}, asyncio.Queue())
    b = adapter.TelethonAdapter(dict(config(), account="keywords"), {}, asyncio.Queue())
    tasks = [asyncio.create_task(a.run()), asyncio.create_task(b.run())]
    try:
        for _ in range(100):
            if len(clients) == 2 and all(c.ready.is_set() for c in clients):
                break
            await asyncio.sleep(0.01)
        assert len(clients) == 2 and all(c.ready.is_set() for c in clients)
        assert all(not t.done() for t in tasks)
        for name in (
            "collector.lock",
            "collector.session.unified.lock",
            "keywords.session.unified.lock",
        ):
            with pytest.raises(Timeout):
                with FileLock(str(tmp_path / name), timeout=0):
                    pass
    finally:
        await a.terminate()
        await b.terminate()
        await asyncio.gather(*tasks)
        a.gate.close()
        b.gate.close()
        a.tenants.close()
        b.tenants.close()
    with FileLock(str(tmp_path / "collector.lock"), timeout=0):
        pass
