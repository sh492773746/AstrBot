"""Public top-twenty ranking names, identity boundaries and nonblocking rendering."""

import asyncio
import copy
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.builtin_stars.wangshangliao_moderation import rankings
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def ranking_case(tmp_path, monkeypatch):
    now = datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    monkeypatch.setattr(rankings.time, "time", lambda: now)
    monkeypatch.setattr(rankings, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(rankings, "is_managed_account", lambda _: False)
    monkeypatch.setattr(rankings, "flood_fixture", lambda *_: None)
    path = tmp_path / "messages.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE inbox(account,team,message,payload,state,received_at,pruned_at)"
        )
    adapter = SimpleNamespace(
        config={"id": "names", "enable": True, "enabled_groups": ["5"]},
        account="900000000001",
        nim_account="700000001",
        ledger=SimpleNamespace(path=path),
        members={"5": {}},
        member_snapshots={},
        group_directory_state={},
        member_refresh_failures={},
        member_refresh_locks={"5": asyncio.Lock()},
        refresh_member_mapping=AsyncMock(
            side_effect=AssertionError("Ranking must never refresh the directory")
        ),
        business=SimpleNamespace(
            request=AsyncMock(
                side_effect=AssertionError("Ranking must never issue HTTP requests")
            ),
            lock=asyncio.Lock(),
        ),
    )
    return SimpleNamespace(
        runtime=rankings.DailyRankings(), adapter=adapter, path=path, now=now
    )


def add_message(case, user="500000000101", name=None, *, age=100, text="Valid speech"):
    stamp = case.now - age
    peer = case.adapter.members["5"].setdefault(user, str(2000000 + len(user)))
    payload = {
        "sender": user,
        "name": name,
        "text": text,
        "created_at": stamp * 1000,
        "recall_route": {"time": stamp * 1000, "peer": peer},
    }
    with closing(sqlite3.connect(case.path)) as db, db:
        db.execute(
            "INSERT INTO inbox VALUES(?,?,?,?,?,?,NULL)",
            (
                case.adapter.account,
                "5",
                f"{user}/{age}/{text}",
                json.dumps(payload),
                "processed",
                stamp,
            ),
        )


def publish_roster(case, rows):
    case.adapter.member_snapshots["5"] = (
        case.adapter.account,
        time.monotonic(),
        {"complete": True, "groupMemberInfo": rows},
    )
    case.adapter.group_directory_state["5"] = {"complete": True, "error": ""}


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [1, 2, 99999])
@pytest.mark.parametrize("native_mentions", [False, True])
async def test_fixed_top_twenty_even_for_legacy_pages(
    ranking_case, page, native_mentions
):
    case = ranking_case
    rows = []
    for index in range(25):
        user, peer = str(500000000101 + index), str(2000000 + index)
        case.adapter.members["5"][user] = peer
        add_message(case, user, user)
        rows.append({"userId": user, "nimId": peer, "userNick": f"成员{index + 1}"})
    publish_roster(case, list(reversed(rows)))
    result = await case.runtime.read(
        case.adapter, "5", "ordinary-member", page=page, native_mentions=native_mentions
    )
    if native_mentions:
        assert isinstance(result, MessageChain)
        assert all(isinstance(part, Plain) for part in result.chain)
        result = "".join(part.text for part in result.chain)
    assert result == "今日发言排行\n\n" + "\n".join(
        f"{index}:成员{index}（有效发言1条）" for index in range(1, 21)
    )
    assert "成员21" not in result
    assert "页" not in result
    for row in rows:
        assert row["userId"] not in result
        assert row["nimId"] not in result
    case.adapter.refresh_member_mapping.assert_not_called()
    case.adapter.business.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        " \t\n\u200b ",
        123,
        {"name": "Not a name"},
        "500000000101",
        "00500000000101",
        "５００００００００１０１",
        "2000012",
    ],
)
async def test_missing_or_identity_fallback_is_explicit_not_an_index(
    ranking_case, name
):
    case = ranking_case
    add_message(case, name=name)
    result = await case.runtime.read(case.adapter, "5", "not-an-admin")
    assert result == "今日发言排行\n\n1:未获取昵称（有效发言1条）"
    assert "500000000101" not in result
    assert "2000012" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["message", "directory"])
@pytest.mark.parametrize("name", ["123", "１二3", "00042"])
async def test_real_numeric_names_are_not_replaced_with_rank_numbers(
    ranking_case, source, name
):
    case = ranking_case
    add_message(case, name=name if source == "message" else None)
    if source == "directory":
        publish_roster(
            case,
            [
                {
                    "userId": "500000000101",
                    "nimId": case.adapter.members["5"]["500000000101"],
                    "userNick": name,
                }
            ],
        )
    assert await case.runtime.read(case.adapter, "5", "") == (
        f"今日发言排行\n\n1:{name}（有效发言1条）"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["groupMemberNick", "nick", "groupNick", "cardName", "userNick", "userName"],
)
async def test_complete_roster_name_fields_replace_stale_recorded_names(
    ranking_case, field
):
    case = ranking_case
    add_message(case, name="旧昵称")
    publish_roster(
        case,
        [
            {
                "userId": "500000000101",
                "nimId": case.adapter.members["5"]["500000000101"],
                field: "\x00新\u200b昵称\n\u2028\u2029",
            }
        ],
    )
    assert await case.runtime.read(case.adapter, "5", "") == (
        "今日发言排行\n\n1:新昵称（有效发言1条）"
    )


@pytest.mark.asyncio
async def test_invalid_group_card_falls_back_to_verified_account_nickname(ranking_case):
    case = ranking_case
    add_message(case)
    publish_roster(
        case,
        [
            {
                "userId": "500000000101",
                "nimId": case.adapter.members["5"]["500000000101"],
                "groupMemberNick": "500000000101",
                "userNick": "账号昵称",
            }
        ],
    )
    assert await case.runtime.read(case.adapter, "5", "") == (
        "今日发言排行\n\n1:账号昵称（有效发言1条）"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "wrong_account",
        "wrong_group",
        "incomplete",
        "directory_failed",
        "refresh_failed",
        "wrong_peer",
        "missing_mapping",
        "duplicate",
        "malformed_snapshot",
        "malformed_roster",
        "missing_name",
    ],
)
async def test_directory_failure_and_unbound_records_never_leak_ids_or_guess_names(
    ranking_case, failure
):
    case = ranking_case
    user = "500000000101"
    add_message(case)
    row = {
        "userId": user,
        "nimId": case.adapter.members["5"][user],
        "groupMemberNick": "目录昵称",
    }
    publish_roster(case, [row, {"userId": "500000000102", "userNick": "其他成员"}])
    snapshot = case.adapter.member_snapshots["5"]
    if failure == "wrong_account":
        case.adapter.member_snapshots["5"] = ("other-account", *snapshot[1:])
    elif failure == "wrong_group":
        case.adapter.member_snapshots["6"] = case.adapter.member_snapshots.pop("5")
    elif failure == "incomplete":
        snapshot[2]["complete"] = False
    elif failure == "directory_failed":
        case.adapter.group_directory_state["5"] = {"complete": False}
    elif failure == "refresh_failed":
        case.adapter.member_refresh_failures["5"] = (case.adapter.account, "failed")
    elif failure == "wrong_peer":
        row["nimId"] = "other-peer"
    elif failure == "missing_mapping":
        case.adapter.members["5"].pop(user)
    elif failure == "duplicate":
        snapshot[2]["groupMemberInfo"].append({**row, "groupMemberNick": "冒名"})
    elif failure == "malformed_snapshot":
        case.adapter.member_snapshots["5"] = None
    elif failure == "malformed_roster":
        snapshot[2]["groupMemberInfo"] = None
    elif failure == "missing_name":
        row["groupMemberNick"] = ""
    result = await case.runtime.read(case.adapter, "5", "")
    assert result == "今日发言排行\n\n1:未获取昵称（有效发言1条）"
    case.adapter.refresh_member_mapping.assert_not_called()
    case.adapter.business.request.assert_not_called()


@pytest.mark.asyncio
async def test_directory_failure_preserves_authenticated_message_name(ranking_case):
    case = ranking_case
    add_message(case, name="消息中核实的昵称")
    case.adapter.group_directory_state["5"] = {"complete": False}
    assert await case.runtime.read(case.adapter, "5", "") == (
        "今日发言排行\n\n1:消息中核实的昵称（有效发言1条）"
    )


@pytest.mark.asyncio
async def test_identity_join_and_counts_are_independent_of_names(ranking_case):
    case = ranking_case
    first, second = "500000000101", "500000000102"
    case.adapter.members["5"] = {first: "2000001", second: "2000002"}
    add_message(case, first, "同名", age=400, text="First text")
    add_message(case, first, None, age=350, text="Second text")
    add_message(case, first, first, age=300, text="Third text")
    add_message(case, second, "同名", age=450)
    publish_roster(
        case,
        [
            {"userId": second, "nimId": "2000002", "groupMemberNick": "乙"},
            {"userId": first, "nimId": "2000001", "groupMemberNick": "甲"},
        ],
    )
    _, _, before = await case.runtime.snapshot(case.adapter, "5")
    assert before == [(first, "同名", 3), (second, "同名", 1)]
    source = case.path.read_bytes()
    original_mapping = copy.deepcopy(case.adapter.members)
    original_roster = copy.deepcopy(case.adapter.member_snapshots)
    assert await case.runtime.read(case.adapter, "5", "not-in-management") == (
        "今日发言排行\n\n1:甲（有效发言3条）\n2:乙（有效发言1条）"
    )
    assert (await case.runtime.snapshot(case.adapter, "5"))[2] == before
    assert case.path.read_bytes() == source
    assert case.adapter.members == original_mapping
    assert case.adapter.member_snapshots == original_roster
    original_roster["5"][2]["groupMemberInfo"][1]["groupMemberNick"] = "新名字"
    case.adapter.member_snapshots = original_roster
    assert "1:新名字（有效发言3条）" in await case.runtime.read(case.adapter, "5", "")


@pytest.mark.asyncio
async def test_blank_later_message_preserves_last_known_name(ranking_case):
    case = ranking_case
    add_message(case, name="已知昵称", age=400, text="First text")
    add_message(case, name=None, age=300, text="Second text")
    assert (await case.runtime.snapshot(case.adapter, "5"))[2] == [
        ("500000000101", "已知昵称", 2)
    ]
    assert "1:已知昵称（有效发言2条）" in await case.runtime.read(case.adapter, "5", "")


@pytest.mark.asyncio
async def test_bulk_enrichment_never_waits_for_http_or_moderation_refresh_locks(
    ranking_case,
):
    case = ranking_case

    class Roster(list):
        iterations = 0

        def __iter__(self):
            assert not case.runtime.lock.locked()
            self.iterations += 1
            return super().__iter__()

    rows = Roster(
        {"userId": str(600000000000 + index), "userNick": "Other"}
        for index in range(10000)
    )
    for index in range(25):
        user, peer = str(500000000101 + index), str(2000000 + index)
        case.adapter.members["5"][user] = peer
        add_message(case, user)
        rows.append({"userId": user, "nimId": peer, "userNick": f"成员{index}"})
    publish_roster(case, rows)
    await case.runtime.snapshot(case.adapter, "5")
    await case.adapter.business.lock.acquire()
    await case.adapter.member_refresh_locks["5"].acquire()
    try:
        started = time.perf_counter()
        result = await asyncio.wait_for(case.runtime.read(case.adapter, "5", ""), 0.25)
        assert time.perf_counter() - started < 0.25
    finally:
        case.adapter.business.lock.release()
        case.adapter.member_refresh_locks["5"].release()
    assert len(result.splitlines()[2:]) == 20
    assert rows.iterations == 1
    case.adapter.business.request.assert_not_called()
    case.adapter.refresh_member_mapping.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_mentions", [False, True])
async def test_empty_ranking_has_no_pagination_or_identity(
    ranking_case, native_mentions
):
    result = await ranking_case.runtime.read(
        ranking_case.adapter,
        "5",
        "500000000101",
        page=2,
        native_mentions=native_mentions,
    )
    if native_mentions:
        assert len(result.chain) == 1
        result = result.chain[0].text
    assert result == "今日发言排行\n\n暂无记录"


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [0, -1, True, "2", 100000])
async def test_invalid_legacy_page_is_rejected_before_storage(ranking_case, page):
    with pytest.raises(ProtocolError, match="ranking_page"):
        await ranking_case.runtime.read(ranking_case.adapter, "5", "", page=page)


@pytest.mark.asyncio
async def test_cached_board_still_rechecks_group_scope(ranking_case):
    case = ranking_case
    add_message(case, name="成员")
    await case.runtime.read(case.adapter, "5", "")
    case.adapter.config["enabled_groups"] = []
    with pytest.raises(ProtocolError, match="ranking_scope"):
        await case.runtime.read(case.adapter, "5", "")


@pytest.mark.asyncio
@pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")
@pytest.mark.parametrize(
    "text",
    [
        "排名",
        "排行",
        "今日排名",
        "今日排行",
        "排行榜",
        "发言排行",
        "发言排行榜",
        "/排名",
        "排名 2",
        "排行 2",
    ],
)
async def test_ordinary_member_aliases_keep_command_and_reply_auto_recall(
    monkeypatch, text
):
    from astrbot.builtin_stars.wangshangliao_moderation import main
    from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
    from astrbot.core.platform.sources.wangshangliao import reply_recall
    from tests.test_wangshangliao_plain_commands import native_event

    event = native_event(text)
    assert not event.is_admin()
    plugin = object.__new__(main.Main)
    plugin.commands = Commands()
    plugin.commands.rankings.snapshot = AsyncMock(
        return_value=("2026-11-07", 0, [("500000000101", "成员昵称", 7)])
    )
    plugin.commands.management = SimpleNamespace(
        allowed=Mock(side_effect=AssertionError("Public ranks need no management role"))
    )
    schedule = AsyncMock()
    monkeypatch.setattr(reply_recall, "schedule_command", schedule)
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", AsyncMock()
    )
    await plugin.moderate(event)
    assert event.is_stopped()
    schedule.assert_awaited_once_with(event)
    assert event.get_extra("wsl_ranking_result") is True
    call = event.platform.send_reply_text.await_args
    assert call.args[2] == "今日发言排行\n\n1:成员昵称（有效发言7条）"
    assert call.kwargs["auto_recall"] is True
    assert not call.kwargs.get("mentions")
    plugin.commands.management.allowed.assert_not_called()
    event.platform.execute_moderation.assert_not_awaited()


def test_ranking_capability_and_aliases_are_public():
    from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
        COMMAND_SPECS,
    )

    spec = COMMAND_SPECS["排名"]
    assert spec.role == "public"
    assert "group" in spec.locations
    assert {"排行", "今日排名", "今日排行", "排行榜", "发言排行", "发言排行榜"} <= set(
        spec.aliases
    )
