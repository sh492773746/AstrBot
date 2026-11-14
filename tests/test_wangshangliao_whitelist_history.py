"""Bounded two-day whitelist history reads without a duplicate content archive."""

import json
import sqlite3
import time
from contextlib import closing

import pytest
import pytest_asyncio
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import (
    member_exemptions,
    speech_activity,
)
from astrbot.builtin_stars.wangshangliao_moderation.member_whitelist import REQUEST
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.speech_activity import (
    SpeechActivity,
)
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_speech_activity import activity as activity
from tests.test_wangshangliao_speech_activity import insert


@pytest_asyncio.fixture
async def history_rpc(rpc_scope, tmp_path, monkeypatch):
    """Provide the real RPC and history reader with isolated native ledgers.

    Args:
        rpc_scope: Existing authenticated two-group service fixture.
        tmp_path: Test-owned storage root.
        monkeypatch: Scoped module dependencies.

    Returns:
        The isolated service, adapter and RPC dispatcher.
    """
    value, adapter, rows = rpc_scope
    root = tmp_path / "history"
    root.mkdir()
    monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: root)
    monkeypatch.setattr(speech_activity, "instance_dir", lambda _: root)
    monkeypatch.setattr(speech_activity, "is_managed_account", lambda uid: False)
    rows[2]["userNick"] = "中性成员甲"
    rows.append(
        {
            "userId": "3",
            "nimId": "1003",
            "groupRole": "GROUP_ROLE_MEMBER",
            "userNick": "中性成员乙",
        }
    )
    adapter.groups = {"5": "1005", "6": "1006"}
    adapter.members["5"]["3"] = "1003"
    for uid in ("2", "3"):
        member_exemptions.set_exemption(
            adapter.config["id"],
            "99",
            "5",
            uid,
            "100" + uid,
            enabled=True,
            actor="server/test",
            reason="History fixture",
            guard=lambda: None,
        )
    now = time.time()
    for uid in ("2", "3"):
        for index in range(105):
            stamp = now - 1000 + index
            payload = {
                "sender": uid,
                "name": "中性测试",
                "text": f"中性聊天 {index + 1}",
                "created_at": stamp * 1000,
                "recall_route": {"time": stamp * 1000},
            }
            await adapter.ledger.db.execute(
                "INSERT INTO inbox(account,team,message,payload,state,received_at,sender) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    "99",
                    "5",
                    f"{uid}-{index}",
                    json.dumps(payload),
                    "processed",
                    stamp,
                    uid,
                ),
            )
    await adapter.ledger.db.commit()
    value.plugin.speech_activity = SpeechActivity(value.context)
    return value, adapter, OpenRPC(value)


@pytest.mark.asyncio
async def test_real_rpc_history_is_read_only_and_rejects_a_non_whitelist_member(
    history_rpc,
):
    value, adapter, rpc = history_rpc
    version = value.store.tenant("99", "5")["version"]
    payload = {
        "account": "99",
        "group_id": "5",
        "tenant_version": version,
        "operation": "history",
        "list_version": 2,
        "member_ids": ["2"],
    }
    result = await rpc.dispatch(
        OpenCall(
            action="member_whitelist",
            actor="dashboard/offline",
            payload=payload,
        )
    )
    assert result["members"][0]["name"] == "中性成员甲"
    assert result["members"][0]["count"] == 105
    assert len(result["members"][0]["messages"]) == 100
    assert result["members"][0]["messages"][0]["text"] == "中性聊天 6"
    assert result["catching_up"] is False
    for change in ({"member_ids": ["1"]}, {"group_id": "6"}):
        with pytest.raises(OpenError, match="member_whitelist_changed"):
            await rpc.dispatch(
                OpenCall(
                    action="member_whitelist",
                    actor="dashboard/offline",
                    payload={**payload, **change},
                )
            )
    assert value.store.tenant("99", "5")["version"] == version
    with closing(sqlite3.connect(adapter.ledger.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_latest_100_are_chronological_and_effective_text_only(activity):
    runtime, source, now, _ = activity
    for index in range(110):
        insert(source, now - 1000 + index, str(index), text=f"正常对话 {index}")
    insert(source, now - 50, "picture", kind="image", text="图片")
    insert(source, now - 50, "command", text="黑名单")
    insert(source, now - 50, "robot", uid="999")
    insert(source, now - 50, "elsewhere", group="6", text="其他群内容")
    insert(source, now - 50, "another-account", account="2")
    insert(source, now - 10, "unsettled", text="尚未核对")
    insert(source, now - 50, "other-member", uid="11", text="其他人内容")
    with closing(sqlite3.connect(source)) as db, db:
        db.execute(
            "INSERT INTO observed_recalls VALUES(?,?,?,?)", ("1", "5", "109", now)
        )
    result = runtime.history(
        "bot", "1", "5", source, [{"id": "10", "name": "当前姓名"}], now=now
    )
    row = result["members"][0]
    assert row["count"] == 109
    assert row["matched_count"] == 109
    assert len(row["messages"]) == 100
    assert row["messages"][0]["text"] == "正常对话 9"
    assert row["messages"][-1]["text"] == "正常对话 108"
    assert row["name"] == "当前姓名"
    assert result["catching_up"] is False
    assert result["from_date"] == "2026-10-25"
    assert result["to_date"] == "2026-10-26"
    with closing(runtime.database("bot")) as db:
        assert not any(
            row[1] in {"text", "payload", "content"}
            for row in db.execute("PRAGMA table_info(speech_messages)")
        )
    with closing(sqlite3.connect(source)) as db:
        assert (
            json.loads(
                db.execute("SELECT payload FROM inbox WHERE message='109'").fetchone()[
                    0
                ]
            )["text"]
            == "正常对话 109"
        )


def test_late_recall_is_reconciled_before_a_second_export(activity):
    runtime, source, now, _ = activity
    insert(source, now - 100, "one", text="有效聊天")
    members = [{"id": "10", "name": "成员"}]
    assert (
        runtime.history("bot", "1", "5", source, members, now=now)["members"][0][
            "count"
        ]
        == 1
    )
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("INSERT INTO outbox VALUES(?,?)", ("recall/1/5/one", "unknown"))
    after = runtime.history("bot", "1", "5", source, members, now=now + 1)
    assert after["members"][0]["count"] == 0
    assert after["members"][0]["messages"] == []


@pytest.mark.parametrize(
    "period,expected",
    [
        ("today", ["today"]),
        ("yesterday", ["yesterday"]),
        ("recent", ["yesterday", "today"]),
    ],
)
def test_beijing_day_and_retention_boundaries(activity, period, expected):
    runtime, source, now, _ = activity
    insert(source, now - 86400, "yesterday", text="昨日交流")
    insert(source, now - 100, "today", text="今日交流")
    insert(source, now - 2 * 86400, "expired", text="过期内容")
    result = runtime.history(
        "bot", "1", "5", source, [{"id": "10", "name": "成员"}], period=period, now=now
    )
    assert [row["message"] for row in result["members"][0]["messages"]] == expected


def test_content_query_is_literal_and_count_remains_total_effective_text(activity):
    runtime, source, now, _ = activity
    insert(source, now - 200, "literal", text="有 %_ 内容 交流")
    insert(source, now - 100, "other", text="其他有效内容")
    result = runtime.history(
        "bot", "1", "5", source, [{"id": "10", "name": "成员"}], query="%_", now=now
    )
    row = result["members"][0]
    assert row["count"] == 2
    assert row["matched_count"] == 1
    assert row["messages"][0]["message"] == "literal"
    assert (
        runtime.history(
            "bot",
            "1",
            "5",
            source,
            [{"id": "10", "name": "成员"}],
            query="' OR 1=1--",
            now=now,
        )["members"][0]["messages"]
        == []
    )


def test_unavailable_original_is_not_fabricated_and_controls_are_removed(activity):
    runtime, source, now, _ = activity
    insert(source, now - 200, "gone", text="原文")
    insert(source, now - 100, "valid", text="第一行\n第二行\u202e\x00")
    runtime.collect("bot", "1", "5", source, now)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("DELETE FROM inbox WHERE message='gone'")
    result = runtime.history(
        "bot", "1", "5", source, [{"id": "10", "name": "成员"}], now=now
    )
    row = result["members"][0]
    assert row["count"] == 2
    assert row["unavailable"] == 1
    assert len(row["messages"]) == 1
    assert row["messages"][0]["text"] == "第一行\n第二行"


def test_duplicate_text_messages_remain_distinct_and_zero_members_are_visible(activity):
    runtime, source, now, _ = activity
    insert(source, now - 100, "one", text="一样")
    insert(source, now - 90, "two", text="一样")
    members = [{"id": "11", "name": "无发言"}, {"id": "10", "name": "有发言"}]
    result = runtime.history("bot", "1", "5", source, members, now=now)
    assert [row["id"] for row in result["members"]] == ["11", "10"]
    assert result["members"][0]["count"] == 0
    assert result["members"][1]["count"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"member_ids": []},
        {"member_ids": ["2"] * 6},
        {"member_ids": ["../secret"]},
        {"member_ids": [2]},
        {"period": "last_month"},
        {"query": "x" * 65},
        {"list_version": -1},
        {"limit": 10000},
    ],
)
def test_history_request_cannot_expand_scope_or_retention(change):
    payload = {
        "account": "1",
        "group_id": "5",
        "tenant_version": 1,
        "operation": "history",
        "member_ids": ["10"],
        "list_version": 0,
    }
    with pytest.raises(ValidationError):
        REQUEST.validate_python({**payload, **change})
