"""Private, user-bound menus for independent owners and their advertisers."""

import json
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from .payments import address_hex, money
from .points import DEFAULT, validate
from .store import Rejected
from .tenants import POLICY, binding, local_config

FEATURES = {
    "group": "本群服务",
    "ads": "广告投放",
    "canada": "加拿大28",
    "k3": "积分快三",
    "duel": "双人对赌",
    "wheel": "积分转盘",
    "slots": "老虎机PvP",
    "mines": "扫雷接龙",
}
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


async def action(ui, update, data, token=""):
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
    if name == "tenant_home":
        rows = store.db.execute(
            "SELECT g.chat,g.status,m.title FROM tenant_groups g JOIN tenants t ON t.id=g.tenant "
            "JOIN mod_groups m ON m.chat=g.chat WHERE g.tenant<>'platform' AND (t.owner=? OR ?) ORDER BY m.title,g.chat",
            (uid, uid == store.owner),
        ).fetchall()
        buttons = [
            (r["title"], {"action": "tenant_group", "chat": r["chat"]}) for r in rows
        ]
        buttons += [
            ("接入群", {"action": "tenant_join"}),
            ("我的直付广告", {"action": "tenant_my_ads"}),
        ]
        if uid == store.owner:
            buttons.append(("平台接入总控", {"action": "tenant_platform"}))
        return await ui.render(
            update,
            "🏪 **我的群**\n每群独立配置、积分和广告订单。\n"
            + (
                "\n".join(
                    f"{r['title']} · {'可管理' if r['status'] == 'active' else '暂停待核查'}"
                    for r in rows
                )
                or "尚未接入群。"
            )
            + "\n\n📌 权限边界\n只能管理自己的群；平台总开关、授权和赔率不在本页修改。",
            buttons,
            fold_sections=True,
        )
    if name == "tenant_join":
        challenge = tenants.invite(uid)
        return await ui.render(
            update,
            "🔗 **接入自己的超级群**\n将机器人加入群并设为管理员，然后由真实群主在目标群发送：\n"
            f"/bindgroup {challenge}\n\n凭证10分钟有效。绑定后功能默认关闭，请回到这里配置。",
            back,
        )
    if name in {"tenant_status_preview", "tenant_status_save"}:
        store.require(uid, owner=True)
        owner = tenants.read(uid, chat)
        if name.endswith("save"):
            await tenants.status(uid, chat, data["active"], data["version"])
            return await action(ui, update, {"action": "tenant_group", "chat": chat})
        return await ui.render(
            update,
            f"🛡 **确认{'恢复' if data['active'] else '暂停'}本群经营权限**\n群：{chat}\n原经营者：{owner['owner']}\n"
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
    if name.startswith("tenant_platform"):
        store.require(uid, owner=True)
        policy = tenants.policy()
        if name == "tenant_platform_preview":
            proposed = {**policy, data["field"]: data["value"]}
            if data["field"] not in POLICY:
                raise Rejected("设置不存在")
            return await ui.render(
                update,
                f"⚠️ **确认平台设置**\n{data['field']} → {data['value']}\n"
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
                raise Rejected("平台设置已变化，请重新预览")
            tenants.configure_policy(uid, data["policy"])
            policy = tenants.policy()
        return await ui.render(
            update,
            "🛡 **多群主平台总控**\n"
            f"公开接入：{'开启' if policy['public'] else '关闭'}\n独立付款单：{'开启' if policy['invoices'] else '关闭'}\n"
            f"每群主上限：{policy['max_groups']}群\n测试群主：{', '.join(policy['pilot_owners']) or '未配置'}\n\n"
            "📌 上线条件\n权限隔离、模拟支付、真实到账分别验收；不开启公众入口不影响原有平台群。",
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
        tenants.read(uid, chat)
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
                "发送本群总调用上限（0—1000）。这是平台授权的次数上限，不是积分或金额；0表示停用。\n/cancel 取消。",
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
                f"🔐 **确认平台付费服务授权**\n群：{chat}\n服务：{'制图' if resource == 'avatar' else 'AI客服'}\n"
                f"总上限：{data['budget']} 次\n当前已用：{current['used'] if current else 0} 次\n"
                "保存后只影响后续请求；平台密钥不向群主开放，调用失败也计入已提交的调用预算。",
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
            f"🔐 **{chat} · 平台付费服务授权**\n"
            "新经营群默认关闭；平台授权后才可使用。预算按群独立，不能转给其他群。\n"
            f"制图：{'开启' if rows.get('avatar', {}).get('enabled') else '关闭'} · "
            f"{rows.get('avatar', {}).get('used', 0)}/{rows.get('avatar', {}).get('budget', 0)} 次\n"
            f"AI客服：{'开启' if rows.get('chat', {}).get('enabled') else '关闭'} · "
            f"{rows.get('chat', {}).get('used', 0)}/{rows.get('chat', {}).get('budget', 0)} 次",
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
    if name in {"tenant_catalog", "tenant_my_ads"}:
        if name == "tenant_catalog":
            rows = store.db.execute(
                "SELECT p.*,m.title FROM merchant_packages p JOIN tenant_groups g ON g.chat=p.chat "
                "JOIN tenants t ON t.id=g.tenant JOIN mod_groups m ON m.chat=g.chat "
                "JOIN tenant_settings s ON s.chat=g.chat AND s.key='ads_enabled' AND s.value='true' "
                "WHERE json_extract(p.config,'$.enabled')=1 AND m.enabled=1 AND g.status='active' AND t.status='active' ORDER BY m.title,p.id LIMIT 30"
            ).fetchall()
            buttons = [
                (
                    f"{r['title']} · {json.loads(r['config'])['name']}",
                    {"action": "tenant_buy", "id": r["id"]},
                )
                for r in rows
            ]
            return await ui.render(
                update,
                "📢 **群主广告位**\n先审核，后付款；款项直达对应群主，不能抵扣平台广告余额。",
                buttons + back,
            )
        rows = store.db.execute(
            "SELECT id,status FROM ads WHERE uid=? AND tenant<>'platform' ORDER BY at DESC LIMIT 20",
            (uid,),
        ).fetchall()
        return await ui.render(
            update,
            "📋 **我的直付广告**\n订单按经营主体独立。",
            [
                (
                    f"{r['id'][:10]} · {STATES.get(r['status'], r['status'])}",
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
        store.dialog(
            uid,
            {
                "form": "tenant_input",
                "kind": "advertisement",
                "id": package["id"],
                "version": package["version"],
            },
        )
        return await ui.render(
            update,
            f"📢 **{config['name']}**\n群：{package['chat']}\n价格：{config['price']} USDT + 最多0.000999识别尾数\n"
            "请发送广告正文，最后一行填写联系方式。\n审核通过前不付款；/cancel 取消。",
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
        "tenant_order",
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
        is_owner = uid in {store.owner, owner["owner"]}
        invoice = store.db.execute(
            "SELECT * FROM merchant_invoices WHERE id=?", (row["id"],)
        ).fetchone()
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
                    f"✅ **确认审核并预留广告位**\n群：{chat}\n订单：{row['id']}\n{row['body']}\n\n"
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
                    f"📌 **确认安排已付款广告**\n订单：{row['id']}\n群：{chat}\n交易：{invoice['tx']}\n"
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
            f"📋 **直付广告订单**\n订单：{row['id']}\n当前群：{chat}\n收款方：本群经营者\n"
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
        text += "\n\n📌 责任说明\n资金直接进入群主地址；平台不托管、不代退款。到账以链上核验为准，显示失败不会重复收款。"
        buttons = [("刷新订单", {"action": "tenant_order", "id": row["id"]})]
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
        if field in {"pilot_owners", "max_groups"}:
            store.require(uid, owner=True)
        elif field == "refund_address":
            order = merchant.order(uid, data["id"])
            if order["uid"] != uid:
                raise Rejected("仅付款人可填写退款地址")
        else:
            await tenants.verify(uid, chat)
        prompts = {
            "k3_limits": "发送：普通项上限 特殊项上限 号码上限 单期合计，例如 1000 100 20 2000。仅对新期生效。",
            "canada_limits": "发送：最低下注 单笔上限 单期合计。不得超过平台当前倍率房上限，仅影响后续新订单。",
            "slots_stakes": "发送本群老虎机开桌档位，从100 300 800 1500 2000中选择，空格分隔。",
            "mines_stakes": "发送本群扫雷加入档位，从100 300 800 1500 2000中选择，空格分隔。",
            "address": "发送本群公开 TRC20 收款地址，勿发送私钥或助记词。",
            "rewards": "发送：签到奖励 聊天奖励 奖励间隔秒 每日聊天上限，例如 10 1 60 100。",
            "points": "发送：用户数字UID 调整积分 原因。例如 123456 100 活动奖励；仅影响当前群。",
            "package": "发送广告位名称及价格，空格分隔，例如 群内推广 10。下一步选择普通／置顶及容量。",
            "pilot_owners": "发送测试群主数字UID，空格分隔；发送 无 清空。",
            "max_groups": "发送每位群主接入上限，1—100。",
            "refund_address": "发送您确认可以接收退款的 TRC20 地址，不要照抄交易所原转出地址。",
        }
        if field not in prompts:
            raise Rejected("设置不存在")
        store.dialog(uid, {"form": "tenant_input", "kind": field, "payload": data})
        return await ui.render(
            update, prompts[field] + "\n/cancel 取消；下一步确认。", back
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
                f"💳 **确认本群收款地址**\n群：{chat}\n{data['value']}\n\n只影响新付款单。格式校验不是钱包归属证明，请自行核实。",
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
            if name.endswith("save"):
                runtime.points.adjust(
                    uid,
                    data["target"],
                    data["amount"],
                    data["reason"],
                    "merchant-adjust:" + token,
                    chat=chat,
                )
                return await action(
                    ui, update, {"action": "tenant_rewards", "chat": chat}
                )
            return await ui.render(
                update,
                f"🧾 **确认本群积分调整**\n群：{chat}\n用户：{data['target']}\n变化：{data['amount']:+d}\n原因：{data['reason']}",
                [("确认调整", {**data, "action": "tenant_points_save"})] + back,
            )
        config = data["config"]
        if name.endswith("save"):
            merchant.configure(uid, chat, data["id"], config, data["version"])
            return await action(ui, update, {"action": "tenant_ads", "chat": chat})
        return await ui.render(
            update,
            f"📢 **广告位预览**\n群：{chat}\n{config['name']} · {config['price']} USDT\n"
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
            return await action(ui, update, {"action": "tenant_games", "chat": chat})
        return await ui.render(
            update,
            f"🎮 **确认本群运营参数**\n群：{chat}\n{data['label']}\n{data['value']}\n\n赔率、概率、抽水不变；已有桌次／期次保留原快照。",
            [
                (
                    "确认保存",
                    {
                        **data,
                        "action": "tenant_operating_save",
                        "version": current[0] if current else 0,
                    },
                ),
                ("返回玩法设置", {"action": "tenant_games", "chat": chat}),
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
        if name.endswith("save"):
            tenants.switch(uid, chat, data["feature"], data["enabled"], data["version"])
            return await action(ui, update, {"action": "tenant_switches", "chat": chat})
        return await ui.render(
            update,
            f"⚙️ **确认本群开关**\n{group['title']}\n{FEATURES[data['feature']]} → {'开启' if data['enabled'] else '关闭'}\n"
            "不改平台总开关、不改其他群；已受理订单按原规则收尾。",
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
        return await ui.render(
            update,
            f"🏪 **{group['title']}**\n群：{chat}\n本群服务：{'开启' if group['enabled'] else '关闭'}\n"
            f"经营权限：{'正常' if owner['status'] == 'active' else '暂停待平台核查'}\n{owner['error']}\n"
            "积分与订单按群独立。付费AI／制图默认未授权；赔率、概率和抽水由平台统一控制。",
            [
                (label, {"action": key, "chat": chat})
                for label, key in [
                    ("功能启停", "tenant_switches"),
                    ("积分与奖励", "tenant_rewards"),
                    ("玩法设置", "tenant_games"),
                    ("广告位与订单", "tenant_ads"),
                    ("收款设置", "tenant_address"),
                    ("异常记录", "tenant_errors"),
                ]
            ]
            + (
                [("AI预算授权（平台）", {"action": "tenant_resources", "chat": chat})]
                if uid == store.owner
                else []
            )
            + (
                [
                    (
                        "平台暂停／恢复",
                        {
                            "action": "tenant_status_preview",
                            "chat": chat,
                            "active": owner["status"] != "active",
                        },
                    )
                ]
                if uid == store.owner
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
            if feature == "group":
                enabled = bool(group["enabled"])
                permitted = True
            elif feature in {"ads", "canada"}:
                enabled = local_config(store, chat, feature + "_enabled", False)
                permitted = modules.get("ads" if feature == "ads" else "game", False)
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
                f"{label}：本群{'开' if enabled else '关'} · 平台{'允许' if permitted else '关闭'}"
            )
            buttons.append(
                (
                    f"{'关闭' if enabled else '开启'}{label}",
                    {
                        "action": "tenant_switch_preview",
                        "chat": chat,
                        "feature": feature,
                        "enabled": not enabled,
                    },
                )
            )
        return await ui.render(
            update,
            f"⚙️ **{group['title']} · 功能启停**\n"
            + "\n".join(lines)
            + "\n\n平台允许不等于本群启用；先开启本群服务，再选择需要的功能。",
            buttons + back,
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
                    "调整成员积分",
                    {"action": "tenant_edit", "chat": chat, "field": "points"},
                ),
            ]
            + back,
        )
    if name == "tenant_games":
        return await ui.render(
            update,
            f"🎮 **{group['title']} · 玩法设置**\n只修改本群运营参数；固定赔率、概率及抽水不可修改。\n开关统一放在本群功能启停。",
            [
                (label, {"action": key, "chat": chat})
                for label, key in [
                    ("转盘档位／次数／冷却", "wheel_group"),
                    ("快三期次与核查", "k3_group"),
                    ("老虎机桌次与托管", "slots_group"),
                    ("扫雷桌次与托管", "mines_group"),
                ]
            ]
            + [
                (label, {"action": "tenant_edit", "chat": chat, "field": field})
                for label, field in [
                    ("加拿大限额", "canada_limits"),
                    ("快三限额", "k3_limits"),
                    ("老虎机开桌档位", "slots_stakes"),
                    ("扫雷加入档位", "mines_stakes"),
                ]
            ]
            + back,
        )
    if name in {"tenant_ads", "tenant_errors"}:
        rows = store.db.execute(
            "SELECT id,status,error FROM ads WHERE tenant=? AND json_extract(package,'$.target')=? ORDER BY at DESC LIMIT 20",
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
                f"{r['id'][:10]} · {STATES.get(r['status'], r['status'])}",
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
                    f"{r['id'][:10]} · {STATES.get(r['status'], r['status'])} {r['error']}"
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


async def input_text(ui, update, dialog):
    """Parse owner input into a preview, never directly approve financial edits.

    Args:
        ui: Existing UI.
        update: Authenticated private text update.
        dialog: User-bound server-side form state.
    """
    uid = str(update.effective_user.id)
    text = (update.message.text or "").strip()
    kind = dialog["kind"]
    payload = dialog.get("payload", {})
    ui.store.clear_dialog(uid)
    try:
        if kind == "advertisement":
            body, contact = text.rsplit("\n", 1)
            return await ui.render(
                update,
                f"📝 **确认提交审核**\n{body}\n联系方式：{contact}\n\n此时不付款，通过审核后领取付款单。",
                [
                    (
                        "提交审核",
                        {
                            "action": "tenant_submit",
                            "id": dialog["id"],
                            "version": dialog["version"],
                            "body": body,
                            "contact": contact,
                        },
                    )
                ],
                fold_sections=True,
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
        elif kind == "points":
            target, amount, reason = text.split(maxsplit=2)
            data.update(
                action="tenant_points_preview",
                target=target,
                amount=int(amount),
                reason=reason,
            )
        elif kind == "package":
            label, price = text.rsplit(maxsplit=1)
            data.update(
                action="tenant_package_preview",
                id="mp_" + secrets.token_hex(8),
                version=0,
                config={
                    "name": label,
                    "price": price,
                    "kind": "normal",
                    "duration": "30days",
                    "slots": 1,
                    "enabled": True,
                },
            )
        else:
            raise Rejected("表单已失效")
        return await action(ui, update, data)
    except (ValueError, TypeError) as exc:
        if isinstance(exc, Rejected):
            raise
        raise Rejected("格式不正确，请重新打开该设置并按示例填写。") from exc
