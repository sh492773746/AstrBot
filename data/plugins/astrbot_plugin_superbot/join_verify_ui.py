"""Private administrator controls for post-join verification."""

import asyncio
import json
import re

from telegram.error import BadRequest, Forbidden

from .store import Rejected, encode


async def action(ui, update, payload, token=""):
    """Handle private, version-bound policy and manual-release controls.

    Args:
        ui: Existing moderation UI.
        update: Authenticated private Telegram update.
        payload: Server-stored callback payload.
        token: Existing expiring callback token.
    """
    runtime, store = ui.runtime, ui.store
    chat, uid = str(payload["chat"]), str(update.effective_user.id)
    await runtime.moderation.check(uid, chat)
    service = runtime.join_verify
    policy = service.policy(chat)
    op = payload["action"]
    home = [("返回入群验证", {"action": "mod_jv_home", "chat": chat})]
    if op == "mod_jv_home":
        targets = json.loads(policy["targets"])
        return await ui.ui.render(
            update,
            f"入群后验证 · {'🟢 已开启' if policy['enabled'] else '🔴 已关闭'}\n"
            + (
                "模式：加入指定群／频道后点击验证\n"
                + "\n".join(target["title"] for target in targets)
                if targets
                else "模式：仅点击验证按钮，无须加入其他群／频道"
            )
            + "\n仅限制新成员；未验证不自动踢出，旧成员不追溯限制。"
            "\n停用只停止限制新成员，已有记录仍可验证或人工解除。",
            [
                ("设置指定群／频道", {"action": "mod_jv_targets", "chat": chat}),
                (
                    "改为仅点击按钮验证",
                    {
                        "action": "mod_jv_preview",
                        "chat": chat,
                        "version": policy["version"],
                        "targets": [],
                        "enabled": bool(policy["enabled"]),
                    },
                )
                if targets
                else (
                    "启用仅点击按钮验证",
                    {
                        "action": "mod_jv_preview",
                        "chat": chat,
                        "version": policy["version"],
                        "targets": [],
                        "enabled": True,
                    },
                ),
                (
                    "关闭验证" if policy["enabled"] else "开启验证",
                    {
                        "action": "mod_jv_preview",
                        "chat": chat,
                        "version": policy["version"],
                        "targets": targets,
                        "enabled": not bool(policy["enabled"]),
                    },
                ),
                ("待验证／故障记录", {"action": "mod_jv_pending", "chat": chat}),
                ("返回群管", {"action": "semantic_group_menu", "chat": chat}),
            ],
        )
    if op == "mod_jv_targets":
        store.dialog(
            uid,
            {
                "moderation": True,
                "kind": "joinverify",
                "chat": chat,
                "version": policy["version"],
            },
        )
        return await ui.ui.render(
            update,
            "发送1至3个公开频道／群的 @用户名 或 t.me/用户名，英文逗号分隔。\n"
            "机器人须在每个目标中担任管理员；先支持公开目标，不接收私密邀请链接。"
            "\n不需要强制加入其他群时，请返回选择“仅点击按钮验证”。",
            home,
        )
    if op in {"mod_jv_preview", "mod_jv_save"}:
        if policy["version"] != payload["version"]:
            raise Rejected("配置已变化，请重新打开")
        targets = payload["targets"]
        enabled = payload["enabled"]
        if (
            type(enabled) is not bool
            or not isinstance(targets, list)
            or len(targets) > 3
        ):
            raise Rejected("无效配置")
        if any(target["id"] == chat for target in targets):
            raise Rejected("验证目标不能是本群")
        if enabled:
            await service.targets(targets)
        if op == "mod_jv_preview":
            return await ui.ui.render(
                update,
                f"确认{'开启' if enabled else '关闭'}入群后验证？\n"
                + (
                    "\n".join(
                        target["title"] + " · " + target["url"] for target in targets
                    )
                    or "无目标"
                )
                + (
                    "\n开启后仅新成员会被验证禁言，加入全部目标后点击按钮放行。"
                    if targets
                    else "\n开启后仅新成员会被验证禁言，点击验证按钮即可放行；不要求加入其他群／频道。"
                )
                + "\n未验证不自动踢出，管理员可核查后人工解除；已存在的限制不覆盖。"
                "\n已有待验证成员继续使用加入时的目标，不受本次改动影响。",
                [("确认保存", {**payload, "action": "mod_jv_save"})] + home,
            )
        async with runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
            await runtime.moderation.check(uid, chat)
            if service.policy(chat)["version"] != payload["version"]:
                raise Rejected("配置已变化，请重新打开")
            with store.tx() as db:
                db.execute(
                    "INSERT INTO jv_policies(chat,actor,targets,enabled) VALUES(?,?,?,?) ON CONFLICT(chat) DO UPDATE SET actor=excluded.actor,targets=excluded.targets,enabled=excluded.enabled,version=jv_policies.version+1",
                    (chat, uid, encode(targets), int(enabled)),
                )
                store.audit(
                    db,
                    uid,
                    "join_verify_policy",
                    {"chat": chat, "targets": targets, "enabled": enabled},
                )
        return await action(ui, update, {"action": "mod_jv_home", "chat": chat})
    if op == "mod_jv_pending":
        page = max(0, int(payload.get("page", 0)))
        rows = store.db.execute(
            "SELECT * FROM jv_entries WHERE chat=? AND status!='complete' ORDER BY created DESC LIMIT 9 OFFSET ?",
            (chat, page * 8),
        ).fetchall()
        buttons = [
            (
                f"{r['uid']} · {r['status']}",
                {"action": "mod_jv_entry", "chat": chat, "entry": r["token"]},
            )
            for r in rows[:8]
        ]
        if page:
            buttons.append(("上一页", {"action": op, "chat": chat, "page": page - 1}))
        if len(rows) > 8:
            buttons.append(("下一页", {"action": op, "chat": chat, "page": page + 1}))
        return await ui.ui.render(
            update, "待验证／待核查记录。结果不明不自动重试。", buttons + home
        )
    if op in {"mod_jv_entry", "mod_jv_release"}:
        row = store.db.execute(
            "SELECT * FROM jv_entries WHERE token=? AND chat=?",
            (payload["entry"], chat),
        ).fetchone()
        if not row:
            raise Rejected("记录不存在")
        if op == "mod_jv_release":
            result = await service.verify(row["token"], row["uid"], chat, actor=uid)
            return await ui.ui.render(update, result, home)
        return await ui.ui.render(
            update,
            f"成员 {row['uid']}\n状态 {row['status']}\n最近情况 {row['error'] or '无故障'}\n"
            "人工解除会跳过加入目标条件，但仍核验当前限制是否属于本功能；不会解除后来新增的处罚。"
            "\n待核查记录需维护人员核实实际状态，不提供盲目重试。",
            (
                [
                    (
                        "确认人工解除验证限制",
                        {
                            "action": "mod_jv_release",
                            "chat": chat,
                            "entry": row["token"],
                        },
                    )
                ]
                if row["status"] == "pending"
                else []
            )
            + home,
        )
    raise Rejected("入群验证操作不存在")


async def input_text(ui, update, chat, text):
    """Resolve public targets to stable IDs before asking for confirmation.

    Args:
        ui: Existing moderation UI.
        update: Administrator private message.
        chat: Managed group.
        text: Comma-separated public Telegram references.
    """
    uid = str(update.effective_user.id)
    dialog = ui.store.dialog(uid)
    if not dialog or dialog.get("kind") != "joinverify" or dialog["chat"] != chat:
        raise Rejected("输入已过期，请重新打开")
    await ui.runtime.moderation.check(uid, chat)
    values = text.replace("，", ",").split(",")
    if not 1 <= len(values) <= 3:
        raise Rejected("支持1至3个公开目标")
    targets = []
    for value in values:
        match = re.fullmatch(
            r"(?:@|(?:https?://)?t\.me/)([A-Za-z][A-Za-z0-9_]{3,31})/?", value.strip()
        )
        if not match:
            raise Rejected("请发送公开 @用户名 或 t.me/用户名")
        try:
            info = await ui.runtime.bot.get_chat("@" + match[1])
        except (BadRequest, Forbidden) as exc:
            raise Rejected(
                f"无法找到或访问 @{match[1]}；请检查公开用户名，确认机器人能访问该群／频道。"
            ) from exc
        if str(info.id) == chat or info.type not in {"supergroup", "channel"}:
            raise Rejected("目标须为其他公开超级群或频道")
        targets.append(
            {
                "id": str(info.id),
                "username": match[1],
                "title": info.title or match[1],
                "url": "https://t.me/" + match[1],
            }
        )
    await ui.runtime.join_verify.targets(targets)
    return await action(
        ui,
        update,
        {
            "action": "mod_jv_preview",
            "chat": chat,
            "version": dialog["version"],
            "targets": targets,
            "enabled": bool(ui.runtime.join_verify.policy(chat)["enabled"]),
        },
    )
