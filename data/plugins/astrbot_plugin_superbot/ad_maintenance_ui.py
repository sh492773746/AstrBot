"""Private cancellation, capacity, recovery and chain review workflows."""

import asyncio
import json

from .store import Rejected


async def action(ui, update, payload):
    """Render or confirm scoped maintenance actions.

    Args:
        ui: Existing private Telegram menu.
        update: Authenticated private interaction.
        payload: Server-stored callback data.
    """
    uid = str(update.effective_user.id)
    store = ui.store
    kind = payload["action"]
    from .tenants import platform_group

    if payload.get("chat") and not platform_group(store, payload["chat"]):
        raise Rejected("独立经营群请使用我的群中的异常核查入口")
    if payload.get("id") or payload.get("op"):
        key = payload.get("id") or payload["op"]
        order = store.db.execute("SELECT tenant FROM ads WHERE id=?", (key,)).fetchone()
        operation = store.db.execute(
            "SELECT chat FROM ad_operations WHERE id=?", (key,)
        ).fetchone()
        if (order and order["tenant"] != "platform") or (
            operation and not platform_group(store, operation["chat"])
        ):
            raise Rejected("此业务属于独立经营者，旧核查按钮不可使用。")
    back = [("返回广告管理", {"action": "admin_ads"})]
    if kind in ("ad_cancel_preview", "ad_cancel_confirm"):
        row = store.db.execute(
            "SELECT * FROM ads WHERE id=?", (payload["id"],)
        ).fetchone()
        if not row or (row["uid"] != uid and not store.allowed(uid, "ads")):
            raise Rejected("无权处理此订单")
        if kind == "ad_cancel_confirm":
            ui.runtime.ads.cancel(uid, row["id"], payload["version"])
            return await ui.render(
                update,
                "订单已取消。余额付款已退回广告余额；人工核款由管理员跟进退款，测试免付款不退款。",
                [("我的广告", {"action": "ad_orders"})],
            )
        if row["status"] != "pending" or row["first_published"]:
            raise Rejected("此订单不能直接取消，请先核查发布结果")
        refund = (
            "退回广告余额"
            if row["payment_source"] == "wallet"
            else "需管理员人工退款"
            if row["paid"] and row["payment_source"] != "test"
            else "无实际款项需要退回"
        )
        return await ui.render(
            update,
            f"确认取消订单？\n{refund}。取消后不会发布。",
            [
                (
                    "确认取消",
                    {
                        "action": "ad_cancel_confirm",
                        "id": row["id"],
                        "version": row["version"],
                    },
                ),
                (
                    "返回",
                    {
                        "action": "ad_view" if row["uid"] == uid else "ad_review",
                        "id": row["id"],
                    },
                ),
            ],
        )
    store.require(uid, "ads")
    if kind == "ad_capacities":
        rows = store.db.execute(
            "SELECT t.chat,t.capacity,COALESCE(g.title,c.title,t.chat) AS title FROM ad_targets t LEFT JOIN mod_groups g ON g.chat=t.chat LEFT JOIN ad_channels c ON c.chat=t.chat WHERE NOT EXISTS(SELECT 1 FROM tenant_groups x WHERE x.chat=t.chat AND x.tenant<>'platform') ORDER BY t.chat"
        ).fetchall()
        page = max(0, int(payload.get("page", 0)))
        return await ui.render(
            update,
            "选择广告栏设置总容量；调低不会移除现有广告。",
            [
                (
                    f"{r['title']} · {r['capacity']}位",
                    {"action": "ad_capacity", "chat": r["chat"]},
                )
                for r in rows[page * 8 : page * 8 + 8]
            ]
            + [
                ("上一页", {"action": kind, "page": max(0, page - 1)}),
                ("下一页", {"action": kind, "page": page + 1}),
            ]
            + back,
        )
    if kind in ("ad_capacity", "ad_capacity_preview", "ad_capacity_save"):
        row = store.db.execute(
            "SELECT * FROM ad_targets WHERE chat=?", (payload["chat"],)
        ).fetchone()
        if not row:
            raise Rejected("广告栏不存在")
        if kind == "ad_capacity":
            store.dialog(
                uid,
                {"form": "ad_capacity", "chat": row["chat"], "version": row["version"]},
            )
            return await ui.render(
                update,
                f"当前容量 {row['capacity']}。点击常用值或发送1—100的整数。",
                [
                    (
                        str(n),
                        {
                            "action": "ad_capacity_preview",
                            "chat": row["chat"],
                            "version": row["version"],
                            "capacity": n,
                        },
                    )
                    for n in (1, 3, 5, 10)
                ]
                + back,
            )
        value = int(payload["capacity"])
        if not 1 <= value <= 100:
            raise Rejected("容量需为1—100")
        if row["version"] != payload["version"]:
            raise Rejected("容量已变化，请刷新")
        if kind == "ad_capacity_preview":
            return await ui.render(
                update,
                f"确认容量改为 {value}？已有展示保留，超出时停止新增。",
                [("确认", {**payload, "action": "ad_capacity_save"})] + back,
            )
        with store.tx() as db:
            store.require(uid, "ads", db)
            db.execute(
                "UPDATE ad_targets SET capacity=?,version=version+1 WHERE chat=? AND version=?",
                (value, row["chat"], payload["version"]),
            )
            store.audit(
                db, uid, "ad_capacity", {"chat": row["chat"], "capacity": value}
            )
        return await ui.render(update, "容量已保存。", back)
    if kind == "ad_recovery":
        rows = store.db.execute(
            "SELECT * FROM ad_operations WHERE status IN ('review','retry','executing') AND NOT EXISTS(SELECT 1 FROM tenant_groups g WHERE g.chat=ad_operations.chat AND g.tenant<>'platform') ORDER BY at"
        ).fetchall()
        page = max(0, int(payload.get("page", 0)))
        legacy = store.db.execute(
            "SELECT COUNT(*) FROM ad_boards b WHERE state='review' AND NOT EXISTS(SELECT 1 FROM ad_operations o WHERE o.chat=b.chat AND o.status='review') AND NOT EXISTS(SELECT 1 FROM tenant_groups g WHERE g.chat=b.chat AND g.tenant<>'platform')"
        ).fetchone()[0]
        return await ui.render(
            update,
            f"待核查／限流操作：{len(rows)}。旧版本无操作快照的冻结栏：{legacy}（需维护人员核对审计，不自动恢复）。",
            [
                (
                    f"{r['chat']} · {r['status']}",
                    {"action": "ad_recover_view", "op": r["id"]},
                )
                for r in rows[page * 8 : page * 8 + 8]
            ]
            + [
                ("上一页", {"action": kind, "page": max(0, page - 1)}),
                ("下一页", {"action": kind, "page": page + 1}),
            ]
            + back,
        )
    if kind in (
        "ad_recover_view",
        "ad_recover_input",
        "ad_recover_confirm",
        "ad_recover_before",
        "ad_recover_rollback",
    ):
        row = store.db.execute(
            "SELECT * FROM ad_operations WHERE id=?", (payload["op"],)
        ).fetchone()
        if not row or row["status"] not in ("review", "retry", "executing"):
            raise Rejected("操作已处理，请刷新")
        data = json.loads(row["payload"])
        if kind == "ad_recover_view":
            link = (
                f"https://t.me/c/{row['chat'][4:]}/{row['message']}"
                if row["message"] and row["chat"].startswith("-100")
                else "消息ID未知"
            )
            return await ui.render(
                update,
                f"目标：{row['chat']}\n{link}\n状态：{row['status']} · {row['error']}\n订单：{', '.join(c['id'] for c in data['changes'])}\n操作前：\n{data['before'] or '无消息'}\n预期操作后：\n{data['after']}\n预期置顶：{'是' if data.get('pin') else '否'}",
                (
                    [
                        (
                            "人工核实已完成",
                            {"action": "ad_recover_input", "op": row["id"]},
                        ),
                        (
                            "已核实未生效／恢复原状",
                            {"action": "ad_recover_before", "op": row["id"]},
                        ),
                    ]
                    if row["status"] == "review"
                    else []
                )
                + back,
            )
        if row["status"] != "review":
            raise Rejected("尚在执行或等待限流，请勿人工覆盖")
        if kind == "ad_recover_before":
            if not all("before_order" in c for c in data["changes"]):
                raise Rejected("旧操作缺少快照，须维护人员核对")
            return await ui.render(
                update,
                "请先人工确认：原广告栏正文与置顶已恢复操作前状态；若此次创建了新消息，确认该新消息不存在或已移除。确认后只恢复本地订单，新增订单撤销审核，须重新审核才发布。无法核实请勿确认。",
                [("确认恢复原状", {"action": "ad_recover_rollback", "op": row["id"]})]
                + back,
            )
        if kind == "ad_recover_rollback":
            lock = store.ad_locks.setdefault(row["chat"], asyncio.Lock())
            async with lock:
                with store.tx() as db:
                    store.require(uid, "ads", db)
                    if (
                        db.execute(
                            "SELECT status FROM ad_operations WHERE id=?", (row["id"],)
                        ).fetchone()[0]
                        != "review"
                    ):
                        raise Rejected("状态已变化")
                    for change in data["changes"]:
                        before = change.get("before_order")
                        if not before:
                            raise Rejected("缺少原状快照")
                        db.execute(
                            "UPDATE ads SET status=?,message=?,expires=?,slot=?,body=?,first_published=?,error=? WHERE id=?",
                            (
                                before["status"],
                                before["message"],
                                before["expires"],
                                before["slot"],
                                before["body"],
                                before["first_published"],
                                "管理员人工确认恢复原状",
                                change["id"],
                            ),
                        )
                        if before["status"] == "pending":
                            db.execute(
                                "UPDATE ads SET approved=0,approved_by='' WHERE id=?",
                                (change["id"],),
                            )
                    if row["kind"] == "board":
                        # The stored board message is only updated after successful completion.
                        db.execute(
                            "UPDATE ad_boards SET state='idle',body=?,error='' WHERE chat=?",
                            (data["before"], row["chat"]),
                        )
                    db.execute(
                        "UPDATE ad_operations SET status='rolled_back' WHERE id=?",
                        (row["id"],),
                    )
                    store.audit(db, uid, "ad_manual_restore_before", {"op": row["id"]})
            return await ui.render(
                update, "已记录人工核查并恢复原状；未发布订单须重新审核。", back
            )
        if kind == "ad_recover_input":
            store.dialog(uid, {"form": "ad_recover", "op": row["id"]})
            return await ui.render(
                update,
                "请人工核对目标消息正文及置顶状态均与预期一致，再发送消息数字ID。Telegram不能直接读取任意历史消息，此操作记为人工确认；无法确认请返回，保持冻结。",
                back,
            )
        message = int(payload["message"])
        if message <= 0 or (row["message"] and message != row["message"]):
            raise Rejected("消息ID不符，不可替换为另一条消息")
        lock = store.ad_locks.setdefault(row["chat"], asyncio.Lock())
        async with lock:
            store.require(uid, "ads")
            current = store.db.execute(
                "SELECT status FROM ad_operations WHERE id=?", (row["id"],)
            ).fetchone()
            if current[0] != "review":
                raise Rejected("操作状态已变化")
            from .ad_board import finish

            finish(store, row["id"], message, uid)
        return await ui.render(
            update, "已按你的人工核查记录恢复；没有重新发送消息。", back
        )
    if kind == "ad_chain_review":
        rows = store.db.execute(
            "SELECT tx,amount,stamp FROM chain_events WHERE status='review' ORDER BY stamp DESC LIMIT 10"
        ).fetchall()
        from .payments import money

        text = "充值核查：金额不符或超时交易不自动归属。\n" + "\n".join(
            f"{money(r['amount'])} USDT · {r['tx']}" for r in rows
        )
        text += (
            "\n扫描健康："
            + str(store.get("usdt_health", {}).get("error") or "正常")
            + "\n"
            + store.get("usdt_scan_warning", "")
        )
        return await ui.render(update, text, back)
    raise Rejected("未知维护操作")
