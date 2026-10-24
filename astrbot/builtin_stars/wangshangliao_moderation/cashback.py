"""Read-only invitation collection feeding the independent manual cash register."""

import asyncio
import json
import sqlite3
import time
import unicodedata
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from astrbot.api import logger as log
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir

from .activities import GroupActivities, clean, member_name
from .cashback_store import WRITE_GUARD, CashbackError, CashbackStore, source_timestamp
from .private_invitation import PrivateInvitationQueries
from .text import plain_text, redact_reply


class CashbackService:
    def __init__(self, context, root, collector_id="wangshangliao_3cf3b038"):
        self.context = context
        self.store = CashbackStore(Path(root) / "cashback.sqlite3")
        self.activities = GroupActivities(context)
        self.locks = {}
        self.business_checked = {}
        self.collector_id = collector_id
        self.private_queries = PrivateInvitationQueries(self)

    def lock(self, group):
        return self.locks.setdefault(group, asyncio.Lock())

    def adapters(self):
        return [
            adapter
            for adapter in self.context.platform_manager.platform_insts
            if adapter.config.get("type") == "wangshangliao"
            and adapter.config.get("id") == self.collector_id
        ]

    def adapter(self, bot):
        for adapter in self.adapters():
            if adapter.config["id"] == bot:
                return adapter
        raise CashbackError(
            "collector_unavailable", "采集机器人不可用，请联系管理员。", 409
        )

    def catalog(self, actor):
        self.store.users(actor)
        result = []
        for adapter in self.adapters():
            for group in adapter.config.get("enabled_groups", []):
                result.append(
                    {
                        "id": group,
                        "bot_id": adapter.config["id"],
                        "name": self.group_name(adapter, group),
                    }
                )
        return result

    def selected_groups(self):
        return {
            str(group)
            for adapter in self.adapters()
            for group in adapter.config.get("enabled_groups", [])
            if not adapter.config.get("_open_integrated")
            or (
                not adapter.config.get("_open_paused")
                and group in adapter.config.get("_open_groups", {})
            )
        }

    def check_selected(self, group):
        if group not in self.selected_groups():
            raise CashbackError(
                "group_not_selected", "此群未在指定机器人中勾选，不能操作。", 403
            )

    def _lifecycle_guard(self, group, rule):
        """Freeze the collector lifecycle for one preview, scan or mutation.

        Args:
            group: Exact selected group ID.
            rule: Persisted group configuration for this operation.

        Returns:
            A synchronous guard preserving any caller's stricter authorization.

        Raises:
            CashbackError: If the group or configured collector is unavailable.
        """
        if not rule:
            raise CashbackError("group_unknown", "尚未配置此群。", 404)
        adapter = self.adapter(rule["bot_id"])
        epoch = adapter.config.get("_business_epoch")
        revision = adapter.config.get("_business_revision")
        inherited_guard = WRITE_GUARD.get()

        def guard():
            if inherited_guard is not None:
                inherited_guard()
            self.check_selected(group)
            config = adapter.config
            if (
                self.adapter(rule["bot_id"]) is not adapter
                or config.get("_business_active") is False
                or not config.get("enable", True)
                or config.get("_business_epoch") != epoch
                or config.get("_business_revision") != revision
            ):
                raise CashbackError(
                    "business_service_inactive",
                    "业务已暂停或运行状态变化，当前仅可查询历史账本。",
                    409,
                )
            if adapter.account != rule["account"]:
                raise CashbackError(
                    "collector_changed", "机器人账号变化，本次未写入。", 409
                )

        return guard

    def risk_cases(self, actor, group):
        self.check_selected(group)
        return self.store.risk_cases(actor, group)

    async def release_risk(self, actor, group, inviter, reason):
        self.check_selected(group)
        rule = await asyncio.to_thread(self.store.get_group, group)
        guard = self._lifecycle_guard(group, rule)
        guard()
        async with self.lock(group):
            token = WRITE_GUARD.set(guard)
            try:
                return await asyncio.to_thread(
                    self.store.release_risk, actor, group, inviter, reason
                )
            finally:
                WRITE_GUARD.reset(token)

    async def invitation_reply(self, event, group, action):
        """Expose sender-only cash totals, not cashier capabilities, to chat."""
        adapter = event.platform
        if (
            event.is_private_chat()
            and not event.is_admin()
            or not event.is_private_chat()
            and str(event.get_group_id()) != group
        ):
            raise CashbackError("query_scope", "请在对应群查询本人的邀请记录。", 403)
        self.activities.authorize(adapter, group)
        if group not in self.selected_groups():
            return None
        result = await asyncio.to_thread(
            self.store.invitation_summary, group, str(event.get_sender_id())
        )
        if result is None:
            return None
        contact = redact_reply(plain_text(result["contact_name"]))[:200]
        contact_note = (
            f"如核算有问题，请联系本群结账员：{contact}。"
            if contact
            else "如核算有问题，请联系本群结账员（联系方式尚未配置）。"
        )
        risk_note = (
            "⚠️ 该群返现结账已因重复风控命中暂停，请联系结账员或管理员复核。"
            if result["risk_status"] == "blocked"
            else "⚠️ 你的邀请记录已触发风控预警；再次命中将暂停本群返现结账。"
            if result["risk_status"] == "warned"
            else ""
        )
        if action == "我的邀请明细":
            if not result["items"]:
                return (
                    "🎟️ 暂无返现邀请记录。\n"
                    + (risk_note + "\n" if risk_note else "")
                    + contact_note
                )
            lines = []
            for index, item in enumerate(result["items"], 1):
                status = (
                    "已结账"
                    if item["state"] == "paid"
                    else "已作废"
                    if item["state"] == "void"
                    else "普通成员不返现，代理商需结账员核验证明"
                    if item["pricing_status"] == "no_reward"
                    else "核验中（待发言）"
                    if result["require_speech"]
                    and item["state"] == "verifying"
                    and item["hold_until"] <= time.time()
                    and not item["first_spoke_at"]
                    else "核验中"
                    if item["state"] == "verifying"
                    else "待结账"
                    if item["verification"] == "verified"
                    and item["present"]
                    and not item["ever_left"]
                    and item["pricing_status"] == "priced"
                    and item["member_class"] in {"ordinary", "agent"}
                    and item["hold_until"] <= time.time()
                    and (not result["require_speech"] or item["first_spoke_at"])
                    else "待核对"
                )
                speech_note = (
                    (
                        "，已发言"
                        if item["first_spoke_at"]
                        else "，发言未记录（原已结账登记保留）"
                        if item["state"] == "paid"
                        else "，未发言，不计入结账"
                    )
                    if result["require_speech"]
                    else ""
                )
                lines.append(
                    f"{index}. {clean(item['member_name'], 48) or '未命名成员'}"
                    f"（{'在群' if item['present'] else '已退群'}，{status}{speech_note}）"
                )
            suffix = (
                f"\n共 {result['total']} 人，仅显示最近 20 人。"
                if result["total"] > 20
                else ""
            )
            return (
                "🎟️ 我的返现邀请\n"
                + "\n".join(lines)
                + suffix
                + (f"\n{risk_note}" if risk_note else "")
                + "\n"
                + contact_note
            )

        def money(cents):
            return f"{Decimal(cents) / 100:.2f}"

        lines = [
            f"返现邀请记录：{result['total']} 人",
            f"当前在群：{result['present']} 人",
            f"目前已退群：{result['total'] - result['present']} 人",
            (
                "新增普通成员不返现；旧账按原规则结账。"
                if result["agent_only"]
                else f"普通成员单价：{money(result['rate'])} 元"
            )
            if result["enabled"]
            else "每人奖励：返现已暂停",
            f"待结账：{money(result['pending'])} 元",
            f"已结账：{money(result['paid'])} 元",
        ]
        if result["agent_rate"] is not None:
            lines.append(f"代理商成员单价：{money(result['agent_rate'])} 元")
        if result["no_reward"]:
            lines.append(f"普通成员不返现：{result['no_reward']} 人")
        if result["risk_status"] == "blocked":
            lines.append(
                f"风控暂停结账：{money(result['risk_blocked_amount'])} 元，等待管理员复核。"
            )
        elif risk_note:
            lines.append(risk_note)
        if result["verifying"]:
            lines.append(
                f"核验中（3小时未到）：{result['hold_pending'] if result['require_speech'] else result['verifying']} 笔"
            )
        if result["require_speech"]:
            lines.extend(
                [
                    f"未结邀请已发言：{result['spoken']} 人",
                    f"未结邀请未发言（暂不计入结账）：{result['unspoken']} 人",
                    "本群需入群满3小时且已发言，才符合结账条件。",
                ]
            )
        if result["review"]:
            lines.append(f"待核对：{result['review']} 笔")
        if result["void"]:
            lines.append(f"已作废：{result['void']} 笔")
        if result["error"] or time.time() - result["scanned"] > 60:
            lines.append("成员状态待刷新，结账前会重新核验。")
        lines.append(contact_note)
        return "\n".join(lines)

    def visible_groups(self, actor):
        with self.store.connect() as db:
            person = self.store._actor(db, actor)
        configured = {g["id"]: g for g in self.store.groups(actor)}
        result = []
        for adapter in self.adapters():
            for group in adapter.config.get("enabled_groups", []):
                saved = configured.get(group)
                if not saved and person["role"] != "admin":
                    continue
                result.append(
                    {
                        **(
                            saved
                            or {
                                "id": group,
                                "rate": None,
                                "enabled": 0,
                                "version": 0,
                                "cashier": "",
                                "agent_rate": None,
                                "agent_only": 0,
                                "qualification_seconds": 10800,
                                "require_speech": 0,
                                "cashier_name": "",
                                "contact": "",
                                "error": "",
                                "scanned": 0,
                            }
                        ),
                        "name": self.group_name(adapter, group),
                        "bot_id": adapter.config["id"],
                        "configured": bool(saved),
                    }
                )
        return result

    async def directory(self, actor, group):
        self.store.users(actor)
        self.check_selected(group)
        adapter = self.adapter(self.collector_id)
        members = await self.roster(adapter, group)
        return [
            {"uid": uid, "name": row["name"]}
            for uid, row in members.items()
            if row["eligible"]
        ]

    async def save_cashier(self, actor, uid, group, active, version):
        profiles = self.store.cashier_profiles(actor)
        old = next((p for p in profiles if p["uid"] == uid), None)
        if old and not active:
            name = old["name"]
        else:
            members = await self.directory(actor, group)
            candidate = next((p for p in members if p["uid"] == uid), None)
            if candidate is None:
                raise CashbackError(
                    "cashier_not_member",
                    "未在指定群的完整名单中找到此联系人账号。",
                    409,
                )
            name = candidate["name"]
        return await asyncio.to_thread(
            self.store.save_cashier, actor, uid, name, active, version
        )

    @staticmethod
    def group_name(adapter, group):
        names = getattr(adapter, "group_names", {})
        return str(names.get(group) or f"群 {group}")

    async def roster(self, adapter, group):
        raw = await asyncio.wait_for(self.activities.roster(adapter, group), timeout=30)
        if not raw:
            raise CashbackError("empty_roster", "成员名单为空，无法完成核验。", 409)
        return {
            uid: {
                "name": member_name(member, "名称待核验"),
                "avatar_ref": clean(str(member.get("userAvatar") or ""))[:128],
                # The platform currently returns only an opaque avatar
                # resource identifier. Never count digits in that identifier.
                "avatar_digit_count": (
                    member.get("avatar_digit_count")
                    if type(member.get("avatar_digit_count")) is int
                    else None
                ),
                "avatar_digits_detected": (
                    member.get("avatar_digits_detected")
                    if type(member.get("avatar_digits_detected")) is bool
                    else None
                ),
                "peer": member["nimId"],
                "eligible": member.get("accountState") == "ACCOUNT_STATE_GOOD"
                and uid != adapter.account
                and not is_managed_account(uid),
            }
            for uid, member in raw.items()
        }

    @staticmethod
    def historical_members(group):
        """Union all available instance histories, including retired collectors."""
        root = instance_dir("_cashback_history_locator").parent
        known = set()
        try:
            for path in root.glob("*/activities.sqlite3"):
                with closing(
                    sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
                ) as db:
                    tables = {
                        row[0]
                        for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    for table in ("invite_seen", "invite_credits", "invite_identities"):
                        if table in tables:
                            known.update(
                                str(row[0])
                                for row in db.execute(
                                    f"SELECT member FROM {table} WHERE group_id=?",
                                    (group,),
                                )
                            )
            for path in root.glob("*/messages.sqlite3"):
                with closing(
                    sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
                ) as db:
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='inbox'"
                    ).fetchone():
                        known.update(
                            str(row[0])
                            for row in db.execute(
                                "SELECT DISTINCT json_extract(payload,'$.sender') FROM inbox "
                                "WHERE team=? AND json_valid(payload) "
                                "AND json_extract(payload,'$.sender') IS NOT NULL",
                                (group,),
                            )
                        )
            for path in root.glob("*/cards.sqlite3"):
                with closing(
                    sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
                ) as db:
                    tables = {
                        row[0]
                        for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    for table in ("card_seen", "card_names"):
                        if table in tables:
                            known.update(
                                str(row[0])
                                for row in db.execute(
                                    f"SELECT json_extract(identity,'$[2]') FROM {table} "
                                    "WHERE json_valid(identity) AND json_extract(identity,'$[1]')=?",
                                    (group,),
                                )
                            )
        except sqlite3.Error as exc:
            raise CashbackError(
                "history_unavailable", "已有邀请历史无法读取，暂不能建立启用基线。", 409
            ) from exc
        return {
            uid
            for uid in known
            if uid.isascii() and uid.isdigit() and 0 < len(uid) <= 20 and int(uid) > 0
        }

    async def evidence(self, adapter, group, members, started):
        """Keep join actors separate from approved business recruiter evidence."""
        result = []
        peers = {value["peer"]: uid for uid, value in members.items()}
        ledger = getattr(adapter, "ledger", None)
        if ledger and getattr(ledger, "db", None):
            # No fixed first-page limit: a busy group must not lose later evidence.
            async with ledger.db.execute(
                "SELECT message,payload,received_at FROM inbox WHERE account=? AND team=? "
                "AND state='invite_notification' AND received_at>=? ORDER BY received_at,message",
                (adapter.account, group, started),
            ) as cursor:
                while rows := await cursor.fetchmany(200):
                    for message, payload, received in rows:
                        try:
                            notice = json.loads(payload)
                        except (TypeError, ValueError):
                            continue
                        if (
                            not isinstance(notice, dict)
                            or notice.get("kind") != "team_invite"
                        ):
                            continue
                        stamp = notice.get("event_at", 0)
                        if type(stamp) not in (int, float):
                            continue
                        stamp = stamp / 1000 if stamp > 100000000000 else stamp
                        if not started <= stamp <= time.time() + 30:
                            continue
                        inviter = peers.get(str(notice.get("inviter_peer", "")))
                        if not inviter or not isinstance(
                            notice.get("member_peers"), list
                        ):
                            continue
                        for peer in notice["member_peers"]:
                            uid = peers.get(str(peer))
                            if uid:
                                result.append(
                                    {
                                        "member": uid,
                                        "inviter": inviter,
                                        "source": "nim_notification",
                                        "event_id": str(message),
                                        "event_at": stamp,
                                    }
                                )
        # Read the source application timestamp, not a later points-ledger write
        # timestamp: old approval records cannot establish new cash eligibility.
        bot = adapter.config["id"]
        checkpoint = await asyncio.to_thread(
            self.store.source_checkpoint, group, bot, adapter.account, started
        )
        evidence_floor = checkpoint.get("evidence_floor", started)
        if (
            time.monotonic() - self.business_checked.get(group, 0) > 35
            and time.time() >= checkpoint["source_retry_at"]
        ):
            self.business_checked[group] = time.monotonic()
            try:
                boundary = max(
                    0,
                    int(evidence_floor) - 1,
                    int(checkpoint["source_cursor"] or 0) - 1,
                )
                page = await asyncio.wait_for(
                    adapter.get_group_invitation_records(
                        group,
                        pending_only=False,
                        # Overlap a second to retain simultaneous source updates.
                        last_id=str(boundary),
                    ),
                    timeout=20,
                )
                await asyncio.to_thread(
                    self.store.remember_source_page,
                    group,
                    bot,
                    adapter.account,
                    started,
                    page,
                )
            except Exception as exc:
                reason = str(exc)
                if isinstance(exc, CashbackError):
                    if exc.code == "collector_changed":
                        raise
                    reason = exc.code
                if reason not in {
                    "response_invalid",
                    "response_limit",
                    "body_limit",
                    "body_authentication_or_compression",
                    "invitation_response",
                    "invitation_read_cooldown",
                    "transport",
                    "rate_limited",
                    "reauth_required",
                    "http_rejected",
                    "business_rejected",
                    "invitation_cursor",
                    "invitation_cache",
                }:
                    reason = "source_unavailable"
                deferred = reason == "invitation_read_cooldown"
                if not deferred:
                    retry_after = getattr(exc, "retry_after", 0) or 0
                    await asyncio.to_thread(
                        self.store.source_failed,
                        group,
                        bot,
                        adapter.account,
                        started,
                        reason,
                        retry_after=retry_after,
                    )
                await asyncio.to_thread(
                    self.store.operation_log,
                    "invitation_source_deferred"
                    if deferred
                    else "invitation_source_failed",
                    group=group,
                    level="info" if deferred else "warning",
                    code=reason,
                    bot_id=bot,
                    sample_seconds=300,
                    detail={
                        "source": "business_apply_log",
                        "exception_type": type(exc).__name__,
                        "reason_code": reason,
                    },
                )
        records = await asyncio.to_thread(
            self.store.source_records, group, bot, adapter.account, started
        )
        for item in records:
            uid, inviter = item.get("member_id"), item.get("inviter_id")
            if (
                item.get("state") != "MEMBER_STATE_GOOD"
                or item.get(
                    "event_type",
                    item.get("apply_type", item.get("group_apply_type", "INVITATION")),
                )
                != "INVITATION"
                or not uid
                or not inviter
                or uid == inviter
                or inviter == str(adapter.account)
                or uid in members
                and item.get("member_nim_id") != members[uid]["peer"]
                or inviter in members
                and item.get("inviter_nim_id") != members[inviter]["peer"]
            ):
                continue
            try:
                stamp = source_timestamp(item["apply_time"])
            except (TypeError, ValueError, KeyError, OverflowError, OSError):
                continue
            if evidence_floor <= stamp <= time.time() + 30:
                result.append(
                    {
                        "member": uid,
                        "inviter": inviter,
                        "source": "business_apply_log",
                        "event_id": item["apply_id"],
                        "event_at": stamp,
                        "member_peer": item["member_nim_id"],
                        "inviter_peer": item["inviter_nim_id"],
                        "member_name": item.get("member_name", ""),
                    }
                )
        return result

    async def _scan(self, group, *, for_payment=False):
        started = time.monotonic()
        rule = await asyncio.to_thread(self.store.get_group, group)
        if not rule:
            raise CashbackError("group_unknown", "尚未配置此群。", 404)
        if not rule["enabled"] and not for_payment:
            return {}
        try:
            commit_guard = self._lifecycle_guard(group, rule)
            commit_guard()
            adapter = self.adapter(rule["bot_id"])
            members = await self.roster(adapter, group)
            evidence = await self.evidence(adapter, group, members, rule["started"])
            speech_data = {}
            if rule.get("require_speech"):
                speech, until = await self.speech(adapter, group, members, rule)
                speech_data = {"speech": speech, "speech_until": until}
            commit_guard()
            self.activities.authorize(adapter, group)

            await asyncio.to_thread(
                self.store.snapshot,
                group,
                rule["bot_id"],
                rule["account"],
                members,
                evidence,
                guard=commit_guard,
                **speech_data,
            )
            await self._deliver_risk_notices(adapter, group, members)
            await asyncio.to_thread(
                self.store.operation_log,
                "invitation_scan",
                group=group,
                bot_id=rule["bot_id"],
                sample_seconds=0 if rule["error"] else 300,
                detail={
                    "members": len(members),
                    "evidence": len(evidence),
                    "outcome": "recovered" if rule["error"] else "completed",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return members
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, CashbackError)
                else "verification_unavailable"
            )
            await asyncio.to_thread(self.store.scan_error, group, code)
            await asyncio.to_thread(
                self.store.operation_log,
                "invitation_scan",
                group=group,
                level="warning" if isinstance(exc, CashbackError) else "error",
                code=code,
                bot_id=rule["bot_id"],
                sample_seconds=300 if rule["error"] == code else 0,
                detail={
                    "exception_type": type(exc).__name__,
                    "outcome": "failed",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            if isinstance(exc, CashbackError):
                raise
            raise CashbackError(
                code, "邀请或完整成员名单核验失败，未登记付款。请稍后重试。", 409
            ) from exc

    async def speech(self, adapter, group, members, rule):
        """Read authenticated human group text without invoking chat or tools.

        Args:
            adapter: The current configured collector.
            group: Exact enabled business group.
            members: Complete verified roster with native peer bindings.
            rule: Current group rule and durable receipt-time cursor.

        Returns:
            Verified message timestamps and the fully read receipt-time boundary.
        """
        ledger = getattr(adapter, "ledger", None)
        if not ledger or not getattr(ledger, "db", None):
            return [], 0
        until = time.time()
        since = max(rule["started"], rule.get("speech_cursor", 0) - 1)
        result = []
        async with ledger.db.execute(
            "SELECT payload,received_at FROM inbox WHERE account=? AND team=? "
            "AND received_at>=? AND received_at<=? "
            "AND state NOT IN ('self','unsupported','rejected_identity','invite_notification') "
            "ORDER BY received_at,message",
            (adapter.account, group, since, until),
        ) as cursor:
            while rows := await cursor.fetchmany(200):
                for encoded, received in rows:
                    try:
                        payload = json.loads(encoded)
                        if not isinstance(payload, dict):
                            continue
                        uid, text = payload.get("sender"), payload.get("text")
                        member = members.get(uid)
                        route = payload.get("recall_route", {})
                        if (
                            not member
                            or not member["eligible"]
                            or uid == adapter.account
                            or is_managed_account(uid)
                            or not isinstance(text, str)
                            or not any(
                                not char.isspace()
                                and unicodedata.category(char) != "Cf"
                                for char in text
                            )
                            or not isinstance(route, dict)
                            or str(route.get("peer", "")) != member["peer"]
                        ):
                            continue
                        stamp = float(route.get("time", 0))
                        if stamp > 100000000000:
                            stamp /= 1000
                        if not rule["started"] <= stamp <= min(until, received + 30):
                            continue
                        result.append(
                            {"member": uid, "peer": member["peer"], "event_at": stamp}
                        )
                    except (TypeError, ValueError):
                        continue
        return result, until

    async def _deliver_risk_notices(self, adapter, group, members):
        """Deliver only committed, idempotent cashback warnings to the inviter."""
        proactive = adapter.config.get("proactive_send", {})
        if not proactive.get("enabled", False) or group not in proactive.get(
            "targets", []
        ):
            return
        try:
            notices = await asyncio.to_thread(
                self.store.pending_risk_notices, group, adapter.account
            )
        except Exception:
            return
        for notice in notices:
            inviter = notice["inviter"]
            member = members.get(inviter)
            peer = str(member.get("peer", "")) if member else ""
            if (
                not member
                or not peer.isascii()
                or not peer.isdigit()
                or not 0 < int(peer) < 1 << 32
                or adapter.members.get(group, {}).get(inviter) != peer
            ):
                continue
            nick = clean(member.get("name", ""), 48) or inviter
            mention = f"@{nick} "
            text = mention + "风控刷子，如果再次违规，直接拉黑，不予结账！"

            def authorize():
                self.check_selected(group)
                self.activities.authorize(adapter, group)
                if (
                    str(adapter.account) != str(self.store.get_group(group)["account"])
                    or adapter.members.get(group, {}).get(inviter) != peer
                ):
                    raise CashbackError(
                        "risk_notice_scope_changed",
                        "风控通知身份或群范围已变化。",
                        409,
                    )

            try:
                result = await adapter.send_text(
                    group,
                    notice["notice_key"],
                    text,
                    mentions=[
                        {
                            "uid": int(peer),
                            "nick": nick,
                            "start": 0,
                            "end": len(mention.encode("utf-16-le")) // 2,
                        }
                    ],
                    proactive=True,
                    authorize=authorize,
                )
                await asyncio.to_thread(
                    self.store.finish_risk_notice, notice["id"], result
                )
            except Exception as exc:
                await asyncio.to_thread(
                    self.store.operation_log,
                    "cashback_risk_notice",
                    group=group,
                    level="warning",
                    code="notice_send_unavailable",
                    bot_id=adapter.config.get("id", ""),
                    detail={"exception_type": type(exc).__name__},
                    sample_seconds=60,
                )

    async def configure(self, actor, config):
        self.store.users(actor)
        group = config["id"]
        self.check_selected(group)
        if config["bot_id"] != self.collector_id:
            raise CashbackError(
                "collector_not_selected", "只能使用指定的采集机器人。", 403
            )
        async with self.lock(group):
            old = self.store.get_group(group)
            if (
                old
                and not config["enabled"]
                and all(
                    config.get(key, old[key]) == old[key]
                    for key in (
                        "bot_id",
                        "contact" if "contact" in config else "cashier",
                        "rate",
                        "agent_rate",
                        "agent_only",
                        "qualification_seconds",
                        "require_speech",
                    )
                )
            ):
                return await asyncio.to_thread(
                    self.store.configure,
                    actor,
                    {**config, "account": old["account"], "name": old["name"]},
                    None,
                    set(),
                )
            adapter = self.adapter(config["bot_id"])
            # Capture changes at the old rate before repricing or changing bot.
            if (
                old
                and old["bot_id"] == config["bot_id"]
                and old["account"] == adapter.account
            ):
                await self._scan(group)
            account = adapter.account
            members = await self.roster(adapter, group)
            history = await asyncio.to_thread(self.historical_members, group)
            if adapter.account != account:
                raise CashbackError(
                    "collector_changed", "机器人账号变化，请重新核验。", 409
                )
            return await asyncio.to_thread(
                self.store.configure,
                actor,
                {**config, "account": account, "name": self.group_name(adapter, group)},
                members,
                history,
            )

    async def price_preview(self, actor, group, items, category):
        """Preview exact prices under the same group lock used by mutations.

        Args:
            actor: Authorized cashier principal.
            group: Exact group ID.
            items: Versioned entry selections.
            category: Target price category.

        Returns:
            Server-generated price confirmation.
        """
        self.check_selected(group)
        rule = await asyncio.to_thread(self.store.get_group, group)
        guard = self._lifecycle_guard(group, rule)
        guard()

        def preview():
            guard()
            result = self.store.price_preview(actor, group, items, category)
            guard()
            return result

        async with self.lock(group):
            return await asyncio.to_thread(preview)

    async def mutate(
        self,
        actor,
        group,
        operation,
        items,
        request_id,
        reason="",
        category=None,
        inviter=None,
        *,
        price_confirmation=None,
        actual_payment_confirmed=False,
    ):
        self.check_selected(group)
        rule = await asyncio.to_thread(self.store.get_group, group)
        guard = self._lifecycle_guard(group, rule)
        guard()
        async with self.lock(group):
            guard()
            prior = await asyncio.to_thread(
                self.store.prior_request,
                actor,
                group,
                operation,
                items,
                request_id,
                reason,
                category,
                inviter,
                price_confirmation=price_confirmation,
                actual_payment_confirmed=actual_payment_confirmed,
            )
            if prior is not None:
                return prior
            token = WRITE_GUARD.set(guard)
            try:
                members = None
                if operation in {"pay", "correct", "assign", "exception"}:
                    if self.store.get_group(group)["enabled"]:
                        members = await self._scan(group)
                    else:
                        members = await self._scan(group, for_payment=True)
                return await asyncio.to_thread(
                    self.store.mutate,
                    actor,
                    group,
                    operation,
                    items,
                    request_id,
                    reason,
                    category,
                    inviter,
                    members=members,
                    price_confirmation=price_confirmation,
                    actual_payment_confirmed=actual_payment_confirmed,
                )
            finally:
                WRITE_GUARD.reset(token)

    async def poll(self):
        """Schedule independent group scans with bounded upstream concurrency.

        A slow or unavailable group must not delay every healthy group's clock.
        Workers retry failures at 20, 40 and then 60 seconds; payment rechecks
        remain immediate and use the same per-group lock.
        """
        slots = asyncio.Semaphore(3)
        workers = {}
        last_log_failure = float("-inf")

        async def watch_group(group):
            failures = 0
            while True:
                try:
                    async with slots:
                        async with self.lock(group):
                            await self._scan(group)
                    failures = 0
                except Exception:
                    failures = min(failures + 1, 3)
                await asyncio.sleep(min(10 * 2**failures, 60))

        try:
            while True:
                try:
                    configured = await asyncio.to_thread(self.store.groups)
                    selected = self.selected_groups()
                except Exception as exc:
                    try:
                        await asyncio.to_thread(
                            self.store.operation_log,
                            "invitation_scheduler",
                            level="error",
                            code="scheduler_unavailable",
                            sample_seconds=300,
                            detail={"exception_type": type(exc).__name__},
                        )
                    except Exception:
                        now = time.monotonic()
                        if now - last_log_failure >= 60:
                            log.error(
                                "Cashback scheduler storage unavailable type=%s",
                                type(exc).__name__,
                            )
                            last_log_failure = now
                    await asyncio.sleep(10)
                    continue
                allowed = {
                    g["id"] for g in configured if g["enabled"] and g["id"] in selected
                }
                for group in list(workers):
                    if group not in allowed or workers[group].done():
                        task = workers.pop(group)
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                for group in sorted(allowed):
                    if group not in workers:
                        workers[group] = asyncio.create_task(watch_group(group))
                await asyncio.sleep(10)
        finally:
            for task in workers.values():
                task.cancel()
            await asyncio.gather(*workers.values(), return_exceptions=True)
