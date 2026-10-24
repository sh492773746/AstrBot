"""Deterministic moderation stays independent of chat throttles and reloads."""

import asyncio
import copy
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import content_rules, policy
from astrbot.builtin_stars.wangshangliao_moderation.flood import FloodRuntime
from astrbot.core.pipeline.rate_limit_check.stage import RateLimitStage
from tests.test_wangshangliao_flood import fresh_bot, ingest

bot = fresh_bot


def runtime_for(bot, **kwargs):
    """Build an isolated worker with the production per-session admin lookup.

    Args:
        bot: Temporary native adapter with real inbox and action ledgers.
        **kwargs: Bounded worker settings.

    Returns:
        The plugin's native rule worker.
    """
    return FloodRuntime(
        SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[bot]),
            get_config=lambda _: {"admins_id": []},
        ),
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "processing", "processed"])
async def test_keyword_review_is_independent_of_chat_rate_limit(bot, state):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    limiter = RateLimitStage()
    limiter.rate_limit_count = 1
    limiter.rate_limit_time = timedelta(seconds=60)
    limiter.rl_strategy = "stall"
    limiter.event_timestamps["fixture-session"].append(datetime.now())
    waiting = asyncio.create_task(
        limiter.process(SimpleNamespace(unified_msg_origin="fixture-session"))
    )
    try:
        await asyncio.sleep(0)
        assert not waiting.done()
        payload = await ingest(bot, "100", "fixture-ad")
        assert "_ingress_flood" not in payload
        assert payload["_ingress_content"]["category"] == "mute_keyword"
        await bot.ledger.mark("1", "5", "100", state)
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert not waiting.done()
        assert bot.calls == [("recall", "100", "22")]
        await policy.handle(bot, "5", "100", payload)
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == [("recall", "100", "22")]
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "image", "video", "forward"])
async def test_high_confidence_ads_use_the_fastlane_without_custom_keywords(bot, kind):
    bot.config["moderation"]["flood_enabled"] = False
    runtime = runtime_for(bot)
    try:
        payload = await ingest(bot, "100", "加我vx:abc123456", kind=kind)
        assert payload["_ingress_content"]["category"] == "external_promotion"
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == [("recall", "100", "22")]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_group_combinations_keep_all_terms_and_group_isolation(bot):
    rule = {"name": "fixture", "terms": ["fixture-a", "fixture-b"], "enabled": True}
    bot.config.update(
        _open_groups={
            "5": {
                "advertising": True,
                "flood": False,
                "keywords": [],
                "custom_rules": [rule],
            },
            "6": {"advertising": True, "flood": False, "keywords": []},
        },
        _open_native_roles={"5": "GROUP_ROLE_ADMIN", "6": "GROUP_ROLE_ADMIN"},
        _open_revisions={"5": "group-5", "6": "group-6"},
    )
    runtime = runtime_for(bot)
    bot.config["enabled_groups"].append("6")
    try:
        partial = await ingest(bot, "100", "fixture-a")
        other = await ingest(bot, "101", "fixture-a fixture-b", group="6")
        match = await ingest(bot, "102", "fixture-a fixture-b")
        assert "_ingress_content" not in partial
        assert "_ingress_content" not in other
        assert match["_ingress_content"]["category"] == "external_promotion"
        # The synthetic native port has no text transport for public warnings.
        from unittest.mock import AsyncMock

        bot.send_reply_text = AsyncMock(return_value={"status": "accepted"})
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert [call for call in bot.calls if call[0] == "recall"] == [
            ("recall", "102", "22")
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "revision",
        "epoch",
        "account",
        "permission",
        "advertising_off",
        "native_admin",
        "managed",
        "stopped",
        "closed",
        "forged_group",
        "forged_category",
        "stale",
    ],
)
async def test_fastlane_never_weakens_sanction_authorization(bot, monkeypatch, change):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    payload = await ingest(bot, "100", "fixture-ad")
    if change == "revision":
        bot.config["_business_revision"] = "new"
    elif change == "epoch":
        bot.config["_business_epoch"] = 2
    elif change == "account":
        bot.account = "9"
    elif change == "permission":
        bot.config["moderation"]["permissions"] = {}
    elif change == "advertising_off":
        bot.config["moderation"]["advertising_enabled"] = False
    elif change == "native_admin":
        bot.get_moderation_members.return_value["groupMemberInfo"][1]["groupRole"] = (
            "GROUP_ROLE_ADMIN"
        )
    elif change == "managed":
        from astrbot.core.platform.sources.wangshangliao import event

        monkeypatch.setattr(event, "is_managed_account", lambda _: True)
    elif change == "stopped":
        bot.stopping.set()
    elif change == "closed":
        runtime.active = False
    elif change == "forged_group":
        payload["_ingress_content"]["group"] = "6"
    elif change == "forged_category":
        payload["_ingress_content"]["category"] = "external_promotion"
    else:
        payload["recall_route"]["time"] = str(int((time.time() - 301) * 1000))
    try:
        await runtime.execute(("fixture", "1", "5", "2"), bot, "100", payload, None)
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_reload_does_not_replay_pre_activation_ads(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    await ingest(bot, "100", "fixture-ad")
    runtime = runtime_for(bot)
    try:
        await ingest(bot, "101", "fixture-ad")
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == [("recall", "101", "22")]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_unknown_recall_is_not_retried_by_pipeline_or_replacement_worker(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    bot.nim.request.side_effect = TimeoutError()
    try:
        payload = await ingest(bot, "100", "fixture-ad")
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.nim.request.await_count == 1
        await policy.handle(bot, "5", "100", payload)
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.nim.request.await_count == 1
    finally:
        await runtime.close()
    replacement = runtime_for(bot)
    try:
        await replacement.step()
        await asyncio.gather(*replacement.workers.values())
        assert bot.nim.request.await_count == 1
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_fastlane_backlog_is_bounded_and_scans_past_benign_messages(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot, slots=1, backlog=2)
    bot.diagnostics.gauges = {}
    try:
        for n in range(12):
            await ingest(bot, str(100 + n), "fixture-ad" if n % 2 else "hello")
        for _ in range(10):
            await runtime.step()
            assert sum(len(lane) for lane in runtime.pending.values()) <= 2
            assert len(runtime.workers) <= 1
            await asyncio.gather(*runtime.workers.values())
        assert len([call for call in bot.calls if call[0] == "recall"]) == 6
        assert len([call for call in bot.calls if call[0] == "mute"]) == 3
        assert bot.diagnostics.gauges["rule_queue_full"] > 0
        assert bot.diagnostics.gauges["rule_pending"] == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "during_directory"])
async def test_new_fastlane_retains_exact_speech_exemption_checks(
    bot, tmp_path, monkeypatch, when
):
    from astrbot.builtin_stars.wangshangliao_moderation import member_exemptions

    monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    payload = await ingest(bot, "100", "fixture-ad")

    def grant():
        member_exemptions.set_exemption(
            "fixture",
            "1",
            "5",
            "2",
            "22",
            enabled=True,
            actor="server/test",
            reason="Isolated fastlane regression",
            guard=lambda: None,
        )

    if when == "before":
        grant()
    else:
        roster = bot.get_moderation_members.return_value

        async def grant_during_directory(_):
            grant()
            return roster

        bot.get_moderation_members.side_effect = grant_during_directory
    try:
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        await policy.handle(bot, "5", "100", payload)
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_busy_benign_traffic_does_not_hide_a_later_ad(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    try:
        for n in range(300):
            await ingest(bot, str(1000 + n), "hello")
        await ingest(bot, "2000", "fixture-ad")
        for _ in range(2):
            await runtime.step()
            await asyncio.gather(*runtime.workers.values())
        assert bot.calls == [("recall", "2000", "22")]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["advertising", "semantic", "command", "history"])
async def test_ingress_does_not_override_disabled_rules_or_semantic_review(bot, switch):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    if switch == "advertising":
        bot.config["moderation"]["advertising_enabled"] = False
    elif switch == "semantic":
        bot.config["moderation"]["semantic"] = {"enabled": True}
    payload = await ingest(
        bot,
        "100",
        "添加关键词 fixture-ad" if switch == "command" else "fixture-ad",
        stamp=int((time.time() - 301) * 1000) if switch == "history" else None,
    )
    assert "_ingress_content" not in payload


@pytest.mark.parametrize(
    "text,name,expected",
    [
        ("名字就是导航", "fixture-example.top", "external_promotion"),
        ("名紫就是导航", "Example.CC", "external_promotion"),
        ("看名字里的导航", "站点example.top", "external_promotion"),
        ("名字就是导航", "普通成员", ""),
        ("看看我的名字", "example.top", ""),
        ("我的名字就是导航吗？", "example.top", ""),
        ("举报：名字就是导航", "example.top", ""),
        ("不要点名字里的导航", "example.top", ""),
        ("名字就是导航", "", ""),
    ],
)
def test_name_navigation_requires_explicit_solicitation_and_real_domain(
    text, name, expected
):
    assert content_rules.classify(text, name=name) == expected


def test_combination_names_are_labels_not_implicit_or_keywords():
    rule = {"name": "名字就是", "terms": ["名紫", "看名字"], "enabled": True}
    assert not content_rules.keyword_category(
        "名字就是导航", {"custom_rules": [copy.deepcopy(rule)]}
    )
