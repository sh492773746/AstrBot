"""Bounded synthetic business benchmark; never connects or sends real messages."""

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


def percentile(values, p):
    return sorted(values)[min(len(values) - 1, int(len(values) * p))]


async def shared_bus_trial(commands, prototype, label):
    """Measure the real bus with bounded synthetic producers and a 10 ms model."""
    from astrbot.core.event_bus import EventBus

    platforms = ("lajiaoliao", "wangshangliao", "telegram")
    queue = asyncio.Queue(maxsize=24)
    manager = SimpleNamespace(
        get_conf_info=lambda origin: {"id": origin.split(":", 1)[0]}
    )
    timings = {name: [] for name in platforms}
    completions = []
    peak_pending = 0
    peak_queue = 0

    async def execute(event):
        nonlocal peak_pending
        peak_pending = max(peak_pending, len(bus._pending_tasks))
        if event.platform_name == "lajiaoliao":
            await commands.observe(event, "")
        await asyncio.sleep(0.01)
        timings[event.platform_name].append(
            (time.perf_counter() - event.queued_at) * 1000
        )

    bus = EventBus(
        queue,
        dict.fromkeys(platforms, SimpleNamespace(execute=execute)),
        manager,
    )
    bus._print_event = lambda *args: None
    dispatcher = asyncio.create_task(bus.dispatch())

    async def produce(name):
        nonlocal peak_queue
        slots = asyncio.Semaphore(8)
        next_arrival = time.perf_counter()
        for index in range(300):
            await asyncio.sleep(max(0, next_arrival - time.perf_counter()))
            next_arrival += 0.01
            await slots.acquire()
            completion = asyncio.get_running_loop().create_future()
            completion.add_done_callback(lambda _: slots.release())
            completions.append(completion)
            event = SimpleNamespace(
                **vars(prototype),
                platform_name=name,
                unified_msg_origin=name + ":synthetic",
                processing_completion=completion,
                queued_at=time.perf_counter(),
            )
            event.message_obj = SimpleNamespace(
                timestamp=time.time(),
                message_str="Synthetic " + str(index),
                message_id=label + name + str(index),
                sender=SimpleNamespace(nickname="Synthetic"),
            )
            await queue.put(event)
            peak_queue = max(peak_queue, queue.qsize())

    started, cpu = time.perf_counter(), time.process_time()
    try:
        await asyncio.gather(*(produce(name) for name in platforms))
        await asyncio.wait_for(asyncio.gather(*completions), timeout=30)
        return {
            "messages_per_platform": 300,
            "arrival_interval_ms": 10,
            "simulated_model_ms": 10,
            "p95_ms": {
                name: round(percentile(timings[name], 0.95), 4) for name in platforms
            },
            "wall_seconds": round(time.perf_counter() - started, 3),
            "cpu_seconds": round(time.process_time() - cpu, 3),
            "peak_pending": peak_pending,
            "peak_queue": peak_queue,
            "limits": "Actual shared EventBus and Lajiao observation only; other platform handlers and model are synthetic; no live platform sends.",
        }
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
        pending = list(bus._pending_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for completion in completions:
            if not completion.done():
                completion.cancel()
            elif not completion.cancelled():
                completion.exception()


async def main():
    if source := os.environ.get("LAJIAOLIAO_BENCHMARK_SOURCE"):
        import data.plugins.astrbot_plugin_lajiaoliao as package

        package.__path__ = [str(Path(source).resolve())]
    from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS, Businesses
    from data.plugins.astrbot_plugin_lajiaoliao.commands import Commands
    from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore

    with tempfile.TemporaryDirectory() as directory:
        store = SessionStore(Path(directory))
        ref = store.create("synthetic-owner")["session_ref"]
        store.complete(ref, "synthetic-owner", "synthetic-bot")
        store.bind(ref, "synthetic-owner", "synthetic-instance")
        plugin = SimpleNamespace(store=store)
        plugin.manager = SimpleNamespace(request=AsyncMock())
        plugin.business = Businesses(plugin)
        commands = Commands(plugin)
        event = SimpleNamespace(
            platform=SimpleNamespace(session_ref=ref, notifications=[]),
            get_group_id=lambda: "synthetic-group",
            get_sender_id=lambda: "synthetic-user",
            is_admin=lambda: False,
            message_obj=SimpleNamespace(
                timestamp=time.time(),
                message_str="synthetic text",
                message_id="warmup",
                sender=SimpleNamespace(nickname="Synthetic"),
            ),
        )
        await commands.observe(event, "")
        descriptors_before = len(list(Path("/proc/self/fd").iterdir()))
        result = {}
        for label, count, enabled in [
            ("default_off", 5000, False),
            ("rankings_on", 1000, True),
        ]:
            settings = copy.deepcopy(DEFAULTS)
            settings["rankings_enabled"] = enabled
            version = (await plugin.business.db(ref, "settings", "synthetic-group"))[
                "version"
            ]
            await plugin.business.db(
                ref, "save_settings", "synthetic-group", version, settings, "synthetic"
            )
            timings = []
            start = time.perf_counter()
            cpu = time.process_time()
            for i in range(count):
                event.message_obj.message_id = label + str(i)
                event.message_obj.timestamp = time.time()
                event.message_obj.message_str = "synthetic text " + str(i)
                tick = time.perf_counter()
                await commands.observe(event, "")
                timings.append((time.perf_counter() - tick) * 1000)
            result[label] = {
                "messages": count,
                "wall_seconds": round(time.perf_counter() - start, 3),
                "cpu_seconds": round(time.process_time() - cpu, 3),
                "stage_p50_ms": round(percentile(timings, 0.5), 4),
                "stage_p95_ms": round(percentile(timings, 0.95), 4),
                "stage_max_ms": round(max(timings), 4),
            }
            result[label]["shared_bus"] = await shared_bus_trial(commands, event, label)
        await plugin.business.close()
        plugin.manager.request.assert_not_awaited()
        result["fd_before"] = descriptors_before
        result["fd_after"] = len(list(Path("/proc/self/fd").iterdir()))
        result["process_peak_rss_kib"] = resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss
        result["real_network_or_sends"] = False
        result["limits"] = (
            "Synthetic observation stage only; not live model latency, cross-platform P95 or long-term memory growth."
        )
        print(json.dumps(result, indent=2))
        if destination := os.environ.get("LAJIAOLIAO_BENCHMARK_OUTPUT"):
            from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

            atomic_json(Path(destination), result)
        assert result["fd_after"] == result["fd_before"]


if __name__ == "__main__":
    asyncio.run(main())
