"""Private single-value activity settings and user-bound public queries."""

import html
import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup

from .activities import TZ
from .command_router import classify, ownership
from .group_authority import require
from .message_policy import retention
from .store import Rejected, encode
from .tenants import binding, local_config

STEPS = (
    "kind",
    "title",
    "description",
    "start",
    "duration",
    "preset",
    "minimum",
    "profile",
    "link",
    "prizes",
)
PROMPTS = {
    "kind": "选择活动类型。",
    "title": "发送活动标题（1—60字）。",
    "description": "发送活动说明（最多600字）；发送“跳过”留空。",
    "start": "发送“现在”，或北京时间 YYYY-MM-DD HH:MM。",
    "duration": "发送持续分钟数（5—43200）；也可点快捷时长。",
    "preset": "选择有效发言标准。标准：排除重复、命令和下注；严格：另需5个有效字符、3个不同字符，每30秒最多1条。",
    "minimum": "发送最低有效发言次数（1—100000），默认1。",
    "profile": "是否要求参与者主页挂本群链接？需要管理员人工审核。",
    "link": "发送要求展示的完整 t.me 群链接；私密群由你提供现有邀请链接，不自动创建。",
    "prizes": "逐条发送奖品说明（每项最多200字、最多50名）。填完点“奖品填完”。奖品由群管理员人工兑现。",
}
STATES = {
    "draft": "草稿",
    "publishing": "发布中",
    "active": "进行中",
    "review": "待审核",
    "finalizing": "公布中",
    "closed": "已公布",
    "cancelled": "已取消",
    "delivery_review": "投递待核查",
}
FULFIL = {"pending": "待发放", "sent": "已发放", "waived": "放弃", "disputed": "争议"}


async def action(ui, update, data):
    """Dispatch private administration inside a fresh group authority session.

    Args:
        ui: Existing private renderer.
        update: Authenticated private update.
        data: Actor-bound callback.
    """
    if update.effective_chat.type != "private":
        raise Rejected("请在私聊打开「我的群」。")
    runtime, store = ui.runtime, ui.store
    service, actor, chat = (
        runtime.activities,
        str(update.effective_user.id),
        str(data["chat"]),
    )
    proof = require(store, actor, chat)
    name = data["action"]
    group = store.db.execute(
        "SELECT title,version FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()
    back = [("返回本群", {"action": "activity_group", "chat": chat})]
    if name == "activity_group":
        store.clear_dialog(actor)
        tenant = binding(store, chat)
        return await ui.render(
            update,
            f"⚙️ 正在管理：{group['title']}\n仅群主与指定群管可操作；积分和记录按群独立。",
            [
                (
                    "本群业务管理",
                    {
                        "action": "semantic_group_menu"
                        if not tenant or tenant["tenant"] == "platform"
                        else "tenant_group",
                        "chat": chat,
                    },
                ),
                ("📊 排行榜与统计", {"action": "activity_stats_menu", "chat": chat}),
                ("🎉 活动管理", {"action": "activity_home", "chat": chat}),
                ("命令与交互", {"action": "interaction_home", "chat": chat}),
                ("指定群管", {"action": "activity_grants", "chat": chat}),
                ("返回我的群", {"action": "tenant_home"}),
            ],
            fold_sections=True,
        )
    if name == "interaction_home":
        mode = local_config(store, chat, "command_mode", "compat")
        return await ui.render(
            update,
            f"⌨️ {group['title']} · 命令与交互\n"
            f"当前：{'严格指向本BOT' if mode == 'strict' else '兼容裸命令'}\n"
            "兼容：/help 与 /help@本BOT 均可；同名裸命令可能被其他BOT响应。\n"
            "严格：仅接收 /命令@本BOT；自然语言、按钮与房间下注仍可用。\n"
            "回复其他机器人的文字不触发本BOT业务。",
            [
                (
                    ("✅ " if mode == "compat" else "") + "兼容模式",
                    {
                        "action": "interaction_save",
                        "chat": chat,
                        "mode": "compat",
                        "version": group["version"],
                    },
                ),
                (
                    ("✅ " if mode == "strict" else "") + "严格模式",
                    {
                        "action": "interaction_save",
                        "chat": chat,
                        "mode": "strict",
                        "version": group["version"],
                    },
                ),
                ("虚拟键盘", {"action": "interaction_keyboard", "chat": chat}),
            ]
            + back,
            fold_sections=True,
        )
    if name == "interaction_save":
        with store.tx() as db:
            require(store, actor, chat, db=db)
            if data["version"] != group["version"] or data["mode"] not in {
                "compat",
                "strict",
            }:
                raise Rejected("群设置已变化，请重新打开。")
            db.execute(
                "INSERT INTO tenant_settings(chat,key,value) VALUES(?,'command_mode',?) ON CONFLICT(chat,key) DO UPDATE SET value=excluded.value,version=tenant_settings.version+1",
                (chat, encode(data["mode"])),
            )
            db.execute("UPDATE mod_groups SET version=version+1 WHERE chat=?", (chat,))
            store.audit(
                db, actor, "group_command_mode", {"chat": chat, "mode": data["mode"]}
            )
        return await ui.render(update, "✅ 已保存本群命令模式。", back)
    if name in {"interaction_keyboard", "interaction_keyboard_save"}:
        keyboard = getattr(runtime, "group_keyboard", None)
        if not keyboard:
            raise Rejected("群键盘服务尚未就绪，请稍后重试。")
        if name == "interaction_keyboard_save":
            if data.get("version") != group["version"]:
                raise Rejected("群设置已变化，请重新打开。")
            await keyboard.set_for_chat(chat, actor, data["enabled"])
            return await ui.render(update, "✅ 本群键盘设置已保存。", back)
        setting = store.db.execute(
            "SELECT enabled FROM group_keyboard_settings WHERE chat=?", (chat,)
        ).fetchone()
        enabled = bool(setting and setting["enabled"])
        return await ui.render(
            update,
            f"⌨️ {group['title']} · 虚拟键盘\n当前：{'✅ 开启' if enabled else '⏸ 关闭'}\n"
            "仅本群生效，可通过Telegram输入框旁的键盘图标收起或展开。",
            [
                (
                    ("关闭" if enabled else "开启"),
                    {
                        "action": "interaction_keyboard_save",
                        "chat": chat,
                        "version": group["version"],
                        "enabled": not enabled,
                    },
                )
            ]
            + back,
            fold_sections=True,
        )
    if name.startswith("activity_grant"):
        if proof.status != "creator":
            raise Rejected("只有当前群主可以指定或撤销群管。")
        if name == "activity_grant_save":
            await runtime.tenants.designate(
                actor, chat, data["target"], data["enabled"], data["version"]
            )
            return await ui.render(update, "✅ 指定群管已更新。", back)
        if name == "activity_grant_preview":
            return await ui.render(
                update,
                f"确认{'授权' if data['enabled'] else '撤销'}群管 {data['target']}？\n仅对本群生效；授权对象必须仍是Telegram管理员。",
                [
                    (
                        "确认",
                        {
                            **data,
                            "action": "activity_grant_save",
                            "version": group["version"],
                        },
                    )
                ]
                + back,
            )
        if name == "activity_grant_add":
            store.dialog(
                actor,
                {"form": "activity_grant", "chat": chat, "version": group["version"]},
            )
            return await ui.render(
                update, "发送要授权的Telegram真人管理员数字UID。\n/cancel 取消。", back
            )
        grants = store.db.execute(
            "SELECT uid FROM group_admin_grants WHERE chat=? AND owner=?", (chat, actor)
        ).fetchall()
        buttons = [("➕ 指定群管", {"action": "activity_grant_add", "chat": chat})]
        buttons += [
            (
                f"撤销 {r['uid']}",
                {
                    "action": "activity_grant_preview",
                    "chat": chat,
                    "target": r["uid"],
                    "enabled": False,
                },
            )
            for r in grants
        ]
        return await ui.render(
            update,
            f"👥 {group['title']} · 指定群管\n群主自动可管理；普通管理员不会自动获得权限。",
            buttons + back,
            fold_sections=True,
        )
    if name == "activity_stats_menu":
        return await ui.render(
            update,
            f"📊 {group['title']} · 排行榜与统计\n从开启后开始统计，不回填历史聊天。",
            [
                (
                    "积分排名",
                    {
                        "action": "activity_stats_view",
                        "chat": chat,
                        "kind": "points_rank",
                    },
                ),
                (
                    "今日活跃",
                    {"action": "activity_stats_view", "chat": chat, "kind": "stats"},
                ),
                (
                    "本周活跃",
                    {
                        "action": "activity_stats_view",
                        "chat": chat,
                        "kind": "stats_week",
                    },
                ),
                ("自定义日期", {"action": "activity_stats_custom", "chat": chat}),
            ]
            + back,
        )
    if name == "activity_stats_custom":
        store.dialog(actor, {"form": "activity_stats", "chat": chat, "step": 0})
        return await ui.render(
            update, "发送统计开始日期 YYYY-MM-DD；下一步填写结束日期。", back
        )
    if name == "activity_stats_view":
        text = query_text(runtime, chat, actor, data.get("kind", "stats"), data)
        return await ui.render(
            update,
            html.unescape(re.sub("<[^>]+>", "", text)),
            [
                ("标准统计", {**data, "strict": False}),
                ("严格统计", {**data, "strict": True}),
            ]
            + back,
            fold_sections=True,
        )
    if name == "activity_home":
        store.clear_dialog(actor)
        cfg = service.config(chat)
        rows = store.db.execute(
            "SELECT id,state,config FROM ga_activities WHERE chat=? AND (state<>'draft' OR actor=?) ORDER BY created DESC LIMIT 15",
            (chat, actor),
        ).fetchall()
        buttons = [
            (
                ("✅ 已开启" if cfg["enabled"] else "⏸ 已关闭") + " · 排行榜与活动",
                {
                    "action": "activity_switch",
                    "chat": chat,
                    "enabled": not bool(cfg["enabled"]),
                    "version": cfg["version"],
                },
            ),
            ("➕ 创建活动", {"action": "activity_new", "chat": chat}),
        ]
        buttons += [
            (
                f"{STATES.get(r['state'], r['state'])} · {json.loads(r['config'])['title'] or '未命名草稿'}",
                {"action": "activity_view", "chat": chat, "id": r["id"]},
            )
            for r in rows
        ]
        return await ui.render(
            update,
            f"🎉 {group['title']} · 活动管理\n关闭仅停止新活动与报名，已发布活动仍可审核收尾。\n奖品为文字说明，由群管理员自行兑现。",
            buttons + back,
            fold_sections=True,
        )
    if name == "activity_switch":
        service.configure(actor, chat, data["enabled"], data["version"])
        return await ui.render(
            update,
            "✅ 本群排行榜与活动设置已更新。",
            [("返回活动管理", {"action": "activity_home", "chat": chat})],
        )
    if name == "activity_new":
        identity = service.draft(actor, chat)
        return await wizard(ui, update, identity, 0)
    identity = data.get("id")
    if not identity:
        raise Rejected("操作已失效，请返回本群。")
    row = service.row(identity)
    if row["chat"] != chat:
        raise Rejected("活动不属于当前群。")
    cfg = json.loads(row["config"])
    detail_back = [
        ("返回活动", {"action": "activity_view", "chat": chat, "id": identity})
    ]
    if name == "activity_step":
        if data.get("version") != row["version"]:
            raise Rejected("草稿已更新，请重新打开。")
        if "value" in data:
            step = data["step"]
            field = STEPS[step]
            if field not in {
                "kind",
                "start",
                "duration",
                "preset",
                "minimum",
                "profile",
            }:
                raise Rejected("该步骤请发送一个值。")
            cfg[field] = data["value"]
            service.save_draft(actor, identity, cfg, row["version"])
            return await wizard(ui, update, identity, step + 1)
        return await wizard(ui, update, identity, data["step"])
    if name == "activity_prizes_done":
        return await preview(ui, update, identity)
    if name == "activity_repeat":
        if not cfg["prizes"] or row["version"] != data["version"]:
            raise Rejected("尚无奖品或草稿已变化。")
        store.dialog(
            actor,
            {
                "form": "activity_input",
                "chat": chat,
                "id": identity,
                "step": 9,
                "repeat": True,
                "version": row["version"],
            },
        )
        return await ui.render(
            update, "发送重复上一项奖品的新增名次数量（1—剩余名额）。", detail_back
        )
    if name == "activity_publish":
        service.publish(actor, identity, data["version"])
        store.clear_dialog(actor)
        return await ui.render(
            update,
            "✅ 已提交发布，确认群面板投递后开始计时。\n发送结果不明时不会重复发布，请查看活动状态。",
            detail_back,
        )
    if name == "activity_review":
        target = store.db.execute(
            "SELECT * FROM ga_candidates WHERE activity=? AND uid=?",
            (identity, str(data["target"])),
        ).fetchone()
        if not target:
            raise Rejected("候选人不存在。")
        text = f"🔎 候选第{target['position']}位 · {target['name']}（{target['uid']}）\n有效发言{target['count']}条 · 当前：{target['review']}\n"
        text += (
            f"要求主页展示：{cfg['link']}\n请人工核验当前主页，不代表截止时快照。"
            if cfg["profile"]
            else "无需主页挂链，仍请核实成员资格。"
        )
        buttons = [
            (
                label,
                {
                    "action": "activity_review_preview",
                    "chat": chat,
                    "id": identity,
                    "target": target["uid"],
                    "decision": decision,
                    "version": row["version"],
                },
            )
            for label, decision in (
                ("通过", "pass"),
                ("拒绝", "reject"),
                ("暂缓", "hold"),
            )
        ]
        return await ui.render(update, text, buttons + detail_back, fold_sections=True)
    if name == "activity_review_preview":
        if data["decision"] == "reject":
            store.dialog(actor, {"form": "activity_review", **data, "chat": chat})
            return await ui.render(
                update, "发送拒绝该候选人的原因；下一步确认。", detail_back
            )
        return await ui.render(
            update,
            f"确认将候选 {data['target']} 标记为{'通过' if data['decision'] == 'pass' else '暂缓'}？",
            [("确认", {**data, "action": "activity_review_save", "reason": ""})]
            + detail_back,
        )
    if name == "activity_review_save":
        await service.review(
            actor,
            identity,
            data["target"],
            data["decision"],
            data.get("reason", ""),
            data["version"],
        )
        return await ui.render(update, "✅ 审核已保存，名单未重新抽取。", detail_back)
    if name == "activity_final_preview":
        return await ui.render(
            update,
            "确认按已冻结顺序和审核结果公布？\n前序待审核候选不能跳过；不足的名次标为空缺。\n确认后不再自动重排，奖品需人工兑现。",
            [
                (
                    "确认公布",
                    {
                        **data,
                        "action": "activity_final_save",
                        "version": row["version"],
                    },
                )
            ]
            + detail_back,
        )
    if name == "activity_final_save":
        service.finalize(actor, identity, data["version"])
        return await ui.render(
            update, "✅ 获奖结果已固定，等待新消息公布。", detail_back
        )
    if name in {"activity_cancel", "activity_fulfil"}:
        store.dialog(
            actor, {"form": name, **data, "chat": chat, "version": row["version"]}
        )
        return await ui.render(
            update,
            "发送取消原因。"
            if name == "activity_cancel"
            else "发送本次兑奖状态的说明；下一步确认。",
            detail_back,
        )
    if name == "activity_cancel_save":
        service.cancel(actor, identity, data["reason"], data["version"])
        return await ui.render(update, "✅ 活动已取消，历史记录保留。", detail_back)
    if name == "activity_fulfil_save":
        service.fulfil(
            actor,
            identity,
            data["place"],
            data["state"],
            data["reason"],
            data["version"],
        )
        return await ui.render(
            update, "✅ 已记录人工兑奖状态，未发生转账或积分变更。", detail_back
        )
    if name == "activity_view":
        if row["state"] == "draft":
            return await wizard(ui, update, identity, 0)
        text = f"🎉 {cfg['title']} · {STATES.get(row['state'], row['state'])}\n规则已冻结；奖品由群管理员人工兑现。\n"
        if row["starts"]:
            text += f"开始：{datetime.fromtimestamp(row['starts'], TZ):%Y-%m-%d %H:%M:%S}\n截止：{datetime.fromtimestamp(row['ends'], TZ):%Y-%m-%d %H:%M:%S}\n"
        if row["error"]:
            text += f"⚠️ {row['error']}；先核查群消息，不盲目补发。\n"
        buttons = []
        page = max(0, int(data.get("page", 0)))
        if row["state"] == "review":
            candidates = store.db.execute(
                "SELECT * FROM ga_candidates WHERE activity=? ORDER BY position LIMIT 16 OFFSET ?",
                (identity, page * 15),
            ).fetchall()
            buttons += [
                (
                    f"{r['position']}. {r['name']} · {r['count']}条 · {r['review']}",
                    {
                        "action": "activity_review",
                        "chat": chat,
                        "id": identity,
                        "target": r["uid"],
                    },
                )
                for r in candidates[:15]
            ]
            if page:
                buttons.append(("⬅️ 上页", {**data, "page": page - 1}))
            if len(candidates) > 15:
                buttons.append(("下页 ➡️", {**data, "page": page + 1}))
            buttons.append(
                (
                    "预览并公布",
                    {"action": "activity_final_preview", "chat": chat, "id": identity},
                )
            )
        if row["state"] in {"active", "review"}:
            buttons.append(
                (
                    "取消活动",
                    {"action": "activity_cancel", "chat": chat, "id": identity},
                )
            )
        prizes = store.db.execute(
            "SELECT * FROM ga_results WHERE activity=? ORDER BY place LIMIT 6 OFFSET ?",
            (identity, page * 5),
        ).fetchall()
        for prize in prizes[:5]:
            text += f"\n{prize['place']}. {prize['name']} — {prize['prize']} · {FULFIL[prize['fulfilment']]}"
            if prize["uid"]:
                buttons += [
                    (
                        f"第{prize['place']}名 · {label}",
                        {
                            "action": "activity_fulfil",
                            "chat": chat,
                            "id": identity,
                            "place": prize["place"],
                            "state": state,
                        },
                    )
                    for state, label in FULFIL.items()
                ]
        if prizes:
            if page:
                buttons.append(("⬅️ 上页", {**data, "page": page - 1}))
            if len(prizes) > 5:
                buttons.append(("下页 ➡️", {**data, "page": page + 1}))
        return await ui.render(update, text, buttons + back, fold_sections=True)
    raise Rejected("活动操作已过期，请返回本群。")


async def wizard(ui, update, identity, step):
    """Render one actor-bound field with backward navigation.

    Args:
        ui: Existing renderer.
        update: Private update.
        identity: Draft ID.
        step: Zero-based field.
    """
    service, actor = ui.runtime.activities, str(update.effective_user.id)
    row = service.row(identity)
    if row["actor"] != actor or row["state"] != "draft":
        raise Rejected("草稿不属于你或已发布。")
    cfg, step = json.loads(row["config"]), max(0, min(step, len(STEPS) - 1))
    if step == 8 and not cfg["profile"]:
        step = 9
    field, buttons = STEPS[step], []
    base = {"chat": row["chat"], "id": identity, "version": row["version"]}
    choices = {
        "kind": [("活跃排行奖励", "rank"), ("达标随机抽奖", "random")],
        "start": [("现在开始", 0)],
        "duration": [("1小时", 3600), ("24小时", 86400), ("3天", 259200)],
        "preset": [("标准", "standard"), ("严格", "strict")],
        "minimum": [("默认1条", 1)],
        "profile": [("不要求", False), ("需要人工审核挂链", True)],
    }
    buttons += [
        (label, {**base, "action": "activity_step", "step": step, "value": value})
        for label, value in choices.get(field, [])
    ]
    if field == "prizes":
        buttons += [
            ("奖品填完", {**base, "action": "activity_prizes_done"}),
            ("重复上一项奖品", {**base, "action": "activity_repeat"}),
        ]
    if step:
        previous = 7 if step == 9 and not cfg["profile"] else step - 1
        buttons.append(
            ("上一步", {**base, "action": "activity_step", "step": previous})
        )
    buttons.append(("保存草稿并返回", {"action": "activity_home", "chat": row["chat"]}))
    ui.store.dialog(actor, {"form": "activity_input", **base, "step": step})
    return await ui.render(
        update,
        f"🎉 活动设置 · 第{step + 1}/{len(STEPS)}项\n{PROMPTS[field]}\n"
        + (f"已填{len(cfg['prizes'])}项奖品。\n" if field == "prizes" else "")
        + "/cancel 退出设置，草稿保留。",
        buttons,
        fold_sections=True,
    )


async def preview(ui, update, identity):
    """Display frozen-rule preview before any public send.

    Args:
        ui: Existing renderer.
        update: Private update.
        identity: Draft ID.
    """
    row = ui.runtime.activities.row(identity)
    if row["actor"] != str(update.effective_user.id) or row["state"] != "draft":
        raise Rejected("草稿已失效。")
    cfg = json.loads(row["config"])
    if not cfg["prizes"]:
        raise Rejected("至少添加一项奖品。")
    starts = (
        datetime.fromtimestamp(cfg["start"], TZ).strftime("%Y-%m-%d %H:%M:%S")
        if cfg["start"]
        else "面板确认送达后"
    )
    text = (
        f"🎉 发布预览 · {cfg['title']}\n{cfg['description']}\n"
        f"类型：{'活跃排行奖励' if cfg['kind'] == 'rank' else '达标随机抽奖'}\n"
        f"有效发言：{'标准' if cfg['preset'] == 'standard' else '严格'} · 至少{cfg['minimum']}条\n"
        f"开始：{starts}（北京时间）\n持续{cfg['duration'] // 60}分钟\n"
    )
    text += f"主页要求：{cfg['link']}\n" if cfg["profile"] else "不要求主页挂链。\n"
    text += "\n奖品（人工兑现）\n" + "\n".join(
        f"{i}. {p}" for i, p in enumerate(cfg["prizes"], 1)
    )
    ui.store.clear_dialog(str(update.effective_user.id))
    return await ui.render(
        update,
        text,
        [
            (
                "确认发布",
                {
                    "action": "activity_publish",
                    "chat": row["chat"],
                    "id": identity,
                    "version": row["version"],
                },
            ),
            (
                "返回奖品",
                {
                    "action": "activity_step",
                    "chat": row["chat"],
                    "id": identity,
                    "version": row["version"],
                    "step": 9,
                },
            ),
        ],
        fold_sections=True,
    )


async def input_text(ui, update, dialog):
    """Validate exactly one value and advance a private wizard.

    Args:
        ui: Existing renderer.
        update: Private plain-text message.
        dialog: Actor-bound stored draft.
    """
    text = (update.message.text or "").strip()
    actor, chat = str(update.effective_user.id), dialog["chat"]
    service = ui.runtime.activities
    if dialog["form"] == "activity_grant":
        current = ui.store.db.execute(
            "SELECT version FROM mod_groups WHERE chat=?", (chat,)
        ).fetchone()
        if not current or dialog["version"] != current[0]:
            raise Rejected("群设置或授权已变化，请重新打开指定群管。")
        if not text.isascii() or not text.isdigit() or int(text) <= 0:
            raise Rejected("请只发送一个正整数UID。")
        ui.store.clear_dialog(actor)
        return await action(
            ui,
            update,
            {
                "action": "activity_grant_preview",
                "chat": chat,
                "target": text,
                "enabled": True,
            },
        )
    if dialog["form"] == "activity_stats":
        try:
            value = date.fromisoformat(text)
        except ValueError as exc:
            raise Rejected("日期格式应为 YYYY-MM-DD。") from exc
        if dialog["step"] == 0:
            ui.store.dialog(actor, {**dialog, "step": 1, "start": value.isoformat()})
            return await ui.render(
                update, "发送结束日期 YYYY-MM-DD（包含当天，最多31天）。"
            )
        ui.store.clear_dialog(actor)
        return await action(
            ui,
            update,
            {
                "action": "activity_stats_view",
                "chat": chat,
                "start": dialog["start"],
                "end": value.isoformat(),
                "kind": "stats",
            },
        )
    if dialog["form"] in {"activity_review", "activity_fulfil", "activity_cancel"}:
        if not 1 <= len(text) <= 600:
            raise Rejected("说明需为1—600字。")
        ui.store.clear_dialog(actor)
        save = {
            "activity_review": "activity_review_save",
            "activity_fulfil": "activity_fulfil_save",
            "activity_cancel": "activity_cancel_save",
        }[dialog["form"]]
        return await ui.render(
            update,
            f"确认本次操作？\n说明：{text}",
            [("确认", {**dialog, "action": save, "reason": text})],
        )
    row = service.row(dialog["id"])
    if row["chat"] != chat or row["version"] != dialog["version"]:
        raise Rejected("草稿已变化，请重新打开。")
    cfg, step = json.loads(row["config"]), dialog["step"]
    if text == "上一步":
        previous = 7 if step == 9 and not cfg["profile"] else step - 1
        return await wizard(ui, update, row["id"], previous)
    field = STEPS[step]
    if field in {"title", "description"}:
        value = "" if field == "description" and text == "跳过" else text
        if (
            not (0 if field == "description" else 1)
            <= len(value)
            <= (600 if field == "description" else 60)
        ):
            raise Rejected(PROMPTS[field])
        cfg[field] = value
    elif field == "start":
        if text == "现在":
            cfg[field] = 0
        else:
            try:
                cfg[field] = (
                    datetime.strptime(text, "%Y-%m-%d %H:%M")
                    .replace(tzinfo=TZ)
                    .timestamp()
                )
            except ValueError as exc:
                raise Rejected(PROMPTS[field]) from exc
            if cfg[field] <= service.store.clock():
                raise Rejected("开始时间必须在未来，或发送“现在”。")
    elif field in {"duration", "minimum"}:
        if not text.isascii() or not text.isdigit():
            raise Rejected(PROMPTS[field])
        value = int(text)
        if not (5 <= value <= 43200 if field == "duration" else 1 <= value <= 100000):
            raise Rejected(PROMPTS[field])
        cfg[field] = value * 60 if field == "duration" else value
    elif field == "link":
        parsed = urlparse(text)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "t.me"
            or not parsed.path.strip("/")
            or len(text) > 300
        ):
            raise Rejected("请发送完整 https://t.me/ 群链接。")
        cfg[field] = text
    elif field == "prizes":
        if text == "完成":
            return await preview(ui, update, row["id"])
        if dialog.get("repeat"):
            if (
                not text.isascii()
                or not text.isdigit()
                or not 1 <= int(text) <= 50 - len(cfg["prizes"])
            ):
                raise Rejected("发送1—剩余名额的整数。")
            cfg["prizes"] += [cfg["prizes"][-1]] * int(text)
        else:
            if not 1 <= len(text) <= 200 or len(cfg["prizes"]) >= 50:
                raise Rejected(PROMPTS[field])
            cfg["prizes"].append(text)
    else:
        raise Rejected("此项请点击按钮选择。")
    service.save_draft(actor, row["id"], cfg, row["version"])
    return await wizard(ui, update, row["id"], step if field == "prizes" else step + 1)


def query_text(runtime, chat, uid, kind, options=None):
    """Build escaped group-isolated leaderboard text.

    Args:
        runtime: Plugin runtime.
        chat: Exact group.
        uid: Requesting user.
        kind: points_rank, stats or stats_week.
        options: Optional dates and strict preset.

    Returns:
        Escaped HTML with native expandable details.
    """
    options, service = options or {}, runtime.activities
    if not service.config(chat)["enabled"]:
        raise Rejected("本群排行榜与活动尚未开启，请联系群主或指定群管。")
    if kind == "points_rank":
        rows = runtime.store.db.execute(
            "SELECT w.uid,w.balance AS count,COALESCE(m.name,w.uid) AS name "
            "FROM group_wallets w LEFT JOIN mod_members m ON m.chat=w.chat AND m.uid=w.uid "
            "WHERE w.chat=? ORDER BY w.balance DESC,CAST(w.uid AS INTEGER) LIMIT 20",
            (str(chat),),
        ).fetchall()
        title = "💰 本群积分排名"
        foot = f"我的积分：{runtime.store.balance(uid, chat)}"
        mine = runtime.store.db.execute(
            "SELECT balance FROM group_wallets WHERE chat=? AND uid=?",
            (str(chat), str(uid)),
        ).fetchone()
        position = (
            None
            if mine is None
            else runtime.store.db.execute(
                "SELECT COUNT(*)+1 FROM group_wallets WHERE chat=? "
                "AND (balance>? OR (balance=? AND CAST(uid AS INTEGER)<?))",
                (str(chat), mine["balance"], mine["balance"], int(uid)),
            ).fetchone()[0]
        )
    else:
        if options.get("start"):
            try:
                start, end = (
                    date.fromisoformat(options["start"]),
                    date.fromisoformat(options["end"]) + timedelta(days=1),
                )
            except (ValueError, KeyError) as exc:
                raise Rejected("日期格式应为 YYYY-MM-DD。") from exc
        else:
            start, end = service.period("week" if kind == "stats_week" else "today")
        rows = service.statistics(chat, start, end, strict=options.get("strict", False))
        title = f"📊 本群活跃统计 · {'严格' if options.get('strict') else '标准'}"
        coverage = datetime.fromtimestamp(service.config(chat)["started"], TZ)
        foot = f"范围：{start}—{end - timedelta(days=1)}（北京时间）\n实际采集始于：{coverage:%Y-%m-%d %H:%M}，不回填历史。"
        position = next(
            (i for i, row in enumerate(rows, 1) if row["uid"] == str(uid)), None
        )
    lines = [
        f'{i}. <a href="tg://user?id={row["uid"]}">{html.escape(row["name"])}</a> · {row["count"]}'
        for i, row in enumerate(rows[:20], 1)
    ]
    return f"<b>{title}</b>\n<blockquote expandable>{chr(10).join(lines) or '暂无记录'}</blockquote>\n{html.escape(foot)}\n我的排名：{position or '暂无'}"


async def feedback(runtime, update, text, markup=None):
    """Send one known-ID personal feedback and schedule central retention.

    Args:
        runtime: Plugin runtime.
        update: Group message or button.
        text: Escaped HTML.
        markup: Optional user-bound query controls.
    """
    chat, user = update.effective_chat, update.effective_user
    source = getattr(update, "message", None) or getattr(
        update.callback_query, "message", None
    )
    mention = f'<a href="tg://user?id={user.id}">{html.escape(getattr(user, "full_name", "") or str(user.id))}</a>'
    sent = await runtime.bot.send_message(
        chat_id=chat.id,
        text=mention + "\n" + text,
        parse_mode="HTML",
        reply_to_message_id=source.message_id,
        reply_markup=markup,
    )
    if type(getattr(sent, "message_id", None)) is int:
        runtime.store.db.execute(
            "INSERT OR IGNORE INTO gt_delete(chat,message,source,due) VALUES(?,?,0,?)",
            (
                str(chat.id),
                sent.message_id,
                runtime.store.clock() + retention(runtime, "query"),
            ),
        )


async def public_message(runtime, update):
    """Route public activity commands without exposing management callbacks.

    Args:
        runtime: Plugin runtime.
        update: New group text.

    Returns:
        Whether this subsystem consumed the message.
    """
    message, uid, chat = (
        update.message,
        str(update.effective_user.id),
        str(update.effective_chat.id),
    )
    decision = ownership(
        message,
        runtime.bot.username,
        runtime.bot.id,
        strict=local_config(runtime.store, chat, "command_mode", "compat") == "strict",
    )
    if not decision.accepted:
        return False
    text = (message.text or "").strip()
    kind = classify(text, runtime.bot.username)
    if kind not in {"points_rank", "stats", "stats_week", "lottery"}:
        return False
    try:
        await runtime.activities.member(chat, uid)
        if kind == "lottery":
            row = runtime.store.db.execute(
                "SELECT id FROM ga_activities WHERE chat=? AND state IN ('active','review','closed') ORDER BY created DESC LIMIT 1",
                (chat,),
            ).fetchone()
            if not row:
                raise Rejected("本群暂无活动，请联系群主或指定群管。")
            activity = runtime.activities.row(row["id"])
            pages, _ = runtime.activities.render(row["id"])
            markup = None
            if activity["message"]:
                # Navigation to the authoritative public panel avoids copied
                # enrolment buttons bound to an unrelated feedback message.
                chat_number = str(chat).removeprefix("-100")
                markup = InlineKeyboardMarkup(
                    [
                        [
                            Button(
                                "🎉 打开活动面板",
                                url=f"https://t.me/c/{chat_number}/{activity['message']}",
                            )
                        ]
                    ]
                )
            await feedback(runtime, update, pages[0], markup)
        else:
            options = {}
            parts = text.split()
            if kind in {"stats", "stats_week"} and len(parts) > 1:
                if len(parts) != 3:
                    raise Rejected(
                        "自定义统计：/stats 开始日期 结束日期，例如 /stats 2026-10-01 2026-10-07。"
                    )
                options = {"start": parts[1], "end": parts[2]}
            buttons = []
            if kind.startswith("stats"):
                for label, choice in (("今日", "stats"), ("本周", "stats_week")):
                    for strict in (False, True):
                        token = runtime.store.callback(
                            uid, chat, {"kind": choice, "strict": strict}
                        )
                        buttons.append(
                            Button(
                                label + ("·严格" if strict else "·标准"),
                                callback_data="gq:" + token[3:],
                            )
                        )
            markup = (
                InlineKeyboardMarkup(
                    [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
                )
                if buttons
                else None
            )
            await feedback(
                runtime, update, query_text(runtime, chat, uid, kind, options), markup
            )
    except Rejected as exc:
        await feedback(runtime, update, html.escape(str(exc)))
    return True


async def public_action(runtime, update):
    """Handle public enrolment and actor-bound query callbacks.

    Args:
        runtime: Plugin runtime.
        update: Native callback update.
    """
    query, chat, uid = (
        update.callback_query,
        str(update.effective_chat.id),
        str(update.effective_user.id),
    )
    if update.effective_chat.type != "supergroup" or update.effective_user.is_bot:
        raise Rejected("仅本群真人成员可操作。")
    await runtime.activities.member(chat, uid)
    if query.data.startswith("gq:"):
        data = runtime.store.resolve(query.data[3:], uid, chat)
        await feedback(
            runtime, update, query_text(runtime, chat, uid, data["kind"], data)
        )
        return "已显示统计"
    parts = query.data.split(":")
    if len(parts) != 3:
        raise Rejected("活动按钮无效。")
    row = runtime.activities.row(parts[1])
    if (
        row["chat"] != chat
        or getattr(query.message, "message_id", None) != row["message"]
    ):
        raise Rejected("按钮不属于当前群或原活动面板。")
    author = getattr(query.message, "from_user", None)
    if author and str(author.id) != str(runtime.bot.id):
        raise Rejected("请使用本BOT发布的活动面板。")
    op = parts[2]
    if op in {"join", "leave"}:
        await runtime.activities.enroll(
            row["id"],
            uid,
            getattr(update.effective_user, "full_name", uid),
            leave=op == "leave",
        )
        return (
            "已退出报名" if op == "leave" else "✅ 报名成功，计入活动开始以来的有效发言"
        )
    if op in {"rank", "progress"}:
        rows = (
            runtime.activities.counts(row["id"])
            if row["state"] == "active"
            else runtime.store.db.execute(
                "SELECT * FROM ga_candidates WHERE activity=? ORDER BY position",
                (row["id"],),
            ).fetchall()
        )
        own = next((r for r in rows if r["uid"] == uid), None)
        text = f"我的有效发言：{own['count'] if own else 0}条\n"
        if op == "rank":
            text += (
                "<blockquote expandable>"
                + "\n".join(
                    f"{i}. {html.escape(r['name'])} · {r['count']}条"
                    for i, r in enumerate(rows[:20], 1)
                )
                + "</blockquote>"
            )
        await feedback(runtime, update, text)
        return "已显示"
    if op == "rules":
        cfg = json.loads(row["config"])
        from .rich_text import cards

        text = (
            f"活动规则 · {cfg['title']}\n{cfg['description']}\n奖品由群管理员人工兑现。\n"
            f"类型：{'活跃排行奖励' if cfg['kind'] == 'rank' else '达标随机抽奖'}\n"
            f"开始：{datetime.fromtimestamp(row['starts'], TZ):%Y-%m-%d %H:%M:%S}\n"
            f"截止：{datetime.fromtimestamp(row['ends'], TZ):%Y-%m-%d %H:%M:%S}（北京时间）\n"
            f"计数：{'严格' if cfg['preset'] == 'strict' else '标准'}，最低{cfg['minimum']}条；需点击报名。\n"
            "重复、命令及下注不计；严格另需5个有效字符、3个不同字符，30秒最多一条。\n"
            "排行同分按先达到者、数字UID；随机等概率且不按次数加权，候补顺序不会重抽。\n"
            + (f"主页须挂：{cfg['link']}；管理员人工审核。\n" if cfg["profile"] else "")
            + "\n".join(f"{i}. {p}" for i, p in enumerate(cfg["prizes"], 1))
        )
        for _, formatted in cards(text, fold_sections=True):
            await feedback(runtime, update, formatted)
        return "已显示规则"
    raise Rejected("活动按钮已失效。")
