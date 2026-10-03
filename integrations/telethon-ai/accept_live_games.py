"""Bounded real-game acceptance in the authorized legacy test group."""

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from telethon import TelegramClient

sys.path.insert(0, "/opt/astrbot-prod")
from accept_tenant_pilot import DATABASE, REGISTRY, Probe

from data.plugins.astrbot_plugin_superbot.points import Points
from data.plugins.astrbot_plugin_superbot.store import Store

CHAT = "-1001000000003"


async def run():
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    directory = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "games-" + stamp
    )
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(DATABASE, isolation_level=None, timeout=5)
    db.row_factory = sqlite3.Row
    with sqlite3.connect(directory / "before.sqlite3") as dest:
        db.backup(dest)
    (directory / "before.sqlite3").chmod(0o600)
    report = {"steps": [], "group": CHAT}
    store = Store.__new__(Store)
    store.db, store.owner, store.clock = db, "1000000001", time.time
    clients, players = [], {}
    temporary = tempfile.TemporaryDirectory(prefix="live-games-")

    def record(action, **data):
        item = {"action": action, **data}
        report["steps"].append(item)
        output = directory / "report.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        output.chmod(0o600)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    async def send(alias, text):
        probe, group, uid = players[alias]
        msg = await probe.call(probe.client.send_message, group, text, parse_mode=None)
        record("sent", alias=alias, command=text, id=msg.id)
        return msg

    async def panel(alias, source):
        probe, group, _ = players[alias]
        for delay in (2, 4, 8):
            await asyncio.sleep(delay)
            messages = await probe.call(
                probe.client.get_messages, group, limit=20, min_id=source
            )
            found = [m for m in messages if m.sender_id == 1000000005 and m.buttons]
            if found:
                result = found[0]
                record(
                    "panel",
                    text=result.raw_text[:1600],
                    buttons=[b.text for row in result.buttons for b in row],
                )
                return result
        raise RuntimeError("No playable panel received")

    try:
        registry = json.loads(REGISTRY.read_text())
        for alias in ("acceptance3", "collector", "keywords"):
            spec = registry[alias]
            path = Path(temporary.name) / (alias + ".session")
            with (
                sqlite3.connect(
                    f"file:{spec['session']}.session?mode=ro", uri=True
                ) as src,
                sqlite3.connect(path) as dest,
            ):
                src.backup(dest)
            path.chmod(0o600)
            client = TelegramClient(
                str(path),
                spec["api_id"],
                spec["api_hash"],
                receive_updates=False,
                request_retries=0,
                flood_sleep_threshold=0,
            )
            clients.append(client)
            await client.connect()
            probe = Probe(client)
            me = await probe.call(client.get_me)
            group = await probe.call(client.get_entity, "wdhihji1")
            if group.id != 4304140268:
                raise RuntimeError("Unexpected group")
            players[alias] = (probe, group, str(me.id))
            try:
                await send(alias, "真实玩法验收：仅测试积分，不涉及付款。")
                record("send_available", alias=alias, uid=me.id)
            except Exception as exc:
                record("send_unavailable", alias=alias, error=type(exc).__name__)
        uid = players["acceptance3"][2]
        before = store.balance(uid, CHAT)
        Points(store).adjust(
            "1000000001",
            uid,
            2000,
            "真实玩法验收测试积分",
            "accept-games/" + stamp,
            CHAT,
        )
        record(
            "test_points",
            uid=uid,
            before=before,
            credit=2000,
            balance=store.balance(uid, CHAT),
        )
        probe = players["acceptance3"][0]
        sent = await send("acceptance3", "转盘")
        wheel = await panel("acceptance3", sent.id)
        for draws in (1, 5, 10):
            if draws != 1:
                await asyncio.sleep(4)
                wheel = await probe.call(
                    probe.client.get_messages, players["acceptance3"][1], ids=wheel.id
                )
            suffix = ":50" if draws == 1 else f":50x{draws}"
            button = next(
                b
                for row in wheel.buttons
                for b in row
                if b.data and b.data.decode().endswith(suffix)
            )
            answer = await probe.call(wheel.click, data=button.data)
            await asyncio.sleep(3)
            orders = [
                dict(r)
                for r in db.execute(
                    "SELECT stake,payout,draws,balance FROM wheel_orders WHERE chat=? AND uid=? ORDER BY at DESC LIMIT 1",
                    (CHAT, uid),
                )
            ]
            record(
                "wheel",
                draws=draws,
                answer=getattr(answer, "message", None),
                orders=orders,
                balance=store.balance(uid, CHAT),
            )
            if not orders or orders[0]["draws"] != draws:
                raise RuntimeError("Wheel order not confirmed")
        await send("acceptance3", "快三")
        await asyncio.sleep(3)
        await send("acceptance3", "大1 单1")
        await asyncio.sleep(3)
        record(
            "k3_accepted",
            orders=[
                dict(r)
                for r in db.execute(
                    "SELECT id,issue,play,amount,status,payout FROM k3_bets WHERE chat=? AND uid=? ORDER BY created DESC LIMIT 4",
                    (CHAT, uid),
                )
            ],
        )
        await send("acceptance3", "取消")
        sent = await send("acceptance3", "老虎机")
        slots = await panel("acceptance3", sent.id)
        button = next(
            b
            for row in slots.buttons
            for b in row
            if b.data
            and b.data.decode().startswith("sl:m:")
            and b.data.decode().endswith(":100")
        )
        answer = await probe.call(slots.click, data=button.data)
        record("slots_created", answer=getattr(answer, "message", None))
        for step in range(24):
            await asyncio.sleep(5)
            bets = [
                dict(r)
                for r in db.execute(
                    "SELECT issue,play,amount,status,payout FROM k3_bets WHERE chat=? AND uid=? ORDER BY created DESC LIMIT 4",
                    (CHAT, uid),
                )
            ]
            tables = [
                dict(r)
                for r in db.execute(
                    "SELECT id,status,pool,fee,error FROM slots_tables WHERE chat=? AND creator=? ORDER BY created DESC LIMIT 1",
                    (CHAT, uid),
                )
            ]
            if step % 4 == 0:
                record("settlement_progress", k3=bets, slots=tables)
            if (
                bets
                and all(b["status"] != "pending" for b in bets)
                and tables
                and tables[0]["status"] in ("settled", "refunded", "cancelled")
            ):
                break
        record("final_orders", k3=bets, slots=tables, balance=store.balance(uid, CHAT))
        ledger = [
            dict(r)
            for r in db.execute(
                "SELECT op,delta,reason FROM group_ledger WHERE chat=? AND uid=? AND at>=?",
                (CHAT, uid, time.mktime(time.strptime(stamp, "%Y%m%dT%H%M%SZ"))),
            )
        ]
        record("ledger", entries=ledger)
        for table in ("wheel_orders", "k3_bets", "slots_players"):
            record(
                "schema",
                table=table,
                columns=[r[1] for r in db.execute(f"PRAGMA table_info({table})")],
            )
    except Exception as exc:
        record("stopped", error=type(exc).__name__, detail=str(exc)[:300])
    finally:
        for client in clients:
            await client.disconnect()
        temporary.cleanup()
        db.close()
        print("REPORT", directory / "report.json", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
