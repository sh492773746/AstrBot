"""Retired private quota notices cannot create cross-account delivery queues."""

from unittest.mock import Mock

from astrbot.builtin_stars.wangshangliao_moderation.ai_plans import AIPlans
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenStore


def test_retired_quota_notice_queues_stay_absent_across_account_reopens(
    tmp_path, monkeypatch
):
    store = OpenStore(tmp_path)
    store.discover("account-A", {})
    store.discover("account-B", {})
    for _ in range(2):
        plans = AIPlans(OpenStore(tmp_path))
        assert "notices" not in plans.status()
        with plans.store.connect() as db:
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='ai_quota_notices'"
            ).fetchone()
            before = "\n".join(db.iterdump())

        blocked = Mock(side_effect=AssertionError("Retired notices attempted IO"))
        with monkeypatch.context() as patch:
            patch.setattr(plans.store, "connect", blocked)
            for name in ("queue_notices", "claim_notice", "finish_notice"):
                assert not hasattr(AIPlans, name)
                assert not hasattr(plans, name)
            blocked.assert_not_called()
        with plans.store.connect() as db:
            assert "\n".join(db.iterdump()) == before
