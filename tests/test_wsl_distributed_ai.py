"""Offline regressions for shared AI accounting recipient isolation."""

import tempfile

from astrbot.builtin_stars.wangshangliao_moderation.ai_plans import AIPlans
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenStore


def test_quota_notices_stay_with_their_account_recipient():
    """Do not return a shared-pool notice through another account's transport."""
    plans = AIPlans(OpenStore(tempfile.mkdtemp(prefix="wsl-distributed-ai-")))

    account_a = plans.queue_notices(
        "nim/1001", "1", "account-A", reason="personal_quota", now=1760000000
    )
    account_b = plans.queue_notices(
        "nim/1001", "1", "account-B", reason="personal_quota", now=1760000001
    )

    assert {row["account"] for row in account_a} == {"account-A"}
    assert {row["account"] for row in account_b} == {"account-B"}

    account_a_again = plans.queue_notices(
        "nim/1001", "1", "account-A", reason="personal_quota", now=1760000002
    )
    account_b_again = plans.queue_notices(
        "nim/1001", "1", "account-B", reason="personal_quota", now=1760000003
    )
    assert {row["account"] for row in account_a_again} == {"account-A"}
    assert {row["account"] for row in account_b_again} == {"account-B"}
