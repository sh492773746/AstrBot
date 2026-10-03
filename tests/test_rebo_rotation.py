"""Exercise scheduler methods with isolated state and fake transport."""

import ast
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup as Keyboard
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

from data.plugins.astrbot_plugin_rebo_live.client import Client, RemoteError


@pytest.fixture
def room_scheduler():
    """Build isolated real scheduler methods with a controlled clock.

    Returns:
        Scheduler using an in-memory room and mock network transport.
    """
    source = Path("data/plugins/astrbot_plugin_rebo_live/main.py").read_text()
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef))
    clock = SimpleNamespace(now=1000.0)
    scope = {
        "OWNER": 1,
        "asyncio": asyncio,
        "json": json,
        "time": SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now),
        "uuid": uuid,
        "aiohttp": aiohttp,
        "RemoteError": RemoteError,
        "logger": logging.getLogger("test"),
    }
    methods = [
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name in {"action", "room_step", "tick"}
    ]
    exec(
        compile(ast.Module(body=methods, type_ignores=[]), "<scheduler>", "exec"),
        scope,
    )
    room = {
        "enabled": True,
        "revision": 1,
        "interval": 50,
        "text": "one",
        "texts": ["one", "two"],
        "cursor": 0,
    }
    obj = SimpleNamespace(
        state={"rooms": {"r": room}},
        status="已登录",
        checked=clock.now,
        visible={"r": {"status": 1}},
        due={"r": 0},
        network=asyncio.Semaphore(1),
        client=SimpleNamespace(
            request=AsyncMock(return_value={}),
            connect=AsyncMock(return_value=AsyncMock()),
            send=AsyncMock(return_value="confirmed-id"),
        ),
        save=lambda *args: None,
        lock=asyncio.Lock(),
        wakeup=asyncio.Event(),
        poll_due=float("inf"),
        poll_task=None,
        workers={},
        clock=clock,
    )
    for method in methods:
        setattr(obj, method.name, MethodType(scope[method.name], obj))
    return obj


@pytest.mark.asyncio
async def test_added_text_has_confirmation_and_full_paged_list():
    tree = ast.parse(Path("data/plugins/astrbot_plugin_rebo_live/main.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    scope = {
        "OWNER": 1,
        "time": time,
        "Button": Button,
        "Keyboard": Keyboard,
        "ApplicationHandlerStop": ApplicationHandlerStop,
        "BadRequest": BadRequest,
        "RemoteError": RemoteError,
        "logger": logging.getLogger("test"),
    }
    method = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "telegram"
    )
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), "<telegram>", "exec"), scope
    )
    room = {"texts": ["first", "second", "second"], "enabled": True, "interval": 50}
    obj = SimpleNamespace(
        pending={1: ("addtext", "r", time.time() + 60)},
        text_revisions={1: 2},
        state={"rooms": {"r": room}, "grants": {}},
        action=AsyncMock(),
        visible={},
        due={},
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    update = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_message=SimpleNamespace(text="second"),
    )
    with pytest.raises(ApplicationHandlerStop):
        await scope["telegram"](obj, update, SimpleNamespace(bot=bot))
    sent = bot.send_message.await_args
    assert "第3条已保存" in sent.args[1] and "与已有文案相同" in sent.args[1]
    assert sent.kwargs["reply_markup"].inline_keyboard[0][1].text == "📝 查看文案（3）"
    room["texts"] = [str(i) * 20 for i in range(7)]
    query = SimpleNamespace(
        data="rb:detail:r:1", answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    update.callback_query = query
    with pytest.raises(ApplicationHandlerStop):
        await scope["telegram"](obj, update, SimpleNamespace(bot=bot))
    rendered = query.edit_message_text.await_args.args[0]
    assert "第6条" in rendered and "第7条" in rendered and "第1条：" not in rendered


@pytest.mark.asyncio
async def test_individual_text_actions_preserve_others_and_reject_stale():
    source = Path("data/plugins/astrbot_plugin_rebo_live/main.py").read_text()
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef))
    method = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "action"
    )
    scope = {"OWNER": 1, "time": time}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<action>", "exec"), scope)
    room = {"text": "one", "text2": "two", "revision": 4, "cursor": 1}
    obj = SimpleNamespace(
        lock=asyncio.Lock(),
        state={"rooms": {"r": room}},
        save=lambda *args: None,
        wakeup=asyncio.Event(),
        due={},
    )
    await scope["action"](
        obj,
        {
            "action": "room",
            "id": "r",
            "text_op": "add",
            "value": "three",
            "revision": 4,
        },
    )
    assert room["texts"] == ["one", "two", "three"] and room["cursor"] == 1
    with pytest.raises(ValueError):
        await scope["action"](
            obj,
            {
                "action": "room",
                "id": "r",
                "text_op": "add",
                "value": "duplicate",
                "revision": 4,
            },
        )
    await scope["action"](
        obj,
        {
            "action": "room",
            "id": "r",
            "text_op": "edit",
            "index": 1,
            "value": "changed",
            "revision": 5,
        },
    )
    assert room["texts"] == ["one", "changed", "three"]
    await scope["action"](
        obj,
        {"action": "room", "id": "r", "text_op": "delete", "index": 0, "revision": 6},
    )
    assert room["texts"] == ["changed", "three"] and room["cursor"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", [2, 7, 3600])
async def test_unknown_send_continues_next_item_without_replay(gap):
    source = Path("data/plugins/astrbot_plugin_rebo_live/main.py").read_text()
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef))
    method = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "room_step"
    )
    scope = {
        "asyncio": asyncio,
        "json": json,
        "time": time,
        "uuid": uuid,
        "aiohttp": aiohttp,
        "RemoteError": RemoteError,
        "logger": logging.getLogger("test"),
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), "<scheduler>", "exec"),
        scope,
    )
    ws = AsyncMock()
    send = AsyncMock(side_effect=[TimeoutError(), "confirmed-id", "third-id"])
    room = {
        "enabled": True,
        "revision": 1,
        "interval": 50,
        "item_interval": gap,
        "text": "one",
        "texts": ["one", "two", "three"],
    }
    obj = SimpleNamespace(
        state={"rooms": {"r": room}},
        status="已登录",
        checked=time.monotonic(),
        visible={"r": {"status": 1}},
        due={"r": 0},
        network=asyncio.Semaphore(1),
        client=SimpleNamespace(
            request=AsyncMock(return_value={}),
            connect=AsyncMock(return_value=ws),
            send=send,
        ),
        save=lambda *args: None,
    )
    await scope["room_step"](obj, "r")
    assert room["enabled"] and room["cursor"] == 1
    assert room["history"][-1]["result"] == "uncertain"
    assert obj.due["r"] > time.monotonic()
    assert gap - 1 < obj.due["r"] - time.monotonic() <= gap
    obj.due["r"] = 0
    await scope["room_step"](obj, "r")
    assert [c.args[1] for c in send.await_args_list] == ["one", "two"]
    assert room["cursor"] == 2 and room["result"] == "confirmed"
    assert gap - 1 < obj.due["r"] - time.monotonic() <= gap
    obj.due["r"] = 0
    await scope["room_step"](obj, "r")
    assert room["cursor"] == 0
    assert 49 < obj.due["r"] - time.monotonic() <= 50


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [2001011, 2010132002])
@pytest.mark.parametrize("endpoint", ["room/info", "room/generate-chat-token"])
async def test_preflight_rejection_bounded_without_sending(
    room_scheduler, code, endpoint
):
    obj = room_scheduler
    room = obj.state["rooms"]["r"]
    # A token failure must not be reset by the preceding successful room lookup.
    failing_method = (
        obj.client.request if endpoint == "room/info" else obj.client.connect
    )
    failing_method.side_effect = RemoteError(code, endpoint=endpoint)
    for count in range(1, 5):
        obj.checked = obj.clock.now
        await obj.room_step("r")
        assert room["enabled"] == (count <= 3)
        assert room["preflight_failures"] == count
        assert room["cursor"] == 0 and "history" not in room
        assert "未提交弹幕" in room["status"]
        if count <= 3:
            delay = (30, 60, 120)[count - 1]
            assert room["retry_at"] == obj.clock.now + delay
            assert obj.due["r"] == 0
            # Persist and reload the room to verify the budget survives restarts.
            obj.state = json.loads(json.dumps(obj.state))
            room = obj.state["rooms"]["r"]
            obj.clock.now += delay
        else:
            assert "r" not in obj.due and "retry_at" not in room
            assert "三次重查后暂停" in room["status"]
    obj.client.send.assert_not_called()
    assert room["last_service_error"]["endpoint"] == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["room/info", "room/generate-chat-token"])
async def test_preflight_recovery_preserves_pending_position(room_scheduler, endpoint):
    obj = room_scheduler
    room = obj.state["rooms"]["r"]
    room["cursor"] = 1
    failing_method = (
        obj.client.request if endpoint == "room/info" else obj.client.connect
    )
    failing_method.side_effect = RemoteError(2010132002, endpoint=endpoint)
    await obj.room_step("r")
    assert room["retry_at"] == 1030 and obj.due["r"] == 0
    obj.clock.now += 30
    obj.checked = obj.clock.now
    failing_method.side_effect = None
    await obj.room_step("r")
    obj.client.send.assert_awaited_once()
    assert obj.client.send.await_args.args[1] == "two"
    assert room["result"] == "confirmed" and room["cursor"] == 0
    assert room["preflight_failures"] == 0 and "retry_at" not in room
    assert obj.due["r"] == obj.clock.now + 50
    assert len(room["history"]) == 1


@pytest.mark.asyncio
async def test_retry_gate_blocks_work_until_persisted_deadline(room_scheduler):
    obj = room_scheduler
    room = obj.state["rooms"]["r"]
    room.update(retry_at=1030, preflight_failures=1)
    obj.room_step = AsyncMock()
    await obj.tick()
    assert not obj.workers
    obj.clock.now = 1029
    await obj.tick()
    assert not obj.workers
    obj.clock.now = 1030
    await obj.tick()
    await obj.workers["r"]
    obj.room_step.assert_awaited_once_with("r")


@pytest.mark.asyncio
async def test_preflight_recovery_does_not_send_before_original_deadline(
    room_scheduler,
):
    obj = room_scheduler
    room = obj.state["rooms"]["r"]
    obj.due["r"] = 1050
    obj.client.request.side_effect = RemoteError(2010132002, endpoint="room/info")
    await obj.room_step("r")
    obj.clock.now = 1030
    obj.client.request.side_effect = None
    await obj.room_step("r")
    assert obj.due["r"] == 1050 and room["preflight_failures"] == 1
    obj.client.connect.assert_not_called()
    obj.client.send.assert_not_called()
    obj.clock.now = 1050
    obj.checked = obj.clock.now
    await obj.room_step("r")
    obj.client.send.assert_awaited_once()
    assert room["preflight_failures"] == 0


@pytest.mark.asyncio
async def test_preflight_login_rejection_requires_fresh_visibility(room_scheduler):
    obj = room_scheduler
    obj.client.request.side_effect = RemoteError(401, endpoint="room/info")
    await obj.room_step("r")
    assert obj.checked == 0 and obj.poll_due == 0 and "r" not in obj.due
    obj.client.send.assert_not_called()


@pytest.mark.asyncio
async def test_manual_enable_resets_exhausted_budget_and_full_interval(room_scheduler):
    obj = room_scheduler
    room = obj.state["rooms"]["r"]
    room.update(enabled=False, preflight_failures=4, retry_at=5000, retry_delay=300)
    await obj.action({"action": "room", "id": "r", "enabled": True})
    assert room["enabled"] and room["preflight_failures"] == 0
    assert "retry_at" not in room and "retry_delay" not in room
    assert "r" not in obj.due
    await obj.room_step("r")
    obj.client.send.assert_not_called()
    assert obj.due["r"] == 1050


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "endpoint"),
    [
        (2010132002, None),
        (2010132002, "other"),
        (403, "room/info"),
        (999999, "room/info"),
    ],
)
async def test_other_preflight_rejections_still_pause(room_scheduler, code, endpoint):
    obj = room_scheduler
    obj.client.request.side_effect = RemoteError(code, endpoint=endpoint)
    await obj.room_step("r")
    room = obj.state["rooms"]["r"]
    assert room["enabled"] is False
    assert "preflight_failures" not in room and "retry_at" not in room
    obj.client.send.assert_not_called()


@pytest.mark.asyncio
async def test_send_rejection_never_enters_preflight_retry(room_scheduler):
    obj = room_scheduler
    obj.client.send.side_effect = RemoteError(2010132002)
    await obj.room_step("r")
    room = obj.state["rooms"]["r"]
    assert room["enabled"] is False and room["result"] == "rejected"
    assert room["cursor"] == 1 and "retry_at" not in room
    obj.client.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "edit", "replace"])
async def test_stale_preflight_failure_cannot_override_admin_change(
    room_scheduler, change
):
    obj = room_scheduler
    room = obj.state["rooms"]["r"]

    async def delayed_rejection(*args):
        if change == "pause":
            await obj.action({"action": "room", "id": "r", "enabled": False})
        elif change == "edit":
            await obj.action({"action": "room", "id": "r", "text": "changed"})
        else:
            obj.state["rooms"]["r"] = dict(room, status="new room")
        raise RemoteError(2010132002, endpoint="room/info")

    obj.client.request.side_effect = delayed_rejection
    await obj.room_step("r")
    current = obj.state["rooms"]["r"]
    assert "last_service_error" not in current and "retry_at" not in current
    assert current["enabled"] == (change != "pause")
    if change == "pause":
        assert current["status"] == "已暂停"
    obj.client.send.assert_not_called()


@pytest.mark.asyncio
async def test_room_mute_does_not_prevent_admin_chat_connection():
    session = SimpleNamespace(ws_connect=AsyncMock(return_value="socket"))
    client = Client(session, "device", {})
    client.request = AsyncMock(
        side_effect=[
            {"all_muted": True, "region": "us-east-1"},
            {"token": "chat-token"},
        ]
    )
    assert await client.connect("room") == "socket"
    session.ws_connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_room_mute_flag_allows_send_and_explicit_rejection_retries():
    cls = next(
        n
        for n in ast.parse(
            Path("data/plugins/astrbot_plugin_rebo_live/main.py").read_text()
        ).body
        if isinstance(n, ast.ClassDef)
    )
    method = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "room_step"
    )
    scope = {
        "asyncio": asyncio,
        "json": json,
        "time": time,
        "uuid": uuid,
        "aiohttp": aiohttp,
        "RemoteError": RemoteError,
        "logger": logging.getLogger("test"),
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), "<scheduler>", "exec"),
        scope,
    )
    room = {"enabled": True, "revision": 1, "interval": 50, "texts": ["one"]}
    ws = AsyncMock()
    send = AsyncMock(side_effect=RemoteError(403, business_code=2010122004))
    obj = SimpleNamespace(
        state={"rooms": {"r": room}},
        status="已登录",
        checked=time.monotonic(),
        visible={"r": {"status": 1}},
        due={"r": 0},
        network=asyncio.Semaphore(1),
        client=SimpleNamespace(
            request=AsyncMock(return_value={"all_muted": True}),
            connect=AsyncMock(return_value=ws),
            send=send,
        ),
        save=lambda *args: None,
    )
    await scope["room_step"](obj, "r")
    send.assert_awaited_once()
    assert room["enabled"] is True
    assert room["result"] == "rejected"
    assert room["retry_at"] > time.time()
    assert room["cursor"] == 0
