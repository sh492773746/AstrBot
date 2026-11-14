"""Private, user-bound menus for independent owners and their advertisers."""

import json
import re
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .game_registry import ENTERTAINMENT, LOCAL_SWITCHES
from .group_identity import group_label
from .merchant_ads import test_enabled
from .payments import address_hex, money
from .points import DEFAULT, validate
from .store import Rejected
from .tenants import POLICY, binding, local_config

FEATURES = LOCAL_SWITCHES
STATES = {
    "merchant_review": "等待群主审核",
    "pending": "等待付款／发布",
    "merchant_expired": "付款窗口已结束",
    "review": "需要核查",
    "active": "展示中",
    "published": "已发布",
    "rejected": "未通过审核",
    "cancelled": "已取消",
    "expired": "已到期",
    "paid": "已核实到账",
    "paid_waiting": "已付款待安排",
    "refund_requested": "等待群主退款",
    "refunded": "退款已核验",
}


async def _chat_label(runtime, store, chat):
    row = store.db.execute(
        "SELECT title FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()
    try:
        info = await runtime.bot.get_chat(chat)
    except Exception:
        info = None
    return group_label(info, row["title"] if row else "")


# Group-owner numeric settings are deliberately collected one value at a time.
# Keeping the step metadata here also makes the prompts and stored dialog state
# consistent across the private UI and avoids ambiguous whitespace parsing.
STEP_FIELDS = {
    "rewards": (
        ("checkin", "签到奖励积分（0—1000000）"),
        ("chat", "聊天奖励积分（0—1000000）"),
        ("interval", "聊天奖励间隔秒数（15—86400秒）"),
        ("cap", "每日聊天奖励上限（0—1000000）"),
    ),
    "k3_limits": (
        ("ordinary", "普通玩法单项上限"),
        ("special", "特殊玩法单项上限"),
        ("number", "指定号码单项上限"),
        ("total", "单期合计上限"),
    ),
    "canada_limits": (
        ("minimum", "最低下注积分"),
        ("maximum", "单笔上限"),
        ("total", "单期合计上限"),
    ),
}
STAKE_FIELDS = {"slots_stakes", "mines_stakes"}
STAKE_OPTIONS = (100, 300, 800, 1500, 2000)


async def gift_member(ui, chat, username, target=None):
    """Resolve a known group member and recheck Telegram identity before a gift.

    Args:
        ui: Private UI and Telegram client.
        chat: Explicit target group.
        username: Canonical public username without an at sign.
        target: Previously confirmed numeric identity, if present.

    Returns:
        The currently verified Telegram user.

    Raises:
        Rejected: Identity is unknown, ambiguous, changed or outside this group.
    """
    rows = ui.store.db.execute(
        "SELECT uid FROM mod_members WHERE chat=? AND lower(username)=?",
        (str(chat), username.lower()),
    ).fetchall()
    if len(rows) != 1 or (target is not None and str(target) != rows[0]["uid"]):
        raise Rejected(
            "未能唯一识别本群成员。请该群员先在目标群发一条消息，再重新发送其 @用户名或 https://t.me/用户名。"
        )
    try:
        member = await ui.runtime.bot.get_chat_member(chat, int(rows[0]["uid"]))
    except Exception as exc:
        raise Rejected("暂时无法核实群员身份，请稍后重试；未调整积分。") from exc
    user = member.user
    if (
        str(user.id) != rows[0]["uid"]
        or (user.username or "").lower() != username.lower()
        or user.is_bot
        or member.status not in {"member", "administrator", "creator", "restricted"}
        or (member.status == "restricted" and not member.is_member)
    ):
        raise Rejected(
            "该成员已离群、用户名已变更或不是个人账号，请重新核对；未调整积分。"
        )
    return user


AD_GROUPS = (
    "FROM mod_groups m JOIN tenant_groups g ON g.chat=m.chat "
    "JOIN tenants t ON t.id=g.tenant "
    "JOIN tenant_settings s ON s.chat=g.chat AND s.key='ads_enabled' AND s.value='true' "
    "WHERE m.enabled=1 AND g.status='active' AND t.status='active' "
    "AND g.tenant<>'platform' AND EXISTS(SELECT 1 FROM merchant_packages p "
    "WHERE p.chat=g.chat AND json_extract(p.config,'$.enabled')=1) "
    "AND EXISTS(SELECT 1 FROM merchant_addresses a WHERE a.chat=g.chat) "
)


async def find_ad_groups(ui, text, page=0, uid=""):
    """Resolve a registered public group or page through literal title matches.

    Args:
        ui: Private UI with the instance store and Telegram client.
        text: Public group link, username, ID, or literal title keyword.
        page: Zero-based result page.

    Returns:
        Up to nine rows (the ninth signals another page) and the page number.

    Raises:
        Rejected: The target is malformed or cannot be verified.
    """
    if not text or len(text) > 100:
        raise Rejected("请发送群公开链接、@用户名、群ID或名称关键词（最多100字）。")
    target = None
    test_target = False
    if text.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        parsed = urlparse(text if "://" in text else "https://" + text)
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 1 or parts[0].startswith(("+", "joinchat")):
            raise Rejected("请发送公开群主页链接，私密邀请链接不用于确认投放目标。")
        target = "@" + parts[0]
    elif text.startswith("@"):
        target = text
    elif text.startswith("-") and text[1:].isdigit():
        target = text
    if target:
        try:
            group = await ui.runtime.bot.get_chat(target)
        except Exception as exc:
            raise Rejected("无法核实该群，请检查公开链接或改用群名称关键词。") from exc
        if group.type != "supergroup":
            raise Rejected("请选择已接入的超级群。")
        registered = ui.store.db.execute(
            "SELECT m.enabled,g.status,g.tenant,t.status AS business_status "
            "FROM mod_groups m LEFT JOIN tenant_groups g ON g.chat=m.chat "
            "LEFT JOIN tenants t ON t.id=g.tenant WHERE m.chat=?",
            (str(group.id),),
        ).fetchone()
        if not registered:
            raise Rejected("该群尚未接入本BOT，请联系群管理员先接入并设置广告位。")
        try:
            bot_member = await ui.runtime.bot.get_chat_member(
                group.id, ui.runtime.bot.id
            )
        except Exception as exc:
            raise Rejected(
                "暂时无法核实机器人是否仍在该群，请稍后重试或联系群管理员。"
            ) from exc
        if bot_member.status in {"left", "kicked"}:
            raise Rejected("机器人已不在该群，暂不能投递；请群管理员重新接入。")
        context = f"📢 目标群：{group.title}\n群ID：{group.id}\n"
        issues = []
        if not registered["enabled"]:
            issues.append("⛔ 本群服务未开启，请群管理员开启本群服务。")
        if not registered["tenant"]:
            issues.append(
                "⚠️ 已有群登记记录，但尚未完成经营者归属核验，请群主完成接入。"
            )
        elif (
            registered["status"] != "active"
            or registered["business_status"] != "active"
        ):
            issues.append("⛔ 群或经营主体已暂停，请联系本BOT管理员核查恢复。")
        if registered["tenant"] == "platform":
            issues.append("⚠️ 尚未开通独立直付广告，请群主配置独立经营入口。")
        if not local_config(ui.store, str(group.id), "ads_enabled", False):
            issues.append("⛔ 本群广告投放开关未开启：我的群 → 功能启停 → 广告投放。")
        if not ui.store.get("modules", {}).get("ads"):
            issues.append("⛔ 本BOT广告总开关已关闭，请联系本BOT管理员。")
        if not ui.store.db.execute(
            "SELECT 1 FROM merchant_packages WHERE chat=? AND json_extract(config,'$.enabled')=1",
            (str(group.id),),
        ).fetchone():
            configured = ui.store.db.execute(
                "SELECT 1 FROM merchant_packages WHERE chat=?", (str(group.id),)
            ).fetchone()
            issues.append(
                (
                    "⚠️ 已配置广告位，但所有广告位均已停用。"
                    if configured
                    else "⚠️ 该群尚未配置广告位。"
                )
                + "请群主在「我的群 → 广告位与订单」创建或启用广告位。"
            )
        test_target = (
            ui.store.is_superadmin(uid)
            and binding(ui.store, str(group.id)) is not None
            and binding(ui.store, str(group.id))["owner"] == str(uid)
            and test_enabled(ui.store, str(group.id))
        )
        if not ui.runtime.merchant_ads.address(str(group.id)) and not test_target:
            issues.append(
                "⚠️ 尚未配置收款地址：请群主在「我的群 → 收款设置」填写本群TRC20地址。"
            )
        if issues:
            raise Rejected(
                context
                + (
                    "✅ 该群已接入本BOT。\n"
                    if registered["tenant"]
                    else "✅ 已找到该群的登记记录。\n"
                )
                + "暂不能投递，以下条件尚未满足：\n"
                + "\n".join(issues)
                + "\n完成配置后重新发送群链接查询；当前未创建订单、未收款。"
            )
        condition, value = "AND m.chat=?", str(group.id)
    else:
        condition, value = "AND instr(lower(m.title),lower(?))>0", text
    page = max(0, int(page))
    rows = ui.store.db.execute(
        "SELECT m.chat,m.title,g.version "
        + (
            AD_GROUPS.replace(
                "AND EXISTS(SELECT 1 FROM merchant_addresses a WHERE a.chat=g.chat) ",
                "",
            )
            if test_target
            else AD_GROUPS
        )
        + condition
        + " ORDER BY m.title,m.chat LIMIT 9 OFFSET ?",
        (value, page * 8),
    ).fetchall()
    return rows, page


def _step_prompt(kind, index, values):
    """Return a short prompt for the next owner input."""
    if kind in STEP_FIELDS:
        label = STEP_FIELDS[kind][index][1]
        total = len(STEP_FIELDS[kind])
        return (
            f"第 {index + 1}/{total} 项：**{label}**\n"
            + "请只发送一个整数；不要和其他数值写在同一条消息。"
            + (f"\n已填写：{len(values)} 项。" if values else "")
        )
    selected = ", ".join(str(x) for x in values) or "暂无"
    return (
        f"已选择档位：{selected}\n"
        "请发送一个档位：100、300、800、1500 或 2000。\n"
        "继续添加请发送下一个数字；发送“完成”进入预览。"
    )


async def action(ui, update, data, token=""):
    """Recheck group administrators for local controls on every request."""
    from .tenants import group_admin

    name = data.get("action", "")
    uid = str(update.effective_user.id)
    chat = str(data.get("chat", ""))
    if (
        not chat
        and data.get("id")
        and name
        in {
            "tenant_order",
            "tenant_test_preview",
            "tenant_test_switch",
            "tenant_test_publish",
            "tenant_approve_preview",
            "tenant_approve",
            "tenant_reject",
            "tenant_reject_save",
            "tenant_refund_proof",
            "tenant_refund_record",
            "tenant_arrange_preview",
            "tenant_arrange_save",
            "tenant_revision_review",
        }
    ):
        order = ui.store.db.execute(
            "SELECT uid,package FROM ads WHERE id=? AND tenant<>'platform'",
            (data["id"],),
        ).fetchone()
        if order:
            target = str(json.loads(order["package"])["target"])
            target_owner = binding(ui.store, target)
            if (
                name != "tenant_order"
                or order["uid"] != uid
                or (target_owner and target_owner["owner"] == uid)
            ):
                chat = target
    if chat and name not in {
        "tenant_ad_group",
        "tenant_ad_packages",
    }:
        async with group_admin(
            ui.runtime,
            uid,
            chat,
            allow_inactive=name
            in {
                "tenant_group",
                "tenant_status_preview",
                "tenant_status_save",
                "tenant_restore_preview",
                "tenant_restore_save",
                "tenant_owner_preview",
                "tenant_owner_save",
            },
        ):
            return await _action(ui, update, data, token)
    return await _action(ui, update, data, token)


async def _action(ui, update, data, token=""):
    """Render scoped menus and revalidate every confirmed mutation.

    Args:
        ui: Existing Telegram UI.
        update: Authenticated private update.
        data: Server-stored callback or validated dialog payload.
        token: Unique callback operation identity.
    """
    if update.effective_chat.type != "private":
        raise Rejected("请私聊打开我的群")
    runtime, store = ui.runtime, ui.store
    tenants, merchant = runtime.tenants, runtime.merchant_ads
    uid, name = str(update.effective_user.id), data["action"]
    chat = str(data.get("chat", ""))
    back = [("返回我的群", {"action": "tenant_home"})]
    if chat and (
        name in {"tenant_ads", "tenant_errors", "tenant_address"}
        or (name == "tenant_edit" and data.get("field") in {"address", "package"})
    ):
        current = binding(store, chat)
        store.require(uid, chat=chat)
    if name in {"tenant_gift_direction", "tenant_gift_preview", "tenant_gift_save"}:
        await tenants.verify(uid, chat, "points")
        member = await gift_member(ui, chat, data["username"], data["target"])
        label = f"{member.full_name} (@{member.username})"
        if name == "tenant_gift_direction":
            if data.get("sign") not in {1, -1}:
                raise Rejected("请先选择 +积分 或 -积分。")
            store.dialog(
                uid, {"form": "tenant_gift", "payload": data, "stage": "amount"}
            )
            return await ui.render(
                update,
                f"🎁 派送积分给群员 · 第3/3步\n群员：{label}\n已选择：{'增加' if data['sign'] == 1 else '扣减'}积分\n请填写数量（1—1000000），例如 {'100' if data['sign'] == 1 else '-100'}。\n/cancel 取消。",
            )
        amount = data["amount"]
        if type(amount) is not int or not 1 <= abs(amount) <= 1000000:
            raise Rejected("积分数量必须为1—1000000的整数。")
        if name == "tenant_gift_save":
            await tenants.verify(uid, chat, "points")
            runtime.points.adjust(
                uid,
                str(member.id),
                amount,
                "群员积分派送",
                "gift:" + data["op"],
                chat=chat,
                notify=label,
            )
            notice = store.db.execute(
                "SELECT status FROM notices WHERE id=?",
                ("points-gift/gift:" + data["op"],),
            ).fetchone()
            state = {
                "sent": "已发送到对应群聊",
                "pending": "等待发送到对应群聊",
                "sending": "正在发送",
                "unknown": "发送结果未知，请先核查群消息，不要重复派送",
            }.get(notice[0] if notice else "", "请核查通知记录")
            return await ui.render(
                update,
                "✅ 积分变更已记账。\n群通知："
                + state
                + "。\n重复确认不会重复派送，请以本群积分流水为准。",
                back,
            )
        balance = store.balance(str(member.id), chat)
        if balance + amount < 0:
            raise Rejected(
                f"该群员本群积分仅有{balance}，不足以扣减{abs(amount)}；请重新填写数量。"
            )
        return await ui.render(
            update,
            f"🎁 确认派送积分给群员\n群：{await _chat_label(runtime, store, chat)}\n群员：{label}\n本次：{amount:+d} 积分\n当前：{balance} → 调整后：{balance + amount}\n确认后通知对应群聊，仅对本群生效。",
            [("确认派送", {**data, "action": "tenant_gift_save"})] + back,
        )
    if name == "tenant_home":
        from .group_visibility import visible_groups

        store.clear_dialog(uid)
        page = max(0, min(10000, int(data.get("page", 0))))
        rows = store.db.execute(
            "SELECT m.chat,COALESCE(g.status,'active') AS status,m.title,COALESCE(g.tenant,'platform') AS tenant "
            "FROM mod_groups m LEFT JOIN tenant_groups g ON m.chat=g.chat "
            "ORDER BY m.title,m.chat LIMIT 9 OFFSET ?",
            (page * 8,),
        ).fetchall()
        shown = await visible_groups(runtime, uid, rows[:8])
        buttons = [
            (
                r["title"],
                {
                    "action": "activity_group",
                    "chat": r["chat"],
                },
            )
            for r in shown
        ]
        if page:
            buttons.append(("上一页", {"action": "tenant_home", "page": page - 1}))
        if len(rows) > 8:
            buttons.append(("下一页", {"action": "tenant_home", "page": page + 1}))
        buttons += [
            ("接入到群聊", {"action": "tenant_join"}),
            ("广告投递指定群聊", {"action": "tenant_ad_search"}),
        ]
        return await ui.render(
            update,
            f"🏪 **我的群 · 第{page + 1}页**\n每群独立配置、积分和广告订单。\n"
            + (
                "\n".join(
                    f"{r['title']} · {'可管理' if r['status'] == 'active' else '暂停待核查'}"
                    for r in shown
                )
                or "尚未接入群。"
            )
            + "\n\n📌 权限边界\n只能管理自己的群；本BOT总开关、授权和赔率不在本页修改。",
            buttons,
            fold_sections=True,
        )
    if name == "tenant_join":
        policy = tenants.policy()
        if not policy["public"] and uid not in {str(x) for x in policy["pilot_owners"]}:
            return await ui.render(
                update,
                "🔒 **接入到群聊**\n"
                "当前尚未对公众开放，请联系本BOT管理员申请测试资格。\n"
                "已获资格的真实群主可将机器人设为管理员后重新进入本页。",
                [("返回我的群", {"action": "tenant_home"})],
                fold_sections=True,
            )
        return await ui.render(
            update,
            "🔗 **接入自己的超级群**\n"
            "1. 将机器人加入目标超级群。\n"
            "2. 将机器人设为管理员并授予所需群管权限。\n"
            "3. Telegram确认后自动登记并启用默认功能。\n\n"
            "群管理员可在「群管理」自行关闭功能或调整本群参数。"
            "历史经营主体如需迁移，才使用绑定命令；新群不需要凭证。",
            back,
        )
    if name in {"tenant_status_preview", "tenant_status_save"}:
        store.require(uid, owner=True)
        owner = tenants.read(uid, chat)
        if owner["owner"] != uid:
            raise Rejected("仅本群经营群主且具备总控权限可暂停或恢复。")
        if name.endswith("save"):
            await tenants.status(uid, chat, data["active"], data["version"])
            return await action(ui, update, {"action": "tenant_group", "chat": chat})
        return await ui.render(
            update,
            f"🛡 **确认{'恢复' if data['active'] else '暂停'}本群经营权限**\n群：{await _chat_label(runtime, store, chat)}\n原经营者：{owner['owner']}\n"
            "恢复时重新核验原群主与机器人权限；不会转移归属，也不会自动开启业务。历史订单与账本保留。",
            [
                (
                    "确认",
                    {
                        **data,
                        "action": "tenant_status_save",
                        "version": owner["version"],
                    },
                )
            ]
            + back,
        )
    if name in {"tenant_owner_preview", "tenant_owner_save"}:
        owner = tenants.read(uid, chat)
        member = await runtime.bot.get_chat_member(chat, int(uid))
        if member.status not in {"creator", "administrator"} or owner["mode"] != "auto":
            raise Rejected("仅 Telegram 本群现任群主或管理员可确认自动接入的经营归属。")
        if name == "tenant_owner_save":
            await tenants.claim_owner(uid, chat, data["version"])
            return await action(ui, update, {"action": "tenant_group", "chat": chat})
        return await ui.render(
            update,
            f"确认经营归属 · {await _chat_label(runtime, store, chat)}\n"
            "将核验并登记 Telegram 真实群主为经营者，操作管理员不会获得收款权限。"
            "已有广告订单、收款或广告位配置的归属变更须人工核查，"
            "不会自动转移。此操作不恢复群服务；确认后请另行确认重新接入。",
            [
                (
                    "确认归属",
                    {
                        "action": "tenant_owner_save",
                        "chat": chat,
                        "version": owner["version"],
                    },
                ),
                ("返回本群", {"action": "tenant_group", "chat": chat}),
            ],
        )
    if name in {"tenant_restore_preview", "tenant_restore_save"}:
        owner = tenants.read(uid, chat)
        if owner["error"] != "bot_left":
            raise Rejected("本群不是退群待恢复状态，请重新打开。")
        if name == "tenant_restore_save":
            await tenants.restore(uid, chat, data["version"])
            return await action(ui, update, {"action": "tenant_group", "chat": chat})
        return await ui.render(
            update,
            f"确认恢复 {await _chat_label(runtime, store, chat)} 的群服务？"
            "本群现任群主或管理员可确认；将实时核验操作者、经营群主及机器人身份。"
            "历史付款与订单保留；待核查投递、语义采集和定时管理不会自动恢复。",
            [
                (
                    "确认恢复",
                    {
                        "action": "tenant_restore_save",
                        "chat": chat,
                        "version": owner["version"],
                    },
                ),
                ("返回本群", {"action": "tenant_group", "chat": chat}),
            ],
        )
    if name.startswith("tenant_platform"):
        store.require(uid, owner=True)
        policy = tenants.policy()
        if name == "tenant_platform_preview":
            proposed = {**policy, data["field"]: data["value"]}
            if data["field"] not in POLICY:
                raise Rejected("设置不存在")
            return await ui.render(
                update,
                f"⚠️ **确认本BOT设置**\n{data['field']} → {data['value']}\n"
                "公开接入与真实付款单是独立开关。真实转账验收未完成前不要对公众开放。",
                [
                    (
                        "确认修改",
                        {
                            "action": "tenant_platform_save",
                            "old": policy,
                            "policy": proposed,
                        },
                    ),
                    ("返回", {"action": "tenant_platform"}),
                ],
            )
        if name == "tenant_platform_save":
            if data["old"] != policy:
                raise Rejected("本BOT设置已变化，请重新预览")
            tenants.configure_policy(uid, data["policy"])
            policy = tenants.policy()
        return await ui.render(
            update,
            "🏪 **接入与收款规则**\n"
            f"公开接入：{'开启' if policy['public'] else '关闭'}\n独立付款单：{'开启' if policy['invoices'] else '关闭'}\n"
            f"每群主上限：{policy['max_groups']}群\n测试群主：{', '.join(policy['pilot_owners']) or '未配置'}\n\n"
            "📌 上线条件\n权限隔离、模拟支付、真实到账分别验收；不开启公众入口不影响原有本BOT群。",
            [
                (
                    "公开接入开关",
                    {
                        "action": "tenant_platform_preview",
                        "field": "public",
                        "value": not policy["public"],
                    },
                ),
                (
                    "独立付款单开关",
                    {
                        "action": "tenant_platform_preview",
                        "field": "invoices",
                        "value": not policy["invoices"],
                    },
                ),
                ("测试群主名单", {"action": "tenant_edit", "field": "pilot_owners"}),
                ("接入数量上限", {"action": "tenant_edit", "field": "max_groups"}),
            ]
            + back,
            fold_sections=True,
        )
    if name in {
        "tenant_resources",
        "tenant_resource_edit",
        "tenant_resource_preview",
        "tenant_resource_save",
    }:
        store.require(uid, owner=True)
        if tenants.read(uid, chat)["owner"] != uid:
            raise Rejected("仅本群经营群主且具备总控权限可设置服务限额。")
        if name == "tenant_resource_edit":
            resource = data["resource"]
            if resource not in {"avatar", "chat"}:
                raise Rejected("服务不存在")
            store.dialog(
                uid,
                {
                    "form": "tenant_input",
                    "kind": "resource_budget",
                    "payload": {
                        "action": "tenant_resource_preview",
                        "chat": chat,
                        "resource": resource,
                    },
                },
            )
            return await ui.render(
                update,
                f"🔐 **设置本群付费{('制图' if resource == 'avatar' else 'AI客服')}预算**\n"
                "发送本群总调用上限（0—1000）。这是本BOT授权的次数上限，不是积分或金额；0表示停用。\n/cancel 取消。",
                back,
            )
        rows = {
            row["resource"]: dict(row)
            for row in store.db.execute(
                "SELECT * FROM tenant_resource_grants WHERE chat=?", (chat,)
            ).fetchall()
        }
        if name in {"tenant_resource_preview", "tenant_resource_save"}:
            resource = data["resource"]
            current = rows.get(resource)
            expected = current["version"] if current else 0
            if name == "tenant_resource_save":
                tenants.resource_grant(
                    uid, chat, resource, data["budget"], data["version"]
                )
                return await action(
                    ui, update, {"action": "tenant_resources", "chat": chat}
                )
            return await ui.render(
                update,
                f"🔐 **确认本BOT付费服务授权**\n群：{await _chat_label(runtime, store, chat)}\n服务：{'制图' if resource == 'avatar' else 'AI客服'}\n"
                f"总上限：{data['budget']} 次\n当前已用：{current['used'] if current else 0} 次\n"
                "保存后只影响后续请求；本BOT密钥不向群主开放，调用失败也计入已提交的调用预算。",
                [
                    (
                        "确认保存",
                        {
                            **data,
                            "action": "tenant_resource_save",
                            "version": expected,
                        },
                    ),
                    ("返回预算", {"action": "tenant_resources", "chat": chat}),
                ],
            )
        return await ui.render(
            update,
            f"🔐 **{chat} · 本BOT付费服务授权**\n"
            "AI客服和制图默认授权，复用总控内容；本页仅总控可设置显式限额，0表示禁用。未配置限额不代表关闭。\n"
            + "\n".join(
                f"{label}："
                + (
                    f"{'限额启用' if rows[key]['enabled'] else '显式停用'} · {rows[key]['used']}/{rows[key]['budget']}次"
                    if key in rows
                    else "默认授权（沿用总控安全额度）"
                )
                for key, label in (("avatar", "制图"), ("chat", "AI客服"))
            ),
            [
                (
                    "设置制图预算",
                    {
                        "action": "tenant_resource_edit",
                        "chat": chat,
                        "resource": "avatar",
                    },
                ),
                (
                    "设置AI客服预算",
                    {
                        "action": "tenant_resource_edit",
                        "chat": chat,
                        "resource": "chat",
                    },
                ),
            ]
            + back,
            fold_sections=True,
        )
    if name in {"tenant_catalog", "tenant_ad_search"}:
        store.dialog(uid, {"form": "tenant_input", "kind": "ad_target"})
        return await ui.render(
            update,
            "📢 **广告投递指定群聊**\n"
            "第1步：发送目标群的公开链接、@用户名、数字群ID或群名关键词。\n"
            "第2步：核对群名和经营者，选择广告位。\n"
            "第3步：按提示分别填写正文、联系方式，提交群主审核。\n"
            "审核通过且有空位后才生成付款单，款项直接进入该群收款地址。\n"
            "不充值、不存余额。已付款广告可申请修改，每次均需审核，不延长原展示期限。\n/cancel 取消。",
            [("我的广告订单", {"action": "tenant_my_ads"})] + back,
        )
    if name == "tenant_ad_matches":
        rows, page = await find_ad_groups(ui, data["query"], data.get("page", 0), uid)
        buttons = [
            (
                r["title"],
                {
                    "action": "tenant_ad_group",
                    "chat": r["chat"],
                    "version": r["version"],
                },
            )
            for r in rows[:8]
        ]
        if page:
            buttons.append(("上一页", {**data, "page": page - 1}))
        if len(rows) > 8:
            buttons.append(("下一页", {**data, "page": page + 1}))
        return await ui.render(
            update,
            "📢 **请选择目标群**\n请核对群名，下一步确认收款方。"
            if rows
            else "未找到可投递的广告位。该群可能尚未接入、已停用广告或尚未配置广告位／收款地址。\n请发送准确的群链接或群ID核实，或联系该群管理员。",
            buttons + [("重新搜索", {"action": "tenant_ad_search"})] + back,
        )
    if name in {"tenant_ad_group", "tenant_ad_packages"}:
        target_owner = binding(store, chat)
        test_target = (
            store.is_superadmin(uid)
            and target_owner is not None
            and target_owner["owner"] == uid
            and test_enabled(store, chat)
        )
        group = store.db.execute(
            "SELECT m.chat,m.title,g.version,t.owner "
            + (
                AD_GROUPS.replace(
                    "AND EXISTS(SELECT 1 FROM merchant_addresses a WHERE a.chat=g.chat) ",
                    "",
                )
                if test_target
                else AD_GROUPS
            )
            + "AND m.chat=?",
            (chat,),
        ).fetchone()
        if not group or group["version"] != data.get("version"):
            raise Rejected("群状态已变化或广告位未开放，请重新搜索确认。")
        address = merchant.address(chat)
        label = store.db.execute(
            "SELECT username FROM user_labels WHERE uid=?", (group["owner"],)
        ).fetchone()
        owner_label = (
            "@" + label["username"].lstrip("@")
            if label and label["username"]
            else "UID " + group["owner"]
        )
        if address is None:
            payment = "🧪 超管测试投递：免付款，当前无收款地址；只在本群测试开关有效期间可提交和发布。"
        else:
            payment = (
                f"收款网络：USDT-TRC20\n当前收款地址：{address['address']}\n\n"
                "款项直接支付给该群经营者；机器人不托管，退款由收款方处理。\n"
                "实际付款以审核后订单的地址、金额和有效期为准。"
            )
        text = (
            f"📢 **确认投放群**\n✅ 广告位已开放\n群名：{group['title']}\n群ID：{chat}\n"
            f"登记经营者：{owner_label}\n{payment}"
        )
        buttons = [("确认此群，选择广告位", {**data, "action": "tenant_ad_packages"})]
        if name == "tenant_ad_packages":
            packages = store.db.execute(
                "SELECT * FROM merchant_packages WHERE chat=? "
                "AND json_extract(config,'$.enabled')=1 ORDER BY id",
                (chat,),
            ).fetchall()
            buttons = [
                (
                    f"{json.loads(p['config'])['name']} · {json.loads(p['config'])['price']} USDT",
                    {"action": "tenant_buy", "id": p["id"]},
                )
                for p in packages
            ]
        return await ui.render(
            update,
            text,
            buttons + [("重新搜索", {"action": "tenant_ad_search"})] + back,
            fold_sections=True,
        )
    if name == "tenant_my_ads":
        rows = store.db.execute(
            "SELECT a.id,a.status,COALESCE(m.title,json_extract(a.package,'$.target')) AS title "
            "FROM ads a LEFT JOIN mod_groups m ON m.chat=json_extract(a.package,'$.target') "
            "WHERE a.uid=? AND a.tenant<>'platform' ORDER BY a.at DESC LIMIT 20",
            (uid,),
        ).fetchall()
        return await ui.render(
            update,
            "📋 **我的广告订单**\n显示最近20笔，按对应群保存；打开已付款订单可申请修改，审核通过才更新。",
            [
                (
                    f"{r['title']} · {r['id'][:10]} · {STATES.get(r['status'], r['status'])}",
                    {"action": "tenant_order", "id": r["id"]},
                )
                for r in rows
            ]
            + back,
        )
    if name == "tenant_buy":
        package = store.db.execute(
            "SELECT * FROM merchant_packages WHERE id=?", (data["id"],)
        ).fetchone()
        if not package or not json.loads(package["config"])["enabled"]:
            raise Rejected("广告位不可用")
        config = json.loads(package["config"])
        if not merchant.address(package["chat"]) and not (
            store.is_superadmin(uid)
            and binding(store, package["chat"])
            and binding(store, package["chat"])["owner"] == uid
            and test_enabled(store, package["chat"])
        ):
            raise Rejected("该群未设置收款地址，暂不能提交普通订单")
        store.dialog(
            uid,
            {
                "form": "tenant_input",
                "kind": "advertisement",
                "id": package["id"],
                "version": package["version"],
            },
        )
        price_text = (
            "🧪 超管测试免付款；仅测试开关有效期间可以发布。\n"
            if not merchant.address(package["chat"])
            else f"价格：{config['price']} USDT + 最多0.000999识别尾数\n"
        )
        return await ui.render(
            update,
            f"📢 **{config['name']}**\n群：{package['chat']}\n"
            + price_text
            + "第1/2步：只发送广告正文（1—2200字）。下一步单独填写联系方式。\n审核通过前不付款；/cancel 取消。",
            back,
        )
    if name == "tenant_submit":
        key = merchant.submit(
            uid,
            data["id"],
            data["body"],
            data["contact"],
            data["version"],
            "merchant:" + token,
        )
        return await action(ui, update, {"action": "tenant_order", "id": key})
    if name in {
        "tenant_revision_edit",
        "tenant_revision_submit",
        "tenant_revision_review",
    }:
        row = merchant.order(uid, data["id"], owner=name == "tenant_revision_review")
        if name == "tenant_revision_review":
            await merchant.review_revision(
                uid, row["id"], data["revision"], data["approve"]
            )
        elif name == "tenant_revision_submit":
            merchant.revision(
                uid,
                row["id"],
                data["body"],
                data["contact"],
                data["version"],
                "revision:" + token,
            )
        else:
            if row["uid"] != uid or not row["paid"]:
                raise Rejected("仅已付款广告的提交人可修改。")
            store.dialog(
                uid,
                {
                    "form": "tenant_input",
                    "kind": "ad_revision",
                    "id": row["id"],
                    "version": row["version"],
                },
            )
            return await ui.render(
                update,
                "📝 **申请修改广告**\n第1/2步：发送新的广告正文（1—2200字）。\n审核前原广告保持不变；不重新收费、不延长展示期限。/cancel 取消。",
                back,
            )
        return await action(ui, update, {"action": "tenant_order", "id": row["id"]})
    if name in {
        "tenant_order",
        "tenant_test_preview",
        "tenant_test_switch",
        "tenant_test_publish",
        "tenant_approve_preview",
        "tenant_approve",
        "tenant_reject",
        "tenant_refund_preview",
        "tenant_refund_save",
        "tenant_refund_proof",
        "tenant_arrange_preview",
        "tenant_arrange_save",
    }:
        row = merchant.order(uid, data["id"])
        package = json.loads(row["package"])
        chat = package["target"]
        owner = binding(store, chat)
        is_owner = uid == owner["owner"]
        invoice = store.db.execute(
            "SELECT * FROM merchant_invoices WHERE id=?", (row["id"],)
        ).fetchone()
        if name.startswith("tenant_test_"):
            if not store.is_superadmin(uid) or not is_owner:
                raise Rejected("测试投递仅本群经营群主且具备总控权限可用")
            await tenants.verify(
                uid,
                chat,
                "ads",
                "can_pin_messages" if package["kind"] == "pinned" else None,
            )
            if row["version"] != data["version"]:
                raise Rejected("订单已变化，请重新打开")
            if name == "tenant_test_switch":
                merchant.test_switch(uid, chat, data["enabled"])
                return await action(
                    ui, update, {"action": "tenant_order", "id": row["id"]}
                )
            if name == "tenant_test_publish":
                merchant.test_publish(uid, row["id"], data["version"])
                return await action(
                    ui, update, {"action": "tenant_order", "id": row["id"]}
                )
            enabled = test_enabled(store, chat)
            buttons = [
                (
                    "关闭本群测试" if enabled else "开启本群测试（1小时）",
                    {
                        "action": "tenant_test_switch",
                        "id": row["id"],
                        "version": row["version"],
                        "enabled": not enabled,
                    },
                ),
            ]
            if enabled and row["status"] == "merchant_review":
                buttons.append(
                    (
                        "确认免付款测试投递",
                        {
                            "action": "tenant_test_publish",
                            "id": row["id"],
                            "version": row["version"],
                        },
                    )
                )
            buttons.append(("返回订单", {"action": "tenant_order", "id": row["id"]}))
            return await ui.render(
                update,
                f"🧪 **测试投递 · 无真实付款**\n群：{await _chat_label(runtime, store, chat)}\n订单：{row['id']}\n"
                f"本群测试开关：{'开启' if enabled else '关闭'}\n\n"
                "仅超管可执行；开启1小时后自动失效。测试内容会标注“未付款”。\n"
                "使用正常发布及置顶队列，不创建链上到账、充值或退款记录。\n"
                "**仅在测试群开启；关闭后未发出的测试订单将停止投递。**",
                buttons,
                fold_sections=True,
            )
        if name in {
            "tenant_approve_preview",
            "tenant_approve",
            "tenant_reject",
            "tenant_refund_proof",
            "tenant_arrange_preview",
            "tenant_arrange_save",
        }:
            await tenants.verify(
                uid,
                chat,
                "ads",
                "can_pin_messages" if package["kind"] == "pinned" else None,
            )
            if name == "tenant_approve_preview":
                return await ui.render(
                    update,
                    f"✅ **确认审核并预留广告位**\n群：{await _chat_label(runtime, store, chat)}\n订单：{row['id']}\n{row['body']}\n\n"
                    "通过后生成30分钟付款单；没空位或监控故障不会要求客户付款。",
                    [
                        (
                            "通过并生成付款单",
                            {
                                "action": "tenant_approve",
                                "id": row["id"],
                                "version": row["version"],
                            },
                        ),
                        ("返回订单", {"action": "tenant_order", "id": row["id"]}),
                    ],
                    fold_sections=True,
                )
            if name == "tenant_arrange_preview":
                if (
                    not invoice
                    or invoice["status"] not in {"paid_waiting", "review"}
                    or not invoice["tx"]
                ):
                    raise Rejected("尚无可安排的到账订单")
                return await ui.render(
                    update,
                    f"📌 **确认安排已付款广告**\n订单：{row['id']}\n群：{await _chat_label(runtime, store, chat)}\n交易：{invoice['tx']}\n"
                    "不再次收款；沿用原内容、价格、地址及展示时长，重新检查空位后进入发布队列。",
                    [
                        (
                            "确认安排",
                            {
                                "action": "tenant_arrange_save",
                                "id": row["id"],
                                "version": invoice["version"],
                            },
                        ),
                        ("返回订单", {"action": "tenant_order", "id": row["id"]}),
                    ],
                )
            if name == "tenant_arrange_save":
                merchant.arrange(uid, row["id"], data["version"])
            if name == "tenant_approve":
                invoice = merchant.approve(uid, row["id"], data["version"])
            elif name == "tenant_reject":
                store.dialog(
                    uid,
                    {
                        "form": "tenant_input",
                        "kind": "reject",
                        "id": row["id"],
                        "version": row["version"],
                    },
                )
                return await ui.render(
                    update, "请填写驳回原因，下一步确认。/cancel 取消。", back
                )
            elif name == "tenant_refund_proof":
                store.dialog(
                    uid,
                    {
                        "form": "tenant_input",
                        "kind": "refund_tx",
                        "id": row["id"],
                        "version": invoice["version"],
                    },
                )
                return await ui.render(
                    update,
                    "请自行完成退款后填写交易哈希；机器人只核对凭证，不发送资金。/cancel 取消。",
                    back,
                )
        if name == "tenant_refund_preview":
            if row["uid"] != uid or not invoice:
                raise Rejected("仅付款人可确认退款地址")
            address_hex(data["address"])
            return await ui.render(
                update,
                f"↩️ **确认退款地址**\n{data['address']}\n退款金额：{money(invoice['amount'])} USDT\n"
                "请自行核实地址归属，不能默认使用交易所原转出地址。",
                [
                    (
                        "这是我的退款地址",
                        {
                            **data,
                            "action": "tenant_refund_save",
                            "version": invoice["version"],
                        },
                    ),
                    ("取消", {"action": "tenant_order", "id": row["id"]}),
                ],
            )
        if name == "tenant_refund_save":
            merchant.refund_request(uid, row["id"], data["address"], data["version"])
        row = merchant.order(uid, row["id"])
        invoice = store.db.execute(
            "SELECT * FROM merchant_invoices WHERE id=?", (row["id"],)
        ).fetchone()
        text = (
            f"📋 **直付广告订单**\n订单：{row['id']}\n当前群：{await _chat_label(runtime, store, chat)}\n收款方：本群经营者\n"
            f"审核：{'已通过' if row['approved'] else '未通过／待审核'}\n状态：{STATES.get(row['status'], row['status'])}\n"
            f"发布消息：{row['message'] or '尚未发布'}\n类型：{'置顶' if package['kind'] == 'pinned' else '普通广告'}\n"
            f"\n📝 广告内容\n{row['body']}\n联系方式：{row['contact']}"
        )
        if invoice:
            deadline = datetime.fromtimestamp(
                invoice["expires"], ZoneInfo("Asia/Shanghai")
            ).strftime("%m-%d %H:%M:%S")
            text += (
                f"\n\n💳 付款记录\n状态：{STATES.get(invoice['status'], invoice['status'])}\n"
                f"实际到账总额：**{money(invoice['amount'])} USDT**\n含识别尾数：{money(invoice['amount'] - invoice['base'])} USDT\n"
                f"网络：TRC20\n地址：{invoice['address']}\n有效至：{deadline} 北京时间\n"
                f"交易：{invoice['tx'] or '未确认'}\n{invoice['error']}"
            )
            if invoice["status"] != "pending" or invoice["expires"] <= store.clock():
                text += "\n**此付款单请勿继续转账。**"
            if invoice["refund_address"]:
                text += f"\n退款地址：{invoice['refund_address']}\n退款交易：{invoice['refund_tx'] or '待提交'}"
        text += "\n\n📌 责任说明\n资金直接进入群主地址；本BOT不托管、不代退款。到账以链上核验为准，显示失败不会重复收款。"
        buttons = [("刷新订单", {"action": "tenant_order", "id": row["id"]})]
        draft = store.db.execute(
            "SELECT * FROM merchant_revisions WHERE ad=? ORDER BY created DESC,rowid DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if draft:
            revision_labels = {
                "pending": "待审核",
                "applying": "更新中／待核查",
                "applied": "已应用",
                "rejected": "已驳回",
            }
            text += f"\n\n📝 最近修改：{revision_labels.get(draft['status'], draft['status'])}\n{draft['body']}\n联系方式：{draft['contact']}"
            if draft["status"] == "pending" and is_owner:
                buttons += [
                    (
                        "通过修改并更新",
                        {
                            "action": "tenant_revision_review",
                            "id": row["id"],
                            "revision": draft["id"],
                            "approve": True,
                        },
                    ),
                    (
                        "驳回修改",
                        {
                            "action": "tenant_revision_review",
                            "id": row["id"],
                            "revision": draft["id"],
                            "approve": False,
                        },
                    ),
                ]
        if (
            row["uid"] == uid
            and row["paid"]
            and row["status"] in {"pending", "active", "published"}
            and (not row["expires"] or row["expires"] > store.clock())
            and (not draft or draft["status"] not in {"pending", "applying"})
        ):
            buttons.append(
                ("申请修改广告", {"action": "tenant_revision_edit", "id": row["id"]})
            )
        buttons.append(("查看我的广告订单", {"action": "tenant_my_ads"}))
        if row["payment_source"] == "test":
            text += "\n\n🧪 **测试免付款订单：未发生真实到账，不可作为付款凭证。**"
        if store.is_superadmin(uid) and is_owner:
            buttons.append(
                (
                    "🧪 测试投递设置",
                    {
                        "action": "tenant_test_preview",
                        "id": row["id"],
                        "version": row["version"],
                    },
                )
            )
        if is_owner and row["status"] == "merchant_review":
            buttons += [
                ("审核通过", {"action": "tenant_approve_preview", "id": row["id"]}),
                ("驳回", {"action": "tenant_reject", "id": row["id"]}),
            ]
        if (
            invoice
            and invoice["tx"]
            and invoice["status"] != "refunded"
            and row["status"] in {"review", "pending", "merchant_expired"}
        ):
            if row["uid"] == uid:
                buttons.append(
                    (
                        "申请退款",
                        {
                            "action": "tenant_edit",
                            "field": "refund_address",
                            "id": row["id"],
                        },
                    )
                )
            if is_owner and invoice["status"] == "refund_requested":
                buttons.append(
                    ("核验退款交易", {"action": "tenant_refund_proof", "id": row["id"]})
                )
            if is_owner and invoice["status"] in {"paid_waiting", "review"}:
                buttons.append(
                    (
                        "安排已付款广告",
                        {"action": "tenant_arrange_preview", "id": row["id"]},
                    )
                )
        return await ui.render(update, text, buttons + back, fold_sections=True)
    if name == "tenant_reject_save":
        row = merchant.order(uid, data["id"], owner=True)
        await tenants.verify(uid, json.loads(row["package"])["target"], "ads")
        merchant.reject(uid, data["id"], data["version"], data["reason"])
        return await action(ui, update, {"action": "tenant_order", "id": data["id"]})
    if name == "tenant_refund_record":
        row = merchant.order(uid, data["id"], owner=True)
        await tenants.verify(uid, json.loads(row["package"])["target"], "ads")
        merchant.refund_proof(
            uid, data["id"], data["version"], data["index"], data["receipt"]
        )
        return await action(ui, update, {"action": "tenant_order", "id": data["id"]})
    if name == "tenant_edit":
        field = data["field"]
        if data.get("game"):
            back = [
                (
                    "返回本玩法",
                    {"action": "tenant_game", "chat": chat, "game": data["game"]},
                )
            ]
        if field in {"pilot_owners", "max_groups"}:
            store.require(uid, owner=True)
        elif field == "refund_address":
            order = merchant.order(uid, data["id"])
            if order["uid"] != uid:
                raise Rejected("仅付款人可填写退款地址")
        else:
            await tenants.verify(uid, chat)
        if field == "points":
            await tenants.verify(uid, chat, "points")
            store.dialog(
                uid,
                {"form": "tenant_gift", "payload": {"chat": chat}, "stage": "member"},
            )
            return await ui.render(
                update,
                "🎁 派送积分给群员 · 第1/3步\n发送群员的 @用户名 或 https://t.me/用户名。\n会先核实该成员，再选择 +积分 / -积分和数量，无需填写原因。\n如尚未识别该成员，请其先在目标群发一条消息。\n/cancel 取消。",
                back,
            )
        prompts = {
            "k3_limits": "将按4步设置：普通、特殊、号码、单期合计上限。每步只发送一个整数，仅对新期生效。",
            "canada_limits": "将按3步设置：最低下注、单笔、单期合计上限。每步只发送一个整数，仅影响后续新订单。",
            "slots_stakes": "将逐个选择老虎机档位：100、300、800、1500、2000。每次发送一个档位，最后发送“完成”。",
            "mines_stakes": "将逐个选择扫雷档位：100、300、800、1500、2000。每次发送一个档位，最后发送“完成”。",
            "address": "发送本群公开 TRC20 收款地址，勿发送私钥或助记词。",
            "rewards": "将按4步设置：签到奖励、聊天奖励、间隔秒数、每日上限。每步只发送一个整数。",
            "package": "第1/2步：发送广告位名称（可以包含空格）。下一步单独填写价格。",
            "pilot_owners": "每次发送一个测试群主数字UID，最后发送“完成”；发送“无”预览清空名单。",
            "max_groups": "发送每位群主接入上限，1—100。",
            "refund_address": "发送您确认可以接收退款的 TRC20 地址，不要照抄交易所原转出地址。",
        }
        if field not in prompts:
            raise Rejected("设置不存在")
        if field in STEP_FIELDS or field in STAKE_FIELDS:
            store.dialog(
                uid,
                {
                    "form": "tenant_input_steps",
                    "kind": field,
                    "payload": data,
                    "values": [],
                },
            )
            prompt = _step_prompt(field, 0, [])
        else:
            store.dialog(uid, {"form": "tenant_input", "kind": field, "payload": data})
            prompt = prompts[field]
        return await ui.render(
            update, prompt + "\n/cancel 取消；可随时重新打开设置。", back
        )
    if name in {
        "tenant_address_preview",
        "tenant_address_save",
        "tenant_rewards_preview",
        "tenant_rewards_save",
        "tenant_points_preview",
        "tenant_points_save",
        "tenant_package_preview",
        "tenant_package_save",
    }:
        await tenants.verify(uid, chat)
        if name.startswith("tenant_address"):
            address_hex(data["value"])
            current = merchant.address(chat)
            if name.endswith("save"):
                merchant.set_address(uid, chat, data["value"], data["version"])
                return await action(
                    ui, update, {"action": "tenant_address", "chat": chat}
                )
            return await ui.render(
                update,
                f"💳 **确认本群收款地址**\n群：{await _chat_label(runtime, store, chat)}\n{data['value']}\n\n只影响新付款单。格式校验不是钱包归属证明，请自行核实。",
                [
                    (
                        "确认地址归属无误",
                        {
                            **data,
                            "action": "tenant_address_save",
                            "version": current["version"] if current else 0,
                        },
                    )
                ]
                + back,
            )
        if name.startswith("tenant_rewards"):
            config = data["config"]
            validate(config)
            version = store.db.execute(
                "SELECT version FROM tenant_settings WHERE chat=? AND key='points'",
                (chat,),
            ).fetchone()
            if name.endswith("save"):
                tenants.settings(uid, chat, "points", config, data["version"])
                return await action(
                    ui, update, {"action": "tenant_rewards", "chat": chat}
                )
            return await ui.render(
                update,
                f"🎁 **确认本群奖励**\n签到{config['checkin']} · 聊天{config['chat']}\n间隔{config['interval']}秒 · 每日上限{config['cap']}\n只影响群{chat}，不补发历史奖励。",
                [
                    (
                        "确认保存",
                        {
                            **data,
                            "action": "tenant_rewards_save",
                            "version": version[0] if version else 0,
                        },
                    )
                ]
                + back,
            )
        if name.startswith("tenant_points"):
            raise Rejected(
                "旧积分调整按钮已失效，请在「积分与奖励 → 派送积分给群员」重新操作。"
            )
        config = data["config"]
        if name.endswith("save"):
            merchant.configure(uid, chat, data["id"], config, data["version"])
            return await action(ui, update, {"action": "tenant_ads", "chat": chat})
        return await ui.render(
            update,
            f"📢 **广告位预览**\n群：{await _chat_label(runtime, store, chat)}\n{config['name']} · {config['price']} USDT\n"
            f"类型：{'置顶' if config['kind'] == 'pinned' else '普通'} · 容量{config['slots']}\n时长：{'30天' if config['duration'] == '30days' else '一个自然月'}\n"
            f"状态：{'启用' if config['enabled'] else '停用'}\n先审核后付款，满位不收款。",
            [
                ("确认保存", {**data, "action": "tenant_package_save"}),
                (
                    "切换普通／置顶",
                    {
                        **data,
                        "config": {
                            **config,
                            "kind": "normal"
                            if config["kind"] == "pinned"
                            else "pinned",
                        },
                    },
                ),
                (
                    "切换30天／自然月",
                    {
                        **data,
                        "config": {
                            **config,
                            "duration": "month"
                            if config["duration"] == "30days"
                            else "30days",
                        },
                    },
                ),
                (
                    "切换启用／停用",
                    {**data, "config": {**config, "enabled": not config["enabled"]}},
                ),
            ]
            + [
                (f"容量{x}", {**data, "config": {**config, "slots": x}})
                for x in (1, 2, 3, 5, 10)
            ]
            + back,
        )
    if name in {"tenant_operating_preview", "tenant_operating_save"}:
        await tenants.verify(uid, chat, "game")
        current = store.db.execute(
            "SELECT version FROM tenant_settings WHERE chat=? AND key=?",
            (chat, data["key"]),
        ).fetchone()
        if name == "tenant_operating_save":
            tenants.operating(uid, chat, data["key"], data["value"], data["version"])
            return await action(
                ui,
                update,
                {
                    "action": "tenant_game",
                    "chat": chat,
                    "game": data.get("game") or data["key"].split("_")[0],
                },
            )
        return await ui.render(
            update,
            f"🎮 **确认本群运营参数**\n群：{await _chat_label(runtime, store, chat)}\n{data['label']}\n{data['value']}\n\n赔率、概率、抽水不变；已有桌次／期次保留原快照。",
            [
                (
                    "确认保存",
                    {
                        **data,
                        "action": "tenant_operating_save",
                        "version": current[0] if current else 0,
                    },
                ),
                (
                    "返回本玩法",
                    {
                        "action": "tenant_game",
                        "chat": chat,
                        "game": data.get("game") or data["key"].split("_")[0],
                    },
                ),
            ],
            fold_sections=True,
        )
    if not chat:
        raise Rejected("请先选择自己的群")
    owner = tenants.read(uid, chat)
    if name not in {
        "tenant_group",
        "tenant_switches",
        "tenant_rewards",
        "tenant_games",
        "tenant_game",
        "tenant_ads",
        "tenant_errors",
        "tenant_address",
        "tenant_resources",
    }:
        owner = await tenants.verify(uid, chat)
    group = store.db.execute(
        "SELECT * FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()
    if name in {"tenant_switch_preview", "tenant_switch_save"}:
        if data.get("feature") not in FEATURES:
            raise Rejected(
                "此开关已移出【功能启停】，旧按钮不能执行。本页仅控制娱乐、AI客服和制图。"
            )
        if name.endswith("save"):
            tenants.switch(uid, chat, data["feature"], data["enabled"], data["version"])
            return await action(ui, update, {"action": "tenant_switches", "chat": chat})
        return await ui.render(
            update,
            f"⚙️ **确认本群开关**\n{group['title']}\n{FEATURES[data['feature']]} → {'开启' if data['enabled'] else '关闭'}\n"
            "不改本BOT总开关、不改其他群；已受理订单按原规则收尾。",
            [
                (
                    "确认",
                    {
                        **data,
                        "action": "tenant_switch_save",
                        "version": owner["version"],
                    },
                ),
                ("返回本群开关", {"action": "tenant_switches", "chat": chat}),
            ],
        )
    if name == "tenant_group":
        from .availability import refresh

        permissions = await refresh(runtime, chat)
        try:
            info = await runtime.bot.get_chat(chat)
        except Exception:
            info = None
        actor_member = await runtime.bot.get_chat_member(chat, int(uid))
        registered_member = await runtime.bot.get_chat_member(chat, int(owner["owner"]))
        mismatch = registered_member.status != "creator"
        reason = {
            "bot_left": "机器人曾退群，重新加入后需本群群主或管理员确认恢复。",
            "ownership_or_bot_rights": "群主身份或机器人权限发生变化，需核查。",
            "platform_suspended": "本BOT已暂停本群服务，需核查后恢复。",
        }.get(owner["error"], "")
        return await ui.render(
            update,
            f"🏪 **正在管理：{group_label(info, group['title'])}**\n以下修改仅对本群生效。\n本群服务：{'开启' if group['enabled'] else '关闭'}\n"
            f"经营权限：{'正常' if owner['status'] == 'active' else '暂停待本BOT核查'}\n{owner['error']}\n"
            + (f"停用原因：{reason}\n" if reason else "")
            + (
                "经营归属待核实：登记账号不是现任群主，请本群群主或管理员确认归属；已有财务配置须人工核查。\n"
                if mismatch
                else ""
            )
            + f"入口权限：{permissions['reason'] or '已核实（操作时仍会复查）'}\n"
            "积分与订单按群独立。AI客服／制图默认授权，统一使用总控内容；赔率、概率和抽水由本BOT统一控制。",
            [
                (label, {"action": key, "chat": chat})
                for label, key in [
                    ("群管", "semantic_group_menu"),
                    ("功能启停", "tenant_switches"),
                    ("积分与奖励", "tenant_rewards"),
                    ("玩法设置", "tenant_games"),
                ]
                + (
                    [
                        ("广告位与订单", "tenant_ads"),
                        ("收款设置", "tenant_address"),
                        ("本群业务联系", "support_contact_edit"),
                        ("异常记录", "tenant_errors"),
                    ]
                    if uid == owner["owner"]
                    else []
                )
            ]
            + (
                [("服务限额（仅总控）", {"action": "tenant_resources", "chat": chat})]
                if store.is_superadmin(uid) and uid == owner["owner"]
                else []
            )
            + (
                [
                    (
                        "本BOT暂停／恢复",
                        {
                            "action": "tenant_status_preview",
                            "chat": chat,
                            "active": owner["status"] != "active",
                        },
                    )
                ]
                if store.is_superadmin(uid) and uid == owner["owner"]
                else []
            )
            + (
                [("确认经营归属", {"action": "tenant_owner_preview", "chat": chat})]
                if owner["mode"] == "auto"
                and actor_member.status in {"creator", "administrator"}
                and mismatch
                else []
            )
            + (
                [("确认重新接入", {"action": "tenant_restore_preview", "chat": chat})]
                if actor_member.status in {"creator", "administrator"}
                and owner["error"] == "bot_left"
                and not mismatch
                else []
            )
            + back,
            fold_sections=True,
        )
    if name == "tenant_switches":
        modules = store.get("modules", {})
        lines = []
        buttons = []
        for feature, label in FEATURES.items():
            if feature == "canada":
                enabled = local_config(store, chat, "canada_enabled", False)
                permitted = modules.get("game", False)
            elif feature in {"chat", "avatar"}:
                enabled = local_config(store, chat, feature + "_enabled", True)
                grant = store.db.execute(
                    "SELECT enabled,budget,used FROM tenant_resource_grants WHERE chat=? AND resource=?",
                    (chat, feature),
                ).fetchone()
                permitted = not grant or bool(
                    grant["enabled"] and grant["used"] < grant["budget"]
                )
            elif feature == "wheel":
                enabled = runtime.wheel.config(chat)[0]["enabled"]
                permitted = modules.get("game") and modules.get(feature)
            else:
                state = store.db.execute(
                    f"SELECT enabled FROM {feature}_groups WHERE chat=?", (chat,)
                ).fetchone()
                enabled = bool(state and state[0])
                permitted = modules.get("game") and modules.get(
                    feature, feature == "duel"
                )
            lines.append(
                f"{label}：{'🟢 已开启' if enabled and permitted else '🔴 已关闭' if not enabled else '🔴 暂不可用'}"
            )
            buttons.append(
                (
                    f"{'关闭' if enabled else '开启'} · {label}",
                    {
                        "action": "tenant_switch_preview",
                        "chat": chat,
                        "feature": feature,
                        "enabled": not enabled,
                    },
                )
            )
        buttons.append(("🕒 每日娱乐时段", {"action": "schedule_home", "chat": chat}))
        return await ui.render(
            update,
            f"⚙️ **{group['title']} · 功能状态**\n"
            + "\n".join(lines)
            + "\n\n仅控制六项娱乐、AI客服、制图；每日时段仅控制娱乐。\nAI客服和制图默认授权开启，统一使用总控模型、知识与模板，群内不可定制。\n总控停用、显式限制、供应商配置及全局安全额度仍生效。",
            buttons + [("返回上一级", {"action": "tenant_group", "chat": chat})],
            fold_sections=True,
        )
    if name == "tenant_rewards":
        config = local_config(store, chat, "points", DEFAULT)
        return await ui.render(
            update,
            f"🎁 **{group['title']} · 积分与奖励**\n签到：{config['checkin']}\n有效聊天：{config['chat']}\n间隔：{config['interval']}秒\n每日聊天上限：{config['cap']}\n"
            "本群钱包独立，广告USDT不会兑换成游戏积分。",
            [
                (
                    "设置奖励",
                    {"action": "tenant_edit", "chat": chat, "field": "rewards"},
                ),
                (
                    "派送积分给群员",
                    {"action": "tenant_edit", "chat": chat, "field": "points"},
                ),
            ]
            + back,
        )
    if name == "tenant_games":
        store.clear_dialog(uid)
        return await ui.render(
            update,
            f"🎮 **{group['title']} · 玩法设置**\n只修改本群运营参数；固定赔率、概率及抽水不可修改。\n开关统一放在本群功能启停。",
            [
                (FEATURES[game], {"action": "tenant_game", "chat": chat, "game": game})
                for game in ENTERTAINMENT
            ]
            + [
                ("返回本群管理", {"action": "tenant_group", "chat": chat}),
                ("返回选群", {"action": "tenant_home"}),
            ],
        )
    if name == "tenant_game":
        game = data.get("game")
        if game not in ENTERTAINMENT:
            raise Rejected("玩法不存在，请重新选择。")
        await tenants.verify(uid, chat, "game")
        store.clear_dialog(uid)
        if game in {"k3", "wheel", "slots", "mines"}:
            return await ui.admin(update, {"action": game + "_group", "chat": chat}, "")
        buttons = []
        if game == "canada":
            room = runtime.group_game.group_room(chat)
            config = runtime.game.rooms(chat=chat)[room]
            text = f"🍁 **{group['title']} · 加拿大28设置**\n最低下注：{config['minimum']}\n单笔上限：{config['maximum']}\n单期上限：{config['total']}\n固定赔率不可修改，已受理订单保留原规则。"
            buttons.append(
                (
                    "本群下注限额",
                    {
                        "action": "tenant_edit",
                        "chat": chat,
                        "field": "canada_limits",
                        "game": game,
                    },
                )
            )
        else:
            rows = store.db.execute(
                "SELECT id,status FROM duels WHERE chat=? ORDER BY id DESC LIMIT 8",
                (chat,),
            ).fetchall()
            text = (
                f"🤝 **{group['title']} · 双人对赌**\n对赌不扣积分，固定玩法规则不可调整，机器人不担保双方彩头。\n当前无可自定义参数。\n\n近期场次：\n"
                + ("\n".join(f"#{r['id']} · {r['status']}" for r in rows) or "暂无")
            )
        return await ui.render(
            update,
            text + "\n\n开启、关闭及每日时段请到【功能启停】。",
            buttons
            + [
                ("返回玩法设置", {"action": "tenant_games", "chat": chat}),
                ("返回选群", {"action": "tenant_home"}),
            ],
        )
    if name in {"tenant_ads", "tenant_errors"}:
        rows = store.db.execute(
            "SELECT id,status,error,EXISTS(SELECT 1 FROM merchant_revisions r WHERE r.ad=ads.id AND r.status='pending') AS revision_pending "
            "FROM ads WHERE tenant=? AND json_extract(package,'$.target')=? ORDER BY at DESC LIMIT 20",
            (owner["tenant"], chat),
        ).fetchall()
        if name == "tenant_errors":
            rows = [r for r in rows if r["status"] == "review" or r["error"]]
        unassigned = []
        if name == "tenant_errors":
            unassigned = store.db.execute(
                "SELECT DISTINCT e.tx,e.amount,e.error FROM merchant_events e JOIN merchant_addresses a ON a.address=e.address "
                "WHERE a.tenant=? AND e.status='review' AND e.invoice='' ORDER BY e.stamp DESC LIMIT 10",
                (owner["tenant"],),
            ).fetchall()
        buttons = [
            (
                f"{r['id'][:10]} · {'📝 修改待审核' if r['revision_pending'] else STATES.get(r['status'], r['status'])}",
                {"action": "tenant_order", "id": r["id"]},
            )
            for r in rows
        ]
        packages = store.db.execute(
            "SELECT * FROM merchant_packages WHERE tenant=? AND chat=?",
            (owner["tenant"], chat),
        ).fetchall()
        if name == "tenant_ads":
            buttons += [
                (
                    json.loads(p["config"])["name"],
                    {
                        "action": "tenant_package_preview",
                        "chat": chat,
                        "id": p["id"],
                        "version": p["version"],
                        "config": json.loads(p["config"]),
                    },
                )
                for p in packages
            ]
            buttons.append(
                (
                    "新增广告位",
                    {"action": "tenant_edit", "chat": chat, "field": "package"},
                )
            )
        return await ui.render(
            update,
            f"📋 **{group['title']} · {'异常核查' if name == 'tenant_errors' else '广告位与订单'}**\n"
            + (
                "\n".join(
                    f"{r['id'][:10]} · {'修改待审核' if r['revision_pending'] else STATES.get(r['status'], r['status'])} {r['error']}"
                    for r in rows
                )
                or "暂无订单异常"
            )
            + (
                "\n\n🔎 本经营者地址未匹配到账\n同一地址可属于多个本人的群，以下记录尚未分配到具体群，不会自动发布。\n"
                + "\n".join(
                    f"{money(r['amount'])} USDT · {r['tx']}\n{r['error']}"
                    for r in unassigned
                )
                if unassigned
                else ""
            ),
            buttons + back,
            fold_sections=True,
        )
    if name == "tenant_address":
        address = merchant.address(chat)
        health = (
            store.db.execute(
                "SELECT * FROM merchant_scans WHERE address=?", (address["address"],)
            ).fetchone()
            if address
            else None
        )
        return await ui.render(
            update,
            f"💳 **{group['title']} · 收款设置**\nUSDT-TRC20\n地址：{address['address'] if address else '未设置'}\n"
            f"地址版本：{address['version'] if address else 0}\n监控：{(health['error'] or ('正常' if store.clock() - health['at'] <= 180 else '同步落后，暂停新付款单')) if health else '未就绪'}\n"
            "更换只影响新付款单；资金进入您自己的地址，退款由您处理。不要提交任何私钥或助记词。",
            [
                (
                    "设置／更换地址",
                    {"action": "tenant_edit", "chat": chat, "field": "address"},
                )
            ]
            + back,
            fold_sections=True,
        )
    raise Rejected("页面已更新，请从我的群重新进入")


async def _input_steps(ui, update, dialog, text):
    """Collect one group-setting value per message and then preview it."""
    uid = str(update.effective_user.id)
    kind = dialog["kind"]
    payload = dialog.get("payload", {})
    cancel = (
        [
            (
                "取消",
                {
                    "action": "tenant_game",
                    "chat": payload["chat"],
                    "game": payload["game"],
                },
            )
        ]
        if payload.get("game")
        else [("取消", {"action": "tenant_home"})]
    )
    if kind in {"advertisement", "ad_revision"}:
        if text == "上一步":
            ui.store.dialog(uid, {k: v for k, v in dialog.items() if k != "body"})
            return await ui.render(
                update, "第1/2步：重新发送广告正文（1—2200字）。/cancel 取消。"
            )
        if "body" not in dialog:
            if not 1 <= len(text) <= 2200:
                raise Rejected("正文须为1—2200字，请重新发送；草稿尚未提交。")
            ui.store.dialog(uid, {**dialog, "body": text})
            return await ui.render(
                update,
                "第2/2步：发送联系方式（1—200字）。\n“上一步”修改正文；/cancel 取消。",
            )
        if not 1 <= len(text) <= 200:
            raise Rejected("联系方式须为1—200字，请重新发送。")
        ui.store.clear_dialog(uid)
        package = ui.store.db.execute(
            "SELECT chat FROM merchant_packages WHERE id=?", (dialog["id"],)
        ).fetchone()
        unpaid_test = (
            kind == "advertisement"
            and package is not None
            and ui.store.is_superadmin(uid)
            and binding(ui.store, package["chat"])
            and binding(ui.store, package["chat"])["owner"] == uid
            and test_enabled(ui.store, package["chat"])
            and not ui.runtime.merchant_ads.address(package["chat"])
        )
        return await ui.render(
            update,
            f"📝 **确认提交审核**\n{dialog['body']}\n联系方式：{text}\n\n"
            + (
                "修改审核通过后才替换原内容；不重复收费，不延长原展示期限。"
                if kind == "ad_revision"
                else (
                    "🧪 本群仅供超管测试：无收款地址，不生成真实付款单。"
                    "提交后可在订单中选择免付款测试投递。"
                    if unpaid_test
                    else "此时不付款，通过审核后领取付款单。"
                )
            ),
            [
                (
                    "提交审核",
                    {
                        "action": "tenant_revision_submit"
                        if kind == "ad_revision"
                        else "tenant_submit",
                        "id": dialog["id"],
                        "version": dialog["version"],
                        "body": dialog["body"],
                        "contact": text,
                    },
                )
            ],
            fold_sections=True,
        )
    if kind == "ad_target":
        result = await action(
            ui, update, {"action": "tenant_ad_matches", "query": text}
        )
        ui.store.clear_dialog(uid)
        return result
    values = list(dialog.get("values", []))

    if text.casefold() in {"完成", "done"} and kind in STAKE_FIELDS:
        if not values:
            raise Rejected("至少选择一个档位，再发送“完成”。")
        ui.store.clear_dialog(uid)
        return await action(
            ui,
            update,
            {
                "action": "tenant_operating_preview",
                "chat": payload["chat"],
                "key": kind,
                "value": values,
                "label": "本群" + kind,
                **({"game": payload["game"]} if payload.get("game") else {}),
            },
        )
    if text.casefold() in {"完成", "done"}:
        raise Rejected("该设置仍有未完成的步骤，请按提示发送下一个值。")

    if kind in STAKE_FIELDS:
        if not text.isascii() or not text.isdigit() or int(text) not in STAKE_OPTIONS:
            raise Rejected("档位只能填写一个：100、300、800、1500 或 2000。")
        value = int(text)
        if value in values:
            raise Rejected("这个档位已选择，请换一个或发送“完成”。")
        values.append(value)
        ui.store.dialog(uid, {**dialog, "values": values})
        return await ui.render(
            update,
            _step_prompt(kind, 0, values),
            cancel,
        )

    index = len(values)
    fields = STEP_FIELDS[kind]
    if index >= len(fields):
        raise Rejected("设置步骤已完成，请重新打开页面。")
    label = fields[index][1]
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise Rejected(f"{label}请只发送一个整数。") from exc
    if value < 0:
        raise Rejected(f"{label}不能为负数。")
    if kind == "rewards":
        low, high = (15, 86400) if index == 2 else (0, 1000000)
        if not low <= value <= high:
            raise Rejected(
                f"{label}须为{low}—{high}的整数，请重新填写本项；此前输入已保留。"
            )

    values.append(value)
    if len(values) < len(fields):
        ui.store.dialog(uid, {**dialog, "values": values})
        return await ui.render(
            update,
            _step_prompt(kind, len(values), values),
            cancel,
        )

    ui.store.clear_dialog(uid)
    data = {"chat": payload["chat"]}
    if payload.get("game"):
        data["game"] = payload["game"]
    if kind == "rewards":
        checkin, chat, interval, cap = values
        data.update(
            action="tenant_rewards_preview",
            config={
                **DEFAULT,
                "enabled": True,
                "checkin": checkin,
                "chat": chat,
                "interval": interval,
                "cap": cap,
                "groups": [payload["chat"]],
            },
        )
    else:
        keys = tuple(field for field, _ in fields)
        config = dict(zip(keys, values, strict=True))
        key = kind
        if kind == "canada_limits":
            room = ui.runtime.group_game.group_room(payload["chat"])
            key += ":" + room
        data.update(
            action="tenant_operating_preview",
            key=key,
            value=config,
            label="本群" + kind,
        )
    return await action(ui, update, data)


async def gift_input(ui, update, dialog):
    """Collect a verified member and signed amount without writing a ledger.

    Args:
        ui: Private UI.
        update: Authenticated private text update.
        dialog: Server-stored gift state.
    """
    from .tenants import group_admin

    if update.effective_chat.type != "private":
        raise Rejected("请私聊打开派送积分给群员。")
    uid = str(update.effective_user.id)
    payload = dialog["payload"]
    chat = payload["chat"]
    text = (update.message.text or "").strip()
    async with group_admin(ui.runtime, uid, chat):
        await ui.runtime.tenants.verify(uid, chat, "points")
        if dialog["stage"] == "member":
            match = re.fullmatch(
                r"(?:@|https?://t\.me/|t\.me/)([A-Za-z][A-Za-z0-9_]{3,31})/?", text
            )
            if not match:
                raise Rejected(
                    "请发送 @用户名 或 https://t.me/用户名，不接受数字UID、群链接或邀请链接。"
                )
            member = await gift_member(ui, chat, match[1])
            data = {
                "action": "tenant_gift_direction",
                "chat": chat,
                "target": str(member.id),
                "username": member.username,
                "op": secrets.token_hex(16),
            }
            ui.store.clear_dialog(uid)
            return await ui.render(
                update,
                f"🎁 派送积分给群员 · 第2/3步\n✅ 已核实本群成员\n{member.full_name} (@{member.username})\n请选择加减方向：",
                [
                    ("+积分", {**data, "sign": 1}),
                    ("-积分", {**data, "sign": -1}),
                ],
            )
        if not re.fullmatch(r"[+-]?[0-9]+", text):
            raise Rejected("请只发送整数数量，例如100或-100（1—1000000）。")
        if (text.startswith("-") and payload["sign"] == 1) or (
            text.startswith("+") and payload["sign"] == -1
        ):
            raise Rejected("数量符号与所选加减方向不符，请重新填写。")
        amount = abs(int(text)) * payload["sign"]
        result = await action(
            ui, update, {**payload, "action": "tenant_gift_preview", "amount": amount}
        )
        ui.store.clear_dialog(uid)
        return result


async def input_text(ui, update, dialog):
    """Parse owner input into a preview, never directly approve financial edits.

    Args:
        ui: Existing UI.
        update: Authenticated private text update.
        dialog: User-bound server-side form state.
    """
    from .tenants import group_admin

    payload = dialog.get("payload", {})
    chat = str(payload.get("chat", ""))
    if not chat and dialog.get("kind") in {"reject", "refund_tx"}:
        order = ui.store.db.execute(
            "SELECT package FROM ads WHERE id=? AND tenant<>'platform'",
            (dialog["id"],),
        ).fetchone()
        if order:
            chat = str(json.loads(order["package"])["target"])
    if chat and binding(ui.store, chat):
        async with group_admin(ui.runtime, str(update.effective_user.id), chat):
            return await _input_text(ui, update, dialog)
    return await _input_text(ui, update, dialog)


async def _input_text(ui, update, dialog):
    uid = str(update.effective_user.id)
    text = (update.message.text or "").strip()
    kind = dialog["kind"]
    if kind == "points":
        raise Rejected(
            "积分派送流程已升级，请重新打开「派送积分给群员」，发送 @用户名或链接。"
        )
    payload = dialog.get("payload", {})
    if kind in {"package", "pilot_owners"}:
        if kind == "package":
            await ui.runtime.tenants.verify(uid, str(payload["chat"]))
        else:
            ui.store.require(uid, owner=True)
        values = list(dialog.get("values", []))
        if text == "上一步":
            values = values[:-1]
            ui.store.dialog(uid, {**dialog, "values": values})
            return await ui.render(
                update,
                "请重新发送广告位名称。"
                if kind == "package"
                else f"已选择 {len(values)} 人；继续发送一个UID，或发送“完成”。",
            )
        if kind == "pilot_owners":
            if text in {"完成", "无"}:
                proposed = [] if text == "无" else values
                if text == "完成" and not proposed:
                    raise Rejected("请先填写一个UID；清空名单请发送“无”。")
                result = await action(
                    ui,
                    update,
                    {
                        "action": "tenant_platform_preview",
                        "field": kind,
                        "value": proposed,
                    },
                )
                ui.store.clear_dialog(uid)
                return result
            if not text.isascii() or not text.isdigit() or int(text) <= 0:
                raise Rejected("每次只发送一个正整数UID。")
            value = str(int(text))
            if value in values:
                raise Rejected("该UID已添加；可继续添加或发送“完成”。")
            if len(values) >= 100:
                raise Rejected("最多100人，请发送“完成”。")
            values.append(value)
            ui.store.dialog(uid, {**dialog, "values": values})
            return await ui.render(
                update,
                f"已添加 {len(values)} 人。\n继续发送一个UID；“上一步”撤销最后一人，“完成”预览，/cancel 取消。",
            )
        if not values:
            if not 1 <= len(text) <= 80:
                raise Rejected("广告位名称需为1—80字。")
            ui.store.dialog(uid, {**dialog, "values": [text]})
            return await ui.render(
                update,
                f"第2/2步：广告位「{text}」\n请单独发送USDT价格，最多两位小数、大于0且不超过1000000。\n发送“上一步”修改名称，/cancel 取消。",
            )
        try:
            price = Decimal(text)
            if (
                not price.is_finite()
                or not 0 < price <= 1000000
                or price.as_tuple().exponent < -2
            ):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise Rejected("请只发送有效价格，例如10或10.50，最多两位小数。") from None
        result = await action(
            ui,
            update,
            {
                "action": "tenant_package_preview",
                "chat": payload["chat"],
                "id": "mp_" + secrets.token_hex(8),
                "version": 0,
                "config": {
                    "name": values[0],
                    "price": str(price),
                    "kind": "normal",
                    "duration": "30days",
                    "slots": 1,
                    "enabled": True,
                },
            },
        )
        ui.store.clear_dialog(uid)
        return result
    if dialog.get("form") == "tenant_input_steps" or kind in {
        "ad_target",
        "advertisement",
        "ad_revision",
    }:
        try:
            return await _input_steps(ui, update, dialog, text)
        except Rejected:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise Rejected(
                "设置未保存，请按当前提示输入一个值；可发送 /cancel 取消。"
            ) from exc
    ui.store.clear_dialog(uid)
    try:
        if kind == "business_contact":
            from .support_menu import contact_url

            value = contact_url(text)
            return await ui.render(
                update,
                f"确认设置本群业务联系人：{value}\n仅对所选群生效。",
                [
                    (
                        "确认保存",
                        {**payload, "action": "support_contact_save", "value": value},
                    )
                ],
            )
        if kind == "reject":
            return await ui.render(
                update,
                f"确认驳回？\n原因：{text}",
                [
                    (
                        "确认驳回",
                        {
                            "action": "tenant_reject_save",
                            "id": dialog["id"],
                            "version": dialog["version"],
                            "reason": text,
                        },
                    )
                ],
            )
        if kind == "refund_tx":
            return await ui.runtime.merchant_ads.verify_refund_input(
                ui, update, dialog, text
            )
        if kind in {"pilot_owners", "max_groups"}:
            value = (
                ([str(int(x)) for x in text.split()] if text != "无" else [])
                if kind == "pilot_owners"
                else int(text)
            )
            return await action(
                ui,
                update,
                {"action": "tenant_platform_preview", "field": kind, "value": value},
            )
        if kind == "resource_budget":
            if not text.isascii() or not text.isdigit():
                raise Rejected("请发送0—1000之间的整数")
            value = int(text)
            if value > 1000:
                raise Rejected("预算上限为1000次")
            return await action(
                ui,
                update,
                {
                    **payload,
                    "budget": value,
                },
            )
        if kind == "refund_address":
            return await action(
                ui,
                update,
                {
                    "action": "tenant_refund_preview",
                    "id": payload["id"],
                    "address": text,
                },
            )
        if kind in {"k3_limits", "canada_limits", "slots_stakes", "mines_stakes"}:
            numbers = list(map(int, text.split()))
            key = kind
            if kind == "k3_limits":
                if len(numbers) != 4:
                    raise Rejected("需要4个整数：普通、特殊、号码、单期上限")
                value = dict(
                    zip(
                        ("ordinary", "special", "number", "total"), numbers, strict=True
                    )
                )
            elif kind == "canada_limits":
                if len(numbers) != 3:
                    raise Rejected("需要3个整数：最低、单笔、单期上限")
                room = ui.runtime.group_game.group_room(payload["chat"])
                key += ":" + room
                value = dict(zip(("minimum", "maximum", "total"), numbers, strict=True))
            else:
                value = numbers
            return await action(
                ui,
                update,
                {
                    "action": "tenant_operating_preview",
                    "chat": payload["chat"],
                    "key": key,
                    "value": value,
                    "label": "本群" + kind,
                },
            )
        data = {"chat": payload["chat"]}
        if kind == "address":
            data.update(action="tenant_address_preview", value=text)
        elif kind == "rewards":
            checkin, chat, interval, cap = map(int, text.split())
            data.update(
                action="tenant_rewards_preview",
                config={
                    **DEFAULT,
                    "enabled": True,
                    "checkin": checkin,
                    "chat": chat,
                    "interval": interval,
                    "cap": cap,
                    "groups": [payload["chat"]],
                },
            )
        else:
            raise Rejected("表单已失效")
        return await action(ui, update, data)
    except (ValueError, TypeError) as exc:
        if isinstance(exc, Rejected):
            raise
        raise Rejected("格式不正确，请重新打开该设置并按示例填写。") from exc
