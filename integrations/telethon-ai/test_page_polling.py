from pathlib import Path

from playwright.sync_api import sync_playwright


def test_automatic_refresh_and_dialog_preservation():
    source = (
        Path(__file__).resolve().parents[2]
        / "data/plugins/astrbot_plugin_telethon_ai/pages/accounts/index.html"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )
        page = browser.new_page()
        page.clock.install()
        page.add_init_script("""
          window.calls = 0;
          window.AstrBotPluginPage = {
            ready: async () => {},
            apiGet: async () => {
              window.calls++;
              return {
                accounts: [{account:"one", user_id:"1", state:"running", model:"model",
                  persona:"persona", allowed_chats:[], allowed_senders:[],
                  daily_used:window.calls, daily_limit:null, quota_scope:"tenant_authorization"}],
                daily_window: {reset_at:1790812800},
                tenants:[], tenant_count:0, customer_entries:0,
                control_attached:true, checks:{}, recent:[]
              };
            }
          };
        """)
        page.goto(source.as_uri())
        page.wait_for_function(
            "window.calls === 1 && document.querySelector('#accounts').textContent.includes('1 次')"
        )
        assert "北京时间" in page.locator(".daily-reset").inner_text()
        assert "统计日切换" in page.locator(".daily-reset").inner_text()
        page.clock.fast_forward(30000)
        page.wait_for_function(
            "window.calls === 2 && document.querySelector('#accounts').textContent.includes('2 次')"
        )
        page.locator("#add-account").click()
        page.locator("#login-alias").fill("untouched")
        page.clock.fast_forward(30000)
        assert page.evaluate("window.calls") == 2
        assert page.locator("#login-alias").input_value() == "untouched"
        page.locator("#login-cancel").click()
        page.clock.fast_forward(30000)
        page.wait_for_function("window.calls === 3")
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("window.calls === 4")
        browser.close()
