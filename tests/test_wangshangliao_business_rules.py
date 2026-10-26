"""Deterministic business rules use isolated stores and fake native receipts only."""

import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import rule_replies
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    recognize_integrated,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import (
    Advertisement,
    RuleConfiguration,
    RuleReplies,
    next_ad_boundary,
)
from astrbot.builtin_stars.wangshangliao_moderation.service_settings import (
    SupportConfiguration,
)
from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    BUSINESS_COMMANDS,
    recognize_command,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_group_ai import model_scope, tenant
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_public_hardening import chat_event


@pytest.fixture
def rules(tmp_path, monkeypatch):
    monkeypatch.setattr(rule_replies, "ACCOUNT_AD_GAP_SECONDS", 0)
    monkeypatch.setattr(rule_replies, "IMAGE_TEXT_GAP_SECONDS", 0)
    store = OpenStore(tmp_path)
    tenant(store)
    service = SimpleNamespace(
        store=store,
        group_ai=SimpleNamespace(activity={}),
        member_evidence={},
        capabilities={},
        roster=AsyncMock(return_value={}),
    )
    return RuleReplies(service)


def save_rules(rules, config, *, account="99", group="5", owner="", peer=""):
    old = rules.read(account, group)
    preview = rules.preview(
        account,
        group,
        old["version"],
        config,
        "test",
        "session",
        rules.store.tenant(account, group),
        owner=owner,
        peer=peer,
        guard=lambda: None,
    )
    return rules.commit(
        account, group, preview["token"], "test", "session", guard=lambda: None
    )


def keyword(id="k", **kwargs):
    return {
        "id": id,
        "name": id,
        "enabled": True,
        "match": "exact",
        "terms": ["链接", "地址"],
        "text": "已确认的业务资料",
        **kwargs,
    }


def event_for(rule_service, *, text="链接", account="99", group="5", mid="message"):
    platform = SimpleNamespace(
        account=account, send_reply_text=AsyncMock(return_value="accepted")
    )
    return SimpleNamespace(
        platform=platform,
        actor_identity=ActorIdentity(account, "2", "1002", "2"),
        is_private_chat=lambda: False,
        get_group_id=lambda: group,
        get_sender_id=lambda: "2",
        message_str=text,
        message_obj=SimpleNamespace(message_id=mid),
    )


def test_preview_confirmation_is_scoped_expiring_versioned_and_audited(rules):
    tenant_before = rules.store.tenants()
    p = rules.preview(
        "99",
        "5",
        0,
        {"keywords": [keyword()]},
        "a",
        "s",
        rules.store.tenant("99", "5"),
        guard=lambda: None,
    )
    assert rules.read("99", "5")["version"] == 0
    for account, group, actor, session in [
        ("88", "5", "a", "s"),
        ("99", "6", "a", "s"),
        ("99", "5", "b", "s"),
        ("99", "5", "a", "other"),
    ]:
        with pytest.raises(OpenError, match="confirmation_invalid"):
            rules.commit(account, group, p["token"], actor, session, guard=lambda: None)
    assert (
        rules.commit("99", "5", p["token"], "a", "s", guard=lambda: None)["version"]
        == 1
    )
    assert (
        rules.commit("99", "5", p["token"], "a", "s", guard=lambda: None)["version"]
        == 1
    )
    assert rules.store.tenants() == tenant_before
    with pytest.raises(OpenError, match="changed"):
        rules.preview(
            "99",
            "5",
            0,
            {},
            "a",
            "s",
            rules.store.tenant("99", "5"),
            guard=lambda: None,
        )
    assert (
        RuleReplies(SimpleNamespace(store=rules.store)).read("99", "5")["version"] == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "contains",
        "synonym",
        "mention",
        "cooldown",
        "duplicate",
        "send_failure",
        "disabled_reply",
    ],
)
async def test_matching_consumes_one_rule_even_if_no_send(rules, monkeypatch, case):
    save_rules(
        rules,
        {
            "keywords": [
                keyword("contains", match="contains", text="contains"),
                keyword("exact", text="exact"),
            ]
        },
    )
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    text = {"contains": "请给我链接", "synonym": "地址"}.get(case, "链接")
    event = event_for(rules.service, text=text)
    if case == "mention":
        event.mentioned = True
    if case == "send_failure":
        event.platform.send_reply_text.side_effect = RuntimeError("transport failed")
    if case == "disabled_reply":
        monkeypatch.setattr(
            rules,
            "send_guard",
            lambda *a, **kw: (_ for _ in ()).throw(OpenError("inactive")),
        )
    assert await rules.observe(event) is True
    assert event.platform.send_reply_text.call_count == (
        0 if case == "disabled_reply" else 1
    )
    if case not in {"disabled_reply", "send_failure"}:
        assert event.platform.send_reply_text.call_args.args[2] == (
            "contains" if case == "contains" else "exact"
        )
        assert event.platform.send_reply_text.call_args.kwargs["auto_recall"] is False
    if case in {"cooldown", "duplicate"}:
        if case == "cooldown":
            event.message_obj.message_id = "second"
        assert await rules.observe(event)
        assert event.platform.send_reply_text.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "other_account",
        "other_group",
        "disabled",
        "private",
        "wrong_identity",
        "echo",
        "no_match",
    ],
)
async def test_nonmatching_and_untrusted_inputs_do_not_send(rules, monkeypatch, case):
    save_rules(rules, {"keywords": [keyword(enabled=case != "disabled")]})
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    event = event_for(rules.service)
    if case == "other_account":
        event.platform.account = "88"
        event.actor_identity = ActorIdentity("88", "2", "1002", "2")
    elif case == "other_group":
        event.get_group_id = lambda: "6"
    elif case == "private":
        event.is_private_chat = lambda: True
    elif case == "wrong_identity":
        event.actor_identity = ActorIdentity("99", "2", "1002")
    elif case == "echo":
        monkeypatch.setattr(
            "astrbot.builtin_stars.wangshangliao_moderation.rule_replies.is_managed_account",
            lambda uid: True,
        )
    elif case == "no_match":
        event.message_str = "普通聊天"
    assert not await rules.observe(event)
    event.platform.send_reply_text.assert_not_called()


@pytest.mark.parametrize(
    "config",
    [
        {"keywords": [keyword(terms=[""])]},
        {"keywords": [keyword(terms=["链接", "链接"])]},
        {"keywords": [keyword(match="regex")]},
        {"keywords": [keyword(tools=["anything"])]},
        {"ads": [{"id": "a", "name": "a", "text": "a", "times": ["25:00"]}]},
        {"ads": [{"id": "a", "name": "a", "text": "a", "times": []}]},
    ],
)
def test_rule_schema_rejects_nonliteral_and_unsafe_values(config):
    with pytest.raises(ValidationError):
        RuleConfiguration.model_validate(config)


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("08:59", "09:00"),
        ("09:00", "18:00"),
        ("18:00", "09:00"),
    ],
)
def test_daily_schedule_strictly_future_beijing(clock, expected):
    now = datetime.fromisoformat("2026-10-20T" + clock).replace(
        tzinfo=ZoneInfo("Asia/Shanghai")
    )
    ad = Advertisement(id="a", name="a", text="confirmed", times=["09:00", "18:00"])
    point = next_ad_boundary(ad, now.timestamp())
    assert point > now.timestamp()
    assert (
        datetime.fromtimestamp(point, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
        == expected
    )


@pytest.mark.parametrize("day", ["2026-10-22", "2026-10-23", "2026-12-31"])
def test_three_staggered_plans_use_twenty_minute_spacing_and_hourly_cycles(day):
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime.fromisoformat(day + "T23:59:59").replace(tzinfo=zone).timestamp()
    plans = [
        Advertisement(
            id=label,
            name=label,
            text="neutral clock fixture",
            times=[f"{hour:02d}:{minute:02d}" for hour in range(24)],
        )
        for label, minute in zip("ABC", (0, 20, 40), strict=True)
    ]
    next_at = {plan.id: next_ad_boundary(plan, now) for plan in plans}
    events = []
    last = {}
    for _ in range(72):
        point, label = min((point, label) for label, point in next_at.items())
        if events:
            assert point - events[-1][0] == 20 * 60
        if label in last:
            assert point - last[label] == 60 * 60
        plan = next(plan for plan in plans if plan.id == label)
        last[label] = point
        events.append((point, label))
        next_at[label] = next_ad_boundary(plan, point)
        # A fresh clock calculation at the boundary never replays that occurrence.
        assert next_ad_boundary(plan, point) == point + 60 * 60
    assert "".join(label for _, label in events) == "ABC" * 24
    assert datetime.fromtimestamp(events[0][0], zone).strftime("%H:%M") == "00:00"


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("21:59", "22:00"),
        ("22:00", "23:00"),
        ("23:59", "00:00"),
        ("01:00", "22:00"),
    ],
)
def test_interval_schedule_handles_midnight(clock, expected):
    now = datetime.fromisoformat("2026-10-20T" + clock).replace(
        tzinfo=ZoneInfo("Asia/Shanghai")
    )
    ad = Advertisement(
        id="a", name="a", text="a", mode="interval", start="22:00", end="01:00"
    )
    point = next_ad_boundary(ad, now.timestamp())
    assert point > now.timestamp()
    assert (
        datetime.fromtimestamp(point, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["accepted", "unknown", "restart", "offline", "missed"]
)
async def test_ads_never_replay_or_retry_unknown(rules, monkeypatch, case):
    save_rules(
        rules,
        {
            "ads": [
                {
                    "id": "a",
                    "name": "a",
                    "text": "confirmed",
                    "times": ["09:00"],
                    "enabled": True,
                }
            ]
        },
    )
    now = time.time()
    rules.started = now - 10 if case != "restart" else now + 1
    cursor = {
        "account": "99",
        "group_id": "5",
        "rule_id": "a",
        "next_at": now
        - (rule_replies.ADMISSION_WINDOW_SECONDS + 1 if case == "missed" else 0.1),
    }
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_ad_cursors SET next_at=?", (cursor["next_at"],))
    rules.service.roster = AsyncMock()
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    adapter = SimpleNamespace(send_reply_text=AsyncMock(return_value=case))
    if case == "offline":
        adapter = None
    await rules.send_ad(adapter, cursor)
    await rules.send_ad(adapter, cursor)
    with rules.store.connect() as db:
        saved = db.execute("SELECT * FROM business_ad_cursors").fetchone()
        assert saved["next_at"] > time.time()
        assert saved["blocked"] == ("delivery_unknown" if case == "unknown" else "")
    if adapter:
        assert adapter.send_reply_text.call_count == (
            1 if case in {"accepted", "unknown"} else 0
        )


def test_interrupted_ad_pauses_after_restart_without_replay(rules):
    save_rules(
        rules,
        {
            "ads": [
                {
                    "id": "a",
                    "name": "a",
                    "text": "confirmed",
                    "times": ["09:00"],
                    "enabled": True,
                }
            ]
        },
    )
    with rules.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO business_rule_sends VALUES("
            "'interrupted','99','5','advertisement','a',?,'dispatched',?,'')",
            (time.time(), time.time()),
        )
    restarted = RuleReplies(rules.service)
    with restarted.store.connect() as db:
        assert (
            db.execute("SELECT blocked FROM business_ad_cursors").fetchone()[0]
            == "delivery_unknown"
        )
        receipt = db.execute("SELECT state,error FROM business_rule_sends").fetchone()
        assert tuple(receipt) == ("unknown", "process_interrupted")
    RuleReplies(rules.service)
    with rules.store.connect() as db:
        assert (
            db.execute("SELECT blocked FROM business_ad_cursors").fetchone()[0]
            == "delivery_unknown"
        )


@pytest.mark.asyncio
async def test_group_ownership_does_not_imply_robot_support_editing(rpc_scope):
    value, adapter, _ = rpc_scope
    event = chat_event(adapter)
    await value.verify_actor(event)
    with pytest.raises(OpenError, match="support_editor_required"):
        await value.support.command(event, "客服话术 new")
    assert value.support.read("99")["version"] == 0
    value.support.grant("99", "1", "1001", 0, True, "server", guard=lambda: None)
    assert value.support.editor(event.actor_identity)
    assert not value.support.editor(ActorIdentity("98", "1", "1001", "1"))
    assert not value.support.editor(ActorIdentity("99", "1", "wrong", "1"))
    assert not value.support.editor(ActorIdentity("99", "1", "1001"))


@pytest.mark.asyncio
async def test_native_demotion_revokes_rule_commit_and_pending_confirmation(rpc_scope):
    value, adapter, _ = rpc_scope
    event = chat_event(adapter)
    event.actor_identity = ActorIdentity("99", "1", "1001", "1")
    event.is_private_chat = lambda: False
    event.get_group_id = lambda: "5"
    _, _, guard = await value.rule_replies.context(event)
    value.member_evidence[("99", "5")]["1"]["groupRole"] = "GROUP_ROLE_MEMBER"
    with pytest.raises(OpenError, match="business_owner_revoked"):
        guard()
    with pytest.raises(OpenError, match="business_owner_revoked"):
        event.get_extra("_wsl_management_delivery_guard")()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["mention", "cooldown", "send_failure", "disabled"])
async def test_complete_ingress_marks_keyword_consumed_before_group_ai(
    rpc_scope, monkeypatch, case
):
    value, adapter, _ = rpc_scope
    await model_scope(value, adapter)
    adapter.send_reply_text = AsyncMock(return_value="accepted")
    save_rules(value.rule_replies, {"keywords": [keyword()]})
    event = chat_event(adapter)
    await value.verify_actor(event)
    event.is_private_chat = lambda: False
    event.get_group_id = lambda: "5"
    event.message_str = "链接"
    event.stop_event = Mock()
    event.set_extra(
        "wangshangliao_payload",
        {
            "created_at": int(time.time() * 1000),
            "mentions": [adapter.nim_account] if case == "mention" else [],
        },
    )
    monkeypatch.setattr(value.rule_replies, "send_guard", lambda *a, **k: None)
    if case == "send_failure":
        adapter.send_reply_text.side_effect = RuntimeError("offline failure")
    elif case == "disabled":
        monkeypatch.setattr(
            value.rule_replies,
            "send_guard",
            lambda *a, **k: (_ for _ in ()).throw(OpenError("disabled")),
        )
    if case == "cooldown":
        await value.rule_replies.observe(event)
        event.message_obj.message_id = "second"
    value.group_ai.observe = AsyncMock()
    await value.on_event(event)
    assert value.group_ai.observe.await_args.kwargs["consumed"] is True
    assert value.store.usage()["used"] == 0


@pytest.mark.asyncio
async def test_support_preview_loses_authority_after_revoke(rpc_scope):
    value, adapter, _ = rpc_scope
    event = chat_event(adapter)
    await value.verify_actor(event)
    value.support.grant("99", "1", "1001", 0, True, "server", guard=lambda: None)
    editor = value.support.editor(event.actor_identity)
    preview = value.support.preview(
        "99",
        0,
        {"wording": "account99 only", "links": ["https://example.test/"]},
        "native",
        "session",
        guard=lambda: None,
        editor=editor,
    )
    value.support.grant("99", "1", "1001", 1, False, "server", guard=lambda: None)
    with pytest.raises(OpenError, match="support_editor_revoked"):
        value.support.commit(
            "99", preview["token"], "native", "session", guard=lambda: None
        )
    assert value.support.read("99")["version"] == 0
    assert value.support.read("98")["config"]["links"] == []


@pytest.mark.parametrize(
    "config",
    [
        {"provider_id": "evil"},
        {"tools": ["code"]},
        {"api_key": "secret"},
        {"links": ["javascript:alert(1)"]},
        {"links": ["https://user:pass@example.test"]},
        {"style": "anything"},
        {"length": True},
        {"knowledge": [""]},
    ],
)
def test_support_schema_never_accepts_infrastructure(config):
    with pytest.raises(ValidationError):
        SupportConfiguration.model_validate(config)


@pytest.mark.parametrize("command", sorted(BUSINESS_COMMANDS))
def test_fixed_business_commands_cannot_fall_back_to_ai(command):
    assert recognize_command(command) == command
    assert recognize_command(command + " argument").startswith(command)
    assert recognize_integrated(command) == command
    assert recognize_integrated(command + " argument").startswith(command)


@pytest.mark.asyncio
async def test_server_support_api_rejects_extra_config_fields(rpc_scope):
    value, _, _ = rpc_scope
    with pytest.raises(ValidationError):
        await OpenRPC(value).dispatch(
            OpenCall(
                action="support_settings",
                actor="dashboard/test",
                payload={
                    "account": "99",
                    "operation": "preview",
                    "version": 0,
                    "config": {"wording": "valid", "tools": ["code"]},
                },
            )
        )
    assert value.support.read("99")["version"] == 0
