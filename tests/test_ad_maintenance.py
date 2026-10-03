"""Isolated lifecycle, funds, concurrency and scan recovery acceptance tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import RetryAfter
from test_superbot_payments import ADDRESS, evidence

from data.plugins.astrbot_plugin_superbot.ad_state import migrate
from data.plugins.astrbot_plugin_superbot.ads import Ads
from data.plugins.astrbot_plugin_superbot.payments import Payments
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store


@pytest.fixture
def env(tmp_path):
    now = [1800000000.0]
    s = Store(tmp_path / "db", "1", lambda: now[0])
    s.db.execute(
        "CREATE TABLE mod_groups(chat TEXT PRIMARY KEY,title TEXT,enabled INTEGER)"
    )
    s.db.execute("INSERT INTO mod_groups VALUES('-1001','Test',1)")
    a = Ads(s)
    with s.tx() as db:
        s.put(db, "modules", {"ads": True})
        s.put(db, "ad_board_enabled", True)
        s.put(db, "usdt_health", {"at": now[0], "error": ""})
    cfg = {
        "name": "test",
        "kind": "pinned",
        "price": "10",
        "currency": "USDT",
        "target": "-1001",
        "duration": "30days",
        "slots": 3,
        "payment": "test",
        "enabled": True,
    }
    a.configure("1", "p", cfg)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=8)),
        edit_message_text=AsyncMock(),
        pin_chat_message=AsyncMock(),
        unpin_chat_message=AsyncMock(),
    )
    yield s, a, now, bot
    s.close()


def order(s, a, key, paid=True):
    a.submit("2", "广告", key, "contact", "p", s.get("packages")["p"], key)
    a.moderate("1", key, "approve")
    if paid:
        a.moderate("1", key, "paid", "verified")


@pytest.mark.asyncio
async def test_renewal_crosses_expiry_without_unpin(env):
    s, a, now, b = env
    order(s, a, "first")
    await a.tick(b)
    old = s.db.execute("SELECT * FROM ads WHERE id='first'").fetchone()
    a.renew("2", "first", "p", s.get("packages")["p"], "renew")
    a.moderate("1", "renew", "approve")
    a.moderate("1", "renew", "paid", "verified")
    now[0] = old["expires"] + 1
    await a.tick(b)
    new = s.db.execute("SELECT * FROM ads WHERE id='renew'").fetchone()
    assert (
        new["status"] == "active"
        and new["slot"] == old["slot"]
        and new["message"] == old["message"]
    )
    assert (
        s.db.execute("SELECT status FROM ads WHERE id='first'").fetchone()[0]
        == "renewed"
    )
    b.unpin_chat_message.assert_not_awaited()
    with pytest.raises(Rejected):
        a.moderate("1", "first", "stop", "test")
    a.renew("2", "renew", "p", s.get("packages")["p"], "next")
    a.moderate("1", "next", "approve")
    a.moderate("1", "next", "paid", "verified")
    await a.tick(b)
    assert (
        s.db.execute("SELECT slot FROM ads WHERE id='next'").fetchone()[0]
        == old["slot"]
    )


@pytest.mark.asyncio
async def test_cancel_refund_and_target_disabled(env):
    s, a, now, b = env
    p = Payments(s, {"usdt_address": ADDRESS, "usdt_enabled": True})
    try:
        with s.tx() as db:
            p.move(db, "seed", "2", 20000000, "seed")
        order(s, a, "one", False)
        v = s.db.execute("SELECT version FROM ads WHERE id='one'").fetchone()[0]
        s.db.execute("UPDATE mod_groups SET enabled=0")
        with pytest.raises(Rejected):
            p.pay("2", "one", 10000000, v)
        s.db.execute("UPDATE mod_groups SET enabled=1")
        p.pay("2", "one", 10000000, v)
        with pytest.raises(Rejected):
            a.cancel("2", "one", v)
        v = s.db.execute("SELECT version FROM ads WHERE id='one'").fetchone()[0]
        a.cancel("2", "one", v)
        a.cancel("2", "one", v)
        assert p.balance("2") == 20000000
        assert (
            s.db.execute(
                "SELECT COUNT(*) FROM ad_money WHERE op='refund:one'"
            ).fetchone()[0]
            == 1
        )
        await a.tick(b)
        b.send_message.assert_not_awaited()
        order(s, a, "manual")
        a.cancel("2", "manual")
        assert (
            s.db.execute("SELECT refund_state FROM ads WHERE id='manual'").fetchone()[0]
            == "manual_pending"
        )
        order(s, a, "test")
        s.db.execute("UPDATE ads SET payment_source='test' WHERE id='test'")
        a.cancel("2", "test")
        assert p.balance("2") == 20000000
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_unknown_claim_blocks_cancel_and_manual_recovery(env):
    s, a, now, b = env
    order(s, a, "one")
    b.pin_chat_message.side_effect = TimeoutError()
    await a.tick(b)
    op = s.db.execute("SELECT * FROM ad_operations").fetchone()
    assert op["status"] == "review" and op["message"] == 8
    with pytest.raises(Rejected):
        a.cancel("2", "one")
    await Ads(s).tick(b)
    b.send_message.assert_awaited_once()
    from data.plugins.astrbot_plugin_superbot.ad_maintenance_ui import action

    ui = SimpleNamespace(store=s, render=AsyncMock(), runtime=SimpleNamespace(ads=a))
    u = SimpleNamespace(effective_user=SimpleNamespace(id=2))
    with pytest.raises(Rejected):
        await action(
            ui, u, {"action": "ad_recover_confirm", "op": op["id"], "message": 8}
        )
    u.effective_user.id = 1
    await action(ui, u, {"action": "ad_recover_confirm", "op": op["id"], "message": 8})
    assert (
        s.db.execute("SELECT status FROM ads WHERE id='one'").fetchone()[0] == "active"
    )
    b.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_limit_retry_resume_same_message(env):
    s, a, now, b = env
    order(s, a, "one")
    b.pin_chat_message.side_effect = [RetryAfter(10), None]
    await a.tick(b)
    assert s.db.execute("SELECT status FROM ad_operations").fetchone()[0] == "retry"
    now[0] += 12
    await Ads(s).tick(b)
    b.send_message.assert_awaited_once()
    assert (
        s.db.execute("SELECT status FROM ads WHERE id='one'").fetchone()[0] == "active"
    )


@pytest.mark.asyncio
async def test_stable_slots_fifo_and_queue_cancel(env):
    s, a, now, b = env
    order(s, a, "first")
    now[0] += 1
    order(s, a, "second")
    await a.tick(b)
    first = s.db.execute("SELECT slot FROM ads WHERE id='first'").fetchone()[0]
    second = s.db.execute("SELECT slot FROM ads WHERE id='second'").fetchone()[0]
    a.moderate("1", "first", "stop", "test")
    await a.tick(b)
    assert (
        s.db.execute("SELECT slot FROM ads WHERE id='second'").fetchone()[0] == second
    )
    assert f"广告位 {second}" in b.edit_message_text.call_args.kwargs["text"]
    s.db.execute("UPDATE ad_targets SET capacity=1")
    now[0] += 1
    order(s, a, "third")
    await a.tick(b)
    assert (
        s.db.execute("SELECT status FROM ads WHERE id='third'").fetchone()[0]
        == "pending"
    )
    a.cancel("2", "third")
    assert first != second


@pytest.mark.asyncio
async def test_cancel_race_after_claim_and_edit_versions(env):
    s, a, now, b = env
    order(s, a, "one")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=8)

    b.send_message.side_effect = send
    task = asyncio.create_task(a.tick(b))
    await entered.wait()
    with pytest.raises(Rejected):
        a.cancel("2", "one")
    release.set()
    await task
    row = s.db.execute("SELECT * FROM ads WHERE id='one'").fetchone()
    await a.edit_published("1", "one", "new", "one", b, row["version"])
    with pytest.raises(Rejected):
        await a.edit_published("1", "one", "stale", "new", b, row["version"])
    after = s.db.execute("SELECT * FROM ads WHERE id='one'").fetchone()
    assert after["expires"] == row["expires"] and after["paid"] == row["paid"]


def test_repeat_migration_preserves_orders(env):
    s, a, now, b = env
    order(s, a, "one")
    before = tuple(s.db.execute("SELECT * FROM ads WHERE id='one'").fetchone())
    migrate(s)
    migrate(s)
    assert tuple(s.db.execute("SELECT * FROM ads WHERE id='one'").fetchone()) == before


@pytest.mark.asyncio
async def test_long_queue_head_no_overtake_and_other_target_runs(env):
    s, a, now, b = env
    order(s, a, "first")
    a.edit("1", "first", "a" * 2100, "first")
    a.moderate("1", "first", "approve")
    await a.tick(b)
    now[0] += 1
    order(s, a, "long")
    a.edit("1", "long", "b" * 2100, "long")
    a.moderate("1", "long", "approve")
    now[0] += 1
    order(s, a, "short")
    await a.tick(b)
    assert (
        s.db.execute("SELECT status FROM ads WHERE id='short'").fetchone()[0]
        == "pending"
    )
    assert "篇幅" in s.db.execute("SELECT error FROM ads WHERE id='long'").fetchone()[0]
    a.cancel("2", "long")
    await a.tick(b)
    assert (
        s.db.execute("SELECT status FROM ads WHERE id='short'").fetchone()[0]
        == "active"
    )
    s.db.execute("INSERT INTO mod_groups VALUES('-1002','Second',1)")
    a.configure("1", "q", {**s.get("packages")["p"], "target": "-1002"})
    a.submit("2", "广告", "other", "contact", "q", s.get("packages")["q"], "other")
    a.moderate("1", "other", "approve")
    a.moderate("1", "other", "paid", "verified")
    s.db.execute("UPDATE ad_boards SET state='review' WHERE chat='-1001'")
    await a.tick(b)
    assert (
        s.db.execute("SELECT status FROM ads WHERE id='other'").fetchone()[0]
        == "active"
    )


@pytest.mark.asyncio
async def test_scan_failure_keeps_cursor_and_daily_rescan(env):
    s, a, now, b = env
    p = Payments(s, {"usdt_enabled": True, "usdt_address": ADDRESS})
    key = "usdt_scan_v2/" + ADDRESS
    with s.tx() as db:
        s.put(db, "usdt_scan_start", now[0] - 10 * 86400)
        s.put(db, key, {"watermark": now[0], "last_rescan": 0, "job": None})
    calls = []
    failed = [False]

    def handler(req):
        calls.append(dict(req.url.params))
        if req.url.params.get("fingerprint") == "next" and not failed[0]:
            failed[0] = True
            raise httpx.ReadTimeout("fixture")
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [],
                "meta": {"fingerprint": "next"}
                if not req.url.params.get("fingerprint")
                else {},
            },
        )

    await p.client.aclose()
    p.client = httpx.AsyncClient(
        base_url="https://api.trongrid.io", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(httpx.ReadTimeout):
            await p.tick()
        state = s.get(key)
        assert state["job"]["fingerprint"] == "next" and state["job"]["daily"]
        assert state["job"]["start"] == now[0] - 7 * 86400
        await p.tick()
        assert s.get(key)["job"] is None and s.get(key)["last_rescan"] == now[0]
        assert calls[-1]["fingerprint"] == "next"
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_deposit_write_failure_rolls_back_evidence(env, monkeypatch):
    s, a, now, b = env
    p = Payments(s, {"usdt_enabled": True, "usdt_address": ADDRESS})
    try:
        invoice = p.create("2", 10, "write-fail")

        def fail(*args):
            raise RuntimeError("database write fixture")

        original = p.move
        monkeypatch.setattr(p, "move", fail)
        with pytest.raises(RuntimeError):
            p.ingest(*evidence(invoice))
        assert s.db.execute("SELECT COUNT(*) FROM chain_events").fetchone()[0] == 0
        monkeypatch.setattr(p, "move", original)
        p.ingest(*evidence(invoice))
        p.ingest(*evidence(invoice))
        assert p.balance("2") == invoice["amount"]
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_scan_pagination_resume_and_late_valid_transfer(env):
    s, a, now, b = env
    p = Payments(s, {"usdt_enabled": True, "usdt_address": ADDRESS})
    try:
        invoice = p.create("2", 10, "deposit")
        index, receipt = evidence(invoice)
        with s.tx() as db:
            s.put(db, "usdt_scan_start", now[0] - 86400)
        now[0] += 1900
        calls = []

        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json=receipt)
            fingerprint = request.url.params.get("fingerprint")
            calls.append(fingerprint)
            n = int(fingerprint or 0)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [index] if n == 21 else [],
                    "meta": {"fingerprint": str(n + 1)} if n < 21 else {},
                },
            )

        await p.client.aclose()
        p.client = httpx.AsyncClient(
            base_url="https://api.trongrid.io", transport=httpx.MockTransport(handler)
        )
        await p.tick()
        state = s.get("usdt_scan_v2/" + ADDRESS)
        assert state["job"]["fingerprint"] == "20"
        assert s.db.execute("SELECT status FROM deposits").fetchone()[0] == "expired"
        await p.tick()
        assert calls[20] == "20"
        assert p.balance("2") == invoice["amount"]
        await p.tick()
        assert p.balance("2") == invoice["amount"]
    finally:
        await p.close()
