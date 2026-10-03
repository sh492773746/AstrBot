"""Permission-checked, versioned point-wheel group configuration."""

from .game_switches import blocker
from .store import Rejected, encode
from .wheel import RULES


async def action(ui, update, payload):
    """Display or update wheel configuration under existing group authority.

    Args:
        ui: Existing administration renderer.
        update: Private administrator update.
        payload: User-bound callback or validated dialog data.
    """
    store, runtime = ui.store, ui.runtime
    uid = str(update.effective_user.id)
    store.require(uid, "game", chat=payload.get("chat"))
    if update.effective_chat.type != "private":
        raise Rejected("请私聊管理积分转盘。")
    name = payload["action"]
    back = [("返回玩法管理", {"action": "admin_game"})]
    if name == "wheel_groups":
        rows = store.db.execute(
            "SELECT chat,title FROM platform_mod_groups WHERE enabled=1 ORDER BY chat"
        ).fetchall()
        rows = [
            row
            for row in rows
            if store.allowed(uid, "manager")
            or store.db.execute(
                "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?", (uid, row["chat"])
            ).fetchone()
        ]
        page = max(0, min(int(payload.get("page", 0)), max(0, (len(rows) - 1) // 8)))
        buttons = [
            (row["title"], {"action": "wheel_group", "chat": row["chat"]})
            for row in rows[page * 8 : page * 8 + 8]
        ]
        if page:
            buttons.append(("上一页", {"action": name, "page": page - 1}))
        if (page + 1) * 8 < len(rows):
            buttons.append(("下一页", {"action": name, "page": page + 1}))
        return await ui.render(
            update,
            "🎡 积分转盘管理\n选择群，配置启停、档位、次数和冷却。\n新群默认关闭；总开关在「全部功能启停」。",
            buttons + back,
        )
    chat = str(payload["chat"])
    await runtime.moderation.check(uid, chat, "view", enabled=False)
    config, version = runtime.wheel.config(chat)
    if name == "wheel_edit":
        if payload["field"] not in {"stakes", "limit", "cooldown"}:
            raise Rejected("设置项无效")
        store.dialog(
            uid, {"form": "wheel_field", "payload": {**payload, "version": version}}
        )
        prompt = {
            "stakes": "发送1—4个不同档位，用空格分隔；每档5—1000且为5的倍数。\n例如：50 100 500 1000",
            "limit": "发送每人每日次数上限：1—1000。",
            "cooldown": "发送冷却秒数：3—3600。",
        }[payload["field"]]
        return await ui.render(
            update,
            prompt + "\n/cancel 取消",
            [("返回", {"action": "wheel_group", "chat": chat})],
        )
    if name in {"wheel_preview", "wheel_save"}:
        if version != payload["version"]:
            raise Rejected("配置已变化，请返回重选。")
        field, value = payload["field"], payload["value"]
        if field == "stakes":
            try:
                value = (
                    [int(x) for x in value.split()] if isinstance(value, str) else value
                )
            except ValueError as exc:
                raise Rejected("档位必须是整数") from exc
            if (
                not isinstance(value, list)
                or not 1 <= len(value) <= 4
                or any(type(x) is not int or not 5 <= x <= 1000 or x % 5 for x in value)
                or len(set(value)) != len(value)
            ):
                raise Rejected("请输入1—4个不同档位，每档5—1000且为5的倍数。")
            value = sorted(value)
        elif field in {"limit", "cooldown"}:
            try:
                value = int(value)
            except (ValueError, TypeError) as exc:
                raise Rejected("请输入整数") from exc
            low, high = (1, 1000) if field == "limit" else (3, 3600)
            if not low <= value <= high:
                raise Rejected(f"请输入{low}—{high}。")
        elif field != "enabled" or type(value) is not bool:
            raise Rejected("无效设置")
        updated = {**config, field: value}
        if name == "wheel_preview":
            store.clear_dialog(uid)
            return await ui.render(
                update,
                f"🎡 确认修改\n仅影响群 {chat}\n开关：{'开启' if updated['enabled'] else '关闭'}\n档位：{updated['stakes']}\n每日次数：{updated['limit']}\n冷却：{updated['cooldown']}秒\n已结算订单不变。",
                [
                    (
                        "确认保存",
                        {
                            "action": "wheel_save",
                            "chat": chat,
                            "version": version,
                            "field": field,
                            "value": value,
                        },
                    ),
                    ("取消", {"action": "wheel_group", "chat": chat}),
                ],
            )
        with store.tx() as db:
            store.require(uid, "game", db=db, chat=chat)
            if runtime.wheel.config(chat)[1] != version:
                raise Rejected("配置已变化，请返回重选。")
            db.execute(
                "INSERT INTO wheel_groups VALUES(?,?,1) ON CONFLICT(chat) DO UPDATE SET config=excluded.config,version=wheel_groups.version+1",
                (chat, encode(updated)),
            )
            store.audit(db, uid, "wheel_config", {"chat": chat, "config": updated})
        config, version = runtime.wheel.config(chat)
    modules = store.get("modules", {})
    reason = blocker(store, chat, "wheel", config["enabled"])
    return await ui.render(
        update,
        f"🎡 本群积分转盘\n群：{chat}\n本群开关：{'🟢 开启' if config['enabled'] else '⚪ 关闭'}\n"
        f"玩法中心总开关：{'开启' if modules.get('game') else '关闭'}\n"
        f"转盘总开关：{'开启' if modules.get('wheel') else '关闭'}\n"
        f"实际状态：{'⚠️ ' + reason if reason else '🟢 配置就绪'}\n"
        f"档位：{' / '.join(map(str, config['stakes']))}\n每日次数：{config['limit']} · 冷却：{config['cooldown']}秒\n\n📖 固定规则\n{RULES}",
        [
            (
                "关闭" if config["enabled"] else "开启",
                {
                    "action": "wheel_preview",
                    "chat": chat,
                    "version": version,
                    "field": "enabled",
                    "value": not config["enabled"],
                },
            ),
            *[
                (label, {"action": "wheel_edit", "chat": chat, "field": field})
                for label, field in [
                    ("投入档位", "stakes"),
                    ("每日次数", "limit"),
                    ("冷却秒数", "cooldown"),
                ]
            ],
            ("返回选群", {"action": "wheel_groups"}),
            ("本群玩法总览", {"action": "games_group_view", "chat": chat}),
        ],
        fold_sections=True,
    )
