"""Offline lottery fixture through authenticated native managed-account ingress."""

import asyncio
import inspect
import json
import socket
import time
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

RECEIVER = "43506527"
ENTRANT = "23691273"
OWNER = "31846829"
GROUP = "1459282"
PEERS = {RECEIVER: "900001", ENTRANT: "900002", OWNER: "900003"}


@pytest_asyncio.fixture
async def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    monkeypatch.setenv("ASTRBOT_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    monkeypatch.delenv("ASTRBOT_WSL_OPEN", raising=False)
    for operation in ("connect", "connect_ex"):
        monkeypatch.setattr(
            socket.socket, operation, Mock(side_effect=AssertionError("No network"))
        )
    monkeypatch.setattr(
        socket, "getaddrinfo", Mock(side_effect=AssertionError("No DNS"))
    )

    import astrbot.core as core
    from astrbot.builtin_stars.wangshangliao_moderation import (
        activities,
        activity_store,
    )
    from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
    from astrbot.core.platform.sources.wangshangliao import runtime_policy, wire
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
    from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger
    from astrbot.core.platform.sources.wangshangliao.test_window import (
        TestWindow as Window,
    )

    platforms = [
        {
            "id": f"offline-lottery-{uid}",
            "type": "wangshangliao",
            "account_id": uid,
            "enable": True,
        }
        for uid in (RECEIVER, ENTRANT, OWNER)
    ]
    monkeypatch.setattr(core, "astrbot_config", {"platform": platforms})
    runtime = runtime_policy.PolicyRuntime()
    runtime.service = SimpleNamespace(active=True)
    runtime.classifier = recognize_command
    runtime.generation = 7
    monkeypatch.setattr(runtime_policy, "runtime", runtime)
    values = {
        "enabled_groups": [GROUP],
        "reply_groups": {GROUP: True},
        "proactive_send": {"enabled": True, "targets": [GROUP]},
        "_open_integrated": True,
        "_open_groups": {GROUP: {"advertising": False, "flood": False, "keywords": []}},
        "_open_managers": {GROUP: [OWNER]},
        "_open_native_roles": {GROUP: "GROUP_ROLE_ADMIN"},
    }
    runtime.publish(
        platforms[0]["id"],
        runtime_policy.PolicySnapshot(
            RECEIVER, "lottery:1", json.dumps(values), time.time()
        ),
    )
    queue = asyncio.Queue()
    receiver = WangshangliaoAdapter(platforms[0], {}, queue)
    receiver.ledger = Ledger(tmp_path / "messages.sqlite3")
    receiver.nim_account = PEERS[RECEIVER]
    receiver.connection_state = "online"
    receiver.groups = {GROUP: "9100001"}
    receiver.members = {GROUP: dict(PEERS)}
    receiver.nim = SimpleNamespace(acknowledge=AsyncMock())
    receiver.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    receiver.diagnostics = SimpleNamespace(emit=Mock(), observe=Mock())
    sender = SimpleNamespace(
        account=ENTRANT,
        nim_account=PEERS[ENTRANT],
        config=platforms[1],
        stopping=asyncio.Event(),
    )

    def window():
        return Window(receiver, sender, [GROUP], ["group_commands"], [], 120, 10)

    original = window()
    receiver.test_window = original
    roster = {
        uid: {
            "userId": uid,
            "nimId": PEERS[uid],
            "userNick": f"Fixture {uid}",
            "accountState": "ACCOUNT_STATE_GOOD",
            "groupRole": role,
        }
        for uid, role in (
            (OWNER, "GROUP_ROLE_OWNER"),
            (RECEIVER, "GROUP_ROLE_ADMIN"),
            (ENTRANT, "GROUP_ROLE_MEMBER"),
        )
    }

    async def directory(_):
        return {"complete": True, "groupMemberInfo": list(roster.values())}

    accepted = []

    async def send(group, key, text, *, authorize=None, **kwargs):
        async with receiver.send_lock:
            if authorize:
                checked = authorize()
                if inspect.isawaitable(checked):
                    await checked
            accepted.append((group, key, text, kwargs))
            return "accepted"

    receiver.get_moderation_members = AsyncMock(side_effect=directory)
    receiver.send_reply_text = AsyncMock(side_effect=send)
    monkeypatch.setattr(activities, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(activity_store, "instance_dir", lambda _: tmp_path)
    service = activities.GroupActivities(
        SimpleNamespace(get_config=lambda _: {"admins_id": [OWNER]})
    )

    def owner(mid="owner-start"):
        return SimpleNamespace(
            platform=receiver,
            is_private_chat=lambda: True,
            is_admin=lambda: True,
            get_sender_id=lambda: OWNER,
            get_sender_name=lambda: "Fixture owner",
            get_group_id=lambda: "",
            unified_msg_origin=f"fixture:FriendMessage:{RECEIVER}/{OWNER}",
            message_obj=SimpleNamespace(message_id=mid),
            get_extra=lambda _: {},
        )

    async def create(*, scoped=True, prize="WSL_TEST_lottery"):
        caller = owner()
        for action, argument in (
            ("抽奖奖励", prize),
            ("中奖人数", "1"),
            ("抽奖倒计时", "1"),
            ("领奖联系人", "offline fixture"),
        ):
            assert "已设置" in await service.command(caller, GROUP, action, argument)
        return await service.command(
            caller, GROUP, "开启抽奖", "", test_window=original if scoped else None
        )

    @asynccontextmanager
    async def native(mid="10001"):
        message = wire.ApplicationMessage(
            sender=wire.Source(id=int(ENTRANT), name="Fixture entrant"),
            target=wire.Source(id=int(GROUP)),
            device=1,
            session=2,
            version=2,
            format=0,
            content=wire.Content(data="参加抽奖"),
        )
        await receiver.ingest(
            {
                0: "1",
                1: receiver.groups[GROUP],
                2: PEERS[ENTRANT],
                7: str(int(time.time() * 1000)),
                8: "100",
                10: wire.seal_message(
                    bytes(32), message, 123, 321, int(time.time() * 1000)
                ),
                12: mid,
            }
        )
        work = asyncio.create_task(receiver.process_group(GROUP, single=True))
        event = None
        try:
            event = await asyncio.wait_for(queue.get(), 2)
            assert event.get_extra("wsl_test_scope") == "group_commands"
            assert event.actor_identity.verified_uid == ENTRANT
            yield event
        finally:
            if event and not event.processing_completion.done():
                event.processing_completion.set_result(None)
            if event is None:
                work.cancel()
            await asyncio.wait_for(asyncio.gather(work, return_exceptions=True), 2)

    def rows(table):
        with closing(activity_store.database(receiver)) as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    async def enroll(event):
        return await service.command(event, GROUP, "参加抽奖", "")

    async def draw():
        return await service.command(
            owner("owner-draw"), GROUP, "立即开奖", "", test_window=original
        )

    def revoke(kind):
        if kind == "replace":
            receiver.test_window = window()
        elif kind == "expire":
            original.expires = 0
        else:
            receiver.test_window = None

    assert all(is_managed_account(uid) for uid in PEERS)
    await receiver.ledger.open()
    try:
        yield SimpleNamespace(
            receiver=receiver,
            sender=sender,
            window=original,
            service=service,
            roster=roster,
            create=create,
            native=native,
            rows=rows,
            enroll=enroll,
            draw=draw,
            revoke=revoke,
            accepted=accepted,
            owner=owner,
            ProtocolError=wire.ProtocolError,
            values=values,
            runtime=runtime,
        )
    finally:
        await receiver.ledger.close()


@pytest.mark.asyncio
async def test_native_member_enroll_and_original_window_owner_draw(lab):
    assert "已开启" in await lab.create()
    async with lab.native() as event:
        assert "参加抽奖成功" in await lab.enroll(event)
        assert "请勿重复" in await lab.enroll(event)
    assert len(lab.rows("lottery_entries")) == 1
    assert "已开奖" in await lab.draw()
    result = lab.rows("lotteries")[0]
    assert result["status"] == "finished" and result["total"] == 1
    assert json.loads(result["results"])[0]["uid"] == ENTRANT
    assert lab.accepted[-1][3]["mentions"][0]["uid"] == int(PEERS[ENTRANT])
    assert lab.receiver.test_window is lab.window
    assert len(lab.accepted) == 2
    assert "没有" in await lab.draw()
    assert len(lab.accepted) == 2
    assert lab.rows("invite_credits") == []


@pytest.mark.asyncio
async def test_normal_lottery_keeps_managed_member_exclusion(lab):
    await lab.create(scoped=False)
    async with lab.native() as event:
        assert "机器人账号不参与" in await lab.enroll(event)
    assert lab.rows("lottery_entries") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["replace", "expire", "close"])
@pytest.mark.parametrize("stage", ["lock", "roster"])
async def test_enrollment_revocation_during_await(lab, kind, stage):
    await lab.create()
    async with lab.native() as event:
        lock = lab.service.lock(lab.receiver, GROUP)
        entered, release = asyncio.Event(), asyncio.Event()
        if stage == "lock":
            await lock.acquire()
        else:
            directory = lab.receiver.get_moderation_members.side_effect

            async def delayed(group):
                entered.set()
                await release.wait()
                return await directory(group)

            lab.receiver.get_moderation_members.side_effect = delayed
        work = asyncio.create_task(lab.enroll(event))
        try:
            if stage == "roster":
                await asyncio.wait_for(entered.wait(), 2)
            else:
                await asyncio.sleep(0)
            lab.revoke(kind)
            if stage == "lock":
                lock.release()
            release.set()
            with pytest.raises(lab.ProtocolError):
                await asyncio.wait_for(work, 2)
        finally:
            release.set()
            if stage == "lock" and lock.locked():
                lock.release()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
    assert lab.rows("lottery_entries") == []
    assert len(lab.accepted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["replace", "expire", "close"])
@pytest.mark.parametrize("stage", ["before", "roster", "send_lock"])
async def test_draw_revocation_preserves_entries_and_never_sends(lab, kind, stage):
    await lab.create()
    async with lab.native() as event:
        assert "参加抽奖成功" in await lab.enroll(event)
    entries = lab.rows("lottery_entries")
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "before":
        lab.revoke(kind)
    elif stage == "roster":
        directory = lab.receiver.get_moderation_members.side_effect

        async def delayed(group):
            entered.set()
            await release.wait()
            return await directory(group)

        lab.receiver.get_moderation_members.side_effect = delayed
    else:
        await lab.receiver.send_lock.acquire()
    work = asyncio.create_task(lab.draw())
    try:
        if stage == "roster":
            await asyncio.wait_for(entered.wait(), 2)
            lab.revoke(kind)
        elif stage == "send_lock":
            for _ in range(200):
                if lab.receiver.send_reply_text.await_count == 2:
                    break
                await asyncio.sleep(0.005)
            assert lab.receiver.send_reply_text.await_count == 2
            lab.revoke(kind)
            lab.receiver.send_lock.release()
        release.set()
        with pytest.raises(lab.ProtocolError):
            await asyncio.wait_for(work, 2)
    finally:
        release.set()
        if lab.receiver.send_lock.locked():
            lab.receiver.send_lock.release()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert lab.rows("lottery_entries") == entries
    row = lab.rows("lotteries")[0]
    assert row["status"] in {"open", "result_unknown"}
    assert len(lab.accepted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["receiver_member", "entrant_admin", "prize", "group", "scope"]
)
async def test_fixture_never_grants_roles_or_broadens_scope(lab, bad):
    if bad == "receiver_member":
        lab.roster[RECEIVER]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif bad == "entrant_admin":
        lab.roster[ENTRANT]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif bad == "group":
        lab.window.groups.append("1516387")
    elif bad == "scope":
        lab.window.scopes = ["group_ai"]
    with pytest.raises(lab.ProtocolError):
        await lab.create(
            prize="ordinary prize" if bad == "prize" else "WSL_TEST_lottery"
        )
    assert lab.rows("lotteries") == []
    assert lab.accepted == []


@pytest.mark.asyncio
async def test_forged_event_is_not_managed_enrollment_authority(lab):
    await lab.create()
    fake = lab.owner("fake-entry")
    fake.is_private_chat = lambda: False
    fake.get_group_id = lambda: GROUP
    fake.get_sender_id = lambda: ENTRANT
    with pytest.raises(lab.ProtocolError, match="test_message_unverified"):
        await lab.enroll(fake)
    assert lab.rows("lottery_entries") == []


@pytest.mark.asyncio
async def test_fixture_binding_loss_cannot_silently_draw_empty_roster(lab):
    await lab.create()
    async with lab.native() as event:
        await lab.enroll(event)
    lab.receiver._lottery_test_fixture = None
    with pytest.raises(lab.ProtocolError):
        await lab.service.command(lab.owner("restart"), GROUP, "立即开奖", "")
    assert lab.rows("lotteries")[0]["status"] == "open"
    assert len(lab.rows("lottery_entries")) == 1
    assert len(lab.accepted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["roster", "send_lock"])
async def test_creation_freezes_original_window_through_await(lab, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "roster":
        directory = lab.receiver.get_moderation_members.side_effect

        async def delayed(group):
            entered.set()
            await release.wait()
            return await directory(group)

        lab.receiver.get_moderation_members.side_effect = delayed
    else:
        await lab.receiver.send_lock.acquire()
    work = asyncio.create_task(lab.create())
    try:
        if stage == "roster":
            await asyncio.wait_for(entered.wait(), 2)
        else:
            for _ in range(200):
                if lab.receiver.send_reply_text.await_count:
                    break
                await asyncio.sleep(0.005)
            assert lab.receiver.send_reply_text.await_count == 1
        lab.revoke("replace")
        release.set()
        if stage == "send_lock":
            lab.receiver.send_lock.release()
        with pytest.raises(lab.ProtocolError):
            await asyncio.wait_for(work, 2)
    finally:
        release.set()
        if lab.receiver.send_lock.locked():
            lab.receiver.send_lock.release()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    assert lab.accepted == []
    rows = lab.rows("lotteries")
    assert not rows if stage == "roster" else rows[0]["status"] == "notice_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["unadmitted", "scope", "peer", "payload", "epoch", "member_role", "member_peer"],
)
async def test_native_message_requires_original_admission_and_member_identity(lab, bad):
    await lab.create()
    async with lab.native() as event:
        if bad == "unadmitted":
            lab.window.accepted.clear()
        elif bad == "scope":
            event.set_extra("wsl_test_scope", "group_ai")
        elif bad == "peer":
            event.actor_identity = SimpleNamespace(
                account=RECEIVER, verified_uid=ENTRANT, peer="999999"
            )
        elif bad == "payload":
            event.get_extra("wangshangliao_payload")["sender"] = OWNER
        elif bad == "epoch":
            event.policy_epoch = -1
        elif bad == "member_role":
            lab.roster[ENTRANT]["groupRole"] = "GROUP_ROLE_ADMIN"
        else:
            lab.roster[ENTRANT]["nimId"] = "999999"
        with pytest.raises(lab.ProtocolError):
            await lab.enroll(event)
    assert lab.rows("lottery_entries") == []


@pytest.mark.asyncio
async def test_new_normal_lottery_does_not_inherit_expired_fixture(lab):
    await lab.create()
    await lab.service.command(lab.owner("cancel"), GROUP, "取消抽奖", "")
    lab.revoke("expire")
    reply = await lab.service.command(lab.owner("normal-start"), GROUP, "开启抽奖", "")
    assert "已开启" in reply
    assert lab.receiver._lottery_test_fixture is None
    # A new admission window is still not a managed participation grant.
    lab.revoke("replace")
    async with lab.native() as event:
        assert "机器人账号不参与" in await lab.enroll(event)
    assert lab.rows("lottery_entries") == []
