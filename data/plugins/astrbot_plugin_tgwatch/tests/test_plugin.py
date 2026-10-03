"""Mocked Telegram integration tests using the installed AstrBot runtime."""

import asyncio
import copy
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import BadRequest, NetworkError
from telegram.ext import ApplicationHandlerStop

from data.plugins.astrbot_plugin_tgwatch.main import TZ, TelegramWatch

pytestmark = pytest.mark.asyncio


@pytest.fixture
def plugin():
    value = TelegramWatch(SimpleNamespace(), {})
    value.admins = {7}
    value.owner = 7
    value.work_chat = -100777
    value.state = {"reports": {}}
    value.saved = []

    async def persist(key, state):
        value.saved.append(copy.deepcopy(state))

    value.put_kv_data = persist
    value.api = AsyncMock(
        return_value={
            "items": [
                {"id": 7, "name": "<Alice>_*", "count": 2, "cumulative": 3, "groups": 1}
            ],
            "health": {"incomplete": False},
        }
    )
    return value


@pytest.fixture
def bot():
    return SimpleNamespace(username="test_work_bot", send_message=AsyncMock())


async def test_report_persists_before_send_and_deduplicates(plugin, bot):
    start = datetime(2026, 9, 20, 14, tzinfo=TZ)

    async def check(**kwargs):
        assert next(iter(plugin.saved[-1]["reports"].values()))["status"] == "sending"
        assert kwargs["parse_mode"] == "MarkdownV2"

    bot.send_message.side_effect = check
    await plugin.report(
        bot, "hour", start, start + timedelta(hours=1), start + timedelta(hours=1)
    )
    await plugin.report(
        bot, "hour", start, start + timedelta(hours=1), start + timedelta(hours=1)
    )
    assert bot.send_message.await_count == 1
    assert next(iter(plugin.state["reports"].values()))["status"] == "sent"


async def test_uncertain_network_result_never_blindly_retries(plugin, bot):
    start = datetime(2026, 9, 20, tzinfo=TZ)
    bot.send_message.side_effect = NetworkError("Lost response")
    await plugin.report(
        bot, "day", start, start + timedelta(days=1), start + timedelta(days=1)
    )
    bot.send_message.side_effect = None
    await plugin.report(
        bot, "day", start, start + timedelta(days=1), start + timedelta(days=2)
    )
    assert bot.send_message.await_count == 1
    assert next(iter(plugin.state["reports"].values()))["status"] == "uncertain"


async def test_explicit_rejection_can_retry(plugin, bot):
    start = datetime(2026, 9, 20, tzinfo=TZ)
    bot.send_message.side_effect = BadRequest("Known rejection")
    await plugin.report(
        bot, "day", start, start + timedelta(days=1), start + timedelta(days=1)
    )
    bot.send_message.side_effect = None
    await plugin.report(
        bot,
        "day",
        start,
        start + timedelta(days=1),
        start + timedelta(days=1, minutes=6),
    )
    assert bot.send_message.await_count == 2
    assert next(iter(plugin.state["reports"].values()))["status"] == "sent"


async def test_api_unavailable_never_sends_zero_board(plugin, bot):
    plugin.api.side_effect = RuntimeError("Unavailable")
    start = datetime(2026, 9, 20, tzinfo=TZ)
    with pytest.raises(RuntimeError):
        await plugin.report(bot, "day", start, start + timedelta(days=1), start)
    bot.send_message.assert_not_called()
    assert not plugin.state["reports"]


@pytest.mark.parametrize("user_id,chat_type", [(99, "private"), (7, "supergroup")])
async def test_callback_permissions(plugin, bot, user_id, chat_type):
    query = SimpleNamespace(data="tw:menu:status", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=99, type=chat_type),
        effective_user=SimpleNamespace(id=user_id),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_not_called()
    query.answer.assert_awaited_once_with("无权访问", show_alert=True)


async def test_private_command_denied_and_unrelated_start_passthrough(plugin, bot):
    update = SimpleNamespace(
        effective_message=SimpleNamespace(text="/tgwatch status"),
        effective_chat=SimpleNamespace(id=99, type="private"),
        effective_user=SimpleNamespace(id=99),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.command(update, SimpleNamespace(bot=bot, args=["status"]))
    plugin.api.assert_not_called()
    update.effective_message.text = "/start another_plugin"
    await plugin.command(update, SimpleNamespace(bot=bot, args=["another_plugin"]))


async def test_literal_long_text_utf16_limit(plugin, bot):
    text = "😀<b>literal</b>_*" * 1000
    await plugin.send_text(bot, 7, text)
    parts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert "".join(re.sub(r"\\(.)", r"\1", part[1:-1]) for part in parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts)
    assert all(
        call.kwargs["parse_mode"] == "MarkdownV2"
        for call in bot.send_message.await_args_list
    )


async def test_record_filters_pagination_and_expired_body(plugin, bot):
    plugin.api.return_value = {
        "items": [
            {
                "sent": "2026-09-20T04:00:00+00:00",
                "deleted": False,
                "text": None,
                "reply_to": 3,
                "message_id": 4,
                "person_name": "A",
                "group_name": "G",
                "media": "photo",
            }
        ],
        "has_next": True,
        "health": {"incomplete": True},
    }
    await plugin.records(bot, 7, "20260920", 8, -100123, 1)
    params = plugin.api.call_args.args[1]
    assert params["start"] == "2026-09-19T16:00:00+00:00"
    assert params["end"] == "2026-09-20T16:00:00+00:00"
    assert params["person"] == 8 and params["group"] == -100123 and params["page"] == 1
    kwargs = bot.send_message.call_args.kwargs
    assert "原文已过保留期" in kwargs["text"] and "可能不完整" in kwargs["text"]
    callbacks = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "tw:records:20260920:8:-100123:2" in callbacks
    assert all(len(value.encode()) <= 64 for value in callbacks)


@pytest.mark.parametrize(
    "hour,minute,expected_day", [(0, 0, 18), (1, 29, 18), (1, 30, 19), (15, 0, 19)]
)
async def test_daily_window_and_no_old_hourly_backfill(
    plugin, bot, hour, minute, expected_day
):
    now = datetime(2026, 9, 20, hour, minute, tzinfo=TZ)
    plugin.last_hour = now.replace(minute=0)
    plugin.report = AsyncMock()
    plugin.api.return_value = {"state": "connected", "groups": []}
    await plugin.tick(bot, now)
    assert plugin.report.await_count == 1
    args = plugin.report.call_args.args
    assert args[1] == "day" and args[2].day == expected_day


async def test_midnight_hour_belongs_to_previous_calendar_day(plugin, bot):
    start = datetime(2026, 9, 19, 23, tzinfo=TZ)
    await plugin.report(
        bot, "hour", start, start + timedelta(hours=1), start + timedelta(hours=1)
    )
    params = plugin.api.call_args.args[1]
    assert params["cumulative_start"] == "2026-09-18T16:00:00+00:00"
    assert params["end"] == "2026-09-19T16:00:00+00:00"


async def test_initialize_recovers_sending_as_uncertain_and_terminate(plugin):
    plugin.config = {
        "enabled": True,
        "admin_ids": [7],
        "work_chat_id": "-100777",
        "platform_id": "telegram1",
        "api_url": "http://127.0.0.1:6190",
        "api_token": "x" * 32,
    }
    plugin.get_kv_data = AsyncMock(
        return_value={"reports": {"day:old": {"status": "sending"}}}
    )
    plugin.run = AsyncMock()
    await plugin.initialize()
    assert plugin.state["reports"]["-100777:day:old"]["status"] == "uncertain"
    application = SimpleNamespace(remove_handler=Mock())
    plugin.application = application
    plugin.handlers = [object(), object()]
    session = plugin.session
    await plugin.terminate()
    assert application.remove_handler.call_count == 3 and session.closed
    assert plugin.task is None and plugin.handlers == []


async def test_adapter_rebuild_rebinds_without_starting_poller(plugin, bot):
    first = SimpleNamespace(
        add_handler=Mock(), remove_handler=Mock(), running=False, bot=bot
    )
    second = SimpleNamespace(
        add_handler=Mock(), remove_handler=Mock(), running=False, bot=bot
    )
    plugin.config = {"platform_id": "telegram1"}
    platforms = iter(
        [SimpleNamespace(application=first), SimpleNamespace(application=second)]
    )
    plugin.context = SimpleNamespace(get_platform_inst=lambda _: next(platforms))
    calls = 0

    async def advance(_):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise asyncio.CancelledError

    from unittest.mock import patch

    with patch(
        "data.plugins.astrbot_plugin_tgwatch.main.asyncio.sleep", side_effect=advance
    ):
        with pytest.raises(asyncio.CancelledError):
            await plugin.run()
    assert first.add_handler.call_count == 4 and first.remove_handler.call_count == 4
    assert second.add_handler.call_count == 4
    await plugin.terminate()
    assert second.remove_handler.call_count == 4


async def test_real_http_bridge_and_independent_collector_lifetime(
    plugin, bot, tmp_path
):
    import json
    from pathlib import Path

    import aiohttp

    collector = Path(__file__).resolve().parents[5] / "telegram-chat-collector"
    python = collector / ".venv/bin/python"
    if not python.exists():
        pytest.skip("Independent collector environment is not installed")
    script = """
import asyncio, json, sys, time
from pathlib import Path
from aiohttp import web
from collector.store import Store
from collector.runtime import Collector
from collector.api import create_app
class Client:
    def add_event_handler(self, *args): pass
    def is_connected(self): return True
async def main():
    store = Store(Path(sys.argv[1]))
    now = time.time()
    store.configure('groups', -100123, 'Test group', True, now - 100)
    store.configure('people', 7, 'Test employee', True, now - 100)
    store.configure_watch(-100123, 7, True, now - 100)
    store.record(-100123, 1, 7, now - 1, 'original <text>', 'text')
    store.record(-100123, 1, 7, now - 1, 'original <text>', 'text')
    service = Collector(Client(), store)
    service.state = 'connected'
    runner = web.AppRunner(create_app(service, 'test-only-api-token-12345678901234567890'), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    print(json.dumps({'port': site._server.sockets[0].getsockname()[1]}), flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        str(python),
        "-c",
        script,
        str(tmp_path / "bridge.sqlite3"),
        cwd=collector,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        port = json.loads(line)["port"]
        plugin.config = {"api_token": "test-only-api-token-12345678901234567890"}
        plugin.api_url = f"http://127.0.0.1:{port}"
        plugin.session = aiohttp.ClientSession()
        del plugin.api
        now = datetime.now(TZ)
        await plugin.report(bot, "hour", now - timedelta(minutes=1), now, now)
        assert any(
            "Test employee" in call.kwargs["text"]
            and "本期 1 条 · 📊 当日 1 条 · 👥 1 个群" in call.kwargs["text"]
            for call in bot.send_message.await_args_list
        )
        bot.send_message.reset_mock()
        await plugin.records(bot, 7, now.strftime("%Y%m%d"), 7, -100123, 0)
        assert any(
            r"original <text\>" in call.kwargs["text"]
            for call in bot.send_message.await_args_list
        )
        await plugin.terminate()
        assert process.returncode is None
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{plugin.api_url}/v1/status",
                headers={"Authorization": f"Bearer {plugin.config['api_token']}"},
            ) as response:
                assert response.status == 200
    finally:
        await plugin.terminate()
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 10)


async def test_select_group_then_username_creates_only_scoped_watch(plugin, bot):
    async def api(resource, params=None, data=None):
        if resource == "groups" and data is None:
            return {"items": [{"id": -100123, "name": "Selected group", "enabled": 1}]}
        if resource == "resolve":
            assert params == {"user": "@alice"}
            return {"id": 99, "name": "Alice"}
        if resource == "people" and data is None:
            return {"items": []}
        return {"ok": True}

    plugin.api = AsyncMock(side_effect=api)
    query = SimpleNamespace(data="tw:addg:-100123", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert plugin.pending[7]["group"] == -100123
    plugin.dispatch = AsyncMock()
    update.effective_message = SimpleNamespace(text="@alice Display name")
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    assert not any(c.args[0] == "watches" for c in plugin.api.await_args_list)
    query.data = "tw:startwatch:-100123:99:now"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_any_await(
        "watches", data={"group": -100123, "person": 99, "enabled": True}
    )
    assert 7 not in plugin.pending
    plugin.dispatch.assert_awaited_once_with(bot, 7, ["watches", "-100123"])


async def test_expired_cancelled_and_unrelated_input_are_not_enrollment(plugin, bot):
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
        effective_message=SimpleNamespace(text="@alice"),
    )
    await plugin.input_person(update, SimpleNamespace(bot=bot))
    plugin.api.assert_not_called()
    plugin.pending[7] = {"group": -123, "expires": 0}
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    assert not plugin.pending
    plugin.api.assert_not_called()
    plugin.pending[7] = {"group": -123, "expires": 10**12}
    update.effective_message.text = "/cancel"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.command(update, SimpleNamespace(bot=bot, args=[]))
    assert not plugin.pending


async def test_person_input_rechecks_administrator_and_retains_retry(plugin, bot):
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=99, type="private"),
        effective_user=SimpleNamespace(id=99),
        effective_message=SimpleNamespace(text="@alice"),
    )
    plugin.pending[99] = {"group": -123, "expires": 10**12}
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    plugin.api.assert_not_called()
    update.effective_chat.id = update.effective_user.id = 7
    plugin.pending[7] = {"group": -123, "expires": 10**12}
    plugin.api.side_effect = RuntimeError("用户名暂不可解析")
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    assert 7 in plugin.pending
    assert "用户名暂不可解析" in bot.send_message.call_args.kwargs["text"]


async def test_pause_button_and_effective_status(plugin, bot):
    plugin.api.return_value = {"paused": True}
    query = SimpleNamespace(data="tw:pause:1", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_awaited_once_with("control", data={"paused": True})
    assert "已暂停全部监控" in bot.send_message.call_args.kwargs["text"]
    plugin.api.return_value = {
        "state": "connected",
        "paused": True,
        "active_watches": 0,
        "retry_at": None,
        "groups": [
            {
                "id": -123,
                "name": "Group",
                "enabled": 1,
                "error": None,
                "stale": False,
                "last_sync": None,
                "last_message": None,
                "today_count": 2,
                "watch_count": 1,
            }
        ],
        "gaps": [],
    }
    await plugin.dispatch(bot, 7, ["status", "account"])
    text = bot.send_message.call_args.kwargs["text"]
    assert "已暂停" in text and "在线" in text and "今日消息" not in text
    await plugin.dispatch(bot, 7, ["status", "groups", "-123"])
    text = bot.send_message.call_args.kwargs["text"]
    assert "全部暂停" in text and "今日消息：2 条" in text


async def test_watch_list_paginates_paused_people(plugin, bot):
    items = [
        {
            "chat_id": -123,
            "user_id": i,
            "person_name": f"User {i}",
            "enabled": 1,
            "person_enabled": 0,
            "group_enabled": 1,
        }
        for i in range(1, 51)
    ]
    plugin.api.side_effect = [{"items": items}, {"paused": False}]
    await plugin.dispatch(bot, 7, ["watches", "-123"])
    kwargs = bot.send_message.call_args.kwargs
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert len(buttons) < 100
    assert any(b.callback_data == "tw:menu:watches:-123:1" for b in buttons)
    assert "User 12｜" in kwargs["text"] and "User 13｜" not in kwargs["text"]


async def test_group_management_is_silent(plugin, bot):
    update = SimpleNamespace(
        effective_message=SimpleNamespace(text="/tgwatch"),
        effective_chat=SimpleNamespace(id=-123, type="group"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.command(update, SimpleNamespace(bot=bot, args=[]))
    bot.send_message.assert_not_awaited()


async def test_dedicated_bot_stops_unrelated_plugins(plugin):
    with pytest.raises(ApplicationHandlerStop):
        await plugin.stop_other_plugins(None, None)


async def test_wangshangliao_platform_filter():
    from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterTypeFilter

    restriction = PlatformAdapterTypeFilter("wangshangliao")
    for platform, allowed in [("telegram", False), ("wangshangliao", True)]:
        event = SimpleNamespace(get_platform_name=lambda: platform)
        assert restriction.filter(event, {}) is allowed


@pytest.mark.parametrize(
    "value", ["t.me/alice", "https://t.me/alice", "https://telegram.me/alice/"]
)
async def test_profile_links_resolve_as_usernames(plugin, bot, value):
    plugin.pending[7] = {"group": -123, "expires": 10**12}
    plugin.api.side_effect = [
        {"id": 9, "name": "Alice"},
        {"items": []},
        {"ok": True},
        {"ok": True},
    ]
    plugin.dispatch = AsyncMock()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
        effective_message=SimpleNamespace(text=value),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    plugin.api.assert_any_await("resolve", {"user": "@alice"})


@pytest.mark.parametrize(
    "user,chat,action,allowed",
    [
        (7, -100777, "records:20260920:0:0:0", True),
        (99, -100777, "records:20260920:0:0:0", False),
        (7, -999, "records:20260920:0:0:0", False),
        (7, -100777, "pause:1", False),
        (7, -100777, "status:0", True),
    ],
)
async def test_work_group_callbacks_are_read_only_and_authorized(
    plugin, bot, user, chat, action, allowed
):
    plugin.records = AsyncMock()
    plugin.monitor_status = AsyncMock()
    query = SimpleNamespace(data="tw:" + action, answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=chat, type="group"),
        effective_user=SimpleNamespace(id=user),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert (
        bool(plugin.records.await_count + plugin.monitor_status.await_count) == allowed
    )
    plugin.api.assert_not_called()


async def test_status_pagination_and_paused_people(plugin, bot):
    plugin.api.return_value = {
        "people": [{"id": i, "name": f"人员{i}", "enabled": True} for i in range(11)],
        "paused": True,
    }
    await plugin.monitor_status(bot, 7, 0)
    message = bot.send_message.call_args.kwargs
    assert "人员0" in message["text"] and "人员10" not in message["text"]
    assert "全部监控已暂停" in message["text"]
    assert any(
        b.callback_data == "tw:status:1"
        for row in message["reply_markup"].inline_keyboard
        for b in row
    )
    await plugin.monitor_status(bot, 7, 1)
    assert "人员10" in bot.send_message.call_args.kwargs["text"]


async def test_scheduler_never_sends_health_notifications(plugin, bot):
    now = datetime(2026, 9, 20, 14, 10, tzinfo=TZ)
    plugin.last_hour = now.replace(minute=0)
    plugin.report = AsyncMock()
    plugin.api.return_value = {"state": "login_required", "groups": []}
    await plugin.tick(bot, now)
    bot.send_message.assert_not_awaited()


async def test_person_status_mixed_groups_and_account(plugin):
    health = {
        "state": "connected",
        "people": [{"id": 7, "enabled": True}],
        "watches": [
            {
                "user_id": 7,
                "chat_id": i,
                "enabled": i != 3,
                "group_enabled": True,
                "person_enabled": True,
            }
            for i in range(1, 4)
        ],
        "groups": [
            {"id": 1, "error": None, "stale": False},
            {"id": 2, "error": "unavailable", "stale": True},
        ],
    }
    assert "1 个群监控中 · 1 个群待同步" in plugin.person_status(7, health)
    health["state"] = "login_required"
    assert "待登录" in plugin.person_status(7, health)
    health["paused"] = True
    assert "全部监控已暂停" in plugin.person_status(7, health)


async def test_report_includes_disabled_people_and_group_record_button(plugin, bot):
    plugin.api.return_value["health"].update(
        people=[{"id": 9, "name": "Bob", "enabled": False}]
    )
    now = datetime(2026, 9, 20, 14, tzinfo=TZ)
    await plugin.report(bot, "hour", now - timedelta(hours=1), now, now)
    message = bot.send_message.call_args.kwargs
    assert "Bob" in message["text"] and "人员已暂停" in message["text"]
    buttons = [b for row in message["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data == "tw:records:20260920:0:0:0" for b in buttons)
    assert all(b.url is None for b in buttons)


async def test_iteration_failure_logs_without_notifying_admins(plugin, bot):
    from unittest.mock import patch

    application = SimpleNamespace(running=True, bot=bot)
    plugin.application = application
    plugin.menu_application = application
    plugin.config = {"platform_id": "test"}
    plugin.context = SimpleNamespace(
        get_platform_inst=lambda _: SimpleNamespace(application=application)
    )
    plugin.tick = AsyncMock(side_effect=RuntimeError("Service unavailable"))
    with patch(
        "data.plugins.astrbot_plugin_tgwatch.main.asyncio.sleep",
        side_effect=asyncio.CancelledError,
    ):
        with pytest.raises(asyncio.CancelledError):
            await plugin.run()
    bot.send_message.assert_not_awaited()


async def test_group_records_translate_media_and_escape_original(plugin, bot):
    plugin.api.return_value = {
        "health": {"incomplete": False},
        "has_next": False,
        "items": [
            {
                "sent": "2026-09-20T04:00:00+00:00",
                "deleted": False,
                "text": "*[link](https://example.org)*",
                "reply_to": None,
                "message_id": 1,
                "person_name": "Alice",
                "group_name": "Test",
                "media": "text",
            }
        ],
    }
    await plugin.records(bot, 7, "20260920", 0, 0, 0)
    message = bot.send_message.call_args.kwargs
    assert message["chat_id"] == 7
    assert "【文字】" in message["text"] and "[text]" not in message["text"]
    assert r"\*\[link\]\(https://example\.org\)\*" in message["text"]


async def test_member_selection_pages_and_enrollment(plugin, bot):
    members = {
        "items": [
            {"id": 9, "name": "Alice", "username": "alice", "selectable": True},
            {"id": 10, "name": "Bot", "selectable": False},
        ],
        "has_next": True,
    }
    plugin.api.return_value = members
    query = SimpleNamespace(data="tw:members:-123:1", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    buttons = [
        b
        for row in bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    callbacks = [b.callback_data for b in buttons]
    assert "tw:members:-123:0" in callbacks and "tw:members:-123:2" in callbacks
    assert "tw:member:-123:1:9" in callbacks and "tw:member:-123:1:10" not in callbacks
    assert all(len(value.encode()) <= 64 for value in callbacks)
    plugin.api.side_effect = [members, {"items": []}, {"ok": True}, {"ok": True}]
    plugin.dispatch = AsyncMock()
    query.data = "tw:member:-123:1:9"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert not any(c.args[0] == "watches" for c in plugin.api.await_args_list)
    query.data = "tw:startwatch:-123:9:now"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_any_await(
        "watches", data={"group": -123, "person": 9, "enabled": True}
    )


@pytest.mark.parametrize(
    "user,chat_type,action",
    [
        (99, "private", "members:-123:0"),
        (7, "group", "members:-123:0"),
        (7, "group", "member:-123:0:9"),
    ],
)
async def test_member_buttons_recheck_admin_and_private_scope(
    plugin, bot, user, chat_type, action
):
    query = SimpleNamespace(data="tw:" + action, answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(
            id=plugin.work_chat if chat_type == "group" else user, type=chat_type
        ),
        effective_user=SimpleNamespace(id=user),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_not_called()


@pytest.mark.parametrize("state", ["joined", "pending", "failed"])
async def test_join_input_is_consumed_without_automatic_monitoring(plugin, bot, state):
    plugin.pending[7] = {"mode": "join", "expires": 10**12}
    if state == "failed":
        plugin.api.side_effect = RuntimeError("Result uncertain")
    else:
        plugin.api.return_value = {"status": state, "id": -123, "name": "Group"}
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
        effective_message=SimpleNamespace(text="https://t.me/testgroup"),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    assert 7 not in plugin.pending
    plugin.api.assert_awaited_once_with(
        "join", data={"link": "https://t.me/testgroup", "prepare": True}
    )
    await plugin.input_person(update, SimpleNamespace(bot=bot))
    assert plugin.api.await_count == 1


async def test_join_button_rejected_in_work_group(plugin, bot):
    query = SimpleNamespace(data="tw:join", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=plugin.work_chat, type="group"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert not plugin.pending
    plugin.api.assert_not_called()


@pytest.mark.parametrize("action", ["rules:0", "ruleedit:7", "ruletoggle:7"])
async def test_rule_management_rejected_in_group(plugin, bot, action):
    query = SimpleNamespace(data="tw:" + action, answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=plugin.work_chat, type="group"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_not_called()


async def test_regex_input_preserves_backslashes(plugin, bot):
    plugin.pending[7] = {"mode": "rule", "person": 7, "expires": 10**12}
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
        effective_message=SimpleNamespace(text=r"https://t\.me/test/?.*"),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    plugin.api.assert_awaited_once_with(
        "link_rules",
        data={"person": 7, "patterns": [r"https://t\.me/test/?.*"], "enabled": True},
    )


async def test_rule_menu_and_group_violation_details(plugin, bot):
    await plugin.dispatch(bot, 7, [])
    callbacks = [
        b.callback_data
        for row in bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "tw:rules:0" in callbacks and "tw:violations:0:0" in callbacks
    plugin.api.return_value = {
        "items": [
            {
                "name": "Alice",
                "sent": "2026-09-20T04:00:00+00:00",
                "group": "Group",
                "message_id": 1,
                "links": ["https://bad.example/*"],
            }
        ],
        "next": 15,
        "rule_count": 1,
        "health": {"incomplete": False},
    }
    query = SimpleNamespace(data="tw:violations:0:0", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=plugin.work_chat, type="group"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    result = bot.send_message.call_args.kwargs
    assert result["chat_id"] == 7 and "违规链接细节" in result["text"]
    assert (
        result["reply_markup"].inline_keyboard[0][0].callback_data
        == "tw:violations:0:15"
    )


async def test_only_primary_admin_can_delegate_and_revoke(plugin, bot):
    plugin.admins.add(9)
    query = SimpleNamespace(data="tw:adminadd", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=9, type="private"),
        effective_user=SimpleNamespace(id=9),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert not plugin.pending
    update.effective_chat.id = update.effective_user.id = 7
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.return_value = {"id": 11, "name": "New admin"}
    update.effective_message = SimpleNamespace(text="11")
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    assert 11 in plugin.admins and 11 in plugin.saved[-1]["managed_admins"]
    query.data = "tw:adminremove:11"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert 11 not in plugin.admins and 11 not in plugin.saved[-1]["managed_admins"]
    query.data = "tw:adminremove:7"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert 7 in plugin.admins


async def test_private_details_never_sent_to_group(plugin, bot):
    await plugin.send_text(bot, plugin.work_chat, "Sensitive data", allow_group=True)
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [BadRequest("Query is too old"), NetworkError("Network unavailable")]
)
async def test_admin_add_continues_after_acknowledgement_failure(plugin, bot, error):
    query = SimpleNamespace(data="tw:adminadd", answer=AsyncMock(side_effect=error))
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert plugin.pending[7]["mode"] == "admin"
    assert "请输入对方数字 ID" in bot.send_message.call_args.kwargs["text"]


async def test_missed_hours_persist_and_do_not_replay(plugin, bot):
    now = datetime(2026, 9, 20, 16, 10, tzinfo=TZ)
    plugin.last_hour = now.replace(hour=14, minute=0)
    plugin.report = AsyncMock()
    await plugin.tick(bot, now)
    assert sum(r["status"] == "missed" for r in plugin.state["reports"].values()) == 2
    assert plugin.state["last_hour"] == now.replace(minute=0).isoformat()
    assert all(call.args[1] == "day" for call in plugin.report.call_args_list)


async def test_work_group_unset_keeps_scheduler_silent(plugin, bot):
    plugin.work_chat = 0
    plugin.report = AsyncMock()
    await plugin.tick(bot, datetime.now(TZ))
    plugin.report.assert_not_called()


async def test_admin_changes_persist_visible_configuration(plugin):
    config = Mock()
    config.save_config_async = AsyncMock(return_value=True)
    plugin.config = config
    await plugin.save_admins({7, 9})
    config.save_config_async.assert_awaited_once_with({"admin_ids": ["7", "9"]})
    assert plugin.admins == {7, 9}


async def test_dedicated_adapter_stays_silent_without_plugin():
    from astrbot.core.platform.sources.telegram.tg_adapter import (
        TelegramPlatformAdapter,
    )

    adapter = SimpleNamespace(
        config={"telegram_dedicated_reporting": True},
        convert_message=AsyncMock(),
        handle_msg=AsyncMock(),
    )
    await TelegramPlatformAdapter.message_handler(
        adapter, SimpleNamespace(message=None), None
    )
    adapter.convert_message.assert_not_called()
    assert TelegramPlatformAdapter.collect_commands(adapter) == []


async def test_setup_menu_checks_services_without_sending_group_message(plugin, bot):
    plugin.context = SimpleNamespace(
        get_platform_inst=lambda _: SimpleNamespace(
            config={"telegram_dedicated_reporting": True}
        )
    )
    plugin.config = {"platform_id": "test"}
    plugin.work_chat = 0
    plugin.api.side_effect = [
        {"state": "connected", "active_watches": 0},
        {"items": []},
    ]
    update = SimpleNamespace(
        callback_query=SimpleNamespace(data="tw:setup", answer=AsyncMock()),
        effective_chat=SimpleNamespace(type="private", id=7),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    message = bot.send_message.call_args.kwargs
    assert "暂无允许发榜的群" in message["text"]
    assert any(
        b.callback_data == "tw:work"
        for row in message["reply_markup"].inline_keyboard
        for b in row
    )


async def test_callback_edits_original_and_paginates_without_new_messages(plugin, bot):
    async def render(update, context):
        await plugin.send_text(bot, 7, "Header\n" + "内容" * 1200)
        raise ApplicationHandlerStop

    plugin.handle_callback = AsyncMock(side_effect=render)
    query = SimpleNamespace(
        data="tw:menu",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(message_id=101),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.callback(update, SimpleNamespace(bot=bot))
    bot.send_message.assert_not_awaited()
    assert query.edit_message_text.await_count == 1
    assert (
        query.edit_message_text.call_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data
        == "tw:panel:1"
    )
    query.data = "tw:panel:1"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.callback(update, SimpleNamespace(bot=bot))
    assert query.edit_message_text.await_count == 2
    assert plugin.handle_callback.await_count == 1


async def test_group_button_does_not_publish_details_or_send_new_message(plugin, bot):
    query = SimpleNamespace(data="tw:records:20260920:0:0:0", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=plugin.work_chat, type="group"),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.callback(update, SimpleNamespace(bot=bot))
    bot.send_message.assert_not_awaited()
    plugin.api.assert_not_called()


async def test_work_notifications_disabled(plugin, bot):
    plugin.config["reports_enabled"] = False
    plugin.report = AsyncMock()
    await plugin.tick(bot, datetime.now(TZ))
    plugin.report.assert_not_called()


async def test_membership_discovery_does_not_require_inviter_admin(plugin, bot):
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            chat=SimpleNamespace(id=-888, type="supergroup", title="New work group"),
            new_chat_member=SimpleNamespace(status="member"),
        )
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.stop_other_plugins(update, SimpleNamespace(bot=bot))
    assert plugin.state["work_groups"]["-888"] == "New work group"
    assert plugin.work_chat == -100777
    assert plugin.admins == {7}
    bot.send_message.assert_not_called()
    update.my_chat_member.new_chat_member.status = "left"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.stop_other_plugins(update, SimpleNamespace(bot=bot))
    assert "-888" not in plugin.state["work_groups"]


async def test_group_update_recovers_missed_membership(plugin, bot):
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(
            id=-888, type="supergroup", title="Recovered group"
        )
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.stop_other_plugins(update, SimpleNamespace(bot=bot))
    assert plugin.state["work_groups"]["-888"] == "Recovered group"
    bot.send_message.assert_not_called()


async def test_multiple_work_groups_deliver_independently(plugin, bot):
    now = datetime(2026, 9, 20, 15, tzinfo=TZ)
    plugin.last_hour = now - timedelta(hours=1)
    plugin.state["work_group_enabled"] = {"-11": True, "-22": True, "-33": False}
    plugin.state["work_group_hours"] = {
        key: plugin.last_hour.isoformat() for key in ("-11", "-22", "-33")
    }
    await plugin.tick(bot, now)
    assert [c.kwargs["chat_id"] for c in bot.send_message.await_args_list] == [
        -11,
        -11,
        -22,
        -22,
    ]
    assert len(plugin.state["reports"]) == 4
    await plugin.tick(bot, now)
    assert bot.send_message.await_count == 4


async def test_uncertain_group_does_not_block_other_group(plugin, bot):
    now = datetime(2026, 9, 20, 15, tzinfo=TZ)
    plugin.last_hour = now - timedelta(hours=1)
    plugin.state["work_group_enabled"] = {"-11": True, "-22": True}
    plugin.state["work_group_hours"] = {
        k: plugin.last_hour.isoformat() for k in ("-11", "-22")
    }

    async def send(**kwargs):
        if kwargs["chat_id"] == -11:
            raise NetworkError("Uncertain")

    bot.send_message.side_effect = send
    await plugin.tick(bot, now)
    reports = plugin.state["reports"]
    assert all(
        v["status"] == "uncertain" for k, v in reports.items() if k.startswith("-11:")
    )
    assert all(
        v["status"] == "sent" for k, v in reports.items() if k.startswith("-22:")
    )


async def test_work_group_toggle_is_per_group(plugin, bot):
    plugin.state["work_groups"] = {"-11": "One", "-22": "Two"}
    plugin.state["work_group_enabled"] = {"-11": True, "-22": False}
    bot.id = 77
    bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(
            type="group",
            title="Two",
            permissions=SimpleNamespace(can_send_messages=True),
        )
    )
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    update = SimpleNamespace(
        callback_query=SimpleNamespace(data="tw:workselect:-22", answer=AsyncMock()),
        effective_chat=SimpleNamespace(type="private", id=7),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert plugin.state["work_group_enabled"] == {"-11": True, "-22": True}
    confirmations = [
        c for c in bot.send_message.call_args_list if c.kwargs["chat_id"] == -22
    ]
    assert len(confirmations) == 1
    assert "启用成功" in confirmations[0].kwargs["text"]
    assert "当前群" not in bot.send_message.call_args.kwargs["text"]
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert plugin.state["work_group_enabled"] == {"-11": True, "-22": False}
    assert (
        len([c for c in bot.send_message.call_args_list if c.kwargs["chat_id"] == -22])
        == 1
    )


async def test_refresh_removes_left_group_not_transient_error(plugin, bot):
    plugin.state["work_groups"] = {"-11": "Left", "-22": "Unknown"}
    plugin.state["work_group_enabled"] = {"-11": True, "-22": True}
    plugin.api.return_value = {"items": []}
    bot.id = 77
    bot.get_chat_member = AsyncMock(
        side_effect=[SimpleNamespace(status="left"), NetworkError("Temporary")]
    )
    update = SimpleNamespace(
        callback_query=SimpleNamespace(data="tw:work", answer=AsyncMock()),
        effective_chat=SimpleNamespace(type="private", id=7),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_not_called()
    assert plugin.state["work_groups"] == {"-22": "Unknown"}
    assert plugin.state["work_group_enabled"] == {"-22": True}


async def test_status_menu_is_categorized_and_available_without_collector(plugin, bot):
    plugin.api.side_effect = RuntimeError("Unavailable")
    await plugin.dispatch(bot, 7, ["status"])
    plugin.api.assert_not_called()
    message = bot.send_message.call_args.kwargs
    callbacks = [
        b.callback_data for row in message["reply_markup"].inline_keyboard for b in row
    ]
    assert all(
        f"tw:menu:status:{name}" in callbacks
        for name in ("account", "work", "groups", "people", "reports", "gaps")
    )
    assert "最后同步" not in message["text"]
    await plugin.dispatch(bot, 7, ["status", "work"])
    plugin.api.assert_not_called()


async def test_menu_registration_failure_does_not_block_reports(plugin, monkeypatch):
    application = SimpleNamespace(
        running=True,
        bot=SimpleNamespace(
            set_my_commands=AsyncMock(side_effect=NetworkError("Sensitive detail"))
        ),
    )
    plugin.application = application
    plugin.context = SimpleNamespace(
        get_platform_inst=lambda _: SimpleNamespace(application=application)
    )
    plugin.config = {"platform_id": "test"}
    plugin.tick = AsyncMock()

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await plugin.run()
    plugin.tick.assert_awaited_once()
    assert plugin.menu_retry_at > 0
    with pytest.raises(asyncio.CancelledError):
        await plugin.run()
    assert plugin.tick.await_count == 2
    application.bot.set_my_commands.assert_awaited_once()


async def test_restart_preserves_sent_uncertain_and_skips_expired_hours(
    plugin, bot, tmp_path
):
    import json

    now = datetime(2026, 9, 20, 15, tzinfo=TZ)
    yesterday = (now - timedelta(days=1)).replace(hour=0)
    plugin.config = {
        "enabled": True,
        "admin_ids": [7],
        "work_chat_id": "-11",
        "platform_id": "test",
        "api_url": "http://127.0.0.1:6190",
        "api_token": "x" * 32,
    }
    state = {
        "admins_config_synced": True,
        "work_delivery_migrated": True,
        "last_hour": now.isoformat(),
        "work_groups": {"-11": "A", "-22": "B"},
        "work_group_enabled": {"-11": True, "-22": True},
        "work_group_hours": {
            k: (now - timedelta(hours=2)).isoformat() for k in ("-11", "-22")
        },
        "reports": {
            f"-11:day:{yesterday.isoformat()}": {
                "status": "sent",
                "created": now.timestamp(),
            },
            f"-22:day:{yesterday.isoformat()}": {
                "status": "sending",
                "created": now.timestamp(),
            },
        },
    }
    saved = tmp_path / "delivery.json"
    saved.write_text(json.dumps(state))
    plugin.get_kv_data = AsyncMock(side_effect=lambda *_: json.loads(saved.read_text()))
    plugin.run = AsyncMock()
    await plugin.initialize()
    try:
        await plugin.tick(bot, now + timedelta(minutes=10))
        bot.send_message.assert_not_called()
        assert (
            plugin.state["reports"][f"-22:day:{yesterday.isoformat()}"]["status"]
            == "uncertain"
        )
        assert (
            sum(v["status"] == "missed" for v in plugin.state["reports"].values()) == 4
        )
    finally:
        await plugin.terminate()


async def test_person_delete_requires_confirmation_and_clears_pending(plugin, bot):
    plugin.api.return_value = {"items": [{"id": 7, "name": "Alice"}]}
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data="tw:persondelete:7:-123", answer=AsyncMock()
        ),
        effective_chat=SimpleNamespace(type="private", id=7),
        effective_user=SimpleNamespace(id=7),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_awaited_once_with("people")
    plugin.state["reports"]["old"] = {"status": "pending", "parts": ["old text"]}
    update.callback_query.data = "tw:personremove:7:-123"
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_awaited_with("remove_person", data={"person": 7, "group": -123})
    assert plugin.state["reports"]["old"]["status"] == "missed"
    assert "parts" not in plugin.state["reports"]["old"]


async def test_ranking_excludes_unassigned_people_with_old_counts(plugin, bot):
    plugin.api.return_value = {
        "items": [
            {
                "id": 7,
                "name": "Removed person",
                "count": 0,
                "cumulative": 2,
                "groups": 1,
            }
        ],
        "health": {
            "incomplete": False,
            "watches": [
                {
                    "user_id": 8,
                    "enabled": False,
                    "group_enabled": True,
                    "person_enabled": True,
                }
            ],
            "people": [
                {"id": 7, "name": "Removed person", "enabled": True},
                {"id": 8, "name": "Paused person", "enabled": True},
            ],
        },
    }
    now = datetime(2026, 9, 21, 1, tzinfo=TZ)
    await plugin.report(bot, "hour", now - timedelta(hours=1), now, now)
    text = bot.send_message.call_args.kwargs["text"]
    assert "Removed person" not in text
    assert "Paused person" in text
    assert "第 1 名" in text


async def test_private_invite_preparation_requires_button_confirmation(plugin, bot):
    plugin.pending[7] = {"mode": "join", "expires": 10**12}
    plugin.api.return_value = {"status": "join_required"}
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
        effective_message=SimpleNamespace(text="https://t.me/+testhash"),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    assert plugin.pending[7]["mode"] == "confirm_join"
    plugin.api.assert_awaited_once_with(
        "join", data={"link": "https://t.me/+testhash", "prepare": True}
    )


async def test_join_confirmation_rechecks_admin(plugin, bot):
    plugin.pending[77] = {
        "mode": "confirm_join",
        "expires": 10**12,
        "link": "t.me/+testhash",
    }
    query = SimpleNamespace(data="tw:confirmjoin", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=77, type="private"),
        effective_user=SimpleNamespace(id=77),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    plugin.api.assert_not_called()


@pytest.mark.parametrize("choice", ["today", "day"])
async def test_start_time_buttons_use_beijing_and_utc(plugin, bot, choice):
    query = SimpleNamespace(data=f"tw:startwatch:-123:9:{choice}", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=7, type="private"),
        effective_user=SimpleNamespace(id=7),
    )
    plugin.dispatch = AsyncMock()
    before = datetime.now(TZ)
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    data = plugin.api.call_args.kwargs["data"]
    start = datetime.fromisoformat(data["start"]).astimezone(TZ)
    assert data["group"] == -123 and data["person"] == 9
    if choice == "today":
        assert start.hour == start.minute == start.second == 0
    else:
        assert abs((start - (before - timedelta(days=1))).total_seconds()) < 2


async def test_custom_backfill_date_and_permissions(plugin, bot):
    query = SimpleNamespace(data="tw:startwatch:-123:9:custom", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=8, type="private"),
        effective_user=SimpleNamespace(id=8),
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert not plugin.pending
    plugin.api.assert_not_awaited()
    update.effective_chat.id = update.effective_user.id = 7
    with pytest.raises(ApplicationHandlerStop):
        await plugin.handle_callback(update, SimpleNamespace(bot=bot))
    assert plugin.pending[7]["mode"] == "history_time"
    plugin.dispatch = AsyncMock()
    start = (datetime.now(TZ) - timedelta(days=2)).replace(second=0, microsecond=0)
    update.effective_message = SimpleNamespace(text=start.strftime("%Y-%m-%d %H:%M"))
    with pytest.raises(ApplicationHandlerStop):
        await plugin.input_person(update, SimpleNamespace(bot=bot))
    assert (
        datetime.fromisoformat(plugin.api.call_args.kwargs["data"]["start"]).astimezone(
            TZ
        )
        == start
    )
    assert 7 not in plugin.pending
