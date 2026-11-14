"""Opt-in CMS polling, one bounded publication per account at a time."""

import asyncio
import re
import sqlite3
import time

from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .settings import business_revision
from .video_relay import database, saved_config
from .video_relay_delivery import (
    delivery_control,
    expire_previews,
    refresh_native_evidence,
)


class VideoRelayRuntime:
    """Run only confirmed CMS schedules, independently of message processing."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.active = False
        self.tasks = {}
        self.recovered = set()

    async def tick(self):
        """Recover stale work and launch at most one due job per native instance."""
        for bot_id, task in tuple(self.tasks.items()):
            if task.done():
                if not task.cancelled():
                    task.exception()
                del self.tasks[bot_id]
        for adapter in tuple(self.plugin.context.platform_manager.platform_insts):
            if adapter.meta().name != "wangshangliao":
                continue
            bot_id = adapter.meta().id
            path = instance_dir(bot_id) / "video-relay-control.sqlite3"
            if bot_id in self.tasks or not path.exists():
                continue
            await expire_previews(path)
            if bot_id not in self.recovered:
                async with AsyncDatabase(lambda: database(path), write=True) as db:
                    await db.execute(
                        "UPDATE relay_schedules SET state='blocked',error='process_interrupted' "
                        "WHERE state='running'"
                    )
                self.recovered.add(bot_id)
            else:
                # The first tick after a reload fences claimed work. Once no
                # preparation/upload/send is still active, the interrupted claim
                # is safe to resume: publication keys remain the final duplicate
                # fence, and the attempt is re-run rather than blindly re-sent.
                async with AsyncDatabase(lambda: database(path), write=True) as db:
                    await db.execute(
                        "UPDATE relay_schedules SET state='scheduled',error='',"
                        "next_at=? WHERE account=? AND state='blocked' "
                        "AND error='process_interrupted' AND NOT EXISTS(SELECT 1 "
                        "FROM relay_video_jobs j WHERE j.account=relay_schedules.account "
                        "AND j.group_id=relay_schedules.group_id "
                        "AND j.state IN ('preparing','prepared','uploading','sending'))",
                        (time.time(), str(adapter.account)),
                    )
            async with AsyncDatabase(lambda: database(path), write=True) as db:
                # Publication keys outlive recent job details and local video bytes.
                await db.execute(
                    "DELETE FROM relay_video_jobs WHERE id IN (SELECT id FROM relay_video_jobs "
                    "WHERE cleanup_state='deleted' AND (updated<? OR id NOT IN "
                    "(SELECT id FROM relay_video_jobs ORDER BY created DESC LIMIT 2048)) AND state NOT IN "
                    "('preparing','prepared','uploading','sending') LIMIT 128)",
                    (time.time() - 7 * 86400,),
                )
                if adapter.connection_state != "online" or not adapter.config.get(
                    "enable", True
                ):
                    continue
                row = await db.fetchone(
                    "SELECT s.*,c.config FROM relay_schedules s JOIN relay_configs c "
                    "ON c.account=s.account AND c.group_id=s.group_id AND c.version=s.version "
                    "WHERE s.account=? AND s.state='scheduled' AND s.next_at<=? "
                    "AND (COALESCE(json_extract(c.config,'$.follow_group_id'),'')='' "
                    "OR EXISTS(SELECT 1 FROM relay_follow_starts f "
                    "JOIN relay_publications p ON p.account=f.account "
                    "AND p.group_id=f.source_group_id AND p.created>=f.since "
                    "WHERE f.account=s.account AND f.group_id=s.group_id "
                    "AND p.source=json_extract(c.config,'$.cms_url') "
                    "AND p.state='accepted' AND NOT EXISTS(SELECT 1 "
                    "FROM relay_publications t WHERE t.account=s.account "
                    "AND t.group_id=s.group_id AND "
                    "((t.source=p.source AND t.source_id=p.source_id) OR t.digest=p.digest)))) "
                    "ORDER BY s.next_at LIMIT 1",
                    (str(adapter.account), time.time()),
                )
                if row is None or not saved_config(row["config"]).enabled:
                    continue
                if not self.active:
                    return
                await db.execute(
                    "UPDATE relay_schedules SET state='running' WHERE account=? AND group_id=?",
                    (row["account"], row["group_id"]),
                )
            self.tasks[bot_id] = asyncio.create_task(
                self.deliver(adapter, path, dict(row))
            )

    async def deliver(self, adapter, path, row):
        """Prepare and publish one scheduled item through the same durable controls.

        Args:
            adapter: Existing native platform instance.
            path: Private per-instance video database.
            row: Claimed schedule and frozen CMS configuration.
        """
        bot_id, account, group = (
            adapter.meta().id,
            str(adapter.account),
            row["group_id"],
        )
        revision = business_revision(adapter.config)
        native_guard = account_guard(adapter)
        service = getattr(self.plugin, "open_service", None)
        state, error = "scheduled", ""
        preview = None
        config = saved_config(row["config"])

        def guard():
            native_guard()
            if (
                not self.active
                or not self.plugin.dashboard_page.active
                or not any(
                    current is adapter
                    for current in self.plugin.context.platform_manager.platform_insts
                )
                or str(adapter.account) != account
                or adapter.meta().id != bot_id
                or adapter.connection_state != "online"
                or not adapter.config.get("enable", True)
                or group not in adapter.config.get("enabled_groups", [])
                or config.follow_group_id
                and config.follow_group_id
                not in adapter.config.get("enabled_groups", [])
                or business_revision(adapter.config) != revision
            ):
                raise ProtocolError("video_schedule_revoked")

        try:
            guard()
            if service is None or not service.active:
                raise ProtocolError("video_service_inactive")
            rules = service.rule_replies
            try:
                async with asyncio.timeout(15):
                    await refresh_native_evidence(service, adapter, group, guard)
            except TimeoutError:
                raise ProtocolError("video_permission_timeout") from None
            rule = await asyncio.to_thread(rules.read, account, group)
            if not rule.get("version"):
                raise ProtocolError("video_rules_unsaved")

            async def posting_guard():
                guard()
                await asyncio.to_thread(rules.send_guard, adapter, rule, posting=True)
                guard()

            body = {
                "operation": "preview_video",
                "version": row["version"],
                "revision": revision,
            }
            preview = await delivery_control(
                path,
                adapter,
                group,
                "cms-schedule:" + bot_id,
                revision,
                body,
                guard,
                posting_guard,
                refresh_authority=lambda: refresh_native_evidence(
                    service, adapter, group, guard
                ),
            )
            result = await delivery_control(
                path,
                adapter,
                group,
                "cms-schedule:" + bot_id,
                revision,
                {**body, "operation": "send_video", "token": preview["token"]},
                guard,
                posting_guard,
                refresh_authority=lambda: refresh_native_evidence(
                    service, adapter, group, guard
                ),
            )
            if result["state"] != "accepted":
                reason = result.get("error") or result["state"]
                # Content-level skips are expected in mixed categories: continue
                # on the next interval instead of pausing the whole schedule.
                if reason in {
                    "video_size_limit",
                    "video_media_invalid",
                    "video_format_unsupported",
                    "video_duration_limit",
                    "video_source_limit",
                    "video_prepare_timeout",
                    "tmzyz_gateway_insufficient",
                    "tmzyz_gateway_response_limit",
                    "video_manifest_invalid",
                    "video_pagination_invalid",
                    "video_operation_failed",
                }:
                    state, error = "scheduled", reason
                else:
                    state, error = "blocked", reason
        except asyncio.CancelledError:
            state, error = "blocked", "process_interrupted"
            raise
        except Exception as cause:
            value = getattr(cause, "data", None)
            value = value.get("code", "") if isinstance(value, dict) else str(cause)
            error = (
                value
                if re.fullmatch(r"[a-z_]{1,64}", value)
                else "video_cms_unavailable"
            )
            if error in {
                "video_cms_unavailable",
                "video_already_published",
                "video_size_limit",
                "video_media_invalid",
                "video_format_unsupported",
                "video_duration_limit",
                "video_source_limit",
                "video_prepare_timeout",
                "tmzyz_gateway_insufficient",
                "tmzyz_gateway_response_limit",
                "video_manifest_invalid",
                "video_pagination_invalid",
                "video_operation_failed",
            }:
                state = "scheduled"
            else:
                state = "blocked"
        finally:
            if preview is not None:
                # If shutdown/config changes happened between preview and commit,
                # release the still-unsubmitted preview without widening send grants.
                async with AsyncDatabase(lambda: database(path), write=True) as db:
                    await db.execute(
                        "UPDATE relay_video_jobs SET expires=0 WHERE id=? AND state='prepared'",
                        (preview["id"],),
                    )
                await expire_previews(path)
            async with AsyncDatabase(lambda: database(path), write=True) as db:
                await db.execute(
                    "UPDATE relay_schedules SET state=?,error=?,next_at=? WHERE "
                    "account=? AND group_id=? AND version=? AND state='running'",
                    (
                        state,
                        error,
                        time.time()
                        + (
                            5
                            if config.follow_group_id
                            else config.interval_minutes * 60
                        ),
                        account,
                        group,
                        row["version"],
                    ),
                )

    async def run(self):
        """Poll persisted opt-in schedules without holding message-processing locks."""
        self.active = True
        while self.active:
            try:
                await self.tick()
            except (OSError, ValueError, sqlite3.Error):
                pass
            await asyncio.sleep(5)

    async def close(self):
        """Fence pending actions and await cleanup of this runtime's workers."""
        self.active = False
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
