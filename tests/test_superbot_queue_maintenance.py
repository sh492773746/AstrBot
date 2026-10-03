"""Legacy buttons, recoverable archival and handler-scoped shutdown."""

# ruff: noqa: F811

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.main import Main


@pytest.mark.asyncio
async def test_legacy_points_button_uses_timed_mention_outbox(text_service):
    s = text_service
    event = update(s, "")
    event.message = None
    event.effective_user.username = "requester"
    event.callback_query = SimpleNamespace(id="old")
    await s.group.action(event, {"action": "points"})
    await s.group.action(event, {"action": "points"})
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_requests").fetchone()[0] == 1
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["text"].startswith("@requester\n")
    assert "reply_parameters" not in args
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.clock[0] + 10
    )


@pytest.mark.asyncio
async def test_archive_preserves_replay_keys_and_full_payload(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, "大10", 10)
    await s.text.deliver("-1001")
    original = s.store.db.execute(
        "SELECT text FROM gt_requests WHERE source=10"
    ).fetchone()[0]
    s.clock[0] += 31 * 86400
    s.text.archive_completed()
    assert not s.store.db.execute("SELECT 1 FROM gt_archive").fetchone()
    await s.text.cleanup()
    s.text.archive_completed()
    archive = s.store.db.execute(
        "SELECT request FROM gt_archive WHERE source=10"
    ).fetchone()[0]
    assert original.splitlines()[0] in archive
    assert (
        s.store.db.execute("SELECT status FROM gt_requests WHERE source=10").fetchone()[
            0
        ]
        == "archived"
    )
    await accept(s, "大10", 10)
    assert s.store.balance("2") == 990
    s.text.archive_completed()
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_archive").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_shutdown_waits_for_handler_not_transport_lifetime():
    plugin = Main(MagicMock(), {})
    plugin.store = MagicMock()
    entered, release, keep_transport = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(*args):
        entered.set()
        await release.wait()

    plugin.receive_update = handler

    async def transport():
        await plugin.receive(None, None)
        await keep_transport.wait()

    task = asyncio.create_task(transport())
    await entered.wait()
    stopping = asyncio.create_task(plugin.terminate())
    await asyncio.sleep(0)
    plugin.store.close.assert_not_called()
    release.set()
    await asyncio.wait_for(stopping, 1)
    assert not task.done()
    plugin.store.close.assert_called_once()
    keep_transport.set()
    await task
