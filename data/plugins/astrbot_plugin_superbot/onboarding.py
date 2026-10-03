"""Public onboarding copy and operator-owned navigation."""

import re


def invitation(runtime):
    """Build state-aware copy and links without granting any entitlement."""
    public = runtime.store.get("tenant_policy", {}).get("public", False)
    text = (
        "🎉 **把我加入群聊，免费使用群管理与积分娱乐功能！**\n"
        "群主将我设为管理员，通过「我的群」完成接入并选择功能。"
        if public
        else "🎉 **群管理与积分娱乐功能免费使用**\n当前分批开放，由真实群主申请接入。"
    )
    text += "\nAI、制图及付费广告另行开通。"
    username = getattr(runtime.bot, "username", "")
    buttons = []
    if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        buttons.append(
            ("➕ 添加到群聊", {"url": f"https://t.me/{username}?startgroup"})
        )
    buttons.append(("🏪 我的群", {"action": "tenant_home"}))
    return text, buttons
