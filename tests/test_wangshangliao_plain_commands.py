"""Exact bare command routing, native role checks and read-only daily rankings."""

import asyncio
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.builtin_stars.wangshangliao_moderation import main, rankings
from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError, b64


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("排名", "排名"),
        (" 今日排名 ", "排名"),
        ("排行", "排名"),
        ("今日排行 2", "排名 2"),
        ("排名 2", "排名 2"),
        ("帮助", "帮助"),
        ("群管帮助", "帮助"),
        ("sid", "我的权限"),
        ("群列表", "群列表"),
        ("成员搜索 小明", "成员搜索 小明"),
        ("选择群\t1", "选择群 1"),
        ("禁言 @Member", "禁言 @Member"),
        ("禁言@Member", "禁言 @Member"),
        ("禁言@Member 3", "禁言 @Member 3"),
        ("禁言 @Member 30", "禁言 @Member 30"),
        ("禁言 2 3", "禁言 2 3"),
        ("解禁@Member", "解禁 @Member"),
        ("公告 hello world", "公告 hello world"),
        ("全员禁言", "全员禁言"),
        ("定时禁言 23:00 08:00", "定时禁言 23:00 08:00"),
        ("确认踢出 12345678", "确认踢出 12345678"),
        ("/群管 选择群 1", "选择群 1"),
        ("/排名", "排名"),
        ("/help", "帮助"),
        ("群管 成员列表", "成员列表"),
    ],
)
def test_exact_native_commands_and_legacy_aliases(text, expected):
    assert recognize_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "这个排名不错",
        "排名 还有什么",
        "排名 0",
        "排名 -1",
        "排名 1 然后禁言",
        "群列表里有什么",
        "请禁言 @Member",
        "不要全员禁言",
        "“全员禁言”",
        "转发：公告 hello",
        "有人说禁言 @Member",
        "禁言 是什么意思 @Member",
        "禁言 @Member\n解禁 @Member",
        "公告 hello\rworld",
        "公告 hello\u2028world",
        "公告\x00 hello",
        "选择群 一",
        "选择群 0",
        "选择群 1 然后禁言",
        "确认踢出 x",
        "未知命令",
        "",
        None,
        "公告 " + "x" * 4096,
    ],
)
def test_ordinary_quoted_negated_and_multiline_messages_are_not_commands(text):
    assert recognize_command(text) == ""


@pytest.mark.parametrize("text", ["排名", "群列表", "禁言 1", "sid"])
def test_bare_commands_never_bypass_private_ai_developer_scope(text):
    receiver = SimpleNamespace(
        account="1", nim_account="901", enabled_groups={"5"}, stopping=asyncio.Event()
    )
    sender = SimpleNamespace(account="2", stopping=asyncio.Event())
    window = Window(receiver, sender, [], ["private_ai"], [], 300, 10)
    receiver.test_window = window
    assert not window.send_command(sender, "private/1/" + b64(b"901"), text, "m")
    assert not window.admit("private/2/peer", "m", {"sender": "2", "text": text})
    assert window.remaining == 10


@pytest.mark.parametrize("text", ["排名", "群列表", "禁言 1", "sid"])
def test_bare_commands_are_allowed_in_exact_private_command_developer_scope(text):
    receiver = SimpleNamespace(
        account="1", nim_account="901", enabled_groups={"5"}, stopping=asyncio.Event()
    )
    sender = SimpleNamespace(account="2", stopping=asyncio.Event())
    window = Window(receiver, sender, [], ["private_commands"], [], 300, 10)
    receiver.test_window = window
    assert window.send_command(sender, "private/1/" + b64(b"901"), text, "m")
    assert (
        window.admit("private/2/peer", "m", {"sender": "2", "text": text})
        == "private_commands"
    )


def native_event(text, *, private=False, reply=True):
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.self_id = "1"
    message.session_id = "1/private/2/peer" if private else "1/5"
    message.group_id = "" if private else "5"
    message.message_id = "message"
    message.sender = MessageMember("2", "Member")
    message.message_str = text
    message.message = [Plain(text)]
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        members={"5": {"2": "902"}},
        groups={"5": "905"},
        config={
            "id": "fixture",
            "reply_private": reply,
            "reply_groups": {"5": reply},
            "enabled_groups": ["5"],
        },
        meta=lambda: PlatformMetadata("wangshangliao", "test", id="fixture"),
        send_reply_text=AsyncMock(),
        execute_moderation=AsyncMock(return_value={"status": "accepted"}),
    )
    event = WangshangliaoEvent(message, adapter, private)
    event.set_extra(
        "wangshangliao_payload",
        {
            "text": text,
            "sender": "2",
            "mentions": [],
            "name": "Member",
            "created_at": time.time() * 1000,
        },
    )
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,private,mention",
    [
        ("开启抽奖", True, False),
        ("帮我创建一个抽奖", True, False),
        ("你好", True, False),
        ("确认设置 1234567890abcdef", True, False),
        ("禁言 @Member", False, False),
        ("排名", False, False),
        ("你好", False, True),
    ],
)
@pytest.mark.parametrize("age", [301, -6, None])
async def test_stale_conversations_never_replay_commands_or_wake_ai(
    monkeypatch, text, private, mention, age
):
    monkeypatch.setattr(main.time, "time", lambda: 1790950000.0)
    event = native_event(text, private=private)
    event.role = "admin"
    event.is_at_or_wake_command = mention
    payload = event.get_extra("wangshangliao_payload")
    payload["created_at"] = 0 if age is None else (time.time() - age) * 1000
    plugin = object.__new__(main.Main)
    plugin.commands = SimpleNamespace(run=AsyncMock())
    plugin._select_route_provider = Mock()
    model = AsyncMock()
    handler = AsyncMock()
    monkeypatch.setattr(main, "assess", model)
    monkeypatch.setattr(main, "handle", handler)
    await plugin.moderate(event)
    assert event.is_stopped()
    plugin.commands.run.assert_not_awaited()
    plugin._select_route_provider.assert_not_called()
    event.platform.send_reply_text.assert_not_awaited()
    model.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize("text", ["公告 Hello", "全员禁言", "排名", "群列表"])
async def test_context_only_role_uses_effective_admin_ids(monkeypatch, text, admin):
    event = native_event(text)
    event.role = "admin"
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(astrbot_config={"admins_id": ["2"] if admin else []})
    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.star_handlers_registry."
        "get_handlers_by_event_type",
        lambda _: [],
    )
    await stage.process(event)
    assert event.is_admin() is admin
    assert event.get_extra("_context_only")
    assert not event.is_at_or_wake_command


@pytest.mark.asyncio
async def test_context_only_api_restriction_cannot_inherit_admin(monkeypatch):
    event = native_event("全员禁言")
    event.set_extra("_api_key_allow_admin_role", False)
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(astrbot_config={"admins_id": ["2"]})
    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.star_handlers_registry."
        "get_handlers_by_event_type",
        lambda _: [],
    )
    await stage.process(event)
    assert not event.is_admin()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["群列表", "sid", "排名", "全员禁言", "选择群 1"])
async def test_nonadmin_private_bare_commands_denied_before_plugins(monkeypatch, text):
    event = native_event(text, private=True, reply=False)
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(astrbot_config={"admins_id": [], "wake_prefix": ["/"]})
    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.star_handlers_registry."
        "get_handlers_by_event_type",
        lambda _: [],
    )
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await stage.process(event)
    assert event.is_stopped()
    assert event.platform.send_reply_text.await_args.args[2] == "无权限"
    await stage.process(event)
    assert event.platform.send_reply_text.await_count == 1
    assert "auto_recall" not in event.platform.send_reply_text.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["参加抽奖", "抽奖状态"])
async def test_lottery_command_replies_are_not_recalled(monkeypatch, text):
    event = native_event(text)
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    plugin.commands.activities.command = AsyncMock(return_value="报名反馈")
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await plugin.moderate(event)
    assert event.is_stopped()
    plugin.commands.activities.command.assert_awaited_once()
    assert event.get_extra("wsl_keep_reply") is True
    assert event.platform.send_reply_text.await_count == 1
    assert not event.platform.send_reply_text.await_args.kwargs.get("auto_recall", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["排名", "今日排名", "/排名"])
async def test_bare_public_rank_without_mention_never_calls_model_or_moderation(
    monkeypatch, text
):
    event = native_event(text)
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    plugin.commands.rankings.read = AsyncMock(return_value="Today fixture")
    model = AsyncMock(side_effect=AssertionError("Unexpected model call"))
    handle = AsyncMock(side_effect=AssertionError("Unexpected moderation"))
    monkeypatch.setattr(main, "assess", model)
    monkeypatch.setattr(main, "handle", handle)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await plugin.moderate(event)
    assert event.is_stopped()
    assert not event.eligible
    assert event.get_extra("wsl_plain_command")
    plugin.commands.rankings.read.assert_awaited_once_with(
        event.platform, "5", "2", page=1, native_mentions=True
    )
    assert event.platform.send_reply_text.await_count == 1
    assert "Today fixture" in event.platform.send_reply_text.await_args.args[2]
    assert event.platform.send_reply_text.await_args.kwargs["auto_recall"] is True
    model.assert_not_awaited()
    handle.assert_not_awaited()
    event.platform.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize(
    "command",
    [
        "帮助",
        "/help",
        "我的权限",
        "群列表",
        "/群管 成员列表",
        "成员搜索 testfork",
        "规则",
        "结果 operation",
        "开启抽奖",
        "抽奖设置",
        "设置邀请奖励 5",
        "开启邀请奖励",
        "定时禁言 23:00 08:00",
        "定时状态",
        "确认定时 abcdefgh",
        "确认踢出 abcdefgh",
    ],
)
async def test_private_only_group_commands_are_consumed_without_reply_or_action(
    monkeypatch, admin, command
):
    event = native_event(command)
    event.role = "admin" if admin else "member"
    event.is_at_or_wake_command = True
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    plugin.commands.activities.command = AsyncMock()
    plugin.commands.schedules = SimpleNamespace(command=AsyncMock())
    model = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(main, "assess", model)
    monkeypatch.setattr(main, "handle", audit)
    await plugin.moderate(event)
    assert event.is_stopped()
    event.platform.send_reply_text.assert_not_awaited()
    event.platform.execute_moderation.assert_not_awaited()
    plugin.commands.activities.command.assert_not_awaited()
    plugin.commands.schedules.command.assert_not_awaited()
    model.assert_not_awaited()
    audit.assert_not_awaited()
    assert not plugin.commands.selections


@pytest.mark.asyncio
async def test_content_mode_group_admin_kick_is_silent_without_preview_or_action():
    event = native_event("踢出 @Member")
    event.role = "admin"
    event.platform.config["moderation"] = {"content_rules_since": 1}
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    await plugin.moderate(event)
    assert event.is_stopped()
    event.platform.send_reply_text.assert_not_awaited()
    event.platform.execute_moderation.assert_not_awaited()
    assert not plugin.commands.selections


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
async def test_bare_announcement_identity_permissions_before_native_operation(
    monkeypatch, admin
):
    event = native_event("公告 hello world")
    event.role = "admin" if admin else "member"
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await plugin.moderate(event)
    assert event.is_stopped()
    if admin:
        event.platform.execute_moderation.assert_awaited_once()
        assert event.platform.execute_moderation.await_args.args[1:5] == (
            "announce",
            5,
            0,
            "hello world",
        )
    else:
        event.platform.execute_moderation.assert_not_awaited()
        assert "无权限" in event.platform.send_reply_text.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_flag,reply,expected", [(False, True, 0), (True, False, 0), (True, True, 1)]
)
async def test_command_reply_exception_does_not_open_chat_or_disabled_groups(
    monkeypatch, command_flag, reply, expected
):
    event = native_event("排名", reply=reply)
    event.set_extra("wsl_plain_command", command_flag)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await event.send(event.plain_result("Ranking fixture"))
    await event.send(event.plain_result("Second reply"))
    assert event.platform.send_reply_text.await_count == expected


@pytest.mark.asyncio
async def test_ordinary_keyword_sentence_still_runs_moderation(monkeypatch):
    event = native_event("这个排名不错")
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    handle = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "handle", handle)
    await plugin.moderate(event)
    handle.assert_awaited_once()
    assert not event.get_extra("wsl_plain_command")
    event.platform.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_disabled_reply_switch_never_executes_silent_bare_mutation(
    monkeypatch, private
):
    event = native_event("公告 hello", private=private, reply=False)
    event.role = "admin"
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await plugin.moderate(event)
    assert event.is_stopped()
    event.platform.execute_moderation.assert_not_awaited()
    event.platform.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_disabled_instance_never_executes_bare_mutation(private):
    event = native_event("全员禁言", private=private)
    event.role = "admin"
    event.platform.config["enable"] = False
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    await plugin.moderate(event)
    assert event.is_stopped()
    event.platform.execute_moderation.assert_not_awaited()
    event.platform.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,accepted",
    [
        ("", True),
        (" 3", True),
        (" 1440", True),
        (" 0", False),
        (" -3", False),
        (" 1.5", False),
        (" 1441", False),
        (" 99999999999", False),
        (" ３", False),
        (" 3分钟", False),
        (" 3 4", False),
        (" 吗", False),
        (" 然后解禁", False),
    ],
)
async def test_bare_target_command_requires_exact_verified_mention_only(
    suffix, accepted
):
    commands = Commands()
    event = native_event("禁言 @Member" + suffix)
    event.role = "admin"
    event.platform.get_moderation_members = AsyncMock(
        return_value={
            "groupMemberInfo": [{"userId": "3", "nimId": "903"}],
            "complete": True,
        }
    )
    event.set_extra("wsl_plain_command", True)
    event.set_extra(
        "wangshangliao_payload",
        {
            "text": event.message_str,
            "mentions": ["903"],
            "mention_spans": [{"uid": "903", "nick": "Member", "start": 3, "end": 10}],
        },
    )
    response = await commands.run(event, "禁言 @Member" + suffix)
    assert event.platform.execute_moderation.await_count == int(accepted)
    if accepted:
        minutes = int(suffix) if suffix else 30
        assert event.platform.execute_moderation.await_args.kwargs == {
            "minutes": minutes
        }
        assert response == f"禁言已受理：{minutes} 分钟。"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["禁言 ", "禁言"])
@pytest.mark.parametrize("nickname", ["Member", "A B", "\U0001f600 Member"])
async def test_mute_duration_uses_native_mention_boundary(
    monkeypatch, prefix, nickname
):
    text = prefix + "@" + nickname + " 3"
    event = native_event(text)
    event.role = "admin"
    event.platform.get_moderation_members = AsyncMock(
        return_value={
            "groupMemberInfo": [{"userId": "3", "nimId": "903"}],
            "complete": True,
        }
    )
    event.set_extra(
        "wangshangliao_payload",
        {
            "text": text,
            "created_at": time.time() * 1000,
            "mentions": ["903"],
            "mention_spans": [
                {
                    "uid": "903",
                    "nick": nickname,
                    "start": len(prefix.encode("utf-16-le")) // 2,
                    "end": len((prefix + "@" + nickname + " ").encode("utf-16-le"))
                    // 2,
                }
            ],
        },
    )
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await plugin.moderate(event)
    assert event.is_stopped()
    assert event.platform.execute_moderation.await_args.kwargs == {"minutes": 3}
    assert "禁言已受理" in event.platform.send_reply_text.await_args.args[2]
    assert event.platform.send_reply_text.await_args.kwargs["auto_recall"] is True
    assert "尚未确认" not in event.platform.send_reply_text.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["禁言 @Member 3", "禁言@Member", "解禁@Member"])
async def test_member_cannot_mutate_even_when_target_mention_is_admin(
    monkeypatch, text
):
    event = native_event(text)
    event.role = "member"
    event.set_extra(
        "wangshangliao_payload",
        {
            "text": text,
            "created_at": time.time() * 1000,
            "mentions": ["901"],
            "sender": "2",
        },
    )
    event.platform.get_moderation_members = AsyncMock()
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await plugin.moderate(event)
    assert "无权限" in event.platform.send_reply_text.await_args.args[2]
    event.platform.get_moderation_members.assert_not_awaited()
    event.platform.execute_moderation.assert_not_awaited()


@pytest_asyncio.fixture
async def ranking_adapter(tmp_path, monkeypatch):
    now = datetime(2026, 10, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    monkeypatch.setattr(rankings.time, "time", lambda: now)
    monkeypatch.setattr(rankings, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(rankings, "is_managed_account", lambda user: user == "9")
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        ledger=ledger,
        members={"5": {"2": "902", "3": "903"}},
        config={"id": "fixture", "enable": True, "enabled_groups": ["5"]},
    )
    yield adapter, tmp_path, now
    await ledger.close()


async def add_message(adapter, mid, user, text, stamp, *, group="5", state="processed"):
    payload = {
        "sender": user,
        "name": f"Member {user}",
        "text": text,
        "created_at": stamp * 1000,
        "recall_route": {"time": str(int(stamp * 1000))},
        "mentions": [],
    }
    await adapter.ledger.ingest(adapter.account, group, mid, payload)
    await adapter.ledger.mark(adapter.account, group, mid, state)


@pytest.mark.asyncio
async def test_ranking_beijing_day_dedup_scopes_bots_commands_spam_and_violations(
    ranking_adapter,
):
    adapter, root, now = ranking_adapter
    start = datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    await add_message(adapter, "1", "2", "First message", start)
    await add_message(adapter, "1", "2", "Duplicate delivery", start)
    await add_message(adapter, "2", "2", "Fast new content", start + 10)
    await add_message(adapter, "3", "2", "Second valid", start + 30)
    await add_message(adapter, "4", "2", "First message", start + 60)
    await add_message(adapter, "5", "2", "Third valid", start + 100)
    await add_message(adapter, "6", "3", "One message", start + 90)
    await add_message(adapter, "7", "3", "Yesterday", start - 1)
    await add_message(adapter, "8", "3", "Future", now + 120)
    await add_message(adapter, "9", "3", "排名", start + 150)
    await add_message(adapter, "10", "3", "/unknown", start + 180)
    await add_message(adapter, "11", "3", "Pure emoji \U0001f389", start + 210)
    await add_message(adapter, "12", "3", "Confirmed violation", start + 240)
    await add_message(adapter, "13", "9", "Managed bot", start + 300)
    await add_message(adapter, "14", "1", "Self bot", start + 300)
    await add_message(
        adapter, "15", "3", "Rejected", start + 300, state="rejected_identity"
    )
    await add_message(adapter, "16", "3", "Other group", start + 300, group="6")
    await add_message(
        adapter, "17", "3", "Private message", start + 300, group="private/3"
    )
    await add_message(adapter, "18", "3", "\U0001f389\U0001f389", start + 310)
    with sqlite3.connect(root / "moderation.sqlite3") as db:
        db.execute(
            "CREATE TABLE content_violations(account TEXT,group_id TEXT,message TEXT,observed REAL,status TEXT)"
        )
        db.execute(
            "INSERT INTO content_violations VALUES('1','5','12',?,'warned')", (now,)
        )
    before = (root / "moderation.sqlite3").read_bytes()
    result = await rankings.DailyRankings().read(adapter, "5", "2")
    assert result.startswith("\U0001f4ac 今日发言排行\n第 1 页 / 每页 20 条\n\n")
    assert "1. Member 2 - 3 条" in result
    assert "2. Member 3 - 2 条" in result
    assert "北京时间" not in result
    assert "你的" not in result
    assert "仅统计" not in result
    assert "Member 9" not in result
    assert "Other group" not in result
    assert (root / "moderation.sqlite3").read_bytes() == before
    async with adapter.ledger.db.execute("SELECT COUNT(*) FROM outbox") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_ranking_same_content_after_five_minutes_counts_again(ranking_adapter):
    adapter, _, now = ranking_adapter
    await add_message(adapter, "1", "2", "Same TEXT", now - 400)
    await add_message(adapter, "2", "2", "same text", now - 100)
    result = await rankings.DailyRankings().read(adapter, "5", "2")
    assert "Member 2 - 2 条" in result


@pytest.mark.asyncio
async def test_ranking_tie_order_independent_of_delivery_order(ranking_adapter):
    adapter, _, now = ranking_adapter
    await add_message(adapter, "2", "3", "Later", now - 100)
    await add_message(adapter, "1", "2", "Earlier", now - 200)
    result = await rankings.DailyRankings().read(adapter, "5", "3")
    assert result.index("Member 2") < result.index("Member 3")
    assert "2. Member 3 - 1 条" in result


@pytest.mark.asyncio
async def test_ranking_verified_leading_bot_commands_and_unknown_time_excluded(
    ranking_adapter,
):
    adapter, _, now = ranking_adapter
    await adapter.ledger.ingest(
        adapter.account,
        "5",
        "native-mention-command",
        {
            "sender": "2",
            "name": "Member 2",
            "text": "@Bot 排名",
            "created_at": now - 200,
            "mention_spans": [{"uid": "901", "nick": "Bot", "start": 0, "end": 4}],
        },
    )
    await add_message(adapter, "unknown", "2", "Unknown timestamp", 0)
    await add_message(adapter, "valid", "2", "Valid text", now - 100)
    result = await rankings.DailyRankings().read(adapter, "5", "2")
    assert "Member 2 - 1 条" in result


@pytest.mark.asyncio
async def test_ranking_empty_scope_and_cache_rechecked_on_disable(ranking_adapter):
    adapter, _, _ = ranking_adapter
    service = rankings.DailyRankings()
    result = await service.read(adapter, "5", "2")
    assert result == "\U0001f4ac 今日发言排行\n第 1 页 / 每页 20 条\n\n暂无记录"
    adapter.config["enabled_groups"] = []
    with pytest.raises(ProtocolError, match="ranking_scope"):
        await service.read(adapter, "5", "2")


@pytest.mark.asyncio
async def test_ranking_missing_storage_is_not_a_fabricated_empty_board(tmp_path):
    adapter = SimpleNamespace(
        account="1",
        config={"id": "fixture", "enable": True, "enabled_groups": ["5"]},
        ledger=SimpleNamespace(path=tmp_path / "missing.sqlite3"),
    )
    with pytest.raises(ProtocolError, match="ranking_unavailable"):
        await rankings.DailyRankings().read(adapter, "5", "2")
    assert not adapter.ledger.path.exists()


@pytest.mark.asyncio
async def test_public_group_ranking_private_management_isolation():
    commands = Commands()
    commands.rankings.read = AsyncMock(return_value="Ranking fixture")
    event = SimpleNamespace(
        is_private_chat=lambda: False,
        is_admin=lambda: False,
        platform=SimpleNamespace(),
        get_group_id=lambda: "5",
        get_sender_id=lambda: "2",
    )
    assert await commands.run(event, "排名") == "Ranking fixture"
    event.is_private_chat = lambda: True
    assert await commands.run(event, "排名") == "无权限"
    assert commands.rankings.read.await_count == 1


@pytest.mark.asyncio
async def test_ranking_twenty_member_pages_and_native_identities(ranking_adapter):
    adapter, _, now = ranking_adapter
    for user in range(10, 33):
        adapter.members["5"][str(user)] = str(900 + user)
        await add_message(adapter, str(user), str(user), "A valid message", now - 100)
    service = rankings.DailyRankings()
    first = await service.read(adapter, "5", "2", native_mentions=True)
    assert isinstance(first, MessageChain)
    assert len([part for part in first.chain if isinstance(part, At)]) == 20
    second = await service.read(adapter, "5", "2", page=2, native_mentions=True)
    identities = [str(part.qq) for part in second.chain if isinstance(part, At)]
    assert identities == ["30", "31", "32"]
    assert second.chain[0].text.endswith("第 2 页 / 每页 20 条\n\n")
    assert any(isinstance(part, Plain) and part.text == "21. " for part in second.chain)
    assert "你的" not in "".join(
        part.text for part in second.chain if isinstance(part, Plain)
    )


@pytest.mark.asyncio
async def test_ranking_unknown_mapping_does_not_forge_a_mention(ranking_adapter):
    adapter, _, now = ranking_adapter
    await add_message(adapter, "message", "4", "Valid message", now - 100)
    chain = await rankings.DailyRankings().read(adapter, "5", "2", native_mentions=True)
    assert not any(isinstance(part, At) for part in chain.chain)
    assert any(part.text == "Member 4 - 1 条" for part in chain.chain)


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [0, -1, True, "2", 100000])
async def test_ranking_page_validation(ranking_adapter, page):
    adapter, _, _ = ranking_adapter
    with pytest.raises(ProtocolError, match="ranking_page"):
        await rankings.DailyRankings().read(adapter, "5", "2", page=page)


@pytest.mark.asyncio
async def test_native_ranking_preserves_mentions_without_prefixing_caller(monkeypatch):
    event = native_event("排名")
    event.platform.members["5"]["3"] = "903"
    event.set_extra("wsl_plain_command", True)
    event.set_extra("wsl_ranking_result", True)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await event.send(
        MessageChain(
            [
                Plain("\U0001f4ac 今日发言排行\n\n1. "),
                At(qq="3", name="Member"),
                Plain("- 5 条"),
            ]
        )
    )
    call = event.platform.send_reply_text.await_args
    assert call.args[2] == "\U0001f4ac 今日发言排行\n\n1. @Member - 5 条"
    assert call.kwargs["mentions"] == [
        {"uid": 903, "nick": "Member", "start": 14, "end": 22}
    ]


@pytest.mark.asyncio
async def test_full_ranking_chain_keeps_row_breaks_and_native_offsets(
    ranking_adapter, monkeypatch
):
    adapter, _, now = ranking_adapter
    await add_message(adapter, "first", "2", "One", now - 200)
    await add_message(adapter, "second", "3", "Two", now - 100)
    chain = await rankings.DailyRankings().read(adapter, "5", "2", native_mentions=True)
    event = native_event("排名")
    event.platform.members = adapter.members
    event.set_extra("wsl_plain_command", True)
    event.set_extra("wsl_ranking_result", True)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await event.send(chain)
    call = event.platform.send_reply_text.await_args
    text = call.args[2]
    assert text == (
        "\U0001f4ac 今日发言排行\n第 1 页 / 每页 20 条\n\n"
        "1. @Member 2 - 1 条\n2. @Member 3 - 1 条"
    )
    people = call.kwargs["mentions"]
    assert [person["uid"] for person in people] == [902, 903]
    encoded = text.encode("utf-16-le")
    for person in people:
        assert (
            encoded[person["start"] * 2 : person["end"] * 2].decode("utf-16-le")
            == "@" + person["nick"] + " "
        )


@pytest.mark.asyncio
async def test_empty_native_ranking_does_not_mention_caller(monkeypatch):
    event = native_event("排名")
    event.set_extra("wsl_plain_command", True)
    event.set_extra("wsl_ranking_result", True)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await event.send(MessageChain([Plain("今日发言排行\n暂无记录")]))
    call = event.platform.send_reply_text.await_args
    assert call.args[2] == "今日发言排行\n暂无记录"
    assert not call.kwargs.get("mentions")


@pytest.mark.asyncio
@pytest.mark.parametrize("open_window", [True, False])
async def test_unmentioned_ranking_to_managed_sender_obeys_test_window(
    monkeypatch, open_window
):
    event = native_event("排名")
    event.set_extra("wsl_plain_command", True)
    event.set_extra("wsl_ranking_result", True)
    event.set_extra("wsl_command_result", True)
    event.platform.test_window = SimpleNamespace(reply=Mock(return_value=open_window))
    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.event.is_managed_account",
        lambda _: True,
    )
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await event.send(MessageChain([Plain("今日发言排行\n暂无记录")]))
    assert event.platform.send_reply_text.await_count == int(open_window)
    event.platform.test_window.reply.assert_called_once_with(
        "5", "message", "1/5/message/0"
    )


@pytest.mark.asyncio
async def test_ranking_marker_does_not_change_ordinary_group_replies(monkeypatch):
    event = native_event("hello")
    event.eligible = True
    event.set_extra("wsl_ranking_result", True)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await event.send(MessageChain([At(qq="3", name="Other"), Plain("Reply")]))
    call = event.platform.send_reply_text.await_args
    assert call.args[2] == "@Member Reply"
    assert call.kwargs["mentions"][0]["uid"] == 902


@pytest.mark.asyncio
async def test_proactive_structured_target_command_keeps_separator():
    event = native_event("Fixture")
    adapter = event.platform
    adapter.config["proactive_send"] = {"enabled": True, "targets": ["5"]}
    adapter.members["5"]["3"] = "903"
    session = MessageSession("fixture", MessageType.GROUP_MESSAGE, "1/5")
    await WangshangliaoAdapter.send_by_session(
        adapter,
        session,
        MessageChain([Plain("禁言 "), At(qq="3", name="testfork")]),
        operation_id="fixture-separator",
    )
    call = adapter.send_reply_text.await_args
    assert call.args[2] == "禁言 @testfork "
    assert call.kwargs["mentions"] == [
        {"uid": 903, "nick": "testfork", "start": 3, "end": 13}
    ]
