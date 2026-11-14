"""Offline advertisement replacement with durable native routes and fake IO."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import (
    RuleConfiguration,
    RuleReplies,
)
from astrbot.core.platform.sources.wangshangliao import (
    advertisement_recall,
    reply_recall,
    wire,
)
from tests.test_wangshangliao_advertisement_images import configuration, upload
from tests.test_wangshangliao_business_rules import rules as rules
from tests.test_wangshangliao_business_rules import save_rules
from tests.test_wangshangliao_group_ai import tenant
from tests.test_wangshangliao_reply_recall import setup as setup


def test_replacement_is_opt_in_and_strictly_boolean():
    assert RuleConfiguration().replace_previous_ad is False
    for invalid in ("true", 1, None, {}):
        with pytest.raises(ValidationError):
            RuleConfiguration(replace_previous_ad=invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_three_plans_replace_previous_group_batch_not_their_own_plan(
    rules, monkeypatch, enabled
):
    config = configuration([], text="Neutral A")
    config["ads"] = [
        {**config["ads"][0], "id": str(index), "name": str(index)} for index in range(3)
    ]
    config["replace_previous_ad"] = enabled
    save_rules(rules, config)
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    recall = AsyncMock(return_value="accepted")
    monkeypatch.setattr(advertisement_recall, "recall_advertisement", recall)
    adapter = SimpleNamespace(send_reply_text=AsyncMock(return_value="accepted"))
    rules.started = time.time() - 60
    operations = []
    for index in range(3):
        planned = time.time() - 0.1
        with rules.store.connect(write=True) as db:
            db.execute(
                "UPDATE business_ad_cursors SET next_at=? WHERE rule_id=?",
                (planned, str(index)),
            )
        cursor = {
            "account": "99",
            "group_id": "5",
            "rule_id": str(index),
            "next_at": planned,
        }
        await rules._send_ad(adapter, cursor)
        await rules._send_ad(adapter, cursor)
        operations.append(adapter.send_reply_text.await_args.args[1])
    assert adapter.send_reply_text.await_count == 3
    assert recall.await_count == (2 if enabled else 0)
    if enabled:
        assert [call.args[2] for call in recall.await_args_list] == operations[:2]
        assert all(
            call.kwargs["retain_recall"]
            for call in adapter.send_reply_text.await_args_list
        )
        with rules.store.connect() as db:
            head = db.execute("SELECT * FROM business_ad_heads").fetchone()
            assert head["operation"] == operations[2]
            assert head["previous"] == operations[1]
            assert head["state"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["rejected", "unknown", "exception"])
async def test_failed_new_ad_keeps_the_old_group_head(rules, monkeypatch, result):
    config = {**configuration([], text="Neutral notice"), "replace_previous_ad": True}
    save_rules(rules, config)
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    recall = AsyncMock(return_value="accepted")
    monkeypatch.setattr(advertisement_recall, "recall_advertisement", recall)
    adapter = SimpleNamespace(send_reply_text=AsyncMock(return_value="accepted"))
    rules.started = time.time() - 60
    planned = time.time() - 5
    for index in range(2):
        with rules.store.connect(write=True) as db:
            db.execute("UPDATE business_ad_cursors SET next_at=?", (planned + index,))
        await rules._send_ad(
            adapter,
            {
                "account": "99",
                "group_id": "5",
                "rule_id": "a",
                "next_at": planned + index,
            },
        )
        if index == 0:
            adapter.send_reply_text.return_value = result
            if result == "exception":
                adapter.send_reply_text.side_effect = TimeoutError()
    recall.assert_not_awaited()
    with rules.store.connect() as db:
        assert db.execute("SELECT operation FROM business_ad_heads").fetchone()[0] == (
            f"advertisement/99/5/a/{int(planned)}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("text_result", ["rejected", "unknown"])
async def test_new_image_alone_never_replaces_old_image_and_text(
    rules, monkeypatch, text_result
):
    image = upload(rules)
    save_rules(
        rules,
        {
            **configuration([image], text="Neutral notice"),
            "replace_previous_ad": True,
        },
    )
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.rule_replies.IMAGE_TEXT_GAP_SECONDS",
        0,
    )
    rules.media.send = AsyncMock(return_value="accepted")
    adapter = SimpleNamespace(send_reply_text=AsyncMock(return_value=text_result))
    recall = AsyncMock()
    monkeypatch.setattr(advertisement_recall, "recall_advertisement", recall)
    rules.started = time.time() - 60
    planned = time.time() - 0.1
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_ad_cursors SET next_at=?", (planned,))
    await rules._send_ad(
        adapter,
        {"account": "99", "group_id": "5", "rule_id": "a", "next_at": planned},
    )
    recall.assert_not_awaited()
    assert rules.media.send.await_args.kwargs["retain_recall"] is True
    with rules.store.connect() as db:
        assert not db.execute("SELECT * FROM business_ad_heads").fetchall()


@pytest.mark.asyncio
async def test_retained_text_waits_for_new_ad_not_twelve_second_timer(setup):
    adapter, now = setup
    operation = "advertisement/1/5/a/1000"
    await adapter.send_reply_text("5", operation, "Neutral notice", retain_recall=True)
    adapter.ledger.recall_wakeup.clear()
    now[0] += 3600
    await reply_recall.recall_due(adapter)
    assert all(call.args[:2] == (8, 2) for call in adapter.nim.request.await_args_list)
    assert not adapter.ledger.recall_wakeup.is_set()
    assert (
        await advertisement_recall.recall_advertisement(
            adapter, "5", operation, authorize=lambda: None
        )
        == "accepted"
    )
    request = adapter.nim.request.await_args
    assert request.args[:2] == (7, 13)
    fields, _ = wire.read_properties(request.args[2])
    assert fields[0] == b"1000000"
    assert fields[2] == b"905" and fields[3] == b"901"
    count = adapter.nim.request.await_count
    assert (
        await advertisement_recall.recall_advertisement(
            adapter, "5", operation, authorize=lambda: None
        )
        == "accepted"
    )
    assert adapter.nim.request.await_count == count


@pytest.mark.asyncio
async def test_actual_image_and_all_text_parts_are_recalled(setup, rules, monkeypatch):
    adapter, _ = setup
    image = upload(rules, account="1")
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET state='ready',asset='1234567' WHERE id=?",
            (image,),
        )
    operation = "advertisement/1/5/a/1000"
    await rules.media.send(
        adapter,
        "5",
        image,
        operation + "/image",
        lambda: None,
        proactive=False,
        retain_recall=True,
    )
    await adapter.send_reply_text("5", operation, "中" * 2000, retain_recall=True)
    async with adapter.ledger.db.execute(
        "SELECT count(*) FROM reply_recalls WHERE state='retained'"
    ) as cursor:
        count = (await cursor.fetchone())[0]
    assert count >= 3
    monkeypatch.setattr(advertisement_recall.asyncio, "sleep", AsyncMock())
    assert (
        await advertisement_recall.recall_advertisement(
            adapter, "5", operation, authorize=lambda: None
        )
        == "accepted"
    )
    assert (
        sum(call.args[:2] == (7, 13) for call in adapter.nim.request.await_args_list)
        == count
    )


@pytest.mark.asyncio
async def test_real_native_batch_is_replaced_only_after_both_new_messages(
    setup, rules, monkeypatch, wsl_active_policy
):
    adapter, clock = setup
    tenant(rules.store, account="1")
    wsl_active_policy(
        {**adapter.config, "proactive_send": {"enabled": True, "targets": ["5"]}}
    )
    image = upload(rules, account="1")
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET state='ready',asset='1234567' WHERE id=?",
            (image,),
        )
    config = configuration([image], text="Neutral notice")
    config["ads"] = [
        {**config["ads"][0], "id": index, "name": index} for index in ("A", "B")
    ]
    config["replace_previous_ad"] = True
    save_rules(rules, config, account="1")
    rules.started = clock[0] - 60
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    monkeypatch.setattr(advertisement_recall.asyncio, "sleep", AsyncMock())
    for index, rule_id in enumerate(("A", "B")):
        clock[0] += index
        planned = clock[0] - 0.1
        with rules.store.connect(write=True) as db:
            db.execute(
                "UPDATE business_ad_cursors SET next_at=? WHERE account='1' AND rule_id=?",
                (planned, rule_id),
            )
        await rules._send_ad(
            adapter,
            {
                "account": "1",
                "group_id": "5",
                "rule_id": rule_id,
                "next_at": planned,
            },
        )
    assert [call.args[:2] for call in adapter.nim.request.await_args_list] == [
        (8, 2),
        (8, 2),
        (8, 2),
        (8, 2),
        (7, 13),
        (7, 13),
    ]
    with rules.store.connect() as db:
        head = db.execute(
            "SELECT * FROM business_ad_heads WHERE account='1' AND group_id='5'"
        ).fetchone()
        assert head["operation"].split("/")[3] == "B"
        assert head["previous"].split("/")[3] == "A"
        assert head["state"] == "accepted"


@pytest.mark.asyncio
async def test_retained_routes_survive_restart_but_submitted_recalls_do_not_retry(
    setup,
):
    adapter, _ = setup
    operation = "advertisement/1/5/a/1000"
    await adapter.send_reply_text("5", operation, "Neutral", retain_recall=True)
    await adapter.ledger.close()
    await adapter.ledger.open()
    async with adapter.ledger.db.execute("SELECT state FROM reply_recalls") as cursor:
        assert await cursor.fetchone() == ("retained",)
    await adapter.ledger.db.execute("UPDATE reply_recalls SET state='sending'")
    await adapter.ledger.db.commit()
    await adapter.ledger.close()
    await adapter.ledger.open()
    adapter.nim.request.reset_mock()
    assert (
        await advertisement_recall.recall_advertisement(
            adapter, "5", operation, authorize=lambda: None
        )
        == "unknown"
    )
    adapter.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["other_group", "other_account", "member_message", "missing", "tampered"]
)
async def test_recall_requires_own_exact_account_group_operation_and_route(
    setup, change
):
    adapter, _ = setup
    operation = "advertisement/1/5/a/1000"
    await adapter.send_reply_text("5", operation, "Neutral", retain_recall=True)
    if change == "other_group":
        operation = "advertisement/1/6/a/1000"
    elif change == "other_account":
        operation = "advertisement/2/5/a/1000"
    elif change == "member_message":
        operation = "recall/1/5/1000"
    elif change == "missing":
        await adapter.ledger.db.execute("DELETE FROM reply_recalls")
    else:
        await adapter.ledger.db.execute("UPDATE reply_recalls SET peer='902'")
    await adapter.ledger.db.commit()
    adapter.nim.request.reset_mock()
    with pytest.raises(wire.ProtocolError):
        await advertisement_recall.recall_advertisement(
            adapter, "5", operation, authorize=lambda: None
        )
    adapter.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["rejected", "unknown"])
async def test_uncertain_or_refused_recall_is_not_retried(setup, result):
    adapter, _ = setup
    operation = "advertisement/1/5/a/1000"
    await adapter.send_reply_text("5", operation, "Neutral", retain_recall=True)
    if result == "unknown":
        adapter.nim.request.side_effect = TimeoutError()
        assert (
            await advertisement_recall.recall_advertisement(
                adapter, "5", operation, authorize=lambda: None
            )
            == "unknown"
        )
    else:
        adapter.nim.request.side_effect = None
        adapter.nim.request.return_value = (403, b"")
        assert (
            await advertisement_recall.recall_advertisement(
                adapter, "5", operation, authorize=lambda: None
            )
            == "rejected"
        )
    adapter.nim.request.reset_mock()
    assert (
        await advertisement_recall.recall_advertisement(
            adapter, "5", operation, authorize=lambda: None
        )
        == result
    )
    adapter.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["authority", "nim", "group", "member"])
async def test_waiting_recall_rechecks_authority_and_transport(
    setup, monkeypatch, change
):
    adapter, _ = setup
    operation = "advertisement/1/5/a/1000"
    await adapter.send_reply_text("5", operation, "Neutral", retain_recall=True)
    adapter.nim.request.reset_mock()
    original_nim = adapter.nim
    entered = asyncio.Event()
    acquire = adapter.send_lock.acquire
    await acquire()

    async def waiting():
        entered.set()
        return await acquire()

    monkeypatch.setattr(adapter.send_lock, "acquire", waiting)
    revoked = False

    def guard():
        if revoked:
            raise wire.ProtocolError("revoked")

    work = asyncio.create_task(
        advertisement_recall.recall_advertisement(
            adapter, "5", operation, authorize=guard
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    if change == "authority":
        revoked = True
    elif change == "nim":
        adapter.nim = object()
    elif change == "group":
        adapter.groups["5"] = "906"
    else:
        adapter.members["5"]["1"] = "903"
    adapter.send_lock.release()
    with pytest.raises(wire.ProtocolError):
        await asyncio.wait_for(work, 1)
    original_nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_change_during_intent_commit_prevents_submission(
    setup, monkeypatch
):
    adapter, _ = setup
    operation = "advertisement/1/5/a/1000"
    await adapter.send_reply_text("5", operation, "Neutral", retain_recall=True)
    commit = adapter.ledger.db.commit

    async def changed():
        await commit()
        adapter.groups["5"] = "906"

    monkeypatch.setattr(adapter.ledger.db, "commit", changed)
    adapter.nim.request.reset_mock()
    with pytest.raises(wire.ProtocolError):
        await advertisement_recall.recall_advertisement(
            adapter, "5", operation, authorize=lambda: None
        )
    adapter.nim.request.assert_not_awaited()
    async with adapter.ledger.db.execute("SELECT state FROM reply_recalls") as cursor:
        assert await cursor.fetchone() == ("rejected",)


def test_interrupted_head_is_quarantined_and_disable_clears_only_this_scope(rules):
    save_rules(rules, {"replace_previous_ad": True})
    with rules.store.connect(write=True) as db:
        db.executemany(
            "INSERT INTO business_ad_heads VALUES(?,?,?,'old','pending','',0)",
            [("99", "5", "new"), ("99", "6", "elsewhere")],
        )
    recovered = RuleReplies(rules.service)
    with rules.store.connect() as db:
        assert {
            row[0] for row in db.execute("SELECT state FROM business_ad_heads")
        } == {"unknown"}
    save_rules(recovered, {"replace_previous_ad": False})
    with rules.store.connect() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT account,group_id,operation FROM business_ad_heads"
            )
        ] == [("99", "6", "elsewhere")]


@pytest.mark.asyncio
async def test_unverifiable_previous_head_is_reported_without_blocking_future_batches(
    rules, monkeypatch
):
    save_rules(rules, {"replace_previous_ad": True})
    with rules.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO business_ad_heads VALUES('99','5',"
            "'advertisement/99/5/old/1000','','not_needed','',0)"
        )
        for index in (1, 2):
            db.execute(
                "INSERT INTO business_rule_sends VALUES(?,'99','5','advertisement',"
                "'a',?,'accepted',?,'')",
                (f"advertisement/99/5/a/{1000 + index}", 1000 + index, 1000 + index),
            )
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    recall = AsyncMock(return_value="accepted")
    monkeypatch.setattr(advertisement_recall, "recall_advertisement", recall)
    row = rules.read("99", "5")
    await rules.replace_ad(object(), row, "advertisement/99/5/a/1001")
    recall.assert_not_awaited()
    with rules.store.connect() as db:
        head = db.execute("SELECT * FROM business_ad_heads").fetchone()
        assert head["operation"] == "advertisement/99/5/a/1001"
        assert head["state"] == "rejected"
        assert head["error"] == "advertisement_replacement_previous_invalid"
    await rules.replace_ad(object(), row, "advertisement/99/5/a/1002")
    assert recall.await_args.args[2] == "advertisement/99/5/a/1001"
