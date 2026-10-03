from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import policy
from astrbot.core.platform.sources.wangshangliao.policy import (
    authorize_action,
    validate_policy,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


def fresh_payload(text="fixture"):
    return {"text": text, "sender": "2", "created_at": policy.time.time() * 1000}


@pytest.fixture
def keyword_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(policy.time, "time", lambda: 1790950000.0)
    return SimpleNamespace(
        config={
            "id": "keyword-fixture",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "permissions": {"5": ["mute", "recall"]},
                "mute_keywords": ["fixture"],
                "recall_enabled": True,
            },
        },
        account="1",
        members={"5": {"2": "22"}},
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "2", "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"}
                ],
            }
        ),
        diagnostics=SimpleNamespace(emit=Mock()),
        send_text=AsyncMock(return_value="accepted"),
        recall_violation=AsyncMock(return_value="accepted"),
        execute_moderation=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timing",
    [
        {},
        {"created_at": 0},
        {"created_at": True},
        {"created_at": "1790950000000"},
        {"created_at": float("nan")},
        {"created_at": float("inf")},
        {"created_at": 1790949699000},
        {"created_at": 1790950006000},
        {"created_at": 1790950000000, "recall_route": {"time": "1790949699000"}},
        {"created_at": 1790950000000, "recall_route": {"time": "bad"}},
        {"recall_route": {"time": True}},
        {"recall_route": {"time": []}},
        {"recall_route": []},
    ],
)
async def test_keyword_unknown_or_old_time_never_warns_or_punishes(
    keyword_adapter, tmp_path, timing
):
    adapter = keyword_adapter
    for mid in ["old-first", "old-second"]:
        assert await policy.handle(
            adapter, "5", mid, {"text": "fixture", "sender": "2", **timing}
        )
    adapter.get_moderation_members.assert_not_awaited()
    adapter.send_text.assert_not_awaited()
    adapter.recall_violation.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()
    assert not (tmp_path / "moderation.sqlite3").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["body", "envelope"])
async def test_keyword_valid_transport_time_still_warns_then_mutes(keyword_adapter, source):
    payload = fresh_payload()
    if source == "envelope":
        payload.update(created_at=0, recall_route={"time": "1790950000000"})
    for mid in ["first", "second"]:
        await policy.handle(keyword_adapter, "5", mid, payload)
    assert keyword_adapter.send_text.await_count == 1
    assert keyword_adapter.recall_violation.await_count == 2
    keyword_adapter.execute_moderation.assert_awaited_once_with(
        "message/5/second", "mute", 5, 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["incomplete", "disabled", "revoked"])
async def test_keyword_rechecks_directory_and_authorization_after_lookup(keyword_adapter, change):
    adapter = keyword_adapter
    roster = adapter.get_moderation_members.return_value

    async def changed(_group):
        if change == "incomplete":
            return {**roster, "complete": False}
        if change == "disabled":
            adapter.config["moderation"]["automation_enabled"] = False
        else:
            adapter.config["moderation"]["permissions"] = {}
        return roster

    adapter.get_moderation_members.side_effect = changed
    await policy.handle(adapter, "5", "first", fresh_payload())
    adapter.send_text.assert_not_awaited()
    adapter.recall_violation.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.parametrize(
    "value",
    [
        {"enabled": "yes"},
        {"superadmins": ["2"]},
        {"permissions": {"mute": "admin"}},
        {"permissions": {"5": [[]]}},
        {"permissions": {"5": ["mute", "mute"]}},
        {"levels": {}},
        {"cooldown_seconds": 0},
        {"keywords": [""]},
        {"semantic": {"enabled": "yes"}},
        {"semantic": {"timeout_seconds": 0}},
        {"semantic": {"context_limit": 51}},
        {"semantic": {"action": "kick"}},
        {"auto_kick": {"5": "yes"}},
        {"auto_kick": {"../5": True}},
    ],
)
def test_invalid_policy(value):
    with pytest.raises(ValueError):
        validate_policy(value)


def test_valid_semantic_and_automatic_kick_policy():
    validate_policy(
        {
            "semantic": {
                "enabled": True,
                "provider_id": "model",
                "timeout_seconds": 12,
                "context_limit": 20,
            },
            "auto_kick": {"5": True},
        }
    )


@pytest.mark.parametrize(
    "enabled,groups,grants,action,allowed",
    [
        (True, ["5"], {"5": ["mute"]}, "mute", True),
        (False, ["5"], {"5": ["mute"]}, "mute", False),
        (True, [], {"5": ["mute"]}, "mute", False),
        (True, ["5"], {"6": ["mute"]}, "mute", False),
        (True, ["5"], {"5": ["mute"]}, "announce", False),
        (True, ["5"], {"5": "mute"}, "mute", False),
    ],
)
def test_capability_gate(enabled, groups, grants, action, allowed):
    config = {
        "enabled_groups": groups,
        "moderation": {"enabled": enabled, "permissions": grants},
    }
    if allowed:
        authorize_action(config, "5", action)
    else:
        with pytest.raises(ProtocolError, match="moderation_permission"):
            authorize_action(config, "5", action)


@pytest.mark.asyncio
async def test_chat_command_never_executes():
    adapter = SimpleNamespace(
        config={"moderation": {"enabled": True}}, execute_moderation=AsyncMock()
    )
    assert not await policy.handle(adapter, "5", "m", {"text": "/群管 禁言 2"})
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grant,role,peer,expected",
    [
        (True, "GROUP_ROLE_MEMBER", "22", True),
        (False, "GROUP_ROLE_MEMBER", "22", False),
        (True, "GROUP_ROLE_ADMIN", "22", False),
        (True, "GROUP_ROLE_MEMBER", "wrong", False),
    ],
)
async def test_automation_requires_grant_identity_and_member(
    tmp_path, monkeypatch, grant, role, peer, expected
):
    monkeypatch.setattr(policy, "instance_dir", lambda _: tmp_path)
    adapter = SimpleNamespace(
        config={
            "id": "bot",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "permissions": {"5": ["mute"] if grant else []},
                "automation_enabled": True,
                "keywords": ["fixture"],
            },
        },
        account="1",
        members={"5": {"2": "22"}},
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [{"userId": 2, "nimId": peer, "groupRole": role}]
            }
        ),
        diagnostics=SimpleNamespace(emit=Mock()),
        execute_moderation=AsyncMock(),
        send_text=AsyncMock(return_value="accepted"),
    )
    assert await policy.handle(adapter, "5", "m", fresh_payload())
    assert adapter.send_text.called is expected
    assert not adapter.execute_moderation.called
    if expected:
        assert await policy.handle(
            adapter, "5", "next", fresh_payload()
        )
        assert adapter.execute_moderation.await_count == 1
        adapter.execute_moderation.assert_awaited_with("message/5/next", "mute", 5, 2)
        await policy.handle(adapter, "5", "next", fresh_payload())
        assert adapter.execute_moderation.await_count == 1
        monkeypatch.setattr(policy.time, "time", lambda: 9999999999)
        await policy.handle(adapter, "5", "new-day", fresh_payload())
        assert adapter.send_text.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("text,action", [("mute kick", "mute"), ("kick", "kick")])
async def test_warning_recall_then_penalty(tmp_path, monkeypatch, text, action):
    monkeypatch.setattr(policy, "instance_dir", lambda _: tmp_path)
    adapter = SimpleNamespace(
        config={
            "id": "bot",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "permissions": {"5": ["mute", "kick", "recall"]},
                "mute_keywords": ["mute"],
                "kick_keywords": ["kick"],
                "recall_enabled": True,
            },
        },
        account="1",
        members={"5": {"2": "22"}},
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "2", "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"}
                ]
            }
        ),
        recall_violation=AsyncMock(side_effect=TimeoutError),
        send_text=AsyncMock(return_value="accepted"),
        execute_moderation=AsyncMock(),
        diagnostics=SimpleNamespace(emit=Mock()),
    )
    for mid in ("first", "first", "second"):
        await policy.handle(adapter, "5", mid, fresh_payload(text))
    assert adapter.recall_violation.await_count == 2
    assert adapter.send_text.await_count == 1
    adapter.execute_moderation.assert_awaited_once_with(
        "message/5/second", action, 5, 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_result,warning_count,penalty_count",
    [("rejected", 2, 0), ("unknown", 1, 0), ("accepted", 1, 1)],
)
async def test_warning_delivery_state_controls_next_violation(
    tmp_path, monkeypatch, first_result, warning_count, penalty_count
):
    monkeypatch.setattr(policy, "instance_dir", lambda _: tmp_path)
    adapter = SimpleNamespace(
        config={
            "id": "bot",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "permissions": {"5": ["mute"]},
                "mute_keywords": ["bad"],
            },
        },
        account="1",
        members={"5": {"2": "22"}},
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "2", "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"}
                ]
            }
        ),
        diagnostics=SimpleNamespace(emit=Mock()),
        send_text=AsyncMock(side_effect=[first_result, "accepted"]),
        execute_moderation=AsyncMock(),
    )
    for mid in ("first", "first", "second"):
        await policy.handle(
            adapter, "5", mid, fresh_payload("bad /群管 not-a-command")
        )
    assert adapter.send_text.await_count == warning_count
    assert adapter.execute_moderation.await_count == penalty_count
