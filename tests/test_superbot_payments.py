"""Offline confirmed-chain, separate-ledger and idempotency verification."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from data.plugins.astrbot_plugin_superbot.ads import Ads
from data.plugins.astrbot_plugin_superbot.payments import (
    TRANSFER,
    USDT,
    Payments,
    address_hex,
)
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store

ADDRESS = "TYVfVgdBiEbPAqQ4aGYgsPCMXywJt87X9X"
SENDER = "TP7ouPn6uoYSyjweRduBeKBoyyi69n2nbj"


@pytest.mark.asyncio
async def test_deposit_qr_owner_and_expiry(wallet):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from data.plugins.astrbot_plugin_superbot.ui import UI

    pay, clock = wallet
    order = pay.create("2", 10, "qr-order")
    bot = SimpleNamespace(send_photo=AsyncMock())
    ui = UI(SimpleNamespace(store=pay.store, payments=pay, bot=bot))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2), effective_chat=SimpleNamespace(id=2)
    )
    await ui.action(update, {"action": "deposit_qr", "id": order["id"]})
    assert bot.send_photo.call_args.kwargs["photo"].getvalue().startswith(b"\x89PNG")
    assert ADDRESS in bot.send_photo.call_args.kwargs["caption"]
    update.effective_user.id = 3
    with pytest.raises(Rejected):
        await ui.action(update, {"action": "deposit_qr", "id": order["id"]})
    update.effective_user.id = 2
    clock[0] += 1801
    with pytest.raises(Rejected):
        await ui.action(update, {"action": "deposit_qr", "id": order["id"]})


@pytest.fixture
def wallet(tmp_path):
    clock = [1800000000.0]
    store = Store(tmp_path / "db.sqlite", "1", lambda: clock[0])
    pay = Payments(store, {"usdt_enabled": True, "usdt_address": ADDRESS})
    with store.tx() as db:
        store.put(db, "usdt_health", {"at": clock[0], "error": ""})
        store.credit(db, "seed", "2", 123, "fixture")
    yield pay, clock
    asyncio.run(pay.close())
    store.close()


def evidence(order, tx="a" * 64):
    stamp = int((order["created"] + 10) * 1000)
    row = {
        "transaction_id": tx,
        "token_info": {"address": USDT, "decimals": 6},
        "type": "Transfer",
        "to": ADDRESS,
        "from": SENDER,
        "value": str(order["amount"]),
        "block_timestamp": stamp,
    }
    receipt = {
        "id": tx,
        "blockNumber": 100,
        "blockTimeStamp": stamp,
        "receipt": {"result": "SUCCESS"},
        "log": [
            {
                "address": address_hex(USDT)[2:],
                "topics": [
                    TRANSFER,
                    "0" * 24 + address_hex(SENDER)[2:],
                    "0" * 24 + address_hex(ADDRESS)[2:],
                ],
                "data": f"{order['amount']:064x}",
            }
        ],
    }
    return row, receipt


def test_credit_once_and_no_points(wallet):
    p, _ = wallet
    order = p.create("2", 10, "one")
    row, receipt = evidence(order)
    p.ingest(row, receipt)
    p.ingest(row, receipt)
    assert p.balance("2") == order["amount"]
    assert p.store.balance("2") == 123
    assert p.create("2", 10, "one")["id"] == "one"
    with pytest.raises(Rejected):
        p.create("3", 10, "one")


@pytest.mark.parametrize(
    "case", ["token", "recipient", "failed", "missing", "log", "multiple", "timestamp"]
)
def test_invalid_receipts_never_credit(wallet, case):
    p, _ = wallet
    row, receipt = evidence(p.create("2", 10, "one"))
    if case == "token":
        row["token_info"]["address"] = ADDRESS
    if case == "recipient":
        row["to"] = SENDER
    if case == "failed":
        receipt["receipt"]["result"] = "REVERT"
    if case == "missing":
        receipt = {}
    if case == "log":
        receipt["log"][0]["data"] = "1"
    if case == "multiple":
        receipt["log"] *= 2
    if case == "timestamp":
        receipt["blockTimeStamp"] = 1
    with pytest.raises(Rejected):
        p.ingest(row, receipt)
    assert p.balance("2") == 0


def test_old_late_and_unmatched_kept_for_review(wallet):
    p, clock = wallet
    order = p.create("2", 10, "one")
    order["created"] += 1900
    clock[0] += 2000
    p.ingest(*evidence(order))
    assert p.balance("2") == 0
    assert (
        p.store.db.execute("SELECT status FROM chain_events").fetchone()[0] == "review"
    )


def test_payment_atomic_refund_and_isolation(wallet):
    p, _ = wallet
    order = p.create("2", 10, "one")
    p.ingest(*evidence(order))
    s = p.store
    cfg = {
        "name": "test",
        "price": "5.00",
        "currency": "USDT",
        "target": "-100123",
        "enabled": True,
    }
    with s.tx() as db:
        s.put(db, "modules", {"ads": True})
        s.put(db, "packages", {"test": cfg})
    package = json.dumps(cfg)
    s.db.execute(
        "INSERT INTO ads(id,uid,kind,body,contact,package,at) VALUES('ad','2','供应','body','contact',?,0)",
        (package,),
    )
    p.pay("2", "ad", 5000000)
    p.pay("2", "ad", 5000000)
    assert p.balance("2") == order["amount"] - 5000000
    with pytest.raises(Rejected):
        Ads(s).moderate("1", "ad", "paid", "manual")
    Ads(s).moderate("1", "ad", "reject", "not accepted")
    assert p.balance("2") == order["amount"]
    assert s.balance("2") == 123


def test_concurrent_deduction_no_overdraft(wallet):
    p, _ = wallet
    with p.store.tx() as db:
        p.move(db, "seed-usdt", "2", 1000000, "fixture")

    def debit(i):
        s = Store(p.store.path, "1")
        other = object.__new__(Payments)
        other.store = s
        try:
            with s.tx() as db:
                other.move(db, "pay:" + str(i), "2", -1000000, "fixture")
            return True
        except Rejected:
            return False
        finally:
            s.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(debit, [1, 2]))
    assert sum(results) == 1 and p.balance("2") == 0


def test_address_and_stale_health(wallet):
    p, clock = wallet
    assert address_hex(ADDRESS).startswith("41")
    with pytest.raises(Rejected):
        address_hex(ADDRESS[:-1] + "Y")
    clock[0] += 200
    with pytest.raises(Rejected):
        p.create("2", 10, "new")


@pytest.mark.asyncio
async def test_scan_failure_blocks_new_orders_then_recovers(wallet):
    from unittest.mock import AsyncMock

    import httpx

    p, clock = wallet
    await p.client.aclose()
    p.client = AsyncMock()
    p.client.get.side_effect = httpx.ReadTimeout("fixture")
    with pytest.raises(httpx.ReadTimeout):
        await p.tick()
    with pytest.raises(Rejected):
        p.create("2", 1, "blocked")
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://api.trongrid.io/test"),
        json={"success": True, "data": [], "meta": {}},
    )
    p.client.get.side_effect = None
    p.client.get.return_value = response
    await p.tick()
    assert not p.store.get("usdt_health")["error"]
    assert p.create("2", 1, "recovered")["status"] == "pending"


def test_restart_replays_without_double_credit(wallet):
    p, _ = wallet
    order = p.create("2", 1, "restart")
    row, receipt = evidence(order)
    p.ingest(row, receipt)
    other_store = Store(p.store.path, "1", p.store.clock)
    other = Payments(other_store, p.config)
    try:
        other.ingest(row, receipt)
        assert other.balance("2") == order["amount"]
        assert (
            other_store.db.execute("SELECT COUNT(*) FROM ad_money").fetchone()[0] == 1
        )
    finally:
        asyncio.run(other.close())
        other_store.close()
