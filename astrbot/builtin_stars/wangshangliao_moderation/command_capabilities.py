"""One permission/location catalog for native help, menus and guidance prompts."""

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from astrbot.core.star.components import CommandSpec

from .rule_replies import Advertisement, KeywordReply
from .service_settings import SupportConfiguration
from .syntax import (
    ALIASES,
    BLACKLIST_COMMANDS,
    BUSINESS_COMMANDS,
    NO_ARGUMENT,
    PERSONAL_KNOWLEDGE_COMMANDS,
    RETIRED_CHAT_ACTIONS,
    WHITELIST_COMMANDS,
    WITH_ARGUMENT,
    argument_pattern,
)

SERVER_COMMANDS = {
    "开启群聊回复",
    "关闭群聊回复",
    "开启自主接话",
    "关闭自主接话",
    "开启冷场发话",
    "关闭冷场发话",
    "添加群知识",
    "修改群知识",
    "删除群知识",
    "群知识引用",
}
SUPPORT_COMMANDS = {c for c in BUSINESS_COMMANDS if "客服" in c}
RULE_COMMANDS = BUSINESS_COMMANDS - SUPPORT_COMMANDS
PUBLIC_GROUP = {
    "排名",
    "今日排名",
    "排行",
    "今日排行",
    "参加抽奖",
    "抽奖状态",
    "邀请奖励",
}
PUBLIC_PRIVATE = {
    "客服列表",
    "选择客服",
    "退出客服",
    "/chat",
    "/exit",
    "我的邀请",
    "我的邀请明细",
    "我的套餐",
    "我的额度",
    "套餐说明",
    "我拉了谁",
    "我邀请了谁",
    "我拉的人",
    "我邀请的人",
    "邀请统计说明",
    "为什么没统计",
    "为什么邀请没统计",
    "邀请为什么没统计",
    "拉人没统计",
} | PERSONAL_KNOWLEDGE_COMMANDS
COMMON = {
    "菜单",
    "私聊菜单",
    "帮助",
    "群管帮助",
    "help",
    "取消",
    "0",
    "我的权限",
    "sid",
}


class NativeArguments(BaseModel):
    """Keep the original native argument envelope bounded and explicit."""

    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(default="", max_length=4096)


def arguments_for(command):
    """Describe compatible text syntax and reuse the parsed business constraints."""
    schema = NativeArguments.model_json_schema()
    pattern = argument_pattern(command)
    if pattern:
        schema["properties"]["text"]["pattern"] = "^(?:" + pattern + ")$"
        schema["properties"]["text"].pop("default", None)
        schema["required"] = ["text"]
    if command in {"添加回复", "修改回复"}:
        schema["x-parsed-value"] = KeywordReply.model_json_schema()
    elif command in {"添加广告", "修改广告"}:
        schema["x-parsed-value"] = Advertisement.model_json_schema()
    elif command in {"客服话术", "客服风格", "客服长度", "客服链接", "添加客服知识"}:
        field = {
            "客服话术": "wording",
            "客服风格": "style",
            "客服长度": "length",
            "客服链接": "links",
            "添加客服知识": "knowledge",
        }[command]
        schema["x-parsed-value"] = SupportConfiguration.model_json_schema()[
            "properties"
        ][field]
    return schema


COMMAND_SPECS = {}
for _name in sorted(
    NO_ARGUMENT | WITH_ARGUMENT | BUSINESS_COMMANDS | PUBLIC_PRIVATE | COMMON | {"结账"}
):
    _role, _locations = "group_manager", ("group", "private")
    if _name in COMMON:
        _role = "public"
    elif _name in PUBLIC_GROUP:
        _role, _locations = "public", ("group",)
    elif _name in PUBLIC_PRIVATE:
        _role, _locations = "public", ("private",)
    elif _name in SUPPORT_COMMANDS:
        _role, _locations = "support_editor", ("private",)
    elif _name == "广告图片":
        _locations = ("private",)
    elif _name in SERVER_COMMANDS:
        _role = "server"
    elif _name in {"结账", "结账结果", "确认已付款", "确认修改"}:
        _role, _locations = "finance", ("private",)
    COMMAND_SPECS[_name] = CommandSpec(
        id="wsl.command." + hashlib.sha256(_name.encode()).hexdigest()[:16],
        plugin="wangshangliao_moderation",
        command=_name,
        aliases=tuple(alias for alias, target in ALIASES.items() if target == _name),
        role=_role,
        locations=_locations,
        capability="wsl.commands",
        arguments=arguments_for(_name),
        retired=_name in RETIRED_CHAT_ACTIONS,
        preview=_name in RULE_COMMANDS - {"关键词回复", "定时广告"}
        or _name in SUPPORT_COMMANDS - {"客服设置", "客服知识"}
        or _name in {"踢出", "添加管理员", "移除管理员", "定时禁言"}
        or _name in WHITELIST_COMMANDS - {"白名单"}
        or _name in BLACKLIST_COMMANDS - {"黑名单", "黑名单列表"}
        or _name
        in PERSONAL_KNOWLEDGE_COMMANDS - {"我的知识", "查看我的知识", "我的知识容量"},
    )

CAPABILITIES = {
    name: {
        "command": spec.command,
        "role": spec.role,
        "locations": spec.locations,
        "preview": spec.preview,
        "retired": spec.retired,
    }
    for name, spec in COMMAND_SPECS.items()
}


def command_help(location, roles=(), *, include_server=False):
    """Render executable location and explicit roles without granting authority."""
    roles = {"public", *roles}
    labels = {
        "group_manager": "本群授权管理",
        "support_editor": "专属客服所有者／本机器人指定客服负责人",
        "finance": "独立财务角色",
        "public": "所有人",
        "server": "服务端专属",
    }
    lines = ["【群内规则命令】" if location == "group" else "【私信规则命令】"]
    for role in ("public", "group_manager", "support_editor", "finance"):
        commands = [
            row
            for row in CAPABILITIES.values()
            if row["role"] == role and location in row["locations"]
        ]
        if role in roles and commands:
            lines.append(
                labels[role]
                + "："
                + "、".join(
                    row["command"]
                    + (
                        "（已停用）"
                        if row["retired"]
                        else "（预览确认）"
                        if row["preview"]
                        else ""
                    )
                    for row in commands
                )
            )
    lines.append(
        "群内默认当前群。"
        if location == "group"
        else "群管理先「群列表」「选择群 序号」；专属客服按已核验开通者跨群跨机器人共用。"
        "多个业务先「客服列表」「选择客服 序号」；旧账号客服保持兼容。"
    )
    lines.append(
        "群内命令不用先加好友；私聊管理先选本人授权群。开通后仍需机器人原生群管理权限。"
    )
    lines.append("关键词回复仅群内触发，规则命中后AI不追加回复；时间均为北京时间。")
    if "group_manager" in roles:
        lines.append(
            "黑名单：群内回复原消息或真正@一名成员发送「黑名单」，加入后立即按本群时长禁言，不踢人。"
            "私信或后台添加只登记、不立即处罚。"
            "每次可信真实重入直接禁言，不踢人；「黑名单列表」查询。"
            "「黑名单禁言时长 N分钟／小时／天」预览后本人另发「确认黑名单 令牌」，"
            "范围1—41760整分钟，默认41760分钟（29天）。"
            "只影响之后的群内拉黑和真实重入，不追罚旧名单或重设已有禁言，普通禁言24小时上限不变。"
            "私信选群后「添加黑名单 名称／移除黑名单 名称 → 选择黑名单 序号 → 确认黑名单 令牌」。"
            "只影响当前机器人和本群。群内真实@或回复发送「解除黑名单」（兼容「移除黑名单」），"
            "移出名单并解禁，分别反馈结果；私信或后台移除只移出名单，单独解禁不移除黑名单。"
        )
        lines.append(
            "发言白名单：群内真正@一名成员＋白名单，直接加入，无需搜索、选择或确认。"
            "移除白名单 名称 → 选择白名单 搜索序号 → "
            "确认白名单 预览令牌。仅可选择当前群真实成员，不授予管理权限，也不自动解禁。"
        )
    if include_server:
        lines.append(
            "【服务端专属设置】管理员指引的配置/人格/手册；群聊与热群的模型/人格/工具/"
            "主题/时段/模式/群知识；客服的模型/密钥/工具/安全边界；客服负责人授权；"
            "发送授权及预算。群管仅能切换已开通群AI总开关，不能代替承担人确认。"
        )
    return "\n".join(lines)
