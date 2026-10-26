import asyncio
import importlib.util
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from test_lajiaoliao_businesses import prepared_plugin, settings

from data.plugins.astrbot_plugin_lajiaoliao import business as business_module
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def live_tool(name="native_acceptance_live.py"):
    path = (
        Path(__file__).resolve().parents[1]
        / "services/lajiaoliao-bridge" / name
    )
    spec = importlib.util.spec_from_file_location("native_acceptance_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_robot_workflow_profile_is_private_and_has_no_transport_credentials():
    tool = live_tool("business_acceptance_live.py")
    defaults = {
        "dashboard": {"jwt_secret": "synthetic-secret"},
        "platform": [{"session_ref": "synthetic-session"}],
        "provider": [{"api_key": "synthetic-key"}],
        "provider_sources": [{"api_key": "synthetic-key"}],
        "provider_settings": {"enable": True},
        "platform_settings": {"enable_id_white_list": True},
        "admins_id": ["existing-admin"],
    }
    profile = tool.isolated_profile(defaults)
    assert profile["dashboard"] == {}
    assert all(profile[key] == [] for key in ("platform", "provider", "provider_sources"))
    assert profile["admins_id"] == [tool.MEMBER_UID]
    assert profile["plugin_set"] == ["astrbot_plugin_lajiaoliao"]
    assert profile["provider_settings"]["enable"] is False
    assert defaults["admins_id"] == ["existing-admin"]
    assert defaults["provider_settings"]["enable"] is True
    assert all(":FriendMessage:private:" in route for route in tool.ROUTES)


def test_robot_workflow_clock_rollover_and_route_shadowing_fail_closed():
    tool = live_tool("business_acceptance_live.py")
    now = datetime(2026, 10, 9, 23, 59, 40, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    clock, due = tool.next_test_clock(now)
    assert clock == "00:01" and due - now == 80
    tool.verify_private_route_priority({})
    tool.verify_private_route_priority({
        "telegram::": "unrelated", tool.ROUTES[0]: "old-profile",
    })
    with pytest.raises(RuntimeError, match="route_shadowed"):
        tool.verify_private_route_priority({"::": "global"})
    with pytest.raises(RuntimeError, match="route_shadowed"):
        tool.verify_private_route_priority({tool.ACTOR + "::": "wide-profile"})


@pytest.mark.parametrize("failure", ["", "mute_unknown", "state", "unmute_unknown"])
def test_all_members_tool_restores_once_and_never_retries_unknown(failure):
    calls = []
    states = iter([{"group_status": 0 if failure == "state" else 3},
                   {"group_status": 0}])

    def execute(body, duplicate=False):
        calls.append((body["action"], duplicate))
        if failure == body["action"].removesuffix("_all") + "_unknown":
            raise RuntimeError("unknown")
        return {}

    if failure == "mute_unknown":
        states = iter([{"group_status": 0}])
    def operation(action):
        return {"action": action, "operation_id": action}

    def state():
        return next(states)
    if failure:
        with pytest.raises(RuntimeError):
            live_tool().accept_all_members(operation, execute, state, Mock())
    else:
        live_tool().accept_all_members(operation, execute, state, Mock())
    assert calls == [("mute_all", True), ("unmute_all", True)]


def event(config, uid, *, private=False, text="sample", mid="sample", stamp=None):
    return SimpleNamespace(
        platform=SimpleNamespace(session_ref=config["session_ref"], config=config,
                                 notifications=[]),
        is_private_chat=lambda: private,
        is_admin=lambda: uid == "admin",
        get_sender_id=lambda: uid,
        get_extra=lambda _: None,
        get_group_id=lambda: "" if private else "g",
        unified_msg_origin="private" if private else "group",
        message_obj=SimpleNamespace(
            message_id=mid, message_str=text,
            timestamp=time.time() if stamp is None else stamp,
            sender=SimpleNamespace(nickname="Name " + uid),
        ),
    )


def test_ranking_and_lottery_command_workflow_with_bot_exclusion(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(
            ref, "save_settings", "g", 0,
            settings(rankings_enabled=True, lottery_enabled=True), "admin",
        )
        admin = event(config, "admin", private=True)
        member = event(config, "member")
        bot_uid = plugin.store._read(ref)["account_id"]
        bot = event(config, bot_uid)
        now = time.time()
        for uid, mid, stamp, text in (
            ("member", "first", now - 90, "First real sample"),
            ("member", "second", now - 45, "Another real sample"),
            (bot_uid, "bot-excluded", now - 10, "Bot sample"),
        ):
            await plugin.commands.observe(event(
                config, uid, mid=mid, stamp=stamp, text=text,
            ), "")
        await plugin.commands.observe(event(
            config, "member", mid="second", stamp=now - 45,
            text="Another real sample",
        ), "")
        ranks = await plugin.business.db(ref, "ranking", "g")
        assert [(row["member_id"], row["count"]) for row in ranks] == [("member", 2)]
        assert not await plugin.business.db(ref, "seen", "g", "bot-excluded")
        await plugin.business.db(ref, "revoked", "g", ["second"])
        assert "1条" in await plugin.commands.execute(member, "排名")
        plugin.commands.directories[(ref, "admin", "private")] = {
            "selected": "g", "expires": now + 600,
        }
        plugin.business.roster = AsyncMock(return_value={
            "member": {"userID": "member", "groupID": "g", "roleLevel": 20,
                       "nickname": "Member"},
            bot_uid: {"userID": bot_uid, "groupID": "g", "roleLevel": 100},
        })
        with pytest.raises(SessionError, match="manual_group_actions_removed"):
            await plugin.commands.execute(admin, "开启抽奖")
        _, state = await plugin.business.start_lottery(ref, "g", "admin", "private")
        assert state == "open"
        member.message_obj.timestamp = time.time()
        assert await plugin.commands.execute(member, "参加抽奖") == "报名成功"
        assert "已报名" in await plugin.commands.execute(member, "参加抽奖")
        assert await plugin.commands.execute(bot, "参加抽奖") == "机器人不能参加抽奖"
        activity = (await plugin.business.db(ref, "activities", "g"))[0]["id"]
        assert await plugin.business.draw_lottery(ref, "g", activity) == "finished"
        rows = await plugin.business.db(ref, "activities", "g")
        assert rows[0]["enrolled"] == 1
        assert rows[0]["winners"] == [{"member_id": "member", "name": "Member"}]
        assert instance.send_parts.await_count == 2
        with pytest.raises(SessionError, match="lottery_not_open"):
            await plugin.business.draw_lottery(ref, "g", activity)
        assert instance.send_parts.await_count == 2
        assert not await plugin.business.db(ref, "activities", "other")
        await plugin.business.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["sent", "unknown", "rejected"])
def test_daily_notification_preview_confirm_poll_receipt_and_cleanup(
    tmp_path, monkeypatch, outcome
):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(
            ref, "save_settings", "g", 0, settings(reminders_enabled=True), "admin",
        )
        clock = [int(time.time()) // 60 * 60]
        monkeypatch.setattr(business_module, "time", SimpleNamespace(
            time=lambda: clock[0], monotonic=time.monotonic,
        ))
        admin = event(config, "admin", private=True)
        plugin.commands.directories[(ref, "admin", "private")] = {
            "selected": "g", "expires": time.time() + 600,
        }
        scheduled = datetime.fromtimestamp(clock[0] + 60, ZoneInfo("Asia/Shanghai"))
        argument = scheduled.strftime("%H:%M") + " Synthetic scheduled notice"
        with pytest.raises(SessionError, match="manual_group_actions_removed"):
            await plugin.commands.execute(admin, "定时通知", argument)
        identifier = await plugin.business.db(ref, "preview_reminder", "g", "admin", "private",
            scheduled.strftime("%H:%M"), "Synthetic scheduled notice", 1)
        instance.send_parts.assert_not_awaited()
        assert not await plugin.business.db(ref, "reminders", "g")
        await plugin.business.db(ref, "confirm_reminder", "g", "admin", "private", identifier, 1)
        with pytest.raises(SessionError, match="schedule_preview_invalid"):
            await plugin.business.db(ref, "confirm_reminder", "g", "admin", "private", identifier, 1)
        job = (await plugin.business.db(ref, "reminders", "g"))[0]
        assert job["next_run"] == clock[0] + 60
        clock[0] = job["next_run"]
        delivered = asyncio.Event()

        async def send(*args):
            delivered.set()
            return {"status": outcome, "message_id": "synthetic-receipt"}

        instance.send_parts.side_effect = send
        plugin.business.poller = asyncio.create_task(plugin.business.poll())
        try:
            await asyncio.wait_for(delivered.wait(), timeout=5)
            deadline = time.monotonic() + 2
            while True:
                current = (await plugin.business.db(ref, "reminders", "g"))[0]
                if current["state"] != "sending":
                    break
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            assert current["state"] == {
                "sent": "active", "unknown": "unknown", "rejected": "paused",
            }[outcome]
            assert current["next_run"] == clock[0] + 86400
            await asyncio.sleep(2.1)
            assert instance.send_parts.await_count == 1
            assert not await plugin.business.db(ref, "reminders", "other")
            if outcome == "sent":
                assert "已暂停" in await plugin.commands.execute(
                    admin, "暂停定时", identifier,
                )
                assert "已删除" in await plugin.commands.execute(
                    admin, "删除定时", identifier,
                )
                assert not await plugin.business.db(ref, "reminders", "g")
            elif outcome == "unknown":
                with pytest.raises(SessionError, match="schedule_result_requires_review"):
                    await plugin.commands.execute(admin, "删除定时", identifier)
        finally:
            await plugin.business.close()

    asyncio.run(run())
