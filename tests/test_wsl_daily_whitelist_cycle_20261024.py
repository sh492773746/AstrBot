"""Current flood thresholds, durable cycles and automatic daily exemptions."""

import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import (
    automatic_whitelist,
    member_exemptions,
    rankings,
)
from astrbot.builtin_stars.wangshangliao_moderation.automatic_whitelist import (
    AutomaticWhitelist,
)
from astrbot.builtin_stars.wangshangliao_moderation.member_exemptions import (
    is_exempt,
    read_exemptions,
    set_exemption,
)
from astrbot.core.platform.sources.wangshangliao import automatic, progressive
from astrbot.core.platform.sources.wangshangliao.diagnostics import Diagnostics
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests import test_wangshangliao_progressive as progressive_tests

bot = progressive_tests.bot


@pytest.mark.asyncio
async def test_two_full_cycles_expiry_restart_and_duplicate_are_durable(
    bot, monkeypatch
):
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    with closing(automatic.database(bot)) as db, db:
        db.execute("DELETE FROM progressive_warnings")
    for cycle in range(2):
        for warning in range(1, 4):
            mid = f"cycle-{cycle}-warning-{warning}"
            result = await progressive.enforce(bot, "5", "2", mid)
            assert result["warning_number"] == warning
            assert (await progressive.enforce(bot, "5", "2", mid)) == result
        for minutes in (1, 3, 10):
            mid = f"cycle-{cycle}-mute-{minutes}"
            result = await progressive.enforce(bot, "5", "2", mid)
            assert result["minutes"] == minutes
            if minutes == 10:
                waiting = await progressive.enforce(bot, "5", "2", f"waiting-{cycle}")
                assert waiting["cycle_wait"] is True
                assert "warning_number" not in waiting
            clock += minutes * 60 + 1
            bot.__dict__.pop("_progression_store", None)
    assert [
        call.kwargs["minutes"] for call in bot.execute_moderation.await_args_list
    ] == [1, 3, 10] * 2
    with closing(automatic.database(bot)) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM progressive_warnings").fetchone()[0] == 6
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automatic_mutes WHERE closed=1"
            ).fetchone()[0]
            == 3
        )


@pytest.mark.asyncio
async def test_new_native_identity_does_not_inherit_old_warning_grace(bot):
    await progressive.enforce(bot, "5", "2", "old-peer")
    bot.members["5"]["2"] = "222"
    result = await progressive.enforce(bot, "5", "2", "new-peer")
    assert result["warning_number"] == 1


@pytest.mark.asyncio
async def test_unknown_final_mute_never_resets_grace(bot, monkeypatch):
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    for index in range(2):
        await progressive.enforce(bot, "5", "2", str(index))
    bot.execute_moderation.side_effect = None
    bot.execute_moderation.return_value = {"status": "unknown"}
    result = await progressive.enforce(bot, "5", "2", "unknown-final")
    assert result["status"] == "unknown"
    clock += 86400
    result = await progressive.enforce(bot, "5", "2", "after-unknown")
    assert result["status"] == "unknown"
    assert "warning_number" not in result
    assert bot.execute_moderation.await_count == 3


@pytest.mark.asyncio
async def test_final_receipt_recovers_cycle_after_interrupted_bookkeeping(
    bot, monkeypatch
):
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    for index in range(3):
        await progressive.enforce(bot, "5", "2", f"mute-{index}")
    with closing(automatic.database(bot)) as db, db:
        db.execute("UPDATE moderation_cycles SET reset_after=0")
        db.execute("UPDATE progressive_actions SET result='{}' WHERE message='mute-2'")
    bot.__dict__.pop("_progression_store", None)
    clock += 601
    result = await progressive.enforce(bot, "5", "2", "after-restart")
    assert result["warning_number"] == 1


@pytest.mark.asyncio
async def test_final_transport_receipt_is_reconciled_without_resubmitting(
    bot, monkeypatch
):
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    for index in range(3):
        await progressive.enforce(bot, "5", "2", f"mute-{index}")
    with closing(automatic.database(bot)) as db, db:
        db.execute("UPDATE moderation_cycles SET reset_after=0")
        identifier = db.execute(
            "SELECT operation FROM automatic_mutes WHERE message='mute-2'"
        ).fetchone()[0]
        db.execute(
            "UPDATE automatic_mutes SET status='unknown' WHERE operation=?",
            (identifier,),
        )
        db.execute(
            "UPDATE automatic_mute_durations SET accepted_at=0 WHERE operation=?",
            (identifier,),
        )
    result = await progressive.enforce(bot, "5", "2", "reconcile-final")
    assert result["cycle_wait"] is True
    assert bot.execute_moderation.await_count == 3
    clock += 601
    result = await progressive.enforce(bot, "5", "2", "new-cycle")
    assert result["warning_number"] == 1


@pytest_asyncio.fixture
async def daily(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 24, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    for module in (automatic_whitelist, member_exemptions, rankings):
        monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    adapter = SimpleNamespace(
        config={
            "id": "daily",
            "type": "wangshangliao",
            "enable": True,
            "enabled_groups": ["5"],
            "moderation": {"automation_enabled": True},
        },
        account="1",
        nim_account="901",
        ledger=ledger,
        members={"5": {str(uid): str(900 + uid) for uid in range(10, 43)}},
        diagnostics=create_autospec(Diagnostics, instance=True),
        connection_state="online",
    )
    ranking = rankings.DailyRankings()
    runtime = AutomaticWhitelist(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter])),
        ranking,
    )
    try:
        yield adapter, ranking, runtime, tmp_path, clock
    finally:
        runtime.active = False
        await ledger.close()


async def message(adapter, uid, mid, stamp, text="valid conversation"):
    await adapter.ledger.ingest(
        adapter.account,
        "5",
        mid,
        {
            "sender": uid,
            "name": f"Member {uid}",
            "text": text,
            "recall_route": {"peer": adapter.members["5"][uid], "time": stamp * 1000},
        },
    )


@pytest.mark.asyncio
async def test_top_thirty_exact_scope_binding_and_daily_expiry(daily):
    adapter, ranking, runtime, root, clock = daily
    for uid in range(10, 43):
        await message(adapter, str(uid), str(uid), clock[0] - 100)
    await runtime.step()
    entries = automatic_whitelist.read_automatic("daily", "1", "5")
    assert len(entries) == 30
    assert set(entries) == {str(uid) for uid in range(10, 40)}
    assert is_exempt("daily", "1", "5", "10", "910")
    assert not is_exempt("daily", "2", "5", "10", "910")
    assert not is_exempt("daily", "1", "6", "10", "910")
    assert not is_exempt("daily", "1", "5", "10", "999")
    assert not (root / "member_exemptions.sqlite3").exists()
    _version, displayed = read_exemptions(
        "daily", "1", "5", include_names=True, include_automatic=True
    )
    assert displayed["10"]["source"] == "daily_top30"
    clock[0] += 91
    assert not is_exempt("daily", "1", "5", "10", "910")
    await runtime.step()
    assert is_exempt("daily", "1", "5", "10", "910")
    clock[0] = datetime(2026, 10, 25, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert not is_exempt("daily", "1", "5", "10", "910")
    await runtime.step()
    assert not automatic_whitelist.read_automatic("daily", "1", "5")


@pytest.mark.asyncio
async def test_recalls_and_unsettled_messages_never_award_automatic_grants(daily):
    adapter, ranking, runtime, root, clock = daily
    await message(adapter, "10", "100", clock[0] - 100)
    await message(adapter, "11", "101", clock[0] - 100)
    await message(adapter, "12", "102", clock[0] - 100)
    await message(adapter, "13", "103", clock[0] - 5)
    await adapter.ledger.db.execute(
        "INSERT INTO observed_recalls VALUES('1','5','notice','55','100','','910','999','','',?)",
        (clock[0],),
    )
    await adapter.ledger.db.execute(
        "INSERT INTO outbox VALUES('recall/1/5/101','','native','accepted','')"
    )
    await adapter.ledger.db.commit()
    with sqlite3.connect(root / "moderation.sqlite3") as db:
        db.executescript("""
            CREATE TABLE flood_batches(operation TEXT,last_seen REAL,account TEXT,group_id TEXT);
            CREATE TABLE flood_recalls(account TEXT,group_id TEXT,message TEXT,batch TEXT,status TEXT);
        """)
        db.execute("INSERT INTO flood_batches VALUES('burst',?,'1','5')", (clock[0],))
        db.execute("INSERT INTO flood_recalls VALUES('1','5','102','burst','accepted')")
    await runtime.step()
    assert not automatic_whitelist.read_automatic("daily", "1", "5")
    text = await ranking.read(adapter, "5", "10")
    assert (
        "Member 10" not in text and "Member 11" not in text and "Member 12" not in text
    )
    assert "Member 13" in text
    clock[0] += 31
    ranking.cache.clear()
    await runtime.step()
    assert set(automatic_whitelist.read_automatic("daily", "1", "5")) == {"13"}


@pytest.mark.asyncio
async def test_manual_removal_suppresses_today_but_does_not_erase_manual_grants(daily):
    adapter, ranking, runtime, root, clock = daily
    await message(adapter, "10", "100", clock[0] - 100)
    await runtime.step()
    options = {
        "enabled": False,
        "actor": "server/test",
        "reason": "explicit removal",
        "guard": lambda: None,
    }
    set_exemption("daily", "1", "5", "10", "910", **options)
    assert not is_exempt("daily", "1", "5", "10", "910")
    await runtime.step()
    assert not is_exempt("daily", "1", "5", "10", "910")
    set_exemption("daily", "1", "5", "11", "911", **{**options, "enabled": True})
    clock[0] += 86400
    assert is_exempt("daily", "1", "5", "11", "911")
    await message(adapter, "10", "next-day", clock[0] - 100)
    await runtime.step()
    assert is_exempt("daily", "1", "5", "10", "910")


@pytest.mark.asyncio
async def test_blacklist_immediately_overrides_automatic_grant_and_replacement(daily):
    adapter, ranking, runtime, root, clock = daily
    await message(adapter, "10", "100", clock[0] - 100)
    await runtime.step()
    assert is_exempt("daily", "1", "5", "10", "910")
    with sqlite3.connect(root / "member_blacklist.sqlite3") as db:
        db.execute(
            "CREATE TABLE blacklist_entries(account TEXT,group_id TEXT,member TEXT,status TEXT)"
        )
        db.execute("INSERT INTO blacklist_entries VALUES('1','5','10','active')")
    assert not is_exempt("daily", "1", "5", "10", "910")
    await runtime.step()
    assert not automatic_whitelist.read_automatic("daily", "1", "5")


@pytest.mark.asyncio
async def test_reauthorization_rejects_stale_mapping_and_cancelled_runtime(daily):
    adapter, ranking, runtime, root, clock = daily
    await message(adapter, "10", "100", clock[0] - 100)
    date, end, ranked = await ranking.snapshot(adapter, "5", settled=True)
    mapping = dict(adapter.members["5"])
    adapter.members["5"]["10"] = "changed"
    with pytest.raises(ProtocolError):
        await asyncio.to_thread(
            runtime.publish, adapter, "5", "1", mapping, date, end, ranked
        )
    runtime.active = False
    with pytest.raises(ProtocolError):
        await asyncio.to_thread(
            runtime.publish,
            adapter,
            "5",
            "1",
            dict(adapter.members["5"]),
            date,
            end,
            ranked,
        )


@pytest.mark.asyncio
async def test_paused_or_unmanaged_group_never_receives_automatic_grants(daily):
    adapter, ranking, runtime, root, clock = daily
    await message(adapter, "10", "100", clock[0] - 100)
    adapter.config["_open_groups"] = {}
    await runtime.step()
    assert not automatic_whitelist.read_automatic("daily", "1", "5")
    assert not (root / "automatic_exemptions.sqlite3").exists()
