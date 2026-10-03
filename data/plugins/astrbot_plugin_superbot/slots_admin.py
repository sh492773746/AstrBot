"""Group-authorized slot switches and read-only escrow/delivery diagnostics."""

import json

from .game_switches import approve_rollout, blocker
from .slots import RULES, group_stakes
from .store import Rejected


async def action(ui, update, payload):
    """Expose group switches and exact table records under game authority.

    Args:
        ui: Existing private admin UI.
        update: Actual Telegram admin update.
        payload: Existing owner-bound callback payload.
    """
    store, runtime = ui.store, ui.runtime
    uid = str(update.effective_user.id)
    store.require(uid, "game", chat=payload.get("chat"))
    if update.effective_chat.type != "private":
        raise Rejected("请私聊管理老虎机。")
    name = payload["action"]
    if name == "slots_groups":
        rows = store.db.execute(
            "SELECT chat,title FROM platform_mod_groups g WHERE enabled=1 AND "
            "(? OR EXISTS(SELECT 1 FROM mod_acl a WHERE a.chat=g.chat AND a.uid=?)) ORDER BY chat",
            (store.allowed(uid, "manager"), uid),
        ).fetchall()
        page = max(0, min(int(payload.get("page", 0)), max(0, (len(rows) - 1) // 8)))
        buttons = [
            (r["title"], {"action": "slots_group", "chat": r["chat"]})
            for r in rows[page * 8 : page * 8 + 8]
        ]
        if page:
            buttons.append(("上一页", {"action": name, "page": page - 1}))
        if (page + 1) * 8 < len(rows):
            buttons.append(("下一页", {"action": name, "page": page + 1}))
        return await ui.render(
            update,
            "🎰老虎机PvP管理\n逐群启停、托管、退款与结算核查。\n新群默认关闭；总开关在「全部功能启停」。",
            buttons + [("返回玩法管理", {"action": "admin_game"})],
            fold_sections=True,
        )
    chat = str(payload["chat"])
    await runtime.moderation.check(uid, chat, "view", enabled=False)
    current = store.db.execute(
        "SELECT * FROM slots_groups WHERE chat=?", (chat,)
    ).fetchone()
    version = current["version"] if current else 0
    enabled = bool(current["enabled"]) if current else False
    if name in {"slots_preview", "slots_save"}:
        if (
            payload.get("version") != version
            or type(payload.get("enabled")) is not bool
        ):
            raise Rejected("设置已改变，请返回重选。")
        if name == "slots_preview":
            return await ui.render(
                update,
                f"🎰 确认调整\n群：{chat}\n本群开关：{'开启' if payload['enabled'] else '关闭'}\n"
                + (
                    "确认本群老虎机与扫雷新版准入；未开启的玩法仍关闭，不修改总开关或其他群。\n"
                    if payload["enabled"]
                    else ""
                )
                + "关闭后：未锁定桌取消退款；已结算不撤销。",
                [
                    (
                        "确认",
                        {
                            **payload,
                            "action": "slots_save",
                            "release_new": payload["enabled"],
                            "rollout": chat in store.get("games_v3_groups", []),
                        },
                    ),
                    ("取消", {"action": "slots_group", "chat": chat}),
                ],
                fold_sections=True,
            )
        with store.tx() as db:
            store.require(uid, "game", db=db, chat=chat)
            latest = db.execute(
                "SELECT version FROM slots_groups WHERE chat=?", (chat,)
            ).fetchone()
            if (latest["version"] if latest else 0) != version:
                raise Rejected("设置已改变，请返回重选。")
            released = chat in store.get("games_v3_groups", [], db=db)
            if payload.get("rollout", released) != released:
                raise Rejected("新版开放状态已变化，请重新预览。")
            if payload["enabled"]:
                if not released and payload.get("release_new") is not True:
                    raise Rejected("请重新预览并确认开放本群新版玩法。")
                approve_rollout(store, db, uid, chat)
            db.execute(
                "INSERT INTO slots_groups VALUES(?,?,1) ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled,version=slots_groups.version+1",
                (chat, int(payload["enabled"])),
            )
            store.audit(
                db, uid, "slots_config", {"chat": chat, "enabled": payload["enabled"]}
            )
        version += 1
        enabled = payload["enabled"]
    back = [("返回本群", {"action": "slots_group", "chat": chat})]
    if name == "slots_table":
        table = store.db.execute(
            "SELECT * FROM slots_tables WHERE id=? AND chat=?", (payload["table"], chat)
        ).fetchone()
        if not table:
            raise Rejected("桌次不存在。")
        people = store.db.execute(
            "SELECT * FROM slots_players WHERE table_id=? ORDER BY ordinal",
            (table["id"],),
        ).fetchall()
        deliveries = store.db.execute(
            "SELECT kind,status,error FROM slots_outbox WHERE table_id=? ORDER BY seq",
            (table["id"],),
        ).fetchall()
        snapshot = json.loads(table["snapshot"])
        lucky_points = store.db.execute(
            "SELECT round,uid,luck FROM slots_v3_rounds WHERE table_id=? AND luck IS NOT NULL ORDER BY round,seq",
            (table["id"],),
        ).fetchall()
        return await ui.render(
            update,
            f"🎰 桌次 {table['id']}\n群：{chat}\n状态：{table['status']}\n"
            f"规则：{snapshot['version']} · 模式：{ {'solo': '单人', 'pool': '多人奖池'}.get(snapshot.get('mode'), '报名中/旧版') }\n"
            f"加赛轮数：{snapshot.get('last_round', 0)}\n总投入：{sum(p['stake'] for p in people if p['status'] != 'withdrawn')} · 抽水：{table['fee']}\n异常：{table['error'] or '无'}\n\n"
            "📋 成员与流水\n"
            + "\n".join(
                f"{p['label']} · {p['status']} · 投入 {p['stake']} · 派发/退款 {p['payout']}"
                for p in people
            )
            + (
                "\n\n🍀 幸运点（同牌型高者胜）\n"
                + "\n".join(
                    f"第{r['round'] + 1}轮 · 用户{r['uid']} · {r['luck']:,}"
                    for r in lucky_points
                )
                if lucky_points
                else ""
            )
            + "\n\n📨 投递记录\n"
            + "\n".join(
                f"{d['kind']} · {d['status']} · {d['error'] or '无异常'}"
                for d in deliveries
            )
            + "\n\n结果不明不自动补发；不会重新抽取或重复派发。",
            back,
            fold_sections=True,
        )
    tables = store.db.execute(
        "SELECT id,status FROM slots_tables WHERE chat=? ORDER BY created DESC LIMIT 8",
        (chat,),
    ).fetchall()
    escrow = store.db.execute(
        "SELECT coalesce(sum(p.stake),0) FROM slots_tables t JOIN slots_players p ON p.table_id=t.id WHERE t.chat=? AND t.status IN ('open','locked') AND p.status='active'",
        (chat,),
    ).fetchone()[0]
    reason = blocker(store, chat, "slots", enabled, rollout=True)
    return await ui.render(
        update,
        f"🎰 本群老虎机PvP\n群：{chat}\n本群开关：{'🟢 开启' if enabled else '⚪ 关闭'}\n"
        f"玩法总开关：{'开启' if store.get('modules', {}).get('game') else '关闭'}\n"
        f"老虎机总开关：{'开启' if store.get('modules', {}).get('slots') else '关闭'}\n"
        f"新版开放：{'已确认' if chat in store.get('games_v3_groups', []) else '待本群开启确认'}\n"
        f"实际状态：{'⚠️ ' + reason if reason else '🟢 配置就绪'}\n"
        f"当前托管：{escrow}积分\n本群开桌档位：{' / '.join(map(str, group_stakes(store, chat)))}\n"
        f"同桌玩家与发起者投入相同。\n\n📖 固定规则\n{RULES}",
        [
            (
                "关闭" if enabled else "开启",
                {
                    "action": "slots_preview",
                    "chat": chat,
                    "version": version,
                    "enabled": not enabled,
                },
            ),
            *[
                (
                    f"{t['id']} · {t['status']}",
                    {"action": "slots_table", "chat": chat, "table": t["id"]},
                )
                for t in tables
            ],
            ("返回选群", {"action": "slots_groups"}),
            ("本群玩法总览", {"action": "games_group_view", "chat": chat}),
        ],
        fold_sections=True,
    )
