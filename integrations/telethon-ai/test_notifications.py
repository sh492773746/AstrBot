import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, NetworkError, RetryAfter

from data.plugins.astrbot_plugin_telethon_ai.notifications import Notifications
from data.plugins.astrbot_plugin_telethon_ai.policy import Gate
from data.plugins.astrbot_plugin_telethon_ai.tenants import Denied, Tenants


@pytest.fixture
def service(tmp_path):
    store = Tenants(tmp_path / "tenants.db")
    gate = Gate(tmp_path / "gate.db")
    tenant = store.create_trial("admin", "111", "222", "AIClient_test", budget=2)
    store.assign("admin", tenant, "one")
    store.authorize_group("admin", tenant, "-100123")
    store.set_enabled("admin", tenant, True)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123))
    )
    accounts = [{"account": "one", "username": "ai_one", "daily_limit": 2}]
    notifier = Notifications(store, gate)
    yield store, gate, tenant, bot, accounts, notifier
    gate.close()
    store.close()


def expire(store, tenant):
    with store.db:
        store.db.execute(
            "UPDATE tenants SET expires=? WHERE id=?", (time.time() - 1, tenant)
        )


def exhaust(store):
    for message in (1, 2):
        reserved, _ = store.reserve("one", "-100123", message)
        store.finish(reserved, "sent")


@pytest.mark.asyncio
async def test_expired_notice_is_private_deduplicated_and_preserves_data(service):
    store, gate, tenant, bot, accounts, notifier = service
    before = store.summary(tenant)
    expire(store, tenant)
    await notifier.tick(bot, accounts)
    await notifier.tick(bot, accounts)
    await Notifications(store, gate).tick(bot, accounts)
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.call_args.kwargs
    assert sent["chat_id"] == 111
    assert sent["parse_mode"] is None
    assert "克隆服务到期通知" in sent["text"]
    assert "回收桶" in sent["text"]
    after = store.summary(tenant)
    assert after["accounts"] == before["accounts"]
    assert after["groups"] == before["groups"]
    assert after["budget"] == before["budget"]
    with pytest.raises(Denied):
        store.reserve("one", "-100123", 99)
    assert store.db.execute("SELECT state FROM notifications").fetchone()[0] == "sent"


@pytest.mark.asyncio
async def test_cumulative_authorization_quota_still_notifies(service):
    store, gate, tenant, bot, accounts, notifier = service
    exhaust(store)
    await notifier.tick(bot, accounts)
    await notifier.tick(bot, accounts)
    assert bot.send_message.await_count == 1
    assert "累计额度用完通知" in bot.send_message.call_args.kwargs["text"]
    assert "2 / 2" in bot.send_message.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_account_daily_statistics_never_generate_quota_notices(service):
    store, gate, tenant, bot, accounts, notifier = service
    now = time.time()
    policy = {"daily_limit": 2, "cooldown_seconds": 10}
    for mid in range(105):
        assert gate.admit("one", "-100123", mid, policy, now=now + mid * 10)
    await notifier.tick(bot, accounts, now=now + 1040)
    await notifier.tick(bot, accounts, now=now + 1040)
    bot.send_message.assert_not_awaited()
    await notifier.tick(bot, accounts, now=now + 86400)
    bot.send_message.assert_not_awaited()
    assert not store.db.execute(
        "SELECT 1 FROM notifications WHERE kind='daily'"
    ).fetchone()
    assert store.summary(tenant)["used"] == 0
    assert store.summary(tenant)["budget"] == 2


@pytest.mark.asyncio
async def test_queued_legacy_daily_notifications_cancel_without_deleting_audit(service):
    store, gate, tenant, bot, accounts, notifier = service
    notifier.enqueue(store.summary(tenant), "daily", "legacy-day:2", "one")
    restarted = Notifications(store, gate)
    assert (
        dict(store.db.execute("SELECT * FROM notifications").fetchone())["state"]
        == "cancelled"
    )
    assert (
        store.db.execute("SELECT error_code FROM notifications").fetchone()[0]
        == "account_daily_cap_removed"
    )
    await restarted.tick(bot, accounts)
    bot.send_message.assert_not_awaited()
    notifier.enqueue(store.summary(tenant), "daily", "late-old-worker:2", "one")
    await restarted.tick(bot, accounts)
    bot.send_message.assert_not_awaited()
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM notifications WHERE state='cancelled'"
        ).fetchone()[0]
        == 2
    )
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM audit WHERE action='notification_cancelled'"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.asyncio
async def test_stale_expiry_queue_cancelled_after_renewal(service):
    store, _, tenant, bot, accounts, notifier = service
    expire(store, tenant)
    await notifier.tick(None, accounts)
    with store.db:
        store.db.execute(
            "UPDATE tenants SET expires=? WHERE id=?", (time.time() + 86400, tenant)
        )
    await notifier.tick(bot, accounts)
    bot.send_message.assert_not_awaited()
    assert (
        store.db.execute("SELECT state FROM notifications").fetchone()[0] == "cancelled"
    )


@pytest.mark.asyncio
async def test_released_reservation_cancels_stale_quota_notice(service):
    store, _, tenant, bot, accounts, notifier = service
    exhaust(store)
    await notifier.tick(None, accounts)
    with store.db:
        store.db.execute(
            "UPDATE reservations SET state='released' WHERE tenant=?", (tenant,)
        )
    await notifier.tick(bot, accounts)
    bot.send_message.assert_not_awaited()
    assert (
        store.db.execute("SELECT state FROM notifications").fetchone()[0] == "cancelled"
    )


@pytest.mark.asyncio
async def test_owner_rechecked_before_delivery(service):
    store, _, tenant, bot, accounts, notifier = service
    expire(store, tenant)
    await notifier.tick(None, accounts)
    with store.db:
        store.db.execute("UPDATE tenants SET owner='333' WHERE id=?", (tenant,))
    await notifier.tick(bot, accounts)
    bot.send_message.assert_not_awaited()
    assert (
        store.db.execute("SELECT state FROM notifications").fetchone()[0] == "cancelled"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,state",
    [
        (Forbidden("blocked"), "blocked"),
        (NetworkError("private detail"), "uncertain"),
    ],
)
async def test_delivery_failure_does_not_blindly_retry(service, error, state):
    store, gate, tenant, bot, accounts, notifier = service
    expire(store, tenant)
    bot.send_message.side_effect = error
    await notifier.tick(bot, accounts)
    await Notifications(store, gate).tick(bot, accounts)
    assert bot.send_message.await_count == 1
    assert store.db.execute("SELECT state FROM notifications").fetchone()[0] == state
    assert "private detail" not in str(
        dict(store.db.execute("SELECT * FROM notifications").fetchone())
    )


@pytest.mark.asyncio
async def test_rate_limit_retries_only_after_delay(service):
    store, _, tenant, bot, accounts, notifier = service
    expire(store, tenant)
    bot.send_message.side_effect = [RetryAfter(60), SimpleNamespace(message_id=321)]
    await notifier.tick(bot, accounts)
    await notifier.tick(bot, accounts)
    assert bot.send_message.await_count == 1
    await notifier.tick(bot, accounts, now=notifier.retry_until + 1)
    assert bot.send_message.await_count == 2
    assert store.db.execute("SELECT state FROM notifications").fetchone()[0] == "sent"


@pytest.mark.asyncio
async def test_interrupted_send_requires_review(service):
    store, gate, tenant, bot, accounts, notifier = service
    expire(store, tenant)
    bot.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await notifier.tick(bot, accounts)
    assert (
        store.db.execute("SELECT state FROM notifications").fetchone()[0] == "uncertain"
    )
    await Notifications(store, gate).tick(bot, accounts)
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_crashed_send_is_not_replayed(service):
    store, gate, tenant, bot, accounts, notifier = service
    expire(store, tenant)
    await notifier.tick(None, accounts)
    with store.db:
        store.db.execute("UPDATE notifications SET state='sending'")
    await Notifications(store, gate).tick(bot, accounts)
    bot.send_message.assert_not_awaited()
    assert (
        store.db.execute("SELECT state FROM notifications").fetchone()[0] == "uncertain"
    )


@pytest.mark.asyncio
async def test_multiple_services_notify_only_their_bound_owners(service):
    store, _, tenant, bot, accounts, notifier = service
    second = store.create_trial("admin", "333", "444", "AIClient_second")
    expire(store, tenant)
    expire(store, second)
    await notifier.tick(bot, accounts)
    assert {call.kwargs["chat_id"] for call in bot.send_message.call_args_list} == {
        111,
        333,
    }
    assert bot.send_message.await_count == 2
    for call in bot.send_message.call_args_list:
        expected = "222" if call.kwargs["chat_id"] == 111 else "444"
        assert f"Bot ID：{expected}" in call.kwargs["text"]


@pytest.mark.asyncio
async def test_duplicate_suppression_survives_reopening_database(service):
    store, gate, tenant, bot, accounts, notifier = service
    expire(store, tenant)
    await notifier.tick(bot, accounts)
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    reopened = Tenants(path)
    try:
        await Notifications(reopened, gate).tick(bot, accounts)
        bot.send_message.assert_awaited_once()
    finally:
        reopened.close()
