"""Report-member mute grants never become general administrator privileges."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import delegated_mutes
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    RETIRED_CHAT_REPLY,
    recognize_command,
)
from astrbot.core.platform.sources.wangshangliao.event import managed_group_control
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_command_parity import setup as setup

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture
async def scope(setup, tmp_path, monkeypatch):
    """Build a non-administrator delegate with separate report and grant files.

    Args:
        setup: Existing isolated native service.
        tmp_path: Private test persistence.
        monkeypatch: Scoped dependency replacements.

    Returns:
        Service, native event, mutable roster, grant path and temporary root.
    """
    value, _, event = setup
    monkeypatch.setattr(delegated_mutes, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.speech_activity.instance_dir",
        lambda _: tmp_path,
    )
    report = {
        "account": "1",
        "groups": [{"id": "5", "name": "Group"}],
        "members": [{"id": "4", "name": "Delegate"}],
    }
    (tmp_path / "speech_report.json").write_text(json.dumps(report))
    grant = tmp_path / "mute_delegates.json"
    grant.write_text(json.dumps({"account": "1", "groups": ["5"], "members": ["4"]}))
    rows = copy.deepcopy(value.roster.return_value)
    rows["4"] = {
        "userId": "4",
        "nimId": "904",
        "groupRole": "GROUP_ROLE_MEMBER",
        "userNick": "Delegate",
    }
    event.message_obj.sender.user_id = "4"
    event.actor_identity = ActorIdentity("1", "4", "904", "4")
    event.platform.members["5"] = {uid: str(row["nimId"]) for uid, row in rows.items()}
    text = "@Target 禁言128"
    event.message_str = text
    payload = event.get_extra("wangshangliao_payload")
    payload.update(
        text=text,
        sender="4",
        mentions=["903"],
        mention_spans=[{"uid": "903", "nick": "Target", "start": 0, "end": 7}],
    )
    value.roster = AsyncMock(side_effect=lambda *a: copy.deepcopy(rows))
    monkeypatch.setattr(delegated_mutes, "is_exempt", lambda *a: False)
    monkeypatch.setattr(delegated_mutes, "is_managed_account", lambda _: False)
    monkeypatch.setattr(
        delegated_mutes,
        "BlacklistStore",
        lambda _: SimpleNamespace(read=lambda *a: (0, [])),
    )
    monkeypatch.setattr(
        delegated_mutes, "muted_member_ids", AsyncMock(return_value=set())
    )
    await value.delegated_mutes.refresh(event.platform)

    async def execute(request, *, authorize):
        await authorize()
        return {"status": "accepted", "operation": request.operation}

    event.platform.execute_action.side_effect = execute
    return value, event, rows, grant, tmp_path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@大蛤 禁言128", "禁言 @大蛤 128"),
        ("@大蛤 禁言 128", "禁言 @大蛤 128"),
        ("@大蛤 禁言128分钟", "禁言 @大蛤 128"),
        ("@大蛤 禁言30分", "禁言 @大蛤 30"),
        ("@大蛤 禁言30分钟。", "禁言 @大蛤 30"),
        ("@大蛤 禁言。", "禁言 @大蛤"),
        ("@大蛤 禁言", "禁言 @大蛤"),
        ("@Name With Space 禁言 128", "禁言 @Name With Space 128"),
        ("@大蛤 禁言0", "禁言 @大蛤 0"),
        ("@大蛤 禁言1441", "禁言 @大蛤 1441"),
        ("不要@大蛤 禁言128", ""),
        ("@大蛤 禁言128 然后踢人", ""),
        ("@大蛤 禁言128\n加黑", ""),
        ("@大蛤 禁言-1", ""),
        ("@大蛤 禁言1.5", ""),
        ("@大蛤 今天不禁言", ""),
        ("@大蛤 禁言是什么意思", ""),
    ],
)
def test_exact_mention_suffix_syntax(text, expected):
    assert recognize_command(text) == expected


@pytest.mark.asyncio
async def test_nonadmin_report_delegate_can_only_submit_scoped_mute(scope):
    value, event, _, _, _ = scope
    assert not value.management.allowed(event, "5")
    result = await value.manager_command(event, recognize_command(event.message_str))
    assert "128分钟" in result
    request = event.platform.execute_action.await_args.args[0]
    assert (request.action, request.group, request.member, request.minutes) == (
        "mute",
        5,
        3,
        128,
    )
    assert request.expected_nim == "903"
    assert request.delegated_mute is True
    assert request.blacklist_intent is None
    assert not value.management.allowed(event, "5")
    for command in ("踢出 @Target", "白名单 @Target", "黑名单 @Target", "解禁 @Target"):
        event.platform.execute_action.reset_mock()
        try:
            reply = await value.manager_command(event, command)
        except OpenError:
            pass
        else:
            assert reply != "禁言已受理：128分钟。"
        event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", ["0", "4321", "99999", "", "-1", "1.5"])
async def test_duration_is_explicit_bounded_integer(scope, minutes):
    value, event, *_ = scope
    result = await value.manager_command(event, "禁言 @Target " + minutes)
    assert "1至4320" in result
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [1, 1440, 1441, 4320])
async def test_duration_boundaries(scope, minutes):
    value, event, *_ = scope
    reply = await value.manager_command(event, f"禁言 @Target {minutes}")
    assert f"{minutes}分钟" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["missing", "wrong_account", "wrong_group", "report"]
)
async def test_grants_do_not_follow_reports_or_cross_scope(scope, change):
    value, event, _, path, root = scope
    if change == "missing":
        path.unlink()
    elif change == "report":
        (root / "speech_report.json").unlink()
    else:
        config = json.loads(path.read_text())
        config["account" if change == "wrong_account" else "groups"] = (
            "8" if change == "wrong_account" else ["6"]
        )
        path.write_text(json.dumps(config))
    reply = await value.manager_command(event, "禁言 @Target 128")
    assert reply == RETIRED_CHAT_REPLY
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fake", "multiple", "span", "mismatch", "quote"])
async def test_real_unique_target_is_required(scope, change):
    value, event, *_ = scope
    payload = event.get_extra("wangshangliao_payload")
    command = "禁言 @Target 128"
    if change == "fake":
        payload.update(mentions=[], mention_spans=[])
    elif change == "multiple":
        payload["mentions"] = ["903", "902"]
    elif change == "span":
        payload["mention_spans"][0]["start"] = 1
    elif change == "mismatch":
        command = "禁言 @Another 128"
    else:
        payload["reply"] = {"server": "", "client": ""}
    with pytest.raises(OpenError):
        await value.manager_command(event, command)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["admin", "bot", "exempt", "blacklist", "muted"])
@pytest.mark.parametrize("minutes", [128, 4320])
async def test_protected_and_existing_mutes_are_not_overridden(
    scope, monkeypatch, protection, minutes
):
    value, event, rows, *_ = scope
    if protection == "admin":
        rows["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif protection == "bot":
        monkeypatch.setattr(delegated_mutes, "is_managed_account", lambda _: True)
    elif protection == "exempt":
        monkeypatch.setattr(delegated_mutes, "is_exempt", lambda *a: True)
    elif protection == "blacklist":
        monkeypatch.setattr(
            delegated_mutes,
            "BlacklistStore",
            lambda _: SimpleNamespace(read=lambda *a: (1, [{"member": "3"}])),
        )
    else:
        delegated_mutes.muted_member_ids.return_value = {"3"}
    with pytest.raises(OpenError, match="delegated_mute_(protected|existing)"):
        await value.manager_command(event, f"禁言 @Target {minutes}")
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["grant", "pause", "identity", "promotion", "caller"]
)
@pytest.mark.parametrize("minutes", [128, 4320])
async def test_queued_action_rechecks_revocation_and_native_identity(
    scope, change, minutes
):
    value, event, rows, grant, _ = scope

    async def waiting(request, *, authorize):
        if change == "grant":
            grant.unlink()
        elif change == "pause":
            value.settings["paused"] = True
        elif change == "promotion":
            rows["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
        elif change == "caller":
            rows.pop("4")
        else:
            rows["3"]["nimId"] = "999"
        await authorize()
        pytest.fail("Revoked action reached submission")

    event.platform.execute_action.side_effect = waiting
    with pytest.raises(OpenError):
        await value.manager_command(event, f"禁言 @Target {minutes}")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "rejected", "unknown"])
async def test_receipt_feedback_does_not_retry_or_claim_success(scope, status):
    value, event, *_ = scope
    event.platform.execute_action.side_effect = None
    event.platform.execute_action.return_value = {"status": status}
    result = await value.manager_command(event, "禁言 @Target 128")
    assert {"accepted": "受理", "rejected": "拒绝", "unknown": "未知"}[status] in result
    event.platform.execute_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_transport_exception_allows_mute_not_other_management(scope):
    value, event, *_ = scope
    event.platform.config = value.policy_values("1", event.platform.config)
    payload = event.get_extra("wangshangliao_payload")
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision

    payload.update(
        _open_revision=group_revision(event.platform.config, "5"),
        recall_route={"peer": "904"},
    )
    event.platform.config.update(_business_active=True)
    assert managed_group_control(event.platform, "5", payload) == "禁言 @Target 128"
    payload["text"] = "踢出 @Target"
    assert managed_group_control(event.platform, "5", payload) == ""
    payload["text"] = "@Target 黑名单"
    assert managed_group_control(event.platform, "5", payload) == ""


@pytest.mark.asyncio
async def test_full_group_event_routes_to_delegate_without_ai_or_admin_upgrade(scope):
    value, event, *_ = scope
    event.platform.config = value.policy_values("1", event.platform.config)
    payload = event.get_extra("wangshangliao_payload")
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision

    payload["_open_revision"] = group_revision(event.platform.config, "5")
    value.navigation.handle = AsyncMock(return_value=False)
    await value.on_event(event)
    event.platform.execute_action.assert_awaited_once()
    assert "128分钟" in event.platform.send_reply_text.await_args.args[2]
    assert not value.management.allowed(event, "5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("@Target 禁言30分", 30),
        ("@Target 禁言30分钟。", 30),
        ("禁言 @Target 30分", 30),
        ("禁言 @Target 30分钟。", 30),
        ("@Target 禁言", None),
        ("@Target 禁言。", None),
    ],
)
@pytest.mark.parametrize("reply_enabled", [True, False])
async def test_common_duration_and_missing_minutes_receive_fixed_feedback(
    scope, text, minutes, reply_enabled
):
    value, event, *_ = scope
    event.message_str = text
    payload = event.get_extra("wangshangliao_payload")
    start = text.index("@Target")
    payload.update(
        text=text,
        mention_spans=[
            {"uid": "903", "nick": "Target", "start": start, "end": start + 7}
        ],
        recall_route={"peer": "904"},
    )
    event.platform.config = value.policy_values("1", event.platform.config)
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision

    payload["_open_revision"] = group_revision(event.platform.config, "5")
    event.platform.config["reply_groups"] = {"5": reply_enabled}
    event.platform.config["_business_active"] = True
    admitted = managed_group_control(event.platform, "5", payload)
    assert admitted == recognize_command(text) and admitted.startswith("禁言 ")
    event.set_extra("_wsl_managed_group_control", admitted)
    value.navigation.handle = AsyncMock(return_value=False)
    value.group_ai.observe = AsyncMock()
    value.rule_replies.observe = AsyncMock(return_value=False)
    await value.on_event(event)
    event.platform.send_reply_text.assert_awaited_once()
    reply = event.platform.send_reply_text.await_args.args[2]
    if minutes is None:
        assert "未执行" in reply and "时长" in reply and "4320" in reply
        event.platform.execute_action.assert_not_awaited()
    else:
        assert f"禁言已受理：{minutes}分钟。" in reply
        assert event.platform.execute_action.await_args.args[0].minutes == minutes
    value.group_ai.observe.assert_not_awaited()
    value.rule_replies.observe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("promote", [False, True])
async def test_intermediate_fences_are_local_but_http_fence_reloads_roster(
    scope, promote
):
    value, event, rows, *_ = scope
    client = object.__new__(delegated_mutes.BusinessClient)
    event.platform.business = client

    async def execute(request, *, authorize):
        before = value.roster.await_count
        for _ in range(4):
            await authorize()
        assert value.roster.await_count == before
        client._authorization_task = asyncio.current_task()
        if promote:
            rows["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
        try:
            await authorize()
        finally:
            client._authorization_task = None
        return {"status": "accepted"}

    event.platform.execute_action.side_effect = execute
    if promote:
        with pytest.raises(OpenError, match="delegated_mute_protected"):
            await value.manager_command(event, "禁言 @Target 128")
    else:
        assert "128分钟" in await value.manager_command(event, "禁言 @Target 128")
    assert value.roster.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("minutes", [128, 1441, 4320])
async def test_native_operation_preserves_minutes_and_never_replays_same_message(
    scope, monkeypatch, unknown, minutes
):
    value, event, rows, _, root = scope
    from astrbot.core.platform.sources.wangshangliao import moderation
    from tests.fixtures.wsl_actions import bind_action_port

    monkeypatch.setattr(moderation, "instance_dir", lambda _: root / "operations")
    monkeypatch.setattr(moderation, "is_managed_account", lambda _: False)
    writes = []

    async def request(route, params, *, authorize=None):
        if authorize:
            await authorize()
        if route == "/v1/group/get-group-list":
            return {"owner": [{"groupId": "5", "me": {"role": "GROUP_ROLE_ADMIN"}}]}
        if route == "/v1/group/get-group-members":
            return {"groupMemberInfo": list(copy.deepcopy(rows).values())}
        assert route == "/v1/group/set-member-mute"
        writes.append(params)
        if unknown:
            raise TimeoutError()
        return {}

    client = SimpleNamespace(fields=[0, 0, 0, "1"], request=request)

    async def execute(
        operation,
        action,
        group,
        member,
        text="",
        *,
        minutes,
        action_check,
        delegated_mute=False,
        expected_nim=None,
    ):
        return await moderation.execute(
            client,
            "fixture",
            "1",
            operation,
            action,
            group,
            member,
            minutes=minutes,
            authorize=action_check,
            delegated_mute=delegated_mute,
            expected_nim=expected_nim,
        )

    bind_action_port(event.platform)
    event.platform.execute_moderation.side_effect = execute
    first = await value.manager_command(event, f"禁言 @Target {minutes}")
    second = await value.manager_command(event, f"禁言 @Target {minutes}")
    assert first == second
    assert ("未知" if unknown else "受理") in first
    assert writes == [{"groupId": 5, "userId": 3, "min": minutes}]


@pytest.mark.asyncio
async def test_three_day_mute_reaches_real_adapter_and_native_ledger(
    scope, monkeypatch
):
    value, event, rows, _, root = scope
    from astrbot.core.platform.sources.wangshangliao import moderation
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
    from tests.fixtures.wsl_actions import bind_action_port

    monkeypatch.setattr(moderation, "instance_dir", lambda _: root / "operations")
    monkeypatch.setattr(moderation, "is_managed_account", lambda _: False)
    writes = []

    async def request(route, params, *, authorize=None):
        if authorize:
            await authorize()
        if route == "/v1/group/get-group-list":
            return {"owner": [{"groupId": "5", "me": {"role": "GROUP_ROLE_ADMIN"}}]}
        if route == "/v1/group/get-group-members":
            return {"groupMemberInfo": list(copy.deepcopy(rows).values())}
        assert route == "/v1/group/set-member-mute"
        writes.append(params)
        return {}

    adapter = event.platform
    adapter.business = SimpleNamespace(fields=[0, 0, 0, "1"], request=request)
    adapter.stopping = asyncio.Event()
    adapter.get_moderation_members = AsyncMock(
        return_value={"complete": True, "groupMemberInfo": list(rows.values())}
    )
    bind_action_port(adapter)
    adapter.execute_moderation = WangshangliaoAdapter.execute_moderation.__get__(
        adapter
    )
    adapter._execute_moderation = WangshangliaoAdapter._execute_moderation.__get__(
        adapter
    )
    assert "4320分钟" in await value.manager_command(event, "禁言 @Target 4320")
    assert writes == [{"groupId": 5, "userId": 3, "min": 4320}]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["grant", "exempt"])
async def test_authority_change_during_native_mute_lookup_stops_submission(
    scope, monkeypatch, change
):
    value, event, _, grant, _ = scope
    protected = False
    calls = 0
    monkeypatch.setattr(delegated_mutes, "is_exempt", lambda *a: protected)

    async def lookup(*a, **kw):
        nonlocal protected, calls
        calls += 1
        if calls == 2:
            if change == "grant":
                grant.unlink()
            else:
                protected = True
        return set()

    delegated_mutes.muted_member_ids.side_effect = lookup
    with pytest.raises(OpenError):
        await value.manager_command(event, "禁言 @Target 128")
    event.platform.execute_action.assert_awaited_once()
