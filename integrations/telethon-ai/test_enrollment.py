import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from build_plugin import FILES, build
from migrate_customer_boundary import ISOLATION, TEST_PLATFORM, check

from data.plugins.astrbot_plugin_telethon_ai import adapter, registry
from data.plugins.astrbot_plugin_telethon_ai.customer import isolated_profile
from data.plugins.astrbot_plugin_telethon_ai.enrollment import Enrollment
from data.plugins.astrbot_plugin_telethon_ai.tenants import Denied, Tenants


def test_grant_enroll_idempotence_and_owner(tmp_path):
    store = Tenants(tmp_path / "tenant.db")
    try:
        store.grant("admin", 1, 2)
        tenant = store.enroll(1, 22, "testbot", b"ciphertext")
        assert store.enroll(1, 22, "testbot", b"ignored") == tenant
        assert store.pending_grant(1) is None
        assert store.summary(tenant)["enabled"] == 0
        with pytest.raises(Denied):
            store.enroll(2, 22, "testbot", b"fake")
        with pytest.raises(Denied):
            store.enroll(1, 23, "another", b"fake")
        with pytest.raises(Denied):
            store.grant("admin", 1, 2)
    finally:
        store.close()


def test_registry_empty_private_and_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "private_root", lambda: tmp_path)
    assert registry.load() == {}
    path = tmp_path / "accounts.json"
    record = {
        "user_id": 1,
        "api_id": 123,
        "api_hash": "fake",
        "session": str(tmp_path / "one"),
    }
    path.write_text(json.dumps({"one": record}))
    path.chmod(0o600)
    assert registry.load()["one"]["user_id"] == 1
    path.chmod(0o644)
    with pytest.raises(ValueError):
        registry.load()
    path.chmod(0o600)
    path.write_text(json.dumps({"one": record, "two": record}))
    with pytest.raises(ValueError):
        registry.load()


def test_legacy_customer_migration_is_scoped():
    legacy = {
        "id": TEST_PLATFORM,
        "type": "telegram",
        "enable": True,
        "telegram_token": "fixture-token",
        "telegram_allowed_updates": ["message", "callback_query"],
    }
    check(legacy)
    check({**legacy, **ISOLATION})
    with pytest.raises(AssertionError):
        check({**legacy, "id": "VIP_DHBot"})
    with pytest.raises(AssertionError):
        check({**legacy, "telegram_dedicated_reporting": False})
    with pytest.raises(AssertionError):
        check({**legacy, "telegram_allowed_updates": ["message", "managed_bot"]})


def test_single_package_is_allowlisted(tmp_path):
    source = tmp_path / "plugin"
    source.mkdir()
    for filename in FILES:
        path = source / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    (source / "secret.session").write_text("secret")
    (source / "runtime.json").write_text("secret")
    archive = build(source, tmp_path / "plugin.zip")
    with ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == set(FILES)
        assert not any("session" in n or "runtime" in n for n in bundle.namelist())


class Config(dict):
    def save_config(self):
        pass


class Manager:
    def __init__(self):
        self.default_conf = Config(platform=[])
        self.confs = {}
        self.names = {}
        self.ucr = SimpleNamespace(
            umop_to_conf_id={},
            update_route=AsyncMock(side_effect=self.route),
            get_conf_id_for_umop=lambda umo: self.ucr.umop_to_conf_id.get(
                umo.split(":", 1)[0] + "::"
            ),
        )

    async def route(self, route, conf_id):
        self.ucr.umop_to_conf_id[route] = conf_id

    def get_conf_list(self):
        return [{"id": key, "name": name} for key, name in self.names.items()]

    async def create_conf(self, config, name):
        key = str(len(self.confs) + 1)
        self.confs[key] = Config(config)
        self.names[key] = name
        return key


@pytest.mark.asyncio
async def test_native_enrollment_retries_without_new_tenant(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "private_root", lambda: tmp_path)
    store = Tenants(tmp_path / "tenant.db")
    manager = Manager()
    loaded = {}

    async def load(config):
        loaded[config["id"]] = SimpleNamespace(
            config=config, required_plugin=config["telegram_required_plugin"]
        )

    context = SimpleNamespace(
        astrbot_config_mgr=manager,
        get_platform_inst=lambda key: loaded.get(key),
        platform_manager=SimpleNamespace(load_platform=AsyncMock(side_effect=load)),
    )
    from unittest.mock import Mock

    plugin = SimpleNamespace(tenants=store, context=context, bind_customers=Mock())
    enroll = Enrollment(plugin)
    try:
        store.grant("admin", 1)
        tenant = store.enroll(1, 22, "testbot", enroll.cipher.encrypt(b"test-token"))
        await enroll.activate("22")
        await enroll.activate("22")
        assert len(manager.confs) == 1
        assert len(manager.default_conf["platform"]) == 1
        assert context.platform_manager.load_platform.await_count == 1
        conf = manager.confs["1"]
        assert not conf["provider_settings"]["enable"]
        assert conf["plugin_set"] == [adapter.NAME]
        assert not conf["admins_id"]
        assert isolated_profile(conf)
        entry = manager.default_conf["platform"][0]
        assert entry["telegram_required_plugin"] == adapter.NAME
        assert entry["telegram_command_register"] is False
        assert entry["telegram_command_auto_refresh"] is False
        assert entry["telegram_dedicated_reporting"] is True
        assert entry["telegram_allowed_updates"] == [
            "message",
            "callback_query",
            "my_chat_member",
        ]
        assert not store.summary(tenant)["enabled"]
        conf["provider_settings"]["enable"] = True
        with pytest.raises(Denied):
            await enroll.activate("22")
    finally:
        store.close()


@pytest.mark.parametrize(
    "path,value",
    [
        (("admins_id",), ["1"]),
        (("provider_settings", "enable"), True),
        (("plugin_set",), ["*"]),
        (("disable_builtin_commands",), False),
        (("kb_names",), ["private-kb"]),
        (("dashboard", "enable"), True),
    ],
)
def test_customer_profile_cannot_gain_ai_or_admin_features(path, value):
    from astrbot.core.config.default import DEFAULT_CONFIG

    conf = copy.deepcopy(DEFAULT_CONFIG)
    conf["admins_id"] = []
    conf["provider_settings"]["enable"] = False
    conf["plugin_set"] = [adapter.NAME]
    conf["disable_builtin_commands"] = True
    conf["kb_names"] = []
    conf["dashboard"]["enable"] = False
    assert isolated_profile(conf)
    changed = conf
    for name in path[:-1]:
        changed = changed[name]
    changed[path[-1]] = value
    assert not isolated_profile(conf)


@pytest.mark.asyncio
async def test_reactivation_rejects_legacy_platform_until_migrated(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(registry, "private_root", lambda: tmp_path)
    store = Tenants(tmp_path / "tenant.db")
    manager = Manager()
    platform = None
    context = SimpleNamespace(
        astrbot_config_mgr=manager,
        get_platform_inst=lambda key: platform,
        platform_manager=SimpleNamespace(load_platform=AsyncMock()),
    )
    plugin = SimpleNamespace(
        tenants=store, context=context, bind_customers=lambda: None
    )
    enroll = Enrollment(plugin)
    try:
        store.grant("admin", 1)
        store.enroll(1, 22, "testbot", enroll.cipher.encrypt(b"test-token"))
        await enroll.activate("22")
        entry = manager.default_conf["platform"][0]
        entry.pop("telegram_required_plugin")
        with pytest.raises(Denied, match="migration"):
            await enroll.activate("22")
        entry["telegram_required_plugin"] = adapter.NAME
        platform = SimpleNamespace(
            required_plugin="",
            config={**entry, "telegram_dedicated_reporting": False},
        )
        with pytest.raises(Denied, match="restarted"):
            await enroll.activate("22")
    finally:
        store.close()


def test_customer_requests_do_not_grant_permissions(tmp_path):
    store = Tenants(tmp_path / "tenant.db")
    try:
        tenant = store.create_trial("admin", 1, 2, "AIClient_2")
        ticket = store.request("AIClient_2", 2, 1, "group", "-100123")
        assert store.summary(tenant)["groups"] == []
        assert not store.summary(tenant)["enabled"]
        store.review_request("admin", ticket, True)
        assert store.summary(tenant)["groups"] == ["-100123"]
        with pytest.raises(Denied):
            store.review_request("admin", ticket, True)
        renewal = store.request("AIClient_2", 2, 1, "renew")
        with pytest.raises(Denied):
            store.review_request("admin", renewal, True)
        store.review_request("admin", renewal, False)
        with pytest.raises(Denied):
            store.request("AIClient_2", 2, 99, "resume")
    finally:
        store.close()
