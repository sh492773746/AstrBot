import time
from types import SimpleNamespace

from astrbot.core.platform.manager import PlatformManager
from astrbot.core.platform.sources.wangshangliao.registration import registrations


def test_saved_bot_without_transport_has_explicit_status(monkeypatch):
    manager = SimpleNamespace(platform_insts=[], astrbot_config={"platform": [{"id": "bot", "type": "wangshangliao", "enable": True}]})
    monkeypatch.setattr(registrations, "transactions", {})
    result = PlatformManager.get_all_stats(manager)
    assert result["platforms"][0]["connection_state"] == "stopped"
    monkeypatch.setattr(registrations, "transactions", {"login": SimpleNamespace(instance="bot", state="authenticated", expires=time.monotonic() + 60)})
    result = PlatformManager.get_all_stats(manager)
    assert result["platforms"][0]["connection_state"] == "awaiting_save"
    assert result["summary"]["total"] == 1
