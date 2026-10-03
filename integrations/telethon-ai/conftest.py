import importlib

import pytest


@pytest.fixture(autouse=True)
def isolated_accounts(monkeypatch):
    adapter = importlib.import_module("data.plugins.astrbot_plugin_telethon_ai.adapter")
    monkeypatch.setattr(
        adapter, "ACCOUNTS", {"collector": 1000000002, "keywords": 1000000003}
    )
