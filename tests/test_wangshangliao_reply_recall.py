"""Native self-recall timing, durable routing, scoping and once-only attempts."""

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.core.platform.sources.wangshangliao import reply_recall, wire
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch, wsl_active_policy):
    now = [1000.0]
    monkeypatch.setattr(reply_recall.time, "time", lambda: now[0])
    monkeypatch.setattr(wire, "seal_message", lambda *args: "sealed")
    adapter = WangshangliaoAdapter(
        {"id": "fixture", "account_id": "1", "enable": True, "enabled_groups": ["5"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.groups = {"5": "905"}
    adapter.members = {"5": {"1": "901", "2": "902"}}
    adapter.nim_account = "901"
    adapter.connection_state = "online"
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: b"k" * 32)
    )
    adapter.diagnostics = SimpleNamespace(emit=Mock())
    count = [122]

    async def request(service, command, body):
        if (service, command) == (7, 13):
            return 200, b""
        fields, _ = wire.read_properties(body)
        count[0] += 1
        return 200, wire.properties(
            [
                (7, str(int(now[0] * 1000)).encode()),
                (11, fields[11]),
                (12, str(count[0]).encode()),
            ]
        )

    adapter.nim = SimpleNamespace(request=AsyncMock(side_effect=request))
    yield adapter, now
    await adapter.ledger.close()


@pytest_asyncio.fixture
async def rule_adapter(setup, wsl_active_policy):
    adapter, _ = setup
    wsl_active_policy(
        {
            **adapter.config,
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "permissions": {"5": ["mute", "recall"]},
            },
        }
    )
    adapter.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {"userId": "1", "nimId": "901", "groupRole": "GROUP_ROLE_ADMIN"},
                {"userId": "2", "nimId": "902", "groupRole": "GROUP_ROLE_MEMBER"},
            ],
        }
    )
    await adapter.ledger.ingest(
        "1",
        "5",
        "123",
        {
            "sender": "2",
            "recall_route": {
                "client": "original-client",
                "time": "1000000",
                "peer": "902",
            },
        },
    )
    return adapter


@pytest_asyncio.fixture
async def command_event(rule_adapter):
    """Construct trusted sender identity through the real native dispatch lane."""
    adapter = rule_adapter
    adapter.diagnostics.observe = Mock()
    directory = await adapter.get_moderation_members("5")
    adapter.member_snapshots["5"] = ("1", time.monotonic(), directory)
    payload = {
        "sender": "2",
        "name": "Member",
        "text": "规则",
        "created_at": time.time() * 1000,
        "mentions": [],
        "recall_route": {
            "client": "original-client",
            "time": "1000",
            "peer": "902",
        },
    }
    await adapter.ledger.db.execute(
        "UPDATE inbox SET payload=? WHERE account='1' AND team='5' AND message='123'",
        (json.dumps(payload),),
    )
    await adapter.ledger.db.commit()
    work = asyncio.create_task(adapter.process_group("5", single=True))
    event = await asyncio.wait_for(adapter._event_queue.get(), 1)
    try:
        yield event
    finally:
        if not event.processing_completion.done():
            event.processing_completion.set_result(None)
        await asyncio.wait_for(work, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["recall", "warning"])
@pytest.mark.parametrize("wait", ["lock", "ledger"])
@pytest.mark.parametrize(
    "change",
    ["account", "group", "peer", "disabled", "removed", "grants", "automation", "nim"],
)
async def test_versioned_rule_wait_rejects_changed_scope(
    rule_adapter, wsl_active_policy, monkeypatch, action, wait, change
):
    adapter = rule_adapter
    nim = adapter.nim
    entered, release = asyncio.Event(), asyncio.Event()
    key = "recall/1/5/123" if action == "recall" else "rule-warning/123"
    intent = ActionRequest.bind(
        adapter,
        key,
        action,
        5,
        member=2,
        expected_nim="902",
        message="123" if action == "recall" else "",
        text="Warning",
    )
    if wait == "lock":
        await adapter.send_lock.acquire()
        acquire = adapter.send_lock.acquire

        async def observed_acquire():
            entered.set()
            return await acquire()

        monkeypatch.setattr(adapter.send_lock, "acquire", observed_acquire)
    else:
        reserve = adapter.ledger.reserve

        async def paused_reserve(*args):
            result = await reserve(*args)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(adapter.ledger, "reserve", paused_reserve)
    work = asyncio.create_task(adapter.execute_action(intent))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "account":
            adapter.account = "2"
        elif change == "group":
            adapter.groups["5"] = "906"
        elif change == "peer":
            adapter.members["5"]["2"] = "903"
        elif change == "nim":
            adapter.nim = SimpleNamespace(request=AsyncMock())
        elif change == "disabled":
            adapter._transport_config["enable"] = False
        else:
            config = adapter.config
            if change == "removed":
                config["enabled_groups"] = []
            elif change == "grants":
                config["moderation"]["permissions"] = {}
            else:
                config["moderation"]["automation_enabled"] = False
            wsl_active_policy(config)
        if wait == "lock":
            adapter.send_lock.release()
        release.set()
        with pytest.raises(wire.ProtocolError):
            await asyncio.wait_for(work, 1)
        nim.request.assert_not_awaited()
        assert await jobs(adapter) == []
        if wait == "ledger":
            async with adapter.ledger.db.execute(
                "SELECT state FROM outbox WHERE key=?", (key,)
            ) as cursor:
                assert await cursor.fetchone() == ("rejected",)
    finally:
        if adapter.send_lock.locked():
            adapter.send_lock.release()
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["recall", "warning"])
async def test_rule_success_receipt_keeps_original_account_and_warning_cleanup(
    rule_adapter, action
):
    adapter = rule_adapter
    native_request = adapter.nim.request.side_effect
    key = "recall/1/5/123" if action == "recall" else "rule-warning/123"
    intent = ActionRequest.bind(
        adapter,
        key,
        action,
        5,
        member=2,
        expected_nim="902",
        message="123" if action == "recall" else "",
        text="Warning",
    )

    async def request(*args):
        receipt = await native_request(*args)
        adapter.account = "2"
        adapter.nim_account = "902"
        return receipt

    adapter.nim.request.side_effect = request
    result = await adapter.execute_action(intent)
    assert result["status"] == "accepted" and result["account"] == "1"
    assert result["operation"] == key
    if action == "warning":
        async with adapter.ledger.db.execute(
            "SELECT account,peer FROM reply_recalls WHERE key=?", (key,)
        ) as cursor:
            assert await cursor.fetchone() == ("1", "901")
    else:
        assert await jobs(adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plain", "nickname", "login_only", "ids_only"])
async def test_rule_warning_recalls_only_accepted_sent_reply_twelve_seconds_later(
    rule_adapter, setup, monkeypatch, mode
):
    adapter = rule_adapter
    _, now = setup
    named = mode != "plain"
    key = "open-warning/1/5/777" if named else "content-warning/5/777"
    name = "Member\U0001f600"
    roster = await adapter.get_moderation_members("5")
    label = "2"
    opaque = "e15ef91a04b74a41b64a3fa6e17f3c8d"
    if mode == "nickname":
        roster["groupMemberInfo"][1]["userNick"] = name
    elif mode == "login_only":
        label = opaque
        roster["groupMemberInfo"][1]["userName"] = opaque
    elif mode == "ids_only":
        label = "902"
        roster["groupMemberInfo"][1].update(userNick="902", groupMemberNick="2")
    messages = []
    monkeypatch.setattr(
        wire,
        "seal_message",
        lambda _, message, *args: messages.append(message) or "sealed",
    )
    intent = ActionRequest.bind(
        adapter,
        key,
        "warning",
        5,
        member=2,
        expected_nim="902",
        mention_name=label if named else None,
        text=f"@{label} Warning" if named else "Warning",
    )
    native_request = adapter.nim.request.side_effect
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_receipt(*args):
        entered.set()
        await release.wait()
        return await native_request(*args)

    adapter.nim.request.side_effect = delayed_receipt
    work = asyncio.create_task(adapter.execute_action(intent))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await jobs(adapter) == []
        now[0] = 1003
        release.set()
        assert (await asyncio.wait_for(work, 1))["status"] == "accepted"
    finally:
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert await jobs(adapter) == [(key, "123", 1015, "pending")]
    if mode == "nickname":
        assert messages[0].mentions.content.data == f"@{name} Warning"
        person = messages[0].mentions.people[0]
        assert (person.uid, person.nick, person.start, person.end) == (
            902,
            name,
            0,
            len(f"@{name} ".encode("utf-16-le")) // 2,
        )
    else:
        assert messages[0].content.data == "Warning"
        assert not messages[0].mentions.people
        assert opaque not in messages[0].content.data
    assert (await adapter.execute_action(intent))["status"] == "accepted"
    assert adapter.nim.request.await_count == 1
    now[0] = 1014.99
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 1
    now[0] = 1015
    await asyncio.gather(
        reply_recall.recall_due(adapter), reply_recall.recall_due(adapter)
    )
    call = adapter.nim.request.await_args.args
    assert call[:2] == (7, 13)
    fields, _ = wire.read_properties(call[2])
    assert fields == {
        0: b"1003000",
        1: b"8",
        2: b"905",
        3: b"901",
        10: str(uuid.uuid5(uuid.NAMESPACE_URL, f"fixture/{key}")).encode(),
        11: b"123",
        16: b"901",
    }
    assert await jobs(adapter) == [(key, "123", 1015, "accepted")]
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "malformed", "wrong_client"])
async def test_rule_warning_requires_accepted_matching_receipt_before_cleanup(
    rule_adapter, outcome
):
    adapter = rule_adapter

    async def receipt(*args):
        if outcome == "rejected":
            return 403, b""
        return 200, wire.properties(
            [(7, b"1000000"), (12, b"" if outcome == "malformed" else b"123")]
            + ([(11, b"another-client")] if outcome == "wrong_client" else [])
        )

    adapter.nim.request.side_effect = receipt
    intent = ActionRequest.bind(
        adapter,
        "open-warning/1/5/777",
        "warning",
        5,
        member=2,
        expected_nim="902",
        text="Warning",
    )
    if outcome == "rejected":
        assert (await adapter.execute_action(intent))["status"] == "rejected"
    else:
        with pytest.raises(wire.ProtocolError):
            await adapter.execute_action(intent)
    assert await jobs(adapter) == []
    assert (await adapter.execute_action(intent))["status"] == (
        "rejected" if outcome == "rejected" else "unknown"
    )
    adapter.nim.request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "unknown"])
async def test_legacy_warning_is_not_resent_or_retroactively_scheduled(
    rule_adapter, status
):
    adapter = rule_adapter
    key = "open-warning/1/5/777"
    if status == "unknown":
        adapter.nim.request.side_effect = TimeoutError()
        with pytest.raises(TimeoutError):
            await adapter.send_text("5", key, "Warning", auto_recall=False)
    else:
        assert await adapter.send_text("5", key, "Warning", auto_recall=False) == status
    intent = ActionRequest.bind(
        adapter,
        key,
        "warning",
        5,
        member=2,
        expected_nim="902",
        text="Warning",
    )
    with pytest.raises(wire.ProtocolError, match="send_key_conflict"):
        await adapter.execute_action(intent)
    assert (await adapter.ledger.receipt(key))["status"] == status
    assert await jobs(adapter) == []
    adapter.nim.request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["recall", "warning"])
@pytest.mark.parametrize("outcome", [TimeoutError(), asyncio.CancelledError()])
async def test_unknown_rule_submission_never_replays(rule_adapter, action, outcome):
    adapter = rule_adapter
    key = "recall/1/5/123" if action == "recall" else "rule-warning/123"
    intent = ActionRequest.bind(
        adapter,
        key,
        action,
        5,
        member=2,
        expected_nim="902",
        message="123" if action == "recall" else "",
        text="Warning",
    )
    adapter.nim.request.side_effect = outcome
    with pytest.raises(type(outcome)):
        await adapter.execute_action(intent)
    result = await adapter.execute_action(intent)
    assert result["status"] == "unknown"
    adapter.nim.request.assert_awaited_once()
    assert await jobs(adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "group", "nim", "business", "ledger", "own_peer"]
)
async def test_segmented_reply_never_moves_remaining_text_to_new_scope(setup, change):
    adapter, _ = setup
    ledger, nim, business = adapter.ledger, adapter.nim, adapter.business
    native_request = nim.request.side_effect

    async def request(*args):
        receipt = await native_request(*args)
        if change == "account":
            adapter.account = "2"
        elif change == "group":
            adapter.groups["5"] = "906"
        elif change == "nim":
            adapter.nim = SimpleNamespace(request=AsyncMock())
        elif change == "business":
            adapter.business = object()
        elif change == "ledger":
            adapter.ledger = object()
        else:
            adapter.members["5"]["1"] = "903"
        return receipt

    nim.request.side_effect = request
    try:
        with pytest.raises(wire.ProtocolError):
            await adapter.send_reply_text(
                "5", "segmented", "a" * 5000, auto_recall=True
            )
        nim.request.assert_awaited_once()
        assert (await ledger.receipt("segmented"))["status"] == "unknown"
        assert (await ledger.receipt("segmented/part/0"))["status"] == "accepted"
        assert await ledger.receipt("segmented/part/4096") is None
        if change == "account":
            adapter.account = "1"
        adapter.groups["5"] = "905"
        adapter.members["5"]["1"] = "901"
        adapter.ledger, adapter.nim, adapter.business = ledger, nim, business
        assert (
            await adapter.send_reply_text(
                "5", "segmented", "a" * 5000, auto_recall=True
            )
            == "unknown"
        )
        nim.request.assert_awaited_once()
    finally:
        adapter.ledger, adapter.nim, adapter.business = ledger, nim, business


async def jobs(adapter):
    async with adapter.ledger.db.execute(
        "SELECT key,server_id,deadline,state FROM reply_recalls ORDER BY key"
    ) as cursor:
        return await cursor.fetchall()


@pytest.mark.asyncio
async def test_exact_twelve_seconds_and_own_native_route(setup):
    adapter, now = setup
    assert (
        await adapter.send_text("5", "reply", "Ranking", auto_recall=True) == "accepted"
    )
    assert await jobs(adapter) == [("reply", "123", 1012.0, "pending")]
    now[0] = 1011.99
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 1
    now[0] = 1012.0
    await asyncio.gather(
        reply_recall.recall_due(adapter), reply_recall.recall_due(adapter)
    )
    assert adapter.nim.request.await_count == 2
    call = adapter.nim.request.await_args.args
    assert call[:2] == (7, 13)
    fields, _ = wire.read_properties(call[2])
    assert fields == {
        0: b"1000000",
        1: b"8",
        2: b"905",
        3: b"901",
        10: str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture/reply")).encode(),
        11: b"123",
        16: b"901",
    }
    assert (await jobs(adapter))[0][3] == "accepted"
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 2


@pytest.mark.asyncio
async def test_segments_each_have_one_recall_and_repeat_does_not_reschedule(setup):
    adapter, now = setup
    text = "a" * 5000
    await adapter.send_reply_text("5", "reply", text, auto_recall=True)
    assert len(await jobs(adapter)) == 2
    await adapter.send_reply_text("5", "reply", text, auto_recall=True)
    assert len(await jobs(adapter)) == 2 and adapter.nim.request.await_count == 2
    now[0] = 1012
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 4
    assert all(row[3] == "accepted" for row in await jobs(adapter))


@pytest.mark.asyncio
async def test_private_and_unflagged_chat_do_not_schedule(setup):
    adapter, _ = setup
    await adapter.send_text("5", "chat", "Chat")
    session = "private/2/" + wire.b64(b"902")
    await adapter.ledger.ingest("1", session, "m", {"sender": "2"})
    await adapter.send_reply_text(session, "private", "Private", auto_recall=True)
    assert await jobs(adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [403, TimeoutError()])
async def test_failed_or_unknown_send_never_schedules(setup, outcome):
    adapter, _ = setup
    if isinstance(outcome, Exception):
        adapter.nim.request.side_effect = outcome
        with pytest.raises(TimeoutError):
            await adapter.send_text("5", "reply", "Ranking", auto_recall=True)
    else:
        adapter.nim.request.side_effect = None
        adapter.nim.request.return_value = outcome, b""
        assert (
            await adapter.send_text("5", "reply", "Ranking", auto_recall=True)
            == "rejected"
        )
    assert await jobs(adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected",
    [
        (403, "rejected"),
        (TimeoutError(), "unknown"),
        (asyncio.CancelledError(), "unknown"),
    ],
)
async def test_failed_or_unknown_recall_is_never_retried(setup, outcome, expected):
    adapter, now = setup
    await adapter.send_text("5", "reply", "Ranking", auto_recall=True)
    now[0] = 1012
    if isinstance(outcome, BaseException):
        adapter.nim.request.side_effect = outcome
    else:
        adapter.nim.request.side_effect = None
        adapter.nim.request.return_value = outcome, b""
    if isinstance(outcome, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await reply_recall.recall_due(adapter)
    else:
        await reply_recall.recall_due(adapter)
    assert (await jobs(adapter))[0][3] == expected
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "account",
        "group",
        "peer",
        "disabled",
        "removed",
        "client",
        "receipt",
        "expired",
    ],
)
async def test_changed_or_invalid_scope_cannot_recall(setup, change, wsl_active_policy):
    adapter, now = setup
    await adapter.send_text("5", "reply", "Ranking", auto_recall=True)
    now[0] = 1012
    if change == "account":
        adapter.account = "2"
    elif change == "group":
        adapter.groups["5"] = "906"
    elif change == "peer":
        adapter.nim_account = "902"
    elif change == "disabled":
        adapter._transport_config["enable"] = False
    elif change == "removed":
        wsl_active_policy({**adapter.config, "enabled_groups": []})
    elif change == "client":
        await adapter.ledger.db.execute("UPDATE reply_recalls SET client='other'")
        await adapter.ledger.db.commit()
    elif change == "receipt":
        await adapter.ledger.finish("reply", "accepted", "999")
    elif change == "expired":
        now[0] = 1313
    await reply_recall.recall_due(adapter)
    assert (await jobs(adapter))[0][3] == "rejected"
    assert adapter.nim.request.await_count == 1


@pytest.mark.asyncio
async def test_restart_restores_pending_but_quarantines_uncertain_attempt(setup):
    adapter, now = setup
    await adapter.send_text("5", "pending", "Ranking", auto_recall=True)
    await adapter.send_text("5", "uncertain", "Result", auto_recall=True)
    await adapter.ledger.db.execute(
        "UPDATE reply_recalls SET state='sending' WHERE key='uncertain'"
    )
    await adapter.ledger.db.commit()
    await adapter.ledger.close()
    await adapter.ledger.open()
    now[0] = 1012
    adapter.connection_state = "reconnecting"
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 2
    adapter.connection_state = "online"
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 3
    assert {row[0]: row[3] for row in await jobs(adapter)} == {
        "pending": "accepted",
        "uncertain": "unknown",
    }


@pytest.mark.asyncio
async def test_worker_stops_without_leaving_a_timer(setup):
    adapter, _ = setup
    task = asyncio.create_task(reply_recall.run(adapter))
    await asyncio.sleep(0)
    adapter.stopping.set()
    await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_idle_worker_does_not_poll_an_empty_ledger(setup):
    adapter, _ = setup
    queries = []
    await adapter.ledger.db.set_trace_callback(
        lambda sql: (
            queries.append(sql)
            if sql.startswith("SELECT") and "reply_recalls" in sql
            else None
        )
    )
    task = asyncio.create_task(reply_recall.run(adapter))
    try:
        await asyncio.sleep(1.2)
        assert len(queries) == 1
        assert not adapter.nim.request.called
    finally:
        adapter.stopping.set()
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_committed_recall_wakes_idle_worker(setup):
    adapter, now = setup
    queried = asyncio.Event()
    loop = asyncio.get_running_loop()
    await adapter.ledger.db.set_trace_callback(
        lambda sql: (
            loop.call_soon_threadsafe(queried.set)
            if sql.startswith("SELECT") and "reply_recalls" in sql
            else None
        )
    )
    task = asyncio.create_task(reply_recall.run(adapter))
    try:
        await asyncio.wait_for(queried.wait(), 1)
        await adapter.send_text("5", "new-recall", "Ranking", auto_recall=True)
        now[0] = 1012
        for _ in range(50):
            if (await jobs(adapter))[0][3] == "accepted":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("A committed recall did not wake its idle worker")
        assert adapter.nim.request.await_count == 2
    finally:
        adapter.stopping.set()
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_recall_notification_waits_for_commit(setup, monkeypatch):
    adapter, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()
    commit = adapter.ledger.db.commit

    async def paused_commit():
        entered.set()
        await release.wait()
        await commit()

    monkeypatch.setattr(adapter.ledger.db, "commit", paused_commit)
    work = asyncio.create_task(
        adapter.ledger.finish(
            "fixture",
            "accepted",
            "123",
            recall=("1", "5", "905", "901", "client", "1000000", 1020),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not adapter.ledger.recall_wakeup.is_set()
        release.set()
        await asyncio.wait_for(work, 1)
        assert adapter.ledger.recall_wakeup.is_set()
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_plain_or_failed_send_does_not_wake_recall_worker(setup):
    adapter, _ = setup
    await adapter.ledger.finish("plain", "accepted", "123")
    assert not adapter.ledger.recall_wakeup.is_set()
    await adapter.ledger.finish(
        "unknown",
        "unknown",
        recall=("1", "5", "905", "901", "client", "1000000", 1020),
    )
    assert not adapter.ledger.recall_wakeup.is_set()


@pytest.mark.asyncio
async def test_cancelled_idle_worker_leaves_no_waiters(setup):
    adapter, _ = setup
    original = asyncio.all_tasks()
    task = asyncio.create_task(reply_recall.run(adapter))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not (asyncio.all_tasks() - original)


@pytest.mark.asyncio
async def test_real_dispatch_classifies_and_recalls_both_command_and_reply(
    command_event, setup, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation.main import Main

    adapter, now = setup
    plugin = object.__new__(Main)
    plugin.commands = SimpleNamespace(run=AsyncMock(return_value="Command result"))
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await plugin.moderate(command_event)
    assert command_event.actor_identity.verified_uid == "2"
    assert command_event.get_extra("wsl_plain_command") is True
    pending = await jobs(adapter)
    assert len(pending) == 2
    assert all(row[2:] == (1012.0, "pending") for row in pending)
    now[0] = 1011.99
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 1
    now[0] = 1012
    await asyncio.gather(
        reply_recall.recall_due(adapter), reply_recall.recall_due(adapter)
    )
    assert adapter.nim.request.await_count == 3
    assert all(row[3] == "accepted" for row in await jobs(adapter))
    assert (await adapter.ledger.receipt("recall/1/5/123"))["status"] == "accepted"
    original = [
        wire.read_properties(call.args[2])[0]
        for call in adapter.nim.request.await_args_list
        if call.args[:2] == (7, 13)
    ]
    assert {route[3] for route in original} == {b"901", b"902"}
    assert (
        next(route for route in original if route[3] == b"902")[10]
        == b"original-client"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("keep", [False, True])
async def test_all_command_replies_including_followups_ignore_keep_marker(
    monkeypatch, private, keep
):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
    from tests.test_wangshangliao_plain_commands import native_event

    event = native_event("菜单", private=private)
    event.eligible = True
    event.set_extra("wsl_plain_command", True)
    event.set_extra("wsl_keep_reply", keep)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    for text in ("Menu", "Confirmation preview"):
        await event.send(MessageChain([Plain(text)]))
    assert event.platform.send_reply_text.await_count == 2
    assert all(
        call.kwargs.get("auto_recall", False) is (not private)
        for call in event.platform.send_reply_text.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("same_sender", [False, True])
async def test_real_menu_numbers_only_clean_up_the_senders_own_active_session(
    command_event, setup, monkeypatch, same_sender
):
    from astrbot.builtin_stars.wangshangliao_moderation.command_menu import (
        CommandMenu,
        overview,
    )
    from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
        recognize_integrated,
    )

    adapter, _ = setup
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    menu = CommandMenu(
        SimpleNamespace(
            integrated=True,
            tenants={},
            recognize_command=recognize_integrated,
            admit_command=lambda _: True,
            menu=overview(),
        )
    )
    assert await menu.handle(command_event, "菜单")
    sender, peer = ("2", "902") if same_sender else ("3", "903")
    adapter.members["5"][sender] = peer
    await adapter.ledger.ingest(
        "1",
        "5",
        "124",
        {
            "sender": sender,
            "name": "Member",
            "text": "3",
            "created_at": time.time() * 1000,
            "mentions": [],
            "recall_route": {"client": "choice-client", "time": "1000", "peer": peer},
        },
    )
    work = asyncio.create_task(adapter.process_group("5", single=True))
    event = await asyncio.wait_for(adapter._event_queue.get(), 1)
    try:
        assert await menu.handle(event, "3") is same_sender
        async with adapter.ledger.db.execute(
            "SELECT sender FROM reply_recalls WHERE key='recall/1/5/124'"
        ) as cursor:
            assert await cursor.fetchone() == (("2",) if same_sender else None)
        assert len(await jobs(adapter)) == (4 if same_sender else 2)
    finally:
        event.processing_completion.set_result(None)
        await asyncio.wait_for(work, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["unclassified", "private", "sender", "peer", "client", "timestamp", "missing"],
)
async def test_input_cleanup_requires_classification_and_authenticated_source(
    command_event, setup, change
):
    adapter, _ = setup
    event = command_event
    event.set_extra("wsl_plain_command", change != "unclassified")
    if change == "private":
        event.is_private_chat = lambda: True
    elif change == "sender":
        event.message_obj.sender.user_id = "3"
    elif change == "missing":
        await adapter.ledger.db.execute("DELETE FROM inbox")
    elif change in {"peer", "client", "timestamp"}:
        field = {"peer": "peer", "client": "client", "timestamp": "time"}[change]
        value = "" if field != "peer" else "903"
        await adapter.ledger.db.execute(
            "UPDATE inbox SET payload=json_set(payload,?,?)",
            (f"$.recall_route.{field}", value),
        )
    await reply_recall.schedule_command(event)
    assert await jobs(adapter) == []
    adapter.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["native_role", "member", "source", "paused", "removed", "account", "route"],
)
async def test_input_recall_rechecks_scope_after_wait(
    command_event, setup, wsl_active_policy, monkeypatch, change
):
    adapter, now = setup
    command_event.set_extra("wsl_plain_command", True)
    await reply_recall.schedule_command(command_event)
    now[0] = 1012
    entered = asyncio.Event()
    await adapter.send_lock.acquire()
    acquire = adapter.send_lock.acquire

    async def observed_acquire():
        entered.set()
        return await acquire()

    monkeypatch.setattr(adapter.send_lock, "acquire", observed_acquire)
    work = asyncio.create_task(reply_recall.recall_due(adapter))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "native_role":
            adapter.member_snapshots["5"][2]["groupMemberInfo"][0]["groupRole"] = (
                "GROUP_ROLE_MEMBER"
            )
        elif change == "member":
            adapter.members["5"]["2"] = "903"
        elif change == "source":
            # A different message route must never be used to clean up a command.
            await adapter.ledger.db.execute("UPDATE reply_recalls SET server_id='999'")
        elif change == "paused":
            wsl_active_policy({**adapter.config, "moderation": {"enabled": False}})
        elif change == "removed":
            wsl_active_policy({**adapter.config, "enabled_groups": []})
        elif change == "account":
            adapter.account = "3"
        else:
            adapter.groups["5"] = "906"
        adapter.send_lock.release()
        await asyncio.wait_for(work, 1)
        adapter.nim.request.assert_not_awaited()
    finally:
        if adapter.send_lock.locked():
            adapter.send_lock.release()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected",
    [
        (403, "rejected"),
        (TimeoutError(), "unknown"),
        (asyncio.CancelledError(), "unknown"),
    ],
)
async def test_input_recall_unknown_and_rejected_results_are_never_replayed(
    command_event, setup, outcome, expected
):
    adapter, now = setup
    command_event.set_extra("wsl_plain_command", True)
    await reply_recall.schedule_command(command_event)
    await reply_recall.schedule_command(command_event)
    assert len(await jobs(adapter)) == 1
    now[0] = 1012
    if isinstance(outcome, BaseException):
        adapter.nim.request.side_effect = outcome
    else:
        adapter.nim.request.side_effect = None
        adapter.nim.request.return_value = outcome, b""
    if isinstance(outcome, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await reply_recall.recall_due(adapter)
    else:
        await reply_recall.recall_due(adapter)
    assert (await jobs(adapter))[0][3] == expected
    assert (await adapter.ledger.receipt("recall/1/5/123"))["status"] == expected
    await reply_recall.recall_due(adapter)
    adapter.nim.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_schema_migration_preserves_existing_recall_jobs(setup):
    adapter, _ = setup
    await adapter.ledger.db.executescript(
        "DROP TABLE reply_recalls;"
        "CREATE TABLE reply_recalls("
        "key TEXT PRIMARY KEY,account TEXT,group_id TEXT,team TEXT,peer TEXT,"
        "client TEXT,server_id TEXT,message_time TEXT,deadline REAL,state TEXT);"
        "INSERT INTO reply_recalls VALUES("
        "'old','1','5','905','901','client','123','1000000',1012,'pending');"
    )
    await adapter.ledger.close()
    await adapter.ledger.open()
    async with adapter.ledger.db.execute(
        "SELECT key,sender,state FROM reply_recalls"
    ) as cursor:
        assert await cursor.fetchall() == [("old", "", "pending")]
