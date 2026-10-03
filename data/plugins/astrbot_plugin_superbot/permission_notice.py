"""Rate-limited permission guidance for Telegram membership updates."""


async def notify(runtime, chat, member):
    """Explain missing permissions without repeatedly messaging a chat.

    Args:
        runtime: Superbot runtime with its existing Telegram client.
        chat: Group or channel receiving a membership update.
        member: Telegram's current bot membership record.
    """
    if member.status in {"left", "kicked"}:
        return
    required = (
        {"can_post_messages": "发布消息", "can_edit_messages": "编辑消息（含置顶）"}
        if chat.type == "channel"
        else {
            "can_pin_messages": "置顶消息",
            "can_delete_messages": "删除消息",
            "can_restrict_members": "封禁成员（含禁言、踢出）",
        }
    )
    missing = [
        label
        for key, label in required.items()
        if member.status != "creator"
        and (member.status != "administrator" or not getattr(member, key, False))
    ]
    key = "permission_notice/" + str(chat.id)
    previous = runtime.store.get(key, {})
    now = runtime.store.clock()
    signature = ",".join(missing)
    if not missing:
        with runtime.store.tx() as db:
            runtime.store.put(db, key, {"missing": "", "at": now})
        return
    if previous.get("missing") == signature and now - previous.get("at", 0) < 86400:
        return
    if previous.get("missing") and now - previous.get("at", 0) < 600:
        return
    with runtime.store.tx() as db:
        runtime.store.put(db, key, {"missing": signature, "at": now})
    text = (
        "请把我设置为管理员，并赋予相关权限："
        if member.status not in {"administrator", "creator"}
        else "我还缺少这些管理员权限，请帮我补上："
    )
    text += "、".join(missing) + "。\n设置好后，我会在权限变化时重新检查。"
    if chat.type != "channel":
        try:
            await runtime.bot.send_message(chat_id=chat.id, text=text)
            return
        except Exception:
            pass
    try:
        await runtime.bot.send_message(
            chat_id=runtime.store.owner,
            text=f"权限提醒 · {chat.title or chat.id}\n{text}",
        )
    except Exception as exc:
        runtime.logger.warning("Permission notification failed: %s", type(exc).__name__)
