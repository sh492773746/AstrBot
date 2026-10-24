"""Offline regressions for original-window mute and flood authorization."""

import asyncio
import json
import re
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests.test_wangshangliao_three_rule_ingress import (
    GROUP,
    MARKER,
    SENDER,
    handle,
    ingest,
)
from tests.test_wangshangliao_three_rule_ingress import (
    three_rule_ingress as three_rule_ingress,
)


def seed_warning_grace(bot):
    """Start at the mute boundary only for native sanction authorization tests."""
    from astrbot.core.platform.sources.wangshangliao.progression_store import (
        ProgressionStore,
    )
    from astrbot.core.platform.sources.wangshangliao.storage import instance_dir

    store = ProgressionStore(instance_dir(bot.config["id"]) / "moderation.sqlite3")
    store.initialize()
    with store.connect() as db:
        db.executemany(
            "INSERT INTO progressive_warnings VALUES(?,?,?,?,?)",
            [(bot.account, GROUP, SENDER, f"prior-{i}", "accepted") for i in range(3)],
        )


@pytest_asyncio.fixture(params=["flood", "content"])
async def automatic_case(three_rule_ingress, request, tmp_path):
    """Reach each real automatic-mute caller with isolated native evidence.

    Args:
        three_rule_ingress: Real ingress and action chain with mocked upstream IO.
        request: Selects flood enforcement or the legacy content escalation.
        tmp_path: Root that must contain every moderation database.

    Returns:
        Harness, committed trigger and optional flood assessment.
    """
    from astrbot.core.platform.sources.wangshangliao.storage import instance_dir

    harness = three_rule_ingress
    bot = harness.receiver
    assert instance_dir(bot.config["id"]).is_relative_to(tmp_path)
    assessment = None
    if request.param == "flood":
        seed_warning_grace(bot)
        for n in range(5):
            mid = str(100 + n)
            payload = await ingest(harness, mid, MARKER + "repeat")
        assert payload["_ingress_flood"]["category"] == "message_flood"
        assessment = {
            "decision": "violation",
            "category": "message_flood",
            "message_id": mid,
        }
    else:
        # Public group views force progressive mode; exercise the legacy caller
        # through its real policy and accepted first-warning history instead.
        bot.config.pop("_open_groups")
        bot.config["moderation"].update(
            advertising_enabled=True,
            progressive_mute=False,
            mute_keywords=[MARKER],
            auto_kick={GROUP: True},
        )

        async def warning(group, key, text, *, authorize, **kwargs):
            assert group == GROUP
            await authorize()
            bot.calls.append(("warning", key))
            return "accepted"

        bot.send_text = AsyncMock(side_effect=warning)
        prior = await ingest(harness, "90", MARKER + "prior")
        assert await harness.policy.handle(bot, GROUP, "90", prior)
        assert [call[0] for call in bot.calls] == ["recall", "warning"]
        bot.calls.clear()
        bot.nim.request.reset_mock()
        mid = "100"
        payload = await ingest(harness, mid, MARKER + "repeat")
    return SimpleNamespace(
        harness=harness, mid=mid, payload=payload, assessment=assessment
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation", ["active", "expired", "closed", "replaced", "unmanaged"]
)
async def test_automatic_mute_keeps_original_guard_after_upstream_lock(
    automatic_case, revocation
):
    case = automatic_case
    harness, bot = case.harness, case.harness.receiver
    gate, entered = asyncio.Lock(), asyncio.Event()
    await gate.acquire()

    async def mute(
        operation, action, group, member, text="", *, minutes=1, action_check=None
    ):
        assert (action, str(group), str(member)) == ("mute", GROUP, SENDER)
        assert callable(action_check)
        entered.set()
        async with gate:
            await action_check()
            bot.calls.append(("mute", SENDER, minutes))
        return {
            "status": "accepted",
            "action": action,
            "group": group,
            "member": member,
        }

    bot.execute_moderation.side_effect = mute
    task = asyncio.create_task(
        harness.policy.handle(
            bot, GROUP, case.mid, case.payload, assessment=case.assessment
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        original_window = bot.test_window
        if revocation == "expired":
            original_window.expires = 0
        elif revocation == "closed":
            bot.test_window = None
        elif revocation == "replaced":
            bot.test_window = harness.Window(
                bot, harness.sender, [GROUP], ["group_rules"], [MARKER], 60, 10
            )
            await ingest(harness, "990", MARKER + "replacement")
            assert bot.test_window.admit(GROUP, case.mid, case.payload) == "group_rules"
            assert harness.test_rules.rule_target(bot, GROUP, SENDER, case.mid)
            assert bot.test_window is not original_window
        elif revocation == "unmanaged":
            import astrbot.core as core
            from astrbot.core.platform.sources.wangshangliao.event import (
                is_managed_account,
            )

            original_window.expires = 0
            core.astrbot_config["platform"][:] = [
                item
                for item in core.astrbot_config["platform"]
                if item["account_id"] != SENDER
            ]
            assert not is_managed_account(SENDER)
        gate.release()
        assert await asyncio.wait_for(task, timeout=5)
    finally:
        if gate.locked():
            gate.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    bot.execute_moderation.assert_awaited_once()
    mutes = [call for call in bot.calls if call[0] == "mute"]
    if revocation == "active":
        assert len(mutes) == 1
        if case.assessment:
            assert [call[1] for call in bot.calls if call[0] == "recall"] == [
                str(100 + n) for n in range(5)
            ]
    else:
        assert mutes == []
        if case.assessment:
            assert bot.calls == []
            bot.nim.request.assert_not_awaited()
        else:
            # The legacy path recalls before it reaches its mute IO wait.
            assert bot.calls == [("recall", case.mid, harness.sender.nim_account)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,same", [(5, True), (5, False)], ids=["5-repeats", "5-in-3"]
)
async def test_replacement_window_cannot_accumulate_or_recall_prior_window(
    three_rule_ingress, count, same
):
    harness = three_rule_ingress
    bot, original_window = harness.receiver, harness.window
    seed_warning_grace(bot)
    old_messages = [str(100 + n) for n in range(count // 2)]
    for n, mid in enumerate(old_messages):
        payload = await ingest(
            harness, mid, MARKER + ("repeat" if same else f"old-{n}")
        )
        assert payload["_ingress_flood"]["category"] == ""
    original_token = payload["_test_rule_window"]
    assert re.fullmatch(r"[0-9a-f]{32}", original_token)

    bot.test_window = harness.Window(
        bot, harness.sender, [GROUP], ["group_rules"], [MARKER], 60, 10
    )
    current_messages = [str(200 + n) for n in range(count)]
    for n, mid in enumerate(current_messages):
        payload = await ingest(
            harness, mid, MARKER + ("repeat" if same else f"new-{n}")
        )
        token = payload["_test_rule_window"]
        evidence = payload["_ingress_flood"]
        assert re.fullmatch(r"[0-9a-f]{32}", token)
        assert token != original_token
        assert evidence["test_window"] == token
        assert evidence["count"] == n + 1
        assert evidence["repeated"] == (n + 1 if same else 1)
        assert all((GROUP, old) not in bot.test_window.accepted for old in old_messages)
        if n < count - 1:
            assert evidence["category"] == ""
            assert not await handle(harness, mid, payload)
            assert bot.calls == []
    assert evidence["messages"] == current_messages
    assert await handle(harness, mid, payload)
    assert bot.calls == [("mute", SENDER, 1)] + [
        ("recall", message, harness.sender.nim_account) for message in current_messages
    ]
    assert original_window.accepted.keys() == {
        (GROUP, message) for message in old_messages
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,same", [(5, True), (5, False)], ids=["5-repeats", "5-in-3"]
)
async def test_complete_replacement_bursts_get_independent_mute_receipts(
    three_rule_ingress, monkeypatch, count, same
):
    harness = three_rule_ingress
    bot = harness.receiver
    seed_warning_grace(bot)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    generations = []
    all_recalls = []
    for batch, minutes in enumerate((1, 3)):
        if batch:
            bot.test_window = harness.Window(
                bot, harness.sender, [GROUP], ["group_rules"], [MARKER], 60, 10
            )
            # Scope isolation and same-scope dedup must both survive a reload.
            bot._review_store = type(bot._review_store)(bot._review_store.path)
        messages = [str(100 + batch * 100 + n) for n in range(count)]
        before = len(bot.calls)
        for n, mid in enumerate(messages):
            payload = await ingest(
                harness, mid, MARKER + ("repeat" if same else f"item-{n}")
            )
            assert payload["_ingress_flood"]["count"] == n + 1
            if n < count - 1:
                assert not await handle(harness, mid, payload)
                assert len(bot.calls) == before
        generations.append(payload["_test_rule_window"])
        assert len(set(generations)) == batch + 1
        assert payload["_ingress_flood"]["messages"] == messages
        assert await handle(harness, mid, payload)
        assert bot.calls[before:] == [("mute", SENDER, minutes)] + [
            ("recall", message, harness.sender.nim_account) for message in messages
        ]

        with bot._review_store.connect() as db:
            batches = db.execute(
                "SELECT operation,revision,trigger,mute_status FROM flood_batches "
                "ORDER BY trigger"
            ).fetchall()
        assert len(batches) == batch + 1
        assert len({row[1] for row in batches}) == batch + 1
        assert batches[-1][2:] == (mid, "accepted")
        assert bot.execute_moderation.call_args.args[0] == batches[-1][0]
        all_recalls.extend(messages)

        before = len(bot.calls)
        bot._review_store = type(bot._review_store)(bot._review_store.path)
        extra_mid = str(180 + batch * 100)
        extra = await ingest(
            harness, extra_mid, MARKER + ("repeat" if same else "extra")
        )
        assert await handle(harness, extra_mid, extra)
        assert bot.calls[before:] == [("recall", extra_mid, harness.sender.nim_account)]
        assert bot.execute_moderation.await_count == batch + 1
        with bot._review_store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM flood_batches").fetchone() == (
                batch + 1,
            )
        all_recalls.append(extra_mid)
    assert [call[1] for call in bot.calls if call[0] == "recall"] == all_recalls
    assert bot.nim.request.await_count == 2 * (count + 1)


@pytest.mark.parametrize("status", ["accepted", "verified", "unknown", "rejected"])
def test_flood_scope_preserves_legacy_schema_and_production_burst_dedup(
    tmp_path, status
):
    from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore

    path = tmp_path / "moderation.sqlite3"
    revision = "43506527/1459282/1/1"
    identity = ("43506527", GROUP, SENDER, "900002", revision)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE flood_batches(
                operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,
                sender TEXT,peer TEXT,revision TEXT,trigger TEXT,
                last_seen REAL,mute_status TEXT);
            CREATE TABLE flood_recalls(
                account TEXT,group_id TEXT,message TEXT,batch TEXT,status TEXT,
                PRIMARY KEY(account,group_id,message));
        """)
        db.execute(
            "INSERT INTO flood_batches VALUES(?,?,?,?,?,?,?,?,?)",
            ("production", *identity, "100", 1000.0, "accepted"),
        )
        schema = {
            table: db.execute(f"PRAGMA table_info({table})").fetchall()
            for table in ("flood_batches", "flood_recalls")
        }
    store = ReviewStore(path)
    assert store.reserve_flood(
        "production-next", *identity, "101", 1001.0, ["101"]
    ) == ("production", "accepted", ["101"])
    assert store.reserve_flood(
        "test-a", *identity, "200", 1002.0, ["200"], scope="a" * 32
    ) == ("test-a", None, ["200"])
    store.finish_flood("test-a", status)

    store = ReviewStore(path)
    assert store.reserve_flood(
        "test-a-next", *identity, "201", 1003.0, ["200", "201"], scope="a" * 32
    ) == ("test-a", status, ["201"])
    assert store.reserve_flood(
        "test-b", *identity, "300", 1004.0, ["300"], scope="b" * 32
    ) == ("test-b", None, ["300"])
    assert store.reserve_flood(
        "production-last", *identity, "102", 1005.0, ["102"], scope=""
    ) == ("production", "accepted", ["102"])
    with store.connect() as db:
        assert {
            table: db.execute(f"PRAGMA table_info({table})").fetchall()
            for table in schema
        } == schema
        assert db.execute(
            "SELECT revision FROM flood_batches WHERE operation='production'"
        ).fetchone() == (revision,)
        assert db.execute("SELECT COUNT(*) FROM flood_batches").fetchone() == (3,)


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["message_flood", "external_promotion"])
async def test_readmission_does_not_reauthorize_committed_old_window_payload(
    three_rule_ingress, category
):
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    harness = three_rule_ingress
    bot = harness.receiver
    bot.config["_open_groups"][GROUP]["advertising"] = True
    for n in range(5):
        mid = str(100 + n)
        payload = await ingest(harness, mid, MARKER + "repeat")
    old_token = payload["_test_rule_window"]
    assert payload["_ingress_flood"]["category"] == "message_flood"

    bot.test_window = harness.Window(
        bot, harness.sender, [GROUP], ["group_rules"], [MARKER], 60, 10
    )
    current = await ingest(harness, "990", MARKER + "replacement")
    assert current["_test_rule_window"] != old_token
    assert bot.test_window.admit(GROUP, mid, payload) == "group_rules"
    assert harness.test_rules.rule_target(bot, GROUP, SENDER, mid)
    assert (
        harness.content_rules.flood_category(
            harness.scoped(bot, GROUP), GROUP, mid, payload
        )
        == ""
    )
    try:
        await harness.policy.handle(
            bot,
            GROUP,
            mid,
            payload,
            assessment={
                "decision": "violation",
                "category": category,
                "message_id": mid,
            },
        )
    except ProtocolError:
        pass
    assert bot.calls == []
    bot.execute_moderation.assert_not_awaited()
    bot.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["old_token", "missing_token", "unadmitted"])
async def test_mixed_or_unadmitted_batch_is_rejected_before_upstream_io(
    three_rule_ingress, fault
):
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    harness = three_rule_ingress
    bot = harness.receiver
    prior = await ingest(harness, "90", MARKER + "repeat")
    bot.test_window = harness.Window(
        bot, harness.sender, [GROUP], ["group_rules"], [MARKER], 60, 10
    )
    messages = [str(100 + n) for n in range(5)]
    committed = {}
    for mid in messages:
        committed[mid] = await ingest(harness, mid, MARKER + "repeat")
    trigger = committed[messages[-1]]
    assert trigger["_ingress_flood"]["messages"] == messages

    if fault == "old_token":
        assert prior["_test_rule_window"] != trigger["_test_rule_window"]
        # Readmission makes rule_target alone insufficient to reject this row.
        assert bot.test_window.admit(GROUP, "90", prior) == "group_rules"
        assert harness.test_rules.rule_target(bot, GROUP, SENDER, "90")
        trigger["_ingress_flood"]["messages"].insert(0, "90")
        await bot.ledger.db.execute(
            "UPDATE inbox SET payload=? WHERE account=? AND team=? AND message=?",
            (json.dumps(trigger), bot.account, GROUP, messages[-1]),
        )
    elif fault == "missing_token":
        first = committed[messages[0]]
        first.pop("_test_rule_window")
        assert harness.test_rules.rule_target(bot, GROUP, SENDER, messages[0])
        await bot.ledger.db.execute(
            "UPDATE inbox SET payload=? WHERE account=? AND team=? AND message=?",
            (json.dumps(first), bot.account, GROUP, messages[0]),
        )
    else:
        assert (
            committed[messages[0]]["_test_rule_window"] == trigger["_test_rule_window"]
        )
        del bot.test_window.accepted[(GROUP, messages[0])]
        assert not harness.test_rules.rule_target(bot, GROUP, SENDER, messages[0])
    await bot.ledger.db.commit()

    assert harness.test_rules.rule_target(bot, GROUP, SENDER, messages[-1])
    try:
        await handle(harness, messages[-1], trigger)
    except ProtocolError:
        pass
    assert bot.calls == []
    bot.execute_moderation.assert_not_awaited()
    bot.nim.request.assert_not_awaited()
