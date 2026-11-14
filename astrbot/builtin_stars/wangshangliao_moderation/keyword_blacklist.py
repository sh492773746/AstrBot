"""Deterministic solicitation signatures with durable, group-local sanctions."""

import asyncio
import copy
import json
import re
import time
import unicodedata

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.blacklist import (
    BlacklistStore,
    protected_target,
)
from astrbot.core.platform.sources.wangshangliao.diagnostics import failure_evidence
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


def matches(text):
    """Recognize explicit solicitation phrases, not isolated everyday words.

    Args:
        text: Untrusted original message text.

    Returns:
        Whether a fixed phrase matches outside a report or quotation.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(
        c
        for c in text
        if not c.isspace() and unicodedata.category(c) != "Cf" and c != "\ufe0f"
    )
    if re.search(
        r"举报|有人发|有人私聊|骗子发|不要加|别加|勿加|不要点|别点|是不是诈骗|"
        r"是骗子吗|谨防|警惕|防骗|反诈|骗局|骗人的|不要相信|别信|勿信|"
        r"引用|转发[:：]|[“”「」]",
        text,
    ):
        return False
    return bool(
        re.search(
            r"(?<!\d)[1-9][0-9]{4,11}(?!\d)旺旺[+➕]|"
            r"正在开会打(?:🐶|狗)|红了发[0-9]{1,7}(?:元)?口令",
            text,
        )
    )


async def enforce(adapter, group, mid, payload, member, guard):
    """Enroll and mute a fresh proven sender without advancing warning cycles.

    Args:
        adapter: Original audit-wrapped action port.
        group: Current enabled business group.
        mid: Stable original inbox message ID.
        payload: Trusted ingress envelope.
        member: Complete preliminary roster row.
        guard: Existing account, policy, whitelist and lifecycle guard.

    Returns:
        Real mute outcome; a duplicate is rejected without submitting again.

    Raises:
        ProtocolError: If original evidence or current target authority is invalid.
        asyncio.CancelledError: After draining any started database operation.
    """
    account, sender = str(adapter.account), str(payload.get("sender", ""))
    peer = str(member.get("nimId", ""))
    frozen = copy.deepcopy(payload)
    evidence = payload.get("_ingress_content", {})
    rows = await adapter.ledger.db.execute_fetchall(
        "SELECT payload,sender,received_at,state FROM inbox "
        "WHERE account=? AND team=? AND message=?",
        (account, group, str(mid)),
    )
    guard()
    original = json.loads(rows[0][0]) if len(rows) == 1 else {}
    if (
        len(rows) != 1
        or rows[0][1] != sender
        or rows[0][3] not in {"pending", "processing", "processed"}
        or not 0 <= time.time() - rows[0][2] <= 300
        or evidence.get("category") != "blacklist_keyword"
        or evidence.get("account") != account
        or evidence.get("group") != group
        or evidence.get("sender") != sender
        or evidence.get("message") != str(mid)
        or evidence.get("received_at") != rows[0][2]
        or any(
            original.get(k) != payload.get(k)
            for k in (
                "sender",
                "text",
                "kind",
                "recall_route",
                "_open_revision",
                "_ingress_content",
                "_test_rule_window",
            )
        )
    ):
        raise ProtocolError("blacklist_keyword_evidence")
    cancelled = False

    def authorize():
        guard()
        snapshot = adapter.member_snapshots.get(group)
        latest = (
            [
                row
                for row in snapshot[2].get("groupMemberInfo", [])
                if str(row.get("userId")) == sender
            ]
            if snapshot and snapshot[0] == account and snapshot[2].get("complete")
            else []
        )
        if (
            cancelled
            or payload != frozen
            or not matches(frozen.get("text", ""))
            or member.get("groupRole") != "GROUP_ROLE_MEMBER"
            or str(member.get("userId")) != sender
            or len(latest) != 1
            or latest[0].get("groupRole") != "GROUP_ROLE_MEMBER"
            or str(latest[0].get("nimId", "")) != peer
            or adapter.members.get(group, {}).get(sender) != peer
            or protected_target(adapter, group, sender, peer)
        ):
            raise ProtocolError("blacklist_target_changed")

    authorize()
    store = BlacklistStore(adapter.config["id"])
    version, _ = await asyncio.to_thread(store.read, account, group)
    authorize()
    enrollment = asyncio.create_task(
        asyncio.to_thread(
            store.set_member,
            account,
            group,
            sender,
            peer,
            str(member.get("groupMemberNick") or member.get("userNick") or "群成员"),
            enabled=True,
            actor=account,
            guard=authorize,
            expected_version=version,
        )
    )
    reservation = None
    intent = None
    result = {"action": "mute", "status": "rejected", "submitted": False}
    interrupted = None
    try:
        _, entries = await asyncio.shield(enrollment)
        authorize()
        entry = next(row for row in entries if row["member"] == sender)
        reservation = asyncio.create_task(
            asyncio.to_thread(
                store.reserve_keyword,
                account,
                group,
                sender,
                peer,
                entry["id"],
                str(mid),
                authorize,
            )
        )
        intent = await asyncio.shield(reservation)
        authorize()
        if intent is None:
            return {**result, "duplicate_or_unresolved": True}
        result = {"action": "mute", "status": "unknown"}
        receipt = await adapter.execute_action(
            ActionRequest.bind(
                adapter,
                intent.operation,
                "mute",
                int(group),
                member=int(sender),
                expected_nim=peer,
                minutes=intent.minutes,
                blacklist_intent=intent,
                blacklist_guard=authorize,
            ),
            authorize=authorize,
        )
        result = dict(receipt) if isinstance(receipt, dict) else result
    except asyncio.CancelledError as exc:
        cancelled, interrupted = True, exc
    except Exception as exc:
        result = {
            **failure_evidence(exc),
            "action": "mute",
            "status": getattr(
                exc,
                "action_status",
                "unknown" if result.get("status") == "unknown" else "rejected",
            ),
            "submitted": getattr(exc, "submitted", None),
        }
    finally:
        for task in (enrollment, reservation):
            if task is None:
                continue
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    cancelled, interrupted = True, exc
                except Exception:
                    break
            if (
                not task.cancelled()
                and task.exception() is None
                and task is reservation
            ):
                intent = task.result()
        if intent is not None:
            saving = asyncio.create_task(asyncio.to_thread(store.done, intent, result))
            while not saving.done():
                try:
                    await asyncio.shield(saving)
                except asyncio.CancelledError as exc:
                    cancelled, interrupted = True, exc
            result["status"] = saving.result()
        if interrupted is not None:
            raise interrupted
    return {**result, "action": "mute"}
