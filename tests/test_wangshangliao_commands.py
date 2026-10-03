from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands


@pytest.mark.parametrize(
    "parts,expected",
    [
        ([], ""),
        (["选择群", "1"], "选择群 1"),
        (["公告", "hello", "world"], "公告 hello world"),
    ],
)
def test_registered_command_preserves_all_arguments(parts, expected):
    from astrbot.builtin_stars.wangshangliao_moderation.main import Main
    from astrbot.core.star.filter.command import CommandFilter

    command_filter = CommandFilter(
        "群管", handler_md=SimpleNamespace(handler=Main.moderation_command)
    )
    assert command_filter.validate_and_convert_params(
        parts, command_filter.handler_params
    ) == {"command": expected}


@pytest.mark.asyncio
async def test_private_commands_require_admin():
    event = SimpleNamespace(
        is_admin=lambda: False,
        get_sender_id=lambda: "123",
        is_private_chat=lambda: True,
    )
    commands = Commands()
    assert await commands.run(event, "帮助") == "无权限"
    assert await commands.run(event, "我的权限") == "无权限"
    assert await commands.run(event, "踢出 1") == "无权限"


@pytest.mark.asyncio
async def test_selection_expiry_and_isolation(monkeypatch):
    from astrbot.builtin_stars.wangshangliao_moderation import commands as module

    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    adapter = SimpleNamespace(
        account="1",
        groups={"5": "team"},
        group_names={"5": "大海兼职群"},
        config={"enabled_groups": ["5"]},
        execute_moderation=AsyncMock(),
    )
    event = SimpleNamespace(
        is_admin=lambda: True,
        get_sender_id=lambda: "2",
        platform=adapter,
        unified_msg_origin="bot:private:2",
        get_group_id=lambda: "",
        is_private_chat=lambda: True,
    )
    commands = Commands()
    assert "1. 大海兼职群" in await commands.run(event, "群列表")
    assert "当前已选群：大海兼职群" == await commands.run(event, "选择群 1")
    assert next(iter(commands.selections.values()))["group"] == "5"
    event.unified_msg_origin = "bot:private:3"
    assert "过期" in await commands.run(event, "选择群 1")
    event.unified_msg_origin = "bot:private:2"
    monkeypatch.setattr(module.time, "monotonic", lambda: 700)
    assert "过期" in await commands.run(event, "选择群 1")
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_selection_disambiguates_duplicate_names_and_empty_scope():
    adapter = SimpleNamespace(
        account="1",
        groups={"5": "team-a", "6": "team-b"},
        group_names={"5": "兼职群", "6": "兼职群"},
        config={"enabled_groups": ["5", "6"]},
    )
    event = SimpleNamespace(
        is_admin=lambda: True,
        get_sender_id=lambda: "2",
        platform=adapter,
        unified_msg_origin="bot:private:2",
        get_group_id=lambda: "",
        is_private_chat=lambda: True,
    )
    commands = Commands()
    groups = await commands.run(event, "群列表")
    assert "1. 兼职群（群号 5）" in groups
    assert "2. 兼职群（群号 6）" in groups
    adapter.groups = {}
    assert "没有可管理的已启用群" in await commands.run(event, "群列表")


@pytest.mark.asyncio
async def test_native_member_names_and_search():
    adapter = SimpleNamespace(
        account="1",
        config={"enabled_groups": ["5"]},
        get_moderation_members=AsyncMock(
            return_value={
                "groupMemberInfo": [
                    {
                        "userId": "2",
                        "nimId": "902",
                        "groupMemberNick": "Group Alias",
                        "userNick": "Account Name",
                    },
                    {
                        "userId": "3",
                        "nimId": "903",
                        "groupMemberNick": "",
                        "userNick": "Fallback Name",
                    },
                ]
            }
        ),
    )
    event = SimpleNamespace(
        is_admin=lambda: True,
        get_sender_id=lambda: "4",
        platform=adapter,
        unified_msg_origin="bot:private:4",
        get_group_id=lambda: "5",
        is_private_chat=lambda: True,
    )
    commands = Commands()
    result = await commands.run(event, "成员列表")
    assert "Group Alias (2)" in result
    assert "Fallback Name (3)" in result
    result = await commands.run(event, "成员搜索 fallback")
    assert "Fallback Name (3)" in result
    assert "Group Alias" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "",
        "帮助",
        "我的权限",
        "群列表",
        "选择群 1",
        "成员列表",
        "成员搜索 名称",
        "能力",
        "规则",
        "违规计数",
        "结果 id",
        "开发门禁",
        "未知命令",
    ],
)
async def test_group_never_discloses_private_queries(command):
    event = SimpleNamespace(is_private_chat=lambda: False)
    assert await Commands().run(event, command) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,action",
    [
        ("禁言", "mute"),
        ("解禁", "unmute"),
        ("踢出", "kick"),
        ("公告 Test", "announce"),
        ("全员禁言", "mute_all"),
        ("解除全员禁言", "unmute_all"),
    ],
)
async def test_group_fixed_actions_no_longer_require_self_mention(command, action):
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        config={"enabled_groups": ["5"]},
        get_moderation_members=AsyncMock(
            return_value={"groupMemberInfo": [{"userId": "3", "nimId": "903"}]}
        ),
        execute_moderation=AsyncMock(return_value={"status": "accepted"}),
    )
    event = SimpleNamespace(
        is_private_chat=lambda: False,
        is_admin=lambda: True,
        platform=adapter,
        get_extra=lambda _: {"mentions": ["903"]},
        get_sender_id=lambda: "2",
        get_group_id=lambda: "5",
        unified_msg_origin="bot:group:5",
        message_obj=SimpleNamespace(message_id="m"),
    )
    response = await Commands().run(event, command)
    assert ("已受理" if action in {"mute", "unmute"} else "服务端已接受") in response
    if action == "mute":
        assert adapter.execute_moderation.call_args.kwargs == {"minutes": 30}
        assert "30" in response and "尚未确认" not in response
    assert adapter.execute_moderation.call_args.args[1] == action
    event.is_admin = lambda: False
    assert await Commands().run(event, command) == "无权限"
    assert adapter.execute_moderation.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mentions", [[], ["901"], ["903", "904"]])
async def test_group_member_actions_still_require_one_real_target(mentions):
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        config={"enabled_groups": ["5"]},
        get_moderation_members=AsyncMock(
            return_value={
                "groupMemberInfo": [
                    {"userId": "3", "nimId": "903"},
                    {"userId": "4", "nimId": "904"},
                ]
            }
        ),
        execute_moderation=AsyncMock(),
    )
    event = SimpleNamespace(
        is_private_chat=lambda: False,
        is_admin=lambda: True,
        platform=adapter,
        get_extra=lambda _: {"mentions": mentions},
        get_sender_id=lambda: "2",
        get_group_id=lambda: "5",
        unified_msg_origin="bot:group:5",
    )
    assert "真实 @" in await Commands().run(event, "禁言 @Someone")
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_byte_bounded_member_pages_keep_snapshot_targets():
    roster = [
        {"userId": str(i), "nimId": str(900 + i), "name": "长昵称" * 20}
        for i in range(40)
    ]
    adapter = SimpleNamespace(
        account="1",
        config={"enabled_groups": ["5"]},
        get_moderation_members=AsyncMock(return_value={"groupMemberInfo": roster}),
        execute_moderation=AsyncMock(return_value={"status": "accepted"}),
    )
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        platform=adapter,
        get_sender_id=lambda: "2",
        unified_msg_origin="b:p:2",
        get_group_id=lambda: "5",
        message_obj=SimpleNamespace(message_id="m"),
    )
    commands = Commands()
    page = await commands.run(event, "成员列表")
    assert len(page.encode()) < 4096 and "下一页" in page
    first_count = len(next(iter(commands.selections.values()))["members"])
    page = await commands.run(event, "下一页")
    assert len(page.encode()) < 4096
    await commands.run(event, "禁言 1")
    assert adapter.execute_moderation.call_args.args[3] == first_count


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["", " 3", " 1440", " 0", " 1441", " -1", " 3 4"])
async def test_private_snapshot_mute_duration_and_member_permission(duration):
    adapter = SimpleNamespace(
        account="1",
        config={"enabled_groups": ["5"]},
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [{"userId": "3", "nimId": "903", "name": "Member"}],
            }
        ),
        execute_moderation=AsyncMock(return_value={"status": "accepted"}),
    )
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        platform=adapter,
        get_sender_id=lambda: "2",
        unified_msg_origin="b:p:2",
        get_group_id=lambda: "5",
        message_obj=SimpleNamespace(message_id="m"),
    )
    commands = Commands()
    await commands.run(event, "成员列表")
    result = await commands.run(event, "禁言 1" + duration)
    accepted = duration in {"", " 3", " 1440"}
    assert adapter.execute_moderation.await_count == int(accepted)
    if accepted:
        minutes = int(duration) if duration else 30
        assert adapter.execute_moderation.await_args.kwargs == {"minutes": minutes}
        assert result == f"禁言已受理：{minutes} 分钟。"
    event.is_admin = lambda: False
    assert await commands.run(event, "禁言 1" + duration) == "无权限"
    assert adapter.execute_moderation.await_count == int(accepted)


@pytest.mark.asyncio
async def test_missing_counter_table_is_empty(tmp_path, monkeypatch):
    import sqlite3

    from astrbot.builtin_stars.wangshangliao_moderation import commands as module

    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        db.execute("CREATE TABLE operations(id TEXT)")
    adapter = SimpleNamespace(
        account="1", config={"id": "bot", "enabled_groups": ["5"]}
    )
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        platform=adapter,
        get_sender_id=lambda: "2",
        unified_msg_origin="b:p:2",
        get_group_id=lambda: "5",
    )
    assert await Commands().run(event, "违规计数") == "暂无记录。"
