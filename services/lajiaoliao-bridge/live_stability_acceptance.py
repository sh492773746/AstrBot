"""Bounded soak and faults for only the two explicitly designated Lajiao workers."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from acceptance_client import INSTANCES, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

BINARY = ROOT / "data/plugins/astrbot_plugin_lajiaoliao/bin/lajiaoliao-bridge"


def service_pid():
    return int(
        subprocess.check_output(
            ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
            text=True,
        ).strip()
    )


def worker_samples(parent):
    found = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if Path(os.readlink(directory / "exe")) != BINARY:
                continue
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) != parent:
                raise RuntimeError("lajiaoliao_orphan_worker_detected")
            status = (directory / "status").read_text().splitlines()
            found[int(directory.name)] = {
                "rss_kib": int(
                    next(row.split()[1] for row in status if row.startswith("VmRSS:"))
                ),
                "fds": len(list((directory / "fd").iterdir())),
                "cpu_ticks": int(fields[11]) + int(fields[12]),
                "start_ticks": int(fields[19]),
            }
        except (FileNotFoundError, ProcessLookupError):
            continue
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--faults", type=int, default=0)
    parser.add_argument("--confirm-designated-workers", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.seconds < 1 or not 0 <= args.faults <= 20:
        parser.error("positive duration and at most 20 faults required")
    if args.faults and not args.confirm_designated_workers:
        parser.error("explicit designated worker fault confirmation required")
    client = AcceptanceClient()
    config = json.loads(client.original_config.decode("utf-8-sig"))
    if {
        row["id"]
        for row in config["platform"]
        if row.get("type") == "lajiaoliao" and row.get("enable")
    } != set(INSTANCES):
        raise RuntimeError("unexpected_running_lajiaoliao_instance")
    baseline = hashlib.sha256(client.original_config).hexdigest()
    parent = service_pid()
    args.output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    started = time.monotonic()
    refresh = started
    report = {
        "mode": "two_real_accounts_idle_no_model_load",
        "started_at": time.time(),
        "completed": False,
        "faults": [],
        "samples": [],
        "messages_sent": 0,
        "limits": "Real idle connections and targeted worker crash recovery, not fixed-load model or WSL/Telegram P95 acceptance.",
    }
    timings, observed = [], {}
    atomic_json(args.output, report)
    try:
        while time.monotonic() - started < args.seconds:
            if service_pid() != parent:
                raise RuntimeError("main_process_changed_stop_acceptance")
            if (
                hashlib.sha256((ROOT / "data/cmd_config.json").read_bytes()).hexdigest()
                != baseline
            ):
                raise RuntimeError("configuration_changed_stop_acceptance")
            if time.monotonic() - refresh > 600:
                client = AcceptanceClient()
                refresh = time.monotonic()
            tick = time.monotonic()
            state = client.api(PREFIX + "management")["instances"]
            timings.append((time.monotonic() - tick) * 1000)
            selected = [row for row in state if row["id"] in INSTANCES]
            if len(selected) != 2 or not all(
                row["connection_state"] == "online" for row in selected
            ):
                raise RuntimeError("test_account_not_online")
            workers = worker_samples(parent)
            if len(workers) != 2:
                raise RuntimeError("unexpected_worker_count")
            observed.update(workers)
            report["samples"].append(
                {
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "aggregate_rss_kib": sum(
                        row["rss_kib"] for row in workers.values()
                    ),
                    "fds": [row["fds"] for row in workers.values()],
                    "worker_count": len(workers),
                }
            )
            if len(report["faults"]) < args.faults:
                pid = sorted(workers)[len(report["faults"]) % 2]
                fd = os.pidfd_open(pid)
                try:
                    if (
                        worker_samples(parent).get(pid, {}).get("start_ticks")
                        != workers[pid]["start_ticks"]
                    ):
                        raise RuntimeError("worker_identity_changed")
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
                recovery = time.monotonic()
                while time.monotonic() - recovery < 60:
                    time.sleep(1)
                    current = worker_samples(parent)
                    states = client.api(PREFIX + "management")["instances"]
                    if (
                        len(current) == 2
                        and pid not in current
                        and all(
                            row["connection_state"] == "online"
                            for row in states
                            if row["id"] in INSTANCES
                        )
                    ):
                        break
                else:
                    raise RuntimeError("targeted_worker_recovery_failed")
                report["faults"].append(
                    {
                        "ordinal": len(report["faults"]) + 1,
                        "recovery_seconds": round(time.monotonic() - recovery, 3),
                        "old_process_gone": not Path(f"/proc/{pid}").exists(),
                    }
                )
                print(
                    "Targeted recovery " + str(len(report["faults"])) + " confirmed.",
                    flush=True,
                )
            atomic_json(args.output, report)
            time.sleep(5 if len(report["faults"]) < args.faults else 30)
        report["completed"] = True
    except Exception as error:
        report["error"] = type(error).__name__ + ":" + str(error)
        raise
    finally:
        current = worker_samples(parent)
        sizes = [row["aggregate_rss_kib"] for row in report["samples"]]
        report.update(
            {
                "duration_seconds": round(time.monotonic() - started, 2),
                "two_hour_gate": report["completed"]
                and time.monotonic() - started >= 7200,
                "forced_recoveries": len(report["faults"]),
                "rss_min_kib": min(sizes, default=0),
                "rss_max_kib": max(sizes, default=0),
                "rss_first_kib": sizes[0] if sizes else 0,
                "rss_last_kib": sizes[-1] if sizes else 0,
                "fds_min": min(
                    (fd for row in report["samples"] for fd in row["fds"]), default=0
                ),
                "fds_max": max(
                    (fd for row in report["samples"] for fd in row["fds"]), default=0
                ),
                "health_p95_ms": round(
                    sorted(timings)[min(len(timings) - 1, int(len(timings) * 0.95))], 3
                )
                if timings
                else 0,
                "worker_cpu_seconds": round(
                    sum(row["cpu_ticks"] for row in observed.values())
                    / os.sysconf("SC_CLK_TCK"),
                    3,
                ),
                "final_worker_count": len(current),
                "old_processes_remaining": sum(
                    Path(f"/proc/{pid}").exists()
                    for pid in observed
                    if pid not in current
                ),
                "main_pid_unchanged": service_pid() == parent,
            }
        )
        atomic_json(args.output, report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "samples"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
