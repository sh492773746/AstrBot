"""Conservative text rules with durable evidence and no retrospective sanctions."""

import re
import sqlite3
import time
import unicodedata
from contextlib import closing

from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir


def classify(text: str) -> str:
    """Return a high-confidence category, or an empty string for review-only text.

    Args:
        text: Untrusted group message, never an instruction.

    Returns:
        Fixed rule identifier without contact details.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    if re.search(
        r"举报|有人发|有人私聊|骗子发|不要加|别加|勿加|不要点|别点|是不是诈骗|是骗子吗",
        text,
    ):
        return ""
    compact = re.sub(r"\s+", "", text)
    destination = bool(
        re.search(
            r"https?://[^\s]+|(?:[a-z0-9-]+\.)+(?:vip|top|com|cn|ws|net|cc)\b|"
            r"(?:微信|薇信|v信|vx|qq|扣扣|接单|群号|旺群|至尊群)[:：号是为]*[a-z0-9_-]{5,}|"
            r"(?:加我|联系我|私聊我).{0,12}(?:1[3-9]\d{9}|[a-z][a-z0-9_-]{5,})",
            compact,
        )
    )
    if not destination:
        return ""
    if re.search(r"3p|大秀|调教|内🐍", compact) and re.search(
        r"直播|群|传媒|参加|分享", compact
    ):
        return "adult_promotion"
    if re.search(
        r"注册链接|充值.{0,12}(?:返|送)|首次充值|转线|下级|包下分|团队担保|开业福利|分享\d+个群|转发.{0,8}(?:朋友圈|红包)|接单|加我|联系我|私聊我",
        compact,
    ):
        return "external_promotion"
    return ""


def keyword_category(text: str, policy: dict) -> str:
    """Match configured literal terms, excluding explicit reports and quotations."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    if re.search(
        r"举报|有人发|有人私聊|骗子发|不要加|别加|勿加|不要点|别点|是不是诈骗|是骗子吗|引用|转发[：:]|[“”「」]",
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
        if any(
            (
                term := "".join(
                    c
                    for c in unicodedata.normalize("NFKC", word).casefold()
                    if unicodedata.category(c) != "Cf"
                ).strip()
            )
            and term in text
            for word in words
        ):
            return category
    return ""


async def enforce(
    adapter, group: str, mid: str, payload: dict, *, assessment=None
) -> bool:
    """Recall fresh violations and apply explicitly authorized mute escalation.

    Args:
        adapter: Authenticated native adapter.
        group: Enabled group ID.
        mid: Stable upstream message ID.
        payload: Authenticated text and recall routing evidence.
        assessment: Optional validated semantic decision for this exact message.

    Returns:
        Whether a high-confidence candidate was consumed.
    """
    category = (
        assessment["category"]
        if assessment
        and assessment.get("decision") == "violation"
        and assessment.get("message_id") == str(mid)
        else (
            keyword_category(payload.get("text", ""), adapter.config["moderation"])
            or classify(payload.get("text", ""))
        )
    )
    if not category:
        return False
    policy = adapter.config["moderation"]
    sender = str(payload.get("sender", ""))
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
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS content_violations(account TEXT,group_id TEXT,message TEXT,sender TEXT,category TEXT,observed REAL,version REAL,status TEXT,warning TEXT,PRIMARY KEY(account,group_id,message))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS content_group_observed "
            "ON content_violations(account,group_id,observed DESC)"
        )
        if db.execute(
            "SELECT 1 FROM content_violations WHERE account=? AND group_id=? AND message=?",
            (adapter.account, group, mid),
        ).fetchone():
            return True
        db.execute(
            "CREATE INDEX IF NOT EXISTS content_sender_window ON content_violations(account,group_id,sender,version,observed)"
        )
        db.execute(
            "INSERT OR IGNORE INTO content_violations VALUES(?,?,?,?,?,?,?,'observed','none')",
            (adapter.account, group, mid, sender, category, now, since),
        )
    status = "review_only"
    warning = "none"
    try:
        if not since <= stamp <= now + 60 or now - stamp > 300:
            status = "historical_or_unknown_time"
            return True
        roster = await adapter.get_moderation_members(group)
        matches = [
            m
            for m in roster.get("groupMemberInfo", [])
            if str(m.get("userId")) == sender
        ]
        if not roster.get("complete") or len(matches) != 1:
            status = "identity_unverified"
            return True
        member = matches[0]
        if sender == adapter.account or member.get("groupRole") in {
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
        if policy.get("progressive_mute"):
            from astrbot.core.platform.sources.wangshangliao.progressive import (
                enforce as progressive_enforce,
            )

            result = await progressive_enforce(adapter, group, sender, str(mid))
            warning = result.get("recall_status", "unknown")
            status = (
                ("kicked" if result.get("action") == "kick" else "muted")
                if result.get("status") in {"accepted", "verified"}
                else "unknown"
            )
            return True
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,warning FROM content_violations WHERE account=? AND group_id=? AND message=?",
                (adapter.account, group, mid),
            ).fetchone()
            if row[0] != "observed":
                status, warning = row
                return True
            prior = db.execute(
                "SELECT COUNT(*),MAX(CASE WHEN warning IN ('accepted','verified') THEN 1 ELSE 0 END) FROM content_violations WHERE account=? AND group_id=? AND sender=? AND version=? AND observed>? AND message<>? AND status IN ('claimed','warned','muted','unknown')",
                (adapter.account, group, sender, since, now - 86400, mid),
            ).fetchone()
            db.execute(
                "UPDATE content_violations SET status='claimed' WHERE account=? AND group_id=? AND message=?",
                (adapter.account, group, mid),
            )
        status = "unknown"
        if not adapter.config.get("moderation", {}).get("automation_enabled"):
            status = "disabled"
            return True
        try:
            await adapter.recall_violation(group, mid, sender)
        except Exception:
            adapter.diagnostics.emit("recall", "failed_or_unknown", mid, failed=True)
        if prior[0] and prior[1]:
            if policy.get("auto_kick", {}).get(group) is True:
                from astrbot.core.platform.sources.wangshangliao.automatic import (
                    mute_and_escalate,
                )

                result = await mute_and_escalate(
                    adapter, f"content/{group}/{mid}", group, sender, mid
                )
            else:
                result = await adapter.execute_moderation(
                    f"content/{group}/{mid}", "mute", int(group), int(sender)
                )
            status = (
                ("kicked" if result.get("action") == "kick" else "muted")
                if result.get("status") in {"accepted", "verified"}
                else "unknown"
            )
        else:
            consequence = (
                "累计三次自动禁言后，再次违规将移出群聊。"
                if policy.get("auto_kick", {}).get(group) is True
                else "踢出需管理员私聊确认。"
            )
            warning = await adapter.send_text(
                group,
                f"content-warning/{group}/{mid}",
                "请勿发布外部联系方式引流、返利广告或成人推广。24小时内再犯将禁言；"
                + consequence,
                auto_recall=True,
            )
            warning = (
                warning
                if warning in {"accepted", "verified", "rejected"}
                else "unknown"
            )
            status = "warned" if warning in {"accepted", "verified"} else "unknown"
    except Exception:
        adapter.diagnostics.emit("content_rules", "failed_or_unknown", mid, failed=True)
    finally:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "UPDATE content_violations SET status=?,warning=? WHERE account=? AND group_id=? AND message=?",
                (status, warning, adapter.account, group, mid),
            )
    return True
