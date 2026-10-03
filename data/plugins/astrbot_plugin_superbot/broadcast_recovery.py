"""Recover an uncertain send from an administrator-supplied Telegram reply."""

import html
import re

from .store import Rejected


async def recover(runtime, update):
    """Bind a verified original bot message without creating replacement posts.

    Args:
        runtime: Bound superbot instance.
        update: Group command replying to the original announcement.
    """
    uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
    runtime.store.require(uid, "game")
    reply = update.message.reply_to_message
    if (
        update.effective_chat.type not in {"group", "supergroup"}
        or getattr(update.message, "sender_chat", None)
        or not reply
        or not reply.from_user
        or reply.from_user.id != runtime.bot.id
        or reply.chat.id != update.effective_chat.id
    ):
        raise Rejected(
            "请以个人身份回复本机器人发出的旧公告，再发送 /recoverbroadcast。"
        )
    member = await runtime.bot.get_chat_member(chat, int(uid))
    if member.status not in {"creator", "administrator"}:
        raise Rejected("仅本群管理员可以恢复公告。")
    text = " ".join((reply.text or "").split())
    async with runtime.group_game.broadcast.locks[chat]:
        rows = runtime.store.db.execute(
            "SELECT * FROM gg_dispatch WHERE chat=? AND "
            "((status IN ('review','sent') AND message IS NULL) OR (status='sent' AND message=?)) "
            "AND kind IN ('round_open','round_close','round_result')",
            (chat, reply.message_id),
        ).fetchall()
        matches = [
            row
            for row in rows
            if " ".join(
                (
                    html.unescape(re.sub(r"<[^>]*>", "", row["text"]))
                    if row["format"] == "HTML"
                    else row["text"]
                ).split()
            )
            == text
        ]
        if len(matches) != 1:
            raise Rejected("未找到唯一匹配的待核查公告；不能绑定或删除这条消息。")
        row = matches[0]
        if row["message"] == reply.message_id:
            return "这条公告已恢复，无需重复操作；清理状态可在管理面板查看。"
        with runtime.store.tx() as db:
            runtime.store.require(uid, "game", db)
            changed = db.execute(
                "UPDATE gg_dispatch SET status='sent',message=?,error='VerifiedReply',version=version+1 "
                "WHERE id=? AND status IN ('review','sent') AND message IS NULL",
                (reply.message_id, row["id"]),
            ).rowcount
            if not changed:
                raise Rejected("记录已变化，请刷新。")
            newer = db.execute(
                "SELECT 1 FROM gg_dispatch WHERE chat=? AND status='sent' AND message IS NOT NULL "
                "AND kind IN ('round_open','round_close','round_result') "
                "AND (issue>? OR (issue=? AND CASE kind WHEN 'round_open' THEN 0 WHEN 'round_close' THEN 1 ELSE 2 END>?))",
                (
                    chat,
                    row["issue"],
                    row["issue"],
                    {"round_open": 0, "round_close": 1, "round_result": 2}[row["kind"]],
                ),
            ).fetchone()
            if newer:
                db.execute(
                    "INSERT OR IGNORE INTO gb_cleanup(job) VALUES(?)", (row["id"],)
                )
            runtime.store.audit(
                db,
                uid,
                "broadcast_recovered_reply",
                {"id": row["id"], "message": reply.message_id, "cleanup": bool(newer)},
            )
    return (
        "已找回公告记录。旧公告已交给清理队列。"
        if newer
        else "已找回公告记录，后续更新将使用原消息。"
    )
