"""Button choices for administrative forms; free text is collected one field at a time."""

import secrets

STEPS = {
    "points": [
        ("奖励开关", ["开", "关"]),
        ("签到奖励积分（0—1000000）", ["5", "10", "20", "50", "100"]),
        ("每次聊天奖励积分（0—1000000）", ["0", "1", "2", "5", "10"]),
        ("聊天奖励间隔秒数（15—86400）", ["15", "30", "60", "120", "300"]),
        ("每日聊天奖励上限（0—1000000）", ["10", "20", "50", "100"]),
        ("奖励群数字 ID，多个用逗号分隔；不设置点下方按钮", ["-"]),
    ],
    "adjust": [
        ("目标用户数字 UID", None),
        ("增减积分（扣分填负数）", ["10", "100", "1000", "-10", "-100"]),
        ("调整原因", None),
    ],
    "grant": [
        (
            "接收授权的账号：数字 UID、@用户名或 t.me/用户名；对方需先私聊机器人发送 /start",
            None,
        ),
        (
            "全部业务管理权限（不含授权和撤权，群操作仍需 Telegram 管理员身份）",
            ["全功能"],
        ),
        ("授权有效天数，也可输入自定义天数", ["1", "7", "30", "90"]),
    ],
    "package": [
        ("广告位编号（字母数字；已有编号会更新该广告位）", None),
        ("广告位显示名称", None),
        ("广告类型", ["普通", "置顶"]),
        ("价格，也可输入自定义金额", ["10", "30", "50", "100", "300", "500"]),
        ("币种代码，也可输入自定义币种", ["USDT"]),
        ("选择已启用管理的发布群", None),
        ("有效期限", ["30天", "月"]),
        ("置顶名额数（普通广告位填1）", ["1", "2", "3", "5", "10"]),
        ("收款说明", None),
        ("广告位开关", ["开", "关"]),
    ],
}


async def run(ui, update, form, values, page=0):
    """Render the next field or a final confirmation without committing changes.

    Args:
        ui: Active private menu renderer.
        update: Authenticated Telegram update.
        form: Fixed administrative form identifier.
        values: Collected field values held in server-side callbacks.
        page: Managed-group selection page.
    """
    from .store import Rejected
    from .ui import FORM_SCOPES, parse_form

    uid = str(update.effective_user.id)
    scope = FORM_SCOPES[form]
    ui.store.require(uid, scope, owner=scope == "owner")
    if form == "package" and len(values) > 5:
        if not ui.store.db.execute(
            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1 UNION ALL SELECT 1 FROM ad_channels WHERE chat=? AND enabled=1",
            (values[5], values[5]),
        ).fetchone():
            raise Rejected("该群未启用管理或已停用，请返回选择发布群")
    if values:
        index = len(values) - 1
        value = values[-1]
        choices = STEPS[form][index][1]
        fixed = (
            (form == "points" and index == 0)
            or (form == "grant" and index == 1)
            or (form == "package" and index in (2, 6, 9))
        )
        if fixed and value not in choices:
            raise Rejected("请使用当前步骤的选择按钮")
        numeric = {
            "points": {1, 2, 3, 4},
            "adjust": {0, 1},
            "grant": {2},
            "package": {5, 7},
        }
        if index in numeric[form] and not value.lstrip("-").isascii():
            raise Rejected("请填写数字")
        if index in numeric[form]:
            try:
                int(value)
            except ValueError:
                raise Rejected("请填写整数") from None
    ui.store.clear_dialog(uid)
    steps = STEPS[form]
    back = [("取消", {"action": "admin"})]
    if values:
        back.insert(
            0, ("上一步", {"action": "wizard", "form": form, "values": values[:-1]})
        )
    if len(values) == len(steps):
        try:
            data = parse_form(
                form, ("\n" if form == "package" else " ").join(values), {}
            )
        except (ValueError, Rejected):
            return await ui.render(
                update, "填写内容有误，可点上一步修改或取消后重新填写。", back
            )
        if form == "grant":
            from .grant_target import resolve

            data.update(await resolve(ui.runtime, data["target"]))
        return await ui.render(
            update,
            "请核对：\n"
            + "\n".join(f"{step[0]}：{value}" for step, value in zip(steps, values))
            + (f"\n最终授权 UID：{data['target']}" if form == "grant" else ""),
            [("确认保存", {"action": "save_form", "form": form, "data": data})] + back,
        )
    label, choices = steps[len(values)]
    if form == "package" and not values:
        packages = list(ui.store.get("packages", {}).items())
        page = max(0, min(int(page), max(0, (len(packages) - 1) // 8)))
        buttons = [
            (
                "➕ 新建广告位",
                {
                    "action": "wizard",
                    "form": form,
                    "values": ["ad_" + secrets.token_hex(6)],
                },
            )
        ]
        buttons += [
            (
                f"重新设置 · {item['name']}",
                {"action": "wizard", "form": form, "values": [key]},
            )
            for key, item in packages[page * 8 : page * 8 + 8]
        ]
        for offset, title in [(-1, "上一页"), (1, "下一页")]:
            if 0 <= page + offset <= (len(packages) - 1) // 8:
                buttons.append(
                    (
                        title,
                        {
                            "action": "wizard",
                            "form": form,
                            "values": [],
                            "page": page + offset,
                        },
                    )
                )
        return await ui.render(
            update,
            "请选择新建或重新设置已有广告位。\n新建自动生成编号；重新设置需逐项填写，确认前不会改动原广告位。",
            buttons + back,
        )
    if form == "package" and len(values) == 5:
        groups = ui.store.db.execute(
            "SELECT chat,title FROM mod_groups WHERE enabled=1 UNION ALL SELECT chat,'频道 · ' || title AS title FROM ad_channels WHERE enabled=1 ORDER BY title,chat"
        ).fetchall()
        page = max(0, min(int(page), max(0, (len(groups) - 1) // 8)))
        buttons = [
            (
                f"{row['title']} · {row['chat']}",
                {"action": "wizard", "form": form, "values": values + [row["chat"]]},
            )
            for row in groups[page * 8 : page * 8 + 8]
        ]
        for offset, title in [(-1, "上一页"), (1, "下一页")]:
            if 0 <= page + offset <= (len(groups) - 1) // 8:
                buttons.append(
                    (
                        title,
                        {
                            "action": "wizard",
                            "form": form,
                            "values": values,
                            "page": page + offset,
                        },
                    )
                )
        return await ui.render(
            update,
            f"请选择发布群 · 第{page + 1}页"
            if groups
            else "暂无已启用管理的群，请先在群管理中添加并启用。",
            buttons + back,
        )
    if form == "points" and len(values) == 5:
        # Discovery alone does not authorize a group for business use.
        groups = ui.store.db.execute(
            "SELECT chat,title FROM mod_groups WHERE enabled=1 ORDER BY title,chat"
        ).fetchall()
        page = max(0, min(int(page), max(0, (len(groups) - 1) // 8)))
        buttons = [
            (
                f"{row['title']} · {row['chat']}",
                {
                    "action": "wizard",
                    "form": form,
                    "values": values + [str(row["chat"])],
                },
            )
            for row in groups[page * 8 : page * 8 + 8]
        ]
        for offset, title in [(-1, "上一页"), (1, "下一页")]:
            if 0 <= page + offset <= (len(groups) - 1) // 8:
                buttons.append(
                    (
                        title,
                        {
                            "action": "wizard",
                            "form": form,
                            "values": values,
                            "page": page + offset,
                        },
                    )
                )
        buttons.append(
            (
                "不设置群",
                {"action": "wizard", "form": form, "values": values + ["-"]},
            )
        )
        ui.store.dialog(uid, {"form": "wizard", "kind": form, "values": values})
        return await ui.render(
            update,
            (
                f"第 6/6 步 · 请选择已启用管理的奖励群（第 {page + 1} 页）\n"
                if groups
                else "暂无已启用管理的群，请先在群管理中确认并启用。\n"
            )
            + "多个群可单独发送群数字 ID，用逗号分隔。",
            buttons + back,
        )
    ui.store.dialog(uid, {"form": "wizard", "kind": form, "values": values})
    buttons = [
        (
            "不设置群" if value == "-" else value,
            {"action": "wizard", "form": form, "values": values + [value]},
        )
        for value in choices or []
    ]
    return await ui.render(
        update,
        f"第 {len(values) + 1}/{len(steps)} 步 · {label}\n"
        + (
            "请点击下方选项。"
            if (form == "points" and len(values) == 0)
            or (form == "grant" and len(values) == 1)
            or (form == "package" and len(values) in (2, 6, 9))
            else "可点击下方常用值，也可单独发送自定义内容。"
            if choices
            else "请发送这一项的内容；也可返回上一步或取消。"
        ),
        buttons + back,
    )
