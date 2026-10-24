"""Native-admitted warm-group tests must never borrow production AI authority."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import group_ai, pc28, test_group_ai
from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import (
    DEFAULT as PROFILE,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.test_group_ai import (
    GROUP,
    RECEIVER,
    SENDER,
    control_group_ai,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.test_window import (
    TestWindow as Window,
)
from tests.test_wangshangliao_ai_plans import setup_billing
from tests.test_wangshangliao_group_ai import fake_http, tenant
from tests.test_wangshangliao_integrated_service import service

OP = "WSL_TEST_WARM_" + "a" * 32


async def control(value, action):
    return await control_group_ai(
        value, "dashboard/test", {"operation_id": OP, "action": action}
    )


@pytest_asyncio.fixture
async def scope(tmp_path):
    value = service(tmp_path)
    value.model = {"provider_id": "existing/model"}
    setup_billing(value)
    tenant(value.store, account=RECEIVER, group=GROUP, managers=[SENDER])
    value.profiles.save(
        "group", "", "", 0, {**PROFILE, "history_turns": 2}, "dashboard/test"
    )
    value.store.confirm_budget(
        value.budget["version"],
        True,
        "existing/model",
        "offline supplier cap",
        "dashboard/test",
        binding=value.store.model_binding(value.resolve_model()),
    )
    peers = {RECEIVER: "1698050908", SENDER: "1938341073", "23691273": "1003"}
    roles = {
        RECEIVER: "GROUP_ROLE_ADMIN",
        SENDER: "GROUP_ROLE_OWNER",
        "23691273": "GROUP_ROLE_MEMBER",
    }
    rows = [
        {
            "userId": uid,
            "nimId": peer,
            "groupRole": roles[uid],
            "accountState": "ACCOUNT_STATE_GOOD",
        }
        for uid, peer in peers.items()
    ]

    class Adapter:
        def __init__(self, uid):
            self.account, self.nim_account = uid, peers[uid]
            self.connection_state = "online"
            self.stopping = asyncio.Event()
            self.groups = {GROUP: "100" + GROUP}
            self.enabled_groups = [GROUP] if uid == RECEIVER else []
            self.nim = SimpleNamespace(request=AsyncMock())
            self.business = SimpleNamespace()
            self.send_lock = asyncio.Lock()
            self.send_text = AsyncMock(return_value="accepted")
            self.get_moderation_members = AsyncMock(
                side_effect=lambda _: {
                    "complete": True,
                    "groupMemberInfo": copy.deepcopy(rows),
                }
            )
            self.test_window = None
            self.dispatch_wakeup = asyncio.Event()

        @property
        def config(self):
            return value.policy_values(
                self.account,
                {
                    "id": "window-" + self.account,
                    "_business_active": True,
                    "_business_epoch": 1,
                    "enabled_groups": self.enabled_groups,
                },
            )

        def meta(self):
            return SimpleNamespace(id="window-" + self.account, name="wangshangliao")

    receiver, sender = Adapter(RECEIVER), Adapter(SENDER)
    receiver.ledger = Ledger(tmp_path / "receiver.sqlite3")
    sender.ledger = Ledger(tmp_path / "sender.sqlite3")
    await receiver.ledger.open()
    await sender.ledger.open()
    value.context.platform_manager.platform_insts = [receiver, sender]
    # The generic test service selects adapters through their platform metadata.
    value.adapters = lambda: [receiver, sender]
    await value.refresh()
    receiver.test_window = Window(receiver, sender, [GROUP], ["group_ai"], [], 300, 3)
    try:
        yield value, receiver, sender, rows
    finally:
        await control(value, "close")
        await receiver.ledger.close()
        await sender.ledger.close()


async def inbound(scope, *, mid="123", text="今天下班想喝柠檬茶", mentions=True):
    _, receiver, sender, _ = scope
    payload = {
        "sender": SENDER,
        "text": text,
        "mentions": [receiver.nim_account] if mentions else [],
        "recall_route": {"peer": sender.nim_account},
    }
    assert await receiver.ledger.ingest(RECEIVER, GROUP, mid, payload)
    scope_name = receiver.test_window.admit(GROUP, mid, payload)
    extras = {"wsl_test_scope": scope_name, "wangshangliao_payload": payload}
    return SimpleNamespace(
        platform=receiver,
        actor_identity=ActorIdentity(RECEIVER, SENDER, sender.nim_account, SENDER),
        get_sender_id=lambda: SENDER,
        get_group_id=lambda: GROUP,
        is_private_chat=lambda: False,
        message_str=text,
        message_obj=SimpleNamespace(message_id=mid),
        get_extra=lambda key: extras.get(key),
    )


@pytest.mark.asyncio
async def test_real_executor_bills_only_isolated_store_and_keeps_test_history(
    scope, monkeypatch
):
    value, receiver, _, _ = scope
    production_budget = dict(value.budget)
    production_tenants = value.store.tenants()
    await control(value, "open")
    fixture = value.group_ai.test_fixture
    assert fixture.pc28 is value.pc28
    assert fixture.group_ai.ready(
        fixture.adapter, fixture.group_ai.configs[(RECEIVER, GROUP)]
    )

    async def send(*args, authorize, **kwargs):
        await authorize()
        return "accepted"

    receiver.send_text.side_effect = send
    requests = fake_http(monkeypatch, content="柠檬茶配小饼干很合适。")
    event = await inbound(scope)
    await value.group_ai.observe(event)
    await asyncio.gather(*fixture.group_ai.tasks.values())
    assert len(requests) == 1
    receiver.send_text.assert_awaited_once()
    assert receiver.send_text.call_args.kwargs["test_mid"] == "123"
    with value.store.connect() as db:
        assert db.execute("SELECT count(*) FROM ai_charges").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM ai_payers").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM group_ai").fetchone()[0] == 0
    with fixture.store.connect() as db:
        charges = db.execute("SELECT state,charged_micro FROM ai_charges").fetchall()
    assert len(charges) == 1 and charges[0]["state"] == "settled"
    assert charges[0]["charged_micro"] > 0
    assert (
        value.budget == production_budget
        and value.store.tenants() == production_tenants
    )
    assert value.chat_count == 0
    await value.group_ai.observe(event)
    assert not fixture.group_ai.tasks and len(requests) == 1


@pytest.mark.asyncio
async def test_tool_continuation_uses_isolated_billing_and_shared_query(
    scope, monkeypatch
):
    value, receiver, _, _ = scope
    await control(value, "open")
    fixture = value.group_ai.test_fixture
    value.pc28.query = AsyncMock(
        return_value={
            "query": "latest",
            "status": "ok",
            "reason": "",
            "source": pc28.SOURCE,
            "latest": {
                "nbr": 100,
                "time": "offline",
                "n1": 1,
                "n2": 2,
                "n3": 3,
                "sum": 6,
            },
        }
    )
    requests = []

    class Response:
        status = 200

        def __init__(self, number):
            self.number, self.content = number, self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def iter_chunked(self, size):
            message = (
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "offline-call",
                            "type": "function",
                            "function": {
                                "name": "pc28_analysis",
                                "arguments": json.dumps(
                                    {"query": "latest", "window": 20}
                                ),
                            },
                        }
                    ],
                }
                if self.number == 1
                else {"content": "历史开奖仅供参考，不保证下期结果。"}
            )
            yield json.dumps(
                {
                    "choices": [{"message": message, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                }
            ).encode()

    class HTTP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            return Response(len(requests))

    monkeypatch.setattr(group_ai.aiohttp, "ClientSession", HTTP)
    await value.group_ai.observe(await inbound(scope, text="加拿大28最新开奖"))
    await asyncio.gather(*fixture.group_ai.tasks.values())
    assert len(requests) == 2 and receiver.send_text.await_count == 1
    answer = receiver.send_text.call_args.args[2]
    assert answer.endswith(pc28.NOTICE)
    assert "pc28.world" not in answer
    assert "https://" not in answer
    assert any(
        message["role"] == "tool" and pc28.SOURCE in message["content"]
        for message in requests[1]["messages"]
    )
    value.pc28.query.assert_awaited_once()
    charges = (await control(value, "status"))["charges"]
    assert len(charges) == 2 and all(c["state"] == "settled" for c in charges)
    assert any(c["request"].endswith("/tool-answer") for c in charges)
    with value.store.connect() as db:
        assert db.execute("SELECT count(*) FROM ai_charges").fetchone()[0] == 0
    assert value.chat_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "consumed",
        "command",
        "identity",
        "evidence",
        "window",
        "profile",
        "role",
        "price",
    ],
)
async def test_rejection_never_calls_model_or_production_fallback(
    scope, monkeypatch, reason
):
    value, receiver, _, rows = scope
    await control(value, "open")
    fixture = value.group_ai.test_fixture
    event = await inbound(scope)
    requests = fake_http(monkeypatch)
    if reason == "identity":
        event.actor_identity = ActorIdentity(RECEIVER, SENDER, "9999", "")
    elif reason == "evidence":
        event.get_extra("wangshangliao_payload")["text"] = "forged"
    elif reason == "window":
        receiver.test_window.expires = 0
    elif reason == "profile":
        value.profiles.save(
            "group", "", "", 1, {**PROFILE, "history_turns": 3}, "dashboard/test"
        )
    elif reason == "role":
        rows[0]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif reason == "price":
        with value.store.connect(write=True) as db:
            db.execute("UPDATE ai_prices SET blocked='operator_review'")
    try:
        await value.group_ai.observe(
            event, "帮助" if reason == "command" else "", consumed=reason == "consumed"
        )
        await asyncio.gather(*fixture.group_ai.tasks.values())
    except OpenError:
        assert reason in {"evidence", "window", "profile", "role", "price"}
    assert not requests and not receiver.send_text.called
    assert value.chat_count == 0


@pytest.mark.asyncio
async def test_test_scope_without_fixture_never_reaches_production(scope):
    value, _, _, _ = scope
    value.group_ai.launch = AsyncMock()
    await value.group_ai.observe(await inbound(scope))
    value.group_ai.launch.assert_not_called()


@pytest.mark.asyncio
async def test_expired_during_model_call_cannot_send_late_result(scope, monkeypatch):
    value, receiver, _, _ = scope
    await control(value, "open")
    fixture = value.group_ai.test_fixture
    entered, resume = asyncio.Event(), asyncio.Event()
    requests = fake_http(monkeypatch, barrier=(entered, resume))
    await value.group_ai.observe(await inbound(scope))
    task = asyncio.gather(*fixture.group_ai.tasks.values())
    await asyncio.wait_for(entered.wait(), 3)
    assert value.chat_count == 2
    receiver.test_window.expires = 0
    resume.set()
    await task
    assert len(requests) == 1 and not receiver.send_text.called
    assert value.chat_count == 0


@pytest.mark.asyncio
async def test_second_turn_keeps_only_same_test_dialogue_and_is_billed_again(
    scope, monkeypatch
):
    value, receiver, _, _ = scope
    await control(value, "open")
    fixture = value.group_ai.test_fixture
    requests = fake_http(monkeypatch, content="柠檬茶。")
    await value.group_ai.observe(await inbound(scope))
    await asyncio.gather(*fixture.group_ai.tasks.values())
    # Move only the offline ledger's cooldown timestamps, never live policy.
    with fixture.store.connect(write=True) as db:
        db.execute("UPDATE group_ai SET last_attempt=0")
        db.execute("UPDATE ai_charges SET created=created-61")
    await fixture.group_ai.refresh()
    await value.group_ai.observe(await inbound(scope, mid="124", text="我刚才说喝什么"))
    await asyncio.gather(*fixture.group_ai.tasks.values())
    assert len(requests) == 2 and receiver.send_text.await_count == 2
    messages = requests[1][1]["json"]["messages"]
    assert any(
        m["role"] == "assistant" and m["content"] == "柠檬茶。" for m in messages
    )
    assert any(m["role"] == "user" and "柠檬茶" in m["content"] for m in messages[:-1])
    assert len((await control(value, "status"))["charges"]) == 2


@pytest.mark.asyncio
async def test_closed_window_preserves_unknown_block_across_reopen(scope):
    value, receiver, sender, _ = scope
    await control(value, "open")
    fixture = value.group_ai.test_fixture
    with fixture.store.connect(write=True) as db:
        db.execute("UPDATE group_ai SET blocked='model_result_unknown'")
    await control(value, "close")
    receiver.test_window = Window(receiver, sender, [GROUP], ["group_ai"], [], 300, 3)
    with pytest.raises(OpenError, match="test_group_ai_unknown"):
        await control(value, "open")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alteration",
    [
        "none",
        "peer",
        "text",
        "group",
        "window",
        "late",
        "sender_uid",
        "route",
        "session",
        "client",
        "kind",
        "adapter",
    ],
)
async def test_native_witness_never_enables_sender_group_or_accepts_other_replies(
    scope, alteration
):
    value, receiver, sender, _ = scope
    await control(value, "open")
    fixture = value.group_ai.test_fixture
    key, text = "group-ai/offline-witness", "柠檬茶配小饼干。"
    fixture.sources[key] = "source"
    receiver.test_window.accepted[(GROUP, "source")] = "group_ai"
    await fixture.adapter.send_text(GROUP, key, text, proactive=True)
    inner = test_group_ai._text_message(
        receiver.config["id"],
        RECEIVER,
        GROUP,
        key,
        text,
        None,
        False,
        int(time.time() * 1000),
    )
    outer = {0: "1", 1: fixture.team, 2: receiver.nim_account, 8: "100", 12: "555"}
    if alteration == "peer":
        outer[2] = "9999"
    elif alteration == "text":
        inner.content.data = "unrelated"
    elif alteration == "group":
        inner.target.id = 1516387
    elif alteration == "window":
        receiver.test_window.expires = 0
    elif alteration == "sender_uid":
        inner.sender.id = 23691273
    elif alteration == "route":
        outer[1] = "another-team"
    elif alteration == "session":
        inner.session = 1
    elif alteration == "client":
        inner.client_id = "another-client"
    elif alteration == "kind":
        outer[0] = "0"
    witness = receiver if alteration == "adapter" else sender
    assert fixture.observe_reply(witness, GROUP, outer, inner) == (
        alteration in {"none", "late"}
    )
    if alteration == "late":
        receiver.test_window.expires = 0
    await asyncio.gather(*fixture.witness_tasks)
    async with sender.ledger.db.execute("SELECT payload FROM inbox") as cursor:
        records = await cursor.fetchall()
    assert len(records) == (alteration == "none")
    assert not sender.enabled_groups
    if alteration == "none":
        assert fixture.observe_reply(sender, GROUP, outer, inner)
        assert not fixture.witness_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["1", "chat/dashboard/test", "dashboard/"])
async def test_chat_principal_cannot_create_test_authority(scope, actor):
    value, _, _, _ = scope
    with pytest.raises(OpenError, match="arguments"):
        await control_group_ai(value, actor, {"operation_id": OP, "action": "open"})


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "unknown"])
async def test_fixed_native_probe_is_bounded_idempotent_and_never_retried(
    scope, monkeypatch, outcome
):
    value, receiver, sender, _ = scope
    await control(value, "open")
    messages = []

    def seal(key, message, *args):
        messages.append(message)
        return "offline-cipher"

    async def request(service, command, body):
        assert (service, command) == (8, 2)
        if outcome == "unknown":
            raise TimeoutError()
        return (
            (200, test_group_ai.wire.properties([(12, b"54321")]))
            if outcome == "accepted"
            else (403, b"")
        )

    sender.business.deployment = SimpleNamespace(key=lambda _: "offline")
    sender.nim.request.side_effect = request
    monkeypatch.setattr(test_group_ai.wire, "seal_message", seal)
    if outcome == "unknown":
        with pytest.raises(TimeoutError):
            await control(value, "send_1")
    else:
        assert (await control(value, "send_1"))["status"] == outcome
    repeated = await control(value, "send_1")
    assert repeated["status"] == outcome and sender.nim.request.await_count == 1
    assert len(messages) == 1 and str(messages[0].target.id) == GROUP
    assert str(messages[0].mentions.people[0].uid) == receiver.nim_account
    assert messages[0].mentions.content.data.endswith(test_group_ai.QUESTIONS[0])
    assert receiver.test_window.outbound_remaining == 2
    with pytest.raises(OpenError, match="send_order"):
        await control(value, "send_3")


@pytest.mark.asyncio
async def test_fixed_tool_probe_can_run_alone_without_reopening_other_steps(
    scope, monkeypatch
):
    value, _, sender, _ = scope
    await control(value, "open")
    sender.business.deployment = SimpleNamespace(key=lambda _: "offline")
    sender.nim.request.return_value = (
        200,
        test_group_ai.wire.properties([(12, b"54321")]),
    )
    monkeypatch.setattr(test_group_ai.wire, "seal_message", lambda *args: "offline")
    result = await control(value, "send_3")
    assert result["status"] == "accepted"
    assert await control(value, "send_3") == result
    for action in ("send_1", "send_2"):
        with pytest.raises(OpenError, match="send_order"):
            await control(value, action)
    assert sender.nim.request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied", [1, 2])
async def test_shared_service_concurrency_cannot_be_bypassed(
    scope, monkeypatch, occupied
):
    value, receiver, _, _ = scope
    await control(value, "open")
    fixture = value.group_ai.test_fixture
    requests = fake_http(monkeypatch)
    value.chat_count = occupied
    await value.group_ai.observe(await inbound(scope))
    assert not fixture.group_ai.tasks and not requests and not receiver.send_text.called
    assert value.chat_count == occupied
    value.chat_count = 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alteration", ["scope", "group", "role", "principal", "budget"]
)
async def test_fixture_open_requires_fixed_real_window_and_native_roster(
    scope, alteration
):
    value, receiver, _, rows = scope
    if alteration == "scope":
        receiver.test_window.scopes = ["group_commands"]
    elif alteration == "group":
        receiver.test_window.groups = ["1516387"]
    elif alteration == "role":
        rows[0]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif alteration == "principal":
        rows[1]["nimId"] = "9999"
    else:
        receiver.test_window.remaining = 4
    with pytest.raises(OpenError):
        await control(value, "open")
    assert getattr(value.group_ai, "test_fixture", None) is None
