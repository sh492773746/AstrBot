"""Fixed one-minute numeric sanctions, independent of warning escalation."""

import asyncio
import copy
import json
import time

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.business import BusinessClient
from astrbot.core.platform.sources.wangshangliao.diagnostics import failure_evidence
from astrbot.core.platform.sources.wangshangliao.directory import muted_member_ids
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


async def enforce(adapter, group, mid, payload, member, guard):
    """Mute once before independent recall without shortening an existing mute.

    Args:
        adapter: Existing audit-wrapped native action port.
        group: Authorized business group.
        mid: Fresh message already reserved by the content evidence store.
        payload: Original authenticated message envelope.
        member: Verified ordinary member from the current complete directory.
        guard: Caller-owned account, policy and exemption fence.

    Returns:
        Independent mute receipt, or a recall-only result if already muted.
        Refusals and unknown submissions are retained without retrying.
    """
    from .content_rules import content_category

    account = str(adapter.account)
    sender, peer = str(member["userId"]), str(member["nimId"])
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
        or (
            evidence.get("category"),
            evidence.get("account"),
            evidence.get("group"),
            evidence.get("sender"),
            evidence.get("message"),
            evidence.get("received_at"),
        )
        != ("consecutive_digits", account, group, sender, str(mid), rows[0][2])
        or any(
            original.get(key) != payload.get(key)
            for key in (
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
        raise ProtocolError("content_policy_changed")
    frozen = copy.deepcopy(payload)
    identity = (
        adapter.nim,
        adapter.business,
        str(adapter.nim_account),
        adapter.groups.get(group),
    )
    result = {"action": "mute", "status": "rejected", "submitted": False}

    async def authorize():
        """Fence the fixed rule and reread native mute state at HTTP submission."""
        guard()
        if (
            payload != frozen
            or sender != str(payload.get("sender", ""))
            or adapter.members.get(group, {}).get(sender) != peer
            or content_category(payload, adapter.config.get("moderation", {}))
            != "consecutive_digits"
            or identity
            != (
                adapter.nim,
                adapter.business,
                str(adapter.nim_account),
                adapter.groups.get(group),
            )
        ):
            raise ProtocolError("content_policy_changed")
        if getattr(
            adapter.business, "_authorization_task", None
        ) is asyncio.current_task() and sender in await muted_member_ids(
            adapter, group, authorize=guard, expected_member=(sender, peer)
        ):
            raise ProtocolError("automatic_existing_native_mute")
        guard()

    try:
        await authorize()
        # Native clients always repeat this read at the final HTTP fence.
        # A preliminary duplicate adds another roster and IM round trip.
        if not isinstance(
            adapter.business, BusinessClient
        ) and sender in await muted_member_ids(
            adapter, group, authorize=guard, expected_member=(sender, peer)
        ):
            guard()
            return {
                "action": "recall",
                "status": "pending",
                "existing_mute": True,
                "submitted": False,
            }
        await authorize()
        result = {"action": "mute", "status": "unknown"}
        receipt = await adapter.execute_action(
            ActionRequest.bind(
                adapter,
                f"numeric/{adapter.account}/{group}/{mid}",
                "mute",
                int(group),
                member=int(sender),
                expected_nim=peer,
                minutes=1,
            ),
            authorize=authorize,
        )
        return {**receipt, "action": "mute"}
    except Exception as exc:
        if str(exc) == "automatic_existing_native_mute":
            return {
                "action": "recall",
                "status": "pending",
                "existing_mute": True,
                "submitted": False,
            }
        return {
            **failure_evidence(exc),
            "action": "mute",
            "status": getattr(exc, "action_status", result["status"]),
            "submitted": getattr(exc, "submitted", result.get("submitted")),
        }
