"""Non-custodial group-owner advertising with immutable direct-payment invoices."""

import asyncio
import hashlib
import json
import re
import secrets
from decimal import Decimal, InvalidOperation

import httpx

from .payments import USDT, address_hex, verify_transfer
from .store import Rejected, encode
from .tenants import PLATFORM, binding, local_config

LATE_WINDOW = 7 * 86400


def migrate(store):
    """Create incremental direct-payment tables without touching legacy funds.

    Args:
        store: Instance store.
    """
    store.db.executescript("""
        CREATE TABLE IF NOT EXISTS merchant_packages(
            id TEXT PRIMARY KEY,tenant TEXT NOT NULL,chat TEXT NOT NULL,
            config TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1);
        CREATE INDEX IF NOT EXISTS merchant_packages_group ON merchant_packages(tenant,chat);
        CREATE TABLE IF NOT EXISTS merchant_addresses(
            chat TEXT NOT NULL,version INTEGER NOT NULL,tenant TEXT NOT NULL,
            address TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(chat,version));
        CREATE INDEX IF NOT EXISTS merchant_addresses_owner ON merchant_addresses(address,tenant);
        CREATE TABLE IF NOT EXISTS merchant_invoices(
            id TEXT PRIMARY KEY REFERENCES ads(id),tenant TEXT NOT NULL,chat TEXT NOT NULL,uid TEXT NOT NULL,
            address TEXT NOT NULL,address_version INTEGER NOT NULL,base INTEGER NOT NULL,amount INTEGER NOT NULL,
            created REAL NOT NULL,expires REAL NOT NULL,watch_until REAL NOT NULL,status TEXT NOT NULL,
            tx TEXT UNIQUE,paid_at REAL,version INTEGER NOT NULL DEFAULT 1,error TEXT NOT NULL DEFAULT '',
            refund_address TEXT NOT NULL DEFAULT '',refund_tx TEXT UNIQUE,refund_at REAL);
        CREATE INDEX IF NOT EXISTS merchant_invoice_match ON merchant_invoices(address,amount,created,watch_until);
        CREATE INDEX IF NOT EXISTS merchant_invoice_due ON merchant_invoices(status,expires);
        CREATE INDEX IF NOT EXISTS merchant_invoice_group ON merchant_invoices(tenant,chat,status);
        CREATE TABLE IF NOT EXISTS merchant_scans(
            address TEXT PRIMARY KEY,cursor TEXT NOT NULL DEFAULT '{}',at REAL NOT NULL DEFAULT 0,
            checked REAL NOT NULL DEFAULT 0,next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT 'not_scanned');
        CREATE TABLE IF NOT EXISTS merchant_events(
            tx TEXT PRIMARY KEY,address TEXT NOT NULL,amount INTEGER NOT NULL,stamp REAL NOT NULL,
            evidence TEXT NOT NULL,status TEXT NOT NULL,invoice TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS merchant_event_address ON merchant_events(address,status,stamp);
        CREATE TABLE IF NOT EXISTS payment_claims(
            tx TEXT PRIMARY KEY,business TEXT NOT NULL,op TEXT NOT NULL,at REAL NOT NULL);
    """)

    if "backfill" not in {
        r["name"] for r in store.db.execute("PRAGMA table_info(merchant_scans)")
    }:
        store.db.execute(
            "ALTER TABLE merchant_scans ADD COLUMN backfill REAL NOT NULL DEFAULT 0"
        )


def authorized(store, order, db=None):
    """Require a frozen owner approval and a proven direct payment for publishing.

    Args:
        store: Instance store.
        order: Advertising order row.
        db: Optional transaction.

    Returns:
        Whether the merchant order can enter the existing publisher.
    """
    db = db or store.db
    package = json.loads(order["package"])
    owner = binding(store, package["target"], db)
    invoice = db.execute(
        "SELECT * FROM merchant_invoices WHERE id=?", (order["id"],)
    ).fetchone()
    return bool(
        owner
        and owner["tenant"] == order["tenant"] != PLATFORM
        and owner["status"] == owner["tenant_status"] == "active"
        and local_config(store, package["target"], "ads_enabled", False, db)
        and order["approved"]
        and order["paid"]
        and order["approved_by"] in {owner["owner"], store.owner}
        and invoice
        and invoice["tenant"] == order["tenant"]
        and invoice["status"] == "paid"
        and invoice["tx"]
        and db.execute(
            "SELECT 1 FROM payment_claims WHERE tx=? AND business='merchant' AND op=?",
            (invoice["tx"], order["id"]),
        ).fetchone()
    )


class MerchantAds:
    """Keep owner receipts separate from prepaid platform advertising balances."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        migrate(self.store)
        self.scan_lock = asyncio.Lock()

    def order(self, uid, key, *, owner=False, db=None):
        db = db or self.store.db
        row = db.execute(
            "SELECT * FROM ads WHERE id=? AND tenant<>'platform'", (key,)
        ).fetchone()
        if not row:
            raise Rejected("订单不存在")
        chat = json.loads(row["package"])["target"]
        business = db.execute(
            "SELECT owner FROM tenants WHERE id=?", (row["tenant"],)
        ).fetchone()
        if not business or (
            str(uid) not in {self.store.owner, business["owner"]}
            and (owner or row["uid"] != str(uid))
        ):
            raise Rejected("没有此经营主体订单的权限")
        if owner:
            current = binding(self.store, chat, db)
            if not current or current["tenant"] != row["tenant"]:
                raise Rejected("群归属已变化，历史订单须由平台核查")
            self.store.require(uid, "ads", db, chat=chat)
        return row

    def address(self, chat, db=None):
        return (
            (db or self.store.db)
            .execute(
                "SELECT * FROM merchant_addresses WHERE chat=? ORDER BY version DESC LIMIT 1",
                (str(chat),),
            )
            .fetchone()
        )

    def set_address(self, uid, chat, address, expected):
        address_hex(address)
        with self.store.tx() as db:
            self.store.require(uid, "ads", db, chat=chat)
            owner = binding(self.store, chat, db)
            if not owner or owner["tenant"] == PLATFORM:
                raise Rejected("平台广告请使用原有收款设置")
            current = self.address(chat, db)
            if (current["version"] if current else 0) != expected:
                raise Rejected("收款地址已变化，请重新预览确认")
            if (
                address == getattr(self.runtime.payments, "address", "")
                or db.execute(
                    "SELECT 1 FROM merchant_addresses WHERE address=? AND tenant<>?",
                    (address, owner["tenant"]),
                ).fetchone()
            ):
                raise Rejected("此地址已用于其他经营主体，请平台核查，不能直接共用。")
            db.execute(
                "INSERT INTO merchant_addresses(chat,version,tenant,address,created) VALUES(?,?,?,?,?)",
                (str(chat), expected + 1, owner["tenant"], address, self.store.clock()),
            )
            db.execute(
                "INSERT OR IGNORE INTO merchant_scans(address) VALUES(?)", (address,)
            )
            self.store.audit(
                db,
                uid,
                "merchant_address",
                {"chat": str(chat), "version": expected + 1, "address": address},
            )

    def configure(self, uid, chat, key, config, expected=0):
        try:
            price = Decimal(str(config.get("price", "")))
            valid = (
                price.is_finite()
                and 0 < price <= 1000000
                and price.as_tuple().exponent >= -2
            )
        except InvalidOperation:
            valid = False
        if (
            set(config) != {"name", "kind", "price", "duration", "slots", "enabled"}
            or not valid
            or not isinstance(config["name"], str)
            or not 1 <= len(config["name"]) <= 40
            or config["kind"] not in {"normal", "pinned"}
            or config["duration"] not in {"30days", "month"}
            or type(config["slots"]) is not int
            or not 1 <= config["slots"] <= 100
            or type(config["enabled"]) is not bool
        ):
            raise Rejected("广告位参数无效：价格大于0、最多两位小数；容量1—100。")
        with self.store.tx() as db:
            self.store.require(uid, "ads", db, chat=chat)
            owner = binding(self.store, chat, db)
            if not owner or owner["tenant"] == PLATFORM:
                raise Rejected("请使用平台广告位管理")
            row = db.execute(
                "SELECT * FROM merchant_packages WHERE id=?", (key,)
            ).fetchone()
            if (
                row and (row["chat"] != str(chat) or row["tenant"] != owner["tenant"])
            ) or (row["version"] if row else 0) != expected:
                raise Rejected("广告位归属或版本已变化")
            if (
                not row
                and db.execute(
                    "SELECT COUNT(*) FROM merchant_packages WHERE chat=?", (str(chat),)
                ).fetchone()[0]
                >= 30
            ):
                raise Rejected("每群最多30个广告位")
            config = {**config, "price": str(price)}
            if config["kind"] == "pinned":
                db.execute(
                    "INSERT INTO ad_targets(chat,capacity) VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET capacity=excluded.capacity,version=version+1",
                    (str(chat), config["slots"]),
                )
            db.execute(
                "INSERT INTO merchant_packages(id,tenant,chat,config) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,version=merchant_packages.version+1",
                (key, owner["tenant"], str(chat), encode(config)),
            )
            self.store.audit(
                db,
                uid,
                "merchant_package",
                {"chat": str(chat), "id": key, "config": config},
            )

    def submit(self, uid, key, body, contact, expected, op):
        if not 1 <= len(body) <= 2200 or not 1 <= len(contact) <= 200:
            raise Rejected("广告正文1—2200字，联系方式1—200字")
        with self.store.tx() as db:
            existing = db.execute("SELECT * FROM ads WHERE id=?", (op,)).fetchone()
            if existing:
                if (
                    existing["uid"] != str(uid)
                    or existing["body"] != body
                    or existing["contact"] != contact
                    or existing["package_key"] != key
                ):
                    raise Rejected("订单操作身份冲突")
                return op
            package = db.execute(
                "SELECT * FROM merchant_packages WHERE id=?", (key,)
            ).fetchone()
            if not package or package["version"] != expected:
                raise Rejected("广告位已变化，请重新选择")
            config = json.loads(package["config"])
            owner = binding(self.store, package["chat"], db)
            group = db.execute(
                "SELECT enabled FROM mod_groups WHERE chat=?", (package["chat"],)
            ).fetchone()
            if (
                not config["enabled"]
                or not group
                or not group[0]
                or not owner
                or owner["status"] != "active"
                or owner["tenant_status"] != "active"
                or not local_config(
                    self.store, package["chat"], "ads_enabled", False, db
                )
                or not self.store.get("modules", {}, db).get("ads")
            ):
                raise Rejected("本群广告投放暂未开放")
            snapshot = {
                **config,
                "currency": "USDT",
                "target": package["chat"],
                "payment": "审核通过后直付群主，平台不托管",
                "_merchant": package["tenant"],
                "_order": op,
            }
            db.execute(
                "INSERT INTO ads(id,uid,kind,body,contact,package,package_key,status,tenant,at) VALUES(?,?,'广告',?,?,?,?,'merchant_review',?,?)",
                (
                    op,
                    str(uid),
                    body,
                    contact,
                    encode(snapshot),
                    key,
                    package["tenant"],
                    self.store.clock(),
                ),
            )
            self.store.audit(
                db, uid, "merchant_submitted", {"id": op, "chat": package["chat"]}
            )
            return op

    def approve(self, uid, key, expected):
        with self.store.tx() as db:
            row = self.order(uid, key, owner=True, db=db)
            existing = db.execute(
                "SELECT * FROM merchant_invoices WHERE id=?", (key,)
            ).fetchone()
            if existing:
                return dict(existing)
            if not self.runtime.tenants.policy()["invoices"]:
                raise Rejected("独立收款尚未开放，不能生成真实付款单。")
            if row["version"] != expected or row["status"] != "merchant_review":
                raise Rejected("订单已变化，请重新审核")
            package = json.loads(row["package"])
            chat = package["target"]
            group = db.execute(
                "SELECT enabled FROM mod_groups WHERE chat=?", (chat,)
            ).fetchone()
            current = db.execute(
                "SELECT config FROM merchant_packages WHERE id=?", (row["package_key"],)
            ).fetchone()
            if (
                not group
                or not group[0]
                or not self.store.get("modules", {}, db).get("ads")
                or not current
                or not json.loads(current[0])["enabled"]
            ):
                raise Rejected("广告功能或广告位已关闭")
            if not local_config(self.store, chat, "ads_enabled", False, db):
                raise Rejected("本群广告投放已关闭")
            # Reservations and active content use one target-wide capacity boundary.
            occupied = db.execute(
                "SELECT COUNT(*) FROM ads a LEFT JOIN merchant_invoices i ON i.id=a.id "
                "WHERE a.tenant=? AND json_extract(a.package,'$.target')=? AND a.id<>? AND "
                "((a.status IN ('active','published','sending','sent','pinning','review') AND (a.expires IS NULL OR a.expires>?)) "
                "OR (i.status='pending' AND i.expires>?) OR (i.status='paid' AND a.status='pending'))",
                (row["tenant"], chat, key, self.store.clock(), self.store.clock()),
            ).fetchone()[0]
            capacity = min(package["slots"], json.loads(current[0])["slots"])
            if occupied >= capacity:
                raise Rejected("广告位没有空位，不生成付款单，请稍后审核。")
            if package["kind"] == "pinned" and self.store.get(
                "ad_board_enabled", False, db
            ):
                from .ad_board import render

                reserved = [
                    dict(r)
                    for r in db.execute(
                        "SELECT a.body,a.slot FROM ads a LEFT JOIN merchant_invoices i ON i.id=a.id "
                        "WHERE a.tenant=? AND json_extract(a.package,'$.target')=? AND json_extract(a.package,'$.kind')='pinned' "
                        "AND ((a.status='active' AND a.expires>?) OR (a.status='pending' AND (i.status='paid' OR (i.status='pending' AND i.expires>?))))",
                        (row["tenant"], chat, self.store.clock(), self.store.clock()),
                    )
                ]
                reserved.append({"body": row["body"], "slot": capacity})
                if len(render(reserved).encode("utf-16-le")) // 2 > 3800:
                    raise Rejected(
                        "广告栏剩余篇幅不足，请精简内容后重新提交；未生成付款单。"
                    )
            address = self.address(chat, db)
            if not address:
                raise Rejected("请先设置并确认本群收款地址")
            health = db.execute(
                "SELECT * FROM merchant_scans WHERE address=?", (address["address"],)
            ).fetchone()
            now = self.store.clock()
            if not health or health["error"] or now - health["at"] > 180:
                raise Rejected(
                    "本地址到账监控尚未就绪或已落后，暂停新付款单，不代表用户未付款。"
                )
            base = int(Decimal(package["price"]) * 1000000)
            reserved = {
                r[0]
                for r in db.execute(
                    "SELECT amount FROM merchant_invoices WHERE address=? AND (watch_until>? OR status IN ('review','paid_waiting','refund_requested'))",
                    (address["address"], now),
                )
            }
            suffixes = [n for n in range(1, 1000) if base + n not in reserved]
            if not suffixes:
                raise Rejected("此地址识别尾数已用完，暂停开单；不会使用模糊金额匹配。")
            amount = base + secrets.choice(suffixes)
            db.execute(
                "INSERT INTO merchant_invoices(id,tenant,chat,uid,address,address_version,base,amount,created,expires,watch_until,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending')",
                (
                    key,
                    row["tenant"],
                    chat,
                    row["uid"],
                    address["address"],
                    address["version"],
                    base,
                    amount,
                    now,
                    now + 1800,
                    now + 1800 + LATE_WINDOW,
                ),
            )
            db.execute(
                "UPDATE ads SET approved=1,approved_by=?,status='pending',payment_source='merchant' WHERE id=?",
                (str(uid), key),
            )
            db.execute(
                "INSERT OR IGNORE INTO notices(id,chat,text) VALUES(?,?,?)",
                (
                    "merchant-invoice/" + key,
                    row["uid"],
                    f"广告审核已通过 · 群 {chat}\n订单 {key}\n请打开「广告投递 → 我的直付广告」核对金额、地址及有效期。过期请勿付款。",
                ),
            )
            self.store.audit(
                db,
                uid,
                "merchant_invoice",
                {"id": key, "amount": amount, "address_version": address["version"]},
            )
            return dict(
                db.execute(
                    "SELECT * FROM merchant_invoices WHERE id=?", (key,)
                ).fetchone()
            )

    def reject(self, uid, key, expected, reason):
        if not reason.strip() or len(reason) > 300:
            raise Rejected("请填写1—300字驳回原因")
        with self.store.tx() as db:
            row = self.order(uid, key, owner=True, db=db)
            if row["version"] != expected or row["status"] != "merchant_review":
                raise Rejected("订单已进入付款或发布流程，不能直接驳回，请走核查退款。")
            db.execute(
                "UPDATE ads SET status='rejected',error=? WHERE id=?", (reason, key)
            )
            self.store.audit(
                db, uid, "merchant_rejected", {"id": key, "reason": reason}
            )

    def expire(self):
        with self.store.tx() as db:
            rows = db.execute(
                "SELECT id FROM merchant_invoices WHERE status='pending' AND expires<=? LIMIT 100",
                (self.store.clock(),),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE merchant_invoices SET status='expired',version=version+1 WHERE id=?",
                    (row[0],),
                )
                db.execute(
                    "UPDATE ads SET status='merchant_expired',error='付款窗口已结束；迟付需核查' WHERE id=? AND paid=0",
                    (row[0],),
                )

    def ingest(self, address, index, receipt):
        tx, amount, stamp = verify_transfer(index, receipt, address, self.store.clock())
        with self.store.tx() as db:
            seen = db.execute(
                "SELECT * FROM merchant_events WHERE tx=?", (tx,)
            ).fetchone()
            if seen:
                if (seen["address"], seen["amount"], seen["stamp"]) != (
                    address,
                    amount,
                    stamp,
                ):
                    raise Rejected("Conflicting chain evidence")
                return seen["status"]
            if db.execute("SELECT 1 FROM payment_claims WHERE tx=?", (tx,)).fetchone():
                raise Rejected("Transfer already used by another business")
            if not db.execute(
                "SELECT 1 FROM merchant_invoices WHERE address=? AND created<=? AND watch_until>=? LIMIT 1",
                (address, stamp, stamp),
            ).fetchone():
                return "unrelated"
            candidates = db.execute(
                "SELECT * FROM merchant_invoices WHERE address=? AND amount=? AND created<=? AND watch_until>=? ORDER BY created",
                (address, amount, stamp, stamp),
            ).fetchall()
            invoice = candidates[0] if len(candidates) == 1 else None
            state, error, key = "review", "金额、时间或订单匹配不唯一，请人工核查", ""
            if invoice:
                key = invoice["id"]
                order = db.execute("SELECT * FROM ads WHERE id=?", (key,)).fetchone()
                if invoice["tx"]:
                    error = "订单已有付款，本次为额外到账，需核查退款"
                elif invoice["created"] <= stamp <= invoice["expires"]:
                    state = (
                        "paid"
                        if invoice["status"] == "pending"
                        and order["status"] == "pending"
                        and self.store.clock() < invoice["expires"]
                        else "paid_waiting"
                    )
                    error = (
                        ""
                        if state == "paid"
                        else "有效付款已确认，但广告位已释放；待安排或退款"
                    )
                    db.execute(
                        "UPDATE merchant_invoices SET status=?,tx=?,paid_at=?,error=?,version=version+1 WHERE id=?",
                        (state, tx, stamp, error, key),
                    )
                    db.execute(
                        "UPDATE ads SET paid=1,paid_by='merchant_chain',ready_at=?,status=?,error=? WHERE id=?",
                        (
                            self.store.clock(),
                            "pending" if state == "paid" else "review",
                            error,
                            key,
                        ),
                    )
                else:
                    error = "付款超过有效期，已记录到账；请核查安排或退款"
                    db.execute(
                        "UPDATE merchant_invoices SET status='review',tx=?,paid_at=?,error=?,version=version+1 WHERE id=?",
                        (tx, stamp, error, key),
                    )
                    db.execute(
                        "UPDATE ads SET paid=1,paid_by='merchant_chain',status='review',error=? WHERE id=?",
                        (error, key),
                    )
            db.execute(
                "INSERT INTO merchant_events(tx,address,amount,stamp,evidence,status,invoice,error) VALUES(?,?,?,?,?,?,?,?)",
                (
                    tx,
                    address,
                    amount,
                    stamp,
                    encode({"index": index, "receipt": receipt}),
                    state,
                    key,
                    error,
                ),
            )
            db.execute(
                "INSERT INTO payment_claims(tx,business,op,at) VALUES(?,'merchant',?,?)",
                (tx, key, self.store.clock()),
            )
            self.store.audit(
                db,
                "chain",
                "merchant_payment",
                {"id": key, "tx": tx, "amount": amount, "state": state},
            )
            return state

    def arrange(self, uid, key, expected):
        """Reserve capacity again for a proven late/backfilled payment, without charging."""
        with self.store.tx() as db:
            row = self.order(uid, key, owner=True, db=db)
            invoice = db.execute(
                "SELECT * FROM merchant_invoices WHERE id=?", (key,)
            ).fetchone()
            if (
                not invoice
                or invoice["version"] != expected
                or invoice["status"] not in {"paid_waiting", "review"}
                or not invoice["tx"]
                or not row["paid"]
                or row["message"]
                or row["first_published"]
            ):
                raise Rejected("只能安排已核实到账且从未投递的订单")
            package = json.loads(row["package"])
            chat = package["target"]
            if db.execute(
                "SELECT 1 FROM ad_operations WHERE chat=? AND status IN ('pending','executing','retry','review') LIMIT 1",
                (chat,),
            ).fetchone():
                raise Rejected("本群有未解决的投递记录，须先核查，不能重复发布")
            current = db.execute(
                "SELECT config FROM merchant_packages WHERE id=?", (row["package_key"],)
            ).fetchone()
            group = db.execute(
                "SELECT enabled FROM mod_groups WHERE chat=?", (chat,)
            ).fetchone()
            if (
                not current
                or not json.loads(current[0])["enabled"]
                or not group
                or not group[0]
                or not local_config(self.store, chat, "ads_enabled", False, db)
                or not self.store.get("modules", {}, db).get("ads")
            ):
                raise Rejected("本群广告投放或广告位已关闭")
            occupied = db.execute(
                "SELECT COUNT(*) FROM ads a LEFT JOIN merchant_invoices i ON i.id=a.id WHERE a.tenant=? AND json_extract(a.package,'$.target')=? AND a.id<>? AND "
                "((a.status IN ('active','published','sending','sent','pinning','review') AND (a.expires IS NULL OR a.expires>?)) OR (i.status='pending' AND i.expires>?) OR (i.status='paid' AND a.status='pending'))",
                (row["tenant"], chat, key, self.store.clock(), self.store.clock()),
            ).fetchone()[0]
            if occupied >= min(package["slots"], json.loads(current[0])["slots"]):
                raise Rejected("暂无空位；保留到账记录，稍后安排或退款")
            if not db.execute(
                "SELECT 1 FROM payment_claims WHERE tx=? AND business='merchant' AND op=?",
                (invoice["tx"], key),
            ).fetchone():
                raise Rejected("到账证据不完整，不能发布")
            db.execute(
                "UPDATE merchant_invoices SET status='paid',error='',version=version+1 WHERE id=?",
                (key,),
            )
            db.execute(
                "UPDATE ads SET status='pending',error='',ready_at=? WHERE id=?",
                (self.store.clock(), key),
            )
            self.store.audit(
                db, uid, "merchant_arranged", {"id": key, "tx": invoice["tx"]}
            )

    def refund_request(self, uid, key, address, expected):
        address_hex(address)
        with self.store.tx() as db:
            row = self.order(uid, key, db=db)
            invoice = db.execute(
                "SELECT * FROM merchant_invoices WHERE id=?", (key,)
            ).fetchone()
            if (
                row["uid"] != str(uid)
                or not invoice
                or invoice["version"] != expected
                or not invoice["tx"]
            ):
                raise Rejected("仅付款人可确认当前已到账订单的退款地址")
            if (
                row["status"] not in {"review", "pending", "merchant_expired"}
                or invoice["status"] == "refunded"
            ):
                raise Rejected("订单已发布或退款结束，请先联系群主核查")
            db.execute(
                "UPDATE merchant_invoices SET status='refund_requested',refund_address=?,refund_at=?,version=version+1 WHERE id=?",
                (address, self.store.clock(), key),
            )
            db.execute(
                "UPDATE ads SET status='review',error='付款人已确认退款地址，等待群主自行退款' WHERE id=?",
                (key,),
            )
            self.store.audit(
                db, uid, "merchant_refund_requested", {"id": key, "address": address}
            )

    def refund_proof(self, uid, key, expected, index, receipt):
        with self.store.tx() as db:
            self.order(uid, key, owner=True, db=db)
            invoice = db.execute(
                "SELECT * FROM merchant_invoices WHERE id=?", (key,)
            ).fetchone()
            if (
                not invoice
                or invoice["version"] != expected
                or invoice["status"] != "refund_requested"
            ):
                raise Rejected("退款状态已变化，请重新核查")
            tx, amount, stamp = verify_transfer(
                index, receipt, invoice["refund_address"], self.store.clock()
            )
            if amount != invoice["amount"] or stamp < invoice["refund_at"]:
                raise Rejected("退款金额或时间不符，必须包含已收尾数")
            if db.execute("SELECT 1 FROM payment_claims WHERE tx=?", (tx,)).fetchone():
                raise Rejected("此交易已被使用，不能重复核销退款")
            db.execute(
                "INSERT INTO payment_claims(tx,business,op,at) VALUES(?,'merchant_refund',?,?)",
                (tx, key, self.store.clock()),
            )
            db.execute(
                "UPDATE merchant_invoices SET status='refunded',refund_tx=?,version=version+1 WHERE id=?",
                (tx, key),
            )
            db.execute(
                "UPDATE ads SET status='cancelled',refund_state='merchant_refunded',error='' WHERE id=?",
                (key,),
            )
            self.store.audit(
                db,
                uid,
                "merchant_refund_verified",
                {
                    "id": key,
                    "tx": tx,
                    "amount": amount,
                    "evidence": {"index": index, "receipt": receipt},
                },
            )

    async def verify_refund_input(self, ui, update, dialog, text):
        """Fetch a solidified receipt and preview a verified refund without signing.

        Args:
            ui: Private Telegram renderer.
            update: Authenticated owner input.
            dialog: Server-side invoice and version binding.
            text: Public transaction hash; never a private key.
        """
        if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
            raise Rejected("请填写64位交易哈希，不要提供私钥。")
        uid = str(update.effective_user.id)
        row = self.order(uid, dialog["id"], owner=True)
        chat = json.loads(row["package"])["target"]
        await self.runtime.tenants.verify(uid, chat, "ads")
        invoice = self.store.db.execute(
            "SELECT * FROM merchant_invoices WHERE id=?", (row["id"],)
        ).fetchone()
        if (
            not invoice
            or invoice["status"] != "refund_requested"
            or invoice["version"] != dialog["version"]
        ):
            raise Rejected("退款状态已变化，请重新打开订单。")
        response = await self.runtime.payments.client.post(
            "/walletsolidity/gettransactioninfobyid",
            json={"value": text.lower()},
            timeout=10,
        )
        response.raise_for_status()
        receipt = response.json()
        logs = [
            log
            for log in receipt.get("log", [])
            if log.get("address", "").lower() == address_hex(USDT)[2:]
            and len(log.get("topics", [])) == 3
            and log["topics"][2].lower()
            == "0" * 24 + address_hex(invoice["refund_address"])[2:]
        ]
        if len(logs) != 1:
            raise Rejected("退款收款记录不唯一或不匹配，不能确认。")
        raw = bytes.fromhex("41" + logs[0]["topics"][1][-40:])
        number = int.from_bytes(
            raw + hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4], "big"
        )
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        sender = ""
        while number:
            number, remainder = divmod(number, 58)
            sender = alphabet[remainder] + sender
        index = {
            "transaction_id": text.lower(),
            "type": "Transfer",
            "token_info": {"address": USDT, "decimals": 6},
            "to": invoice["refund_address"],
            "from": sender,
            "value": str(int(logs[0]["data"], 16)),
            "block_timestamp": receipt.get("blockTimeStamp"),
        }
        tx, amount, stamp = verify_transfer(
            index, receipt, invoice["refund_address"], self.store.clock()
        )
        if amount != invoice["amount"] or stamp < invoice["refund_at"]:
            raise Rejected("退款金额或时间不符合订单；必须包含已收尾数。")
        from .payments import money

        return await ui.render(
            update,
            f"↩️ **退款凭证核验通过，等待确认**\n{tx}\n收款地址：{invoice['refund_address']}\n金额：{money(amount)} USDT\n机器人不会转账，只记录该凭证。",
            [
                (
                    "确认登记退款",
                    {
                        "action": "tenant_refund_record",
                        "id": invoice["id"],
                        "version": invoice["version"],
                        "index": index,
                        "receipt": receipt,
                    },
                ),
                ("返回订单", {"action": "tenant_order", "id": invoice["id"]}),
            ],
        )

    async def scan(self, address):
        """Process one bounded page and advance only after complete ingestion.

        Args:
            address: A configured public address with active invoice monitoring.
        """
        now = self.store.clock()
        row = self.store.db.execute(
            "SELECT * FROM merchant_scans WHERE address=?", (address,)
        ).fetchone()
        cursor = json.loads(row["cursor"])
        if not cursor:
            first = self.store.db.execute(
                "SELECT MIN(created) FROM merchant_invoices WHERE address=?", (address,)
            ).fetchone()[0]
            if first is None:
                first = (
                    self.store.db.execute(
                        "SELECT MIN(created) FROM merchant_addresses WHERE address=?",
                        (address,),
                    ).fetchone()[0]
                    or now
                )
            audit = now - row["backfill"] >= 86400
            cursor = {
                "start": int(
                    max(first, now - LATE_WINDOW if audit else row["at"] - 120) * 1000
                ),
                "end": int(now * 1000),
                "audit": now if audit else row["backfill"],
            }
        params = {
            "only_confirmed": "true",
            "only_to": "true",
            "contract_address": USDT,
            "limit": 50,
            "order_by": "block_timestamp,asc",
            "min_timestamp": cursor["start"],
            "max_timestamp": cursor["end"],
        }
        if cursor.get("fingerprint"):
            params["fingerprint"] = cursor["fingerprint"]
        client = self.runtime.payments.client
        response = await client.get(
            f"/v1/accounts/{address}/transactions/trc20", params=params, timeout=10
        )
        response.raise_for_status()
        body = response.json()
        if body.get("success") is not True or not isinstance(body.get("data"), list):
            raise Rejected("Invalid chain index response")
        for entry in body["data"]:
            tx = entry.get("transaction_id", "")
            if self.store.db.execute(
                "SELECT 1 FROM merchant_events WHERE tx=?", (tx,)
            ).fetchone():
                continue
            receipt = await client.post(
                "/walletsolidity/gettransactioninfobyid", json={"value": tx}, timeout=10
            )
            receipt.raise_for_status()
            self.ingest(address, entry, receipt.json())
        fingerprint = body.get("meta", {}).get("fingerprint")
        if fingerprint and fingerprint == cursor.get("fingerprint"):
            raise Rejected("Chain pagination did not advance")
        if fingerprint:
            cursor["fingerprint"] = fingerprint
            self.store.db.execute(
                "UPDATE merchant_scans SET cursor=?,checked=?,next=?,error='catching_up' WHERE address=?",
                (encode(cursor), now, now + 1, address),
            )
        else:
            self.store.db.execute(
                "UPDATE merchant_scans SET cursor='{}',at=?,checked=?,next=?,error='',backfill=? WHERE address=?",
                (
                    cursor["end"] / 1000,
                    now,
                    now + 30,
                    cursor.get("audit", row["backfill"]),
                    address,
                ),
            )

    async def tick(self):
        if self.scan_lock.locked():
            return
        async with self.scan_lock:
            self.expire()
            now = self.store.clock()
            addresses = self.store.db.execute(
                "SELECT s.address FROM merchant_scans s WHERE s.next<=? AND "
                "(EXISTS(SELECT 1 FROM merchant_addresses a WHERE a.address=s.address AND a.version=(SELECT MAX(version) FROM merchant_addresses WHERE chat=a.chat)) "
                "OR EXISTS(SELECT 1 FROM merchant_invoices i WHERE i.address=s.address AND (i.watch_until>? OR i.status IN ('review','paid_waiting','refund_requested')))) "
                "ORDER BY s.checked,s.address LIMIT 4",
                (now, now),
            ).fetchall()
            tasks = [asyncio.wait_for(self.scan(r[0]), timeout=30) for r in addresses]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for row, result in zip(addresses, results, strict=True):
                if isinstance(result, BaseException):
                    delay = 30
                    if isinstance(result, httpx.HTTPStatusError):
                        if result.response.status_code == 429:
                            try:
                                delay = max(
                                    1,
                                    min(
                                        86400,
                                        float(
                                            result.response.headers.get(
                                                "Retry-After", "30"
                                            )
                                        ),
                                    ),
                                )
                            except ValueError:
                                delay = 30
                        elif result.response.status_code == 400:
                            self.store.db.execute(
                                "UPDATE merchant_scans SET cursor='{}' WHERE address=?",
                                (row[0],),
                            )
                    self.store.db.execute(
                        "UPDATE merchant_scans SET checked=?,next=?,error=? WHERE address=?",
                        (now, now + delay, type(result).__name__, row[0]),
                    )

    async def loop(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                self.runtime.report("merchant_payments", exc)
            await asyncio.sleep(5)
