"""Inspect only integrated public controls through a synthetic local bridge."""

import asyncio
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageStat
from playwright.async_api import async_playwright, expect

from scripts.check_wsl_policy_page import FIXTURE, ROOT

PUBLIC = r"""
const publicState = {
  mode:"integrated",
  settings:{version:1,paused:0,personal_limit:20,global_limit:100000},
  allocated_count:1,operating_count:1,group_limit:20,queue_health:[],
  ai_budget:{version:1,confirmed:false,provider:"existing/model",checked:0},
  accounts:[{account:"99",nickname:"测试机器人",state:"online",instance_id:"open",enabled_groups:1}],
  groups:[
    {account:"99",group_id:"5",name:"开放群甲",present:1,active:1,paused:0,version:2,managers:["1"],admission:"verified",native_role:"GROUP_ROLE_ADMIN",native_capability:{state:"available"},rules:{advertising:true,flood:true,keywords:[]}},
    {account:"99",group_id:"6",name:"待开通群",present:1,active:0,paused:0,version:1,managers:[],admission:"verified",rules:{advertising:true,flood:true,keywords:[]}}
  ],
  model:{provider_id:"existing/model"},model_configured:true,
  providers:[{id:"existing/model",model:"configured"}],audit:[],admins:[],
  manager_quotas:[],group_ai:[],ai_profiles:[],
  billing:{
    settings:{version:1,contact_name:"",contact:"",monthly_micro:0},
    pools:[],subscriptions:[],payers:[],notices:[],unknown:[],
    prices:[{provider:"existing/model",version:1,current:true,binding:"fixture",blocked:"",
      config:{input_per_million:"",output_per_million:"",multiplier:"0.25",credit_cny:"1",
        input_ceiling:0,output_ceiling:4096,all_output_billed_in_limit:false,evidence:"",
        valid_until:1893427200}}]
  },
  ai_choices:{configs:[{id:"default",name:"客服配置",compatible:true}],
    personas:[{id:"support",name:"客服人格"}]}
};
const originalGet = window.AstrBotPluginPage.apiGet;
window.AstrBotPluginPage.apiGet = async (endpoint, ...args) => {
  const result = await originalGet(endpoint,...args);
  return endpoint === "settings" ? {...result,open_mode:"integrated"} : result;
};
const originalPost = window.AstrBotPluginPage.apiPost;
window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
  if (endpoint !== "open-service") return originalPost(endpoint, body);
  window.policyRequests.push({endpoint,body:structuredClone(body)});
  if (body.action === "status") return structuredClone(publicState);
  if (body.action === "tenant_members") return {
    members:[{id:"1",name:"管理员甲"},{id:"2",name:"成员乙"}],
    native_capability:{state:"available"}
  };
  if (body.action === "activation_code") return {code:"fixture-only",expires_in:600};
  if (body.action === "tenant_preview") return {confirmation:"preview-test",expires_in:600};
  if (body.action === "tenant") {
    Object.assign(publicState.groups[0], body.payload, {version:publicState.groups[0].version+1});
    return {saved:true};
  }
  if (body.action === "billing_settings") {
    const p = body.payload;
    publicState.billing.settings = {...p,version:p.version+1,monthly_micro:Number(p.monthly_cny)*1000000};
    return {saved:true};
  }
  if (body.action === "plan_preview") {
    const now = Date.now()/1000;
    return {...body.payload,starts:now,ends:now+30*86400,budget_micro:Number(body.payload.received_cny)*700000,upgrade:true,confirmation:"plan-preview"};
  }
  if (body.action === "plan_grant") {
    const p = body.payload;
    if (p.confirmation !== "plan-preview") throw new Error("bad_confirmation");
    publicState.billing.pools = [{
      uid:p.uid,pool:"uid/"+p.uid,name:p.tier === "plus" ? "Plus" : "Pro",tier:p.tier,
      version:p.version+1,daily:p.tier === "plus" ? 2000 : 5000,used:0,remaining:2000,
      expires:Date.now()/1000+30*86400,cost_micro:0,budget_micro:Number(p.received_cny)*700000
    }];
    return {saved:true};
  }
  if (body.action === "payer_nominate") {
    const p = body.payload;
    publicState.billing.payers = [{...p,version:p.version+1,confirmed:0}];
    return {saved:true};
  }
  if (body.action === "billing_price") {
    publicState.billing.prices = [{...body.payload,current:true,version:body.payload.version+1,binding:"fixture",blocked:""}];
    return {saved:true};
  }
  if (body.action === "group_ai") {
    const p = body.payload;
    const row = {...p,version:p.version+1,owner:"",last_attempt:0,blocked:""};
    publicState.group_ai = publicState.group_ai.filter(g => g.account !== p.account || g.group_id !== p.group_id);
    publicState.group_ai.push(row);
    return {saved:true};
  }
  if (body.action === "ai_profile") {
    const p = body.payload;
    const old = publicState.ai_profiles.find(r => r.kind === p.kind && r.account === p.account && r.group_id === p.group_id);
    if ((old?.version || 0) !== p.version) throw new Error("ai_profile_changed");
    publicState.ai_profiles = publicState.ai_profiles.filter(r => r !== old);
    publicState.ai_profiles.push({...p,version:p.version+1});
    return {saved:true};
  }
  throw new Error("Unexpected integrated action");
};
"""


async def main():
    """Test responsive saves and confirmations without any production requests."""
    output = Path(
        os.environ.get("WSL_OPEN_PAGE_OUTPUT")
        or tempfile.mkdtemp(prefix="wsl-integrated-page-")
    )
    output.mkdir(parents=True, exist_ok=True)
    asset = Path(
        os.environ.get("WSL_OPEN_PAGE_STATIC")
        or ROOT
        / "astrbot/builtin_stars/wangshangliao_moderation/pages/management/index.html"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            args=["--no-sandbox"],
        )
        try:
            for width in (320, 390, 768, 1440):
                page = await browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.add_init_script(FIXTURE + PUBLIC)
                await page.goto(asset.resolve().as_uri())
                await page.get_by_text("开放服务运行中", exact=True).wait_for()
                section = page.locator(".open-service")
                await section.get_by_label("群聊", exact=True).select_option("99/5")
                plans = section.get_by_role("region", name="AI套餐与费用保护")
                await plans.get_by_label("统一升级客服名称", exact=True).fill(
                    "服务客服"
                )
                await plans.get_by_label("客服账号或联系方式", exact=True).fill(
                    "测试客服"
                )
                await plans.get_by_label(
                    "全站每自然月模型成本上限（元）", exact=True
                ).fill("1000")
                await plans.get_by_role(
                    "button", name="保存客服与成本上限", exact=True
                ).click()
                await page.get_by_text(
                    "升级客服和全站费用上限已保存", exact=True
                ).wait_for()
                manager = plans.get_by_label("套餐管理员", exact=True)
                await expect(manager.locator("option")).to_have_count(1)
                await plans.get_by_role(
                    "button", name="预览授予套餐", exact=True
                ).click()
                await plans.get_by_role("alertdialog", name="确认授予AI套餐").wait_for()
                assert not any(
                    r["body"].get("action") == "plan_grant"
                    for r in await page.evaluate("window.policyRequests")
                )
                await plans.get_by_role(
                    "button", name="确认已收款并授予", exact=True
                ).click()
                await plans.get_by_text(
                    "套餐已授予，未开启模型或群AI", exact=True
                ).wait_for()
                await expect(plans.locator(".summary")).to_contain_text("当前Plus")
                page.once("dialog", lambda dialog: dialog.accept())
                await plans.get_by_role(
                    "button", name="指定本群额度承担人", exact=True
                ).click()
                await plans.get_by_text(
                    "已指定候选承担人，等待本人确认", exact=True
                ).wait_for()
                await expect(
                    plans.get_by_text("尚未确认，不扣共享额度", exact=False)
                ).to_be_visible()
                await plans.locator("summary").filter(has_text="模型计费依据").click()
                await plans.get_by_label("计费模型", exact=True).select_option(
                    "existing/model"
                )
                await expect(
                    plans.get_by_text("当前计费依据有效至", exact=False)
                ).to_be_visible()
                await plans.get_by_label(
                    "每百万输入Token原价（供应商扣费单位）", exact=True
                ).fill("0.15")
                await plans.get_by_label(
                    "每百万输出Token原价（供应商扣费单位）", exact=True
                ).fill("0.6")
                await plans.get_by_label(
                    "供应商单次全部输入Token硬上界", exact=True
                ).fill("100000")
                await plans.get_by_label("计费及上界核验依据", exact=True).fill(
                    "synthetic browser fixture only"
                )
                await plans.get_by_role(
                    "checkbox", name="已确认输出限额包含所有计费推理Token且无额外费用"
                ).check()
                await plans.get_by_role(
                    "button", name="保存已核验计费依据", exact=True
                ).click()
                await plans.get_by_text(
                    "模型计费上界已登记，供应商硬预算门禁保持原状", exact=True
                ).wait_for()
                prices = [
                    row["body"]["payload"]["config"]
                    for row in await page.evaluate("window.policyRequests")
                    if row["body"].get("action") == "billing_price"
                ]
                assert prices[-1]["valid_until"] == 1893427200
                await plans.locator("summary").filter(has_text="模型计费依据").click()
                await section.get_by_label("启用本群AI", exact=True).check()
                await section.get_by_label("冷场时主动发起话题", exact=True).check()
                await section.get_by_label("冷场间隔（分钟）", exact=True).fill("30")
                await section.get_by_role(
                    "button", name="保存本群AI", exact=True
                ).click()
                await page.get_by_text(
                    "本群AI设置已保存，预算核验前不调用模型", exact=True
                ).wait_for()
                await expect(
                    section.get_by_label("冷场间隔（分钟）", exact=True)
                ).to_have_value("30")
                group_shot = output / f"group-ai-{width}.png"
                await page.screenshot(path=str(group_shot), full_page=True)
                await section.get_by_label("防刷屏", exact=True).uncheck()
                await section.get_by_role("button", name="保存本群", exact=True).click()
                await page.get_by_text("本群配置已保存", exact=True).wait_for()
                await (
                    section.locator("label")
                    .filter(has_text="成员乙")
                    .get_by_role("checkbox")
                    .check()
                )
                await section.get_by_role("button", name="保存本群", exact=True).click()
                await section.get_by_role(
                    "alertdialog", name="确认管理员变更"
                ).wait_for()
                requests = await page.evaluate("window.policyRequests")
                assert sum(r["body"].get("action") == "tenant" for r in requests) == 1
                await section.get_by_role("button", name="确认变更", exact=True).click()
                await section.get_by_text("群管理员变更已确认", exact=True).wait_for()
                requests = await page.evaluate("window.policyRequests")
                saved = [
                    r["body"]["payload"]
                    for r in requests
                    if r["body"].get("action") == "tenant"
                ][-1]
                assert saved["managers"] == ["1", "2"]
                assert saved["confirmation"] == "preview-test"
                assert not saved["rules"]["flood"]
                await expect(
                    section.get_by_label("AI咨询模型", exact=True)
                ).to_have_value("existing/model")
                await section.get_by_label(
                    "私聊咨询 · AstrBot配置", exact=True
                ).select_option("default")
                await section.get_by_label("私聊咨询 · 人格", exact=True).select_option(
                    "support"
                )
                await section.get_by_label("私聊咨询 · 最多回复Token", exact=True).fill(
                    "1200"
                )
                await section.get_by_role(
                    "button", name="保存私聊咨询配置", exact=True
                ).click()
                await (
                    section.get_by_role("status")
                    .filter(has_text="AI对话配置已保存")
                    .wait_for()
                )
                await expect(
                    section.get_by_label("私聊咨询 · 人格", exact=True)
                ).to_have_value("support")
                await expect(
                    section.get_by_label("私聊咨询 · 最多回复Token", exact=True)
                ).to_have_value("1200")
                await expect(
                    section.get_by_label("群聊默认 · 人格", exact=True)
                ).to_have_value("")
                await section.get_by_label("群聊默认 · 人格", exact=True).select_option(
                    "__builtin__"
                )
                await section.get_by_role(
                    "button", name="保存群聊默认配置", exact=True
                ).click()
                await expect(
                    section.get_by_label("群聊默认 · 人格", exact=True)
                ).to_have_value("__builtin__")
                await section.get_by_label("使用群聊AI默认配置", exact=True).uncheck()
                await section.get_by_label(
                    "本群AI · AstrBot配置", exact=True
                ).select_option("default")
                await section.get_by_label("本群AI · 人格", exact=True).select_option(
                    "support"
                )
                await section.get_by_role(
                    "button", name="保存本群AI配置", exact=True
                ).click()
                await expect(
                    section.get_by_label("本群AI · 人格", exact=True)
                ).to_have_value("support")
                await section.get_by_label("使用群聊AI默认配置", exact=True).check()
                await section.get_by_role(
                    "button", name="保存本群AI配置", exact=True
                ).click()
                await expect(
                    section.get_by_label("本群AI · 人格", exact=True)
                ).to_have_count(0)
                await expect(
                    section.get_by_label("私聊咨询 · 人格", exact=True)
                ).to_have_value("support")
                requests = await page.evaluate("window.policyRequests")
                profiles = [
                    r["body"]["payload"]
                    for r in requests
                    if r["body"].get("action") == "ai_profile"
                ]
                assert len(profiles) == 4 and profiles[-1]["selection"] is None
                await expect(
                    page.get_by_text("独立账号登录", exact=True)
                ).to_have_count(0)
                await expect(
                    page.get_by_label("独立API密钥", exact=True)
                ).to_have_count(0)
                await section.get_by_label("群聊", exact=True).select_option("99/6")
                await expect(
                    section.get_by_label("套餐管理员", exact=True)
                ).to_have_count(0)
                await expect(
                    section.get_by_label("启用本群AI", exact=True)
                ).to_have_count(0)
                await section.get_by_role(
                    "button", name="生成一次性开通码", exact=True
                ).click()
                await section.get_by_text("认领 6 fixture-only", exact=True).wait_for()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                for button in await section.locator("button").all():
                    assert await button.evaluate(
                        "e => e.scrollWidth <= e.clientWidth + 1"
                    )
                shot = output / f"integrated-{width}.png"
                await page.screenshot(path=str(shot), full_page=True)
                with Image.open(shot) as image:
                    assert max(ImageStat.Stat(image.convert("RGB")).stddev) > 10
                assert not errors, errors
                await page.close()
            retired = await browser.new_page()
            await retired.add_init_script(
                FIXTURE + PUBLIC + 'publicState.mode = "isolated";'
            )
            await retired.goto(asset.resolve().as_uri())
            await expect(retired.get_by_role("alert")).to_contain_text("已停用")
            await expect(retired.get_by_label("每日个人AI额度")).to_have_count(0)
            await retired.close()
        finally:
            await browser.close()
    print("PASS integrated controls at 320/390/768/1440px; evidence:", output)


if __name__ == "__main__":
    asyncio.run(main())
