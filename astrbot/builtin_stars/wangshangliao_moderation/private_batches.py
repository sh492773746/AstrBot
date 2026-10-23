"""Bounded private AI batches, separate from native message acknowledgement."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

from astrbot.core.platform.sources.wangshangliao.database import account_guard

from .consultation_access import consultation_access
from .open_store import OpenError

WINDOW = 2


@dataclass
class Batch:
    id: str
    key: tuple
    event: object
    snapshot: str
    started: float
    fresh: bool
    messages: list = field(default_factory=list)
    task: object = None
    terminal: str = ""


class PrivateBatches:
    """Keep only transient text in memory and durable, non-replayable batch IDs."""

    def __init__(self, service):
        self.service, self.pending, self.running = service, {}, set()
        with service.store.connect(write=True) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS private_ai_batches("
                "id TEXT PRIMARY KEY,account TEXT,principal TEXT,messages TEXT,"
                "state TEXT,created REAL,updated REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS private_ai_sources("
                "account TEXT,principal TEXT,message_id TEXT,batch_id TEXT,"
                "PRIMARY KEY(account,principal,message_id))"
            )
            db.execute(
                "UPDATE private_ai_batches SET state=CASE WHEN state='buffered' "
                "THEN 'discarded' ELSE 'unknown' END,updated=? "
                "WHERE state IN ('buffered','dispatched')",
                (time.time(),),
            )

    def key(self, event):
        """Bind batching to native account, peer and explicit chat generation."""
        account, principal = str(event.platform.account), event.actor_identity.principal
        return (
            event.platform.config["id"],
            account,
            principal,
            self.service.chat_generations.get((account, principal), 0),
        )

    def snapshot(self, event):
        """Freeze role, persona, business data and account policy, without text."""
        service, account = self.service, str(event.platform.account)
        access = consultation_access(service, event)
        kind = access["kind"]
        scope = access.get("support_scope", account)
        profile = service.profiles.resolve(
            kind, scope if kind == "consultation" else ""
        )
        return json.dumps(
            {
                "access": access,
                "profile": profile["fingerprint"],
                "support": service.support.fingerprint(scope),
                "epoch": event.platform.config.get("_business_epoch"),
                "revision": event.platform.config.get("_business_revision"),
                "policy": service.governance.policy()["version"],
            },
            sort_keys=True,
        )

    def record(self, batch, state):
        """Persist identifiers and lifecycle only, never buffered message bodies."""
        with self.service.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO private_ai_batches VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET messages=excluded.messages,"
                "state=excluded.state,updated=excluded.updated",
                (
                    batch.id,
                    batch.key[1],
                    batch.key[2],
                    json.dumps([mid for mid, _ in batch.messages]),
                    state,
                    time.time(),
                    time.time(),
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO private_ai_sources VALUES(?,?,?,?)",
                [
                    (batch.key[1], batch.key[2], mid, batch.id)
                    for mid, _ in batch.messages
                ],
            )

    def cancel(self, event):
        """Discard an unissued batch before processing any fixed command."""
        key = self.key(event)
        batch = self.pending.pop(key, None)
        if batch is not None:
            batch.terminal = "discarded"
            batch.task.cancel()
            self.record(batch, "discarded")

    async def enqueue(self, event):
        """Acknowledge one fragment promptly and schedule at most one batch timer."""
        service = self.service
        if not service.governance.eligible(event.platform, event=event):
            return
        key, frozen = self.key(event), account_guard(event.platform)
        # Resolve a trusted peer before capturing an administrator guidance role.
        try:
            await service.verify_actor(event)
        except (ValueError, OpenError):
            pass
        frozen()
        if (
            self.key(event) != key
            or event.processing_completion.cancelled()
            or not service.active
            or not service.governance.eligible(event.platform, event=event)
        ):
            return
        access = consultation_access(service, event)
        if access["kind"] == "consultation" and access.get("business", {}).get(
            "unavailable"
        ):
            await service.reject_reply(
                event, "专属客服授权已变化，请重新发送“客服列表”核实。未调用AI。"
            )
            return
        if access["kind"] == "consultation" and access.get("business", {}).get(
            "ambiguous"
        ):
            await service.reject_reply(
                event, "请先发送“客服列表”，再发送“选择客服 序号”；未调用AI。"
            )
            return
        scope = access.get("support_scope", "")
        if scope.startswith("owner:"):
            await service.owner_support.ensure_profile(
                scope.removeprefix("owner:"),
                event.actor_identity.principal,
                guard=account_guard(event.platform),
            )
        frozen()
        if self.key(event) != key or event.processing_completion.cancelled():
            return
        stamp = self.snapshot(event)
        batch = self.pending.get(key)
        if batch is not None and batch.snapshot != stamp:
            self.cancel(event)
            batch = None
        mid = str(event.message_obj.message_id)
        if batch is not None and any(existing == mid for existing, _ in batch.messages):
            return
        with service.store.connect() as db:
            duplicate = db.execute(
                "SELECT 1 FROM private_ai_sources WHERE account=? AND principal=? "
                "AND message_id=?",
                (key[1], key[2], mid),
            ).fetchone()
        if duplicate:
            return
        if batch is None:
            if len(self.running) >= 8:
                await service.reject_reply(event, "AI咨询繁忙，请稍后再试。")
                return
            batch = Batch(
                hashlib.sha256(f"{key}/{mid}".encode()).hexdigest(),
                key,
                event,
                stamp,
                time.monotonic(),
                bool(event.get_extra("wsl_chat_fresh")),
            )
            self.pending[key] = batch
        batch.messages.append((mid, event.message_str))
        if (
            len(batch.messages) > 12
            or sum(len(text) for _, text in batch.messages) > 4096
        ):
            batch.terminal = "rejected"
            if batch.task is not None:
                batch.task.cancel()
            self.pending.pop(key, None)
            self.record(batch, "rejected")
            await service.reject_reply(event, "问题过长，请控制在4096字以内。")
            return
        self.record(batch, "buffered")
        if batch.task is None:
            batch.task = asyncio.create_task(self.flush(batch))
            self.running.add(batch.task)
            batch.task.add_done_callback(self.running.discard)

    async def flush(self, batch):
        """Recheck every boundary before one separately accounted AI execution."""
        event, service = batch.event, self.service
        original = event.message_str
        previous_guard = event.get_extra("_wsl_management_delivery_guard")
        previous_deadline = event.get_extra("_component_deadline")
        previous_batch_guard = event.get_extra("_component_batch_guard")
        frozen = account_guard(event.platform)
        state = "discarded"
        try:
            await asyncio.sleep(WINDOW)
            if self.pending.get(batch.key) is not batch:
                return
            self.pending.pop(batch.key)

            def guard():
                frozen()
                if (
                    not service.active
                    or not service.governance.eligible(event.platform, event=event)
                    or self.key(event) != batch.key
                    or self.snapshot(event) != batch.snapshot
                    or event.processing_completion.cancelled()
                    or time.monotonic() >= batch.started + 45
                ):
                    raise OpenError("ai_access_changed")
                if callable(previous_guard):
                    previous_guard()

            guard()
            event.message_str = "\n".join(text for _, text in batch.messages)
            event.set_extra("wsl_chat_fresh", batch.fresh)
            event.set_extra("_component_deadline", batch.started + 45)
            event.set_extra("_component_batch_guard", guard)
            event.set_extra("_wsl_management_delivery_guard", guard)
            self.record(batch, "dispatched")
            state = "unknown"
            await service.chat(event)
            receipt = event.get_extra("_wsl_native_reply_result")
            state = (
                "completed"
                if receipt in {"accepted", "verified"}
                else "not_sent"
                if receipt in {"not_sent", "rejected"}
                else "unknown"
            )
        except asyncio.CancelledError:
            raise
        except OpenError as exc:
            replies = {
                "personal_quota": "今日AI咨询额度已用完，请明天再试；固定命令不受影响。",
                "global_quota": "今日AI公共额度已用完；固定命令仍可使用。",
                "chat_cooldown": "请间隔5秒再发送问题。",
                "component_busy": "AI咨询繁忙，请稍后再试。",
                "component_scope_capacity": "AI咨询繁忙，请稍后再试。",
                "component_circuit_open": "AI咨询暂不可用，等待服务端核对；固定命令仍可使用。",
            }
            if str(exc) in replies or str(exc) == "request_duplicate":
                state = "not_sent"
                if str(exc) in replies:
                    try:
                        guard()
                        await service.reject_reply(event, replies[str(exc)])
                    except Exception:
                        pass
        except Exception:
            # A failed batch is terminal; never return its text to normal dispatch.
            pass
        finally:
            if self.pending.get(batch.key) is batch:
                self.pending.pop(batch.key)
            event.message_str = original
            event.set_extra("_component_deadline", previous_deadline)
            event.set_extra("_component_batch_guard", previous_batch_guard)
            event.set_extra("_wsl_management_delivery_guard", previous_guard)
            self.record(batch, batch.terminal or state)

    async def close(self):
        """Discard pending text and cancel tracked batches during plugin unload."""
        tasks = tuple(self.running)
        for batch in tuple(self.pending.values()):
            batch.terminal = "discarded"
            self.record(batch, "discarded")
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
