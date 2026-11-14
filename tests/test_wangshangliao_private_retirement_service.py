"""Private business removal without losing group commands or media codecs."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import feature_image
from astrbot.builtin_stars.wangshangliao_moderation.command_menu import CommandMenu
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    OpenService,
    recognize_integrated,
)

ROOT = Path(feature_image.__file__).parent


@pytest.mark.parametrize(
    "name",
    [
        "friend_auto",
        "owner_support",
        "private_batches",
        "private_interaction",
        "private_invitation",
        "guidance_trial",
        "consultation_access",
        "service_settings",
        "finance_chat",
    ],
)
def test_private_business_modules_physically_removed(name):
    assert not (ROOT / f"{name}.py").exists()


@pytest.mark.parametrize(
    "name",
    [
        "verify_actor",
        "menu_command",
        "plan_command",
        "notify_quota",
        "chat",
        "_chat",
        "end_chat",
    ],
)
def test_private_service_methods_physically_removed(name):
    assert not hasattr(OpenService, name)


@pytest.mark.parametrize(
    "action",
    [
        "activation_code",
        "guidance_trial_preview",
        "guidance_trial",
        "support_settings",
        "support_editor",
        "owner_support",
        "provision_profiles",
        "finance_test",
        "finance_members",
        "finance_grant",
        "test_guides",
    ],
)
def test_private_actions_absent_from_valid_control_schema(action):
    with pytest.raises(ValidationError):
        OpenCall(action=action, actor="dashboard/test")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["on_event", "_on_event"])
@pytest.mark.parametrize("text", ["菜单", "/chat", "普通私信", "黑名单", "开通"])
async def test_non_group_event_never_initializes_or_dispatches(entry, text):
    service = OpenService.__new__(OpenService)
    event = SimpleNamespace(
        message_str=text,
        is_private_chat=lambda: True,
        set_extra=Mock(),
        stop_event=Mock(),
    )
    await getattr(service, entry)(event)
    event.set_extra.assert_called_once_with("_component_claimed", True)
    event.stop_event.assert_called_once_with()
    assert vars(service) == {}


@pytest.mark.parametrize(
    "text",
    [
        "/chat",
        "/exit",
        "菜单",
        "我的套餐",
        "我的额度",
        "我的知识",
        "客服列表",
        "选择群 1",
        "客服设置",
    ],
)
def test_private_words_have_no_group_command_route(text):
    assert recognize_integrated(text) == ""


@pytest.mark.parametrize(
    "text",
    [
        "排名",
        "排行",
        "发言排行榜",
        "参与抽奖",
        "我的邀请",
        "我的邀请明细",
        "黑名单列表",
    ],
)
def test_existing_group_routes_remain_available(text):
    assert recognize_integrated(text)


def test_private_menu_and_guide_business_absent_but_group_image_codec_present():
    assert not hasattr(CommandMenu, "items_for")
    assert not hasattr(CommandMenu, "advance")
    assert not hasattr(feature_image, "FeatureImage")
    assert not hasattr(feature_image, "FeatureGuides")
    image = feature_image.ImageMessage(
        sender={"id": 12},
        target={"id": 34},
        session=2,
        format=1,
        image={"asset": {"uri": "123456"}, "width": 10, "height": 10, "size": 100},
    )
    decoded = feature_image.ImageMessage.FromString(image.SerializeToString())
    assert decoded.image.asset.uri == "123456"
    assert decoded.session == 2
