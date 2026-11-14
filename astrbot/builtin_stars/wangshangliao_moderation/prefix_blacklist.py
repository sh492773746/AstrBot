"""Operator-authorized nickname prefixes, separate from chat or AI authority."""

import asyncio
import json
import time

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.blacklist import (
    BlacklistStore,
    protected_target,
)
from astrbot.core.platform.sources.wangshangliao.directory import muted_member_ids
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .member_exemptions import read_exemptions
from .member_whitelist import member_name
from .open_store import OpenError


def read_prefix_rule(instance, account, group):
    """Read a bounded private rule explicitly installed by the server operator.

    Args:
        instance: Saved receiving platform instance.
        account: Authenticated robot UID.
        group: Exact business group.

    Returns:
        Valid seven-day configuration, or None when absent, invalid or out of scope.
    """
    path = instance_dir(instance) / "blacklist_prefixes.json"
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8192:
            return None
        rule = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(rule, dict)
            or set(rule) != {"account", "groups", "prefixes", "minutes", "actor"}
            or rule["account"] != account
            or type(rule["minutes"]) is not int
            or rule["minutes"] != 10080
            or not isinstance(rule["actor"], str)
            or not rule["actor"].startswith("dashboard/")
            or not 10 < len(rule["actor"]) <= 256
            or not rule["actor"].isprintable()
            or not isinstance(rule["groups"], list)
            or not 1 <= len(rule["groups"]) <= 20
            or any(
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
                or len(value) > 19
                or not 0 < int(value) < 1 << 63
                or str(int(value)) != value
                for value in rule["groups"]
            )
            or len(set(rule["groups"])) != len(rule["groups"])
            or group not in rule["groups"]
            or not isinstance(rule["prefixes"], list)
            or not 1 <= len(rule["prefixes"]) <= 8
            or any(
                not isinstance(value, str)
                or not 1 <= len(value) <= 64
                or value != value.strip()
                or not value.isprintable()
                for value in rule["prefixes"]
            )
            or len({value.casefold() for value in rule["prefixes"]})
            != len(rule["prefixes"])
        ):
            return None
        return rule
    except (OSError, ValueError, TypeError):
        return None


async def enforce_prefixes(owner, adapter, group):
    """Enroll current matching members and submit a single durable seven-day mute.

    Args:
        owner: Existing bounded blacklist worker and its lifecycle.
        adapter: Original authenticated receiving account.
        group: Explicit worker group; never inferred from a nickname or message.

    This scan runs at most once per minute, or after a native join wakeup. It never
    downloads media, waits for OCR, invents join IDs or retries unknown mutations.
    """
    service, account = owner.service, str(adapter.account)
    rule = await asyncio.to_thread(
        read_prefix_rule, adapter.config["id"], account, group
    )
    if rule is None:
        return
    key, now = (account, group), time.monotonic()
    previous = owner.prefix_scans.get(key)
    if previous and previous[1] == rule and now - previous[0] < 60:
        return
    owner.prefix_scans[key] = (now, rule)
    counts = {
        "matched": 0,
        "added": 0,
        "accepted": 0,
        "rejected": 0,
        "unknown": 0,
        "already_muted": 0,
        "blocked": 0,
        "paused": 0,
    }
    try:
        tenant = dict(service.tenants.get(key, {}))
        if (
            not tenant
            or not tenant["active"]
            or not tenant["present"]
            or tenant["paused"]
            or service.settings["paused"]
        ):
            return
        frozen = service.write_guard(adapter)
        members = await service.roster(adapter, group)
        frozen()
        authority = service.write_guard(
            adapter, group=group, tenant=tenant, members=members, control=True
        )

        def base_guard():
            authority()
            if (
                service.settings["paused"]
                or tenant["paused"]
                or service.store.tenant(account, group) != tenant
                or read_prefix_rule(adapter.config["id"], account, group) != rule
            ):
                raise OpenError("blacklist_prefix_revoked")

        base_guard()
        prefixes = tuple(value.casefold() for value in rule["prefixes"])
        candidates = [
            (uid, row)
            for uid, row in members.items()
            if str(row.get("groupMemberNick") or row.get("userNick") or "")
            .casefold()
            .startswith(prefixes)
            and row.get("groupRole") == "GROUP_ROLE_MEMBER"
            and row.get("accountState")
            not in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
            and not protected_target(adapter, group, uid, str(row.get("nimId", "")))
        ]
        counts["matched"] = len(candidates)
        if candidates:
            store = await asyncio.to_thread(BlacklistStore, adapter.config["id"])
            settings = await asyncio.to_thread(store.settings, account, group)
            base_guard()
            if settings["mute_minutes"] != rule["minutes"]:
                raise OpenError("blacklist_prefix_duration_changed")
            _, exemptions = await asyncio.to_thread(
                read_exemptions, adapter.config["id"], account, group
            )
            exemption_ids = {value.lstrip("0") for value in exemptions}
            _, entries = await asyncio.to_thread(store.read, account, group)
            base_guard()
            by_uid = {entry["member"]: entry for entry in entries}
            muted = await muted_member_ids(adapter, group, authorize=base_guard)
            base_guard()
            for uid, row in candidates:
                if (
                    sum(counts[state] for state in ("accepted", "rejected", "unknown"))
                    >= 20
                ):
                    owner.prefix_scans.pop(key, None)
                    break
                peer = str(row.get("nimId", ""))
                if not peer or uid in exemption_ids:
                    counts["blocked"] += 1
                    continue
                intent = None

                def guard(uid=uid, peer=peer):
                    base_guard()
                    live = service.member_evidence.get(key, {}).get(uid, {})
                    if (
                        str(live.get("nimId", "")) != peer
                        or live.get("groupRole") != "GROUP_ROLE_MEMBER"
                        or live.get("accountState")
                        in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
                        or not str(
                            live.get("groupMemberNick") or live.get("userNick") or ""
                        )
                        .casefold()
                        .startswith(prefixes)
                        or protected_target(adapter, group, uid, peer)
                    ):
                        raise OpenError("blacklist_prefix_target_changed")

                async def authorize():
                    guard()
                    await service.roster(adapter, group)
                    guard()

                guard()
                entry = by_uid.get(uid)
                if entry is None:
                    try:
                        _, saved = await asyncio.to_thread(
                            store.set_member,
                            account,
                            group,
                            uid,
                            peer,
                            member_name(row),
                            enabled=True,
                            actor=rule["actor"],
                            guard=guard,
                        )
                    except ProtocolError as exc:
                        if str(exc) != "member_list_conflict":
                            raise
                        counts["blocked"] += 1
                        continue
                    entry = next(item for item in saved if item["member"] == uid)
                    counts["added"] += 1
                if entry["peer"] != peer:
                    counts["blocked"] += 1
                    continue
                if uid in muted:
                    counts["already_muted"] += 1
                    continue
                await authorize()
                intent = await asyncio.to_thread(
                    store.reserve_prefix, account, group, uid, peer, entry["id"], guard
                )
                if intent is None:
                    counts["blocked"] += 1
                    continue
                result = {"status": "unknown"}
                try:
                    request = ActionRequest.bind(
                        adapter,
                        intent.operation,
                        "mute",
                        int(group),
                        member=int(uid),
                        minutes=intent.minutes,
                        expected_nim=peer,
                        blacklist_intent=intent,
                        blacklist_guard=guard,
                    )
                    receipt = await adapter.execute_action(request, authorize=authorize)
                    if isinstance(receipt, dict):
                        result = dict(receipt)
                except (OpenError, ProtocolError, OSError, TimeoutError) as exc:
                    result = {
                        "status": "rejected"
                        if getattr(exc, "submitted", None) is False
                        else "unknown",
                        "submitted": getattr(exc, "submitted", None),
                    }
                finally:
                    status = await asyncio.shield(
                        asyncio.to_thread(store.done, intent, result)
                    )
                counts[
                    "accepted"
                    if status in {"accepted", "verified"}
                    else status
                    if status in {"rejected", "unknown"}
                    else "blocked"
                ] += 1
        adapter.diagnostics.emit("member_blacklist_prefix", "scanned", group=group)
    except (OpenError, ProtocolError, OSError, TimeoutError) as exc:
        counts["paused"] = 1
        adapter.diagnostics.emit(
            "member_blacklist_prefix",
            "paused",
            group=group,
            failed=True,
            error="configuration_conflict"
            if str(exc) == "blacklist_prefix_duration_changed"
            else "permission_denied"
            if str(exc)
            in {"blacklist_prefix_revoked", "blacklist_prefix_target_changed"}
            else "operation_failed",
        )
    finally:
        for name, value in counts.items():
            adapter.diagnostics.gauges[f"blacklist_prefix_{group}_{name}"] = value
        adapter.diagnostics.gauges[f"blacklist_prefix_{group}_checked_at"] = int(
            time.time()
        )
