"""Channel discovery, explicit enablement and publishing permission checks."""

import re

from .store import Rejected


async def check(bot, chat, actor=None):
    """Validate channel identity and required permissions.

    Args:
        bot: Existing Telegram client.
        chat: Channel ID or public username.
        actor: Optional administrator requesting channel enablement.

    Returns:
        Verified Telegram channel information.
    """
    info = await bot.get_chat(chat)
    if info.type != "channel":
        raise Rejected("这里请添加频道；群聊仍在群管理中启用")
    member = await bot.get_chat_member(info.id, bot.id)
    if (
        member.status != "administrator"
        or not member.can_post_messages
        or not member.can_edit_messages
    ):
        raise Rejected(
            "请把我设置为管理员，并赋予相关权限：发布消息、编辑消息（含置顶）"
        )
    if actor is not None:
        member = await bot.get_chat_member(info.id, int(actor))
        if member.status != "creator" and (
            member.status != "administrator"
            or not member.can_post_messages
            or not member.can_edit_messages
        ):
            raise Rejected("你需要是该频道的所有者，或具备发布及编辑权限的管理员")
    return info


async def action(ui, update, payload):
    """Render channel selection and commit explicit activation.

    Args:
        ui: Private UI controller.
        update: Authenticated Telegram update.
        payload: Stored owner-bound callback payload.
    """
    uid = str(update.effective_user.id)
    ui.store.require(uid, "ads")
    back = [("返回频道列表", {"action": "channels"})]
    kind = payload["action"]
    if kind == "channels":
        rows = ui.store.db.execute(
            "SELECT * FROM ad_channels ORDER BY title,chat"
        ).fetchall()
        page = max(0, min(int(payload.get("page", 0)), max(0, (len(rows) - 1) // 8)))
        buttons = [
            (
                f"{r['title']} · {'已启用' if r['enabled'] else '待启用'}",
                {
                    "action": "channel_preview",
                    "chat": r["chat"],
                    "enabled": not r["enabled"],
                },
            )
            for r in rows[page * 8 : page * 8 + 8]
        ]
        for offset, label in [(-1, "上一页"), (1, "下一页")]:
            if 0 <= page + offset <= (len(rows) - 1) // 8:
                buttons.append((label, {"action": "channels", "page": page + offset}))
        return await ui.render(
            update,
            "频道广告管理：先将机器人设为频道管理员，再添加并确认启用。",
            buttons
            + [
                ("添加频道", {"action": "channel_add"}),
                ("返回广告管理", {"action": "admin_ads"}),
            ],
        )
    if kind == "channel_add":
        ui.store.dialog(uid, {"form": "channel_add"})
        return await ui.render(
            update,
            "发送频道数字 ID、@用户名或公开 t.me/用户名链接。私密频道请使用数字 ID。",
            back,
        )
    if kind in ("channel_preview", "channel_save"):
        info = await check(ui.runtime.bot, payload["chat"], uid)
        enabled = bool(payload.get("enabled", True))
        if kind == "channel_preview":
            return await ui.render(
                update,
                f"确认{'启用' if enabled else '停用'}频道广告？\n{info.title} · {info.id}\n停用后不再接受此频道新广告位，已发布广告仍按到期处理。",
                [
                    (
                        "确认",
                        {
                            "action": "channel_save",
                            "chat": str(info.id),
                            "enabled": enabled,
                        },
                    )
                ]
                + back,
            )
        with ui.store.tx() as db:
            ui.store.require(uid, "ads", db)
            db.execute(
                "INSERT INTO ad_channels(chat,title,enabled) VALUES(?,?,?) ON CONFLICT(chat) DO UPDATE SET title=excluded.title,enabled=excluded.enabled",
                (str(info.id), info.title, int(enabled)),
            )
            ui.store.audit(
                db, uid, "ad_channel_config", {"chat": str(info.id), "enabled": enabled}
            )
        return await ui.render(
            update, "频道设置已保存；启用后可在设置广告位时选择。", back
        )
    raise Rejected("未知频道操作")


def target(text):
    """Parse a numeric channel ID or public Telegram username.

    Args:
        text: User-supplied channel reference.

    Returns:
        Numeric ID or Telegram username.
    """
    text = text.strip()
    if re.fullmatch(r"-[0-9]+", text):
        return int(text)
    match = re.fullmatch(r"(?:@|https?://t\.me/|t\.me/)([A-Za-z0-9_]{5,32})/?", text)
    if not match:
        raise Rejected("请发送频道数字 ID、@用户名或公开频道链接")
    return "@" + match[1]
