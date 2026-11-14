"""Automatic eligibility survives day changes, not seven idle days or revocation."""

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    automatic_whitelist,
    member_exemptions,
    rankings,
)
from astrbot.builtin_stars.wangshangliao_moderation.automatic_whitelist import (
    INACTIVITY_SECONDS,
    AutomaticWhitelist,
    read_automatic,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

ZONE = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def retention(tmp_path, monkeypatch):
    """Provide a private collector and mutable clock.

    Args:
        tmp_path: Isolated SQLite directory.
        monkeypatch: Reversible clock and storage replacements.

    Returns:
        Runtime, adapter, clock, initial qualifying timestamp and storage path.
    """
    now = datetime(2026, 11, 1, 12, tzinfo=ZONE).timestamp()
    clock = {"now": now}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    for module in (automatic_whitelist, member_exemptions, rankings):
        monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(automatic_whitelist, "is_managed_account", lambda _: False)
    monkeypatch.setattr(automatic_whitelist, "flood_fixture", lambda *_: None)
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        config={
            "id": "retention",
            "enable": True,
            "enabled_groups": ["5", "6"],
            "moderation": {"automation_enabled": True},
        },
        members={"5": {"10": "1010"}, "6": {"10": "1010"}},
    )
    runtime = AutomaticWhitelist(None, None)
    stamp = now - 13 * 3600
    publish(runtime, adapter, [("10", "Target", 101, stamp)])
    return runtime, adapter, clock, stamp, tmp_path


def publish(runtime, adapter, ranked=(), current=(), *, group="5"):
    """Refresh the current test day using supplied settled text snapshots.

    Args:
        runtime: Automatic whitelist instance.
        adapter: Isolated platform fixture.
        ranked: Yesterday's ranked activity tuples.
        current: Today's ranked activity tuples.
        group: Exact group being refreshed.

    Returns:
        Active entry count.
    """
    today = datetime.fromtimestamp(time.time(), ZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return runtime.publish(
        adapter,
        group,
        adapter.account,
        dict(adapter.members[group]),
        (today - timedelta(days=1)).strftime("%Y-%m-%d"),
        today.timestamp(),
        ranked,
        current,
    )


def test_retained_after_day_change_and_restart_with_exact_idle_boundary(retention):
    _, adapter, clock, stamp, _ = retention
    restarted = AutomaticWhitelist(None, None)
    clock["now"] = stamp + INACTIVITY_SECONDS - 1
    assert publish(restarted, adapter) == 1
    assert read_automatic("retention", "1", "5")["10"]["count"] == 101
    clock["now"] += 1
    assert read_automatic("retention", "1", "5") == {}
    assert publish(restarted, adapter) == 0


def test_one_eligible_chat_refreshes_seven_days_without_requalifying(retention):
    runtime, adapter, clock, stamp, path = retention
    clock["now"] = stamp + 6 * 86400
    latest = clock["now"] - 60
    assert publish(runtime, adapter, current=[("10", "Target", 1, latest)]) == 1
    with closing(sqlite3.connect(path / "automatic_exemptions.sqlite3")) as db:
        last, history = db.execute(
            "SELECT last_activity,activity_history FROM automatic_exemptions"
        ).fetchone()
    assert last == latest
    assert max(json.loads(history).values()) == latest
    clock["now"] = latest + INACTIVITY_SECONDS - 1
    assert publish(AutomaticWhitelist(None, None), adapter) == 1
    clock["now"] += 1
    assert publish(runtime, adapter) == 0


def test_refresh_without_new_speech_does_not_extend_activity(retention):
    runtime, adapter, clock, stamp, _ = retention
    clock["now"] = stamp + INACTIVITY_SECONDS - 60
    for _ in range(3):
        assert publish(runtime, adapter) == 1
        clock["now"] += 20
    assert read_automatic("retention", "1", "5") == {}
    assert publish(runtime, adapter) == 0


def test_chat_after_seven_idle_days_cannot_resurrect_old_qualification(retention):
    runtime, adapter, clock, stamp, _ = retention
    clock["now"] = stamp + INACTIVITY_SECONDS + 3600
    assert (
        publish(runtime, adapter, current=[("10", "Target", 1, clock["now"] - 60)]) == 0
    )


def test_revoked_last_chat_rolls_back_to_older_valid_activity(retention):
    runtime, adapter, clock, stamp, _ = retention
    clock["now"] += 86400
    latest = clock["now"] - 60
    assert publish(runtime, adapter, current=[("10", "Target", 1, latest)]) == 1
    # The new message was recalled before the following refresh.
    assert publish(runtime, adapter) == 1
    clock["now"] = stamp + INACTIVITY_SECONDS
    assert publish(runtime, adapter) == 0


def test_late_recall_can_revoke_initial_qualification(retention):
    runtime, adapter, _, stamp, _ = retention
    assert publish(runtime, adapter, [("10", "Target", 100, stamp)]) == 0
    assert read_automatic("retention", "1", "5") == {}


def test_expired_health_lease_does_not_delete_retained_history(retention):
    runtime, adapter, clock, stamp, _ = retention
    clock["now"] += 91
    assert read_automatic("retention", "1", "5") == {}
    clock["now"] += 86400
    assert publish(runtime, adapter) == 1
    assert read_automatic("retention", "1", "5")["10"]["peer"] == "1010"
    assert stamp < clock["now"]


def test_manual_removal_suppresses_historical_readdition_across_days(retention):
    runtime, adapter, clock, _, _ = retention
    member_exemptions.set_exemption(
        "retention",
        "1",
        "5",
        "10",
        "1010",
        enabled=False,
        actor="server/test",
        reason="Explicit removal",
        guard=lambda: None,
    )
    assert read_automatic("retention", "1", "5") == {}
    clock["now"] += 86400
    assert read_automatic("retention", "1", "5") == {}
    assert publish(runtime, adapter) == 0


def test_removed_member_needs_a_later_day_new_qualification(retention):
    runtime, adapter, clock, stamp, _ = retention
    member_exemptions.set_exemption(
        "retention",
        "1",
        "5",
        "10",
        "1010",
        enabled=False,
        actor="server/test",
        reason="Explicit removal",
        guard=lambda: None,
    )
    assert publish(runtime, adapter, [("10", "Target", 101, stamp)]) == 0
    clock["now"] += 86400
    assert publish(runtime, adapter, [("10", "Target", 101, stamp + 86400)]) == 1
    assert read_automatic("retention", "1", "5")["10"]["peer"] == "1010"


def test_new_top_twenty_does_not_evict_active_historical_members(retention):
    runtime, adapter, clock, _, _ = retention
    clock["now"] += 86400
    fresh = [
        (str(uid), f"Member {uid}", 200 - uid, clock["now"] - 13 * 3600)
        for uid in range(20, 40)
    ]
    adapter.members["5"].update({str(uid): str(uid + 1000) for uid in range(20, 40)})
    assert publish(runtime, adapter, fresh) == 21
    assert len(read_automatic("retention", "1", "5")) == 21


def test_manual_whitelist_never_expires_with_automatic_retention(retention):
    runtime, adapter, clock, _, _ = retention
    member_exemptions.set_exemption(
        "retention",
        "1",
        "5",
        "11",
        "1011",
        enabled=True,
        actor="server/test",
        reason="Manual grant",
        guard=lambda: None,
    )
    clock["now"] += 8 * 86400
    assert publish(runtime, adapter) == 0
    assert member_exemptions.is_exempt("retention", "1", "5", "11", "1011")


def test_blacklist_peer_and_group_isolation(retention):
    runtime, adapter, clock, _, path = retention
    assert read_automatic("retention", "1", "6") == {}
    assert read_automatic("retention", "2", "5") == {}
    assert read_automatic("retention", "1", "5", member="10", peer="9999") == {}
    clock["now"] += 86400
    adapter.members["5"]["10"] = "9999"
    assert publish(runtime, adapter) == 0
    adapter.members["5"]["10"] = "1010"
    yesterday_stamp = clock["now"] - 13 * 3600
    assert publish(runtime, adapter, [("10", "Target", 101, yesterday_stamp)]) == 1
    with closing(sqlite3.connect(path / "member_blacklist.sqlite3")) as db, db:
        db.execute("CREATE TABLE blacklist_entries(account,group_id,member,status)")
        db.execute("INSERT INTO blacklist_entries VALUES('1','5','10','active')")
    assert read_automatic("retention", "1", "5") == {}
    assert publish(runtime, adapter, [("10", "Target", 101, yesterday_stamp)]) == 0
    with closing(sqlite3.connect(path / "member_blacklist.sqlite3")) as db, db:
        db.execute("UPDATE blacklist_entries SET status='removed'")
    clock["now"] += 86400
    assert publish(runtime, adapter) == 0


@pytest.mark.parametrize(
    "field,value", [("enable", False), ("_business_active", False)]
)
def test_disabled_scope_cannot_renew_retention(retention, field, value):
    runtime, adapter, _, _, _ = retention
    adapter.config[field] = value
    with pytest.raises(ProtocolError, match="automatic_whitelist_scope_changed"):
        publish(runtime, adapter)


def test_old_schema_migrates_only_from_verified_ranking(tmp_path, monkeypatch):
    monkeypatch.setattr(automatic_whitelist, "instance_dir", lambda _: tmp_path)
    now = datetime(2026, 11, 1, 12, tzinfo=ZONE).timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    with closing(sqlite3.connect(tmp_path / "automatic_exemptions.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE automatic_exemptions(account,group_id,member,peer,name,"
            "messages,day_start,valid_until,PRIMARY KEY(account,group_id,member))"
        )
        db.execute(
            "INSERT INTO automatic_exemptions VALUES('1','5','10','1010','Old',200,?,?)",
            (now - 86400, now + 90),
        )
    assert read_automatic("legacy", "1", "5") == {}
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "legacy",
            "enabled_groups": ["5"],
            "moderation": {"automation_enabled": True},
        },
        members={"5": {"10": "1010"}},
    )
    runtime = AutomaticWhitelist(None, None)
    assert publish(runtime, adapter) == 0
    assert publish(runtime, adapter, [("10", "New", 101, now - 13 * 3600)]) == 1


@pytest.mark.asyncio
async def test_refresh_worker_reads_both_settled_days_before_renewing(
    retention, monkeypatch
):
    _, adapter, clock, stamp, _ = retention
    adapter.config["type"] = "wangshangliao"
    adapter.config["enabled_groups"] = ["5"]
    adapter.connection_state = "online"
    adapter.diagnostics = SimpleNamespace(emit=Mock())
    monkeypatch.setattr(automatic_whitelist, "scoped", lambda adapter, _: adapter)
    today = datetime.fromtimestamp(clock["now"], ZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    latest = clock["now"] - 60
    source = SimpleNamespace(
        snapshot=AsyncMock(
            side_effect=[
                (
                    (today - timedelta(days=1)).strftime("%Y-%m-%d"),
                    today.timestamp(),
                    [("10", "Target", 101, stamp)],
                ),
                (
                    today.strftime("%Y-%m-%d"),
                    today.timestamp() + 86400,
                    [("10", "Target", 1, latest)],
                ),
            ]
        )
    )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    runtime = AutomaticWhitelist(context, source)
    await runtime.step()
    assert source.snapshot.await_count == 2
    assert all(
        call.kwargs.get("include_activity") and call.kwargs.get("settled")
        for call in source.snapshot.await_args_list
    )
    adapter.diagnostics.emit.assert_called_once()
    assert read_automatic("retention", "1", "5")["10"]["count"] == 101


@pytest.mark.asyncio
async def test_activity_timestamps_share_rankings_filters_and_cache(retention):
    _, adapter, clock, _, path = retention
    ledger = path / "messages.sqlite3"
    adapter.ledger = SimpleNamespace(path=ledger)
    today = (
        datetime.fromtimestamp(clock["now"], ZONE)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    first = today - 3600
    with closing(sqlite3.connect(ledger)) as db, db:
        db.execute(
            "CREATE TABLE inbox(account,team,message,payload,state,received_at,pruned_at)"
        )
        db.execute(
            "CREATE TABLE observed_recalls(account,group_id,message_id,received_at)"
        )
        for index, text in enumerate(
            ["Normal conversation", "Normal conversation", "白名单", "", "Another chat"]
        ):
            stamp = first + 31 * index
            db.execute(
                "INSERT INTO inbox VALUES('1','5',?,?,'processed',?,NULL)",
                (
                    str(index),
                    json.dumps(
                        {
                            "sender": "10",
                            "name": "Target",
                            "text": text,
                            "created_at": stamp * 1000,
                        }
                    ),
                    stamp,
                ),
            )
        db.execute(
            "INSERT INTO observed_recalls VALUES('1','5','4',?)",
            (clock["now"],),
        )
    runtime = rankings.DailyRankings()
    for _ in range(2):
        _, _, ordinary = await runtime.snapshot(
            adapter, "5", previous_day=True, settled=True
        )
        _, _, activity = await runtime.snapshot(
            adapter, "5", previous_day=True, settled=True, include_activity=True
        )
        assert ordinary == [("10", "Target", 1)]
        assert activity == [("10", "Target", 1, first)]
        runtime.cache.clear()
