"""Offline owner-template permissions, versioning and transaction acceptance."""

import json
import sqlite3

import pytest
from test_superbot_community import community, private  # noqa: F401
from test_superbot_game_hardening import flow  # noqa: F401
from test_superbot_moderation import Member, setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_template import KEY, action
from data.plugins.astrbot_plugin_superbot.store import Rejected, encode


@pytest.fixture
def template(flow):  # noqa: F811
    s = flow
    s.runtime.group_game = s.flow
    with s.store.tx() as db:
        s.store.put(db, "modules", {"moderation": True, "game": True, "points": True})
    return s


async def preview(s, mode="default", chat="-1001"):
    """Get a real private callback produced by the normal preview renderer."""
    await action(
        s.runtime.ui,
        private(),
        {"action": "mod_tpl_preview", "chat": chat, "mode": mode},
    )
    token = (
        s.bot.send_message.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data[3:]
    )
    return token, s.store.resolve(token, "1", "1")


@pytest.mark.asyncio
async def test_default_atomic_apply_owner_only_preserves_sensitive_state(template):
    s = template
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Other',1)"
    )
    before = s.store.balance("2")
    token, payload = await preview(s)
    assert s.runtime.community.policy("-1001")["version"] == 0
    for uid in (2, 3):
        with pytest.raises(Rejected):
            await action(s.runtime.ui, private(uid), payload, token)
    await action(s.runtime.ui, private(), payload, token)
    assert all(s.runtime.community.policy("-1001")["config"]["enabled"].values())
    assert not s.store.db.execute("SELECT 1 FROM gb_policy").fetchone()
    assert s.runtime.community.policy("-1002")["version"] == 0
    assert s.runtime.ad_killer.policy("-1001")["version"] == 0
    assert s.store.balance("2") == before
    assert not s.store.db.execute("SELECT 1 FROM mod_acl").fetchone()
    assert s.bot.ban_chat_member.await_count == 0
    with pytest.raises(Rejected):
        await action(s.runtime.ui, private(), payload, token)


@pytest.mark.asyncio
async def test_manager_template_and_ad_policy_without_group_acl(template):
    s = template
    s.store.accept_grant("2", s.store.grant("1", "2", ["manager"], 30))
    s.members[2].status = "administrator"
    await action(
        s.runtime.ui,
        private(2),
        {"action": "mod_tpl_preview", "chat": "-1001", "mode": "default"},
    )
    token = (
        s.bot.send_message.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data[3:]
    )
    payload = s.store.resolve(token, "2", "2")
    await action(s.runtime.ui, private(2), payload, token)
    assert all(s.runtime.community.policy("-1001")["config"]["enabled"].values())
    cfg = json.loads(s.runtime.ad_killer.policy("-1001")["config"])
    version = s.runtime.ad_killer.policy("-1001")["version"]
    s.runtime.ad_killer.save("-1001", "2", version, cfg, True)
    s.store.revoke("1", "2")
    with pytest.raises(Rejected):
        await action(
            s.runtime.ui, private(2), {"action": "mod_tpl_home", "chat": "-1001"}
        )


@pytest.mark.asyncio
async def test_capture_and_reuse_preserves_target_whitelists_and_log(template):
    s = template
    token, payload = await preview(s)
    await action(s.runtime.ui, private(), payload, token)
    cfg = json.loads(s.runtime.ad_killer.policy("-1001")["config"])
    cfg["rules"]["links"]["enabled"] = True
    cfg["domains"] = ["source.example"]
    s.runtime.ad_killer.save("-1001", "1", 0, cfg, True)
    token, payload = await preview(s, "capture")
    await action(s.runtime.ui, private(), payload, token)
    saved = s.store.get(KEY)
    assert "domains" not in saved["killer"]["config"]
    assert "log" not in saved["content"]
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Other',1)"
    )
    cfg["domains"] = ["target.example"]
    cfg["rules"]["links"]["enabled"] = False
    s.runtime.ad_killer.save("-1002", "1", 0, cfg, False)
    target = s.runtime.community.policy("-1002")["config"]
    target["log"] = {"enabled": True, "channel": "-1009", "title": "Local"}
    s.store.db.execute(
        "INSERT INTO cm_config(chat,actor,version,config) VALUES('-1002','1',1,?)",
        (encode(target),),
    )
    token, payload = await preview(s, "saved", "-1002")
    await action(s.runtime.ui, private(), payload, token)
    result = s.runtime.ad_killer.policy("-1002")
    assert result["enabled"] == 1
    assert json.loads(result["config"])["domains"] == ["target.example"]
    assert json.loads(result["config"])["rules"]["links"]["enabled"]
    assert s.runtime.community.policy("-1002")["config"]["log"] == target["log"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["target", "template", "expiry", "membership", "disabled"]
)
async def test_stale_or_unauthorized_confirmation_rejected(template, change):
    s = template
    token, payload = await preview(s)
    if change == "target":
        s.store.db.execute("UPDATE mod_groups SET version=version+1")
    elif change == "template":
        with s.store.tx() as db:
            s.store.put(db, KEY, {"schema": 1})
    elif change == "expiry":
        s.clock[0] += 601
    elif change == "membership":
        s.members[1] = Member("member")
    else:
        s.store.db.execute("UPDATE mod_groups SET enabled=0")
    with pytest.raises(Rejected):
        await action(s.runtime.ui, private(), payload, token)
    assert s.runtime.community.policy("-1001")["version"] == 0
    assert not s.store.db.execute("SELECT 1 FROM gb_policy").fetchone()


@pytest.mark.asyncio
async def test_database_failure_rolls_back_configuration_and_confirmation(template):
    s = template
    token, payload = await preview(s)
    s.store.db.execute(
        "CREATE TRIGGER fail_content BEFORE INSERT ON cm_config BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await action(s.runtime.ui, private(), payload, token)
    assert s.runtime.community.policy("-1001")["version"] == 0
    assert (
        s.store.db.execute(
            "SELECT used FROM callbacks WHERE token=?", (token,)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_saved_punishment_requires_current_permissions_and_no_fault(template):
    s = template
    cfg = json.loads(s.runtime.ad_killer.policy("-1001")["config"])
    cfg["rules"]["links"] = {"enabled": True, "action": "mute"}
    s.runtime.ad_killer.save("-1001", "1", 0, cfg, True)
    token, payload = await preview(s, "capture")
    await action(s.runtime.ui, private(), payload, token)
    token, payload = await preview(s, "saved")
    s.members[9].can_restrict_members = False
    with pytest.raises(Rejected):
        await action(s.runtime.ui, private(), payload, token)
    s.members[9].can_restrict_members = True
    s.store.db.execute("UPDATE ak_policies SET error='unknown_request'")
    with pytest.raises(Rejected):
        await action(s.runtime.ui, private(), payload, token)
    assert (
        s.store.db.execute(
            "SELECT used FROM callbacks WHERE token=?", (token,)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_navigation_and_group_use_never_apply(template):
    s = template
    await action(s.runtime.ui, private(), {"action": "mod_tpl_home", "chat": "-1001"})
    assert "内置基础模板" in str(s.bot.send_message.await_args.kwargs["reply_markup"])
    await action(
        s.runtime.ui,
        private(),
        {"action": "mod_tpl_details", "chat": "-1001", "mode": "default"},
    )
    assert s.runtime.community.policy("-1001")["version"] == 0
    update = private()
    update.effective_chat.type = "supergroup"
    with pytest.raises(Rejected):
        await action(s.runtime.ui, update, {"action": "mod_tpl_home", "chat": "-1001"})


@pytest.mark.asyncio
async def test_authorized_group_admin_cannot_use_owner_template(template):
    s = template
    s.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES('2',?,?)",
        (encode(["moderation"]), s.clock[0] + 3600),
    )
    s.store.db.execute("INSERT INTO mod_acl(uid,chat) VALUES('2','-1001')")
    s.members[2] = Member("administrator")
    await s.runtime.ui.action(private(2), {"action": "mod_group", "chat": "-1001"})
    assert "超管群配置模板" not in str(
        s.bot.send_message.await_args.kwargs["reply_markup"]
    )
    with pytest.raises(Rejected):
        await action(
            s.runtime.ui, private(2), {"action": "mod_tpl_home", "chat": "-1001"}
        )
    await s.runtime.ui.action(private(), {"action": "mod_group", "chat": "-1001"})
    assert "超管群配置模板" in str(s.bot.send_message.await_args.kwargs["reply_markup"])
