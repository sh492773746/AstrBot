"""Plugin settlement authentication, shared ledger and retired-page regressions."""

import copy
import os
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback import CashbackService
from astrbot.builtin_stars.wangshangliao_moderation.cashback_dashboard import (
    CashierCall,
    dispatch,
)
from astrbot.builtin_stars.wangshangliao_moderation.cashback_finance import (
    DashboardFinanceActor,
)
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import CashbackError
from tests.test_wangshangliao_cashback import (
    OWNER,
    member,
    row,
    scan,
    select,
)
from tests.test_wangshangliao_cashback import (
    store as store,
)
from tests.test_wangshangliao_dashboard import (
    PREFIX,
)
from tests.test_wangshangliao_dashboard import (
    headers as headers,
)
from tests.test_wangshangliao_dashboard import (
    page_app as page_app,
)


@pytest.fixture
def settlement(page_app, store):
    _, page, bots, configs, _ = page_app
    config = configs["fixture"]
    config["account_id"] = "99"
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.account = "99"
    adapter.config = copy.deepcopy(config)
    domain = CashbackService(page.context, store.path.parent, collector_id="fixture")
    domain.store = store
    domain.group_name = lambda adapter, group: f"测试群{group}"
    page.cashback = SimpleNamespace(service=domain)
    store.configure(
        OWNER,
        {**store.get_group("5"), "bot_id": "fixture", "enabled": True},
        {"1": member("1")},
        {"7"},
    )
    return page, bots, configs, adapter, domain


@pytest.mark.asyncio
async def test_plugin_rejects_unauthenticated_or_forged_finance_identity(
    page_app, settlement, headers
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=page_app[0]), base_url="http://fixture"
    ) as client:
        response = await client.post(
            PREFIX + "/cashier", json={"action": "groups", "payload": {}}
        )
        assert response.status_code in {401, 403}
        response = await client.post(
            PREFIX + "/cashier",
            headers=headers,
            json={"action": "me", "payload": {"actor": OWNER, "role": "admin"}},
        )
        assert response.status_code == 400
        response = await client.post(
            PREFIX + "/cashier",
            headers=headers,
            json={"action": "me", "payload": {}},
        )
        assert response.status_code == 200
        assert response.json()["data"]["role"] == "admin"


@pytest.mark.asyncio
async def test_groups_remain_readable_offline_and_reject_cross_scope(settlement):
    page, bots, _, _, domain = settlement
    page.context.platform_manager.platform_insts.clear()
    groups = await dispatch(page, bots, "fixture-admin", CashierCall(action="groups"))
    assert next(item for item in groups if item["id"] == "5")["rate"] == 1800
    assert all("cashier" not in item and "account" not in item for item in groups)
    assert (await dispatch(page, bots, "fixture-admin", CashierCall(action="status")))[
        "read_only"
    ]
    with pytest.raises(CashbackError, match="group_forbidden"):
        await dispatch(
            page,
            bots,
            "fixture-admin",
            CashierCall(action="entries", payload={"group": "999"}),
        )
    assert domain.store.get_group("5")["rate"] == 1800


@pytest.mark.asyncio
async def test_dashboard_payment_reuses_ledger_idempotency_and_identity(settlement):
    page, bots, _, adapter, domain = settlement
    scan(
        domain.store,
        bot="fixture",
        notices=[
            {
                "member": "2",
                "inviter": "1",
                "source": "business_apply_log",
                "event_id": "plugin-join",
                "event_at": time() + 1,
            }
        ],
    )
    assert row(domain.store)["inviter"] == "1"
    domain._scan = AsyncMock(return_value={uid: member(uid) for uid in ("1", "2")})
    payload = {
        "group": "5",
        "operation": "pay",
        "items": [select(domain.store)],
        "request_id": "dashboard-payment-001",
    }
    first = await dispatch(
        page, bots, "fixture-admin", CashierCall(action="payments", payload=payload)
    )
    assert first["count"] == 1 and row(domain.store)["state"] == "paid"
    principal = str(DashboardFinanceActor("fixture-admin", lambda group: None))
    assert row(domain.store)["paid_by"] == principal
    assert row(domain.store)["paid_by"] != OWNER
    assert (
        await dispatch(
            page, bots, "fixture-admin", CashierCall(action="payments", payload=payload)
        )
        == first
    )
    adapter.config["_business_active"] = False
    with pytest.raises(CashbackError, match="business_service_inactive"):
        await dispatch(
            page,
            bots,
            "fixture-admin",
            CashierCall(
                action="payments",
                payload={**payload, "request_id": "dashboard-payment-002"},
            ),
        )


@pytest.mark.asyncio
async def test_scope_change_during_read_fails_closed(settlement):
    page, bots, configs, _, domain = settlement
    original = domain.store.groups

    def changed(actor):
        result = original(actor)
        configs["fixture"]["enabled_groups"] = ["6"]
        return result

    domain.store.groups = changed
    with pytest.raises(Exception, match="配置已变化"):
        await dispatch(page, bots, "fixture-admin", CashierCall(action="groups"))


@pytest.mark.asyncio
async def test_new_group_uses_distinct_nonlogin_audit_principal(settlement):
    page, bots, _, _, domain = settlement
    domain.roster = AsyncMock(return_value={"1": member("1")})
    domain.historical_members = lambda group: set()
    result = await dispatch(
        page,
        bots,
        "fixture-admin",
        CashierCall(
            action="admin/groups",
            payload={
                "id": "6",
                "bot_id": "fixture",
                "contact": "测试联系人",
                "rate": 500,
                "agent_rate": 1000,
                "enabled": True,
                "version": 0,
            },
        ),
    )
    assert result == {"status": "saved"}
    rule = domain.store.get_group("6")
    assert rule["cashier"].startswith("dashboard/")
    assert rule["rate"] == 500
    with domain.store.connect() as db:
        with pytest.raises(CashbackError, match="forbidden"):
            domain.store._actor(db, rule["cashier"])
    assert not domain.store.finance_grants()


@pytest.mark.asyncio
async def test_paused_collector_cannot_release_financial_risk(settlement):
    page, bots, _, adapter, domain = settlement
    adapter.config["_business_active"] = False
    domain.store.release_risk = lambda *args: pytest.fail(
        "Must reject before ledger write"
    )
    with pytest.raises(CashbackError, match="business_service_inactive"):
        await dispatch(
            page,
            bots,
            "fixture-admin",
            CashierCall(
                action="admin/risk/release",
                payload={"group": "5", "inviter": "1", "reason": "已完成复核"},
            ),
        )


@pytest.mark.asyncio
async def test_configuration_rechecks_scope_after_roster_io(settlement):
    page, bots, configs, _, domain = settlement

    async def revoked(adapter, group):
        configs["fixture"]["enabled_groups"] = ["5"]
        return {"1": member("1")}

    domain.roster = revoked
    domain.historical_members = lambda group: set()
    with pytest.raises(Exception, match="配置已变化"):
        await dispatch(
            page,
            bots,
            "fixture-admin",
            CashierCall(
                action="admin/groups",
                payload={
                    "id": "6",
                    "bot_id": "fixture",
                    "contact": "测试联系人",
                    "rate": 500,
                    "enabled": True,
                    "version": 0,
                },
            ),
        )
    assert domain.store.get_group("6") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tenant_paused", "service_paused", "version"])
async def test_integrated_finance_rechecks_tenant_at_commit(settlement, change):
    page, bots, _, _, domain = settlement
    tenant = {"active": True, "present": True, "paused": False, "version": 1}
    merged = SimpleNamespace(
        active=True,
        integrated=True,
        settings={"paused": False},
        tenants={("99", "6"): tenant},
    )
    page.plugin = SimpleNamespace(open_service=merged)

    async def changed(adapter, group):
        if change == "tenant_paused":
            tenant["paused"] = True
        elif change == "service_paused":
            merged.settings["paused"] = True
        else:
            tenant["version"] += 1
        return {"1": member("1")}

    domain.roster = changed
    domain.historical_members = lambda group: set()
    with pytest.raises(CashbackError, match="business_service_inactive"):
        await dispatch(
            page,
            bots,
            "fixture-admin",
            CashierCall(
                action="admin/groups",
                payload={
                    "id": "6",
                    "bot_id": "fixture",
                    "contact": "测试联系人",
                    "rate": 500,
                    "enabled": True,
                    "version": 0,
                },
            ),
        )
    assert domain.store.get_group("6") is None


def test_settlement_has_no_standalone_web_or_rpc_entrypoints():
    from astrbot.builtin_stars.wangshangliao_moderation import cashback_runtime

    root = Path(cashback_runtime.__file__).parent
    for name in ("cashback_web.py", "cashback_rpc.py", "cashback_standalone.py"):
        assert not (root / name).exists()
    runtime = cashback_runtime.CashbackRuntime(None)
    assert not hasattr(runtime, "rpc")
    assert "6186" not in Path(cashback_runtime.__file__).read_text()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_ACTIVITY_PAGE"), reason="Opt-in compiled UI"
)
async def test_compiled_plugin_settlement_and_risk_group_binding(
    page_app, settlement, headers, tmp_path
):
    from playwright.async_api import async_playwright, expect

    _, _, _, _, domain = settlement
    scan(
        domain.store,
        bot="fixture",
        notices=[
            {
                "member": "2",
                "inviter": "1",
                "source": "business_apply_log",
                "event_id": "plugin-join",
                "event_at": time() + 1,
            }
        ],
    )
    domain._scan = AsyncMock(return_value={uid: member(uid) for uid in ("1", "2")})
    calls = []
    output = Path(os.environ.get("WSL_ACTIVITY_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=page_app[0]),
        base_url="http://fixture",
        headers=headers,
    ) as client:

        async def api(_source, method, endpoint, body=None):
            calls.append((method, endpoint, copy.deepcopy(body)))
            if endpoint == "cashier" and body["action"] == "admin/risk":
                if body["payload"]["group"] == "6":
                    raise RuntimeError("Fixture group risk unavailable")
                return [
                    {
                        "id": "fixture-risk",
                        "inviter": "1",
                        "inviter_name": "风险邀请人",
                        "detected": time(),
                        "member_count": 10,
                        "matching_count": 10,
                        "name_length": 4,
                        "status": "blocked",
                        "blocked_at": time(),
                        "notice_status": "accepted",
                        "resolution": "",
                    }
                ]
            if endpoint == "open-service":
                if body["action"] == "command_capabilities":
                    from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
                        CAPABILITIES,
                    )

                    return {"commands": list(CAPABILITIES.values())}
                return {"mode": "integrated", "groups": [], "settings": {}}
            response = await client.request(
                method,
                PREFIX + "/" + endpoint,
                **({"params": body or {}} if method == "GET" else {"json": body}),
            )
            if response.status_code != 200:
                raise RuntimeError(response.text)
            result = response.json()["data"]
            if endpoint == "settings":
                result["open_mode"] = "integrated"
            return result

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                for width in (320, 390, 1440):
                    page = await browser.new_page(
                        viewport={"width": width, "height": 1000}
                    )
                    errors, requests = [], []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.on("request", lambda request: requests.append(request.url))
                    await page.expose_binding("fixtureAPI", api)
                    await page.add_init_script("""
                      Object.defineProperty(window, "AstrBotPluginPage", {value:{
                        ready:async()=>({}),getLocale:()=> "zh-CN",onContext:()=>()=>{},
                        apiGet:(endpoint,body)=>window.fixtureAPI("GET",endpoint,body),
                        apiPost:(endpoint,body)=>window.fixtureAPI("POST",endpoint,body),
                      }});
                    """)
                    await page.goto(
                        Path(os.environ["WSL_ACTIVITY_PAGE"]).resolve().as_uri()
                    )
                    await page.get_by_role("tab", name="活动与财务", exact=True).click()
                    await page.get_by_role(
                        "tab", name="邀请记账／结账", exact=True
                    ).click()
                    panel = page.locator(".cashier-shell")
                    await expect(
                        panel.get_by_text("管理面板总控", exact=True)
                    ).to_be_visible()
                    await expect(
                        panel.get_by_text("普通新成员", exact=True)
                    ).to_have_count(0)
                    await expect(panel.get_by_text("成员2", exact=True)).to_be_visible()
                    await panel.get_by_role(
                        "button", name="登记已结账", exact=True
                    ).first.click()
                    await expect(
                        page.get_by_text("确认已人工付款", exact=True)
                    ).to_be_visible()
                    await page.get_by_role(
                        "button", name="取消", exact=True
                    ).last.click()
                    await panel.get_by_role("button", name="群设置", exact=True).click()
                    await expect(
                        panel.get_by_label("结账联系方式", exact=True)
                    ).to_be_visible()
                    await expect(
                        panel.get_by_label("每位有效新成员金额（元）", exact=True)
                    ).to_have_value("18.00")
                    await page.screenshot(
                        path=str(output / f"settlement-settings-{width}.png"),
                        full_page=True,
                    )
                    await panel.get_by_role(
                        "button", name="复核并解除本群冻结", exact=True
                    ).click()
                    await expect(
                        page.get_by_text("复核并解除返现冻结", exact=True)
                    ).to_be_visible()
                    await page.get_by_role(
                        "button", name="取消", exact=True
                    ).last.click()
                    await panel.locator(".settings").get_by_role("combobox").click()
                    await page.get_by_role("option", name="测试群6", exact=True).click()
                    await expect(
                        panel.get_by_text("Fixture group risk unavailable", exact=False)
                    ).to_be_visible()
                    await expect(
                        panel.get_by_role(
                            "button", name="复核并解除本群冻结", exact=True
                        )
                    ).to_have_count(0)
                    await expect(
                        page.get_by_role("button", name="确认解除", exact=True)
                    ).to_have_count(0)
                    await panel.get_by_role("button", name="账本", exact=True).click()
                    await page.screenshot(
                        path=str(output / f"settlement-ledger-{width}.png"),
                        full_page=True,
                    )
                    await page.get_by_role("tab", name="使用帮助", exact=True).click()
                    await expect(
                        page.get_by_label("全部兼容命令", exact=True)
                    ).not_to_be_checked()
                    await page.screenshot(
                        path=str(output / f"quick-help-{width}.png"), full_page=True
                    )
                    await page.get_by_role(
                        "button", name="邀请记账／结账", exact=True
                    ).click()
                    await expect(page.locator(".cashier-shell")).to_be_visible()
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )
                    assert not errors, errors
                    assert not any(
                        "yqjz.xhn.top" in url or "/api/events" in url
                        for url in requests
                    )
                    await page.close()
            finally:
                await browser.close()
    assert any(
        endpoint == "cashier" and body["action"] == "entries"
        for _, endpoint, body in calls
    )
    assert row(domain.store)["state"] == "pending"
