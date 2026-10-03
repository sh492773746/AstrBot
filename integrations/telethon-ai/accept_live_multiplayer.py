"""Finish real multiplayer acceptance without fabricating game outcomes."""

import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from accept_live_games import CHAT, Points, Store
from accept_tenant_pilot import DATABASE, REGISTRY, Probe
from telethon import TelegramClient


async def run():
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    folder = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "multiplayer-" + stamp
    )
    folder.mkdir(mode=0o700)
    db = sqlite3.connect(DATABASE, isolation_level=None, timeout=5)
    db.row_factory = sqlite3.Row
    with sqlite3.connect(folder / "before.sqlite3") as dst:
        db.backup(dst)
    (folder / "before.sqlite3").chmod(0o600)
    store = Store.__new__(Store)
    store.db, store.owner, store.clock = db, "1000000001", time.time
    report, players, clients = [], {}, []
    temp = tempfile.TemporaryDirectory(prefix="multiplayer-")

    def record(action, **data):
        report.append({"action": action, **data})
        output = folder / "report.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        output.chmod(0o600)
        print(json.dumps(report[-1], ensure_ascii=False), flush=True)

    async def send(alias, text):
        p, g, _ = players[alias]
        msg = await p.call(p.client.send_message, g, text, parse_mode=None)
        record("send", alias=alias, text=text, id=msg.id)
        await asyncio.sleep(2)
        return msg

    async def read(alias, minimum=0, message=None):
        p, g, _ = players[alias]
        if message:
            return await p.call(p.client.get_messages, g, ids=message)
        for delay in (2, 4, 8):
            await asyncio.sleep(delay)
            rows = await p.call(p.client.get_messages, g, min_id=minimum, limit=12)
            for row in rows:
                if row.sender_id == 1000000005 and row.buttons:
                    return row
        raise RuntimeError("No current keyboard")

    async def click(alias, msg, suffix, prefix):
        p = players[alias][0]
        button = next(
            b
            for row in msg.buttons or []
            for b in row
            if b.data
            and b.data.decode().startswith(prefix)
            and b.data.decode().endswith(suffix)
        )
        result = await p.call(msg.click, data=button.data)
        record(
            "click",
            alias=alias,
            text=button.text,
            answer=getattr(result, "message", None),
            message=msg.id,
        )
        await asyncio.sleep(2)

    try:
        specs = json.loads(REGISTRY.read_text())
        for alias in ("acceptance3", "collector", "keywords"):
            s = specs[alias]
            path = Path(temp.name) / (alias + ".session")
            with (
                sqlite3.connect(
                    f"file:{s['session']}.session?mode=ro", uri=True
                ) as src,
                sqlite3.connect(path) as dst,
            ):
                src.backup(dst)
            path.chmod(0o600)
            c = TelegramClient(
                str(path),
                s["api_id"],
                s["api_hash"],
                receive_updates=False,
                request_retries=0,
                flood_sleep_threshold=0,
            )
            clients.append(c)
            await c.connect()
            p = Probe(c)
            me = await p.call(c.get_me)
            g = await p.call(c.get_entity, "wdhihji1")
            if g.id != 4304140268:
                raise RuntimeError("Unexpected group")
            players[alias] = (p, g, str(me.id))
        for alias in ("collector", "keywords"):
            uid = players[alias][2]
            before = store.balance(uid, CHAT)
            Points(store).adjust(
                "1000000001",
                uid,
                500,
                "多人玩法验收测试积分",
                f"accept-multi/{stamp}/{uid}",
                CHAT,
            )
            record(
                "credit",
                alias=alias,
                before=before,
                amount=500,
                after=store.balance(uid, CHAT),
            )
        sent = await send("acceptance3", "老虎机")
        menu = await read("acceptance3", sent.id)
        await click("acceptance3", menu, ":100", "sl:m:")
        table = db.execute(
            "SELECT * FROM slots_tables WHERE chat=? ORDER BY created DESC LIMIT 1",
            (CHAT,),
        ).fetchone()
        for _ in range(5):
            if table["message"]:
                break
            await asyncio.sleep(2)
            table = db.execute(
                "SELECT * FROM slots_tables WHERE id=?", (table["id"],)
            ).fetchone()
        msg = await read("collector", message=table["message"])
        # Current room exposes a single join button for the founder's stake.
        record(
            "slots_buttons", labels=[b.text for row in msg.buttons or [] for b in row]
        )
        buttons = [
            b
            for row in msg.buttons or []
            for b in row
            if b.data
            and b.data.decode().startswith("sl:t:")
            and (b.data.decode().endswith(":100") or b.data.decode().endswith(":join"))
        ]
        if not buttons:
            raise RuntimeError("Cannot locate same-stake join")
        result = await players["collector"][0].call(msg.click, data=buttons[0].data)
        record("slots_join", answer=getattr(result, "message", None), table=table["id"])
        sent = await send("acceptance3", "扫雷")
        menu = await read("acceptance3", sent.id)
        await click("acceptance3", menu, ":100", "mn:m:")
        mine = db.execute(
            "SELECT * FROM mines_tables WHERE chat=? ORDER BY created DESC LIMIT 1",
            (CHAT,),
        ).fetchone()
        for _ in range(5):
            if mine["message"]:
                break
            await asyncio.sleep(2)
            mine = db.execute(
                "SELECT * FROM mines_tables WHERE id=?", (mine["id"],)
            ).fetchone()
        for alias in ("collector", "keywords"):
            msg = await read(alias, message=mine["message"])
            try:
                await click(alias, msg, ":join100", "mn:t:")
            except Exception as exc:
                record("mines_join_blocked", alias=alias, error=type(exc).__name__)
        record(
            "mines_started",
            table=mine["id"],
            state=json.loads(
                db.execute(
                    "SELECT state FROM mines_tables WHERE id=?", (mine["id"],)
                ).fetchone()[0]
            ),
        )
        # Real server timeouts select unopened cells; do not inspect or choose stored mines.
        for index in range(52):
            await asyncio.sleep(5)
            sl = dict(
                db.execute(
                    "SELECT id,status,pool,fee,error FROM slots_tables WHERE id=?",
                    (table["id"],),
                ).fetchone()
            )
            mn = dict(
                db.execute(
                    "SELECT id,status,error FROM mines_tables WHERE id=?", (mine["id"],)
                ).fetchone()
            )
            if index % 6 == 0:
                record("progress", slots=sl, mines=mn)
            if sl["status"] in ("settled", "cancelled", "refunded") and mn[
                "status"
            ] in ("settled", "cancelled", "refunded"):
                break
        record(
            "multiplayer_results",
            slots=sl,
            mines=mn,
            slots_players=[
                dict(r)
                for r in db.execute(
                    "SELECT uid,stake,result,payout,status FROM slots_players WHERE table_id=?",
                    (table["id"],),
                )
            ],
            mines_state=json.loads(
                db.execute(
                    "SELECT state FROM mines_tables WHERE id=?", (mine["id"],)
                ).fetchone()[0]
            ),
        )
        await send("acceptance3", "加拿大")
        await send("acceptance3", "大1")
        await asyncio.sleep(3)
        record(
            "canada_orders",
            bets=[
                dict(r)
                for r in db.execute(
                    "SELECT id,issue,play,amount,status,payout FROM bets WHERE points_chat=? AND uid=? ORDER BY at DESC LIMIT 3",
                    (CHAT, players["acceptance3"][2]),
                )
            ],
        )
        await send("acceptance3", "取消")
        await send("collector", "取消")
        await send("acceptance3", "对赌 @example_account_two")
        await send("collector", "同意")
        await send("acceptance3", "说一句加油")
        await send("collector", "说一句加油")
        await send("acceptance3", "确认")
        await send("collector", "确认")
        await send("acceptance3", "大")
        await send("collector", "小")
        record(
            "duel_rows",
            rows=[
                dict(r)
                for r in db.execute(
                    "SELECT * FROM duels WHERE chat=? ORDER BY id DESC LIMIT 1", (CHAT,)
                )
            ],
        )
        for alias in ("acceptance3", "collector", "keywords"):
            uid = players[alias][2]
            bal = store.balance(uid, CHAT)
            total = db.execute(
                "SELECT coalesce(sum(delta),0) FROM group_ledger WHERE chat=? AND uid=?",
                (CHAT, uid),
            ).fetchone()[0]
            record(
                "wallet_reconciled",
                alias=alias,
                balance=bal,
                ledger_total=total,
                equal=bal == total,
            )
    except Exception as exc:
        record("stopped", error=type(exc).__name__, detail=str(exc)[:300])
    finally:
        for c in clients:
            await c.disconnect()
        temp.cleanup()
        db.close()
        print("REPORT", folder / "report.json", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
