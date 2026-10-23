"""Isolated native fixtures never re-enable retired public chat commands."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fcntl", reason="POSIX-only maintenance CLI")

from scripts.check_wsl_pair_live import (
    GROUP,
    ORDINARY,
    PEER,
    RECEIVER,
    PairCheck,
)


@pytest.fixture
def native_check(monkeypatch):
    """Create an in-memory test journal without production sessions or files."""
    with closing(sqlite3.connect(":memory:")) as journal:
        journal.execute(
            "CREATE TABLE attempts(id TEXT PRIMARY KEY,run TEXT,kind TEXT,state TEXT,created REAL)"
        )
        check = PairCheck.__new__(PairCheck)
        check.phases = ["manual", "native-extra"]
        check.ordinary = object()
        check.business = SimpleNamespace(
            fields=[None, None, None, PEER], request=AsyncMock(return_value={})
        )
        check.journal = journal
        check.marker = "isolated-fixture"
        check.last_write = 0
        check.mute_possible = True
        check.guard = AsyncMock()
        check.checked_roster = AsyncMock()
        check.record = lambda *_args, **_kwargs: None
        monkeypatch.setattr("scripts.check_wsl_pair_live.asyncio.sleep", AsyncMock())
        yield check


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,route,params",
    [
        (
            "mute",
            "/v1/group/set-member-mute",
            {"groupId": int(GROUP), "userId": int(ORDINARY), "min": 1},
        ),
        (
            "unmute",
            "/v1/group/member-mute-cancel",
            {"groupId": int(GROUP), "userId": int(ORDINARY)},
        ),
        (
            "mute_all",
            "/v1/group/set-group-mute",
            {"groupId": int(GROUP), "muteMode": "MUTE_MEMBER"},
        ),
        (
            "unmute_all",
            "/v1/group/set-group-mute",
            {"groupId": int(GROUP), "muteMode": "MUTE_NO"},
        ),
    ],
)
async def test_native_uses_durable_executor_and_exact_controlled_target(
    native_check, monkeypatch, action, route, params
):
    check = native_check

    async def execute(
        client, instance, account, operation, kind, group, member, **opts
    ):
        assert (instance, account, kind, group) == (
            RECEIVER,
            PEER,
            action,
            int(GROUP),
        )
        assert member == (int(ORDINARY) if action in {"mute", "unmute"} else 0)
        assert opts == {"minutes": 1, "authorize": check.guard}
        assert check.journal.execute(
            "SELECT state FROM attempts WHERE id=?", (operation,)
        ).fetchone() == ("unknown",)
        await client.request(route, params, authorize=check.guard)
        return {
            "operation": operation,
            "status": "accepted",
            "action": kind,
            "group": group,
            "member": member,
        }

    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.moderation.execute", execute
    )
    result = await check.test_native(action)
    check.checked_roster.assert_awaited_once()
    check.business.request.assert_awaited_once_with(
        route, params, authorize=check.guard
    )
    assert check.journal.execute(
        "SELECT state FROM attempts WHERE id=?", (result["operation"],)
    ).fetchone() == ("accepted",)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["phase", "identity", "fixture", "unknown_action"])
async def test_native_refuses_out_of_scope_before_any_intent(
    native_check, monkeypatch, change
):
    check, action = native_check, "mute"
    if change == "phase":
        check.phases = ["commands"]
    elif change == "identity":
        check.business.fields[3] = ORDINARY
    elif change == "fixture":
        check.ordinary = None
    else:
        action = "kick"
    execute = AsyncMock()
    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.moderation.execute", execute
    )
    with pytest.raises(RuntimeError, match="controlled test phase"):
        await check.test_native(action)
    execute.assert_not_awaited()
    assert not check.journal.execute("SELECT 1 FROM attempts").fetchone()


@pytest.mark.asyncio
async def test_native_rejects_arbitrary_route_and_preserves_unknown_intent(
    native_check, monkeypatch
):
    check = native_check

    async def execute(client, *_args, **_kwargs):
        await client.request(
            "/v1/group/member-mute-cancel",
            {"groupId": 1516387, "userId": int(ORDINARY)},
        )

    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.moderation.execute", execute
    )
    with pytest.raises(RuntimeError, match="fixed test scope"):
        await check.test_native("mute")
    check.business.request.assert_not_awaited()
    assert check.journal.execute("SELECT state FROM attempts").fetchall() == [
        ("unknown",)
    ]


@pytest.mark.asyncio
async def test_native_rechecks_authority_after_request_cooldown(
    native_check, monkeypatch
):
    check = native_check
    check.guard.side_effect = [None, RuntimeError("revoked")]

    async def execute(client, *_args, **_kwargs):
        await client.request(
            "/v1/group/set-member-mute",
            {"groupId": int(GROUP), "userId": int(ORDINARY), "min": 1},
        )

    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.moderation.execute", execute
    )
    with pytest.raises(RuntimeError, match="revoked"):
        await check.test_native("mute")
    check.business.request.assert_not_awaited()
    assert check.journal.execute("SELECT state FROM attempts").fetchall() == [
        ("unknown",)
    ]


@pytest.mark.asyncio
async def test_retired_chat_denial_never_becomes_a_manual_mute_origin(
    native_check, monkeypatch
):
    """A denied public command cannot create a phantom native cleanup obligation."""
    check = native_check
    check.window = False
    check.expected = {}
    check.results = []
    check.manual_mute_message = ""

    async def deliver(self, text, expected):
        self.results.append(
            {
                "input": text,
                "incoming": "retired-command-message",
                "reply": expected,
                "passed": True,
            }
        )

    monkeypatch.setattr("scripts.check_wsl_tree_menu_live.MenuCheck.send", deliver)
    await check.command("禁言 3 1", "不再接受这些聊天命令")
    assert check.manual_mute_message == ""
    assert not check.journal.execute("SELECT 1 FROM attempts").fetchone()
