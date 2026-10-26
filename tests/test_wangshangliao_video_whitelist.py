"""Video-only recalls share the bounded native worker without warning escalation."""

import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    automatic_whitelist,
    member_exemptions,
    policy,
)
from astrbot.core.platform.sources.wangshangliao.group_scope import group_config
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import fresh_bot, ingest

bot = fresh_bot


def configure(bot):
    """Enable only the fixed video policy in one isolated group."""
    bot.config.update(
        _open_groups={
            "5": {
                "advertising": False,
                "flood": False,
                "keywords": [],
                "video_whitelist_only": True,
            },
            "6": {"advertising": False, "flood": False, "keywords": []},
        },
        _open_native_roles={"5": "GROUP_ROLE_ADMIN", "6": "GROUP_ROLE_ADMIN"},
        _open_revisions={"5": "video-5", "6": "video-6"},
    )
    bot.config["moderation"]["permissions"]["5"] = ["recall"]


@pytest.mark.asyncio
async def test_non_whitelisted_videos_are_recalled_once_without_warning_or_mute(bot):
    configure(bot)
    runtime = runtime_for(bot)
    try:
        for n in range(8):
            payload = await ingest(bot, str(100 + n), "", kind="video")
            assert payload["_ingress_content"]["category"] == "restricted_video"
        for _ in range(8):
            await runtime.step()
            await asyncio.gather(*runtime.workers.values())
        assert len(bot.calls) == 8 and all(c[0] == "recall" for c in bot.calls)
        bot.execute_moderation.assert_not_awaited()
        await policy.handle(bot, "5", "107", payload)
        assert len(bot.calls) == 8
        with closing(
            sqlite3.connect(bot.ledger.path.parent / "moderation.sqlite3")
        ) as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM content_violations WHERE category='restricted_video' AND status='recalled'"
                ).fetchone()[0]
                == 8
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "automatic"])
async def test_manual_and_yesterday_automatic_whitelists_can_send_video(
    bot, tmp_path, monkeypatch, source
):
    configure(bot)
    monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(automatic_whitelist, "instance_dir", lambda _: tmp_path)
    if source == "manual":
        member_exemptions.set_exemption(
            "fixture",
            "1",
            "5",
            "2",
            "22",
            enabled=True,
            actor="server/test",
            reason="Fixture",
            guard=lambda: None,
        )
    else:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with closing(sqlite3.connect(tmp_path / "automatic_exemptions.sqlite3")) as db:
            db.execute(
                "CREATE TABLE automatic_exemptions(account,group_id,member,peer,name,messages,day_start,valid_until,ranking_day)"
            )
            db.execute(
                "INSERT INTO automatic_exemptions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "1",
                    "5",
                    "2",
                    "22",
                    "Member",
                    101,
                    today.timestamp(),
                    time.time() + 90,
                    (today - timedelta(days=1)).strftime("%Y-%m-%d"),
                ),
            )
            db.commit()
    runtime = runtime_for(bot)
    try:
        payload = await ingest(bot, "100", "", kind="video")
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert not await policy.handle(bot, "5", "100", payload)
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "forward", None])
async def test_video_restriction_does_not_recall_other_content(bot, kind):
    configure(bot)
    runtime = runtime_for(bot)
    try:
        payload = await ingest(bot, "100", "hello" if kind is None else "", kind=kind)
        assert "_ingress_content" not in payload
        assert not await policy.handle(bot, "5", "100", payload)
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["admin", "managed", "disabled", "permission", "stale", "unknown"]
)
async def test_protected_changed_or_unknown_video_is_not_retried(
    bot, monkeypatch, change
):
    configure(bot)
    runtime = runtime_for(bot)
    try:
        payload = await ingest(bot, "100", "", kind="video")
        if change == "admin":
            bot.get_moderation_members.return_value["groupMemberInfo"][1][
                "groupRole"
            ] = "GROUP_ROLE_ADMIN"
        elif change == "managed":
            from astrbot.core.platform.sources.wangshangliao import event

            monkeypatch.setattr(event, "is_managed_account", lambda _: True)
        elif change == "disabled":
            bot.config["_open_groups"]["5"]["video_whitelist_only"] = False
        elif change == "permission":
            bot.config["moderation"]["permissions"]["5"] = []
        elif change == "stale":
            payload["recall_route"]["time"] = str(int((time.time() - 301) * 1000))
        else:
            bot.nim.request.side_effect = TimeoutError()
        await runtime.execute(("fixture", "1", "5", "2"), bot, "100", payload, None)
        await policy.handle(bot, "5", "100", payload)
        if change == "unknown":
            assert bot.nim.request.await_count == 1
        else:
            assert not bot.calls
        bot.execute_moderation.assert_not_awaited()
    finally:
        await runtime.close()


def test_video_switch_defaults_off_and_does_not_enable_another_group():
    config = {
        "_open_groups": {
            "5": {
                "advertising": False,
                "flood": False,
                "keywords": [],
                "video_whitelist_only": True,
            },
            "6": {"advertising": False, "flood": False, "keywords": []},
        }
    }
    assert group_config(config, "5")["moderation"]["automation_enabled"]
    assert not group_config(config, "6")["moderation"]["automation_enabled"]
    assert group_config(config, "6")["moderation"]["video_whitelist_only"] is False
