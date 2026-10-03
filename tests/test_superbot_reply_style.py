"""Rendered Telegram entities preserve literal user text and UTF-16 offsets."""

from telegram import MessageEntity

from data.plugins.astrbot_plugin_superbot.reply_style import style_reply


def test_heading_and_amounts_with_non_bmp_recipient():
    text, entities = style_reply(
        "第 123 期 · 已受理\n大单 100\n合计扣分：100 · 剩余积分：900",
        "accepted",
        "😀用户\n",
    )
    encoded = text.encode("utf-16-le")
    slices = [
        encoded[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le")
        for e in entities
    ]
    assert slices == ["✅ 第 123 期 · 已受理", "100", "900"]
    assert all(e.type == MessageEntity.BOLD for e in entities)


def test_untrusted_markup_stays_literal():
    text, entities = style_reply(
        "结算名单\n<b>昵称</b> · 投注10 · 返还20", "settlement"
    )
    assert "<b>昵称</b>" in text
    assert len(entities) == 3


def test_empty_text():
    assert style_reply("", "points", "@user\n") == ("@user\n", [])


def test_duel_native_folding_and_bold_with_emoji():
    text, entities = style_reply(
        "双人挑战\n🎁 您的筹码：“😀唱歌”\n🎯 您的玩法：大\n🏆 结果：甲胜\n仅供娱乐",
        "duel_result",
        "玩家\n",
    )

    def content(entity):
        data = text.encode("utf-16-le")
        return data[entity.offset * 2 : (entity.offset + entity.length) * 2].decode(
            "utf-16-le"
        )

    assert [content(e) for e in entities if e.type == "expandable_blockquote"] == [
        "🎁 您的筹码：“😀唱歌”",
        "仅供娱乐",
    ]
    assert "🏆 结果：甲胜" in [
        content(e) for e in entities if e.type == MessageEntity.BOLD
    ]


def test_empty_flow_keeps_summary_visible():
    _, entities = style_reply(
        "本群近期流水\n暂无已结算期次\n\n今日统计\n投注0 · 返还0", "flow", "😀用户\n"
    )
    assert not any(e.type == "expandable_blockquote" for e in entities)


def test_flow_fold_utf16_excludes_daily_summary():
    text, entities = style_reply(
        "本群近期流水\n第 123 期 · 投注10 · 返还20\n\n今日统计\n净变动+10",
        "flow",
        "😀用户\n",
    )
    fold = next(e for e in entities if e.type == "expandable_blockquote")
    assert (
        text.encode("utf-16-le")[
            fold.offset * 2 : (fold.offset + fold.length) * 2
        ].decode("utf-16-le")
        == "第 123 期 · 投注10 · 返还20"
    )


def test_fold_excludes_summary_and_preserves_utf16():
    text, entities = style_reply(
        "结算名单\n😀甲 · 投注30\n  大10 · 中奖\n  单10 · 未中奖\n  小10 · 回本\n乙 · 投注10\n  大10 · 中奖",
        "settlement",
        fold_lines=[2, 3, 4],
    )
    folds = [e for e in entities if e.type == "expandable_blockquote"]
    assert len(folds) == 1
    entity = folds[0]
    selected = text.encode("utf-16-le")[
        entity.offset * 2 : (entity.offset + entity.length) * 2
    ].decode("utf-16-le")
    assert selected == "  大10 · 中奖\n  单10 · 未中奖\n  小10 · 回本"
