"""Dashboard lottery controls use real activities and isolated native ingress."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from tests.test_wangshangliao_lottery_test_fixture import (
    GROUP,
    OWNER,
    PEERS,
    RECEIVER,
)
from tests.test_wangshangliao_lottery_test_fixture import (
    lab as lab,
)

OPERATION = "WSL_TEST_LOTTERY_" + "a" * 32


@pytest_asyncio.fixture
async def control(lab):
    from astrbot.builtin_stars.wangshangliao_moderation.test_lottery import (
        control_lottery,
    )

    owner = SimpleNamespace(
        account=OWNER,
        nim_account=PEERS[OWNER],
        config={"id": "offline-owner"},
        connection_state="online",
        stopping=asyncio.Event(),
    )
    lab.sender.connection_state = "online"
    lab.receiver.group_catalog = {GROUP: {"team": lab.receiver.groups[GROUP]}}
    tenant = {
        "active": True,
        "present": True,
        "paused": False,
        "managers": [OWNER],
        "epoch": 1,
        "version": 1,
    }
    service = SimpleNamespace(
        active=True,
        integrated=True,
        settings={"paused": False},
        adapters=lambda: [lab.receiver, lab.sender, owner],
        plugin=SimpleNamespace(activities=lab.service),
        tenants={(RECEIVER, GROUP): tenant},
        store=SimpleNamespace(tenant=lambda *_: dict(tenant)),
        suspended_groups=set(),
    )
    for action, argument in (
        ("抽奖奖励", OPERATION + "_offline"),
        ("中奖人数", "1"),
        ("参与上限", "2"),
        ("抽奖邀请门槛", "0"),
        ("抽奖倒计时", "1"),
        ("领奖联系人", "offline"),
    ):
        assert "已设置" in await lab.service.command(
            lab.owner(), GROUP, action, argument
        )
    upstream_send = lab.receiver.send_reply_text.side_effect

    async def send(group, key, text, **kwargs):
        status = await upstream_send(group, key, text, **kwargs)
        await lab.receiver.ledger.reserve(key, key)
        await lab.receiver.ledger.finish(key, status, str(100000 + len(lab.accepted)))
        return status

    lab.receiver.send_reply_text.side_effect = send

    async def call(action, *, operation=OPERATION, actor="dashboard/offline"):
        return await control_lottery(
            service, actor, {"operation_id": operation, "action": action}
        )

    lab.control, lab.open_service, lab.tenant = call, service, tenant
    yield lab


@pytest.mark.asyncio
async def test_dashboard_preview_native_enrollment_and_receipts(control):
    settings = control.rows("lottery_settings")
    first = await control.control("start")
    assert first["channel"] == "dashboard-test" and first["status"] == "accepted"
    assert first["fixture_id"] and first["receipt_id"] and first["message_id"]
    assert first["text"] == control.accepted[-1][2]
    assert "抽奖已开启" in first["command_reply"]
    assert (await control.control("start"))["replayed"]
    assert len(control.accepted) == 1
    async with control.native() as event:
        assert "参加抽奖成功" in await control.enroll(event)
    result = await control.control("draw")
    assert result["channel"] == "dashboard-test" and result["status"] == "accepted"
    assert result["fixture_id"] == first["fixture_id"]
    assert control.rows("lotteries")[0]["total"] == 1
    assert (await control.control("draw"))["replayed"]
    assert len(control.accepted) == 2
    assert control.window.outbound_remaining == 8
    assert control.rows("lottery_settings") == settings
    assert control.rows("invite_credits") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "lucky"])
async def test_fixed_fixture_accepts_small_packet_draw_without_payments(
    control, mode, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation.activity_store import database
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    with database(control.receiver) as db:
        db.execute(
            "UPDATE lottery_settings SET packet_mode=?,total_amount=3,winners=2",
            (mode,),
        )
    await control.control("start")
    async with control.native() as event:
        assert "参加抽奖成功" in await control.enroll(event)
    before = control.rows("lotteries")
    with pytest.raises(OpenError, match="test_lottery_not_due"):
        await control.control("draw")
    assert control.rows("lotteries") == before
    assert len(control.accepted) == 1
    assert "draw" not in control.window._lottery_control["results"]
    monkeypatch.setattr("time.time", lambda: before[0]["ends"])
    result = await control.control("draw")
    assert result["status"] == "accepted"
    row = control.rows("lotteries")[0]
    allocations = json.loads(row["results"])
    packets = json.loads(row["packet_amounts"])
    assert len(packets) == 2 and sum(packets) == 3
    assert len(allocations) == 1
    assert allocations[0]["amount"] == packets[0]
    assert row["unclaimed_amount"] == packets[1]
    assert "退回（未支出）" in result["text"]
    assert control.rows("invite_credits") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["draw", "cancel"])
async def test_cannot_choose_or_create_another_fixture(control, action):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    with pytest.raises(OpenError):
        await control.control(action)
    first = await control.control("start")
    with pytest.raises(OpenError):
        await control.control(action, operation="WSL_TEST_LOTTERY_" + "b" * 32)
    assert control.rows("lotteries")[0]["id"] == first["fixture_id"]
    assert len(control.accepted) == 1


@pytest.mark.asyncio
async def test_cancel_is_bound_idempotent_and_prevents_draw(control):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    start = await control.control("start")
    result = await control.control("cancel")
    assert result["fixture_id"] == start["fixture_id"]
    assert result["status"] == "accepted"
    assert control.rows("lotteries")[0]["status"] == "cancelled"
    assert (await control.control("cancel"))["replayed"]
    with pytest.raises(OpenError):
        await control.control("draw")
    assert len(control.accepted) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "actor",
        "operation",
        "action",
        "extra",
        "receiver_member",
        "manager",
        "budget",
        "settings",
    ],
)
async def test_arguments_roles_settings_and_budget_are_not_granted(control, bad):
    from astrbot.builtin_stars.wangshangliao_moderation import test_lottery
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    payload = {"operation_id": OPERATION, "action": "start"}
    actor = "dashboard/offline"
    if bad == "actor":
        actor = "nim/900003"
    elif bad == "operation":
        payload["operation_id"] = "WSL_TEST_LOTTERY_" + "z" * 32
    elif bad == "action":
        payload["action"] = "__getattribute__"
    elif bad == "extra":
        payload["group"] = "1516387"
    elif bad == "receiver_member":
        control.roster[RECEIVER]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif bad == "manager":
        control.tenant["managers"] = []
    elif bad == "budget":
        control.window.outbound_remaining = 1
    else:
        await control.service.command(control.owner(), GROUP, "参与上限", "15")
    with pytest.raises((OpenError, control.ProtocolError)):
        await test_lottery.control_lottery(control.open_service, actor, payload)
    assert control.rows("lotteries") == []
    assert control.accepted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["activity_lock", "roster", "send_lock"])
@pytest.mark.parametrize("fault", ["window", "manager"])
@pytest.mark.parametrize("action", ["start", "draw", "cancel"])
async def test_original_guard_survives_waits(control, stage, fault, action):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    if action != "start":
        await control.control("start")
        if action == "draw":
            async with control.native() as event:
                await control.enroll(event)
    sends = len(control.accepted)
    entries = control.rows("lottery_entries")
    entered, release = asyncio.Event(), asyncio.Event()
    lock = None
    if stage == "activity_lock":
        lock = control.service.lock(control.receiver, GROUP)
        await lock.acquire()
    elif stage == "send_lock":
        lock = control.receiver.send_lock
        await lock.acquire()
    else:
        directory = control.receiver.get_moderation_members.side_effect

        async def delayed(group):
            entered.set()
            await release.wait()
            return await directory(group)

        control.receiver.get_moderation_members.side_effect = delayed
    work = asyncio.create_task(control.control(action))
    try:
        if stage == "roster":
            await asyncio.wait_for(entered.wait(), 2)
        elif stage == "send_lock":
            for _ in range(200):
                if control.receiver.send_reply_text.await_count > sends:
                    break
                await asyncio.sleep(0.005)
            assert control.receiver.send_reply_text.await_count == sends + 1
        else:
            await asyncio.sleep(0)
        if fault == "window":
            control.revoke("replace")
        else:
            control.tenant["managers"] = []
        release.set()
        if lock:
            lock.release()
        with pytest.raises((OpenError, control.ProtocolError)):
            await asyncio.wait_for(work, 2)
    finally:
        release.set()
        if lock and lock.locked():
            lock.release()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert len(control.accepted) == sends
    assert control.rows("lottery_entries") == entries


@pytest.mark.asyncio
async def test_unknown_submission_is_not_retried(control):
    async def unknown(*args, **kwargs):
        await kwargs["authorize"]()
        raise TimeoutError("submission outcome unknown")

    control.receiver.send_reply_text.side_effect = unknown
    with pytest.raises(TimeoutError):
        await control.control("start")
    result = await control.control("start")
    assert result["replayed"] and result["status"] == "unknown"
    assert result["fixture_id"] and result["receipt_id"] and result["text"]
    assert control.receiver.send_reply_text.await_count == 1


@pytest.mark.asyncio
async def test_draw_without_native_enrollment_is_not_passed(control):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    await control.control("start")
    with pytest.raises(OpenError, match="enrollment"):
        await control.control("draw")
    assert control.rows("lotteries")[0]["status"] == "open"
    assert len(control.accepted) == 1
