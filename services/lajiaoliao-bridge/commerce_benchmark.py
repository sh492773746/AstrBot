"""Bounded ten-customer, hundred-group synthetic billing load; no live SDK calls."""

import argparse
import asyncio
import os
import secrets
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from data.plugins.astrbot_plugin_lajiaoliao.ai import AIService
from data.plugins.astrbot_plugin_lajiaoliao.business import Businesses
from data.plugins.astrbot_plugin_lajiaoliao.commerce import NAMESPACE
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore, atomic_json


async def run(count):
    with tempfile.TemporaryDirectory(prefix="lajiao-commerce-load-") as directory:
        root = Path(directory)
        plugin = SimpleNamespace(
            store=SessionStore(root),
            _stopping=False,
            logger=SimpleNamespace(warning=lambda *args: None),
        )
        plugin.business = Businesses(plugin)
        plugin.ai = AIService(plugin)
        db = plugin.business.db
        await db(NAMESPACE, "bootstrap", "operator", [])

        async def mutate(route, action, version=0, **values):
            return await db(
                NAMESPACE,
                "execute",
                "commerce-" + route,
                {
                    "request_id": secrets.token_hex(16),
                    "confirm": True,
                    "version": version,
                    "action": action,
                    **values,
                },
                "operator",
            )

        await mutate(
            "rates",
            "save",
            provider_id="synthetic",
            model="synthetic",
            fingerprint="d" * 64,
            rate={
                "input_cny_per_million": "1",
                "cached_input_cny_per_million": "0.1",
                "output_cny_per_million": "5",
                "verified": True,
                "evidence_ref": "SYNTHETIC-ONLY",
                "input_count_verified": True,
                "output_bound_verified": True,
                "usage_verified": True,
            },
        )
        accounts = []
        for index in range(10):
            row = plugin.store.create("operator")
            ref, instance = row["session_ref"], "synthetic-bot-" + str(index)
            plugin.store.complete(ref, "operator", "synthetic-account-" + str(index))
            plugin.store.bind(ref, "operator", instance)
            customer = (await mutate("customers", "create", label=instance))[
                "customer_id"
            ]
            groups = ["group-" + str(index * 10 + i) for i in range(10)]
            await mutate(
                "customers",
                "bind",
                1,
                customer_id=customer,
                instance_id=instance,
                session_ref=ref,
                groups=groups,
            )
            await mutate(
                "subscriptions",
                "activate",
                2,
                customer_id=customer,
                plan="ai",
                receipt_ref="SYNTHETIC-" + str(index),
                paid_confirmed=True,
            )
            accounts.append((instance, ref, groups))
        fd_before = len(list(Path("/proc/self/fd").iterdir()))
        queue = asyncio.Queue(maxsize=64)
        latencies = []
        peaks = {"queue": 0, "ai_global": 0, "ai_instance": 0}
        started = time.perf_counter()

        async def worker():
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    index, stamp = item
                    instance, ref, groups = accounts[index % len(accounts)]
                    group = groups[(index // len(accounts)) % len(groups)]
                    event = SimpleNamespace(stop_event=lambda: None)
                    key = object()
                    await plugin.ai._acquire(event, instance, key, 30)
                    try:
                        peaks["ai_global"] = max(
                            peaks["ai_global"], sum(plugin.ai.running.values())
                        )
                        peaks["ai_instance"] = max(
                            peaks["ai_instance"], plugin.ai.running[instance]
                        )
                        call = (
                            await db(
                                NAMESPACE,
                                "reserve",
                                instance,
                                str(index),
                                1,
                                "chat",
                                "synthetic",
                                "synthetic",
                                "d" * 64,
                                1000,
                                4096,
                            )
                        )["call_id"]
                        checked = await db(
                            ref,
                            "ai_check",
                            instance,
                            "uid-" + str(index),
                            group,
                            "chat",
                            str(index),
                            consume=True,
                        )
                        assert checked["allowed"]
                        await db(NAMESPACE, "dispatch", call)
                        await asyncio.sleep(0)
                        assert (
                            await db(
                                NAMESPACE,
                                "settle",
                                call,
                                {
                                    "ordinary": 800,
                                    "cached": 200,
                                    "candidates": 100,
                                    "thinking": 100,
                                },
                            )
                        )["state"] == "settled"
                        latencies.append((time.perf_counter() - stamp) * 1000)
                    finally:
                        await plugin.ai._release(key)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(8)]
        try:
            for index in range(count):
                await queue.put((index, time.perf_counter()))
                peaks["queue"] = max(peaks["queue"], queue.qsize())
            await queue.join()
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await plugin.ai.close()
            await plugin.business.close()
        elapsed = time.perf_counter() - started
        fd_after = len(list(Path("/proc/self/fd").iterdir()))
        ordered = sorted(latencies)
        assert peaks["queue"] <= 64 and peaks["ai_global"] <= 8
        assert peaks["ai_instance"] <= 4 and plugin.business.database_peak <= 64
        assert fd_after == fd_before and not plugin.business.database_tasks
        return {
            "synthetic_only": True,
            "real_model_calls": 0,
            "real_sends": 0,
            "customers": 10,
            "groups": 100,
            "requests": len(latencies),
            "elapsed_seconds": elapsed,
            "requests_per_second": len(latencies) / elapsed,
            "p95_queue_to_settle_ms": ordered[int(len(ordered) * 0.95)],
            "peak": {**peaks, "database_tasks": plugin.business.database_peak},
            "fd_before": fd_before,
            "fd_after": fd_after,
            "real_model_capacity_verified": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--requests", default=1000, type=int)
    args = parser.parse_args()
    if not 100 <= args.requests <= 10000:
        raise ValueError("invalid_synthetic_request_count")
    os.umask(0o077)
    result = asyncio.run(run(args.requests))
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(args.output, result)
    print("Synthetic commerce load passed; no real model calls or sends.")


if __name__ == "__main__":
    main()
