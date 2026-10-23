"""Dead-code cleanup must preserve legacy financial rows in temporary ledgers."""

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import CashbackStore
from tests.test_wangshangliao_cashback_qualification import OWNER, scan
from tests.test_wangshangliao_cashback_qualification import scenario as scenario


@pytest.mark.parametrize("state", ["verifying", "pending", "paid"])
@pytest.mark.parametrize("avatar_status", ["detected", "unavailable", "manual"])
def test_reopening_never_normalizes_historical_agent_pricing(
    scenario, state, avatar_status
):
    """Preserve the entire ledger when legacy avatar metadata is present.

    Args:
        scenario: Temporary ledger with a synthetic invitation and fixed clock.
        state: Historical payment or qualification state.
        avatar_status: Historical avatar evidence, including cashier decisions.
    """
    store, *_ = scenario
    scan(scenario)
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET amount=777,member_class='agent',"
            "member_avatar_status=?,member_avatar_digit_count=7,state=?,"
            "paid_at=?,paid_by=?,version=37 WHERE member='2'",
            (
                avatar_status,
                state,
                2000 if state == "paid" else None,
                OWNER if state == "paid" else None,
            ),
        )
    with store.connect() as db:
        before = tuple(db.iterdump())
    store.close()

    for _ in range(2):
        reopened = CashbackStore(store.path)
        try:
            with reopened.connect() as db:
                assert tuple(db.iterdump()) == before
        finally:
            reopened.close()
