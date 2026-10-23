"""Offline AI queue, context isolation and punishment regression."""

# ruff: noqa: F811
import asyncio
from unittest.mock import AsyncMock

import pytest
from test_superbot_ad_killer import enable, env, message  # noqa: F401

from integrations.superbot_legacy.ad_ai_engine import AdAI


def configure(env, mode="observe"):
    runtime = env.runtime
    enable(env)
    ai = runtime.ad_ai = AdAI(runtime)
    with runtime.store.tx() as db:
        runtime.store.put(db, "modules", {"ad_ai": True, "moderation": True})
        runtime.store.put(
            db,
            "ad_ai_release",
            {
                "prompt": "context-noul-1",
                "offline_pass": True,
                "telethon_pass": True,
                "rotated_key": True,
            },
        )
    runtime.store.db.execute(
        "INSERT INTO ak_ai_groups(chat,mode,actor) VALUES('-1001',?,'1')", (mode,)
    )
    for i in range(30):
        runtime.store.db.execute(
            """INSERT INTO ak_ai_jobs(chat,message,digest,uid,version,policy_version,
            prompt,status,at,reviewed) VALUES('-1001',?,'fixture','2',1,1,
            'context-noul-1','candidate',1,1)""",
            (-i - 1,),
        )
    runtime.store.db.execute("UPDATE ak_ai_jobs SET at=?", (env.now[0] - 100,))
    ai.probability = AsyncMock(return_value=0.95)
    return ai


async def drain(ai):
    task = asyncio.create_task(ai.worker())
    try:
        await asyncio.wait_for(ai.queue.join(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_observation_dedup_no_punishment(env):
    ai = configure(env)
    ai.submit(message("买课程，私信我"))
    ai.submit(message("买课程，私信我"))
    await drain(ai)
    assert ai.probability.await_count == 2
    env.runtime.bot.delete_message.assert_not_awaited()
    assert (
        env.runtime.store.db.execute(
            "SELECT status FROM ak_ai_jobs WHERE message=1"
        ).fetchone()[0]
        == "candidate"
    )


@pytest.mark.asyncio
async def test_three_hits_mute_ten_minutes_without_ban(env):
    ai = configure(env, "auto")
    for i in range(3):
        ai.submit(message("购买课程私信我", number=i + 1))
        await drain(ai)
    assert env.runtime.bot.delete_message.await_count == 3
    call = env.runtime.bot.restrict_chat_member.await_args
    assert call.kwargs["until_date"] == int(env.now[0] + 600)
    env.runtime.bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_invalidates_queued_ad(env):
    ai = configure(env, "auto")
    ai.submit(message("推广广告"))
    edited = message("/help")
    edited.edited_message, edited.message = edited.message, None
    ai.submit(edited)
    await drain(ai)
    ai.probability.assert_not_awaited()
    env.runtime.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_gate_and_old_rule_win(env):
    ai = configure(env, "auto")
    ai.submit(message("推广广告"))
    env.runtime.store.db.execute(
        """INSERT INTO ak_hits(chat,message,uid,rules,action,version,status,step,at)
        VALUES('-1001',1,'2','["keywords"]','delete',1,'accepted','done',0)"""
    )
    await drain(ai)
    env.runtime.bot.delete_message.assert_not_awaited()
    ai.submit(message("广告", number=2))
    env.runtime.store.db.execute("UPDATE ak_ai_groups SET mode='off',version=version+1")
    await drain(ai)
    env.runtime.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_bound_context_redaction_and_circuit(env):
    ai = configure(env)
    ai.submit(message("联系 @someone https://example.com", number=1))
    ai.submit(message("大家好", number=2))
    snapshots = list(ai.queue._queue)
    assert "@someone" not in str(snapshots[1][2]["context"])
    assert "example.com" not in str(snapshots[1][2]["context"])
    for i in range(3, 36):
        ai.submit(message("大家好", number=i))
    assert ai.queue.qsize() == 32
    ai.probability = AsyncMock(side_effect=ValueError("mock"))
    await drain(ai)
    assert ai.probability.await_count == 3
    assert (
        env.runtime.store.db.execute(
            "SELECT count(*) FROM ak_ai_jobs WHERE status='circuit_open'"
        ).fetchone()[0]
        > 0
    )


@pytest.mark.asyncio
async def test_admin_never_classified(env):
    ai = configure(env, "auto")
    ai.submit(message("推广广告", user=1))
    await drain(ai)
    ai.probability.assert_not_awaited()
    env.runtime.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_release_evidence_prevents_execution(env):
    ai = configure(env, "auto")
    with env.runtime.store.tx() as db:
        env.runtime.store.put(db, "ad_ai_release", {})
    ai.submit(message("推广广告"))
    await drain(ai)
    env.runtime.bot.delete_message.assert_not_awaited()
    assert (
        env.runtime.store.db.execute(
            "SELECT status FROM ak_ai_jobs WHERE message=1"
        ).fetchone()[0]
        == "candidate"
    )


@pytest.mark.asyncio
async def test_runtime_requires_observation_age_and_distinct_reviews(env):
    ai = configure(env, "auto")
    db = env.runtime.store.db
    db.execute("UPDATE ak_ai_groups SET observed_since=?", (env.now[0],))
    assert not ai.release_ready("-1001")
    db.execute("UPDATE ak_ai_groups SET observed_since=0")
    db.execute("UPDATE ak_ai_jobs SET reviewed=0 WHERE message=-1")
    assert not ai.release_ready("-1001")
    db.execute("UPDATE ak_ai_jobs SET reviewed=1 WHERE message=-1")
    assert ai.release_ready("-1001")
    db.execute("UPDATE ak_ai_jobs SET reviewed=-1 WHERE message=-1")
    assert not ai.release_ready("-1001")


@pytest.mark.asyncio
async def test_execution_can_complete_after_decision_deadline(env):
    import time

    ai = configure(env, "auto")
    ai.submit(message("推广广告"))
    job = env.runtime.store.db.execute(
        "SELECT * FROM ak_ai_jobs WHERE message=1"
    ).fetchone()
    original = env.runtime.ad_killer._execute

    async def delayed(*args, **kwargs):
        await asyncio.sleep(0.03)
        return await original(*args, **kwargs)

    env.runtime.ad_killer._execute = delayed
    assert await ai.execute(job, time.monotonic() + 0.01) == "accepted"
    env.runtime.bot.delete_message.assert_awaited_once()
