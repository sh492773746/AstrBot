"""Scoped two-day activity, recalls, midnight retention and incremental scans."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import speech_activity
from astrbot.builtin_stars.wangshangliao_moderation.speech_activity import (
    SpeechActivity,
    day_window,
)
from tests import test_wangshangliao_dashboard as dashboard_tests

PREFIX = dashboard_tests.PREFIX
headers = dashboard_tests.headers
page_app = dashboard_tests.page_app


@pytest.fixture
def activity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        speech_activity, "instance_dir", lambda instance: tmp_path / instance
    )
    monkeypatch.setattr(speech_activity, "is_managed_account", lambda uid: uid == "999")
    now = datetime(2026, 10, 26, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    root = tmp_path / "bot"
    root.mkdir()
    source = root / "messages.sqlite3"
    with closing(sqlite3.connect(source)) as db, db:
        db.executescript(
            """
            CREATE TABLE inbox(account,team,message,payload,state,received_at);
            CREATE INDEX inbox_ranking_window ON inbox(account,team,received_at);
            CREATE TABLE observed_recalls(account,group_id,message_id,received_at);
            CREATE TABLE outbox(key PRIMARY KEY,state);
            """
        )
    runtime = SpeechActivity(SimpleNamespace())
    return runtime, source, now, tmp_path


def insert(
    source,
    now,
    mid,
    *,
    account="1",
    group="5",
    uid="10",
    text="正常聊天",
    kind=None,
    state="processed",
    received=None,
):
    payload = {
        "sender": uid,
        "name": f"成员{uid}",
        "text": text,
        "created_at": now * 1000,
        "recall_route": {"time": now * 1000},
    }
    if kind:
        payload["kind"] = kind
    with closing(sqlite3.connect(source)) as db, db:
        db.execute(
            "INSERT INTO inbox VALUES(?,?,?,?,?,?)",
            (
                account,
                group,
                mid,
                json.dumps(payload),
                state,
                received if received is not None else now,
            ),
        )


def test_group_media_member_search_dedup_and_scope(activity):
    runtime, source, now, _ = activity
    for index, kind in enumerate((None, "image", "video", "forward")):
        insert(source, now - 100 + index, str(index), kind=kind)
    insert(source, now - 50, "another-member", uid="11")
    insert(source, now - 50, "other-group", group="6")
    insert(source, now - 50, "other-account", account="2")
    assert not runtime.collect("bot", "1", "5", source, now)
    first = runtime.read("bot", "1", "5", now=now, query="成员")
    assert first["days"][0]["total"] == 5
    assert first["days"][0]["active_members"] == 2
    assert first["days"][0]["kinds"] == {
        "text": 2,
        "image": 1,
        "video": 1,
        "forward": 1,
    }
    assert sum(first["days"][0]["hours"]) == 5
    assert {row["id"] for row in first["members"]} == {"10", "11"}
    runtime.collect("bot", "1", "5", source, now + 1)
    selected = runtime.read("bot", "1", "5", now=now, member="10")
    assert selected["days"][0]["total"] == 5
    assert selected["days"][0]["member"]["count"] == 4
    assert sum(selected["days"][0]["hours"]) == 4
    assert runtime.read("bot", "2", "5", now=now)["days"][0]["total"] == 0
    assert runtime.read("bot", "1", "6", now=now)["members"] == []
    assert (
        runtime.read("bot", "1", "5", now=now, member="other")["selected_member"]
        is None
    )
    zero = runtime.read(
        "bot", "1", "5", now=now, names={"12": "无发言成员"}, query="无发言"
    )
    assert zero["members"] == [{"id": "12", "name": "无发言成员"}]


@pytest.mark.parametrize(
    "state",
    [
        "unsupported",
        "archived",
        "self",
        "departed",
        "ignored_bot",
        "application_notice",
    ],
)
def test_non_dispatchable_evidence_is_never_counted(activity, state):
    runtime, source, now, _ = activity
    insert(source, now - 100, state, state=state)
    runtime.collect("bot", "1", "5", source, now)
    assert runtime.read("bot", "1", "5", now=now)["days"][0]["total"] == 0


@pytest.mark.parametrize(
    "uid,text,kind",
    [
        ("1", "机器人", None),
        ("999", "另一个机器人", None),
        ("10", "黑名单", None),
        ("10", "@成员 白名单", None),
        ("10", "/chat", None),
        ("10", "", None),
        ("10", "other", "unknown"),
        ("invalid", "invalid", None),
    ],
)
def test_bots_commands_empty_and_unsupported_never_count(activity, uid, text, kind):
    runtime, source, now, _ = activity
    insert(source, now - 100, "excluded", uid=uid, text=text, kind=kind)
    runtime.collect("bot", "1", "5", source, now)
    assert runtime.read("bot", "1", "5", now=now)["days"][0]["total"] == 0


def test_recall_reconciliation_after_counting(activity):
    runtime, source, now, _ = activity
    for mid in ("native", "submitted", "violation", "flood", "manual", "safe"):
        insert(source, now - 100, mid)
    runtime.collect("bot", "1", "5", source, now)
    assert runtime.read("bot", "1", "5", now=now)["days"][0]["total"] == 6
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("INSERT INTO observed_recalls VALUES('1','5','native',?)", (now,))
        db.execute("INSERT INTO outbox VALUES('recall/1/5/submitted','unknown')")
        db.execute("INSERT INTO outbox VALUES('recall/1/6/safe','accepted')")
    with closing(sqlite3.connect(source.parent / "moderation.sqlite3")) as db, db:
        db.executescript(
            """
            CREATE TABLE content_violations(account,group_id,message,observed,status);
            CREATE TABLE flood_batches(operation,account,group_id,last_seen);
            CREATE TABLE flood_recalls(batch,message,status);
            CREATE TABLE moderation_action_audit(account,group_id,message,action,status,created);
            """
        )
        db.execute(
            "INSERT INTO content_violations VALUES('1','5','violation',?,'warned')",
            (now,),
        )
        db.execute("INSERT INTO flood_batches VALUES('b','1','5',?)", (now,))
        db.execute("INSERT INTO flood_recalls VALUES('b','flood','accepted')")
        db.execute(
            "INSERT INTO moderation_action_audit VALUES('1','5','manual','recall','accepted',?)",
            (now,),
        )
    SpeechActivity(SimpleNamespace()).collect("bot", "1", "5", source, now + 1)
    assert runtime.read("bot", "1", "5", now=now)["days"][0]["total"] == 1


def test_midnight_reset_retention_downtime_and_no_message_bodies(activity):
    runtime, source, now, _ = activity
    yesterday, today, tomorrow = day_window(now)
    for stamp in (yesterday - 1, yesterday + 1, today + 1, now - 100):
        insert(source, stamp, str(stamp))
    runtime.collect("bot", "1", "5", source, now)
    snapshot = runtime.read("bot", "1", "5", now=now)
    assert [day["total"] for day in snapshot["days"]] == [2, 1]
    midnight = runtime.read("bot", "1", "5", now=tomorrow)
    assert [day["total"] for day in midnight["days"]] == [0, 2]
    assert [day["date"] for day in midnight["days"]] == ["2026-10-27", "2026-10-26"]
    future = runtime.read("bot", "1", "5", now=tomorrow + 86400 * 4)
    assert [day["total"] for day in future["days"]] == [0, 0]
    with closing(runtime.database("bot")) as db:
        assert db.execute("SELECT count(*) FROM speech_messages").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM speech_cursors").fetchone()[0] == 0
    assert (
        b"\\u6b63\\u5e38\\u804a\\u5929"
        not in (source.parent / "speech_activity.sqlite3").read_bytes()
    )
    with closing(sqlite3.connect(source)) as db:
        assert db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 4


def test_settlement_cursor_batches_ties_and_restarts(activity, monkeypatch):
    runtime, source, now, _ = activity
    monkeypatch.setattr(speech_activity, "BATCH_SIZE", 2)
    for index in range(5):
        insert(source, now - 100, str(index))
    insert(source, now - 10, "unsettled")
    assert runtime.collect("bot", "1", "5", source, now)
    assert runtime.read("bot", "1", "5", now=now)["catching_up"]
    assert SpeechActivity(SimpleNamespace()).collect("bot", "1", "5", source, now)
    assert not runtime.collect("bot", "1", "5", source, now)
    assert runtime.read("bot", "1", "5", now=now)["days"][0]["total"] == 5
    runtime.collect("bot", "1", "5", source, now + 31)
    assert runtime.read("bot", "1", "5", now=now + 31)["days"][0]["total"] == 6


def test_cleanup_inactive_bot_and_only_statistics(activity, monkeypatch):
    runtime, source, now, root = activity
    insert(source, now - 100, "normal")
    runtime.collect("bot", "1", "5", source, now)
    data_root = root / "data"
    target = data_root / "platform_data" / "wangshangliao" / "removed"
    target.mkdir(parents=True)
    (source.parent / "speech_activity.sqlite3").rename(
        target / "speech_activity.sqlite3"
    )
    monkeypatch.setattr(speech_activity, "get_astrbot_data_path", lambda: data_root)
    monkeypatch.setattr(speech_activity.time, "time", lambda: now + 86400 * 4)
    runtime.prune()
    with closing(sqlite3.connect(target / "speech_activity.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM speech_messages").fetchone()[0] == 0
    assert source.is_file()


@pytest.mark.asyncio
async def test_real_native_ledger_schema(activity):
    from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger

    runtime, source, now, _ = activity
    native = source.parent / "native.sqlite3"
    ledger = Ledger(native)
    ReviewStore(native.parent / "moderation.sqlite3").initialize()
    await ledger.open()
    try:
        await ledger.ingest(
            "1",
            "5",
            "native-real",
            {
                "sender": "10",
                "name": "原生成员",
                "text": "有效文字",
                "created_at": (now - 100) * 1000,
            },
        )
        await ledger.db.execute("UPDATE inbox SET received_at=?", (now - 100,))
        await ledger.db.commit()
        runtime.collect("bot", "1", "5", native, now)
        assert runtime.read("bot", "1", "5", now=now)["days"][0]["total"] == 1
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_dashboard_auth_scope_and_exact_directory_member(
    page_app,
    headers,
    monkeypatch,
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard

    app, page, service, configs, root = page_app
    monkeypatch.setattr(speech_activity, "instance_dir", lambda _: root)
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.members = {"5": {"10": "9010"}, "6": {"11": "9011"}}
    adapter.member_snapshots = {
        "5": (
            "1",
            0,
            {
                "complete": True,
                "groupMemberInfo": [{"userId": "10", "userNick": "当前群成员"}],
            },
        ),
    }
    page.plugin = SimpleNamespace(speech_activity=SpeechActivity(page.context))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        route = f"{PREFIX}/speech-activity"
        target = {"bot_id": "fixture", "group_id": "5"}
        assert (await client.get(route, params=target)).status_code == 401
        assert (
            await client.get(route, params=target, headers={"X-API-Key": "plugin-only"})
        ).status_code == 403
        for changes in (
            {"group_id": "99"},
            {"bot_id": "other"},
            {"member_id": "invalid"},
            {"query": "a" * 65},
        ):
            assert (
                await client.get(route, params=target | changes, headers=headers)
            ).status_code == 400
        result = await client.get(
            route, params=target | {"query": "当前"}, headers=headers
        )
        assert result.status_code == 200
        assert result.json()["data"]["members"] == [{"id": "10", "name": "当前群成员"}]
        selected = await client.get(
            route, params=target | {"member_id": "10"}, headers=headers
        )
        assert selected.json()["data"]["selected_member"]["id"] == "10"
        other = await client.get(
            route, params=target | {"member_id": "11"}, headers=headers
        )
        assert other.json()["data"]["selected_member"] is None

        async def deny():
            raise dashboard.ApiError("插件未启用", status_code=403)

        page.authorize = AsyncMock(side_effect=deny)
        assert (
            await client.get(route, params=target, headers=headers)
        ).status_code == 403
        service.update_bot.assert_not_awaited()


def test_departure_clears_only_its_statistics(activity):
    from astrbot.builtin_stars.wangshangliao_moderation.lifecycle import (
        clear_working_data,
    )

    runtime, source, now, _ = activity
    insert(source, now - 100, "normal")
    runtime.collect("bot", "1", "5", source, now)
    # Exercise only the new store; the minimal source fixture is not a native ledger.
    source.unlink()
    with closing(runtime.database("bot")) as db, db:
        db.execute(
            "INSERT INTO speech_messages SELECT account,'6',message,member,name,day,stamp,kind FROM speech_messages"
        )
    clear_working_data(source.parent, "1", "5", "55", lambda: None)
    assert runtime.read("bot", "1", "5", now=now)["days"][0]["total"] == 0
    assert runtime.read("bot", "1", "6", now=now)["days"][0]["total"] == 1


@pytest.mark.asyncio
async def test_dashboard_statistics_wait_does_not_block_or_leak_changed_scope(
    page_app,
    headers,
    monkeypatch,
):
    app, page, _service, configs, root = page_app
    monkeypatch.setattr(speech_activity, "instance_dir", lambda _: root)
    runtime = SpeechActivity(page.context)
    runtime.database("fixture").close()
    page.plugin = SimpleNamespace(speech_activity=runtime)
    blocker = sqlite3.connect(root / "speech_activity.sqlite3")
    blocker.execute("BEGIN EXCLUSIVE")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        work = asyncio.create_task(
            client.get(
                f"{PREFIX}/speech-activity",
                params={"bot_id": "fixture", "group_id": "5"},
                headers=headers,
            )
        )
        try:
            await asyncio.sleep(0.05)
            assert not work.done()
            configs["fixture"]["account_id"] = "changed"
            blocker.rollback()
            response = await asyncio.wait_for(work, 3)
            assert response.status_code == 409
        finally:
            blocker.rollback()
            blocker.close()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)


def report_definition():
    return {
        "account": "1",
        "groups": [{"id": "5", "name": "群甲"}, {"id": "6", "name": "群乙"}],
        "members": [{"id": "10", "name": "成员甲"}, {"id": "11", "name": "成员乙"}],
    }


def test_focused_report_exact_ids_text_only_two_days_and_totals(activity):
    runtime, source, now, _ = activity
    for mid, extra in (
        ("normal", {}),
        ("image", {"kind": "image"}),
        ("other-member", {"uid": "12"}),
        ("other-group", {"group": "7"}),
        ("other-account", {"account": "2"}),
        ("same-id-other-group", {"group": "6"}),
        ("same-name-different-id", {"uid": "11", "group": "6"}),
        ("recalled", {}),
    ):
        insert(source, now - 100, mid, **extra)
    insert(source, now - 86400, "yesterday", group="6")
    insert(source, now - 2 * 86400, "expired")
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("INSERT INTO observed_recalls VALUES('1','5','recalled',?)", (now,))
    for group in ("5", "6", "7"):
        runtime.collect("bot", "1", group, source, now)
    report = runtime.report("bot", "1", report_definition(), now=now)
    assert report["catching_up"] is False
    assert report["days"][0]["members"] == [
        {"id": "10", "name": "成员甲", "counts": [1, 1], "total": 2},
        {"id": "11", "name": "成员乙", "counts": [0, 1], "total": 1},
    ]
    assert report["days"][0]["totals"] == [1, 2]
    assert report["days"][0]["total"] == 3
    assert report["days"][1]["total"] == 1
    assert (
        runtime.report("bot", "2", report_definition(), now=now)["days"][0]["total"]
        == 0
    )
    assert (
        runtime.report("bot", "1", report_definition(), now=now + 86400)["days"][1][
            "total"
        ]
        == 3
    )


@pytest.mark.parametrize(
    "change", ["account", "duplicate", "invalid_id", "controls", "oversized", "symlink"]
)
def test_focused_report_definition_fails_closed(activity, change):
    runtime, _source, _now, root = activity
    path = root / "bot/speech_report.json"
    config = report_definition()
    if change == "account":
        config["account"] = "other"
    elif change == "duplicate":
        config["members"].append(config["members"][0])
    elif change == "invalid_id":
        config["groups"][0]["id"] = "5); DROP TABLE speech_messages"
    elif change == "controls":
        config["members"][0]["name"] = "hidden\u202e"
    path.write_text(json.dumps(config))
    if change == "oversized":
        path.write_text(" " * 16385)
    elif change == "symlink":
        real = path.with_name("other.json")
        path.rename(real)
        path.symlink_to(real)
    with pytest.raises(ValueError):
        runtime.report_config("bot", "1")


def test_focused_report_missing_cursors_are_incomplete_not_confirmed_zero(activity):
    runtime, _source, now, _ = activity
    assert (
        runtime.report("bot", "1", report_definition(), now=now)["catching_up"] is True
    )
    assert runtime.report_config("bot", "1") is None


@pytest.mark.asyncio
async def test_focused_report_dashboard_auth_all_scopes_and_post_read_revocation(
    page_app, headers, monkeypatch
):
    app, page, _service, configs, root = page_app
    monkeypatch.setattr(speech_activity, "instance_dir", lambda _: root)
    runtime = SpeechActivity(page.context)
    page.plugin = SimpleNamespace(speech_activity=runtime)
    path = root / "speech_report.json"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        route = f"{PREFIX}/speech-report"
        target = {"bot_id": "fixture"}
        assert (await client.get(route, params=target)).status_code == 401
        assert (
            await client.get(route, params=target, headers={"X-API-Key": "plugin-only"})
        ).status_code == 403
        assert (await client.get(route, params=target, headers=headers)).json()[
            "data"
        ] == {"configured": False}
        path.write_text(json.dumps(report_definition()))
        configs["fixture"]["enabled_groups"].remove("6")
        assert (
            await client.get(route, params=target, headers=headers)
        ).status_code == 409
        configs["fixture"]["enabled_groups"].append("6")
        result = await client.get(route, params=target, headers=headers)
        assert result.status_code == 200
        assert result.json()["data"]["configured"] is True
        assert result.json()["data"]["catching_up"] is True

        original = runtime.report

        def revoked(*args, **kwargs):
            result = original(*args, **kwargs)
            configs["fixture"]["enabled_groups"].remove("6")
            return result

        monkeypatch.setattr(runtime, "report", revoked)
        assert (
            await client.get(route, params=target, headers=headers)
        ).status_code == 409
