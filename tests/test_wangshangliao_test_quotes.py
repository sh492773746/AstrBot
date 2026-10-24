"""Fixed-account quote rehearsal through real native encryption and ingestion."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import test_probe
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao import nim, runtime_policy, wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.test_window import (
    TestWindow as NativeTestWindow,
)

pytestmark = pytest.mark.usefixtures("wsl_active_policy")
GROUP = test_probe.QuoteRehearsal.GROUP
ACCOUNTS = test_probe.QuoteRehearsal.ACCOUNTS
OPERATION = "WSL_TEST_QUOTES_0123456789abcdef"
REAL_SLEEP = asyncio.sleep


@pytest_asyncio.fixture
async def rehearsal(tmp_path, monkeypatch):
    """Bind three temporary real adapters, with decoded native transport simulation.

    Args:
        tmp_path: Disposable ledger root.
        monkeypatch: Scoped directory, scheduling and native-transport replacements.

    Yields:
        Exact controlled identities with no live account credentials or sockets.
    """
    adapters = {}
    members = {}
    sent = []
    monkeypatch.setattr(runtime_policy.runtime, "open_service", None)
    for index, uid in enumerate(ACCOUNTS):
        adapter = WangshangliaoAdapter(
            {
                "id": f"quote-{index}",
                "account_id": uid,
                "enabled_groups": [GROUP] if index == 1 else [],
            },
            {},
            asyncio.Queue(),
        )
        adapter.connection_state = "online"
        adapter.nim_account = str(index + 10)
        adapter.groups = {GROUP: "555"} if index == 1 else {}
        adapter.ledger = Ledger(tmp_path / f"{uid}.sqlite")
        await adapter.ledger.open()
        adapter.business = SimpleNamespace(
            deployment=SimpleNamespace(key=lambda _: bytes(32))
        )
        adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
        members[uid] = {
            "userId": uid,
            "nimId": adapter.nim_account,
            "groupRole": "GROUP_ROLE_OWNER" if index == 0 else "GROUP_ROLE_MEMBER",
            "accountState": "ACCOUNT_STATE_GOOD",
        }
        adapters[uid] = adapter
    for adapter in adapters.values():
        adapter.members = {GROUP: {uid: str(m["nimId"]) for uid, m in members.items()}}
    receiver = adapters[ACCOUNTS[1]]
    tenant = {
        "active": True,
        "present": True,
        "paused": False,
        "version": 1,
        "epoch": 1,
    }
    service = SimpleNamespace(
        integrated=True,
        active=True,
        settings={"paused": False},
        adapters=lambda: list(adapters.values()),
        tenants={(receiver.account, GROUP): copy.deepcopy(tenant)},
        suspended_groups=set(),
        membership_receiving={},
        store=SimpleNamespace(tenant=lambda account, group: copy.deepcopy(tenant)),
        policy_values=lambda account, values: values,
    )
    monkeypatch.setattr(runtime_policy.runtime, "open_service", service)
    receiver.test_window = NativeTestWindow(
        receiver,
        adapters[ACCOUNTS[2]],
        [GROUP],
        ["group_commands"],
        [],
        300,
        10,
    )
    monkeypatch.setattr(
        test_probe,
        "member_directory",
        AsyncMock(
            return_value={"complete": True, "groupMemberInfo": list(members.values())}
        ),
    )
    monkeypatch.setattr(test_probe.asyncio, "sleep", AsyncMock())
    fault = [""]

    def transport(author):
        async def request(channel, command, raw):
            assert (channel, command) == (8, 2)
            fields, end = wire.read_properties(raw)
            assert end == len(raw)
            inner = wire.open_message(bytes(32), fields[10].decode())
            sent.append((author, fields, inner))
            mid = str(1000 + len(sent))
            envelope = {key: value.decode() for key, value in fields.items()}
            envelope.update(
                {
                    2: adapters[author].nim_account,
                    7: str(int(time.time() * 1000)),
                    12: mid,
                }
            )
            if fault[0] == "reference" and len(sent) > 3:
                envelope[29] = "999"
            if fault[0] == "missing_reference" and len(sent) > 3:
                envelope = {key: value for key, value in envelope.items() if key < 29}
            if fault[0] == "wrong_reply" and len(sent) > 3:
                inner.reply.server_id = "999"
                envelope[10] = wire.seal_message(
                    bytes(32), inner, len(sent), 1, int(time.time())
                )
                envelope = {key: value for key, value in envelope.items() if key < 29}
            if fault[0] == "unknown":
                raise TimeoutError("simulated result unknown")
            for uid, observer in adapters.items():
                if uid != author:
                    await observer.ingest(dict(envelope))
            return 200, wire.properties(
                [(12, mid.encode()), (11, inner.client_id.encode())]
            )

        return request

    for uid, adapter in adapters.items():
        adapter.nim.request = AsyncMock(side_effect=transport(uid))
    try:
        yield SimpleNamespace(
            service=service,
            adapters=adapters,
            sent=sent,
            tenant=tenant,
            receiver=receiver,
            fault=fault,
        )
    finally:
        run = getattr(service, "quote_rehearsal", None)
        if run is not None:
            run.closed = True
        for adapter in adapters.values():
            await adapter.ledger.close()


async def call(rehearsal, step):
    return await test_probe.control_quotes(
        rehearsal.service,
        "dashboard/test",
        {"operation_id": OPERATION, "step": step},
    )


@pytest.mark.asyncio
async def test_all_six_quote_directions_are_received_by_both_other_connections(
    rehearsal,
):
    configs = {uid: copy.deepcopy(a.config) for uid, a in rehearsal.adapters.items()}
    for step in range(9):
        result = await call(rehearsal, step)
        assert result["status"] == "accepted"
        assert len(result["witnesses"]) == 2
        assert result["message"] == str(1001 + step)
    assert len(rehearsal.sent) == 9
    directions = set()
    for step, (author, fields, inner) in enumerate(rehearsal.sent):
        source = test_probe.QuoteRehearsal.PLAN[step][1]
        if source is None:
            assert not inner.HasField("reply")
        else:
            source_author, source_fields, source_inner = rehearsal.sent[source]
            directions.add((author, source_author))
            assert inner.reply.server_id == str(1001 + source)
            assert inner.reply.client_id == source_inner.client_id
            assert inner.reply.content.data == source_inner.content.data
            assert (
                fields[29]
                == str(rehearsal.adapters[source_author].nim_account).encode()
            )
            assert fields[32] == inner.reply.server_id.encode()
            assert fields[33] == source_fields[11]
        for uid, adapter in rehearsal.adapters.items():
            if uid == author:
                continue
            async with adapter.ledger.db.execute(
                "SELECT state,payload FROM inbox WHERE account=? AND message=?",
                (uid, str(1001 + step)),
            ) as cursor:
                state, data = await cursor.fetchone()
            assert state == "ignored_bot"
            assert json.loads(data)["_test_quotes"] == OPERATION
    assert directions == {(a, b) for a in ACCOUNTS for b in ACCOUNTS if a != b}
    assert await call(rehearsal, 8) == result
    assert len(rehearsal.sent) == 9
    assert rehearsal.receiver.test_window.outbound_remaining == 1
    assert configs == {uid: a.config for uid, a in rehearsal.adapters.items()}
    assert (await call(rehearsal, "close"))["status"] == "closed"
    assert rehearsal.service.quote_rehearsal is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation_id": OPERATION, "step": True},
        {"operation_id": OPERATION, "step": -1},
        {"operation_id": OPERATION, "step": 9},
        {"operation_id": OPERATION, "step": 0, "group": "1516387"},
        {"operation_id": OPERATION, "step": 0, "text": "arbitrary"},
        {"operation_id": OPERATION, "step": 0, "account": ACCOUNTS[0]},
        {"operation_id": "invalid", "step": 0},
    ],
)
async def test_no_arbitrary_target_sender_text_or_method_is_accepted(
    rehearsal, payload
):
    with pytest.raises(OpenError, match="test_quotes_arguments"):
        await test_probe.control_quotes(rehearsal.service, "dashboard/test", payload)
    assert not rehearsal.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["", "dashboard/", "chat/31846829"])
async def test_chat_identities_cannot_start_a_quote_rehearsal(rehearsal, actor):
    with pytest.raises(OpenError, match="test_quotes_arguments"):
        await test_probe.control_quotes(
            rehearsal.service, actor, {"operation_id": OPERATION, "step": 0}
        )
    assert not rehearsal.sent


@pytest.mark.asyncio
async def test_unknown_native_result_is_not_replayed_or_followed_by_another_step(
    rehearsal,
):
    rehearsal.fault[0] = "unknown"
    with pytest.raises(TimeoutError):
        await call(rehearsal, 0)
    assert (await call(rehearsal, 0))["status"] == "unknown"
    with pytest.raises(OpenError, match="test_quotes_order"):
        await call(rehearsal, 1)
    assert len(rehearsal.sent) == 1


@pytest.mark.asyncio
async def test_quote_fields_must_survive_the_actual_native_receive_chain(rehearsal):
    for step in range(3):
        await call(rehearsal, step)
    rehearsal.fault[0] = "reference"
    result = await call(rehearsal, 3)
    assert result["status"] == "witness_missing"
    assert len(result["mismatches"]) == 2
    assert all(
        item["reference_matches"]
        and item["appoint"] == 1
        and item["native_fields_mismatched"] == [29]
        and item["native_fields_present"] == list(range(29, 39))
        for item in result["mismatches"]
    )
    with pytest.raises(OpenError, match="test_quotes_order"):
        await call(rehearsal, 4)
    assert len(rehearsal.sent) == 4


@pytest.mark.asyncio
async def test_platform_may_omit_optional_transport_tags_but_application_quote_is_verified(
    rehearsal,
):
    rehearsal.fault[0] = "missing_reference"
    for step in range(9):
        result = await call(rehearsal, step)
        assert result["status"] == "accepted"
        assert len(result["witnesses"]) == 2
        if step >= 3:
            assert len(result["quote_evidence"]) == 2
            assert all(
                item["application_reference_verified"]
                and item["native_fields_present"] == []
                for item in result["quote_evidence"]
            )
    assert len(rehearsal.sent) == 9


@pytest.mark.asyncio
async def test_wrong_application_quote_is_rejected_even_without_transport_tags(
    rehearsal,
):
    for step in range(3):
        await call(rehearsal, step)
    rehearsal.fault[0] = "wrong_reply"
    result = await call(rehearsal, 3)
    assert result["status"] == "witness_missing"
    assert len(result["mismatches"]) == 2
    assert all(
        not item["reference_matches"]
        and item["appoint"] == 1
        and item["native_fields_mismatched"] == []
        and item["native_fields_present"] == []
        for item in result["mismatches"]
    )
    with pytest.raises(OpenError, match="test_quotes_order"):
        await call(rehearsal, 4)
    assert len(rehearsal.sent) == 4


@pytest.mark.asyncio
async def test_pause_while_waiting_for_send_lock_prevents_a_new_native_submission(
    rehearsal,
):
    await call(rehearsal, 0)
    sender = rehearsal.adapters[ACCOUNTS[1]]
    await sender.send_lock.acquire()
    task = asyncio.create_task(call(rehearsal, 1))
    try:
        for _ in range(100):
            await REAL_SLEEP(0)
            if sender.send_lock._waiters:
                break
        assert sender.send_lock._waiters
        rehearsal.service.settings["paused"] = True
    finally:
        sender.send_lock.release()
    with pytest.raises(OpenError, match="test_quotes_revoked"):
        await task
    assert len(rehearsal.sent) == 1


@pytest.mark.asyncio
async def test_closing_a_run_does_not_close_a_replacement_developer_window(rehearsal):
    await call(rehearsal, 0)
    original = rehearsal.receiver.test_window
    replacement = NativeTestWindow(
        rehearsal.receiver,
        rehearsal.adapters[ACCOUNTS[2]],
        [GROUP],
        ["group_commands"],
        [],
        300,
        10,
    )
    rehearsal.receiver.test_window = replacement
    await call(rehearsal, "close")
    assert rehearsal.receiver.test_window is replacement
    assert original is not replacement and replacement.active()


@pytest.mark.asyncio
async def test_another_dashboard_actor_cannot_replace_or_close_the_original_run(
    rehearsal,
):
    await call(rehearsal, 0)
    with pytest.raises(OpenError, match="test_quotes_busy"):
        await test_probe.control_quotes(
            rehearsal.service,
            "dashboard/other",
            {"operation_id": OPERATION, "step": "close"},
        )
    assert rehearsal.service.quote_rehearsal.actor == "dashboard/test"
    assert len(rehearsal.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["role", "account", "window"])
async def test_roster_and_original_connections_are_rechecked_after_directory_io(
    rehearsal, monkeypatch, fault
):
    original = test_probe.member_directory

    async def changed(*args):
        directory = copy.deepcopy(await original(*args))
        if fault == "role":
            directory["groupMemberInfo"][0]["groupRole"] = "GROUP_ROLE_MEMBER"
        elif fault == "account":
            rehearsal.adapters[ACCOUNTS[0]].nim_account = "999"
        else:
            rehearsal.receiver.test_window = None
        return directory

    monkeypatch.setattr(test_probe, "member_directory", changed)
    with pytest.raises(OpenError, match="test_quotes_roster"):
        await call(rehearsal, 0)
    assert not rehearsal.sent


@pytest.mark.asyncio
async def test_evidence_revocation_does_not_reconnect_or_publish_a_false_witness(
    rehearsal, monkeypatch
):
    await call(rehearsal, 0)
    sender = rehearsal.adapters[ACCOUNTS[1]]
    observer = rehearsal.adapters[ACCOUNTS[0]]
    original = observer.ledger.ingest

    async def revoked(*args, **kwargs):
        rehearsal.service.settings["paused"] = True
        return await original(*args, **kwargs)

    monkeypatch.setattr(observer.ledger, "ingest", revoked)
    with pytest.raises(OpenError, match="test_quotes_revoked"):
        await call(rehearsal, 1)
    run = rehearsal.service.quote_rehearsal
    assert (1, observer.account) not in run.seen
    assert sender.nim.request.await_count == 1
    assert observer.connection_state == "online"


def test_native_decoder_keeps_optional_reply_and_thread_properties():
    fields = [
        (0, b"1"),
        (1, b"555"),
        (2, b"10"),
        (7, b"1000"),
        (8, b"100"),
        (12, b"123"),
    ]
    fields.extend((tag, str(tag).encode()) for tag in range(29, 39))
    decoded, boundary = nim.messages((8, 3, 1, 200, wire.properties(fields)))
    assert boundary is None
    assert all(decoded[0][tag] == str(tag) for tag in range(29, 39))


@pytest.mark.parametrize("value", [b"\xff", b"x" * 129])
def test_malformed_optional_reference_does_not_drop_ordinary_messages(value):
    fields = [
        (0, b"1"),
        (1, b"555"),
        (2, b"10"),
        (7, b"1000"),
        (8, b"100"),
        (12, b"123"),
        (29, value),
    ]
    decoded, _ = nim.messages((8, 3, 1, 200, wire.properties(fields)))
    assert decoded[0][12] == "123" and 29 not in decoded[0]
