"""Bounded text-tree navigation over the existing fixed command handlers."""

import re
import time
from dataclasses import dataclass, field, replace

from astrbot.api.message_components import Plain
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .command_capabilities import CAPABILITIES, SUPPORT_COMMANDS
from .observability import record_event
from .owner_support import SELECTION_COMMANDS
from .policy import require_group_admin
from .syntax import (
    BLACKLIST_COMMANDS,
    KNOWLEDGE_COMMANDS,
    PERSONAL_KNOWLEDGE_COMMANDS,
    WHITELIST_COMMANDS,
    recognize_command,
)
from .text import redact_reply

FOOTER = "\n回复本页一个数字或完整栏目名；0 返回上一步；取消 退出菜单。"
ENTRY_WORDS = {
    "菜单",
    "帮助",
    "私聊菜单",
    "群管帮助",
    "help",
    "/菜单",
    "/帮助",
    "/help",
}
READ_COMMANDS = {
    "黑名单",
    "黑名单列表",
    "添加黑名单",
    "移除黑名单",
    "选择黑名单",
    "黑名单禁言时长",
    "白名单",
    "添加白名单",
    "移除白名单",
    "选择白名单",
    "客服列表",
    "关键词回复",
    "定时广告",
    "客服设置",
    "客服知识",
    "我的权限",
    "群列表",
    "选择群",
    "成员列表",
    "禁言名单",
    "成员搜索",
    "下一页",
    "能力",
    "规则",
    "测试规则",
    "违规计数",
    "结果",
    "开发门禁",
    "管理员列表",
    "热群状态",
    "我的套餐",
    "我的额度",
    "套餐说明",
    "本群额度",
    "定时状态",
    "排名",
    "抽奖状态",
    "抽奖设置",
    "中奖名单",
    "抽奖记录",
    "我的邀请",
    "我的邀请明细",
    "邀请奖励",
    "邀请统计说明",
    "邀请奖励状态",
    "邀请记录",
    "邀请统计",
    "我的知识",
    "查看我的知识",
    "我的知识容量",
    "本群知识",
    "查看群知识",
    "本群知识容量",
    "本群知识范围",
}
MEMBER_COMMANDS = {"解禁", "踢出", "添加管理员", "移除管理员"}


@dataclass(frozen=True)
class Item:
    """Describe one fixed command or a numbered menu branch."""

    label: str
    command: str = ""
    prompt: str = ""
    children: tuple = ()
    page: str = ""


TREE = (
    Item(
        "开通与身份",
        children=(
            Item("开通本群", "开通"),
            Item("用开通码认领", "认领", "请输入：群号 开通码"),
            Item("我的权限", "我的权限"),
            Item("机器人管理能力", "能力"),
        ),
    ),
    Item(
        "群与成员",
        children=(
            Item("选择管理群", "选择群", "请输入上面列表中的群序号"),
            Item("群列表", "群列表"),
            Item("成员列表", "成员列表"),
            Item("搜索成员", "成员搜索", "请输入要查找的成员名称"),
            Item("成员下一页", "下一页"),
            Item("今日发言排名", "排名"),
            Item("发言排名翻页", "排名", "请输入排行榜页码，从1开始"),
        ),
    ),
    Item(
        "审核规则",
        children=(
            Item("查看规则与关键词", "规则"),
            Item(
                "广告审核",
                children=(
                    Item("开启广告审核", "开启广告审核"),
                    Item("关闭广告审核", "关闭广告审核"),
                ),
            ),
            Item(
                "防刷屏",
                children=(
                    Item("开启防刷屏", "开启防刷屏"),
                    Item("关闭防刷屏", "关闭防刷屏"),
                ),
            ),
            Item(
                "添加关键词", "添加关键词", "请输入一个关键词（不是代码或正则表达式）"
            ),
            Item("删除关键词", "删除关键词", "请输入上面规则中要删除的关键词序号"),
            Item("违规计数", "违规计数"),
            Item("查询操作结果", "结果", "请输入要核对的操作编号"),
            Item("测试功能状态", "开发门禁"),
            Item("修改关键词", "修改关键词", "请输入：关键词序号 新关键词"),
            Item(
                "组合规则管理",
                children=(
                    Item("查看本群规则", "规则"),
                    Item(
                        "添加组合规则",
                        "添加规则",
                        "请输入：规则名称/词一/词二（须同时出现；最多5个词；也可用 | 分隔）",
                    ),
                    Item(
                        "修改组合规则",
                        "修改规则",
                        "请输入：规则序号 规则名称/词一/词二（也可用 | 分隔）",
                    ),
                    Item("启用组合规则", "启用规则", "请输入要启用的规则序号"),
                    Item("停用组合规则", "停用规则", "请输入要停用的规则序号"),
                    Item("删除组合规则", "删除规则", "请输入要删除的规则序号"),
                ),
            ),
            Item("测试一段文字", "测试规则", "请输入待测试的文字；只检查，不处罚"),
            Item(
                "发言白名单",
                children=(
                    Item("查看本群白名单", "白名单"),
                    Item(
                        "添加白名单",
                        "添加白名单",
                        "请输入对方在本群的名称，搜索后再选择",
                    ),
                    Item(
                        "移除白名单",
                        "移除白名单",
                        "请输入对方在本群的名称，搜索后再选择",
                    ),
                    Item(
                        "选择搜索成员", "选择白名单", "请输入本次搜索结果中的成员序号"
                    ),
                    Item("确认已有白名单申请", "确认白名单", "请输入刚才收到的确认码"),
                ),
            ),
            Item(
                "本群黑名单",
                children=(
                    Item("查看本群黑名单", "黑名单列表"),
                    Item("群内回复或真正@一人拉黑", "黑名单"),
                    Item("私信添加黑名单", "添加黑名单", "请输入对方在本群的名称"),
                    Item("移除黑名单", "移除黑名单", "请输入黑名单成员的名称"),
                    Item("选择搜索成员", "选择黑名单", "请输入本次搜索结果中的序号"),
                    Item("确认黑名单修改", "确认黑名单", "请输入刚才收到的确认码"),
                    Item(
                        "设置黑名单重入禁言时长",
                        "黑名单禁言时长",
                        "请输入整分钟时长，如60分钟、24小时或29天；换算后须为1—41760分钟",
                    ),
                ),
            ),
        ),
    ),
    Item(
        "成员与管理员操作",
        children=(
            Item("移出成员", "踢出", "请输入成员序号，也可用客户端真正@该成员"),
            Item("管理员列表", "管理员列表"),
            Item("添加管理员", "添加管理员", "请输入成员序号，也可用客户端真正@该成员"),
            Item("移除管理员", "移除管理员", "请输入成员序号，也可用客户端真正@该成员"),
            Item("确认已有移出申请", "确认踢出", "请输入刚才收到的确认码"),
            Item("确认已有授权申请", "确认授权", "请输入刚才收到的确认码"),
            Item("解除成员禁言", "解禁", "请输入成员序号，也可用客户端真正@该成员"),
            Item("查看禁言名单", "禁言名单"),
        ),
    ),
    Item(
        "公告与定时禁言",
        children=(
            Item("发布公告", "公告", "请输入公告内容（一次一行）"),
            Item(
                "设置每日禁言和解除时间",
                "定时禁言",
                "请输入北京时间：禁言时间 解除时间，例如 23:00 08:00",
            ),
            Item("查看定时状态", "定时状态"),
            Item("暂停定时", "暂停定时"),
            Item("恢复定时", "恢复定时"),
            Item("删除定时", "删除定时"),
            Item("确认已有定时申请", "确认定时", "请输入刚才收到的确认码"),
        ),
    ),
    Item(
        "抽奖活动",
        children=(
            Item("参与玩家福利抽奖", "参加抽奖"),
            Item("抽奖状态", "抽奖状态"),
            Item("查看抽奖设置", "抽奖设置"),
            Item(
                "抽奖设置",
                children=(
                    Item("设置奖品", "抽奖奖励", "请输入奖品名称"),
                    Item("中奖人数", "中奖人数", "请输入中奖人数"),
                    Item("开奖倒计时", "抽奖倒计时", "请输入倒计时分钟数"),
                    Item(
                        "参与人数上限",
                        "参与上限",
                        "请输入人数；输入「不限」表示不限制人数",
                    ),
                    Item(
                        "邀请人数门槛",
                        "抽奖邀请门槛",
                        "请输入人数；输入「不限」表示不设门槛",
                    ),
                    Item("领奖联系人", "领奖联系人", "请输入领奖联系信息"),
                ),
            ),
            Item("开启抽奖", "开启抽奖"),
            Item("立即开奖", "立即开奖"),
            Item("取消抽奖", "取消抽奖"),
            Item("中奖名单", "中奖名单"),
            Item("中奖名单翻页", "中奖名单", "请输入中奖名单页码，从1开始"),
            Item("历史抽奖记录", "抽奖记录"),
        ),
    ),
    Item(
        "邀请查询与活动奖励",
        children=(
            Item("我的邀请（私聊查返现）", "我的邀请"),
            Item("我邀请的成员", "我的邀请明细"),
            Item("我的活动邀请奖励", "邀请奖励"),
            Item("为什么没有统计", "邀请统计说明"),
            Item(
                "群邀请统计与奖励",
                children=(
                    Item("邀请统计", "邀请统计"),
                    Item("邀请记录", "邀请记录"),
                    Item("奖励状态", "邀请奖励状态"),
                    Item(
                        "设置活动奖励",
                        "设置邀请奖励",
                        "请输入每人活动奖励金额，例如 5；不是返现账本改价",
                    ),
                    Item("开启邀请统计", "开启邀请统计"),
                    Item("暂停邀请统计", "暂停邀请统计"),
                    Item("开启邀请奖励", "开启邀请奖励"),
                    Item("暂停邀请奖励", "暂停邀请奖励"),
                ),
            ),
            Item("财务结账（仅私聊授权人员）", "结账"),
        ),
    ),
    Item(
        "私聊AI咨询（不执行操作）",
        children=(
            Item("进入AI咨询", "/chat"),
            Item("结束AI咨询", "/exit"),
            Item("我的套餐 / 我的额度", "我的套餐"),
            Item("套餐说明与升级联系", "套餐说明"),
            Item(
                "我的知识库",
                children=(
                    Item("查看内容", "我的知识"),
                    Item("添加内容", "添加我的知识", "请输入：标题 | 正文"),
                    Item(
                        "修改内容",
                        "修改我的知识",
                        "请输入：资料序号 新标题 | 新正文",
                    ),
                    Item("删除内容", "删除我的知识", "请输入要删除的资料序号"),
                    Item("查看容量", "我的知识容量"),
                    Item("查看全文", "查看我的知识", "请输入资料序号"),
                ),
            ),
        ),
    ),
    Item(
        "群聊与热群AI",
        children=(
            Item("本群聊天/热群状态", "热群状态"),
            Item(
                "本群AI总开关",
                children=(
                    Item("开启本群AI", "开启热群"),
                    Item("关闭本群AI", "关闭热群"),
                ),
            ),
            Item(
                "@热群对话",
                children=(
                    Item("开启@回复", "开启群聊回复"),
                    Item("关闭@回复", "关闭群聊回复"),
                ),
            ),
            Item(
                "冷场主动发话",
                children=(
                    Item("开启冷场发话", "开启冷场发话"),
                    Item("关闭冷场发话", "关闭冷场发话"),
                ),
            ),
            Item("本群额度", "本群额度"),
            Item("确认本群使用我的共享额度", "确认本群扣费"),
            Item(
                "本群知识库",
                children=(
                    Item("查看内容", "本群知识"),
                    Item("添加内容", "添加群知识", "请输入：标题 | 正文"),
                    Item(
                        "修改内容",
                        "修改群知识",
                        "请输入：资料序号 新标题 | 新正文",
                    ),
                    Item("删除内容", "删除群知识", "请输入要删除的资料序号"),
                    Item(
                        "设置使用范围",
                        children=(
                            Item("查看当前范围", "本群知识范围"),
                            Item("允许@回复引用", "群知识引用 回复 开启"),
                            Item("停止@回复引用", "群知识引用 回复 关闭"),
                            Item("允许主动热群引用", "群知识引用 热群 开启"),
                            Item("停止主动热群引用", "群知识引用 热群 关闭"),
                        ),
                    ),
                    Item("查看容量", "本群知识容量"),
                    Item("查看全文", "查看群知识", "请输入资料序号"),
                ),
            ),
            Item(
                "普通聊天自主接话",
                children=(
                    Item("开启自主接话", "开启自主接话"),
                    Item("关闭自主接话", "关闭自主接话"),
                ),
            ),
        ),
    ),
    Item(
        "群关键词回复与定时广告",
        children=tuple(
            Item(
                name,
                name,
                ""
                if name in {"关键词回复", "定时广告"}
                else "请输入对应规则参数；保存前需预览确认",
            )
            for name in (
                "关键词回复",
                "添加回复",
                "修改回复",
                "删除回复",
                "启用回复",
                "停用回复",
                "定时广告",
                "添加广告",
                "修改广告",
                "删除广告",
                "启用广告",
                "停用广告",
            )
        ),
    ),
    Item(
        "专属与账号业务客服",
        children=tuple(
            Item(
                name,
                name,
                "" if name in {"客服设置", "客服知识"} else "请输入客服业务资料",
            )
            for name in (
                "客服设置",
                "客服话术",
                "客服风格",
                "客服长度",
                "客服链接",
                "客服知识",
                "添加客服知识",
                "删除客服知识",
            )
        )
        + (
            Item("客服列表", "客服列表"),
            Item("选择客服", "选择客服", "请输入客服列表中的序号"),
            Item("退出客服", "退出客服"),
        ),
    ),
)


QUICK_TREE = (
    Item(
        "本群状态",
        children=(
            Item("我的权限", "我的权限"),
            Item("查看本群规则", "规则"),
            Item("关键词回复", "关键词回复"),
            Item("定时发言", "定时广告"),
            Item("开通本群", "开通"),
            Item("选择管理群", "选择群", "请输入群列表中的序号"),
            Item("群列表", "群列表"),
        ),
    ),
    Item(
        "成员操作",
        children=(
            Item("查看禁言名单", "禁言名单"),
            Item(
                "解除成员禁言",
                "解禁",
                "请从客户端真正@需要解禁的成员，也可输入成员序号",
            ),
            Item("成员列表", "成员列表"),
            Item("移出成员", "踢出", "请从客户端真正@需要移出的成员，也可输入成员序号"),
            Item("管理员列表", "管理员列表"),
        ),
    ),
    Item(
        "抽奖与邀请",
        children=(
            Item("参与玩家福利抽奖", "参加抽奖"),
            Item("抽奖状态", "抽奖状态"),
            Item("我的邀请", "我的邀请"),
            Item("我邀请的成员", "我的邀请明细"),
            Item("开启抽奖", "开启抽奖"),
            Item("立即开奖", "立即开奖"),
            Item("财务结账（私聊）", "结账"),
        ),
    ),
    Item(
        "AI与额度",
        children=(
            Item("进入AI咨询", "/chat"),
            Item("结束AI咨询", "/exit"),
            Item("我的套餐", "我的套餐"),
            Item("本群额度", "本群额度"),
            Item("本人确认本群扣费", "确认本群扣费"),
        ),
    ),
    Item(
        "插件设置",
        page="插件管理 → 旺商聊管理\n"
        "群管理 → 开通、规则（关键词回复、定时发言）\n"
        "AI服务 → 群聊与热群、套餐\n"
        "活动与财务 → 玩家福利抽奖、邀请奖励、邀请记账／结账\n"
        "https://astr.xhn.top/#/plugin-page/wangshangliao_moderation/management",
    ),
)


def overview(integrated=True):
    """Render the numbered root for welcome messages without creating authority.

    Args:
        integrated: Whether existing invitation and activity commands are available.

    Returns:
        Plain-text tree with the shared navigation controls.
    """
    items = QUICK_TREE if integrated else QUICK_TREE[:2] + QUICK_TREE[-1:]
    return (
        "【大海机器人 · 常用操作】\n"
        + "\n".join(
            f"{'└─' if n == len(items) else '├─'} {n}. {item.label}"
            for n, item in enumerate(items, 1)
        )
        + "\n可直接发送「规则」「禁言名单」「解禁 @成员」；无需先选菜单。"
        "\n配置请打开插件设置；私聊管理先发送「群列表」「选择群 1」。"
        "\n群主或群管理员拉我进群后，拉我的人在群里发送「开通」，不用先加好友。"
        "\nAI只回答问题，不会替你修改设置。" + FOOTER
    )


@dataclass
class Session:
    """Keep navigation separate from grants and authoritative command snapshots."""

    expires: float
    path: list = field(default_factory=list)
    pending: Item | None = None
    confirm: str = ""
    stamp: tuple = ()
    message: str = ""
    busy: bool = False
    closed: bool = False
    invitations: bool = False
    invitation_detail: bool = False
    preview_message: str = ""
    knowledge_snapshot: object = None
    unmute_group: bool = False
    whitelist_group: str = ""
    items: tuple = ()


class CommandMenu:
    """Navigate fixed commands without interpreting arbitrary text as operations."""

    def __init__(self, service):
        self.service = service
        self.sessions = {}

    def items_for(self, event, *, full=False):
        """Filter server-owned operations and independently granted private roles."""
        service = self.service
        identity = getattr(event, "actor_identity", None)
        support = getattr(service, "support", None)
        owners = getattr(service, "owner_support", None)
        support_editor = bool(
            event.is_private_chat()
            and identity
            and support
            and (support.editor(identity) or owners and owners.editor(identity))
        )
        finance = False
        guidance_trial = False
        rules_only = bool(
            service.tenants.get(
                (str(event.platform.account), event.get_group_id()), {}
            ).get("rules_only")
        )
        if event.is_private_chat() and identity and identity.verified_uid:
            from .consultation_access import consultation_access

            access = consultation_access(service, event)
            finance = bool(access["finance"])
            rules_only = access["rules_only"]
            guidance_trial = bool(access.get("guidance_trial"))

        def visible(items):
            result = []
            for item in items:
                capability = CAPABILITIES.get(item.command.split(" ", 1)[0], {})
                if (
                    item.command in SELECTION_COMMANDS
                    and not event.is_private_chat()
                    or capability.get("role") == "server"
                    or capability.get("role") == "finance"
                    and not finance
                    or item.command in SUPPORT_COMMANDS
                    and not support_editor
                    or rules_only
                    and (
                        item.label == TREE[8].label
                        or item.label == TREE[7].label
                        and not guidance_trial
                    )
                ):
                    continue
                source = item.children
                if rules_only and guidance_trial and item.label == TREE[7].label:
                    source = tuple(
                        child
                        for child in source
                        if child.command in {"/chat", "/exit", "我的套餐"}
                    )
                children = visible(source) if source else ()
                if item.children and not children:
                    continue
                result.append(replace(item, children=children))
            return tuple(result)

        if full:
            return visible(TREE if service.integrated else TREE[:5] + TREE[7:])
        items = QUICK_TREE if service.integrated else QUICK_TREE[:2] + QUICK_TREE[-1:]
        if rules_only:
            items = tuple(
                replace(
                    item,
                    children=tuple(
                        child
                        for child in item.children
                        if child.command in {"/chat", "/exit", "我的套餐"}
                    ),
                )
                if item.label == "AI与额度"
                else item
                for item in items
                if item.label != "AI与额度" or guidance_trial
            )
        return visible(items)

    def accepts(self, event, text):
        """Recognize exact menu input without interpreting ordinary chat as actions.

        Args:
            event: Transport-bound message for this conversation.
            text: Original user text, before menu selection normalization.

        Returns:
            Whether navigation must claim the message before any AI routing.
        """
        key = self.key(event)
        if key is None:
            return False
        command = self.service.recognize_command(text)
        if command in {"菜单", "帮助", "取消", "0"}:
            return True
        if text in {"/chat", "/exit"}:
            return False
        state = self.sessions.get(key)
        if state:
            if state.closed or not state.busy and state.expires <= time.monotonic():
                if command == "菜单选择":
                    return True
            else:
                if state.pending or state.confirm or state.invitations:
                    return True
                items = state.items or (
                    TREE if self.service.integrated else TREE[:5] + TREE[7:]
                )
                for index in state.path:
                    items = items[index].children
                if any(item.label == text for item in items):
                    return True
                return (
                    command == "菜单选择"
                    or not command
                    and (
                        event.is_private_chat()
                        or bool(re.fullmatch(r"[0-9][0-9.,，、；; \t]{1,127}", text))
                    )
                )
        return (
            any(item.label == text for item in TREE)
            and event.is_private_chat()
            and any(
                item.children and item.label == text
                for item in self.items_for(event, full=True)
            )
        )

    def key(self, event):
        """Bind state to the signed transport sender, account and conversation.

        Args:
            event: Native message with a transport-authenticated actor identity.

        Returns:
            Isolation key, or None when transport identity is unavailable.
        """
        actor = getattr(event, "actor_identity", None)
        origin = getattr(event, "unified_msg_origin", "")
        if not isinstance(actor, ActorIdentity) or not origin:
            return None
        if actor.account != str(event.platform.account) or actor.claimed_uid != str(
            event.get_sender_id()
        ):
            return None
        try:
            principal = actor.principal
        except (ProtocolError, UnicodeError):
            return None
        return (
            actor.account,
            principal,
            str(origin),
            str(event.get_group_id() or ""),
        )

    def cancel_whitelist(self, event):
        """Cancel optional whitelist state without breaking legacy menu owners."""
        whitelist = getattr(self.service, "member_whitelist", None)
        if whitelist is not None:
            whitelist.cancel(event)
        blacklist = getattr(self.service, "member_blacklist", None)
        if blacklist is not None:
            blacklist.cancel(event)

    def dismiss(self, event):
        """Invalidate pending navigation before cancellation feedback or awaits.

        Args:
            event: Authenticated exit or cancellation event.
        """
        self.cancel_whitelist(event)
        state = self.sessions.get(self.key(event))
        if state is not None:
            state.closed = True
            state.message = str(event.message_obj.message_id)
            state.path.clear()
            state.pending, state.confirm, state.preview_message = None, "", ""
            state.invitations = state.invitation_detail = False
            state.knowledge_snapshot = None
            state.unmute_group = False
            state.whitelist_group = ""

    def stamp(self, event):
        """Capture the selected group and current authorization generation.

        Args:
            event: Native event, never a model-provided request.

        Returns:
            Version tuple invalidating an unfinished mutation after scope changes.
        """
        adapter = event.platform
        selection = self.service.plugin.commands.selections.get(
            (
                id(adapter),
                adapter.account,
                event.get_sender_id(),
                event.unified_msg_origin,
            ),
            {},
        )
        group = event.get_group_id() or (
            selection.get("group", "")
            if selection.get("expires", 0) > time.monotonic()
            else ""
        )
        tenant = self.service.tenants.get((str(adapter.account), group), {})
        return (
            event.get_sender_id(),
            getattr(event, "policy_epoch", None),
            adapter.config.get("_business_revision"),
            group,
            tenant.get("version"),
            tenant.get("epoch"),
            tenant.get("native_role"),
            (str(adapter.account), group) in self.service.suspended_groups,
            bool(self.service.membership_receiving.get((str(adapter.account), group))),
            self.service.settings["version"],
            self.service.group_ai.configs.get((str(adapter.account), group), {}).get(
                "version", 0
            ),
            self.service.plans.confirm_stamp(
                str(adapter.account),
                group,
                event.actor_identity.principal,
                event.actor_identity.verified_uid,
            ),
        )

    def screen(self, state):
        """Render one level with stable numbers, never member or account IDs.

        Args:
            state: Current conversation navigation.

        Returns:
            One bounded text screen.
        """
        items = state.items or (
            TREE if self.service.integrated else TREE[:5] + TREE[7:]
        )
        labels = ["命令菜单"]
        for index in state.path:
            labels.append(items[index].label)
            items = items[index].children
        return (
            "【"
            + " > ".join(labels)
            + "】\n"
            + "\n".join(
                f"{'└─' if n == len(items) else '├─'} {n}. {item.label}"
                for n, item in enumerate(items, 1)
            )
            + (
                "\n直接发送：规则、禁言名单、解禁 @成员、参与玩家福利抽奖。"
                "\n设置规则和活动请打开「插件设置」。"
                if not state.path
                else ""
            )
            + FOOTER
        )

    async def handle(self, event, text):
        """Consume only an explicit menu session or its navigation controls.

        Args:
            event: Fresh native event; all actions still use existing grant checks.
            text: Original, single-line user text.

        Returns:
            Whether the event was consumed, including duplicate deliveries.
        """
        key = self.key(event)
        if key is None:
            return False
        accepted = self.accepts(event, text)
        now = time.monotonic()
        state = self.sessions.get(key)
        if state and not state.busy and state.expires <= now:
            self.sessions.pop(key, None)
            state = None
        command = self.service.recognize_command(text)
        entry = command in {"菜单", "帮助"}
        if text in {"/chat", "/exit"}:
            self.dismiss(event)
            self.sessions.pop(key, None)
            return False
        if not accepted:
            if state and command:
                self.sessions.pop(key, None)
            return False
        event.set_extra("_component_claimed", True)
        record_event(event, "menu", "claimed")
        event.set_extra("wsl_plain_command", True)
        from astrbot.core.platform.sources.wangshangliao.reply_recall import (
            schedule_command,
        )

        await schedule_command(event)
        event.set_extra("wsl_keep_reply", True)
        if event.get_extra("wsl_test_scope"):
            event.set_extra("wsl_command_result", True)
        message = str(event.message_obj.message_id)
        if state and (state.busy and text != "取消" or state.message == message):
            record_event(
                event,
                "menu",
                "duplicate" if state.message == message else "busy",
            )
            return True
        if text != "取消" and not self.service.admit_command(event):
            record_event(event, "menu", "cooldown")
            return True
        restart = state is None and not entry and text not in {"取消", "0"}
        if state is None:
            self.sessions = {
                k: s for k, s in self.sessions.items() if s.busy or s.expires > now
            }
            if len(self.sessions) >= 4096:
                await event.send(event.plain_result("菜单暂时繁忙，请稍后再试。"))
                return True
            state = Session(
                now + 600,
                items=self.items_for(
                    event,
                    full=event.is_private_chat()
                    and any(item.label == text for item in TREE),
                ),
            )
            self.sessions[key] = state
        if not state.pending and not state.confirm and not state.invitations:
            items = state.items or (
                TREE if self.service.integrated else TREE[:5] + TREE[7:]
            )
            for index in state.path:
                items = items[index].children
            matches = [i for i, item in enumerate(items, 1) if item.label == text]
            if len(matches) == 1:
                record_event(event, "menu", "label_selected")
                text = str(matches[0])
                restart = False
        state.message, state.expires = message, now + 600
        if text == "取消":
            in_flight = state.busy
            self.dismiss(event)
            if event.is_private_chat():
                event.set_extra("_wsl_menu_exit", True)
                await self.service.menu_command(event, "/exit")
            if self.service.admit_command(event):
                await event.send(
                    event.plain_result(
                        "已取消并退出菜单。发送「菜单」可重新打开。"
                        + (
                            "已经提交的操作不能撤销，请核对执行结果。"
                            if in_flight
                            else ""
                        )
                    )
                )
            return True
        if state.closed and not entry:
            await event.send(event.plain_result("菜单已退出，发送「菜单」重新打开。"))
            return True
        if entry:
            self.cancel_whitelist(event)
            state.items = self.items_for(event, full=text.strip("/ ") == "完整菜单")
            state.path.clear()
            state.pending, state.confirm, state.invitations = None, "", False
            state.preview_message = ""
            state.knowledge_snapshot = None
            state.unmute_group = False
            state.whitelist_group = ""
            state.closed = False
            reply = self.screen(state)
            if command == "帮助":
                reply += "\n群内操作本群；私聊先发送「群列表」「选择群 1」。"
            if event.is_private_chat():
                from .feature_image import GUIDE_LINKS

                reply += "\n\n" + GUIDE_LINKS
        elif restart:
            record_event(event, "menu", "session_reopened")
            reply = (
                "菜单已失效或尚未打开，已重新打开。请按本页选择；之前的操作未执行。\n"
                + self.screen(state)
            )
        elif text == "0":
            if state.pending or state.confirm or state.invitations:
                self.cancel_whitelist(event)
                state.pending, state.confirm, state.invitations = None, "", False
                state.preview_message = ""
                state.knowledge_snapshot = None
                state.unmute_group = False
                state.whitelist_group = ""
            elif state.path:
                state.path.pop()
            reply = self.screen(state)
        else:
            state.busy = True
            try:
                reply = await self.advance(event, state, text, message)
            finally:
                state.busy = False
            if state.closed and state.message != message:
                state.pending, state.confirm, state.preview_message = None, "", ""
                return True
        if isinstance(reply, str):
            items = state.items or (
                TREE if self.service.integrated else TREE[:5] + TREE[7:]
            )
            root = items[state.path[0]] if state.path else None
            knowledge = (
                root is not None
                and len(state.path) > 1
                and root.children[state.path[1]].label in {"我的知识库", "本群知识库"}
            )
            if (
                root is not None
                and root.label in {TREE[7].label, TREE[8].label}
                and not knowledge
                and "【AI套餐说明】" not in reply
            ):
                if root.label == TREE[7].label:
                    reply += "\n\n" + (
                        await self.service.plan_command(event, "我的套餐")
                        if event.is_private_chat()
                        else self.service.plans.describe()
                    )
                elif self.service.management.allowed(
                    event, event.get_group_id() or self.stamp(event)[3]
                ):
                    reply += "\n\n" + str(
                        await self.service.manager_command(event, "本群额度")
                    )
                else:
                    reply += "\n\n" + self.service.plans.describe()
            result = event.plain_result(redact_reply(reply))
        elif reply is not None:
            result = event.chain_result(reply.chain)
        else:
            return True
        try:
            await event.send(result)
        except Exception:
            # A transport exception cannot prove the reply was not delivered.
            record_event(
                event,
                "menu_reply",
                "unknown",
                failed=True,
                error="result_unknown",
                aggregate=False,
            )
            raise
        outcome = event.get_extra("_wsl_native_reply_result")
        outcome = (
            outcome
            if outcome in {"accepted", "verified", "rejected", "unknown", "not_sent"}
            else "returned"
        )
        record_event(
            event,
            "menu_reply",
            outcome,
            failed=outcome in {"rejected", "unknown"},
            error="result_unknown" if outcome == "unknown" else "",
            aggregate=False,
        )
        return True

    async def advance(self, event, state, text, message):
        """Validate a single input, then delegate to the fixed command dispatcher.

        Args:
            event: Current authenticated message, preserving real mention metadata.
            state: Short-lived menu state for this exact conversation.
            text: One user selection or prompted argument.
            message: Fresh native message identifier.

        Returns:
            A menu screen or existing native command result.
        """
        command = ""
        if state.confirm:
            if text != "1":
                return "尚未执行。回复 1 确认；0 放弃并返回；取消 退出。"
            if state.stamp != self.stamp(event) or message == state.preview_message:
                self.cancel_whitelist(event)
                state.confirm = ""
                state.preview_message = ""
                return "群设置或你的权限已变化，请重新选择操作。\n" + self.screen(state)
            command, state.confirm = state.confirm, ""
        elif state.invitations:
            if not re.fullmatch(r"(?:[1-9][0-9]?|100)", text):
                return "请回复邀请列表中的群序号；0 返回；取消 退出。"
            command = "我的邀请 " + text
            if state.invitation_detail:
                command += " 明细"
        elif state.pending:
            item = state.pending
            if state.stamp != self.stamp(event):
                self.cancel_whitelist(event)
                state.pending = None
                state.unmute_group = False
                state.whitelist_group = ""
                return "群设置或你的权限已变化，请重新选择操作。\n" + self.screen(state)
            if item.command in MEMBER_COMMANDS and text == "99":
                result = await self.service.menu_command(event, "下一页")
                return str(result) + "\n" + item.prompt + "\n99. 下一页成员" + FOOTER
            if item.command in {"参与上限", "抽奖邀请门槛"} and text == "不限":
                text = "0"
            command = item.command + " " + text
            valid = (
                re.fullmatch(r"认领 [0-9]{1,20} [A-Za-z0-9_-]{8,128}", command)
                if item.command == "认领"
                else item.command in KNOWLEDGE_COMMANDS or recognize_command(command)
            )
            if (
                not valid
                or len(text) > 4096
                or any(c in text for c in ("\r", "\u2028", "\u2029", "\x00"))
                or "\n" in text
                and item.command not in KNOWLEDGE_COMMANDS
            ):
                return "输入格式不正确，没有执行。\n" + item.prompt + FOOTER
            state.pending = None
        else:
            items = state.items or (
                TREE if self.service.integrated else TREE[:5] + TREE[7:]
            )
            for index in state.path:
                items = items[index].children
            if not re.fullmatch(r"[1-9][0-9]{0,2}", text) or int(text) > len(items):
                record_event(event, "menu", "invalid_selection")
                return "请按本页的数字选择，也可回复完整栏目名。\n" + self.screen(state)
            index = int(text) - 1
            item = items[index]
            if item.page:
                return item.page + "\n" + FOOTER
            if item.children:
                if item.label == "我的知识库" and not event.is_private_chat():
                    return (
                        "个人知识只在私聊中编辑，请私聊我发送「菜单」，选择8，再选择5。"
                    )
                if (
                    item.label in {"我的知识库", "本群知识库"}
                    and event.is_private_chat()
                ):
                    key = self.key(event)
                    await self.service.end_chat(
                        key[0],
                        key[1],
                        event=event,
                    )
                state.path.append(index)
                return self.screen(state)
            if item.command == "认领" and not event.is_private_chat():
                return "开通码请私聊机器人使用，不要发到群里。\n" + self.screen(state)
            if (
                event.is_private_chat()
                and item.command
                not in PERSONAL_KNOWLEDGE_COMMANDS
                | SUPPORT_COMMANDS
                | SELECTION_COMMANDS
                | {
                    "开通",
                    "认领",
                    "群列表",
                    "选择群",
                    "我的权限",
                    "我的邀请",
                    "我的邀请明细",
                    "/chat",
                    "/exit",
                    "我的套餐",
                    "我的额度",
                    "套餐说明",
                }
            ):
                await self.service.verify_actor(event)
                group = self.stamp(event)[3]
                if group and item.command != "解禁":
                    if not self.service.management.allowed(event, group):
                        return "无权限"
                    require_group_admin(event.platform.config, group)
            if item.prompt:
                prefix = ""
                if (
                    event.is_private_chat()
                    and item.command in WHITELIST_COMMANDS | BLACKLIST_COMMANDS
                    and not self.stamp(event)[3]
                ):
                    prefix = await self.service.menu_command(event, "群列表")
                    if isinstance(prefix, str) and prefix.startswith("已启用群"):
                        state.pending = Item(
                            "选择白名单群", "选择群", "请选择本人授权群的序号"
                        )
                        state.whitelist_group = item.command
                        state.stamp = self.stamp(event)
                        return prefix + "\n" + state.pending.prompt + FOOTER
                    return str(prefix) + "\n" + self.screen(state)
                if item.command == "解禁" and event.is_private_chat():
                    prefix = await self.service.menu_command(event, "群列表")
                    if (
                        not self.stamp(event)[3]
                        and isinstance(prefix, str)
                        and prefix.startswith("已启用群")
                    ):
                        state.pending = Item(
                            "选择解禁群", "选择群", "请选择上面你要管理的群序号"
                        )
                        state.unmute_group = True
                        state.stamp = self.stamp(event)
                        return prefix + "\n" + state.pending.prompt + FOOTER
                    return str(prefix) + "\n" + self.screen(state)
                if item.command in KNOWLEDGE_COMMANDS:
                    state.knowledge_snapshot = await self.service.knowledge.capture(
                        event, item.command
                    )
                prerequisite = (
                    "群列表"
                    if item.command == "选择群"
                    else "客服列表"
                    if item.command == "选择客服"
                    else "禁言名单"
                    if item.command == "解禁"
                    else "成员列表"
                    if item.command in MEMBER_COMMANDS
                    else "规则"
                    if item.command
                    in {
                        "删除关键词",
                        "修改关键词",
                        "修改规则",
                        "启用规则",
                        "停用规则",
                        "删除规则",
                    }
                    else "我的知识"
                    if item.command in {"修改我的知识", "删除我的知识", "查看我的知识"}
                    else "本群知识"
                    if item.command in {"修改群知识", "删除群知识", "查看群知识"}
                    else ""
                )
                if prerequisite:
                    prefix = await self.service.menu_command(event, prerequisite)
                    if not isinstance(prefix, str):
                        return prefix
                    if prefix == "无权限":
                        return prefix + "\n" + self.screen(state)
                    if item.command == "解禁" and not prefix.startswith(
                        "【当前禁言名单】"
                    ):
                        return prefix + "\n" + self.screen(state)
                state.pending, state.stamp = item, self.stamp(event)
                return (
                    (prefix + "\n" if prefix else "")
                    + item.prompt
                    + ("\n99. 下一页成员" if item.command in MEMBER_COMMANDS else "")
                    + FOOTER
                )
            command = item.command
        action = command.split(" ", 1)[0]
        # Menu confirmations are not a replacement for backend grant/roster checks.
        if (
            action not in READ_COMMANDS | {"/chat", "/exit"}
            and not state.preview_message
        ):
            if action in KNOWLEDGE_COMMANDS and state.knowledge_snapshot is None:
                state.knowledge_snapshot = await self.service.knowledge.capture(
                    event, command
                )
            state.confirm, state.stamp = command, self.stamp(event)
            state.preview_message = message
            return (
                "即将执行："
                + command
                + "\n├─ 1. 确认执行\n└─ 0. 放弃并返回\n取消 退出菜单"
            )
        state.preview_message = ""
        if command == "确认本群扣费":
            event.set_extra("_wsl_billing_consent_stamp", state.stamp)
        if action in KNOWLEDGE_COMMANDS:
            event.set_extra("_wsl_knowledge_context", (state.knowledge_snapshot, state))
        try:
            result = await self.service.menu_command(event, command)
        except BaseException:
            state.pending, state.confirm, state.invitations = None, "", False
            state.unmute_group = False
            state.whitelist_group = ""
            raise
        finally:
            if action in KNOWLEDGE_COMMANDS:
                event.set_extra("_wsl_knowledge_context", None)
                state.knowledge_snapshot = None
        if command in {"/chat", "/exit"}:
            state.closed = True
            return result
        if isinstance(result, str):
            if action == "选择群" and state.whitelist_group:
                next_command, state.whitelist_group = state.whitelist_group, ""
                if result.startswith("当前已选群："):
                    state.pending = Item(
                        next_command,
                        next_command,
                        "请输入整分钟时长，如60分钟、24小时或29天；换算后须为1—41760分钟"
                        if next_command == "黑名单禁言时长"
                        else "请输入对方在本群的名称",
                    )
                    state.stamp = self.stamp(event)
                    return result + "\n" + state.pending.prompt + FOOTER
                return result + "\n" + self.screen(state)
            if action in {"添加白名单", "移除白名单"} and (
                self.service.member_whitelist.chat_search.get(self.key(event))
            ):
                state.pending = Item(
                    "选择本群成员", "选择白名单", "请选择上面本次搜索结果中的成员序号"
                )
                state.stamp = self.stamp(event)
                return result + "\n" + state.pending.prompt + FOOTER
            if action in {"添加黑名单", "移除黑名单"} and (
                self.service.member_blacklist.chat_search.get(self.key(event))
            ):
                state.pending = Item(
                    "选择黑名单成员", "选择黑名单", "请选择本次搜索结果中的成员序号"
                )
                state.stamp = self.stamp(event)
                return result + "\n" + state.pending.prompt + FOOTER
            if action == "选择群" and state.unmute_group:
                state.unmute_group = False
                if not result.startswith("当前已选群："):
                    return result + "\n" + self.screen(state)
                muted = await self.service.menu_command(event, "禁言名单")
                if not isinstance(muted, str):
                    return muted
                if not muted.startswith("【当前禁言名单】"):
                    return result + "\n" + muted + "\n" + self.screen(state)
                state.pending = Item(
                    "解除成员禁言", "解禁", "请选择上面被禁言成员的序号"
                )
                state.stamp = self.stamp(event)
                return (
                    result
                    + "\n"
                    + muted
                    + "\n"
                    + state.pending.prompt
                    + "\n99. 下一页成员"
                    + FOOTER
                )
            expected = {
                "踢出": "确认踢出",
                "添加管理员": "确认授权",
                "移除管理员": "确认授权",
                "定时禁言": "确认定时",
                "恢复定时": "确认定时",
                "选择白名单": "确认白名单",
                "选择黑名单": "确认黑名单",
                "黑名单禁言时长": "确认黑名单",
            }.get(action)
            matches = (
                list(re.finditer(expected + r" [A-Za-z0-9_-]{8,128}(?=\s|$)", result))
                if expected
                else []
            )
            confirmation = matches[-1] if matches else None
            if confirmation:
                state.confirm = confirmation[0]
                state.stamp, state.preview_message = self.stamp(event), message
                return (
                    result[: confirmation.start()]
                    + "\n├─ 1. 确认执行\n└─ 0. 放弃并返回\n取消 退出菜单"
                )
            state.invitations = event.is_private_chat() and action in {
                "我的邀请",
                "我的邀请明细",
            }
            state.invitation_detail = action == "我的邀请明细" or " 明细" in command
            return result + (
                FOOTER if state.invitations else "\n\n" + self.screen(state)
            )
        if result is not None:
            result.chain.append(Plain("\n\n" + self.screen(state)))
        return result
