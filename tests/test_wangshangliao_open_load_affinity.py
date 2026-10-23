"""Optional benchmark affinity is explicit, verified and never a service change."""

import argparse
import builtins
import io
import itertools
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_wsl_open_load as benchmark


def test_synthetic_consultation_binding_keeps_two_slow_models_and_rules(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio\n"
            "from scripts.check_wsl_open_load import worker\n"
            "asyncio.run(worker(1))\n",
        ],
        cwd=Path(benchmark.__file__).resolve().parents[1],
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key != "ASTRBOT_WSL_OPEN"
            },
            "ASTRBOT_ROOT": str(tmp_path),
            "ASTRBOT_DISABLE_FILE_LOGGING": "1",
            "ASTRBOT_DISABLE_METRICS": "1",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr[-6000:] + result.stdout[-6000:]
    assert "LOAD_READY" in result.stdout.splitlines()
    report = json.loads(
        next(
            line.removeprefix("LOAD_RESULT ")
            for line in result.stdout.splitlines()
            if line.startswith("LOAD_RESULT ")
        )
    )
    assert report["model_requests"] == 2
    assert report["messages"] == 100
    assert report["groups"] == 20
    assert report["slow_ai_did_not_block_rules"] is True
    assert report["atomic_plan_cost_reservations"] is True
    assert report["native_decode_dispatch_storage"] is True
    assert report["private_flood_rejected"] > 0
    assert report["rule_p95_ms"] < 1000


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0", {0}),
        ("1-3", {1, 2, 3}),
        ("0,2-3", {0, 2, 3}),
        ("3,1,1-2", {1, 2, 3}),
        (" 0, 2-3 ", {0, 2, 3}),
    ],
)
def test_cpu_list_accepts_explicit_linux_lists(text, expected):
    assert benchmark.cpu_list(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "0,",
        ",1",
        "1,,2",
        "-1",
        "2-1",
        "1-2-3",
        "all",
        "1.0",
        "1;2",
        "0-9999999999999999999999",
    ],
)
def test_cpu_list_rejects_invalid_or_unbounded_ranges(text):
    with pytest.raises(argparse.ArgumentTypeError):
        benchmark.cpu_list(text)


def test_default_affinity_is_never_changed(monkeypatch):
    def unexpected(*_):
        pytest.fail("No explicit CPU option means no affinity write")

    monkeypatch.setattr(benchmark.os, "sched_setaffinity", unexpected)
    benchmark.apply_affinity(None)


def test_requested_affinity_is_verified_instead_of_accepting_silent_truncation(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        benchmark.os, "sched_setaffinity", lambda pid, mask: calls.append((pid, mask))
    )
    monkeypatch.setattr(benchmark.os, "sched_getaffinity", lambda _: {0})
    with pytest.raises(RuntimeError, match="affinity"):
        benchmark.apply_affinity({0, 1})
    assert calls == [(0, {0, 1})]


def test_affinity_error_is_explicit_and_no_retry(monkeypatch):
    calls = []

    def rejected(pid, mask):
        calls.append((pid, mask))
        raise OSError("fixture unavailable CPU")

    monkeypatch.setattr(benchmark.os, "sched_setaffinity", rejected)
    with pytest.raises(OSError, match="unavailable CPU"):
        benchmark.apply_affinity({0})
    assert calls == [(0, {0})]


def test_explicit_affinity_is_rejected_on_unsupported_platform(monkeypatch):
    monkeypatch.delattr(benchmark.os, "sched_setaffinity")
    with pytest.raises(RuntimeError, match="support"):
        benchmark.apply_affinity({0})
    benchmark.apply_affinity(None)


@pytest.mark.parametrize("options", [("--reader-cpus", "4"), ("--load-cpus", "4")])
def test_both_masks_are_validated_before_any_affinity_or_database_write(
    monkeypatch,
    capsys,
    options,
):
    monkeypatch.setattr(
        benchmark.sys, "argv", ["benchmark", "--seconds", "10", *options]
    )
    monkeypatch.setattr(benchmark.os, "sched_getaffinity", lambda _: {0, 1, 2, 3})

    def unexpected(*_):
        pytest.fail("Invalid masks must fail before affinity or ledger admission")

    monkeypatch.setattr(benchmark.os, "sched_setaffinity", unexpected)
    monkeypatch.setattr(benchmark.sqlite3, "connect", unexpected)
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2
    assert "available CPUs" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_worker_explicit_mask_replaces_inherited_reader_mask_before_imports(
    monkeypatch,
):
    state = {"mask": {0}}
    calls = []

    def set_affinity(pid, mask):
        calls.append((pid, mask))
        state["mask"] = set(mask)

    original = builtins.__import__

    def stop_before_network(name, *args, **kwargs):
        if name == "aiohttp":
            assert state["mask"] == {1, 2, 3}
            raise RuntimeError("fixture stopped before network setup")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(benchmark.os, "sched_setaffinity", set_affinity)
    monkeypatch.setattr(benchmark.os, "sched_getaffinity", lambda _: state["mask"])
    monkeypatch.setattr(builtins, "__import__", stop_before_network)
    with pytest.raises(RuntimeError, match="fixture stopped"):
        await benchmark.worker(10, load_cpus={1, 2, 3})
    assert calls == [(0, {1, 2, 3})]


def test_placement_evidence_includes_cgroup_and_ancestor_quota(monkeypatch):
    files = {
        "/proc/self/cgroup": "0::/fixture/worker\n",
        "/sys/fs/cgroup/fixture/worker/cpu.max": "50000 100000\n",
        "/sys/fs/cgroup/fixture/worker/cpu.stat": "usage_usec 100\nnr_throttled 3\n",
        "/sys/fs/cgroup/fixture/worker/cpuset.cpus.effective": "0-3\n",
        "/sys/fs/cgroup/fixture/cpu.max": "200000 100000\n",
        "/sys/fs/cgroup/cpuset.cpus.effective": "0-7\n",
    }

    def read(path, *_, **__):
        if str(path) not in files:
            raise FileNotFoundError(str(path))
        return files[str(path)]

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(benchmark.os, "sched_getaffinity", lambda _: {1, 2, 3})
    result = benchmark.placement()
    assert result["actual_cpus"] == [1, 2, 3]
    assert result["cgroup_membership"] == ["0::/fixture/worker"]
    assert result["cgroup_cpu"][0] == {
        "path": "/sys/fs/cgroup/fixture/worker",
        "cpu.max": "50000 100000",
        "cpu.stat": "usage_usec 100\nnr_throttled 3",
        "cpuset.cpus.effective": "0-3",
    }
    assert result["cgroup_cpu"][1]["cpu.max"] == "200000 100000"


def test_placement_reports_missing_cgroup_as_unknown_not_unlimited(monkeypatch):
    def missing(*_, **__):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", missing)
    monkeypatch.setattr(benchmark.os, "sched_getaffinity", lambda _: {0})
    result = benchmark.placement()
    assert result["actual_cpus"] == [0]
    assert result["cgroup_version"] is None
    assert result["cgroup_membership"] == result["cgroup_cpu"] == []


def test_absolute_query_report_is_rejected_for_direct_worker(monkeypatch, capsys):
    monkeypatch.setattr(
        benchmark.sys,
        "argv",
        ["benchmark", "--worker", "--absolute-query-report", "--seconds", "10"],
    )

    def unexpected(*_, **__):
        pytest.fail("Report-only parser validation must happen before worker setup")

    monkeypatch.setattr(benchmark, "worker", unexpected)
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2
    assert "only valid for the baseline/read process" in capsys.readouterr().err


@pytest.mark.parametrize(
    "explicit,drift,worker_drift",
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (True, False, True),
    ],
)
@pytest.mark.parametrize(
    "report_mode,large_growth",
    [(False, False), (False, True), (True, True)],
)
def test_main_baseline_and_load_keep_reader_mask_and_pass_only_worker_mask(
    tmp_path,
    monkeypatch,
    capsys,
    explicit,
    drift,
    worker_drift,
    report_mode,
    large_growth,
):
    from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
        CashbackStore,
    )

    database = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE groups(id TEXT); INSERT INTO groups VALUES('5');"
            "CREATE TABLE entries(id TEXT,group_id TEXT);"
            "CREATE TABLE users(email TEXT,active INTEGER,role TEXT);"
            "INSERT INTO users VALUES('fixture@example.com',1,'admin');"
        )
    state = {"mask": {0, 1, 2, 3}}
    applied, commands, sample_masks = [], [], []

    def set_affinity(pid, mask):
        applied.append((pid, set(mask)))
        state["mask"] = set(mask)

    def sample(*_, **__):
        sample_masks.append(set(state["mask"]))
        return {}

    worker_mask = [1, 2, 3] if explicit else [0, 1, 2, 3]

    def popen(command, **kwargs):
        commands.append(command)
        state["loaded"] = True
        if drift:
            state["mask"] = {2}
        assert kwargs["env"]["ASTRBOT_ROOT"] != "/opt/astrbot-prod"
        assert "--reader-cpus" not in command
        assert "--absolute-query-report" not in command
        reported_worker_mask = [0] if worker_drift else worker_mask
        return SimpleNamespace(
            stdout=io.StringIO("LOAD_READY\n"),
            stderr=io.StringIO(),
            returncode=0,
            communicate=lambda **_: (
                "LOAD_RESULT "
                + json.dumps(
                    {
                        "rule_p95_ms": 1,
                        "worker_placement": {
                            "start": {"actual_cpus": reported_worker_mask},
                            "end": {"actual_cpus": reported_worker_mask},
                        },
                    }
                ),
                "",
            ),
        )

    output = tmp_path / "evidence"
    output.mkdir()
    real_path = benchmark.Path

    def local_path(*args):
        path = real_path(*args)
        return output if str(path) == "/opt/backups" else path

    argv = ["benchmark", "--database", str(database), "--seconds", "10"]
    if explicit:
        argv.extend(["--reader-cpus", "0", "--load-cpus", "1-3"])
    if report_mode:
        argv.append("--absolute-query-report")
    monkeypatch.setattr(benchmark.sys, "argv", argv)
    monkeypatch.setattr(benchmark, "Path", local_path)
    monkeypatch.setattr(benchmark.os, "sched_setaffinity", set_affinity)
    monkeypatch.setattr(benchmark.os, "sched_getaffinity", lambda _: state["mask"])
    monkeypatch.setattr(CashbackStore, "list_entries", sample)
    ticks = itertools.count()
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(benchmark.time, "sleep", lambda _: None)
    performance_ticks = itertools.count()
    monkeypatch.setattr(
        benchmark.time,
        "perf_counter",
        lambda: (
            next(performance_ticks)
            * (0.01 if large_growth and state.get("loaded") else 0.001)
        ),
    )
    monkeypatch.setattr(benchmark.subprocess, "Popen", popen)
    if drift or worker_drift:
        with pytest.raises(RuntimeError, match="affinity changed"):
            benchmark.main()
    elif large_growth and not report_mode:
        with pytest.raises(RuntimeError, match="P95 target not met"):
            benchmark.main()
    else:
        benchmark.main()
    result = json.loads(capsys.readouterr().out)
    expected = {0} if explicit else {0, 1, 2, 3}
    assert sample_masks
    if not drift:
        assert all(mask == expected for mask in sample_masks)
    assert applied == ([(0, {0})] if explicit else [])
    assert len(commands) == 1
    if explicit:
        assert commands[0][-2:] == ["--load-cpus", "1,2,3"]
    else:
        assert "--load-cpus" not in commands[0]
    placement = result["process_placement"]
    assert placement["initial_available_cpus"] == [0, 1, 2, 3]
    assert placement["requested_reader_cpus"] == ([0] if explicit else None)
    assert placement["requested_load_cpus"] == (worker_mask if explicit else None)
    assert placement["production_service_configuration_changed"] is False
    assert placement["reader_masks_unchanged"] is (not drift)
    assert placement["worker_masks_match_request"] is (not worker_drift)
    if not drift:
        assert all(
            item["actual_cpus"] == sorted(expected)
            for item in placement["reader"].values()
        )
    assert len(list(output.glob("*.json"))) == 1
    queries = result["production_queries"]
    assert queries["acceptance_mode"] == (
        "absolute_query_report" if report_mode else "relative_growth"
    )
    assert queries["relative_growth_enforced"] is (not report_mode)
    assert queries["within_ten_percent"] is (not large_growth)
    assert queries["baseline_p95_ms"] == pytest.approx(9)
    assert queries["under_load_p95_ms"] == pytest.approx(90 if large_growth else 9)
    saved = json.loads(next(output.glob("*.json")).read_text())
    assert saved["production_queries"] == queries
