"""Fixed rehearsal capabilities do not weaken normal account protection."""

import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import test_flood
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.test_rules import (
    advertising_path,
    flood_fixture,
    rule_message,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_test_advertising import control as control
from tests.test_wangshangliao_three_rule_ingress import (
    three_rule_ingress as three_rule_ingress,
)

OPERATION = "WSL_TEST_FLOOD_" + "f" * 32


async def bind(lab, monkeypatch, case="rapid"):
    """Use the existing native fixture with fixed flood-only rules."""
    monkeypatch.setattr(test_flood, "instance_dir", lambda _: lab.live_path.parent)
    lab.receiver.nim = object()
    for account in (lab.sender, lab.third):
        account.nim = AsyncMock()
        account.business = SimpleNamespace(
            deployment=SimpleNamespace(key=lambda _: b"x" * 32)
        )
        account.ledger = create_autospec(Ledger, instance=True)
    lab.receiver.test_window = lab.window = lab.Window(
        lab.receiver, lab.sender, ["1459282"], ["group_rules"], [OPERATION], 300, 10
    )
    lab.tenant["rules"] = {"advertising": False, "flood": True, "keywords": []}
    lab.receiver.config["_open_groups"]["1459282"] = lab.tenant["rules"]
    return await test_flood.control_flood(
        lab.service,
        "dashboard/offline",
        {"operation": "begin", "operation_id": OPERATION, "case": case},
    )


@pytest.mark.asyncio
async def test_fixed_fixture_binds_without_native_actions(control, monkeypatch):
    lab = control
    assert (await bind(lab, monkeypatch))["bound"]
    assert lab.window.remaining == 20
    lab.receiver.execute_moderation.assert_not_awaited()
    fixture = flood_fixture(lab.receiver, "1459282")
    assert (
        fixture.path
        == lab.live_path.parent / "test-flood" / OPERATION / "moderation.sqlite3"
    )
    assert flood_fixture(lab.receiver, "1516387") is None
    with pytest.raises(ProtocolError):
        advertising_path(lab.receiver, "1459282", "23691273", "unadmitted")
    lab.window.accepted[("1459282", "123")] = "group_rules"
    assert advertising_path(lab.receiver, "1459282", "23691273", "123") == fixture.path
    operation = (
        "flood/"
        + hashlib.sha256(
            json.dumps(
                [
                    "43506527",
                    "1459282",
                    "23691273",
                    lab.sender.nim_account,
                    group_revision(lab.receiver.config, "1459282"),
                    "123",
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert rule_message(lab.receiver, "1459282", "23691273", operation) == "123"
    fixture.check(lab.receiver, "1459282", "23691273", operation, "mute", 3)
    for action, minutes in (("kick", 1), ("mute", 29), ("unmute", 1)):
        with pytest.raises(ProtocolError):
            fixture.check(
                lab.receiver, "1459282", "23691273", operation, action, minutes
            )
    lab.window.expires = 0
    with pytest.raises(Exception, match="test_flood_revoked"):
        flood_fixture(lab.receiver, "1459282")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["role", "paused", "window", "account", "permissions"]
)
async def test_revocation_fails_before_any_send(control, monkeypatch, change):
    lab = control
    if change == "role":
        lab.roster[2]["groupRole"] = "GROUP_ROLE_ADMIN"
        with pytest.raises(Exception, match="test_flood_roster"):
            await bind(lab, monkeypatch)
        return
    await bind(lab, monkeypatch)
    if change == "paused":
        lab.tenant["paused"] = True
    elif change == "window":
        lab.receiver.test_window = None
    elif change == "account":
        lab.sender.account = "999"
    else:
        lab.tenant["managers"] = []
    with pytest.raises(Exception):
        await test_flood.control_flood(
            lab.service,
            "dashboard/offline",
            {"operation": "send", "operation_id": OPERATION, "case": "rapid"},
        )
    lab.receiver.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_general_window_still_rejects_twenty(control):
    lab = control
    with pytest.raises(ProtocolError, match="test_window_arguments"):
        lab.Window(
            lab.receiver, lab.sender, ["1459282"], ["group_rules"], [OPERATION], 300, 20
        )


@pytest.mark.asyncio
async def test_unrequested_fields_never_select_target_or_payload(control):
    with pytest.raises(Exception, match="test_flood_arguments"):
        await test_flood.control_flood(
            control.service,
            "dashboard/offline",
            {
                "operation": "begin",
                "operation_id": OPERATION,
                "case": "rapid",
                "group_id": "1516387",
            },
        )


@pytest.mark.asyncio
async def test_native_sender_never_retries_same_case(control, monkeypatch):
    lab = control
    await bind(lab, monkeypatch)
    lab.window._flood_cases.add("rapid")
    lab.sender.nim = AsyncMock()
    with pytest.raises(Exception):
        await test_flood.control_flood(
            lab.service,
            "dashboard/offline",
            {"operation": "send", "operation_id": OPERATION, "case": "rapid"},
        )
    lab.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_uses_verified_receiver_route_without_enabling_sender_group(
    control, monkeypatch
):
    lab = control
    await bind(lab, monkeypatch, case="below")
    lab.sender.groups = {}
    lab.sender.ledger.reserve.return_value = (1, "new")
    lab.sender.nim.request.return_value = (
        200,
        test_flood.wire.properties([(7, b"1792836546000"), (12, b"123")]),
    )
    lab.service.flood_rehearsal.receipts["123"] = {
        "account": test_flood.OWNER,
        "group": test_flood.GROUP,
        "sender": test_flood.TARGET,
        "message": "123",
    }
    monkeypatch.setattr(
        test_flood.wire, "seal_message", lambda *_: "fixed-neutral-test-envelope"
    )
    result = await test_flood.control_flood(
        lab.service,
        "dashboard/offline",
        {"operation": "send", "operation_id": OPERATION, "case": "below"},
    )
    assert len(result["receipts"]) == 4
    assert lab.sender.groups == {}
    for call in lab.sender.nim.request.await_args_list:
        properties, _ = test_flood.wire.read_properties(call.args[2])
        assert properties[1].decode() == lab.receiver.groups["1459282"]


@pytest.mark.asyncio
async def test_live_runner_cleanup_does_not_stop_after_one_failure():
    from scripts.check_wsl_flood_cycle_live import cleanup_steps, restore_values

    first = AsyncMock(side_effect=RuntimeError("rules conflict"))
    second, third = AsyncMock(), AsyncMock()
    records = []
    errors = await cleanup_steps(
        [("rules", first), ("role", second), ("enable", third)],
        lambda case, **data: records.append((case, data)),
    )
    assert errors == ["rules"]
    second.assert_awaited_once()
    third.assert_awaited_once()
    original = {
        "rules": {"advertising": True, "flood": True, "keywords": []},
        "managers": ["31846829"],
        "paused": 0,
    }
    restored = restore_values(original)
    assert restored["rules"]["custom_rules"] == []
    assert restored["paused"] is False
    assert "custom_rules" not in original["rules"]


@pytest.mark.asyncio
async def test_passive_receipt_observer_is_fixed_scope_and_expires(
    control, monkeypatch
):
    lab = control
    await bind(lab, monkeypatch)
    observer = lab.service.flood_rehearsal
    inner = test_flood.wire.ApplicationMessage(
        sender=test_flood.wire.Source(id=int(test_flood.TARGET)),
        target=test_flood.wire.Source(id=int(test_flood.GROUP)),
        session=2,
        format=0,
        client_id="fixed-client",
        content=test_flood.wire.Content(data=OPERATION + " 中性验收 below 0"),
    )
    observer.expected[inner.client_id] = inner.content.data
    outer = {
        0: "1",
        1: lab.receiver.groups[test_flood.GROUP],
        2: lab.sender.nim_account,
        8: "100",
        11: inner.client_id,
        12: "123",
    }
    assert not await observer.receive(lab.receiver, outer, inner)
    assert not await observer.receive(lab.third, {**outer, 2: "untrusted"}, inner)
    inner.target.id = 1516387
    assert not await observer.receive(lab.third, outer, inner)
    inner.target.id = int(test_flood.GROUP)
    assert await observer.receive(lab.third, outer, inner)
    assert set(observer.receipts) == {"123"}
    lab.window.expires = 0
    assert not await observer.receive(lab.third, {**outer, 12: "124"}, inner)
    assert set(observer.receipts) == {"123"}


@pytest.mark.asyncio
async def test_close_retracts_only_the_fixed_temporary_grant(control, monkeypatch):
    lab = control
    await bind(lab, monkeypatch, case="rank")
    path = lab.live_path.parent / "automatic_exemptions.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE automatic_exemptions(account,group_id,member)")
        entries = [
            ("43506527", "1459282", "23691273"),
            ("43506527", "1516387", "23691273"),
            ("43506527", "1459282", "12345"),
        ]
        db.executemany("INSERT INTO automatic_exemptions VALUES(?,?,?)", entries)
    result = await test_flood.control_flood(
        lab.service,
        "dashboard/offline",
        {"operation": "close", "operation_id": OPERATION},
    )
    assert result["closed"]
    assert flood_fixture(lab.receiver, "1459282") is None
    assert lab.service.flood_rehearsal is None
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT * FROM automatic_exemptions").fetchall() == entries[1:]
        )
