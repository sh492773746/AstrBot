"""Run complete, deterministic module shards with durable CI evidence."""

import json
import os
import sys
import threading
from pathlib import Path

import pytest


class ModuleShard:
    """Keep module fixtures together while balancing collected test counts."""

    def __init__(self, index: int, count: int, evidence: Path):
        if count < 1 or not 0 <= index < count:
            raise ValueError("Invalid CI shard index or count")
        self.index, self.count, self.evidence = index, count, evidence

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        """Select one disjoint shard after the repository collection hooks.

        Args:
            config: Pytest configuration used to report deselected cases.
            items: Complete collection, retaining its original execution order.
        """
        modules = {}
        for item in items:
            modules.setdefault(str(item.path), []).append(item)
        loads = [0] * self.count
        assignment = {}
        for path in sorted(modules, key=lambda path: (-len(modules[path]), path)):
            shard = min(range(self.count), key=lambda index: (loads[index], index))
            assignment[path] = shard
            loads[shard] += len(modules[path])
        selected = [item for item in items if assignment[str(item.path)] == self.index]
        deselected = [
            item for item in items if assignment[str(item.path)] != self.index
        ]
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        self.evidence.write_text(
            json.dumps(
                {
                    "index": self.index,
                    "count": self.count,
                    "total_collected": len(items),
                    "loads": loads,
                    "selected": [item.nodeid for item in selected],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


def main() -> None:
    """Execute pytest with a live watchdog until all output has been flushed."""
    index = int(os.environ.get("ASTRBOT_CI_SHARD_INDEX", "0"))
    count = int(os.environ.get("ASTRBOT_CI_SHARD_COUNT", "1"))
    evidence = Path("ci-results")
    evidence.mkdir(exist_ok=True)
    timeout = int(os.environ.get("ASTRBOT_CI_TEST_TIMEOUT_SECONDS", "2400"))
    stderr_fd = os.dup(2)

    def expire():
        try:
            os.write(stderr_fd, b"[ci] Test process exceeded its time budget.\n")
            if os.name == "nt":
                import subprocess

                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(os.getpid())],
                    stdout=stderr_fd,
                    stderr=stderr_fd,
                    timeout=10,
                    check=False,
                )
        finally:
            os._exit(124)

    timer = threading.Timer(timeout, expire) if timeout > 0 else None
    if timer:
        timer.daemon = True
        timer.start()
    result = int(
        pytest.main(
            [
                *sys.argv[1:],
                f"--junitxml={evidence / f'junit-{index}.xml'}",
            ],
            plugins=[ModuleShard(index, count, evidence / f"collection-{index}.json")],
        )
    )
    # Keep the watchdog active through flushing: leaked threads can own log locks.
    sys.stdout.flush()
    sys.stderr.flush()
    if timer:
        timer.cancel()
    os.close(stderr_fd)
    os._exit(result)


if __name__ == "__main__":
    main()
