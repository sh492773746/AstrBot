from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import main
from astrbot.core.platform.sources.wangshangliao import event as native_event
from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("admin", [True, False])
async def test_builtin_help_native_response_is_scoped(monkeypatch, private, admin):
    from astrbot.builtin_stars.builtin_commands.commands.help import HelpCommand
    from astrbot.builtin_stars.wangshangliao_moderation.commands import (
        HELP,
    )

    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: private,
        is_admin=lambda: admin,
        get_extra=lambda _: "private_commands",
        set_extra=Mock(),
        send=AsyncMock(),
        plain_result=lambda text: text,
        stop_event=Mock(),
    )
    event.handle_builtin_help = lambda: (
        native_event.WangshangliaoEvent.handle_builtin_help(event)
    )
    monkeypatch.setattr(runtime, "service", SimpleNamespace(active=True))
    monkeypatch.setattr(
        runtime, "help_responder", lambda event: main.Main.moderation_help(None, event)
    )
    await HelpCommand(None).help(event)
    if private:
        event.send.assert_awaited_once_with(HELP if admin else "无权限")
    else:
        event.send.assert_not_awaited()
    assert event.stop_event.called


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_native_help_is_silent_without_an_active_registered_handler(
    monkeypatch, active
):
    from astrbot.builtin_stars.builtin_commands.commands.help import HelpCommand

    responder = AsyncMock()
    monkeypatch.setattr(runtime, "service", SimpleNamespace(active=active))
    monkeypatch.setattr(runtime, "help_responder", responder if not active else None)
    event = SimpleNamespace(stop_event=Mock())
    event.handle_builtin_help = lambda: (
        native_event.WangshangliaoEvent.handle_builtin_help(event)
    )
    command = HelpCommand(None)
    command._query_astrbot_notice = AsyncMock()
    await command.help(event)
    responder.assert_not_awaited()
    command._query_astrbot_notice.assert_not_awaited()
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_native_help_handler_failure_stops_without_generic_fallback(monkeypatch):
    from astrbot.builtin_stars.builtin_commands.commands.help import HelpCommand

    monkeypatch.setattr(runtime, "service", SimpleNamespace(active=True))
    monkeypatch.setattr(runtime, "help_responder", AsyncMock(side_effect=RuntimeError))
    event = SimpleNamespace(stop_event=Mock())
    event.handle_builtin_help = lambda: (
        native_event.WangshangliaoEvent.handle_builtin_help(event)
    )
    command = HelpCommand(None)
    command._query_astrbot_notice = AsyncMock()
    with pytest.raises(RuntimeError):
        await command.help(event)
    command._query_astrbot_notice.assert_not_awaited()
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("admin", [True, False])
async def test_plugin_help_only_replies_in_private(private, admin):
    from astrbot.builtin_stars.wangshangliao_moderation.commands import HELP

    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: private,
        is_admin=lambda: admin,
        get_extra=lambda _: None,
        set_extra=Mock(),
        send=AsyncMock(),
        plain_result=lambda text: text,
        stop_event=Mock(),
    )
    await main.Main.moderation_help(None, event)
    if private:
        event.send.assert_awaited_once_with(HELP if admin else "无权限")
    else:
        event.send.assert_not_awaited()
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,private,managed,payload,consumed,called",
    [
        ("wangshangliao", False, False, {"text": "test"}, True, True),
        ("wangshangliao", False, False, {"text": "test"}, False, True),
        ("wangshangliao", False, True, {"text": "test"}, True, False),
        ("wangshangliao", True, False, {"text": "test"}, True, False),
        ("telegram", False, False, {"text": "test"}, True, False),
        ("wangshangliao", False, False, None, True, False),
    ],
)
async def test_plugin_entry_filters(
    monkeypatch, platform, private, managed, payload, consumed, called
):
    if payload:
        payload = {**payload, "created_at": main.time.time() * 1000}
    handler = AsyncMock(return_value=consumed)
    monkeypatch.setattr(main, "handle", handler)
    monkeypatch.setattr(native_event, "is_managed_account", lambda _: managed)
    event = SimpleNamespace(
        get_platform_name=lambda: platform,
        is_private_chat=lambda: private,
        is_admin=lambda: False,
        get_sender_id=lambda: "2",
        get_group_id=lambda: "5",
        get_extra=lambda _: payload,
        platform=SimpleNamespace(account="1", config={}),
        set_extra=Mock(),
        message_obj=SimpleNamespace(message_id="stable-message"),
        stop_event=Mock(),
    )
    plugin = object.__new__(main.Main)
    plugin.context = SimpleNamespace(get_provider_by_id=lambda _: None)
    await plugin.moderate(event)
    assert handler.await_count == int(called)
    assert event.stop_event.call_count == int(called and consumed)
    if called:
        handler.assert_awaited_once_with(event.platform, "5", "stable-message", payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "forward"])
@pytest.mark.parametrize("consumed", [True, False])
@pytest.mark.parametrize("private", [True, False])
async def test_media_never_falls_back_to_ai(monkeypatch, kind, consumed, private):
    payload = {"kind": kind, "text": "", "created_at": main.time.time() * 1000}
    handler = AsyncMock(return_value=consumed)
    monkeypatch.setattr(main, "handle", handler)
    extra = {"wangshangliao_payload": payload}
    stopped = False

    def stop():
        nonlocal stopped
        stopped = True

    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: private,
        is_admin=lambda: False,
        get_group_id=lambda: "private/2" if private else "5",
        get_extra=lambda key: extra.get(key),
        set_extra=lambda key, value: extra.update({key: value}),
        platform=SimpleNamespace(account="1", config={}),
        message_obj=SimpleNamespace(message_id="800"),
        message_str="",
        stop_event=stop,
        is_stopped=lambda: stopped,
    )
    plugin = object.__new__(main.Main)
    plugin.ai_runtime = SimpleNamespace(customer=AsyncMock())
    await plugin.moderate(event)
    assert stopped
    assert extra["_component_claimed"]
    assert handler.await_count == (0 if private else 1)
    plugin.ai_runtime.customer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin,recognized,command,skipped",
    [
        (True, True, "公告 keyword", True),
        (False, True, "公告 keyword", False),
        (True, False, "公告 keyword", False),
        (True, True, "garbage keyword", False),
    ],
)
async def test_rule_exemption_requires_recognized_admin_command(
    monkeypatch, admin, recognized, command, skipped
):
    handler = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "handle", handler)
    monkeypatch.setattr(native_event, "is_managed_account", lambda _: False)
    extra = {
        "wangshangliao_payload": {"text": "keyword /群管 garbage"},
        "activated_handlers": [
            SimpleNamespace(
                handler=main.Main.moderation_command, handler_full_name="cmd"
            )
        ]
        if recognized
        else [],
        "handlers_parsed_params": {"cmd": {"command": command}},
    }
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: False,
        is_admin=lambda: admin,
        get_extra=extra.get,
        get_sender_id=lambda: "2",
        get_group_id=lambda: "5",
        platform=object(),
        message_obj=SimpleNamespace(message_id="m"),
        stop_event=Mock(),
    )
    await main.Main.moderate(None, event)
    assert handler.await_count == int(not skipped)
