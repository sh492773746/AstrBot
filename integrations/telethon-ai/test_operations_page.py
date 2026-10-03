import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from data.plugins.astrbot_plugin_telethon_ai.main import TelethonAI
from data.plugins.astrbot_plugin_telethon_ai.policy import Gate
from data.plugins.astrbot_plugin_telethon_ai.tenants import Tenants


def test_account_ai_display_uses_resolved_astrbot_profile(monkeypatch):
    from data.plugins.astrbot_plugin_telethon_ai import adapter

    monkeypatch.setattr(adapter, "ACCOUNTS", {"one": 123})
    info = Mock(return_value={"id": "profile-one", "name": "Renamed profile"})
    profile = {
        "agent_runner": {
            "config": {
                "model": {"provider_id": "selected-model"},
                "persona": {"persona_id": "selected-persona"},
            }
        },
        "provider": [{"api_key": "not-for-status"}],
    }
    subject = SimpleNamespace(
        context=SimpleNamespace(
            get_platform_inst=lambda _: None,
            get_config=lambda origin=None: profile if origin else {"platform": []},
            astrbot_config_mgr=SimpleNamespace(get_conf_info=info),
        ),
        gate=SimpleNamespace(paused=lambda _: False),
    )
    result = TelethonAI.accounts(subject)[0]
    assert result["profile_id"] == "profile-one"
    assert result["profile_name"] == "Renamed profile"
    assert result["model"] == "selected-model"
    assert result["persona"] == "selected-persona"
    assert result["daily_limit"] is None
    assert result["quota_scope"] == "tenant_authorization"
    assert "not-for-status" not in json.dumps(result)
    info.assert_called_once_with("TelethonAI_one:GroupMessage:0")


def test_operations_summary_is_readonly_and_secret_free(tmp_path):
    store = Tenants(tmp_path / "tenant.db")
    gate = Gate(tmp_path / "gate.db")
    try:
        tenant = store.create_trial("admin", "123", "456", "AIClient_456")
        store.assign("admin", tenant, "one")
        subject = SimpleNamespace(
            accounts=lambda: [{"account": "one"}],
            tenants=store,
            gate=gate,
            control=SimpleNamespace(platform=None, application=None),
            customers={},
        )
        before = store.db.total_changes
        status = TelethonAI.operational_status(subject)
        assert status["tenant_count"] == 1
        assert status["accounts"][0]["tenant"] == tenant
        assert status["accounts"][0]["daily_used"] == 0
        assert status["tenants"][0]["remaining"] == status["tenants"][0]["budget"]
        assert not status["checks"]["telegram_hooks"]
        assert store.db.total_changes == before
        encoded = json.dumps(status)
        for forbidden in ("token_cipher", "api_hash", "session", "password"):
            assert forbidden not in encoded
    finally:
        store.close()
        gate.close()


def test_daily_status_rolls_over_without_resetting_tenant_budget(tmp_path, monkeypatch):
    import data.plugins.astrbot_plugin_telethon_ai.main as mod

    store = Tenants(tmp_path / "tenant.db")
    gate = Gate(tmp_path / "gate.db")
    try:
        tenant = store.create_trial("admin", "123", "456", "AIClient_456")
        store.assign("admin", tenant, "one")
        stamp = datetime(2026, 9, 30, 23, 59, 30, tzinfo=timezone.utc).timestamp()
        policy = {"daily_limit": 2, "cooldown_seconds": 10}
        assert gate.admit("one", -1, 1, policy, now=stamp)
        assert gate.admit("one", -1, 2, policy, now=stamp + 10)
        assert not gate.admit("one", -1, 2, policy, now=stamp + 20)
        subject = SimpleNamespace(
            accounts=lambda: [{"account": "one"}],
            tenants=store,
            gate=gate,
            control=SimpleNamespace(platform=None, application=None),
            customers={},
        )
        monkeypatch.setattr(mod.time, "time", lambda: stamp + 20)
        before = TelethonAI.operational_status(subject)
        assert before["accounts"][0]["daily_used"] == 2
        assert before["daily_window"] == {
            "day": "2026-09-30",
            "timezone": "UTC",
            "reset_at": stamp + 30,
        }
        monkeypatch.setattr(mod.time, "time", lambda: stamp + 30)
        after = TelethonAI.operational_status(subject)
        assert after["accounts"][0]["daily_used"] == 0
        assert after["daily_window"]["day"] == "2026-10-01"
        assert after["tenants"][0]["budget"] == before["tenants"][0]["budget"]
        assert after["tenants"][0]["used"] == before["tenants"][0]["used"]
        assert gate.admit("one", -1, 3, policy, now=stamp + 30)
        assert TelethonAI.operational_status(subject)["accounts"][0]["daily_used"] == 1
        assert gate.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 3
    finally:
        store.close()
        gate.close()


@pytest.mark.asyncio
async def test_status_denies_non_admin_before_reading_data():
    subject = SimpleNamespace(
        dashboard_admin=lambda: False,
        operational_status=lambda: pytest.fail("Unauthorized database access"),
    )
    response = await TelethonAI.page_status(subject)
    assert response.status_code == 403
