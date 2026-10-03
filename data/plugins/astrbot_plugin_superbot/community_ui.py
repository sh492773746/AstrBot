"""Button-first public content and scoped community administration."""

import asyncio
import json
import re
from pathlib import Path

from .community import FEATURES, REASONS
from .store import Rejected, encode

WRITE_ACTIONS = {
    "cm_ai_save",
    "cm_save",
    "cm_submit",
    "cm_decide",
    "cm_warn_save",
    "cm_revoke",
    "cm_log_retry",
}


async def group_command(runtime, update, command):
    """Offer a bound private entry without disclosing private case contents.

    Args:
        runtime: Existing plugin runtime.
        update: Native group update.
        command: Already verified bot-targeted command.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
    try:
        if getattr(update.message, "sender_chat", None):
            raise Rejected("请使用个人身份操作")
        if command == "/report":
            value = await runtime.community.ticket(update)
            case = await runtime.community.submit(uid, value, "other")
            await runtime.bot.send_message(
                chat_id=chat,
                text=f"举报已受理，案件 #{case}。由管理员或达到门槛后的自动审理处理，不公开举报理由。",
                parse_mode=None,
            )
            return
        else:
            await runtime.community.member(uid, chat)
            kind = command[1:]
            if not runtime.community.policy(chat)["config"]["enabled"][kind]:
                raise Rejected("此群尚未公开这项内容")
            config = runtime.community.policy(chat)["config"]
            if kind == "rules":
                await runtime.bot.send_message(
                    chat_id=chat, text=config["rules"], parse_mode=None
                )
            else:
                for note in config["notes"]:
                    await runtime.bot.send_message(
                        chat_id=chat,
                        text=note["title"] + "\n" + note["body"],
                        parse_mode=None,
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("相关入口", url=note["url"])]]
                        )
                        if note["url"]
                        else None,
                    )
    except Rejected as exc:
        await runtime.bot.send_message(chat_id=chat, text=str(exc))
    except Exception as exc:
        runtime.report("community_entry", exc)
        await runtime.bot.send_message(chat_id=chat, text="暂时无法打开，请稍后再试。")


async def action(ui, update, payload, token=""):
    """Dispatch server-stored callbacks with separate public and admin paths.

    Args:
        ui: Existing private-menu renderer.
        update: Current Telegram update.
        payload: Bound callback data.
        token: One-use confirmation identity.
    """
    runtime, store = ui.runtime, ui.store
    if getattr(update.effective_chat, "type", "private") != "private":
        raise Rejected("请在私聊操作")
    service = runtime.community
    uid = str(update.effective_user.id)
    op, chat = payload["action"], str(payload.get("chat", ""))
    page = max(0, min(int(payload.get("page", 0)), 100000))
    store.clear_dialog(uid)
    back = [("返回群管理", {"action": "mod_group", "chat": chat})]
    if op == "cm_public_groups":
        rows = store.db.execute(
            "SELECT chat,title FROM mod_groups WHERE enabled=1 ORDER BY chat LIMIT 9 OFFSET ?",
            (page * 8,),
        ).fetchall()
        buttons = []
        for row in rows[:8]:
            try:
                await service.member(uid, row["chat"])
            except Exception:
                continue
            cfg = service.policy(row["chat"])["config"]
            if cfg["enabled"]["rules"] or cfg["enabled"]["notes"]:
                buttons.append(
                    (row["title"], {"action": "cm_public", "chat": row["chat"]})
                )
        if page:
            buttons.append(("上一页", {"action": op, "page": page - 1}))
        if len(rows) > 8:
            buttons.append(("下一页", {"action": op, "page": page + 1}))
        return await ui.render(
            update,
            "选择你所在的群，查看已公开的群规和说明。",
            buttons + [("返回帮助", {"action": "help"})],
        )
    if op in {"cm_public", "cm_rules", "cm_notes", "cm_note"}:
        await service.member(uid, chat)
        cfg = service.policy(chat)["config"]
        buttons = []
        if op == "cm_rules":
            if not cfg["enabled"]["rules"]:
                raise Rejected("此群尚未公开群规")
            text = cfg["rules"]
        elif op in {"cm_notes", "cm_note"}:
            if not cfg["enabled"]["notes"]:
                raise Rejected("此群尚未公开常用说明")
            if op == "cm_note":
                index = int(payload["index"])
                if not 0 <= index < len(cfg["notes"]):
                    raise Rejected("说明已变化，请重新打开")
                note = cfg["notes"][index]
                text = note["title"] + "\n" + note["body"]
                # URL buttons are never translated into internal callbacks.
                if note["url"]:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                    await ui.render(
                        update,
                        text,
                        [("返回说明", {"action": "cm_notes", "chat": chat})],
                    )
                    await runtime.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="相关入口",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton(note["title"], url=note["url"])]]
                        ),
                    )
                    return
            else:
                text = "常用说明"
                buttons = [
                    (n["title"], {"action": "cm_note", "chat": chat, "index": i})
                    for i, n in enumerate(cfg["notes"])
                ]
        else:
            text = "群公开说明。举报请回群回复目标消息发送 /report。"
            buttons = [
                (label, {"action": "cm_" + key, "chat": chat})
                for key, label in (("rules", "群规"), ("notes", "常用说明"))
                if cfg["enabled"][key]
            ]
        return await ui.render(
            update, text, buttons + [("选择其他群", {"action": "cm_public_groups"})]
        )
    if op in {"cm_mine", "cm_my_warnings"}:
        if op == "cm_mine":
            rows = store.db.execute(
                "SELECT c.id,c.chat,c.status,c.outcome FROM cm_cases c JOIN cm_reports r ON r.case_id=c.id WHERE r.uid=? ORDER BY c.created DESC LIMIT 9 OFFSET ?",
                (uid, page * 8),
            ).fetchall()
            text = "我的举报\n" + (
                "\n".join(
                    f"#{r['id']} · 群 {r['chat']} · {r['outcome'] or r['status']}"
                    for r in rows[:8]
                )
                or "还没有举报记录。"
            )
        else:
            rows = store.db.execute(
                "SELECT chat,reason,at,revoked FROM cm_violations WHERE uid=? ORDER BY at DESC LIMIT 9 OFFSET ?",
                (uid, page * 8),
            ).fetchall()
            from datetime import datetime

            from .moderation import TZ

            lines = []
            for row in rows[:8]:
                cfg = json.loads(runtime.ad_killer.policy(row["chat"])["config"])
                status = (
                    "已撤销"
                    if row["revoked"]
                    else "已过期"
                    if row["at"] < store.clock() - cfg["escalation_hours"] * 3600
                    else "有效"
                )
                lines.append(
                    f"群 {row['chat']} · {row['reason']} · {status} · {datetime.fromtimestamp(row['at'], TZ):%m-%d %H:%M}"
                )
            text = "我的警告／违规\n" + ("\n".join(lines) or "暂无记录。")
        buttons = [
            ("我的举报", {"action": "cm_mine"}),
            ("我的警告", {"action": "cm_my_warnings"}),
        ]
        if page:
            buttons.append(("上一页", {"action": op, "page": page - 1}))
        if len(rows) > 8:
            buttons.append(("下一页", {"action": op, "page": page + 1}))
        return await ui.render(
            update, text, buttons + [("返回我的", {"action": "account"})]
        )
    if op in {"cm_report", "cm_report_reason", "cm_report_detail", "cm_submit"}:
        ticket = store.db.execute(
            "SELECT * FROM cm_tickets WHERE token=? AND uid=?", (payload["ticket"], uid)
        ).fetchone()
        if not ticket or ticket["expires"] <= store.clock():
            raise Rejected("举报入口过期或不属于你")
        await service.member(uid, ticket["chat"])
        if op == "cm_submit":
            case_id = await service.submit(
                uid, ticket["token"], payload["reason"], payload.get("detail", "")
            )
            return await ui.render(
                update,
                f"举报已提交，编号 #{case_id}。可在「我的」查看结果。",
                [("我的举报", {"action": "cm_mine"})],
            )
        if op == "cm_report_detail":
            store.dialog(
                uid, {"community": True, "kind": "report_detail", "payload": payload}
            )
            return await ui.render(
                update,
                "请发送补充说明，最多1000字；也可以直接提交。",
                [
                    ("不补充，提交", {**payload, "action": "cm_submit"}),
                    ("取消", {"action": "cm_mine"}),
                ],
            )
        if op == "cm_report":
            return await ui.render(
                update,
                "请选择举报原因。举报不会直接触发处罚，身份只对该群授权管理员可见。",
                [
                    (
                        name,
                        {
                            "action": "cm_report_reason",
                            "ticket": ticket["token"],
                            "reason": key,
                        },
                    )
                    for key, name in REASONS.items()
                ]
                + [("取消", {"action": "cm_mine"})],
            )
        if payload.get("reason") not in REASONS:
            raise Rejected("原因无效")
        return await ui.render(
            update,
            f"举报原因：{REASONS[payload['reason']]}\n{payload.get('detail', '')[:1000]}\n确认提交给该群管理员？",
            [
                ("确认提交", {**payload, "action": "cm_submit"}),
                ("补充说明", {**payload, "action": "cm_report_detail"}),
                ("取消", {"action": "cm_mine"}),
            ],
        )
    # Everything below this boundary is scoped group administration.
    await runtime.moderation.check(uid, chat, "view")
    policy = service.policy(chat)
    cfg = policy["config"]
    if op in {"cm_ai", "cm_ai_save"}:
        await runtime.moderation.check(uid, chat, "delete")
        saved = store.db.execute(
            "SELECT * FROM cm_ai_policy WHERE chat=?", (chat,)
        ).fetchone()
        version = saved["version"] if saved else 0
        enabled = bool(saved and saved["enabled"])
        if op == "cm_ai_save":
            async with runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
                await runtime.moderation.check(uid, chat, "delete")
                fresh = store.db.execute(
                    "SELECT version FROM cm_ai_policy WHERE chat=?", (chat,)
                ).fetchone()
                if (fresh[0] if fresh else 0) != payload["version"]:
                    raise Rejected("自动审理设置已变化")
                with store.tx() as db:
                    db.execute(
                        "INSERT INTO cm_ai_policy(chat,actor,enabled,version) VALUES(?,?,?,1) ON CONFLICT(chat) DO UPDATE SET actor=excluded.actor,enabled=excluded.enabled,version=cm_ai_policy.version+1",
                        (chat, uid, int(payload["enabled"])),
                    )
                    store.audit(
                        db,
                        uid,
                        "community_config",
                        {"chat": chat, "ai_review_enabled": payload["enabled"]},
                    )
            return await ui.render(update, "自动审理设置已保存。", back)
        return await ui.render(
            update,
            f"10人举报自动审理：{'开启' if enabled else '关闭'}\n同一消息10位不同当前成员有效举报后触发一次。"
            "\nAI证据明确且权限核验通过后直接撤回；本功能不自动踢人或封禁。"
            "\n文字证据会交给当前配置的聊天模型，不附举报人信息。模型可能误判，撤回不可恢复；失败、证据不足或结果未知留待人工核查。",
            [
                (
                    "确认关闭" if enabled else "确认开启并直接撤回",
                    {
                        "action": "cm_ai_save",
                        "chat": chat,
                        "version": version,
                        "enabled": not enabled,
                    },
                )
            ]
            + back,
        )
    if op == "cm_template":
        proposed = json.loads(
            Path(__file__)
            .with_name("community_defaults.json")
            .read_text(encoding="utf-8")
        )
        proposed["log"] = cfg["log"]
        return await ui.render(
            update,
            "套用基础配置：开启举报、欢迎、群规、常用说明及人工警告。\n"
            "会替换本群现有欢迎、群规和说明；日志配置、广告杀手、累计升级和入群验证均不变。\n"
            "人工警告只开放管理员操作，不会因此开启自动处罚。确认后可逐项修改。",
            [
                (
                    "确认套用基础配置",
                    {
                        "action": "cm_save",
                        "chat": chat,
                        "version": policy["version"],
                        "config": proposed,
                    },
                ),
                ("查看群规模板", {"action": "cm_template_rules", "chat": chat}),
            ]
            + back,
        )
    if op == "cm_template_rules":
        proposed = json.loads(
            Path(__file__)
            .with_name("community_defaults.json")
            .read_text(encoding="utf-8")
        )
        return await ui.render(
            update,
            proposed["welcome"] + "\n\n" + proposed["rules"],
            [("返回套用配置", {"action": "cm_template", "chat": chat})] + back,
        )
    if op == "cm_note_skip":
        return await input_text(ui, update, payload["dialog"], "-")
    if op == "cm_settings":
        section = payload.get("section", "content")
        keys = {
            "content": ("welcome", "rules"),
            "notes": ("notes",),
            "reports": ("reports",),
            "warnings": ("warnings",),
        }.get(section, ())
        buttons = []
        for key in keys:
            buttons.append(
                (
                    f"{FEATURES[key]}：{'开启' if cfg['enabled'][key] else '关闭'}",
                    {
                        "action": "cm_toggle",
                        "chat": chat,
                        "key": key,
                        "version": policy["version"],
                    },
                )
            )
            if key in {"welcome", "rules"}:
                buttons.append(
                    (
                        "编辑" + FEATURES[key],
                        {"action": "cm_edit", "chat": chat, "field": key},
                    )
                )
        if section == "notes":
            buttons += [
                (n["title"], {"action": "cm_note_edit", "chat": chat, "index": i})
                for i, n in enumerate(cfg["notes"])
            ]
            if len(cfg["notes"]) < 10:
                buttons.append(
                    (
                        "添加说明",
                        {
                            "action": "cm_note_edit",
                            "chat": chat,
                            "index": len(cfg["notes"]),
                        },
                    )
                )
        return await ui.render(
            update,
            "按群配置，改动预览后确认保存。欢迎可使用 {name}、{group}。",
            buttons + back,
        )
    if op in {"cm_edit", "cm_note_edit"}:
        field = payload.get("field", "note")
        if field not in {"welcome", "rules", "note"}:
            raise Rejected("字段无效")
        store.dialog(
            uid,
            {
                "community": True,
                "kind": field,
                "chat": chat,
                "version": policy["version"],
                "index": payload.get("index"),
            },
        )
        if field == "note":
            return await ui.render(
                update,
                "发送说明标题（最多40字），下一步填写正文和可选网址。",
                [("取消", {"action": "cm_settings", "chat": chat, "section": "notes"})]
                + (
                    [
                        (
                            "删除此说明",
                            {
                                "action": "cm_note_delete",
                                "chat": chat,
                                "index": payload["index"],
                                "version": policy["version"],
                            },
                        )
                    ]
                    if payload["index"] < len(cfg["notes"])
                    else []
                ),
            )
        return await ui.render(
            update,
            f"当前{FEATURES[field]}：\n{cfg[field]}\n\n发送新的正文，最多2500字。",
            back,
        )
    if op in {"cm_toggle", "cm_note_delete", "cm_preview", "cm_save"}:
        if payload["version"] != policy["version"]:
            raise Rejected("配置已变化，请重新打开")
        proposed = json.loads(encode(payload.get("config", cfg)))
        if op == "cm_toggle":
            proposed["enabled"][payload["key"]] = not proposed["enabled"][
                payload["key"]
            ]
        elif op == "cm_note_delete":
            del proposed["notes"][int(payload["index"])]
            if not proposed["notes"]:
                proposed["enabled"]["notes"] = False
        if op == "cm_save":
            await service.save(uid, chat, payload["version"], proposed)
            return await ui.render(update, "已保存。", back)
        text = "确认保存本群配置？\n" + "，".join(
            FEATURES[k] + ("开启" if v else "关闭")
            for k, v in proposed["enabled"].items()
        )
        for key in ("welcome", "rules"):
            if (
                proposed[key] != cfg[key]
                or proposed["enabled"][key] != cfg["enabled"][key]
            ):
                text += "\n" + FEATURES[key] + "预览：\n" + proposed[key]
        if proposed["notes"] != cfg["notes"]:
            for note in proposed["notes"]:
                if note not in cfg["notes"]:
                    text += (
                        "\n说明："
                        + note["title"]
                        + "\n"
                        + note["body"]
                        + ("\n" + note["url"] if note["url"] else "")
                    )
            removed = [n["title"] for n in cfg["notes"] if n not in proposed["notes"]]
            if removed:
                text += "\n替换或删除旧说明：" + "、".join(removed)
        if proposed["log"] != cfg["log"]:
            text += "\n日志：" + ("开启" if proposed["log"]["enabled"] else "关闭")
            text += (
                "\n频道："
                + proposed["log"]["title"]
                + "\n频道读者可见群管摘要和成员编号，不包含举报人或证据原文。"
            )
        if len(text) > 3900:
            raise Rejected("预览过长，请分次修改")
        return await ui.render(
            update,
            text,
            [
                (
                    "确认保存",
                    {
                        "action": "cm_save",
                        "chat": chat,
                        "version": policy["version"],
                        "config": proposed,
                    },
                )
            ]
            + back,
        )
    if op == "cm_cases":
        state = payload.get("state", "open")
        if state not in {"open", "closed", "review", "processing"}:
            raise Rejected("分类无效")
        rows = store.db.execute(
            "SELECT * FROM cm_cases WHERE chat=? AND status=? ORDER BY created DESC LIMIT 9 OFFSET ?",
            (chat, state, page * 8),
        ).fetchall()
        buttons = [
            (
                f"案件 #{r['id']} · {r['status']}",
                {"action": "cm_case", "chat": chat, "id": r["id"]},
            )
            for r in rows[:8]
        ]
        if page:
            buttons.append(("上一页", {**payload, "page": page - 1}))
        if len(rows) > 8:
            buttons.append(("下一页", {**payload, "page": page + 1}))
        buttons += [
            (name, {"action": op, "chat": chat, "state": key})
            for key, name in (
                ("open", "待处理"),
                ("closed", "已结案"),
                ("review", "待核查"),
            )
        ]
        buttons.append(
            ("举报启停", {"action": "cm_settings", "chat": chat, "section": "reports"})
        )
        if hasattr(runtime, "report_ai"):
            buttons.append(("10人举报自动审理", {"action": "cm_ai", "chat": chat}))
        return await ui.render(
            update, "举报案件，举报不会直接累计违规。", buttons + back
        )
    if op in {"cm_case", "cm_case_preview", "cm_decide"}:
        case = store.db.execute(
            "SELECT * FROM cm_cases WHERE id=? AND chat=?", (payload["id"], chat)
        ).fetchone()
        if not case:
            raise Rejected("案件不存在")
        if op == "cm_decide":
            await service.decide(
                uid,
                case["id"],
                payload["version"],
                payload["decision"],
                payload.get("reason", "other"),
                token,
                prepared=payload.get("prepared"),
            )
            return await action(
                ui, update, {"action": "cm_case", "chat": chat, "id": case["id"]}
            )
        if op == "cm_case_preview":
            decision = payload["decision"]
            if case["status"] != "open":
                raise Rejected("案件不在待处理状态")
            prepared = None
            if decision in {"delete", "mute", "kick", "ban"}:
                prepared = await runtime.moderation.preview(
                    uid,
                    chat,
                    decision,
                    str(case["message"] if decision == "delete" else case["target"]),
                    10 if decision == "mute" else 0,
                )
            warning_text = ""
            if decision == "warn":
                if payload.get("reason") not in REASONS:
                    return await ui.render(
                        update,
                        "选择本次警告的公开原因，不会公开举报人的补充说明。",
                        [
                            (label, {**payload, "reason": key})
                            for key, label in REASONS.items()
                        ]
                        + [
                            (
                                "返回案件",
                                {"action": "cm_case", "chat": chat, "id": case["id"]},
                            )
                        ],
                    )
                if not cfg["enabled"]["warnings"]:
                    raise Rejected("请先开启本群人工警告")
                ak = runtime.ad_killer.policy(chat)
                settings = json.loads(ak["config"])
                prepared = {"version": policy["version"], "ak_version": ak["version"]}
                warning_text = (
                    f"\n警告原因：{REASONS[payload['reason']]}"
                    f"\n累计窗口{settings['escalation_hours']}小时，阈值{settings['escalation']['count']}次。"
                    f"\n升级{'开启' if ak['enabled'] and settings['escalation']['enabled'] else '关闭'}，"
                    f"动作 {settings['escalation']['action']}，禁言{settings['mute_minutes']}分钟。"
                    "\n踢出可重进；封禁须解除且可能清除历史消息。"
                )
            return await ui.render(
                update,
                f"确认处理案件 #{case['id']}？\n成员 {case['target']} · 消息 {case['message']}\n"
                + {
                    "ignore": "忽略并结案",
                    "warn": "警告并按已启用累计规则处理",
                    "delete": "撤回原消息",
                    "mute": "禁言10分钟",
                    "kick": "踢出，可重新加入；Telegram可能清除历史消息",
                    "ban": "封禁，解除前不能重新加入；Telegram可能清除历史消息",
                }[decision]
                + warning_text,
                [
                    (
                        "确认处理",
                        {
                            "action": "cm_decide",
                            "chat": chat,
                            "id": case["id"],
                            "version": case["version"],
                            "decision": decision,
                            "prepared": prepared,
                            "reason": payload.get("reason", "other"),
                        },
                    ),
                    ("返回案件", {"action": "cm_case", "chat": chat, "id": case["id"]}),
                ],
            )
        reports = store.db.execute(
            "SELECT uid,reason,detail FROM cm_reports WHERE case_id=? ORDER BY created LIMIT 5 OFFSET ?",
            (case["id"], page * 5),
        ).fetchall()
        total = store.db.execute(
            "SELECT COUNT(*) FROM cm_reports WHERE case_id=?", (case["id"],)
        ).fetchone()[0]
        text = f"案件 #{case['id']} · {case['status']}\n成员 {case['target']}\nhttps://t.me/c/{chat.removeprefix('-100')}/{case['message']}\n证据：{(case['body'] or '无文字或已清理')[:1200]}\n"
        text += "\n".join(
            f"举报人 {r['uid']} · {REASONS.get(r['reason'], '其他')}\n{(r['detail'] or '')[:240]}"
            for r in reports
        )
        text += "\n" + case["outcome"]
        if hasattr(runtime, "report_ai"):
            review = store.db.execute(
                "SELECT status,reason FROM cm_ai_reviews WHERE case_id=?", (case["id"],)
            ).fetchone()
            if review:
                text += (
                    "\n自动审理："
                    + review["status"]
                    + "；未确认执行的结果请人工核查，不会自动重发。"
                )
        buttons = []
        if case["status"] == "open":
            buttons += [
                (
                    label,
                    {
                        "action": "cm_case_preview",
                        "chat": chat,
                        "id": case["id"],
                        "decision": key,
                    },
                )
                for key, label in (
                    ("ignore", "忽略"),
                    ("warn", "警告"),
                    ("delete", "撤回"),
                    ("mute", "禁言10分钟"),
                    ("kick", "踢出"),
                    ("ban", "封禁"),
                )
            ]
        if page:
            buttons.append(("上一页举报人", {**payload, "page": page - 1}))
        if total > (page + 1) * 5:
            buttons.append(("下一页举报人", {**payload, "page": page + 1}))
        return await ui.render(
            update,
            text[:3800],
            buttons + [("返回案件列表", {"action": "cm_cases", "chat": chat})],
        )
    if op == "cm_violations":
        rows = store.db.execute(
            "SELECT * FROM cm_violations WHERE chat=? ORDER BY at DESC LIMIT 9 OFFSET ?",
            (chat, page * 8),
        ).fetchall()
        buttons = [
            (
                f"{r['uid']} · {r['reason']} · {'已撤销' if r['revoked'] else '记录'}",
                {"action": "cm_violation", "chat": chat, "id": r["id"]},
            )
            for r in rows[:8]
        ]
        if page:
            buttons.append(("上一页", {**payload, "page": page - 1}))
        if len(rows) > 8:
            buttons.append(("下一页", {**payload, "page": page + 1}))
        buttons += [
            ("新增人工警告", {"action": "cm_warn_members", "chat": chat}),
            (
                "人工警告启停",
                {"action": "cm_settings", "chat": chat, "section": "warnings"},
            ),
            ("累计与处罚设置", {"action": "mod_ak_settings", "chat": chat}),
        ]
        return await ui.render(
            update,
            "统一违规记录：人工警告＋已执行广告命中；同一消息只计一次。",
            buttons + back,
        )
    if op in {"cm_violation", "cm_revoke_preview", "cm_revoke"}:
        row = store.db.execute(
            "SELECT * FROM cm_violations WHERE chat=? AND id=?", (chat, payload["id"])
        ).fetchone()
        if not row:
            raise Rejected("记录不存在")
        if op == "cm_revoke":
            async with runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
                await runtime.moderation.check(uid, chat, "view")
                with store.tx() as db:
                    if not db.execute(
                        "UPDATE cm_violations SET revoked=1,version=version+1 WHERE id=? AND version=? AND revoked=0",
                        (row["id"], payload["version"]),
                    ).rowcount:
                        raise Rejected("记录已变化")
                    if row["message"] is not None:
                        db.execute(
                            "UPDATE ak_hits SET false_positive=1 WHERE chat=? AND message=?",
                            (chat, row["message"]),
                        )
                    store.audit(
                        db, uid, "community_revoke", {"chat": chat, "id": row["id"]}
                    )
            return await action(ui, update, {"action": "cm_violations", "chat": chat})
        text = f"成员 {row['uid']}\n原因 {row['reason']}\n来源 {row['source']} · 升级 {row['escalation'] or '未执行'}\n"
        cfg = json.loads(runtime.ad_killer.policy(chat)["config"])
        text += f"当前有效次数：{service.count(chat, row['uid'], cfg['escalation_hours'])}\n撤销只排除有效次数，不恢复消息、不自动解禁。"
        return await ui.render(
            update,
            text,
            (
                []
                if row["revoked"]
                else [
                    (
                        "确认撤销警告／标记误判"
                        if op == "cm_revoke_preview"
                        else "撤销／误判",
                        {
                            "action": "cm_revoke"
                            if op == "cm_revoke_preview"
                            else "cm_revoke_preview",
                            "chat": chat,
                            "id": row["id"],
                            "version": row["version"],
                        },
                    )
                ]
            )
            + [("返回记录", {"action": "cm_violations", "chat": chat})],
        )
    if op == "cm_warn_members":
        rows = store.db.execute(
            "SELECT uid,name FROM mod_members WHERE chat=? ORDER BY uid LIMIT 9 OFFSET ?",
            (chat, page * 8),
        ).fetchall()
        buttons = [
            (
                r["name"][:30],
                {"action": "cm_warn_reason", "chat": chat, "target": r["uid"]},
            )
            for r in rows[:8]
        ]
        if page:
            buttons.append(("上一页", {**payload, "page": page - 1}))
        if len(rows) > 8:
            buttons.append(("下一页", {**payload, "page": page + 1}))
        return await ui.render(update, "选择机器人已识别的群成员。", buttons + back)
    if op == "cm_warn_reason":
        return await ui.render(
            update,
            "选择警告原因。",
            [
                (label, {**payload, "action": "cm_warn_preview", "reason": key})
                for key, label in REASONS.items()
            ]
            + back,
        )
    if op in {"cm_warn_preview", "cm_warn_save"}:
        if not cfg["enabled"]["warnings"]:
            raise Rejected("请先开启本群人工警告")
        ak = runtime.ad_killer.policy(chat)
        if op == "cm_warn_save":
            if (
                policy["version"] != payload["version"]
                or ak["version"] != payload["ak_version"]
            ):
                raise Rejected("警告或升级配置已变化，请重新预览")
            result = await service.warn(
                uid,
                chat,
                str(payload["target"]),
                payload["reason"],
                "manual:" + token,
                expected=payload,
            )
            return await ui.render(
                update,
                result,
                [("违规记录", {"action": "cm_violations", "chat": chat})],
            )
        penalty = json.loads(ak["config"])
        from .ad_killer import ACTIONS

        text = (
            f"确认警告成员 {payload['target']}？\n原因：{REASONS[payload['reason']]}\n"
        )
        text += f"累计{penalty['escalation_hours']}小时内{penalty['escalation']['count']}次；升级{'开启' if ak['enabled'] and penalty['escalation']['enabled'] else '关闭'}：{ACTIONS[penalty['escalation']['action']]}"
        text += "\n开启升级时本次警告可能触发处罚；踢出可重进，封禁不可自行重进且可能影响历史消息。"
        return await ui.render(
            update,
            text,
            [
                (
                    "确认警告",
                    {
                        **payload,
                        "action": "cm_warn_save",
                        "version": policy["version"],
                        "ak_version": ak["version"],
                    },
                )
            ]
            + back,
        )
    if op in {"cm_logs", "cm_log_edit", "cm_log_toggle", "cm_log_retry"}:
        if op == "cm_log_edit":
            store.dialog(
                uid,
                {
                    "community": True,
                    "kind": "log",
                    "chat": chat,
                    "version": policy["version"],
                },
            )
            return await ui.render(
                update,
                "发送专用私有日志频道的数字 ID。你须为频道管理员，机器人须有发帖权限。\n频道读者能看到群管摘要及成员编号；不会收到举报人或证据原文。",
                back,
            )
        if op == "cm_log_toggle":
            changed = json.loads(encode(cfg))
            changed["log"]["enabled"] = not changed["log"]["enabled"]
            return await action(
                ui,
                update,
                {
                    "action": "cm_preview",
                    "chat": chat,
                    "version": policy["version"],
                    "config": changed,
                },
            )
        if op == "cm_log_retry":
            row = store.db.execute(
                "SELECT * FROM cm_delivery WHERE id=? AND chat=? AND status='blocked' AND private=0",
                (payload["id"], chat),
            ).fetchone()
            if not row or row["error"] not in {"Forbidden", "BadRequest", "Rejected"}:
                raise Rejected("该记录不能安全重发")
            if not cfg["log"]["enabled"] or row["destination"] != cfg["log"]["channel"]:
                raise Rejected("日志目标已变化")
            await runtime.community_logs.check_channel(uid, row["destination"])
            store.db.execute(
                "UPDATE cm_delivery SET status='pending',actor=?,next=0 WHERE id=? AND status='blocked'",
                (uid, row["id"]),
            )
            with store.tx() as db:
                store.audit(db, uid, "community_log_retry", {"chat": chat})
        rows = store.db.execute(
            "SELECT id,status,error FROM cm_delivery WHERE chat=? AND private=0 ORDER BY rowid DESC LIMIT 8",
            (chat,),
        ).fetchall()
        text = (
            f"日志：{'开启' if cfg['log']['enabled'] else '关闭'}\n目标：{cfg['log']['title'] or '未设置'}\n"
            + "\n".join(f"{r['status']} · {r['error'] or '无错误'}" for r in rows)
        )
        buttons = [
            ("设置日志频道", {"action": "cm_log_edit", "chat": chat}),
            (
                "关闭日志" if cfg["log"]["enabled"] else "启用日志",
                {"action": "cm_log_toggle", "chat": chat},
            ),
        ]
        buttons += [
            (
                "确认重试未提交日志",
                {"action": "cm_log_retry", "chat": chat, "id": r["id"]},
            )
            for r in rows
            if r["status"] == "blocked"
            and r["error"] in {"Forbidden", "BadRequest", "Rejected"}
        ]
        return await ui.render(
            update,
            text + "\n结果未知不提供重发；修复权限后只能重试明确未提交的日志。",
            buttons + back,
        )
    raise Rejected("操作不存在")


async def input_text(ui, update, dialog, text):
    """Advance only explicit custom-text forms.

    Args:
        ui: Existing renderer.
        update: Administrator or reporter private message.
        dialog: Persistent form state.
        text: User-provided bounded text.
    """
    uid = str(update.effective_user.id)
    kind = dialog["kind"]
    if kind == "report_detail":
        if len(text) > 1000:
            raise Rejected("补充说明最多1000字")
        return await action(
            ui,
            update,
            {**dialog["payload"], "action": "cm_report_reason", "detail": text},
        )
    chat = dialog["chat"]
    await ui.runtime.moderation.check(uid, chat, "view")
    policy = ui.runtime.community.policy(chat)
    if dialog["version"] != policy["version"]:
        raise Rejected("配置已变化，请重新打开")
    cfg = json.loads(encode(policy["config"]))
    if kind in {"welcome", "rules"}:
        if not 1 <= len(text) <= 2500:
            raise Rejected("请输入1至2500字正文")
        cfg[kind] = text
    elif kind in {"note", "note_body", "note_url"}:
        back = [("取消", {"action": "cm_settings", "chat": chat, "section": "notes"})]
        if kind == "note":
            if not 1 <= len(text) <= 40:
                raise Rejected("标题最多40字")
            ui.store.dialog(uid, {**dialog, "kind": "note_body", "title": text})
            return await ui.render(update, "发送说明正文，最多2500字。", back)
        if kind == "note_body":
            if not 1 <= len(text) <= 2500:
                raise Rejected("正文最多2500字")
            ui.store.dialog(uid, {**dialog, "kind": "note_url", "body": text})
            return await ui.render(
                update,
                "发送可选按钮网址，或点下方跳过。",
                [
                    (
                        "不设置网址",
                        {
                            "action": "cm_note_skip",
                            "chat": chat,
                            "dialog": {**dialog, "body": text, "kind": "note_url"},
                        },
                    )
                ]
                + back,
            )
        if text != "-" and not re.match(r"^https?://", text):
            raise Rejected("请输入 http/https 网址，或发送 - 跳过")
        note = {
            "title": dialog["title"],
            "body": dialog["body"],
            "url": "" if text == "-" else text,
        }
        index = int(dialog["index"])
        if index == len(cfg["notes"]):
            cfg["notes"].append(note)
        elif 0 <= index < len(cfg["notes"]):
            cfg["notes"][index] = note
        else:
            raise Rejected("说明列表已变化")
    elif kind == "log":
        info = await ui.runtime.community_logs.check_channel(uid, text.strip())
        cfg["log"] = {"enabled": False, "channel": str(info.id), "title": info.title}
    else:
        raise Rejected("输入状态不存在")
    return await action(
        ui,
        update,
        {
            "action": "cm_preview",
            "chat": chat,
            "version": policy["version"],
            "config": cfg,
        },
    )
