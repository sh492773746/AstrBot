"""Friend commands, native directory APIs and dashboard routes are retired."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.dashboard import DashboardPage
from astrbot.builtin_stars.wangshangliao_moderation.settings import BusinessSettings
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao.nim import NimClient


@pytest.mark.parametrize(
    "text",
    [
        "删除好友",
        "清理好友",
        "好友清理",
        "/删除好友 123",
        "批量删除好友 100",
        "同意好友申请",
        "自动同意好友",
        "拒绝好友申请",
        "好友列表",
        "好友搜索 fixture",
    ],
)
def test_friend_mutations_have_no_conversational_command(text):
    """Reject friendship mutations from every conversational identity.

    Args:
        text: Former cleanup or approval command.
    """
    assert not recognize_command(text)


def test_friend_cleanup_and_approval_implementations_are_removed():
    """Ensure removal extends beyond hiding the dashboard entry."""
    for module in ("friends", "friend_auto"):
        assert (
            importlib.util.find_spec(
                f"astrbot.builtin_stars.wangshangliao_moderation.{module}"
            )
            is None
        )
    assert not hasattr(DashboardPage, "friends")
    assert not hasattr(DashboardPage, "friend_approval")
    assert not hasattr(BusinessSettings, "friend_cleanup_hold")
    root = Path(__file__).resolve().parents[1]
    for component in ("FriendCleanup.vue", "FriendApproval.vue"):
        assert not (root / "dashboard/plugin-pages/wangshangliao" / component).exists()
    app = (root / "dashboard/plugin-pages/wangshangliao/App.vue").read_text()
    assert "FriendCleanup" not in app
    assert "FriendApproval" not in app


@pytest.mark.parametrize("entry", ["get_friends", "get_friend_names", "delete_friend"])
def test_native_friend_directory_and_mutation_apis_do_not_exist(entry):
    """Reject native friendship access rather than retaining directory codecs.

    Args:
        entry: Former native directory or mutation method.
    """
    assert not hasattr(NimClient, entry)
    assert not hasattr(object.__new__(NimClient), entry)


def test_dashboard_registration_removes_stale_friend_routes_and_keeps_group_routes():
    """Remove retired endpoints on reload without touching unrelated routes."""
    retired_handler = AsyncMock()
    retired = {
        "/wangshangliao_moderation/friends",
        "/wangshangliao_moderation/friend-approval",
    }
    unrelated = ("/other-plugin/fixture", retired_handler, ["GET"], "fixture")
    context = SimpleNamespace(
        registered_web_apis=[
            (route, retired_handler, [method], "retired")
            for route in sorted(retired)
            for method in ("GET", "POST")
        ]
        + [unrelated]
    )
    context.register_web_api = lambda *args: context.registered_web_apis.append(args)
    page = DashboardPage(context)
    for _ in range(2):
        page.register()
        routes = {api[0] for api in context.registered_web_apis}
        assert routes.isdisjoint(retired)
        assert "/wangshangliao_moderation/groups" in routes
        assert "/wangshangliao_moderation/group-managers" in routes
        assert unrelated in context.registered_web_apis
    retired_handler.assert_not_awaited()
