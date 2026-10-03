import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


@pytest.mark.asyncio
async def test_recall_routing_and_no_repeat(tmp_path):
    ledger = Ledger(tmp_path / "recall.sqlite")
    await ledger.open()
    adapter = SimpleNamespace(
        account="1",
        config={
            "enabled_groups": ["5"],
            "moderation": {"enabled": True, "permissions": {"5": ["recall"]}},
        },
        ledger=ledger,
        members={"5": {"2": "902"}},
        groups={"5": "905"},
        stopping=asyncio.Event(),
        connection_state="online",
        send_lock=asyncio.Lock(),
        nim=SimpleNamespace(request=AsyncMock(return_value=(200, b""))),
        diagnostics=SimpleNamespace(emit=Mock()),
        get_moderation_members=AsyncMock(
            return_value={
                "groupMemberInfo": [
                    {"userId": "1", "groupRole": "GROUP_ROLE_ADMIN"},
                    {"userId": "2", "groupRole": "GROUP_ROLE_MEMBER"},
                ]
            }
        ),
    )
    try:
        await ledger.ingest(
            "1",
            "5",
            "123",
            {
                "sender": "2",
                "recall_route": {"client": "client", "time": "1000", "peer": "902"},
            },
        )
        assert (
            await WangshangliaoAdapter.recall_violation(adapter, "5", "123", "2")
            == "accepted"
        )
        assert (
            await WangshangliaoAdapter.recall_violation(adapter, "5", "123", "2")
            == "accepted"
        )
        assert adapter.nim.request.await_count == 1
        request = adapter.nim.request.call_args.args
        assert request[:2] == (7, 13)
        fields, _ = wire.read_properties(request[2])
        assert fields[2] == b"905" and fields[11] == b"123" and fields[3] == b"902"
        with pytest.raises(wire.ProtocolError, match="recall_identity"):
            await WangshangliaoAdapter.recall_violation(adapter, "5", "124", "2")
        roster = adapter.get_moderation_members.return_value

        async def revoke_during_lookup(group):
            adapter.config["moderation"]["enabled"] = False
            return roster

        adapter.get_moderation_members.side_effect = revoke_during_lookup
        with pytest.raises(wire.ProtocolError, match="moderation_permission"):
            await WangshangliaoAdapter.recall_violation(adapter, "5", "123", "2")
        assert adapter.nim.request.await_count == 1
    finally:
        await ledger.close()
