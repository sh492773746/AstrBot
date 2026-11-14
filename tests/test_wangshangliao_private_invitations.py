"""Group-bound invitation summaries and cashier contact privacy."""

import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.message_components import Plain
from astrbot.builtin_stars.wangshangliao_moderation.cashback import CashbackService
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent


@pytest.fixture
def setup(tmp_path):
    adapter = SimpleNamespace(
        account="11",
        config={
            "id": "fixture",
            "type": "wangshangliao",
            "enable": True,
            "reply_private": False,
            "enabled_groups": ["5", "6", "8"],
            "_business_active": True,
            "_business_epoch": 1,
            "_business_revision": 1,
        },
        meta=lambda: PlatformMetadata("wangshangliao", "test", id="fixture"),
        execute_moderation=AsyncMock(),
    )
    context = SimpleNamespace(
        get_config=lambda *args, **kwargs: {"admins_id": []},
        platform_manager=SimpleNamespace(platform_insts=[adapter]),
    )
    service = CashbackService(context, tmp_path, "fixture")
    store = service.store
    owner = "owner@example.com"
    store.bootstrap([{"email": owner, "name": "SECRET_OWNER"}])
    baseline = {
        uid: {"name": f"用户{uid}", "peer": str(1000 + int(uid)), "eligible": True}
        for uid in ("21", "22")
    }
    for group, name, bot, account in [
        ("5", "本人活动群", "fixture", "11"),
        ("6", "本人核验群", "fixture", "11"),
        ("7", "SECRET_UNSELECTED_GROUP", "fixture", "11"),
        ("8", "SECRET_OTHER_BOT_GROUP", "other", "33"),
    ]:
        store.configure(
            owner,
            {
                "id": group,
                "name": name,
                "bot_id": bot,
                "account": account,
                "enabled": True,
                "rate": 500,
                "contact": "fixture-cashier",
                "version": 0,
            },
            baseline,
            set(),
        )
        members = {
            **baseline,
            "23": {"name": "本人邀请成员", "peer": "1023", "eligible": True},
            "24": {"name": "SECRET_OTHER_INVITEE", "peer": "1024", "eligible": True},
        }
        store.snapshot(
            group,
            bot,
            account,
            members,
            [
                {
                    "member": uid,
                    "inviter": inviter,
                    "source": "business_apply_log",
                    "event_id": f"{group}-{uid}",
                }
                for uid, inviter in (("23", "22"), ("24", "21"))
            ],
        )
    with store.connect(True) as db:
        db.execute("UPDATE entries SET amount=9876500 WHERE inviter='21'")
        db.execute(
            "UPDATE entries SET state='verifying',hold_until=? WHERE group_id='6' AND inviter='22'",
            (time.time() + 10800,),
        )
    service.activities.authorize = lambda adapter, group: None

    def event(sender="22", text="我的邀请"):
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.self_id = "11"
        message.session_id = "11/5"
        message.group_id = "5"
        message.message_id = str(time.monotonic_ns())
        message.sender = MessageMember(sender, "管理员")
        message.message_str = text
        message.message = [Plain(text)]
        result = WangshangliaoEvent(message, adapter, True)
        from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity

        result.actor_identity = ActorIdentity(
            "11", sender, str(1000 + int(sender)), sender
        )
        result.role = "member"
        result.set_extra(
            "wangshangliao_payload",
            {"text": text, "sender": sender, "created_at": int(time.time() * 1000)},
        )
        result.send = AsyncMock()
        return result

    return service, adapter, event


def snapshot(store):
    with sqlite3.connect(store.path) as db:
        return "\n".join(db.iterdump())


@pytest.mark.asyncio
async def test_contact_is_group_bound_public_text_and_not_an_identity(setup):
    service, _, make = setup
    with service.store.connect(True) as db:
        db.execute("UPDATE groups SET contact='微信：cashier5' WHERE id='5'")
        db.execute("UPDATE groups SET contact='微信：cashier6' WHERE id='6'")
        db.execute(
            "UPDATE groups SET contact='SECRET_OTHER_CONTACT' WHERE id IN ('7','8')"
        )
    before = snapshot(service.store)
    reply = await service.invitation_reply(make(), "5", "我的邀请")
    assert "请联系本群结账员：微信：cashier5。" in reply
    assert "cashier6" not in reply and "SECRET" not in reply
    assert "owner@example.com" not in reply
    assert snapshot(service.store) == before


@pytest.mark.asyncio
async def test_unconfigured_contact_does_not_invent_a_cashier(setup):
    service, _, make = setup
    with service.store.connect(True) as db:
        db.execute("UPDATE groups SET contact='' WHERE id='5'")
    reply = await service.invitation_reply(make(), "5", "我的邀请")
    assert "联系方式尚未配置" in reply
    assert "SECRET_OWNER" not in reply and "owner@example.com" not in reply


@pytest.mark.asyncio
async def test_contact_content_is_plain_redacted_data_not_an_operation(setup):
    service, adapter, make = setup
    with service.store.connect(True) as db:
        db.execute(
            "UPDATE groups SET contact=? WHERE id='5'",
            ("微信：客服 token=private-contact-secret <script>do_evil()</script>",),
        )
    before = snapshot(service.store)
    reply = await service.invitation_reply(make(), "5", "我的邀请")
    assert "微信：客服" in reply and "private-contact-secret" not in reply
    assert "<script>do_evil()</script>" in reply
    adapter.execute_moderation.assert_not_awaited()
    assert snapshot(service.store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["我的邀请", "我的邀请明细"])
async def test_group_reply_has_contact_and_correct_invitee_price_labels(setup, action):
    service, _, make = setup
    event = make(sender="22")
    service.activities.authorize = lambda adapter, group: None
    with service.store.connect(True) as db:
        db.execute("UPDATE groups SET agent_rate=1000 WHERE id='5'")
    reply = await service.invitation_reply(event, "5", action)
    assert "如核算有问题，请联系本群结账员：fixture-cashier。" in reply
    assert "代理商邀请人" not in reply and "普通邀请人" not in reply
    if action == "我的邀请":
        assert "普通成员单价：5.00 元" in reply
        assert "代理商成员单价：10.00 元" in reply


@pytest.mark.asyncio
async def test_group_invitation_summary_explains_three_hour_qualification(setup):
    service, _, make = setup
    event = make(sender="22")
    service.activities.authorize = lambda adapter, group: None
    with service.store.connect(True) as db:
        db.execute(
            "UPDATE entries SET state='verifying',hold_until=? "
            "WHERE group_id='5' AND inviter='22'",
            (time.time() + 10800,),
        )
    before = snapshot(service.store)
    reply = await service.invitation_reply(event, "5", "我的邀请")
    assert "核验中（3小时未到）：1 笔" in reply
    assert "如核算有问题，请联系本群结账员：fixture-cashier。" in reply
    assert snapshot(service.store) == before
