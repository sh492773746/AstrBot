"""Automatic grants use yesterday's first twenty ranks without backfilling."""

import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import automatic_whitelist
from astrbot.builtin_stars.wangshangliao_moderation.automatic_whitelist import (
    AutomaticWhitelist,
    read_automatic,
)


@pytest.mark.parametrize("twentieth_count,expected", [(101, 20), (100, 19)])
def test_yesterday_top_twenty_requires_over_one_hundred_without_backfill(
    tmp_path, monkeypatch, twentieth_count, expected
):
    monkeypatch.setattr(automatic_whitelist, "instance_dir", lambda _: tmp_path)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    monkeypatch.setattr(time, "time", lambda: today.timestamp() + 3600)
    mapping = {str(uid): str(uid + 1000) for uid in range(10, 35)}
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "top20",
            "enable": True,
            "enabled_groups": ["5"],
            "moderation": {"automation_enabled": True},
        },
        members={"5": mapping},
    )
    ranked = [
        (str(uid), f"Member {uid}", 200 - uid, today.timestamp() - 3600)
        for uid in range(10, 35)
    ]
    ranked[19] = ("29", "Member 29", twentieth_count, today.timestamp() - 3600)
    runtime = AutomaticWhitelist(None, None)
    assert (
        runtime.publish(
            adapter,
            "5",
            "1",
            mapping,
            (today - timedelta(days=1)).strftime("%Y-%m-%d"),
            today.timestamp(),
            ranked,
        )
        == expected
    )
    entries = read_automatic("top20", "1", "5")
    assert len(entries) == expected
    assert "28" in entries
    assert ("29" in entries) is (twentieth_count > 100)
    assert "30" not in entries
    assert "31" not in entries
