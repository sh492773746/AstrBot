"""Real request-local authorization remains task-, actor-, store- and group-bound."""

# ruff: noqa: F811

import asyncio

import pytest
from superbot_authority import designate_admin
from test_superbot_ad_killer import Member, env  # noqa: F401

from data.plugins.astrbot_plugin_superbot.ad_killer import defaults
from data.plugins.astrbot_plugin_superbot.group_authority import (
    CURRENT,
    GroupAuthority,
    VerificationUnavailable,
)
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store


@pytest.mark.asyncio
async def test_group_owner_needs_request_and_proof_is_cleared_on_exit(env):
    runtime = env.runtime
    assert CURRENT.get() is None
    with pytest.raises(Rejected):
        runtime.ad_killer.save("-1001", "1", 0, defaults(), True)
    async with GroupAuthority(runtime).session("1", "-1001") as proof:
        assert proof.task is asyncio.current_task()
        runtime.ad_killer.save("-1001", "1", 0, defaults(), True)
    assert CURRENT.get() is None
    with pytest.raises(Rejected):
        runtime.ad_killer.save("-1001", "1", 1, defaults(), False)
    assert runtime.ad_killer.policy("-1001")["enabled"]


@pytest.mark.asyncio
async def test_child_task_cannot_inherit_parent_authorization(env):
    runtime = env.runtime

    async def child(proof):
        assert CURRENT.get() is proof
        with pytest.raises(Rejected):
            runtime.ad_killer.save("-1001", "1", 0, defaults(), True)
        async with GroupAuthority(runtime).session("1", "-1001") as own:
            assert own.task is asyncio.current_task()
            assert own.task is not proof.task
            runtime.store.require("1", "moderation", chat="-1001")

    async with GroupAuthority(runtime).session("1", "-1001") as proof:
        await asyncio.create_task(child(proof))
        assert CURRENT.get() is proof
        runtime.store.require("1", "moderation", chat="-1001")
    assert runtime.ad_killer.policy("-1001")["version"] == 0
    assert CURRENT.get() is None


@pytest.mark.parametrize(
    "uid,chat,scope",
    [("2", "-1001", "game"), ("1", "-1002", "ads"), ("1", "-1001", "owner")],
)
@pytest.mark.asyncio
async def test_proof_cannot_change_actor_group_or_capability(env, uid, chat, scope):
    runtime = env.runtime
    async with GroupAuthority(runtime).session("1", "-1001"):
        with pytest.raises(Rejected):
            runtime.store.require(uid, scope, chat=chat)


@pytest.mark.asyncio
async def test_proof_cannot_authorize_another_store(env, tmp_path):
    other = Store(tmp_path / "other.sqlite3", "1")
    try:
        async with GroupAuthority(env.runtime).session("1", "-1001"):
            with pytest.raises(Rejected):
                other.require("1", "moderation", chat="-1001")
    finally:
        other.close()


@pytest.mark.asyncio
async def test_group_revision_invalidates_current_proof(env):
    runtime = env.runtime
    async with GroupAuthority(runtime).session("1", "-1001"):
        runtime.store.db.execute(
            "UPDATE mod_groups SET version=version+1 WHERE chat='-1001'"
        )
        with pytest.raises(Rejected):
            runtime.ad_killer.save("-1001", "1", 0, defaults(), True)
    async with GroupAuthority(runtime).session("1", "-1001"):
        runtime.ad_killer.save("-1001", "1", 0, defaults(), True)


@pytest.mark.asyncio
async def test_designation_revocation_invalidates_admin_proof(env):
    runtime = env.runtime
    env.members[2] = Member("administrator")
    with pytest.raises(Rejected):
        async with GroupAuthority(runtime).session("2", "-1001"):
            pytest.fail("Undesignated administrators must not enter the session")
    await designate_admin(runtime, "2")
    async with GroupAuthority(runtime).session("2", "-1001"):
        runtime.store.require("2", "game", chat="-1001")
        await designate_admin(runtime, "2", enabled=False)
        with pytest.raises(Rejected):
            runtime.store.require("2", "game", chat="-1001")
    with pytest.raises(Rejected):
        async with GroupAuthority(runtime).session("2", "-1001"):
            pytest.fail("Revoked administrators must not enter the session")
    assert not runtime.store.allowed("2")
    assert CURRENT.get() is None


@pytest.mark.asyncio
async def test_failed_lookup_and_exception_leave_no_proof(env):
    runtime = env.runtime
    runtime.bot.get_chat.side_effect = TimeoutError("offline fixture")
    with pytest.raises(VerificationUnavailable):
        async with GroupAuthority(runtime).session("1", "-1001"):
            pytest.fail("Unavailable Telegram identity must deny authorization")
    assert CURRENT.get() is None
    runtime.bot.get_chat.side_effect = None
    with pytest.raises(ValueError, match="fixture"):
        async with GroupAuthority(runtime).session("1", "-1001"):
            raise ValueError("fixture")
    assert CURRENT.get() is None
