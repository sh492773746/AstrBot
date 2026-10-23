"""Trusted content rules with durable evidence and no retrospective sanctions."""

import asyncio
import copy
import json
import re
import sqlite3
import time
import unicodedata

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .review_store import review_store

CONTEXTUAL_KEYWORDS = frozenset(
    {
        "p图",
        "8\u20e38\u20e3",
        "新项目",
        "蛋糕",
        ".cc",
        ".vip",
        ".pw",
        "宋",
        "挣钱",
        "代发",
        "兼职",
        "58",
        "小额",
    }
)


def classify(text: str, *, name: str = "") -> str:
    """Return a high-confidence category, or an empty string for review-only text.

    Args:
        text: Untrusted group message, never an instruction.
        name: Native sender display name, used only as solicitation evidence.

    Returns:
        Fixed rule identifier without contact details.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    if re.search(
        r"举报|有人发|有人私聊|骗子发|不要加|别加|勿加|不要点|别点|是不是诈骗|是骗子吗|"
        r"谨防|警惕|防骗|反诈|骗局|骗人的|不要相信|别信|勿信",
        text,
    ) or re.match(
        r"\s*(?:引用|转发[:：]|[“「])",
        text,
    ):
        return ""
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(
        r"(?:(?:名字|名紫)(?:就是|是)导航(?:网)?|看(?:名字|名紫)(?:里的?)?导航(?:网)?)[!！。]*",
        compact,
    ) and re.search(
        r"(?<![a-z0-9_.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)"
        r"{1,8}(?:vip|top|com|cn|ws|net|cc)\b",
        unicodedata.normalize("NFKC", name).casefold(),
    ):
        return "external_promotion"
    # Native administrator recalls are review evidence, not automatic labels.
    # These combinations require solicitation, not a lone name, number or topic.
    invitation = bool(re.search(r"加我|联系我|私聊我|私信我|来找我|想做的", compact))
    native_contact = bool(
        re.search(
            r"(?:进(?:群)?|加群|群号|旺旺|旺商聊|加q|qq)[:：号]*"
            r"[^\w]{0,4}[1-9]\d{4,11}(?!\d)",
            compact,
        )
    )
    if (
        re.fullmatch(
            r"(?:新用户)?注册(?:就|即|即可)?(?:送|赠送)"
            r"(?:(?:体验金|彩金)\d{1,5}(?:\.\d{1,2})?(?:元|u)?|"
            r"\d{1,5}(?:\.\d{1,2})?(?:元|u)?(?:体验金|彩金))[!！。]*",
            compact,
        )
        or re.fullmatch(
            r"(?:做单|接单)(?:加我?|\+)[:：]?"
            r"(?:\([1-9]\d{4,11}\)|[1-9]\d{4,11})[!！。]*",
            compact,
        )
        or re.fullmatch(
            r"微信附近的人(?:每|一|1)(?:个)?小时(?:给|返)?\d{1,5}"
            r"(?:元|块|u)?(?:秒结(?:账)?|日结)?[!！。]*",
            compact,
        )
    ):
        return "external_promotion"
    # A destination alone is insufficient; preserve each solicitation combination.
    group_contact = bool(
        re.search(
            r"(?:超级群|直播群|项目群|群号)[:：号]*"
            r"[^\w]{0,4}[1-9]\d{4,11}(?!\d)",
            compact,
        )
    )
    if group_contact and (
        re.search(
            r"(?:进群|加群)(?:后)?联系管理(?:员)?[,，:：]*(?:做项目|做单)",
            compact,
        )
        or re.search(r"广告费[:：]?\d{1,5}(?!\d)|(?<!\d)\d{1,5}广告费", compact)
        and re.search(
            r"(?:分享|转发)(?:文案)?(?:到|至|给)\d{1,3}个(?:千人|百人)?群",
            compact,
        )
    ):
        return "external_promotion"
    if (
        "赔率" in compact
        and re.search(r"需要(?:的)?滴我|私聊我|联系我|加我", compact)
        and re.search(
            r"(?:嘉汪|嘉旺|加旺|旺旺|旺商聊)[:：号]*"
            r"[^\w]{0,4}[1-9]\d{4,11}(?!\d)",
            compact,
        )
    ):
        return "external_promotion"
    if re.fullmatch(r"(?:进群|加群|入群)看p[!！。]*", compact):
        return "adult_promotion"
    if (
        re.search(r"(?:免费|免废|免菲)(?:p图|劈图|劈涂|批图)上分", compact)
        or re.fullmatch(
            r"(?:找我|联系我|私聊我)?(?:免费|免废|免菲)上分"
            r"(?:来找我|加我|联系我|私聊我)?[!！。]*",
            compact,
        )
        or re.search(
            r"(?:没米|没钱|缺米|缺钱)上分(?:的)?(?:老哥|兄弟|朋友)?"
            r"(?:就)?(?:来找我|加我|联系我|私聊我|私信我|d我|滴我)",
            compact,
        )
        or re.fullmatch(
            r"有(?:微信|支付宝)扫码秒存渠道的(?:老板|朋友)?"
            r"(?:联系我|私聊我|加我)[:：]?[1-9]\d{4,11}[!！。]*",
            compact,
        )
        or re.fullmatch(r"做项目的来秒结[!！。]*", compact)
        or re.fullmatch(
            r"(?:代办)?洗(?:钱|💰)(?:的)?(?:来|联系我|私聊我)[!！。]*",
            compact,
        )
        or re.fullmatch(r"(?:加我|添加我|私聊我)(?:就)?送\d{1,6}[!！。]*", compact)
    ):
        return "external_promotion"
    if re.search(r"网红.{0,6}(?:黑料|黑历史|视频)", compact) and (
        invitation
        or native_contact
        or re.search(r"(?:想看|看看|全部).{0,12}(?:在这里|给你发)", compact)
    ):
        return "external_promotion"
    if (
        re.search(r"(?:来几个|招募|招人).{0,20}(?:挂壁仔|做项目)", compact)
        and re.search(r"支持担保|担保交易", compact)
        and re.search(r"没钱|一单|\d{1,3}(?:-\d{1,3})?分钟", compact)
    ):
        return "external_promotion"
    if (
        (
            re.search(r"所有人|来几个|收款账户|收款账号", compact)
            and re.search(r"支付宝|微信|银行卡", compact)
            and re.search(r"(?:能|可以)收款的截图(?:加我|联系我)", compact)
        )
        or (
            invitation
            and re.search(r"(?:有|收|租)(?:快手|抖音|小红书)(?:账号|号)", compact)
            and re.search(r"\d{1,3}小时(?:给|返|结算?)\d{1,6}", compact)
        )
        or (
            invitation
            and "微信附近的人" in compact
            and re.search(r"(?:每|一|1)(?:个)?小时\d{1,5}", compact)
        )
    ):
        return "external_promotion"
    if (
        re.search(r"(?<!\d)[1-9]\d{4,11}(?!\d)免费领取付费预测", compact)
        or native_contact
        and re.search(r"拿\d{1,5}(?:\.\d{1,2})?的预测", compact)
    ):
        return "external_promotion"
    destination = (
        bool(
            re.search(
                r"https?://[^\s]+|(?<![a-z0-9_.-])(?:[a-z0-9]"
                r"(?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,8}(?:vip|top|com|cn|ws|net|cc)\b|"
                r"(?:微信|薇信|v信|vx|qq|扣扣|接单|群号|旺群|至尊群)[:：号是为]*[a-z0-9_-]{5,}|"
                r"(?:加我|联系我|私聊我).{0,12}(?:1[3-9]\d{9}|[a-z][a-z0-9_-]{5,})",
                compact,
            )
        )
        or native_contact
    )
    if not destination:
        return ""
    if (
        re.search(r"推广扶持|合营招商|招收代理|发展下级", compact)
        and re.search(r"下注|返水|彩金|充值.{0,12}(?:返|送)", compact)
        and re.search(r"联系|私聊|私信|加我", compact)
    ):
        return "external_promotion"
    if re.search(r"3p|大秀|调教|内🐍", compact) and re.search(
        r"直播|群|传媒|参加|分享|进[1-9]\d{4,11}(?!\d)", compact
    ):
        return "adult_promotion"
    if re.search(
        r"注册链接|充值.{0,12}(?:返|送)|首次充值|转线|下级|包下分|团队担保|开业福利|分享\d+个群|转发.{0,8}(?:朋友圈|红包)|接单|加我|联系我|私聊我",
        compact,
    ):
        return "external_promotion"
    return ""


async def contextual_category(adapter, group: str, mid: str, payload: dict) -> str:
    """Correlate split contact ads using bounded, live native inbox evidence.

    Args:
        adapter: Scoped native adapter with its durable inbound ledger.
        group: Current business group, never a transport team identifier.
        mid: Current upstream message identifier.
        payload: Current authenticated envelope.

    Returns:
        An ad category only for a fresh numeric contact with same-sender evidence.
    """
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision

    text = unicodedata.normalize("NFKC", payload.get("text", ""))
    number = "".join(
        c for c in text if not c.isspace() and unicodedata.category(c) != "Cf"
    )
    sender = str(payload.get("sender", ""))
    revision = payload.get("_open_revision")
    policy = copy.deepcopy(adapter.config.get("moderation", {}))
    db = getattr(getattr(adapter, "ledger", None), "db", None)
    if (
        not re.fullmatch(r"[1-9][0-9]{4,11}", number)
        or not sender
        or not isinstance(revision, str)
        or not revision
        or revision != group_revision(adapter.config, group)
        or db is None
    ):
        return ""
    account = adapter.account
    try:
        current = await db.execute_fetchall(
            "SELECT payload,received_at,sender,state FROM inbox "
            "WHERE account=? AND team=? AND message=?",
            (account, group, str(mid)),
        )
        if len(current) != 1:
            return ""
        saved, received, stored_sender, state = current[0]
        now = time.time()
        if (
            stored_sender != sender
            or state not in {"pending", "processing", "processed"}
            or not 0 <= now - received <= 15
        ):
            return ""
        original = json.loads(saved)
        if any(
            original.get(key) != payload.get(key)
            for key in (
                "text",
                "sender",
                "recall_route",
                "_open_revision",
                "_test_rule_window",
            )
        ):
            return ""
        current_stamp = float(original.get("recall_route", {}).get("time") or 0)
        if current_stamp > 100000000000:
            current_stamp /= 1000
        since = policy.get("content_rules_since", 0)
        if not 0 <= received - current_stamp <= 15 or current_stamp < since:
            return ""
        rows = await db.execute_fetchall(
            "SELECT message,payload,received_at,sender,state FROM inbox "
            "WHERE account=? AND team=? AND received_at>=? AND received_at<=? "
            "ORDER BY received_at DESC LIMIT 64",
            (account, group, received - 120, received),
        )
        messages = list(dict.fromkeys([str(mid), *(str(row[0]) for row in rows)]))
        marks = ",".join("?" for _ in messages)
        tables = {
            row[0]
            for row in await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('observed_recalls','outbox')"
            )
        }
        recalled = set()
        if "observed_recalls" in tables:
            recalled = {
                (str(message), str(client), str(author))
                for message, client, author in await db.execute_fetchall(
                    "SELECT DISTINCT message_id,client,author FROM observed_recalls "
                    "WHERE account=? AND group_id=? AND received_at>=? "
                    f"AND message_id IN ({marks}) LIMIT 256",
                    (account, group, received - 120, *messages),
                )
                if client and author
            }
        unavailable = set()
        if "outbox" in tables:
            unavailable = {
                row[0]
                for row in await db.execute_fetchall(
                    f"SELECT key FROM outbox WHERE key IN ({marks}) "
                    "AND state IN ('accepted','verified','unknown','sending')",
                    tuple(
                        f"recall/{account}/{group}/{message}" for message in messages
                    ),
                )
            }
        route = original.get("recall_route", {})
        if (
            f"recall/{account}/{group}/{mid}" in unavailable
            or (str(mid), str(route.get("client", "")), str(route.get("peer", "")))
            in recalled
        ):
            return ""
        ad = False
        cues = []
        repeated = []
        for prior_mid, raw, observed, member, row_state in rows:
            if member != sender or row_state not in {
                "pending",
                "processing",
                "processed",
            }:
                continue
            item = json.loads(raw)
            route = item.get("recall_route", {})
            if (
                f"recall/{account}/{group}/{prior_mid}" in unavailable
                or (
                    str(prior_mid),
                    str(route.get("client", "")),
                    str(route.get("peer", "")),
                )
                in recalled
            ):
                continue
            stamp = float(item.get("recall_route", {}).get("time") or 0)
            if stamp > 100000000000:
                stamp /= 1000
            if (
                item.get("sender") != sender
                or item.get("_open_revision") != revision
                or item.get("_test_rule_window") != payload.get("_test_rule_window")
                or not 0 <= observed - stamp <= 15
                or stamp < since
                or stamp > current_stamp
            ):
                continue
            prior = unicodedata.normalize("NFKC", item.get("text", ""))
            prior = "".join(
                c for c in prior if not c.isspace() and unicodedata.category(c) != "Cf"
            )
            ad = ad or bool(classify(prior))
            if re.fullmatch(r"(?:开播|直播开始|开播了)[!！。]*", prior):
                cues.append(stamp)
            if prior == number:
                repeated.append(stamp)
        if (
            adapter.account != account
            or revision != group_revision(adapter.config, group)
            or policy != adapter.config.get("moderation", {})
        ):
            return ""
        if ad or (cues and sum(t >= min(cues) for t in repeated) >= 2):
            return "external_promotion"
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        # Incomplete evidence must not turn a bare number into a violation.
        return ""
    return ""


def keyword_category(text: str, policy: dict) -> str:
    """Match configured terms without treating broad words as standalone ads.

    Args:
        text: Untrusted current group message.
        policy: Configured literal mute and high-risk terms.

    Returns:
        Keyword category, or an empty string without sufficient ad evidence.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(
        c
        for c in text
        if unicodedata.category(c) != "Cf" and not c.isspace() and c != "\ufe0f"
    )
    if re.search(
        r"举报|有人发|有人私聊|骗子发|不要加|别加|勿加|不要点|别点|是不是诈骗|是骗子吗|"
        r"谨防|警惕|防骗|反诈|骗局|骗人的|不要相信|别信|勿信|引用|转发[：:]|[“”「」]",
        text,
    ):
        return ""
    for field, category in (
        ("mute_keywords", "mute_keyword"),
        ("kick_keywords", "kick_keyword"),
    ):
        words = policy.get(
            field, policy.get("keywords", []) if field == "mute_keywords" else []
        )
        for word in words:
            term = "".join(
                c
                for c in unicodedata.normalize("NFKC", word).casefold()
                if unicodedata.category(c) != "Cf" and not c.isspace() and c != "\ufe0f"
            ).strip()
            if not term or term not in text:
                continue
            if term == "58" and not re.search(r"(?<![\d.])58(?![\d.])", text):
                continue
            if term in {".cc", ".vip", ".pw"} and not re.search(
                r"(?<![a-z0-9_-])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                + re.escape(term)
                + r"(?=$|[^a-z0-9_.-])",
                text,
            ):
                continue
            if term in CONTEXTUAL_KEYWORDS and not (
                re.search(
                    r"上分|下分|返佣|返点|注册送|注册就送|包赔|送彩金|博彩|收fc", text
                )
                or (
                    re.search(r"新项目|挣钱|兼职|小额|日结|代发|p图|福利|蛋糕", text)
                    and re.search(
                        r"加我|联系我|私聊我|私信我|添加(?:微信|好友)|vx[:：号]|"
                        r"微信[:：号]|qq[:：号]|https?://|"
                        r"(?<![a-z0-9_-])(?:[a-z0-9]"
                        r"(?:[a-z0-9-]{0,61}[a-z0-9])?\.)"
                        r"{1,8}(?:cc|vip|pw|com|cn|net|top)\b",
                        text,
                    )
                )
            ):
                continue
            return category
    from .custom_rules import normalize

    for rule in policy.get("custom_rules", []):
        if rule["enabled"] and all(normalize(term) in text for term in rule["terms"]):
            return "external_promotion"
    return ""


async def enforce(
    adapter,
    group: str,
    mid: str,
    payload: dict,
    *,
    assessment=None,
    execution_check=None,
) -> bool:
    """Recall fresh violations and apply explicitly authorized mute escalation.

    Args:
        adapter: Authenticated native adapter.
        group: Enabled group ID.
        mid: Stable upstream message ID.
        payload: Authenticated content kind, text and recall routing evidence.
        assessment: Optional validated semantic decision for this exact message.
        execution_check: Optional caller-owned authority check, repeated before effects.

    Returns:
        Whether a high-confidence candidate was consumed.
    """
    from .action_receipts import ModerationActions
    from .member_exemptions import is_exempt

    adapter = ModerationActions(adapter, group)
    if await asyncio.to_thread(
        is_exempt,
        adapter.config["id"],
        str(adapter.account),
        group,
        str(payload.get("sender", "")),
        payload.get("recall_route"),
    ):
        return False
    policy = adapter.config.get("moderation", {})
    if policy.get("advertising_enabled") is False and (
        not assessment or assessment.get("category") != "message_flood"
    ):
        return False
    category = (
        assessment["category"]
        if assessment
        and assessment.get("decision") == "violation"
        and assessment.get("message_id") == str(mid)
        and assessment.get("category")
        not in {"prohibited_image", "prohibited_video", "prohibited_forward"}
        else (
            keyword_category(
                payload.get("text", ""),
                {
                    **policy,
                    "custom_rules": adapter.config.get("_open_groups", {})
                    .get(group, {})
                    .get("custom_rules", []),
                },
            )
            or classify(payload.get("text", ""), name=payload.get("name", ""))
        )
    )
    if not category:
        category = await contextual_category(adapter, group, mid, payload)
    if not category:
        return False
    policy = adapter.config["moderation"]
    frozen_policy = copy.deepcopy(policy)
    account = adapter.account
    from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
    from astrbot.core.platform.sources.wangshangliao.test_rules import rule_target

    revision = group_revision(adapter.config, group)
    sender = str(payload.get("sender", ""))
    protected_sender = is_managed_account(sender) or bool(
        payload.get("_test_rule_window")
    )
    original_window = getattr(adapter, "test_window", None)

    def authorize():
        if execution_check:
            execution_check()
        if is_exempt(
            adapter.config["id"],
            str(adapter.account),
            group,
            sender,
            payload.get("recall_route"),
        ):
            raise ProtocolError("moderation_member_exempt")
        authorize_action(adapter.config, group, "recall")
        authorize_action(adapter.config, group, "mute")
        if (
            not frozen_policy.get("automation_enabled")
            or adapter.config.get("moderation", {}) != frozen_policy
            or group_revision(adapter.config, group) != revision
        ):
            raise ProtocolError("content_policy_changed")
        if protected_sender and (
            getattr(adapter, "test_window", None) is not original_window
            or not rule_target(adapter, group, sender, mid)
            or not payload.get("_test_rule_window")
            or payload["_test_rule_window"]
            != getattr(original_window, "_rule_generation", "")
        ):
            raise ProtocolError("moderation_identity")

    guard = account_guard(adapter, authorize)
    since = policy.get("content_rules_since", 0)
    now = time.time()
    # Upstream envelope time is preferable to the often-zero custom body time.
    try:
        stamp = float(payload.get("recall_route", {}).get("time") or 0)
        if stamp > 100000000000:
            stamp /= 1000
    except (ValueError, TypeError):
        stamp = 0
    root = instance_dir(adapter.config["id"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / "moderation.sqlite3"
    store = review_store(adapter, path)
    if execution_check:
        execution_check()
    if not await asyncio.to_thread(
        store.observe, account, group, mid, sender, category, now, since
    ):
        return True
    status = "review_only"
    warning = "none"
    try:
        if not since <= stamp <= now + 60 or now - stamp > 300:
            status = "historical_or_unknown_time"
            return True
        roster = await adapter.get_moderation_members(group)
        guard()
        matches = [
            m
            for m in roster.get("groupMemberInfo", [])
            if str(m.get("userId")) == sender
        ]
        if not roster.get("complete") or len(matches) != 1:
            status = "identity_unverified"
            return True
        member = matches[0]
        if sender == account or member.get("groupRole") in {
            "GROUP_ROLE_OWNER",
            "GROUP_ROLE_ADMIN",
        }:
            status = "administrator_exempt"
            return False
        if member.get("groupRole") != "GROUP_ROLE_MEMBER" or str(
            member.get("nimId")
        ) != adapter.members.get(group, {}).get(sender):
            status = "identity_unverified"
            return True
        authorize_action(adapter.config, group, "recall")
        authorize_action(adapter.config, group, "mute")
        if category == "message_flood":
            from .flood import enforce as enforce_flood

            result = await enforce_flood(
                adapter, group, str(mid), payload, store=store, guard=guard
            )
            status = (
                "muted"
                if result.get("mute_status") in {"accepted", "verified"}
                else result.get("mute_status", "review_only")
            )
            warning = result.get("recall_status", "none")
            return True
        if policy.get("progressive_mute"):
            from astrbot.core.platform.sources.wangshangliao.progressive import (
                enforce as progressive_enforce,
            )

            result = await progressive_enforce(
                adapter, group, sender, str(mid), execution_check=guard
            )
            warning = result.get("recall_status", "unknown")
            # Notification failure must not erase an accepted original sanction.
            status = (
                (
                    "recalled"
                    if result.get("action") == "recall"
                    else "kicked"
                    if result.get("action") == "kick"
                    else "muted"
                )
                if result.get("status") in {"accepted", "verified"}
                else "unknown"
            )
            if (
                "_open_groups" in adapter.config
                and result.get("warning_number")
                and warning in {"accepted", "verified"}
            ):
                guard()
                nick = (
                    str(
                        member.get("userNick")
                        or member.get("name")
                        or member.get("userName")
                        or sender
                    ).strip()[:100]
                    or sender
                )
                await adapter.execute_action(
                    ActionRequest.bind(
                        adapter,
                        f"open-warning/{account}/{group}/{mid}",
                        "warning",
                        int(group),
                        member=int(sender),
                        expected_nim=str(member["nimId"]),
                        mention_name=nick,
                        text=f"@{nick} 第{result['warning_number']}次广告违规，消息已撤回。"
                        "前三次撤回并警告，第4次起将禁言，请勿继续发布广告。",
                    ),
                    authorize=guard,
                )
            return True
        row, prior = await asyncio.to_thread(
            store.claim, account, group, mid, sender, since, now
        )
        guard()
        if prior is None:
            status, warning = row
            return True
        status = "unknown"
        if not adapter.config.get("moderation", {}).get("automation_enabled"):
            status = "disabled"
            return True
        try:
            await adapter.execute_action(
                ActionRequest.bind(
                    adapter,
                    f"recall/{account}/{group}/{mid}",
                    "recall",
                    int(group),
                    member=int(sender),
                    expected_nim=str(member["nimId"]),
                    message=mid,
                ),
                authorize=guard,
            )
        except Exception:
            adapter.diagnostics.emit("recall", "failed_or_unknown", mid, failed=True)
        guard()
        if prior[0] and prior[1]:
            if policy.get("auto_kick", {}).get(group) is True:
                from astrbot.core.platform.sources.wangshangliao.automatic import (
                    mute_and_escalate,
                )

                result = await mute_and_escalate(
                    adapter,
                    f"content/{group}/{mid}",
                    group,
                    sender,
                    mid,
                    execution_check=guard,
                )
            else:
                result = await adapter.execute_action(
                    ActionRequest.bind(
                        adapter,
                        f"content/{group}/{mid}",
                        "mute",
                        int(group),
                        member=int(sender),
                    ),
                    authorize=guard,
                )
            status = (
                ("kicked" if result.get("action") == "kick" else "muted")
                if result.get("status") in {"accepted", "verified"}
                else "unknown"
            )
        else:
            consequence = "自动处理不踢人；踢出需管理员私聊确认。"
            result = await adapter.execute_action(
                ActionRequest.bind(
                    adapter,
                    f"content-warning/{group}/{mid}",
                    "warning",
                    int(group),
                    member=int(sender),
                    expected_nim=str(member["nimId"]),
                    text="请勿发布外部联系方式引流、返利广告或成人推广。24小时内再犯将禁言；"
                    + consequence,
                ),
                authorize=guard,
            )
            warning = result["status"]
            warning = (
                warning
                if warning in {"accepted", "verified", "rejected"}
                else "unknown"
            )
            status = "warned" if warning in {"accepted", "verified"} else "unknown"
    except Exception as exc:
        adapter.diagnostics.emit(
            "content_rules",
            "failed_or_unknown",
            mid,
            failed=True,
            error=str(exc) if isinstance(exc, ProtocolError) else "operation_failed",
        )
    finally:
        await asyncio.to_thread(store.finish, account, group, mid, status, warning)
    return True


def flood_category(adapter, group, mid, payload):
    """Read ingress evidence without recounting messages at processing time.

    Args:
        adapter: Current authenticated adapter.
        group: Business group identifier.
        mid: Stable platform message identifier.
        payload: Authenticated message envelope.

    Returns:
        Flood category or an empty string.
    """
    from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
    from astrbot.core.platform.sources.wangshangliao.test_rules import rule_target

    from .policy import is_fresh_message

    policy = adapter.config.get("moderation", {})
    sender = str(payload.get("sender", ""))
    now = time.time()
    if (
        group.startswith("private/")
        or not group
        or not sender
        or not policy.get("enabled")
        or not policy.get("automation_enabled")
        or policy.get("flood_enabled") is False
        or adapter.config.get("_business_active") is False
        or group not in adapter.config.get("enabled_groups", [])
        or is_managed_account(sender)
        and not rule_target(adapter, group, sender, mid)
        or sender == adapter.account
        or not is_fresh_message(payload, now)
    ):
        return ""
    evidence = payload.get("_ingress_flood")
    if not isinstance(evidence, dict) or not evidence.get("live"):
        return ""
    if is_managed_account(sender) or payload.get("_test_rule_window"):
        token = payload.get("_test_rule_window")
        if (
            not token
            or token != evidence.get("test_window")
            or token
            != getattr(getattr(adapter, "test_window", None), "_rule_generation", "")
        ):
            return ""
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision

    if evidence.get("revision") != group_revision(adapter.config, group):
        return ""
    return "message_flood" if evidence.get("category") == "message_flood" else ""
