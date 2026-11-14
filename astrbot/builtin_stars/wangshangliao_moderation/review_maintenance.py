"""Low-priority, bounded retention outside the message and sanction workers."""

import asyncio
import sqlite3

from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .review_store import ReviewStore, review_store


class ReviewMaintenance:
    """Maintain only existing account-bound stores, with drainable shutdown."""

    def __init__(self, context):
        self.context = context
        self.active = True
        self.stopping = asyncio.Event()
        self.inflight = None

    async def initialize(self):
        """Prepare existing indexes before group automation is activated."""
        adapters = list(self.context.platform_manager.platform_insts)
        sources = [(adapter, getattr(adapter, "config", {})) for adapter in adapters]
        loaded = {config.get("id") for _, config in sources}
        # Plugins initialize before platform instances on a full process start.
        sources.extend(
            (None, config)
            for config in getattr(self.context, "get_config", lambda: {})().get(
                "platform", []
            )
            if isinstance(config, dict) and config.get("id") not in loaded
        )
        for adapter, config in sources:
            account = (
                str(getattr(adapter, "account", ""))
                if adapter is not None
                else str(config.get("account_id", ""))
            )
            if (
                config.get("type") != "wangshangliao"
                or not isinstance(config.get("id"), str)
                or not config["id"]
                or not account
            ):
                continue
            identity = (config["id"], config.get("account_id"))
            path = instance_dir(config["id"]) / "moderation.sqlite3"
            if path.is_symlink() or not path.is_file():
                continue
            guard = account_guard(adapter) if adapter is not None else None
            if guard:
                guard()
            store = (
                review_store(adapter, path)
                if adapter is not None
                else ReviewStore(path)
            )
            self.inflight = asyncio.create_task(asyncio.to_thread(store.initialize))
            try:
                await asyncio.shield(self.inflight)
            except asyncio.CancelledError:
                self.active = False
                while not self.inflight.done():
                    try:
                        await asyncio.shield(self.inflight)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if self.inflight.done() and not self.inflight.cancelled():
                    self.inflight.exception()
                raise
            finally:
                self.inflight = None
            if not self.active or identity != (
                config.get("id"),
                config.get("account_id"),
            ):
                raise ProtocolError("database_authority_changed")
            if guard:
                guard()

    async def step(self):
        """Yield to business writers and never initialize an unused store."""
        for adapter in list(self.context.platform_manager.platform_insts):
            config = getattr(adapter, "config", {})
            store = getattr(adapter, "_review_store", None)
            account = str(getattr(adapter, "account", ""))
            if (
                not self.active
                or config.get("type") != "wangshangliao"
                or not config.get("id")
                or not account
                or not isinstance(store, ReviewStore)
            ):
                continue
            try:
                if store.path != instance_dir(config["id"]) / "moderation.sqlite3":
                    continue
            except (OSError, ValueError, ProtocolError):
                adapter.diagnostics.emit("review_maintenance", "deferred")
                continue

            def authorize():
                if not self.active:
                    raise ProtocolError("review_maintenance_stopped")

            guard = account_guard(adapter, authorize)
            self.inflight = asyncio.create_task(
                asyncio.to_thread(store.prune, account=account, limit=128, guard=guard)
            )
            try:
                result = await asyncio.shield(self.inflight)
                gauges = getattr(adapter.diagnostics, "gauges", {})
                gauges["review_cleanup_deleted"] = gauges.get(
                    "review_cleanup_deleted", 0
                ) + sum(result.values())
                gauges["review_cleanup_scanned"] = store.retention_stats["scanned"]
                gauges["review_cleanup_migration_required"] = int(
                    store.retention_stats["skipped"] == "unindexed"
                )
                if store.retention_stats["skipped"] in {"busy", "budget"}:
                    adapter.diagnostics.emit("review_maintenance", "deferred")
            except asyncio.CancelledError:
                self.active = False
                while not self.inflight.done():
                    try:
                        await asyncio.shield(self.inflight)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if self.inflight.done() and not self.inflight.cancelled():
                    self.inflight.exception()
                raise
            except ProtocolError:
                adapter.diagnostics.emit("review_maintenance", "deferred")
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", 0) & 255 in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                    sqlite3.SQLITE_INTERRUPT,
                }:
                    adapter.diagnostics.emit("review_maintenance", "deferred")
                else:
                    adapter.diagnostics.emit(
                        "review_maintenance",
                        "failed",
                        failed=True,
                        error="operation_failed",
                    )
            except Exception:
                adapter.diagnostics.emit(
                    "review_maintenance",
                    "failed",
                    failed=True,
                    error="operation_failed",
                )
            finally:
                self.inflight = None

    async def run(self):
        """Run one small batch per minute, independently of ingress."""
        while self.active:
            await self.step()
            try:
                await asyncio.wait_for(self.stopping.wait(), 60)
            except TimeoutError:
                pass

    async def close(self):
        """Revoke cleanup and wait for any worker transaction to settle."""
        self.active = False
        self.stopping.set()
        task = self.inflight
        if task is not None:
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
