"""Behavioral coverage for eligibility, recovery, retention, and the HTTP API."""

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from collector.api import create_app
from collector.runtime import Collector
from collector.store import Store

TOKEN = "test-secret-not-for-deployment-1234567890"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "test.sqlite3")
    yield value
    value.db.close()


@pytest.fixture
def configured(store):
    now = time.time() - 3600
    store.configure("groups", -100123, "Group", True, now)
    store.configure("people", 7, "Alice", True, now)
    store.configure("people", 8, "Bob", True, now)
    store.configure_watch(-100123, 7, True, now)
    store.configure_watch(-100123, 8, True, now)
    return store


class FakeClient:
    def __init__(self):
        self.handlers = []
        self.history = []
        self.options = []
        self.fail = False
        self.get_dialogs = AsyncMock(
            return_value=[SimpleNamespace(id=-100123, name="Group", is_group=True)]
        )
        self.get_entity = AsyncMock(return_value=types.User(id=7, first_name="Alice"))

    def add_event_handler(self, callback, event):
        self.handlers.append(callback)

    def is_connected(self):
        return True

    async def __call__(self, request):
        return None

    async def iter_messages(self, group, **kwargs):
        self.options.append(kwargs)
        for message in self.history:
            if message.id > kwargs["min_id"]:
                yield message
        if self.fail:
            raise ConnectionError("Sensitive upstream details")


def test_intervals_and_rename(configured):
    s = configured
    now = time.time()
    s.configure("people", 7, "Alice", False, now - 200)
    s.configure("people", 7, "Alice", True, now - 100)
    s.configure("people", 7, "Renamed", True, now - 50)
    assert s.record(-100123, 1, 7, now - 300, "before pause", "text")
    assert not s.record(-100123, 2, 7, now - 150, "paused", "text")
    assert s.record(-100123, 3, 7, now - 80, "resumed", "text")
    assert not s.record(-100123, 4, 999, now - 80, "untracked", "text")
    assert not s.record(-999, 4, 7, now - 80, "other group", "text")
    assert not s.record(-100123, 4, -7, now - 80, "anonymous", "text")
    assert not s.record(-100123, 4, 7, now - 5000, "before enrollment", "text")
    assert s.stats(now - 3600, now, now - 3600)[0]["count"] == 2
    assert (
        s.db.execute(
            "SELECT COUNT(*) FROM intervals WHERE kind='people' AND id=7"
        ).fetchone()[0]
        == 2
    )


def test_group_disable_boundaries(configured):
    now = time.time()
    configured.configure("groups", -100123, "Group", False, now - 200)
    configured.configure("groups", -100123, "Group", True, now - 100)
    assert not configured.record(-100123, 1, 7, now - 200, "excluded", "text")
    assert configured.record(-100123, 2, 7, now - 100, "included", "text")


def test_dedup_edit_delete_and_zero_ranking(configured):
    now = time.time()
    for _ in range(3):
        configured.record(-100123, 1, 7, now - 100, "original", "text")
    configured.record(-100123, 1, 7, now - 100, "edited", "text", now - 20)
    configured.record(-100123, 1, 7, now - 100, "old edit", "text", now - 30)
    result = configured.messages(now - 3600, now, 7, 0, 0)
    assert result["items"][0]["text"] == "edited"
    rows = configured.stats(now - 3600, now, now - 3600)
    assert [(r["id"], r["count"]) for r in rows] == [(7, 1), (8, 0)]
    configured.delete(-100123, [1])
    configured.record(-100123, 1, 7, now - 100, "resurrect", "text", now - 1)
    item = configured.messages(now - 3600, now, 7, 0, 0)["items"][0]
    assert item["deleted"] and item["text"] is None
    assert configured.stats(now - 3600, now, now - 3600)[0]["count"] == 1


def test_retention_preserves_statistics_and_dedup(configured):
    now = time.time()
    configured.record(-100123, 1, 7, now - 10, "private text", "text")
    configured.prune(now + 91 * 86400)
    configured.record(-100123, 1, 7, now - 10, "replayed text", "text")
    assert configured.messages(now - 3600, now, 7, 0, 0)["items"][0]["text"] is None
    assert configured.stats(now - 3600, now, now - 3600)[0]["count"] == 1


def test_pagination_and_cumulative(configured):
    now = time.time()
    for index in range(12):
        configured.record(-100123, index + 1, 7, now - 300 + index, "body", "text")
    configured.record(-100123, 20, 8, now - 10, "other", "text")
    first = configured.messages(now - 3600, now, 7, -100123, 0)
    second = configured.messages(now - 3600, now, 7, -100123, 1)
    assert len(first["items"]) == 10 and first["has_next"]
    assert len(second["items"]) == 2 and not second["has_next"]
    assert set(r["message_id"] for r in first["items"]).isdisjoint(
        r["message_id"] for r in second["items"]
    )
    rows = configured.stats(now - 60, now, now - 3600)
    assert rows[0]["id"] == 8
    assert rows[1]["count"] == 0 and rows[1]["cumulative"] == 12


async def test_recovery_cursor_is_not_live_cursor(configured):
    client = FakeClient()
    service = Collector(client, configured)
    now = datetime.now(timezone.utc)
    client.history = [
        types.Message(
            id=i,
            peer_id=types.PeerChannel(123),
            from_id=types.PeerUser(7),
            date=now,
            message=str(i),
        )
        for i in (1, 2, 3)
    ]
    service.ingest(-100123, client.history[-1])
    assert configured.db.execute("SELECT message_id FROM progress").fetchone()[0] == 0
    await service.reconcile({"id": -100123})
    assert configured.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    assert configured.db.execute("SELECT message_id FROM progress").fetchone()[0] == 3
    await service.reconcile({"id": -100123})
    assert client.options[-1]["min_id"] == 3
    assert configured.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3


async def test_partial_recovery_and_gap(configured):
    client = FakeClient()
    service = Collector(client, configured)
    client.fail = True
    with pytest.raises(ConnectionError):
        await service.reconcile({"id": -100123})
    configured.gap(-100123, "ConnectionError", time.time())
    configured.gap(-100123, "ConnectionError", time.time())
    health = service.status()
    assert health["incomplete"] and len(health["gaps"]) == 1
    client.fail = False
    await service.reconcile({"id": -100123})
    service.state = "connected"
    assert service.status()["gaps"][0]["end"] is not None
    assert service.status()["incomplete"]


async def test_identity_media_and_unlocatable_deletion(configured):
    service = Collector(FakeClient(), configured)
    now = datetime.now(timezone.utc)
    service.ingest(
        -100123,
        types.Message(
            id=1,
            peer_id=types.PeerChannel(123),
            from_id=types.PeerChannel(9),
            date=now,
            message="anonymous",
        ),
    )
    service.ingest(
        -100123,
        types.MessageService(
            id=2,
            peer_id=types.PeerChannel(123),
            from_id=types.PeerUser(7),
            date=now,
            action=types.MessageActionEmpty(),
        ),
    )
    service.ingest(
        -100123,
        types.Message(
            id=3,
            peer_id=types.PeerChannel(123),
            from_id=types.PeerUser(7),
            date=now,
            message="caption",
            media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=5)),
        ),
    )
    service.ingest(
        -100123,
        types.Message(
            id=4,
            peer_id=types.PeerChannel(123),
            from_id=types.PeerUser(7),
            date=now,
            message="",
            media=types.MessageMediaPhoto(photo=types.PhotoEmpty(id=6)),
        ),
    )
    await service.on_delete(SimpleNamespace(chat_id=None, deleted_ids=[3]))
    rows = configured.db.execute(
        "SELECT * FROM messages ORDER BY message_id"
    ).fetchall()
    assert len(rows) == 2 and rows[0]["media"] == "photo" and not rows[0]["deleted"]


async def test_authenticated_api(configured, aiohttp_client):
    service = Collector(FakeClient(), configured)
    service.state = "connected"
    client = await aiohttp_client(create_app(service, TOKEN))
    assert (await client.get("/v1/status")).status == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert (await client.get("/v1/status", headers=headers)).status == 200
    result = await client.get("/v1/dialogs", headers=headers)
    assert (await result.json())["items"][0]["id"] == -100123
    result = await client.put(
        "/v1/groups", headers=headers, json={"id": -999, "enabled": True}
    )
    assert result.status == 400
    result = await client.put(
        "/v1/people", headers=headers, json={"id": 9, "name": "New", "enabled": True}
    )
    assert result.status == 200
    result = await client.put(
        "/v1/people", headers=headers, json={"id": 9, "enabled": "false"}
    )
    assert result.status == 400
    result = await client.get(
        "/v1/stats",
        headers=headers,
        params={
            "start": "2026-01-01",
            "end": "2026-01-02",
            "cumulative_start": "2026-01-01",
        },
    )
    assert result.status == 400
    service.client.get_dialogs.side_effect = ConnectionError("private details")
    result = await client.get("/v1/dialogs", headers=headers)
    assert result.status == 503 and "private" not in await result.text()


async def test_api_does_not_return_false_zero_on_database_failure(
    configured, aiohttp_client
):
    service = Collector(FakeClient(), configured)
    client = await aiohttp_client(create_app(service, TOKEN))
    configured.db.close()
    result = await client.get(
        "/v1/stats",
        headers={"Authorization": f"Bearer {TOKEN}"},
        params={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
            "cumulative_start": "2026-01-01T00:00:00Z",
        },
    )
    assert result.status == 503 and "items" not in await result.text()


async def test_collector_run_without_astrbot(configured):
    client = FakeClient()
    client.is_user_authorized = AsyncMock(return_value=True)
    service = Collector(client, configured)
    task = asyncio.create_task(service.run())
    try:
        for _ in range(20):
            await asyncio.sleep(0.01)
            if configured.db.execute("SELECT last_sync FROM progress").fetchone()[0]:
                break
        assert configured.db.execute("SELECT last_sync FROM progress").fetchone()[0]
        assert service.state == "connected"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_flood_wait_gates_subsequent_api_requests(configured, aiohttp_client):
    from telethon.errors import FloodWaitError

    service = Collector(FakeClient(), configured)
    service.state = "connected"
    service.client.get_dialogs.side_effect = FloodWaitError(request=None, capture=30)
    client = await aiohttp_client(create_app(service, TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    first = await client.get("/v1/dialogs", headers=headers)
    second = await client.get("/v1/dialogs", headers=headers)
    assert first.status == second.status == 503
    assert service.client.get_dialogs.await_count == 1
    assert service.retry_at > time.time() and service.state == "flood_wait"


def test_stable_ties_and_window_end(configured):
    now = time.time()
    configured.record(-100123, 1, 8, now - 10, "B", "text")
    configured.record(-100123, 2, 7, now - 10, "A", "text")
    configured.record(-100123, 3, 8, now, "next window", "text")
    rows = configured.stats(now - 60, now, now - 60)
    assert [(r["id"], r["count"]) for r in rows] == [(7, 1), (8, 1)]


async def test_online_backup(configured, tmp_path):
    import json
    import sqlite3

    from collector.__main__ import execute

    # The fixture database remains open with WAL enabled during backup.
    configured.record(-100123, 1, 7, time.time() - 10, "backup body", "text")
    source = Path(configured.db.execute("PRAGMA database_list").fetchone()[2])
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path)}))
    # CLI uses the standard database filename.
    with sqlite3.connect(tmp_path / "activity.sqlite3") as standard:
        configured.db.backup(standard)
    destination = tmp_path / "backup.sqlite3"
    await execute(SimpleNamespace(config=config, command="backup", output=destination))
    with sqlite3.connect(destination) as backup:
        assert (
            backup.execute("SELECT text FROM messages").fetchone()[0] == "backup body"
        )
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert source.exists()


def test_disabling_group_closes_open_gap(configured):
    now = time.time()
    configured.gap(-100123, "unreachable", now - 10)
    configured.configure("groups", -100123, "Group", False, now)
    service = Collector(FakeClient(), configured)
    service.state = "connected"
    assert service.status(now + 1, now + 3600)["gaps"] == []
    assert not service.status(now + 1, now + 3600)["incomplete"]


async def test_revoked_session_detected_even_when_cached_auth_is_true(
    configured, monkeypatch
):
    from telethon.errors import AuthKeyUnregisteredError

    client = FakeClient()
    client.is_user_authorized = AsyncMock(return_value=True)
    monkeypatch.setattr(
        FakeClient, "__call__", AsyncMock(side_effect=AuthKeyUnregisteredError(None))
    )
    service = Collector(client, configured)
    service.state = "connected"
    task = asyncio.create_task(service.run())
    try:
        for _ in range(20):
            await asyncio.sleep(0.01)
            if service.state == "login_required":
                break
        assert service.state == "login_required"
        assert service.status()["incomplete"]
        client.is_user_authorized.assert_not_called()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_watch_scope_and_pause_boundaries(configured):
    now = time.time()
    configured.configure("groups", -100456, "Other group", True, now - 300)
    assert not configured.record(-100456, 1, 7, now - 200, "not enrolled here", "text")
    configured.configure_watch(-100456, 7, True, now - 150)
    assert configured.record(-100456, 2, 7, now - 140, "enrolled", "text")
    configured.configure_watch(-100123, 7, False, now - 100)
    configured.configure_watch(-100123, 7, True, now - 50)
    assert not configured.record(-100123, 3, 7, now - 80, "paused here", "text")
    assert configured.record(-100456, 3, 7, now - 80, "other group continues", "text")
    assert configured.record(-100123, 4, 7, now - 40, "resumed", "text")


def test_global_pause_survives_restart_and_keeps_individual_switches(
    configured, tmp_path
):
    now = time.time()
    configured.configure_watch(-100123, 8, False, now - 200)
    configured.pause(True, now - 100)
    configured.pause(True, now - 90)
    reopened = Store(tmp_path / "test.sqlite3")
    try:
        assert Collector(FakeClient(), reopened).status()["paused"]
        assert not reopened.record(-100123, 1, 7, now - 80, "paused", "text")
        reopened.pause(False, now - 50)
        assert not reopened.record(-100123, 1, 7, now - 80, "backfill paused", "text")
        assert reopened.record(-100123, 2, 7, now - 40, "resumed", "text")
        assert not reopened.record(
            -100123, 3, 8, now - 40, "individually paused", "text"
        )
        assert reopened.db.execute("SELECT COUNT(*) FROM pauses").fetchone()[0] == 1
    finally:
        reopened.db.close()


def test_existing_global_targets_migrate_once(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = Store(path)
    now = time.time()
    store.configure("groups", -123, "Legacy group", True, now - 200)
    store.configure("people", 9, "Legacy user", True, now - 100)
    with store.db:
        store.db.execute("DELETE FROM metadata WHERE key='scoped_watches_v1'")
    store.db.close()
    for _ in range(2):
        store = Store(path)
        assert len(store.watches()) == 1
        assert store.record(-123, 1, 9, now - 90, "legacy backlog", "text")
        assert not store.record(-123, 2, 9, now - 150, "before user enabled", "text")
        assert (
            store.db.execute("SELECT COUNT(*) FROM watch_intervals").fetchone()[0] == 1
        )
        store.db.close()


async def test_watch_management_and_pause_api(configured, aiohttp_client):
    service = Collector(FakeClient(), configured)
    service.state = "connected"
    client = await aiohttp_client(create_app(service, TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert (await client.put("/v1/control", json={"paused": True})).status == 401
    assert (
        await client.put("/v1/control", headers=headers, json={"paused": "false"})
    ).status == 400
    result = await client.put("/v1/control", headers=headers, json={"paused": True})
    health = await result.json()
    assert health["paused"] and health["active_watches"] == 0
    result = await client.put(
        "/v1/watches",
        headers=headers,
        json={"group": -100123, "person": 7, "enabled": False},
    )
    assert result.status == 200
    result = await client.put("/v1/control", headers=headers, json={"paused": False})
    assert (await result.json())["active_watches"] == 1
    result = await client.get("/v1/watches?group=-100123", headers=headers)
    rows = (await result.json())["items"]
    assert rows[0]["enabled"] == 0 and rows[1]["enabled"] == 1
    result = await client.put(
        "/v1/watches",
        headers=headers,
        json={"group": -999, "person": 7, "enabled": True},
    )
    assert result.status == 400


async def test_members_page_filters_and_authorization(configured, aiohttp_client):
    from telethon import types

    service = Collector(FakeClient(), configured)
    service.state = "connected"
    service.client = AsyncMock()
    service.client.get_entity.return_value = types.Channel(
        id=123, title="Test", photo=types.ChatPhotoEmpty(), date=None, megagroup=True
    )
    service.client.return_value = SimpleNamespace(
        users=[
            types.User(id=i, first_name=f"Person {i}", bot=i == 1) for i in range(1, 12)
        ],
        participants=[SimpleNamespace(user_id=i) for i in range(1, 12)],
    )
    client = await aiohttp_client(create_app(service, TOKEN))
    response = await client.get("/v1/members?group=-100123&page=2")
    assert response.status == 401
    service.client.assert_not_called()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = await client.get("/v1/members?group=-100123&page=2", headers=headers)
    data = await response.json()
    assert response.status == 200 and len(data["items"]) == 10 and data["has_next"]
    assert not data["items"][0]["selectable"]
    request = service.client.call_args.args[0]
    assert request.offset == 20 and request.limit == 11
    for query in ("group=-999&page=0", "group=-100123&page=-1"):
        response = await client.get("/v1/members?" + query, headers=headers)
        assert response.status == 400


async def test_member_permissions_return_explicit_error(configured, aiohttp_client):
    from telethon.errors import ChatAdminRequiredError

    service = Collector(FakeClient(), configured)
    service.state = "connected"
    service.client.get_entity = AsyncMock(
        side_effect=ChatAdminRequiredError(request=None)
    )
    client = await aiohttp_client(create_app(service, TOKEN))
    response = await client.get(
        "/v1/members?group=-100123", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status == 403
    assert (await response.json())["error"] == "members_unavailable"


@pytest.mark.parametrize(
    "mode", ["public", "invite", "pending", "timeout", "invalid", "person"]
)
async def test_join_group_results(configured, aiohttp_client, mode):
    from telethon import errors, types

    service = Collector(FakeClient(), configured)
    service.state = "connected"
    service.client = AsyncMock()
    group = types.Channel(
        id=123,
        title="Group",
        photo=types.ChatPhotoEmpty(),
        date=None,
        megagroup=True,
        left=True,
    )
    service.client.get_entity.return_value = group
    service.client.return_value = SimpleNamespace(chats=[group])
    link = "https://t.me/testgroup"
    if mode == "invite":
        link = "https://t.me/+testhash"
        service.client.side_effect = [
            SimpleNamespace(channel=True, megagroup=True),
            SimpleNamespace(chats=[group]),
        ]
    elif mode == "pending":
        service.client.side_effect = errors.InviteRequestSentError(request=None)
    elif mode == "timeout":
        service.client.side_effect = asyncio.TimeoutError()
    elif mode == "invalid":
        link = "https://example.com/testgroup"
    elif mode == "person":
        service.client.get_entity.return_value = types.User(id=7)
    client = await aiohttp_client(create_app(service, TOKEN))
    response = await client.put(
        "/v1/join", json={"link": link}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    data = await response.json()
    if mode in ("public", "invite"):
        assert data["status"] == "joined" and data["id"] == -1001000000001
    elif mode == "pending":
        assert data == {"status": "pending"}
    else:
        assert (
            data["error"]
            == {
                "timeout": "join_unconfirmed",
                "invalid": "invalid_group_link",
                "person": "group_required",
            }[mode]
        )
    assert not configured.db.execute(
        "SELECT 1 FROM targets WHERE id=-1001000000001"
    ).fetchone()


@pytest.mark.parametrize("result", ["missing_chats", "rpc_failure", "already_joined"])
async def test_join_reconciles_membership_without_repeat_import(
    configured, aiohttp_client, result
):
    from telethon import types

    service = Collector(FakeClient(), configured)
    service.state = "connected"
    service.client = AsyncMock()
    group = types.Channel(
        id=123, title="Group", photo=types.ChatPhotoEmpty(), date=None, megagroup=True
    )
    already = types.ChatInviteAlready(chat=group)
    service.client.side_effect = (
        [already]
        if result == "already_joined"
        else [
            SimpleNamespace(channel=True, megagroup=True),
            RuntimeError("sanitized")
            if result == "rpc_failure"
            else types.UpdatesTooLong(),
            already,
        ]
    )
    client = await aiohttp_client(create_app(service, TOKEN))
    response = await client.put(
        "/v1/join",
        json={"link": "https://t.me/+testhash"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    data = await response.json()
    assert response.status == 200 and data["id"] == -1001000000001
    assert service.client.await_count == (1 if result == "already_joined" else 3)


async def test_link_rules_details_edits_and_deletion(configured, aiohttp_client):
    service = Collector(FakeClient(), configured)
    service.state = "connected"
    client = await aiohttp_client(create_app(service, TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = await client.put(
        "/v1/link_rules",
        headers=headers,
        json={"person": 7, "patterns": [r"https://allowed\.com/.*"], "enabled": True},
    )
    assert response.status == 200
    now = time.time()
    configured.record(
        -100123,
        500,
        7,
        now,
        "https://allowed.com/ok https://bad.com/no",
        "text",
        links=["https://masked.com"],
    )
    response = await client.get("/v1/violations?person=7", headers=headers)
    result = await response.json()
    assert set(result["items"][0]["links"]) == {
        "https://bad.com/no",
        "https://masked.com",
    }
    configured.record(
        -100123, 500, 7, now, "https://allowed.com/ok", "text", edited=now + 1
    )
    response = await client.get("/v1/violations", headers=headers)
    assert not (await response.json())["items"]
    configured.record(-100123, 501, 7, now, "https://bad.com", "text")
    configured.delete(-100123, [501])
    response = await client.get("/v1/violations", headers=headers)
    assert not (await response.json())["items"]
    assert (
        configured.db.execute(
            "SELECT links FROM messages WHERE message_id=501"
        ).fetchone()[0]
        is None
    )
    response = await client.put(
        "/v1/link_rules",
        headers=headers,
        json={"person": 7, "patterns": ["["], "enabled": True},
    )
    assert response.status == 400
    response = await client.get("/v1/link_rules", headers=headers)
    assert (await response.json())["items"][0]["patterns"] == [
        r"https://allowed\.com/.*"
    ]


async def test_link_rules_disabled_pagination_retention(configured, aiohttp_client):
    service = Collector(FakeClient(), configured)
    client = await aiohttp_client(create_app(service, TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    rule = {"person": 7, "patterns": ["https://allowed"], "enabled": True}
    await client.put("/v1/link_rules", headers=headers, json=rule)
    now = time.time()
    for i in range(12):
        configured.record(-100123, 600 + i, 7, now, "https://bad", "text")
    response = await client.get("/v1/violations", headers=headers)
    first = await response.json()
    assert len(first["items"]) == 10 and first["next"]
    response = await client.get(
        f"/v1/violations?before={first['next']}", headers=headers
    )
    assert len((await response.json())["items"]) == 2
    rule["enabled"] = False
    await client.put("/v1/link_rules", headers=headers, json=rule)
    response = await client.get("/v1/violations", headers=headers)
    assert not (await response.json())["items"]
    configured.prune(now + 91 * 86400)
    assert not configured.db.execute(
        "SELECT 1 FROM messages WHERE links IS NOT NULL"
    ).fetchone()


def test_backup_bundle_is_consistent_private_and_retained(configured, tmp_path):
    import json
    import sqlite3
    import zipfile

    from collector.maintenance import backup_bundle

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": "."}))
    with sqlite3.connect(tmp_path / "activity.sqlite3") as target:
        configured.db.backup(target)
    for _ in range(15):
        archive = backup_bundle(config)
    assert len(list((tmp_path / "backups").glob("scheduled-*.zip"))) == 14
    assert archive.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(archive) as bundle:
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(bundle.read("activity.sqlite3"))
    with sqlite3.connect(restored) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM targets").fetchone()[0] > 0


async def test_setup_and_restore_do_not_overwrite_existing(tmp_path, monkeypatch):
    import json
    import sqlite3

    from collector.__main__ import execute
    from collector.maintenance import backup_bundle

    config = tmp_path / "config.json"
    monkeypatch.setattr("builtins.input", lambda _: "12345")
    monkeypatch.setattr("collector.__main__.getpass.getpass", lambda _: "a" * 32)
    await execute(SimpleNamespace(command="setup", config=config))
    saved = json.loads(config.read_text())
    assert len(saved["api_token"]) >= 32
    assert config.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        await execute(SimpleNamespace(command="setup", config=config))
    (tmp_path / "var").mkdir()
    with sqlite3.connect(tmp_path / "var/activity.sqlite3") as db:
        db.execute("CREATE TABLE marker(value TEXT)")
    archive = backup_bundle(config)
    destination = tmp_path / "restored"
    await execute(
        SimpleNamespace(command="restore", archive=archive, output=destination)
    )
    assert (destination / "var/activity.sqlite3").exists()
    with pytest.raises(ValueError):
        await execute(
            SimpleNamespace(command="restore", archive=archive, output=destination)
        )


def test_backup_reads_astrbot_bom_configuration(configured, tmp_path):
    import json
    import sqlite3
    import zipfile

    from collector.maintenance import backup_bundle

    config = tmp_path / "collector.json"
    config.write_text(json.dumps({"data_dir": "."}))
    with sqlite3.connect(tmp_path / "activity.sqlite3") as target:
        configured.db.backup(target)
    root = tmp_path / "AstrBot"
    (root / "data/config").mkdir(parents=True)
    (root / "data/config/astrbot_plugin_tgwatch_config.json").write_text(
        json.dumps({"platform_id": "test"}), encoding="utf-8-sig"
    )
    (root / "data/cmd_config.json").write_text(
        json.dumps({"platform": [{"id": "test"}]}), encoding="utf-8-sig"
    )
    with zipfile.ZipFile(backup_bundle(config, root)) as archive:
        assert json.loads(archive.read("telegram-platform.json"))["id"] == "test"


async def test_remove_group_requires_pause_preserves_history(
    configured, aiohttp_client
):
    service = Collector(FakeClient(), configured)
    client = await aiohttp_client(create_app(service, TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    configured.record(-100123, 900, 7, time.time(), "retained", "text")
    response = await client.put(
        "/v1/remove_group", headers=headers, json={"group": -100123, "leave": False}
    )
    assert response.status == 400
    configured.configure("groups", -100123, "Group", False)
    response = await client.put(
        "/v1/remove_group", headers=headers, json={"group": -100123, "leave": False}
    )
    assert response.status == 200
    assert not configured.targets("groups")
    assert (
        configured.db.execute(
            "SELECT text FROM messages WHERE message_id=900"
        ).fetchone()[0]
        == "retained"
    )
    configured.configure("groups", -100123, "Group", True)
    assert len(configured.targets("groups")) == 1
    assert not any(w["enabled"] for w in configured.watches(-100123))


async def test_paused_group_leave_uses_user_account(configured, aiohttp_client):
    service = Collector(FakeClient(), configured)
    service.state = "connected"
    service.client = AsyncMock()
    service.client.get_entity.return_value = types.Channel(
        id=123, title="Group", photo=types.ChatPhotoEmpty(), date=None, megagroup=True
    )
    configured.configure("groups", -100123, "Group", False)
    client = await aiohttp_client(create_app(service, TOKEN))
    response = await client.put(
        "/v1/remove_group",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"group": -100123, "leave": True},
    )
    assert response.status == 200
    assert type(service.client.call_args.args[0]).__name__ == "LeaveChannelRequest"
    assert not configured.targets("groups")


def test_status_excludes_archived_groups(configured):
    configured.db.execute("INSERT INTO removed_groups VALUES(?)", (-100123,))
    service = Collector(FakeClient(), configured)
    assert service.status()["groups"] == []


@pytest.mark.asyncio
async def test_left_group_is_archived_not_reported_as_monitoring(
    configured, monkeypatch
):
    client = FakeClient()
    client.get_entity.return_value = SimpleNamespace(left=True)
    service = Collector(client, configured)

    async def stop_after_round(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_round)
    with pytest.raises(asyncio.CancelledError):
        await service.run()
    assert configured.targets("groups") == []
    assert service.status()["groups"] == []
    assert all(not w["enabled"] for w in configured.watches())
    assert client.options == []


async def test_disconnect_restart_replays_without_double_count(tmp_path):
    path = tmp_path / "restart.sqlite3"
    store = Store(path)
    now = time.time()
    store.configure("groups", -100123, "Group", True, now - 60)
    store.configure("people", 7, "Alice", True, now - 60)
    store.configure_watch(-100123, 7, True, now - 60)
    messages = [
        types.Message(
            id=i,
            peer_id=types.PeerChannel(123),
            from_id=types.PeerUser(7),
            date=datetime.fromtimestamp(now, timezone.utc),
            message=str(i),
        )
        for i in (1, 2, 3)
    ]
    client = FakeClient()
    client.history = messages[:1]
    service = Collector(client, store)
    await service.reconcile({"id": -100123})
    client.fail = True
    with pytest.raises(ConnectionError):
        await service.reconcile({"id": -100123})
    store.gap(-100123, "ConnectionError", now)
    store.db.close()
    store = Store(path)
    try:
        client = FakeClient()
        client.history = messages
        recovered = Collector(client, store)
        recovered.ingest(-100123, messages[2])
        await recovered.reconcile({"id": -100123})
        await recovered.reconcile({"id": -100123})
        assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        assert store.db.execute("SELECT message_id FROM progress").fetchone()[0] == 3
        assert (
            store.db.execute("SELECT COUNT(*) FROM gaps WHERE end IS NULL").fetchone()[
                0
            ]
            == 0
        )
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        store.db.close()


async def test_delete_active_person_scoped_then_global(configured, aiohttp_client):
    now = time.time()
    configured.configure("groups", -200, "Other", True, now - 30)
    configured.configure_watch(-200, 7, True, now - 30)
    configured.record(-100123, 901, 7, now, "first", "text")
    configured.record(-200, 902, 7, now, "second", "text")
    service = Collector(FakeClient(), configured)
    client = await aiohttp_client(create_app(service, TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert (await client.put("/v1/remove_person", json={"person": 7})).status == 401
    response = await client.put(
        "/v1/remove_person", headers=headers, json={"person": 7, "group": -100123}
    )
    assert response.status == 200
    assert [
        r[0]
        for r in configured.db.execute("SELECT chat_id FROM messages WHERE user_id=7")
    ] == [-200]
    assert any(p["id"] == 7 for p in configured.targets("people"))
    assert not any(w["user_id"] == 7 for w in configured.watches(-100123))
    response = await client.put(
        "/v1/remove_person", headers=headers, json={"person": 7}
    )
    assert response.status == 200
    assert not configured.db.execute(
        "SELECT 1 FROM messages WHERE user_id=7"
    ).fetchone()
    assert not any(p["id"] == 7 for p in configured.targets("people"))
    assert any(p["id"] == 8 for p in configured.targets("people"))


async def test_dialogs_hide_left_deleted_and_forbidden_groups(
    configured, aiohttp_client
):
    telegram = FakeClient()
    telegram.get_dialogs.return_value = [
        SimpleNamespace(
            id=-100123, name="Active", is_group=True, entity=SimpleNamespace(left=False)
        ),
        SimpleNamespace(
            id=-2, name="Left", is_group=True, entity=SimpleNamespace(left=True)
        ),
        SimpleNamespace(
            id=-3,
            name="Deleted",
            is_group=True,
            entity=SimpleNamespace(deactivated=True),
        ),
        SimpleNamespace(
            id=-4,
            name="Forbidden",
            is_group=True,
            entity=types.ChatForbidden(id=4, title="Forbidden"),
        ),
    ]
    service = Collector(telegram, configured)
    service.state = "connected"
    client = await aiohttp_client(create_app(service, TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = await client.get("/v1/dialogs", headers=headers)
    assert (await response.json())["items"] == [{"id": -100123, "name": "Active"}]
    response = await client.put(
        "/v1/groups", headers=headers, json={"id": -2, "enabled": True}
    )
    assert response.status == 400


@pytest.mark.parametrize(
    "entity",
    [types.ChatForbidden(id=123, title="Gone"), SimpleNamespace(deactivated=True)],
)
async def test_unavailable_group_hides_watches_and_preserves_history(
    configured, monkeypatch, entity
):
    configured.record(-100123, 999, 7, time.time(), "history", "text")
    client = FakeClient()
    client.get_entity.return_value = entity
    service = Collector(client, configured)

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await service.run()
    assert configured.watches() == []
    assert service.status()["groups"] == []
    assert configured.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    configured.configure("groups", -100123, "Rejoined", True)
    assert all(not w["enabled"] for w in configured.watches())


async def test_transient_group_lookup_error_does_not_remove_membership(
    configured, monkeypatch
):
    client = FakeClient()
    client.get_entity.side_effect = TimeoutError("temporary")
    service = Collector(client, configured)

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await service.run()
    assert configured.targets("groups")
    assert configured.watches()
    assert service.status()["incomplete"]
