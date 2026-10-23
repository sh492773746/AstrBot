"""Offline tenant isolation and non-custodial advertising acceptance."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


@pytest.mark.asyncio
async def test_auto_owner_correction_rejects_member_and_requires_separate_restore(merchant):
    runtime, _, owners = merchant
    store = runtime.store
    store.db.execute("UPDATE tenant_groups SET mode='auto' WHERE chat='-1003'")
    store.suspend_group("-1003")
    owners["-1003"] = "30"
    version = binding(store, "-1003")["version"]
    with pytest.raises(Rejected, match="现任群主"):
        await runtime.tenants.claim_owner("10", "-1003", version)
    await runtime.tenants.claim_owner("30", "-1003", version)
    row = binding(store, "-1003")
    assert row["owner"] == "30" and row["mode"] == "verified"
    assert row["status"] == "suspended"
    assert store.db.execute(
        "SELECT enabled FROM mod_groups WHERE chat='-1003'"
    ).fetchone()[0] == 0
    with pytest.raises(Rejected):
        await runtime.tenants.claim_owner("30", "-1003", version)
    await runtime.tenants.restore("30", "-1003", row["version"])
    assert binding(store, "-1003")["status"] == "active"
    assert binding(store, "-1001")["owner"] == "10"


@pytest.mark.asyncio
async def test_auto_owner_correction_cannot_transfer_financial_configuration(merchant):
    runtime, _, owners = merchant
    runtime.store.db.execute("UPDATE tenant_groups SET mode='auto' WHERE chat='-1001'")
    owners["-1001"] = "30"
    before = dict(binding(runtime.store, "-1001"))
    with pytest.raises(Rejected, match="人工核查"):
        await runtime.tenants.claim_owner("30", "-1001", before["version"])
    assert dict(binding(runtime.store, "-1001")) == before


@pytest.mark.asyncio
async def test_auto_owner_correction_rechecks_version_after_telegram(merchant):
    runtime, _, owners = merchant
    runtime.store.db.execute("UPDATE tenant_groups SET mode='auto' WHERE chat='-1003'")
    owners["-1003"] = "30"
    before = binding(runtime.store, "-1003")["version"]
    original = runtime.bot.get_chat_member.side_effect

    async def changed(chat, uid):
        if uid == runtime.bot.id:
            runtime.store.db.execute(
                "UPDATE tenant_groups SET version=version+1 WHERE chat='-1003'"
            )
        return await original(chat, uid)

    runtime.bot.get_chat_member.side_effect = changed
    with pytest.raises(Rejected, match="状态已变化"):
        await runtime.tenants.claim_owner("30", "-1003", before)
    assert binding(runtime.store, "-1003")["owner"] == "10"


@pytest.mark.asyncio
async def test_admin_can_confirm_owner_and_restore_through_private_buttons(merchant):
    from data.plugins.astrbot_plugin_superbot.tenant_ui import action
    from data.plugins.astrbot_plugin_superbot.ui import UI

    runtime, _, owners = merchant
    store = runtime.store
    store.db.execute("UPDATE tenant_groups SET mode='auto' WHERE chat='-1003'")
    store.suspend_group("-1003")
    owners["-1003"] = "30"
    original = runtime.bot.get_chat_member.side_effect

    async def member(chat, uid):
        if str(chat) == "-1003" and uid == 10:
            return SimpleNamespace(status="administrator")
        return await original(chat, uid)

    runtime.bot.get_chat_member.side_effect = member
    runtime.bot.get_chat_administrators = AsyncMock(return_value=[
        SimpleNamespace(status="administrator", user=SimpleNamespace(id=10)),
        SimpleNamespace(status="creator", user=SimpleNamespace(id=30)),
    ])
    ui = UI(runtime)
    ui.render = AsyncMock()
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(id=10, type="private"),
    )
    await action(ui, event, {"action": "tenant_group", "chat": "-1003"})
    buttons = dict(ui.render.await_args.args[2])
    await action(ui, event, buttons["确认经营归属"])
    confirmation = ui.render.await_args.args[2][0][1]
    await action(ui, event, confirmation)
    row = binding(store, "-1003")
    assert row["owner"] == "30" and row["status"] == "suspended"
    assert "广告位与订单" not in dict(ui.render.await_args.args[2])
    buttons = dict(ui.render.await_args.args[2])
    await action(ui, event, buttons["确认重新接入"])
    await action(ui, event, ui.render.await_args.args[2][0][1])
    assert binding(store, "-1003")["owner"] == "30"
    assert binding(store, "-1003")["status"] == "active"
    assert store.db.execute(
        "SELECT enabled FROM mod_groups WHERE chat='-1003'"
    ).fetchone()[0] == 1
    with pytest.raises(Rejected):
        await action(ui, event, {"action": "tenant_address", "chat": "-1003"})
    audit = store.db.execute(
        "SELECT actor,data FROM audit WHERE action='tenant_owner_confirmed' ORDER BY id DESC"
    ).fetchone()
    assert audit["actor"] == "10" and json.loads(audit["data"])["owner"] == "30"
    assert binding(store, "-1001")["owner"] == "10"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "left", "kicked"])
@pytest.mark.parametrize("operation", ["claim_owner", "restore"])
async def test_owner_confirmation_and_restore_reject_non_admins(merchant, role, operation):
    runtime, _, _ = merchant
    store = runtime.store
    store.db.execute("UPDATE tenant_groups SET mode='auto' WHERE chat='-1003'")
    store.suspend_group("-1003")
    before = dict(binding(store, "-1003"))
    original = runtime.bot.get_chat_member.side_effect

    async def member(chat, uid):
        if uid == 20:
            return SimpleNamespace(status=role)
        return await original(chat, uid)

    runtime.bot.get_chat_member.side_effect = member
    with pytest.raises(Rejected):
        await getattr(runtime.tenants, operation)("20", "-1003", before["version"])
    assert dict(binding(store, "-1003")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["actor_revoked", "owner_changed", "bot_demoted"])
async def test_admin_owner_confirmation_rechecks_after_creator_lookup(merchant, change):
    runtime, _, owners = merchant
    store = runtime.store
    store.db.execute("UPDATE tenant_groups SET mode='auto' WHERE chat='-1003'")
    store.suspend_group("-1003")
    owners["-1003"] = "30"
    before = dict(binding(store, "-1003"))
    original = runtime.bot.get_chat_member.side_effect
    changed = False

    async def member(chat, uid):
        if uid == 20:
            return SimpleNamespace(
                status="member" if changed and change == "actor_revoked" else "administrator"
            )
        if uid == runtime.bot.id and changed and change == "bot_demoted":
            return SimpleNamespace(status="member")
        return await original(chat, uid)

    async def administrators(chat):
        nonlocal changed
        changed = True
        if change == "owner_changed":
            owners["-1003"] = "40"
        return [SimpleNamespace(status="creator", user=SimpleNamespace(id=30))]

    runtime.bot.get_chat_member.side_effect = member
    runtime.bot.get_chat_administrators = AsyncMock(side_effect=administrators)
    with pytest.raises(Rejected, match="权限已变化"):
        await runtime.tenants.claim_owner("20", "-1003", before["version"])
    assert dict(binding(store, "-1003")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["claim_owner", "restore"])
async def test_admin_cannot_override_platform_suspension(merchant, operation):
    runtime, _, _ = merchant
    store = runtime.store
    store.db.execute(
        "UPDATE tenant_groups SET mode='auto',status='suspended',error='platform_suspended' "
        "WHERE chat='-1003'"
    )
    before = dict(binding(store, "-1003"))
    with pytest.raises(Rejected):
        await getattr(runtime.tenants, operation)("10", "-1003", before["version"])
    assert dict(binding(store, "-1003")) == before


def authorize_owner_as_superadmin(runtime):
    runtime.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
        ("10", '["superadmin"]', runtime.store.clock() + 7200),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [None, False])
async def test_tenant_verification_waits_for_telegram_binding(merchant, running):
    runtime, _, _ = merchant
    runtime.application = None if running is None else SimpleNamespace(running=running)
    runtime.tenants.verify = AsyncMock()
    before = [tuple(r) for r in runtime.store.db.execute(
        "SELECT chat,status,error,version FROM tenant_groups ORDER BY chat"
    )]
    with patch(
        "data.plugins.astrbot_plugin_superbot.tenants.asyncio.sleep",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await runtime.tenants.loop()
    runtime.tenants.verify.assert_not_awaited()
    assert [tuple(r) for r in runtime.store.db.execute(
        "SELECT chat,status,error,version FROM tenant_groups ORDER BY chat"
    )] == before


def test_temporary_delivery_switch_does_not_fake_payment(merchant):
    runtime, clock, _ = merchant
    ads = runtime.merchant_ads
    store = runtime.store
    ads.submit("30", "pkg1", "TEST ONLY", "contact", 1, "temporary-test-order")
    row = ads.order("30", "temporary-test-order")

    with pytest.raises(Rejected, match="仅超管"):
        ads.test_switch("10", "-1001", True)
    with pytest.raises(Rejected, match="权限"):
        ads.test_publish("1", row["id"], row["version"])
    with pytest.raises(Rejected):
        ads.test_switch("1", "-1001", True)
    authorize_owner_as_superadmin(runtime)
    with pytest.raises(Rejected, match="未开启"):
        ads.test_publish("10", row["id"], row["version"])
    ads.test_switch("10", "-1001", True)
    ads.test_publish("10", row["id"], row["version"])
    published = ads.order("30", row["id"])
    assert published["status"] == "pending"
    assert published["payment_source"] == "test"
    assert published["body"].startswith("【测试广告·未付款】")
    assert authorized(store, published)
    assert not store.db.execute(
        "SELECT 1 FROM merchant_invoices WHERE id=?", (row["id"],)
    ).fetchone()
    assert not store.db.execute(
        "SELECT 1 FROM payment_claims WHERE op=?", (row["id"],)
    ).fetchone()
    with pytest.raises(Rejected, match="权限"):
        ads.test_publish("1", row["id"], published["version"])

    ads.test_switch("10", "-1001", False)
    assert not authorized(store, ads.order("30", row["id"]))
    ads.test_switch("10", "-1001", True)
    clock[0] += 3601
    assert not authorized(store, ads.order("30", row["id"]))


def test_no_address_test_order_cannot_be_submitted_by_normal_customer(merchant):
    runtime, _, _ = merchant
    authorize_owner_as_superadmin(runtime)
    ads = runtime.merchant_ads
    runtime.store.db.execute("DELETE FROM merchant_addresses WHERE chat='-1001'")
    ads.test_switch("10", "-1001", True)
    with pytest.raises(Rejected, match="收款地址"):
        ads.submit("30", "pkg1", "normal", "contact", 1, "no-address-normal")
    key = ads.submit("10", "pkg1", "test only", "contact", 1, "no-address-test")
    assert ads.order("10", key)["status"] == "merchant_review"
    assert not runtime.store.db.execute(
        "SELECT 1 FROM merchant_invoices WHERE id=?", (key,)
    ).fetchone()
    ads.test_switch("10", "-1001", False)
    with pytest.raises(Rejected, match="收款地址"):
        ads.submit("10", "pkg1", "expired", "contact", 1, "no-address-expired")


@pytest.mark.asyncio
async def test_temporary_delivery_publishes_labeled_message_only_while_enabled(merchant):
    runtime, _, _ = merchant
    authorize_owner_as_superadmin(runtime)
    ads = runtime.merchant_ads
    ads.submit("30", "pkg1", "TEST ONLY", "contact", 1, "publish-test-order")
    ads.test_switch("10", "-1001", True)
    ads.test_publish("10", "publish-test-order", 1)
    await runtime.ads.tick(runtime.bot)
    order = ads.order("30", "publish-test-order")
    assert order["status"] == "published", (order["status"], order["error"])
    runtime.bot.send_message.assert_awaited_once()
    call = runtime.bot.send_message.await_args
    assert call.kwargs["chat_id"] == "-1001"
    assert "测试广告·未付款" in call.kwargs["text"]
    assert not runtime.store.db.execute(
        "SELECT 1 FROM merchant_invoices WHERE id=?", (order["id"],)
    ).fetchone()


@pytest.mark.asyncio
async def test_expired_delivery_switch_cannot_send_to_group(merchant):
    runtime, clock, _ = merchant
    authorize_owner_as_superadmin(runtime)
    ads = runtime.merchant_ads
    ads.submit("30", "pkg1", "TEST ONLY", "contact", 1, "expired-test-order")
    ads.test_switch("10", "-1001", True)
    ads.test_publish("10", "expired-test-order", 1)
    clock[0] += 3601
    await runtime.ads.tick(runtime.bot)
    runtime.bot.send_message.assert_not_awaited()
    assert ads.order("30", "expired-test-order")["status"] != "published"


@pytest.mark.asyncio
async def test_departure_preserves_finance_and_requires_original_owner_to_restore(merchant):
    runtime, _, owners = merchant
    store = runtime.store
    invoice(runtime, "depart-order")
    before = store.db.execute(
        "SELECT * FROM merchant_invoices WHERE id='depart-order'"
    ).fetchone()
    store.db.execute(
        "UPDATE ads SET status='pending',paid=1,approved=1 WHERE id='depart-order'"
    )
    store.suspend_group("-1001")
    store.suspend_group("-1001")
    store.register_group("-1001", "test", "10", fresh=True)
    assert dict(store.db.execute(
        "SELECT * FROM merchant_invoices WHERE id='depart-order'"
    ).fetchone()) == dict(before)
    assert runtime.merchant_ads.order("30", "depart-order")["status"] == "review"
    assert not store.db.execute(
        "SELECT enabled FROM mod_groups WHERE chat='-1001'"
    ).fetchone()[0]
    await runtime.ads.tick(runtime.bot)
    runtime.bot.send_message.assert_not_awaited()
    row = binding(store, "-1001")
    with pytest.raises(Rejected):
        await runtime.tenants.restore("20", "-1001", row["version"])
    owners["-1001"] = "20"
    with pytest.raises(Rejected, match="群主变更"):
        await runtime.tenants.restore("10", "-1001", row["version"])
    owners["-1001"] = "10"
    await runtime.tenants.restore("10", "-1001", row["version"])
    assert binding(store, "-1001")["status"] == "active"
    assert runtime.merchant_ads.order("30", "depart-order")["status"] == "review"
    with pytest.raises(Rejected):
        await runtime.tenants.restore("10", "-1001", row["version"])


@pytest.mark.asyncio
async def test_departure_during_send_cannot_complete_order(merchant):
    runtime, _, _ = merchant
    authorize_owner_as_superadmin(runtime)
    runtime.merchant_ads.submit("30", "pkg1", "TEST ONLY", "contact", 1, "mid-send")
    runtime.merchant_ads.test_switch("10", "-1001", True)
    runtime.merchant_ads.test_publish("10", "mid-send", 1)

    async def depart(**kwargs):
        runtime.store.suspend_group("-1001")
        return SimpleNamespace(message_id=88)

    runtime.bot.send_message.side_effect = depart
    await runtime.ads.tick(runtime.bot)
    assert runtime.merchant_ads.order("30", "mid-send")["status"] == "review"
    assert runtime.store.db.execute(
        "SELECT status FROM ad_operations WHERE chat='-1001' ORDER BY at DESC LIMIT 1"
    ).fetchone()["status"] == "review"


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
    r, _, owners = merchant
    with pytest.raises(Rejected):
        await r.tenants.bind("20", "-1001", r.tenants.invite("20"))
    with pytest.raises(Rejected):
        await r.tenants.bind("10", "-1001", r.tenants.invite("10"))
    owners["-10099"] = "99"
    token = r.tenants.invite("99")
    with pytest.raises(Rejected, match="自助接入已关闭"):
        await r.tenants.bind("99", "-10099", token)
    assert binding(r.store, "-10099") is None
    assert not r.store.db.execute("SELECT 1 FROM tenants WHERE owner='99'").fetchone()
    assert (
        r.store.db.execute(
            "SELECT used FROM tenant_bindings WHERE token=?", (token,)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("case", ["expired", "foreign", "replaced", "reused"])
@pytest.mark.asyncio
async def test_binding_challenge_cannot_be_replayed_or_borrowed(merchant, case):
    r, clock, owners = merchant
    owners.update({"-1004": "10", "-1005": "10"})
    token = r.tenants.invite("20" if case == "foreign" else "10")
    if case == "expired":
        clock[0] += 600
    elif case == "replaced":
        r.tenants.invite("10")
    elif case == "reused":
        await r.tenants.bind("10", "-1004", token)
    before = [
        tuple(row)
        for row in r.store.db.execute("SELECT * FROM tenant_groups ORDER BY chat")
    ]
    with pytest.raises(Rejected, match="绑定凭证"):
        await r.tenants.bind("10", "-1005", token)
    assert binding(r.store, "-1005") is None
    assert before == [
        tuple(row)
        for row in r.store.db.execute("SELECT * FROM tenant_groups ORDER BY chat")
    ]


def test_direct_payment_replay_after_reopen_is_idempotent(merchant):
    r, _, _ = merchant
    order = invoice(r)
    proof = transfer(order)
    assert r.merchant_ads.ingest(ADDRESS, *proof) == "paid"
    before = dict(r.merchant_ads.order("30", order["id"]))
    reopened = Store(r.store.path, "1", r.store.clock)
    try:
        service = MerchantAds(SimpleNamespace(store=reopened))
        assert service.ingest(ADDRESS, *proof) == "paid"
        assert dict(service.order("30", order["id"])) == before
        assert (
            reopened.db.execute("SELECT COUNT(*) FROM payment_claims").fetchone()[0]
            == 1
        )
        assert (
            reopened.db.execute("SELECT COUNT(*) FROM merchant_events").fetchone()[0]
            == 1
        )
        assert reopened.balance("30", "-1001") == 0
        assert r.payments.balance("30") == 0
    finally:
        reopened.close()


def test_one_payment_transaction_cannot_pay_two_orders(merchant):
    r, _, _ = merchant
    first = invoice(r, "first")
    second = invoice(r, "second")
    assert first["amount"] != second["amount"]
    assert r.merchant_ads.ingest(ADDRESS, *transfer(first)) == "paid"
    with pytest.raises(Rejected, match="Conflicting chain evidence"):
        r.merchant_ads.ingest(ADDRESS, *transfer(second))
    assert not r.merchant_ads.order("30", "second")["paid"]
    assert r.store.db.execute("SELECT COUNT(*) FROM payment_claims").fetchone()[0] == 1
    assert r.merchant_ads.ingest(ADDRESS, *transfer(second, tx="b" * 64)) == "paid"
    assert r.store.db.execute("SELECT COUNT(*) FROM payment_claims").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_platform_resource_budget_menu_renders_sqlite_rows(merchant):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from data.plugins.astrbot_plugin_superbot.tenant_ui import action
    from data.plugins.astrbot_plugin_superbot.ui import UI

    runtime, _, _ = merchant
    authorize_owner_as_superadmin(runtime)
    runtime.tenants.resource_grant("10", "-1001", "avatar", 3, 0)
    ui = UI(runtime)
    ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(id=1, type="private"),
        callback_query=None,
    )
    await action(ui, update, {"action": "tenant_resources", "chat": "-1001"})
    text = ui.render.await_args.args[1]
    assert "制图：限额启用" in text
    assert "0/3次" in text
    assert "AI客服：默认授权" in text


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


def test_shared_resources_default_on_but_private_context_still_required(merchant):
    r, _, _ = merchant
    assert not r.tenants.resource_allowed("10")
    assert r.tenants.resource_allowed("30", "-1001")
    assert r.tenants.resource_allowed("1")


def test_platform_grant_is_scoped_and_consumed_once(merchant):
    r, _, _ = merchant
    r.store.db.execute(
        "INSERT INTO mod_members(chat,uid,name,username,seen) VALUES(?,?,?,?,?)",
        ("-1001", "30", "buyer", "buyer", 1),
    )
    authorize_owner_as_superadmin(r)
    r.tenants.resource_grant("10", "-1001", "avatar", 2, 0)
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
    authorize_owner_as_superadmin(r)
    r.tenants.resource_grant("10", "-1003", "chat", 1, 0)
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
