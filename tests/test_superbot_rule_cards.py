"""Rule text uses escaped, bounded native expandable sections."""

from pathlib import Path

from data.plugins.astrbot_plugin_superbot.rich_text import PlainHTML, cards


def test_rules_native_sections_and_bounds():
    text = Path("data/plugins/astrbot_plugin_superbot/docs/player-help.md").read_text()
    pages = cards(text, fold_sections=True)
    assert len(pages) == 1
    for plain, html in pages:
        parser = PlainHTML()
        parser.feed(html)
        assert "".join(parser.parts) == plain
        assert "<blockquote expandable>" in html
        assert "<b>" in html
        assert len(html.encode("utf-16-le")) // 2 <= 3800
    assert "50项" in text
    assert "512字符" in text
    assert "筹码" in text
    assert "单人／多人" in text
    assert "各选投入" in text
    assert "最多追加5轮" in text
    assert "没有看到回复不代表未扣分" in text
    assert "快三历史／快三流水／快三输赢" in text


def test_rules_escape_markup_and_leave_other_cards_unchanged():
    _, html = cards("标题\n<script> & 用户", fold_sections=True)[0]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<blockquote" not in cards("普通\n文本")[0][1]
