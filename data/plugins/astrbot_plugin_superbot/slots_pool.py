"""Versioned solo/pool settlement with immutable tie-break evidence."""

import json
import secrets
from html import escape

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup

from .slots import NAMES, STAKES, SYMBOLS, rank
from .store import encode


def finish(engine, identity, cancel=False):
    """Freeze all round evidence before crediting a version-three table.

    Args:
        engine: Existing slots service and shared transactional store.
        identity: Table identifier.
        cancel: Cancel an unlocked registration table.
    """
    store = engine.store
    with store.tx() as db:
        table = db.execute(
            "SELECT * FROM slots_tables WHERE id=?", (identity,)
        ).fetchone()
        if not table or table["status"] not in {"open", "locked"}:
            return
        if table["status"] == "open":
            if not cancel and store.clock() < table["deadline"]:
                return
            people = db.execute(
                "SELECT * FROM slots_players WHERE table_id=? AND status='active' ORDER BY ordinal",
                (identity,),
            ).fetchall()
            snapshot = json.loads(table["snapshot"])
            snapshot.update(mode="solo" if len(people) == 1 else "pool", winner=None)
            snapshot["refund"] = bool(cancel or not people)
            snapshot["refund_reason"] = "功能关闭或面板不可用" if cancel else "无人参与"
            contenders = list(people)
            seq = 0
            if not snapshot["refund"]:
                for round_no in range(snapshot["max_extra_rounds"] + 1):
                    results = [
                        (p, [secrets.randbelow(4) for _ in range(3)])
                        for p in contenders
                    ]
                    luck = (
                        {
                            p["uid"]: secrets.randbelow(snapshot["luck_max"]) + 1
                            for p, _ in results
                        }
                        if snapshot["version"] == "slots-5"
                        else {}
                    )
                    best = max(
                        (rank(result), luck.get(p["uid"], 0)) for p, result in results
                    )
                    winners = [
                        p
                        for p, result in results
                        if (rank(result), luck.get(p["uid"], 0)) == best
                    ]
                    for p, result in results:
                        seq += 1
                        db.execute(
                            "INSERT INTO slots_v3_rounds(table_id,round,uid,result,advanced,seq,luck) VALUES(?,?,?,?,?,?,?)",
                            (
                                identity,
                                round_no,
                                p["uid"],
                                encode(result),
                                int((rank(result), luck.get(p["uid"], 0)) == best),
                                seq,
                                luck.get(p["uid"]),
                            ),
                        )
                        db.execute(
                            "UPDATE slots_players SET result=? WHERE table_id=? AND uid=?",
                            (encode(result), identity, p["uid"]),
                        )
                    snapshot["last_round"] = round_no
                    if len(winners) == 1:
                        snapshot["winner"] = winners[0]["uid"]
                        break
                    contenders = winners
                else:
                    snapshot.update(
                        refund=True, refund_reason="追加5轮仍平局，全桌退款"
                    )
            db.execute(
                "UPDATE slots_tables SET status='locked',snapshot=?,next_edit=0 WHERE id=?",
                (encode(snapshot), identity),
            )
            store.audit(
                db, "system", "slots_v3_locked", {"table": identity, **snapshot}
            )
    # Persisted outcomes survive a failed credit or process interruption.
    with store.tx() as db:
        table = db.execute(
            "SELECT * FROM slots_tables WHERE id=?", (identity,)
        ).fetchone()
        if table["status"] != "locked":
            return
        snapshot = json.loads(table["snapshot"])
        people = db.execute(
            "SELECT * FROM slots_players WHERE table_id=? AND status='active' ORDER BY ordinal",
            (identity,),
        ).fetchall()
        pool = sum(p["stake"] for p in people)
        refund = snapshot["refund"]
        fee = (
            pool * snapshot["fee_percent"] // 100
            if not refund and snapshot["mode"] == "pool"
            else 0
        )
        for p in people:
            if refund:
                payout = p["stake"]
            elif snapshot["mode"] == "solo":
                payout = (
                    p["stake"]
                    * snapshot["multipliers"][str(rank(json.loads(p["result"])))]
                    // 10
                )
            else:
                payout = pool - fee if p["uid"] == snapshot["winner"] else 0
            if payout:
                kind = "refund" if refund else "payout"
                store.credit(
                    db,
                    f"slots:{identity}:{p['uid']}:{kind}",
                    p["uid"],
                    payout,
                    f"slots_{kind}",
                    chat=table["chat"],
                )
            db.execute(
                "UPDATE slots_players SET status=?,payout=? WHERE table_id=? AND uid=?",
                ("refunded" if refund else "settled", payout, identity, p["uid"]),
            )
        outcomes = db.execute(
            "SELECT seq,uid FROM slots_v3_rounds WHERE table_id=? ORDER BY seq",
            (identity,),
        ).fetchall()
        if refund and not outcomes:
            db.execute(
                "UPDATE slots_outbox SET status='cancelled' WHERE table_id=? AND kind='panel' AND status='pending'",
                (identity,),
            )
        for result in outcomes:
            db.execute(
                "INSERT INTO slots_outbox(table_id,seq,kind,uid) VALUES(?,?,'animation',?)",
                (identity, result["seq"], result["uid"]),
            )
        if snapshot["version"] in {"slots-4", "slots-5"}:
            db.execute(
                "INSERT INTO slots_outbox(table_id,seq,kind) VALUES(?,?,'summary')",
                (identity, len(outcomes) + 1),
            )
        db.execute(
            "UPDATE slots_tables SET status=?,pool=?,fee=?,next_edit=0,error=? WHERE id=?",
            (
                "cancelled" if refund else "settled",
                pool,
                fee,
                snapshot["refund_reason"] if refund else "",
                identity,
            ),
        )
        store.audit(
            db,
            "system",
            "slots_v3_finish",
            {
                "table": identity,
                "pool": pool,
                "fee": fee,
                "winner": snapshot["winner"],
                "refund": refund,
                "mode": snapshot["mode"],
            },
        )


def render(engine, table, summary=False):
    """Render a bounded v3 panel, including chronological tie-break evidence.

    Args:
        engine: Slots service.
        table: Persisted table.
        summary: Whether this is the final outbox summary.

    Returns:
        HTML text and an inline keyboard.
    """
    identity = table["id"]
    people = engine.store.db.execute(
        "SELECT * FROM slots_players WHERE table_id=? AND status!='withdrawn' ORDER BY ordinal",
        (identity,),
    ).fetchall()
    snapshot = json.loads(table["snapshot"])
    pool = sum(p["stake"] for p in people)
    text = f"<b>🎰 第{identity}桌 ·老虎机PvP</b>\n总投入 <b>{pool}积分</b> · {len(people)}人\n"
    rows = []
    if table["status"] == "open":
        left = max(0, int(table["deadline"] - engine.store.clock()))
        text += (
            f"⏳ 报名剩余 <b>{left}秒</b>\n"
            "每人自选投入，胜率不加权。\n"
            "多人：唯一胜者获总池90%，抽水10%；最高同牌型免费加赛，最多追加5轮仍平局全退。\n"
            "单人：散牌0倍／对子1倍／三连6.2倍，不额外抽水。\n"
        )
        buttons = [
            Button(f"{s}积分", callback_data=f"sl:t:{identity}:{s}") for s in STAKES
        ]
        rows = [
            buttons[:2],
            buttons[2:],
            [Button("退出退款", callback_data=f"sl:t:{identity}:leave")],
        ]
        if snapshot["version"] in {"slots-4", "slots-5"}:
            text = text.replace(
                "每人自选投入，胜率不加权。",
                f"本桌每人投入{table['stake']}积分，与发起者同额。",
            )
            rows = [
                [
                    Button(
                        f"加入 · {table['stake']}积分",
                        callback_data=f"sl:t:{identity}:join",
                    ),
                    Button("退出退款", callback_data=f"sl:t:{identity}:leave"),
                ]
            ]
        if snapshot["version"] == "slots-5":
            text = text.replace(
                "最高同牌型免费加赛",
                "同牌型比幸运点1—1,000,000，高者胜；完全同分免费加赛",
            )
    elif table["status"] == "locked":
        text += "🔒 报名已锁定，结果已保存，正在派发。\n"
    elif table["status"] == "cancelled":
        text += "↩️ <b>原投入已全额退还，不抽水。</b>\n" + escape(table["error"]) + "\n"
    else:
        text += f"🏁 <b>已结算 · {'单人模式' if snapshot['mode'] == 'solo' else '多人奖池'}</b>\n"
        winner = next((p for p in people if p["uid"] == snapshot["winner"]), None)
        if winner and snapshot["mode"] == "pool":
            text += f'🏆 <a href="tg://user?id={winner["uid"]}">{escape(winner["label"][:24])}</a> 独得 <b>{winner["payout"]}积分</b>\n'
        text += f"抽水 {table['fee']} · 总返还 {sum(p['payout'] for p in people)}积分\n"
    outcomes = engine.store.db.execute(
        "SELECT * FROM slots_v3_rounds WHERE table_id=? ORDER BY round,seq", (identity,)
    ).fetchall()
    history = {}
    for outcome in outcomes:
        history.setdefault(outcome["uid"], []).append(outcome)
    # A later tie-break entrant always ranks ahead of an earlier eliminated player.
    ranked = sorted(
        people,
        key=lambda p: (
            -int(p["uid"] == snapshot.get("winner")),
            -max((r["round"] for r in history.get(p["uid"], [])), default=-1),
            -rank(json.loads(p["result"])) if p["result"] else 0,
            -(history[p["uid"]][-1]["luck"] or 0) if history.get(p["uid"]) else 0,
            p["ordinal"],
        ),
    )
    previous, place = None, 0
    for index, p in enumerate(ranked, 1):
        score = (
            p["uid"] == snapshot.get("winner"),
            max((r["round"] for r in history.get(p["uid"], [])), default=-1),
            rank(json.loads(p["result"])) if p["result"] else 0,
            (history[p["uid"]][-1]["luck"] or 0) if history.get(p["uid"]) else 0,
        )
        if score != previous:
            place = index
        previous = score
        text += f'<blockquote expandable>{"第" + str(place) + "名 · " if outcomes else ""}<a href="tg://user?id={p["uid"]}">{escape(p["label"][:24])}</a> · 投入{p["stake"]}'
        if table["status"] in {"settled", "cancelled"}:
            text += f" · 返还{p['payout']} · 净增减{p['payout'] - p['stake']:+d}"
        for r in history.get(p["uid"], []):
            result = json.loads(r["result"])
            text += (
                "\n"
                + ("初轮" if r["round"] == 0 else f"加赛{r['round']}")
                + " "
                + "".join(SYMBOLS[n] for n in result)
                + " "
                + NAMES[rank(result)]
                + (f" · 🍀{r['luck']:,}" if r["luck"] is not None else "")
            )
        text += "</blockquote>\n"
    if summary:
        text += "⏱ 汇总及已送达动画60秒后撤回。"
    return text, InlineKeyboardMarkup(rows)
