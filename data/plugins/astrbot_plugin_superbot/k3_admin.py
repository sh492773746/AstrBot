"""Administrative controls for the group-scoped K3 switch."""

from .game_switches import blocker
from .store import Rejected


async def action(ui, update, payload):
    """Render or update group K3 configuration and review actions."""
    store = ui.store
    uid = str(update.effective_user.id)
    store.require(uid, "game", chat=payload.get("chat"))
    if update.effective_chat.type != "private":
        raise Rejected("请私聊管理积分快三。")
    name = payload["action"]
    if name == "k3_groups":
        rows = store.db.execute(
            "SELECT g.chat,g.title,COALESCE(k.enabled,0) enabled,"
            "(SELECT COUNT(*) FROM k3_rounds r WHERE r.chat=g.chat "
            "AND r.status IN ('open','closing')) active,"
            "(SELECT COUNT(*) FROM k3_rounds r WHERE r.chat=g.chat "
            "AND r.status='review') reviews "
            "FROM platform_mod_groups g LEFT JOIN k3_groups k ON k.chat=g.chat "
            "WHERE g.enabled=1 ORDER BY g.title,g.chat"
        ).fetchall()
        modules = store.get("modules", {})
        return await ui.render(
            update,
            "🎲 积分快三管理\n"
            f"玩法中心：{'🟢 已开启' if modules.get('game') else '⚪ 已关闭'}\n"
            f"快三总开关：{'🟢 已开启' if modules.get('k3') else '⚪ 已关闭'}\n"
            f"已登记群：{len(rows)}个\n\n"
            "📋 逐群概况\n"
            + (
                "\n".join(
                    f"{index}. {row['title'] or row['chat']} · "
                    f"{'🟢 开启' if row['enabled'] else '⚪ 关闭'} · "
                    f"进行中{row['active']}期"
                    + (f" · ⚠️ 待核查{row['reviews']}期" if row["reviews"] else "")
                    for index, row in enumerate(rows, 1)
                )
                or "暂无已登记群"
            )
            + "\n\n📌 配置边界\n"
            "新群默认关闭；总开关与逐群开关均开启才接受新下注。\n"
            "关闭只停止新下注，已受理期次继续开奖或进入核查。",
            [
                (
                    f"管理 {index}",
                    {"action": "k3_group", "chat": row["chat"]},
                )
                for index, row in enumerate(rows, 1)
            ]
            + [("返回玩法管理", {"action": "admin_game"})],
            fold_sections=True,
        )
    chat = str(payload["chat"])
    await ui.runtime.moderation.check(uid, chat, "view", enabled=False)
    group = store.db.execute(
        "SELECT title FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()
    if not group:
        raise Rejected("群未登记。")
    title = group["title"] or chat
    row = store.db.execute(
        "SELECT enabled,version FROM k3_groups WHERE chat=?", (chat,)
    ).fetchone()
    enabled, version = (bool(row["enabled"]), row["version"]) if row else (False, 0)
    if name == "k3_save":
        if payload.get("version") != version:
            raise Rejected("配置已变更，请返回重选。")
        with store.tx() as db:
            db.execute(
                "INSERT INTO k3_groups(chat,enabled,version) VALUES(?,?,1) "
                "ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled,version=version+1",
                (chat, int(payload["enabled"])),
            )
            store.audit(
                db, uid, "k3_config", {"chat": chat, "enabled": payload["enabled"]}
            )
        enabled, version = bool(payload["enabled"]), version + 1
    if name == "k3_preview":
        return await ui.render(
            update,
            "🎲 确认调整本群快三\n"
            f"群：{title}\n群ID：{chat}\n"
            f"调整为：{'🟢 开启' if payload['enabled'] else '⚪ 关闭'}\n\n"
            "📌 影响\n仅停止新下注，已受理期次继续开奖或进入核查。",
            [
                ("确认", {**payload, "action": "k3_save"}),
                ("取消", {"action": "k3_group", "chat": chat}),
            ],
            fold_sections=True,
        )
    if name == "k3_refund_preview":
        await ui.runtime.moderation.check(uid, chat, "mute")
        issue = int(payload["issue"])
        return await ui.render(
            update,
            "🎲 确认异常退款\n"
            f"群：{title}\n期次：第{issue}期\n\n"
            "⚠️ 将退还本期尚未结算的全部下注；已结算期次不可退款。\n"
            "退款后该期不能恢复开奖，证据和审计记录保留。",
            [
                ("确认退款", {"action": "k3_refund", "chat": chat, "issue": issue}),
                ("取消", {"action": "k3_group", "chat": chat}),
            ],
            fold_sections=True,
        )
    if name == "k3_refund":
        await ui.runtime.moderation.check(uid, chat, "mute")
        ui.runtime.k3.refund_round(chat, int(payload["issue"]), uid, "管理员异常退款")
        return await ui.render(
            update,
            "🎲 期次已退款\n已受理积分已原路退回，异常证据和审计记录已保留。",
            [("返回本群", {"action": "k3_group", "chat": chat})],
            fold_sections=True,
        )
    modules = store.get("modules", {})
    rounds = store.db.execute(
        "SELECT r.issue,r.status,r.closes,"
        "(SELECT COUNT(DISTINCT uid) FROM k3_bets b "
        "WHERE b.chat=r.chat AND b.issue=r.issue) players,"
        "(SELECT COALESCE(SUM(amount),0) FROM k3_bets b "
        "WHERE b.chat=r.chat AND b.issue=r.issue) stake,"
        "(SELECT COUNT(*) FROM k3_dice d WHERE d.chat=r.chat "
        "AND d.issue=r.issue AND d.status='review') dice_reviews "
        "FROM k3_rounds r WHERE r.chat=? ORDER BY r.issue DESC LIMIT 8",
        (chat,),
    ).fetchall()
    status_names = {
        "open": "收集中",
        "closing": "开奖中",
        "settled": "已结算",
        "review": "待核查",
        "refunded": "已退款",
    }
    usable = bool(modules.get("game") and modules.get("k3") and enabled)
    reason = blocker(store, chat, "k3", enabled, points=False)
    text = (
        "🎲 本群积分快三\n"
        f"群：{title}\n群ID：{chat}\n"
        f"当前可用：{'🟢 是' if usable else '⚪ 否'}\n"
        f"玩法中心：{'🟢 开启' if modules.get('game') else '⚪ 关闭'}\n"
        f"快三总开关：{'🟢 开启' if modules.get('k3') else '⚪ 关闭'}\n"
        f"本群开关：{'🟢 开启' if enabled else '⚪ 关闭'}\n"
        f"实际状态：{'⚠️ ' + reason if reason else '🟢 配置就绪；有异常期时仍需核查'}\n\n"
        "📋 近期8期\n"
        + (
            "\n".join(
                f"第{r['issue']}期 · {status_names.get(r['status'], '未知状态')} · "
                f"{r['players']}人/{r['stake']}积分"
                + (f" · ⚠️ 骰子待核查{r['dice_reviews']}" if r["dice_reviews"] else "")
                for r in rounds
            )
            or "暂无期次"
        )
        + "\n\n📌 安全规则\n"
        "固定赔率不可编辑；Telegram 原生骰子逐颗开奖。\n"
        "未知发送、回执不完整或开奖中断进入待核查，不自动重掷。\n"
        "异常退款仅对待核查且未结算期次开放，并保留完整审计。"
    )
    return await ui.render(
        update,
        text,
        [
            (
                "关闭本群" if enabled else "开启本群",
                {
                    "action": "k3_preview",
                    "chat": chat,
                    "version": version,
                    "enabled": not enabled,
                },
            ),
            *[
                (
                    f"异常退款 第{r['issue']}期",
                    {"action": "k3_refund_preview", "chat": chat, "issue": r["issue"]},
                )
                for r in rounds
                if r["status"] == "review"
            ],
            ("返回群列表", {"action": "k3_groups"}),
            ("返回玩法管理", {"action": "admin_game"}),
            ("本群玩法总览", {"action": "games_group_view", "chat": chat}),
        ],
        fold_sections=True,
    )
