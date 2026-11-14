"""Explain blacklist target failures without changing lists or submitting actions."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import dashboard
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.dashboard.responses import ApiError
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_member_blacklist import direct, enroll, mention
from tests.test_wangshangliao_member_blacklist import scope as scope

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["mention", "reply"])
@pytest.mark.parametrize("remove", [False, True])
async def test_absent_member_feedback_does_not_claim_departure_or_change_state(
    scope, source, remove
):
    value, event, rows, store = scope
    if remove:
        enroll(event, store)
    before = store.read("1", "5")
    command = "解除黑名单" if remove else "黑名单"
    text = f"@Target {command}" if source == "mention" else command
    mention(event, text=text if source == "mention" else f"@Target {command}")
    payload = event.get_extra("wangshangliao_payload")
    if source == "reply":
        await event.platform.ledger.ingest(
            "1",
            "5",
            "original",
            {"sender": "3", "recall_route": {"peer": "903", "client": "client"}},
            state="done",
        )
        payload.update(
            mentions=[],
            mention_spans=[],
            reply={"server": "original", "client": "client"},
        )
    payload.update(sender="2", created_at=time.time() * 1000)
    rows.pop("3")

    reply = await step(value, event, text, native=True)

    assert "当前群成员名单中找不到该成员" in reply
    assert "可能已退群或成员资料尚未同步" in reply
    assert "本次未修改黑名单，也未提交禁言或解禁" in reply
    assert "请先核实该成员是否仍在本群" in reply
    assert "请检查命令" not in reply
    assert "不要手打昵称" not in reply
    assert store.read("1", "5") == before
    assert store.pending("1") == []
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["reply", "duplicate_peer"])
async def test_changed_or_ambiguous_identity_has_distinct_feedback(scope, source):
    value, event, rows, store = scope
    text = "@Target 黑名单"
    mention(event, text=text)
    payload = event.get_extra("wangshangliao_payload")
    if source == "reply":
        await event.platform.ledger.ingest(
            "1",
            "5",
            "original",
            {"sender": "3", "recall_route": {"peer": "903", "client": "client"}},
            state="done",
        )
        payload.update(
            mentions=[],
            mention_spans=[],
            reply={"server": "original", "client": "client"},
        )
        rows["3"]["nimId"] = "999"
        text = "黑名单"
    else:
        rows["4"] = {**rows["3"], "userId": "4"}
    payload.update(sender="2", created_at=time.time() * 1000)

    reply = await step(value, event, text, native=True)

    assert "当前成员身份绑定不一致或不唯一" in reply
    assert "可能已退群" not in reply
    assert store.read("1", "5") == (0, [])
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["missing", "wrong_client", "conflict"])
async def test_unverified_quote_and_conflicting_targets_have_distinct_feedback(
    scope, source
):
    value, event, rows, store = scope
    await event.platform.ledger.ingest(
        "1",
        "5",
        "original",
        {"sender": "3", "recall_route": {"peer": "903", "client": "client"}},
        state="done",
    )
    text = "黑名单"
    payload = {
        "reply": {
            "server": "missing" if source == "missing" else "original",
            "client": "wrong" if source == "wrong_client" else "client",
        },
        "sender": "2",
        "created_at": time.time() * 1000,
    }
    if source == "conflict":
        rows["4"] = {**rows["3"], "userId": "4", "nimId": "904"}
        text = "@Target 黑名单"
        mention(event, peer="904", text=text)
        payload.update(event.get_extra("wangshangliao_payload"))
    elif source == "wrong_client":
        rows.pop("3")
    event.set_extra("wangshangliao_payload", payload)

    reply = await step(value, event, text, native=True)

    if source == "conflict":
        assert "@成员与回复对象不是同一人" in reply
    else:
        assert "无法核实回复的同群原消息" in reply
        assert "记录可能缺失或引用不匹配" in reply
    assert "可能已退群" not in reply
    assert store.read("1", "5") == (0, [])
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["span", "many", "plain"])
async def test_invalid_mention_does_not_claim_member_is_missing(scope, change):
    value, event, rows, store = scope
    mention(event)
    payload = event.get_extra("wangshangliao_payload")
    rows.pop("3")
    if change == "span":
        payload["mention_spans"][0]["end"] -= 1
    elif change == "many":
        payload["mentions"].append("904")
    else:
        payload.update(mentions=[], mention_spans=[])

    with pytest.raises(OpenError, match="^blacklist_target$"):
        await direct(value, event)

    assert store.read("1", "5") == (0, [])
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("blacklist_member_unavailable", "可能已退群或成员资料尚未同步"),
        ("blacklist_identity_changed", "当前成员身份绑定不一致或不唯一"),
        ("blacklist_target_conflict", "@成员与回复对象不是同一人"),
    ],
)
async def test_dashboard_maps_target_errors_without_generic_format_blame(
    monkeypatch, reason, message
):
    monkeypatch.setattr(
        dashboard,
        "request",
        SimpleNamespace(_request=SimpleNamespace(method="GET"), username="test"),
    )
    page = dashboard.DashboardPage(SimpleNamespace())
    page.authorize = AsyncMock()
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            active=True,
            integrated=True,
            rpc=SimpleNamespace(
                lock=asyncio.Lock(),
                dispatch=AsyncMock(side_effect=OpenError(reason)),
            ),
        )
    )

    with pytest.raises(ApiError, match=message):
        await page.open_service()
