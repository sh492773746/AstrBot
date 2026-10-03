"""Serialized advertisements with durable upstream operation journals."""

import asyncio
import json
import secrets

from telegram.error import BadRequest, RetryAfter

from .ad_state import available
from .store import Rejected, encode


def claim(store, chat, kind, payload, message=None):
    """Persist exact intended changes before any remote write.

    Args:
        store: Database owner outside a transaction.
        chat: Target ID.
        kind: Board synchronization or standalone edit.
        payload: Before/after text and order changes.
        message: Previously known message ID.

    Returns:
        Unique operation ID.
    """
    op = secrets.token_urlsafe(16)
    with store.tx() as db:
        if db.execute(
            "SELECT 1 FROM ad_operations WHERE chat=? AND status IN ('executing','retry','review')",
            (chat,),
        ).fetchone():
            raise Rejected("此目标正在执行或待核查")
        db.execute(
            "INSERT INTO ad_operations(id,chat,kind,status,payload,message,at) VALUES(?,?,?,'executing',?,?,?)",
            (op, chat, kind, encode(payload), message, store.clock()),
        )
        if kind == "board":
            db.execute("UPDATE ad_boards SET state='busy' WHERE chat=?", (chat,))
        for change in payload["changes"]:
            row = db.execute("SELECT * FROM ads WHERE id=?", (change["id"],)).fetchone()
            if not row or row["version"] != change["version"]:
                raise Rejected("订单已变化")
            change["before_order"] = {
                k: row[k]
                for k in (
                    "status",
                    "message",
                    "expires",
                    "slot",
                    "body",
                    "first_published",
                    "error",
                )
            }
            db.execute("UPDATE ads SET status='sending' WHERE id=?", (change["id"],))
        db.execute(
            "UPDATE ad_operations SET payload=? WHERE id=?", (encode(payload), op)
        )
        store.audit(
            db, "system", "ad_operation_claim", {"id": op, "chat": chat, "kind": kind}
        )
    return op


def finish(store, op, message, actor="system"):
    """Commit verified changes, or an explicitly audited manual attestation.

    Args:
        store: Instance database owner.
        op: Journal identifier.
        message: Verified or manually attested message ID.
        actor: System or administrator.
    """
    from .ads import expiry

    with store.tx() as db:
        row = db.execute("SELECT * FROM ad_operations WHERE id=?", (op,)).fetchone()
        if row["status"] == "done":
            return
        data = json.loads(row["payload"])
        now = store.clock()
        for change in data["changes"]:
            current = db.execute(
                "SELECT * FROM ads WHERE id=?", (change["id"],)
            ).fetchone()
            expires = change.get("expires", current["expires"])
            if change["new"] and (
                change["status"] == "active"
                or (change["status"] == "published" and current["tenant"] != "platform")
            ):
                expires = expiry(now, json.loads(current["package"])["duration"])
            db.execute(
                "UPDATE ads SET status=?,message=?,expires=?,slot=?,body=?,first_published=COALESCE(first_published,?),error=? WHERE id=?",
                (
                    change["status"],
                    message,
                    expires,
                    change.get("slot", current["slot"]),
                    change.get("body", current["body"]),
                    now if change["status"] in ("active", "published") else None,
                    "",
                    change["id"],
                ),
            )
        if row["kind"] == "board":
            db.execute(
                "UPDATE ad_boards SET state='idle',message=?,body=?,error='' WHERE chat=?",
                (message, data["after"], row["chat"]),
            )
        db.execute(
            "UPDATE ad_operations SET status='done',message=?,error='' WHERE id=?",
            (message, op),
        )
        store.audit(
            db,
            actor,
            "ad_operation_complete"
            if actor == "system"
            else "ad_operation_manual_attestation",
            {"id": op, "message": message},
        )


async def execute(ads, bot, op):
    """Resume known steps without replaying uncertain writes.

    Args:
        ads: Advertising service.
        bot: Existing Telegram client.
        op: Journal identifier.
    """
    store = ads.store
    row = store.db.execute("SELECT * FROM ad_operations WHERE id=?", (op,)).fetchone()
    if row["status"] not in ("executing", "retry") or row["retry_at"] > store.clock():
        return
    data = json.loads(row["payload"])
    message = row["message"]
    done = json.loads(row["step"] or "[]")
    merchant_new = [
        store.db.execute(
            "SELECT * FROM ads WHERE id=? AND tenant<>'platform'", (c["id"],)
        ).fetchone()
        for c in data["changes"]
        if c["new"]
    ]
    if any(merchant_new):
        try:
            from .merchant_ads import authorized
            from .tenants import binding

            authority = binding(store, row["chat"])
            await ads.runtime.tenants.verify(
                authority["owner"],
                row["chat"],
                "ads",
                "can_pin_messages" if data.get("pin") else None,
            )
            for order in merchant_new:
                if order:
                    fresh = store.db.execute(
                        "SELECT * FROM ads WHERE id=?", (order["id"],)
                    ).fetchone()
                    available(store, json.loads(fresh["package"]), fresh["package_key"])
                    if not authorized(store, fresh):
                        raise Rejected("独立订单付款或审核状态已变化")
        except Exception as exc:
            with store.tx() as db:
                db.execute(
                    "UPDATE ad_operations SET status='review',error=? WHERE id=?",
                    ("Merchant preflight: " + type(exc).__name__, op),
                )
                for order in merchant_new:
                    if order:
                        db.execute(
                            "UPDATE ads SET status='review',error='发布前权限或付款复核失败，未重发' WHERE id=?",
                            (order["id"],),
                        )
                if row["kind"] == "board":
                    db.execute(
                        "UPDATE ad_boards SET state='review',error='merchant_preflight' WHERE chat=?",
                        (row["chat"],),
                    )
            return
    if row["status"] == "retry":
        try:
            for change in data["changes"]:
                if change["new"]:
                    order = store.db.execute(
                        "SELECT * FROM ads WHERE id=?", (change["id"],)
                    ).fetchone()
                    available(store, json.loads(order["package"]), order["package_key"])
                    from .ad_state import payment_authorized

                    if not payment_authorized(store, order):
                        raise Rejected("审核或付款授权待核查")
        except Rejected as exc:
            with store.tx() as db:
                db.execute(
                    "UPDATE ad_operations SET status='review',error=? WHERE id=?",
                    (str(exc), op),
                )
                if row["kind"] == "board":
                    db.execute(
                        "UPDATE ad_boards SET state='review',error=? WHERE chat=?",
                        (str(exc), row["chat"]),
                    )
                for change in data["changes"]:
                    db.execute(
                        "UPDATE ads SET status='review',error=? WHERE id=?",
                        (str(exc), change["id"]),
                    )
            return
    steps = (["send"] if not message or "send" in done else ["edit"]) + (
        ["pin"]
        if data.get("pin")
        else ["unpin"]
        if row["kind"] in ("board", "unpin")
        else []
    )
    for step in steps:
        if step in done:
            continue
        store.db.execute(
            "UPDATE ad_operations SET status='executing' WHERE id=?", (op,)
        )
        try:
            if step == "send":
                sent = await bot.send_message(chat_id=row["chat"], text=data["after"])
                message = sent.message_id
            elif step == "edit":
                if data["before"] != data["after"]:
                    await bot.edit_message_text(
                        chat_id=row["chat"], message_id=message, text=data["after"]
                    )
            elif step == "pin":
                await bot.pin_chat_message(
                    chat_id=row["chat"], message_id=message, disable_notification=True
                )
            else:
                await bot.unpin_chat_message(chat_id=row["chat"], message_id=message)
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else exc.retry_after
            )
            store.db.execute(
                "UPDATE ad_operations SET status='retry',retry_at=?,error='Telegram 限流，等待重试' WHERE id=?",
                (store.clock() + float(delay) + 1, op),
            )
            return
        except Exception as exc:
            if not (
                step == "edit"
                and isinstance(exc, BadRequest)
                and "message is not modified" in str(exc).lower()
            ):
                with store.tx() as db:
                    db.execute(
                        "UPDATE ad_operations SET status='review',message=?,error=? WHERE id=?",
                        (message, type(exc).__name__, op),
                    )
                    if row["kind"] == "board":
                        db.execute(
                            "UPDATE ad_boards SET state='review',error=? WHERE chat=?",
                            (type(exc).__name__, row["chat"]),
                        )
                    for change in data["changes"]:
                        db.execute(
                            "UPDATE ads SET status='review',error='广告操作结果待核查' WHERE id=?",
                            (change["id"],),
                        )
                return
        done.append(step)
        store.db.execute(
            "UPDATE ad_operations SET step=?,message=? WHERE id=?",
            (encode(done), message, op),
        )
    finish(store, op, message)


async def tick(ads, bot):
    """Reconcile targets independently under their shared execution locks.

    Args:
        ads: Advertising service.
        bot: Existing Telegram client.
    """
    store = ads.store
    chats = {r[0] for r in store.db.execute("SELECT chat FROM ad_boards")}
    chats.update(
        r[0]
        for r in store.db.execute(
            "SELECT DISTINCT json_extract(package,'$.target') FROM ads WHERE status='pending' AND paid=1 AND approved=1 AND json_extract(package,'$.kind')='pinned'"
        )
    )
    chats.update(
        r[0]
        for r in store.db.execute("SELECT chat FROM ad_operations WHERE status='retry'")
    )
    for chat in sorted(chats):
        lock = store.ad_locks.setdefault(chat, asyncio.Lock())
        if lock.locked():
            continue
        async with lock:
            try:
                await sync(ads, bot, chat)
            except Exception as exc:
                store.audit(
                    store.db,
                    "system",
                    "ad_target_failure",
                    {"chat": chat, "error": type(exc).__name__},
                )


async def sync(ads, bot, chat):
    """Prepare renewals before expiry and retain stable display positions.

    Args:
        ads: Advertising service.
        bot: Existing Telegram client.
        chat: Serialized target ID.
    """
    from .ads import expiry

    store = ads.store
    now = store.clock()
    channel = store.db.execute(
        "SELECT enabled FROM ad_channels WHERE chat=?", (chat,)
    ).fetchone()
    if channel:
        from .channels import check

        try:
            await check(bot, chat)
        except Exception as exc:
            store.db.execute(
                "UPDATE ads SET error=? WHERE json_extract(package,'$.target')=? AND status IN ('pending','active')",
                ("频道权限或连接失败：" + type(exc).__name__, chat),
            )
            return
    outstanding = store.db.execute(
        "SELECT * FROM ad_operations WHERE chat=? AND status IN ('executing','retry','review') ORDER BY at LIMIT 1",
        (chat,),
    ).fetchone()
    if outstanding:
        if outstanding["status"] == "retry":
            await execute(ads, bot, outstanding["id"])
        return
    store.db.execute(
        "INSERT OR IGNORE INTO ad_boards(chat,state,body) VALUES(?,'idle','')", (chat,)
    )
    board = store.db.execute("SELECT * FROM ad_boards WHERE chat=?", (chat,)).fetchone()
    if board["state"] != "idle":
        return
    maximum = max(
        [
            p["slots"]
            for p in store.get("packages", {}).values()
            if p["target"] == chat and p["kind"] == "pinned"
        ]
        or [1]
    )
    store.db.execute(
        "INSERT OR IGNORE INTO ad_targets(chat,capacity) VALUES(?,?)", (chat, maximum)
    )
    capacity = store.db.execute(
        "SELECT capacity FROM ad_targets WHERE chat=?", (chat,)
    ).fetchone()[0]
    reserved = [
        dict(r)
        for r in store.db.execute(
            "SELECT id,slot FROM ads WHERE status IN ('active','review') AND json_extract(package,'$.kind')='pinned' AND json_extract(package,'$.target')=? AND (message IS NOT ?)",
            (chat, board["message"]),
        )
    ]
    existing = [
        dict(r)
        for r in store.db.execute(
            "SELECT * FROM ads WHERE status='active' AND message=? AND json_extract(package,'$.target')=? ORDER BY slot,at,id",
            (board["message"], chat),
        )
    ]
    queue = [
        dict(r)
        for r in store.db.execute(
            "SELECT * FROM ads WHERE status='pending' AND approved=1 AND paid=1 AND json_extract(package,'$.kind')='pinned' AND json_extract(package,'$.target')=? ORDER BY ready_at,at,id",
            (chat,),
        )
    ]
    changes = []
    renewed = set()
    blocked = set()
    legacy_ids = {r["id"] for r in reserved}
    queue = [r for r in queue if r["renewal_of"] not in legacy_ids]
    for row in queue:
        try:
            available(store, json.loads(row["package"]), row["package_key"])
        except Rejected as exc:
            store.db.execute("UPDATE ads SET error=? WHERE id=?", (str(exc), row["id"]))
            blocked.add(row["id"])
            continue
        from .ad_state import payment_authorized

        if not payment_authorized(store, row):
            store.db.execute(
                "UPDATE ads SET error='审核或核款授权已失效' WHERE id=?", (row["id"],)
            )
            blocked.add(row["id"])
            continue
        old = next((r for r in existing if r["id"] == row["renewal_of"]), None)
        if old and not old["error"].startswith("管理员停止"):
            new = {
                **row,
                "slot": old["slot"],
                "expires": expiry(
                    old["expires"], json.loads(row["package"])["duration"]
                ),
            }
            if new["expires"] <= now:
                continue
            proposed = [new if r["id"] == old["id"] else r for r in existing]
            if len(render(proposed).encode("utf-16-le")) // 2 > 3900:
                store.db.execute(
                    "UPDATE ads SET error='续期文案过长，请精简后重新审核' WHERE id=?",
                    (row["id"],),
                )
                blocked.add(row["id"])
                continue
            changes += [
                {
                    "id": old["id"],
                    "version": old["version"],
                    "status": "renewed",
                    "new": False,
                },
                {
                    "id": row["id"],
                    "version": row["version"],
                    "status": "active",
                    "new": False,
                    "slot": old["slot"],
                    "expires": new["expires"],
                },
            ]
            existing = proposed
            renewed.add(row["id"])
    expired = [r for r in existing if r["expires"] <= now]
    active = [r for r in existing if r["expires"] > now]
    changes += [
        {"id": r["id"], "version": r["version"], "status": "expired", "new": False}
        for r in expired
    ]
    for row in queue:
        if row["id"] in renewed:
            continue
        if row["id"] in blocked:
            break
        if row["renewal_of"]:
            old = store.db.execute(
                "SELECT status FROM ads WHERE id=?", (row["renewal_of"],)
            ).fetchone()
            if old and old[0] not in ("active", "expired", "stopped"):
                store.db.execute(
                    "UPDATE ads SET error='原广告状态待核查，续期暂缓' WHERE id=?",
                    (row["id"],),
                )
                break
        if len(active) + len(reserved) >= capacity:
            store.db.execute(
                "UPDATE ads SET error='广告栏满位，按就绪顺序排队；未发布可取消退款' WHERE id=?",
                (row["id"],),
            )
            break
        used = {r["slot"] for r in active + reserved}
        slot = next(
            i
            for i in range(1, max(capacity, len(active) + len(reserved)) + 2)
            if i not in used
        )
        new = {**row, "slot": slot}
        if len(render(active + [new]).encode("utf-16-le")) // 2 > 3900:
            store.db.execute(
                "UPDATE ads SET error='广告栏篇幅不足，等待空位或精简文案；后续订单不插队' WHERE id=?",
                (row["id"],),
            )
            break
        active.append(new)
        changes.append(
            {
                "id": row["id"],
                "version": row["version"],
                "new": True,
                "status": "active",
                "slot": slot,
            }
        )
    if not changes:
        return
    payload = {
        "before": board["body"],
        "after": render(active),
        "pin": bool(active),
        "changes": changes,
    }
    op = claim(store, chat, "board", payload, board["message"])
    await execute(ads, bot, op)


def render(rows):
    """Build exact shared text while retaining numbered positions.

    Args:
        rows: Displayed order snapshots.

    Returns:
        Plain Telegram message content.
    """
    return (
        "\n\n━━━━━━━━━━━━\n\n".join(
            f"【广告位 {r['slot']}】\n{r['body']}"
            for r in sorted(rows, key=lambda r: r["slot"] or 0)
        )
        or "暂无有效广告"
    )
