"""Offline tests for business rules and private human kick confirmation."""

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import commands
from astrbot.builtin_stars.wangshangliao_moderation import content_rules as rules
from astrbot.core.platform.sources.wangshangliao import automatic
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.fixtures.wsl_actions import bind_action_port


@pytest.mark.parametrize(
    "text,expected",
    [
        ("加我vx:abc123456", "external_promotion"),
        ("加我ＶＸ：ａｂｃ１２３４５６", "external_promotion"),
        ("加我v\u200bx:abc123456", "external_promotion"),
        ("首次充值送30% example.vip", "external_promotion"),
        ("首次充值送30% example.com", "external_promotion"),
        ("首次充值送30% a.example.cc", "external_promotion"),
        ("首次充值送30% a.cn", "external_promotion"),
        ("首次充值送30% " + "a" * 63 + ".net", "external_promotion"),
        ("3p大秀直播 至尊群:12345678", "adult_promotion"),
        ("https://example.vip", ""),
        ("12345678", ""),
        ("微信怎么使用", ""),
        ("举报骗子发的 加我vx:abc123456", ""),
        ("复制链接去浏览器", ""),
    ],
)
def test_classification(text, expected):
    assert rules.classify(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("所有人支付宝微信银行卡能收款的截图加我12345678", "external_promotion"),
        ("有快手号的来找我48小时给288🧧旺12345678", "external_promotion"),
        ("有快手号的来找我48小时给288🧧", "external_promotion"),
        ("微信附近的人一小时60秒结账想做的老哥加我", "external_promotion"),
        ("网红视频黑历史想看都加Q12345678", "external_promotion"),
        ("网红视频黑历史想看都在这里", "external_promotion"),
        ("网红视频全部都是在这里", "external_promotion"),
        ("想看各种网红黑历史加我", "external_promotion"),
        ("来几个挂壁仔做项目 一单10-15分钟即可完成 支持担保", "external_promotion"),
        ("来几个16岁以上的挂壁仔 没钱的联系我 支持担保", "external_promotion"),
        ("来几个没钱的做项目 一单10-15分钟即可完成 支持担保", "external_promotion"),
        ("免废劈图上分", "external_promotion"),
        ("免费P图上分", "external_promotion"),
        ("免菲批图上分", "external_promotion"),
        ("免费上分", "external_promotion"),
        ("找我免费上分", "external_promotion"),
        ("免\u200b费 上 分", "external_promotion"),
        ("手痒想干 没米上分的来找我", "external_promotion"),
        ("缺钱上分私聊我", "external_promotion"),
        ("１２３４５６７８免费领取付费预测每天不定时抽奖", "external_promotion"),
        ("进群12345678找管理拿38.8的预测", "external_promotion"),
        ("洗💰的来", "external_promotion"),
        ("加我送88", "external_promotion"),
        ("进群看P", "adult_promotion"),
        ("纯爱老师被调教 进群12345678", "adult_promotion"),
        ("纯爱老师被调教 进12345678", "adult_promotion"),
        (
            "有快手号的来找我48小时给288🧧".replace("来", "\u200b来"),
            "external_promotion",
        ),
        (
            "自媒体评论推广扶持，支持赛事中途下注，联系合营招商旺旺：12345678",
            "external_promotion",
        ),
        (
            "自媒体评论推广扶持，支持赛事中途下注，联系合营招商旺旺：\n🔍 12345678",
            "external_promotion",
        ),
        ("注册就送体验金38", "external_promotion"),
        ("新用户注册即可赠送38.8元彩金！", "external_promotion"),
        ("注册即送彩金38U", "external_promotion"),
        ("注\u200b册 就 送 体验金３８", "external_promotion"),
        ("做单+（12345678）", "external_promotion"),
        ("接单加我：12345678", "external_promotion"),
        ("微信附近的人1小时100", "external_promotion"),
        ("微信附近的人每小时100元秒结账", "external_promotion"),
        (
            "做项目 聊天 加拿大\n旺旺不禁言超级群12345678\n"
            "做单透明 诚信可靠\n进群联系管理员做项目",
            "external_promotion",
        ),
        ("项目群：12345678 加群后联系管理，做单", "external_promotion"),
        (
            "传媒88广告费，每轮抽奖前需分享文案到3个千人群+朋友圈1条，"
            "今晚9点开播，🌶️聊直播群：12345678",
            "external_promotion",
        ),
        (
            "广告费：88 转发到3个群 直播群：１２３４５６７８",
            "external_promotion",
        ),
        (
            "嘉汪12345678各种赔率都有需要滴我，捧场安排茶水待遇拉满",
            "external_promotion",
        ),
        ("旺商聊：12345678 各种赔率都有，私聊我", "external_promotion"),
    ],
)
def test_recalled_advertising_combinations_apply_without_custom_keywords(
    text, expected
):
    assert rules.classify(text) == expected
    assert rules.keyword_category(text, {"mute_keywords": []}) == ""


@pytest.mark.parametrize(
    "text",
    [
        "所有人支付宝微信银行卡能收款的截图加我12345678",
        "有快手号的来找我48小时给288",
        "微信附近的人一小时60秒结账想做的老哥加我",
        "网红视频黑历史想看都在这里",
        "来几个挂壁仔做项目 一单10-15分钟即可完成 支持担保",
        "免废劈图上分",
        "免费上分",
        "手痒想干 没米上分的来找我",
        "12345678免费领取付费预测",
        "洗💰的来",
        "加我送88",
        "进群看p",
        "注册就送体验金38",
        "做单+（12345678）",
        "微信附近的人1小时100",
        "超级群12345678 进群联系管理员做项目",
        "广告费88 分享文案到3个千人群 直播群：12345678",
        "嘉汪12345678各种赔率都有需要滴我，安排茶水",
    ],
)
@pytest.mark.parametrize("prefix", ["举报：", "警惕", "引用：", "转发："])
def test_recall_derived_combinations_do_not_punish_reports_or_quotations(text, prefix):
    assert rules.classify(prefix + text) == ""


@pytest.mark.parametrize(
    "text",
    [
        "服务器崩盘了",
        "别撤回啊",
        "28",
        "12345678",
        "https://example.invalid",
        "关注一波",
        "求扶持",
        "38体验金不是很正常吗？",
        "来几个同事做项目，十分钟开会",
        "做项目需要担保吗",
        "我有快手号，但忘了密码",
        "有快手号的来找我核对登录问题",
        "微信附近的人功能在哪里",
        "微信附近的人一小时60元是什么",
        "支付宝收款截图发给结账员核对",
        "所有人核对账本里的支付宝收款",
        "网红视频怎么剪辑",
        "网红黑历史是什么意思",
        "我想看网红视频的拍摄教程",
        "大家进群看PPT",
        "进群看片会的安排",
        "洗钱的来龙去脉应该弄清楚",
        "免费的预测天气网站",
        "进群12345678讨论天气预测",
        "合营招商是什么意思",
        "加我送一下88号文件",
        "我先上分再改头像，不需要P图",
        "免费上分是什么意思",
        "谁说免费上分是真的",
        "我没钱上分，先不玩了",
        "手痒想干活，来找我搬文件",
        "开播",
        "236666",
        "注册送体验金38是真的吗？",
        "注册就送38元体验金是什么意思",
        "注册就送学习资料38页",
        "注册已经完成，体验金38还没到账",
        "做单+（12345678）这个算广告吗？",
        "做单号12345678已核对",
        "做单+12345678=多少",
        "微信附近的人1小时100是什么意思",
        "微信附近的人每小时100元是真的吗？",
        "超级群12345678",
        "进群联系管理员做项目",
        "项目群12345678，今晚开会讨论项目",
        "这是直播群12345678，明天讨论广告费预算",
        "广告费88，请分享文案到3个千人群",
        "分享文案到3个千人群，直播群12345678",
        "广告费88，直播群12345678",
        "嘉汪12345678",
        "赔率是什么意思，怎么计算",
        "各种赔率都有需要滴我",
        "嘉汪12345678，各种赔率怎么算？",
        "加拿大留学生，今天聊点什么",
        "好想学做项目，先把管理员的文档看完",
    ],
)
def test_recall_derived_rules_keep_ordinary_topics_and_contacts(text):
    assert rules.classify(text) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["solicitation", "broadcast"])
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "sender",
        "group",
        "account",
        "revision",
        "test_window",
        "historical",
        "delayed_current",
        "recalled",
        "wrong_current",
        "single_number",
        "report",
        "missing_time",
        "closed_database",
        "revoked_during_read",
        "receiver_changed",
        "advertising_changed",
        "reordered_source",
        "reordered_repeat",
    ],
)
async def test_split_contact_ads_require_scoped_live_native_evidence(
    tmp_path, monkeypatch, style, change
):
    now = time.time()
    revision = "1/5/4/2"
    payload = {
        "sender": "2",
        "text": "236666",
        "recall_route": {
            "time": int((now - 6 if change == "reordered_source" else now) * 1000)
        },
        "_open_revision": revision,
    }
    adapter = SimpleNamespace(
        account="1",
        config={
            "_open_revisions": {"5": revision},
            "moderation": {"content_rules_since": now - 300, "flood_enabled": False},
        },
    )
    async with aiosqlite.connect(tmp_path / "inbox.db") as db:
        await db.executescript("""
            CREATE TABLE inbox(account,team,message,payload,state,received_at,sender,
                PRIMARY KEY(account,team,message));
            CREATE INDEX inbox_ranking_window ON inbox(account,team,received_at);
        """)
        evidence = [
            (
                now - 4,
                "手痒想干 没米上分的来找我" if style == "solicitation" else "开播",
            ),
            (now - 2, "236666"),
        ]
        if change == "report":
            evidence[0] = (now - 4, "举报：" + evidence[0][1])
        if change == "single_number":
            evidence = []
        for index, (observed, text) in enumerate(evidence):
            prior = {
                **payload,
                "text": text,
                "sender": "3" if change == "sender" else "2",
                "recall_route": {
                    "time": 0 if change == "missing_time" else int(observed * 1000)
                },
            }
            if change == "revision":
                prior["_open_revision"] = "1/5/3/2"
            if change == "test_window":
                prior["_test_rule_window"] = "old-window"
            if change == "reordered_repeat" and index == 1:
                prior["recall_route"]["time"] = int((now - 6) * 1000)
            if change == "historical":
                observed -= 180
            await db.execute(
                "INSERT INTO inbox VALUES(?,?,?,?,?,?,?)",
                (
                    "9" if change == "account" else "1",
                    "6" if change == "group" else "5",
                    f"old-{index}",
                    json.dumps(prior),
                    "recalled" if change == "recalled" else "processed",
                    observed,
                    prior["sender"],
                ),
            )
        received = now - 30 if change == "delayed_current" else now
        await db.execute(
            "INSERT INTO inbox VALUES('1','5','current',?,'processing',?,'2')",
            (json.dumps(payload), received),
        )
        await db.commit()
        adapter.ledger = SimpleNamespace(db=db)
        if change == "wrong_current":
            payload = {**payload, "text": "12345678"}
        if change == "closed_database":
            await db.execute("DROP TABLE inbox")
        if change in {
            "revoked_during_read",
            "receiver_changed",
            "advertising_changed",
        }:
            original_read = db.execute_fetchall

            async def revoke(*args):
                rows = await original_read(*args)
                if change == "advertising_changed":
                    adapter.config["moderation"]["advertising_enabled"] = False
                elif change == "receiver_changed":
                    adapter.account = "9"
                else:
                    adapter.config["_open_revisions"]["5"] = "1/5/5/2"
                return rows

            monkeypatch.setattr(db, "execute_fetchall", revoke)
        result = await rules.contextual_category(adapter, "5", "current", payload)
        expected = change == "none" or (
            change == "reordered_repeat" and style == "solicitation"
        )
        assert result == ("external_promotion" if expected else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["solicitation", "broadcast"])
@pytest.mark.parametrize("target", ["source", "prior", "current"])
@pytest.mark.parametrize(
    "receipt",
    [
        "notice",
        "accepted",
        "verified",
        "unknown",
        "sending",
        "rejected",
        "other_account",
        "other_group",
        "other_client",
        "other_author",
        "other_outbox",
    ],
)
async def test_split_contacts_exclude_native_recalls_without_crediting_punishments(
    tmp_path, monkeypatch, style, target, receipt
):
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger

    now = time.time()
    clock = [now - 4]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    adapter = SimpleNamespace(
        account="1",
        config={
            "_open_revisions": {"5": "r"},
            "moderation": {"content_rules_since": now - 300},
        },
        ledger=ledger,
    )
    payloads = {}
    try:
        for mid, text, offset in (
            ("source", "找我免费上分" if style == "solicitation" else "开播", 4),
            ("prior", "12345678", 2),
            ("current", "12345678", 0),
        ):
            clock[0] = now - offset
            item = {
                "sender": "2",
                "text": text,
                "recall_route": {
                    "time": str(int(clock[0] * 1000)),
                    "client": f"client-{mid}",
                    "peer": "22",
                },
                "_open_revision": "r",
            }
            assert await ledger.ingest("1", "5", mid, item)
            payloads[mid] = item
        route = payloads[target]["recall_route"]
        notice_types = {
            "notice",
            "other_account",
            "other_group",
            "other_client",
            "other_author",
        }
        if receipt in notice_types:
            assert await ledger.observe_recall(
                "9" if receipt == "other_account" else "1",
                "6" if receipt == "other_group" else "5",
                {
                    "notice_id": "notice",
                    "team": "55",
                    "message_id": target,
                    "client": "other" if receipt == "other_client" else route["client"],
                    "author": "33" if receipt == "other_author" else route["peer"],
                    "operator": "99",
                    "message_time": route["time"],
                    "recalled_at": str(int(now * 1000)),
                },
            )
        else:
            key = f"recall/{'9' if receipt == 'other_outbox' else '1'}/5/{target}"
            await ledger.reserve(key, json.dumps(route, sort_keys=True))
            if receipt != "sending":
                await ledger.finish(
                    key, "accepted" if receipt == "other_outbox" else receipt
                )
        matches = receipt in {"notice", "accepted", "verified", "unknown", "sending"}
        blocked = matches and (target != "prior" or style == "broadcast")
        assert await rules.contextual_category(
            adapter, "5", "current", payloads["current"]
        ) == ("" if blocked else "external_promotion")
        # Classification never rewrites inbox state or creates a punishment.
        assert await ledger.db.execute_fetchall("SELECT DISTINCT state FROM inbox") == [
            ("pending",)
        ]
        assert not await ledger.db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('automatic_mutes','progressive_warnings')"
        )
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_split_contact_detector_is_bounded_and_does_not_read_for_ordinary_text(
    tmp_path,
):
    db = SimpleNamespace(execute_fetchall=AsyncMock())
    adapter = SimpleNamespace(
        config={"_open_revisions": {"5": "r"}}, ledger=SimpleNamespace(db=db)
    )
    for text in ("今天有236666人", "123", "免费上分是什么意思", "加", "➕"):
        assert (
            await rules.contextual_category(
                adapter, "5", "m", {"text": text, "sender": "2", "_open_revision": "r"}
            )
            == ""
        )
    db.execute_fetchall.assert_not_awaited()


AD_KEYWORDS = [
    "收fc",
    "P图",
    "8️⃣8️⃣",
    "新项目",
    "注册就送",
    "蛋糕",
    ".cc",
    ".vip",
    "宋",
    ".pw",
    "挣钱",
    "代发",
    "新苔",
    "免费P图上分",
    "兼职",
    "直播打狗中",
    "58",
    "小额",
]


def test_requested_ad_keywords_are_unique_persistent_defaults():
    policy = json.loads(
        Path(rules.__file__).with_name("keyword_defaults.json").read_text()
    )
    assert set(AD_KEYWORDS) <= set(policy["mute_keywords"])
    assert len(policy["mute_keywords"]) == 36
    assert len(policy["mute_keywords"]) == len(set(policy["mute_keywords"]))
    assert policy["kick_keywords"] == []


@pytest.mark.parametrize(
    "word,text",
    [
        ("收fc", "收 ＦＣ"),
        ("P图", "P 图上分"),
        ("8️⃣8️⃣", "8⃣8⃣ 上分"),
        ("新项目", "新项目私聊我"),
        ("注册就送", "注册就送"),
        ("蛋糕", "蛋糕下分秒到账"),
        (".cc", "新项目example.cc私聊我"),
        (".vip", "小额上分example.vip"),
        ("宋", "宋小额上分"),
        (".pw", "新项目example.pw私聊我"),
        ("挣钱", "挣钱兼职私聊我"),
        ("代发", "代发广告加我vx:abc123"),
        ("新苔", "新苔"),
        ("免费P图上分", "免费 P 图 上分"),
        ("兼职", "日结兼职联系我"),
        ("直播打狗中", "直播打狗中"),
        ("58", "58小额上分"),
        ("小额", "小额上分"),
    ],
)
def test_requested_keywords_recognize_promotion_variants(word, text):
    assert rules.keyword_category(text, {"mute_keywords": [word]}) == "mute_keyword"
    assert rules.keyword_category(text, {"mute_keywords": []}) == ""


@pytest.mark.parametrize(
    "text",
    [
        "小宋帮我核对邀请账本",
        "小宋联系我核对账本",
        "58分钟后开始",
        "15800元的订单",
        "金额50.58元",
        "我想找兼职",
        "蛋糕收到了",
        "新项目今天开会",
        "我想挣钱",
        "帮我P图头像",
        "8️⃣8️⃣晚安",
        "这笔小额付款已经到账",
        "谁可以代发今天的群公告",
        ".cc域名常见吗",
        "https://example.cc",
        "有人发收fc广告",
        "谨防免费P图上分骗局",
        "引用“注册就送”的广告",
    ],
)
def test_requested_keywords_preserve_ordinary_discussion_and_reports(text):
    assert rules.keyword_category(text, {"mute_keywords": AD_KEYWORDS}) == ""


@pytest.mark.parametrize(
    "word,text",
    [
        (".cc", "新项目example.ccc私聊我"),
        (".vip", "新项目example.vip.example.com私聊我"),
        (".pw", "新项目文件.pwn私聊我"),
        ("58", "账号15800小额上分"),
        ("58", "50.58元上分"),
        ("8️⃣8️⃣", "88小额上分"),
    ],
)
def test_short_ad_terms_require_exact_number_or_domain_boundary(word, text):
    assert rules.keyword_category(text, {"mute_keywords": [word]}) == ""


def test_malformed_oversized_domain_does_not_match_a_short_suffix():
    text = "新项目" + "a" * 100000 + ".cc私聊我"
    assert rules.keyword_category(text, {"mute_keywords": [".cc"]}) == ""


@pytest.mark.parametrize(
    "host",
    [
        "a" * 16384,
        "a" * 64 + ".vip",
        "a" * 16384 + ".cc",
        "-invalid.com",
        "invalid-.com",
    ],
)
def test_builtin_promotion_requires_bounded_domain_labels(host):
    assert rules.classify("首次充值送30% " + host) == ""


def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(commands, "instance_dir", lambda _: tmp_path)
    member = {"userId": "2", "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"}
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "bot",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "content_rules_since": time.time() - 60,
                "permissions": {"5": ["mute", "recall", "kick"]},
            },
        },
        members={"5": {"2": "22"}},
        get_moderation_members=AsyncMock(
            return_value={"complete": True, "groupMemberInfo": [member]}
        ),
        recall_violation=AsyncMock(return_value="accepted"),
        send_text=AsyncMock(return_value="accepted"),
        execute_moderation=AsyncMock(return_value={"status": "accepted"}),
        diagnostics=SimpleNamespace(emit=Mock()),
    )
    return bind_action_port(adapter), member


@pytest.mark.asyncio
@pytest.mark.parametrize("notification", ["accepted", "rejected", "exception"])
@pytest.mark.parametrize(
    "text",
    [
        "加我vx:abc123456",
        "注册就送体验金38",
        "做单+（12345678）",
        "微信附近的人1小时100",
        "超级群12345678 进群联系管理员做项目",
        "广告费88 分享文案到3个千人群 直播群：12345678",
        "嘉汪12345678各种赔率都有需要滴我，安排茶水",
    ],
)
async def test_progressive_first_warning_is_mentioned_and_independent(
    tmp_path, monkeypatch, notification, text
):
    adapter, member = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(automatic, "instance_dir", lambda _: tmp_path)
    adapter.config["_open_groups"] = {}
    adapter.config["moderation"].update(progressive_mute=True, recall_enabled=True)
    member["userNick"] = "Member\U0001f600"
    if notification == "exception":
        adapter.send_text.side_effect = ProtocolError("not_online")
    else:
        adapter.send_text.return_value = notification
    payload = {
        "text": text,
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    assert await rules.enforce(adapter, "5", "first", payload)
    call = adapter.send_text.await_args
    assert call.args[1] == "open-warning/1/5/first"
    assert call.args[2].startswith("@Member\U0001f600 第1次广告违规")
    assert call.kwargs["auto_recall"] is True
    assert call.kwargs["mentions"] == [
        {"uid": 22, "nick": member["userNick"], "start": 0, "end": 10}
    ]
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        row = db.execute(
            "SELECT status FROM content_violations WHERE message='first'"
        ).fetchone()
    assert row == ("recalled",)
    assert await rules.enforce(adapter, "5", "first", payload)
    adapter.send_text.assert_awaited_once()
    adapter.recall_violation.assert_awaited_once()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_nickname", [False, True])
async def test_progressive_warning_does_not_display_login_or_erase_sanction(
    tmp_path, monkeypatch, has_nickname
):
    adapter, member = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(automatic, "instance_dir", lambda _: tmp_path)
    adapter.config["_open_groups"] = {}
    adapter.config["moderation"].update(progressive_mute=True, recall_enabled=True)
    member["userName"] = "e15ef91a04b74a41b64a3fa6e17f3c8d"
    name = "Real\U0001f600 Member"
    if has_nickname:
        member["groupMemberNick"] = name
    payload = {
        "text": "加我vx:abc123456",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    assert await rules.enforce(adapter, "5", "first", payload)
    call = adapter.send_text.await_args
    assert member["userName"] not in call.args[2]
    assert call.kwargs["auto_recall"] is True
    if has_nickname:
        assert call.args[2].startswith(f"@{name} 第1次广告违规")
        assert call.kwargs["mentions"] == [
            {
                "uid": 22,
                "nick": name,
                "start": 0,
                "end": len(f"@{name} ".encode("utf-16-le")) // 2,
            }
        ]
    else:
        assert call.args[2].startswith("第1次广告违规")
        assert "@" not in call.args[2]
        assert call.kwargs["mentions"] is None
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        row = db.execute(
            "SELECT status FROM content_violations WHERE message='first'"
        ).fetchone()
    assert row == ("recalled",)
    assert await rules.enforce(adapter, "5", "first", payload)
    adapter.send_text.assert_awaited_once()
    adapter.recall_violation.assert_awaited_once()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("advertising", [False, True])
@pytest.mark.parametrize(
    "text",
    [
        "所有人支付宝微信银行卡能收款的截图加我12345678",
        "注册就送体验金38",
        "做单+（12345678）",
        "微信附近的人1小时100",
        "超级群12345678 进群联系管理员做项目",
        "广告费88 分享文案到3个千人群 直播群：12345678",
        "嘉汪12345678各种赔率都有需要滴我，安排茶水",
    ],
)
async def test_recall_derived_rules_respect_group_advertising_switch(
    tmp_path, monkeypatch, advertising, text
):
    adapter, _ = fixture(tmp_path, monkeypatch)
    adapter.config["moderation"].update(
        advertising_enabled=advertising, mute_keywords=[], kick_keywords=[]
    )
    payload = {
        "text": text,
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    assert await rules.enforce(adapter, "5", "recalled-ad", payload) is advertising
    if advertising:
        adapter.recall_violation.assert_awaited_once()
        adapter.send_text.assert_awaited_once()
    else:
        adapter.recall_violation.assert_not_awaited()
        adapter.send_text.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("advertising", [False, True])
@pytest.mark.parametrize("recalled", [False, True])
async def test_split_contact_evidence_reaches_enforcement_without_flood_switch(
    tmp_path, monkeypatch, advertising, recalled
):
    adapter, _ = fixture(tmp_path, monkeypatch)
    now = time.time()
    revision = "1/5/4/2"
    adapter.config["_open_revisions"] = {"5": revision}
    adapter.config["moderation"].update(
        advertising_enabled=advertising, flood_enabled=False
    )
    payload = {
        "text": "12345678",
        "sender": "2",
        "recall_route": {
            "time": int(now * 1000),
            "client": "client-current",
            "peer": "22",
        },
        "_open_revision": revision,
    }
    async with aiosqlite.connect(tmp_path / "inbox.db") as db:
        await db.execute(
            "CREATE TABLE inbox(account,team,message,payload,state,received_at,sender)"
        )
        await db.execute(
            "CREATE TABLE observed_recalls("
            "account,group_id,message_id,client,author,received_at)"
        )
        for mid, text, offset in (
            ("old", "找我免费上分", 2),
            ("contact", "12345678", 0),
        ):
            item = {
                **payload,
                "text": text,
                "recall_route": {
                    "time": int((now - offset) * 1000),
                    "client": "client-old" if mid == "old" else "client-current",
                    "peer": "22",
                },
            }
            await db.execute(
                "INSERT INTO inbox VALUES('1','5',?,?,'processing',?,'2')",
                (mid, json.dumps(item), now - offset),
            )
        if recalled:
            await db.execute(
                "INSERT INTO observed_recalls VALUES"
                "('1','5','old','client-old','22',?)",
                (now,),
            )
        await db.commit()
        adapter.ledger = SimpleNamespace(db=db)
        assert await rules.enforce(adapter, "5", "contact", payload) is (
            advertising and not recalled
        )
    if advertising and not recalled:
        adapter.recall_violation.assert_awaited_once()
    else:
        adapter.recall_violation.assert_not_awaited()


@pytest.mark.asyncio
async def test_instance_keywords_apply_to_each_authorized_group(tmp_path, monkeypatch):
    adapter, _ = fixture(tmp_path, monkeypatch)
    adapter.config["enabled_groups"] = ["5", "6"]
    adapter.config["moderation"]["mute_keywords"] = ["收fc"]
    adapter.config["moderation"]["permissions"]["6"] = ["mute", "recall"]
    adapter.members["6"] = {"2": "22"}
    payload = {
        "text": "收FC",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    for group in ("5", "6"):
        assert await rules.enforce(adapter, group, f"ad-{group}", payload)
    assert adapter.recall_violation.await_count == 2
    assert adapter.send_text.await_count == 2
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_warn_repeat_mute_and_duplicate(tmp_path, monkeypatch):
    adapter, _ = fixture(tmp_path, monkeypatch)
    payload = {
        "text": "加我vx:abc123456",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    for mid in ("1", "1", "2", "2"):
        assert await rules.enforce(adapter, "5", mid, payload)
    assert adapter.send_text.await_count == 1
    assert adapter.recall_violation.await_count == 2
    adapter.execute_moderation.assert_awaited_once()
    assert adapter.execute_moderation.await_args.args[:4] == (
        "content/5/2",
        "mute",
        5,
        2,
    )


@pytest.mark.asyncio
async def test_full_semantic_sanction_path_never_kicks_with_legacy_opt_in(
    tmp_path, monkeypatch
):
    adapter, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(automatic, "instance_dir", lambda _: tmp_path)
    adapter.config["moderation"]["auto_kick"] = {"5": True}

    async def execute(
        operation, action, group, member, text="", *, minutes=1, action_check=None
    ):
        if action_check:
            await action_check()
        if action == "kick":
            automatic.authorize_kick(adapter, operation, str(group), str(member))
        receipt = {
            "operation": operation,
            "action": action,
            "group": group,
            "member": member,
            "status": "accepted",
        }
        with closing(automatic.database(adapter)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
            )
            db.execute(
                "INSERT INTO operations VALUES(?, 'fixture', ?)",
                (operation, json.dumps(receipt)),
            )
        return receipt

    adapter.execute_moderation.side_effect = execute
    payload = {
        "text": "这里还有首次充值福利",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    assert rules.classify(payload["text"]) == ""
    for mid in ("1", "1", "2", "3", "4", "4", "5"):
        await rules.enforce(
            adapter,
            "5",
            mid,
            payload,
            assessment={
                "decision": "violation",
                "category": "external_promotion",
                "message_id": mid,
            },
        )
    assert adapter.send_text.await_count == 1
    assert [call.args[1] for call in adapter.execute_moderation.await_args_list] == [
        "mute",
        "mute",
        "mute",
        "mute",
    ]
    assert adapter.recall_violation.await_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["admin", "owner", "old", "no_time", "no_grant", "incomplete", "wrong_peer"]
)
async def test_no_sanctions_on_unsafe_evidence(tmp_path, monkeypatch, case):
    adapter, member = fixture(tmp_path, monkeypatch)
    payload = {
        "text": "加我vx:abc123456",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    if case in {"admin", "owner"}:
        member["groupRole"] = "GROUP_ROLE_" + case.upper()
    elif case == "old":
        payload["recall_route"]["time"] = "1000000000000"
    elif case == "no_time":
        payload["recall_route"] = {}
    elif case == "no_grant":
        adapter.config["moderation"]["permissions"] = {}
    elif case == "incomplete":
        adapter.get_moderation_members.return_value["complete"] = False
    else:
        member["nimId"] = "wrong"
    await rules.enforce(adapter, "5", "1", payload)
    adapter.recall_violation.assert_not_called()
    adapter.send_text.assert_not_called()
    adapter.execute_moderation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", ["directory", "claim"])
@pytest.mark.parametrize("change", ["account", "policy", "grants", "automation"])
async def test_content_wait_never_uses_changed_authority(
    tmp_path, monkeypatch, wait, change
):
    adapter, _ = fixture(tmp_path, monkeypatch)
    payload = {
        "text": "加我vx:abc123456",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    entered, release = threading.Event(), threading.Event()
    if wait == "directory":
        roster = adapter.get_moderation_members.return_value

        async def directory(_group):
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return roster

        adapter.get_moderation_members.side_effect = directory
    else:
        store = rules.review_store(adapter, tmp_path / "moderation.sqlite3")
        claim = store.claim

        def paused_claim(*args):
            result = claim(*args)
            entered.set()
            assert release.wait(2)
            return result

        monkeypatch.setattr(store, "claim", paused_claim)
    work = asyncio.create_task(rules.enforce(adapter, "5", "1", payload))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if change == "account":
            adapter.account = "another"
        elif change == "policy":
            adapter.config["moderation"]["content_rules_since"] = time.time()
        elif change == "grants":
            adapter.config["moderation"]["permissions"] = {}
        else:
            adapter.config["moderation"]["automation_enabled"] = False
        release.set()
        assert await asyncio.wait_for(work, 2)
        adapter.recall_violation.assert_not_awaited()
        adapter.send_text.assert_not_awaited()
        adapter.execute_moderation.assert_not_awaited()
        with closing(sqlite3.connect(tmp_path / "moderation.sqlite3")) as db:
            assert db.execute(
                "SELECT account,status FROM content_violations"
            ).fetchall() == [("1", "review_only")]
    finally:
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_content_success_after_account_switch_finishes_original_evidence(
    tmp_path, monkeypatch
):
    adapter, _ = fixture(tmp_path, monkeypatch)

    async def warning(*args, authorize, **kwargs):
        await authorize()
        adapter.account = "another"
        return "accepted"

    adapter.send_text.side_effect = warning
    await rules.enforce(
        adapter,
        "5",
        "1",
        {
            "text": "加我vx:abc123456",
            "sender": "2",
            "recall_route": {"time": str(int(time.time() * 1000))},
        },
    )
    with closing(sqlite3.connect(tmp_path / "moderation.sqlite3")) as db:
        assert db.execute(
            "SELECT account,status,warning FROM content_violations"
        ).fetchall() == [("1", "warned", "accepted")]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["preview", "confirm"])
@pytest.mark.parametrize("change", ["owner", "account", "cancel"])
async def test_private_kick_writer_wait_revalidates(
    tmp_path, monkeypatch, operation, change
):
    adapter, member = fixture(tmp_path, monkeypatch)
    event = SimpleNamespace(
        platform=adapter,
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        get_sender_id=lambda: "9",
        unified_msg_origin="bot:private:9",
        get_group_id=lambda: "",
        message_obj=SimpleNamespace(message_id="preview"),
    )
    service = commands.Commands()
    service.selections[(id(adapter), "1", "9", event.unified_msg_origin)] = {
        "expires": time.monotonic() + 600,
        "group": "5",
        "members": [member],
    }
    command = "踢出 1"
    if operation == "confirm":
        preview = await service.run(event, command)
        command = "确认踢出 " + preview.rsplit(" ", 1)[-1]
        event.message_obj.message_id = "confirmation"
    blocker = sqlite3.connect(tmp_path / "moderation.sqlite3")
    blocker.execute("BEGIN IMMEDIATE")
    work = asyncio.create_task(service.run(event, command))
    try:
        await asyncio.sleep(0.05)
        assert not work.done()
        if change == "owner":
            event.is_admin = lambda: False
        elif change == "account":
            adapter.account = "other"
        else:
            work.cancel()
        blocker.rollback()
        with pytest.raises(
            asyncio.CancelledError if change == "cancel" else ProtocolError
        ):
            await asyncio.wait_for(work, 3)
        adapter.execute_moderation.assert_not_awaited()
        if operation == "confirm":
            assert blocker.execute(
                "SELECT used FROM kick_confirmations"
            ).fetchone() == (0,)
        else:
            assert not blocker.execute(
                "SELECT 1 FROM sqlite_master WHERE name='kick_confirmations'"
            ).fetchone()
    finally:
        blocker.rollback()
        blocker.close()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_only", [False, True])
async def test_private_confirmation_bound_single_use(
    tmp_path, monkeypatch, manual_only
):
    adapter, member = fixture(tmp_path, monkeypatch)
    if manual_only:
        adapter.config["moderation"]["manual_kick_only"] = True
        adapter.config["moderation"].pop("content_rules_since")
    event = SimpleNamespace(
        platform=adapter,
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        get_sender_id=lambda: "9",
        unified_msg_origin="bot:private:9",
        get_group_id=lambda: "",
        message_obj=SimpleNamespace(message_id="preview"),
    )
    service = commands.Commands()
    service.selections[(id(adapter), "1", "9", event.unified_msg_origin)] = {
        "expires": time.monotonic() + 600,
        "group": "5",
        "members": [member],
    }
    if manual_only:
        assert await service.execute(event, "踢出", "1", ai=True) == {
            "status": "rejected",
            "reason": "ai_management_retired",
        }
    preview = await service.run(event, "踢出 1")
    token = preview.rsplit(" ", 1)[-1]
    adapter.execute_moderation.assert_not_called()
    assert "无效" in await service.run(event, "确认踢出 " + token)
    event.message_obj.message_id = "confirm"
    event.get_sender_id = lambda: "8"
    assert "无效" in await service.run(event, "确认踢出 " + token)
    event.get_sender_id = lambda: "9"
    assert "accepted" in await service.run(event, "确认踢出 " + token)
    assert "无效" in await service.run(event, "确认踢出 " + token)
    adapter.execute_moderation.assert_awaited_once()
