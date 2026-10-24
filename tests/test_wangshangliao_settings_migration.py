"""Business migration, hot publication and disabled-plugin safety boundaries."""

import copy
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.settings import (
    BusinessSettings,
    business_revision,
    validate_settings,
)
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
    BUSINESS_FIELDS,
    runtime,
)


class Config(dict):
    async def save_config_async(self):
        return True


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    from astrbot.core.platform.sources.wangshangliao import adapter as native
    from astrbot.core.platform.sources.wangshangliao import storage

    monkeypatch.setattr(runtime, "service", None)
    monkeypatch.setattr(runtime, "classifier", None)
    monkeypatch.setattr(runtime, "help_responder", None)
    monkeypatch.setattr(runtime, "snapshots", {})
    monkeypatch.setattr(runtime, "generation", 0)
    monkeypatch.setattr(runtime, "observation_groups", {})
    monkeypatch.setattr(native, "instance_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(storage, "instance_dir", lambda name: tmp_path / name)
    bot = {
        "id": "bot",
        "type": "wangshangliao",
        "account_id": "12",
        "enable": True,
        "nickname": "Fixture",
        "enabled_groups": ["34"],
        "reply_private": True,
        "reply_groups": {"34": True},
        "proactive_send": {"enabled": True, "targets": ["34"]},
        "ai_routes": {"admin_provider_id": "", "groups": {"34": ""}},
        "moderation": {
            "enabled": True,
            "permissions": {"34": ["mute", "recall", "rename"]},
            "manual_kick_only": True,
            "card_auto": {"34": False},
        },
        "developer_test": {"seconds": 120, "budget": 3},
    }
    original = copy.deepcopy(bot)
    config = Config(platform=[bot], admins_id=["99"])
    adapter = native.WangshangliaoAdapter(bot, {}, __import__("asyncio").Queue())
    adapter.connection_state = "online"
    adapter.load_groups = AsyncMock()
    manager = SimpleNamespace(
        platform_insts=[adapter],
        terminate_platform=AsyncMock(),
        load_platform=AsyncMock(),
    )
    context = SimpleNamespace(get_config=lambda *args: config, platform_manager=manager)
    settings = BusinessSettings(context, tmp_path / "plugin")
    return settings, adapter, config, original, manager


@pytest.mark.asyncio
async def test_import_is_exact_idempotent_and_removes_core_business_fields(fixture):
    service, adapter, config, original, manager = fixture
    await service.start(recognize_command)
    assert not (set(config["platform"][0]) & BUSINESS_FIELDS)
    assert config["admins_id"] == ["99"]
    for key in BUSINESS_FIELDS:
        if key in original:
            expected = copy.deepcopy(original[key])
            if key == "moderation":
                expected.update(service.keyword_defaults)
            assert adapter.config[key] == expected
    first = adapter.config["_business_revision"]
    await service.close()
    await service.start(recognize_command)
    assert adapter.config["_business_revision"] == first
    assert len(service.history("bot")["changes"]) == 1
    manager.terminate_platform.assert_not_called()
    manager.load_platform.assert_not_called()


@pytest.mark.asyncio
async def test_policy_journal_migration_preserves_data_for_readonly_cashier(fixture):
    service, _, _, _, _ = fixture
    await service.start(recognize_command)
    with sqlite3.connect(service.path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        before = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("policies", "policy_history", "settings_log")
        }
    db.close()
    assert not service.path.with_name(service.path.name + "-shm").exists()
    service.initialized = False
    with service.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        after = {
            table: [
                tuple(row)
                for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in before
        }
    assert after == before
    with sqlite3.connect(service.path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        policy = json.loads(
            db.execute("SELECT payload FROM policies WHERE instance='bot'").fetchone()[
                0
            ]
        )
    assert policy["enabled_groups"] == ["34"]
    assert not list(service.root.glob("settings.sqlite3-*"))
    await service.close()


@pytest.mark.asyncio
async def test_help_handler_is_extension_owned_and_replaced_after_reactivation(fixture):
    service, _, _, _, _ = fixture
    first = AsyncMock()
    second = AsyncMock()
    await service.start(recognize_command, help_responder=first)
    assert runtime.help_responder is first
    await service.close()
    assert runtime.help_responder is None
    await service.start(recognize_command, help_responder=second)
    assert runtime.help_responder is second
    await service.close()
    assert runtime.help_responder is None


@pytest.mark.parametrize(
    "keywords",
    [
        {},
        {"mute_keywords": [], "kick_keywords": []},
        {"mute_keywords": ["custom"], "kick_keywords": ["risk"]},
        {"keywords": ["legacy"]},
    ],
)
def test_keyword_defaults_fill_only_missing_new_policy_fields(fixture, keywords):
    service, _, config, _, _ = fixture
    bot = config["platform"][0]
    bot["moderation"].update(keywords)
    loaded = service.load(bot).values()["moderation"]
    for field in ("mute_keywords", "kick_keywords"):
        if field == "mute_keywords" and "keywords" in keywords:
            assert field not in loaded
        else:
            assert loaded[field] == keywords.get(field, service.keyword_defaults[field])
    assert loaded.get("keywords") == keywords.get("keywords")
    assert service.load(bot).values()["moderation"] == loaded


@pytest.mark.asyncio
async def test_cleared_keywords_remain_empty_after_reload(fixture):
    service, adapter, _, _, _ = fixture
    await service.start(recognize_command)
    proposal = adapter.config
    proposal["moderation"]["mute_keywords"] = []
    await service.save(proposal, business_revision(proposal), "admin")
    assert service.load(proposal).values()["moderation"]["mute_keywords"] == []


@pytest.mark.asyncio
async def test_hot_save_cas_immutable_snapshot_and_transport_identity(fixture):
    service, adapter, config, _, manager = fixture
    await service.start(recognize_command)
    before = adapter.config
    proposal = copy.deepcopy(before)
    proposal["moderation"]["card_suffix"] = {"34": "·成员"}
    result = await service.save(proposal, business_revision(before), "dashboard:admin")
    assert result != business_revision(before)
    assert adapter.config["moderation"]["card_suffix"] == {"34": "·成员"}
    detached = adapter.config
    detached["moderation"]["permissions"]["34"].clear()
    assert "mute" in adapter.config["moderation"]["permissions"]["34"]
    with pytest.raises(ValueError, match="configuration_changed"):
        await service.save(before, business_revision(before), "dashboard:stale")
    assert not (set(config["platform"][0]) & BUSINESS_FIELDS)
    manager.terminate_platform.assert_not_called()
    manager.load_platform.assert_not_called()
    assert len(service.history("bot")["changes"]) == 2
    assert service.history("bot")["logs"][0]["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_policy_and_snapshot(fixture):
    service, adapter, _, _, _ = fixture
    await service.start(recognize_command)
    before = adapter.config
    proposal = copy.deepcopy(before)
    proposal["reply_private"] = False
    with service.connect() as db:
        db.execute("""
            CREATE TRIGGER reject_committed_audit BEFORE INSERT ON settings_log
            WHEN NEW.outcome='committed'
            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        await service.save(proposal, business_revision(before), "admin")
    assert adapter.config == before
    assert len(service.history("bot")["changes"]) == 1
    assert service.load(before).revision == business_revision(before)


@pytest.mark.asyncio
async def test_disabled_plugin_blocks_replies_moderation_and_new_policy_writes(fixture):
    from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
    from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

    service, adapter, _, _, manager = fixture
    await service.start(recognize_command)
    proposal = adapter.config
    await service.close()
    assert adapter.connection_state == "online"
    assert adapter.enabled_groups == {"34"}
    assert not adapter.config["reply_private"]
    assert not adapter.config["proactive_send"]["enabled"]
    assert not runtime.recognize("排名")
    with pytest.raises(ProtocolError, match="moderation_permission"):
        authorize_action(adapter.config, "34", "mute")
    with pytest.raises(ProtocolError, match="business_service_inactive"):
        await adapter.send_text("34", "blocked", "must not send")
    with pytest.raises(ValueError, match="business_service_inactive"):
        await service.save(proposal, business_revision(proposal), "admin")
    manager.terminate_platform.assert_not_called()


@pytest.mark.asyncio
async def test_account_binding_refuses_reuse_and_invalid_import_fails_closed(fixture):
    service, adapter, config, _, _ = fixture
    await service.start(recognize_command)
    await service.close()
    config["platform"][0]["account_id"] = "13"
    with pytest.raises(ValueError, match="account_binding"):
        service.load(config["platform"][0])
    assert not adapter.config["_business_active"]


@pytest.mark.asyncio
async def test_restart_loads_latest_settings_without_rebuilding_history(fixture):
    service, adapter, config, _, _ = fixture
    await service.start(recognize_command)
    proposal = adapter.config
    proposal["reply_private"] = False
    latest = await service.save(proposal, business_revision(proposal), "admin")
    await service.close()
    reopened = BusinessSettings(service.context, service.root)
    await reopened.start(recognize_command)
    assert adapter.config["_business_revision"] == latest
    assert adapter.config["reply_private"] is False
    assert len(reopened.history("bot")["changes"]) == 2


@pytest.mark.asyncio
async def test_only_group_selection_changes_refresh_the_native_directory(fixture):
    service, adapter, _, _, _ = fixture
    await service.start(recognize_command)
    adapter.load_groups.reset_mock()
    proposal = adapter.config
    proposal["reply_private"] = False
    await service.save(proposal, business_revision(proposal), "admin")
    adapter.load_groups.assert_not_awaited()
    proposal = adapter.config
    proposal["enabled_groups"] = ["34", "35"]
    await service.save(proposal, business_revision(proposal), "admin")
    adapter.load_groups.assert_awaited_once()


@pytest.mark.asyncio
async def test_transport_changes_do_not_invalidate_business_drafts(fixture):
    service, adapter, config, _, _ = fixture
    await service.start(recognize_command)
    before = business_revision(adapter.config)
    config["platform"][0]["nickname"] = "Renamed transport"
    assert business_revision(adapter.config) == before


@pytest.mark.parametrize(
    "values",
    [
        {"enabled_groups": [{}]},
        {"reply_private": "true"},
        {"moderation": {"permissions": {"34": ["invalid"]}}},
        {"moderation": {"manual_kick_only": True, "auto_kick": {"34": True}}},
        {"developer_test": {"seconds": 301}},
        {"developer_test": {"budget": 11}},
        {"developer_test": {"scopes": [{}]}},
        {"account_id": "13"},
    ],
)
def test_invalid_policy_never_grants_permissions(values):
    with pytest.raises(ValueError):
        validate_settings(values)


@pytest.mark.asyncio
async def test_cold_observation_is_read_only_and_cannot_grant_execution(fixture):
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import PolicyRuntime

    service, _, config, _, _ = fixture
    await service.start(recognize_command)
    await service.close()
    cold = PolicyRuntime()
    bot = config["platform"][0]
    before = service.path.read_bytes()
    cold.restore_observation(bot, service.path)
    view = cold.view(bot)
    assert view["enabled_groups"] == ["34"]
    assert view["_business_active"] is False
    assert not view["reply_private"]
    assert view["moderation"] == {"enabled": False}
    assert cold.snapshot(bot) is None
    assert service.path.read_bytes() == before
    assert cold.view({**bot, "account_id": "another"})["enabled_groups"] == []


@pytest.mark.asyncio
async def test_missing_migrated_policy_fails_closed_instead_of_reimporting_defaults(
    fixture,
):
    service, adapter, config, _, _ = fixture
    await service.start(recognize_command)
    await service.close()
    with service.connect() as db:
        db.execute("DELETE FROM policies")
    await service.start(recognize_command)
    assert not adapter.config["_business_active"]
    assert service.history("bot")["logs"][0]["code"] == "settings_import_failed"
    assert not (set(config["platform"][0]) & BUSINESS_FIELDS)


@pytest.mark.asyncio
async def test_pause_consumes_queued_chat_but_preserves_invitation_evidence(
    fixture, tmp_path
):
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger

    service, adapter, _, _, _ = fixture
    await service.start(recognize_command)
    old_epoch = adapter.config["_business_epoch"]
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    try:
        await adapter.ledger.ingest("12", "34", "chat", {"text": "排名"})
        await adapter.ledger.ingest("12", "34", "invite", {"kind": "team_invite"})
        await adapter.ledger.mark("12", "34", "invite", "invite_notification")
        await service.close()
        await service.start(recognize_command)
        async with adapter.ledger.db.execute(
            "SELECT message,state FROM inbox ORDER BY message"
        ) as cursor:
            assert await cursor.fetchall() == [
                ("chat", "business_suspended"),
                ("invite", "invite_notification"),
            ]
        assert adapter.config["_business_epoch"] != old_epoch
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_failed_core_cleanup_never_activates_business_and_is_retryable(fixture):
    service, adapter, config, original, _ = fixture
    config.save_config_async = AsyncMock(return_value=False)
    with pytest.raises(ValueError, match="persistence_superseded"):
        await service.start(recognize_command)
    assert runtime.service is None
    assert not adapter.config["_business_active"]
    assert config["platform"] == [original]
    config.save_config_async = AsyncMock(return_value=True)
    await service.start(recognize_command)
    assert len(service.history("bot")["changes"]) == 1


@pytest.mark.asyncio
async def test_backup_and_rollback_export_preserve_latest_values_without_restoring_ledgers(
    fixture, tmp_path
):
    import sqlite3
    from contextlib import closing

    from astrbot.builtin_stars.wangshangliao_moderation.migration import (
        backup,
        export_config,
    )

    service, adapter, config, _, _ = fixture
    data = tmp_path / "data"
    service.root = data / "plugin_data" / "wangshangliao_moderation"
    service.path = service.root / "settings.sqlite3"
    await service.start(recognize_command)
    proposal = adapter.config
    proposal["reply_private"] = False
    await service.save(proposal, business_revision(proposal), "admin")
    (data / "cmd_config.json").write_text(json.dumps(config))
    ledger = data / "platform_data" / "wangshangliao" / "fixture" / "activities.sqlite3"
    ledger.parent.mkdir(parents=True)
    with closing(sqlite3.connect(ledger)) as db:
        db.execute("CREATE TABLE paid(id TEXT PRIMARY KEY, amount INTEGER)")
        db.execute("INSERT INTO paid VALUES('immutable',500)")
        db.commit()
    before = ledger.read_bytes()
    dest = tmp_path / "backup"
    manifest = backup(data, dest)
    assert len(manifest["files"]) == 3
    with closing(sqlite3.connect(dest / ledger.relative_to(data))) as db:
        assert db.execute("SELECT * FROM paid").fetchall() == [("immutable", 500)]
    output = tmp_path / "rollback.json"
    assert export_config(data, output) == 1
    assert json.loads(output.read_text())["platform"][0]["reply_private"] is False
    assert ledger.read_bytes() == before
    assert (data / "cmd_config.json").read_text() == json.dumps(config)
    with pytest.raises(FileExistsError):
        export_config(data, output)
