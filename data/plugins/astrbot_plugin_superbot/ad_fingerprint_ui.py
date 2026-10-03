"""Versioned sample approval and per-group observation controls."""

import asyncio

from .store import Rejected


async def action(mod_ui, update, payload):
    """Handle private authenticated controls; group authorization is never implicit.

    Args:
        mod_ui: Existing moderation UI.
        update: Private Telegram callback update.
        payload: Server-stored callback payload.
    """
    ui, runtime, store = mod_ui.ui, mod_ui.runtime, mod_ui.store
    if update.effective_chat.type != "private":
        raise Rejected("请在私聊管理样本")
    uid, chat = str(update.effective_user.id), str(payload["chat"])
    await runtime.moderation.check(uid, chat, "delete")
    engine = runtime.fingerprints
    op = payload["action"]
    home = [("返回样本识别", {"action": "mod_ak_fp", "chat": chat})]
    group = store.db.execute("SELECT * FROM fp_groups WHERE chat=?", (chat,)).fetchone()
    version = group["version"] if group else 0
    if op in {"mod_ak_fp_rollout", "mod_ak_fp_rollout_save"}:
        store.require(uid, "manager")
        if op == "mod_ak_fp_rollout":
            return await ui.render(
                update,
                "🌐 全局观察推广\n对当前广告杀手已启用且无故障、尚未配置样本识别的群建立观察快照。\n"
                "需要完成离线与测试群验收；不覆盖已有人为设置的群。",
                [
                    (
                        "确认建立观察快照",
                        {"action": "mod_ak_fp_rollout_save", "chat": chat},
                    )
                ]
                + home,
            )
        result = await engine.begin_rollout(uid)
        return await ui.render(
            update,
            f"观察群：{len(result['observing'])}\n跳过群：{len(result['skipped'])}\n"
            "观察满24小时且30条无误判复核后，后台再次核验再自动启用。",
            home,
        )
    if op in {"mod_ak_fp_restore", "mod_ak_fp_restore_save"}:
        await runtime.moderation.check(uid, chat, "mute")
        if op == "mod_ak_fp_restore":
            return await ui.render(
                update,
                "确认恢复此次样本识别禁言？\n只恢复本次且未被后续操作修改的限制；消息无法恢复。",
                [("确认恢复", {**payload, "action": "mod_ak_fp_restore_save"})] + home,
            )
        await engine.restore(uid, chat, payload["message"])
        return await ui.render(update, "已恢复此次禁言。", home)
    if op in {"mod_ak_fp_mode", "mod_ak_fp_mode_save"}:
        mode = payload.get("mode")
        if mode not in {"off", "observe", "auto"}:
            raise Rejected("无效模式")
        if op == "mod_ak_fp_mode":
            return await ui.render(
                update,
                f"🛡 本群模式调整\n目标：{mode}\n全局批准样本将在本群参与识别。\n"
                "观察不处罚；自动模式需验收。删除无法恢复；第3次有效命中禁言10分钟。",
                [
                    (
                        "确认",
                        {
                            **payload,
                            "action": "mod_ak_fp_mode_save",
                            "version": version,
                        },
                    )
                ]
                + home,
            )
        if mode == "auto":
            await runtime.moderation.check(uid, chat, "mute")
        async with runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
            with store.tx() as db:
                fresh = db.execute(
                    "SELECT * FROM fp_groups WHERE chat=?", (chat,)
                ).fetchone()
                if payload.get("version") != (fresh["version"] if fresh else 0):
                    raise Rejected("配置已变化")
                if mode == "auto" and not engine.ready(chat):
                    raise Rejected(
                        "需要离线、测试群验收及本群24小时观察、30条无误判复核"
                    )
                db.execute(
                    """INSERT INTO fp_groups(chat,mode,actor,since) VALUES(?,?,?,?)
                    ON CONFLICT(chat) DO UPDATE SET mode=excluded.mode,actor=excluded.actor,
                    version=fp_groups.version+1,rollout=0,
                    since=CASE WHEN fp_groups.mode='off' THEN excluded.since ELSE fp_groups.since END""",
                    (chat, mode, uid, store.clock()),
                )
                store.audit(db, uid, "fp_mode", {"chat": chat, "mode": mode})
        return await action(mod_ui, update, {"action": "mod_ak_fp", "chat": chat})
    if op == "mod_ak_fp_from_hit":
        hit = store.db.execute(
            "SELECT * FROM ak_hits WHERE chat=? AND message=?",
            (chat, payload["message"]),
        ).fetchone()
        if not hit or not hit["body"] or hit["false_positive"]:
            raise Rejected("原记录不可用，请回复仍存在的原消息提交")
        identity = await engine.submit(uid, chat, hit["message"], hit["body"])
        return await ui.render(update, f"样本 #{identity} 已提交，待全局审批。", home)
    if op.startswith("mod_ak_fp_sample") or op == "mod_ak_fp_library":
        store.require(uid, "manager")
        if op == "mod_ak_fp_library":
            page = max(0, min(int(payload.get("page", 0)), 10000))
            rows = store.db.execute(
                "SELECT * FROM fp_samples ORDER BY id DESC LIMIT 11 OFFSET ?",
                (page * 10,),
            ).fetchall()
            buttons = [
                (
                    f"样本 #{r['id']}",
                    {"action": "mod_ak_fp_sample", "chat": chat, "id": r["id"]},
                )
                for r in rows[:10]
            ]
            if page:
                buttons.append(
                    ("上一页", {"action": op, "chat": chat, "page": page - 1})
                )
            if len(rows) > 10:
                buttons.append(
                    ("下一页", {"action": op, "chat": chat, "page": page + 1})
                )
            return await ui.render(
                update,
                "🌐 全局广告样本库\n仅批准样本参与所有启用群识别。\n\n"
                + (
                    "\n".join(f"#{r['id']} · {r['status']}" for r in rows[:10])
                    or "暂无样本"
                ),
                buttons + home,
            )
        row = store.db.execute(
            "SELECT * FROM fp_samples WHERE id=?", (payload["id"],)
        ).fetchone()
        if not row:
            raise Rejected("样本不存在")
        if op == "mod_ak_fp_sample_preview":
            return await ui.render(
                update,
                f"确认全局操作：{payload['target']}\n样本 #{row['id']}\n影响所有启用群。\n\n"
                + row["body"],
                [
                    (
                        "确认全局变更",
                        {
                            **payload,
                            "action": "mod_ak_fp_sample_save",
                            "version": row["version"],
                        },
                    )
                ]
                + home,
            )
        if op == "mod_ak_fp_sample_save":
            engine.decide(uid, row["id"], payload["version"], payload["target"])
            return await action(
                mod_ui, update, {"action": "mod_ak_fp_library", "chat": chat}
            )
        exceptions = store.db.execute(
            "SELECT count(*) FROM fp_exceptions WHERE sample=?", (row["id"],)
        ).fetchone()[0]
        return await ui.render(
            update,
            f"🧾 样本 #{row['id']}\n状态：{row['status']}\n来源群：{row['source_chat']}\n"
            f"本群停用反馈：{exceptions} 群\n有效期至时间戳：{int(row['expires'])}\n\n"
            f"📋 脱敏正文\n{row['body'] or '正文已删除'}\n\n批准后全局共享；到期不自动续期。",
            [
                (
                    label,
                    {
                        "action": "mod_ak_fp_sample_preview",
                        "chat": chat,
                        "id": row["id"],
                        "target": target,
                    },
                )
                for label, target in [
                    ("批准启用", "active"),
                    ("暂停", "paused"),
                    ("撤销", "revoked"),
                    ("删除正文", "deleted"),
                ]
            ]
            + home,
        )
    if op in {"mod_ak_fp_review", "mod_ak_fp_review_save"}:
        row = store.db.execute(
            "SELECT * FROM fp_events WHERE chat=? AND message=?",
            (chat, payload["message"]),
        ).fetchone()
        if not row:
            raise Rejected("记录不存在")
        if op == "mod_ak_fp_review_save":
            if payload.get("correct") is False:
                engine.false_positive(uid, chat, row["message"])
            elif (
                payload.get("correct") is True
                and row["status"] == "candidate"
                and row["body"]
            ):
                with store.tx() as db:
                    db.execute(
                        "UPDATE fp_events SET reviewed=1 WHERE chat=? AND message=? AND reviewed=0",
                        (chat, row["message"]),
                    )
                    store.audit(
                        db,
                        uid,
                        "fp_review",
                        {"chat": chat, "message": row["message"], "correct": True},
                    )
            else:
                raise Rejected("缺少可核查依据，不能计入验收")
            return await action(mod_ui, update, {"action": "mod_ak_fp", "chat": chat})
        link = (
            f"https://t.me/c/{chat[4:]}/{row['message']}"
            if chat.startswith("-100")
            else "无可用定位"
        )
        return await ui.render(
            update,
            f"📋 消息 {row['message']}\n原因：{row['reason']}\n相似度：{row['score']:.3f}\n"
            f"状态：{row['status']}\n原消息：{link}\n原消息可能已删除或无访问权限。\n\n"
            f"{row['body'] or '脱敏正文已过期'}\n\n请核查实际语境；不确定请返回，不计入正确复核。",
            [
                (
                    label,
                    {**payload, "action": "mod_ak_fp_review_save", "correct": correct},
                )
                for label, correct in [
                    ("确认广告", True),
                    ("误判／本群停用样本", False),
                ]
            ]
            + (
                [
                    (
                        "恢复此次禁言",
                        {
                            "action": "mod_ak_fp_restore",
                            "chat": chat,
                            "message": row["message"],
                        },
                    )
                ]
                if store.db.execute(
                    "SELECT 1 FROM fp_restrictions WHERE chat=? AND message=? AND status='active'",
                    (chat, row["message"]),
                ).fetchone()
                else []
            )
            + home,
        )
    rows = store.db.execute(
        "SELECT * FROM fp_events WHERE chat=? ORDER BY at DESC LIMIT 10", (chat,)
    ).fetchall()
    buttons = [
        (label, {"action": "mod_ak_fp_mode", "chat": chat, "mode": mode})
        for label, mode in [("关闭", "off"), ("观察", "observe"), ("自动", "auto")]
    ]
    if store.allowed(uid, "manager"):
        buttons.append(("全局样本审批", {"action": "mod_ak_fp_library", "chat": chat}))
        buttons.append(("推广到已有群", {"action": "mod_ak_fp_rollout", "chat": chat}))
    buttons.extend(
        (
            f"查看消息 {r['message']}",
            {"action": "mod_ak_fp_review", "chat": chat, "message": r["message"]},
        )
        for r in rows
    )
    return await ui.render(
        update,
        f"🛡 样本与行为识别\n本群模式：{group['mode'] if group else 'off'}\n"
        "无模型、无外部识别费用。全局批准样本共享，本群白名单和例外优先。\n"
        "回复原消息 /admark 提交待审样本；样本保存90天，疑似正文7天。\n\n"
        "📋 近期结果\n"
        + (
            "\n".join(f"{r['message']} · {r['reason']} · {r['status']}" for r in rows)
            or "暂无记录"
        ),
        buttons + [("返回广告杀手", {"action": "mod_ak_home", "chat": chat})],
    )
