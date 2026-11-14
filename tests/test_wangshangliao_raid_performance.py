"""Keep the isolated latency benchmark on the supported group codec."""

import pytest

from scripts.check_wangshangliao_raid_performance import measure


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_group_benchmark_drains_every_measured_message(tmp_path, enabled):
    result = await measure(tmp_path, 16, enabled)
    assert result["processed"] == 16
    assert result["remaining"] == 0
    for metric in ("decode", "receive_enqueue", "observer", "decision", "queue_wait"):
        assert result[metric]["samples"] == 16
        assert 0 <= result[metric]["p95_ms"] <= result[metric]["max_ms"]
