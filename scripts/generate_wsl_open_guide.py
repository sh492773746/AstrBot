"""Generate the public-only menu bitmap using the configured asynchronous image API."""

import argparse
import base64
import io
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from PIL import Image, ImageDraw, ImageOps


def main():
    """Submit one image, poll its task and save only the completed public asset."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--logo", type=Path)
    parser.add_argument("--merged", action="store_true")
    parser.add_argument("--kind", choices=("instructions", "examples"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = {}
    for line in (
        Path("/root/.config/api-credentials/savnx-image.env").read_text().splitlines()
    ):
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip("\"'")
    base = config["SAVNX_IMAGE_BASE_URL"].rstrip("/") + "/"
    prompt = (
        "制作一张1024x1024正方形中文机器人功能说明图，文字准确清晰，现代清爽平面设计，"
        "白色背景、青绿和少量珊瑚红点缀，四个有序的信息带，左侧小型友好机器人形象。"
        "所有文字只包含以下内容，不添加其他功能，不涉及返现、邀请奖励或财务："
        "标题：旺商聊开放机器人。副标题：常用命令。"
        "第一部分：AI咨询。/chat 进入咨询。/exit 结束咨询。"
        "第二部分：功能菜单。菜单。"
        "第三部分：群聊开通。拉机器人进群后发送：开通。"
        "第四部分：群管理。管理 群列表。管理 选择群 1。管理 帮助。"
        "底部：AI只提供说明，不执行操作。群管理仅可管理已授权群。"
        "专业信息图，高对比度中文排版，边距充足，不重叠，不使用渐变或发光效果。"
    )
    if args.logo:
        prompt = (
            "制作1024x1024中文机器人命令菜单海报，手机可读、文字清晰、四周留白。"
            "参考图是客户指定的大海品牌Logo：保留原图红金圆形龙纹及大海字样，"
            "作为顶部左侧160x160品牌标志，不能替换、重画或写错字。"
            "标题：大海机器人。副标题：常用命令。"
            "整体白色底、黑色文字、深红标题、金色细线，专业整洁，不使用渐变、发光、装饰卡片。"
            "Logo只在顶部，不作为文字背景。下方按三个横向信息区清晰排版，字号大且绝不重叠。"
            "第一部分标题：私聊命令。内容：菜单  查看功能；"
            + ("我的邀请  查看自己的邀请记录；" if args.merged else "")
            + "/chat  进入AI咨询；/exit  结束AI咨询。"
            "第二部分标题：群聊开通。内容：群主或群管理员拉我进群；拉我的人在群里发送：开通。"
            "第三部分标题：群管理。内容：管理 群列表；管理 选择群 1；管理 帮助。"
            "底部两行小字：AI咨询开放后可用，不会替你修改设置。群管理命令仅管理员可用。"
            "除这些文字外不添加电话、二维码、UID、网址、价格或虚构功能。"
        )
    if args.kind:
        if not args.logo or not args.output:
            parser.error("--kind requires --logo and --output")
        prompt = (
            "制作一张1024x1024正方形、高清中文机器人入门说明图片，用于手机聊天中打开查看。"
            "参考图是客户的大海品牌Logo。画面顶部左侧(48,32)-(176,160)保留128像素正方形的"
            "Logo区域，仅此区域使用红金Logo，区域内不能有任何其他文字。"
            "标题在Logo右侧，距左至少210像素。正文从y=190开始。"
            "整体背景纯白，深灰正文，青绿用于信息层级，深红和少量金色呼应品牌。"
            "使用工整简洁的中文无衬线字，标题48像素，正文至少25像素。"
            "不使用水印、二维码、网址、电话、UID、渐变、光晕或额外人物插画。"
            "按内容设计真实可读的信息图，不要放在手机模型或展示样机中。"
            "所有文字清晰准确、对齐，不能交叠、截断或溢出；不要添加未列出的功能和承诺。"
        )
        prompt += (
            "标题：大海机器人。副标题：01 使用说明。"
            "第一条醒目文字：发送「菜单」开始使用。"
            "中间按三列三行排列九个主菜单，严格保留序号："
            "1 开通与身份；2 群与成员；3 审核规则；"
            "4 成员与管理员；5 公告与定时；6 抽奖活动；"
            "7 邀请与活动奖励；8 私聊AI咨询；9 群聊与热群AI。"
            "下一部分标题：群聊开通。两行："
            "群主或群管理员拉我进群，由邀请者发送「开通」。"
            "将机器人设为群管理员，再发送「菜单」使用。"
            "下一部分标题：选择与返回。内容：按数字逐级选择；0 返回上一步；取消 退出。"
            "下一行：个人知识库 8→5（私聊）；本群知识库 9→7（管理）。"
            "底部两行：/chat 进入私聊AI咨询；/exit 结束咨询。"
            "仅管理已授权群；AI不执行操作，是否可用以服务状态为准。"
            if args.kind == "instructions"
            else "标题：大海机器人。副标题：02 对话示例。"
            "醒目说明：数字按顺序逐条发送，不要一次发送整串。"
            "正文分上下两部分，使用克制的左右对话气泡，用户和机器人角色清晰，不要挤满。"
            "第一部分标题：群管理 · 添加关键词。"
            "先显示导航：菜单 → 3 → 4。注明：逐条发送。"
            "机器人：请输入一个关键词。"
            "你：示例广告词。"
            "机器人：预览修改，1 确认 / 0 返回。"
            "你：1。"
            "机器人：关键词已添加。"
            "第二部分标题：私聊 · 添加我的知识。"
            "先显示导航：菜单 → 8 → 5 → 2。注明：逐条发送。"
            "机器人：请输入 标题 | 正文。"
            "你：营业时间 | 每天9点开门。"
            "机器人：预览保存，1 确认 / 0 返回。"
            "你：1。"
            "机器人：知识已保存。"
            "底部：0 返回上一步；取消 退出。"
            "说明：示例仅演示操作，群管理须授权；别人不能替你确认。"
        )
    with httpx.Client(timeout=90, trust_env=False) as client:
        headers = {"Authorization": "Bearer " + config["SAVNX_IMAGE_API_KEY"]}
        request = {
            "model": config.get("SAVNX_IMAGE_MODEL", "gpt-image-2"),
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "quality": "medium",
            "async": True,
        }
        if args.logo:
            with args.logo.open("rb") as logo:
                response = client.post(
                    urljoin(base, "images/edits"),
                    headers=headers,
                    data={
                        key: str(value).lower()
                        if isinstance(value, bool)
                        else str(value)
                        for key, value in request.items()
                    },
                    files={"image": (args.logo.name, logo, "image/jpeg")},
                )
        else:
            response = client.post(
                urljoin(base, "images/generations"), headers=headers, json=request
            )
        response.raise_for_status()
        task = response.json()
        task_id = task.get("task_id") or task["id"]
        poll = urljoin(base, task.get("poll_url") or f"images/tasks/{task_id}")
        if httpx.URL(poll).host != httpx.URL(base).host:
            raise RuntimeError("Unexpected image task origin")
        print("Image task submitted:", task_id, flush=True)
        deadline = time.monotonic() + 1800
        while task["status"] == "processing":
            if time.monotonic() > deadline:
                raise RuntimeError("Image task polling deadline reached")
            time.sleep(max(3, min(30, int(response.headers.get("Retry-After", "10")))))
            response = client.get(poll, headers=headers)
            response.raise_for_status()
            task = response.json()
        if task["status"] != "completed":
            raise RuntimeError("Image task failed: " + str(task_id))
        result = task.get("result", {}).get("data", [])
        item = result[0] if result else {"url": task.get("image_url")}
        if item.get("b64_json"):
            data = base64.b64decode(item["b64_json"], validate=True)
        else:
            response = client.get(item["url"])
            response.raise_for_status()
            data = response.content
        if len(data) > 20 * 1024 * 1024:
            raise RuntimeError("Image asset exceeds limit")
    output = args.output or Path(__file__).resolve().parents[1] / (
        "astrbot/builtin_stars/wangshangliao_moderation/assets/open-feature-guide-v1.jpg"
    )
    with Image.open(io.BytesIO(data)) as image:
        if image.size != (1024, 1024):
            raise RuntimeError("Image asset has unexpected dimensions")
        image = image.convert("RGB")
        if args.logo and args.kind:
            box, position, side = (
                ((24, 16, 240, 196), (48, 32), 160)
                if args.kind == "instructions"
                else ((16, 16, 248, 236), (32, 24), 192)
            )
            image.paste("white", box)
            with Image.open(args.logo) as original:
                logo = ImageOps.fit(original.convert("RGB"), (side, side))
            image.paste(logo, position)
        elif args.logo:
            # Keep the supplied brand pixels rather than the model's interpretation.
            image.paste("white", (64, 16, 288, 240))
            with Image.open(args.logo) as original:
                logo = ImageOps.fit(original.convert("RGB"), (224, 224))
            mask = Image.new("L", (224, 224))
            ImageDraw.Draw(mask).ellipse((0, 0, 223, 223), fill=255)
            image.paste(logo, (64, 16), mask)
        image.save(output, "JPEG", quality=92, optimize=True)
    print("Generated public-only guide:", output)


if __name__ == "__main__":
    main()
