"""Public draw announcements use frozen terms without exposing identity suffixes."""

import copy
import json
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.activities import GroupActivities
from astrbot.builtin_stars.wangshangliao_moderation.activity_store import database
from tests.test_wangshangliao_activities import configure, event, latest
from tests.test_wangshangliao_activities import setup as setup


def frozen_round(**changes):
    """Build a representative frozen round without accessing a live account."""
    return {
        "prize": "群友福利",
        "packet_mode": "tiered",
        "entry_phrase": "一起参加",
        "starts": 0,
        "created": 1794031200,
        "ends": 1794204000,
        "duration": 3600,
        "draw_on_full": 0,
        "prize_tiers": json.dumps(
            [{"amount": 8800, "count": 5}, {"amount": 20000, "count": 1}]
        ),
        "total": 1003,
        "total_amount": 300000,
        "capacity": 30,
        "winners": 30,
        "packet_amounts": "[]",
        "luckiest_bonus": 0,
        "owner": "31846829",
        "owner_name": "",
        "contact": "活动管理员",
        "remarks": "领奖时请保留活动头像",
        **changes,
    }


def test_result_layout_contains_frozen_terms_and_names_in_screenshot_order():
    row = frozen_round()
    winners = [
        {"uid": "32146264", "peer": "9032146264", "name": "小明", "amount": 8800},
        {"uid": "36190414", "peer": "9036190414", "name": "小红", "amount": 20000},
    ]
    before = copy.deepcopy((row, winners))
    text, mentions = GroupActivities().result_text(row, winners)
    expected = [
        "群友福利",
        "活动类型：关键词抽奖（分档拼手气）",
        "参与关键词：一起参加",
        "开始时间：2026-11-07 14:00:00（北京时间）",
        "定时开奖：2026-11-09 14:00:00（北京时间）",
        "距离结束：已开奖",
        "奖品列表：",
        "200元现金红包 × 1",
        "88元现金红包 × 5",
        "随机红包 × 24",
        "已参与人数：1003",
        "活动开奖啦！",
        "总共参与：1003，综合中奖率0.20%",
        "恭喜以下用户中奖：",
        "@小红 获得奖品：200元现金红包",
        "@小明 获得奖品：88元现金红包",
    ]
    assert text.startswith("\n".join(expected) + "\n")
    assert "活动备注：\n领奖时请保留活动头像" in text
    assert "联系 活动管理员 领取奖品。" in text
    assert "以上红包均待发放" in text
    assert "UID" not in text and "抽奖创建者" not in text
    for identity in ("32146264", "36190414", "9032146264", "9036190414", "31846829"):
        assert identity not in text
    assert [mention["uid"] for mention in mentions] == [9036190414, 9032146264]
    assert (row, winners) == before


@pytest.mark.parametrize(
    "name", [None, "", "32146264", "9032146264", "３２１４６２６４"]
)
def test_missing_or_identity_only_nickname_never_falls_back_to_internal_id(name):
    row = frozen_round()
    winners = [{"uid": "32146264", "peer": "9032146264", "name": name, "amount": 1}]
    text, mentions = GroupActivities().result_text(row, winners)
    assert "未获取昵称 获得奖品：0.01元现金红包" in text
    assert "32146264" not in text and "9032146264" not in text
    assert "３２１４６２６４" not in text and not mentions


@pytest.mark.parametrize("mode", ["ordinary", "lucky", "tiered", "prize"])
@pytest.mark.parametrize("full", [False, True])
def test_packet_modes_and_full_draws_keep_correct_conditions_and_zero_rate(mode, full):
    row = frozen_round(packet_mode=mode, draw_on_full=int(full), total=0)
    text, mentions = GroupActivities().result_text(row, [])
    assert "已参与人数：0" in text and "综合中奖率0.00%" in text
    assert "暂无中奖用户。" in text and "恭喜以下用户中奖" not in text
    assert not mentions
    if full:
        assert "开奖条件：报满30人" in text and "定时开奖" not in text
    else:
        assert "定时开奖：" in text and "开奖条件：报满" not in text
    if mode == "prize":
        assert "群友福利 × 30" in text and "现金红包" not in text
    elif mode == "tiered":
        assert "200元现金红包 × 1" in text and "随机红包 × 24" in text
    else:
        assert "3000元，共30份" in text


def test_scheduled_start_does_not_shift_after_deadline_extension():
    row = frozen_round(starts=1794117600, ends=1794290400)
    text, _ = GroupActivities().result_text(row, [])
    assert "开始时间：2026-11-08 14:00:00（北京时间）" in text
    assert "定时开奖：2026-11-10 14:00:00（北京时间）" in text


@pytest.mark.asyncio
async def test_draw_publishes_once_and_history_reuses_frozen_layout(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET prize='本期福利',entry_phrase='一起参加',"
            "packet_mode='tiered',total_amount=10000,"
            "prize_tiers=?,remarks='本期参与要求'",
            (json.dumps([{"amount": 5000, "count": 1}]),),
        )
    await service.command(caller, "5", "开启抽奖", "")
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    with database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET prize='下期名称',remarks='下期备注',"
            "entry_phrase='下期口令'"
        )
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == "finished"
    sent = adapter.send_reply_text.await_args
    text = sent.args[2]
    assert text.startswith("本期福利\n活动类型：关键词抽奖（分档拼手气）")
    assert "参与关键词：一起参加" in text and "本期参与要求" in text
    assert "下期" not in text and "50元现金红包 × 1" in text
    assert "退回（未支出）" in text and "待发放" in text
    assert sent.kwargs["auto_recall"] is False and sent.kwargs["proactive"] is True
    adapter.get_moderation_members = AsyncMock(
        side_effect=AssertionError("Result display must not query a member directory")
    )
    assert await service.command(caller, "5", "中奖名单", "") == text
    await service.tick(adapter)
    assert latest(adapter) == row and adapter.send_reply_text.await_count == 2
