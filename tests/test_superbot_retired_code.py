"""Keep retired external classifiers outside the production plugin package."""

import ast
from pathlib import Path


def test_production_plugin_has_no_retired_classifier_dependency():
    plugin = (
        Path(__file__).resolve().parents[1] / "data/plugins/astrbot_plugin_superbot"
    )
    assert not (plugin / "ad_ai.py").exists()
    assert not (plugin / "ad_ai_engine.py").exists()
    for source in plugin.glob("*.py"):
        text = source.read_text()
        assert "jev.bocha.cn" not in text, source.name
        assert "superbot_legacy" not in text, source.name
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"ad_ai", "ad_ai_engine"}, source.name


def test_legacy_cleanup_remains_registered_without_countdown_scheduler():
    plugin = (
        Path(__file__).resolve().parents[1] / "data/plugins/astrbot_plugin_superbot"
    )
    text = (plugin / "main.py").read_text()
    assert "self.legacy_broadcast_cleanup_loop()" in text
    assert "self.group_game.countdown_tick()" in text
    assert "game_countdown_loop" not in text
