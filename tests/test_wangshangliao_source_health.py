"""Bounded invitation decoding and persistent source-health regressions."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import nacl.bindings
import pytest
import zstandard
from aiohttp import web
from test_wangshangliao_cashback import OWNER, member, scan, select
from test_wangshangliao_cashback import store as store
from test_wangshangliao_invitations import invitation_record, make_adapter
from test_wangshangliao_native import deployment as deployment

from astrbot.builtin_stars.wangshangliao_moderation.cashback import CashbackService
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
)
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    BusinessError,
)


@pytest.mark.parametrize("known_size", [True, False])
@pytest.mark.parametrize(
    ("size", "limit", "accepted"),
    [
        (wire.LIMIT, wire.LIMIT, True),
        (wire.LIMIT + 1, wire.LIMIT, False),
        (wire.LIMIT + 1, wire.INVITATION_LOG_LIMIT, True),
        (wire.INVITATION_LOG_LIMIT, wire.INVITATION_LOG_LIMIT, True),
        (wire.INVITATION_LOG_LIMIT + 1, wire.INVITATION_LOG_LIMIT, False),
    ],
)
def test_authenticated_frame_size_bounds(known_size, size, limit, accepted):
    plain = b"a" * size
    compressed = zstandard.ZstdCompressor(write_content_size=known_size).compress(plain)
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        compressed, b"fixture", bytes(8), bytes(32)
    )
    body = sealed[-16:] + bytes(16) + sealed[:-16]
    if accepted:
        assert wire.open_business(bytes(32), b"fixture", body, limit=limit) == plain
    else:
        with pytest.raises(wire.ProtocolError):
            wire.open_business(bytes(32), b"fixture", body, limit=limit)
    with pytest.raises(wire.ProtocolError):
        wire.open_business(bytes(32), b"wrong-aad", body, limit=limit)


@pytest.mark.parametrize(
    "suffix", [b"trailing", zstandard.ZstdCompressor().compress(b"b")]
)
def test_authenticated_extra_frame_or_bytes_rejected(suffix):
    compressed = zstandard.ZstdCompressor().compress(b"a") + suffix
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        compressed, b"fixture", bytes(8), bytes(32)
    )
    with pytest.raises(wire.ProtocolError):
        wire.open_business(
            bytes(32), b"fixture", sealed[-16:] + bytes(16) + sealed[:-16]
        )


def test_authenticated_large_window_rejected_before_decompression():
    params = zstandard.ZstdCompressionParameters.from_level(
        3, window_log=23, force_max_window=True, write_content_size=False
    )
    compressed = zstandard.ZstdCompressor(compression_params=params).compress(
        b"a" * (wire.INVITATION_LOG_LIMIT + 1)
    )
    assert (
        zstandard.get_frame_parameters(compressed).window_size
        > wire.INVITATION_LOG_LIMIT
    )
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        compressed, b"fixture", bytes(8), bytes(32)
    )
    with pytest.raises(wire.ProtocolError, match="body_limit"):
        wire.open_business(
            bytes(32),
            b"fixture",
            sealed[-16:] + bytes(16) + sealed[:-16],
            limit=wire.INVITATION_LOG_LIMIT,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("encrypted", [True, False])
@pytest.mark.parametrize(
    ("route", "size", "accepted"),
    [
        ("/v1/group/get-apply-logs", wire.LIMIT + 500, True),
        ("/v1/group/get-group-apply-list", wire.LIMIT + 500, False),
        ("/v1/group/get-apply-logs", wire.INVITATION_LOG_LIMIT + 1, False),
    ],
)
async def test_only_invitation_log_route_has_larger_response_limit(
    deployment, encrypted, route, size, accepted
):
    response = json.dumps({"code": 0, "data": {"value": "a" * size}}).encode()

    async def respond(request):
        if not encrypted:
            return web.Response(body=response, content_type="application/json")
        metadata = wire.unb64(request.headers["x-request"])
        fields, offset = {}, 0
        while offset < len(metadata):
            tag, offset = wire.read_varint(metadata, offset)
            value, offset = wire.read_varint(metadata, offset, 64)
            fields[tag >> 3] = value
        aad = wire.binding(fields[6], b"{}", fields[1])[1]
        compressed = zstandard.ZstdCompressor().compress(response)
        sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
            compressed, aad, bytes(8), bytes(32)
        )
        return web.Response(body=sealed[-16:] + bytes(16) + sealed[:-16])

    app = web.Application()
    app.router.add_post(route, respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fixture = deployment.model_copy(
        update={
            "origin": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        }
    )
    try:
        async with aiohttp.ClientSession() as http:
            client = BusinessClient(fixture, http)
            client.jwt = "fixture"
            if accepted:
                assert len((await client.request(route, {}))["value"]) == size
            else:
                with pytest.raises(BusinessError):
                    await client.request(route, {})
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [201, 4096])
async def test_group_invitation_records_are_not_truncated_at_200(tmp_path, count):
    adapter = make_adapter()
    adapter.business.request.return_value = {
        "list": [invitation_record(str(i + 1)) for i in range(count)]
    }
    result = await adapter.get_group_invitation_records("5", pending_only=False)
    assert len(result["items"]) == count


@pytest.fixture
def source_page(store):
    started = store.get_group("5")["started"]
    return {
        "items": [
            {
                "apply_id": "100",
                "member_id": "2",
                "inviter_id": "1",
                "member_nim_id": "1002",
                "inviter_nim_id": "1001",
                "apply_time": str(started),
                "state": "MEMBER_STATE_GOOD",
            }
        ],
        "last_id": str(int(time.time())),
    }


def test_source_page_commit_dedup_restart_and_late_mapping(store, source_page):
    started = store.get_group("5")["started"]
    with store.connect() as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM entries ORDER BY id")]
    for _ in range(2):
        store.remember_source_page("5", "bot-a", "99", started, source_page)
    reopened = CashbackStore(store.path)
    # The cache normalizes optional display fields without changing identities.
    assert reopened.source_records("5", "bot-a", "99", started) == [
        {**item, "member_name": "", "apply_type": "INVITATION"}
        for item in source_page["items"]
    ]
    assert (
        reopened.source_checkpoint("5", "bot-a", "99", started)["source_cursor"]
        == source_page["last_id"]
    )
    with reopened.connect() as db:
        assert [
            tuple(r) for r in db.execute("SELECT * FROM entries ORDER BY id")
        ] == before


@pytest.mark.parametrize("changed", ["bot", "account", "baseline", "cursor"])
def test_invalid_source_page_never_advances_or_partly_commits(
    store, source_page, changed
):
    started = store.get_group("5")["started"]
    store.remember_source_page("5", "bot-a", "99", started, source_page)
    before = store.get_group("5")["source_cursor"]
    page = {**source_page, "items": [{**source_page["items"][0], "apply_id": "101"}]}
    bot, account, baseline = "bot-a", "99", started
    if changed == "bot":
        bot = "other"
    elif changed == "account":
        account = "other"
    elif changed == "baseline":
        baseline -= 1
    elif changed == "cursor":
        page["last_id"] = str(int(before) - 1)
    with pytest.raises(CashbackError):
        store.remember_source_page("5", bot, account, baseline, page)
    assert store.get_group("5")["source_cursor"] == before
    assert len(store.source_records("5", "bot-a", "99", started)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_kind", ["missing_inviter", "invalid_identity", "invalid_peer"]
)
async def test_mixed_page_keeps_good_row_audits_bad_row_and_never_rewards_it(
    store, source_page, bad_kind
):
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "qualification_seconds": 10800},
        {"1": member("1")},
        set(),
    )
    started = store.get_group("5")["started"]
    good = {
        **source_page["items"][0],
        "apply_id": "101",
        "apply_time": str(time.time()),
    }
    bad = {**good, "apply_id": "102", "member_id": "3", "member_nim_id": "1003"}
    if bad_kind == "missing_inviter":
        bad.update(inviter_id="", inviter_nim_id="")
    elif bad_kind == "invalid_identity":
        bad["member_id"] = "token=fixture-private"
    else:
        bad["member_nim_id"] = "not-a-peer"
    page = {**source_page, "items": [good, bad]}
    adapter = SimpleNamespace(
        account="99",
        config={"id": "bot-a"},
        ledger=None,
        get_group_invitation_records=AsyncMock(return_value=page),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.business_checked = store, {}
    members = {uid: member(uid) for uid in ("1", "2", "3")}
    proof = await service.evidence(adapter, "5", members, started)
    store.snapshot("5", "bot-a", "99", members, proof)
    assert {item["member"] for item in proof} == {"2"}
    assert store.get_group("5")["source_cursor"] == page["last_id"]
    assert store.get_group("5")["source_error"] == ""
    records = store.source_records("5", "bot-a", "99", started)
    assert any(item["apply_id"] == "101" for item in records)
    with store.connect() as db:
        rejected = dict(
            db.execute("SELECT * FROM cashback_source_quarantine").fetchone()
        )
        good_entry = dict(
            db.execute("SELECT * FROM entries WHERE member='2'").fetchone()
        )
        bad_entry = dict(
            db.execute("SELECT * FROM entries WHERE member='3'").fetchone()
        )
    assert rejected["event_id"] == "102"
    assert rejected["reason"] == (
        "missing_inviter" if bad_kind == "missing_inviter" else "invalid_member_event"
    )
    assert good_entry["inviter"] == "1" and good_entry["amount"] == 1800
    assert (
        bad_entry["inviter"] == ""
        and bad_entry["amount"] == 0
        and bad_entry["paid_at"] is None
    )
    result = store.list_entries(OWNER, "5", "verifying")
    assert (
        result["total"] == 1
        and result["amount"] == 1800
        and result["payable_amount"] == 0
    )
    audits = [
        item
        for item in store.audit(OWNER, "5")
        if item["action"] == "invitation_source_row_quarantined"
    ]
    assert len(audits) == 1
    assert json.loads(audits[0]["after_json"])["event_id"] == "102"
    assert "fixture-private" not in audits[0]["after_json"]
    store.remember_source_page("5", "bot-a", "99", started, page)
    assert (
        len(
            [
                item
                for item in store.audit(OWNER, "5")
                if item["action"] == "invitation_source_row_quarantined"
            ]
        )
        == 1
    )
    with pytest.raises(CashbackError, match="not_payable"):
        store.mutate(
            OWNER,
            "5",
            "exception",
            [{"id": bad_entry["id"], "version": bad_entry["version"]}],
            f"bad-source-{bad_kind}-not-payable",
            "External payment cannot establish attribution",
            actual_payment_confirmed=True,
        )


@pytest.mark.asyncio
async def test_source_replay_after_roster_delay_and_restart_uses_incremental_cursor(
    store, source_page
):
    started = store.get_group("5")["started"]
    adapter = SimpleNamespace(
        account="99",
        config={"id": "bot-a"},
        ledger=None,
        get_group_invitation_records=AsyncMock(return_value=source_page),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.business_checked = store, {}
    with store.connect() as db:
        previous_entries = [
            tuple(r) for r in db.execute("SELECT * FROM entries ORDER BY id")
        ]
    initial = await service.evidence(adapter, "5", {"1": member("1")}, started)
    # Authenticated source proof survives a roster delay; evidence alone is not payment eligibility.
    assert initial[0]["member"] == "2"
    assert initial[0]["source"] == "business_apply_log"
    with store.connect() as db:
        assert [
            tuple(r) for r in db.execute("SELECT * FROM entries ORDER BY id")
        ] == previous_entries
    service.store = CashbackStore(store.path)
    service.business_checked.clear()
    adapter.get_group_invitation_records.return_value = {
        "items": [],
        "last_id": source_page["last_id"],
    }
    result = await service.evidence(
        adapter, "5", {u: member(u) for u in ("1", "2")}, started
    )
    assert result[0]["member"] == "2"
    assert adapter.get_group_invitation_records.call_args.kwargs["last_id"] == str(
        int(source_page["last_id"]) - 1
    )


@pytest.mark.asyncio
async def test_fault_keeps_cached_proof_backs_off_and_recovers_without_erasing_audit(
    store, source_page
):
    started = store.get_group("5")["started"]
    store.remember_source_page("5", "bot-a", "99", started, source_page)
    adapter = SimpleNamespace(
        account="99",
        config={"id": "bot-a"},
        ledger=None,
        get_group_invitation_records=AsyncMock(side_effect=TimeoutError()),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.business_checked = store, {}
    for _ in range(3):
        service.business_checked.clear()
        result = await service.evidence(
            adapter, "5", {u: member(u) for u in ("1", "2")}, started
        )
        assert result[0]["member"] == "2"
    assert adapter.get_group_invitation_records.await_count == 1
    checkpoint = store.get_group("5")
    assert checkpoint["source_cursor"] == source_page["last_id"]
    assert checkpoint["source_error"] == "source_unavailable"
    assert checkpoint["source_retry_at"] > time.time()
    store.remember_source_page("5", "bot-a", "99", started, source_page)
    logs = store.audit(OWNER, "5")
    failed = next(r for r in logs if r["action"] == "invitation_source_failed")
    assert failed["level"] == "warning" and failed["resolved_at"]
    assert store.get_group("5")["source_error"] == ""
    assert any(r["action"] == "invitation_source_recovered" for r in logs)


@pytest.mark.asyncio
async def test_source_cooldown_is_not_a_fault(store):
    adapter = SimpleNamespace(
        account="99",
        config={"id": "bot-a"},
        ledger=None,
        get_group_invitation_records=AsyncMock(
            side_effect=wire.ProtocolError("invitation_read_cooldown")
        ),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.business_checked = store, {}
    await service.evidence(
        adapter, "5", {"1": member("1")}, store.get_group("5")["started"]
    )
    assert not store.get_group("5")["source_error"]
    logs = store.audit(OWNER, "5")
    assert any(
        r["action"] == "invitation_source_deferred" and r["level"] == "info"
        for r in logs
    )
    assert not any(r["action"] == "invitation_source_failed" for r in logs)


def test_source_fault_blocks_payment_without_changing_history(store):
    scan(store)
    started = store.get_group("5")["started"]
    store.source_failed("5", "bot-a", "99", started, "transport")
    with pytest.raises(CashbackError, match="invitation_source_unavailable"):
        store.mutate(OWNER, "5", "pay", [select(store)], "source-failed-payment")
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM entries WHERE state='paid'").fetchone()[0]
            == 0
        )
