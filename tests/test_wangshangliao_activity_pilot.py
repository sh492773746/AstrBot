"""Offline safety checks for the opt-in fixed-group acceptance driver."""

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fcntl", reason="POSIX-only maintenance CLI")

from scripts import check_wsl_activity_pilot as pilot


def test_native_receipts_do_not_claim_official_phone_acceptance(tmp_path):
    check = pilot.Pilot(None, tmp_path)
    assert check.evidence["acceptance_level"] == "transport_only"
    assert check.evidence["phone_verified"] is False


@pytest.mark.asyncio
async def test_rpc_keeps_envelope_action_separate_from_lottery_action(tmp_path):
    check = pilot.Pilot(None, tmp_path)
    check.api = AsyncMock(return_value={"status": "accepted"})
    await check.rpc(
        "test_lottery", operation_id="WSL_TEST_LOTTERY_fixture", action="start"
    )
    assert check.api.await_args.args == (
        "POST",
        pilot.PREFIX + "open-service",
        {
            "action": "test_lottery",
            "payload": {
                "operation_id": "WSL_TEST_LOTTERY_fixture",
                "action": "start",
            },
        },
    )


@pytest.mark.asyncio
async def test_unknown_unmute_is_never_automatically_resubmitted(tmp_path):
    check = pilot.Pilot(None, tmp_path)
    check.own_mute = True
    check.rpc = AsyncMock(
        side_effect=[
            {"version": 1, "members": [{"id": pilot.MEMBER}]},
            {"confirmation": "single-use"},
            {"status": "unknown", "verified": False},
        ]
    )
    with pytest.raises(RuntimeError, match="Unmute not verified"):
        await check.unmute()
    assert not check.own_mute
    assert check.rpc.await_count == 3


@pytest.mark.asyncio
async def test_changed_formal_group_stops_before_test_writes(tmp_path, monkeypatch):
    check = pilot.Pilot(None, tmp_path)
    check.baseline = "baseline"
    monkeypatch.setattr(pilot, "protected", lambda: "changed")
    check.state = AsyncMock()
    with pytest.raises(RuntimeError, match="Protected"):
        await check.guard()
    check.state.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_mute_is_never_guessed_from_history(tmp_path):
    check = pilot.Pilot(None, tmp_path)
    check.rpc = AsyncMock(return_value={"version": 1, "members": [{"id": "different"}]})
    with pytest.raises(RuntimeError, match="Unexpected native mute target"):
        await check.unmute()
    check.rpc.assert_awaited_once()
