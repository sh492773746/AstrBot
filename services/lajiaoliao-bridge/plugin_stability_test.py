"""Credential-free local worker soak; never opens an upstream message connection."""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from data.plugins.astrbot_plugin_lajiaoliao.runtime import Runtime
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore


async def run(seconds, workers=1, binary=None, restart_every=10):
    root = Path(__file__).resolve().parents[2]
    binary = binary or root / "data/plugins/astrbot_plugin_lajiaoliao/bin/lajiaoliao-bridge"
    with tempfile.TemporaryDirectory() as directory:
        store = SessionStore(Path(directory))
        refs = []
        for index in range(workers):
            row = store.create(f"synthetic-soak-owner-{index}")
            # Extend only synthetic fixtures for long-duration local testing.
            row["expires_at"] = time.time() + seconds + 60
            store._write(row)
            refs.append(row["session_ref"])
        manager = Runtime(store, binary)
        pids, memory, aggregate_memory, timings, descriptors = [], [], [], [], []
        cpu_ticks = {}
        started = time.monotonic()

        async def sample(ref, worker):
            tick = time.monotonic()
            status = await manager.request(ref, "GET", "/v1/status")
            assert not status["authenticated"]
            timings.append((time.monotonic() - tick) * 1000)
            stat = Path(f"/proc/{worker.process.pid}/status").read_text()
            rss = int(next(line.split()[1] for line in stat.splitlines() if line.startswith("VmRSS:")))
            memory.append(rss)
            descriptors.append(len(list(Path(f"/proc/{worker.process.pid}/fd").iterdir())))
            fields = Path(f"/proc/{worker.process.pid}/stat").read_text().rsplit(")", 1)[1].split()
            cpu_ticks[worker.process.pid] = int(fields[11]) + int(fields[12])
            return rss

        try:
            running = list(await asyncio.gather(*(manager.start(ref, connect=False) for ref in refs)))
            pids.extend(worker.process.pid for worker in running)
            iteration = 0
            while time.monotonic() - started < seconds:
                sizes = await asyncio.gather(*(sample(ref, worker) for ref, worker in zip(refs, running)))
                aggregate_memory.append(sum(sizes))
                iteration += 1
                if iteration % restart_every == 0:
                    index = (iteration // restart_every - 1) % workers
                    worker = running[index]
                    worker.process.kill()
                    await worker.process.wait()
                    worker = await manager.start(refs[index], connect=False)
                    running[index] = worker
                    pids.append(worker.process.pid)
                await asyncio.sleep(1)
        finally:
            await manager.close()
        assert not any(Path(f"/proc/{pid}").exists() for pid in pids)
        ordered = sorted(timings)
        print(json.dumps({
            "mode": "synthetic_authorization_worker_no_upstream",
            "concurrent_accounts": workers,
            "duration_seconds": round(time.monotonic() - started, 1),
            "samples": len(timings), "worker_starts": len(pids),
            "forced_restarts": len(pids) - workers,
            "fds_min": min(descriptors), "fds_max": max(descriptors),
            "rss_min_kib": min(memory), "rss_max_kib": max(memory),
            "aggregate_rss_min_kib": min(aggregate_memory),
            "aggregate_rss_max_kib": max(aggregate_memory),
            "sampled_worker_cpu_seconds": round(sum(cpu_ticks.values()) / os.sysconf("SC_CLK_TCK"), 3),
            "local_status_p95_ms": round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 2),
            "orphan_processes": 0,
        }))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--binary", type=Path, default=None)
    parser.add_argument("--restart-every", type=int, default=10, help="Synthetic iterations between worker crashes")
    args = parser.parse_args()
    if args.seconds < 1:
        parser.error("duration must be positive")
    if not 1 <= args.workers <= 16:
        parser.error("workers must be between 1 and 16")
    if args.restart_every < 1:
        parser.error("restart interval must be positive")
    asyncio.run(run(args.seconds, args.workers, args.binary, args.restart_every))
