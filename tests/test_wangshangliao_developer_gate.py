import pytest

@pytest.fixture(autouse=True)
def configured_accounts(monkeypatch):
    from astrbot.core import astrbot_config
    monkeypatch.setitem(astrbot_config, "platform", [{"type": "wangshangliao", "account_id": account} for account in ("1", "2", "3")])

@pytest.mark.asyncio
@pytest.mark.parametrize("mentions,opened,replies,expected", [
    (["901"], True, True, [1, 0]),
    (["902"], True, True, [0, 1]),
    (["901", "902"], True, True, [1, 1]),
    (["1", "2"], True, True, [0, 0]),
    ([], True, True, [0, 0]),
    (["3"], True, True, [0, 0]),
    (["901", "902"], False, True, [0, 0]),
    (["901", "902"], True, False, [0, 0]),
])
async def test_three_accounts_inbox_to_reply(tmp_path, mentions, opened, replies, expected):
    import asyncio
    from unittest.mock import AsyncMock

    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger
    from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow
    from types import SimpleNamespace

    counts = []
    for account in ("1", "2"):
        adapter = WangshangliaoAdapter({"id": f"offline-{account}", "account_id": account,
            "enabled_groups": ["5"], "reply_groups": {"5": replies}}, {}, asyncio.Queue())
        adapter.ledger = Ledger(tmp_path / f"{account}.sqlite")
        await adapter.ledger.open()
        adapter.connection_state = "online"
        adapter.nim_account = "90" + account
        adapter.members = {"5": {"3": "903"}}
        adapter.send_text = AsyncMock(return_value="accepted")
        if opened:
            adapter.test_window = TestWindow(adapter,
                SimpleNamespace(account="3", stopping=asyncio.Event()),
                ["5"], ["group_commands"], [], 300, 1)
        payload = {"sender": "3", "name": "Fixture", "text": "/群管 帮助",
                   "created_at": 0, "mentions": mentions}
        await adapter.ledger.ingest(account, "5", "one", payload)
        await adapter.ledger.ingest(account, "5", "one", payload)
        worker = asyncio.create_task(adapter.process_group("5"))
        try:
            if opened:
                event = await asyncio.wait_for(adapter._event_queue.get(), 2)
                event.set_extra("wsl_command_result", True)
                assert event.eligible == (replies and adapter.nim_account in mentions)
                if event.eligible:
                    from astrbot.api.message_components import At

                    assert isinstance(event.message_obj.message[0], At)
                    assert str(event.message_obj.message[0].qq) == account
                await event.send(MessageChain([Plain("reply")]))
                await event.send(MessageChain([Plain("budget prevents second reply")]))
                event.processing_completion.set_result(None)
            async with asyncio.timeout(2):
                while True:
                    async with adapter.ledger.db.execute(
                        "SELECT state FROM inbox WHERE message='one'"
                    ) as cursor:
                        state = (await cursor.fetchone())[0]
                    if state in {"processed", "ignored_bot"}:
                        break
                    await asyncio.sleep(0.01)
            assert state == ("processed" if opened else "ignored_bot")
            assert adapter._event_queue.empty()
            counts.append(adapter.send_text.await_count)
        finally:
            adapter.stopping.set()
            await asyncio.wait_for(worker, 2)
            await adapter.ledger.close()
    assert counts == expected
