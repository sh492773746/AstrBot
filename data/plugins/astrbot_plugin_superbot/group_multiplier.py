"""Private, scoped group-wide multiplier selection."""

from .rules import NAMES
from .store import Rejected


async def action(ui, update, payload, token):
    """Preview and confirm a group profile without rewriting accepted wagers.

    Args:
        ui: Private renderer.
        update: Authenticated interaction.
        payload: Bound callback.
        token: Single-use confirmation.
    """
    runtime, store = ui.runtime, ui.store
    uid, chat = str(update.effective_user.id), str(payload["chat"])
    if update.effective_chat.type != "private":
        raise Rejected("请私聊设置本群倍率")
    await runtime.moderation.check(uid, chat, "view")
    current = runtime.group_game.group_room(chat)
    row = store.db.execute(
        "SELECT * FROM gg_group_room WHERE chat=?", (chat,)
    ).fetchone()
    rooms = runtime.game.rooms()
    back = [("返回群管理", {"action": "mod_group", "chat": chat})]
    if payload["action"] == "mod_multiplier":
        return await ui.render(
            update,
            f"本群当前倍率：{NAMES[current]}\n全群共用；普通玩家不可自行切换。",
            [
                (
                    NAMES[k],
                    {
                        "action": "mod_multiplier_preview",
                        "chat": chat,
                        "room": k,
                        "version": row["version"],
                    },
                )
                for k, v in rooms.items()
                if v["enabled"]
            ]
            + back,
        )
    selected = payload.get("room")
    if row["version"] != payload.get("version") or not rooms.get(selected, {}).get(
        "enabled"
    ):
        raise Rejected("倍率配置已变化，请重新打开")
    if payload["action"] == "mod_multiplier_preview":
        return await ui.render(
            update,
            f"确认将本群 {NAMES[current]} 切换为 {NAMES[selected]}？\n新下注按新倍率；旧确认失效，已受理下注仍按原规则结算。",
            [("确认切换", {**payload, "action": "mod_multiplier_save"})] + back,
        )
    if payload["action"] != "mod_multiplier_save":
        raise Rejected("无效操作")
    async with runtime.group_game.broadcast.locks[chat]:
        await runtime.moderation.check(uid, chat, "view")
        with store.tx() as db:
            if not runtime.game.rooms(db).get(selected, {}).get("enabled"):
                raise Rejected("该倍率已关闭")
            if not db.execute(
                "UPDATE callbacks SET used=1 WHERE token=? AND uid=? AND chat=? AND used=0 AND expires>?",
                (token, uid, str(update.effective_chat.id), store.clock()),
            ).rowcount:
                raise Rejected("确认已使用或过期")
            if not db.execute(
                "UPDATE gg_group_room SET room=?,version=version+1 WHERE chat=? AND version=?",
                (selected, chat, payload["version"]),
            ).rowcount:
                raise Rejected("配置已变化")
            db.execute("UPDATE gg_flows SET version=version+1 WHERE chat=?", (chat,))
            db.execute("DELETE FROM gg_forms WHERE chat=?", (chat,))
            store.audit(
                db,
                uid,
                "group_multiplier",
                {"chat": chat, "before": current, "after": selected},
            )
    return await ui.render(
        update, f"本群已切换为 {NAMES[selected]}，发送 jnd 激活文字下注。", back
    )
