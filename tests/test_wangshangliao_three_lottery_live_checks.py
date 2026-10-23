"""Keep three-bot lottery evidence distinct from ordinary entrant acceptance."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import check_wsl_pair_live as live


@pytest.fixture
def flow(tmp_path, monkeypatch):
    """Model only the controlled runner's IO, not production activity behavior."""
    check = live.PairCheck.__new__(live.PairCheck)
    check.existing_fixture = "wangshangliao_6541e09d"
    check.native_path = tmp_path / "group_management.sqlite3"
    check.marker = "WSL_TEST_LOTTERY_FIXTURE"
    check.members = {live.ORDINARY: {"nimId": "111"}}
    check.results = []
    check.events = []
    check.rules = AsyncMock()
    state = SimpleNamespace(
        check=check,
        status=None,
        fault="",
        config={
            "revision": 1,
            "invite_rate": 5,
            "invitation": {"enabled": False},
            "lottery": {
                "prize": "",
                "winners": 1,
                "duration_minutes": 10,
                "capacity": 0,
                "invite_gate": 0,
                "contact": "",
            },
        },
    )
    state.original = copy.deepcopy(state.config)

    async def api(method, path, body=None):
        if method == "POST" and path == live.PREFIX + "open-service":
            assert body["action"] == "test_lottery"
            state.status = {"start": "open", "draw": "finished", "cancel": "cancelled"}[
                body["payload"]["action"]
            ]
            return {
                "channel": "dashboard-test",
                "status": "accepted",
                "message_id": "notice",
                "fixture_id": "own-test",
            }
        if method == "POST":
            assert body["invite_rate"] == state.original["invite_rate"]
            assert body["revision"] == state.config["revision"]
            state.config["lottery"] = copy.deepcopy(body["lottery"])
            state.config["revision"] += 1
        return copy.deepcopy(state.config)

    async def command(text, expected, **kwargs):
        if text == "开启抽奖":
            state.status = "open"
        elif text == "立即开奖":
            state.status = "finished"
        elif text == "取消抽奖":
            state.status = "cancelled"
        else:
            raise AssertionError(text)
        return expected

    async def reply(receipt):
        if (
            check.existing_fixture
            and not getattr(check, "_lottery_member_active", False)
            and state.fault != "accepted_robot"
        ):
            return "@" + live.ORDINARY + " 机器人账号不参与抽奖。"
        return "@test 参加抽奖成功。"

    def rows(path, statement, values=()):
        managed = bool(check.existing_fixture) and not getattr(
            check, "_lottery_member_active", False
        )
        if statement.startswith("SELECT 1 FROM lotteries"):
            return []
        if statement.startswith("SELECT * FROM lotteries"):
            return [{"id": "own-test", "status": state.status, "owner": live.OWNER}]
        if statement.startswith("SELECT member,peer FROM lottery_entries"):
            return [] if managed else [{"member": live.ORDINARY, "peer": "111"}]
        if statement.startswith("SELECT state,server_id FROM outbox"):
            return [{"state": "accepted", "server_id": "notice"}]
        if statement.startswith("SELECT status,results,total FROM lotteries"):
            winners = (
                []
                if managed and state.fault != "managed_winner"
                else [{"uid": live.ORDINARY, "peer": "111"}]
            )
            return [
                {
                    "status": state.status,
                    "results": json.dumps(winners),
                    "total": len(winners),
                }
            ]
        if statement.startswith("SELECT id,status,owner FROM lotteries"):
            return [{"id": "own-test", "status": state.status, "owner": live.OWNER}]
        if statement.startswith("SELECT id FROM lotteries"):
            return [{"id": "own-test"}]
        raise AssertionError(statement)

    class Replies:
        def __contains__(self, message):
            return message == "notice"

        def __getitem__(self, message):
            assert message == "notice"
            return {
                "sender": live.PEER if state.fault != "wrong_sender" else live.OWNER,
                "mentions": (
                    []
                    if check.existing_fixture
                    and not getattr(check, "_lottery_member_active", False)
                    else ["111"]
                ),
                "text": state.config["lottery"]["prize"],
            }

    check.api = AsyncMock(side_effect=api)
    check.command = AsyncMock(side_effect=command)
    check.probe = AsyncMock(return_value={"status": "accepted", "message": "input"})
    check.wait_reply = AsyncMock(side_effect=reply)
    check.ordinary = SimpleNamespace(replies=Replies())
    check.record = lambda case, **values: check.events.append({"case": case, **values})
    monkeypatch.setattr(live, "read_rows", rows)
    return state


@pytest.mark.asyncio
async def test_three_bots_verify_rejection_and_empty_draw_not_ordinary_enrollment(flow):
    await flow.check.lottery()
    result = next(
        e for e in flow.check.events if e["case"] == "lottery_managed_guard_passed"
    )
    assert result["managed_account_rejected"] and result["empty_draw_verified"]
    assert not result["ordinary_enrollment_verified"]
    assert not result["native_mention_verified"]
    assert not any(e["case"] == "lottery_passed" for e in flow.check.events)
    assert flow.config["lottery"] == flow.original["lottery"]
    assert flow.config["invitation"] == flow.original["invitation"]
    assert flow.config["invite_rate"] == flow.original["invite_rate"]
    assert flow.status == "finished"


@pytest.mark.asyncio
async def test_external_entrant_still_requires_native_winner_and_durable_entry(flow):
    flow.check.existing_fixture = None
    await flow.check.lottery()
    result = next(e for e in flow.check.events if e["case"] == "lottery_passed")
    assert result["ordinary_enrollment_verified"] and result["native_mention_verified"]
    assert not result["managed_account_rejected"]
    assert not result["empty_draw_verified"]


@pytest.mark.asyncio
async def test_controlled_native_member_preserves_window_and_labels_owner_dashboard(
    flow,
):
    await flow.check.lottery(controlled=True)
    result = next(
        e for e in flow.check.events if e["case"] == "lottery_controlled_member_passed"
    )
    assert result["ordinary_enrollment_verified"] and result["native_mention_verified"]
    assert result["controlled_native_member"]
    assert result["owner_control_channel"] == "dashboard-test"
    assert not result["managed_account_rejected"]
    flow.check.command.assert_not_awaited()
    actions = [
        call.args[2]["payload"]["action"]
        for call in flow.check.api.await_args_list
        if call.args[1] == live.PREFIX + "open-service"
    ]
    assert actions == ["start", "draw"]
    assert flow.config["lottery"] == flow.original["lottery"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "error"),
    [
        ("accepted_robot", "Managed entrant was not rejected"),
        ("managed_winner", "Lottery not durably finished"),
        ("wrong_sender", "Exact native result"),
    ],
)
async def test_managed_flow_never_hides_registration_or_result_errors(
    flow, fault, error
):
    flow.fault = fault
    with pytest.raises(RuntimeError, match=error):
        await flow.check.lottery()
    assert flow.config["lottery"] == flow.original["lottery"]
    assert flow.status in {"finished", "cancelled"}
    assert not any(
        e["case"] in {"lottery_passed", "lottery_managed_guard_passed"}
        for e in flow.check.events
    )
