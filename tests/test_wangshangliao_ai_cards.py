"""Private AI card controls reuse persistent previews and current authority."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import ai_config, cards
from astrbot.builtin_stars.wangshangliao_moderation.main import Main


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda uid: uid == "4")
    admins = ["9"]
    context = SimpleNamespace(
        get_config=lambda _: {"admins_id": admins},
        get_all_providers=lambda: [],
    )
    member = {
        "userId": "2",
        "nimId": "n2",
        "groupRole": "GROUP_ROLE_MEMBER",
        "groupMemberNick": "原群名片",
        "display_name": "张三",
    }
    adapter = SimpleNamespace(
        account="1",
        groups={"10": {}, "11": {}},
        group_names={"10": "目标群", "11": "另一个群"},
        config={
            "id": "fixture",
            "enable": True,
            "enabled_groups": ["10", "11"],
            "moderation": {
                "enabled": True,
                "permissions": {"10": ["rename"], "11": ["rename"]},
                "card_auto": {"10": False, "11": True},
                "manual_kick_only": True,
                "auto_kick": {"10": False},
            },
        },
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "1", "nimId": "n1", "groupRole": "GROUP_ROLE_ADMIN"},
                    copy.deepcopy(member),
                ],
            }
        ),
        rename_member=AsyncMock(return_value={"status": "verified"}),
    )
    extras = {}
    event = SimpleNamespace(
        platform=adapter,
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: "9" in admins,
        get_sender_id=lambda: "9",
        unified_msg_origin="bot:FriendMessage:9",
        get_extra=extras.get,
        set_extra=extras.__setitem__,
        message_str="把张三的群名片改成值班小张",
        message_obj=SimpleNamespace(message_id="preview"),
    )
    plugin = Main(context)
    selection = {"expires": time.monotonic() + 600, "group": "10", "members": [member]}
    plugin.commands.selections[(id(adapter), "1", "9", event.unified_msg_origin)] = (
        selection
    )
    return plugin, event, adapter, admins, selection


async def preview(plugin, event, **override):
    value = {"member_number": 1, "card_name": "值班小张", **override}
    return json.loads(
        await plugin.private_management(event, "card_member_preview", json.dumps(value))
    )


@pytest.mark.asyncio
async def test_single_member_preview_then_separate_confirmation_executes_once(setup):
    plugin, event, adapter, _, _ = setup
    result = await preview(plugin, event)
    assert result["status"] == "preview"
    assert result["group"] == "10"
    assert result["items"] == [
        {"member": "2", "original": "原群名片", "name": "值班小张", "state": "pending"}
    ]
    assert "目标群" in result["display_text"]
    assert event.get_extra("wsl_config_display") == result["display_text"]
    adapter.rename_member.assert_not_awaited()
    event.message_str = "确认修改名片"
    assert (
        json.loads(
            await plugin.private_management(event, "card_execute", result["preview_id"])
        )["status"]
        == "rejected"
    )
    event.message_obj.message_id = "confirmation"
    queued = json.loads(
        await plugin.private_management(event, "card_execute", result["preview_id"])
    )
    assert queued["status"] == "queued"
    await asyncio.gather(*list(plugin.cards.tasks))
    adapter.rename_member.assert_awaited_once()
    args = adapter.rename_member.await_args.args
    assert args[1:] == (10, 2, "值班小张", "原群名片", "n2")
    await plugin.private_management(event, "card_execute", result["preview_id"])
    assert adapter.rename_member.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "nonadmin",
        "group",
        "expired_snapshot",
        "wrong_peer",
        "protected_role",
        "no_permission",
    ],
)
async def test_unsafe_member_preview_rejected(setup, case):
    plugin, event, adapter, admins, selection = setup
    if case == "nonadmin":
        admins.clear()
    elif case == "group":
        event.is_private_chat = lambda: False
    elif case == "expired_snapshot":
        selection["expires"] = 0
    elif case == "wrong_peer":
        adapter.get_moderation_members.return_value["groupMemberInfo"][1]["nimId"] = (
            "different"
        )
    elif case == "protected_role":
        adapter.get_moderation_members.return_value["groupMemberInfo"][1][
            "groupRole"
        ] = "GROUP_ROLE_ADMIN"
    else:
        adapter.config["moderation"]["permissions"]["10"] = []
    assert (await preview(plugin, event))["status"] == "rejected"
    adapter.rename_member.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {"member_number": True},
        {"member_number": 2},
        {"card_name": ""},
        {"card_name": "a\nb"},
        {"card_name": "海" * 86},
        {"member": "2"},
    ],
)
async def test_member_preview_arguments_are_strict(setup, value):
    plugin, event, adapter, _, _ = setup
    assert (await preview(plugin, event, **value))["status"] == "rejected"
    adapter.rename_member.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "other_admin",
        "other_session",
        "expired",
        "revoke",
        "no_grant",
        "negative_confirmation",
    ],
)
async def test_confirmation_remains_caller_bound_and_rechecks_authority(setup, case):
    plugin, event, adapter, admins, _ = setup
    result = await preview(plugin, event)
    event.message_obj.message_id = "confirm"
    event.message_str = "确认修改名片"
    if case == "other_admin":
        admins.append("8")
        event.get_sender_id = lambda: "8"
    elif case == "other_session":
        event.unified_msg_origin = "other:FriendMessage:9"
    elif case == "expired":
        owner = json.dumps(["1", "9", event.unified_msg_origin])
        job = plugin.cards.status(adapter, result["preview_id"], owner)
        job["expires"] = 0
        plugin.cards.save(adapter, job)
    elif case == "revoke":
        admins.clear()
    elif case == "no_grant":
        adapter.config["moderation"]["permissions"]["10"] = []
    else:
        event.message_str = "不要确认修改名片"
    outcome = json.loads(
        await plugin.private_management(event, "card_execute", result["preview_id"])
    )
    assert outcome["status"] == "rejected"
    adapter.rename_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_revocation_after_queue_stops_background_job(setup):
    plugin, event, adapter, admins, _ = setup
    result = await preview(plugin, event)
    event.message_obj.message_id = "confirm"
    event.message_str = "确认修改名片"
    queued = json.loads(
        await plugin.private_management(event, "card_execute", result["preview_id"])
    )
    assert queued["status"] == "queued"
    admins.clear()
    await asyncio.gather(*list(plugin.cards.tasks))
    adapter.rename_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_auto_preview_and_confirm_changes_only_named_group(
    setup, monkeypatch
):
    plugin, event, adapter, _, _ = setup
    original = copy.deepcopy(adapter.config)
    persisted = SimpleNamespace(
        get=lambda key, default=None: (
            [copy.deepcopy(original)] if key == "platform" else default
        ),
        save_config_async=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ai_config, "astrbot_config", persisted)
    proposal = json.dumps(
        {"target": "moderation", "group": "目标群", "changes": {"card_auto": True}}
    )
    result = json.loads(
        await plugin.private_management(event, "config_preview", proposal)
    )
    assert result["status"] == "preview"
    assert result["changes"]["before"]["card_auto"] is False
    assert result["changes"]["after"]["card_auto"] is True
    assert "关闭不恢复" in result["display_text"]
    assert adapter.config == original
    token = result["confirmation"].split()[-1]
    event.message_str = "确认设置 " + token
    result = json.loads(await plugin.private_management(event, "config_confirm", token))
    assert result["status"] == "saved"
    expected = copy.deepcopy(original)
    expected["moderation"]["card_auto"]["10"] = True
    assert adapter.config == expected
    persisted.save_config_async.assert_awaited_once()
    assert (
        json.loads(await plugin.private_management(event, "config_confirm", token))[
            "status"
        ]
        == "rejected"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False, {"10": True}])
async def test_card_auto_no_implicit_permission_grant_and_disable_allowed(setup, value):
    plugin, event, adapter, _, _ = setup
    adapter.config["moderation"]["permissions"]["10"] = []
    original = copy.deepcopy(adapter.config)
    proposal = json.dumps(
        {"target": "moderation", "group": "10", "changes": {"card_auto": value}}
    )
    result = json.loads(
        await plugin.private_management(event, "config_preview", proposal)
    )
    assert result["status"] == ("preview" if value is False else "rejected")
    assert adapter.config == original


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["other_admin", "other_session", "revoked", "expired", "changed_config"])
async def test_card_auto_confirmation_is_bound_and_conflict_protected(setup, case):
    plugin, event, adapter, admins, _ = setup
    proposal = json.dumps({"target": "moderation", "group": "目标群", "changes": {"card_auto": True}})
    result = json.loads(await plugin.private_management(event, "config_preview", proposal))
    token = result["confirmation"].split()[-1]
    if case == "other_admin":
        admins.append("8")
        event.get_sender_id = lambda: "8"
    elif case == "other_session":
        event.unified_msg_origin = "other:FriendMessage:9"
    elif case == "revoked":
        admins.clear()
    elif case == "expired":
        plugin.ai_config.drafts[plugin.ai_config._key(event)]["expires"] = 0
    else:
        adapter.config["moderation"]["permissions"]["10"] = []
    event.message_str = "确认设置 " + token
    assert json.loads(await plugin.private_management(event, "config_confirm", token))["status"] == "rejected"
    assert adapter.config["moderation"]["card_auto"] == {"10": False, "11": True}
    assert adapter.config["moderation"]["manual_kick_only"] is True
