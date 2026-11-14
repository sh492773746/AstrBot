"""Per-group literal rule editing, bounded matching and sender-scoped navigation."""

import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import content_rules
from astrbot.builtin_stars.wangshangliao_moderation.custom_rules import parse, validate
from astrbot.builtin_stars.wangshangliao_moderation.open_service import recognize
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    DEFAULT_GROUP_RULES,
    OpenError,
)
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_content_rules import fixture

RULE = {"name": "收款招募", "terms": ["收款截图", "加我"], "enabled": True}
pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


def test_default_template_is_bounded_literal_and_contains_no_business_authority():
    validate(DEFAULT_GROUP_RULES["custom_rules"])
    assert set(DEFAULT_GROUP_RULES) == {
        "advertising",
        "flood",
        "keywords",
        "custom_rules",
    }
    assert (
        len(DEFAULT_GROUP_RULES["keywords"])
        == len(set(DEFAULT_GROUP_RULES["keywords"]))
        <= 50
    )
    defaults = json.loads(
        Path(content_rules.__file__).with_name("keyword_defaults.json").read_text()
    )
    assert DEFAULT_GROUP_RULES["keywords"] == defaults["mute_keywords"]
    assert len(DEFAULT_GROUP_RULES["keywords"]) == 49


@pytest.mark.parametrize(
    "text",
    [
        "支付宝微信银行卡能收款的截图加我",
        "能收 款的截\u200b图，请加我",
        "出租闲鱼，项目客服",
        "QQ电脑PC端出租 月亮报价",
        "手机号码接码，一个验证码，秒结账",
        "微信朋友圈代发，旺旺项目客服",
        "做项目，旺旺不禁言超级群，进群联系管理员",
        "高价收结余账号，最低门槛联系我",
        "每日首冲活动，转发朋友圈，加入交流群",
        "申请代言费，注册好绑定提现信息，开通合营发给我数据",
        "周期内充值满额秒返，无视ip，玩过的也可以来",
        "注册送礼品，导航网，请找推广人员",
    ],
)
def test_default_template_matches_observed_solicitation_combinations(text):
    assert (
        content_rules.keyword_category(text, {**DEFAULT_GROUP_RULES, "keywords": []})
        == "external_promotion"
    )


@pytest.mark.parametrize(
    "text",
    [
        "今晚做饭吃蛋糕",
        "我姓宋，今年58岁",
        "小额转账要多久到账",
        "银行卡封了，等去派出所解",
        "有没有兼职",
        "上次叫我代发那个快手啊",
        "会做饭的男人贼加分",
        "有没有人要借钱的",
        "QQ有月亮等级",
        "验证码不能发给陌生人",
        "首冲有多少流水",
        "代言费什么时候发",
        "@群主-导航网example.cc 回一下私信",
        "举报：支付宝能收款的截图加我",
        "最近常见骗局：出租闲鱼，项目客服，接码验证码秒结账",
        "不要相信高价收结余账号联系我",
        "转发：无视ip，充值，玩过也可以来",
    ],
)
def test_default_template_preserves_chat_reports_and_partial_matches(text):
    assert content_rules.keyword_category(text, DEFAULT_GROUP_RULES) == ""


@pytest.mark.parametrize(
    "text",
    [
        "\u2795我",
        "加我",
        "+我",
        "jia我",
        "扫码",
        "扫马",
        "扫\U0001f40e",
        "注册就送88？",
        "有没有P图上分？",
        "@群主-导航网example.cc 注册就送88？",
        "成人大秀",
    ],
)
def test_updated_default_keywords_match_requested_variants(text):
    assert content_rules.keyword_category(text, DEFAULT_GROUP_RULES) == "mute_keyword"


@pytest.mark.parametrize(
    "text",
    [
        "稳赚不赔",
        "保本套利",
        "导师带赚",
        "刷单垫付",
        "垫付刷单",
        "充值解冻",
        "缴费解冻",
        "私聊领取彩金",
        "包赔上分",
        "刷流水返佣",
        "出售银行卡",
        "收购银行卡",
        "出售实名账号",
        "代办洗钱",
        "裸聊接单",
        "色情直播",
        "博彩开户注册",
        "进",
    ],
)
def test_removed_keywords_do_not_remain_in_default_literal_rules(text):
    assert text not in DEFAULT_GROUP_RULES["keywords"]
    assert content_rules.keyword_category(text, DEFAULT_GROUP_RULES) == ""


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [RULE] * 21,
        [{**RULE, "enabled": 1}],
        [{**RULE, "action": "kick"}],
        [{**RULE, "terms": ["收款截图"]}],
        [{**RULE, "terms": ["加"] * 2}],
        [{**RULE, "terms": ["收款截图", "加我", "私聊", "代收", "租号", "微信"]}],
        [{**RULE, "terms": ["收款截图", 123]}],
        [{**RULE, "terms": ["收款截图", "\u200b\u200b"]}],
        [{**RULE, "terms": ["收款截图", "收款"]}],
        [{**RULE, "terms": ["联系我", "联 系 我"]}],
        [{**RULE, "terms": ["收款截图", "x" * 41]}],
        [{**RULE, "terms": ["收款截图", " " * 1000 + "加我"]}],
        [{**RULE, "terms": ["收款截图", "加我\n禁言"]}],
        [{**RULE, "terms": ["收款截图", "加我\u2028公告"]}],
        [{**RULE, "name": "\u200b"}],
        [{**RULE, "name": "x" * 25}],
        [{**RULE, "name": " " * 1000 + "名称"}],
        [RULE, {**RULE, "name": "收 款 招 募"}],
    ],
)
def test_invalid_or_unbounded_rule_documents_are_rejected(value):
    with pytest.raises(ValueError, match="custom_rules_"):
        validate(value)


def test_parse_and_matching_are_literal_not_executable():
    rule = parse("测试 | a.*b | __import__('os')")
    validate([rule])
    policy = {"custom_rules": [rule]}
    assert content_rules.keyword_category("axxxb __import__('os')", policy) == ""
    assert (
        content_rules.keyword_category("a.*b __import__('os')", policy)
        == "external_promotion"
    )


@pytest.mark.parametrize(
    "text",
    [
        "收款招募/收款截图/加我",
        " 收款招募 / 收款截图 / 加我 ",
        "收款招募 | 收款截图 | 加我",
    ],
)
def test_slash_and_pipe_entry_produce_the_same_bounded_rule(text):
    assert parse(text) == RULE


def test_pipe_entry_preserves_literal_slashes_and_urls():
    rule = parse("网址招募 | example.com/path | 联系我")
    assert rule["terms"] == ["example.com/path", "联系我"]
    policy = {"custom_rules": [rule]}
    assert (
        content_rules.keyword_category("example.com/path 联系我", policy)
        == "external_promotion"
    )
    assert content_rules.keyword_category("example.com 联系我", policy) == ""


@pytest.mark.parametrize(
    "text",
    [
        "收款招募/收款截图",
        "收款招募//收款截图/加我",
        "收款招募/收款截图/加我/",
        "/收款截图/加我",
        "收款招募/收款截图|加我",
    ],
)
def test_missing_or_mixed_separator_fields_are_not_silently_dropped(text):
    with pytest.raises(ValueError, match="custom_rules_invalid"):
        parse(text)


@pytest.mark.parametrize(
    "text,enabled,expected",
    [
        ("收款截图 加我", True, "external_promotion"),
        ("加我，需要收 款截\u200b图", True, "external_promotion"),
        ("收款截图", True, ""),
        ("加我讨论天气", True, ""),
        ("举报收款截图加我", True, ""),
        ("转发：收款截图加我", True, ""),
        ("收款截图加我", False, ""),
    ],
)
def test_all_terms_and_report_exemptions(text, enabled, expected):
    policy = {"custom_rules": [{**RULE, "enabled": enabled}]}
    assert content_rules.keyword_category(text, policy) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["/", " | "])
async def test_real_group_route_edits_only_current_group_and_dry_run_has_no_effects(
    setup, separator
):
    value, _, event = setup
    await step(
        value,
        event,
        "添加规则 " + separator.join(["收款招募", "收款截图", "加我"]),
        native=True,
    )
    assert value.store.tenant("1", "5")["rules"]["custom_rules"] == [RULE]
    assert "custom_rules" not in value.store.tenant("1", "6")["rules"]
    before = value.store.tenant("1", "5")
    reply = await step(value, event, "测试规则 收款截图 加我", native=True)
    assert "命中广告规则" in reply and "仅测试" in reply
    assert value.store.tenant("1", "5") == before
    event.platform.execute_action.assert_not_awaited()
    await step(value, event, "停用规则 1", native=True)
    await step(
        value,
        event,
        "修改规则 1 " + separator.join(["修改名称", "账号截图", "联系我"]),
        native=True,
    )
    saved = value.store.tenant("1", "5")["rules"]["custom_rules"][0]
    assert saved == {
        "name": "修改名称",
        "terms": ["账号截图", "联系我"],
        "enabled": False,
    }
    assert "没有命中" in await step(
        value, event, "测试规则 账号截图联系我", native=True
    )
    await step(value, event, "启用规则 1", native=True)
    assert "命中广告规则" in await step(
        value, event, "测试规则 账号截图联系我", native=True
    )
    await step(value, event, "修改关键词 1 新广告词", native=True)
    assert value.store.tenant("1", "5")["rules"]["keywords"] == ["新广告词"]
    assert value.store.tenant("1", "6")["rules"]["keywords"] == ["old"]
    await step(value, event, "删除规则 1", native=True)
    assert value.store.tenant("1", "5")["rules"]["custom_rules"] == []


@pytest.mark.asyncio
async def test_old_dashboard_cannot_silently_discard_rules_and_audit_preserves_change(
    setup,
):
    value, _, event = setup
    before = value.store.tenant("1", "5")
    await value.manager_command(event, "添加规则 收款招募 | 收款截图 | 加我")
    current = value.store.tenant("1", "5")
    with pytest.raises(OpenError, match="tenant_rules_refresh"):
        value.store.change("1", "5", current["version"], "2", rules=before["rules"])
    assert value.store.tenant("1", "5") == current
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenStore

    assert OpenStore(value.root).tenant("1", "5") == current
    with value.store.connect() as db:
        row = db.execute(
            "SELECT detail FROM audit WHERE action='tenant_changed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert "custom_rules" in row[0]


@pytest.mark.asyncio
async def test_private_rule_routes_are_blocked_even_with_a_forged_group_selection(
    setup,
):
    from tests.test_wangshangliao_plain_commands import native_event

    value, engine, template = setup
    before = {group: value.store.tenant("1", group) for group in ("5", "6")}
    event = native_event("群列表", private=True)
    event.platform = template.platform
    event.eligible = True
    engine.selections[(id(event.platform), "1", "2", event.unified_msg_origin)] = {
        "group": "6",
        "expires": time.monotonic() + 600,
    }
    try:
        assert value.navigation.key(event) is None
        for text in (
            "群列表",
            "选择群 1",
            "添加规则 收款招募 | 收款截图 | 加我",
            "删除规则 1",
            "测试规则 收款截图 加我",
        ):
            assert await engine.run(event, text) is None
            assert await step(value, event, text, native=True) == ""
            assert event.is_stopped()
        with pytest.raises(OpenError, match="private_messages_retired"):
            await value.manager_command(event, "添加规则 收款招募 | 收款截图 | 加我")
        assert not value.navigation.sessions
        assert {group: value.store.tenant("1", group) for group in ("5", "6")} == before
        value.roster.assert_not_awaited()
        event.platform.get_moderation_members.assert_not_awaited()
        event.platform.execute_action.assert_not_awaited()
    finally:
        event.processing_completion.cancel()


@pytest.mark.asyncio
async def test_group_rule_editor_ignores_forged_cross_group_selection(setup):
    value, engine, event = setup
    row = value.store.tenant("1", "6")
    value.store.change("1", "6", row["version"], "2", managers=["3"])
    await value.refresh()
    engine.selections[(id(event.platform), "1", "2", event.unified_msg_origin)] = {
        "group": "6",
        "expires": time.monotonic() + 600,
    }
    reply = await step(value, event, "添加规则 收款招募 | 收款截图 | 加我", native=True)
    assert "已保存" in reply
    assert value.store.tenant("1", "5")["rules"]["custom_rules"] == [RULE]
    assert "custom_rules" not in value.store.tenant("1", "6")["rules"]
    event.message_obj.group_id = "6"
    reply = await step(
        value, event, "添加规则 越权测试 | 自定义甲 | 自定义乙", native=True
    )
    assert "无权限" in reply
    assert "custom_rules" not in value.store.tenant("1", "6")["rules"]


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_removed_rule_number_menu_never_creates_a_draft_or_changes_rules(
    setup, private
):
    value, _, event = setup
    if private:
        event.is_private_chat = lambda: True
        event.get_group_id = lambda: ""
    before = {group: value.store.tenant("1", group) for group in ("5", "6")}
    for text in ("菜单", "3", "10", "2", "收款招募 | 收款截图 | 加我", "1"):
        assert not recognize(text)
        assert not value.navigation.accepts(event, text)
        assert await step(value, event, text, native=True) == ""
    assert not value.navigation.sessions
    assert {group: value.store.tenant("1", group) for group in ("5", "6")} == before
    value.roster.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["/", " | "])
async def test_fixed_group_rule_command_saves_once_and_rejects_invalid_combinations(
    setup, separator
):
    value, _, event = setup
    command = "添加规则 " + separator.join(["收款招募", "收款截图", "加我"])
    assert "已保存" in await step(value, event, command, native=True)
    saved = value.store.tenant("1", "5")
    assert saved["rules"]["custom_rules"] == [RULE]
    await value.on_event(event)
    assert value.store.tenant("1", "5") == saved
    reply = await step(value, event, "添加规则 错误 | 收款 | 收款截图", native=True)
    assert "不能互相包含" in reply
    assert value.store.tenant("1", "5") == saved


@pytest.mark.asyncio
async def test_rule_write_wait_rechecks_revocation_before_committing(setup):
    value, _, event = setup
    original = value.store.change
    waiting, released = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(waiting.set)
        asyncio.run_coroutine_threadsafe(released.wait(), loop).result(timeout=5)
        return original(*args, **kwargs)

    value.store.change = blocked
    pending = asyncio.create_task(
        value.manager_command(event, "添加规则 收款招募 | 收款截图 | 加我")
    )
    await asyncio.wait_for(waiting.wait(), 2)
    value.management.assignments[value.management.key(event.platform, "5")] = ()
    released.set()
    with pytest.raises(OpenError, match="operation_revoked"):
        await pending
    assert "custom_rules" not in value.store.tenant("1", "5")["rules"]


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_custom_enforcement_reads_exact_group_and_honors_ad_switch(
    tmp_path, monkeypatch, disabled
):
    adapter, _ = fixture(tmp_path, monkeypatch)
    adapter.config["_open_groups"] = {
        "5": {"custom_rules": [copy.deepcopy(RULE)]},
        "6": {"custom_rules": []},
    }
    adapter.config["moderation"]["advertising_enabled"] = not disabled
    adapter.config["moderation"]["flood_enabled"] = True
    payload = {
        "text": "收款截图 加我",
        "sender": "2",
        "recall_route": {"time": str(int(time.time() * 1000))},
    }
    assert not await content_rules.enforce(adapter, "6", "other", payload)
    assert await content_rules.enforce(adapter, "5", "ours", payload) is not disabled
    assert adapter.recall_violation.await_count == (0 if disabled else 1)


@pytest.mark.parametrize(
    "command",
    [
        "修改关键词 1 新词",
        "添加规则 名称 | 词一 | 词二",
        "修改规则 1 名称 | 词一 | 词二",
        "删除规则 1",
        "启用规则 1",
        "停用规则 1",
        "测试规则 普通聊天",
    ],
)
def test_fixed_rule_commands_keep_the_group_command_prefixes(command):
    assert recognize(command) == command
    assert recognize("管理 " + command) == command
    assert recognize("/管理 " + command) == command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("custom_rules_invalid", "每群最多20条"),
        ("custom_rules_redundant", "不能互相包含"),
        ("tenant_rules_refresh", "刷新页面"),
        ("tenant_invalid", "关键词最多50个"),
    ],
)
async def test_dashboard_rule_validation_has_actionable_localized_errors(
    monkeypatch, reason, expected
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard

    page = object.__new__(dashboard.DashboardPage)
    page.authorize = AsyncMock()
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            integrated=True,
            active=True,
            rpc=SimpleNamespace(
                lock=asyncio.Lock(), dispatch=AsyncMock(side_effect=OpenError(reason))
            ),
        )
    )

    async def stream():
        yield b'{"action":"tenant","payload":{}}'

    monkeypatch.setattr(
        dashboard,
        "request",
        SimpleNamespace(
            _request=SimpleNamespace(method="POST", stream=stream),
            username="test",
            json=AsyncMock(return_value={"action": "tenant", "payload": {}}),
        ),
    )
    with pytest.raises(dashboard.ApiError) as caught:
        await page.open_service()
    assert caught.value.status_code == 409
    assert expected in str(caught.value)
    page.authorize.assert_awaited_once()
