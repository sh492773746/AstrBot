"""Repeat isolated before/after business benchmarks; no live sends or model calls."""

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-plugin", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not (args.baseline_plugin / "business.py").is_file():
        raise RuntimeError("baseline_plugin_unavailable")
    root = Path(__file__).resolve().parents[2]
    script = Path(__file__).with_name("plugin_business_benchmark.py")
    reports = {}
    for label in ("before", "after"):
        reports[label] = []
        for _ in range(3):
            env = dict(os.environ)
            env.pop("LAJIAOLIAO_BENCHMARK_OUTPUT", None)
            env.pop("LAJIAOLIAO_BENCHMARK_SOURCE", None)
            if label == "before":
                env["LAJIAOLIAO_BENCHMARK_SOURCE"] = str(args.baseline_plugin.resolve())
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            reports[label].append(json.loads(result.stdout))
    comparison = {}
    for stage in ("default_off", "rankings_on"):
        before = statistics.median(
            row[stage]["stage_p95_ms"] for row in reports["before"]
        )
        after = statistics.median(
            row[stage]["stage_p95_ms"] for row in reports["after"]
        )
        comparison[stage] = {
            "before_p95_median_ms": before,
            "after_p95_median_ms": after,
            "change_ms": round(after - before, 4),
            "synthetic_tolerance_passed": after - before <= max(before * 0.1, 100),
        }
        comparison[stage]["shared_bus"] = {}
        for name in ("lajiaoliao", "wangshangliao", "telegram"):
            before_bus = statistics.median(
                row[stage]["shared_bus"]["p95_ms"][name] for row in reports["before"]
            )
            after_bus = statistics.median(
                row[stage]["shared_bus"]["p95_ms"][name] for row in reports["after"]
            )
            comparison[stage]["shared_bus"][name] = {
                "before_p95_median_ms": before_bus,
                "after_p95_median_ms": after_bus,
                "change_ms": round(after_bus - before_bus, 4),
                "synthetic_tolerance_passed": after_bus - before_bus
                <= max(before_bus * 0.1, 100),
            }
    report = {
        "mode": "sequential_synthetic_three_trials_each",
        "baseline_code_sha256": hashlib.sha256(
            (args.baseline_plugin / "business.py").read_bytes()
        ).hexdigest(),
        "current_code_sha256": hashlib.sha256(
            (root / "data/plugins/astrbot_plugin_lajiaoliao/business.py").read_bytes()
        ).hexdigest(),
        "comparison": comparison,
        "trials": reports,
        "file_descriptors_stable": all(
            row["fd_before"] == row["fd_after"]
            for trials in reports.values()
            for row in trials
        ),
        "cross_platform_p95": "pending_fixed_load_model_and_designated_WSL_Telegram_targets",
        "limits": "Synthetic observation and real shared EventBus with fixed 10 ms model, 100 messages/s per platform, bounded producers. WSL/Telegram handlers are synthetic, not live production latency. Process peak RSS includes full AstrBot imports and is not plugin incremental RSS or long-run growth.",
    }
    args.output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_json(args.output, report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "trials"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
