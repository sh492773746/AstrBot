"""Exercise operator UI against isolated synthetic ledgers, never live accounts."""

import argparse
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from astrbot.dashboard.services.plugin_page_service import PluginPageService
from data.plugins.astrbot_plugin_lajiaoliao.commerce import PLANS, CommerceStore
from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    directory = root / "data/plugins/astrbot_plugin_lajiaoliao/pages/management"
    service = PluginPageService(None)
    rendered = service.rewrite_plugin_page_html(
        (directory / "index.html").read_text(),
        "astrbot_plugin_lajiaoliao",
        "management",
        "index.html",
        theme=None,
    )
    parent = """<!doctype html><meta charset="utf-8">
    <style>body{margin:0}iframe{border:0;width:100%;height:1100px}</style>
    <iframe src="/page" sandbox="allow-scripts allow-forms allow-downloads"></iframe>
    <script>addEventListener('message',async event=>{
      const frame=document.querySelector('iframe'),message=event.data;
      if(event.source!==frame.contentWindow||message.channel!=='astrbot-plugin-page')return;
      const send=value=>frame.contentWindow.postMessage({channel:'astrbot-plugin-page',...value},'*');
      if(message.kind==='ready'){send({kind:'context',context:{pluginName:'astrbot_plugin_lajiaoliao'}});return}
      if(message.kind!=='request')return;
      const response=await fetch('/fake/'+message.endpoint,{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(message.body||{})});
      send({kind:'response',requestId:message.requestId,...await response.json()});
    });</script>"""
    report = {
        "synthetic_only": True,
        "real_model_calls": 0,
        "real_sends": 0,
        "views": [],
    }
    with tempfile.TemporaryDirectory(prefix="lajiao-commerce-ui-") as temporary:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
                headless=True,
            )
            try:
                for name, viewport in [
                    ("desktop", {"width": 1440, "height": 1000}),
                    ("mobile", {"width": 390, "height": 844}),
                    ("mobile-small", {"width": 320, "height": 700}),
                ]:
                    store = CommerceStore(Path(temporary) / name)
                    store.bootstrap("operator", [])
                    configs = [
                        {
                            "id": "synthetic-" + str(index),
                            "session_ref": str(index) * 32,
                            "account_id": "account-" + str(index),
                            "enabled": False,
                            "connection_state": "stopped",
                            "revision": "r1",
                            "message_policy": {
                                "group_mode": "allowlist",
                                "group_ids": [],
                                "private_enabled": False,
                            },
                        }
                        for index in (1, 2)
                    ]
                    errors, rejected = [], []

                    def api(endpoint, body):
                        if endpoint == "management":
                            return {"instances": configs, "commerce_operator": True}
                        if endpoint == "groups":
                            return {
                                "groupInfos": [{"groupID": "g", "groupName": "测试群"}]
                            }
                        if endpoint == "commerce-customers" and not body:
                            snapshot = store.snapshot()
                            return {
                                "customers": list(snapshot["customers"].values()),
                                "bindings": list(snapshot["bindings"].values()),
                                "plans": PLANS,
                            }
                        if endpoint == "commerce-rates" and not body:
                            return {
                                "rates": store.rates(),
                                "providers": [
                                    {
                                        "id": "native-synthetic",
                                        "model": "synthetic",
                                        "fingerprint": "c" * 64,
                                    }
                                ],
                            }
                        if endpoint == "commerce-usage":
                            return store.usage(
                                body.get("customer_id", ""), body.get("page", 1)
                            )
                        if endpoint.startswith("commerce-"):
                            if (
                                endpoint == "commerce-customers"
                                and body.get("action") == "bind"
                            ):
                                body = {**body, "groups": []}
                            if endpoint == "commerce-rates":
                                body = {**body, "fingerprint": "c" * 64}
                            return store.execute(endpoint, body, "operator")
                        raise ValueError("unsupported")

                    def serve(route):
                        path = urlparse(route.request.url).path
                        if path.startswith("/fake/"):
                            endpoint = path.rsplit("/", 1)[-1]
                            try:
                                result = {
                                    "ok": True,
                                    "data": api(
                                        endpoint,
                                        route.request.post_data_json or {},
                                    ),
                                }
                            except Exception as exc:
                                rejected.append(str(exc))
                                result = {"ok": False, "error": str(exc)}
                            route.fulfill(json=result)
                        elif path == "/":
                            route.fulfill(body=parent, content_type="text/html")
                        elif path == "/page":
                            route.fulfill(body=rendered, content_type="text/html")
                        elif path == "/api/plugin/page/bridge-sdk.js":
                            route.fulfill(
                                body=service.bridge_file.read_text(),
                                content_type="text/javascript",
                            )
                        elif Path(path).name in {
                            "app.js",
                            "commerce.js",
                            "style.css",
                            "logo.ico",
                        }:
                            route.fulfill(path=directory / Path(path).name)
                        elif "/icons/" in path:
                            route.fulfill(path=directory / "icons" / Path(path).name)
                        else:
                            route.abort()

                    context = browser.new_context(viewport=viewport)
                    context.route("**/*", serve)
                    host = context.new_page()
                    host.on("pageerror", lambda error: errors.append(str(error)))
                    host.on("dialog", lambda dialog: dialog.accept())
                    host.goto("https://commerce.test/")
                    page = host.frame_locator("iframe")
                    tab = page.get_by_role("tab", name="套餐运营", exact=True)
                    expect(tab).to_be_visible()
                    tab.click()
                    expect(page.locator("#commerce-status")).to_contain_text(
                        "商业 AI 未启用"
                    )
                    page.locator("#commerce-label").fill("模拟客户")
                    page.locator("#commerce-create-confirm").check()
                    page.get_by_role("button", name="新增客户", exact=True).click()
                    expect(page.locator("#commerce-customer")).to_contain_text(
                        "模拟客户"
                    )
                    page.locator("#commerce-action").select_option("bind")
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-bindings")).to_contain_text(
                        "模拟客户"
                    )
                    page.locator("#commerce-action").select_option("activate")
                    page.locator("#commerce-plan").select_option("standard")
                    page.locator("#commerce-receipt").fill("mock-payment")
                    page.locator("#commerce-paid").check()
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-summary")).to_contain_text("普通版")
                    page.locator("#commerce-action").select_option("renew")
                    page.locator("#commerce-receipt").fill("mock-renewal")
                    page.locator("#commerce-paid").check()
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-periods p")).to_have_count(2)
                    page.locator("#commerce-action").select_option("topup")
                    page.locator("#commerce-receipt").fill("not-allowed")
                    page.locator("#commerce-paid").check()
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-status")).to_contain_text(
                        "仅有效 AI 版"
                    )
                    customer = next(iter(store.snapshot()["customers"].values()))
                    store.execute(
                        "commerce-customers",
                        {
                            "action": "pause",
                            "customer_id": customer["id"],
                            "version": customer["version"],
                            "confirm": True,
                            "request_id": secrets.token_hex(16),
                        },
                        "operator",
                    )
                    page.locator("#commerce-action").select_option("pause")
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-status")).to_contain_text(
                        "版本已更新"
                    )
                    page.locator("#commerce-refresh").click()
                    expect(page.locator("#commerce-customer-state")).to_contain_text(
                        "暂停"
                    )
                    page.locator("#commerce-action").select_option("resume")
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-customer-state")).to_contain_text(
                        "有效"
                    )
                    page.locator("#commerce").get_by_text(
                        "内部费率与核验证据", exact=True
                    ).click()
                    page.locator("#commerce-input-price").fill("1")
                    page.locator("#commerce-cache-price").fill("0.1")
                    page.locator("#commerce-output-price").fill("5")
                    page.locator("#commerce-rate-confirm").check()
                    page.locator("#commerce-rate button[type=submit]").click()
                    expect(page.locator("#commerce-status")).to_contain_text(
                        "商业 AI 未启用"
                    )
                    assert (
                        len(store.rates()) == 1
                        and not store.rates()[0]["value"]["verified"]
                    )
                    page.locator("#commerce-rate-evidence").fill(
                        "SYNTHETIC-INVOICE-ONLY"
                    )
                    for control in (
                        "commerce-count-proof",
                        "commerce-bound-proof",
                        "commerce-usage-proof",
                        "commerce-verified",
                        "commerce-rate-confirm",
                    ):
                        page.locator("#" + control).check()
                    page.locator("#commerce-rate button[type=submit]").click()
                    expect(page.locator("#commerce-status")).to_contain_text(
                        "已保存核验费率"
                    )
                    page.locator("#commerce-label").fill("模拟 AI 客户")
                    page.locator("#commerce-create-confirm").check()
                    page.get_by_role("button", name="新增客户", exact=True).click()
                    expect(page.locator("#commerce-customer")).to_contain_text(
                        "模拟 AI 客户"
                    )
                    expect(page.locator("#commerce-customer")).to_have_value(
                        next(
                            row["id"]
                            for row in store.snapshot()["customers"].values()
                            if row["label"] == "模拟 AI 客户"
                        )
                    )
                    page.locator("#commerce-action").select_option("bind")
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-bindings")).to_contain_text(
                        "模拟 AI 客户"
                    )
                    page.locator("#commerce-action").select_option("activate")
                    page.locator("#commerce-plan").select_option("ai")
                    page.locator("#commerce-receipt").fill("synthetic-ai-payment")
                    page.locator("#commerce-paid").check()
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-summary")).to_contain_text("20,000")
                    page.locator("#commerce-action").select_option("topup")
                    page.locator("#commerce-product").select_option("5000")
                    page.locator("#commerce-receipt").fill("synthetic-topup")
                    page.locator("#commerce-paid").check()
                    page.locator("#commerce-confirm").check()
                    page.locator("#commerce-order button[type=submit]").click()
                    expect(page.locator("#commerce-summary")).to_contain_text("25,000")
                    call = store.reserve(
                        "synthetic-2",
                        "synthetic-unknown-request",
                        1,
                        "chat",
                        "native-synthetic",
                        "synthetic",
                        "c" * 64,
                        200,
                        100,
                    )["call_id"]
                    store.dispatch(call)
                    store.settle(call, None)
                    page.locator("#commerce-refresh").click()
                    expect(page.locator("#commerce-calls")).to_contain_text("用量未知")
                    page.get_by_text("人工对账", exact=True).click()
                    page.locator("#commerce-actual").fill("0.0002")
                    page.locator("#commerce-evidence").fill("SYNTHETIC-RECONCILIATION")
                    page.locator("#commerce-reconcile-confirm").check()
                    page.locator("#commerce-reconcile button[type=submit]").click()
                    expect(page.locator("#commerce-calls")).to_contain_text(
                        "人工对账完成"
                    )
                    expect(page.locator("[data-main-panel]:visible")).to_have_count(1)
                    frame = host.frames[1]
                    assert frame.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )
                    assert frame.evaluate(
                        "Array.from(document.images).every(img => img.complete && img.naturalWidth > 0)"
                    )
                    assert not errors, errors
                    frame.evaluate("window.scrollTo(0, 0)")
                    host.evaluate("window.scrollTo(0, 0)")
                    host.screenshot(path=str(args.output / (name + ".png")))
                    report["views"].append(
                        {
                            "name": name,
                            "errors": errors,
                            "rejections": rejected,
                            "orders": 4,
                            "version_conflict": True,
                            "draft_and_synthetic_verified_rate": True,
                            "unknown_reconciled": True,
                            "topup_points": 5000,
                            "overflow": False,
                        }
                    )
                    context.close()
            finally:
                browser.close()
    atomic_json(args.output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
