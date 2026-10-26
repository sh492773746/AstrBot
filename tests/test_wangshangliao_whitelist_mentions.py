"""Real mention-only whitelist additions retain native and tenant authorization."""

from dataclasses import replace

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import member_exemptions
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao import blacklist
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_member_whitelist import whitelist as whitelist

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


def mention(event, text="@Target 白名单"):
    """Attach a transport-verified span without replacing freshness evidence."""
    start = len(text[: text.index("@")].encode("utf-16-le")) // 2
    payload = event.get_extra("wangshangliao_payload")
    payload.update(
        text=text,
        mentions=["903"],
        mention_spans=[
            {"uid": "903", "nick": "Target", "start": start, "end": start + 7}
        ],
    )
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["@Target 白名单", "白名单 @Target", "白名单@Target"])
async def test_mention_directly_adds_once_without_confirmation(whitelist, text):
    value, event, _ = whitelist
    mention(event, text)
    assert recognize_command(text) == "白名单 @Target"
    reply = await step(value, event, text, native=True)
    assert "已加入本群发言白名单" in reply
    assert not value.member_whitelist.pending
    instance = event.platform.config["id"]
    assert member_exemptions.is_exempt(instance, "1", "5", "3", "903")
    assert not member_exemptions.is_exempt(instance, "1", "6", "3", "903")
    assert not member_exemptions.is_exempt(instance, "1", "5", "3", "999")
    assert "无需重复加入" in await step(value, event, text, native=True)
    assert member_exemptions.read_exemptions(instance, "1", "5")[0] == 1
    value.context.get_provider.assert_not_called()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "typed",
        "multiple",
        "span",
        "unknown_peer",
        "left",
        "wrong_actor",
        "ordinary",
        "revoked",
        "banned",
        "bad_reply",
        "private",
    ],
)
async def test_unproven_or_unauthorized_mentions_never_add(whitelist, change):
    value, event, rows = whitelist
    payload = mention(event)
    if change == "typed":
        payload["mentions"] = []
        payload["mention_spans"] = []
    elif change == "multiple":
        payload["mentions"].append("902")
    elif change == "span":
        payload["mention_spans"][0]["end"] = 2
    elif change == "unknown_peer":
        payload["mentions"] = ["999"]
    elif change == "left":
        rows.pop("3")
    elif change == "wrong_actor":
        event.actor_identity = replace(event.actor_identity, peer="999")
    elif change == "ordinary":
        rows["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif change == "revoked":
        value.management.assignments[value.management.key(event.platform, "5")] = ()
    elif change == "banned":
        rows["3"]["accountState"] = "ACCOUNT_STATE_BAN"
    elif change == "bad_reply":
        payload["reply"] = {"server": "forged"}
    else:
        event.is_private_chat = lambda: True
        event.get_group_id = lambda: ""
        event.eligible = True
    assert "已加入" not in await step(value, event, "@Target 白名单", native=True)
    assert member_exemptions.read_exemptions(event.platform.config["id"], "1", "5") == (
        0,
        {},
    )
    assert not value.member_whitelist.pending
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_blacklist_conflict_is_not_automatically_transferred(
    whitelist, tmp_path, monkeypatch
):
    value, event, _ = whitelist
    monkeypatch.setattr(blacklist, "instance_dir", lambda _: tmp_path)
    store = blacklist.BlacklistStore(event.platform.config["id"])
    store.set_member(
        "1", "5", "3", "903", "Target", True, event.actor_identity, lambda: None
    )
    mention(event)
    reply = await step(value, event, "@Target 白名单", native=True)
    assert "另一份名单" in reply
    assert store.read("1", "5")[1][0]["member"] == "3"
    assert member_exemptions.read_exemptions(event.platform.config["id"], "1", "5") == (
        0,
        {},
    )


@pytest.mark.asyncio
async def test_last_moment_identity_change_rolls_back_direct_add(
    whitelist, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import member_whitelist

    value, event, _ = whitelist
    mention(event)
    original = member_exemptions.set_exemption

    def changed(*args, **kwargs):
        value.member_evidence[("1", "5")]["3"]["nimId"] = "999"
        return original(*args, **kwargs)

    monkeypatch.setattr(member_whitelist, "set_exemption", changed)
    assert "已加入" not in await step(value, event, "@Target 白名单", native=True)
    assert member_exemptions.read_exemptions(event.platform.config["id"], "1", "5") == (
        0,
        {},
    )


@pytest.mark.asyncio
async def test_old_add_command_only_explains_new_way(whitelist):
    value, event, _ = whitelist
    reply = await step(value, event, "添加白名单 Target", native=True)
    assert "真正@" in reply and "无需搜索或确认" in reply
    assert not value.member_whitelist.pending
