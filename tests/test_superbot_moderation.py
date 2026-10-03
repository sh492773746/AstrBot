"""Offline moderation acceptance with a stateful Telegram substitute."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ChatPermissions

from data.plugins.astrbot_plugin_superbot.moderation import TZ, Moderation, boundary
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store
from data.plugins.astrbot_plugin_superbot.ui import UI


class Member:
    def __init__(self, status, **kwargs):
        self.status = status
        self.can_restrict_members = True
        self.can_delete_messages = True
        self.extra = kwargs

    def to_dict(self):
        return {"status": self.status, **self.extra}


@pytest.fixture
def setup(tmp_path):
    clock = [datetime(2026, 9, 22, 22, 59, 50, tzinfo=TZ).timestamp()]
    store = Store(tmp_path / "mod.db", "1", lambda: clock[0])
    permissions = ChatPermissions.all_permissions()
    group = SimpleNamespace(
        id=-1001, title="Fixture", type="supergroup", permissions=permissions
    )
    members = {1: Member("creator"), 9: Member("administrator"), 2: Member("member")}
    bot = SimpleNamespace(id=9, username="fixture", send_message=AsyncMock())
    bot.get_chat = AsyncMock(return_value=group)
    bot.get_chat_member = AsyncMock(side_effect=lambda chat, uid: members[uid])

    async def restrict(chat, target, permissions, **kwargs):
        members[target] = (
            Member("member")
            if permissions.can_send_messages
            else Member(
                "restricted",
                permissions=permissions.to_dict(),
                until=kwargs.get("until_date"),
            )
        )

    async def ban(chat, target, **kwargs):
        members[target] = Member("kicked")

    async def unban(chat, target, **kwargs):
        members[target] = Member("left")

    async def set_permissions(chat, permissions, **kwargs):
        group.permissions = permissions

    bot.restrict_chat_member = AsyncMock(side_effect=restrict)
    bot.ban_chat_member = AsyncMock(side_effect=ban)
    bot.unban_chat_member = AsyncMock(side_effect=unban)
    bot.delete_message = AsyncMock()
    bot.set_chat_permissions = AsyncMock(side_effect=set_permissions)
    runtime = SimpleNamespace(
        store=store, bot=bot, application=SimpleNamespace(running=True)
    )
    mod = runtime.moderation = Moderation(runtime)
    runtime.ui = UI(runtime)
    store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1001','Fixture',1)"
    )
    with store.tx() as db:
        store.put(db, "modules", {"moderation": True})
    yield SimpleNamespace(
        mod=mod,
        store=store,
        clock=clock,
        bot=bot,
        group=group,
        members=members,
        runtime=runtime,
    )
    store.close()


@pytest.mark.asyncio
async def test_mute_restore_and_duplicate(setup):
    s = setup
    payload = await s.mod.preview("1", "-1001", "mute", "2", 60)
    await s.mod.execute("1", payload, "first")
    assert s.members[2].status == "restricted"
    with pytest.raises(Rejected):
        await s.mod.execute("1", payload, "first")
    assert s.bot.restrict_chat_member.await_count == 1
    restore = await s.mod.preview("1", "-1001", "unmute", "2")
    await s.mod.execute("1", restore, "restore")
    assert s.members[2].status == "member"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["restricted", "administrator", "creator", "left", "kicked"]
)
async def test_protected_target(setup, status):
    setup.members[2] = Member(status)
    with pytest.raises(Rejected):
        await setup.mod.preview("1", "-1001", "mute", "2", 1)
    setup.bot.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_dual_authority_and_revocation(setup):
    s = setup
    s.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES('2','[\"moderation\"]',?)",
        (s.clock[0] + 600,),
    )
    s.members[2] = Member("administrator")
    with pytest.raises(Rejected):
        await s.mod.check("2", "-1001")
    s.store.db.execute("INSERT INTO mod_acl VALUES('2','-1001')")
    await s.mod.check("2", "-1001")
    s.members[2].can_restrict_members = False
    with pytest.raises(Rejected):
        await s.mod.check("2", "-1001")
    s.store.revoke("1", "2")
    with pytest.raises(Rejected):
        await s.mod.check("2", "-1001")


@pytest.mark.asyncio
async def test_group_restore_conflict_and_preserved_permissions(setup):
    s = setup
    original = s.group.permissions.to_dict()
    await s.mod.execute("1", await s.mod.preview("1", "-1001", "mute_all"), "mute")
    assert not s.group.permissions.can_send_messages
    altered = s.group.permissions.to_dict()
    altered["can_send_photos"] = True
    s.group.permissions = ChatPermissions.de_json(altered, None)
    with pytest.raises(Rejected):
        await s.mod.preview("1", "-1001", "unmute_all")
    altered["can_send_photos"] = False
    altered["can_invite_users"] = False
    s.group.permissions = ChatPermissions.de_json(altered, None)
    await s.mod.execute("1", await s.mod.preview("1", "-1001", "unmute_all"), "restore")
    assert s.group.permissions.can_send_messages == original["can_send_messages"]
    assert not s.group.permissions.can_invite_users


@pytest.mark.asyncio
async def test_unknown_kick_does_not_unban_or_retry(setup):
    s = setup
    s.bot.ban_chat_member.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await s.mod.execute("1", await s.mod.preview("1", "-1001", "kick", "2"), "kick")
    s.bot.unban_chat_member.assert_not_awaited()
    assert s.store.db.execute("SELECT status FROM mod_ops").fetchone()[0] == "unknown"
    Moderation(s.runtime)
    with pytest.raises(Rejected):
        await s.mod.execute(
            "1", await s.mod.preview("1", "-1001", "mute", "2", 1), "new"
        )


@pytest.mark.asyncio
async def test_kick_success(setup):
    s = setup
    await s.mod.execute("1", await s.mod.preview("1", "-1001", "kick", "2"), "kick")
    assert s.members[2].status == "left"
    assert s.store.db.execute("SELECT status,step FROM mod_ops").fetchone()[:] == (
        "accepted",
        "done",
    )


def insert_schedule(s):
    stamp, action = boundary("23:00", "08:00", s.clock[0])
    s.store.db.execute(
        "INSERT INTO mod_schedules(chat,actor,start,end,enabled,version,next,action) VALUES('-1001','1','23:00','08:00',1,1,?,?)",
        (stamp, action),
    )
    return stamp


@pytest.mark.asyncio
async def test_full_daily_cycle_and_restart(setup):
    s = setup
    s.clock[0] = insert_schedule(s)
    await s.mod.tick()
    assert not s.group.permissions.can_send_messages
    await s.mod.tick()
    assert s.bot.set_chat_permissions.await_count == 1
    s.mod = Moderation(s.runtime)
    s.clock[0] = s.store.db.execute("SELECT next FROM mod_schedules").fetchone()[0]
    await s.mod.tick()
    assert s.group.permissions.can_send_messages
    assert s.bot.set_chat_permissions.await_count == 2


@pytest.mark.asyncio
async def test_missed_boundary_and_offline(setup):
    s = setup
    s.clock[0] = insert_schedule(s) + 20
    await s.mod.tick()
    assert not s.store.db.execute("SELECT enabled FROM mod_schedules").fetchone()[0]
    s.bot.set_chat_permissions.assert_not_awaited()
    s.runtime.application.running = False
    with pytest.raises(Rejected):
        await s.mod.preview("1", "-1001", "mute_all")


@pytest.mark.asyncio
async def test_pause_while_scheduled_operation_waits(setup):
    s = setup
    s.clock[0] = insert_schedule(s)
    lock = s.mod.locks.setdefault("-1001", asyncio.Lock())
    await lock.acquire()
    task = asyncio.create_task(s.mod.tick())
    await asyncio.sleep(0)
    s.mod.pause("-1001", "manual")
    lock.release()
    await task
    s.bot.set_chat_permissions.assert_not_awaited()


def test_boundary_and_hidden_menu(setup):
    s = setup
    for start, end in [("23:00", "23:00"), ("24:00", "08:00"), ("x", "y")]:
        with pytest.raises(Rejected):
            boundary(start, end, s.clock[0])
    assert "群管理" not in str(s.runtime.ui.keyboard("2"))
    assert "⚙️ 管理" not in str(s.runtime.ui.keyboard("2"))


def test_scoped_grant_and_one_use(setup):
    import hashlib

    s = setup
    token = s.store.grant("1", "2", ["moderation"], 1)
    s.store.db.execute(
        "INSERT INTO mod_grants VALUES(?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), json.dumps(["-1001"])),
    )
    with pytest.raises(Rejected):
        s.store.accept_grant("3", token)
    s.store.accept_grant("2", token)
    assert (
        s.store.db.execute("SELECT chat FROM mod_acl WHERE uid='2'").fetchone()[0]
        == "-1001"
    )
    with pytest.raises(Rejected):
        s.store.accept_grant("2", token)


@pytest.mark.asyncio
async def test_preview_changed_and_stale_version(setup):
    s = setup
    payload = await s.mod.preview("1", "-1001", "mute", "2", 1)
    s.store.db.execute("UPDATE mod_groups SET version=version+1")
    with pytest.raises(Rejected):
        await s.mod.execute("1", payload, "stale")
    s.bot.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_schedule_confirmation_and_stale_button(setup):
    s = setup
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        callback_query=None,
    )
    ui = s.runtime.ui.moderation_ui
    await ui.preview(update, "schedule", "-1001", "23:00 08:00")
    markup = s.bot.send_message.await_args.kwargs["reply_markup"]
    token = markup.inline_keyboard[0][0].callback_data[3:]
    payload = s.store.resolve(token, "1", "1")
    with pytest.raises(Rejected):
        s.store.resolve(token, "2", "2")
    s.store.consume(token)
    await ui.action(update, payload, token)
    assert s.store.db.execute("SELECT enabled FROM mod_schedules").fetchone()[0]
    with pytest.raises(Rejected):
        await ui.action(update, payload, token)
    with pytest.raises(Rejected):
        s.store.consume(token)
    s.bot.set_chat_permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_requires_known_non_admin_sender(setup):
    s = setup
    with pytest.raises(Rejected):
        await s.mod.preview("1", "-1001", "delete", "10")
    s.store.db.execute("INSERT INTO mod_messages VALUES('-1001',10,'1')")
    with pytest.raises(Rejected):
        await s.mod.preview("1", "-1001", "delete", "10")
    s.store.db.execute("UPDATE mod_messages SET sender='2'")
    await s.mod.execute(
        "1", await s.mod.preview("1", "-1001", "delete", "10"), "delete"
    )
    s.bot.delete_message.assert_awaited_once_with("-1001", 10)


def test_restart_missed_boundary_is_never_replayed(setup):
    s = setup
    s.clock[0] = insert_schedule(s)
    Moderation(s.runtime)
    assert s.store.db.execute("SELECT enabled,error FROM mod_schedules").fetchone()[
        :
    ] == (0, "restart_missed_boundary")


@pytest.mark.asyncio
async def test_group_list_checks_live_membership(setup):
    s = setup
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        callback_query=None,
    )
    await s.runtime.ui.moderation_ui.action(update, {"action": "mod_home"})
    assert "Fixture（已启用）" in str(
        s.bot.send_message.await_args.kwargs["reply_markup"]
    )
    s.members[1] = Member("member")
    await s.runtime.ui.moderation_ui.action(update, {"action": "mod_home"})
    assert "Fixture（已启用）" not in str(
        s.bot.send_message.await_args.kwargs["reply_markup"]
    )


@pytest.mark.asyncio
async def test_membership_discovers_basic_group(setup):
    from unittest.mock import MagicMock

    from data.plugins.astrbot_plugin_superbot.main import Main

    plugin = Main(MagicMock(), {})
    plugin.store, plugin.moderation = setup.store, setup.mod
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-999, type="group", title="Invited"),
        my_chat_member=SimpleNamespace(
            new_chat_member=SimpleNamespace(status="member")
        ),
        message=None,
        effective_user=None,
    )
    await plugin.receive(update, None)
    assert setup.store.db.execute(
        "SELECT title,enabled FROM mod_groups WHERE chat='-999'"
    ).fetchone()[:] == ("Invited", 0)


@pytest.mark.asyncio
async def test_member_pages_and_username_verification(setup):
    s = setup
    for i in range(20, 30):
        user = SimpleNamespace(
            id=i, is_bot=False, full_name=f"Member {i}", username=f"user{i}"
        )
        s.mod.remember("-1001", user)
        member = Member("member")
        member.user = user
        s.members[i] = member
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        callback_query=None,
    )
    ui = s.runtime.ui.moderation_ui
    await ui.action(update, {"action": "mod_form", "kind": "mute", "chat": "-1001"})
    markup = str(s.bot.send_message.await_args.kwargs["reply_markup"])
    assert "下一页" in markup and "Member 20" in markup and "Member 29" not in markup
    await ui.action(
        update, {"action": "mod_members", "kind": "mute", "chat": "-1001", "page": 1}
    )
    assert "上一页" in str(s.bot.send_message.await_args.kwargs["reply_markup"])
    await ui.preview(update, "member_lookup:mute", "-1001", "https://t.me/user20")
    assert s.store.dialog("1")["kind"] == "member_minutes:20"
    s.members[20].user.username = "renamed"
    with pytest.raises(Rejected):
        await ui.preview(update, "member_lookup:mute", "-1001", "@user20")
    s.bot.restrict_chat_member.assert_not_awaited()
    await ui.action(update, {"action": "mod_group", "chat": "-1001"})
    assert s.store.dialog("1") is None


@pytest.mark.asyncio
async def test_revoke_during_target_query(setup):
    s = setup
    original = s.bot.get_chat_member.side_effect

    async def changed(chat, uid):
        if uid == 2:
            with s.store.tx() as db:
                s.store.put(db, "modules", {"moderation": False})
        return original(chat, uid)

    payload = await s.mod.preview("1", "-1001", "mute", "2", 1)
    s.bot.get_chat_member.side_effect = changed
    with pytest.raises(Rejected):
        await s.mod.execute("1", payload, "disabled")
    s.bot.restrict_chat_member.assert_not_awaited()
