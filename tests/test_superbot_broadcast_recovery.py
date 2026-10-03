"""Recovery must use an actual matching bot reply from a group administrator."""

from types import SimpleNamespace

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_game_hardening import flow  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_round_broadcast import rounds  # noqa: F401

from data.plugins.astrbot_plugin_superbot.broadcast_recovery import recover
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["review", "sent"])
async def test_verified_reply_binds_id_and_schedules_cleanup(rounds, status):  # noqa: F811
    s = rounds
    s.runtime.group_game = s.flow
    s.store.db.execute(
        "INSERT INTO gg_dispatch(id,chat,op,kind,text,format,issue,status,error) "
        "VALUES('lost','-1001','99','round_open','<b>旧公告</b>','HTML',99,'review','TimedOut')"
    )
    s.store.db.execute("UPDATE gg_dispatch SET status=? WHERE id='lost'", (status,))
    s.store.db.execute(
        "INSERT INTO gg_dispatch(id,chat,op,kind,text,issue,status,message) "
        "VALUES('new','-1001','100','round_open','new',100,'sent',501)"
    )
    reply = SimpleNamespace(
        from_user=SimpleNamespace(id=9),
        chat=SimpleNamespace(id=-1001),
        message_id=500,
        text="旧公告",
    )
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
        message=SimpleNamespace(reply_to_message=reply, sender_chat=None),
    )
    reply.from_user.id = 2
    with pytest.raises(Rejected):
        await recover(s.runtime, event)
    reply.from_user.id = 9
    assert "清理" in await recover(s.runtime, event)
    assert (
        s.store.db.execute(
            "SELECT message FROM gg_dispatch WHERE id='lost'"
        ).fetchone()[0]
        == 500
    )
    assert (
        s.store.db.execute("SELECT status FROM gb_cleanup WHERE job='lost'").fetchone()[
            0
        ]
        == "pending"
    )
    assert "无需重复" in await recover(s.runtime, event)
    assert s.store.db.execute("SELECT count(*) FROM gb_cleanup").fetchone()[0] == 1
    assert s.bot.send_message.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["scope", "member", "anonymous", "chat", "text", "duplicate", "private"],
)
async def test_invalid_recovery_cannot_bind_or_delete(rounds, invalid):  # noqa: F811
    s = rounds
    s.store.db.execute(
        "INSERT INTO gg_dispatch(id,chat,op,kind,text,format,issue,status,error) "
        "VALUES('lost','-1001','99','round_open','<b>旧公告</b>','HTML',99,'review','TimedOut')"
    )
    reply = SimpleNamespace(
        from_user=SimpleNamespace(id=9),
        chat=SimpleNamespace(id=-1001),
        message_id=500,
        text="旧公告",
    )
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
        message=SimpleNamespace(reply_to_message=reply, sender_chat=None),
    )
    if invalid == "scope":
        event.effective_user.id = 2
    elif invalid == "member":
        s.members[1].status = "member"
    elif invalid == "anonymous":
        event.message.sender_chat = SimpleNamespace(id=-1001)
    elif invalid == "chat":
        reply.chat.id = -1002
    elif invalid == "text":
        reply.text = "另一条公告"
    elif invalid == "private":
        event.effective_chat.type = "private"
    else:
        s.store.db.execute(
            "INSERT INTO gg_dispatch(id,chat,op,kind,text,format,issue,status) "
            "VALUES('duplicate','-1001','98','round_open','<b>旧公告</b>','HTML',98,'review')"
        )
    with pytest.raises(Rejected):
        await recover(s.runtime, event)
    assert (
        s.store.db.execute(
            "SELECT message FROM gg_dispatch WHERE id='lost'"
        ).fetchone()[0]
        is None
    )
    assert s.store.db.execute("SELECT count(*) FROM gb_cleanup").fetchone()[0] == 0
    assert s.bot.send_message.await_count == 0
    assert s.bot.delete_message.await_count == 0
