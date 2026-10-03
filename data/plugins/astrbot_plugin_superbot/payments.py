"""Read-only TRON verification and a separate advertising-only USDT ledger."""

import hashlib
import json
import re
import secrets
from decimal import Decimal

import httpx

from .store import Rejected, encode

USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRANSFER = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def address_hex(address):
    """Validate a Base58Check mainnet address and return its payload hex.

    Args:
        address: Public TRON address.

    Returns:
        Hexadecimal address including the network byte.
    """
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    try:
        number = 0
        for char in address:
            number = number * 58 + alphabet.index(char)
        raw = number.to_bytes(25, "big")
        if (
            len(address) != 34
            or raw[0] != 65
            or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4]
            != raw[-4:]
        ):
            raise ValueError
        return raw[:-4].hex()
    except (ValueError, OverflowError):
        raise Rejected("TRC20 收款地址校验失败") from None


def money(value):
    return f"{Decimal(value) / 1000000:.6f}"


def verify_transfer(row, receipt, address, now):
    """Validate a single confirmed USDT transfer without mutating balances.

    Args:
        row: Confirmed transfer index entry.
        receipt: Matching walletsolidity transaction receipt.
        address: Immutable expected recipient address.
        now: Current Unix timestamp.

    Returns:
        Transaction ID, integer amount and block timestamp.

    Raises:
        Rejected: Index data and confirmed receipt do not prove one transfer.
    """
    tx = row.get("transaction_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", tx):
        raise Rejected("Invalid chain transaction")
    if (
        row.get("to") != address
        or row.get("type") != "Transfer"
        or row.get("token_info", {}).get("address") != USDT
        or row.get("token_info", {}).get("decimals") != 6
    ):
        raise Rejected("Invalid token or recipient")
    raw = row.get("value", "")
    if isinstance(raw, bool) or not re.fullmatch(r"[0-9]{1,19}", str(raw)):
        raise Rejected("Invalid chain amount")
    amount = int(raw)
    if not 0 < amount <= 10**18:
        raise Rejected("Invalid chain amount")
    if (
        receipt.get("id") != tx
        or receipt.get("receipt", {}).get("result") != "SUCCESS"
        or not receipt.get("blockNumber")
    ):
        raise Rejected("Unconfirmed or failed transaction")
    stamp = receipt.get("blockTimeStamp", 0) / 1000
    if stamp != row.get("block_timestamp", 0) / 1000 or not 0 < stamp <= now + 30:
        raise Rejected("Invalid chain timestamp")
    logs = []
    for log in receipt.get("log", []):
        topics = log.get("topics", [])
        if (
            log.get("address", "").lower() == address_hex(USDT)[2:]
            and len(topics) == 3
            and topics[0].lower() == TRANSFER
            and topics[2].lower() == "0" * 24 + address_hex(address)[2:]
        ):
            logs.append(log)
    try:
        valid = (
            len(logs) == 1
            and int(logs[0].get("data", "0"), 16) == amount
            and logs[0]["topics"][1].lower()
            == "0" * 24 + address_hex(row.get("from", ""))[2:]
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise Rejected("Transfer evidence mismatch or multiple transfers")
    return tx, amount, stamp


class Payments:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.address = config.get("usdt_address", "")
        if self.address:
            address_hex(self.address)
        self.client = httpx.AsyncClient(
            base_url="https://api.trongrid.io",
            timeout=20,
            headers={"TRON-PRO-API-KEY": config["trongrid_api_key"]}
            if config.get("trongrid_api_key")
            else {},
        )
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS ad_wallets(uid TEXT PRIMARY KEY,balance INTEGER NOT NULL CHECK(balance>=0));
            CREATE TABLE IF NOT EXISTS ad_money(op TEXT PRIMARY KEY,uid TEXT NOT NULL,delta INTEGER NOT NULL,reason TEXT NOT NULL,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deposits(id TEXT PRIMARY KEY,uid TEXT NOT NULL,address TEXT NOT NULL,amount INTEGER NOT NULL UNIQUE,created REAL NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL,tx TEXT UNIQUE);
            CREATE INDEX IF NOT EXISTS deposits_pending ON deposits(status,created);
            CREATE TABLE IF NOT EXISTS chain_events(tx TEXT PRIMARY KEY,amount INTEGER NOT NULL,stamp REAL NOT NULL,evidence TEXT NOT NULL,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ad_payments(ad TEXT PRIMARY KEY,uid TEXT NOT NULL,amount INTEGER NOT NULL,status TEXT NOT NULL);
        """)
        store.db.execute(
            "INSERT OR IGNORE INTO payment_claims(tx,business,op,at) SELECT tx,'platform',tx,stamp FROM chain_events"
        )

    def balance(self, uid):
        row = self.store.db.execute(
            "SELECT balance FROM ad_wallets WHERE uid=?", (str(uid),)
        ).fetchone()
        return row[0] if row else 0

    def move(self, db, op, uid, delta, reason):
        row = db.execute(
            "SELECT uid,delta,reason FROM ad_money WHERE op=?", (op,)
        ).fetchone()
        if row:
            if tuple(row) != (str(uid), delta, reason):
                raise Rejected("资金流水冲突，需核查")
            return False
        db.execute(
            "INSERT OR IGNORE INTO ad_wallets(uid,balance) VALUES(?,0)", (str(uid),)
        )
        if not db.execute(
            "UPDATE ad_wallets SET balance=balance+? WHERE uid=? AND balance+?>=0 AND balance+?<=1000000000000000",
            (delta, str(uid), delta, delta),
        ).rowcount:
            raise Rejected("广告余额不足或超出上限")
        db.execute(
            "INSERT INTO ad_money(op,uid,delta,reason,at) VALUES(?,?,?,?,?)",
            (op, str(uid), delta, reason, self.store.clock()),
        )
        return True

    def create(self, uid, whole, op):
        if not self.config.get("usdt_enabled") or not self.address:
            raise Rejected("充值暂未开放")
        if type(whole) is not int or not 1 <= whole <= 10000:
            raise Rejected("请输入 1—10000 的整数充值金额")
        health = self.store.get("usdt_health", {})
        if health.get("error") or self.store.clock() - health.get("at", 0) > 180:
            raise Rejected("到账查询暂时不可用，请稍后创建充值单")
        with self.store.tx() as db:
            existing = db.execute("SELECT * FROM deposits WHERE id=?", (op,)).fetchone()
            if existing:
                if existing["uid"] != str(uid):
                    raise Rejected("订单不属于你")
                return dict(existing)
            if (
                db.execute(
                    "SELECT COUNT(*) FROM deposits WHERE uid=? AND status='pending' AND expires>?",
                    (str(uid), self.store.clock()),
                ).fetchone()[0]
                >= 3
            ):
                raise Rejected("最多同时保留三笔充值单，请先完成已有订单")
            amount = None
            for _ in range(100):
                candidate = whole * 1000000 + secrets.randbelow(999999) + 1
                if not db.execute(
                    "SELECT 1 FROM deposits WHERE amount=?", (candidate,)
                ).fetchone():
                    amount = candidate
                    break
            if amount is None:
                raise Rejected("暂时无法分配充值金额")
            now = self.store.clock()
            db.execute(
                "INSERT INTO deposits(id,uid,address,amount,created,expires,status) VALUES(?,?,?,?,?,?,'pending')",
                (op, str(uid), self.address, amount, now, now + 1800),
            )
            self.store.audit(db, uid, "deposit_created", {"id": op, "amount": amount})
            return dict(
                db.execute("SELECT * FROM deposits WHERE id=?", (op,)).fetchone()
            )

    def ingest(self, row, receipt):
        """Credit only a unique matched order backed by a solidified transfer log.

        Args:
            row: Confirmed TronGrid transfer index entry.
            receipt: Receipt from walletsolidity, never the unconfirmed endpoint.
        """
        tx = row.get("transaction_id", "")
        if not re.fullmatch(r"[0-9a-f]{64}", tx):
            raise Rejected("Invalid chain transaction")
        if (
            row.get("to") != self.address
            or row.get("type") != "Transfer"
            or row.get("token_info", {}).get("address") != USDT
            or row.get("token_info", {}).get("decimals") != 6
        ):
            raise Rejected("Invalid token or recipient")
        amount = int(row["value"])
        if amount <= 0 or amount > 10**18:
            raise Rejected("Invalid chain amount")
        if (
            receipt.get("id") != tx
            or receipt.get("receipt", {}).get("result") != "SUCCESS"
            or not receipt.get("blockNumber")
        ):
            raise Rejected("Unconfirmed or failed transaction")
        stamp = receipt.get("blockTimeStamp", 0) / 1000
        if (
            stamp != row.get("block_timestamp", 0) / 1000
            or stamp <= 0
            or stamp > self.store.clock() + 30
        ):
            raise Rejected("Invalid chain timestamp")
        logs = []
        for log in receipt.get("log", []):
            topics = log.get("topics", [])
            if (
                log.get("address", "").lower() == address_hex(USDT)[2:]
                and len(topics) == 3
                and topics[0].lower() == TRANSFER
                and topics[2].lower() == "0" * 24 + address_hex(self.address)[2:]
            ):
                logs.append(log)
        if (
            len(logs) != 1
            or int(logs[0].get("data", "0"), 16) != amount
            or logs[0]["topics"][1].lower() != "0" * 24 + address_hex(row["from"])[2:]
        ):
            raise Rejected("Transfer evidence mismatch or multiple transfers")
        evidence = encode({"index": row, "receipt": receipt})
        with self.store.tx() as db:
            seen = db.execute(
                "SELECT amount,stamp FROM chain_events WHERE tx=?", (tx,)
            ).fetchone()
            if seen:
                if tuple(seen) != (amount, stamp):
                    raise Rejected("Conflicting chain evidence")
                return
            if db.execute("SELECT 1 FROM payment_claims WHERE tx=?", (tx,)).fetchone():
                raise Rejected("Transfer already used by another business")
            order = db.execute(
                "SELECT * FROM deposits WHERE address=? AND amount=?",
                (self.address, amount),
            ).fetchone()
            matched = (
                order
                and order["status"] in ("pending", "expired")
                and order["created"] <= stamp <= order["expires"]
            )
            db.execute(
                "INSERT INTO chain_events(tx,amount,stamp,evidence,status) VALUES(?,?,?,?,?)",
                (tx, amount, stamp, evidence, "credited" if matched else "review"),
            )
            db.execute(
                "INSERT INTO payment_claims(tx,business,op,at) VALUES(?,'platform',?,?)",
                (tx, tx, self.store.clock()),
            )
            if matched:
                self.move(db, "deposit:" + tx, order["uid"], amount, "USDT-TRC20充值")
                db.execute(
                    "UPDATE deposits SET status='credited',tx=? WHERE id=?",
                    (tx, order["id"]),
                )
                self.store.audit(
                    db,
                    "chain",
                    "deposit_credited",
                    {"id": order["id"], "tx": tx, "amount": amount},
                )

    def pay(self, uid, ad_id, expected, expected_version=None):
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM ads WHERE id=? AND uid=?", (ad_id, str(uid))
            ).fetchone()
            if not row:
                raise Rejected("广告订单不存在")
            if row["tenant"] != "platform":
                raise Rejected("此订单须直接支付给对应群主，不能使用平台广告余额。")
            from .ad_state import available, version

            payment = db.execute(
                "SELECT status FROM ad_payments WHERE ad=?", (ad_id,)
            ).fetchone()
            if payment:
                if payment[0] == "paid":
                    return
                raise Rejected("订单已退款，不能重复付款")
            version(row, expected_version)
            package = json.loads(row["package"])
            available(self.store, package, row["package_key"], db)
            price = Decimal(package["price"]) * 1000000
            if (
                package["currency"].upper() != "USDT"
                or price != expected
                or price != price.to_integral_value()
                or row["status"] != "pending"
                or row["paid"]
            ):
                raise Rejected("订单状态或金额已变化；仅支持 USDT 套餐")
            self.move(db, "ad:" + ad_id, uid, -int(price), "广告消费")
            db.execute(
                "INSERT INTO ad_payments(ad,uid,amount,status) VALUES(?,?,?,'paid')",
                (ad_id, str(uid), int(price)),
            )
            db.execute(
                "UPDATE ads SET paid=1,paid_by='ad_wallet',payment_source='wallet' WHERE id=?",
                (ad_id,),
            )
            db.execute(
                "UPDATE ads SET ready_at=CASE WHEN approved=1 THEN ? ELSE NULL END WHERE id=?",
                (row["ready_at"] or self.store.clock(), ad_id),
            )
            self.store.audit(
                db, uid, "ad_wallet_paid", {"ad": ad_id, "amount": int(price)}
            )

    async def tick(self):
        """Advance durable bounded scans only after full-page processing.

        Returns:
            None; persisted health identifies backlog or source failure.
        """
        if not self.address:
            return
        now = self.store.clock()
        key = "usdt_scan_v2/" + self.address
        with self.store.tx() as db:
            db.execute(
                "UPDATE deposits SET status='expired' WHERE status='pending' AND expires<?",
                (now,),
            )
            began = self.store.get("usdt_scan_start", None, db)
            if began is None:
                began = now
                self.store.put(db, "usdt_scan_start", began)
            state = self.store.get(
                key, {"watermark": began, "last_rescan": 0, "job": None}, db
            )
            if not state["job"]:
                daily = (
                    state["watermark"] >= now - 60
                    and state["last_rescan"] < now - 86400
                )
                state["job"] = {
                    "start": max(
                        began,
                        (now - 7 * 86400 if daily else state["watermark"] - 86400),
                    ),
                    "end": now if daily else min(now, state["watermark"] + 21600),
                    "fingerprint": None,
                    "daily": daily,
                }
                self.store.put(db, key, state)
        job = state["job"]
        params = {
            "only_confirmed": "true",
            "only_to": "true",
            "contract_address": USDT,
            "limit": 200,
            "order_by": "block_timestamp,asc",
            "min_timestamp": int(job["start"] * 1000),
            "max_timestamp": int(job["end"] * 1000),
        }
        if job["fingerprint"]:
            params["fingerprint"] = job["fingerprint"]
        try:
            for _ in range(20):
                response = await self.client.get(
                    f"/v1/accounts/{self.address}/transactions/trc20", params=params
                )
                if response.status_code == 400 and job["fingerprint"]:
                    job["fingerprint"] = None
                    with self.store.tx() as db:
                        self.store.put(db, key, state)
                    raise Rejected("Scan cursor expired; fixed window will restart")
                response.raise_for_status()
                body = response.json()
                if body.get("success") is not True or not isinstance(
                    body.get("data"), list
                ):
                    raise Rejected("Invalid chain index response")
                for row in body["data"]:
                    if self.store.db.execute(
                        "SELECT 1 FROM chain_events WHERE tx=?",
                        (row.get("transaction_id"),),
                    ).fetchone():
                        continue
                    receipt = await self.client.post(
                        "/walletsolidity/gettransactioninfobyid",
                        json={"value": row.get("transaction_id")},
                    )
                    receipt.raise_for_status()
                    self.ingest(row, receipt.json())
                    if (
                        row.get("block_timestamp", 0) / 1000
                        < state["watermark"] - 86400
                    ):
                        self.store.audit(
                            self.store.db,
                            "chain",
                            "late_index_detected",
                            {
                                "tx": row.get("transaction_id"),
                                "stamp": row.get("block_timestamp"),
                            },
                        )
                        with self.store.tx() as db:
                            self.store.put(
                                db,
                                "usdt_scan_warning",
                                "发现延迟索引交易，请查看充值核查记录",
                            )
                fingerprint = body.get("meta", {}).get("fingerprint")
                if fingerprint and fingerprint == job["fingerprint"]:
                    raise Rejected("Chain pagination cursor did not advance")
                with self.store.tx() as db:
                    if fingerprint:
                        job["fingerprint"] = fingerprint
                    else:
                        if job["daily"]:
                            state["last_rescan"] = now
                        else:
                            state["watermark"] = max(state["watermark"], job["end"])
                        state["job"] = None
                    self.store.put(db, key, state)
                    self.store.put(
                        db,
                        "usdt_health",
                        {
                            "at": self.store.clock(),
                            "error": ""
                            if not fingerprint and state["watermark"] >= now - 180
                            else "充值扫描积压，正在恢复",
                            "watermark": state["watermark"],
                        },
                    )
                if not fingerprint:
                    return
                params["fingerprint"] = fingerprint
            # The exact window and cursor remain pending for the next bounded run.
        except Exception as exc:
            with self.store.tx() as db:
                self.store.put(
                    db,
                    "usdt_health",
                    {
                        "at": self.store.clock(),
                        "error": type(exc).__name__,
                        "watermark": state["watermark"],
                    },
                )
            raise

    async def close(self):
        await self.client.aclose()
