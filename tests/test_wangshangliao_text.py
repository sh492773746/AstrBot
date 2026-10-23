from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.api.message_components import At, Plain
from astrbot.builtin_stars.wangshangliao_moderation.main import Main
from astrbot.builtin_stars.wangshangliao_moderation.text import (
    PLAIN_TEXT_INSTRUCTION,
    format_group_command_reply,
    plain_text,
    redact_reply,
    strip_reasoning_markup,
)


def test_reply_redaction_preserves_management_identifiers():
    text = '密码: "test password"\n验证码：123456\nAPI Key=sk-testabcdefghijklmnop\nBearer abc.def\n电话 13812345678 邮箱 tester@example.com\nUID: 23691273 群: 1143980 操作ID: command/abc123'
    result = redact_reply(text)
    for secret in ['test password', '123456\n', 'sk-testabcdefghijklmnop', 'abc.def', '13812345678', 'tester@example.com']:
        assert secret not in result
    assert '138****5678' in result
    assert 't***@example.com' in result
    assert 'UID: 23691273 群: 1143980 操作ID: command/abc123' in result
    assert 'token=secretvalue' not in redact_reply('https://example.com/?token=secretvalue&target=1143980')
    assert 'target=1143980' in redact_reply('https://example.com/?token=secretvalue&target=1143980')
    assert redact_reply(result) == result


def test_secret_in_code_is_redacted_after_plain_text():
    result = redact_reply(plain_text('```\npassword="synthetic value"\n```'))
    assert 'synthetic value' not in result
    assert '[REDACTED]' in result


def test_markdown_plain_text():
    assert plain_text("# 标题\n\n**重点** [链接](https://example.com/a_b)") == "【标题】\n\n重点 链接 (https://example.com/a_b)"
    assert "a_b = '**keep**'" in plain_text("```python\na_b = '**keep**'\n```")
    assert plain_text("普通文本\n下一行") == "普通文本\n下一行"
    table = plain_text("| 名称 | 值 |\n| --- | --- |\n| A | 1 |")
    assert "名称" in table and "A  1" in table and "|" not in table
    assert "1. 一" in plain_text("1. 一\n2. 二")


def test_reasoning_markup_is_not_sent_to_wangshangliao():
    text = (
        "<thought>\n内部判断，不应发送\n</thought>\n"
        "最终回复：可以正常咨询。"
    )
    assert strip_reasoning_markup(text) == "最终回复：可以正常咨询。"
    assert plain_text(text) == "最终回复：可以正常咨询。"
    assert redact_reply(text) == "最终回复：可以正常咨询。"


def test_truncated_or_orphan_reasoning_markup_is_hidden():
    assert strip_reasoning_markup("<thought>内部判断") == ""
    assert strip_reasoning_markup("最终回复</thought>") == "最终回复"
    assert strip_reasoning_markup("<think>判断</think>答复") == "答复"
    assert strip_reasoning_markup("普通文本 <analysis>说明</analysis>") == "普通文本"


def test_group_command_reply_uses_one_semantic_emoji():
    assert format_group_command_reply("参加抽奖", "参加抽奖成功。") == "🎉 参加抽奖成功。"
    assert format_group_command_reply("禁言 @用户 3", "已禁言 3 分钟。") == "🔇 已禁言 3 分钟。"
    assert format_group_command_reply("参加抽奖", "尚未开启抽奖。") == "⚠️ 尚未开启抽奖。"
    assert format_group_command_reply("禁言 @用户", "🚫 无权限") == "🚫 无权限"
    assert (
        format_group_command_reply("抽奖状态", "🎁 当前没有进行中的抽奖。")
        == "🎁 当前没有进行中的抽奖。"
    )
    assert format_group_command_reply("未知命令", "无法识别。") == "无法识别。"


@pytest.mark.parametrize(
    "reply",
    [
        "报名消息已过期，请在活动期间重新发送“参加抽奖”。",
        "报名已截止。",
        "本次报名名额已满。",
        "本次需有效邀请3人；你当前为0人。",
        "成员身份未确认，不能参加抽奖。",
        "机器人账号不参与抽奖。",
    ],
)
def test_lottery_rejection_does_not_use_success_marker(reply):
    assert format_group_command_reply("参加抽奖", reply) == "⚠️ " + reply


def test_duplicate_lottery_signup_is_informational_not_a_new_success():
    reply = "你已参加本次抽奖，请勿重复报名。"
    assert format_group_command_reply("参加抽奖", reply) == "📋 " + reply


@pytest.mark.asyncio
async def test_scoped_prompt_and_components():
    req = SimpleNamespace(system_prompt="Original persona", func_tool=None)
    mention = At(qq="123")
    result = SimpleNamespace(chain=[mention, Plain("**回答**")], is_llm_result=lambda: True, use_t2i=Mock())
    event = SimpleNamespace(get_platform_name=lambda: "wangshangliao", get_result=lambda: result,
                            get_extra=lambda _: None, is_private_chat=lambda: True, is_admin=lambda: False,
                            platform=SimpleNamespace(config={"id": "test"}))
    await Main.plain_text_request(None, event, req)
    await Main.plain_text_request(None, event, req)
    assert req.system_prompt.startswith("Original persona")
    assert req.system_prompt.count(PLAIN_TEXT_INSTRUCTION) == 1
    await Main.plain_text_result(None, event)
    assert result.chain[0] is mention
    assert result.chain[1].text == "回答"
    result.use_t2i.assert_called_once_with(False)
    event.get_platform_name = lambda: "telegram"
    result.chain[1].text = "**unchanged**"
    await Main.plain_text_result(None, event)
    assert result.chain[1].text == "**unchanged**"


@pytest.mark.asyncio
async def test_verified_activity_display_replaces_model_claims():
    result = SimpleNamespace(
        chain=[Plain("立即开奖成功")], is_llm_result=lambda: True, use_t2i=Mock()
    )
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao", get_result=lambda: result,
        get_extra=lambda key: "确认后开启报名，1分钟到时开奖" if key == "wsl_config_display" else None,
    )
    await Main.plain_text_result(None, event)
    assert len(result.chain) == 1
    assert result.chain[0].text == "确认后开启报名，1分钟到时开奖"


@pytest.mark.asyncio
async def test_group_prompt_allows_sparse_emoji():
    req = SimpleNamespace(system_prompt="Original persona", func_tool=None)
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        get_extra=lambda _: None,
        is_private_chat=lambda: False,
        is_admin=lambda: False,
        platform=SimpleNamespace(config={"id": "test"}),
    )
    await Main.plain_text_request(None, event, req)
    await Main.plain_text_request(None, event, req)
    assert "少量 Emoji" in req.system_prompt
    assert "处罚说明保持克制、中性" in req.system_prompt
    assert req.system_prompt.count("群聊回复可按语境使用少量 Emoji") == 1
