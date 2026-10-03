"""Reviewed manual-payment advertisements and conservative Telegram delivery."""

import calendar
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .ad_state import available, payment_authorized, version
from .store import Rejected, encode


def expiry(now, mode):
    point = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))
    if mode == "30days":
        return (point + timedelta(days=30)).timestamp()
    year, month = (
        (point.year + 1, 1) if point.month == 12 else (point.year, point.month + 1)
    )
    return point.replace(
        year=year, month=month, day=min(point.day, calendar.monthrange(year, month)[1])
    ).timestamp()


class Ads:
    def __init__(self, store):
        self.store = store
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS ad_channels(chat TEXT PRIMARY KEY,title TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0)"
        )
        store.db.execute(
            "UPDATE ads SET status='review',error='文案修改中断，结果待核查' WHERE status='editing'"
        )
        store.db.execute(
            "CREATE TABLE IF NOT EXISTS ad_boards(chat TEXT PRIMARY KEY,message INTEGER,state TEXT NOT NULL,body TEXT NOT NULL,error TEXT NOT NULL DEFAULT '')"
        )
        store.db.execute(
            "UPDATE ad_boards SET state='review',error='restart_unknown' WHERE state='busy'"
        )
        store.db.execute(
            "UPDATE ad_operations SET status='review',error='restart_unknown' WHERE status='executing'"
        )
        # RetryAfter confirms no submission for that step; its journal remains resumable.
        store.db.execute(
            "UPDATE ad_boards SET state='busy' WHERE chat IN (SELECT chat FROM ad_operations WHERE status='retry')"
        )

    def configure(self, actor, key, package):
        try:
            price = Decimal(str(package.get("price", "")))
            valid_price = (
                price.is_finite()
                and 0 <= price <= 1000000
                and price.as_tuple().exponent >= -2
            )
        except InvalidOperation:
            valid_price = False
        if (
            not re.fullmatch("[a-zA-Z0-9_-]{1,24}", key)
            or set(package)
            != {
                "name",
                "kind",
                "price",
                "currency",
                "target",
                "duration",
                "slots",
                "payment",
                "enabled",
            }
            or not valid_price
            or package["kind"] not in ("normal", "pinned")
            or package["duration"] not in ("30days", "month")
            or type(package["slots"]) is not int
            or not 1 <= package["slots"] <= 100
            or type(package["enabled"]) is not bool
            or not re.fullmatch(r"-[0-9]+", str(package["target"]))
            or not 1 <= len(package["name"]) <= 40
            or not 1 <= len(package["payment"]) <= 500
            or not re.fullmatch("[A-Za-z0-9]{2,12}", package["currency"])
        ):
            raise Rejected("广告位无效，请按表单填写；币种代码2—12位，价格最多两位小数")
        package = {**package, "target": str(package["target"]), "price": str(price)}
        with self.store.tx() as db:
            self.store.require(actor, "ads", db)
            from .tenants import platform_group

            if not platform_group(self.store, package["target"], db):
                raise Rejected("独立经营群请使用本群广告位管理，不能配置为平台广告位。")
            packages = self.store.get("packages", {}, db)
            if key not in packages and len(packages) >= 30:
                raise Rejected("最多配置30个广告位")
            packages[key] = package
            self.store.put(db, "packages", packages)
            if package["kind"] == "pinned":
                db.execute(
                    "INSERT OR IGNORE INTO ad_targets(chat,capacity) VALUES(?,?)",
                    (package["target"], package["slots"]),
                )
            self.store.audit(db, actor, "package_config", {"id": key, **package})

    def submit(self, uid, kind, body, contact, package_id, snapshot, op):
        if (
            kind not in ("广告", "供应", "需求")
            or not 1 <= len(body) <= 2200
            or not 1 <= len(contact) <= 200
        ):
            raise Rejected("广告正文1—2200字，联系方式1—200字")
        with self.store.tx() as db:
            if not self.store.get("modules", {}, db).get("ads"):
                raise Rejected("广告发布尚未启用")
            package = self.store.get("packages", {}, db).get(package_id)
            if not package or not package["enabled"] or package != snapshot:
                raise Rejected("广告位已变化或停用，请重新预览")
            available(self.store, package, package_id, db)
            row = db.execute(
                "SELECT uid,kind,body,contact,package FROM ads WHERE id=?", (op,)
            ).fetchone()
            if row:
                if tuple(row) != (str(uid), kind, body, contact, encode(package)):
                    raise Rejected("订单操作冲突")
                return
            db.execute(
                "INSERT INTO ads(id,uid,kind,body,contact,package,at) VALUES(?,?,?,?,?,?,?)",
                (
                    op,
                    str(uid),
                    kind,
                    body,
                    contact,
                    encode(package),
                    self.store.clock(),
                ),
            )
            self.store.audit(db, uid, "ad_submitted", {"id": op})
            db.execute("UPDATE ads SET package_key=? WHERE id=?", (package_id, op))

    def proof(self, uid, key, text):
        if not 1 <= len(text) <= 1000:
            raise Rejected("凭证说明过长或为空")
        with self.store.tx() as db:
            row = db.execute("SELECT uid,status FROM ads WHERE id=?", (key,)).fetchone()
            if not row or row["uid"] != str(uid) or row["status"] != "pending":
                raise Rejected("订单不存在或已离开审核阶段")
            db.execute("UPDATE ads SET proof=? WHERE id=?", (text, key))
            self.store.audit(db, uid, "payment_proof", {"id": key})

    def renew(self, uid, previous, package_id, snapshot, op):
        with self.store.tx() as db:
            old = db.execute(
                "SELECT * FROM ads WHERE id=? AND uid=? AND status='active' AND expires>?",
                (previous, str(uid), self.store.clock()),
            ).fetchone()
            package = self.store.get("packages", {}, db).get(package_id)
            if (
                not self.store.get("modules", {}, db).get("ads")
                or not old
                or not package
                or not package["enabled"]
                or package != snapshot
                or package["kind"] != "pinned"
                or package["target"] != json.loads(old["package"])["target"]
            ):
                raise Rejected(
                    "仅可续期尚在有效期内、同目标的置顶广告；广告位变化请重新预览"
                )
            if db.execute(
                "SELECT 1 FROM ads WHERE renewal_of=? AND status='pending'", (previous,)
            ).fetchone():
                raise Rejected("该广告已有待审核续期订单")
            available(self.store, package, package_id, db)
            db.execute(
                "INSERT INTO ads(id,uid,kind,body,contact,package,at,renewal_of) VALUES(?,?,?,?,?,?,?,?)",
                (
                    op,
                    str(uid),
                    old["kind"],
                    old["body"],
                    old["contact"],
                    encode(package),
                    self.store.clock(),
                    previous,
                ),
            )
            self.store.audit(
                db, uid, "ad_renewal_submitted", {"id": op, "previous": previous}
            )
            db.execute("UPDATE ads SET package_key=? WHERE id=?", (package_id, op))

    def moderate(
        self, actor, key, action, reason="", expected=None, expected_version=None
    ):
        with self.store.tx() as db:
            self.store.require(actor, "ads", db)
            row = db.execute("SELECT * FROM ads WHERE id=?", (key,)).fetchone()
            if not row:
                raise Rejected("订单不存在")
            if row["tenant"] != "platform":
                raise Rejected(
                    "独立收款订单请使用群主订单入口，不能人工改为平台已付款。"
                )
            version(row, expected_version)
            if expected is not None and row["body"] != expected:
                raise Rejected("文案已修改，请重新预览后审核")
            if action in ("approve", "paid"):
                if action == "paid":
                    available(
                        self.store, json.loads(row["package"]), row["package_key"], db
                    )
                    if row["paid"] and row["payment_source"] != "manual":
                        raise Rejected("该付款来源不可重复核款")
                if action == "paid" and row["paid_by"] == "ad_wallet":
                    raise Rejected("订单已付款，不能重复核款")
                if row["status"] != "pending":
                    raise Rejected("订单不在待处理状态")
                if action == "paid" and not reason.strip():
                    package = json.loads(row["package"])
                    reason = (
                        f"管理员 {actor} 于 "
                        f"{datetime.fromtimestamp(self.store.clock(), ZoneInfo('Asia/Shanghai')):%Y-%m-%d %H:%M:%S} "
                        f"确认实际到账；应收 {package['price']} {package['currency']}；"
                        "未填写外部流水号。"
                    )
                field = "approved" if action == "approve" else "paid"
                db.execute(
                    f"UPDATE ads SET {field}=1,{field}_by=? WHERE id=?",
                    (str(actor), key),
                )
                if action == "paid":
                    db.execute(
                        "UPDATE ads SET payment_source='manual' WHERE id=?", (key,)
                    )
                db.execute(
                    "UPDATE ads SET ready_at=CASE WHEN paid=1 AND approved=1 THEN ? ELSE NULL END WHERE id=?",
                    (row["ready_at"] or self.store.clock(), key),
                )
            elif action == "reject":
                if row["status"] != "pending" or not reason.strip():
                    raise Rejected("仅可驳回待处理订单，需填写原因")
                db.execute(
                    "UPDATE ads SET status='rejected',error=? WHERE id=?", (reason, key)
                )
                if row["paid_by"] == "ad_wallet":
                    from .payments import Payments

                    payment = db.execute(
                        "SELECT * FROM ad_payments WHERE ad=? AND status='paid'", (key,)
                    ).fetchone()
                    if payment:
                        wallet = object.__new__(Payments)
                        wallet.store = self.store
                        wallet.move(
                            db,
                            "refund:" + key,
                            row["uid"],
                            payment["amount"],
                            "广告审核驳回退回广告余额",
                        )
                        db.execute(
                            "UPDATE ad_payments SET status='refunded' WHERE ad=?",
                            (key,),
                        )
                        db.execute(
                            "UPDATE ads SET refund_state='refunded' WHERE id=?", (key,)
                        )
                elif row["paid"] and row["payment_source"] != "test":
                    db.execute(
                        "UPDATE ads SET refund_state='manual_pending' WHERE id=?",
                        (key,),
                    )
            elif action == "manual_refund":
                if (
                    row["refund_state"] != "manual_pending"
                    or row["status"] not in ("cancelled", "rejected")
                    or not reason.strip()
                ):
                    raise Rejected("仅人工退款待处理订单可确认，须填写退款依据")
                db.execute(
                    "UPDATE ads SET refund_state='manual_done' WHERE id=?", (key,)
                )
            elif action == "stop":
                if row["status"] not in ("active", "published"):
                    raise Rejected("只能停止正在展示的广告；未发布订单请取消退款")
                board = db.execute(
                    "SELECT state FROM ad_boards WHERE chat=?",
                    (json.loads(row["package"])["target"],),
                ).fetchone()
                if board and board[0] != "idle":
                    raise Rejected("广告栏正在执行或待核查，不能停止")
                lock = self.store.ad_locks.get(json.loads(row["package"])["target"])
                if lock and lock.locked():
                    raise Rejected("此目标正在处理，请稍后停止")
                if row["status"] in ("sending", "pinning", "unpinning", "editing"):
                    raise Rejected("上游请求执行中，请稍后检查结果")
                status = (
                    "active"
                    if row["message"] and json.loads(row["package"])["kind"] == "pinned"
                    else "stopped"
                )
                db.execute(
                    "UPDATE ads SET status=?,expires=?,error=? WHERE id=?",
                    (status, self.store.clock(), "管理员停止排程：" + reason, key),
                )
            else:
                raise Rejected("未知广告操作")
            self.store.audit(db, actor, "ad_" + action, {"id": key, "reason": reason})

    def edit(self, actor, key, body, expected, expected_version=None):
        with self.store.tx() as db:
            self.store.require(actor, "ads", db)
            row = db.execute("SELECT * FROM ads WHERE id=?", (key,)).fetchone()
            if not row:
                raise Rejected("订单不存在")
            if row["tenant"] != "platform":
                raise Rejected("独立收款订单不允许旧编辑入口修改，请重新提交审核。")
            version(row, expected_version)
            if not 1 <= len(body) <= 2200:
                raise Rejected("广告正文1—2200字")
            changed = db.execute(
                "UPDATE ads SET body=?,approved=0,approved_by='' WHERE id=? AND status='pending' AND body=?",
                (body, key, expected),
            ).rowcount
            if not changed:
                raise Rejected("订单内容或状态已变化，请重新打开")
            self.store.audit(
                db, actor, "ad_edit", {"id": key, "before": expected, "after": body}
            )

    def cancel(self, actor, key, expected_version=None, reason="取消未发布订单"):
        """Cancel and refund an unsubmitted order in one transaction.

        Args:
            actor: Order owner or advertising administrator.
            key: Order ID.
            expected_version: Confirmation version.
            reason: Audit explanation.
        """
        with self.store.tx() as db:
            row = db.execute("SELECT * FROM ads WHERE id=?", (key,)).fetchone()
            if not row or (
                str(actor) != row["uid"] and not self.store.allowed(actor, "ads", db)
            ):
                raise Rejected("无权取消此订单")
            if row["tenant"] != "platform":
                raise Rejected("请从直付订单申请取消或退款，不能使用平台取消按钮。")
            if row["status"] == "cancelled":
                return
            version(row, expected_version)
            if row["status"] != "pending" or row["message"] or row["first_published"]:
                raise Rejected("仅未发布的待处理订单可取消；执行中或未知结果须先核查")
            refund = "none"
            if row["paid"] and row["payment_source"] == "wallet":
                from .payments import Payments

                payment = db.execute(
                    "SELECT * FROM ad_payments WHERE ad=? AND status='paid'", (key,)
                ).fetchone()
                if not payment:
                    raise Rejected("付款凭据不一致，需核查")
                wallet = object.__new__(Payments)
                wallet.store = self.store
                wallet.move(
                    db,
                    "refund:" + key,
                    row["uid"],
                    payment["amount"],
                    "广告取消退回广告余额",
                )
                db.execute(
                    "UPDATE ad_payments SET status='refunded' WHERE ad=?", (key,)
                )
                refund = "refunded"
            elif row["paid"] and row["payment_source"] != "test":
                refund = "manual_pending"
            db.execute(
                "UPDATE ads SET status='cancelled',refund_state=?,error=? WHERE id=?",
                (refund, reason, key),
            )
            self.store.audit(
                db, actor, "ad_cancel", {"id": key, "refund": refund, "reason": reason}
            )

    async def edit_published(
        self, actor, key, body, expected, bot, expected_version=None
    ):
        """Edit under the same target lock and journal used by publication.

        Args:
            actor: Advertising administrator.
            key: Order ID.
            body: Confirmed replacement content.
            expected: Previous copy.
            bot: Existing Telegram client.
            expected_version: Human confirmation version.
        """
        import asyncio

        from .ad_board import claim, execute, render

        self.store.require(actor, "ads")
        row = self.store.db.execute("SELECT * FROM ads WHERE id=?", (key,)).fetchone()
        if not row:
            raise Rejected("订单不存在")
        if row["tenant"] != "platform":
            raise Rejected("独立经营订单请在本群广告管理核查")
        chat = json.loads(row["package"])["target"]
        lock = self.store.ad_locks.setdefault(chat, asyncio.Lock())
        async with lock:
            if self.store.db.execute(
                "SELECT 1 FROM ad_channels WHERE chat=?", (chat,)
            ).fetchone():
                from .channels import check

                await check(bot, chat)
            self.store.require(actor, "ads")
            row = self.store.db.execute(
                "SELECT * FROM ads WHERE id=?", (key,)
            ).fetchone()
            version(row, expected_version)
            if (
                row["status"] not in ("active", "published")
                or row["body"] != expected
                or not row["message"]
            ):
                raise Rejected("订单内容或状态已变化")
            if row["status"] == "active" and row["expires"] <= self.store.clock():
                raise Rejected("广告已到期")
            if not 1 <= len(body) <= 2200:
                raise Rejected("广告正文1—2200字")
            if body == expected:
                return
            board = self.store.db.execute(
                "SELECT * FROM ad_boards WHERE chat=? AND message=?",
                (chat, row["message"]),
            ).fetchone()
            if board and board["state"] != "idle":
                raise Rejected("广告栏正在执行或待核查")
            text = f"【广告】\n{body}"
            if board:
                rows = [
                    dict(r)
                    for r in self.store.db.execute(
                        "SELECT * FROM ads WHERE status='active' AND message=? AND json_extract(package,'$.target')=?",
                        (row["message"], chat),
                    )
                ]
                text = render(
                    [{**r, "body": body} if r["id"] == key else r for r in rows]
                )
            if len(text.encode("utf-16-le")) // 2 > 3900:
                raise Rejected("广告栏篇幅不足")
            payload = {
                "before": board["body"] if board else f"【广告】\n{expected}",
                "after": text,
                "pin": bool(board),
                "changes": [
                    {
                        "id": key,
                        "version": row["version"],
                        "status": row["status"],
                        "new": False,
                        "body": body,
                    }
                ],
            }
            op = claim(
                self.store, chat, "board" if board else "edit", payload, row["message"]
            )
            await execute(self, bot, op)
            result = self.store.db.execute(
                "SELECT status FROM ad_operations WHERE id=?", (op,)
            ).fetchone()[0]
            if result != "done":
                raise Rejected("修改尚未完成，请查看待核查或限流状态")

    async def tick(self, bot):
        """Dispatch shared and legacy targets under one lock per destination.

        Args:
            bot: Existing Telegram client.
        """
        import asyncio

        if self.store.get("ad_board_enabled", False):
            from .ad_board import tick

            await tick(self, bot)
        targets = [
            r[0]
            for r in self.store.db.execute(
                "SELECT DISTINCT json_extract(package,'$.target') FROM ads WHERE status IN ('pending','sent','active')"
            )
        ]
        for chat in targets:
            lock = self.store.ad_locks.setdefault(chat, asyncio.Lock())
            if lock.locked():
                continue
            async with lock:
                try:
                    await self._tick_legacy(bot, chat)
                except Exception as exc:
                    self.store.audit(
                        self.store.db,
                        "system",
                        "ad_target_failure",
                        {"chat": chat, "error": type(exc).__name__},
                    )

    async def _tick_legacy(self, bot, chat_filter):
        """Claim before every upstream write; never automatically resend unknowns.

        Args:
            bot: The AstrBot-owned Telegram bot client.
        """
        now = self.store.clock()
        rows = self.store.db.execute(
            "SELECT * FROM ads WHERE status='sent' OR (status='pending' AND approved=1 AND paid=1) OR (status='active' AND expires<=?) ORDER BY CASE WHEN status='pending' AND renewal_of<>'' THEN 0 ELSE 1 END,at LIMIT 20",
            (now,),
        ).fetchall()
        for row in rows:
            row = self.store.db.execute(
                "SELECT * FROM ads WHERE id=?", (row["id"],)
            ).fetchone()
            if row["status"] not in ("pending", "sent", "active") or (
                row["status"] == "active" and row["expires"] > now
            ):
                continue
            package = json.loads(row["package"])
            if package["target"] != chat_filter:
                continue
            if self.store.db.execute(
                "SELECT 1 FROM ad_operations WHERE chat=? AND status IN ('executing','retry','review')",
                (chat_filter,),
            ).fetchone():
                continue
            channel = self.store.db.execute(
                "SELECT enabled FROM ad_channels WHERE chat=?", (package["target"],)
            ).fetchone()
            if channel:
                if not channel[0] and row["status"] != "active":
                    continue
                from .channels import check

                try:
                    await check(bot, package["target"])
                except Exception as exc:
                    self.store.db.execute(
                        "UPDATE ads SET error=? WHERE id=?",
                        ("频道权限或连接检查失败：" + type(exc).__name__, row["id"]),
                    )
                    continue
                row = self.store.db.execute(
                    "SELECT * FROM ads WHERE id=?", (row["id"],)
                ).fetchone()
                if row["status"] not in ("pending", "sent", "active"):
                    continue
                if row["status"] == "pending" and not (row["paid"] and row["approved"]):
                    continue
            if (
                self.store.get("ad_board_enabled", False)
                and package["kind"] == "pinned"
            ):
                board = self.store.db.execute(
                    "SELECT message FROM ad_boards WHERE chat=?", (package["target"],)
                ).fetchone()
                legacy_renewal = False
                if row["renewal_of"]:
                    previous = self.store.db.execute(
                        "SELECT message,status FROM ads WHERE id=?",
                        (row["renewal_of"],),
                    ).fetchone()
                    legacy_renewal = (
                        previous
                        and previous["status"] == "active"
                        and (not board or previous["message"] != board[0])
                    )
                if (row["status"] == "pending" and not legacy_renewal) or (
                    board and row["message"] == board[0]
                ):
                    continue
            expiring = row["status"] == "active"
            if not expiring:
                try:
                    available(self.store, package, row["package_key"])
                except Rejected as exc:
                    self.store.db.execute(
                        "UPDATE ads SET error=? WHERE id=?", (str(exc), row["id"])
                    )
                    continue
                if not self.store.get("modules", {}).get("ads"):
                    continue
            if not expiring:
                if not payment_authorized(self.store, row):
                    self.store.db.execute(
                        "UPDATE ads SET error='审核或核款管理员权限已失效，需重新审核' WHERE id=?",
                        (row["id"],),
                    )
                    continue
            if row["status"] == "pending" and package["kind"] == "normal":
                from .ad_board import claim, execute

                op = claim(
                    self.store,
                    package["target"],
                    "publish",
                    {
                        "before": "",
                        "after": f"【广告】\n{row['body']}",
                        "pin": False,
                        "changes": [
                            {
                                "id": row["id"],
                                "version": row["version"],
                                "status": "published",
                                "new": True,
                            }
                        ],
                    },
                )
                await execute(self, bot, op)
                continue
            if row["renewal_of"] and row["status"] == "pending":
                with self.store.tx() as db:
                    old = db.execute(
                        "SELECT * FROM ads WHERE id=? AND status='active'",
                        (row["renewal_of"],),
                    ).fetchone()
                    if not old:
                        db.execute(
                            "UPDATE ads SET status='review',error='原广告已到期或停用，续期需人工核查' WHERE id=?",
                            (row["id"],),
                        )
                    else:
                        db.execute(
                            "UPDATE ads SET status='renewed' WHERE id=?", (old["id"],)
                        )
                        db.execute(
                            "UPDATE ads SET status='active',message=?,expires=? WHERE id=?",
                            (
                                old["message"],
                                expiry(old["expires"], package["duration"]),
                                row["id"],
                            ),
                        )
                        self.store.audit(
                            db,
                            "system",
                            "ad_renewed",
                            {"id": row["id"], "previous": old["id"]},
                        )
                continue
            if row["status"] == "pending" and package["kind"] == "pinned":
                busy = self.store.db.execute(
                    "SELECT package FROM ads WHERE status IN ('sending','sent','pinning','active','review')"
                ).fetchall()
                occupied = sum(
                    json.loads(r[0])["target"] == package["target"]
                    and json.loads(r[0])["kind"] == "pinned"
                    for r in busy
                )
                if occupied >= package["slots"]:
                    continue
            state = (
                "unpinning"
                if expiring
                else "sending"
                if row["status"] == "pending"
                else "pinning"
            )
            if not expiring and row["tenant"] != "platform":
                from .tenants import binding

                owner = binding(self.store, package["target"])
                try:
                    await self.runtime.tenants.verify(
                        owner["owner"],
                        package["target"],
                        "ads",
                        "can_pin_messages" if package["kind"] == "pinned" else None,
                    )
                    available(self.store, package, row["package_key"])
                    current = self.store.db.execute(
                        "SELECT * FROM ads WHERE id=?", (row["id"],)
                    ).fetchone()
                    if (
                        not payment_authorized(self.store, current)
                        or current["version"] != row["version"]
                    ):
                        raise Rejected("订单授权已变化")
                except Exception as exc:
                    self.store.db.execute(
                        "UPDATE ads SET error=? WHERE id=?",
                        ("投递前权限核查失败：" + type(exc).__name__, row["id"]),
                    )
                    continue
            with self.store.tx() as db:
                changed = db.execute(
                    "UPDATE ads SET status=? WHERE id=? AND status=?",
                    (state, row["id"], row["status"]),
                ).rowcount
            if not changed:
                continue
            try:
                if expiring:
                    await bot.unpin_chat_message(
                        chat_id=package["target"], message_id=row["message"]
                    )
                    self.store.db.execute(
                        "UPDATE ads SET status='expired' WHERE id=? AND status='unpinning'",
                        (row["id"],),
                    )
                elif state == "sending":
                    text = f"【广告】\n{row['body']}"
                    sent = await bot.send_message(chat_id=package["target"], text=text)
                    self.store.db.execute(
                        "UPDATE ads SET status=?,message=? WHERE id=?",
                        (
                            "sent" if package["kind"] == "pinned" else "published",
                            sent.message_id,
                            row["id"],
                        ),
                    )
                else:
                    await bot.pin_chat_message(
                        chat_id=package["target"],
                        message_id=row["message"],
                        disable_notification=True,
                    )
                    self.store.db.execute(
                        "UPDATE ads SET status='active',expires=? WHERE id=? AND status='pinning'",
                        (expiry(self.store.clock(), package["duration"]), row["id"]),
                    )
            except Exception as exc:
                self.store.db.execute(
                    "UPDATE ads SET status='review',error=? WHERE id=?",
                    (type(exc).__name__, row["id"]),
                )
                self.store.audit(
                    self.store.db,
                    "system",
                    "ad_delivery_review",
                    {"id": row["id"], "error": type(exc).__name__},
                )
