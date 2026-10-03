"""Keep the main documentation command inventory aligned with the parser."""

from pathlib import Path

from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    ALIASES,
    NO_ARGUMENT,
    WITH_ARGUMENT,
)

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_readme_covers_every_canonical_command():
    text = (
        ROOT / "astrbot/builtin_stars/wangshangliao_moderation/README.md"
    ).read_text()
    inventory = text.split("## 完整命令索引", 1)[1].split("### 数值范围", 1)[0]
    canonical = {ALIASES.get(name, name) for name in NO_ARGUMENT | WITH_ARGUMENT}
    for command in canonical:
        assert f"`{command}`" in inventory or f"`{command} " in inventory, command


def test_current_docs_separate_historical_evidence():
    text = (ROOT / "docs/zh/platform/wangshangliao-testing.md").read_text()
    assert "当次未执行" in text
    assert "发送测试失败" in text
    history = (ROOT / "docs/zh/platform/wangshangliao-testing-history.md").read_text()
    assert "历史归档" in history
    assert "版本日期：2026-09-19" in history
