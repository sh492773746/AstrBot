import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_instance_management import provision
from test_lajiaoliao_plugin_api import call, make_plugin

from astrbot.core.platform.instance_control import instance_revision
from data.plugins.astrbot_plugin_lajiaoliao.business import (
    DEFAULTS,
    Businesses,
    BusinessStore,
    next_daily,
    validate_settings,
)
from data.plugins.astrbot_plugin_lajiaoliao.commands import Commands, recognize
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def settings(**flags):
    value = copy.deepcopy(DEFAULTS)
    value.update(flags)
    value["lottery"].update(prize="Test prize", contact="Test contact")
    return value


def test_store_permissions_default_off_and_optimistic_versions(tmp_path):
    store = BusinessStore(tmp_path)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.settings("one") == {"version": 0, "settings": DEFAULTS}
    store.save_settings("one", 0, settings(rankings_enabled=True), "owner")
    with pytest.raises(SessionError, match="business_changed"):
        store.save_settings("one", 0, settings(), "owner")
    assert store.settings("two")["version"] == 0
    assert store.settings("one")["settings"]["rankings_enabled"]
    assert BusinessStore(tmp_path).settings("one")["version"] == 1
    assert len(store.audit("one")) == 1 and not store.audit("two")


def test_rankings_freshness_throttle_same_text_isolation_and_recall(tmp_path):
    store = BusinessStore(tmp_path)
    now = time.time()
    for mid, member, stamp, text, allowed, review in [
        ("m1", "u1", now - 100, "Hello", True, ""),
        ("m1", "u1", now - 100, "Hello", True, ""),
        ("m2", "u1", now - 90, "Other", True, ""),
        ("m3", "u1", now - 60, " hello ", True, ""),
        ("m4", "u1", now - 20, "new", True, ""),
        ("m5", "u2", now - 15, "keyword", True, "review_only"),
        ("m6", "u2", now - 600, "history", True, ""),
        ("m7", "u2", now + 30, "future", True, ""),
    ]:
        store.observe("one", mid, member, member, stamp, text, allowed, review)
    assert store.ranking("one")[0]["count"] == 2
    assert not store.ranking("two")
    store.revoked("one", ["m4"])
    assert store.ranking("one")[0]["count"] == 1
    assert any(row["status"] == "review_only" for row in store.audit("one"))
    with pytest.raises(ValueError):
        store.ranking("one", True)


def test_lottery_freeze_deduplication_fixed_draw_and_unknown_restart(tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings("one", 0, settings(lottery_enabled=True), "admin")
    identifier, params = store.prepare_activity("one", "admin", "umo", 1)
    assert store.activities("one")[0]["state"] == "opening"
    with pytest.raises(SessionError, match="already_active"):
        store.prepare_activity("one", "admin", "umo", 1)
    assert store.activity_receipt(identifier, "opening", {"status": "sent"}) == "open"
    assert store.enroll("one", "u1", "User 1", time.time()) == "enrolled"
    assert store.enroll("one", "u1", "User 1", time.time()) == "already_enrolled"
    store.enroll("one", "u2", "User 2", time.time())
    changed = settings(lottery_enabled=True)
    changed["lottery"]["prize"] = "Different"
    with pytest.raises(SessionError, match="frozen"):
        store.save_settings("one", 1, changed, "admin")
    winners, count, _ = store.draw(identifier, {"u1"})
    assert count == 1 and winners == [{"member_id": "u1", "name": "User 1"}]
    with pytest.raises(SessionError, match="not_open"):
        store.draw(identifier, {"u1", "u2"})
    assert (
        store.activity_receipt(identifier, "publishing", {"status": "unknown"})
        == "unknown"
    )
    restored = BusinessStore(tmp_path)
    assert restored.activities("one")[0]["winners"] == winners
    with pytest.raises(SessionError, match="already_active"):
        restored.prepare_activity("one", "admin", "umo", 1)


def test_activity_crash_before_receipt_never_resubmits(tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, settings(lottery_enabled=True), "admin")
    identifier, _ = store.prepare_activity("g", "admin", "umo", 1)
    restored = BusinessStore(tmp_path)
    assert restored.activities("g")[0]["state"] == "unknown"
    assert not restored.due(time.time() + 86400)[1]


def test_reminder_confirmation_ownership_missed_boundary_and_unknown(tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, settings(reminders_enabled=True), "admin")
    preview = store.preview_reminder("g", "admin", "umo", "23:15", "Notify", 1)
    for owner, group, umo in [
        ("other", "g", "umo"),
        ("admin", "other", "umo"),
        ("admin", "g", "other"),
    ]:
        with pytest.raises(SessionError, match="invalid"):
            store.confirm_reminder(group, owner, umo, preview, 1)
    task = store.confirm_reminder("g", "admin", "umo", preview, 1)
    with pytest.raises(SessionError):
        store.confirm_reminder("g", "admin", "umo", preview, 1)
    with store.connect() as db:
        db.execute("UPDATE reminders SET next_run=?", (time.time() - 70,))
    assert not store.reserve_reminder(task, time.time())
    assert store.reminders("g")[0]["state"] == "paused"
    with store.connect() as db:
        db.execute("UPDATE reminders SET state='active',next_run=?", (time.time() - 1,))
    assert store.reserve_reminder(task, time.time())
    restored = BusinessStore(tmp_path)
    assert restored.reminders("g")[0]["state"] == "unknown"
    assert not restored.due(time.time())[0]
    with pytest.raises(SessionError, match="requires_review"):
        restored.pause_reminder("g", task)


def test_settings_preview_requires_same_human_and_version(tmp_path):
    store = BusinessStore(tmp_path)
    draft = store.preview_settings(
        "g", "admin", "umo", settings(rankings_enabled=True), 0
    )
    with pytest.raises(SessionError):
        store.confirm_settings("g", "other", "umo", draft)
    assert store.confirm_settings("g", "admin", "umo", draft) == 1
    assert store.settings("g")["settings"]["rankings_enabled"]
    with pytest.raises(SessionError):
        store.confirm_settings("g", "admin", "umo", draft)
    draft = store.preview_settings("g", "admin", "umo", settings(), 1)
    store.save_settings("g", 1, settings(review_enabled=True), "dashboard")
    with pytest.raises(SessionError):
        store.confirm_settings("g", "admin", "umo", draft)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("排名", ("排名", "")),
        ("/今日排名 2", ("排名", "2")),
        ("群管 选择群 1", ("选择群", "1")),
        ("为什么没统计", ("邀请统计说明", "")),
        ("定时通知 08:00 早上好", ("定时通知", "08:00 早上好")),
        ("排名\n禁言 1", ("", "")),
        ("请帮我禁言", ("", "")),
        ("我说的是排名", ("", "")),
    ],
)
def test_exact_command_recognition(text, expected):
    assert recognize(text) == expected


def test_unverified_invitation_settings_and_unknown_fields_rejected():
    proposal = settings()
    proposal["lottery"]["min_invitations"] = 1
    with pytest.raises(ValueError, match="invitation"):
        validate_settings(proposal)
    with pytest.raises(ValueError):
        validate_settings({**settings(), "auto_kick": True})
    with pytest.raises(ValueError):
        next_daily("25:00", time.time())
    assert next_daily("00:00", time.time()) > time.time()


def prepared_plugin(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path, monkeypatch)
    config = provision(plugin, "alice", "one")
    config.update(
        enable=True,
        message_policy={
            "group_mode": "allowlist",
            "group_ids": ["g"],
            "private_enabled": True,
        },
    )
    instance = SimpleNamespace(
        config=config,
        connection_state="online",
        notifications=[],
        send_parts=AsyncMock(return_value={"status": "sent", "message_id": "receipt"}),
    )
    plugin.context.get_config = lambda umo=None: {
        "platform": [config],
        "admins_id": ["admin"],
    }
    plugin.context.get_platform_inst = lambda _: instance
    plugin.business = Businesses(plugin)
    plugin.commands = Commands(plugin)
    return plugin, config, instance


def test_business_api_owner_group_revision_and_no_sends(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        body = {"session_ref": config["session_ref"], "group_id": "g"}
        assert (await call(plugin, "business_page", "bob", body))[0] == 403
        assert (
            await call(plugin, "business_page", "alice", {**body, "group_id": "other"})
        )[0] == 403
        status, result = await call(plugin, "business_page", "alice", body)
        assert (
            status == 200
            and result["version"] == 0
            and not result["settings"]["lottery_enabled"]
        )
        save = {
            **body,
            "action": "save",
            "confirm": True,
            "version": 0,
            "instance_revision": instance_revision(config),
            "settings": settings(rankings_enabled=True),
        }
        assert (
            await call(
                plugin, "business_page", "alice", {**save, "instance_revision": "stale"}
            )
        )[0] == 409
        assert (await call(plugin, "business_page", "alice", save))[0] == 200
        assert (await call(plugin, "business_page", "alice", save))[0] == 409
        instance.send_parts.assert_not_awaited()
        assert len(plugin.business.stores) == 1

    asyncio.run(run())


def test_runtime_draw_failure_unknown_result_never_redrawn(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(
            ref, "save_settings", "g", 0, settings(lottery_enabled=True), "admin"
        )
        plugin.business.roster = AsyncMock(return_value={"u": {"roleLevel": 20}})
        identifier, state = await plugin.business.start_lottery(
            ref, "g", "admin", "private"
        )
        assert state == "open"
        await plugin.business.db(ref, "enroll", "g", "u", "User", time.time())
        instance.send_parts.side_effect = TimeoutError("secret-never-return")
        assert await plugin.business.draw_lottery(ref, "g", identifier) == "unknown"
        assert instance.send_parts.await_count == 2
        with pytest.raises(SessionError):
            await plugin.business.draw_lottery(ref, "g", identifier)
        assert instance.send_parts.await_count == 2
        receipt = (await plugin.business.db(ref, "activities", "g"))[0]["receipt"]
        assert "secret" not in receipt

    asyncio.run(run())


def test_chat_group_private_permissions_and_ai_preview_never_mutates(
    tmp_path, monkeypatch
):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(
            ref, "save_settings", "g", 0,
            settings(ai_management_enabled=True), "owner",
        )
        event = SimpleNamespace(
            platform=SimpleNamespace(session_ref=ref, config=config),
            is_private_chat=lambda: True,
            is_admin=lambda: True,
            get_sender_id=lambda: "admin",
            get_platform_name=lambda: "lajiaoliao",
            get_extra=lambda _: None,
            unified_msg_origin="private",
        )
        plugin.commands.directories[(ref, "admin", "private")] = {
            "selected": "g",
            "expires": time.time() + 600,
        }
        result = json.loads(
            await plugin.private_management(
                event, "settings_preview", {"changes": {"rankings_enabled": True}}
            )
        )
        assert result["status"] == "preview"
        assert not (await plugin.business.db(ref, "settings", "g"))["settings"][
            "rankings_enabled"
        ]
        assert await plugin.commands.execute(
            event, "确认设置", result["confirm"].split()[1]
        )
        assert (await plugin.business.db(ref, "settings", "g"))["settings"][
            "rankings_enabled"
        ]
        instance.send_parts.assert_not_awaited()
        event.get_sender_id = lambda: "ordinary"
        event.is_admin = lambda: False
        assert await plugin.commands.execute(event, "帮助") == "无权限"
        assert (
            json.loads(await plugin.private_management(event, "groups", {}))["status"]
            == "rejected"
        )

    asyncio.run(run())


def test_sqlite_rollback_account_isolation_and_connection_cleanup(tmp_path):
    one = BusinessStore(tmp_path / "one")
    two = BusinessStore(tmp_path / "two")
    with pytest.raises(RuntimeError):
        with one.connect() as db:
            db.execute("INSERT INTO settings VALUES('g',1,'{}')")
            raise RuntimeError("abort")
    assert one.settings("g")["version"] == 0
    one.save_settings("g", 0, settings(rankings_enabled=True), "admin")
    assert two.settings("g")["version"] == 0
    for _ in range(100):
        assert one.settings("g")["version"] == 1
    for file in (tmp_path / "one").iterdir():
        assert file.stat().st_mode & 0o077 == 0


def test_paused_reminder_resume_preserves_owner_and_never_catches_up(tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, settings(reminders_enabled=True), "admin")
    draft = store.preview_reminder("g", "admin", "umo", "08:00", "Notify", 1)
    identifier = store.confirm_reminder("g", "admin", "umo", draft, 1)
    store.pause_reminder("g", identifier)
    for group, owner, umo, version in [
        ("other", "admin", "umo", 1),
        ("g", "other", "umo", 1),
        ("g", "admin", "other", 1),
        ("g", "admin", "umo", 0),
    ]:
        with pytest.raises(SessionError):
            store.resume_reminder(group, identifier, owner, umo, version)
    store.resume_reminder("g", identifier, "admin", "umo", 1)
    assert store.reminders("g")[0]["next_run"] > time.time()
    with pytest.raises(SessionError):
        store.resume_reminder("g", identifier, "admin", "umo", 1)
    for text in ["multi\nline", "multi\rline", "nul\x00"]:
        with pytest.raises(ValueError):
            store.preview_reminder("g", "admin", "umo", "08:00", text, 1)


def test_business_hot_update_stops_only_known_jobs_and_preserves_unknown(tmp_path):
    store = BusinessStore(tmp_path)
    current = settings(lottery_enabled=True, reminders_enabled=True)
    store.save_settings("g", 0, current, "admin")
    draft = store.preview_reminder("g", "admin", "umo", "08:00", "Notify", 1)
    task = store.confirm_reminder("g", "admin", "umo", draft, 1)
    identifier, _ = store.prepare_activity("g", "admin", "umo", 1)
    store.activity_receipt(identifier, "opening", {"status": "sent"})
    current["rankings_enabled"] = True
    store.save_settings("g", 1, current, "admin")
    assert store.reminders("g")[0]["state"] == "active"
    assert store.activities("g")[0]["state"] == "open"
    assert store.reminders("g")[0]["revision"] == 2
    with store.connect() as db:
        db.execute("UPDATE reminders SET state='unknown'")
    store.save_settings("g", 2, current, "admin")
    assert store.reminders("g")[0]["state"] == "unknown"
    assert store.reminders("g")[0]["revision"] == 2


def test_current_admin_revocation_blocks_stale_role_and_preview_confirmation(
    tmp_path, monkeypatch
):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        event = SimpleNamespace(
            platform=SimpleNamespace(session_ref=ref, config=config),
            is_private_chat=lambda: True,
            is_admin=lambda: True,
            get_sender_id=lambda: "admin",
            get_platform_name=lambda: "lajiaoliao",
            get_extra=lambda _: None,
            unified_msg_origin="private",
        )
        draft = await plugin.business.db(
            ref, "preview_settings", "g", "admin", "private", settings(), 0
        )
        plugin.commands.directories[(ref, "admin", "private")] = {
            "selected": "g",
            "expires": time.time() + 600,
        }
        plugin.context.get_config = lambda umo=None: {
            "platform": [config],
            "admins_id": [],
        }
        assert await plugin.commands.execute(event, "确认设置", draft) == "无权限"
        assert (await plugin.business.db(ref, "settings", "g"))["version"] == 0
        assert (
            json.loads(await plugin.private_management(event, "groups", {}))["status"]
            == "rejected"
        )

    asyncio.run(run())


def test_observation_defaults_off_cache_invalidation_flood_and_duplicate_review(
    tmp_path, monkeypatch
):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        now = time.time()
        event = SimpleNamespace(
            platform=SimpleNamespace(session_ref=ref, notifications=[]),
            get_group_id=lambda: "g",
            get_sender_id=lambda: "ordinary",
            is_admin=lambda: False,
            message_obj=SimpleNamespace(
                timestamp=now,
                message_str="sample",
                message_id="one",
                sender=SimpleNamespace(nickname="Member"),
            ),
        )
        plugin.context.llm_generate = AsyncMock(
            side_effect=lambda **kw: SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "message_id": json.loads(kw["prompt"])["message_id"],
                        "decision": "allow",
                    }
                )
            )
        )
        await plugin.commands.observe(event, "")
        assert not await plugin.business.db(ref, "seen", "g", "one")
        await plugin.business.db(
            ref,
            "save_settings",
            "g",
            0,
            settings(
                rankings_enabled=True, review_enabled=True, review_provider_id="mock"
            ),
            "admin",
        )
        for i in range(5):
            event.message_obj.message_id = "m" + str(i)
            event.message_obj.timestamp = now - 4 + i
            await plugin.commands.observe(event, "")
        assert plugin.context.llm_generate.await_count == 5
        await plugin.commands.observe(event, "")
        assert plugin.context.llm_generate.await_count == 5
        logs = await plugin.business.db(ref, "audit", "g")
        assert any("flood_candidate" in log["detail"] for log in logs)
        assert all(
            "native_sanctions_unverified" in log["detail"]
            for log in logs
            if log["kind"] == "review"
        )
        assert all(
            isinstance(item[1], bytes) and len(item[1]) == 32
            for window in plugin.business.windows.values()
            for item in window
        )
        event.message_obj.message_id = "oversized"
        event.message_obj.message_str = "x" * 16385
        await plugin.commands.observe(event, "")
        assert plugin.context.llm_generate.await_count == 5
        assert not await plugin.business.db(ref, "seen", "g", "oversized")
        await plugin.business.db(ref, "save_settings", "g", 1, settings(), "admin")
        event.message_obj.message_id = "disabled"
        await plugin.commands.observe(event, "")
        assert not await plugin.business.db(ref, "seen", "g", "disabled")
        instance.send_parts.assert_not_awaited()

    asyncio.run(run())


def test_group_directory_policy_revoked_during_io_blocks_activity(
    tmp_path, monkeypatch
):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(
            ref, "save_settings", "g", 0, settings(lottery_enabled=True), "admin"
        )

        async def directory(*_):
            config["message_policy"]["group_ids"] = []
            return {"members": [{"groupID": "g", "userID": "u", "roleLevel": 20}]}

        plugin.manager.request.side_effect = directory
        with pytest.raises(PermissionError):
            await plugin.business.start_lottery(ref, "g", "admin", "private")
        assert not await plugin.business.db(ref, "activities", "g")
        instance.send_parts.assert_not_awaited()

    asyncio.run(run())


def test_draw_cancels_after_creator_permission_revocation(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        await plugin.business.db(
            ref, "save_settings", "g", 0, settings(lottery_enabled=True), "admin"
        )
        plugin.business.roster = AsyncMock(return_value={"u": {"roleLevel": 20}})
        identifier, _ = await plugin.business.start_lottery(
            ref, "g", "admin", "private"
        )
        plugin.context.get_config = lambda umo=None: {
            "platform": [config],
            "admins_id": [],
        }
        assert await plugin.business.draw_lottery(ref, "g", identifier) == "cancelled"
        assert instance.send_parts.await_count == 1
        assert (await plugin.business.db(ref, "activities", "g"))[0][
            "state"
        ] == "cancelled"

    asyncio.run(run())


def test_real_waking_and_process_pipeline_never_models_fixed_commands(
    tmp_path, monkeypatch
):
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.process_stage.stage import (
        ProcessStage,
        StarRequestSubStage,
    )
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry
    from data.plugins.astrbot_plugin_lajiaoliao.event import (
        LajiaoLiaoEvent,
        incoming_to_message,
    )

    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        instance.meta = lambda: PlatformMetadata("lajiaoliao", "test", "one")
        other = plugin.store.create("bob")["session_ref"]
        plugin.store.complete(other, "bob", "admin")
        plugin.store.bind(other, "bob", "other")
        instance.member_ids = {}
        instance.session_ref = config["session_ref"]
        plugin.logger = SimpleNamespace(warning=lambda *_: None)
        handler = copy.copy(
            next(
                row
                for row in star_handlers_registry
                if row.handler_name == "group_businesses"
                and row.handler_module_path.endswith("astrbot_plugin_lajiaoliao.main")
            )
        )
        handler.handler = plugin.group_businesses
        handler.enabled = True
        monkeypatch.setattr(star_handlers_registry, "_handlers", [handler])
        monkeypatch.setitem(
            star_map,
            handler.handler_module_path,
            SimpleNamespace(
                name="astrbot_plugin_lajiaoliao", activated=True, reserved=False
            ),
        )
        context = PipelineContext(
            astrbot_config={
                "platform_settings": {},
                "provider_settings": {
                    "enable": True,
                    "prompt_prefix": "",
                    "identifier": "",
                },
                "wake_prefix": ["/"],
                "admins_id": ["admin"],
                "plugin_set": ["*"],
            },
            plugin_manager=SimpleNamespace(),
            astrbot_config_id="test",
        )
        wake = WakingCheckStage()
        await wake.initialize(context)
        commands = StarRequestSubStage()
        await commands.initialize(context)
        model_calls = []

        class Model:
            async def process(self, event):
                model_calls.append(event.message_obj.message_id)
                yield

        process = ProcessStage()
        process.ctx = context
        process.star_request_sub_stage = commands
        process.agent_sub_stage = Model()
        await plugin.business.db(
            config["session_ref"], "save_settings", "g", 0,
            settings(manager_user_ids=["delegate"], group_ai_enabled=True), "dashboard:owner",
        )
        for mid, text, group, user, model_expected, sends_expected in [
            ("passive", "ordinary group text", True, "ordinary", False, 0),
            ("wake", "/ordinary model request", True, "ordinary", True, 0),
            ("command", "/排名", True, "ordinary", False, 1),
            ("private_group", "/禁言 123", True, "admin", False, 0),
            ("denied", "/帮助", False, "ordinary", False, 1),
            ("allowed", "/帮助", False, "admin", False, 1),
            ("delegated", "/帮助", False, "delegate", False, 1),
            ("identity", "/我的权限", True, "delegate", False, 1),
            ("off_model", "/ordinary model request", True, "ordinary", False, 0),
            ("off_command", "/排名", True, "ordinary", False, 1),
        ]:
            if mid == "off_model":
                await plugin.business.db(
                    config["session_ref"], "save_settings", "g", 1,
                    settings(manager_user_ids=["delegate"], group_ai_enabled=False),
                    "dashboard:owner",
                )
            event = LajiaoLiaoEvent(
                incoming_to_message(
                    {
                        "message_id": mid,
                        "sender_id": user,
                        "sender_name": user,
                        "target_id": "g" if group else user,
                        "kind": "group" if group else "private",
                        "timestamp": time.time(),
                        "text": text,
                    },
                    "bot",
                ),
                instance,
            )
            previous = len(model_calls)
            sent_before = instance.send_parts.await_count
            await wake.process(event)
            async for _ in process.process(event):
                pass
            assert (len(model_calls) > previous) == model_expected
            assert instance.send_parts.await_count - sent_before == sends_expected
            if user == "delegate":
                assert not event.is_admin()
        await plugin.business.close()

    asyncio.run(run())


def test_platform_filter_and_tool_removal_are_scoped_and_do_not_mutate_global_tools(
    tmp_path, monkeypatch
):
    from data.plugins.astrbot_plugin_lajiaoliao.main import LajiaoLiaoFilter

    class Tools:
        def __init__(self):
            self.tools = ["lajiaoliao_private_management", "other_tool"]

        def get_tool(self, name):
            return name if name in self.tools else None

        def remove_tool(self, name):
            self.tools.remove(name)

    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        await plugin.business.db(
            config["session_ref"], "save_settings", "g", 0,
            settings(ai_management_enabled=True), "owner",
        )
        event = SimpleNamespace(
            get_platform_name=lambda: "telegram",
            is_private_chat=lambda: True,
            get_extra=lambda _: None,
            get_sender_id=lambda: "admin",
            unified_msg_origin="private",
        )
        original = Tools()
        req = SimpleNamespace(func_tool=original)
        await plugin.restrict_management_tool(event, req)
        assert req.func_tool.tools == ["other_tool"]
        assert len(original.tools) == 2
        assert not LajiaoLiaoFilter().filter(event, {})
        event.get_platform_name = lambda: "wangshangliao"
        assert not LajiaoLiaoFilter().filter(event, {})
        event.get_platform_name = lambda: "lajiaoliao"
        event.platform = SimpleNamespace(session_ref=config["session_ref"])
        assert LajiaoLiaoFilter().filter(event, {})
        req.func_tool = original
        await plugin.restrict_management_tool(event, req)
        assert req.func_tool is original
        event.get_extra = lambda _: False
        await plugin.restrict_management_tool(event, req)
        assert req.func_tool.tools == ["other_tool"]
        assert len(original.tools) == 2

    asyncio.run(run())


def test_database_caller_cancel_shutdown_waits_for_inflight_write(
    tmp_path, monkeypatch
):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        release = threading.Event()
        committed = []

        def delayed():
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=2)
            committed.append(True)

        plugin.business.stores[ref] = SimpleNamespace(delayed=delayed)
        task = asyncio.create_task(plugin.business.db(ref, "delayed"))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert plugin.business.database_tasks and not committed
        assert plugin.business.database_slots._value == 3
        asyncio.get_running_loop().call_later(0.05, release.set)
        await plugin.business.close()
        assert committed and not plugin.business.database_tasks
        with pytest.raises(SessionError, match="plugin_stopping"):
            await plugin.business.db(ref, "delayed")

    asyncio.run(run())


def test_cancelled_database_queue_does_not_later_execute(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        executed = []
        plugin.business.database_slots = asyncio.Semaphore(0)
        plugin.business.stores[ref] = SimpleNamespace(
            write=lambda: executed.append(True)
        )
        task = asyncio.create_task(plugin.business.db(ref, "write"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        plugin.business.database_slots.release()
        await asyncio.sleep(0)
        assert not executed and not plugin.business.database_tasks
        await plugin.business.close()

    asyncio.run(run())
