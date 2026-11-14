import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
from astrbot.builtin_stars.wangshangliao_moderation.group_management import (
    GroupManagement,
)
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.fixtures.wsl_actions import bind_action_port
from tests.test_wangshangliao_plain_commands import native_event


@pytest_asyncio.fixture(autouse=True)
async def offline(monkeypatch):
    """Deny network I/O after the runner initializes its Windows self-pipe.

    Args:
        monkeypatch: Test-scoped patch factory.

    Yields:
        Nothing; denial remains active through dependent-fixture cleanup.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("real_network_forbidden")

    with monkeypatch.context() as guard:
        guard.setattr(socket.socket, "connect", forbidden)
        guard.setattr(socket.socket, "connect_ex", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        yield


class TestOfflineNetworkGuard:
    """Exercise the Windows self-pipe without exempting business connections."""

    @classmethod
    @pytest.fixture(scope="class", autouse=True)
    def windows_selfpipe(cls):
        """Use CPython's TCP socketpair only for event-loop initialization.

        Yields:
            Counts of initialized self-pipes and cleanly closed runners.
        """
        state = {"pairs": 0, "closed": 0}
        connect = socket.socket.connect
        connect_ex = socket.socket.connect_ex
        create_connection = socket.create_connection
        close = asyncio.BaseEventLoop.close

        def socketpair():
            pair = socket._fallback_socketpair()
            assert all(sock.family == socket.AF_INET for sock in pair)
            state["pairs"] += 1
            return pair

        def close_after_guard(loop):
            assert socket.socket.connect is connect
            assert socket.socket.connect_ex is connect_ex
            assert socket.create_connection is create_connection
            close(loop)
            state["closed"] += 1

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(socket, "socketpair", socketpair)
            patch.setattr(asyncio.BaseEventLoop, "close", close_after_guard)
            yield state
        assert state["pairs"] > 0
        assert state["closed"] == state["pairs"]

    def assert_network_blocked(self, host):
        """Exercise every guarded connection API without a loopback exemption.

        Args:
            host: Numeric loopback or documentation-only remote address.
        """
        address = (host, 9)
        with socket.socket() as sock:
            with pytest.raises(AssertionError):
                sock.connect(address)
            with pytest.raises(AssertionError):
                sock.connect_ex(address)
        with pytest.raises(AssertionError):
            socket.create_connection(address, timeout=0.1)

    @pytest_asyncio.fixture
    async def guarded_cleanup(self):
        """Keep dependent-fixture cleanup inside the network-denial lifetime.

        Yields:
            Nothing; cleanup itself must still be guarded.
        """
        yield
        self.assert_network_blocked("127.0.0.1")

    @pytest.mark.parametrize("host", ["127.0.0.1", "192.0.2.1"])
    def test_sync_network_remains_blocked(
        self, windows_selfpipe, guarded_cleanup, host
    ):
        assert windows_selfpipe["pairs"] > 0
        self.assert_network_blocked(host)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", ["127.0.0.1", "192.0.2.1"])
    async def test_async_network_remains_blocked(
        self, windows_selfpipe, guarded_cleanup, host
    ):
        assert asyncio.get_running_loop()._ssock.family == socket.AF_INET
        assert windows_selfpipe["pairs"] > 0
        self.assert_network_blocked(host)


@pytest_asyncio.fixture
async def private_command_event():
    """Build a cached native private event with observable delivery boundaries.

    Yields:
        Retired private event whose directory and send methods are offline mocks.
    """
    event = native_event("private command", private=True)
    adapter = event.platform
    adapter.get_moderation_members = AsyncMock()
    adapter.get_moderation_result = AsyncMock()
    adapter.business = SimpleNamespace(request=AsyncMock())
    try:
        yield event
    finally:
        event.processing_completion.cancel()


@pytest_asyncio.fixture
async def managed_group(tmp_path):
    """Bind a native group event to verified membership and an exact group grant.

    Args:
        tmp_path: Isolated path for the grant service.

    Yields:
        Command engine and actual group event with a non-global manager identity.
    """
    event = native_event("group command")
    adapter = event.platform
    adapter.config.update(
        id="bot",
        enable=True,
        _business_active=True,
        _open_groups={"5": {"advertising": True, "flood": True, "keywords": ["old"]}},
        _open_native_roles={"5": "GROUP_ROLE_ADMIN"},
    )
    adapter.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {
                    "userId": "1",
                    "nimId": "901",
                    "groupRole": "GROUP_ROLE_ADMIN",
                    "userNick": "Bot",
                },
                {
                    "userId": "2",
                    "nimId": "902",
                    "groupRole": "GROUP_ROLE_OWNER",
                    "userNick": "Manager",
                },
                {
                    "userId": "3",
                    "nimId": "903",
                    "groupRole": "GROUP_ROLE_MEMBER",
                    "userNick": "Member",
                },
            ],
        }
    )
    commands = Commands()
    commands.management = GroupManagement(
        SimpleNamespace(get_config=lambda _: {"admins_id": []}), root=tmp_path
    )
    key = commands.management.key(adapter, "5")
    commands.management.assignments[key] = ("2",)
    commands.management.versions[key] = 1
    assert not event.is_private_chat()
    assert not commands.management.superadmin(event)
    assert commands.management.allowed(event, "5")
    try:
        yield commands, event
    finally:
        event.processing_completion.cancel()


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
@pytest.mark.parametrize("admin", [False, True])
async def test_private_commands_retired_regardless_of_admin(
    private_command_event, admin
):
    event = private_command_event
    event.role = "admin" if admin else "member"
    commands = Commands()
    commands.management = SimpleNamespace(
        allowed=Mock(side_effect=AssertionError("private_authority_must_not_run"))
    )
    for command in (
        "帮助",
        "我的权限",
        "踢出 1",
        "成员搜索 名称",
        "规则",
        "违规计数",
        "结果 id",
    ):
        assert await commands.run(event, command) is None
    assert not commands.selections
    commands.management.allowed.assert_not_called()
    event.platform.get_moderation_members.assert_not_awaited()
    event.platform.get_moderation_result.assert_not_awaited()
    event.platform.execute_moderation.assert_not_awaited()
    event.platform.send_reply_text.assert_not_awaited()
    event.platform.business.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_selection_never_created_expired_or_rebound(
    private_command_event,
):
    event = private_command_event
    adapter = event.platform
    adapter.group_names = {"5": "大海兼职群"}
    commands = Commands()
    assert await commands.run(event, "群列表") is None
    assert await commands.run(event, "选择群 1") is None
    assert not commands.selections
    key = (
        id(adapter),
        adapter.account,
        event.get_sender_id(),
        event.unified_msg_origin,
    )
    historical = {"group": "5", "expires": 0, "members": []}
    commands.selections[key] = historical
    original = event.unified_msg_origin
    event.unified_msg_origin = "bot:FriendMessage:1/private/3/OTAz"
    assert await commands.run(event, "选择群 1") is None
    event.unified_msg_origin = original
    assert await commands.run(event, "选择群 1") is None
    assert commands.selections == {key: historical}
    adapter.get_moderation_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()
    adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_group_list_never_discloses_duplicate_names_or_empty_scope(
    private_command_event,
):
    event = private_command_event
    adapter = event.platform
    adapter.groups = {"5": "team-a", "6": "team-b"}
    adapter.group_names = {"5": "兼职群", "6": "兼职群"}
    adapter.config["enabled_groups"] = ["5", "6"]
    commands = Commands()
    assert await commands.run(event, "群列表") is None
    adapter.groups = {}
    assert await commands.run(event, "群列表") is None
    assert not commands.selections
    adapter.get_moderation_members.assert_not_awaited()
    adapter.send_reply_text.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_group_member_names_and_search(managed_group):
    commands, event = managed_group
    roster = event.platform.get_moderation_members.return_value["groupMemberInfo"]
    roster[1].update(groupMemberNick="Group Alias", userNick="Account Name")
    roster[2].update(groupMemberNick="", userNick="Fallback Name")
    result = await commands.run(event, "成员列表")
    assert "Group Alias (2)" in result
    assert "Fallback Name (3)" in result
    result = await commands.run(event, "成员搜索 fallback")
    assert "Fallback Name (3)" in result
    assert "Group Alias" not in result
    event.platform.execute_moderation.assert_not_awaited()
    event.platform.send_reply_text.assert_not_awaited()


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
    bind_action_port(adapter)
    if action == "unmute":
        roster = {
            "complete": True,
            "groupMemberInfo": [
                {"userId": "1", "nimId": "901", "groupRole": "GROUP_ROLE_ADMIN"},
                {"userId": "3", "nimId": "903", "groupRole": "GROUP_ROLE_MEMBER"},
            ],
        }
        adapter.get_moderation_members.return_value = roster
        adapter.groups = {"5": "905"}
        adapter.business = SimpleNamespace(request=AsyncMock(return_value=roster))
        adapter.nim = SimpleNamespace(
            get_team_muted_members=AsyncMock(return_value={"903"})
        )
        adapter.config["moderation"] = {
            "enabled": True,
            "permissions": {"5": ["unmute"]},
        }
    event = SimpleNamespace(
        is_private_chat=lambda: False,
        is_admin=lambda: True,
        platform=adapter,
        get_extra=lambda _: {"mentions": ["903"]},
        get_sender_id=lambda: "2",
        get_group_id=lambda: "5",
        unified_msg_origin="bot:group:5",
        message_obj=SimpleNamespace(message_id="m"),
        actor_identity=ActorIdentity("1", "2", "902", "2"),
    )
    response = await Commands().run(event, command)
    assert ("已受理" if action in {"mute", "unmute"} else "服务端已接受") in response
    if action == "mute":
        assert adapter.execute_moderation.call_args.kwargs["minutes"] == 30
        assert "30" in response and "尚未确认" not in response
    assert adapter.execute_moderation.call_args.args[1] == action
    event.is_admin = lambda: False
    assert await Commands().run(event, command) == "无权限"
    assert adapter.execute_moderation.await_count == 1


@pytest.mark.asyncio
async def test_admin_revocation_while_action_waits_prevents_submission():
    entered, release = asyncio.Event(), asyncio.Event()
    authority = {"admin": True}
    submitted = []

    async def execute(*args, action_check, **kwargs):
        entered.set()
        await release.wait()
        await action_check()
        submitted.append(args)
        return {"status": "accepted"}

    adapter = SimpleNamespace(
        account="1",
        config={"enabled_groups": ["5"]},
        execute_moderation=AsyncMock(side_effect=execute),
    )
    bind_action_port(adapter)
    event = SimpleNamespace(
        is_private_chat=lambda: False,
        is_admin=lambda: authority["admin"],
        platform=adapter,
        get_sender_id=lambda: "2",
        get_group_id=lambda: "5",
        unified_msg_origin="bot:group:5",
        message_obj=SimpleNamespace(message_id="m"),
    )
    work = asyncio.create_task(Commands().run(event, "公告 Test"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        authority["admin"] = False
        release.set()
        with pytest.raises(ProtocolError, match="command_admin_revoked"):
            await asyncio.wait_for(work, 1)
        assert submitted == []
    finally:
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "progressive,manual_only", [(False, False), (True, False), (False, True)]
)
async def test_legacy_auto_kick_rule_text_does_not_promise_removal(
    managed_group, progressive, manual_only
):
    commands, event = managed_group
    adapter = event.platform
    legacy = {
        "auto_kick": {"5": True},
        "kick_keywords": ["old"],
        "progressive_mute": progressive,
        "manual_kick_only": manual_only,
    }
    adapter.config["moderation"] = legacy
    result = await commands.run(event, "规则")
    assert "自动踢出已停用" in result
    assert "人工踢出请使用旺商聊App" in result
    assert "私聊预览并确认" not in result
    assert "第四次" not in result
    assert "前三次确认违规仅撤回并警告" in result
    assert "1分钟、3分钟、10分钟" in result
    assert "原踢出关键词仅参与违规判断，不自动踢出" in result
    assert adapter.config["moderation"] == legacy
    adapter.execute_moderation.assert_not_awaited()
    adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_retired_group_help_is_silent_without_business_actions(managed_group):
    commands, event = managed_group
    response = await commands.run(event, "帮助")
    assert response is None
    assert not commands.selections
    event.platform.execute_moderation.assert_not_awaited()
    event.platform.get_moderation_members.assert_not_awaited()


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
async def test_byte_bounded_group_member_pages_keep_snapshot_targets(managed_group):
    commands, event = managed_group
    adapter = event.platform
    roster = [
        *adapter.get_moderation_members.return_value["groupMemberInfo"][:2],
        *[
            {
                "userId": str(i),
                "nimId": str(900 + i),
                "name": "长昵称" * 20,
                "groupRole": "GROUP_ROLE_MEMBER",
            }
            for i in range(3, 43)
        ],
    ]
    adapter.get_moderation_members.return_value["groupMemberInfo"] = roster
    page = await commands.run(event, "成员列表")
    assert len(page.encode()) < 4096 and "下一页" in page
    first_count = len(next(iter(commands.selections.values()))["members"])
    page = await commands.run(event, "下一页")
    assert len(page.encode()) < 4096
    selection = next(iter(commands.selections.values()))
    assert selection["group"] == "5"
    assert selection["members"][0]["userId"] == roster[first_count]["userId"]
    assert selection["members"][0]["nimId"] == roster[first_count]["nimId"]
    adapter.execute_moderation.assert_not_awaited()
    adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_member_snapshot_expires_and_does_not_cross_sessions(managed_group):
    commands, event = managed_group
    await commands.run(event, "成员列表")
    original = event.unified_msg_origin
    event.unified_msg_origin = "other:GroupMessage:1/5"
    assert await commands.run(event, "下一页") == "列表已过期，请重新查询成员。"
    event.unified_msg_origin = original
    next(iter(commands.selections.values()))["expires"] = 0
    assert await commands.run(event, "下一页") == "列表已过期，请重新查询成员。"
    event.platform.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["", " 3", " 1440", " 0", " 1441", " -1", " 3 4"])
async def test_private_snapshot_mute_always_silent_without_selection(
    private_command_event, duration
):
    event = private_command_event
    adapter = event.platform
    commands = Commands()
    for role in ("admin", "member"):
        event.role = role
        assert await commands.run(event, "成员列表") is None
        assert await commands.run(event, "禁言 1" + duration) is None
    assert not commands.selections
    adapter.get_moderation_members.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()
    adapter.send_reply_text.assert_not_awaited()
    adapter.business.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [1, 3, 30, 1440, 0, 1441, -1, "3 4", True])
async def test_native_mute_action_keeps_24_hour_duration_boundary(minutes):
    adapter = SimpleNamespace(
        account="1",
        config={"id": "offline-boundary", "enabled_groups": ["5"]},
        execute_moderation=AsyncMock(return_value={"status": "accepted"}),
        get_moderation_members=AsyncMock(),
    )
    bind_action_port(adapter)
    request = ActionRequest.bind(
        adapter, "duration-boundary", "mute", 5, member=3, minutes=minutes
    )
    if type(minutes) is int and 1 <= minutes <= 1440:
        receipt = await adapter.execute_action(request)
        assert receipt["status"] == "accepted"
        assert adapter.execute_moderation.await_args.kwargs["minutes"] == minutes
        adapter.execute_moderation.assert_awaited_once()
    else:
        with pytest.raises(ProtocolError, match="^action_parameters_invalid$"):
            await adapter.execute_action(request)
        adapter.execute_moderation.assert_not_awaited()
    adapter.get_moderation_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_counter_table_is_empty(tmp_path, monkeypatch, managed_group):
    import sqlite3

    from astrbot.builtin_stars.wangshangliao_moderation import commands as module

    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        db.execute("CREATE TABLE operations(id TEXT)")
    commands, event = managed_group
    assert await commands.run(event, "违规计数") == "暂无业务规则记录。"
    event.platform.execute_moderation.assert_not_awaited()
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("operations",)]


@pytest.mark.asyncio
async def test_violation_counter_has_no_retired_kick_threshold(
    tmp_path, monkeypatch, managed_group
):
    import sqlite3

    from astrbot.builtin_stars.wangshangliao_moderation import commands as module

    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        db.execute(
            "CREATE TABLE content_violations(account TEXT,group_id TEXT,"
            "sender TEXT,category TEXT,status TEXT,observed REAL)"
        )
        db.execute(
            "CREATE TABLE automatic_mutes(account TEXT,group_id TEXT,"
            "member TEXT,closed INTEGER,status TEXT)"
        )
        db.executemany(
            "INSERT INTO automatic_mutes VALUES('1','5','3',0,'accepted')",
            [() for _ in range(4)],
        )
    commands, event = managed_group
    event.platform.config["moderation"] = {
        "content_rules_since": 1,
        "auto_kick": {"5": True},
    }
    result = await commands.run(event, "违规计数")
    assert "受理 4 次" in result
    assert "/3" not in result
    assert "自动处理不踢人" in result
    assert "人工踢出请使用旺商聊App" in result
    event.platform.execute_moderation.assert_not_awaited()
    event.platform.send_reply_text.assert_not_awaited()
