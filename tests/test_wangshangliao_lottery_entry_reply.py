"""Enrollment receipts reuse frozen public terms without identity suffixes."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.builtin_stars.wangshangliao_moderation import activity_store
from tests.test_wangshangliao_activities import configure, event, latest
from tests.test_wangshangliao_activities import setup as setup
from tests.test_wangshangliao_plain_commands import native_event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,full",
    [
        ("prize", False),
        ("ordinary", False),
        ("ordinary", True),
        ("lucky", False),
        ("lucky", True),
        ("tiered", False),
        ("tiered", True),
    ],
)
async def test_signup_repeats_frozen_notice_then_count_and_name(setup, mode, full):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET prize='群友福利',contact='活动管理员',"
            "packet_mode=?,total_amount=?,prize_tiers=?,luckiest_bonus=?,"
            "draw_on_full=?,integer_packets=?,by_invites=0,remarks=?",
            (
                mode,
                0 if mode == "prize" else 10000,
                json.dumps([{"amount": 3000, "count": 1}])
                if mode == "tiered"
                else "[]",
                2000 if mode == "lucky" else 0,
                int(full),
                int(mode != "prize"),
                "请使用活动头像\n领奖时核对昵称",
            ),
        )
    await service.command(caller, "5", "开启抽奖", "")
    announcement = adapter.send_reply_text.await_args.args[2]
    frozen = latest(adapter)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET prize='下一期活动',"
            "contact='另一管理员',remarks='下一期备注',duration=600"
        )
    now[0] += 20
    user = event(adapter, "2", private=False)
    reply = await service.command(user, "5", "参加抽奖", "")
    assert reply == announcement + "\n\n已参与人数：1\nMember 2，参加抽奖成功。"
    user.set_extra.assert_called_once_with("wsl_lottery_entry_result", True)
    assert "下一期" not in reply and "另一管理员" not in reply
    assert "UID" not in reply and "902" not in reply
    assert latest(adapter)["packet_amounts"] == frozen["packet_amounts"]
    assert adapter.send_reply_text.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["card", "profile", "sender", "none"])
@pytest.mark.parametrize("identity", ["uid", "peer", "account", "fullwidth_uid"])
async def test_signup_never_uses_an_identity_as_the_display_name(
    setup, source, identity
):
    service, adapter, roster, _, _, _ = setup
    caller = await configure(setup)
    await service.command(caller, "5", "开启抽奖", "")
    member = roster["2"]
    member["accountId"] = 123456789
    internal = {
        "uid": "2",
        "peer": "902",
        "account": "123456789",
        "fullwidth_uid": "２",
    }[identity]
    member["groupMemberNick"] = internal
    member["userNick"] = internal
    user = event(adapter, "2", private=False)
    user.get_sender_name = lambda: internal
    if source == "card":
        member["groupMemberNick"] = "小明"
    elif source == "profile":
        member["userNick"] = "小明"
    elif source == "sender":
        user.get_sender_name = lambda: "小明"
    reply = await service.command(user, "5", "参加抽奖", "")
    assert reply.split("\n\n")[-1] == (
        "已参与人数：1\n" + ("小明，" if source != "none" else "") + "参加抽奖成功。"
    )
    assert internal not in reply.split("\n\n")[-1].splitlines()[-1]


@pytest.mark.asyncio
async def test_concurrent_entries_report_unique_committed_counts(setup):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup, capacity=3)
    await service.command(caller, "5", "开启抽奖", "")
    replies = await asyncio.gather(
        *[
            service.command(event(adapter, uid, private=False), "5", "参加抽奖", "")
            for uid in ("2", "3", "4", "2")
        ]
    )
    assert sum(reply.endswith("参加抽奖成功。") for reply in replies) == 3
    for count in (1, 2, 3):
        assert sum(f"已参与人数：{count}\n" in reply for reply in replies) == 1
    assert replies.count("你已参加本次抽奖，请勿重复报名。") == 1


@pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")
@pytest.mark.asyncio
@pytest.mark.parametrize("keep_reply", [False, True])
async def test_native_signup_keeps_layout_and_recall_without_mention(
    monkeypatch, keep_reply
):
    message = native_event("参与抽奖")
    message.set_extra("wsl_plain_command", True)
    message.set_extra("wsl_lottery_entry_result", True)
    message.set_extra("wsl_keep_reply", keep_reply)
    message.get_sender_name = lambda: "123456789"
    message.platform.members["5"].pop("2", None)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    text = (
        "群友福利\n群内发送“参与抽奖”即可报名。\n\n已参与人数：1\n小明，参加抽奖成功。"
    )
    await message.send(MessageChain([Plain(text)]))
    call = message.platform.send_reply_text.await_args
    assert call.args[2] == text
    assert not call.kwargs.get("mentions")
    assert call.kwargs["auto_recall"] is True
    assert "123456789" not in call.args[2]


@pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")
@pytest.mark.asyncio
async def test_signup_marker_does_not_change_noncommand_replies(monkeypatch):
    message = native_event("你好")
    message.eligible = True
    message.set_extra("wsl_lottery_entry_result", True)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await message.send(MessageChain([Plain("你好")]))
    call = message.platform.send_reply_text.await_args
    assert call.args[2] == "@Member 你好"
    assert call.kwargs["mentions"][0]["uid"] == 902
