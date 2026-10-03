"""Durable, once-only cleanup of the bot's own group feature replies."""

import asyncio
import time
import uuid

from . import wire


async def recall_due(adapter) -> None:
    """Recall a bounded batch of accepted feature replies after twenty seconds.

    Args:
        adapter: Account-bound native adapter owning the message ledger.

    Raises:
        CancelledError: If the adapter shuts down during a request.
    """
    if adapter.stopping.is_set() or adapter.connection_state != "online":
        return
    async with adapter.ledger.db.execute(
        "SELECT key,account,group_id,team,peer,client,server_id,message_time,deadline "
        "FROM reply_recalls WHERE state='pending' AND deadline<=? "
        "ORDER BY deadline LIMIT 20",
        (time.time(),),
    ) as cursor:
        rows = await cursor.fetchall()
    for key, account, group, team, peer, client, mid, stamp, deadline in rows:
        async with adapter.send_lock:
            if adapter.stopping.is_set() or adapter.connection_state != "online":
                return
            state = "rejected"
            receipt = await adapter.ledger.receipt(key)
            async with adapter.ledger.db.execute(
                "SELECT state FROM reply_recalls WHERE key=?", (key,)
            ) as cursor:
                job = await cursor.fetchone()
            if not job or job[0] != "pending":
                continue
            valid = (
                adapter.config.get("enable", True)
                and group in adapter.config.get("enabled_groups", [])
                and account == adapter.account
                and team == adapter.groups.get(group)
                and peer == str(adapter.nim_account)
                and client
                == str(uuid.uuid5(uuid.NAMESPACE_URL, f"{adapter.config['id']}/{key}"))
                and mid.isascii()
                and mid.isdigit()
                and int(mid) > 0
                and stamp.isascii()
                and stamp.isdigit()
                and int(stamp) > 0
                and time.time() - deadline <= 300
                and receipt == {"status": "accepted", "server_id": mid}
            )
            if valid:
                # Commit the attempt before I/O; restart must not resend it.
                await adapter.ledger.db.execute(
                    "UPDATE reply_recalls SET state='sending' WHERE key=? AND state='pending'",
                    (key,),
                )
                await adapter.ledger.db.commit()
                try:
                    code, _ = await adapter.nim.request(
                        7,
                        13,
                        wire.properties(
                            [
                                (0, stamp.encode()),
                                (1, b"8"),
                                (2, team.encode()),
                                (3, peer.encode()),
                                (10, client.encode()),
                                (11, mid.encode()),
                                (16, peer.encode()),
                            ]
                        ),
                    )
                    state = "accepted" if code == 200 else "rejected"
                except BaseException as exc:
                    state = "unknown"
                    await adapter.ledger.db.execute(
                        "UPDATE reply_recalls SET state=? WHERE key=?",
                        (state, key),
                    )
                    await adapter.ledger.db.commit()
                    if not isinstance(exc, Exception):
                        adapter.diagnostics.emit(
                            "reply_recall", state, key, failed=True
                        )
                        raise
            await adapter.ledger.db.execute(
                "UPDATE reply_recalls SET state=? WHERE key=?", (state, key)
            )
            await adapter.ledger.db.commit()
            adapter.diagnostics.emit(
                "reply_recall", state, key, failed=state != "accepted"
            )


async def run(adapter) -> None:
    """Maintain one bounded recall worker across native reconnects.

    Args:
        adapter: Native adapter with an open durable message ledger.
    """
    while not adapter.stopping.is_set():
        try:
            await recall_due(adapter)
        except asyncio.CancelledError:
            raise
        except Exception:
            adapter.diagnostics.emit("reply_recall", "worker_failed", failed=True)
        try:
            await asyncio.wait_for(adapter.stopping.wait(), 0.5)
        except TimeoutError:
            pass
