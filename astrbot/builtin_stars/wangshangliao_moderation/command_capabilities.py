"""Active group command declarations and non-executable WebUI guidance."""

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from astrbot.core.star.components import CommandSpec

from .rule_replies import Advertisement, KeywordReply
from .syntax import (
    ALIASES,
    BLACKLIST_COMMANDS,
    BUSINESS_COMMANDS,
    NO_ARGUMENT,
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
RULE_COMMANDS = BUSINESS_COMMANDS
PUBLIC_GROUP = {
    "排名",
    "今日排名",
    "排行",
    "今日排行",
    "参加抽奖",
    "抽奖状态",
    "邀请奖励",
    "我的邀请",
    "我的邀请明细",
    "我拉了谁",
    "我邀请了谁",
    "我拉的人",
    "我邀请的人",
    "邀请统计说明",
    "为什么没统计",
    "为什么邀请没统计",
    "邀请为什么没统计",
    "拉人没统计",
}
COMMON = {"取消"}


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
    return schema


COMMAND_SPECS = {}
for _name in sorted(
    (NO_ARGUMENT | WITH_ARGUMENT | BUSINESS_COMMANDS | COMMON) - RETIRED_CHAT_ACTIONS
):
    _role, _locations = "group_manager", ("group",)
    if _name in COMMON:
        _role = "public"
    elif _name in PUBLIC_GROUP:
        _role, _locations = "public", ("group",)
    elif _name in SERVER_COMMANDS:
        _role, _locations = "server", ()
    COMMAND_SPECS[_name] = CommandSpec(
        id="wsl.command." + hashlib.sha256(_name.encode()).hexdigest()[:16],
        plugin="wangshangliao_moderation",
        command=_name,
        aliases=tuple(alias for alias, target in ALIASES.items() if target == _name),
        role=_role,
        locations=_locations,
        capability="wsl.commands",
        arguments=arguments_for(_name),
        preview=_name in RULE_COMMANDS - {"关键词回复", "定时广告"}
        or _name in {"踢出", "添加管理员", "移除管理员", "定时禁言"}
        or _name in WHITELIST_COMMANDS - {"白名单"}
        or _name in BLACKLIST_COMMANDS - {"黑名单", "黑名单列表"},
    )

COMMAND_SPECS["禁言"] = CommandSpec(
    id="wsl.command.delegated_mute",
    plugin="wangshangliao_moderation",
    command="禁言",
    role="mute_delegate",
    locations=("group",),
    capability="wsl.commands",
    arguments=arguments_for("禁言"),
    preview=False,
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
    if location != "group":
        return (
            "旺商聊私信功能已移除，不处理私信命令、菜单、AI、客服、财务或图片录入。"
            "群管理请在目标群使用现有管理命令；使用帮助、授权管理、规则、图文、活动和财务由已有相应后台权限的负责人在WebUI维护。"
            "普通群管理员不会因此获得全局后台权限。历史会话、账本与审计保留。"
        )
    roles = {"public", *roles}
    labels = {
        "group_manager": "本群授权管理",
        "public": "所有人",
        "server": "服务端专属",
        "mute_delegate": "本群禁言专属授权",
    }
    lines = ["【群内规则命令】"]
    for role in ("public", "group_manager", "mute_delegate"):
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
                    row["command"] + ("（预览确认）" if row["preview"] else "")
                    for row in commands
                )
            )
    lines.append("群内默认当前群，不列出或切换其他群。")
    lines.append(
        "排名、排行及别名供所有群成员查询今日有效发言前20名，不分页、不显示UID；旧页码仍返回前20名。"
    )
    lines.append(
        "群内命令不用先加好友；仍须本人本群授权及机器人原生群管理权限。私信功能已移除。"
    )
    lines.append("关键词回复仅群内触发，规则命中后AI不追加回复；时间均为北京时间。")
    if "mute_delegate" in roles:
        lines.append(
            "禁言专属：客户端真正@一名成员发送「@成员 禁言128」或「@成员 禁言 128」，"
            "表示禁言128分钟，范围1至4320整分钟（最多3天）。仅当前机器人和获授权群，不含踢人、解禁或名单管理。"
            "也支持「@成员 禁言30分」和「@成员 禁言30分钟」；不填时长只提示，不会禁言。"
            "不处理自己、群主、管理员、机器人、有效白名单及黑名单成员；已有禁言不覆盖。"
            "禁言受理后，后台逐条限速撤回该成员本群命令前30分钟内的可核实消息，"
            "单批最多500条，超限、忙碌或未知结果会提示或停止；进度请在后台查看。"
            "受理、拒绝和未知分别提示，未知不自动重试。"
        )
    if "group_manager" in roles:
        lines.append(
            "名单简写：加黑＝黑名单，删黑＝解除黑名单，加白＝白名单，删白＝移除白名单。"
            "原命令继续可用；加白用真正@，加黑和删黑用真正@或可信原生回复；"
            "真正@一名成员＋删白或删白 名称均返回搜索结果，仍需选择并确认，不直接移除或解禁。"
        )
        lines.append(
            "黑名单：群内回复原消息或真正@一名成员发送「黑名单」，加入后立即按本群时长禁言，不踢人。"
            "后台添加只登记、不立即处罚。"
            "每次可信真实重入直接禁言，不踢人；「黑名单列表」查询。"
            "禁言时长沿用本群已保存设置；已有相应后台权限的负责人可在WebUI黑名单设置中预览并确认修改，范围1—41760整分钟。"
            "只影响之后的群内拉黑和真实重入，不追罚旧名单或重设已有禁言，普通禁言24小时上限不变。"
            "只影响当前机器人和本群。群内真实@或回复发送「解除黑名单」（兼容「移除黑名单」），"
            "移出名单并解禁，分别反馈结果；后台移除只移出名单，机器人命令或后台解禁经核验后也移出本群黑名单。"
        )
        lines.append(
            "发言白名单：群内真正@一名成员＋白名单，直接加入，无需搜索、选择或确认。"
            "移除白名单 当前名／保存旧名／精确UID → 选择白名单 搜索序号 → "
            "确认白名单 预览令牌。仅可选择当前群真实成员，不授予管理权限，也不自动解禁。"
        )
        lines.append(
            "普通手动禁言、全员禁言和解除全员禁言的聊天命令已停用，请用旺商聊App。"
            "专门汇报成员只有经服务端单独授权才可用「@成员 禁言128」；管理身份不自动包含此授权。"
            "本群授权管理可用「解禁 @成员」或「解除禁言 @成员」，必须客户端真实@；"
            "普通用户不能自行解禁，AI不能代办。核验后同时移出本群当前黑名单；"
            "平台未核验、名单或权限变化时分别反馈，不强行删除。"
            "自动广告处罚、防刷屏和每日定时禁言、解除仍按设置运行。"
            "「定时禁言 23:00 08:00」预览后本人「确认定时 令牌」。"
            "未知结果暂停核对，不自动重试。"
        )
    if include_server:
        lines.append(
            "【WebUI与服务端】规则、关键词及定时图片、活动与财务；群聊与热群的模型/人格/"
            "工具/主题/时段/模式/群知识、发送授权及预算。"
            "仅已有相应后台权限者维护，不向普通群管理授予全局后台登录。"
            "群管仅能切换已开通群AI总开关，不能代替承担人确认。"
        )
    return "\n".join(lines)
