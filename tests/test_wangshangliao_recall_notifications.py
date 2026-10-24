"""Native mobile recall observation never executes moderation or reads content."""

import asyncio
from unittest.mock import Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.nim import (
    NimClient,
    recall_notifications,
)
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


def recall_body(**changes):
    """Build a normal-team SDK system notification.

    Args:
        changes: Numeric property replacements.

    Returns:
        Encoded system message properties.
    """
    fields = {
        0: "1791948000000",
        1: "8",
        2: "905",
        3: "902",
        6: "1001",
        10: "client-uuid",
        11: "2001",
        14: "1791947990000",
        15: "private-name",
        16: "901",
        4: "private-content",
    }
    fields.update({int(k): v for k, v in changes.items()})
    return wire.properties([(key, value.encode()) for key, value in fields.items()])


@pytest.mark.parametrize("command", [3, 14, 15])
def test_sdk_recall_routes_only_return_bounded_metadata(command):
    body = recall_body()
    if command == 15:
        body = wire.varint(1) + body + (1791948000000).to_bytes(8, "little") + b"\x01"
    rows = recall_notifications((7, command, 0, 200, body))
    assert rows == [
        {
            "recalled_at": "1791948000000",
            "team": "905",
            "author": "902",
            "notice_id": "1001",
            "message_id": "2001",
            "message_time": "1791947990000",
            "client": "client-uuid",
            "operator": "901",
        }
    ]
    assert "private" not in str(rows)


@pytest.mark.parametrize("kind", ["7", "12", "13", "14", "999"])
def test_other_scenes_and_one_way_deletes_are_not_group_recall(kind):
    assert recall_notifications((7, 14, 0, 200, recall_body(**{"1": kind}))) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("0", "0"), ("2", "../group"), ("11", "-1"), ("10", "x\n"), ("16", "name")],
)
def test_recall_invalid_ids_never_publish_metadata(field, value):
    with pytest.raises(wire.ProtocolError, match="recall_notification"):
        recall_notifications((7, 14, 0, 200, recall_body(**{field: value})))


def test_recall_wrapper_matches_live_sdk_notification():
    body = wire.packet(7, 14, 0, recall_body())
    expected = recall_notifications((7, 14, 0, 200, recall_body()))
    assert recall_notifications((4, 1, 0, 200, b"\0" * 8 + body)) == expected
    with pytest.raises(wire.ProtocolError):
        recall_notifications((7, 14, 0, 200, recall_body() + b"\0"))
    with pytest.raises(wire.ProtocolError):
        recall_notifications((7, 15, 0, 200, wire.varint(1025)))
    with pytest.raises(wire.ProtocolError, match="recall_notification"):
        recall_notifications((7, 14, 0, 200, b"\x01"))


@pytest.mark.asyncio
async def test_recall_is_durable_account_scoped_and_deduplicated(tmp_path):
    ledger = Ledger(tmp_path / "recalls.sqlite3")
    await ledger.open()
    row = recall_notifications((7, 14, 0, 200, recall_body()))[0]
    try:
        assert await ledger.observe_recall("1", "5", row)
        assert not await ledger.observe_recall("1", "5", row)
        assert await ledger.observe_recall("2", "5", row)
    finally:
        await ledger.close()
    await ledger.open()
    try:
        async with ledger.db.execute(
            "SELECT account,group_id FROM observed_recalls"
        ) as cursor:
            assert await cursor.fetchall() == [("1", "5"), ("2", "5")]
        assert not await ledger.observe_recall("1", "5", row)
        async with ledger.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_paused_catalog_group_can_observe_recall_without_any_business_action(
    tmp_path,
):
    adapter = WangshangliaoAdapter(
        {"id": "recall-fixture", "account_id": "1", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.ledger = Ledger(tmp_path / "recalls.sqlite3")
    await adapter.ledger.open()
    adapter.group_catalog = {"5": {"id": "5", "team": "905"}}
    adapter.diagnostics.emit = Mock()
    adapter.nim = NimClient(None)
    await adapter.nim.pushes.put((7, 14, 0, 200, recall_body()))
    task = asyncio.create_task(adapter.consume_nim_pushes(adapter.nim))
    try:
        for _ in range(100):
            async with adapter.ledger.db.execute(
                "SELECT COUNT(*) FROM observed_recalls WHERE account='1' AND group_id='5'"
            ) as cursor:
                if (await cursor.fetchone())[0] == 1:
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Native recall was not persisted")
        assert adapter.unsupported_pushes == 0
        assert adapter.groups == {}
        adapter.diagnostics.emit.assert_called_once_with(
            "recall", "observed", "1001", group="5"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.ledger.close()
