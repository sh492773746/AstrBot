"""Private, permission-checked semantic review controls."""

import json
from datetime import datetime, timezone

from .group_identity import group_label
from .store import Rejected

NAMES = {
    "normal": "正常",
    "advert": "广告引流",
    "other": "其他违规",
    "uncertain": "不确定",
}
NOTICE = (
    "本群已开启群聊语义复核记录。机器人仅保存其实际收到的新文字和媒体说明"
    "（含编辑版本），用于人工标注与规则试跑；不会自动分析或处罚。"
    "普通正文保留7天，人工选存样本30天。管理员可在「我的群 → 群管"
    " → 广告与刷屏 → 语义检测」暂停或清理。"
)


async def action(ui, update, data, token=""):
    runtime, store = ui.runtime, ui.store
    review = runtime.semantic_review
    uid = str(update.effective_user.id)
    if update.effective_chat.type != "private":
        raise Rejected("请在私聊打开群管")
    name, chat = data["action"], str(data.get("chat", ""))
    owner = store.is_superadmin(uid)
    if name == "semantic_global":
        if not owner:
            raise Rejected("仅首位超管可跨群复核")
        rows = store.db.execute(
            "SELECT c.chat,g.title,c.enabled,c.gap FROM sr_consent c "
            "JOIN mod_groups g ON g.chat=c.chat ORDER BY c.chat LIMIT 60"
        ).fetchall()
        return await ui.render(
            update,
            "语义复核 · 已授权群\n人工复核，不接入自动AI分析。",
            [
                (
                    f"{r['title']} · {'记录中' if r['enabled'] else '已暂停'}",
                    {"action": "semantic_group", "chat": r["chat"]},
                )
                for r in rows
            ]
            + [("采集容量", {"action": "semantic_limits"})]
            + [("返回上一级", {"action": "bot_control"})],
        )
    if name in {"semantic_limits", "semantic_limit_input", "semantic_limit_save"}:
        if not owner:
            raise Rejected("仅首位超管可设置采集容量")
        limits = {
            "group": ("semantic_group_bytes", 20),
            "instance": ("semantic_instance_bytes", 200),
        }
        if name == "semantic_limits":
            return await ui.render(
                update,
                f"每日采集容量 · UTC 日\n每群 {store.get('semantic_group_bytes', 20 * 1048576) // 1048576} MiB"
                f"\n全实例 {store.get('semantic_instance_bytes', 200 * 1048576) // 1048576} MiB"
                "\n超限显示记录缺口，不影响群聊业务。",
                [
                    ("调整每群", {"action": "semantic_limit_input", "field": "group"}),
                    (
                        "调整全实例",
                        {"action": "semantic_limit_input", "field": "instance"},
                    ),
                    ("返回上一级", {"action": "semantic_global"}),
                ],
            )
        field = data.get("field")
        if field not in limits:
            raise Rejected("容量字段无效")
        setting, _ = limits[field]
        if name == "semantic_limit_input":
            store.dialog(uid, {"form": "semantic_limit", "field": field})
            return await ui.render(
                update,
                "发送每日容量 MiB 整数（每群 1—200，全实例 1—1000）。",
                [("返回上一级", {"action": "semantic_limits"})],
            )
        value = data.get("value")
        if type(value) is not int or not 1 <= value <= (
            200 if field == "group" else 1000
        ):
            raise Rejected("容量超出范围")
        if not data.get("confirm"):
            return await ui.render(
                update,
                f"确认将{field}每日容量调整为 {value} MiB？"
                "仅影响后续采集；已超限会显示缺口。",
                [
                    ("确认保存", {**data, "confirm": True}),
                    ("返回上一级", {"action": "semantic_limits"}),
                ],
            )
        with store.tx() as db:
            store.put(db, setting, value * 1048576)
            store.audit(db, uid, "semantic_limit", {"field": field, "mib": value})
        return await action(ui, update, {"action": "semantic_limits"})
    if not chat:
        raise Rejected("请先选择目标群")
    info = None
    if not owner or name in {
        "semantic_group_menu",
        "semantic_home",
        "semantic_notice",
        "semantic_start",
        "semantic_pause",
        "semantic_clear_preview",
        "semantic_clear",
        "semantic_adopt",
        "semantic_undo",
        "semantic_label",
        "semantic_label_input",
    }:
        info = await review.authorize(uid, chat)
    if owner and info is None:
        try:
            info = await runtime.bot.get_chat(chat)
        except Exception:
            pass
    stored_group = store.db.execute(
        "SELECT title FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()
    group_name = group_label(info, stored_group["title"] if stored_group else "")
    row = review.consent(chat)
    version = row["version"] if row else 0
    back = [("返回语义检测", {"action": "semantic_home", "chat": chat})]
    if name == "semantic_group_menu":
        platform_group = not store.db.execute(
            "SELECT 1 FROM tenant_groups WHERE chat=? AND tenant<>'platform'", (chat,)
        ).fetchone()
        group = store.db.execute(
            "SELECT enabled,version FROM mod_groups WHERE chat=?", (chat,)
        ).fetchone()
        exited = store.db.execute(
            "SELECT 1 FROM meta WHERE key=?", ("group_exit:" + chat,)
        ).fetchone()
        restore = [
            ("确认重新接入", {
                "action": "semantic_restore_preview", "chat": chat,
                "version": group["version"],
            })
        ] if platform_group and exited and not group["enabled"] and uid == store.owner else []
        return await ui.render(
            update,
            f"群管 · {group_name}\n"
            + ("🔴 已停用，需原经营群主确认重新接入。\n" if exited and not group["enabled"] else "")
            + "本群现任管理员操作，逐步确认后生效。",
            restore + [
                ("广告与刷屏", {"action": "semantic_ads", "chat": chat}),
                ("入群验证", {"action": "mod_jv_home", "chat": chat}),
                (
                    "欢迎与群规",
                    {"action": "cm_settings", "chat": chat, "section": "content"},
                ),
                ("成员管理", {"action": "mod_group", "chat": chat}),
                ("全群禁言", {"action": "mod_all", "chat": chat}),
                ("举报处理", {"action": "cm_cases", "chat": chat}),
                ("操作记录", {"action": "mod_records", "chat": chat}),
                (
                    "返回上一级",
                    {"action": "tenant_home"}
                    if platform_group
                    else {"action": "tenant_group", "chat": chat},
                ),
            ],
        )
    if name in {"semantic_restore_preview", "semantic_restore_save"}:
        if uid != store.owner:
            raise Rejected("仅原经营群主可确认重新接入")
        actor = await runtime.bot.get_chat_member(chat, int(uid))
        bot = await runtime.bot.get_chat_member(chat, runtime.bot.id)
        if (
            not info or info.type != "supergroup" or actor.status != "creator"
            or bot.status not in {"creator", "administrator"}
        ):
            raise Rejected("群主已变更或机器人不是管理员，请单独核查")
        version = data.get("version")
        if name == "semantic_restore_save":
            store.restore_platform_group(uid, chat, version)
            return await action(ui, update, {"action": "semantic_group_menu", "chat": chat})
        return await ui.render(
            update,
            f"确认重新接入 · {group_name}\n"
            "只恢复基础群服务；语义记录、定时任务、广告队列保持停用或待人工核查。"
            "不会重新发布此前待投递的订单。",
            [
                ("确认恢复", {
                    "action": "semantic_restore_save", "chat": chat, "version": version,
                }),
                ("返回上一级", {"action": "semantic_group_menu", "chat": chat}),
            ],
        )
    if name == "semantic_ads":
        return await ui.render(
            update,
            f"广告与刷屏 · {group_name}",
            [
                ("执行开关", {"action": "mod_ak_home", "chat": chat}),
                ("检测规则与命中记录", {"action": "mod_ak_rules", "chat": chat}),
                ("语义检测", {"action": "semantic_home", "chat": chat}),
                ("返回上一级", {"action": "semantic_group_menu", "chat": chat}),
            ],
        )
    if name in {"semantic_home", "semantic_group"}:
        text = (
            "语义检测 · 人工复核模式，未启用自动AI分析\n"
            f"{group_name} · {'🟢 记录中' if row and row['enabled'] else '🔴 已暂停'}\n"
            "范围：机器人实际收到的超级群新文字／媒体说明／编辑；"
            "不抓历史或私聊，不下载媒体。\n"
            "普通正文7天、选存样本30天；分析包24小时。\n"
            f"记录缺口：{row['gap'] if row and row['gap'] else '无'}"
        )
        buttons = [
            (
                "暂停记录" if row and row["enabled"] else "开启记录",
                {
                    "action": "semantic_pause"
                    if row and row["enabled"]
                    else "semantic_notice",
                    "chat": chat,
                },
            ),
            ("本群复核样本", {"action": "semantic_list", "chat": chat}),
            ("待确认规则", {"action": "semantic_drafts", "chat": chat}),
            (
                "清理本群全部复核数据",
                {"action": "semantic_clear_preview", "chat": chat},
            ),
        ]
        if owner:
            buttons += [
                ("生成分析包", {"action": "semantic_package_preview", "chat": chat}),
            ]
        buttons.append(("返回上一级", {"action": "semantic_ads", "chat": chat}))
        return await ui.render(update, text, buttons)
    if name == "semantic_notice":
        return await ui.render(
            update,
            "授权记录 · 范围及隐私说明\n"
            + NOTICE
            + "\n确认后先向群内发送告知，明确送达后才开始记录；"
            "送达结果不明时不会采集。请确保群成员知情。",
            [
                (
                    "确认并向群内告知",
                    {"action": "semantic_start", "chat": chat, "version": version},
                )
            ]
            + back,
        )
    if name == "semantic_start":
        if version != data.get("version"):
            raise Rejected("授权状态已变化，请重新确认")
        if row and row["enabled"]:
            raise Rejected("该群已开启")
        try:
            await runtime.bot.send_message(chat_id=chat, text=NOTICE)
        except Exception as exc:
            raise Rejected(
                "群内告知未确认送达，未开始记录；请核对群消息后重试"
            ) from exc
        await review.authorize(uid, chat)
        review.start(chat, uid, version)
        return await action(ui, update, {"action": "semantic_home", "chat": chat})
    if name == "semantic_pause":
        review.pause(chat, "manual")
        with store.tx() as db:
            store.audit(db, uid, "semantic_pause", {"chat": chat})
        return await action(ui, update, {"action": "semantic_home", "chat": chat})
    if name == "semantic_clear_preview":
        return await ui.render(
            update,
            "确认清理？将立即暂停采集，删除本群复核正文、标注、上下文、分析包和未发布草稿；无法恢复。",
            [
                (
                    "确认清理",
                    {"action": "semantic_clear", "chat": chat, "version": version},
                )
            ]
            + back,
        )
    if name == "semantic_clear":
        review.clear(chat, uid, data.get("version"))
        return await action(ui, update, {"action": "semantic_home", "chat": chat})
    if name == "semantic_list":
        day, label = data.get("day"), data.get("label")
        rows = review.rows(chat, day, label)
        text = (
            f"{group_name} · 复核样本\n{day or '最近'} · {NAMES.get(label, '全部')}\n"
        )
        buttons = [
            (
                f"#{r['message']} · {NAMES.get(r['label'], '未标注')} · "
                f"{r['body'][:28].replace(chr(10), ' ')}",
                {
                    "action": "semantic_message",
                    "chat": chat,
                    "message": r["message"],
                    "revision": r["revision"],
                },
            )
            for r in rows
        ]
        buttons += [
            (
                title,
                {"action": "semantic_list", "chat": chat, "label": value, "day": day},
            )
            for value, title in NAMES.items()
        ]
        buttons.append(("按日期筛选", {"action": "semantic_date", "chat": chat}))
        if owner:
            buttons.append(
                (
                    "预览本次筛选分析包",
                    {
                        "action": "semantic_package_preview",
                        "chat": chat,
                        "day": day,
                        "label": label,
                    },
                )
            )
        return await ui.render(
            update, text + ("无样本" if not rows else ""), buttons + back
        )
    if name == "semantic_date":
        store.dialog(
            uid, {"form": "semantic_date", "chat": chat, "label": data.get("label")}
        )
        return await ui.render(update, "发送 UTC 日期 YYYY-MM-DD", back)
    if name == "semantic_message":
        item = store.db.execute(
            "SELECT m.*,l.label,l.reason FROM sr_messages m LEFT JOIN sr_labels l "
            "ON m.chat=l.chat AND m.message=l.message AND m.revision=l.revision "
            "WHERE m.chat=? AND m.message=? AND m.revision=?",
            (chat, data["message"], data["revision"]),
        ).fetchone()
        if not item:
            raise Rejected("样本已到期或被清理")
        context = store.db.execute(
            "SELECT message,body FROM sr_messages WHERE chat=? AND "
            "((message BETWEEN ? AND ?) OR message=?) ORDER BY message LIMIT 7",
            (chat, item["message"] - 2, item["message"] + 2, item["reply"] or -1),
        ).fetchall()
        with store.tx() as db:
            store.audit(
                db,
                uid,
                "semantic_view",
                {
                    "chat": chat,
                    "message": item["message"],
                    "revision": item["revision"],
                },
            )
        text = f"样本 #{item['message']} · 版本 {item['revision']}\n"
        text += f"身份：{item['sender_kind']} · 发送者 {item['actor']}\n"
        text += f"标注：{NAMES.get(item['label'], '未标注')}\n"
        text += "\n".join(f"#{r['message']}: {r['body'][:600]}" for r in context)
        buttons = [
            (
                title,
                {
                    "action": "semantic_label_input",
                    "chat": chat,
                    "message": item["message"],
                    "revision": item["revision"],
                    "label": label,
                },
            )
            for label, title in NAMES.items()
        ]
        return await ui.render(
            update,
            text[:3900],
            buttons + [("返回样本", {"action": "semantic_list", "chat": chat})],
        )
    if name == "semantic_label_input":
        if data.get("label") not in NAMES:
            raise Rejected("标注无效")
        store.dialog(uid, {"form": "semantic_reason", **data})
        return await ui.render(
            update,
            f"标注为{NAMES[data['label']]}。请发送理由（最多500字），"
            "无补充则发送“无”。",
            back,
        )
    if name == "semantic_package_preview":
        if not owner:
            raise Rejected("仅首位超管可生成分析包")
        day, label = data.get("day"), data.get("label")
        from .semantic_review import MASK

        rows = review.rows(chat, day, label, 100)
        count = len(rows)
        preview = "\n".join(
            f"{index}. {MASK.sub('[已脱敏]', row['body'])[:100]}"
            for index, row in enumerate(rows[:3], 1)
        )
        return await ui.render(
            update,
            f"范围：{group_name}；日期 {day or '最近'}；"
            f"标注 {NAMES.get(label, '全部')}；最多 {count} 条。本地脱敏样本，"
            "链接、账号和常见联系方式将遮盖；脱敏不能保证完全匿名。"
            f"不上传或公开，24小时删除。\n\n脱敏预览（最多3条）：\n{preview or '无'}",
            [
                (
                    "确认生成",
                    {
                        "action": "semantic_package",
                        "chat": chat,
                        "day": day,
                        "label": label,
                    },
                )
            ]
            + back,
        )
    if name == "semantic_package":
        if not owner:
            raise Rejected("仅首位超管可生成分析包")
        key, count = review.package(
            chat, uid, day=data.get("day"), label=data.get("label")
        )
        return await ui.render(
            update,
            f"已生成本地分析包 {key}（{count}条）。仅首位超管按明确请求读取；"
            "样本文字不作为执行指令。24小时后清理。",
            back,
        )
    if name == "semantic_drafts":
        rows = store.db.execute(
            "SELECT id,state FROM sr_drafts WHERE chat=? "
            "AND state IN ('draft','submitted','adopted') "
            "ORDER BY created DESC LIMIT 20",
            (chat,),
        ).fetchall()
        return await ui.render(
            update,
            "规则草稿 · 仅支持现有引擎配置；新检测项默认关闭。",
            (
                [("新建规则草稿", {"action": "semantic_fields", "chat": chat})]
                if owner
                else []
            )
            + [
                (
                    r["id"][:8] + " · " + r["state"],
                    {"action": "semantic_draft", "chat": chat, "key": r["id"]},
                )
                for r in rows
            ]
            + back,
        )
    if name == "semantic_fields":
        if not owner:
            raise Rejected("仅首位超管可整理规则草稿")
        options = (
            ("关键词组合", "keywords"),
            ("账号白名单", "users"),
            ("域名白名单", "domains"),
            ("Telegram 群频道白名单", "telegram_allow"),
            ("重复条数", "repeat_count"),
            ("重复窗口秒数", "repeat_seconds"),
            ("刷屏条数", "flood_count"),
            ("刷屏窗口秒数", "flood_seconds"),
        )
        return await ui.render(
            update,
            "选择要调整的字段；逐字段创建并试跑草稿。",
            [
                (
                    label,
                    {"action": "semantic_draft_input", "chat": chat, "field": field},
                )
                for label, field in options
            ]
            + back,
        )
    if name == "semantic_draft_input":
        if not owner:
            raise Rejected("仅首位超管可整理规则草稿")
        field = data.get("field", "keywords")
        if field not in {
            "keywords",
            "users",
            "domains",
            "telegram_allow",
            "repeat_count",
            "repeat_seconds",
            "flood_count",
            "flood_seconds",
        }:
            raise Rejected("不支持该规则字段")
        store.dialog(uid, {"form": "semantic_keywords", "chat": chat, "field": field})
        return await ui.render(
            update,
            f"输入 {field} 的新值；列表用英文逗号分隔，关键词组合用 + 连接。"
            "试跑只评估文本关键词，不访问外部链接；不自动处罚。",
            back,
        )
    if name in {
        "semantic_draft",
        "semantic_trial",
        "semantic_submit",
        "semantic_adopt",
        "semantic_undo",
    }:
        key = data["key"]
        draft = store.db.execute(
            "SELECT * FROM sr_drafts WHERE id=? AND chat=?", (key, chat)
        ).fetchone()
        if not draft:
            raise Rejected("草稿已撤销或目标群不符")
        if name in {"semantic_trial", "semantic_submit"}:
            if not owner:
                raise Rejected("仅首位超管可试跑和提交草稿")
        if name == "semantic_trial":
            review.trial(key, uid)
        if name == "semantic_submit":
            review.submit(key, uid)
        if name == "semantic_adopt":
            if draft["state"] != "submitted":
                raise Rejected("仅可采用已提交草稿")
            if not data.get("confirm"):
                current = runtime.ad_killer.policy(chat)
                before = json.loads(current["config"])
                after = json.loads(draft["config"])
                fields = (
                    "keywords",
                    "users",
                    "domains",
                    "telegram_allow",
                    "repeat_count",
                    "repeat_seconds",
                    "flood_count",
                    "flood_seconds",
                )
                diff = "\n".join(
                    f"{field}：{before[field]} → {after[field]}"
                    for field in fields
                    if before[field] != after[field]
                )
                latest = store.db.execute(
                    "SELECT result FROM sr_trials WHERE draft=? ORDER BY at DESC LIMIT 1",
                    (key,),
                ).fetchone()
                trial_result = json.loads(latest["result"]) if latest else {}
                return await ui.render(
                    update,
                    f"采用规则草稿\n{diff or '无字段变更'}"
                    f"\n最近试跑：命中{trial_result.get('hit', 0)}，"
                    f"误判{trial_result.get('false_positive', 0)}，"
                    f"漏判{trial_result.get('missed', 0)}，"
                    f"不确定{trial_result.get('uncertain', 0)}。"
                    "\n已有检测项开启时，修改立即影响检测；处罚等级不变。"
                    "请核对试跑结果和群内规则。",
                    [("确认采用", {**data, "confirm": True})] + back,
                )
            await review.adopt(key, uid)
        if name == "semantic_undo":
            if not data.get("confirm"):
                return await ui.render(
                    update,
                    "仅撤回本次规则配置变更；后续修改发生后不能覆盖，不撤销历史处罚。",
                    [("确认撤回", {**data, "confirm": True})] + back,
                )
            await review.undo(key, uid)
        draft = store.db.execute(
            "SELECT * FROM sr_drafts WHERE id=?", (key,)
        ).fetchone()
        trial = store.db.execute(
            "SELECT result FROM sr_trials WHERE draft=? ORDER BY at DESC LIMIT 1",
            (key,),
        ).fetchone()
        result = json.loads(trial["result"]) if trial else {}
        buttons = []
        if owner and draft["state"] in {"draft", "submitted"}:
            buttons.append(
                ("试跑", {"action": "semantic_trial", "chat": chat, "key": key})
            )
        if owner and draft["state"] == "draft" and trial:
            buttons.append(
                (
                    "提交本群确认",
                    {"action": "semantic_submit", "chat": chat, "key": key},
                )
            )
        if draft["state"] == "submitted":
            buttons.append(
                ("确认采用", {"action": "semantic_adopt", "chat": chat, "key": key})
            )
        if draft["state"] == "adopted":
            buttons.append(
                ("撤回本次变更", {"action": "semantic_undo", "chat": chat, "key": key})
            )
        return await ui.render(
            update,
            f"草稿 {key[:8]} · {draft['state']}\n关键词："
            f"{json.loads(draft['config']).get('keywords', [])}\n"
            f"试跑：命中{result.get('hit', 0)}，正常误判{result.get('false_positive', 0)}，"
            f"标注广告漏判{result.get('missed', 0)}，不确定{result.get('uncertain', 0)}；"
            f"证据消息 {result.get('evidence', [])}\n"
            "未标注样本不计算准确率，链接和其他离线不可核实条件不参与本次试跑。",
            buttons + [("返回草稿", {"action": "semantic_drafts", "chat": chat})],
        )
    raise Rejected("复核入口已失效，请重新打开")


async def input_text(ui, update, dialog):
    uid = str(update.effective_user.id)
    form = dialog["form"]
    review = ui.runtime.semantic_review
    if form == "semantic_limit":
        if not ui.store.is_superadmin(uid):
            raise Rejected("仅首位超管可调整容量")
        text = (update.message.text or "").strip()
        if not text.isascii() or not text.isdigit():
            raise Rejected("请填写整数 MiB")
        ui.store.clear_dialog(uid)
        return await action(
            ui,
            update,
            {
                "action": "semantic_limit_save",
                "field": dialog["field"],
                "value": int(text),
            },
        )
    chat = str(dialog["chat"])
    if form == "semantic_keywords" and not ui.store.is_superadmin(uid):
        raise Rejected("仅首位超管可新建草稿")
    if form == "semantic_reason":
        await review.authorize(uid, chat)
    text = (update.message.text or "").strip()
    if form == "semantic_reason":
        review.label(
            chat, dialog["message"], dialog["revision"], dialog["label"], text, uid
        )
        ui.store.clear_dialog(uid)
        return await action(
            ui,
            update,
            {
                "action": "semantic_message",
                "chat": chat,
                "message": dialog["message"],
                "revision": dialog["revision"],
            },
        )
    if form == "semantic_date":
        if not ui.store.is_superadmin(uid):
            await review.authorize(uid, chat)
        try:
            datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise Rejected("日期格式应为 YYYY-MM-DD") from exc
        ui.store.clear_dialog(uid)
        return await action(
            ui,
            update,
            {
                "action": "semantic_list",
                "chat": chat,
                "day": text,
                "label": dialog.get("label"),
            },
        )
    key = review.draft(chat, uid, text, dialog.get("field", "keywords"))
    ui.store.clear_dialog(uid)
    return await action(
        ui, update, {"action": "semantic_draft", "chat": chat, "key": key}
    )
