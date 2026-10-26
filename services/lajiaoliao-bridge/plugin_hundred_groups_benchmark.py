"""Exercise the real plugin handler across 100 isolated synthetic groups."""

import argparse
import asyncio
import copy
import json
import os
import resource
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ["ASTRBOT_DISABLE_METRICS"] = "1"

from astrbot.core.event_bus import EventBus
from astrbot.core.platform.platform_metadata import PlatformMetadata
from data.plugins.astrbot_plugin_lajiaoliao import runtime
from data.plugins.astrbot_plugin_lajiaoliao.ai import AIService
from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS, Businesses
from data.plugins.astrbot_plugin_lajiaoliao.commands import Commands
from data.plugins.astrbot_plugin_lajiaoliao.event import (
    LajiaoLiaoEvent,
    incoming_to_message,
)
from data.plugins.astrbot_plugin_lajiaoliao.main import LajiaoLiaoPlugin
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore, atomic_json


async def trial(output):
    original_runtime = runtime.active
    with tempfile.TemporaryDirectory() as directory:
        plugin = object.__new__(LajiaoLiaoPlugin)
        plugin.store = SessionStore(Path(directory))
        plugin.manager = SimpleNamespace(request=AsyncMock())
        plugin.logger = SimpleNamespace(warning=lambda *_: None)
        plugin._stopping = False
        refs, instances, configs = [], {}, []
        for account in range(2):
            ref = plugin.store.create("synthetic-owner")["session_ref"]
            uid, identifier = f"bot-{account}", f"instance-{account}"
            plugin.store.complete(ref, "synthetic-owner", uid)
            plugin.store.bind(ref, "synthetic-owner", identifier)
            config = {
                "type": "lajiaoliao",
                "id": identifier,
                "session_ref": ref,
                "enable": True,
                "message_policy": {
                    "group_mode": "allowlist",
                    "group_ids": [f"group-{account}-{group}" for group in range(50)],
                    "private_enabled": True,
                },
            }
            instance = SimpleNamespace(
                manager=plugin.manager,
                session_ref=ref,
                member_ids={},
                config=config,
                notifications=[],
                connection_state="online",
                meta=lambda identifier=identifier: PlatformMetadata(
                    "lajiaoliao", "synthetic", identifier
                ),
                send_parts=AsyncMock(
                    return_value={"status": "sent", "message_id": "mock"}
                ),
            )
            refs.append(ref)
            configs.append(config)
            instances[identifier] = instance
        plugin.context = SimpleNamespace(
            get_config=lambda umo=None: {
                "platform": configs,
                "admins_id": [],
                "provider_settings": {"enable": True},
            },
            get_platform_inst=instances.get,
            persona_manager=SimpleNamespace(
                get_persona_v3_by_id=lambda _: {"name": "mock"}
            ),
        )
        plugin.business = Businesses(plugin)
        plugin.commands = Commands(plugin)
        plugin.ai = AIService(plugin)
        plugin.manager.ai = plugin.ai
        runtime.active = plugin.manager
        try:
            for account, ref in enumerate(refs):
                for group in range(50):
                    value = copy.deepcopy(DEFAULTS)
                    value.update(rankings_enabled=True, group_ai_enabled=True)
                    await plugin.business.db(
                        ref,
                        "save_settings",
                        f"group-{account}-{group}",
                        0,
                        value,
                        "synthetic-owner",
                    )
            descriptors_before = len(list(Path("/proc/self/fd").iterdir()))
            reports = []
            for ai_enabled in (False, True):
                for account, ref in enumerate(refs):
                    for group in range(50):
                        current = await plugin.business.settings(
                            ref, f"group-{account}-{group}"
                        )
                        value = copy.deepcopy(current["settings"])
                        value["group_ai_enabled"] = ai_enabled
                        await plugin.business.db(
                            ref,
                            "save_settings",
                            f"group-{account}-{group}",
                            current["version"],
                            value,
                            "synthetic-owner",
                        )
                plugin.business.settings_cache.clear()
                queue = asyncio.Queue(maxsize=64)
                timings, model_calls = [], 0
                peak_queue, peak_database = 0, 0
                count = 2000
                companion_queue = asyncio.Queue(maxsize=24)
                companion_done = asyncio.Event()
                companion_timings = {"wangshangliao": [], "telegram": []}
                companion_completions = []
                companion_peak = 0

                async def companion_execute(event):
                    nonlocal companion_peak
                    companion_peak = max(
                        companion_peak, len(companion_bus._pending_tasks)
                    )
                    await asyncio.sleep(0.01)
                    companion_timings[event.platform_name].append(
                        (time.perf_counter() - event.queued_at) * 1000
                    )

                companion_bus = EventBus(
                    companion_queue,
                    dict.fromkeys(
                        companion_timings, SimpleNamespace(execute=companion_execute)
                    ),
                    SimpleNamespace(
                        get_conf_info=lambda origin: {"id": origin.split(":", 1)[0]}
                    ),
                )
                companion_bus._print_event = lambda *args: None
                dispatcher = asyncio.create_task(companion_bus.dispatch())

                async def companion_produce(name):
                    slots = asyncio.Semaphore(8)
                    arrival = time.perf_counter()
                    for _ in range(5000):
                        await asyncio.sleep(max(0, arrival - time.perf_counter()))
                        arrival += 0.01
                        if companion_done.is_set():
                            break
                        await slots.acquire()
                        completion = asyncio.get_running_loop().create_future()
                        completion.add_done_callback(lambda _: slots.release())
                        companion_completions.append(completion)
                        await companion_queue.put(
                            SimpleNamespace(
                                platform_name=name,
                                unified_msg_origin=name + ":synthetic",
                                processing_completion=completion,
                                queued_at=time.perf_counter(),
                            )
                        )

                companions = [
                    asyncio.create_task(companion_produce(name))
                    for name in companion_timings
                ]

                async def worker():
                    nonlocal model_calls, peak_database
                    while True:
                        row = await queue.get()
                        try:
                            if row is None:
                                return
                            index, queued_at = row
                            account, group = index % 2, (index // 2) % 50
                            text = "排名" if index % 20 == 0 else f"synthetic {index}"
                            event = LajiaoLiaoEvent(
                                incoming_to_message(
                                    {
                                        "message_id": f"phase-{ai_enabled}-{index}",
                                        "sender_id": f"ordinary-{index}",
                                        "sender_name": "Synthetic",
                                        "kind": "group",
                                        "target_id": f"group-{account}-{group}",
                                        "timestamp": time.time(),
                                        "text": text,
                                    },
                                    f"bot-{account}",
                                ),
                                instances[f"instance-{account}"],
                            )
                            event.is_at_or_wake_command = True
                            await plugin.group_businesses(event)
                            if event.is_at_or_wake_command and not event.is_stopped():
                                try:
                                    await event.prepare_ai_request(plugin.context)
                                    await event.authorize_ai_model_request("chat")
                                    model_calls += 1
                                    await asyncio.sleep(0.01)
                                    await event.record_ai_model_response(
                                        "chat", SimpleNamespace(usage=None)
                                    )
                                finally:
                                    await event.finish_ai_request()
                            peak_database = max(
                                peak_database, len(plugin.business.database_tasks)
                            )
                            timings.append((time.perf_counter() - queued_at) * 1000)
                        finally:
                            queue.task_done()

                workers = [asyncio.create_task(worker()) for _ in range(32)]
                started, cpu = time.perf_counter(), time.process_time()
                try:
                    for index in range(count):
                        await queue.put((index, time.perf_counter()))
                        peak_queue = max(peak_queue, queue.qsize())
                    await asyncio.wait_for(queue.join(), timeout=60)
                finally:
                    companion_done.set()
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    await asyncio.gather(*companions)
                    await asyncio.gather(*companion_completions)
                    dispatcher.cancel()
                    await asyncio.gather(dispatcher, return_exceptions=True)
                timings.sort()
                wall = time.perf_counter() - started
                reports.append(
                    {
                        "ai_enabled": ai_enabled,
                        "messages": count,
                        "groups": 100,
                        "accounts": 2,
                        "producers_inflight": 32,
                        "synthetic_model_ms": 10,
                        "ai_global_concurrency_limit": 8,
                        "ai_instance_concurrency_limit": 4,
                        "model_calls": model_calls,
                        "commands": count // 20,
                        "wall_seconds": round(wall, 3),
                        "throughput_messages_s": round(count / wall, 1),
                        "cpu_seconds": round(time.process_time() - cpu, 3),
                        "queue_included_p95_ms": round(timings[int(count * 0.95)], 3),
                        "queue_included_max_ms": round(max(timings), 3),
                        "peak_queue": peak_queue,
                        "queue_limit": 64,
                        "peak_database_tasks": peak_database,
                        "database_limit": 64,
                        "companion_event_bus": {
                            "arrival_interval_ms": 10,
                            "synthetic_model_ms": 10,
                            "peak_pending": companion_peak,
                            "messages": {
                                name: len(rows)
                                for name, rows in companion_timings.items()
                            },
                            "p95_ms": {
                                name: round(sorted(rows)[int(len(rows) * 0.95)], 3)
                                for name, rows in companion_timings.items()
                            },
                        },
                    }
                )
                assert model_calls == (1900 if ai_enabled else 0)
            await plugin.business.close()
            descriptors_after = len(list(Path("/proc/self/fd").iterdir()))
            assert descriptors_before == descriptors_after
            assert not plugin.business.database_tasks
            assert not plugin.business.account_pollers
            plugin.manager.request.assert_not_awaited()
            report = {
                "mode": "offline_actual_group_business_handler",
                "trials": reports,
                "fd_before": descriptors_before,
                "fd_after": descriptors_after,
                "database_peak": plugin.business.database_peak,
                "process_peak_rss_kib": resource.getrusage(
                    resource.RUSAGE_SELF
                ).ru_maxrss,
                "settings_cache_entries": len(plugin.business.settings_cache),
                "live_messages_sent": False,
                "companion_tolerance": {
                    name: {
                        "change_ms": round(
                            reports[1]["companion_event_bus"]["p95_ms"][name]
                            - reports[0]["companion_event_bus"]["p95_ms"][name],
                            3,
                        ),
                        "passed": reports[1]["companion_event_bus"]["p95_ms"][name]
                        - reports[0]["companion_event_bus"]["p95_ms"][name]
                        <= max(
                            100, reports[0]["companion_event_bus"]["p95_ms"][name] * 0.1
                        ),
                    }
                    for name in ("wangshangliao", "telegram")
                },
                "limits": "Two temporary accounts, 100 total groups, real handler/SQLite/ranking/commands; model and transport mocked. Not a 100-group production SLA or long-stability acceptance.",
            }
            atomic_json(output, report)
            print(json.dumps(report, indent=2))
        finally:
            await plugin.business.close()
            runtime.active = original_runtime


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(trial(parser.parse_args().output))
