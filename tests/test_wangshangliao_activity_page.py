"""Opt-in compiled page checks against real isolated activity and control APIs."""

import asyncio
import base64
import copy
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_ACTIVITY_PAGE"), reason="Opt-in compiled UI"
)
async def test_split_pages_lottery_fields_and_current_unmute(
    page_app, headers, rpc_scope, tmp_path
):
    from playwright.async_api import async_playwright, expect

    app = page_app[0]
    service, adapter, _ = rpc_scope
    adapter.groups = {"5": "1005", "6": "1006"}
    muted = {"1002"}
    adapter.nim = SimpleNamespace(
        get_team_muted_members=AsyncMock(side_effect=lambda _: set(muted))
    )

    async def execute(request, *, authorize):
        await authorize()
        assert request.action == "unmute" and request.member == 2
        muted.clear()
        return {"status": "accepted"}

    adapter.execute_action = AsyncMock(side_effect=execute)
    calls = []
    slow_upload = False
    rpc = OpenRPC(service)
    output = Path(os.environ.get("WSL_ACTIVITY_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:

        async def api(_source, method, endpoint, body=None):
            calls.append((method, endpoint, copy.deepcopy(body)))
            if endpoint == "open-service":
                if slow_upload and body["payload"].get("operation") == "image_upload":
                    await asyncio.sleep(0.4)
                return await rpc.dispatch(OpenCall(actor="dashboard/offline", **body))
            if method == "GET" and endpoint == "settings":
                result = (await client.get(PREFIX + "/settings")).json()["data"]
                result["open_mode"] = "integrated"
                result["bots"][0]["account_id"] = "99"
                return result
            response = await client.request(
                method,
                PREFIX + "/" + endpoint,
                **({"params": body or {}} if method == "GET" else {"json": body}),
            )
            assert response.status_code == 200, response.text
            return response.json()["data"]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                for width in (320, 390, 1440):
                    muted.add("1002")
                    page = await browser.new_page(
                        viewport={"width": width, "height": 1000}
                    )
                    errors = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    await page.expose_binding("fixtureAPI", api)
                    await page.add_init_script("""
                      const realTimeout = window.setTimeout.bind(window);
                      window.setTimeout = (callback, delay, ...args) =>
                        realTimeout(callback, (window.shortImageTimeout && delay === 45000 || window.shortReadTimeout && delay === 15000) ? 25 : delay, ...args);
                      Object.defineProperty(window, "AstrBotPluginPage", {value:{
                        ready:async()=>({}),getLocale:()=> "zh-CN",onContext:()=>()=>{},
                        apiGet:(endpoint,body)=>window.fixtureAPI("GET",endpoint,body),
                        apiPost:(endpoint,body)=>window.fixtureAPI("POST",endpoint,body),
                      }});
                    """)
                    await page.goto(
                        Path(os.environ["WSL_ACTIVITY_PAGE"]).resolve().as_uri()
                    )
                    try:
                        await expect(
                            page.get_by_role("tab", name="开通", exact=True)
                        ).to_be_visible(timeout=10000)
                    except AssertionError:
                        raise AssertionError(
                            {
                                "text": await page.locator("body").inner_text(),
                                "errors": errors,
                                "calls": calls,
                            }
                        ) from None
                    await page.get_by_role("tab", name="开通", exact=True).click()
                    scope = page.locator(".open-service")
                    await scope.get_by_label("群聊", exact=True).select_option("99/5")
                    await expect(
                        scope.get_by_role("checkbox", name="防刷屏", exact=True)
                    ).not_to_be_visible()
                    await page.get_by_role("tab", name="规则", exact=True).click()
                    await expect(
                        scope.get_by_role("checkbox", name="防刷屏", exact=True)
                    ).to_be_visible()
                    await expect(
                        scope.get_by_role("button", name="生成一次性开通码")
                    ).not_to_be_visible()
                    await page.screenshot(
                        path=str(output / f"rules-{width}.png"), full_page=True
                    )
                    from tests.test_wangshangliao_advertisement_images import bitmap

                    automation = scope.locator(".automation")
                    await automation.get_by_role(
                        "button", name="添加发言计划", exact=True
                    ).click()
                    ad = automation.locator("article").last
                    await ad.get_by_label("名称", exact=True).fill(
                        f"图片轮换测试{width}"
                    )
                    await ad.get_by_label("时刻（逗号分隔）", exact=True).fill(
                        "09:00,18:00"
                    )
                    files = [
                        {
                            "name": f"image-{n}.png",
                            "mimeType": "image/png",
                            "buffer": base64.b64decode(bitmap(color)),
                        }
                        for n, color in enumerate(("red", "green", "blue"), 1)
                    ]
                    async with page.expect_file_chooser() as picker:
                        await ad.get_by_role(
                            "button", name="上传图片", exact=False
                        ).click()
                    await (await picker.value).set_files([])
                    await expect(
                        automation.get_by_role("button", name="预览修改", exact=True)
                    ).to_be_enabled()
                    uploads_before = len(
                        [
                            c
                            for c in calls
                            if c[1] == "open-service"
                            and c[2].get("payload", {}).get("operation")
                            == "image_upload"
                        ]
                    )
                    await ad.get_by_label("上传轮换图片", exact=True).set_input_files(
                        [*files, files[0]]
                    )
                    await expect(automation.get_by_role("alert")).to_have_text(
                        "每个计划最多上传3张图片"
                    )
                    assert (
                        len(
                            [
                                c
                                for c in calls
                                if c[1] == "open-service"
                                and c[2].get("payload", {}).get("operation")
                                == "image_upload"
                            ]
                        )
                        == uploads_before
                    )
                    await ad.get_by_label("上传轮换图片", exact=True).set_input_files(
                        files
                    )
                    await expect(ad.locator(".media-item")).to_have_count(3)
                    await ad.get_by_label("替换第1张图片", exact=True).set_input_files(
                        files[2]
                    )
                    await expect(ad.locator("fieldset:disabled")).to_have_count(0)
                    await expect(
                        automation.get_by_role("button", name="预览修改", exact=True)
                    ).to_be_enabled()
                    before_timeout = await ad.locator(".media-item > img").evaluate_all(
                        "(items) => items.map(item => item.src)"
                    )
                    slow_upload = True
                    await page.evaluate("window.shortImageTimeout = true")
                    await ad.get_by_label("替换第1张图片", exact=True).set_input_files(
                        files[0]
                    )
                    await expect(automation.get_by_role("alert")).to_contain_text(
                        "图片上传超时"
                    )
                    await expect(
                        automation.get_by_role("button", name="预览修改", exact=True)
                    ).to_be_enabled()
                    await page.wait_for_timeout(600)
                    assert (
                        await ad.locator(".media-item > img").evaluate_all(
                            "(items) => items.map(item => item.src)"
                        )
                        == before_timeout
                    )
                    slow_upload = False
                    await page.evaluate("window.shortImageTimeout = false")
                    await page.evaluate("""() => {
                        window.originalRead = FileReader.prototype.readAsDataURL;
                        FileReader.prototype.readAsDataURL = function() {};
                        window.shortReadTimeout = true;
                    }""")
                    await ad.get_by_label("替换第1张图片", exact=True).set_input_files(
                        files[0]
                    )
                    await expect(automation.get_by_role("alert")).to_contain_text(
                        "图片读取超时"
                    )
                    await expect(
                        automation.get_by_role("button", name="预览修改", exact=True)
                    ).to_be_enabled()
                    await page.evaluate("""() => {
                        FileReader.prototype.readAsDataURL = window.originalRead;
                        window.shortReadTimeout = false;
                    }""")
                    await ad.get_by_role(
                        "button", name="上移第3张图片", exact=True
                    ).click()
                    await ad.get_by_role(
                        "button", name="移除第2张图片", exact=True
                    ).click()
                    await expect(ad.locator(".media-item")).to_have_count(2)
                    await ad.screenshot(
                        path=str(output / f"scheduled-image-editor-{width}.png")
                    )
                    await ad.get_by_role(
                        "group", name="定时发言图片", exact=True
                    ).evaluate(
                        """(element, encoded) => {
                          const transfer = new DataTransfer();
                          transfer.items.add(new File([Uint8Array.from(atob(encoded), ch => ch.charCodeAt(0))], "pasted.png", {type:"image/png"}));
                          element.dispatchEvent(new ClipboardEvent("paste", {clipboardData: transfer, bubbles: true, cancelable: true}));
                        }""",
                        base64.b64encode(files[0]["buffer"]).decode(),
                    )
                    await expect(ad.locator(".media-item")).to_have_count(3)
                    await ad.get_by_role(
                        "button", name="移除第3张图片", exact=True
                    ).click()
                    await ad.get_by_role(
                        "group", name="定时发言图片", exact=True
                    ).evaluate(
                        """(element, encoded) => {
                          const transfer = new DataTransfer();
                          transfer.items.add(new File([Uint8Array.from(atob(encoded), ch => ch.charCodeAt(0))], "dropped.png", {type:"image/png"}));
                          element.dispatchEvent(new DragEvent("drop", {dataTransfer: transfer, bubbles: true, cancelable: true}));
                        }""",
                        base64.b64encode(files[1]["buffer"]).decode(),
                    )
                    await expect(ad.locator(".media-item")).to_have_count(3)
                    await expect(
                        automation.get_by_role("button", name="预览修改", exact=True)
                    ).to_be_enabled()
                    assert await ad.locator("img").evaluate_all(
                        "(images) => images.every(img => img.complete && img.naturalWidth > 0)"
                    )
                    image_count = await automation.locator(".media-item").count()
                    await automation.get_by_role(
                        "button", name="预览修改", exact=True
                    ).click()
                    confirmation = automation.get_by_role(
                        "alertdialog", name="确认业务规则"
                    )
                    await expect(confirmation.locator("img")).to_have_count(image_count)
                    await confirmation.get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    await expect(confirmation).to_have_count(0)
                    await expect(
                        automation.get_by_text("尚未保存", exact=False)
                    ).to_have_count(0)
                    saved = service.rule_replies.read("99", "5")["config"]["ads"][-1]
                    assert len(saved["images"]) == 3 and not saved["text"]
                    await page.screenshot(
                        path=str(output / f"scheduled-images-{width}.png"),
                        full_page=True,
                    )
                    saved_before = copy.deepcopy(
                        service.rule_replies.read("99", "5")["config"]
                    )
                    await automation.get_by_role(
                        "button", name="添加关键词回复", exact=True
                    ).click()
                    reply = automation.locator("fieldset").first.locator("article").last
                    await reply.get_by_label("名称", exact=True).fill("配图回复测试")
                    await reply.get_by_label("同义词（每行一个）", exact=True).fill(
                        "通知"
                    )
                    await reply.get_by_label(
                        "上传关键词配图", exact=True
                    ).set_input_files(files[0])
                    await expect(reply.locator(".media-item")).to_have_count(1)
                    await reply.get_by_label(
                        "替换关键词配图", exact=True
                    ).set_input_files(files[1])
                    await expect(
                        automation.get_by_role("button", name="预览修改", exact=True)
                    ).to_be_enabled()
                    await reply.screenshot(
                        path=str(output / f"keyword-image-{width}.png")
                    )
                    await automation.get_by_role(
                        "button", name="新增三条计划", exact=True
                    ).click()
                    stagger = automation.get_by_role(
                        "group", name="三条消息错峰排程", exact=True
                    )
                    new_ads = automation.locator("fieldset").nth(1).locator("article")
                    await expect(stagger).to_contain_text("20 / 20 / 20")
                    for index in range(3):
                        item = new_ads.nth(await new_ads.count() - 3 + index)
                        expected = [f"{hour:02}:{index * 20:02}" for hour in range(24)]
                        await expect(
                            item.get_by_label("时刻（逗号分隔）", exact=True)
                        ).to_have_value(",".join(expected))
                        await item.get_by_label("文字与链接", exact=True).fill(
                            f"测试通知{index + 1}"
                        )
                        await item.get_by_label(
                            "上传轮换图片", exact=True
                        ).set_input_files(files[index])
                    assert (
                        service.rule_replies.read("99", "5")["config"] == saved_before
                    )
                    await stagger.get_by_label("第2条 · 每小时第几分").fill("0")
                    await stagger.get_by_role("button", name="应用三条时间").click()
                    await expect(automation.get_by_role("alert")).to_contain_text(
                        "分钟须为0至59"
                    )
                    await stagger.get_by_label("第2条 · 每小时第几分").fill("15")
                    await stagger.get_by_label("第3条 · 每小时第几分").fill("45")
                    await stagger.get_by_role("button", name="应用三条时间").click()
                    await expect(stagger).to_contain_text("15 / 30 / 15")
                    assert (
                        service.rule_replies.read("99", "5")["config"] == saved_before
                    )
                    await stagger.screenshot(
                        path=str(output / f"three-message-timing-{width}.png")
                    )
                    selected_ids = await stagger.locator("select").evaluate_all(
                        "(items) => items.map(item => item.value)"
                    )
                    await automation.get_by_role(
                        "button", name="预览修改", exact=True
                    ).click()
                    await expect(confirmation.locator("img")).to_have_count(
                        image_count + 4
                    )
                    await confirmation.get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    await expect(confirmation).to_have_count(0)
                    result = service.rule_replies.read("99", "5")["config"]
                    assert (
                        await stagger.locator("select").evaluate_all(
                            "(items) => items.map(item => item.value)"
                        )
                        == selected_ids
                    )
                    await expect(
                        stagger.get_by_label("第2条 · 每小时第几分")
                    ).to_have_value("15")
                    assert (
                        result["keywords"][-1]["images"]
                        and not result["keywords"][-1]["text"]
                    )
                    for item, minute in zip(result["ads"][-3:], (0, 15, 45)):
                        assert item["mode"] == "daily" and not item["enabled"]
                        assert item["times"] == [
                            f"{hour:02}:{minute:02}" for hour in range(24)
                        ]
                        assert len(item["images"]) == 1
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    await page.get_by_role("tab", name="活动与财务", exact=True).click()
                    await page.get_by_role(
                        "tab", name="玩家福利抽奖", exact=True
                    ).click()
                    await expect(
                        page.get_by_label("拉几人可以抽", exact=False)
                    ).to_have_count(0)
                    await expect(
                        page.get_by_label("每位有效邀请奖励", exact=False)
                    ).to_have_count(0)
                    await page.get_by_label("活动名称", exact=True).fill("玩家福利测试")
                    await page.get_by_label("领奖联系人", exact=True).fill("测试管理")
                    await page.get_by_label("总金额（元）", exact=True).fill("10.01")
                    await page.get_by_label("参加人数", exact=True).fill("3")
                    note = "必须带活动头像\n昵称带江哥福利 <仅作文字展示>"
                    await page.get_by_label("抽奖备注", exact=False).fill(note)
                    await expect(
                        page.get_by_label("抽奖备注", exact=False)
                    ).to_have_attribute("maxlength", "500")
                    await page.get_by_label("红包类型", exact=True).select_option(
                        "lucky"
                    )
                    await page.get_by_role(
                        "checkbox", name="是否按照邀请进行抽奖", exact=True
                    ).check()
                    await page.get_by_label("拉几人可以抽", exact=False).fill("2")
                    await expect(
                        page.get_by_label("拉几人可以抽", exact=False)
                    ).to_be_visible()
                    await page.get_by_role(
                        "checkbox", name="是否按照邀请进行抽奖", exact=True
                    ).uncheck()
                    await page.get_by_role(
                        "button", name="保存抽奖参数", exact=True
                    ).click()
                    await expect(
                        page.get_by_text("活动参数已保存", exact=True)
                    ).to_be_visible()
                    last = [
                        body
                        for method, endpoint, body in calls
                        if method == "POST" and endpoint == "activities"
                    ][-1]
                    assert (
                        "invite_rate" not in last
                        and last["lottery"]["invite_gate"] == 0
                        and last["lottery"]["remarks"] == note
                    )
                    with sqlite3.connect(page_app[4] / "activities.sqlite3") as db:
                        db.execute(
                            "INSERT INTO lotteries(id,account,group_id,prize,status,"
                            "created,ends,capacity,winners,contact,total,total_amount,"
                            "packet_mode,packet_amounts,results,unclaimed_amount,remarks) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                f"finished-{width}",
                                "1",
                                "5",
                                "红包分配测试",
                                "finished",
                                1792656000 + width,
                                1792656000 + width,
                                3,
                                3,
                                "测试管理",
                                1,
                                1001,
                                "ordinary",
                                "[334,334,333]",
                                json.dumps(
                                    [
                                        {
                                            "uid": "2",
                                            "peer": "902",
                                            "name": "玩家",
                                            "amount": 334,
                                        }
                                    ]
                                ),
                                667,
                                note,
                            ),
                        )
                        db.execute(
                            "INSERT INTO lottery_entries(lottery,member,peer,name,joined) "
                            "VALUES(?,?,?,?,?)",
                            (
                                f"finished-{width}",
                                "2",
                                "902",
                                "玩家",
                                1792656000 + width,
                            ),
                        )
                    await page.get_by_role(
                        "button", name="刷新活动状态", exact=True
                    ).click()
                    await expect(page.locator(".lottery-remarks")).to_have_text(note)
                    await expect(page.locator(".lottery-remarks img")).to_have_count(0)
                    await expect(
                        page.locator("dl div").filter(
                            has=page.get_by_text("未领取退回（未支出）", exact=True)
                        )
                    ).to_contain_text("6.67")
                    await page.screenshot(
                        path=str(output / f"lottery-{width}.png"), full_page=True
                    )
                    await page.get_by_role("tab", name="邀请奖励", exact=True).click()
                    await expect(
                        page.get_by_label("总金额（元）", exact=True)
                    ).to_have_count(0)
                    await page.get_by_label("每位有效邀请奖励", exact=False).fill(
                        str(width / 100)
                    )
                    await page.get_by_role(
                        "button", name="保存邀请奖励", exact=True
                    ).click()
                    await expect(
                        page.get_by_text("活动参数已保存", exact=True)
                    ).to_be_visible()
                    last = [
                        body
                        for method, endpoint, body in calls
                        if method == "POST" and endpoint == "activities"
                    ][-1]
                    assert "lottery" not in last
                    await page.screenshot(
                        path=str(output / f"invite-rewards-{width}.png"), full_page=True
                    )
                    await page.get_by_role("tab", name="运行与日志", exact=True).click()
                    await page.get_by_role("tab", name="处罚记录", exact=True).click()
                    panel = page.get_by_role("region", name="当前禁言成员", exact=True)
                    await panel.get_by_role(
                        "button", name="解除禁言", exact=True
                    ).click()
                    dialog = panel.get_by_role(
                        "alertdialog", name="确认解除禁言", exact=True
                    )
                    await expect(dialog).to_be_visible()
                    before = adapter.execute_action.await_count
                    await dialog.get_by_role("button", name="取消", exact=True).click()
                    assert adapter.execute_action.await_count == before
                    await panel.get_by_role(
                        "button", name="解除禁言", exact=True
                    ).click()
                    await page.screenshot(
                        path=str(output / f"current-mutes-{width}.png"), full_page=True
                    )
                    await dialog.get_by_role(
                        "button", name="确认解禁", exact=True
                    ).click()
                    await expect(
                        panel.get_by_text("当前没有禁言成员", exact=True)
                    ).to_be_visible()
                    assert adapter.execute_action.await_count == before + 1
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    assert not errors, errors
                    await page.close()
            finally:
                await browser.close()
