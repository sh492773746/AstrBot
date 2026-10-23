"""Fresh advertising progression uses native ingress without resetting history."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests.test_wangshangliao_three_rule_ingress import (
    GROUP,
    RECEIVER,
    SENDER,
    THIRD,
    ingest,
)
from tests.test_wangshangliao_three_rule_ingress import (
    three_rule_ingress as three_rule_ingress,
)

OPERATION = "WSL_TEST_AD_" + "a" * 32


@pytest_asyncio.fixture
async def control(three_rule_ingress, tmp_path, monkeypatch):
    from astrbot.builtin_stars.wangshangliao_moderation import test_advertising
    from astrbot.core.platform.sources.wangshangliao import automatic, test_rules
    from astrbot.core.platform.sources.wangshangliao.progression_store import (
        ProgressionStore,
    )

    lab = three_rule_ingress
    bot = lab.receiver
    root = tmp_path / "production"
    for module in (test_rules, automatic, test_advertising, lab.content_rules):
        monkeypatch.setattr(module, "instance_dir", lambda _: root)
    rules = {"advertising": True, "flood": False, "keywords": [OPERATION]}
    bot.config["_open_groups"][GROUP] = rules
    bot.config["_open_managers"][GROUP] = [THIRD]
    bot.group_catalog = {GROUP: {"team": bot.groups[GROUP]}}
    bot.members[GROUP][RECEIVER] = bot.nim_account
    bot.send_text = AsyncMock(return_value="accepted")
    bot.business = SimpleNamespace()
    for account in (lab.sender, lab.third):
        account.connection_state = "online"
    rows = [
        {
            "userId": a.account,
            "nimId": a.nim_account,
            "groupRole": role,
            "accountState": "ACCOUNT_STATE_GOOD",
        }
        for a, role in (
            (lab.third, "GROUP_ROLE_OWNER"),
            (bot, "GROUP_ROLE_ADMIN"),
            (lab.sender, "GROUP_ROLE_MEMBER"),
        )
    ]
    bot.get_moderation_members.return_value = {
        "complete": True,
        "groupMemberInfo": rows,
    }
    window = lab.Window(bot, lab.sender, [GROUP], ["group_rules"], [OPERATION], 300, 6)
    bot.test_window = window
    lab.window = window
    tenant = {
        "active": True,
        "present": True,
        "paused": False,
        "managers": [THIRD],
        "epoch": 1,
        "version": 1,
        "rules": rules,
    }
    service = SimpleNamespace(
        active=True,
        integrated=True,
        settings={"paused": False},
        adapters=lambda: [lab.third, lab.sender, bot],
        tenants={(RECEIVER, GROUP): tenant},
        store=SimpleNamespace(tenant=lambda *_: dict(tenant)),
        suspended_groups=set(),
    )
    store = ProgressionStore(root / "moderation.sqlite3")
    store.initialize()
    with closing(automatic.database(bot)) as db, db:
        db.executemany(
            "INSERT INTO progressive_warnings VALUES(?,?,?,?,?)",
            [(RECEIVER, GROUP, SENDER, f"prior-{i}", "accepted") for i in range(3)],
        )
        db.executemany(
            "INSERT INTO automatic_mutes VALUES(?,?,?,?,?,?,?,1,0)",
            [
                (
                    f"prior-mute-{i}",
                    RECEIVER,
                    GROUP,
                    SENDER,
                    lab.sender.nim_account,
                    f"prior-mid-{i}",
                    "accepted",
                )
                for i in range(3)
            ],
        )
    lab.service, lab.tenant, lab.roster = service, tenant, rows
    lab.begin = lambda **kwargs: test_advertising.begin_advertising(
        service,
        kwargs.get("actor", "dashboard/offline"),
        kwargs.get("payload", {"operation_id": OPERATION}),
    )
    lab.live_path = root / "moderation.sqlite3"
    lab.test_path = root / "test-advertising" / OPERATION / "moderation.sqlite3"
    yield lab


def records(path, table):
    """Read selected test-owned audit tables without opening production files."""
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("nickname", ["Member", None])
async def test_real_observer_three_warnings_then_five_fifteen_sixty_without_reset(
    control, nickname
):
    lab, bot = control, control.receiver
    if nickname:
        next(row for row in lab.roster if row["userId"] == SENDER)["userNick"] = (
            nickname
        )
    before = {
        t: records(lab.live_path, t)
        for t in ("progressive_warnings", "progressive_actions", "automatic_mutes")
    }
    assert (await lab.begin())["test_only"]
    assert not lab.test_path.exists()
    for index in range(6):
        mid = str(1000 + index)
        payload = await ingest(lab, mid, OPERATION + f" AD {index}")
        assert payload["_test_rule_window"] == lab.window._rule_generation
        assert lab.window.accepted[(GROUP, mid)] == "group_rules"
        assert await lab.policy.handle(bot, GROUP, mid, payload)
        assert await lab.policy.handle(bot, GROUP, mid, payload)
        if index < 3:
            bot.execute_moderation.assert_not_awaited()
    assert [c[2] for c in bot.calls if c[0] == "mute"] == [5, 15, 60]
    assert len([c for c in bot.calls if c[0] == "recall"]) == 6
    assert bot.send_text.await_count == 3
    assert [call.args[2] for call in bot.send_text.await_args_list] == [
        (f"@{nickname} " if nickname else "") + f"第{i}次广告违规，消息已撤回。"
        "前三次撤回并警告，第4次起将禁言，请勿继续发布广告。"
        for i in (1, 2, 3)
    ]
    for call in bot.send_text.await_args_list:
        assert call.kwargs["auto_recall"] is True
        if nickname:
            assert call.kwargs["mentions"][0]["uid"] == int(lab.sender.nim_account)
            assert call.kwargs["mentions"][0]["nick"] == nickname
        else:
            assert call.kwargs["mentions"] is None
            assert SENDER not in call.args[2]
    assert len(records(lab.test_path, "progressive_warnings")) == 3
    assert len(records(lab.test_path, "progressive_actions")) == 6
    assert len(records(lab.test_path, "automatic_mutes")) == 3
    assert {t: records(lab.live_path, t) for t in before} == before
    assert (await lab.begin())["replayed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "actor",
        "operation",
        "extra_path",
        "wrong_window",
        "wrong_marker",
        "wrong_sender",
        "expired",
        "budget",
        "used_window",
        "existing_path",
        "paused",
        "manager",
        "rules",
        "native_role",
        "wrong_nim",
        "extra_member",
    ],
)
async def test_fixture_never_grants_identity_permissions_or_arbitrary_paths(
    control, fault
):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    lab, bot = control, control.receiver
    kwargs = {}
    if fault == "actor":
        kwargs["actor"] = "nim/900003"
    elif fault == "operation":
        kwargs["payload"] = {"operation_id": "WSL_TEST_AD_../other"}
    elif fault == "extra_path":
        kwargs["payload"] = {"operation_id": OPERATION, "path": str(lab.live_path)}
    elif fault == "wrong_window":
        bot.test_window = None
    elif fault == "wrong_marker":
        lab.window.keywords = ["WSL_TEST_other"]
    elif fault == "wrong_sender":
        lab.window.sender = lab.third
    elif fault == "expired":
        lab.window.expires = 0
    elif fault == "budget":
        lab.window.remaining = 10
    elif fault == "used_window":
        lab.window.admit(GROUP, "91", {"sender": SENDER, "text": OPERATION})
    elif fault == "existing_path":
        lab.test_path.parent.mkdir(parents=True)
        lab.test_path.touch()
    elif fault == "paused":
        lab.tenant["paused"] = True
    elif fault == "manager":
        lab.tenant["managers"] = []
    elif fault == "rules":
        lab.tenant["rules"] = {"advertising": False, "flood": True, "keywords": []}
    elif fault == "native_role":
        lab.roster[1]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif fault == "wrong_nim":
        lab.roster[2]["nimId"] = "999"
    else:
        lab.roster.append(dict(lab.roster[-1]))
    with pytest.raises(OpenError):
        await lab.begin(**kwargs)
    bot.execute_moderation.assert_not_awaited()
    bot.nim.request.assert_not_awaited()
    assert len(records(lab.live_path, "automatic_mutes")) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["close", "replace", "expire", "manager", "pause"])
async def test_revocation_during_sqlite_wait_stops_recall_and_keeps_live_history(
    control, fault
):
    lab, bot = control, control.receiver
    await lab.begin()
    payload = await ingest(lab, "1111", OPERATION + " AD")
    from astrbot.core.platform.sources.wangshangliao.progression_store import (
        ProgressionStore,
    )

    ProgressionStore(lab.test_path).initialize()
    blocker = sqlite3.connect(lab.test_path)
    blocker.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(lab.policy.handle(bot, GROUP, "1111", payload))
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
        if fault == "close":
            bot.test_window = None
        elif fault == "replace":
            bot.test_window = lab.Window(
                bot, lab.sender, [GROUP], ["group_rules"], [OPERATION], 300, 6
            )
        elif fault == "expire":
            lab.window.expires = 0
        elif fault == "manager":
            lab.tenant["managers"] = []
        else:
            lab.tenant["paused"] = True
        blocker.rollback()
        await asyncio.wait_for(task, 3)
        bot.nim.request.assert_not_awaited()
        bot.execute_moderation.assert_not_awaited()
        assert len(records(lab.live_path, "automatic_mutes")) == 3
    finally:
        blocker.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "accepted", "rejected"])
@pytest.mark.parametrize("moved", [False, True])
async def test_native_timeout_reads_original_receipt_after_window_closes(
    control, status, moved, monkeypatch, tmp_path
):
    from astrbot.core.platform.sources.wangshangliao import automatic

    lab, bot = control, control.receiver
    await lab.begin()
    await ingest(lab, "2001", OPERATION + " timeout")

    async def interrupted(operation, action, group, member, text="", **kwargs):
        await kwargs["action_check"]()
        receipt = {
            "action": action,
            "group": group,
            "member": member,
            "status": status,
        }
        if status == "rejected":
            receipt["submitted"] = False
        with closing(automatic.database(bot)) as db, db:
            db.execute(
                "CREATE TABLE operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
            )
            db.execute(
                "INSERT INTO operations VALUES(?, 'native', ?)",
                (operation, json.dumps(receipt)),
            )
        bot.test_window = None
        if moved:
            bot.config["id"] = "replacement-instance"
            monkeypatch.setattr(automatic, "instance_dir", lambda _: tmp_path / "new")
        raise TimeoutError("Controlled transport outcome")

    bot.execute_moderation.side_effect = interrupted
    operation = (
        "progressive/"
        + hashlib.sha256(
            json.dumps([RECEIVER, GROUP, SENDER, "2001"]).encode()
        ).hexdigest()
    )
    result = await automatic.mute_and_escalate(bot, operation, GROUP, SENDER, "2001")
    assert result["status"] == status
    assert result["mute_count"] == (1 if status == "accepted" else 0)
    isolated = records(lab.test_path, "automatic_mutes")
    assert len(isolated) == 1
    assert isolated[0][6] == status
    assert len(records(lab.live_path, "automatic_mutes")) == 3
    assert len(records(lab.live_path, "operations")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["counter", "native_mute", "native_unmute"])
async def test_fresh_counter_cannot_bypass_an_unresolved_native_result(control, source):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
    from astrbot.core.platform.sources.wangshangliao import automatic

    lab, bot = control, control.receiver
    with closing(automatic.database(bot)) as db, db:
        if source == "counter":
            db.execute("UPDATE automatic_mutes SET status='unknown'")
        else:
            db.execute(
                "CREATE TABLE operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
            )
            db.execute(
                "INSERT INTO operations VALUES('unresolved', 'native', ?)",
                (
                    json.dumps(
                        {
                            "status": "unknown",
                            "action": source.removeprefix("native_"),
                            "group": int(GROUP),
                            "member": int(SENDER),
                        }
                    ),
                ),
            )
    with pytest.raises(OpenError, match="test_advertising_unresolved"):
        await lab.begin()
    assert not hasattr(lab.window, "_advertising_fixture")
    assert not lab.test_path.exists()
    bot.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version", "rule", "pause", "window"])
async def test_directory_wait_cannot_commit_changed_test_authority(control, change):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    lab, bot = control, control.receiver
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_directory(group):
        entered.set()
        await release.wait()
        return {"complete": True, "groupMemberInfo": lab.roster}

    bot.get_moderation_members.side_effect = blocked_directory
    task = asyncio.create_task(lab.begin())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "version":
            lab.tenant["version"] += 1
        elif change == "rule":
            lab.tenant["rules"]["other_setting"] = True
        elif change == "pause":
            lab.tenant["paused"] = True
        else:
            bot.test_window = None
        release.set()
        with pytest.raises(OpenError, match="test_advertising_revoked"):
            await asyncio.wait_for(task, 1)
        assert not hasattr(lab.window, "_advertising_fixture")
        bot.nim.request.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["dashboard/offline", "nim/900003"])
async def test_allowlisted_rpc_requires_dashboard_principal(control, actor):
    from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import (
        OpenCall,
        OpenRPC,
    )
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    call = OpenCall(
        action="test_advertising", actor=actor, payload={"operation_id": OPERATION}
    )
    rpc = OpenRPC(control.service)
    if actor.startswith("dashboard/"):
        assert await rpc.dispatch(call) == {
            "test_only": True,
            "operation_id": OPERATION,
            "replayed": False,
        }
    else:
        with pytest.raises(OpenError, match="test_advertising_arguments"):
            await rpc.dispatch(call)
        assert not hasattr(control.window, "_advertising_fixture")
