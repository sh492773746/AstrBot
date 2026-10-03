"""One-group game overview and explicitly confirmed activation."""

from .game_switches import FEATURES, blocker, enable_group, snapshot
from .store import Rejected


async def action(ui, update, payload):
    """Render admission gates and confirm one-group changes under existing rights.

    Args:
        ui: Existing private administrative renderer.
        update: Authenticated Telegram update.
        payload: Owner-bound server-side callback payload.
    """
    store = ui.store
    uid = str(update.effective_user.id)
    store.require(uid, "game", chat=payload.get("chat"))
    if update.effective_chat.type != "private":
        raise Rejected("请私聊管理本群玩法启停。")
    name = payload["action"]
    if name == "games_group_list":
        rows = store.db.execute(
            "SELECT chat,title FROM platform_mod_groups g WHERE enabled=1 AND "
            "(? OR EXISTS(SELECT 1 FROM mod_acl a WHERE a.chat=g.chat AND a.uid=?)) ORDER BY chat",
            (store.allowed(uid, "manager"), uid),
        ).fetchall()
        page = max(0, min(int(payload.get("page", 0)), max(0, (len(rows) - 1) // 8)))
        buttons = [
            (row["title"], {"action": "games_group_view", "chat": row["chat"]})
            for row in rows[page * 8 : page * 8 + 8]
        ]
        if page:
            buttons.append(("上一页", {"action": name, "page": page - 1}))
        if (page + 1) * 8 < len(rows):
            buttons.append(("下一页", {"action": name, "page": page + 1}))
        return await ui.render(
            update,
            "🎮 本群玩法启停\n选择群查看实际开关与阻塞原因。\n"
            "管理授权、总开关、逐群开关分别控制；新群默认关闭。",
            buttons + [("返回玩法管理", {"action": "admin_game"})],
            fold_sections=True,
        )
    chat = str(payload["chat"])
    await ui.runtime.moderation.check(uid, chat, "view")
    current = snapshot(store, chat)
    title = store.db.execute(
        "SELECT title FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()["title"]
    if name == "games_group_enable_preview":
        return await ui.render(
            update,
            f"🎮 确认开启本群玩法\n群：{title or chat}\n\n📋 本次调整\n"
            "开启本群：积分转盘、老虎机、扫雷、快三、双人对赌。\n"
            "同时确认本群老虎机和扫雷新版开放；无须另改隐藏名单。\n"
            "保留已有档位、次数、冷却、赔率及所有积分和订单。\n\n📌 范围\n"
            "仅修改当前群，不改变总开关、其他群或加拿大28倍率房。\n"
            "若总开关关闭，本群配置就绪后仍暂停新业务。",
            [
                (
                    "确认开启本群",
                    {
                        "action": "games_group_enable",
                        "chat": chat,
                        "expected": current,
                    },
                ),
                ("取消", {"action": "games_group_view", "chat": chat}),
            ],
            fold_sections=True,
        )
    if name == "games_group_enable":
        enable_group(store, uid, chat, payload.get("expected"))
        current = snapshot(store, chat)
    elif name != "games_group_view":
        raise Rejected("无效玩法管理操作。")
    details = []
    for key, label in FEATURES.items():
        config = current["features"][key]
        reason = blocker(
            store,
            chat,
            key,
            config["enabled"],
            rollout=key in {"slots", "mines"},
            points=key != "duel",
        )
        details.append(
            f"{'⚪' if reason else '🟢'} {label} · "
            f"本群{'开启' if config['enabled'] else '关闭'} · "
            f"总开关{'开启' if current['modules'].get(key, key == 'duel') else '关闭'}\n"
            + (
                f"阻塞：{reason}"
                if reason
                else "配置就绪；参与时仍核验成员与业务条件。"
            )
        )
    return await ui.render(
        update,
        f"🎮 本群玩法启停\n群：{title or chat}\n"
        f"玩法中心：{'🟢 开启' if current['modules'].get('game') else '⚪ 关闭'}\n\n"
        "📋 实际状态\n"
        + "\n\n".join(details)
        + "\n\n📌 说明\n总开关不代表本群已开放，授权不自动改开关。\n"
        "加拿大28的倍率、开放时间与数据状态请在加拿大28管理查看。",
        [
            (
                "开启本群全部玩法",
                {"action": "games_group_enable_preview", "chat": chat},
            ),
            ("🎡 转盘设置", {"action": "wheel_group", "chat": chat}),
            ("🎰老虎机设置", {"action": "slots_group", "chat": chat}),
            ("💣 扫雷设置", {"action": "mines_group", "chat": chat}),
            ("🎲 快三设置", {"action": "k3_group", "chat": chat}),
            ("🤝 对赌设置", {"action": "games_duel"}),
            ("返回群列表", {"action": "games_group_list"}),
        ],
        fold_sections=True,
    )
