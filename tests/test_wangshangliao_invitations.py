"""Read-only inviter codec, identity mapping and lifecycle regression tests."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.nim import NimClient


def inviter_reply(pairs):
    result = wire.varint(len(pairs))
    for pair in pairs:
        for item in pair:
            encoded = item.encode()
            result += wire.varint(len(encoded)) + encoded
    return result


@pytest.mark.asyncio
async def test_inviter_query_matches_sdk_long_array_and_map_vector():
    client = NimClient(None)
    client.request = AsyncMock(
        return_value=(200, bytes.fromhex("02023232023131023233023131"))
    )
    assert await client.get_team_inviters("55", ["22", "23"]) == {
        "22": "11",
        "23": "11",
    }
    client.request.assert_awaited_once_with(
        8, 33, bytes.fromhex("370000000000000002023232023233")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("team", "accounts"),
    [
        ("0", ["22"]),
        ("-1", ["22"]),
        ("55", []),
        (55, ["22"]),
        ("x55", ["22"]),
        ("9" * 30, ["22"]),
        (str(1 << 64), ["22"]),
        ("55", ["22", "22"]),
        ("55", [None]),
        ("55", [""]),
        ("55", ["22\n"]),
        ("55", ["x" * 1025]),
        ("55", ("22",)),
        ("55", [str(i + 1) for i in range(201)]),
    ],
)
async def test_inviter_query_invalid_input_never_sends(team, accounts):
    client = NimClient(None)
    client.request = AsyncMock()
    with pytest.raises(wire.ProtocolError, match="invitation_arguments"):
        await client.get_team_inviters(team, accounts)
    client.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"\x03",
        inviter_reply([("24", "11")]),
        inviter_reply([("22", "11"), ("22", "12")]),
        inviter_reply([("22", "11")]) + b"\x00",
        inviter_reply([("22", "11\n")]),
        b"\x01\x0222\x01\xff",
        b"\x01\x0222\x0511",
        b"\x01\x0222" + wire.varint(1025) + b"x" * 1025,
    ],
)
async def test_inviter_query_rejects_unrelated_ambiguous_or_malformed_data(reply):
    client = NimClient(None)
    client.request = AsyncMock(return_value=(200, reply))
    with pytest.raises(wire.ProtocolError):
        await client.get_team_inviters("55", ["22", "23"])


@pytest.mark.asyncio
async def test_inviter_query_preserves_empty_and_missing_attribution():
    client = NimClient(None)
    client.request = AsyncMock(
        side_effect=[(200, b"\x00"), (200, inviter_reply([("22", "")]))]
    )
    assert await client.get_team_inviters("55", ["22"]) == {}
    assert await client.get_team_inviters("55", ["22"]) == {"22": ""}


@pytest.mark.asyncio
async def test_inviter_query_rejection_has_no_retry_or_write():
    client = NimClient(None)
    client.request = AsyncMock(return_value=(403, b"private-upstream-value"))
    with pytest.raises(wire.ProtocolError, match="invitation_query_rejected"):
        await client.get_team_inviters("55", ["22"])
    assert client.request.await_count == 1


def make_adapter():
    adapter = WangshangliaoAdapter(
        {
            "id": "invitation-fixture",
            "account_id": "1",
            "enabled_groups": ["5"],
            "enable": True,
        },
        {},
        asyncio.Queue(),
    )
    adapter.connection_state = "online"
    adapter.groups = {"5": "55"}
    adapter.business = SimpleNamespace(
        fields=[0, 0, 0, 1],
        request=AsyncMock(return_value={"list": [], "lastId": ""}),
    )
    adapter.nim = SimpleNamespace(
        get_team_inviters=AsyncMock(return_value={"22": "11"})
    )
    adapter.refresh_member_mapping = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {"userId": "1", "nimId": "11", "userNick": "Inviter"},
                {"userId": "2", "nimId": "22", "userNick": "New member"},
                {"userId": "3", "nimId": "33", "userNick": "Unattributed"},
            ],
        }
    )
    return adapter


@pytest.mark.asyncio
async def test_adapter_resolves_business_id_from_verified_peer_not_nickname():
    adapter = make_adapter()
    result = await adapter.get_group_inviters("5")
    row = next(item for item in result["items"] if item["member_id"] == "2")
    assert row == {
        "member_id": "2",
        "member_name": "New member",
        "member_nim_id": "22",
        "inviter_id": "1",
        "inviter_name": "Inviter",
        "inviter_nim_id": "11",
        "status": "attributed",
    }
    assert result["scope"] == "current_members"
    assert result["source"] == "nim_team_member_inviter"
    assert result["complete"]
    assert result["queried_count"] == 3
    assert adapter.ledger.db is None
    adapter.nim.get_team_inviters.assert_awaited_once_with("55", ["11", "22", "33"])
    with pytest.raises(wire.ProtocolError, match="invitation_read_cooldown"):
        await adapter.get_group_inviters("5")
    assert adapter.nim.get_team_inviters.await_count == 1


@pytest.mark.asyncio
async def test_adapter_unknown_inviter_is_not_invented_or_assigned_to_administrator():
    adapter = make_adapter()
    adapter.nim.get_team_inviters.return_value = {"22": "unknown-former-peer"}
    result = await adapter.get_group_inviters("5", "2")
    assert result["items"][0]["inviter_id"] == ""
    assert result["items"][0]["inviter_name"] == ""
    assert result["items"][0]["inviter_nim_id"] == "unknown-former-peer"
    assert result["items"][0]["status"] == "inviter_not_in_roster"
    adapter.nim.get_team_inviters.assert_awaited_once_with("55", ["22"])


@pytest.mark.asyncio
async def test_adapter_pending_or_absent_member_cannot_be_reported_as_joined():
    adapter = make_adapter()
    with pytest.raises(wire.ProtocolError, match="invitation_member_not_found"):
        await adapter.get_group_inviters("5", "99")
    adapter.nim.get_team_inviters.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["disabled", "offline", "wrong_account", "wrong_group", "stopping", "no_nim"],
)
async def test_adapter_invalid_scope_never_queries(change):
    adapter = make_adapter()
    nim = adapter.nim
    if change == "disabled":
        adapter.config["enable"] = False
    elif change == "offline":
        adapter.connection_state = "reconnecting"
    elif change == "wrong_account":
        adapter.business.fields[3] = 9
    elif change == "wrong_group":
        adapter.config["enabled_groups"] = ["6"]
    elif change == "stopping":
        adapter.stopping.set()
    else:
        adapter.nim = None
    with pytest.raises(wire.ProtocolError, match="invitation_scope"):
        await adapter.get_group_inviters("5")
    adapter.refresh_member_mapping.assert_not_called()
    nim.get_team_inviters.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_rechecks_scope_after_directory_read():
    adapter = make_adapter()
    roster = adapter.refresh_member_mapping.return_value

    async def refresh(_group):
        adapter.config["enabled_groups"] = []
        return roster

    adapter.refresh_member_mapping.side_effect = refresh
    with pytest.raises(wire.ProtocolError, match="invitation_scope"):
        await adapter.get_group_inviters("5")
    adapter.nim.get_team_inviters.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_discards_response_after_connection_replacement():
    adapter = make_adapter()
    nim = adapter.nim

    async def query(_team, _accounts):
        adapter.nim = SimpleNamespace()
        return {"22": "11"}

    nim.get_team_inviters.side_effect = query
    with pytest.raises(wire.ProtocolError, match="invitation_scope"):
        await adapter.get_group_inviters("5")


@pytest.mark.asyncio
async def test_adapter_never_publishes_partial_directory_attribution():
    adapter = make_adapter()
    adapter.refresh_member_mapping.return_value["complete"] = False
    with pytest.raises(wire.ProtocolError, match="invitation_roster_incomplete"):
        await adapter.get_group_inviters("5")
    adapter.nim.get_team_inviters.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_caps_query_and_marks_large_roster_incomplete():
    adapter = make_adapter()
    members = [{"userId": str(i + 1), "nimId": str(i + 101)} for i in range(201)]
    adapter.refresh_member_mapping.return_value["groupMemberInfo"] = members
    adapter.nim.get_team_inviters.return_value = {}
    result = await adapter.get_group_inviters("5")
    assert result["member_count"] == 201
    assert result["queried_count"] == 200
    assert not result["complete"]
    assert len(adapter.nim.get_team_inviters.call_args.args[1]) == 200
    assert all(row["status"] == "unattributed" for row in result["items"])


@pytest.mark.asyncio
async def test_exact_joined_batch_queries_beyond_first_two_hundred():
    adapter = make_adapter()
    members = [
        {
            "userId": str(i + 1),
            "nimId": str(i + 101),
            "accountState": "ACCOUNT_STATE_GOOD",
        }
        for i in range(300)
    ]
    adapter.refresh_member_mapping.return_value["groupMemberInfo"] = members
    adapter.nim.get_team_inviters.return_value = {"400": "101"}
    result = await adapter.get_group_inviters("5", members=["300"])
    assert result["complete"] and result["member_count"] == 300
    assert result["queried_count"] == 1
    assert result["items"][0]["inviter_id"] == "1"
    assert result["items"][0]["member_state"] == "ACCOUNT_STATE_GOOD"
    assert result["items"][0]["inviter_state"] == "ACCOUNT_STATE_GOOD"
    adapter.nim.get_team_inviters.assert_awaited_once_with("55", ["400"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "members", [[], ["2", "2"], ["0"], ["x"], [True], "2", ["2"] * 201]
)
async def test_inviter_batch_invalid_or_duplicate_ids_never_query(members):
    adapter = make_adapter()
    with pytest.raises(wire.ProtocolError, match="invitation_arguments"):
        await adapter.get_group_inviters("5", members=members)
    adapter.refresh_member_mapping.assert_not_called()
    adapter.nim.get_team_inviters.assert_not_called()


@pytest.mark.asyncio
async def test_inviter_batch_requires_all_requested_members_to_be_joined():
    adapter = make_adapter()
    with pytest.raises(wire.ProtocolError, match="invitation_member_not_found"):
        await adapter.get_group_inviters("5", members=["2", "99"])
    adapter.nim.get_team_inviters.assert_not_called()


def invitation_record(apply_id="91", group=5, member=99, state="MEMBER_STATE_INVITED"):
    return {
        "applyId": apply_id,
        "groupId": group,
        "state": state,
        "groupApplyType": "INVITATION",
        "enterGroupMode": "INVITE",
        "applyTime": "2026-10-01T11:21:00Z",
        "applicant": {
            "userId": member,
            "accountId": 199,
            "nimId": 299,
            "nick": "Not yet joined",
            "avatar": "private-avatar-url",
        },
        "inviter": {
            "userId": 1,
            "accountId": 101,
            "nimId": 11,
            "nick": "Inviter",
        },
        "remark": "private-remark",
    }


@pytest.mark.asyncio
async def test_pending_records_do_not_require_membership_or_nim_query():
    adapter = make_adapter()
    record = invitation_record()
    adapter.business.request.return_value = {
        "list": [record, invitation_record("92", group=6)],
        "lastId": "92",
        "jwtToken": "private-session",
    }
    adapter.nim = None
    result = await adapter.get_group_invitation_records("5")
    item = result["items"][0]
    assert item == {
        "apply_id": "91",
        "state": "MEMBER_STATE_INVITED",
        "pending": True,
        "apply_time": "2026-10-01T11:21:00Z",
        "member_id": "99",
        "member_account_id": "199",
        "member_nim_id": "299",
        "member_name": "Not yet joined",
        "inviter_id": "1",
        "inviter_account_id": "101",
        "inviter_nim_id": "11",
        "inviter_name": "Inviter",
        "status": "attributed",
    }
    assert result["scope"] == "account_visible_records"
    assert result["source"] == "business_apply_logs"
    assert result["viewer_id"] == "1"
    assert not result["complete"]
    assert result["pending_only"]
    assert result["last_id"] == "92"
    assert len(result["items"]) == 1
    assert "private" not in str(result)
    assert adapter.ledger.db is None
    adapter.business.request.assert_awaited_once_with("/v1/group/get-apply-logs", {})
    adapter.refresh_member_mapping.assert_not_called()
    with pytest.raises(wire.ProtocolError, match="invitation_read_cooldown"):
        await adapter.get_group_invitation_records("5")
    assert adapter.business.request.await_count == 1


@pytest.mark.asyncio
async def test_record_cursor_and_member_filter_do_not_invent_group_parameters():
    adapter = make_adapter()
    adapter.business.request.return_value = {
        "list": [invitation_record(), invitation_record("92", member=100)],
        "lastId": "93",
    }
    result = await adapter.get_group_invitation_records("5", "99", last_id="90")
    assert [item["member_id"] for item in result["items"]] == ["99"]
    assert result["last_id"] == "93"
    adapter.business.request.assert_awaited_once_with(
        "/v1/group/get-apply-logs", {"lastId": "90"}
    )


@pytest.mark.asyncio
async def test_pending_filter_keeps_applied_and_invited_not_history_or_self_applications():
    adapter = make_adapter()
    self_application = invitation_record("96")
    self_application["groupApplyType"] = "APPLY"
    adapter.business.request.return_value = {
        "list": [
            invitation_record(),
            invitation_record("92", state="MEMBER_STATE_APPLIED"),
            invitation_record("93", state="MEMBER_STATE_GOOD"),
            invitation_record("94", state="MEMBER_STATE_MANAGER_REJECT"),
            invitation_record("95", state="FUTURE_STATE"),
            self_application,
        ],
        "lastId": "96",
    }
    result = await adapter.get_group_invitation_records("5")
    assert [item["apply_id"] for item in result["items"]] == ["91", "92"]
    assert all(item["pending"] is True for item in result["items"])
    adapter.invitation_read_at.clear()
    result = await adapter.get_group_invitation_records("5", pending_only=False)
    assert len(result["items"]) == 5
    assert [item["pending"] for item in result["items"]] == [
        True,
        True,
        False,
        False,
        None,
    ]
    assert not result["complete"]


@pytest.mark.asyncio
async def test_absent_record_is_account_visibility_not_no_invitations_anywhere():
    adapter = make_adapter()
    result = await adapter.get_group_invitation_records("5", "99")
    assert result["items"] == []
    assert result["scope"] == "account_visible_records"
    assert not result["complete"]


@pytest.mark.asyncio
async def test_pending_record_without_inviter_does_not_guess_administrator():
    adapter = make_adapter()
    record = invitation_record()
    record["inviter"] = None
    adapter.business.request.return_value = {"list": [record], "lastId": "91"}
    result = await adapter.get_group_invitation_records("5")
    item = result["items"][0]
    assert item["member_id"] == "99"
    assert item["inviter_id"] == ""
    assert item["status"] == "unattributed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["disabled", "offline", "wrong_account", "wrong_group", "stopping", "no_business"],
)
async def test_record_read_invalid_scope_never_sends(change):
    adapter = make_adapter()
    business = adapter.business
    if change == "disabled":
        adapter.config["enable"] = False
    elif change == "offline":
        adapter.connection_state = "reconnecting"
    elif change == "wrong_account":
        adapter.business.fields[3] = 9
    elif change == "wrong_group":
        adapter.config["enabled_groups"] = ["6"]
    elif change == "stopping":
        adapter.stopping.set()
    else:
        adapter.business = None
    with pytest.raises(wire.ProtocolError, match="invitation_scope"):
        await adapter.get_group_invitation_records("5")
    business.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["disabled", "offline", "wrong_account", "wrong_group", "stopping", "replaced"],
)
async def test_record_read_discards_result_after_scope_changes(change):
    adapter = make_adapter()

    async def read(_route, _params):
        if change == "disabled":
            adapter.config["enable"] = False
        elif change == "offline":
            adapter.connection_state = "reconnecting"
        elif change == "wrong_account":
            adapter.business.fields[3] = 9
        elif change == "wrong_group":
            adapter.config["enabled_groups"] = []
        elif change == "stopping":
            adapter.stopping.set()
        else:
            adapter.business = SimpleNamespace(fields=[0, 0, 0, 1])
        return {"list": [invitation_record()], "lastId": "91"}

    adapter.business.request.side_effect = read
    with pytest.raises(wire.ProtocolError, match="invitation_scope"):
        await adapter.get_group_invitation_records("5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"last_id": 91},
        {"last_id": "../other"},
        {"last_id": "9" * 21},
        {"last_id": "９１"},
        {"pending_only": "false"},
    ],
)
async def test_record_arguments_are_validated_before_read(kwargs):
    adapter = make_adapter()
    with pytest.raises(wire.ProtocolError, match="invitation_arguments"):
        await adapter.get_group_invitation_records("5", **kwargs)
    adapter.business.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        {"list": {}},
        {"list": [None]},
        {"list": [], "lastId": 91},
        {"list": [], "lastId": "private-value"},
        {"list": [], "lastId": "9" * 21},
        {"list": [invitation_record(), invitation_record()]},
        {"list": [invitation_record(apply_id="")]},
        {"list": [invitation_record(apply_id="0")]},
        {"list": [invitation_record(apply_id="secret")]},
        {"list": [invitation_record(state=None)]},
        {"list": [invitation_record(state="x" * 81)]},
        {"list": [invitation_record(member=True)]},
        {"list": [invitation_record(member=0)]},
        {"list": [invitation_record(member="nickname")]},
        {"list": [invitation_record(member="9" * 21)]},
        {"list": [invitation_record(str(i + 1)) for i in range(201)]},
        {"list": [{}] * 4097},
    ],
)
async def test_record_malformed_identity_or_framing_is_not_published(response):
    adapter = make_adapter()
    adapter.business.request.return_value = copy.deepcopy(response)
    with pytest.raises(wire.ProtocolError, match="invitation_response"):
        await adapter.get_group_invitation_records("5")


@pytest.mark.asyncio
async def test_record_provider_error_does_not_fall_back_to_nim_or_retry():
    adapter = make_adapter()
    adapter.business.request.side_effect = wire.ProtocolError("transport")
    with pytest.raises(wire.ProtocolError, match="transport"):
        await adapter.get_group_invitation_records("5")
    assert adapter.business.request.await_count == 1
    adapter.nim.get_team_inviters.assert_not_called()
