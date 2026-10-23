"""Default chat shortcuts and explicit legacy navigation remain separate."""

from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.command_menu import (
    QUICK_TREE,
    Session,
)
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_command_parity import setup as setup

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest.mark.asyncio
async def test_default_menu_is_short_and_configuration_redirects_without_writes(setup):
    service, _, event = setup
    service.menu_command = AsyncMock(return_value="read only")
    reply = await step(service, event, "菜单", native=True, full=False)
    assert all(item.label in reply for item in QUICK_TREE)
    assert "组合规则管理" not in reply
    assert "无需" not in reply or "选择" in reply
    reply = await step(service, event, "插件设置", native=True, full=False)
    assert "规则（关键词回复、定时发言）" in reply
    assert "plugin-page/wangshangliao_moderation/management" in reply
    service.menu_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_tree_requires_explicit_full_menu(setup):
    service, _, event = setup
    quick = await step(service, event, "菜单", native=True, full=False)
    assert "审核规则" not in quick
    full = await step(service, event, "完整菜单", native=True, full=False)
    assert "审核规则" in full
    assert "群关键词回复与定时广告" in full


def test_default_screen_lists_direct_commands_and_no_configuration_questions(setup):
    service, _, event = setup
    state = Session(expires=1000, items=service.navigation.items_for(event))
    text = service.navigation.screen(state)
    assert "直接发送：规则、禁言名单、解禁 @成员" in text
    assert "请输入对应规则参数" not in text
