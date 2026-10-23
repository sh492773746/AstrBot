import asyncio
import copy
import json
import time
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_businesses import prepared_plugin, settings
from test_lajiaoliao_group_acceptance import event

from data.plugins.astrbot_plugin_lajiaoliao.business import (
    BusinessStore,
    validate_settings,
)
from data.plugins.astrbot_plugin_lajiaoliao.lottery import result_gif, result_text
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def notify_settings():
    value = settings(lottery_enabled=True)
    value["lottery"].update(notify_results=True, winners=1)
    return value


def opened_store(tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, notify_settings(), "admin")
    identifier, _ = store.prepare_activity("g", "admin", "umo", 1)
    store.activity_receipt(identifier, "opening", {"status": "sent"})
    return store, identifier


def test_old_settings_default_off_strict_fields_and_bounded_notification_size():
    old = settings()
    del old["lottery"]["notify_results"]
    assert validate_settings(old)["lottery"]["notify_results"] is False
    assert "notify_results" not in old["lottery"]
    for field, value in [("notify_results", 1), ("capacity", 0), ("capacity", 101)]:
        bad = notify_settings()
        bad["lottery"][field] = value
        with pytest.raises(ValueError):
            validate_settings(bad)
    del old["lottery"]["contact"]
    with pytest.raises(ValueError):
        validate_settings(old)


def test_signup_keeps_first_id_filters_draw_and_freezes_results(tmp_path):
    store, identifier = opened_store(tmp_path)
    store.enroll("g", "u1", "One", time.time(), "original-1")
    assert store.enroll("g", "u1", "One", time.time(), "later-1") == "already_enrolled"
    store.enroll("g", "u2", "Two", time.time(), "original-2")
    winners, count, _ = store.draw(identifier, {"u1"})
    assert count == 1 and winners[0]["member_id"] == "u1"
    rows = store.notification_rows("g", identifier)
    assert len(rows) == 2 and rows[0]["message_id"] == "original-1"
    assert rows[0]["text"] == "您的奖品是 Test prize"
    assert not store.notification_rows("other", identifier)
    assert not BusinessStore(tmp_path / "other").notification_rows("g", identifier)
    with pytest.raises(SessionError):
        store.draw(identifier, {"u2"})
    assert store.activities("g")[0]["winners"] == winners
    changed = copy.deepcopy(notify_settings())
    changed["lottery"]["notify_results"] = False
    with pytest.raises(SessionError, match="frozen"):
        store.save_settings("g", 1, changed, "admin")


def test_reservations_order_unknown_restart_and_sanitized_receipts(tmp_path):
    store, identifier = opened_store(tmp_path)
    store.enroll("g", "u", "One", time.time(), "source")
    store.draw(identifier, {"u"})
    assert not store.reserve_notification("g", identifier, "u", "gif")
    with pytest.raises(SessionError):
        store.reserve_notification("other", identifier, "u", "quote")
    assert store.reserve_notification("g", identifier, "u", "quote")
    assert not store.reserve_notification("g", identifier, "u", "quote")
    store.finish_notification(
        "g",
        identifier,
        "u",
        "quote",
        {"status": "sent", "message_id": "result", "error": "https://secret"},
    )
    assert store.reserve_notification("g", identifier, "u", "gif")
    restored = BusinessStore(tmp_path)
    assert restored.activities("g")[0]["state"] == "unknown"
    assert restored.notification_rows("g", identifier)[1]["state"] == "unknown"
    assert "secret" not in restored.notification_rows("g", identifier)[0]["receipt"]
    with pytest.raises(SessionError):
        restored.reserve_notification("g", identifier, "u", "gif")


def test_missing_signup_evidence_never_fabricates_quote(tmp_path):
    store, identifier = opened_store(tmp_path)
    store.enroll("g", "legacy", "Legacy", time.time())
    store.draw(identifier, {"legacy"})
    rows = store.notification_rows("g", identifier)
    assert [row["state"] for row in rows] == ["rejected", "skipped"]
    assert not store.reserve_notification("g", identifier, "legacy", "quote")
    assert not store.reserve_notification("g", identifier, "legacy", "gif")


def test_ai_cannot_enable_result_automation(tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, settings(), "admin")
    proposal = settings()
    proposal["lottery"]["notify_results"] = True
    draft = store.preview_settings("g", "admin", "private", proposal, 1)
    with pytest.raises(SessionError, match="dashboard_automation_grants_required"):
        store.confirm_settings("g", "admin", "private", draft)
    assert store.settings("g")["settings"]["lottery"]["notify_results"] is False


def test_command_passes_original_signup_id_and_keeps_bot_exclusion(
    tmp_path, monkeypatch
):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        business, ref = plugin.business, config["session_ref"]
        await business.db(ref, "save_settings", "g", 0, notify_settings(), "admin")
        bot = plugin.store._read(ref)["account_id"]
        business.roster = AsyncMock(
            return_value={
                "u": {"roleLevel": 20},
                bot: {"roleLevel": 20},
            }
        )
        identifier, _ = await business.start_lottery(ref, "g", "admin", "private")
        assert (
            await plugin.commands.execute(
                event(config, bot, text="参加抽奖", mid="bot-source"), "参加抽奖"
            )
            == "机器人不能参加抽奖"
        )
        assert (
            await plugin.commands.execute(
                event(config, "u", text="参加抽奖", mid="first-source"), "参加抽奖"
            )
            == "报名成功"
        )
        assert (
            await plugin.commands.execute(
                event(config, "u", text="参加抽奖", mid="duplicate-source"), "参加抽奖"
            )
            == "你已报名，请勿重复提交"
        )
        await business.db(ref, "draw", identifier, {"u", bot})
        rows = await business.db(ref, "notification_rows", "g", identifier)
        assert len(rows) == 2 and rows[0]["message_id"] == "first-source"
        assert (await business.db(ref, "activities", "g"))[0]["enrolled"] == 1

    asyncio.run(run())


async def prepare_runtime(tmp_path, monkeypatch):
    plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
    business, ref = plugin.business, config["session_ref"]
    await business.db(ref, "save_settings", "g", 0, notify_settings(), "admin")
    business.roster = AsyncMock(
        return_value={"u1": {"roleLevel": 20}, "u2": {"roleLevel": 20}}
    )
    identifier, _ = await business.start_lottery(ref, "g", "admin", "private")
    for uid in ("u1", "u2"):
        await business.db(ref, "enroll", "g", uid, uid, time.time(), "signup-" + uid)
    instance.send_parts.reset_mock()
    return business, ref, instance, identifier


def test_runtime_quote_result_then_gif_for_each_member_and_summary(
    tmp_path, monkeypatch
):
    async def run():
        business, ref, instance, identifier = await prepare_runtime(
            tmp_path, monkeypatch
        )
        assert await business.draw_lottery(ref, "g", identifier) == "finished"
        row = (await business.db(ref, "activities", "g"))[0]
        winner = row["winners"][0]["member_id"]
        calls = instance.send_parts.await_args_list
        assert len(calls) == 5
        for index, member in enumerate(("u1", "u2")):
            quote, gif = calls[2 * index].args, calls[2 * index + 1].args
            assert quote == (
                "g",
                "signup-" + member,
                [{"type": "text", "text": result_text("Test prize", member == winner)}],
                "group",
            )
            assert gif == ("g", "", [result_gif()], "group")
        assert (
            calls[-1].args[1] == "" and "【抽奖结果】" in calls[-1].args[2][0]["text"]
        )
        assert row["notifications"] == {"sent": 4}
        with pytest.raises(SessionError):
            await business.draw_lottery(ref, "g", identifier)
        assert instance.send_parts.await_count == 5

    asyncio.run(run())


@pytest.mark.parametrize("status", ["unknown", "rejected", "unsupported", "failed"])
def test_quote_failure_skips_only_its_gif_and_preserves_draw(
    tmp_path, monkeypatch, status
):
    async def run():
        business, ref, instance, identifier = await prepare_runtime(
            tmp_path, monkeypatch
        )
        receipts = [{"status": status}] + [{"status": "sent", "message_id": "ok"}] * 3
        instance.send_parts.side_effect = receipts
        assert await business.draw_lottery(ref, "g", identifier) == (
            "unknown" if status == "unknown" else "failed"
        )
        assert instance.send_parts.await_count == 4
        assert instance.send_parts.await_args_list[1].args[1] == "signup-u2"
        row = (await business.db(ref, "activities", "g"))[0]
        assert len(row["winners"]) == 1 and row["notifications"]["skipped"] == 1
        assert json.loads(row["receipt"])["status"] == (
            "unknown" if status == "unknown" else "failed"
        )

    asyncio.run(run())


def test_unknown_gif_is_not_retried(tmp_path, monkeypatch):
    async def run():
        business, ref, instance, identifier = await prepare_runtime(
            tmp_path, monkeypatch
        )
        instance.send_parts.side_effect = [
            {"status": "sent"},
            TimeoutError("never-expose-this"),
            {"status": "sent"},
            {"status": "sent"},
            {"status": "sent"},
        ]
        assert await business.draw_lottery(ref, "g", identifier) == "unknown"
        assert instance.send_parts.await_count == 5
        rows = await business.db(ref, "notification_rows", "g", identifier)
        assert rows[1]["state"] == "unknown"
        assert "never-expose" not in rows[1]["receipt"]
        with pytest.raises(SessionError):
            await business.draw_lottery(ref, "g", identifier)
        assert instance.send_parts.await_count == 5

    asyncio.run(run())


def test_summary_rejection_cannot_hide_unknown_individual_result(tmp_path, monkeypatch):
    async def run():
        business, ref, instance, identifier = await prepare_runtime(
            tmp_path, monkeypatch
        )
        instance.send_parts.side_effect = [
            {"status": "unknown"},
            {"status": "sent"},
            {"status": "sent"},
            {"status": "rejected"},
        ]
        assert await business.draw_lottery(ref, "g", identifier) == "unknown"
        row = (await business.db(ref, "activities", "g"))[0]
        receipt = json.loads(row["receipt"])
        assert receipt["summary_status"] == "rejected"
        assert receipt["notifications"]["unknown"] == 1
        with pytest.raises(SessionError, match="already_active"):
            await business.start_lottery(ref, "g", "admin", "private")

    asyncio.run(run())


@pytest.mark.parametrize("change", ["authority", "policy", "settings"])
def test_revocation_between_quote_and_gif_stops_next_sends(
    tmp_path, monkeypatch, change
):
    async def run():
        business, ref, instance, identifier = await prepare_runtime(
            tmp_path, monkeypatch
        )

        async def send(*args):
            if change == "authority":
                business.task_authorized = AsyncMock(return_value=False)
            elif change == "policy":
                instance.config["message_policy"]["group_ids"] = []
            else:
                old = await business.db(ref, "settings", "g")
                value = old["settings"]
                value["lottery_enabled"] = False
                await business.db(
                    ref, "save_settings", "g", old["version"], value, "admin"
                )
            return {"status": "sent"}

        instance.send_parts.side_effect = send
        assert await business.draw_lottery(ref, "g", identifier) == "failed"
        assert instance.send_parts.await_count == 1
        assert (await business.db(ref, "activities", "g"))[0]["notifications"][
            "sent"
        ] == 1

    asyncio.run(run())
