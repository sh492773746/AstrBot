"""Player-facing help must not render operational documentation."""

from pathlib import Path

import pytest
from test_superbot_community import community, private  # noqa: F401
from test_superbot_game_hardening import flow  # noqa: F401
from test_superbot_group_flows import group_update
from test_superbot_moderation import setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.asyncio
async def test_group_and_private_help_exclude_internal_docs(flow):  # noqa: F811
    s = flow
    for call in (
        lambda: s.flow.action(group_update(), {"action": "play"}),
        lambda: s.runtime.ui.action(private(2), {"action": "rules"}),
    ):
        s.bot.send_message.reset_mock()
        await call()
        text = "\n".join(c.kwargs["text"] for c in s.bot.send_message.await_args_list)
        assert "/play" in text and "特殊回本" in text
        assert "<blockquote expandable>" in text and "<b>" in text
        assert "50项" in text and "双人挑战" in text
        for forbidden in (
            "tests/",
            "SQLite",
            "Ruff",
            "隔离验收",
            "维护",
            "gb_",
            "部署",
            "回滚",
        ):
            assert forbidden not in text


def test_provision_imports_public_documents_only():
    root = Path(__file__).parents[1] / "data/plugins/astrbot_plugin_superbot"
    source = (root / "provision.py").read_text()
    assert '("player-help.md", "player-services.md")' in source
    assert '("knowledge.md", "game-rules.md", "moderation.md")' not in source


def test_public_service_knowledge_covers_current_boundaries():
    root = Path(__file__).parents[1] / "data/plugins/astrbot_plugin_superbot"
    text = (root / "docs/player-services.md").read_text()
    for expected in (
        "USDT-TRC20",
        "6位小数",
        "等待人工退款",
        "发布结果待核查",
        "以“大海传媒”开头",
        "累计两张",
        "当前成员",
        "至少5个",
        "群内 AI",
        "公开可见",
        "不排队补执行",
        "/report",
        "不能恢复",
        "平台彩金",
    ):
        assert expected in text
    for forbidden in (
        "SQLite",
        "Bocha",
        "SAVNX",
        "sk-",
        "jwt_secret",
        "superbot.sqlite3",
    ):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_private_catalog_is_guidance_only(flow):  # noqa: F811
    ui = flow.runtime.ui
    labels = [button.text for row in ui.keyboard("2").keyboard for button in row]
    assert "玩法大全" in labels and "加拿大28" not in labels
    for action in ("game",):
        flow.bot.send_message.reset_mock()
        await ui.action(private(2), {"action": action})
        text = "\n".join(
            c.kwargs["text"] for c in flow.bot.send_message.await_args_list
        )
        assert "私聊不能下注、开桌或抽奖" in text
        assert "加拿大28" in text and "老虎机PvP" in text
        assert "积分快三" in text and "两房间互斥" in text
        assert "同桌同额" in text and "每人独立选档" in text
        assert "5连抽和10连抽" in text and text.count("抽水10%") == 2
        assert "每人选择自己的投入" not in text and "全奖池" not in text
        assert "当前第" not in text
    for action in ("room", "numbers", "bet_submit", "chase_submit"):
        with pytest.raises(Rejected, match="群里"):
            await ui.action(private(2), {"action": action})
