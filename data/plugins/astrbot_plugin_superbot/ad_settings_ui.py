"""Readable advertising settings with scoped, single-field confirmation."""

import secrets

from .store import Rejected

FIELDS = {
    "name": "名称",
    "price": "价格",
    "currency": "币种",
    "kind": "类型",
    "target": "投放位置",
    "duration": "期限",
    "slots": "套餐名额",
    "payment": "收款说明",
    "enabled": "开关",
}
CHOICES = {
    "kind": [("普通", "normal"), ("置顶", "pinned")],
    "duration": [("30天", "30days"), ("自然月", "month")],
    "enabled": [("开启", True), ("关闭", False)],
    "currency": [("USDT", "USDT"), ("CNY", "CNY")],
    "slots": [(str(n), n) for n in (1, 2, 3, 5, 10)],
}


async def action(ui, update, payload):
    """Display data as text and choose one field via permission-bound callbacks.

    Args:
        ui: Private administrative UI.
        update: Authenticated user interaction.
        payload: Server-stored callback or dialog payload.
    """
    uid = str(update.effective_user.id)
    if update.effective_chat.type != "private":
        raise Rejected("请在私聊管理广告。")
    ui.store.require(uid, "ads")
    kind = payload["action"]
    packages = ui.store.get("packages", {})
    back = [("⬅️ 广告管理", {"action": "admin_ads"})]
    if kind in {"ap_home", "ap_list"}:
        ui.store.clear_dialog(uid)
    if kind == "ap_home":
        counts = dict(
            ui.store.db.execute(
                "SELECT status,COUNT(*) FROM platform_ads GROUP BY status"
            ).fetchall()
        )
        text = (
            "📣 广告管理\n选择下方业务，不需要逐项翻找设置。\n\n"
            f"📊 当前概况\n广告位 {len(packages)} 个 · 开启 {sum(bool(p['enabled']) for p in packages.values())} 个\n"
            f"待处理 {counts.get('pending', 0)} 单 · 展示中 {counts.get('active', 0) + counts.get('published', 0)} 单\n"
            f"待核查 {counts.get('review', 0)} 单（退款与充值异常另见核查页）\n\n"
            "ℹ️ 操作说明\n广告位：查看配置、修改单项、新建。\n订单：审核内容与确认收款；两项独立。\n投放：频道与广告栏容量。\n核查：发送异常、退款和充值；未知发送结果不自动重发。"
        )
        return await ui.render(
            update,
            text,
            [
                ("⚙️ 广告位设置", {"action": "ap_list"}),
                ("📋 订单审核", {"action": "ad_queue"}),
                ("📍 投放频道", {"action": "channels"}),
                ("🧱 广告栏容量", {"action": "ad_capacities"}),
                ("⚠️ 广告异常", {"action": "ad_recovery"}),
                ("💳 充值核查", {"action": "ad_chain_review"}),
                ("⬅️ 管理中心", {"action": "admin"}),
            ],
            fold_sections=True,
        )
    if kind == "ap_list":
        entries = list(packages.items())
        page = min(max(0, int(payload.get("page", 0))), max(0, (len(entries) - 1) // 6))
        entries = entries[page * 6 : page * 6 + 6]
        text = f"⚙️ 广告位设置 · 第{page + 1}页\n点击编号选择；数据在正文查看，修改不用重填整套配置。"
        buttons = []
        for number, (key, package) in enumerate(entries, page * 6 + 1):
            text += (
                f"\n\n{number}. {package['name']}\n"
                f"{'🟢 开启' if package['enabled'] else '⚪ 关闭'} · "
                f"{'置顶' if package['kind'] == 'pinned' else '普通'} · "
                f"{package['price']} {package['currency']}\n"
                f"位置 {package['target']} · {'30天' if package['duration'] == '30days' else '自然月'}"
            )
            buttons.append((f"选择 {number}", {"action": "ap_detail", "key": key}))
        if not entries:
            text += "\n暂无广告位，点击新建。"
        if page:
            buttons.append(("上一页", {"action": kind, "page": page - 1}))
        if (page + 1) * 6 < len(packages):
            buttons.append(("下一页", {"action": kind, "page": page + 1}))
        return await ui.render(
            update,
            text,
            buttons
            + [
                (
                    "➕ 新建广告位",
                    {
                        "action": "wizard",
                        "form": "package",
                        "values": ["ad_" + secrets.token_hex(6)],
                    },
                ),
            ]
            + back,
            fold_sections=True,
        )
    key = payload.get("key")
    package = packages.get(key)
    if not package:
        raise Rejected("广告位不存在，请重新打开列表。")
    back = [("⬅️ 返回广告位", {"action": "ap_detail", "key": key})]
    if kind == "ap_detail":
        ui.store.clear_dialog(uid)
        target = ui.store.db.execute(
            "SELECT title FROM mod_groups WHERE chat=? UNION ALL SELECT title FROM ad_channels WHERE chat=?",
            (package["target"], package["target"]),
        ).fetchone()
        text = (
            f"⚙️ {package['name']}\n编号：{key} · {'🟢 开启' if package['enabled'] else '⚪ 关闭'}\n\n"
            f"📍 投放与规格\n位置：{target[0] if target else package['target']}（{package['target']}）\n"
            f"类型：{'置顶' if package['kind'] == 'pinned' else '普通'}\n"
            f"期限：{'30天' if package['duration'] == '30days' else '自然月'} · 套餐名额：{package['slots']}\n\n"
            f"💰 收费信息\n价格：{package['price']} {package['currency']}\n收款说明：{package['payment']}\n\n"
            "✏️ 修改说明\n选择一个字段 → 输入或点选 → 核对保存。其他字段保持原值；既有订单快照不变。\n套餐名额与广告栏总容量是两项不同设置。"
        )
        return await ui.render(
            update,
            text,
            [
                (
                    label,
                    {
                        "action": "ap_edit",
                        "key": key,
                        "field": field,
                        "expected": package,
                    },
                )
                for field, label in FIELDS.items()
            ]
            + [("⬅️ 广告位列表", {"action": "ap_list"})],
            fold_sections=True,
        )
    field = payload.get("field")
    if field not in FIELDS or package != payload.get("expected"):
        raise Rejected("配置已变化，请重新打开广告位后修改。")
    current_label = next(
        (label for label, value in CHOICES.get(field, []) if value == package[field]),
        str(package[field]),
    )
    if kind == "ap_edit":
        ui.store.clear_dialog(uid)
        buttons = [
            (label, {**payload, "action": "ap_preview", "value": value})
            for label, value in CHOICES.get(field, [])
        ]
        if field == "target":
            rows = ui.store.db.execute(
                "SELECT chat,title FROM platform_mod_groups WHERE enabled=1 UNION SELECT chat,title FROM ad_channels WHERE enabled=1 ORDER BY chat"
            ).fetchall()
            page = min(
                max(0, int(payload.get("page", 0))), max(0, (len(rows) - 1) // 8)
            )
            buttons = [
                (
                    str(row["title"])[:35],
                    {**payload, "action": "ap_preview", "value": row["chat"]},
                )
                for row in rows[page * 8 : page * 8 + 8]
            ]
            if page:
                buttons.append(("上一页", {**payload, "page": page - 1}))
            if (page + 1) * 8 < len(rows):
                buttons.append(("下一页", {**payload, "page": page + 1}))
        if field not in {"target", "kind", "duration", "enabled"}:
            ui.store.dialog(uid, {"form": "ap_field", "payload": payload})
        return await ui.render(
            update,
            f"✏️ 修改{FIELDS[field]}\n当前值：{current_label}\n"
            + (
                "点击下方选项。"
                if field in {"target", "kind", "duration", "enabled"}
                else "发送新值，或选择下方常用值；下一步核对，不会立即保存。"
            ),
            buttons + back,
            fold_sections=True,
        )
    value = payload.get("value")
    if field == "slots":
        try:
            value = int(value)
        except (ValueError, TypeError):
            raise Rejected("名额请填写1—100的整数。") from None
    if field in {"kind", "duration", "enabled"} and value not in [
        v for _, v in CHOICES[field]
    ]:
        raise Rejected("请使用选项按钮。")
    if (
        field == "target"
        and not ui.store.db.execute(
            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1 UNION ALL SELECT 1 FROM ad_channels WHERE chat=? AND enabled=1",
            (value, value),
        ).fetchone()
    ):
        raise Rejected("投放位置已停用，请重新选择。")
    if kind == "ap_preview":
        ui.store.clear_dialog(uid)
        new_label = next(
            (label for label, choice in CHOICES.get(field, []) if choice == value),
            str(value),
        )
        return await ui.render(
            update,
            f"✅ 核对单项修改\n广告位：{package['name']}\n字段：{FIELDS[field]}\n"
            f"原值：{current_label}\n新值：{new_label}\n\nℹ️ 保存范围\n只修改此项；既有订单与付款状态不变。",
            [("确认保存", {**payload, "action": "ap_save", "value": value})] + back,
            fold_sections=True,
        )
    if kind != "ap_save":
        raise Rejected("操作无效。")
    # No await between the snapshot comparison and the existing synchronous save.
    ui.runtime.ads.configure(uid, key, {**package, field: value})
    ui.store.clear_dialog(uid)
    return await action(ui, update, {"action": "ap_detail", "key": key})
