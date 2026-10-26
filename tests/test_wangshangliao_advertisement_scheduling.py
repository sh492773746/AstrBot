"""Isolated campaign authorization, pacing and preflight receipt regressions."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import rule_replies
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_business_rules import keyword, save_rules
from tests.test_wangshangliao_business_rules import rules as rules
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


def campaign(rules, *, account="99", group="5", planned=None):
    """Create a neutral isolated campaign and make exactly one occurrence due."""
    save_rules(
        rules,
        {
            "keywords": [keyword()],
            "ads": [
                {
                    "id": "a",
                    "name": "Neutral schedule",
                    "text": "Neutral scheduled notice",
                    "times": ["09:00"],
                    "enabled": True,
                }
            ],
        },
        account=account,
        group=group,
    )
    planned = time.time() - 0.01 if planned is None else planned
    rules.started = planned - 1
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_ad_cursors SET next_at=? WHERE account=? AND group_id=?",
            (planned, account, group),
        )
    return {
        "account": account,
        "group_id": group,
        "rule_id": "a",
        "next_at": planned,
    }


def add_group(rules, group):
    """Extend the complete offline catalog without marking prior groups absent."""
    catalog = {
        row["group_id"]: {"name": row["name"], "team": row["team"]}
        for row in rules.store.tenants()
        if row["account"] == "99" and row["present"]
    }
    catalog[group] = {"name": "Offline " + group, "team": "100" + group}
    rules.store.discover("99", catalog)
    rules.store.import_group(
        "99", group, {"advertising": True, "flood": True, "keywords": []}, ["1"], "test"
    )
    current = rules.store.tenant("99", group)
    rules.store.native_role("99", group, current["epoch"], "GROUP_ROLE_ADMIN")


@pytest.mark.parametrize("control", [False, True])
@pytest.mark.parametrize("change", ["keywords", "flood", "advertising", "noop"])
def test_moderation_edit_keeps_confirmed_business_consent(rules, control, change):
    campaign(rules)
    add_group(rules, "6")
    campaign(rules, group="6")
    other = rules.read("99", "6")
    before = rules.read("99", "5")
    current = rules.store.tenant("99", "5")
    settings = dict(current["rules"])
    if change == "keywords":
        settings["keywords"] = ["neutral blocked phrase"]
    elif change != "noop":
        settings[change] = not settings[change]
    with rules.store.connect() as db:
        cursors = list(db.execute("SELECT * FROM business_ad_cursors"))
    rules.store.change(
        "99",
        "5",
        current["version"],
        "1",
        rules=settings,
        control=control,
        guard=lambda: None,
    )
    after = rules.read("99", "5")
    assert after == {**before, "tenant_version": current["version"] + 1}
    assert rules.read("99", "6") == other
    with rules.store.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM business_ad_cursors")] == [
            tuple(r) for r in cursors
        ]
        detail = json.loads(
            db.execute(
                "SELECT detail FROM audit WHERE action='tenant_changed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert detail["business_rules_rebound"] is True


@pytest.mark.parametrize(
    "barrier",
    [
        "paused",
        "pause",
        "stale",
        "epoch",
        "absent",
        "inactive",
        "admission",
        "role",
        "cleanup",
    ],
)
def test_moderation_edit_never_revives_revoked_campaigns(rules, barrier):
    campaign(rules)
    if barrier in {"stale", "epoch"}:
        with rules.store.connect(write=True) as db:
            column = "tenant_version" if barrier == "stale" else "epoch"
            db.execute(f"UPDATE business_rules SET {column}={column}-1")
    elif barrier != "pause":
        sql = {
            "paused": "paused=1",
            "absent": "present=0",
            "inactive": "active=0",
            "admission": "admission='rejected'",
            "role": "native_role='GROUP_ROLE_MEMBER'",
            "cleanup": "cleanup_epoch=2,cleanup_done=1",
        }[barrier]
        with rules.store.connect(write=True) as db:
            db.execute(f"UPDATE tenants SET {sql}")
    before = rules.read("99", "5")
    current = rules.store.tenant("99", "5")
    if barrier in {"absent", "inactive", "admission", "role", "cleanup"}:
        with pytest.raises(OpenError, match="permission_denied"):
            rules.store.change(
                "99",
                "5",
                current["version"],
                "1",
                rules=current["rules"],
                guard=lambda: None,
            )
    else:
        rules.store.change(
            "99",
            "5",
            current["version"],
            "1",
            rules=current["rules"],
            paused=True if barrier == "pause" else None,
            control=barrier == "paused",
            guard=lambda: None,
        )
    assert rules.read("99", "5") == before


@pytest.mark.parametrize("entry", ["dashboard", "native"])
@pytest.mark.parametrize("owner", ["", "1", "3"])
@pytest.mark.parametrize("change", ["add", "remove", "remove_owner", "stale", "epoch"])
def test_manager_changes_preserve_only_current_retained_creator_consent(
    rules, entry, owner, change
):
    campaign(rules)
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE tenants SET managers=?", (json.dumps(["1", "2"]),))
        db.execute(
            "UPDATE business_rules SET owner=?,peer=?",
            (owner, "100" + owner if owner else ""),
        )
        if change in {"stale", "epoch"}:
            column = "tenant_version" if change == "stale" else "epoch"
            db.execute(f"UPDATE business_rules SET {column}={column}-1")
    before = rules.read("99", "5")
    current = rules.store.tenant("99", "5")
    with rules.store.connect() as db:
        cursors = [
            tuple(row) for row in db.execute("SELECT * FROM business_ad_cursors")
        ]
        rotations = [
            tuple(row) for row in db.execute("SELECT * FROM business_ad_rotations")
        ]
    target = "1" if change == "remove_owner" else "2" if change == "remove" else "3"
    operation = "remove" if change in {"remove", "remove_owner"} else "add"
    actor = "2" if change == "remove_owner" else "1"
    if entry == "dashboard":
        managers = list(current["managers"])
        if operation == "add":
            managers.append(target)
        else:
            managers.remove(target)
        token = rules.store.preview_control(
            "99",
            "5",
            current["version"],
            "dashboard/offline",
            current["rules"],
            managers,
            False,
            guard=lambda: None,
        )
        rules.store.change(
            "99",
            "5",
            current["version"],
            "dashboard/offline",
            managers=managers,
            paused=False,
            control=True,
            confirmation=token,
            guard=lambda: None,
        )
    else:
        token = rules.store.preview_manager(
            "99",
            "5",
            current["version"],
            actor,
            "session",
            "preview",
            target,
            operation,
            peer="100" + target,
            guard=lambda: None,
        )
        rules.store.confirm_manager(
            token,
            actor,
            "session",
            "confirm",
            "99",
            "5",
            {"1": "1001", "2": "1002", "3": "1003"},
            eligible_managers={"1", "2", "3"},
            guard=lambda: None,
        )
    preserved = (
        change not in {"stale", "epoch"}
        and owner != "3"
        and not (owner and change == "remove_owner")
    )
    assert rules.read("99", "5") == {
        **before,
        "tenant_version": current["version"] + 1
        if preserved
        else before["tenant_version"],
    }
    with rules.store.connect() as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM business_ad_cursors")
        ] == cursors
        assert [
            tuple(row) for row in db.execute("SELECT * FROM business_ad_rotations")
        ] == rotations
        detail = json.loads(
            db.execute("SELECT detail FROM audit ORDER BY id DESC LIMIT 1").fetchone()[
                0
            ]
        )
        assert detail["business_rules_rebound"] is preserved


def test_rebinding_rolls_back_with_the_owning_tenant_transaction(rules):
    campaign(rules)
    before = rules.read("99", "5")
    current = rules.store.tenant("99", "5")
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OpenError("revoked")

    with pytest.raises(OpenError, match="revoked"):
        rules.store.change(
            "99",
            "5",
            current["version"],
            "1",
            rules={**current["rules"], "flood": False},
            guard=guard,
        )
    assert rules.store.tenant("99", "5") == current
    assert rules.read("99", "5") == before


@pytest.mark.asyncio
async def test_inflight_authority_is_fenced_but_future_campaign_stays_enabled(
    rpc_scope,
):
    service, adapter, _ = rpc_scope
    campaign(service.rule_replies)
    frozen = service.rule_replies.read("99", "5")
    await service.roster(adapter, "5")
    service.rule_replies.send_guard(adapter, frozen)
    current = service.store.tenant("99", "5")
    service.store.change(
        "99",
        "5",
        current["version"],
        "1",
        rules={**current["rules"], "keywords": ["neutral"]},
        guard=lambda: None,
    )
    with pytest.raises(OpenError, match="business_rules_inactive"):
        service.rule_replies.send_guard(adapter, frozen)
    fresh = service.rule_replies.read("99", "5")
    service.rule_replies.send_guard(adapter, fresh)
    assert fresh["config"] == frozen["config"]


@pytest.mark.asyncio
async def test_three_due_groups_are_serialized_and_spaced_without_dropping(
    rules, monkeypatch
):
    monkeypatch.setattr(rule_replies, "ACCOUNT_AD_GAP_SECONDS", 0.025)
    monkeypatch.setattr(rules, "send_guard", lambda *args, **kw: None)
    cursors = []
    for group in ("5", "6", "7"):
        if group != "5":
            add_group(rules, group)
        cursors.append(campaign(rules, group=group, planned=time.time() - 20))
    rules.started = min(c["next_at"] for c in cursors) - 1
    starts, finishes = [], []

    async def send(group, *args, **kw):
        starts.append((group, time.monotonic()))
        await asyncio.sleep(0.01)
        finishes.append((group, time.monotonic()))
        return "accepted"

    adapter = SimpleNamespace(send_reply_text=AsyncMock(side_effect=send))
    await asyncio.gather(*(rules.send_ad(adapter, c) for c in cursors))
    assert [group for group, _ in starts] == ["5", "6", "7"]
    assert all(starts[i][1] - finishes[i - 1][1] >= 0.02 for i in (1, 2))
    with rules.store.connect() as db:
        assert [r[0] for r in db.execute("SELECT state FROM business_rule_sends")] == [
            "accepted",
            "accepted",
            "accepted",
        ]


@pytest.mark.asyncio
async def test_account_spacing_rechecks_deadline_after_early_timer_wakeup(
    rules, monkeypatch
):
    clock = [0.0]
    starts = []
    waits = []

    async def sleep(delay):
        waits.append(delay)
        clock[0] += min(delay, 0.5)

    async def send(*args):
        starts.append(clock[0])
        return "accepted"

    monkeypatch.setattr(rule_replies, "ACCOUNT_AD_GAP_SECONDS", 3)
    monkeypatch.setattr(
        rule_replies, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(
        rule_replies, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=sleep)
    )
    monkeypatch.setattr(rules, "_send_ad", send)
    await rules.send_ad(None, {"account": "99"})
    await rules.send_ad(None, {"account": "99"})
    assert starts == [0, rule_replies.ACCOUNT_AD_GAP_SECONDS]
    assert len(waits) > 1


@pytest.mark.asyncio
async def test_different_receiving_accounts_do_not_block_each_other(rules, monkeypatch):
    monkeypatch.setattr(rule_replies, "ACCOUNT_AD_GAP_SECONDS", 1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(adapter, cursor):
        if cursor["account"] == "99":
            entered.set()
            await release.wait()
        return cursor["account"]

    monkeypatch.setattr(rules, "_send_ad", send)
    first = asyncio.create_task(rules.send_ad(None, {"account": "99"}))
    try:
        await entered.wait()
        assert (
            await asyncio.wait_for(rules.send_ad(None, {"account": "199"}), 0.1)
            == "199"
        )
    finally:
        release.set()
        await first


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["guard", "roster", "offline", "expired", "restart"])
async def test_unsubmitted_slots_have_durable_reasons_and_are_not_replayed(
    rules, monkeypatch, case
):
    cursor = campaign(rules)
    monkeypatch.setattr(rules, "send_guard", lambda *args, **kw: None)
    adapter = SimpleNamespace(send_reply_text=AsyncMock(return_value="accepted"))
    if case == "guard":
        monkeypatch.setattr(
            rules,
            "send_guard",
            lambda *args, **kw: (_ for _ in ()).throw(
                OpenError("business_rules_inactive")
            ),
        )
    elif case == "roster":
        rules.service.roster.side_effect = OpenError("membership_unproven")
    elif case == "offline":
        adapter = None
    elif case == "restart":
        rules.started = cursor["next_at"] + 1
    else:
        cursor["next_at"] = time.time() - rule_replies.ADMISSION_WINDOW_SECONDS - 1
        rules.started = cursor["next_at"] - 1
        with rules.store.connect(write=True) as db:
            db.execute("UPDATE business_ad_cursors SET next_at=?", (cursor["next_at"],))
    await rules.send_ad(adapter, cursor)
    await rules.send_ad(adapter, cursor)
    with rules.store.connect() as db:
        receipts = db.execute("SELECT state,error FROM business_rule_sends").fetchall()
        assert len(receipts) == 1 and receipts[0]["state"] == "skipped"
        assert (
            receipts[0]["error"]
            == {
                "guard": "business_rules_inactive",
                "roster": "membership_unproven",
                "offline": "advertisement_adapter_unavailable",
                "expired": "advertisement_slot_expired",
                "restart": "advertisement_before_runtime",
            }[case]
        )
        assert db.execute("SELECT blocked FROM business_ad_cursors").fetchone()[0] == ""
    if adapter:
        adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_is_rechecked_after_waiting_for_another_group(
    rules, monkeypatch
):
    cursors = [campaign(rules)]
    add_group(rules, "6")
    cursors.append(campaign(rules, group="6"))
    rules.started = min(c["next_at"] for c in cursors) - 1
    allowed = True

    def guard(*args, **kw):
        if not allowed:
            raise OpenError("business_rules_inactive")

    async def send(*args, **kw):
        nonlocal allowed
        allowed = False
        return "accepted"

    monkeypatch.setattr(rules, "send_guard", guard)
    adapter = SimpleNamespace(send_reply_text=AsyncMock(side_effect=send))
    await asyncio.gather(*(rules.send_ad(adapter, c) for c in cursors))
    adapter.send_reply_text.assert_awaited_once()
    with rules.store.connect() as db:
        assert [
            tuple(r)
            for r in db.execute(
                "SELECT group_id,state FROM business_rule_sends ORDER BY group_id"
            )
        ] == [("5", "accepted"), ("6", "skipped")]


@pytest.mark.asyncio
async def test_schedule_read_exposes_last_skipped_reason_without_sending(rpc_scope):
    service, adapter, _ = rpc_scope
    cursor = campaign(service.rule_replies)
    await service.rule_replies.send_ad(None, cursor)
    current = service.store.tenant("99", "5")
    value = await OpenRPC(service).dispatch(
        OpenCall(
            action="business_rules",
            actor="dashboard/offline",
            payload={
                "account": "99",
                "group_id": "5",
                "tenant_version": current["version"],
                "operation": "read",
            },
        )
    )
    assert value["schedules"][0]["last_state"] == "skipped"
    assert value["schedules"][0]["last_error"] == "advertisement_adapter_unavailable"
    assert value["schedules"][0]["last_at"] > cursor["next_at"]


@pytest.mark.asyncio
async def test_tick_eventually_drains_three_groups_with_two_task_slots(
    rules, monkeypatch
):
    monkeypatch.setattr(rule_replies, "ACCOUNT_AD_GAP_SECONDS", 0.005)
    monkeypatch.setattr(rules, "send_guard", lambda *args, **kw: None)
    rules.service.active = True
    adapter = SimpleNamespace(
        account="99", send_reply_text=AsyncMock(return_value="accepted")
    )
    rules.service.adapters = lambda: [adapter]
    cursors = []
    for group in ("5", "6", "7"):
        if group != "5":
            add_group(rules, group)
        cursors.append(campaign(rules, group=group))
    rules.started = min(c["next_at"] for c in cursors) - 1
    try:
        for _ in range(100):
            rules.tick()
            assert len(rules.tasks) <= 2
            if adapter.send_reply_text.await_count == 3:
                break
            await asyncio.sleep(0.005)
        assert adapter.send_reply_text.await_count == 3
    finally:
        await asyncio.gather(*tuple(rules.tasks.values()))
        await rules.close()


@pytest.mark.asyncio
async def test_caption_is_spaced_after_accepted_image_and_still_authorized(
    rules, monkeypatch
):
    monkeypatch.setattr(rule_replies, "IMAGE_TEXT_GAP_SECONDS", 0.025)
    cursor = campaign(rules)
    row = rules.read("99", "5")
    image_id = "a" * 32
    row["config"]["ads"][0]["images"] = [image_id]
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rules SET config=?",
            (json.dumps(row["config"]),),
        )
        db.execute(
            "UPDATE business_ad_rotations SET images=?", (json.dumps([image_id]),)
        )
    monkeypatch.setattr(rules, "send_guard", lambda *args, **kw: None)
    image_at = []
    caption_at = []

    async def image(*args, **kw):
        image_at.append(time.monotonic())
        return "accepted"

    async def caption(*args, **kw):
        kw["authorize"]()
        caption_at.append(time.monotonic())
        return "accepted"

    rules.media.send = AsyncMock(side_effect=image)
    adapter = SimpleNamespace(send_reply_text=AsyncMock(side_effect=caption))
    await rules.send_ad(adapter, cursor)
    assert len(image_at) == len(caption_at) == 1
    assert caption_at[0] - image_at[0] >= 0.02


@pytest.mark.asyncio
async def test_cancel_after_image_before_caption_never_reports_complete_delivery(
    rules, monkeypatch
):
    monkeypatch.setattr(rule_replies, "IMAGE_TEXT_GAP_SECONDS", 60)
    cursor = campaign(rules)
    row = rules.read("99", "5")
    row["config"]["ads"][0]["images"] = ["a" * 32]
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_rules SET config=?", (json.dumps(row["config"]),))
        db.execute(
            "UPDATE business_ad_rotations SET images=?", (json.dumps(["a" * 32]),)
        )
    monkeypatch.setattr(rules, "send_guard", lambda *args, **kw: None)
    image_accepted = asyncio.Event()

    async def image(*args, **kw):
        image_accepted.set()
        return "accepted"

    rules.media.send = AsyncMock(side_effect=image)
    adapter = SimpleNamespace(send_reply_text=AsyncMock(return_value="accepted"))
    task = asyncio.create_task(rules.send_ad(adapter, cursor))
    await image_accepted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    adapter.send_reply_text.assert_not_awaited()
    with rules.store.connect() as db:
        assert (
            db.execute("SELECT state FROM business_rule_sends").fetchone()[0]
            == "unknown"
        )
        assert (
            db.execute("SELECT blocked FROM business_ad_cursors").fetchone()[0]
            == "delivery_unknown"
        )
