"""Scoped, coalesced welcomes backed by verified native admission evidence."""

import asyncio
import hashlib
import json
import secrets
import threading
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from astrbot.core.platform.sources.wangshangliao.adapter import _text_message
from astrbot.core.platform.sources.wangshangliao.blacklist import BlacklistStore
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.invitations import (
    latest_membership_events,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .open_store import OpenError


class WelcomeConfig(BaseModel):
    """One opt-in group greeting; mentions and recall timing are server-owned."""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    text: str = Field(default="欢迎加入本群！", max_length=300)

    @model_validator(mode="after")
    def content(self):
        """Require a nonempty greeting when enabled."""
        if self.enabled and not self.text.strip():
            raise ValueError("welcome_text_required")
        if any(ord(c) < 32 and c not in "\n\t" for c in self.text):
            raise ValueError("welcome_text_invalid")
        return self


class WelcomeMessages:
    """Bound admission batches independently of chat, OCR and sanction workers."""

    def __init__(self, service):
        self.service, self.store = service, service.store
        self.configs = {}
        self.config_lock = threading.Lock()
        self.pending = {}
        self.tasks = {}
        self.active = True
        self.started = time.time()
        self.pruned_at = 0
        with self.store.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS welcome_config(
                    account TEXT,group_id TEXT,version INTEGER,epoch INTEGER,
                    enabled_at REAL,config TEXT,PRIMARY KEY(account,group_id));
                CREATE TABLE IF NOT EXISTS welcome_drafts(
                    token TEXT PRIMARY KEY,account TEXT,group_id TEXT,actor TEXT,
                    session TEXT,version INTEGER,tenant_version INTEGER,epoch INTEGER,
                    config TEXT,expires REAL,used INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS welcome_admissions(
                    account TEXT,group_id TEXT,member TEXT,join_id TEXT,
                    operation TEXT,created REAL,state TEXT,
                    PRIMARY KEY(account,group_id,member,join_id));
                CREATE INDEX IF NOT EXISTS welcome_admissions_created
                    ON welcome_admissions(created);
            """)
            db.execute(
                "UPDATE welcome_admissions SET state='unknown' WHERE state='dispatched'"
            )
            for row in db.execute("SELECT * FROM welcome_config"):
                self.configs[(row["account"], row["group_id"])] = {
                    **dict(row),
                    "config": json.loads(row["config"]),
                }

    def control(self, payload, actor, session, tenant, guard):
        """Read, freeze or commit only a caller-bound group configuration.

        Args:
            payload: Validated explicit dashboard operation.
            actor: Authenticated dashboard actor.
            session: Original dashboard session.
            tenant: Exact current tenant snapshot.
            guard: Current native control authority checked before commit.

        Returns:
            Scoped configuration or expiring preview, never a sent message.

        Raises:
            OpenError: If arguments, scope, confirmation or versions changed.
        """
        account, group = tenant["account"], tenant["group_id"]
        common = {"account", "group_id", "tenant_version", "operation"}
        operation = payload.get("operation")
        expected = common | {
            "read": set(),
            "preview": {"version", "config"},
            "commit": {"token"},
        }.get(operation, {"invalid"})
        if set(payload) != expected:
            raise OpenError("welcome_arguments")
        with self.store.connect(write=operation != "read", guard=guard) as db:
            row = db.execute(
                "SELECT * FROM welcome_config WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            saved = (
                {**dict(row), "config": json.loads(row["config"])}
                if row
                else {"version": 0, "config": WelcomeConfig().model_dump()}
            )
            if operation == "read":
                last = db.execute(
                    "SELECT operation,state,created FROM welcome_admissions "
                    "WHERE account=? AND group_id=? ORDER BY created DESC LIMIT 1",
                    (account, group),
                ).fetchone()
                return {
                    **saved,
                    "last": dict(last) if last else None,
                    "authorization_required": bool(
                        row and row["epoch"] != tenant["epoch"]
                    ),
                }
            if operation == "preview":
                if (
                    type(payload["version"]) is not int
                    or payload["version"] != saved["version"]
                ):
                    raise OpenError("welcome_changed")
                config = WelcomeConfig.model_validate(payload["config"]).model_dump()
                token = secrets.token_urlsafe(18)
                db.execute("DELETE FROM welcome_drafts WHERE expires<?", (time.time(),))
                db.execute(
                    "INSERT INTO welcome_drafts VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        token,
                        account,
                        group,
                        actor,
                        session,
                        saved["version"],
                        tenant["version"],
                        tenant["epoch"],
                        json.dumps(config),
                        time.time() + 120,
                    ),
                )
                return {"token": token, "config": config, "expires_in": 120}
            if operation != "commit" or not isinstance(payload["token"], str):
                raise OpenError("welcome_arguments")
            draft = db.execute(
                "SELECT * FROM welcome_drafts WHERE token=?", (payload["token"],)
            ).fetchone()
            if (
                not draft
                or draft["expires"] <= time.time()
                or (
                    draft["account"],
                    draft["group_id"],
                    draft["actor"],
                    draft["session"],
                )
                != (account, group, actor, session)
            ):
                raise OpenError("welcome_confirmation_invalid")
            if draft["used"]:
                return {"saved": True, "version": draft["version"] + 1}
            if (
                draft["version"] != saved["version"]
                or draft["tenant_version"] != tenant["version"]
                or draft["epoch"] != tenant["epoch"]
                or self.store.tenant(account, group) != tenant
            ):
                raise OpenError("welcome_changed")
            updated = {
                "account": account,
                "group_id": group,
                "version": saved["version"] + 1,
                "epoch": tenant["epoch"],
                "enabled_at": time.time(),
                "config": json.loads(draft["config"]),
            }
            db.execute(
                "INSERT INTO welcome_config VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(account,group_id) DO UPDATE SET version=excluded.version,"
                "epoch=excluded.epoch,enabled_at=excluded.enabled_at,config=excluded.config",
                (
                    account,
                    group,
                    updated["version"],
                    updated["epoch"],
                    updated["enabled_at"],
                    draft["config"],
                ),
            )
            db.execute(
                "UPDATE welcome_drafts SET used=1 WHERE token=?", (draft["token"],)
            )
            self.store.log(
                db,
                actor,
                account,
                group,
                "welcome_saved",
                {
                    "version": updated["version"],
                    "enabled": updated["config"]["enabled"],
                },
            )
        with self.config_lock:
            if (
                self.configs.get((account, group), {}).get("version", 0)
                < updated["version"]
            ):
                self.configs[(account, group)] = updated
        return {"saved": True, "version": updated["version"]}

    def enabled(self, adapter, group):
        """Check local opt-in and runtime fences without IO on incoming notices."""
        account = str(adapter.account)
        key = (account, group)
        tenant = self.service.tenants.get(key, {})
        row = self.configs.get(key, {})
        return bool(
            self.active
            and self.service.active
            and getattr(adapter, "supports_welcome_recall", False) is True
            and row.get("config", {}).get("enabled")
            and row.get("epoch") == tenant.get("epoch")
            and not self.service.settings["paused"]
            and not self.service.capacity_paused
            and tenant.get("active")
            and tenant.get("present")
            and not tenant.get("paused")
            and tenant.get("admission", "legacy") in {"legacy", "verified"}
            and tenant.get("cleanup_epoch", 0) <= tenant.get("cleanup_done", 0)
            and group not in self.service.retired_groups
            and tenant.get("native_role") in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
            and key not in self.service.suspended_groups
            and not self.service.membership_receiving.get(key)
            and not self.service.reconciling_groups.get(key)
            and adapter.config.get("enable", True)
            and adapter.config.get("_business_active")
            and group in adapter.enabled_groups
            and adapter.config.get("reply_groups", {}).get(group, True)
            and adapter.connection_state == "online"
            and not adapter.stopping.is_set()
        )

    def receive(self, adapter, group, mid, notice):
        """Coalesce trusted native notices; never perform IO in the ACK lane."""
        if not self.enabled(adapter, group):
            return
        key = (str(adapter.account), group)
        stamp = notice.get("event_at")
        if (
            type(stamp) is not int
            or not max(self.started, self.configs[key]["enabled_at"], time.time() - 30)
            <= stamp / 1000
            <= time.time() + 2
            or not isinstance(notice.get("member_peers"), list)
            or not 1 <= len(notice["member_peers"]) <= 200
        ):
            return
        if key not in self.pending and len(self.pending) >= 32:
            adapter.diagnostics.emit("welcome", "queue_full", group=group, failed=True)
            return
        batch = self.pending.setdefault(key, {})
        if len(batch) < 64:
            batch.setdefault(str(mid), dict(notice))
        self.tick()

    def tick(self):
        """Run at most four independent group batches, never one task per member."""
        for key, task in list(self.tasks.items()):
            if task.done():
                if not task.cancelled():
                    task.exception()
                del self.tasks[key]
        adapters = {str(a.account): a for a in self.service.adapters()}
        for key in list(self.pending):
            if key in self.tasks or len(self.tasks) >= 4:
                continue
            adapter = adapters.get(key[0])
            if adapter is None or not self.enabled(adapter, key[1]):
                self.pending.pop(key, None)
                continue
            self.tasks[key] = asyncio.create_task(self.send(adapter, key[1]))

    async def send(self, adapter, group):
        """Corroborate a bounded join batch, reserve once, then send one greeting.

        Args:
            adapter: Authenticated original receiving transport.
            group: Exact business group from a native notification.
        """
        account, key = str(adapter.account), (str(adapter.account), group)
        frozen = account_guard(adapter)
        row = self.configs[key]
        native = (
            adapter.ledger,
            adapter.nim,
            adapter.business,
            adapter.groups.get(group),
        )

        def guard():
            frozen()
            if (
                not self.enabled(adapter, group)
                or self.configs.get(key) != row
                or native
                != (
                    adapter.ledger,
                    adapter.nim,
                    adapter.business,
                    adapter.groups.get(group),
                )
            ):
                raise OpenError("welcome_changed")

        operation = ""
        state = "rejected"
        try:
            await asyncio.sleep(2)
            guard()
            notices = self.pending.pop(key, {})
            notified = {}
            for notice in notices.values():
                for peer in notice["member_peers"]:
                    notified.setdefault(peer, set()).add(notice["event_at"] / 1000)
            candidates = {}
            # Application logs can briefly lag the native notification. Only reads
            # are retried, within the same fixed freshness window and batch.
            for attempt in range(3):
                guard()
                members = await self.service.roster(adapter, group)
                response = await adapter.business.request(
                    "/v1/group/get-apply-logs", {}
                )
                events = await asyncio.to_thread(
                    latest_membership_events, response, group
                )
                guard()
                candidates = {}
                for uid, evidence in events.items():
                    event, member = evidence["row"], members.get(uid, {})
                    peer = str(member.get("nimId", ""))
                    if (
                        peer not in notified
                        or evidence["ambiguous"]
                        or is_managed_account(uid)
                        or uid == account
                        or event.get("state") != "MEMBER_STATE_GOOD"
                        or event.get("groupApplyType") not in {"INVITATION", "APPLY"}
                        or str(event["applicant"].get("nimId", "")) != peer
                        or adapter.members.get(group, {}).get(uid) != peer
                        or not peer.isascii()
                        or not peer.isdigit()
                        or not 0 < int(peer) < 1 << 32
                        or not max(self.started, row["enabled_at"], time.time() - 30)
                        <= evidence["timestamp"]
                        <= time.time()
                    ):
                        continue
                    if any(
                        abs(evidence["timestamp"] - stamp) <= 120
                        for stamp in notified.get(peer, ())
                    ):
                        candidates[uid] = (str(event["applyId"]), peer, member)
                resolved = {item[1] for item in candidates.values()}
                if notified.keys() <= resolved or attempt == 2:
                    break
                await asyncio.sleep(2)
            if not candidates:
                return
            blacklist = await asyncio.to_thread(BlacklistStore, adapter.config["id"])
            _, blocked = await asyncio.to_thread(blacklist.read, account, group)
            blocked = {str(item["member"]) for item in blocked}
            candidates = {
                uid: item for uid, item in candidates.items() if uid not in blocked
            }
            guard()

            def reserve():
                with self.store.connect(write=True, guard=guard) as db:
                    fresh = {
                        uid: item
                        for uid, item in candidates.items()
                        if not db.execute(
                            "SELECT 1 FROM welcome_admissions WHERE account=? AND group_id=? "
                            "AND member=? AND join_id=?",
                            (account, group, uid, item[0]),
                        ).fetchone()
                    }
                    digest = hashlib.sha256(
                        json.dumps(
                            sorted((uid, item[0]) for uid, item in fresh.items())
                        ).encode()
                    ).hexdigest()[:32]
                    op = f"welcome/{account}/{group}/{digest}"
                    db.executemany(
                        "INSERT INTO welcome_admissions VALUES(?,?,?,?,?,?,'dispatched')",
                        [
                            (account, group, uid, item[0], op, time.time())
                            for uid, item in fresh.items()
                        ],
                    )
                    if time.time() - self.pruned_at > 3600:
                        db.execute(
                            "DELETE FROM welcome_admissions WHERE created<? AND state NOT IN ('dispatched','unknown')",
                            (time.time() - 86400,),
                        )
                        self.pruned_at = time.time()
                    return op, fresh

            operation, candidates = await asyncio.to_thread(reserve)
            if not candidates:
                operation = ""
                return
            text, mentions = "", []
            # A single protocol message is mandatory even for a very large batch.
            # Mention the bounded prefix; retain the full batch for deduplication.
            for uid, (_, peer, member) in list(candidates.items())[:20]:
                nick = (
                    str(member.get("userNick") or uid)
                    .replace("\n", " ")
                    .replace("\r", " ")[:32]
                )
                start = len(text.encode("utf-16-le")) // 2
                candidate_text = text + f"@{nick} "
                candidate_mentions = [
                    *mentions,
                    {
                        "uid": int(peer),
                        "nick": nick,
                        "start": start,
                        "end": len(candidate_text.encode("utf-16-le")) // 2,
                    },
                ]
                if (
                    _text_message(
                        adapter.config["id"],
                        account,
                        group,
                        operation,
                        candidate_text
                        + "\n"
                        + row["config"]["text"]
                        + f"\n共{len(candidates)}位新成员",
                        candidate_mentions,
                        False,
                        (1 << 63) - 1,
                    ).ByteSize()
                    > 4096
                ):
                    break
                text, mentions = candidate_text, candidate_mentions
            text += ("\n" if text else "") + row["config"]["text"]
            if len(candidates) > len(mentions):
                text += f"\n共{len(candidates)}位新成员"
            if (
                _text_message(
                    adapter.config["id"],
                    account,
                    group,
                    operation,
                    text,
                    mentions,
                    False,
                    (1 << 63) - 1,
                ).ByteSize()
                > 4096
            ):
                raise OpenError("welcome_message_limit")

            def authorize():
                guard()
                current = self.service.member_evidence.get(key, {})
                if (
                    self.service.capabilities.get(key, {}).get("checked_at", 0)
                    < time.time() - 60
                    or current.get(account, {}).get("groupRole")
                    not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
                    or str(current.get(account, {}).get("nimId", ""))
                    != str(adapter.nim_account)
                    or any(
                        adapter.members.get(group, {}).get(uid) != item[1]
                        for uid, item in candidates.items()
                    )
                ):
                    raise OpenError("welcome_identity_changed")

            authorize()
            state = "unknown"
            state = await adapter.send_text(
                group,
                operation,
                text,
                mentions=mentions,
                welcome_recall=True,
                authorize=authorize,
            )
            if state not in {"accepted", "rejected"}:
                state = "unknown"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            adapter.diagnostics.emit(
                "welcome",
                state,
                operation,
                group=group,
                failed=True,
                error=str(exc)
                if isinstance(exc, (OpenError, ProtocolError))
                else "operation_failed",
            )
        finally:
            if operation:

                def finish():
                    with self.store.connect(write=True) as db:
                        db.execute(
                            "UPDATE welcome_admissions SET state=? WHERE operation=?",
                            (state, operation),
                        )

                await asyncio.to_thread(finish)

    async def close(self):
        """Stop local batches; durable native recall timers survive plugin reload."""
        self.active = False
        self.pending.clear()
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
