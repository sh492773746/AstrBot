"""Offline tests for business rules and private human kick confirmation."""

import json
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import commands
from astrbot.builtin_stars.wangshangliao_moderation import content_rules as rules
from astrbot.core.platform.sources.wangshangliao import automatic


@pytest.mark.parametrize(
    "text,expected",
    [
        ("加我vx:abc123456", "external_promotion"),
        ("加我ＶＸ：ａｂｃ１２３４５６", "external_promotion"),
        ("加我v\u200bx:abc123456", "external_promotion"),
        ("首次充值送30% example.vip", "external_promotion"),
        ("3p大秀直播 至尊群:12345678", "adult_promotion"),
        ("https://example.vip", ""),
        ("12345678", ""),
        ("微信怎么使用", ""),
        ("举报骗子发的 加我vx:abc123456", ""),
        ("复制链接去浏览器", ""),
    ],
)
def test_classification(text, expected):
    assert rules.classify(text) == expected


def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(commands, "instance_dir", lambda _: tmp_path)
    member = {"userId": "2", "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"}
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "bot",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "content_rules_since": time.time() - 60,
                "permissions": {"5": ["mute", "recall", "kick"]},
            },
        },
        members={"5": {"2": "22"}},
        get_moderation_members=AsyncMock(
            return_value={"complete": True, "groupMemberInfo": [member]}
        ),
        recall_violation=AsyncMock(return_value="accepted"),
        send_text=AsyncMock(return_value="accepted"),
        execute_moderation=AsyncMock(return_value={"status": "accepted"}),
        diagnostics=SimpleNamespace(emit=Mock()),
    )
    return adapter, member


@pytest.mark.asyncio
async def test_warn_repeat_mute_and_duplicate(tmp_path, monkeypatch):
    adapter, _ = fixture(tmp_path, monkeypatch)
    payload = {
        "text": "加我vx:abc123456",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    for mid in ("1", "1", "2", "2"):
        assert await rules.enforce(adapter, "5", mid, payload)
    assert adapter.send_text.await_count == 1
    assert adapter.recall_violation.await_count == 2
    adapter.execute_moderation.assert_awaited_once_with("content/5/2", "mute", 5, 2)


@pytest.mark.asyncio
async def test_full_semantic_sanction_path_warns_then_three_mutes_then_new_violation_kicks(
    tmp_path, monkeypatch
):
    adapter, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(automatic, "instance_dir", lambda _: tmp_path)
    adapter.config["moderation"]["auto_kick"] = {"5": True}

    async def execute(operation, action, group, member):
        if action == "kick":
            automatic.authorize_kick(adapter, operation, str(group), str(member))
        receipt = {
            "operation": operation,
            "action": action,
            "group": group,
            "member": member,
            "status": "accepted",
        }
        with closing(automatic.database(adapter)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
            )
            db.execute(
                "INSERT INTO operations VALUES(?, 'fixture', ?)",
                (operation, json.dumps(receipt)),
            )
        return receipt

    adapter.execute_moderation.side_effect = execute
    payload = {
        "text": "这里还有首次充值福利",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    assert rules.classify(payload["text"]) == ""
    for mid in ("1", "1", "2", "3", "4", "4", "5"):
        await rules.enforce(
            adapter,
            "5",
            mid,
            payload,
            assessment={
                "decision": "violation",
                "category": "external_promotion",
                "message_id": mid,
            },
        )
    assert adapter.send_text.await_count == 1
    assert [call.args[1] for call in adapter.execute_moderation.await_args_list] == [
        "mute",
        "mute",
        "mute",
        "kick",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["admin", "owner", "old", "no_time", "no_grant", "incomplete", "wrong_peer"]
)
async def test_no_sanctions_on_unsafe_evidence(tmp_path, monkeypatch, case):
    adapter, member = fixture(tmp_path, monkeypatch)
    payload = {
        "text": "加我vx:abc123456",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    if case in {"admin", "owner"}:
        member["groupRole"] = "GROUP_ROLE_" + case.upper()
    elif case == "old":
        payload["recall_route"]["time"] = "1000000000000"
    elif case == "no_time":
        payload["recall_route"] = {}
    elif case == "no_grant":
        adapter.config["moderation"]["permissions"] = {}
    elif case == "incomplete":
        adapter.get_moderation_members.return_value["complete"] = False
    else:
        member["nimId"] = "wrong"
    await rules.enforce(adapter, "5", "1", payload)
    adapter.recall_violation.assert_not_called()
    adapter.send_text.assert_not_called()
    adapter.execute_moderation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_only", [False, True])
async def test_private_confirmation_bound_single_use(tmp_path, monkeypatch, manual_only):
    adapter, member = fixture(tmp_path, monkeypatch)
    if manual_only:
        adapter.config["moderation"]["manual_kick_only"] = True
        adapter.config["moderation"].pop("content_rules_since")
    event = SimpleNamespace(
        platform=adapter,
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        get_sender_id=lambda: "9",
        unified_msg_origin="bot:private:9",
        get_group_id=lambda: "",
        message_obj=SimpleNamespace(message_id="preview"),
    )
    service = commands.Commands()
    service.selections[(id(adapter), "1", "9", event.unified_msg_origin)] = {
        "expires": time.monotonic() + 600,
        "group": "5",
        "members": [member],
    }
    if manual_only:
        assert await service.execute(event, "踢出", "1", ai=True) == {
            "status": "rejected", "reason": "manual_kick_only",
        }
    preview = await service.run(event, "踢出 1")
    token = preview.rsplit(" ", 1)[-1]
    adapter.execute_moderation.assert_not_called()
    assert "无效" in await service.run(event, "确认踢出 " + token)
    event.message_obj.message_id = "confirm"
    event.get_sender_id = lambda: "8"
    assert "无效" in await service.run(event, "确认踢出 " + token)
    event.get_sender_id = lambda: "9"
    assert "accepted" in await service.run(event, "确认踢出 " + token)
    assert "无效" in await service.run(event, "确认踢出 " + token)
    adapter.execute_moderation.assert_awaited_once()
