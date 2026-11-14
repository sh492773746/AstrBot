"""Compare isolated receive/enqueue/decision latency without live API calls."""

import argparse
import asyncio
import json
import math
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrbot.builtin_stars.wangshangliao_moderation.raid_defense import RaidDefense
from astrbot.builtin_stars.wangshangliao_moderation.settings import BusinessSettings
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
    PolicySnapshot,
    runtime,
)
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


async def measure(root, count, enabled):
    """Measure real codec, ledger and decision work with a draining local consumer.

    Args:
        root: Isolated temporary directory.
        count: Number of measured mixed messages.
        enabled: Whether observed newcomers use the new protection observer.

    Returns:
        Metadata-only percentiles; no network or sanction acceptance is implied.
    """
    ledger = Ledger(root / "messages.sqlite3")
    await ledger.open()
    account, group = "1", "5"
    adapter = SimpleNamespace(
        account=account,
        config={
            "id": "benchmark",
            "type": "wangshangliao",
            "_business_revision": "1",
            "enable": True,
            "_business_active": True,
            "enabled_groups": [group],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "advertising_enabled": False,
                "flood_enabled": True,
                "raid_defense_enabled": enabled,
            },
        },
        stopping=asyncio.Event(),
    )
    service = SimpleNamespace(
        active=True,
        integrated=False,
        settings={"paused": False},
        capacity_paused=False,
        suspended_groups=set(),
        membership_receiving={},
        reconciling_groups={},
        tenants={
            (account, group): {
                "active": True,
                "present": True,
                "paused": False,
                "native_role": "GROUP_ROLE_ADMIN",
                "rules": {"raid_defense_enabled": enabled},
            }
        },
        adapters=lambda: [adapter],
    )
    defense = RaidDefense(service)
    service.raid_defense = defense
    previous = runtime.open_service
    runtime.open_service = service
    settings = BusinessSettings(None, root / "settings")
    snapshot = PolicySnapshot(account, "1", json.dumps(adapter.config), 0)
    base = time.time()
    defense.states[(account, group)] = {
        "enabled": enabled,
        "generation": "isolated",
        "enabled_at": base - 2,
    }
    defense.members[(account, group)] = {
        str(uid): {
            "member": str(uid),
            "peer": str(uid + 10000),
            "join_id": str(uid),
            "joined_at": base - 1,
            "observed_until": base + 600,
            "mute_until": 0,
        }
        for uid in range(1000, 1250)
    }
    timings = {
        name: []
        for name in ("decode", "receive_enqueue", "observer", "decision", "queue_wait")
    }
    handled, maximum, done = 0, 0, False
    work = asyncio.Event()

    async def consume():
        nonlocal handled, maximum
        while not done or handled < count:
            async with ledger.ingress_lock:
                async with ledger.db.execute(
                    "SELECT message,payload,received_at FROM inbox WHERE state='pending' ORDER BY received_at LIMIT 32"
                ) as cursor:
                    rows = await cursor.fetchall()
                maximum = max(maximum, len(rows))
                for mid, encoded, received_at in rows:
                    start = time.perf_counter()
                    payload = json.loads(encoded)
                    decision = payload.get("_ingress_raid") or payload.get(
                        "_ingress_flood", {}
                    )
                    assert isinstance(decision.get("category", ""), str)
                    timings["queue_wait"].append(
                        max(0, time.time() - received_at) * 1000
                    )
                    timings["decision"].append((time.perf_counter() - start) * 1000)
                    await ledger.mark(account, group, mid, "processed")
                    handled += 1
            if not rows:
                work.clear()
                if handled == count:
                    break
                await work.wait()

    task = asyncio.create_task(consume())
    started = time.perf_counter()
    try:
        for index in range(count):
            uid = 1000 + index % 250
            kind = ("text", "image", "image", "video")[index % 4]
            message = wire.ApplicationMessage(
                sender=wire.Source(id=uid, name="fixture"),
                target=wire.Source(id=int(group)),
                device=1,
                session=1,
                version=2,
                format=0,
                content=wire.Content(data="neutral fixture"),
            )
            encoded = wire.seal_message(
                bytes(32), message, 100000 + index, 321, int(time.time() * 1000)
            )
            start = time.perf_counter()
            decoded = wire.open_message(bytes(32), encoded)
            timings["decode"].append((time.perf_counter() - start) * 1000)
            payload = {
                "sender": str(decoded.sender.id),
                "text": decoded.content.data,
                "kind": kind,
                "recall_route": {
                    "peer": str(uid + 10000),
                    "time": str(int(time.time() * 1000)),
                },
            }

            def observe(body, stamp, mid=str(index)):
                observed = time.perf_counter()
                settings.observe(snapshot, account, group, mid, body, stamp)
                timings["observer"].append((time.perf_counter() - observed) * 1000)

            await ledger.ingest(
                account,
                group,
                str(index),
                payload,
                observe=observe,
            )
            timings["receive_enqueue"].append((time.perf_counter() - start) * 1000)
            work.set()
        done = True
        work.set()
        await asyncio.wait_for(task, 30)
        result = {}
        for name, values in timings.items():
            ordered = sorted(values)
            result[name] = {
                "samples": len(values),
                "p95_ms": round(ordered[math.ceil(len(values) * 0.95) - 1], 5),
                "p99_ms": round(ordered[math.ceil(len(values) * 0.99) - 1], 5),
                "max_ms": round(ordered[-1], 5),
            }
        result.update(
            processed=handled,
            remaining=count - handled,
            max_consumer_batch=maximum,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
        return result
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        runtime.open_service = previous
        await defense.close()
        await ledger.close()


async def main():
    """Run a metadata-only comparison and fail if the stated local budget is exceeded."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 1000:
        parser.error("at least 1000 samples are required")
    with tempfile.TemporaryDirectory(prefix="wsl-raid-performance-") as directory:
        root = Path(directory)
        baseline = await measure(root / "baseline", args.samples, False)
        enabled = await measure(root / "enabled", args.samples, True)
    limit = baseline["receive_enqueue"]["p95_ms"] + max(
        baseline["receive_enqueue"]["p95_ms"] * 0.2,
        1,
    )
    result = {
        "scope": "isolated AEAD decode + actual ingress ledger + cached rule decision; no live HTTP/OCR/sanctions",
        "baseline": baseline,
        "raid_enabled": enabled,
        "p95_limit_ms": round(limit, 5),
        "passed": enabled["receive_enqueue"]["p95_ms"] <= limit
        and enabled["remaining"] == 0,
        "http_wait": "not exercised; report live measurements separately",
        "ocr": "not exercised; observer never downloads or recognizes media",
    }
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
