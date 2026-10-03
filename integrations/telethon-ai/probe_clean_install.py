"""Run only inside the isolated upstream checkout, with networking disabled."""

import asyncio
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(os.environ["ASTRBOT_ROOT"]).resolve()
assert ROOT.name == "isolated" and "astrbot-compat" in str(ROOT)


async def main():
    report = {"network": "disabled by unshare", "production_credentials": False}
    import astrbot
    from astrbot.core.event_bus import EventBus
    from astrbot.core.platform.sources.telegram.tg_adapter import (
        TelegramPlatformAdapter,
    )

    report["astrbot_source"] = str(Path(astrbot.__file__).resolve())
    assert "astrbot-compat" in report["astrbot_source"]
    report["interfaces"] = {
        name: callable(getattr(TelegramPlatformAdapter, name, None))
        for name in (
            "register_application_hook",
            "unregister_application_hook",
            "suspend_required_plugin",
        )
    }
    report["interfaces"]["lazy_pipeline"] = callable(
        getattr(EventBus, "ensure_scheduler", None)
    )
    try:
        module = importlib.import_module("data.plugins.astrbot_plugin_telethon_ai.main")
        report["plugin_import"] = "passed"
    except Exception as exc:
        report["plugin_import"] = {
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
        return report

    platform = TelegramPlatformAdapter(
        {
            "id": "VIP_DHBot",
            "type": "telegram",
            "enable": False,
            "telegram_token": "123456789:isolated-placeholder-not-a-real-token",
        },
        {},
        asyncio.Queue(),
    )
    context = SimpleNamespace(
        get_platform_inst=lambda platform_id: (
            platform if platform_id == "VIP_DHBot" else None
        ),
        register_web_api=lambda *args: None,
    )
    plugin = module.TelethonAI(
        context,
        {
            "provision_profiles": False,
            "control_platform_id": "VIP_DHBot",
            "controller_mode": "standalone",
            "admin_ids": [],
        },
    )
    try:
        await plugin.initialize()
        report["controller_initialization"] = "passed"
    except Exception as exc:
        report["controller_initialization"] = {
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
    finally:
        plugin.gate.close()
        plugin.tenants.close()
    return report


if __name__ == "__main__":
    os.umask(0o077)
    result = asyncio.run(main())
    (ROOT / "compatibility-result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
