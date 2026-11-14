"""Developer windows retain group reply gates and cannot admit private traffic."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window


@pytest.fixture(autouse=True)
def configured_accounts(monkeypatch, wsl_active_policy):
    from astrbot.core import astrbot_config

    monkeypatch.setitem(
        astrbot_config,
        "platform",
        [
            {"type": "wangshangliao", "account_id": account}
            for account in ("1", "2", "3")
        ],
    )
    monkeypatch.setattr(runtime, "open_service", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mentions,opened,replies,expected",
    [
        (["901"], True, True, [1, 0]),
        (["902"], True, True, [0, 1]),
        (["901", "902"], True, True, [1, 1]),
        (["1", "2"], True, True, [0, 0]),
        ([], True, True, [0, 0]),
        (["3"], True, True, [0, 0]),
        (["901", "902"], False, True, [0, 0]),
        (["901", "902"], True, False, [0, 0]),
    ],
)
async def test_three_accounts_inbox_to_reply(
    tmp_path, mentions, opened, replies, expected
):
    counts = []
    for account in ("1", "2"):
        adapter = WangshangliaoAdapter(
            {
                "id": f"offline-{account}",
                "account_id": account,
                "enabled_groups": ["5"],
                "reply_groups": {"5": replies},
            },
            {},
            asyncio.Queue(),
        )
        adapter.ledger = Ledger(tmp_path / f"{account}.sqlite")
        await adapter.ledger.open()
        worker = event = None
        try:
            adapter.connection_state = "online"
            adapter.nim_account = "90" + account
            adapter.members = {"5": {"3": "903"}}
            adapter.send_text = AsyncMock()
            adapter.send_reply_text = AsyncMock(return_value="accepted")
            if opened:
                adapter.test_window = Window(
                    adapter,
                    SimpleNamespace(account="3", stopping=asyncio.Event()),
                    ["5"],
                    ["group_commands"],
                    [],
                    300,
                    1,
                )
            payload = {
                "sender": "3",
                "name": "Fixture",
                "text": "成员列表",
                "created_at": 0,
                "mentions": mentions,
                "recall_route": {"peer": "903"},
            }
            assert await adapter.ledger.ingest(account, "5", "one", payload)
            assert not await adapter.ledger.ingest(account, "5", "one", payload)
            worker = asyncio.create_task(adapter.process_group("5", single=True))
            if opened:
                event = await asyncio.wait_for(adapter._event_queue.get(), 2)
                event.set_extra("wsl_command_result", True)
                assert event.message_obj.type is MessageType.GROUP_MESSAGE
                assert event.get_group_id() == "5"
                assert event.get_extra("wsl_test_scope") == "group_commands"
                assert event.actor_identity.verified_uid == "3"
                assert event.eligible == (replies and adapter.nim_account in mentions)
                if event.eligible:
                    assert isinstance(event.message_obj.message[0], At)
                    assert str(event.message_obj.message[0].qq) == account
                await event.send(MessageChain([Plain("reply")]))
                await event.send(MessageChain([Plain("budget prevents second reply")]))
                event.processing_completion.set_result(None)
                assert adapter.test_window.remaining == 0
            await asyncio.wait_for(worker, 2)
            async with adapter.ledger.db.execute(
                "SELECT state FROM inbox WHERE message='one'"
            ) as cursor:
                assert (await cursor.fetchone())[0] == (
                    "processed" if opened else "ignored_bot"
                )
            assert adapter._event_queue.empty()
            adapter.send_text.assert_not_awaited()
            if adapter.send_reply_text.await_count:
                call = adapter.send_reply_text.call_args
                assert call.args[0] == "5"
                assert call.kwargs["mentions"][0]["uid"] == 903
            counts.append(adapter.send_reply_text.await_count)
        finally:
            adapter.stopping.set()
            if event is not None and not event.processing_completion.done():
                event.processing_completion.cancel()
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await adapter.ledger.close()
    assert counts == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("opened", [False, True])
@pytest.mark.parametrize("body", ["private-body-canary", None])
async def test_private_ingress_cannot_enter_group_developer_window(
    tmp_path, monkeypatch, opened, body
):
    decode = Mock(side_effect=AssertionError("Private body must not be decoded"))
    observe = Mock(side_effect=AssertionError("Private traffic must not be observed"))
    monkeypatch.setattr(wire, "open_message", decode)
    monkeypatch.setattr(runtime, "observe", observe)
    adapter = WangshangliaoAdapter(
        {"id": "private-drop", "account_id": "1", "enabled_groups": ["5"]},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "private.sqlite")
    await adapter.ledger.open()
    try:
        adapter.connection_state = "online"
        adapter.nim_account = "901"
        adapter.nim = SimpleNamespace(acknowledge=AsyncMock(), request=AsyncMock())
        adapter.send_text = AsyncMock()
        adapter.send_reply_text = AsyncMock()
        adapter.ledger.ingest = AsyncMock(
            side_effect=AssertionError("Private traffic must not enter the ledger")
        )
        if opened:
            adapter.test_window = Window(
                adapter,
                SimpleNamespace(account="3", stopping=asyncio.Event()),
                ["5"],
                ["group_commands"],
                [],
                300,
                1,
            )
        outer = {0: "0", 1: "901", 2: "903", 8: "100", 10: body, 12: "private"}
        changes = adapter.ledger.db.total_changes
        await adapter.ingest(outer)
        await adapter.ingest(outer)
        assert adapter.nim.acknowledge.await_count == 2
        adapter.nim.acknowledge.assert_awaited_with(0, "private")
        header_only = {key: value for key, value in outer.items() if key != 10}
        await adapter.ingest({**header_only, 12: "header-only"})
        adapter.nim.acknowledge.assert_awaited_with(0, "header-only")
        for invalid in (
            {1: "other-account"},
            {2: "901"},
            {2: ""},
            {2: None},
            {2: "x" * 1025},
        ):
            await adapter.ingest({**outer, **invalid})
        adapter.nim_account = ""
        await adapter.ingest(outer)
        assert adapter.nim.acknowledge.await_count == 3
        decode.assert_not_called()
        observe.assert_not_called()
        adapter.ledger.ingest.assert_not_awaited()
        adapter.nim.request.assert_not_awaited()
        adapter.send_text.assert_not_awaited()
        adapter.send_reply_text.assert_not_awaited()
        assert adapter.ledger.db.total_changes == changes
        async with adapter.ledger.db.execute(
            "SELECT (SELECT COUNT(*) FROM inbox),(SELECT COUNT(*) FROM outbox)"
        ) as cursor:
            assert await cursor.fetchone() == (0, 0)
        assert adapter._event_queue.empty()
        assert not adapter.dispatch_wakeup.is_set()
        assert not adapter.workers
        if opened:
            assert adapter.test_window.remaining == 1
            assert adapter.test_window.outbound_remaining == 1
            assert adapter.test_window.accepted == adapter.test_window.replies == {}
    finally:
        adapter.stopping.set()
        await adapter.ledger.close()
