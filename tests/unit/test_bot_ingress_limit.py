"""Verify flood limits without contacting Telegram."""

from astrbot.core.utils.bot_ingress_limit import BotIngressLimit


def test_burst_recovers_and_users_are_independent():
    limiter = BotIngressLimit()
    assert all(limiter.allow(1, 0) for _ in range(5))
    assert not limiter.allow(1, 1)
    assert limiter.allow(2, 1)
    assert limiter.allow(1, 6)


def test_global_limit_and_recovery():
    limiter = BotIngressLimit()
    assert all(limiter.allow(i, 0) for i in range(120))
    assert not limiter.allow(121, 0)
    assert limiter.allow(121, 60)


def test_per_minute_budget_and_bounded_identity_memory():
    limiter = BotIngressLimit()
    for at in [0, 6, 12, 18]:
        assert all(limiter.allow(1, at) for _ in range(5))
    assert not limiter.allow(1, 30)
    assert limiter.allow(1, 60)
    for user in range(3000):
        limiter.allow(user, 120 + user)
    assert len(limiter.users) <= 2048
