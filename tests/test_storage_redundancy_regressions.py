"""Bound repeated storage work without weakening freshness or retaining bitmaps."""

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    automatic_whitelist,
    media_moderation,
    member_exemptions,
    rankings,
)
from astrbot.core.db.vec_db.faiss_impl.document_storage import DocumentStorage
from astrbot.core.knowledge_base.kb_db_sqlite import KBSQLiteDatabase
from tests.test_wangshangliao_advertisement_images import (
    configuration,
    upload,
)
from tests.test_wangshangliao_business_rules import rules as rules
from tests.test_wangshangliao_business_rules import save_rules
from tests.test_wsl_daily_whitelist_cycle_20261024 import daily as daily
from tests.test_wsl_daily_whitelist_cycle_20261024 import message


@pytest.mark.asyncio
async def test_settled_rankings_reuse_bodies_but_recheck_late_inputs(
    daily, monkeypatch
):
    adapter, ranking, _runtime, _root, clock = daily
    clock[0] -= 86400
    await message(adapter, "10", "yesterday", clock[0] - 100)
    clock[0] += 86400
    statements = []
    connect = rankings.aiosqlite.connect

    @asynccontextmanager
    async def traced(*args, **kwargs):
        async with connect(*args, **kwargs) as db:
            await db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(rankings.aiosqlite, "connect", traced)
    first = await ranking.snapshot(adapter, "5", previous_day=True, settled=True)
    assert first[2] == [("10", "Member 10", 1)]
    ranking.cache.clear()
    assert (
        await ranking.snapshot(adapter, "5", previous_day=True, settled=True) == first
    )

    def body_reads():
        return sum(
            "SELECT rowid,message,state,pruned_at,payload FROM inbox" in sql
            for sql in statements
        )

    assert body_reads() == 1

    await adapter.ledger.db.execute(
        "UPDATE inbox SET state='needs_review' WHERE message='yesterday'"
    )
    await adapter.ledger.db.commit()
    ranking.cache.clear()
    assert not (await ranking.snapshot(adapter, "5", previous_day=True, settled=True))[
        2
    ]
    assert body_reads() == 2

    await adapter.ledger.db.execute(
        "UPDATE inbox SET state='processed' WHERE message='yesterday'"
    )
    await adapter.ledger.db.commit()
    ranking.cache.clear()
    assert (await ranking.snapshot(adapter, "5", previous_day=True, settled=True))[2]
    await adapter.ledger.db.execute(
        "INSERT INTO outbox(key,fingerprint,nonce,state) VALUES(?,?,?,?)",
        ("recall/1/5/yesterday", "test", "test", "accepted"),
    )
    await adapter.ledger.db.commit()
    ranking.cache.clear()
    assert not (await ranking.snapshot(adapter, "5", previous_day=True, settled=True))[
        2
    ]
    assert body_reads() == 4
    assert (
        sum(
            "SELECT rowid,message,state,pruned_at FROM inbox" in sql
            for sql in statements
        )
        == 5
    )
    for sql in statements:
        if sql.startswith("SELECT rowid,message,state,pruned_at"):
            assert "WHERE account='1' AND team='5'" in sql
            assert f"received_at>={first[1] - 86400}" in sql
            assert f"received_at<{first[1]}" in sql
            assert "ORDER BY rowid" in sql


@pytest.mark.asyncio
async def test_settled_cache_invalidates_for_late_yesterday_message(daily):
    adapter, ranking, _runtime, _root, clock = daily
    clock[0] -= 86400
    await message(adapter, "10", "first", clock[0] - 100)
    clock[0] += 86400
    first = await ranking.snapshot(adapter, "5", previous_day=True, settled=True)
    clock[0] -= 86400
    await message(adapter, "11", "late", clock[0] - 100)
    clock[0] += 86400
    ranking.cache.clear()
    second = await ranking.snapshot(adapter, "5", previous_day=True, settled=True)
    assert len(first[2]) == 1
    assert len(second[2]) == 2
    clock[0] += 86400
    ranking.cache.clear()
    latest = await ranking.snapshot(adapter, "5", previous_day=True, settled=True)
    assert not latest[2]
    assert all(key[3] == latest[0] for key in ranking.settled_cache)


@pytest.mark.asyncio
async def test_automatic_qualification_and_late_recall_stay_fresh(daily):
    adapter, ranking, runtime, _root, clock = daily
    yesterday = (
        automatic_whitelist.datetime.fromtimestamp(
            clock[0], automatic_whitelist.ZoneInfo("Asia/Shanghai")
        )
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
        - 86400
    )
    rows = []
    for uid in range(10, 42):
        for number in range(100 if uid == 41 else 101):
            stamp = yesterday + 100 + number * 31
            rows.append(
                (
                    "1",
                    "5",
                    f"{uid}-{number}",
                    json.dumps(
                        {
                            "sender": str(uid),
                            "name": f"Member {uid}",
                            "text": f"Unique normal sentence {number}",
                            "recall_route": {
                                "time": stamp * 1000,
                                "peer": str(900 + uid),
                            },
                        }
                    ),
                    "processed",
                    stamp,
                    stamp,
                )
            )
    await adapter.ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,payload,state,received_at,completed_at) "
        "VALUES(?,?,?,?,?,?,?)",
        rows,
    )
    await adapter.ledger.db.commit()
    await runtime.step()
    entries = automatic_whitelist.read_automatic("daily", "1", "5")
    assert set(entries) == {str(uid) for uid in range(10, 30)}
    assert all(entry["count"] == 101 for entry in entries.values())
    assert "41" not in entries
    await adapter.ledger.db.execute(
        "INSERT INTO outbox(key,fingerprint,nonce,state) VALUES(?,?,?,?)",
        ("recall/1/5/10-0", "test", "test", "accepted"),
    )
    await adapter.ledger.db.commit()
    ranking.cache.clear()
    await runtime.step()
    entries = automatic_whitelist.read_automatic("daily", "1", "5")
    assert set(entries) == {str(uid) for uid in range(11, 31)}
    assert all(entry["count"] == 101 for entry in entries.values())


def test_automatic_lookup_reuses_manual_connection_and_has_no_schema_probes(
    tmp_path, monkeypatch
):
    for module in (automatic_whitelist, member_exemptions):
        monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    member_exemptions.set_exemption(
        "fixture",
        "1",
        "5",
        "10",
        "910",
        enabled=False,
        actor="server/test",
        reason="Fixture",
        guard=lambda: None,
    )
    now = time.time()
    today = automatic_whitelist.datetime.fromtimestamp(
        now, automatic_whitelist.ZoneInfo("Asia/Shanghai")
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = (today - automatic_whitelist.timedelta(days=1)).strftime("%Y-%m-%d")
    last_activity = today.timestamp() - 100
    with sqlite3.connect(tmp_path / "automatic_exemptions.sqlite3") as db:
        db.execute(
            "CREATE TABLE automatic_exemptions("
            "account,group_id,member,peer,name,messages,day_start,valid_until,ranking_day,"
            "last_activity,activity_history,PRIMARY KEY(account,group_id,member))"
        )
        db.execute(
            "INSERT INTO automatic_exemptions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "1",
                "5",
                "10",
                "910",
                "Member",
                101,
                today.timestamp(),
                now + 60,
                yesterday,
                last_activity,
                json.dumps({yesterday: last_activity}),
            ),
        )
    # Yesterday's explicit removal does not suppress today's automatic grant.
    with sqlite3.connect(tmp_path / "member_exemptions.sqlite3") as db:
        db.execute("UPDATE speech_exemptions SET created=?", (today.timestamp() - 1,))
    original = sqlite3.connect
    paths, statements = [], []

    def traced(path, *args, **kwargs):
        paths.append(str(path))
        db = original(path, *args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    assert member_exemptions.is_exempt("fixture", "1", "5", "10", "910")
    assert sum("member_exemptions.sqlite3" in path for path in paths) == 1
    assert sum("automatic_exemptions.sqlite3" in path for path in paths) == 1
    assert any(
        "FROM automatic_exemptions WHERE account='1' AND group_id='5'" in sql
        and "AND member='10' AND peer='910'" in sql
        for sql in statements
    )
    assert not any(
        "sqlite_master" in sql or "PRAGMA table_info" in sql for sql in statements
    )
    assert not member_exemptions.is_exempt("fixture", "2", "5", "10", "910")
    assert not member_exemptions.is_exempt("fixture", "1", "6", "10", "910")
    assert not member_exemptions.is_exempt("fixture", "1", "5", "10", "999")
    member_exemptions.set_exemption(
        "fixture",
        "1",
        "5",
        "10",
        "910",
        enabled=False,
        actor="server/test",
        reason="Removed",
        guard=lambda: None,
    )
    assert not member_exemptions.is_exempt("fixture", "1", "5", "10", "910")


@pytest.mark.parametrize(
    "protection", ["saved", "draft", "send", "inflight", "unknown", "fresh"]
)
def test_periodic_image_cleanup_protects_live_references(rules, protection):
    reference = upload(rules)
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_rule_images SET created=?", (time.time() - 90000,))
        if protection == "unknown":
            db.execute("UPDATE business_rule_images SET state='unknown'")
        if protection == "fresh":
            db.execute("UPDATE business_rule_images SET created=?", (time.time(),))
        if protection == "send":
            db.execute(
                "INSERT INTO business_rule_image_sends VALUES(?,?,?,?)",
                ("test", "99", "5", reference),
            )
        if protection == "draft":
            db.execute(
                "INSERT INTO business_rule_drafts(token,account,group_id,config,expires) "
                "VALUES(?,?,?,?,?)",
                (
                    "test",
                    "99",
                    "5",
                    json.dumps(configuration([reference])),
                    time.time() + 60,
                ),
            )
    if protection == "saved":
        save_rules(rules, configuration([reference]))
    pin = (rules.store.path, "99", "5", reference)
    if protection == "inflight":
        with rules.media._inflight_lock:
            rules.media._inflight[pin] += 1
    try:
        assert rules.media.cleanup() == 0
        with rules.store.connect() as db:
            assert (
                db.execute("SELECT count(*) FROM business_rule_images").fetchone()[0]
                == 1
            )
    finally:
        if protection == "inflight":
            with rules.media._inflight_lock:
                del rules.media._inflight[pin]


def test_periodic_image_cleanup_removes_orphans_without_another_upload(rules):
    reference = upload(rules)
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_rule_images SET created=?", (time.time() - 90000,))
        db.execute(
            "INSERT INTO business_rule_drafts(token,account,group_id,config,expires) "
            "VALUES(?,?,?,?,?)",
            (
                "expired",
                "99",
                "5",
                json.dumps(configuration([reference])),
                time.time() - 1,
            ),
        )
    assert rules.media.cleanup() == 1
    assert rules.media.cleanup() == 0
    with rules.store.connect() as db:
        assert not db.execute("SELECT 1 FROM business_rule_images").fetchone()
        assert not db.execute("SELECT 1 FROM business_rule_drafts").fetchone()


def test_periodic_cleanup_advances_past_a_full_referenced_batch(rules):
    reference = upload(rules)
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_rule_images SET created=?", (time.time() - 90000,))
        for number in range(501):
            db.execute(
                "INSERT INTO business_rule_images "
                "SELECT ?,account,group_id,digest,width,height,data,thumbnail,created,asset,state "
                "FROM business_rule_images WHERE id=?",
                (f"{number:032x}", reference),
            )
        refs = [f"{number:032x}" for number in range(500)]
        db.execute(
            "INSERT INTO business_rule_drafts(token,account,group_id,config,expires) "
            "VALUES(?,?,?,?,?)",
            (
                "active",
                "99",
                "5",
                json.dumps({"ads": [{"images": refs + [reference]}]}),
                time.time() + 60,
            ),
        )
    assert rules.media.cleanup() == 0
    assert rules.media.cleanup() == 1
    with rules.store.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM business_rule_images").fetchone()[0] == 501
        )


@pytest.mark.asyncio
async def test_shared_image_download_cancellation_is_scoped_and_releases_bytes(
    monkeypatch,
):
    runtime = media_moderation.MediaModeration()
    ready, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Session(Response):
        def get(self, url, **kwargs):
            calls.append(url)
            return Response()

    async def read(*_):
        ready.set()
        await release.wait()
        return b"bitmap"

    monkeypatch.setattr(
        media_moderation.aiohttp, "ClientSession", lambda **_: Session()
    )
    monkeypatch.setattr(media_moderation, "_read_bounded", read)
    endpoints = ("https://neutral.test/upload", "https://neutral.test/assets/")
    key = ("bot", "1", "5", endpoints[1], "123", 320, 180)
    first = asyncio.create_task(runtime._download(key, endpoints, "123"))
    await ready.wait()
    second = asyncio.create_task(runtime._download(key, endpoints, "123"))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert not second.done()
    release.set()
    assert await second == b"bitmap"
    assert len(calls) == 1
    assert not runtime.downloads
    assert (
        await runtime._download((*key[:2], "6", *key[3:]), endpoints, "123")
        == b"bitmap"
    )
    assert len(calls) == 2
    assert not runtime.downloads


@pytest.mark.asyncio
async def test_fresh_document_store_has_only_one_doc_id_unique_index(tmp_path):
    storage = DocumentStorage(str(tmp_path / "docs.db"))
    try:
        await storage.initialize()
        with sqlite3.connect(tmp_path / "docs.db") as db:
            unique = [
                row[1]
                for row in db.execute("PRAGMA index_list(documents)")
                if row[2] and not row[4]
            ]
            assert len(unique) == 1
            assert unique[0] == "sqlite_autoindex_documents_1"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_document_store_still_receives_missing_uniqueness(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE documents(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "doc_id TEXT NOT NULL,text TEXT NOT NULL,metadata TEXT,"
            "created_at DATETIME,updated_at DATETIME)"
        )
    storage = DocumentStorage(str(path))
    try:
        await storage.initialize()
        with sqlite3.connect(path) as db:
            assert any(
                row[1] == "idx_documents_doc_id_unique" and row[2]
                for row in db.execute("PRAGMA index_list(documents)")
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_knowledge_migration_reuses_orm_index_names(tmp_path):
    path = tmp_path / "kb.db"
    database = KBSQLiteDatabase(str(path))
    try:
        await database.initialize()
        await database.migrate_to_v1()
        await database.migrate_to_v1()
        with sqlite3.connect(path) as db:
            names = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            assert {
                "ix_kb_documents_kb_id",
                "ix_kb_media_kb_id",
                "ix_kb_media_doc_id",
            } <= names
            assert not {"idx_doc_kb_id", "idx_media_kb_id", "idx_media_doc_id"} & names
    finally:
        await database.close()
