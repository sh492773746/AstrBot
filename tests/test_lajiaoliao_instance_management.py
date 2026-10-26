import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_lajiaoliao_plugin_api import call, make_plugin


class Config(dict):
    def save_config(self):
        pass


@pytest.fixture
def adapter_class(monkeypatch):
    from astrbot.core.platform import register

    # Legacy adapter tests share this test process; isolate registration on import.
    with monkeypatch.context() as registry:
        registry.setattr(register, "platform_cls_map", {
            key: value for key, value in register.platform_cls_map.items() if key != "lajiaoliao"
        })
        registry.setattr(register, "platform_registry", [
            item for item in register.platform_registry if item.name != "lajiaoliao"
        ])
        from data.plugins.astrbot_plugin_lajiaoliao.adapter import LajiaoLiaoAdapter
    return LajiaoLiaoAdapter


def provision(plugin, owner, instance):
    plugin.manager.store = plugin.store
    ref = plugin.store.create(owner)["session_ref"]
    plugin.store.complete(ref, owner, "account-" + instance)
    plugin.store.bind(ref, owner, instance)
    return {"id": instance, "type": "lajiaoliao", "session_ref": ref, "enable": False,
            "message_policy": {"group_mode": "allowlist", "group_ids": [], "private_enabled": False}}


def test_dashboard_validator_checks_owner_and_immutable_binding(tmp_path, monkeypatch, adapter_class):
    plugin = make_plugin(tmp_path, monkeypatch)
    config = provision(plugin, "alice", "one")
    adapter_class.validate_dashboard_config(config, owner="alice", current=None)
    with pytest.raises(ValueError, match="session_access_denied"):
        adapter_class.validate_dashboard_config(config, owner="bob", current=None)
    with pytest.raises(ValueError, match="session_binding_mismatch"):
        adapter_class.validate_dashboard_config({**config, "id": "two"}, owner="alice", current=None)
    with pytest.raises(ValueError, match="account_change_requires_new_instance"):
        adapter_class.validate_dashboard_config(config, owner="alice", current={**config, "session_ref": "b" * 32})
    with pytest.raises(ValueError, match="platform_type_change"):
        adapter_class.validate_dashboard_config(config, owner="alice", current={"type": "telegram"})


def test_management_owner_scope_policy_hot_update_and_revision(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        one = provision(plugin, "alice", "one")
        two = provision(plugin, "alice", "two")
        foreign = provision(plugin, "bob", "foreign")
        config = Config(platform=[one, two, foreign])
        untouched = copy.deepcopy([two, foreign])
        instance = SimpleNamespace(config=copy.deepcopy(one), connection_state="stopped")
        plugin.context.get_config = lambda: config
        plugin.context.get_platform_inst = lambda id_: instance if id_ == "one" else None
        status, result = await call(plugin, "management", "alice")
        assert status == 200
        assert [item["id"] for item in result["instances"]] == ["one", "two"]
        body = {"session_ref": one["session_ref"], "instance_id": "one",
                "revision": result["instances"][0]["revision"],
                "message_policy": {"group_mode": "allowlist", "group_ids": ["test-group"], "private_enabled": True}}
        assert (await call(plugin, "policy", "bob", body))[0] == 403
        assert (await call(plugin, "policy", "alice", {**body, "instance_id": "two"}))[0] == 409
        assert (await call(plugin, "policy", "alice", {**body, "message_policy": {"group_mode": "all"}}))[0] == 400
        assert (await call(plugin, "policy", "alice", body))[0] == 200
        assert instance.config["message_policy"] == one["message_policy"] == body["message_policy"]
        assert one["enable"] is False
        assert [two, foreign] == untouched
        assert (await call(plugin, "policy", "alice", body))[0] == 409
        _, result = await call(plugin, "management", "alice")
        body["revision"] = result["instances"][0]["revision"]
        body["message_policy"] = {"group_mode": "allowlist", "group_ids": [], "private_enabled": False}
        before = copy.deepcopy(one)
        config.save_config = Mock(side_effect=OSError("synthetic-disk-error"))
        assert (await call(plugin, "policy", "alice", body))[0] == 409
        assert one == before
        assert instance.config["message_policy"] == before["message_policy"]
    asyncio.run(run())


def test_bot_service_rejects_foreign_reference_before_persistence(tmp_path, monkeypatch, adapter_class):
    from astrbot.core.platform.register import platform_cls_map
    from astrbot.dashboard.services.config_service import BotConfigService

    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        monkeypatch.setitem(platform_cls_map, "lajiaoliao", adapter_class)
        saved = {"platform": []}
        service = BotConfigService(SimpleNamespace(astrbot_config=saved))
        with pytest.raises(ValueError, match="session_access_denied"):
            await service.create_bot(config, owner="bob")
        assert saved["platform"] == []
        saved["platform"].append(copy.deepcopy(config))
        with pytest.raises(ValueError, match="session_access_denied"):
            await service.update_bot("one", config, owner="bob")
        assert saved["platform"] == [config]
    asyncio.run(run())


def test_shared_revision_rejects_stale_editor_after_group_policy_save(tmp_path, monkeypatch, adapter_class):
    from astrbot.core.platform.instance_control import instance_revision
    from astrbot.core.platform.register import platform_cls_map
    from astrbot.dashboard.responses import ApiError
    from astrbot.dashboard.services import config_service
    from astrbot.dashboard.services.config_service import BotConfigService

    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        bot = provision(plugin, "alice", "one")
        config = Config(platform=[bot])
        plugin.context.get_config = lambda: config
        lifecycle = SimpleNamespace(astrbot_config=config, platform_manager=SimpleNamespace(reload=AsyncMock()))
        service = BotConfigService(lifecycle)
        monkeypatch.setitem(platform_cls_map, "lajiaoliao", adapter_class)
        save = Mock()
        monkeypatch.setattr(config_service, "save_config", save)
        draft = service.get_bot("one")["bot"]
        assert draft["_instance_revision"] == instance_revision(bot)
        _, management = await call(plugin, "management", "alice")
        body = {"session_ref": bot["session_ref"], "instance_id": "one",
                "revision": management["instances"][0]["revision"],
                "message_policy": {"group_mode": "allowlist", "group_ids": ["new"], "private_enabled": False}}
        assert (await call(plugin, "policy", "alice", body))[0] == 200
        with pytest.raises(ApiError) as conflict:
            await service.update_bot("one", draft, owner="alice")
        assert conflict.value.status_code == 409
        save.assert_not_called()
        fresh = service.get_bot("one")["bot"]
        fresh["message_policy"]["private_enabled"] = True
        await service.update_bot("one", fresh, owner="alice")
        assert "_instance_revision" not in config["platform"][0]
        assert (await call(plugin, "policy", "alice", body))[0] == 409
        assert config["platform"][0]["message_policy"]["private_enabled"] is True
    asyncio.run(run())


def test_enabled_patch_uses_owner_and_preserves_other_platform_behavior(tmp_path, monkeypatch, adapter_class):
    from astrbot.core.platform.register import platform_cls_map
    from astrbot.dashboard.services import config_service
    from astrbot.dashboard.services.config_service import BotConfigService

    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        bot = provision(plugin, "alice", "one")
        config = Config(platform=[bot])
        lifecycle = SimpleNamespace(astrbot_config=config, platform_manager=SimpleNamespace(reload=AsyncMock()))
        service = BotConfigService(lifecycle)
        monkeypatch.setitem(platform_cls_map, "lajiaoliao", adapter_class)
        monkeypatch.setattr(config_service, "save_config", Mock())
        with pytest.raises(ValueError, match="session_access_denied"):
            await service.set_bot_enabled("one", True, owner="bob")
        await service.set_bot_enabled("one", True, owner="alice")
        assert config["platform"][0]["enable"] is True
    asyncio.run(run())


def test_two_accounts_keep_contexts_and_deduplication_separate(tmp_path, monkeypatch, adapter_class):
    from data.plugins.astrbot_plugin_lajiaoliao.event import incoming_to_message

    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        configs = [provision(plugin, "alice", name) for name in ("one", "two")]
        queue = asyncio.Queue()
        adapters = [adapter_class(config, {}, queue) for config in configs]
        payload = {"message_id": "same-id", "sender_id": "same-sender", "target_id": "same-group",
                   "kind": "group", "text": "synthetic"}
        events = [adapter.create_event(incoming_to_message(payload, adapter.account_id)) for adapter in adapters]
        assert events[0].unified_msg_origin != events[1].unified_msg_origin
        assert adapters[0].session_ref != adapters[1].session_ref
        assert all(adapter._remember_message("same-id") for adapter in adapters)
        assert not any(adapter._remember_message("same-id") for adapter in adapters)
        for event in events:
            event.processing_completion.cancel()
    asyncio.run(run())


@pytest.mark.parametrize("via,expected", [("jwt", "alice"), ("api_key", "")])
def test_all_bot_save_routes_forward_dashboard_identity(via, expected):
    from astrbot.dashboard.api import bots
    from astrbot.dashboard.schemas import BotConfigRequest

    async def run():
        config = {"type": "lajiaoliao", "id": "one", "session_ref": "a" * 32}
        payload = BotConfigRequest(config=config, bot_id="one")
        auth = SimpleNamespace(username="alice", via=via)
        service = SimpleNamespace(create_bot=AsyncMock(), update_bot=AsyncMock())
        await bots.create_bot(payload, auth, service)
        await bots.update_bot_by_id(payload, auth, service)
        await bots.update_bot("one", payload, auth, service)
        await bots.create_dashboard_alias_platform(payload, auth, service)
        request = SimpleNamespace(json=AsyncMock(return_value={"id": "one", "config": config}))
        await bots.update_dashboard_alias_platform(request, auth, service)
        assert service.create_bot.await_count == 2
        assert service.update_bot.await_count == 3
        for call_ in service.create_bot.await_args_list + service.update_bot.await_args_list:
            assert call_.kwargs["owner"] == expected
    asyncio.run(run())
