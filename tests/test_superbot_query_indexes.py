"""Indexed group lookups and historical receipt migration."""

# ruff: noqa: F811

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, text_service  # noqa: F401

from data.plugins.astrbot_plugin_superbot.game_broadcast import GameBroadcast


@pytest.mark.asyncio
async def test_receipt_backfill_preserves_roster_and_is_idempotent(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, "大10大5", 10)
    original = s.group.broadcast.roster("-1001", 101)
    assert original[0]["stake"] == 15
    with s.store.tx() as db:
        db.execute("UPDATE bets SET receipt_op=NULL")
        s.store.put(db, "receipt_index_migrated", False)
    restored = GameBroadcast(s.group)
    assert restored.roster("-1001", 101) == original
    assert GameBroadcast(s.group).roster("-1001", 101) == original
    assert s.store.balance("2") == 985


@pytest.mark.parametrize(
    "query,index",
    [
        (
            "SELECT * FROM bets WHERE uid='2' AND points_chat='-1001' ORDER BY at DESC,rowid DESC LIMIT 10",
            "bets_group_history",
        ),
        (
            "SELECT * FROM bets WHERE uid='2' AND points_chat='-1001' AND issue=101",
            "bets_group_period",
        ),
        (
            "SELECT * FROM gt_requests WHERE chat='-1001' AND status='pending' ORDER BY rowid LIMIT 1",
            "gt_feedback_chat_status",
        ),
        (
            "SELECT b.* FROM gg_receipts r JOIN bets b ON b.receipt_op=r.op WHERE r.chat='-1001' AND r.issue=101",
            "bets_receipt_status",
        ),
    ],
)
def test_hot_query_uses_index_without_temporary_sort(text_service, query, index):
    plan = " ".join(
        str(tuple(r))
        for r in text_service.store.db.execute("EXPLAIN QUERY PLAN " + query)
    )
    assert index in plan
    assert "TEMP B-TREE" not in plan
    assert "SCAN b" not in plan
