"""Offline tenant isolation and non-custodial advertising acceptance."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data.plugins.astrbot_plugin_superbot.ads import Ads
from data.plugins.astrbot_plugin_superbot.group_points import migrate as points_migrate
from data.plugins.astrbot_plugin_superbot.merchant_ads import MerchantAds, authorized
from data.plugins.astrbot_plugin_superbot.moderation import Moderation
from data.plugins.astrbot_plugin_superbot.payments import (
    TRANSFER,
    USDT,
    Payments,
    address_hex,
)
from data.plugins.astrbot_plugin_superbot.points import DEFAULT, Points
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store
from data.plugins.astrbot_plugin_superbot.tenants import Tenants, binding

ADDRESS = "TYVfVgdBiEbPAqQ4aGYgsPCMXywJt87X9X"
SENDER = "TP7ouPn6uoYSyjweRduBeKBoyyi69n2nbj"


@pytest.fixture
def merchant(tmp_path):
    clock = [1800000000.0]
    store = Store(tmp_path / "tenant.sqlite", "1", lambda: clock[0])
    points_migrate(store)
    runtime = SimpleNamespace(store=store, report=lambda *args: None)
    owners = {"-1001": "10", "-1002": "20", "-1003": "10"}

    async def member(chat, uid):
        return SimpleNamespace(
            status="administrator"
            if uid == 999
            else "creator"
            if owners.get(str(chat)) == str(uid)
            else "member",
            can_pin_messages=True,
        )

    runtime.bot = SimpleNamespace(
        id=999,
        get_chat=AsyncMock(
            return_value=SimpleNamespace(type="supergroup", title="test")
        ),
        get_chat_member=AsyncMock(side_effect=member),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
    )
    runtime.moderation = Moderation(runtime)
    runtime.tenants = Tenants(runtime)
    runtime.payments = Payments(store, {})
    runtime.merchant_ads = MerchantAds(runtime)
    runtime.points = Points(store)
    runtime.ads = Ads(store)
    runtime.ads.runtime = runtime
    with store.tx() as db:
        store.put(
            db,
            "modules",
            {
                "ads": True,
                "game": True,
                "points": True,
                "moderation": True,
                "wheel": True,
                "k3": True,
            },
        )
    runtime.tenants.configure_policy(
        "1",
        {
            "public": False,
            "invoices": True,
            "max_groups": 3,
            "pilot_owners": ["10", "20"],
        },
    )
    for chat, uid in (("-1001", "10"), ("-1002", "20"), ("-1003", "10")):
        asyncio.run(runtime.tenants.bind(uid, chat, runtime.tenants.invite(uid)))
        runtime.tenants.switch(
            uid, chat, "group", True, binding(store, chat)["version"]
        )
        runtime.tenants.switch(uid, chat, "ads", True, binding(store, chat)["version"])
    runtime.merchant_ads.set_address("10", "-1001", ADDRESS, 0)
    store.db.execute("UPDATE merchant_scans SET at=?,error=''", (clock[0],))
    runtime.merchant_ads.configure(
        "10",
        "-1001",
        "pkg1",
        {
            "name": "test ad",
            "kind": "normal",
            "price": "10.00",
            "duration": "30days",
            "slots": 10,
            "enabled": True,
        },
    )
    yield runtime, clock, owners
    asyncio.run(runtime.payments.close())
    store.close()


def invoice(runtime, key="order1", buyer="30"):
    runtime.merchant_ads.submit(buyer, "pkg1", "test creative", "test contact", 1, key)
    row = runtime.merchant_ads.order(buyer, key)
    return runtime.merchant_ads.approve("10", key, row["version"])


def transfer(order, tx="a" * 64, amount=None, stamp=None, to=None):
    amount = order["amount"] if amount is None else amount
    recipient = to or order["address"]
    stamp = int((order["created"] + 10) * 1000) if stamp is None else int(stamp * 1000)
    index = dict(
        transaction_id=tx,
        token_info={"address": USDT, "decimals": 6},
        type="Transfer",
        to=recipient,
        **{"from": SENDER},
        value=str(amount),
        block_timestamp=stamp,
    )
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
                    "0" * 24 + address_hex(recipient)[2:],
                ],
                "data": f"{amount:064x}",
            }
        ],
    }
    return index, receipt


def test_no_global_role_and_cross_owner_access(merchant):
    r, _, _ = merchant
    assert not r.store.allowed("10")
    r.store.require("10", "points", chat="-1001")
    with pytest.raises(Rejected):
        r.store.require("20", "points", chat="-1001")
    token = r.store.grant("1", "40", ["manager"], 1)
    r.store.accept_grant("40", token)
    with pytest.raises(Rejected):
        r.store.require("40", "ads", chat="-1001")
    with pytest.raises(Rejected):
        r.store.grant("10", "50", ["manager"], 1)


@pytest.mark.asyncio
async def test_binding_owner_and_replay(merchant):
    r, _, _ = merchant
    with pytest.raises(Rejected):
        await r.tenants.bind("20", "-1001", r.tenants.invite("20"))
    with pytest.raises(Rejected):
        await r.tenants.bind("10", "-1001", r.tenants.invite("10"))
    with pytest.raises(Rejected):
        r.tenants.invite("99")


@pytest.mark.asyncio
async def test_platform_resource_budget_menu_renders_sqlite_rows(merchant):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from data.plugins.astrbot_plugin_superbot.tenant_ui import action
    from data.plugins.astrbot_plugin_superbot.ui import UI

    runtime, _, _ = merchant
    runtime.tenants.resource_grant("1", "-1001", "avatar", 3, 0)
    ui = UI(runtime)
    ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1, type="private"),
        callback_query=None,
    )
    await action(ui, update, {"action": "tenant_resources", "chat": "-1001"})
    text = ui.render.await_args.args[1]
    assert "制图：开启" in text
    assert "0/3 次" in text


def test_rewards_are_scoped_and_settings_cannot_cross_groups(merchant):
    r, _, _ = merchant
    config = {**DEFAULT, "enabled": True, "checkin": 17, "groups": ["-1001"]}
    r.tenants.settings("10", "-1001", "points", config, 1)
    r.points.checkin("30", "-1001")
    assert r.store.balance("30", "-1001") == 17
    assert r.store.balance("30", "-1003") == 0
    with pytest.raises(Rejected):
        r.tenants.settings("20", "-1001", "points", config, 2)
    with pytest.raises(Rejected):
        r.tenants.settings("10", "-1003", "points", config, 1)


def test_direct_payment_never_credits_platform_or_points(merchant):
    r, _, _ = merchant
    order = invoice(r)
    assert 1 <= order["amount"] - order["base"] <= 999
    assert r.merchant_ads.ingest(ADDRESS, *transfer(order)) == "paid"
    assert r.merchant_ads.ingest(ADDRESS, *transfer(order)) == "paid"
    assert authorized(r.store, r.merchant_ads.order("30", order["id"]))
    assert r.payments.balance("30") == 0
    assert r.store.balance("30", "-1001") == 0
    assert r.store.db.execute("SELECT COUNT(*) FROM payment_claims").fetchone()[0] == 1


def test_order_identity_and_cross_owner(merchant):
    r, _, _ = merchant
    order = invoice(r)
    assert invoice(r) == order
    with pytest.raises(Rejected):
        r.merchant_ads.order("20", order["id"])
    with pytest.raises(Rejected):
        r.merchant_ads.submit(
            "31", "pkg1", "test creative", "test contact", 1, order["id"]
        )
    with pytest.raises(Rejected):
        r.merchant_ads.approve("20", order["id"], 1)


@pytest.mark.parametrize(
    "case",
    [
        "wrong_token",
        "wrong_recipient",
        "missing",
        "duplicate_logs",
        "fraction",
        "failed",
    ],
)
def test_invalid_evidence_never_marks_paid(merchant, case):
    r, _, _ = merchant
    order = invoice(r)
    index, receipt = transfer(order)
    if case == "wrong_token":
        index["token_info"]["address"] = ADDRESS
    if case == "wrong_recipient":
        index["to"] = SENDER
    if case == "missing":
        receipt = {}
    if case == "duplicate_logs":
        receipt["log"] *= 2
    if case == "fraction":
        index["value"] = "100.1"
    if case == "failed":
        receipt["receipt"]["result"] = "REVERT"
    with pytest.raises(Rejected):
        r.merchant_ads.ingest(ADDRESS, index, receipt)
    assert not r.merchant_ads.order("30", order["id"])["paid"]


@pytest.mark.parametrize("delta", [-1, 1])
def test_mismatched_amount_requires_review(merchant, delta):
    r, _, _ = merchant
    order = invoice(r)
    assert (
        r.merchant_ads.ingest(ADDRESS, *transfer(order, amount=order["amount"] + delta))
        == "review"
    )
    assert not r.merchant_ads.order("30", order["id"])["paid"]


def test_late_and_delayed_valid_payment_do_not_overbook(merchant):
    r, clock, _ = merchant
    order = invoice(r)
    clock[0] += 2000
    r.merchant_ads.expire()
    assert r.merchant_ads.ingest(ADDRESS, *transfer(order)) == "paid_waiting"
    assert not authorized(r.store, r.merchant_ads.order("30", order["id"]))
    r.store.db.execute("UPDATE merchant_scans SET at=?", (clock[0],))
    order2 = invoice(r, "order2")
    clock[0] += 2000
    assert (
        r.merchant_ads.ingest(ADDRESS, *transfer(order2, tx="b" * 64, stamp=clock[0]))
        == "review"
    )


def test_address_is_frozen_and_foreign_owner_cannot_reuse(merchant):
    r, _, _ = merchant
    order = invoice(r)
    r.merchant_ads.set_address("10", "-1001", SENDER, 1)
    assert (
        r.store.db.execute("SELECT address FROM merchant_invoices").fetchone()[0]
        == ADDRESS
    )
    assert r.merchant_ads.ingest(ADDRESS, *transfer(order)) == "paid"
    with pytest.raises(Rejected):
        r.merchant_ads.set_address("20", "-1002", ADDRESS, 0)
    r.merchant_ads.set_address("10", "-1003", ADDRESS, 0)


def test_stale_health_closed_invoices_and_capacity_no_invoice(merchant):
    r, _, _ = merchant
    invoice(r)
    r.store.db.execute("UPDATE merchant_scans SET error='RateLimit'")
    with pytest.raises(Rejected):
        invoice(r, "order2")
    assert (
        r.store.db.execute("SELECT COUNT(*) FROM merchant_invoices").fetchone()[0] == 1
    )
    r.store.db.execute("UPDATE merchant_scans SET error=''")
    cfg = json.loads(
        r.store.db.execute(
            "SELECT config FROM merchant_packages WHERE id='pkg1'"
        ).fetchone()[0]
    )
    r.merchant_ads.configure("10", "-1001", "pkg1", {**cfg, "slots": 1}, 1)
    with pytest.raises(Rejected):
        r.merchant_ads.approve("10", "order2", 1)


def test_legacy_buttons_cannot_pay_edit_or_cancel_merchant(merchant):
    r, _, _ = merchant
    order = invoice(r)
    with pytest.raises(Rejected):
        r.payments.pay("30", order["id"], order["base"])
    with pytest.raises(Rejected):
        r.ads.moderate("1", order["id"], "paid")
    with pytest.raises(Rejected):
        r.ads.edit("1", order["id"], "replacement", "test creative")
    with pytest.raises(Rejected):
        r.ads.cancel("1", order["id"])


def test_refund_requires_customer_address_full_amount_and_unique_tx(merchant):
    r, clock, _ = merchant
    order = invoice(r)
    r.merchant_ads.ingest(ADDRESS, *transfer(order))
    row = r.store.db.execute("SELECT * FROM merchant_invoices").fetchone()
    with pytest.raises(Rejected):
        r.merchant_ads.refund_request("10", order["id"], SENDER, row["version"])
    r.merchant_ads.refund_request("30", order["id"], SENDER, row["version"])
    row = r.store.db.execute("SELECT * FROM merchant_invoices").fetchone()
    bad = transfer(
        order, tx="b" * 64, to=SENDER, amount=order["base"], stamp=clock[0] + 1
    )
    with pytest.raises(Rejected):
        r.merchant_ads.refund_proof("10", order["id"], row["version"], *bad)
    proof = transfer(order, tx="b" * 64, to=SENDER, stamp=clock[0] + 1)
    r.merchant_ads.refund_proof("10", order["id"], row["version"], *proof)
    assert (
        r.store.db.execute("SELECT status FROM merchant_invoices").fetchone()[0]
        == "refunded"
    )
    with pytest.raises(Rejected):
        r.merchant_ads.refund_proof("10", order["id"], row["version"], *proof)


@pytest.mark.asyncio
async def test_owner_change_suspends_group_without_balances_changed(merchant):
    r, _, owners = merchant
    owners["-1001"] = "99"
    with pytest.raises(Rejected):
        await r.tenants.verify("10", "-1001")
    assert binding(r.store, "-1001")["status"] == "suspended"
    assert (
        r.store.db.execute(
            "SELECT enabled FROM mod_groups WHERE chat='-1001'"
        ).fetchone()[0]
        == 0
    )


def test_paid_resources_not_inherited_by_owner_or_group(merchant):
    r, _, _ = merchant
    assert not r.tenants.resource_allowed("10")
    assert not r.tenants.resource_allowed("30", "-1001")
    assert r.tenants.resource_allowed("1")


def test_platform_grant_is_scoped_and_consumed_once(merchant):
    r, _, _ = merchant
    r.store.db.execute(
        "INSERT INTO mod_members(chat,uid,name,username,seen) VALUES(?,?,?,?,?)",
        ("-1001", "30", "buyer", "buyer", 1),
    )
    r.tenants.resource_grant("1", "-1001", "avatar", 2, 0)
    assert r.tenants.resource_allowed("30", "-1001", "avatar")
    with r.store.tx() as db:
        r.tenants.consume_resource("30", "-1001", "avatar", "job-1", db)
    assert (
        r.store.db.execute(
            "SELECT used FROM tenant_resource_grants WHERE chat='-1001' AND resource='avatar'"
        ).fetchone()[0]
        == 1
    )
    with r.store.tx() as db:
        with pytest.raises(Rejected):
            r.tenants.consume_resource("30", "-1001", "avatar", "job-1", db)
    with r.store.tx() as db:
        r.tenants.consume_resource("30", "-1001", "avatar", "job-2", db)
    assert not r.tenants.resource_allowed("30", "-1001", "avatar")


def test_private_resource_finds_member_budget_without_cross_tenant_fallback(merchant):
    r, _, _ = merchant
    r.store.db.execute(
        "INSERT INTO mod_members(chat,uid,name,username,seen) VALUES(?,?,?,?,?)",
        ("-1003", "30", "buyer", "buyer", 2),
    )
    r.tenants.resource_grant("1", "-1003", "chat", 1, 0)
    assert r.tenants.resource_allowed("30", None, "chat")
    assert not r.tenants.resource_allowed("31", None, "chat")


def test_historical_transfer_without_watch_window_is_unrelated(merchant):
    r, clock, _ = merchant
    order = invoice(r)
    clock[0] += 4000
    r.merchant_ads.expire()
    old = transfer(order, tx="c" * 64, stamp=order["created"] - 100)
    assert r.merchant_ads.ingest(ADDRESS, *old) == "unrelated"
    assert (
        r.store.db.execute(
            "SELECT COUNT(*) FROM merchant_events WHERE tx=?", ("c" * 64,)
        ).fetchone()[0]
        == 0
    )
