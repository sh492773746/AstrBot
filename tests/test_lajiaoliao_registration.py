"""The retired core endpoint must not retain a global bridge connection."""

import inspect
from pathlib import Path

from astrbot.dashboard.services.platform_service import PlatformService


def test_lajiaoliao_registration_is_plugin_owned():
    assert not hasattr(PlatformService, "_handle_lajiaoliao_registration")
    assert "lajiaoliao" not in inspect.getsource(PlatformService.handle_platform_registration)


def test_plugin_exposes_only_management_page_and_no_separate_login_route():
    root = Path(__file__).resolve().parents[1] / "data/plugins/astrbot_plugin_lajiaoliao"
    pages = {path.name for path in (root / "pages").iterdir() if (path / "index.html").is_file()}
    assert pages == {"management"}
    from data.plugins.astrbot_plugin_lajiaoliao.main import LajiaoLiaoPlugin

    assert not hasattr(LajiaoLiaoPlugin, "authorize")
    assert not hasattr(LajiaoLiaoPlugin, "bind")
