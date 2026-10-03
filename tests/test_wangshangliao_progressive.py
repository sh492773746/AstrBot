"""Progressive paired sanctions, deduplication and independent failure evidence."""

import asyncio
import json
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import content_rules, policy
from astrbot.core.platform.sources.wangshangliao import automatic, progressive
from astrbot.core.platform.sources.wangshangliao.policy import validate_policy
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def bot(tmp_path, monkeypatch):
    for module in (automatic, content_rules):
        monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "fixture",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "progressive_mute": True,
                "recall_enabled": True,
                "content_rules_since": time.time() - 60,
                "permissions": {"5": ["mute", "recall", "kick"]},
                "auto_kick": {"5": True},
                "mute_keywords": ["badword"],
                "kick_keywords": ["severeword"],
            },
        },
        members={"5": {"2": "22"}},
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "2", "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"},
                ],
            }
        ),
        diagnostics=SimpleNamespace(emit=Mock()),
        recall_violation=AsyncMock(return_value="accepted"),
        send_text=AsyncMock(),
    )

    async def execute(operation, action, group, member, *, minutes=1):
        if action == "kick":
            automatic.authorize_kick(adapter, operation, str(group), str(member))
        receipt = {
            "action": action,
            "group": group,
            "member": member,
            "minutes": minutes,
            "status": "accepted",
            "operation": operation,
        }
        with closing(automatic.database(adapter)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
            )
            db.execute(
                "INSERT INTO operations VALUES(?,'fixture',?)",
                (operation, json.dumps(receipt)),
            )
        return receipt

    adapter.execute_moderation = AsyncMock(side_effect=execute)
    return adapter


def payload(text="badword"):
    return {
        "sender": "2",
        "text": text,
        "recall_route": {"time": str(int(time.time() * 1000))},
    }


@pytest.mark.asyncio
async def test_logs_keep_recall_and_mute_receipts_separate(bot):
    bot.recall_violation.return_value = "unknown"
    result = await progressive.enforce(bot, "5", "2", "receipt-log")
    assert result["status"] == "accepted"
    logs = [call for call in bot.diagnostics.emit.call_args_list if call.args[0] == "progressive_result"]
    assert [(call.kwargs["action"], call.args[1]) for call in logs] == [("recall", "unknown"), ("mute", "accepted")]
    assert all(call.args[2] == "receipt-log" for call in logs)
    assert logs[0].kwargs["failed"] is True
    assert logs[1].kwargs["failed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["badword", "severeword", "加我vx:abc123456"])
async def test_first_three_violations_mute_and_recall_then_fourth_kicks(bot, text):
    for mid in ("1", "1", "2", "3"):
        assert await policy.handle(bot, "5", mid, payload(text))
    calls = bot.execute_moderation.await_args_list
    assert [c.args[1] for c in calls] == ["mute", "mute", "mute"]
    assert [c.kwargs["minutes"] for c in calls[:3]] == [5, 15, 60]
    assert bot.recall_violation.await_count == 3
    assert await policy.handle(bot, "5", "3", payload(text))
    assert bot.execute_moderation.await_count == 3
    assert await policy.handle(bot, "5", "4", payload(text))
    assert [c.args[1] for c in bot.execute_moderation.await_args_list] == ["mute", "mute", "mute", "kick"]
    assert bot.recall_violation.await_count == 4
    bot.send_text.assert_not_awaited()
    with closing(automatic.database(bot)) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM progressive_actions WHERE recall_status='accepted'"
            ).fetchone()[0]
            == 4
        )


@pytest.mark.asyncio
async def test_semantic_violation_without_keyword_uses_same_progression(bot):
    for mid in ("1", "2"):
        assert await policy.handle(
            bot,
            "5",
            mid,
            payload("当前消息的语境证据"),
            assessment={
                "decision": "violation",
                "category": "external_promotion",
                "message_id": mid,
            },
        )
    assert [c.kwargs["minutes"] for c in bot.execute_moderation.await_args_list] == [
        5,
        15,
    ]
    assert bot.recall_violation.await_count == 2


@pytest.mark.asyncio
async def test_no_kick_opt_in_caps_at_sixty_minutes_and_restart_preserves_count(bot):
    bot.config["moderation"]["auto_kick"] = {}
    for mid in ("1", "2", "3", "4"):
        await progressive.enforce(bot, "5", "2", mid)
        bot = SimpleNamespace(**vars(bot))
    assert [c.kwargs["minutes"] for c in bot.execute_moderation.await_args_list] == [
        5,
        15,
        60,
        60,
    ]
    assert all(c.args[1] == "mute" for c in bot.execute_moderation.await_args_list)


@pytest.mark.asyncio
async def test_manual_only_caps_mutes_even_with_stale_auto_kick_enabled(bot):
    bot.config["moderation"]["manual_kick_only"] = True
    for mid in ("1", "2", "3", "4", "5"):
        await progressive.enforce(bot, "5", "2", mid)
    calls = bot.execute_moderation.await_args_list
    assert [c.args[1] for c in calls] == ["mute"] * 5
    assert [c.kwargs["minutes"] for c in calls] == [5, 15, 60, 60, 60]
    assert bot.recall_violation.await_count == 5
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        automatic.authorize_kick(bot, "automatic-kick/old", "5", "2")


@pytest.mark.asyncio
async def test_duplicate_parallel_submissions_are_once_only(bot):
    results = await asyncio.gather(
        *[progressive.enforce(bot, "5", "2", "1") for _ in range(8)]
    )
    assert all(r["status"] == "accepted" for r in results)
    assert bot.recall_violation.await_count == bot.execute_moderation.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recall", ["rejected", "unknown", "exception"])
async def test_recall_failure_does_not_skip_mute_or_claim_full_success(bot, recall):
    if recall == "exception":
        bot.recall_violation.side_effect = TimeoutError
    else:
        bot.recall_violation.return_value = recall
    result = await progressive.enforce(bot, "5", "2", "1")
    assert result["status"] == "accepted"
    assert result["recall_status"] == ("unknown" if recall == "exception" else recall)
    assert bot.execute_moderation.await_args.kwargs["minutes"] == 5
    with closing(automatic.database(bot)) as db:
        row = db.execute(
            "SELECT recall_status,result FROM progressive_actions"
        ).fetchone()
    assert row[0] == result["recall_status"]
    assert json.loads(row[1])["recall_status"] == row[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "unknown", "exception"])
async def test_failed_mutes_never_increment_or_skip_recall(bot, failure):
    original = bot.execute_moderation.side_effect
    bot.execute_moderation.side_effect = (
        TimeoutError if failure == "exception" else None
    )
    bot.execute_moderation.return_value = {"status": failure}
    result = await progressive.enforce(bot, "5", "2", "1")
    assert result["recall_status"] == "accepted"
    bot.execute_moderation.side_effect = original
    result = await progressive.enforce(bot, "5", "2", "2")
    assert bot.recall_violation.await_count == 2
    if failure == "rejected":
        assert result["mute_count"] == 1
        assert bot.execute_moderation.await_args.kwargs["minutes"] == 5
    else:
        assert result["status"] == "unknown"
        assert bot.execute_moderation.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["automation", "recall", "grants", "peer", "account"]
)
async def test_revoke_during_recall_stops_mute(bot, change):
    async def recall(*_):
        if change == "automation":
            bot.config["moderation"]["automation_enabled"] = False
        elif change == "recall":
            bot.config["moderation"]["recall_enabled"] = False
        elif change == "grants":
            bot.config["moderation"]["permissions"] = {}
        elif change == "peer":
            bot.members["5"]["2"] = "other"
        else:
            bot.account = "another"
        return "accepted"

    bot.recall_violation.side_effect = recall
    with pytest.raises(ProtocolError):
        await progressive.enforce(bot, "5", "2", "1")
    bot.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["admin", "incomplete", "stale", "unknown"])
async def test_protected_or_unverified_message_never_sanctioned(bot, case):
    data = payload()
    if case == "admin":
        bot.get_moderation_members.return_value["groupMemberInfo"][0]["groupRole"] = (
            "GROUP_ROLE_ADMIN"
        )
    elif case == "incomplete":
        bot.get_moderation_members.return_value["complete"] = False
    elif case == "stale":
        data["recall_route"]["time"] = str(int((time.time() - 600) * 1000))
    else:
        data["recall_route"]["time"] = "0"
    await policy.handle(bot, "5", "1", data)
    bot.execute_moderation.assert_not_awaited()
    bot.recall_violation.assert_not_awaited()


@pytest.mark.parametrize(
    "text,category",
    [
        ("BADWORD", "mute_keyword"),
        ("ｂａｄｗｏｒｄ", "mute_keyword"),
        ("severeword", "kick_keyword"),
        ("badword severeword", "mute_keyword"),
        ("举报 badword", ""),
        ("引用 severeword", ""),
        ("“badword”", ""),
        ("你好", ""),
    ],
)
def test_keyword_literal_normalization_and_reporting_exemptions(bot, text, category):
    assert content_rules.keyword_category(text, bot.config["moderation"]) == category


def test_progressive_mode_requires_recall_and_preserves_separate_kick_grant():
    validate_policy({"progressive_mute": True, "recall_enabled": True})
    for config in [
        {"progressive_mute": True},
        {"progressive_mute": "yes", "recall_enabled": True},
    ]:
        with pytest.raises(ValueError):
            validate_policy(config)
