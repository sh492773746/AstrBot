"""Exercise per-instance management through the official sandbox bridge."""

import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from astrbot.dashboard.services.plugin_page_service import PluginPageService


def main():
    root = Path(__file__).resolve().parents[2]
    page_dir = root / "data/plugins/astrbot_plugin_lajiaoliao/pages/management"
    service = PluginPageService(None)
    rendered = service.rewrite_plugin_page_html(
        page_dir.joinpath("index.html").read_text(),
        "astrbot_plugin_lajiaoliao",
        "management",
        "index.html",
        theme=None,
    )
    artifacts = Path(
        os.environ.get(
            "SMOKE_ARTIFACT_DIR",
            "/opt/lajiaoliao-migration-backups/20261009-business-v9v8XH/ui",
        )
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            headless=True,
        )
        try:
            for name, size in [
                ("desktop", {"width": 1280, "height": 900}),
                ("mobile", {"width": 390, "height": 844}),
                ("mobile-small", {"width": 320, "height": 700}),
                ("mobile-dark", {"width": 390, "height": 844}),
            ]:
                context = browser.new_context(
                    viewport=size,
                    color_scheme="dark" if name.endswith("dark") else "light",
                )
                parent = """<!doctype html><html><meta charset="utf-8"><body style="margin:0">
                <iframe src="/page" sandbox="allow-scripts allow-forms allow-downloads" style="border:0;width:100%;height:950px"></iframe>
                <script>
                const rows = ['one','two'].map((id,i) => ({id,session_ref:(i?'b':'a').repeat(32),
                  account_id:'account-'+id,enabled:false,connection_state:'stopped',revision:'r1',
                  message_policy:{group_mode:'allowlist',group_ids:[],private_enabled:false}}));
                window.saves = []; window.actions = []; window.businessSaves = []; window.reads = []; window.failDirectory = false;
                window.businesses = {};
                const defaults = {rankings_enabled:false,lottery_enabled:false,reminders_enabled:false,review_enabled:false,
                  native_actions:[],manager_user_ids:[],invitations_enabled:false,invitation_points:1,
                  keywords:[],review_provider_id:'',lottery:{prize:'',winners:3,minutes:10,capacity:15,min_invitations:0,contact:''}};
                addEventListener('message', event => {
                  const frame = document.querySelector('iframe');
                  if (event.source !== frame.contentWindow || event.data.channel !== 'astrbot-plugin-page') return;
                  const message = event.data;
                  const send = value => frame.contentWindow.postMessage({channel:'astrbot-plugin-page',...value},'*');
                  if(message.kind==='ready'){send({kind:'context',context:{pluginName:'astrbot_plugin_lajiaoliao'}});return}
                  if(message.kind!=='request') return;
                  let data;
                  if(message.endpoint==='management') data={instances:rows};
                  else if(message.endpoint==='capabilities') data={features:[],business:[]};
                  else if(message.endpoint==='groups') {
                    if(window.failDirectory){send({kind:'response',requestId:message.requestId,ok:false,error:'instance_offline'});return}
                    const row=rows.find(row=>row.session_ref===message.body.session_ref);
                    data={groupInfos:[{groupID:'group-'+row.id,groupName:'群 '+row.id},
                      {groupID:'extra-'+row.id,groupName:'另一个群 '+row.id}]};
                  } else if(message.endpoint==='policy') {
                    saves.push(message.body);
                    const row=rows.find(row=>row.id===message.body.instance_id);
                    row.message_policy=message.body.message_policy; row.revision='r2'; data={saved:true};
                  } else if(message.endpoint==='business') {
                    const row=rows.find(row=>row.session_ref===message.body.session_ref);
                    reads.push({endpoint:message.endpoint,...message.body});
                    const key=message.body.group_id==='group-'+row.id ? row.id : row.id+':'+message.body.group_id;
                    const state=businesses[key] ||= {version:0,settings:structuredClone(defaults)};
                    if(['save','save-managers'].includes(message.body.action)){
                      if(message.body.version !== state.version || message.body.instance_revision !== row.revision) {
                        send({kind:'response',requestId:message.requestId,ok:false,error:'business_changed_refresh_required'});return;
                      }
                      businessSaves.push(message.body);
                      if(message.body.action==='save-managers') state.settings.manager_user_ids=message.body.manager_user_ids;
                      else state.settings=structuredClone(message.body.settings);
                      state.version++;
                    }
                    data={...state,group_id:message.body.group_id,instance_revision:row.revision,
                      rankings:[{member_id:'member',name:'模拟成员',count:2}],activities:[],reminders:[],
                      audit:[{id:1,stamp:1,kind:'settings',status:'saved',subject:'dashboard'}],
                      invitations:[],native_actions:{mute:'成员禁言',kick:'人工踢人',announce:'群公告'},
                      unsupported:{revoke_others:'撤回他人消息'}};
                  } else if(message.endpoint==='group-users') {
                    const row=rows.find(row=>row.session_ref===message.body.session_ref);
                    data={group_id:message.body.group_id,instance_id:row.id,has_more:false,members:[
                      {user_id:'ordinary',nickname:'普通成员',identity:'member',native_role:20},
                      {user_id:'admin',nickname:'系统管理员',identity:'astrbot_admin',native_role:20},
                      {user_id:'bot',nickname:'机器人',identity:'bot',native_role:100}]};
                  } else if(message.endpoint==='group-state') {
                    reads.push({endpoint:message.endpoint,...message.body});
                    data={group_id:message.body.group_id,nickname:'旧名片',role_level:rows[0].role??20,announcement:'编辑测试',messages:[{message_id:'own-message',text:'测试消息'}],
                      notifications:[{action:'group_member_updated',user_id:'account-one',nickname:'旧名片'}]};
                  } else if(message.endpoint==='platform-action') {
                    actions.push(message.body);
                    data={status:message.body.query?'succeeded':'unknown',operation_id:message.body.operation_id};
                  } else {send({kind:'response',requestId:message.requestId,ok:false,error:'unsupported'});return}
                  send({kind:'response',requestId:message.requestId,ok:true,data});
                });
                </script></body></html>"""

                def serve(route):
                    path = urlparse(route.request.url).path
                    if path == "/":
                        route.fulfill(body=parent, content_type="text/html")
                    elif path == "/page":
                        route.fulfill(body=rendered, content_type="text/html")
                    elif path == "/api/plugin/page/bridge-sdk.js":
                        route.fulfill(
                            body=service.bridge_file.read_text(),
                            content_type="text/javascript",
                        )
                    elif path.endswith("/app.js"):
                        route.fulfill(
                            body=page_dir.joinpath("app.js").read_text(),
                            content_type="text/javascript",
                        )
                    elif path.endswith("/style.css"):
                        route.fulfill(
                            body=page_dir.joinpath("style.css").read_text(),
                            content_type="text/css",
                        )
                    elif path.endswith("/logo.ico"):
                        route.fulfill(
                            body=page_dir.joinpath("logo.ico").read_bytes(),
                            content_type="image/x-icon",
                        )
                    elif "/icons/" in path and path.endswith(".svg"):
                        route.fulfill(
                            body=page_dir.joinpath("icons", Path(path).name).read_bytes(),
                            content_type="image/svg+xml",
                        )
                    else:
                        route.abort()

                context.route("**/*", serve)
                host = context.new_page()
                errors = []
                host.on("pageerror", lambda error: errors.append(str(error)))
                host.goto("http://plugin.test/")
                page = host.frame_locator("iframe")
                page.get_by_label("当前机器人", exact=True).select_option("one")
                expect(page.locator("#main-tabs [role=tab]")).to_have_count(7)
                expect(page.locator("[data-main-panel]:visible")).to_have_count(1)
                expect(page.locator("#group-actions")).not_to_be_visible()
                expect(page.locator("#business-form")).not_to_be_visible()
                expect(page.locator("#group-list")).to_have_count(0)
                expect(page.locator("#action-group option")).to_have_count(2)
                main_box = page.locator("#main-tabs").bounding_box()
                assert main_box and main_box["y"] + main_box["height"] < size["height"]
                page.get_by_role("button", name="刷新群列表", exact=True).click()
                page.get_by_role(
                    "checkbox", name="允许群 group-one", exact=True
                ).check()
                page.get_by_label("当前群", exact=True).select_option("extra-one")
                expect(page.get_by_role("checkbox", name="允许群 extra-one", exact=True)).not_to_be_checked()
                page.get_by_role("checkbox", name="允许群 extra-one", exact=True).check()
                page.get_by_label("当前群", exact=True).select_option("group-one")
                expect(page.get_by_role("checkbox", name="允许群 group-one", exact=True)).to_be_checked()
                page.get_by_label("当前群", exact=True).select_option("extra-one")
                page.get_by_role("checkbox", name="允许群 extra-one", exact=True).uncheck()
                page.get_by_role("tab", name="自动化设置", exact=True).click()
                expect(page.locator("#business-status")).to_contain_text("当前群未开启")
                expect(page.locator("#business-form")).not_to_be_visible()
                assert host.evaluate("reads.length") == 0
                page.get_by_role("tab", name="消息权限", exact=True).click()
                page.get_by_role("checkbox", name="允许私聊", exact=True).check()
                page.get_by_role("button", name="保存权限", exact=True).click()
                page.get_by_text("权限已保存", exact=True).wait_for()
                assert host.evaluate(
                    "saves.length === 1 && saves[0].instance_id === 'one' && JSON.stringify(saves[0].message_policy.group_ids)==='[\"group-one\"]'"
                )
                expect(page.get_by_label("当前群", exact=True)).to_have_value("extra-one")
                expect(page.get_by_role("checkbox", name="允许群 extra-one", exact=True)).not_to_be_checked()
                page.get_by_label("当前机器人", exact=True).select_option("two")
                expect(
                    page.get_by_role("checkbox", name="允许私聊", exact=True)
                ).not_to_be_checked()
                page.get_by_role("button", name="刷新群列表", exact=True).click()
                expect(
                    page.get_by_role("checkbox", name="允许群 group-two", exact=True)
                ).not_to_be_checked()
                assert (
                    page.get_by_role(
                        "checkbox", name="允许群 group-one", exact=True
                    ).count()
                    == 0
                )
                page.get_by_role("checkbox", name="允许私聊", exact=True).check()
                page.get_by_label("当前机器人", exact=True).select_option("one")
                page.get_by_role("button", name="返回编辑", exact=True).click()
                expect(page.get_by_label("当前机器人", exact=True)).to_have_value("two")
                page.get_by_label("当前机器人", exact=True).select_option("one")
                page.get_by_role("button", name="放弃修改", exact=True).click()
                expect(
                    page.get_by_role("checkbox", name="允许群 group-one", exact=True)
                ).to_be_checked()
                expect(
                    page.get_by_role("checkbox", name="允许私聊", exact=True)
                ).to_be_checked()
                page.get_by_role("tab", name="自动化状态", exact=True).click()
                page.get_by_role("button", name="刷新群状态", exact=True).click()
                expect(page.locator("#native-form")).to_have_count(0)
                expect(page.locator("#nickname-form")).to_have_count(0)
                expect(page.locator("#automation-status-summary")).to_contain_text("已关闭")
                assert host.evaluate("actions.length") == 0
                page.get_by_role("tab", name="记录", exact=True).click()
                page.get_by_role("tab", name="机器人消息", exact=True).click()
                expect(page.locator("#own-messages button")).to_have_count(0)
                assert host.evaluate("actions.length") == 0
                page.get_by_role("tab", name="自动化设置", exact=True).click()
                page.get_by_role("button", name="刷新业务状态", exact=True).click()
                expect(page.get_by_label("群抽奖", exact=True)).not_to_be_checked()
                expect(
                    page.get_by_label("每日定时通知", exact=True)
                ).not_to_be_checked()
                page.get_by_role("checkbox", name="发言排名", exact=True).check()
                page.get_by_label("邀请统计（新成员）", exact=True).check()
                assert host.evaluate("actions.length") == 0
                page.get_by_label("当前群", exact=True).select_option("extra-one")
                expect(page.get_by_label("当前群", exact=True)).to_have_value("group-one")
                expect(page.locator("#business-status")).to_contain_text("请先保存")
                page.get_by_role("button", name="保存业务设置", exact=True).click()
                page.get_by_text(
                    "业务设置已保存；已启用的计划将在预设时间自动运行", exact=True
                ).wait_for()
                assert host.evaluate(
                    "businessSaves.length===1 && businessSaves[0].group_id==='group-one' && businessSaves[0].settings.rankings_enabled"
                )
                read_count = host.evaluate("reads.length")
                page.get_by_label("当前群", exact=True).select_option("extra-one")
                expect(page.locator("#business-form")).not_to_be_visible()
                expect(page.locator("#business-status")).to_contain_text("当前群未开启")
                assert host.evaluate("reads.length") == read_count
                page.get_by_role("tab", name="消息权限", exact=True).click()
                page.get_by_role("checkbox", name="允许群 extra-one", exact=True).check()
                page.get_by_role("tab", name="自动化状态", exact=True).click()
                expect(page.locator("#native-form")).not_to_be_visible()
                assert host.evaluate("reads.length") == read_count
                page.get_by_role("tab", name="消息权限", exact=True).click()
                page.get_by_role("button", name="保存权限", exact=True).click()
                page.get_by_text("权限已保存", exact=True).wait_for()
                expect(page.get_by_label("当前群", exact=True)).to_have_value("extra-one")
                page.get_by_role("tab", name="自动化设置", exact=True).click()
                expect(page.get_by_role("checkbox", name="发言排名", exact=True)).not_to_be_checked()
                assert host.evaluate("businesses.one.settings.rankings_enabled && !businesses['one:extra-one'].settings.rankings_enabled")
                page.get_by_role("tab", name="消息权限", exact=True).click()
                page.get_by_role("checkbox", name="允许群 extra-one", exact=True).uncheck()
                page.get_by_role("button", name="保存权限", exact=True).click()
                page.get_by_text("权限已保存", exact=True).wait_for()
                expect(page.get_by_label("当前群", exact=True)).to_have_value("extra-one")
                read_count = host.evaluate("reads.length")
                page.get_by_role("tab", name="自动化状态", exact=True).click()
                expect(page.locator("#native-form")).not_to_be_visible()
                expect(page.locator("#business-status")).to_contain_text("当前群未开启")
                assert host.evaluate("reads.length") == read_count
                assert host.evaluate("JSON.stringify(rows[0].message_policy.group_ids)==='[\"group-one\"]'")
                page.get_by_role("tab", name="自动化设置", exact=True).click()
                page.get_by_label("当前群", exact=True).select_option("group-one")
                expect(page.get_by_role("checkbox", name="发言排名", exact=True)).to_be_checked()
                page.get_by_role("tab", name="自动化状态", exact=True).click()
                expect(page.locator("#native-form")).to_have_count(0)
                assert host.evaluate("actions.length") == 0
                host.evaluate("rows[0].role=60")
                page.get_by_role("button", name="刷新群状态", exact=True).click()
                page.get_by_role("button", name="配置自动化规则", exact=True).click()
                expect(page.locator("#business-form")).to_be_visible()
                assert host.evaluate("actions.length") == 0
                page.get_by_role("tab", name="活动任务", exact=True).click()
                page.get_by_role("tab", name="发言排名", exact=True).click()
                expect(page.locator("#rankings-list")).to_contain_text("模拟成员")
                settings_tab = page.get_by_role("tab", name="自动化设置", exact=True)
                rankings_tab = page.get_by_role("tab", name="发言排名", exact=True)
                expect(settings_tab).to_have_attribute("tabindex", "-1")
                expect(rankings_tab).to_have_attribute("tabindex", "0")
                rankings_tab.press("ArrowRight")
                expect(page.get_by_role("tab", name="抽奖记录", exact=True)).to_have_attribute("aria-selected", "true")
                page.get_by_role("tab", name="抽奖记录", exact=True).press("Home")
                expect(rankings_tab).to_have_attribute("aria-selected", "true")
                expect(rankings_tab).to_have_attribute("tabindex", "0")
                for tab in [
                    "抽奖记录",
                    "定时通知",
                    "邀请积分",
                ]:
                    page.get_by_role("tab", name=tab, exact=True).click()
                    assert page.locator("html").evaluate(
                        "el=>el.scrollWidth<=innerWidth"
                    )
                page.get_by_role("tab", name="记录", exact=True).click()
                page.get_by_role("tab", name="审核记录", exact=True).click()
                expect(page.locator("#audit-list")).to_contain_text("saved")
                page.get_by_role("tab", name="能力", exact=True).click()
                expect(page.locator("#capabilities-list")).to_contain_text("成员禁言")
                page.get_by_role("tab", name="自动化设置", exact=True).click()
                page.get_by_label("内容审核", exact=True).check()
                host.evaluate("businesses.one.version++")
                page.get_by_role("button", name="保存业务设置", exact=True).click()
                page.get_by_text(
                    "群业务设置已更新，请刷新后重新编辑。", exact=True
                ).wait_for()
                expect(
                    page.get_by_label("内容审核", exact=True)
                ).to_be_checked()
                assert host.evaluate("businessSaves.length") == 1
                page.get_by_label("当前机器人", exact=True).select_option("two")
                page.get_by_role("button", name="返回编辑", exact=True).click()
                expect(page.get_by_label("当前机器人", exact=True)).to_have_value("one")
                page.get_by_label("当前机器人", exact=True).select_option("two")
                page.get_by_role("button", name="放弃修改", exact=True).click()
                page.get_by_role("tab", name="消息权限", exact=True).click()
                page.get_by_role("button", name="刷新群列表", exact=True).click()
                page.get_by_role(
                    "checkbox", name="允许群 group-two", exact=True
                ).check()
                page.get_by_role("button", name="保存权限", exact=True).click()
                page.get_by_text("权限已保存", exact=True).wait_for()
                page.get_by_role("tab", name="自动化设置", exact=True).click()
                page.get_by_role("button", name="刷新业务状态", exact=True).click()
                expect(page.get_by_role("checkbox", name="发言排名", exact=True)).not_to_be_checked()
                assert host.evaluate(
                    "!businesses.two.settings.rankings_enabled && !businesses.two.settings.invitations_enabled && businesses.two.settings.native_actions.length===0 && actions.length===0"
                )
                page.get_by_role("tab", name="管理授权", exact=True).click()
                expect(page.get_by_role("checkbox", name="授权群管理员 admin", exact=True)).to_be_disabled()
                expect(page.get_by_role("checkbox", name="授权群管理员 bot", exact=True)).to_be_disabled()
                grant = page.get_by_role("checkbox", name="授权群管理员 ordinary", exact=True)
                grant.check()
                expect(page.locator("#users-list")).to_contain_text("待授权")
                page.get_by_role("button", name="保存授权", exact=True).click()
                page.get_by_role("button", name="取消", exact=True).click()
                assert host.evaluate("businessSaves.length") == 1
                page.get_by_label("当前群", exact=True).select_option("extra-two")
                expect(page.get_by_label("当前群", exact=True)).to_have_value("group-two")
                page.get_by_label("当前机器人", exact=True).select_option("one")
                page.get_by_role("button", name="返回编辑", exact=True).click()
                expect(page.get_by_label("当前机器人", exact=True)).to_have_value("two")
                page.get_by_role("button", name="保存授权", exact=True).click()
                page.get_by_role("button", name="确认保存授权", exact=True).click()
                page.get_by_text("授权已保存", exact=True).wait_for()
                expect(page.locator("#users-list")).to_contain_text("本群机器人管理员")
                assert host.evaluate("businesses.two.settings.manager_user_ids.join(',')==='ordinary' && businesses.one.settings.manager_user_ids.length===0 && actions.length===0")
                host.screenshot(path=str(artifacts / f"management-managers-{name}.png"))
                grant.uncheck()
                page.get_by_role("button", name="保存授权", exact=True).click()
                page.get_by_role("button", name="确认保存授权", exact=True).click()
                page.get_by_text("授权已保存", exact=True).wait_for()
                assert host.evaluate("businesses.two.settings.manager_user_ids.length===0 && actions.length===0")
                assert page.locator("html").evaluate(
                    "el => el.scrollWidth <= innerWidth"
                )
                assert page.locator("img").evaluate_all(
                    "images => images.every(img => img.complete && img.naturalWidth > 0)"
                )
                # Capture a fresh iframe after modal tests; headless Chromium can
                # retain an empty out-of-process surface after closing a modal.
                host.locator("iframe").evaluate("el => el.src = '/page?capture'")
                page.get_by_label("当前机器人", exact=True).wait_for()
                frame = host.frames[1]
                frame.evaluate("window.scrollTo(0, 0)")
                host.evaluate("window.scrollTo(0, 0)")
                host.wait_for_timeout(150)
                host.screenshot(
                    path=str(artifacts / f"management-{name}.png"), full_page=False
                )
                page.get_by_role("tab", name="自动化设置", exact=True).click()
                expect(page.locator("#business-form")).to_be_visible()
                host.wait_for_timeout(150)
                host.screenshot(
                    path=str(artifacts / f"management-business-{name}.png"),
                    full_page=False,
                )
                box = frame.locator("#main-tabs").bounding_box()
                assert box and 0 <= box["y"] < size["height"]
                # Automatic rules save only after explicit confirmation; cancellation
                # leaves the API untouched. This parent is entirely synthetic.
                page.get_by_label("内容审核", exact=True).check()
                page.locator("#automation-rules summary").click()
                page.locator("#automation-enabled").check()
                page.locator("#ai-management-enabled").check()
                count = host.evaluate("businessSaves.length")
                page.get_by_role("button", name="保存业务设置", exact=True).click()
                expect(page.locator("#automation-dialog")).to_be_visible()
                page.locator("#cancel-automation").click()
                assert host.evaluate("businessSaves.length") == count
                page.get_by_role("button", name="保存业务设置", exact=True).click()
                page.locator("#confirm-automation").click()
                expect(page.locator("#business-status")).to_contain_text("业务设置已保存")
                assert host.evaluate("businesses.one.settings.automation.enabled && businesses.one.settings.review_enabled && businesses.one.settings.native_actions.includes('mute') && businesses.one.settings.ai_management_enabled && actions.length===0")
                assert host.evaluate("businessSaves.length") == count + 1
                assert page.locator("html").evaluate("el => el.scrollWidth <= innerWidth")
                host.screenshot(path=str(artifacts / f"management-automation-{name}.png"))
                page.locator("#automation-enabled").uncheck()
                page.locator("#ai-management-enabled").uncheck()
                page.get_by_label("内容审核", exact=True).uncheck()
                page.get_by_role("button", name="保存业务设置", exact=True).click()
                expect(page.locator("#business-status")).to_contain_text("业务设置已保存")
                # Main navigation remains exclusive, keyboard accessible, and drafts
                # survive changing views without saving or mutating the API.
                page.get_by_label("内容审核", exact=True).check()
                page.get_by_role("tab", name="能力", exact=True).click()
                page.get_by_role("tab", name="自动化设置", exact=True).click()
                expect(page.get_by_label("内容审核", exact=True)).to_be_checked()
                expect(page.locator("#tab-business-settings")).to_have_attribute("data-dirty", "true")
                page.get_by_role("tab", name="消息权限", exact=True).click()
                expect(page.get_by_role("tab", name="消息权限", exact=True)).to_be_enabled()
                page.get_by_role("tab", name="消息权限", exact=True).press("ArrowRight")
                expect(page.get_by_role("tab", name="自动化状态", exact=True)).to_have_attribute("aria-selected", "true")
                expect(page.locator("[data-main-panel]:visible")).to_have_count(1)
                expect(page.get_by_role("tab", name="自动化状态", exact=True)).to_be_enabled()
                page.get_by_role("tab", name="自动化状态", exact=True).press("End")
                expect(page.get_by_role("tab", name="能力", exact=True)).to_have_attribute("aria-selected", "true")
                expect(page.get_by_role("tab", name="能力", exact=True)).to_be_enabled()
                page.get_by_role("tab", name="能力", exact=True).press("Home")
                expect(page.get_by_role("tab", name="消息权限", exact=True)).to_have_attribute("aria-selected", "true")
                # A failed directory refresh keeps existing names/permissions
                # and does not hide navigation or submit a configuration.
                save_count = host.evaluate("saves.length")
                host.evaluate("window.failDirectory=true")
                page.get_by_role("button", name="刷新群列表", exact=True).click()
                expect(page.locator("#status")).to_contain_text("机器人未在线")
                expect(page.locator("#action-group")).to_contain_text("群 one")
                assert host.evaluate("saves.length") == save_count
                host.evaluate("window.failDirectory=false")
                page.get_by_role("button", name="刷新群列表", exact=True).click()
                expect(page.get_by_role("button", name="刷新群列表", exact=True)).to_be_enabled()
                expect(page.locator("#status")).to_be_empty()
                host.evaluate("rows.splice(0)")
                page.get_by_role("button", name="刷新状态", exact=True).click()
                page.get_by_role("button", name="放弃修改", exact=True).click()
                expect(page.locator("#workspace")).not_to_be_visible()
                expect(page.locator("#identity")).to_contain_text("暂无已创建")
                assert not errors, errors
                context.close()
                print(
                    name
                    + ": isolated management/save/discard/sandbox/layout/logo passed"
                )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
