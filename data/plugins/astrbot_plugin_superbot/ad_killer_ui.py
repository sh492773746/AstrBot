"""Private button controls for per-group automatic advertisement moderation."""

import asyncio
import json

from .ad_killer import ACTIONS, RULES, validate
from .store import Rejected

LIMITS = {
    "repeat_count": ("重复达到第几条", [3, 4, 5, 8]),
    "repeat_seconds": ("重复统计秒数", [300, 600, 1200]),
    "flood_count": ("刷屏达到第几条", [5, 6, 8, 10]),
    "flood_seconds": ("刷屏统计秒数", [15, 30, 60]),
    "mute_minutes": ("禁言分钟数", [10, 60, 1440, 10080]),
    "escalation_count": ("累计达到第几次", [2, 3, 5, 10]),
    "escalation_hours": ("累计窗口小时", [1, 24, 72, 168]),
}
LISTS = {
    "keywords": "关键词组合（多项用英文逗号分隔，每项用 + 连接同时出现的词）",
    "users": "账号白名单（数字 UID，多个用英文逗号分隔）",
    "domains": "域名白名单（不带协议和路径，多个用英文逗号分隔）",
    "telegram_allow": "群频道白名单（@用户名、负数ID或完整邀请链接；英文逗号分隔）",
}


async def action(moderation_ui, update, payload, token=""):
    """Render or confirm one policy revision under the existing private route."""
    ui = moderation_ui.ui
    runtime, store = moderation_ui.runtime, moderation_ui.store
    uid = str(update.effective_user.id)
    chat = str(payload.get("chat", ""))
    operation = payload["action"]
    await runtime.moderation.check(uid, chat, "delete")
    row = runtime.ad_killer.policy(chat)
    config = json.loads(row["config"])
    back = [("返回群管理", {"action": "mod_group", "chat": chat})]
    home = [("返回广告杀手", {"action": "mod_ak_home", "chat": chat})]
    if operation.startswith("mod_ak_fp"):
        from .ad_fingerprint_ui import action as fingerprint_action

        return await fingerprint_action(moderation_ui, update, payload)
    if operation.startswith("mod_ak_ai"):
        raise Rejected("AI 广告识别已停用，请使用样本与行为识别")
    if operation == "mod_ak_home":
        rules = "\n".join(
            f"{RULES[key]}：{'开启' if entry['enabled'] else '关闭'} · {ACTIONS[entry['action']]}"
            for key, entry in config["rules"].items()
        )
        buttons = [
            (
                "启用自动执行" if not row["enabled"] else "停用自动执行",
                {
                    "action": "mod_ak_preview",
                    "chat": chat,
                    "version": row["version"],
                    "change": {"enabled": not bool(row["enabled"])},
                },
            ),
            *[
                (label, {"action": "mod_ak_rule", "chat": chat, "rule": key})
                for key, label in RULES.items()
            ],
            ("处罚与阈值", {"action": "mod_ak_settings", "chat": chat}),
            ("关键词、白名单", {"action": "mod_ak_lists", "chat": chat}),
            ("命中记录", {"action": "mod_ak_hits", "chat": chat, "page": 0}),
            ("🛡 样本与行为识别", {"action": "mod_ak_fp", "chat": chat}),
        ]
        if row["error"]:
            buttons.append(("故障核查", {"action": "mod_ak_recovery", "chat": chat}))
        return await ui.render(
            update,
            f"广告杀手 · {'已启用' if row['enabled'] else '未启用'}\n{rules}\n"
            "默认只撤回；禁言、踢出及封禁均需逐项确认。"
            + (
                f"\n故障：{row['error']}。已暂停自动处罚，请先核查远端结果。"
                if row["error"]
                else ""
            ),
            buttons + back,
        )
    if operation == "mod_ak_rule":
        key = payload["rule"]
        if key not in RULES:
            raise Rejected("检测项不存在")
        selected = config["rules"][key]
        buttons = [
            (
                "关闭检测" if selected["enabled"] else "开启检测",
                {
                    "action": "mod_ak_preview",
                    "chat": chat,
                    "version": row["version"],
                    "change": {"rule": key, "enabled": not selected["enabled"]},
                },
            )
        ]
        buttons += [
            (
                label + (" ✓" if selected["action"] == action_name else ""),
                {
                    "action": "mod_ak_preview",
                    "chat": chat,
                    "version": row["version"],
                    "change": {"rule": key, "action": action_name},
                },
            )
            for action_name, label in ACTIONS.items()
        ]
        return await ui.render(
            update,
            f"{RULES[key]} · {'开启' if selected['enabled'] else '关闭'}\n"
            f"处罚：{ACTIONS[selected['action']]}\n"
            + (
                "只处理普通成员手动转发的频道内容；关联频道自动同步、匿名频道身份不自动处罚。\n"
                if key == "channel_forward"
                else "公开目标需核实为群/频道；邀请链接可能无法分辨本群，先加完整邀请链接白名单。\n"
                if key == "group_links"
                else "识别非 Telegram 网站和平台链接；正常网站也可能命中，请先配置域名白名单。\n"
                if key == "platform_links"
                else ""
            )
            + "处罚含撤回；踢出后可重新加入，封禁需人工解除，Telegram 可能移除历史消息。",
            buttons + home,
        )
    if operation == "mod_ak_settings":
        buttons = [
            (
                f"{label}：{config['escalation']['count'] if key == 'escalation_count' else config[key]}",
                {"action": "mod_ak_limit", "chat": chat, "key": key},
            )
            for key, (label, _) in LIMITS.items()
        ]
        buttons += [
            (
                "启用累计升级"
                if not config["escalation"]["enabled"]
                else "关闭累计升级",
                {
                    "action": "mod_ak_preview",
                    "chat": chat,
                    "version": row["version"],
                    "change": {
                        "escalation_enabled": not config["escalation"]["enabled"]
                    },
                },
            ),
            (
                "累计升级处罚：" + ACTIONS[config["escalation"]["action"]],
                {"action": "mod_ak_escalation", "chat": chat},
            ),
        ]
        return await ui.render(
            update,
            "重复和刷屏按同一成员统计；累计窗口可调整。统一累计仅含上线后的有效警告和已受理广告命中，撤销、误判及过期不计。",
            buttons + home,
        )
    if operation == "mod_ak_escalation":
        return await ui.render(
            update,
            "选择累计升级处罚。重处罚须再次确认；规则尚未启用时不会自动执行。",
            [
                (
                    label,
                    {
                        "action": "mod_ak_preview",
                        "chat": chat,
                        "version": row["version"],
                        "change": {"escalation_action": key},
                    },
                )
                for key, label in ACTIONS.items()
            ]
            + home,
        )
    if operation == "mod_ak_limit":
        key = payload["key"]
        if key not in LIMITS:
            raise Rejected("数值项目不存在")
        label, choices = LIMITS[key]
        store.dialog(
            uid,
            {
                "moderation": True,
                "kind": "adkiller:" + key,
                "chat": chat,
                "version": row["version"],
            },
        )
        return await ui.render(
            update,
            f"{label}，点击常用值或单独发送整数。",
            [
                (
                    str(value),
                    {
                        "action": "mod_ak_preview",
                        "chat": chat,
                        "version": row["version"],
                        "change": {key: value},
                    },
                )
                for value in choices
            ]
            + home,
        )
    if operation == "mod_ak_lists":
        return await ui.render(
            update,
            "账号白名单豁免该成员；域名白名单豁免对应链接。群频道白名单放行对应链接或频道转发来源，不豁免关键词、重复和刷屏。群主、群管理员和机器人自身自动豁免。",
            [
                (
                    label + f"（{len(config[key])}）",
                    {"action": "mod_ak_list", "chat": chat, "key": key},
                )
                for key, label in LISTS.items()
            ]
            + home,
        )
    if operation == "mod_ak_list":
        key = payload["key"]
        if key not in LISTS:
            raise Rejected("列表项目不存在")
        store.dialog(
            uid,
            {
                "moderation": True,
                "kind": "adkiller:" + key,
                "chat": chat,
                "version": row["version"],
            },
        )
        return await ui.render(
            update,
            f"{LISTS[key]}\n当前：{', '.join(config[key]) or '空'}\n"
            "发送完整新列表；发送 - 清空。保存前还需按钮确认。",
            home,
        )
    if operation == "mod_ak_preview":
        if row["version"] != payload.get("version"):
            raise Rejected("规则已变化，请重新打开")
        change = payload["change"]
        updated = json.loads(row["config"])
        enabled = bool(row["enabled"])
        if set(change) == {"enabled"} and type(change["enabled"]) is bool:
            enabled = change["enabled"]
        elif "rule" in change and change["rule"] in RULES and len(change) == 2:
            key = change["rule"]
            if "enabled" in change and type(change["enabled"]) is bool:
                updated["rules"][key]["enabled"] = change["enabled"]
            elif "action" in change and change["action"] in ACTIONS:
                updated["rules"][key]["action"] = change["action"]
            else:
                raise Rejected("规则选项无效")
        elif len(change) == 1:
            key, value = next(iter(change.items()))
            if key in LIMITS:
                if key == "escalation_count":
                    updated["escalation"]["count"] = value
                else:
                    updated[key] = value
            elif key in LISTS:
                updated[key] = value
            elif key == "escalation_enabled" and type(value) is bool:
                updated["escalation"]["enabled"] = value
            elif key == "escalation_action" and value in ACTIONS:
                updated["escalation"]["action"] = value
            else:
                raise Rejected("配置选项无效")
        else:
            raise Rejected("配置选项无效")
        validate(updated)
        if (
            enabled
            and any(
                r["enabled"] and r["action"] != "delete"
                for r in updated["rules"].values()
            )
            or (
                enabled
                and updated["escalation"]["enabled"]
                and updated["escalation"]["action"] != "delete"
            )
        ):
            await runtime.moderation.check(uid, chat, "mute")
        penalties = (
            "、".join(
                f"{RULES[k]}→{ACTIONS[v['action']]}"
                for k, v in updated["rules"].items()
                if v["enabled"]
            )
            or "无检测项"
        )
        return await ui.render(
            update,
            f"确认保存本群规则？\n{'启用后立即自动执行' if enabled else '自动执行关闭'}\n"
            f"{penalties}\n累计升级：{'开启' if updated['escalation']['enabled'] else '关闭'}，"
            f"{updated['escalation']['count']}次→{ACTIONS[updated['escalation']['action']]}\n"
            "误判后消息无法恢复；踢出可重新加入，封禁不能自行重进且可能影响历史消息。"
            "\n同一群已有待核查操作时不能启用。",
            [
                (
                    "确认保存",
                    {
                        "action": "mod_ak_save",
                        "chat": chat,
                        "version": row["version"],
                        "enabled": enabled,
                        "config": updated,
                    },
                ),
                ("返回广告杀手", {"action": "mod_ak_home", "chat": chat}),
            ],
        )
    if operation == "mod_ak_save":
        config = payload["config"]
        validate(config)
        if payload["enabled"] and (
            any(
                rule["enabled"] and rule["action"] != "delete"
                for rule in config["rules"].values()
            )
            or config["escalation"]["enabled"]
            and config["escalation"]["action"] != "delete"
        ):
            await runtime.moderation.check(uid, chat, "mute")
        async with runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
            runtime.ad_killer.save(
                chat, uid, payload["version"], config, payload["enabled"]
            )
        return await action(
            moderation_ui, update, {"action": "mod_ak_home", "chat": chat}
        )
    if operation == "mod_ak_recovery":
        if not row["error"]:
            raise Rejected("当前群无待核查故障")
        pending = store.db.execute(
            "SELECT message,uid,step,error FROM ak_hits WHERE chat=? AND status='review' ORDER BY at DESC LIMIT 10",
            (chat,),
        ).fetchall()
        count = store.db.execute(
            "SELECT COUNT(*) FROM ak_hits WHERE chat=? AND status='review'", (chat,)
        ).fetchone()[0]
        details = "\n".join(
            f"消息 {hit['message']} / 用户 {hit['uid']} / {hit['step']} / {hit['error']}"
            for hit in pending
        )
        return await ui.render(
            update,
            f"故障核查 · {count}条请求结果不明\n{row['error']}\n"
            f"{details or '无本模块结果不明的消息，检查其他群管操作记录。'}\n"
            "请先查看命中记录与 Telegram 当前状态；确认后只关闭本模块故障，"
            "不会重发处罚，也不会自动恢复规则。其他群管待核查操作须单独处理。",
            [
                (
                    "已核查，查看确认",
                    {
                        "action": "mod_ak_recovery_preview",
                        "chat": chat,
                        "version": row["version"],
                        "count": count,
                    },
                )
            ]
            + home,
        )
    if operation == "mod_ak_recovery_preview":
        if not row["error"] or row["version"] != payload["version"]:
            raise Rejected("故障状态已变化，请重新打开")
        count = store.db.execute(
            "SELECT COUNT(*) FROM ak_hits WHERE chat=? AND status='review'", (chat,)
        ).fetchone()[0]
        if count != payload["count"]:
            raise Rejected("待核查记录已变化，请重新打开")
        return await ui.render(
            update,
            f"请确认：已人工核实 Telegram 群 {chat} 的当前状态与 {count} 条待核查记录，"
            "并已手动处理必要的禁言、踢出或封禁后果。\n"
            "确认只标记本模块记录为已人工核查并保持规则关闭；"
            "不会自动重试、自动恢复广告杀手或处理其他群管故障。",
            [
                (
                    "确认已核查（保持关闭）",
                    {
                        "action": "mod_ak_recovery_confirm",
                        "chat": chat,
                        "version": row["version"],
                        "count": count,
                    },
                )
            ]
            + home,
        )
    if operation == "mod_ak_recovery_confirm":
        async with runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
            runtime.ad_killer.acknowledge(
                chat, uid, payload["version"], payload["count"]
            )
        return await action(
            moderation_ui, update, {"action": "mod_ak_home", "chat": chat}
        )
    if operation == "mod_ak_hits":
        page = max(0, int(payload.get("page", 0)))
        total = store.db.execute(
            "SELECT COUNT(*) FROM ak_hits WHERE chat=?", (chat,)
        ).fetchone()[0]
        page = min(page, max(0, (total - 1) // 8))
        rows = store.db.execute(
            "SELECT message,uid,rules,status FROM ak_hits WHERE chat=? ORDER BY at DESC,message DESC LIMIT 8 OFFSET ?",
            (chat, page * 8),
        ).fetchall()
        buttons = [
            (
                f"消息{r['message']} · {r['status']}",
                {"action": "mod_ak_hit", "chat": chat, "message": r["message"]},
            )
            for r in rows
        ]
        if page:
            buttons.append(
                ("上一页", {"action": "mod_ak_hits", "chat": chat, "page": page - 1})
            )
        if total > 8 * (page + 1):
            buttons.append(
                ("下一页", {"action": "mod_ak_hits", "chat": chat, "page": page + 1})
            )
        return await ui.render(
            update,
            f"命中记录 · 第{page + 1}页，共{total}条。待核查须检查 Telegram 当前状态，不自动重发。",
            buttons + home,
        )
    if operation in {"mod_ak_hit", "mod_ak_false"}:
        hit = store.db.execute(
            "SELECT * FROM ak_hits WHERE chat=? AND message=?",
            (chat, payload["message"]),
        ).fetchone()
        if not hit:
            raise Rejected("命中记录不存在")
        if operation == "mod_ak_false":
            if hit["false_positive"]:
                raise Rejected("已标记误判")
            async with runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
                await runtime.moderation.check(uid, chat, "view")
                with store.tx() as db:
                    db.execute(
                        "UPDATE ak_hits SET false_positive=1 WHERE chat=? AND message=? AND false_positive=0",
                        (chat, hit["message"]),
                    )
                    if "fingerprint" in json.loads(hit["rules"]):
                        # Local exception is committed with the original correction.
                        event = db.execute(
                            "SELECT sample FROM fp_events WHERE chat=? AND message=?",
                            (chat, hit["message"]),
                        ).fetchone()
                        if event and event["sample"]:
                            db.execute(
                                "INSERT OR IGNORE INTO fp_exceptions VALUES(?,?,?,?)",
                                (chat, event["sample"], uid, store.clock()),
                            )
                        db.execute(
                            "UPDATE fp_events SET reviewed=-1 WHERE chat=? AND message=?",
                            (chat, hit["message"]),
                        )
                    if hasattr(runtime, "community"):
                        db.execute(
                            "UPDATE cm_violations SET revoked=1,version=version+1 WHERE chat=? AND message=? AND revoked=0",
                            (chat, hit["message"]),
                        )
                    store.audit(
                        db,
                        uid,
                        "ad_killer_false_positive",
                        {"chat": chat, "message": hit["message"]},
                    )
            return await action(
                moderation_ui,
                update,
                {"action": "mod_ak_hit", "chat": chat, "message": hit["message"]},
            )
        message_link = f"https://t.me/c/{chat.removeprefix('-100')}/{hit['message']}"
        return await ui.render(
            update,
            f"命中 {', '.join({'ai': '历史AI识别', 'fingerprint': '样本识别'}.get(k, RULES.get(k, k)) for k in json.loads(hit['rules']))}\n"
            f"用户 {hit['uid']} · 消息 {message_link}\n"
            f"处罚 {ACTIONS[hit['action']]} · {hit['status']} · 步骤 {hit['step']}\n"
            f"内容：{('已按30天规则清理' if hit['body'] is None else hit['body'] or '无文字的转发或媒体消息')[:1500]}\n"
            f"{'已标记误判' if hit['false_positive'] else '可标记误判，不会恢复已删除的消息。'}"
            + (f"\n故障：{hit['error']}（维护人员核查）" if hit["error"] else ""),
            (
                []
                if hit["false_positive"]
                else [
                    (
                        "标记误判",
                        {
                            "action": "mod_ak_false_preview",
                            "chat": chat,
                            "message": hit["message"],
                        },
                    )
                ]
            )
            + (
                [
                    (
                        "提交全局待审样本",
                        {
                            "action": "mod_ak_fp_from_hit",
                            "chat": chat,
                            "message": hit["message"],
                        },
                    )
                ]
                if hit["body"] and not hit["false_positive"]
                else []
            )
            + [("返回记录", {"action": "mod_ak_hits", "chat": chat})],
        )
    if operation == "mod_ak_false_preview":
        return await ui.render(
            update,
            "确认标记误判？该操作只影响今后的累计统计，不会恢复消息或自动解禁。",
            [
                (
                    "确认误判",
                    {
                        "action": "mod_ak_false",
                        "chat": chat,
                        "message": payload["message"],
                    },
                ),
                (
                    "返回记录",
                    {
                        "action": "mod_ak_hit",
                        "chat": chat,
                        "message": payload["message"],
                    },
                ),
            ],
        )
    raise Rejected("广告杀手操作不存在")


async def input_text(moderation_ui, update, kind, chat, text):
    """Convert custom numbers and lists to the same button confirmation path."""
    uid = str(update.effective_user.id)
    dialog = moderation_ui.store.dialog(uid)
    if (
        not dialog
        or dialog.get("kind") != "adkiller:" + kind
        or dialog.get("chat") != chat
    ):
        raise Rejected("输入已过期，请重新打开设置")
    version = dialog["version"]
    if kind in LIMITS:
        if not text.isascii() or not text.isdigit():
            raise Rejected("请输入整数")
        change = {kind: int(text)}
    elif kind in LISTS:
        values = (
            []
            if text.strip() == "-"
            else [
                v.strip()
                if kind == "telegram_allow" and v.strip().startswith("https://")
                else v.strip().lower()
                for v in text.replace("，", ",").split(",")
            ]
        )
        if not all(values) or len(text) > 1000:
            raise Rejected("请发送有效列表，或用 - 清空")
        change = {kind: list(dict.fromkeys(values))}
    else:
        raise Rejected("设置项不存在")
    return await action(
        moderation_ui,
        update,
        {
            "action": "mod_ak_preview",
            "chat": chat,
            "version": version,
            "change": change,
        },
    )
