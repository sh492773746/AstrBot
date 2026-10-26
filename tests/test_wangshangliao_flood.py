"""Burst-wide sanctions through real ingress storage and the V1 action port."""

import asyncio
import copy
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import content_rules, policy
from astrbot.builtin_stars.wangshangliao_moderation.flood import (
    FloodRuntime,
    FloodWindows,
)
from astrbot.builtin_stars.wangshangliao_moderation.review_store import (
    ReviewStore,
    review_store,
)
from astrbot.builtin_stars.wangshangliao_moderation.settings import BusinessSettings
from astrbot.core.platform.sources.wangshangliao import automatic, wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.runtime_policy import PolicySnapshot
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.fixtures.wsl_actions import bind_action_port


@pytest_asyncio.fixture
async def fresh_bot(tmp_path, monkeypatch):
    for module in (automatic, content_rules):
        monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    calls = []
    adapter = SimpleNamespace(
        account="1",
        config={
            "type": "wangshangliao",
            "id": "fixture",
            "enabled_groups": ["5"],
            "_business_revision": "1:1",
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "progressive_mute": True,
                "recall_enabled": True,
                "content_rules_since": time.time() - 60,
                "permissions": {"5": ["mute", "recall"]},
            },
        },
        ledger=ledger,
        members={"5": {"2": "22"}},
        groups={"5": "55"},
        stopping=asyncio.Event(),
        connection_state="online",
        send_lock=asyncio.Lock(),
        diagnostics=SimpleNamespace(emit=Mock()),
        nim=SimpleNamespace(request=AsyncMock()),
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "1", "groupRole": "GROUP_ROLE_ADMIN"},
                    {"userId": "2", "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"},
                ],
            }
        ),
        calls=calls,
        settings=BusinessSettings(None, tmp_path / "settings"),
    )

    async def mute(
        operation, action, group, member, text="", *, minutes=1, action_check=None
    ):
        if action_check:
            await action_check()
        calls.append((action, str(member), minutes))
        return {
            "status": "accepted",
            "action": action,
            "group": group,
            "member": member,
        }

    async def recall(service, command, encoded):
        assert (service, command) == (7, 13)
        properties, _ = wire.read_properties(encoded)
        calls.append(("recall", properties[11].decode(), properties[3].decode()))
        return 200, b""

    adapter.execute_moderation = AsyncMock(side_effect=mute)
    adapter.diagnostics.observe = Mock()
    adapter.nim.request.side_effect = recall
    adapter.recall_violation = WangshangliaoAdapter.recall_violation.__get__(adapter)
    bind_action_port(adapter)
    try:
        yield adapter
    finally:
        await ledger.close()


@pytest.fixture
def bot(fresh_bot, tmp_path):
    """Reach the mute boundary without contaminating first-warning fixtures."""
    from astrbot.core.platform.sources.wangshangliao.progression_store import (
        ProgressionStore,
    )

    store = ProgressionStore(tmp_path / "moderation.sqlite3")
    store.initialize()
    with store.connect() as db:
        db.executemany(
            "INSERT INTO progressive_warnings VALUES('1','5','2',?,'accepted')",
            [(f"prior-warning-{n}",) for n in range(3)],
        )
    return fresh_bot


async def ingest(
    bot, mid, text, *, sender="2", group="5", account=None, stamp=None, kind=None
):
    """Collect one authenticated test envelope with the production ingress observer.

    Args:
        bot: Isolated native adapter.
        mid: Numeric upstream message ID.
        text: Message text.
        sender: Business member ID.
        group: Business group ID.
        account: Optional collector account.
        stamp: Optional upstream time in milliseconds.
        kind: Optional authenticated media kind, without downloading its content.

    Returns:
        The stored immutable payload.
    """
    account = account or bot.account
    envelope = {
        "sender": sender,
        "text": text,
        "recall_route": {
            "time": str(stamp if stamp is not None else int(time.time() * 1000)),
            "client": "client-" + mid,
            "peer": "22" if sender == "2" else "33",
        },
    }
    if kind:
        envelope["kind"] = kind

    def observe(payload, received_at):
        snapshot = PolicySnapshot(
            account,
            bot.config["_business_revision"],
            json.dumps(
                {
                    "enabled_groups": bot.config["enabled_groups"],
                    "moderation": bot.config["moderation"],
                    **{
                        key: value
                        for key, value in bot.config.items()
                        if key.startswith("_open_")
                    },
                }
            ),
            0,
        )
        bot.settings.observe(snapshot, account, group, mid, payload, received_at)

    await bot.ledger.ingest(account, group, mid, envelope, observe=observe)
    async with bot.ledger.db.execute(
        "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
        (account, group, mid),
    ) as cursor:
        return json.loads((await cursor.fetchone())[0])


async def handle(bot, mid, envelope):
    """Run the production flood decision and business enforcement boundary.

    Args:
        bot: Isolated native adapter.
        mid: Trigger message ID.
        envelope: Stored trusted payload.

    Returns:
        Whether the event was consumed.
    """
    category = content_rules.flood_category(bot, "5", mid, envelope)
    if not category:
        return False
    return await policy.handle(
        bot,
        "5",
        mid,
        envelope,
        assessment={"decision": "violation", "category": category, "message_id": mid},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["stopping", "disabled"])
async def test_flood_scanner_skips_inactive_adapter_even_while_online(
    bot, monkeypatch, state
):
    if state == "stopping":
        bot.stopping.set()
    else:
        bot.config["enable"] = False
    query = Mock(side_effect=AssertionError("Inactive adapter must not scan"))
    monkeypatch.setattr(bot.ledger.db, "execute", query)
    runtime = FloodRuntime(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[bot]))
    )
    try:
        await runtime.step()
        query.assert_not_called()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_flood_scanner_rechecks_stop_after_waiting_for_ingress_lock(bot):
    runtime = FloodRuntime(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[bot]))
    )
    await bot.ledger.ingress_lock.acquire()
    task = asyncio.create_task(runtime.step())
    try:
        await asyncio.sleep(0.02)
        assert not task.done()
        bot.stopping.set()
    finally:
        bot.ledger.ingress_lock.release()
        await task
        await runtime.close()
    assert not runtime.pending and not runtime.workers


@pytest.mark.asyncio
async def test_ledger_close_waits_for_flood_scan_and_drops_stopping_results(
    bot, monkeypatch
):
    for index in range(5):
        await ingest(bot, str(8800 + index), "fixture")
    runtime = FloodRuntime(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[bot]))
    )
    entered, release = asyncio.Event(), asyncio.Event()
    execute = bot.ledger.db.execute

    @asynccontextmanager
    async def delayed_query(*args, **kwargs):
        async with execute(*args, **kwargs) as cursor:
            entered.set()
            await release.wait()
            yield cursor

    monkeypatch.setattr(bot.ledger.db, "execute", delayed_query)
    scanning = asyncio.create_task(runtime.step())
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        bot.stopping.set()
        closing = asyncio.create_task(bot.ledger.close())
        await asyncio.sleep(0.02)
        assert not closing.done()
        assert not runtime.cursors and not runtime.pending and not runtime.workers
    finally:
        release.set()
        await asyncio.gather(scanning, *([closing] if closing else []))
        await runtime.close()
    assert bot.ledger.db is None
    assert not runtime.cursors and not runtime.pending and not runtime.workers


@pytest.mark.asyncio
async def test_flood_scan_does_not_suppress_real_database_faults(bot, monkeypatch):
    query = Mock(side_effect=sqlite3.OperationalError("fixture database fault"))
    monkeypatch.setattr(bot.ledger.db, "execute", query)
    runtime = FloodRuntime(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[bot]))
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="fixture database fault"):
            await runtime.step()
        query.assert_called_once()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_concurrent_ledger_close_detaches_once_before_awaiting_io(
    bot, monkeypatch
):
    connection = bot.ledger.db
    entered, release = asyncio.Event(), asyncio.Event()
    close = connection.close
    count = 0

    async def delayed_close():
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        await close()

    monkeypatch.setattr(connection, "close", delayed_close)
    first = asyncio.create_task(bot.ledger.close())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert bot.ledger.db is None
        second = asyncio.create_task(bot.ledger.close())
        await asyncio.sleep(0.02)
        assert count == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []))
    await bot.ledger.close()
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "forward"])
@pytest.mark.parametrize("flood_enabled", [False, True])
@pytest.mark.parametrize("integrated", [False, True])
async def test_single_media_never_becomes_a_violation(
    bot, kind, flood_enabled, integrated
):
    bot.config["moderation"]["flood_enabled"] = flood_enabled
    if integrated:
        bot.config.update(
            _open_groups={
                "5": {"advertising": True, "flood": flood_enabled, "keywords": []}
            },
            _open_revisions={"5": "group-5-v1"},
            _open_native_roles={"5": "GROUP_ROLE_ADMIN"},
        )
    envelope = await ingest(bot, "800", "", kind=kind)
    assert not await policy.handle(bot, "5", "800", envelope)
    assert bot.calls == []
    bot.execute_moderation.assert_not_awaited()
    bot.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "forward"])
async def test_queued_retired_media_assessment_cannot_trigger_punishment(bot, kind):
    envelope = await ingest(bot, "800", "", kind=kind)
    assert not await policy.handle(
        bot,
        "5",
        "800",
        envelope,
        assessment={
            "decision": "violation",
            "category": "prohibited_" + kind,
            "message_id": "800",
        },
    )
    assert bot.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "forward"])
async def test_media_with_confirmed_ad_text_still_uses_normal_ad_rule(bot, kind):
    with sqlite3.connect(bot.ledger.path.with_name("moderation.sqlite3")) as db:
        db.execute("DELETE FROM progressive_warnings")
    bot.config.update(
        _open_groups={
            "5": {"advertising": True, "flood": False, "keywords": ["fixture-ad"]}
        },
        _open_revisions={"5": "group-5-v1"},
        _open_native_roles={"5": "GROUP_ROLE_ADMIN"},
    )
    envelope = await ingest(bot, "800", "fixture-ad", kind=kind)
    assert await policy.handle(bot, "5", "800", envelope)
    assert bot.calls == [("recall", "800", "22")]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "forward"])
async def test_flood_worker_does_not_punish_single_media(bot, kind):
    bot.config["moderation"]["flood_enabled"] = False
    await ingest(bot, "800", "", kind=kind)
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[bot]),
        get_config=lambda _: {},
    )
    runtime = FloodRuntime(context)
    try:
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == []
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "forward"])
async def test_flood_worker_still_punishes_a_real_media_burst(bot, kind):
    for n in range(5):
        await ingest(bot, str(800 + n), "", kind=kind)
    runtime = FloodRuntime(
        SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[bot]),
            get_config=lambda _: {},
        )
    )
    try:
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == [("mute", "2", 1)] + [
            ("recall", str(800 + n), "22") for n in range(5)
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("count,same", [(5, False), (5, True)])
async def test_mute_first_then_recall_entire_burst_after_warning_grace(
    bot, count, same
):
    for n in range(count):
        mid = str(100 + n)
        envelope = await ingest(bot, mid, "repeat" if same else str(n))
        if n < count - 1:
            assert not await handle(bot, mid, envelope)
    assert await handle(bot, mid, envelope)
    assert bot.calls == [("mute", "2", 1)] + [
        ("recall", str(100 + n), "22") for n in range(count)
    ]
    assert len(bot.calls) == count + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_images_and_mixed_messages_mute_first_then_recall_all(bot, mixed):
    for n in range(5):
        image = not mixed or n % 2 == 0
        mid = str(900 + n)
        envelope = await ingest(
            bot, mid, "" if image else f"text-{n}", kind="image" if image else None
        )
        if n < 4:
            assert not await handle(bot, mid, envelope)
    assert await handle(bot, mid, envelope)
    assert bot.calls == [("mute", "2", 1)] + [
        ("recall", str(900 + n), "22") for n in range(5)
    ]


@pytest.mark.asyncio
async def test_images_are_not_fake_repeated_text_and_duplicates_do_not_count(bot):
    for n in range(2):
        mid = str(900 + n)
        envelope = await ingest(bot, mid, "", kind="image")
        assert not await handle(bot, mid, envelope)
        duplicate = await ingest(bot, mid, "", kind="image")
        assert duplicate == envelope
    assert not bot.calls


@pytest.mark.asyncio
async def test_integrated_flood_uses_frozen_group_revision_not_account_revision(bot):
    bot.config.update(
        _open_integrated=True,
        _open_groups={"5": {"advertising": False, "flood": True, "keywords": []}},
        _open_revisions={"5": "1/5/9/1"},
        _open_managers={"5": []},
        _open_native_roles={"5": "GROUP_ROLE_ADMIN"},
    )
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    assert envelope["_ingress_flood"]["revision"] == "1/5/9/1"
    assert bot.config["_business_revision"] == "1:1"
    assert await handle(bot, "104", envelope)
    assert bot.calls == [("mute", "2", 1)] + [
        ("recall", str(100 + n), "22") for n in range(5)
    ]


@pytest.mark.asyncio
async def test_burst_dedup_additional_message_and_reload_never_remute(bot):
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    await handle(bot, "104", envelope)
    await handle(bot, "104", envelope)
    bot.settings.flood_windows = FloodWindows()
    async with bot.ledger.db.execute(
        "SELECT team,message,payload,received_at FROM inbox ORDER BY received_at"
    ) as cursor:
        rows = [(r[0], r[1], json.loads(r[2]), r[3]) for r in await cursor.fetchall()]
    bot.settings.flood_windows.restore("1", rows)
    extra = await ingest(bot, "105", "repeat")
    await handle(bot, "105", extra)
    assert [call for call in bot.calls if call[0] == "mute"] == [("mute", "2", 1)]
    assert [call[1] for call in bot.calls if call[0] == "recall"] == [
        str(100 + n) for n in range(6)
    ]


@pytest.mark.asyncio
async def test_repeated_text_excludes_other_text_members_groups_and_accounts(
    bot, monkeypatch
):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now - 6)
    await ingest(bot, "90", "normal")
    await ingest(bot, "91", "repeat", sender="3")
    await ingest(bot, "92", "repeat", group="6")
    await ingest(bot, "93", "repeat", account="9")
    monkeypatch.setattr(time, "time", lambda: now)
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    await handle(bot, "104", envelope)
    assert bot.calls[1:] == [("recall", str(100 + n), "22") for n in range(5)]


@pytest.mark.asyncio
async def test_unknown_recall_does_not_skip_rest_or_retry(bot):
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    original = bot.nim.request.side_effect

    async def timeout(service, command, encoded):
        properties, _ = wire.read_properties(encoded)
        if properties[11] == b"101":
            bot.calls.append(("recall", "101", "unknown"))
            raise TimeoutError()
        return await original(service, command, encoded)

    bot.nim.request.side_effect = timeout
    await handle(bot, "104", envelope)
    extra = await ingest(bot, "105", "repeat")
    await handle(bot, "105", extra)
    assert len([c for c in bot.calls if c[0] == "mute"]) == 1
    assert [c[1] for c in bot.calls if c[0] == "recall"] == [
        str(100 + n) for n in range(6)
    ]
    store = bot._review_store
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT status FROM flood_recalls WHERE message='101'"
            ).fetchone()[0]
            == "unknown"
        )


@pytest.mark.asyncio
async def test_unknown_mute_still_recalls_and_never_resends_mute(bot):
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    bot.execute_moderation.return_value = {"status": "unknown"}
    bot.execute_moderation.side_effect = None
    await handle(bot, "104", envelope)
    await handle(bot, "105", await ingest(bot, "105", "repeat"))
    assert bot.execute_moderation.await_count == 1
    assert len(bot.calls) == 6 and all(c[0] == "recall" for c in bot.calls)
    with bot._review_store.connect() as db:
        assert (
            db.execute("SELECT mute_status FROM flood_batches").fetchone()[0]
            == "unknown"
        )


@pytest.mark.asyncio
async def test_departure_after_mute_is_audited_as_unsubmitted_not_unknown(bot):
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), str(n))
    original = bot.execute_moderation.side_effect

    async def remove_after_mute(*args, **kwargs):
        result = await original(*args, **kwargs)
        bot.members["5"].pop("2")
        return result

    bot.execute_moderation.side_effect = remove_after_mute
    await handle(bot, "104", envelope)
    assert bot.calls == [("mute", "2", 1)]
    bot.nim.request.assert_not_awaited()
    with bot._review_store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM flood_recalls WHERE status='rejected'"
            ).fetchone()[0]
            == 5
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM moderation_action_audit "
                "WHERE action='recall' AND status='rejected' AND submitted=0 "
                "AND error='action_automation_changed'"
            ).fetchone()[0]
            == 5
        )


@pytest.mark.asyncio
async def test_separate_bursts_escalate_only_once_each_and_cap_at_ten(bot, monkeypatch):
    now = time.time()
    for burst, minutes in enumerate((1, 3, 10)):
        monkeypatch.setattr(time, "time", lambda: now + burst * 31)
        for n in range(5):
            mid = str(100 + burst * 10 + n)
            envelope = await ingest(bot, mid, "repeat")
        await handle(bot, mid, envelope)
        assert [c[2] for c in bot.calls if c[0] == "mute"][-1] == minutes
    assert len([c for c in bot.calls if c[0] == "mute"]) == 3
    assert len([c for c in bot.calls if c[0] == "recall"]) == 15


@pytest.mark.asyncio
async def test_fresh_flood_cycles_warn_three_times_and_count_whole_burst_once(
    bot, monkeypatch
):
    with sqlite3.connect(bot.ledger.path.with_name("moderation.sqlite3")) as db:
        db.execute("DELETE FROM progressive_warnings")
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    for burst in range(7):
        for n in range(5):
            mid = str(1000 + burst * 10 + n)
            envelope = await ingest(bot, mid, f"unique-{mid}")
        assert await handle(bot, mid, envelope)
        await handle(bot, mid, envelope)
        expected = [1, 3, 10][: max(0, min(3, burst - 2))]
        assert [c[2] for c in bot.calls if c[0] == "mute"] == expected
        clock += 61 if burst == 3 else 181 if burst == 4 else 601 if burst == 5 else 31
    assert len([c for c in bot.calls if c[0] == "recall"]) == 35
    with sqlite3.connect(bot.ledger.path.with_name("moderation.sqlite3")) as db:
        assert db.execute("SELECT warnings FROM moderation_cycles").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM progressive_warnings").fetchone() == (
            4,
        )


@pytest.mark.asyncio
async def test_main_event_gate_stops_after_complete_flood_sanction(bot):
    from astrbot.builtin_stars.wangshangliao_moderation.main import Main

    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    event = SimpleNamespace(
        platform=bot,
        message_str="repeat",
        message_obj=SimpleNamespace(message_id="104"),
        get_platform_name=lambda: "wangshangliao",
        get_extra=lambda name: envelope if name == "wangshangliao_payload" else None,
        is_private_chat=lambda: False,
        is_admin=lambda: False,
        get_group_id=lambda: "5",
        stop_event=Mock(),
    )
    await Main._moderate(SimpleNamespace(), event)
    event.stop_event.assert_called_once()
    assert bot.calls == [("mute", "2", 1)] + [
        ("recall", str(100 + n), "22") for n in range(5)
    ]


@pytest.mark.asyncio
async def test_fast_flood_does_not_wait_for_an_earlier_model_and_keeps_idempotency(bot):
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[bot]),
        get_config=lambda _: {"admins_id": []},
    )
    runtime = FloodRuntime(context)
    slow_model = asyncio.Event()
    model_task = asyncio.create_task(slow_model.wait())
    try:
        for n in range(5):
            envelope = await ingest(bot, str(100 + n), "repeat")
        for _ in range(3):
            await runtime.step()
            await asyncio.gather(*runtime.workers.values())
        assert not model_task.done()
        assert bot.calls == [("mute", "2", 1)] + [
            ("recall", str(100 + n), "22") for n in range(5)
        ]
        await handle(bot, "104", envelope)
        assert len(bot.calls) == 6
        timing = [
            call
            for call in bot.diagnostics.emit.call_args_list
            if call.args[0] == "flood_response"
        ]
        assert len(timing) == 1
        assert timing[0].args[1] == "completed"
        assert timing[0].kwargs["duration_ms"] >= 0
    finally:
        model_task.cancel()
        await asyncio.gather(model_task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_recalled_queued_message_does_not_enter_ai_or_customer_processing(
    bot, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import main

    for n in range(5):
        await ingest(bot, str(100 + n), "repeat")
    runtime = FloodRuntime(
        SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[bot]),
            get_config=lambda _: {"admins_id": []},
        )
    )
    try:
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assessment = AsyncMock()
        monkeypatch.setattr(main, "assess", assessment)
        async with bot.ledger.db.execute(
            "SELECT payload FROM inbox WHERE message='100'"
        ) as cursor:
            payload = json.loads((await cursor.fetchone())[0])
        event = SimpleNamespace(
            platform=bot,
            message_str="repeat",
            message_obj=SimpleNamespace(message_id="100"),
            get_platform_name=lambda: "wangshangliao",
            get_extra=lambda name: payload if name == "wangshangliao_payload" else None,
            is_private_chat=lambda: False,
            is_admin=lambda: False,
            get_group_id=lambda: "5",
            stop_event=Mock(),
        )
        await main.Main._moderate(SimpleNamespace(), event)
        event.stop_event.assert_called_once()
        assessment.assert_not_awaited()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_late_model_result_for_a_recalled_message_is_not_sent(bot):
    from astrbot.api.message_components import Plain
    from astrbot.builtin_stars.wangshangliao_moderation.main import Main

    key = "recall/1/5/100"
    await bot.ledger.reserve(key, "{}")
    await bot.ledger.finish(key, "accepted", "100")
    result = SimpleNamespace(
        chain=[Plain("late model reply")], is_llm_result=lambda: True, use_t2i=Mock()
    )
    event = SimpleNamespace(
        platform=bot,
        message_obj=SimpleNamespace(message_id="100"),
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: False,
        get_group_id=lambda: "5",
        get_sender_id=lambda: "2",
        get_result=lambda: result,
        stop_event=Mock(),
    )
    await Main.plain_text_result(None, event)
    assert not result.chain
    event.stop_event.assert_called_once()
    result.use_t2i.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "review", "skip", "violation"])
async def test_recalled_message_is_gated_after_inflight_model_review(
    bot, monkeypatch, decision
):
    from astrbot.builtin_stars.wangshangliao_moderation import main

    bot.config["moderation"]["semantic"] = {"enabled": True}
    payload = await ingest(bot, "100", "repeat")
    entered, release = asyncio.Event(), asyncio.Event()

    async def assess(*_):
        entered.set()
        await release.wait()
        return {"decision": decision}

    monkeypatch.setattr(main, "assess", assess)
    event = SimpleNamespace(
        platform=bot,
        message_str="repeat",
        message_obj=SimpleNamespace(message_id="100"),
        get_platform_name=lambda: "wangshangliao",
        get_extra=lambda name: payload if name == "wangshangliao_payload" else None,
        is_private_chat=lambda: False,
        is_admin=lambda: False,
        get_group_id=lambda: "5",
        get_sender_id=lambda: "2",
        stop_event=Mock(),
    )
    runtime = FloodRuntime(
        SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[bot]),
            get_config=lambda _: {"admins_id": []},
        )
    )
    task = asyncio.create_task(
        main.Main._moderate(SimpleNamespace(context=None), event)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for mid in ("101", "102", "103", "104"):
            await ingest(bot, mid, "repeat")
        for _ in range(3):
            await runtime.step()
            await asyncio.gather(*runtime.workers.values())
        release.set()
        await task
        event.stop_event.assert_called_once()
        assert len([c for c in bot.calls if c[0] == "mute"]) == 1
        assert len([c for c in bot.calls if c[0] == "recall"]) == 5
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_recalled_message_is_gated_after_customer_slot_wait(bot, monkeypatch):
    from astrbot.builtin_stars.wangshangliao_moderation import main

    bot.config["moderation"]["semantic"] = {"enabled": True}
    payload = await ingest(bot, "100", "repeat")
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(main, "assess", AsyncMock(return_value={"decision": "allow"}))

    async def customer(_):
        entered.set()
        await release.wait()
        return True

    event = SimpleNamespace(
        platform=bot,
        message_str="repeat",
        message_obj=SimpleNamespace(message_id="100"),
        get_platform_name=lambda: "wangshangliao",
        get_extra=lambda name: payload if name == "wangshangliao_payload" else None,
        is_private_chat=lambda: False,
        is_admin=lambda: False,
        get_group_id=lambda: "5",
        get_sender_id=lambda: "2",
        stop_event=Mock(),
        is_stopped=lambda: False,
        eligible=True,
        processing_completion=asyncio.get_running_loop().create_future(),
    )
    runtime = FloodRuntime(
        SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[bot]),
            get_config=lambda _: {"admins_id": []},
        )
    )
    handler = SimpleNamespace(
        context=None, ai_runtime=SimpleNamespace(customer=customer)
    )
    task = asyncio.create_task(main.Main.moderate(handler, event))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for mid in ("101", "102", "103", "104"):
            await ingest(bot, mid, "repeat")
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        release.set()
        await task
        event.stop_event.assert_called_once()
    finally:
        task.cancel()
        event.processing_completion.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "admin",
        "managed",
        "account",
        "revision",
        "epoch",
        "offline",
        "stopped",
        "closed",
    ],
)
async def test_fast_flood_rejects_changed_authority_before_any_action(
    bot, change, monkeypatch
):
    admins = []
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[bot]),
        get_config=lambda _: {"admins_id": admins},
    )
    runtime = FloodRuntime(context)
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    if change == "admin":
        admins.append("2")
    elif change == "managed":
        from astrbot.core.platform.sources.wangshangliao import event

        monkeypatch.setattr(event, "is_managed_account", lambda _: True)
    elif change == "account":
        bot.account = "9"
    elif change == "revision":
        bot.config["_business_revision"] = "new"
    elif change == "epoch":
        bot.config["_business_epoch"] = 2
    elif change == "offline":
        bot.connection_state = "reconnecting"
    elif change == "stopped":
        bot.stopping.set()
    else:
        runtime.active = False
    await runtime.execute(("fixture", "1", "5", "2"), bot, "104", envelope, None)
    assert not bot.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["directory", "after_mute"])
async def test_fast_flood_rechecks_chat_admin_role_after_awaits(bot, when):
    admins = []
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[bot]),
        get_config=lambda _: {"admins_id": admins},
    )
    runtime = FloodRuntime(context)
    for n in range(5):
        await ingest(bot, str(100 + n), "repeat")
    if when == "directory":
        roster = bot.get_moderation_members.return_value

        async def promote(_):
            admins.append("2")
            return roster

        bot.get_moderation_members.side_effect = promote
    else:
        original = bot.execute_moderation.side_effect

        async def promote(*args, **kwargs):
            receipt = await original(*args, **kwargs)
            admins.append("2")
            return receipt

        bot.execute_moderation.side_effect = promote
    try:
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == ([] if when == "directory" else [("mute", "2", 1)])
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_fast_flood_backlog_stays_bounded_without_losing_the_last_trigger(bot):
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[bot]),
        get_config=lambda _: {"admins_id": []},
    )
    runtime = FloodRuntime(context, slots=1, backlog=2)
    for n in range(9):
        await ingest(bot, str(100 + n), "repeat")
    try:
        for _ in range(10):
            await runtime.step()
            assert sum(len(queue) for queue in runtime.pending.values()) <= 2
            assert len(runtime.workers) <= 1
            await asyncio.gather(*runtime.workers.values())
        assert [call for call in bot.calls if call[0] == "mute"] == [("mute", "2", 1)]
        assert [call[1] for call in bot.calls if call[0] == "recall"] == [
            str(100 + n) for n in range(9)
        ]
    finally:
        await runtime.close()
    assert not runtime.workers and not runtime.pending and not runtime.cursors


@pytest.mark.asyncio
async def test_cancellation_quarantines_reserved_recalls_without_replaying(bot):
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    original = bot.nim.request.side_effect
    bot.nim.request.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await handle(bot, "104", envelope)
    bot.nim.request.side_effect = original
    await handle(bot, "105", await ingest(bot, "105", "repeat"))
    assert bot.execute_moderation.await_count == 1
    assert bot.nim.request.await_count == 2
    with bot._review_store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM flood_recalls WHERE status='unknown'"
            ).fetchone()[0]
            == 5
        )


@pytest.mark.asyncio
async def test_overlapping_triggers_are_serialized_and_do_not_double_recall(bot):
    for n in range(5):
        fifth = await ingest(bot, str(100 + n), "repeat")
    sixth = await ingest(bot, "105", "repeat")
    await asyncio.gather(handle(bot, "104", fifth), handle(bot, "105", sixth))
    assert bot.execute_moderation.await_count == 1
    assert sorted(c[1] for c in bot.calls if c[0] == "recall") == [
        str(100 + n) for n in range(6)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "permission", "account", "disabled"])
async def test_revocation_after_mute_stops_all_recalls(bot, change):
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    original = bot.execute_moderation.side_effect

    async def revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "revision":
            bot.config["_business_revision"] = "1:2"
        elif change == "permission":
            bot.config["moderation"]["permissions"] = {}
        elif change == "account":
            bot.account = "9"
        else:
            bot.config["moderation"]["automation_enabled"] = False
        return result

    bot.execute_moderation.side_effect = revoke
    await handle(bot, "104", envelope)
    assert bot.calls == [("mute", "2", 1)]
    with bot._review_store.connect() as db:
        assert db.execute(
            "SELECT account,mute_status FROM flood_batches"
        ).fetchone() == ("1", "accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["protected", "managed", "missing", "forged", "recall_disabled"]
)
async def test_unsafe_flood_evidence_cannot_sanction(bot, change, monkeypatch):
    for n in range(5):
        envelope = await ingest(bot, str(100 + n), "repeat")
    if change == "protected":
        bot.get_moderation_members.return_value["groupMemberInfo"][1]["groupRole"] = (
            "GROUP_ROLE_ADMIN"
        )
    elif change == "managed":
        from astrbot.core.platform.sources.wangshangliao import event

        monkeypatch.setattr(event, "is_managed_account", lambda account: account == "2")
    elif change == "missing":
        del bot.ledger
    elif change == "forged":
        envelope = copy.deepcopy(envelope)
        envelope["_ingress_flood"]["messages"].append("999")
    else:
        bot.config["moderation"]["recall_enabled"] = False
    await handle(bot, "104", envelope)
    assert not bot.calls


def test_revision_change_starts_a_new_detection_window():
    windows = FloodWindows()
    now = time.time()
    envelope = {"sender": "2", "text": "repeat", "recall_route": {"time": now * 1000}}
    for n in range(2):
        assert not windows.observe("1", "5", str(n), envelope, now, revision="old")[
            "category"
        ]
    assert not windows.observe("1", "5", "2", envelope, now, revision="new")["category"]


def test_hot_reload_replaces_the_previous_review_repository(tmp_path):
    path = tmp_path / "moderation.sqlite3"
    old = SimpleNamespace(path=path, initialized=True)
    adapter = SimpleNamespace(_review_store=old)
    store = review_store(adapter, path)
    assert isinstance(store, ReviewStore) and store is not old
    store.initialize()
    with store.connect() as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='flood_batches'"
        ).fetchone()
    assert review_store(adapter, path) is store


@pytest.mark.parametrize(
    "text",
    [
        "\u7a33\u8d5a\u4e0d\u8d54",
        "\u7a33 \u8d5a \u4e0d \u8d54",
        "\u7a33\u200b\u8d5a\u4e0d\u8d54",
    ],
)
def test_default_keywords_match_obfuscated_promotions(tmp_path, text):
    defaults = BusinessSettings(None, tmp_path).keyword_defaults
    assert content_rules.keyword_category(text, defaults) == "mute_keyword"


@pytest.mark.parametrize(
    "word",
    json.loads(
        (Path(content_rules.__file__).parent / "keyword_defaults.json").read_text(
            encoding="utf-8"
        )
    )["mute_keywords"],
)
def test_each_configured_default_matches_without_auto_kick(tmp_path, word):
    defaults = BusinessSettings(None, tmp_path).keyword_defaults
    assert len(defaults["mute_keywords"]) == len(set(defaults["mute_keywords"])) == 39
    assert not defaults["kick_keywords"]
    text = ("example" + word if word.startswith(".") else word) + " \u4e0a\u5206"
    assert (
        content_rules.keyword_category(text, {"mute_keywords": [word]})
        == "mute_keyword"
    )
    assert not content_rules.keyword_category("\u4e3e\u62a5\uff1a" + text, defaults)


@pytest.mark.parametrize(
    "text",
    [
        "\u5927\u6d77\u517c\u804c\u62db\u8058",
        "\u8fd4\u73b0\u5df2\u5230\u8d26",
        "\u4e3e\u62a5\uff1a\u7a33\u8d5a\u4e0d\u8d54\u662f\u9a97\u5c40",
        "\u4e0d\u8981\u52a0\u7a33\u8d5a\u4e0d\u8d54\u7684\u7fa4",
        "\u201c\u7a33\u8d5a\u4e0d\u8d54\u201d\u662f\u8bc8\u9a97\u8bdd\u672f",
        "\u8b66\u60d5\u7a33\u8d5a\u4e0d\u8d54\u7684\u9a97\u5c40",
        "\u7a33\u8d5a\u4e0d\u8d54\u90fd\u662f\u9a97\u4eba\u7684",
    ],
)
def test_defaults_preserve_business_messages_reports_and_quotes(tmp_path, text):
    defaults = BusinessSettings(None, tmp_path).keyword_defaults
    assert not content_rules.keyword_category(text, defaults)
