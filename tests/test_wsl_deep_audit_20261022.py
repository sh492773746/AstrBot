"""Offline regressions for asynchronous activity and scheduled-rule boundaries."""

import asyncio
import json
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import activity_store
from astrbot.builtin_stars.wangshangliao_moderation.lifecycle import clear_working_data
from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import (
    ADMISSION_WINDOW_SECONDS,
)
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_activities import configure, event, latest
from tests.test_wangshangliao_activities import setup as setup
from tests.test_wangshangliao_business_rules import event_for, keyword, save_rules
from tests.test_wangshangliao_business_rules import rules as rules

pytestmark = pytest.mark.usefixtures("wsl_active_policy")


@pytest.mark.asyncio
async def test_invitation_owner_revocation_disables_tracking_in_one_transaction(
    setup,
):
    service, adapter, _, _, now, config = setup
    caller = event(adapter)
    await service.command(caller, "5", "设置邀请奖励", "1.25")
    await service.command(caller, "5", "开启邀请奖励", "")
    with closing(activity_store.database(adapter)) as db:
        baseline = [tuple(row) for row in db.execute("SELECT * FROM invite_seen")]
    config["admins_id"] = []
    now[0] += 36
    await service.tick(adapter)
    with closing(activity_store.database(adapter)) as db:
        row = db.execute("SELECT * FROM invite_rules").fetchone()
        assert (
            row["enabled"],
            row["tracking_enabled"],
            row["presence_checked_at"],
        ) == (
            0,
            0,
            0,
        )
        assert row["error"] == "ProtocolError" and row["rate"] == 125
        assert [
            tuple(row) for row in db.execute("SELECT * FROM invite_seen")
        ] == baseline
    adapter.get_moderation_members.reset_mock()
    await service.tick(adapter)
    adapter.get_moderation_members.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["announcement", "result"])
@pytest.mark.parametrize("receipt", ["accepted", "unknown"])
async def test_late_lottery_receipt_cannot_resurrect_departed_activity(
    setup, tmp_path, stage, receipt
):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup)
    with closing(activity_store.database(adapter)) as db, db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode='ordinary',"
            "capacity=3,winners=3,remarks='private activity requirement'"
        )
    if stage == "result":
        await service.command(caller, "5", "开启抽奖", "")
        await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
        now[0] += 61

    async def depart(*args, **kwargs):
        await asyncio.to_thread(
            clear_working_data, tmp_path, "1", "5", "905", lambda: None
        )
        return receipt

    adapter.send_reply_text.side_effect = depart
    reply = (
        await service.command(caller, "5", "开启抽奖", "")
        if stage == "announcement"
        else await service.draw(adapter, latest(adapter)["id"])
    )
    row = latest(adapter)
    assert row["status"] == "cancelled" and row["error"] == "robot_departed"
    assert row["remarks"] == "" and row["results"] == "[]"
    assert "已开启" not in reply and "已开奖" not in reply
    before = adapter.send_reply_text.await_count
    await service.tick(adapter)
    assert adapter.send_reply_text.await_count == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["keyword", "advertisement"])
@pytest.mark.parametrize(
    "text", ["中" * 2000, "\U0001f600" * 2000], ids=["chinese", "emoji"]
)
@pytest.mark.parametrize("failure", [False, True])
async def test_long_rule_text_uses_native_logical_segments_without_replay(
    rules, tmp_path, monkeypatch, kind, text, failure
):
    adapter = WangshangliaoAdapter(
        {
            "id": "audit-long-rules",
            "account_id": "99",
            "enable": True,
            "_business_active": True,
            "enabled_groups": ["5"],
            "proactive_send": {"enabled": True, "targets": ["5"]},
        },
        {},
        asyncio.Queue(),
    )
    adapter.groups = {"5": "1005"}
    adapter.connection_state = "online"
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    adapter.ledger = Ledger(tmp_path / "long-rules.sqlite3")
    await adapter.ledger.open()
    sent = []

    async def request(*args):
        outer, _ = wire.read_properties(args[2])
        message = wire.open_message(bytes(32), outer[10].decode())
        sent.append(message.content.data)
        assert message.ByteSize() <= 4096
        if failure and len(sent) == 2:
            raise TimeoutError("isolated_transport_timeout")
        return 200, wire.properties([(7, b"1000000"), (12, str(len(sent)).encode())])

    adapter.nim = SimpleNamespace(request=AsyncMock(side_effect=request))
    monkeypatch.setattr(rules, "send_guard", lambda *args, **kwargs: None)
    try:
        if kind == "keyword":
            save_rules(rules, {"keywords": [keyword(text=text)]})
            incoming = event_for(rules.service)
            incoming.platform = adapter
            assert await rules.observe(incoming)
            count = len(sent)
            assert await rules.observe(incoming)
        else:
            save_rules(
                rules,
                {
                    "ads": [
                        {
                            "id": "a",
                            "name": "long",
                            "enabled": True,
                            "text": text,
                            "times": ["09:00"],
                        }
                    ]
                },
            )
            rules.started = time.time() - 10
            cursor = {
                "account": "99",
                "group_id": "5",
                "rule_id": "a",
                "next_at": time.time() - 0.1,
            }
            with rules.store.connect(write=True) as db:
                db.execute(
                    "UPDATE business_ad_cursors SET next_at=?", (cursor["next_at"],)
                )
            await rules.send_ad(adapter, cursor)
            count = len(sent)
            await rules.send_ad(adapter, cursor)
        assert count >= 2 and len(sent) == count
        if failure:
            assert count == 2 and text.startswith("".join(sent))
        else:
            assert "".join(sent) == text
        with rules.store.connect() as db:
            state = db.execute("SELECT state FROM business_rule_sends").fetchone()[0]
            assert state == ("unknown" if failure else "accepted")
            if kind == "advertisement":
                blocked = db.execute(
                    "SELECT blocked FROM business_ad_cursors"
                ).fetchone()[0]
                assert bool(blocked) == failure
        async with adapter.ledger.db.execute(
            "SELECT COUNT(*) FROM reply_recalls"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await adapter.ledger.close()


def test_removed_advertisements_do_not_leave_obsolete_rotation_rows(rules):
    for number in range(5):
        save_rules(
            rules,
            {
                "ads": [
                    {
                        "id": f"ad_{number}",
                        "name": "Notice",
                        "text": "Notice",
                        "times": ["09:00"],
                    }
                ]
            },
        )
    with rules.store.connect() as db:
        assert [
            tuple(row)
            for row in db.execute("SELECT rule_id,images FROM business_ad_rotations")
        ] == [("ad_4", json.dumps([]))]
    save_rules(rules, {})
    with rules.store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM business_ad_rotations").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_roster_wait_cannot_send_an_advertisement_after_its_live_window(
    rules, monkeypatch
):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    save_rules(
        rules,
        {
            "ads": [
                {
                    "id": "a",
                    "name": "Notice",
                    "text": "Notice",
                    "enabled": True,
                    "times": ["09:00"],
                }
            ]
        },
    )
    rules.started = now[0] - 10
    cursor = {
        "account": "99",
        "group_id": "5",
        "rule_id": "a",
        "next_at": now[0] - 0.1,
    }
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_ad_cursors SET next_at=?", (cursor["next_at"],))

    async def slow_roster(*args):
        now[0] += ADMISSION_WINDOW_SECONDS + 1
        return {}

    rules.service.roster.side_effect = slow_roster
    monkeypatch.setattr(rules, "send_guard", lambda *args, **kwargs: None)
    adapter = SimpleNamespace(send_text=AsyncMock(), send_reply_text=AsyncMock())
    await rules.send_ad(adapter, cursor)
    adapter.send_text.assert_not_awaited()
    adapter.send_reply_text.assert_not_awaited()
    with rules.store.connect() as db:
        evidence = db.execute(
            "SELECT state,error FROM business_rule_sends"
        ).fetchall()
        assert [tuple(row) for row in evidence] == [
            ("skipped", "advertisement_slot_expired")
        ]
        current = db.execute("SELECT * FROM business_ad_cursors").fetchone()
        assert current["next_at"] > now[0] and not current["blocked"]
