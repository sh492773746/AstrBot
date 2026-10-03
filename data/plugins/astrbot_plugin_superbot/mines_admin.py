"""Scoped game switches and read-only escrow diagnostics."""

import json

from .game_switches import approve_rollout, blocker
from .mines import RULES
from .slots import group_stakes
from .store import Rejected


async def action(ui, update, payload):
    """Manage only groups authorized by existing game and moderation rights.

    Args:
        ui: Private administrative UI.
        update: Authenticated Telegram update.
        payload: Stored user-bound action.
    """
    store, runtime = ui.store, ui.runtime
    uid = str(update.effective_user.id)
    store.require(uid, "game", chat=payload.get("chat"))
    if update.effective_chat.type != "private":
        raise Rejected("请私聊管理扫雷接龙。")
    name = payload["action"]
    if name == "mines_groups":
        rows = store.db.execute(
            "SELECT chat,title FROM platform_mod_groups g WHERE enabled=1 AND "
            "(? OR EXISTS(SELECT 1 FROM mod_acl a WHERE a.chat=g.chat AND a.uid=?)) ORDER BY chat",
            (store.allowed(uid, "manager"), uid),
        ).fetchall()
        page = max(0, min(int(payload.get("page", 0)), max(0, (len(rows) - 1) // 8)))
        buttons = [
            (r["title"], {"action": "mines_group", "chat": r["chat"]})
            for r in rows[page * 8 : page * 8 + 8]
        ]
        if page:
            buttons.append(("上一页", {"action": name, "page": page - 1}))
        if (page + 1) * 8 < len(rows):
            buttons.append(("下一页", {"action": name, "page": page + 1}))
        return await ui.render(
            update,
            "💣 扫雷接龙管理\n选择群查看开关、桌次及异常；新群默认关闭。",
            buttons + [("返回玩法管理", {"action": "admin_game"})],
            fold_sections=True,
        )
    chat = str(payload["chat"])
    await runtime.moderation.check(uid, chat, "view", enabled=False)
    row = store.db.execute(
        "SELECT * FROM mines_groups WHERE chat=?", (chat,)
    ).fetchone()
    version, enabled = (row["version"], bool(row["enabled"])) if row else (0, False)
    if name in {"mines_preview", "mines_save"}:
        if (
            payload.get("version") != version
            or type(payload.get("enabled")) is not bool
        ):
            raise Rejected("配置已变更，请返回重选。")
        if name == "mines_preview":
            return await ui.render(
                update,
                f"💣 确认调整本群扫雷\n群：{chat}\n状态：{'开启' if payload['enabled'] else '关闭'}\n"
                + (
                    "确认本群老虎机与扫雷新版准入；未开启的玩法仍关闭，不修改总开关或其他群。\n"
                    if payload["enabled"]
                    else ""
                )
                + "关闭后报名桌退款，已开始桌继续完成；不修改总开关。",
                [
                    (
                        "确认",
                        {
                            **payload,
                            "action": "mines_save",
                            "release_new": payload["enabled"],
                            "rollout": chat in store.get("games_v3_groups", []),
                        },
                    ),
                    ("取消", {"action": "mines_group", "chat": chat}),
                ],
                fold_sections=True,
            )
        with store.tx() as db:
            store.require(uid, "game", db=db, chat=chat)
            latest = db.execute(
                "SELECT version FROM mines_groups WHERE chat=?", (chat,)
            ).fetchone()
            if (latest["version"] if latest else 0) != version:
                raise Rejected("配置已变更，请返回重选。")
            released = chat in store.get("games_v3_groups", [], db=db)
            if payload.get("rollout", released) != released:
                raise Rejected("新版开放状态已变化，请重新预览。")
            if payload["enabled"]:
                if not released and payload.get("release_new") is not True:
                    raise Rejected("请重新预览并确认开放本群新版玩法。")
                approve_rollout(store, db, uid, chat)
            db.execute(
                "INSERT INTO mines_groups VALUES(?,?,1) ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled,version=mines_groups.version+1",
                (chat, int(payload["enabled"])),
            )
            store.audit(
                db, uid, "mines_config", {"chat": chat, "enabled": payload["enabled"]}
            )
        version += 1
        enabled = payload["enabled"]
    if name == "mines_table":
        table = store.db.execute(
            "SELECT * FROM mines_tables WHERE id=? AND chat=?", (payload["table"], chat)
        ).fetchone()
        if not table:
            raise Rejected("本群桌次不存在。")
        state = json.loads(table["state"])
        effect = store.db.execute(
            "SELECT status,error FROM mines_effects WHERE table_id=?", (table["id"],)
        ).fetchone()
        text = (
            f"💣 桌次 {table['id']}\n群：{chat}\n状态：{table['status']} · 投递：{table['delivery']}\n"
            f"规则：{state.get('rules', 'mines-1')} · 回合版本：{table['version']}\n"
            f"总池：{sum(p.get('stake', table['stake']) for p in state['people'] if p['status'] != 'withdrawn')} · 已结算抽水：{state.get('fee', 0)}\n异常：{table['error'] or '无'}\n\n"
            "📋 托管与结算\n"
            + "\n".join(
                f"{p['label']} · {p['status']} · 投入{p.get('stake', table['stake'])} · 返还{p['payout']}"
                for p in state["people"]
            )
            + "\n\n📌 恢复规则\n不展示未揭晓雷位；未知发送不重发，连续故障5分钟自动退款，不重抽历史结果。"
        )
        if effect:
            text += f"\n\n🎞 终局动画\n状态：{effect['status']} · 异常：{effect['error'] or '无'}"
        events = store.db.execute(
            "SELECT kind,status,error FROM mines_events WHERE table_id=? ORDER BY rowid",
            (table["id"],),
        ).fetchall()
        if events:
            text += "\n\n🎞 动画记录\n" + "\n".join(
                f"{'奖杯' if e['kind'] == 'trophy' else '淘汰'} · {e['status']} · {e['error'] or '无异常'}"
                for e in events
            )
        return await ui.render(
            update,
            text,
            [("返回本群", {"action": "mines_group", "chat": chat})],
            fold_sections=True,
        )
    modules = store.get("modules", {})
    reason = blocker(store, chat, "mines", enabled, rollout=True)
    tables = store.db.execute(
        "SELECT id,status,error FROM mines_tables WHERE chat=? ORDER BY created DESC LIMIT 8",
        (chat,),
    ).fetchall()
    text = (
        f"💣 本群扫雷接龙\n群：{chat}\n本群开关：{'开启' if enabled else '关闭'}\n"
        f"玩法总开关：{'开启' if modules.get('game') else '关闭'}\n"
        f"新版开放：{'已确认' if chat in store.get('games_v3_groups', []) else '待本群开启确认'}\n"
        f"实际状态：{'⚠️ ' + reason if reason else '🟢 配置就绪'}\n"
        f"扫雷总开关：{'开启' if modules.get('mines') else '关闭'}\n"
        f"本群加入档位：{' / '.join(map(str, group_stakes(store, chat, 'mines')))}\n\n📖 固定规则\n{RULES}\n\n"
        "📋 近期桌次\n"
        + (
            "\n".join(
                f"{r['id']} · {r['status']} · {r['error'] or '无异常'}" for r in tables
            )
            or "暂无"
        )
    )
    buttons = [
        (
            "关闭本群" if enabled else "开启本群",
            {
                "action": "mines_preview",
                "chat": chat,
                "version": version,
                "enabled": not enabled,
            },
        )
    ]
    buttons += [
        (r["id"], {"action": "mines_table", "chat": chat, "table": r["id"]})
        for r in tables
    ]
    return await ui.render(
        update,
        text,
        buttons
        + [
            ("返回群列表", {"action": "mines_groups"}),
            ("本群玩法总览", {"action": "games_group_view", "chat": chat}),
        ],
        fold_sections=True,
    )
