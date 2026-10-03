from pathlib import Path


def test_page_does_not_wait_forever_for_bridge():
    source = (
        Path(__file__).resolve().parents[2]
        / "data/plugins/astrbot_plugin_telethon_ai/pages/accounts/app.js"
    ).read_text()
    assert "8000" in source
    assert "插件桥接未建立" in source
    assert "refresh();" in source
    assert "clearTimeout(timer)" in source
