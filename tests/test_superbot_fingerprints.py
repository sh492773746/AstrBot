"""Local sample matching and approval security; no network moderation."""

# ruff: noqa: F811
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot_ad_killer import enable, env, message  # noqa: F401

from data.plugins.astrbot_plugin_superbot.ad_fingerprint import Fingerprints, masked
from data.plugins.astrbot_plugin_superbot.store import Rejected

BODY = "专业课程限时优惠，招代理高返佣，私信咨询下单加入我们的学习计划"


def setup(env):
    enable(env)
    fp = env.runtime.fingerprints = Fingerprints(env.runtime)
    with env.runtime.store.tx() as db:
        env.runtime.store.put(db, "modules", {"moderation": True, "fingerprint": True})
    env.runtime.store.db.execute(
        "INSERT INTO fp_groups(chat,mode,actor,since) VALUES('-1001','observe','1',?)",
        (env.now[0],),
    )
    return fp


@pytest.mark.asyncio
async def test_pending_approval_global_match_and_stale_controls(env):
    fp = setup(env)
    identity = await fp.submit("1", "-1001", 90, BODY)
    assert fp.match("-1001", BODY) is None
    with pytest.raises(Rejected):
        fp.decide("2", identity, 1, "active")
    fp.decide("1", identity, 1, "active")
    assert fp.match("-1001", BODY)["automatic"]
    assert fp.match("-1002", BODY)["automatic"]
    with pytest.raises(Rejected):
        fp.decide("1", identity, 1, "deleted")
    fp.decide("1", identity, 2, "deleted")
    assert fp.match("-1001", BODY) is None
    with pytest.raises(Rejected):
        fp.decide("1", identity, 3, "active")


@pytest.mark.asyncio
async def test_masking_does_not_claim_exact_match(env):
    fp = setup(env)
    identity = await fp.submit("1", "-1001", 90, BODY + " https://first.example/path")
    fp.decide("1", identity, 1, "active")
    row = env.runtime.store.db.execute("SELECT body FROM fp_samples").fetchone()
    assert "first.example" not in row[0]
    result = fp.match("-1001", BODY + " https://other.example/path")
    assert result["reason"] == "similar"
    assert (
        fp.match("-1001", BODY + " https://first.example/path", reply=True)["automatic"]
        is False
    )
    assert masked("联系 @someone +8613812345678") == "联系 [联系信息] [联系信息]"


@pytest.mark.asyncio
async def test_observation_false_positive_and_expiry(env):
    fp = setup(env)
    identity = await fp.submit("1", "-1001", 90, BODY)
    fp.decide("1", identity, 1, "active")
    assert not await fp.inspect(message(BODY))
    env.runtime.bot.delete_message.assert_not_awaited()
    fp.false_positive("1", "-1001", 1)
    assert fp.match("-1001", BODY) is None
    assert fp.match("-1002", BODY)["automatic"]
    env.now[0] += 91 * 86400
    assert fp.match("-1002", BODY) is None


@pytest.mark.asyncio
async def test_closed_release_gate_prevents_auto(env):
    fp = setup(env)
    identity = await fp.submit("1", "-1001", 90, BODY)
    fp.decide("1", identity, 1, "active")
    env.runtime.store.db.execute("UPDATE fp_groups SET mode='auto'")
    await fp.inspect(message(BODY))
    env.runtime.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_and_similar_share_single_executor_and_short_mute(env):
    fp = setup(env)
    identity = await fp.submit("1", "-1001", 90, BODY)
    fp.decide("1", identity, 1, "active")
    env.runtime.store.db.execute("UPDATE fp_groups SET mode='auto'")
    fp.ready = lambda _: True
    for i in range(1, 4):
        assert await fp.inspect(message(BODY, number=i))
    assert env.runtime.bot.delete_message.await_count == 3
    assert env.runtime.bot.restrict_chat_member.await_args.kwargs["until_date"] == int(
        env.now[0] + 600
    )
    env.runtime.bot.ban_chat_member.assert_not_awaited()
    assert not await fp.inspect(message(BODY, number=3))
    assert env.runtime.bot.delete_message.await_count == 3


@pytest.mark.asyncio
async def test_edit_while_identity_lookup_cancels_penalty(env):
    fp = setup(env)
    identity = await fp.submit("1", "-1001", 90, BODY)
    fp.decide("1", identity, 1, "active")
    env.runtime.store.db.execute("UPDATE fp_groups SET mode='auto'")
    fp.ready = lambda _: True

    async def member(chat, uid):
        fp.invalidate(message("普通消息"))
        return SimpleNamespace(status="member")

    env.runtime.bot.get_chat_member = AsyncMock(side_effect=member)
    await fp.inspect(message(BODY))
    env.runtime.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sequence_is_observation_only_and_group_scoped(env):
    fp = setup(env)
    await fp.inspect(message("我们在招代理，提供课程优惠", number=1))
    await fp.inspect(message("想了解的私信我", number=2))
    row = env.runtime.store.db.execute("SELECT * FROM fp_events").fetchone()
    assert row["reason"] == "sequence" and row["status"] == "suspect"
    env.runtime.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sample_ui_approval_requires_manager(env):
    from data.plugins.astrbot_plugin_superbot.ad_fingerprint_ui import action

    setup(env)
    env.runtime.ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1, type="private"),
    )
    await action(
        env.runtime.ui.moderation_ui, update, {"action": "mod_ak_fp", "chat": "-1001"}
    )
    assert "无模型" in env.runtime.ui.render.await_args.args[1]


@pytest.mark.asyncio
async def test_restore_rejects_conflicts_and_is_single_use(env):
    from data.plugins.astrbot_plugin_superbot.store import encode

    fp = setup(env)
    member = SimpleNamespace(
        status="restricted",
        to_dict=lambda: {"status": "restricted", "until_date": 123456},
    )
    expected = encode({"member": encode(member.to_dict())})
    db = env.runtime.store.db
    db.execute(
        "INSERT INTO fp_restrictions VALUES('-1001',1,'2',?,'active',?)",
        (expected, env.now[0]),
    )
    env.runtime.moderation.save_restriction(
        "-1001", "2", {"member": encode(member.to_dict())}
    )
    env.runtime.moderation.check = AsyncMock()
    env.runtime.bot.get_chat_member = AsyncMock(return_value=member)
    changed = SimpleNamespace(
        to_dict=lambda: {"status": "restricted", "until_date": 123457}
    )
    env.runtime.bot.get_chat_member.return_value = changed
    with pytest.raises(Rejected, match="限制已变化"):
        await fp.restore("1", "-1001", 1)
    env.runtime.bot.restrict_chat_member.assert_not_awaited()
    env.runtime.bot.get_chat_member.return_value = member
    await fp.restore("1", "-1001", 1)
    with pytest.raises(Rejected):
        await fp.restore("1", "-1001", 1)
    assert env.runtime.bot.restrict_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_actual_sample_buttons_and_history_submission(env):
    from data.plugins.astrbot_plugin_superbot.ad_fingerprint_ui import action

    fp = setup(env)
    identity = await fp.submit("1", "-1001", 99, BODY)
    env.runtime.ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1, type="private"),
    )
    mod_ui = env.runtime.ui.moderation_ui
    await action(
        mod_ui, update, {"action": "mod_ak_fp_sample", "chat": "-1001", "id": identity}
    )
    button = env.runtime.ui.render.await_args.args[2][0][1]
    await action(mod_ui, update, button)
    confirmation = env.runtime.ui.render.await_args.args[2][0][1]
    await action(mod_ui, update, confirmation)
    assert fp.match("-1002", BODY)["automatic"]
    with pytest.raises(Rejected):
        await action(mod_ui, update, confirmation)


@pytest.mark.asyncio
async def test_allowlisted_link_does_not_whitelist_other_promotion(env):
    import json

    fp = setup(env)
    identity = await fp.submit("1", "-1001", 99, BODY)
    fp.decide("1", identity, 1, "active")
    config = json.loads(env.runtime.ad_killer.policy("-1001")["config"])
    config["domains"] = ["permitted.example"]
    env.runtime.store.db.execute(
        "UPDATE ak_policies SET config=?", (json.dumps(config),)
    )
    env.runtime.store.db.execute("UPDATE fp_groups SET mode='auto'")
    fp.ready = lambda _: True
    assert await fp.inspect(message(BODY + " https://permitted.example"))
    env.runtime.bot.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_5000_sample_index_benchmark(env):
    import hashlib
    import statistics
    import time

    from data.plugins.astrbot_plugin_superbot.ad_fingerprint import canonical, grams

    fp = setup(env)
    db = env.runtime.store.db
    with env.runtime.store.tx():
        for i in range(5000):
            body = BODY + hashlib.sha256(str(i).encode()).hexdigest()[:24]
            clean = canonical(body)
            db.execute(
                """INSERT INTO fp_samples(id,source_chat,source_message,body,digest,
                template,status,submitter,created,expires)
                VALUES(?,'-1001',?,?,?,?,'active','1',?,?)""",
                (
                    i + 1,
                    i + 1,
                    body,
                    hashlib.sha256(clean.encode()).hexdigest(),
                    clean,
                    env.now[0],
                    env.now[0] + 86400,
                ),
            )
            db.executemany(
                "INSERT INTO fp_grams VALUES(?,?)", [(g, i + 1) for g in grams(clean)]
            )
    measurements = []
    for i in range(100):
        text = BODY + hashlib.sha256(str(i).encode()).hexdigest()[:24]
        if i % 2:
            text += "额外文本"
        start = time.perf_counter()
        fp.match("-1001", text)
        measurements.append((time.perf_counter() - start) * 1000)
    p95 = statistics.quantiles(measurements, n=20)[18]
    print(f"fingerprint benchmark: samples=5000 requests=100 p95_ms={p95:.3f}")
    assert p95 < 50


@pytest.mark.asyncio
async def test_test_permit_is_exact_and_short_lived(env):
    fp = setup(env)
    store = env.runtime.store
    with store.tx() as db:
        store.put(db, "fp_release", {"version": "fingerprint-1", "offline_pass": True})
        store.put(
            db,
            "fp_test_permit",
            {
                "actor": "1",
                "chat": "-1001000000003",
                "uid": "2",
                "started": env.now[0],
                "expires": env.now[0] + 600,
                "messages": [12],
                "samples": [7],
            },
        )
    assert fp.test_allowed("-1001000000003", "2", 12, 7)
    assert not fp.test_allowed("-1001", "2", 12, 7)
    assert not fp.test_allowed("-1001000000003", "3", 12, 7)
    assert not fp.test_allowed("-1001000000003", "2", 13, 7)
    assert not fp.test_allowed("-1001000000003", "2", 12, 8)
    with store.tx() as db:
        store.put(db, "fp_release", {})
    assert not fp.test_allowed("-1001000000003", "2", 12, 7)
    permit = store.get("fp_test_permit")
    permit.update(version="fingerprint-1", authorization="owner_scoped_test_only")
    with store.tx() as db:
        store.put(db, "fp_test_permit", permit)
    assert fp.test_allowed("-1001000000003", "2", 12, 7)
    assert not fp.ready("-1001000000003")
    assert not fp.test_allowed("-1001", "2", 12, 7)
    assert not fp.test_allowed("-1001000000003", "3", 12, 7)
    assert not fp.test_allowed("-1001000000003", "2", 13, 7)
    assert not fp.test_allowed("-1001000000003", "2", 12, 8)
    env.now[0] += 601
    assert not fp.test_allowed("-1001000000003", "2", 12, 7)


@pytest.mark.asyncio
async def test_rollout_does_not_overwrite_existing_group_config(env):
    fp = setup(env)
    with env.runtime.store.tx() as db:
        env.runtime.store.put(
            db,
            "fp_release",
            {
                "version": "fingerprint-1",
                "offline_pass": True,
                "telethon_pass": True,
            },
        )
    result = await fp.begin_rollout("1")
    assert result == {"observing": [], "skipped": ["-1001"]}
    assert (
        env.runtime.store.db.execute("SELECT mode FROM fp_groups").fetchone()[0]
        == "observe"
    )


@pytest.mark.asyncio
async def test_legacy_ai_callback_is_retired(env):
    from data.plugins.astrbot_plugin_superbot.ad_killer_ui import action

    setup(env)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1, type="private"),
    )
    with pytest.raises(Rejected, match="已停用"):
        await action(
            env.runtime.ui.moderation_ui,
            update,
            {"action": "mod_ak_ai_save", "chat": "-1001"},
        )
