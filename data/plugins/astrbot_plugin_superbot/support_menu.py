"""Explicit group-scoped support; navigation never starts a paid request."""

import re

from .store import Rejected, encode
from .tenants import binding, local_config


def contact_url(value):
    """Accept Telegram usernames, never phone numbers or arbitrary URLs."""
    match = re.fullmatch(
        r"(?:@|https://t\.me/)([A-Za-z][A-Za-z0-9_]{4,31})", value.strip()
    )
    if not match:
        raise Rejected("请填写 @用户名 或 https://t.me/用户名，不接受手机号。")
    return "https://t.me/" + match[1]


async def action(ui, update, data):
    """Resolve explicit membership and authorization before exposing group data."""
    runtime, store = ui.runtime, ui.store
    uid = str(update.effective_user.id)
    private = update.effective_chat.type == "private"
    name = data["action"]
    if private and hasattr(runtime, "chat_sessions"):
        runtime.chat_sessions.pop((str(update.effective_chat.id), uid), None)
    if name in {"support_contact_edit", "support_contact_save"} and not private:
        raise Rejected("请私聊管理本群业务联系人。")
    chat = str(data.get("chat", "")) if private else str(update.effective_chat.id)
    back = [("返回客服", {"action": "support", "chat": chat})]
    if name == "support":
        return await ui.render(
            update,
            "💬 **客服与帮助**\n平台负责使用及接入问题；本群广告收款、退款及积分争议由群主负责。",
            [
                ("平台使用帮助", {"action": "help"}),
                ("联系平台管理员", {"url": f"tg://user?id={store.owner}"}),
                ("本群业务联系", {"action": "support_contact", "chat": chat}),
                ("AI咨询", {"action": "support_ai", "chat": chat}),
            ],
        )
    if not chat:
        rows = store.db.execute(
            "SELECT g.chat,g.title FROM mod_groups g WHERE g.enabled=1 AND "
            "(EXISTS(SELECT 1 FROM mod_members m WHERE m.chat=g.chat AND m.uid=?) OR "
            "EXISTS(SELECT 1 FROM tenant_groups b JOIN tenants t ON t.id=b.tenant WHERE b.chat=g.chat AND t.owner=?))",
            (uid, uid),
        ).fetchall()
        return await ui.render(
            update,
            "请先选择本次咨询的群；不会沿用其他群的选择。",
            [(row["title"], {"action": name, "chat": row["chat"]}) for row in rows]
            + back,
        )
    if name not in {"support_contact_edit", "support_contact_save"}:
        await runtime.community.member(uid, chat, require_moderation=False)
    owner = binding(store, chat)
    if owner and (owner["status"] != "active" or owner["tenant_status"] != "active"):
        raise Rejected("本群经营服务已暂停，请联系平台管理员。")
    group = store.db.execute(
        "SELECT title FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()
    title = group["title"] if group else chat
    if name == "support_contact":
        target = local_config(store, chat, "business_contact", "")
        target = (
            contact_url(target)
            if target
            else f"tg://user?id={owner['owner'] if owner else store.owner}"
        )
        return await ui.render(
            update,
            f"🏪 **本群业务联系：{title}**\n广告审核、收款、退款及积分争议由本群经营者处理。\n如身份链接不可访问，请在原群联系群主。",
            [("联系本群经营者", {"url": target})] + back,
        )
    if name == "support_ai":
        if not private:
            return await ui.render(
                update, "AI咨询仅在私聊使用，请私聊打开「客服」并选择对应群。", []
            )
        if owner and owner["tenant"] != "platform":
            await runtime.tenants.select_resource(uid, chat, "chat")
        if not runtime.tenants.resource_allowed(uid, chat, "chat"):
            return await ui.render(
                update,
                "本群未获AI预算或预算已用完；未调用模型。请使用静态帮助或联系本群经营者。",
                back,
            )
        if not owner or owner["tenant"] == "platform":
            store.db.execute(
                "INSERT INTO tenant_service_contexts(uid,resource,chat,expires) VALUES(?,'chat',?,?) "
                "ON CONFLICT(uid,resource) DO UPDATE SET chat=excluded.chat,expires=excluded.expires",
                (uid, chat, store.clock() + 1800),
            )
        return await ui.render(
            update, "已选择本群服务预算，请发送 /chat 开始AI咨询。", back
        )
    if name in {"support_contact_edit", "support_contact_save"}:
        if owner and owner["tenant"] != "platform":
            verified = await runtime.tenants.verify(uid, chat)
        else:
            await runtime.moderation.check(uid, chat, action="view")
            verified = binding(store, chat)
            if not verified:
                raise Rejected("本群归属尚未登记，请平台管理员核查。")
        row = store.db.execute(
            "SELECT version FROM tenant_settings WHERE chat=? AND key='business_contact'",
            (chat,),
        ).fetchone()
        version = row[0] if row else 0
        if name.endswith("edit"):
            store.dialog(
                uid,
                {
                    "form": "tenant_input",
                    "kind": "business_contact",
                    "payload": {
                        "chat": chat,
                        "version": version,
                        "owner_version": verified["version"],
                    },
                },
            )
            return await ui.render(
                update,
                f"🏪 **正在管理：{title}**\n发送 @用户名 或 https://t.me/用户名。\n仅修改本群业务联系人；/cancel 取消。",
                back,
            )
        value = contact_url(data["value"])
        with store.tx() as db:
            current = binding(store, chat, db)
            row = db.execute(
                "SELECT version FROM tenant_settings WHERE chat=? AND key='business_contact'",
                (chat,),
            ).fetchone()
            if (
                not current
                or current["version"] != data["owner_version"]
                or (row[0] if row else 0) != data["version"]
            ):
                raise Rejected("群归属或配置已变化，请重新操作。")
            store.require(uid, "moderation", db=db, chat=chat)
            db.execute(
                "INSERT INTO tenant_settings(chat,key,value) VALUES(?,'business_contact',?) ON CONFLICT(chat,key) DO UPDATE SET value=excluded.value,version=tenant_settings.version+1",
                (chat, encode(value)),
            )
            store.audit(db, uid, "business_contact", {"chat": chat, "contact": value})
        return await ui.render(
            update, f"已保存「{title}」业务联系人，仅本群生效。", back
        )
    raise Rejected("客服入口已失效，请重新打开。")
