"""Offline acceptance of restriction ownership and persistent verification."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot_moderation import Member, setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.community import Community
from data.plugins.astrbot_plugin_superbot.join_verify import JoinVerify
from data.plugins.astrbot_plugin_superbot.store import Rejected, encode


@pytest.fixture
def verified(setup):  # noqa: F811
    s = setup
    s.runtime.report = lambda *args: None
    s.runtime.join_verify = JoinVerify(s.runtime)
    s.store.db.execute(
        "INSERT INTO jv_policies(chat,actor,targets,enabled) VALUES('-1001','1',?,1)",
        (encode([{"id": "-1002", "title": "News", "url": "https://t.me/news"}]),),
    )
    s.bot.get_chat = AsyncMock(
        side_effect=lambda chat: (
            s.group
            if str(chat) == "-1001"
            else SimpleNamespace(id=-1002, type="channel")
        )
    )
    original_member = s.bot.get_chat_member.side_effect
    s.bot.get_chat_member.side_effect = lambda chat, uid: (
        original_member(chat, uid)
        if str(chat) == "-1001"
        else Member("administrator" if uid == 9 else "member")
    )
    original_restrict = s.bot.restrict_chat_member.side_effect

    async def restrict(chat, uid, permissions, **kwargs):
        await original_restrict(chat, uid, permissions, **kwargs)
        s.members[uid].can_send_messages = bool(permissions.can_send_messages)

    s.bot.restrict_chat_member.side_effect = restrict
    s.user = SimpleNamespace(id=2, is_bot=False, full_name="Member <test>")
    return s


@pytest.mark.asyncio
async def test_join_duplicate_verified_and_cross_identity(verified):
    s = verified
    service = s.runtime.join_verify
    await service.join("-1001", s.user, 10)
    await service.join("-1001", s.user, 10)
    token = s.store.db.execute("SELECT token FROM jv_entries").fetchone()[0]
    assert s.bot.restrict_chat_member.await_count == 1
    for uid, chat in (("3", "-1001"), ("2", "-1002")):
        with pytest.raises(Rejected):
            await service.verify(token, uid, chat)
    assert "通过" in await service.verify(token, "2", "-1001")
    await service.verify(token, "2", "-1001")
    await service.join("-1001", s.user, 10)
    assert s.bot.restrict_chat_member.await_count == 2


@pytest.mark.asyncio
async def test_foreign_restriction_and_timeout_are_never_replayed(verified):
    s = verified
    s.members[2] = Member("restricted", external=True)
    await s.runtime.join_verify.join("-1001", s.user, 10)
    s.bot.restrict_chat_member.assert_not_awaited()
    s.members[2] = Member("member")
    s.bot.restrict_chat_member.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await s.runtime.join_verify.join("-1001", s.user, 11)
    service = JoinVerify(s.runtime)
    await service.join("-1001", s.user, 11)
    row = s.store.db.execute("SELECT * FROM jv_entries").fetchone()
    assert row["status"] == "review"
    with pytest.raises(Rejected):
        await service.verify(row["token"], "2", "-1001")
    assert s.bot.restrict_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_other_punishment_and_membership_denial(verified):
    s = verified
    await s.runtime.join_verify.join("-1001", s.user, 12)
    token = s.store.db.execute("SELECT token FROM jv_entries").fetchone()[0]
    original = s.bot.get_chat_member.side_effect
    s.bot.get_chat_member.side_effect = lambda chat, uid: (
        Member("left") if str(chat) == "-1002" and uid == 2 else original(chat, uid)
    )
    with pytest.raises(Rejected, match="还没有"):
        await s.runtime.join_verify.verify(token, "2", "-1001")
    s.clock[0] += 6
    s.bot.get_chat_member.side_effect = original
    s.store.db.execute("INSERT INTO mod_restrictions VALUES('-1001','2','{}')")
    with pytest.raises(Rejected, match="其他处罚"):
        await s.runtime.join_verify.verify(token, "2", "-1001")
    assert s.bot.restrict_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_welcome_and_rules_merge_without_changing_verification(verified):
    s = verified
    s.runtime.community = Community(s.runtime)
    cfg = s.runtime.community.policy("-1001")["config"]
    cfg["enabled"].update(welcome=True, rules=True)
    cfg["welcome"] = "Hello {name}, welcome to {group}"
    cfg["rules"] = "Be kind"
    await s.runtime.community.save("1", "-1001", 0, cfg)
    await s.runtime.join_verify.join("-1001", s.user, 13)
    await s.runtime.community.welcome("-1001", s.user, 13, "Fixture")
    s.bot.send_message.assert_awaited_once()
    sent = s.bot.send_message.await_args.kwargs
    assert "Hello Member <test>" in sent["text"]
    assert sent["parse_mode"] is None
    assert "我已加入" in str(sent["reply_markup"])
    assert "cmrules_-1001" in str(sent["reply_markup"])
    token = s.store.db.execute("SELECT token FROM jv_entries").fetchone()[0]
    assert "通过" in await s.runtime.join_verify.verify(token, "2", "-1001")
