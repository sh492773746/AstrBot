import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import adapter as module


def make_adapter():
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        members={},
        groups={},
        enabled_groups={"5", "6"},
        group_directory_state={},
        member_refresh_locks={},
        member_refresh_at={},
        diagnostics=SimpleNamespace(emit=Mock()),
        business=SimpleNamespace(),
    )
    adapter.refresh_member_mapping = (
        module.WangshangliaoAdapter.refresh_member_mapping.__get__(adapter)
    )
    return adapter


@pytest.mark.asyncio
async def test_mapping_refresh_coalesces_and_publishes(monkeypatch):
    adapter = make_adapter()
    roster = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}
    fetch = AsyncMock(return_value=roster)
    monkeypatch.setattr(module, "member_directory", fetch)
    await asyncio.gather(
        *(adapter.refresh_member_mapping("5", bounded=True) for _ in range(5))
    )
    assert fetch.await_count == 1
    assert adapter.members["5"] == {"2": "902"}
    assert adapter.group_directory_state["5"]["complete"]


@pytest.mark.asyncio
async def test_failed_mapping_preserves_valid_snapshot_and_throttles(monkeypatch):
    adapter = make_adapter()
    adapter.members["5"] = {"2": "902"}
    fetch = AsyncMock(side_effect=module.wire.ProtocolError("member_identity"))
    monkeypatch.setattr(module, "member_directory", fetch)
    with pytest.raises(module.wire.ProtocolError):
        await adapter.refresh_member_mapping("5", bounded=True)
    assert await adapter.refresh_member_mapping("5", bounded=True) is None
    assert adapter.members["5"] == {"2": "902"}
    assert not adapter.group_directory_state["5"]["complete"]
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_bad_group_does_not_block_other_group(monkeypatch):
    adapter = make_adapter()
    adapter.business.request = AsyncMock(
        return_value={
            "owner": [],
            "member": [
                {"groupId": "5", "groupCloudId": "905"},
                {"groupId": "6", "groupCloudId": "906"},
            ],
        }
    )
    fetch = AsyncMock(
        side_effect=[
            module.wire.ProtocolError("member_identity"),
            {
                "complete": True,
                "groupMemberInfo": [{"userId": "2", "nimId": "902"}],
            },
        ]
    )
    monkeypatch.setattr(module, "member_directory", fetch)
    await module.WangshangliaoAdapter.load_groups(adapter)
    assert adapter.groups == {"5": "905", "6": "906"}
    assert adapter.members == {"6": {"2": "902"}}
    assert not adapter.group_directory_state["5"]["complete"]


@pytest.mark.asyncio
async def test_operation_query_missing_table_and_group_isolation(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    adapter = SimpleNamespace(config={"id": "test"})
    query = module.WangshangliaoAdapter.get_moderation_result
    assert await query(adapter, "op", "5") == {}
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        assert await query(adapter, "op", "5") == {}
        db.execute("CREATE TABLE operations(id TEXT, result TEXT)")
        db.execute(
            'INSERT INTO operations VALUES(\'op\', \'{"group":5,"status":"accepted"}\')'
        )
    assert await query(adapter, "op", "6") == {}
    assert (await query(adapter, "op", "5"))["status"] == "accepted"
