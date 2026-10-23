"""Private, authorized review of group-game delivery and missing draw evidence."""

from .store import Rejected


async def action(ui, update, payload, token):
    """Review delivery without changing orders, balances or draw results.

    Args:
        ui: Existing private UI.
        update: Authenticated Telegram update.
        payload: Server-stored callback payload.
        token: Single-use confirmation token.
    """
    store = ui.store
    uid = str(update.effective_user.id)
    store.require(uid, "game")
    op = payload["action"]
    back = [("返回异常记录", {"action": "game_delivery"})]
    if op == "game_delivery":
        page = max(0, int(payload.get("page", 0)))
        jobs = store.db.execute(
            "SELECT * FROM gg_dispatch WHERE status IN ('review','blocked') ORDER BY rowid DESC LIMIT 8 OFFSET ?",
            (page * 8,),
        ).fetchall()
        countdowns = store.db.execute(
            "SELECT chat,issue,error FROM gg_round_notices WHERE status='review' ORDER BY closes DESC LIMIT 8"
        ).fetchall()
        broadcasts = store.db.execute(
            "SELECT r.chat,r.issue,r.error,d.message FROM gb_rounds r LEFT JOIN gg_dispatch d "
            "ON d.chat=r.chat AND d.issue=r.issue AND d.kind='round_open' "
            "WHERE r.edit_state='review' ORDER BY r.issue DESC LIMIT 8"
        ).fetchall()
        missing = store.db.execute(
            "SELECT issue,status FROM game_backfill WHERE status<>'complete' ORDER BY issue LIMIT 10"
        ).fetchall()
        cleanup = store.db.execute(
            "SELECT d.chat,d.issue,c.status,c.error,c.attempts FROM gb_cleanup c JOIN gg_dispatch d ON d.id=c.job "
            "WHERE c.status IN ('review','blocked') ORDER BY d.rowid DESC LIMIT 8"
        ).fetchall()
        feedback = store.db.execute(
            "SELECT chat,source,status,error FROM gt_requests WHERE status IN ('review','blocked') "
            "ORDER BY rowid DESC LIMIT 8"
        ).fetchall()
        short_cleanup = store.db.execute(
            "SELECT chat,message,status,error,attempts FROM gt_delete "
            "WHERE status IN ('review','blocked') OR (status='pending' AND attempts>0) "
            "ORDER BY rowid DESC LIMIT 8"
        ).fetchall()
        return await ui.render(
            update,
            f"投递与历史公告异常 · 第{page + 1}页\n未知结果必须先核查群内实际消息，不能据此重复下注或结算。\n"
            + "\n".join(
                f"下注反馈：群{r['chat']} 原消息{r['source']} · {r['status']} · {r['error']}；不自动重发。"
                for r in feedback
            )
            + "\n"
            + "\n".join(
                f"反馈撤回：群{r['chat']} 消息{r['message']} · {r['status']} · {r['error']} · 已尝试{r['attempts']}次"
                for r in short_cleanup
            )
            + "\n"
            + "\n".join(
                f"倒计时：群{r['chat']} 第{r['issue']}期 · {r['error']}"
                for r in countdowns
            )
            + "\n"
            + "\n".join(
                f"旧公告清理：群{r['chat']} 第{r['issue']}期 · {'待核查' if r['status'] == 'review' else '删除被拒绝'} · {r['error']} · 已尝试{r['attempts']}次；不影响开奖结算。"
                for r in cleanup
            )
            + "\n"
            + "\n".join(
                f"连续播报：群{r['chat']} 第{r['issue']}期 · 消息{r['message'] or '未知'} · 待核查（{r['error']}）"
                for r in broadcasts
            )
            + "\n"
            + "\n".join(f"缺期：{r['issue']} · 等待可靠开奖证据" for r in missing),
            [
                (
                    f"{'受理' if r['kind'] == 'accepted' else '结算'} · 群{r['chat']} · {r['status']}",
                    {"action": "game_notice_view", "id": r["id"]},
                )
                for r in jobs
            ]
            + [
                (
                    f"停止第{r['issue']}期异常提醒",
                    {
                        "action": "game_countdown_stop_view",
                        "chat": r["chat"],
                        "issue": r["issue"],
                    },
                )
                for r in countdowns
            ]
            + [
                (
                    f"停止第{r['issue']}期异常编辑",
                    {
                        "action": "game_countdown_stop_view",
                        "chat": r["chat"],
                        "issue": r["issue"],
                        "broadcast": True,
                    },
                )
                for r in broadcasts
            ]
            + [
                ("上一页", {"action": "game_delivery", "page": max(0, page - 1)}),
                ("下一页", {"action": "game_delivery", "page": page + 1}),
                ("返回管理", {"action": "admin_game"}),
            ],
        )
    if op.startswith("game_countdown_stop"):
        if op.endswith("_view"):
            return await ui.render(
                update,
                "只停止这期异常提醒，不改订单或积分。确认前请核对群和期号：\n"
                f"{payload['chat']} · {payload['issue']}",
                [("确认停止", {**payload, "action": "game_countdown_stop"})] + back,
            )
        with store.tx() as db:
            if not db.execute(
                "UPDATE callbacks SET used=1 WHERE token=? AND uid=? AND chat=? AND used=0 AND expires>?",
                (token, uid, str(update.effective_chat.id), store.clock()),
            ).rowcount:
                raise Rejected("确认已使用或过期")
            if payload.get("broadcast"):
                db.execute(
                    "UPDATE gb_rounds SET edit_state='closed',revision=revision+1 WHERE chat=? AND issue=? AND edit_state='review'",
                    (payload["chat"], payload["issue"]),
                )
            else:
                db.execute(
                    "UPDATE gg_round_notices SET status='stopped' WHERE chat=? AND issue=? AND status='review'",
                    (payload["chat"], payload["issue"]),
                )
            store.audit(db, uid, "game_countdown_stop", payload)
        return await ui.render(update, "异常提醒已停止，订单和积分不变。", back)
    row = store.db.execute(
        "SELECT * FROM gg_dispatch WHERE id=?", (payload["id"],)
    ).fetchone()
    if not row or row["status"] not in {"review", "blocked"}:
        raise Rejected("这条公告已处理，请刷新")
    missing_id = row["message"] is None and row["kind"] in {
        "round_open",
        "round_close",
        "round_result",
    }
    if op == "game_notice_view":
        base = {
            "action": "game_notice_resolve",
            "id": row["id"],
            "version": row["version"],
        }
        return await ui.render(
            update,
            f"群：{row['chat']}\n状态：{row['status']} · {row['error']}\n\n{row['text']}\n\n"
            "请在群内人工核实。旧公告不再重新投递，未送达只标记为已替代，不改订单或积分。"
            + (
                "\n已送达的公告请回复原消息执行 /recoverbroadcast，找回消息ID后才能清理。"
                if missing_id
                else ""
            ),
            (
                []
                if missing_id
                else [
                    ("已核实送达，只记结果", {**base, "result": "sent"}),
                ]
            )
            + [
                ("已核实未送达，不再投递", {**base, "result": "superseded"}),
            ]
            + back,
        )
    if payload.get("result") == "pending":
        raise Rejected("旧公告不再投递，请刷新后登记核查结果")
    if op != "game_notice_resolve" or payload.get("result") not in {
        "sent",
        "superseded",
    }:
        raise Rejected("操作无效")
    if missing_id and payload["result"] == "sent":
        raise Rejected("请回复原公告执行 /recoverbroadcast，不能只标记已送达。")
    with store.tx() as db:
        store.require(uid, "game", db)
        if not db.execute(
            "UPDATE callbacks SET used=1 WHERE token=? AND uid=? AND chat=? AND used=0 AND expires>?",
            (token, uid, str(update.effective_chat.id), store.clock()),
        ).rowcount:
            raise Rejected("确认已使用或过期")
        if not db.execute(
            "UPDATE gg_dispatch SET status=?,next=0,error='ManualVerification',version=version+1 WHERE id=? AND version=? AND status IN ('review','blocked')",
            (payload["result"], row["id"], payload["version"]),
        ).rowcount:
            raise Rejected("公告状态已变化，请重新核查")
        if row["kind"] == "settlement":
            db.execute(
                "UPDATE gg_receipts SET status=? WHERE op=?",
                (payload["result"], row["op"]),
            )
        store.audit(db, uid, "game_delivery_manual_verification", payload)
    return await ui.render(update, "已记录人工核查结果；订单和积分不变。", back)
