"""Durable, once-only cleanup of group commands and their feature replies."""

import asyncio
import json
import time
import uuid

from . import wire
from .database import account_guard
from .group_scope import group_revision
from .policy import authorize_action


async def schedule_command(event) -> None:
    """Queue only a classified group command backed by its authenticated inbox.

    Args:
        event: Native event marked as a fixed command or menu input.
    """
    if (
        event.is_private_chat()
        or event.get_extra("wsl_plain_command") is not True
        or event.get_extra("_wsl_command_recall_scheduled") is True
    ):
        return
    adapter = event.platform
    ledger = getattr(adapter, "ledger", None)
    if ledger is None or ledger.db is None:
        return
    account, group, mid, sender = (
        adapter.account,
        event.get_group_id(),
        str(event.message_obj.message_id),
        str(event.get_sender_id()),
    )
    guard = account_guard(adapter)
    revision = group_revision(adapter.config, group)
    async with ledger.db.execute(
        "SELECT payload,received_at FROM inbox WHERE account=? AND team=? AND message=?",
        (account, group, mid),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return
    data = json.loads(row[0])
    route = data.get("recall_route", {})
    actor = getattr(event, "actor_identity", None)
    peer = adapter.members.get(group, {}).get(sender)
    now = time.time()
    if (
        data.get("sender") != sender
        or actor is None
        or (actor.account, actor.peer, actor.verified_uid) != (account, peer, sender)
        or not peer
        or not route.get("client")
        or route.get("peer") != peer
        or not mid.isascii()
        or not mid.isdigit()
        or int(mid) <= 0
        or not str(route.get("time", "")).isascii()
        or not str(route.get("time", "")).isdigit()
        or int(route["time"]) <= 0
        or row[1] is None
        or not 0 <= now - row[1] <= 300
    ):
        return
    try:
        guard()
    except wire.ProtocolError:
        return
    if (
        event.policy_epoch != adapter.config.get("_business_epoch")
        or revision != group_revision(adapter.config, group)
        or group not in adapter.enabled_groups
        or not adapter.groups.get(group)
        or adapter.config.get("_business_active") is False
    ):
        return
    await ledger.db.execute(
        "INSERT OR IGNORE INTO reply_recalls"
        "(key,account,group_id,team,peer,client,server_id,message_time,deadline,sender)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            f"recall/{account}/{group}/{mid}",
            account,
            group,
            adapter.groups[group],
            peer,
            route["client"],
            mid,
            str(route["time"]),
            row[1] + 12,
            sender,
        ),
    )
    await ledger.db.commit()
    event.set_extra("_wsl_command_recall_scheduled", True)
    ledger.recall_wakeup.set()


def _authorize_command(adapter, account, group, sender, peer):
    """Recheck local recall grants without putting HTTP waits in the timer lane.

    Args:
        adapter: Account-bound native adapter.
        account: Original receiving business account.
        group: Original business group.
        sender: Authenticated business sender, not a name or self-reported UID.
        peer: Original transport sender.

    Raises:
        ProtocolError: If identity or native recall permission is missing.
    """
    authorize_action(adapter.config, group, "recall")
    role = adapter.config.get("_open_native_roles", {}).get(group)
    if role is None:
        snapshot = adapter.member_snapshots.get(group)
        directory = (
            snapshot[2]
            if snapshot
            and snapshot[0] == account
            and time.monotonic() - snapshot[1] <= 30
            else {}
        )
        if directory.get("complete"):
            role = next(
                (
                    member.get("groupRole")
                    for member in directory.get("groupMemberInfo", [])
                    if str(member["userId"]) == account
                    and str(member["nimId"]) == str(adapter.nim_account)
                ),
                None,
            )
    if (
        role not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
        or adapter.members.get(group, {}).get(sender) != peer
    ):
        raise wire.ProtocolError("command_recall_permission")


async def _command_evidence(adapter, account, group, sender, mid, client, stamp, peer):
    """Require the exact received command, independent of outgoing reply receipts.

    Args:
        adapter: Account-bound native adapter.
        account: Original receiving business account.
        group: Original business group.
        sender: Authenticated business sender.
        mid: Original server message identifier.
        client: Original native client identifier.
        stamp: Original native timestamp.
        peer: Original transport sender.

    Returns:
        Stored native route used for once-only outbox reservation.

    Raises:
        ProtocolError: If permission or original message evidence is missing.
    """
    _authorize_command(adapter, account, group, sender, peer)
    async with adapter.ledger.db.execute(
        "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
        (account, group, mid),
    ) as cursor:
        row = await cursor.fetchone()
    data = json.loads(row[0]) if row else {}
    route = data.get("recall_route", {})
    if data.get("sender") != sender or (
        route.get("client"),
        str(route.get("time")),
        route.get("peer"),
    ) != (client, stamp, peer):
        raise wire.ProtocolError("command_recall_identity")
    return route


async def recall_due(adapter) -> None:
    """Recall a bounded batch of commands and accepted replies after twelve seconds.

    Args:
        adapter: Account-bound native adapter owning the message ledger.

    Raises:
        CancelledError: If the adapter shuts down during a request.
    """
    if (
        adapter.stopping.is_set()
        or adapter.connection_state != "online"
        or adapter.config.get("_business_active") is False
    ):
        return
    async with adapter.ledger.db.execute(
        "SELECT key,account,group_id,team,peer,client,server_id,message_time,deadline,sender "
        "FROM reply_recalls WHERE state='pending' AND deadline<=? "
        "ORDER BY deadline LIMIT 20",
        (time.time(),),
    ) as cursor:
        rows = await cursor.fetchall()
    for key, account, group, team, peer, client, mid, stamp, deadline, sender in rows:
        guard = account_guard(adapter)
        revision = group_revision(adapter.config, group)
        ledger, nim = adapter.ledger, adapter.nim
        route = None
        if sender:
            try:
                route = await _command_evidence(
                    adapter, account, group, sender, mid, client, stamp, peer
                )
            except Exception:
                # No native write was attempted; this is not an unknown result.
                pass
        async with adapter.send_lock:
            if (
                adapter.stopping.is_set()
                or adapter.connection_state != "online"
                or adapter.config.get("_business_active") is False
            ):
                return
            state = "rejected"
            receipt = await ledger.receipt(key)
            async with ledger.db.execute(
                "SELECT state,account,group_id,team,peer,client,server_id,message_time,deadline,sender "
                "FROM reply_recalls WHERE key=?",
                (key,),
            ) as cursor:
                job = await cursor.fetchone()
            if not job or job[0] != "pending":
                continue
            source_valid = (
                bool(route) and key == f"recall/{account}/{group}/{mid}"
                if sender
                else peer == str(adapter.nim_account)
                and client
                == str(uuid.uuid5(uuid.NAMESPACE_URL, f"{adapter.config['id']}/{key}"))
                and receipt == {"status": "accepted", "server_id": mid}
            )
            valid = (
                adapter.config.get("enable", True)
                and group in adapter.config.get("enabled_groups", [])
                and account == adapter.account
                and team == adapter.groups.get(group)
                and mid.isascii()
                and mid.isdigit()
                and int(mid) > 0
                and stamp.isascii()
                and stamp.isdigit()
                and int(stamp) > 0
                and time.time() - deadline <= 300
                and job[1:]
                == (account, group, team, peer, client, mid, stamp, deadline, sender)
                and source_valid
            )
            try:
                guard()
                if (
                    adapter.ledger is not ledger
                    or adapter.nim is not nim
                    or revision != group_revision(adapter.config, group)
                ):
                    valid = False
                if sender:
                    _authorize_command(adapter, account, group, sender, peer)
            except wire.ProtocolError:
                valid = False
            if valid and sender:
                _, existing = await ledger.reserve(
                    key, json.dumps(route, sort_keys=True)
                )
                if existing != "new":
                    await ledger.db.execute(
                        "UPDATE reply_recalls SET state=? WHERE key=?",
                        ("unknown" if existing == "sending" else existing, key),
                    )
                    await ledger.db.commit()
                    continue
            if valid:
                # Commit the attempt before I/O; restart must not resend it.
                async with ledger.db.execute(
                    "UPDATE reply_recalls SET state='sending' WHERE key=? AND state='pending'",
                    (key,),
                ) as cursor:
                    claimed = cursor.rowcount == 1
                await ledger.db.commit()
                if not claimed:
                    if sender:
                        await ledger.finish(key, "rejected")
                    continue
                try:
                    guard()
                    if sender:
                        _authorize_command(adapter, account, group, sender, peer)
                    current = (
                        adapter.ledger is ledger
                        and adapter.nim is nim
                        and adapter.groups.get(group) == team
                        and revision == group_revision(adapter.config, group)
                        and (
                            adapter.members.get(group, {}).get(sender) == peer
                            if sender
                            else str(adapter.nim_account) == peer
                        )
                    )
                except wire.ProtocolError:
                    current = False
                if (
                    not current
                    or adapter.config.get("_business_active") is False
                    or not adapter.config.get("enable", True)
                    or group not in adapter.enabled_groups
                    or adapter.stopping.is_set()
                ):
                    await ledger.db.execute(
                        "UPDATE reply_recalls SET state='business_suspended' WHERE key=?",
                        (key,),
                    )
                    await ledger.db.commit()
                    if sender:
                        await ledger.finish(key, "rejected")
                    continue
                try:
                    body = wire.properties(
                        [
                            (0, stamp.encode()),
                            (1, b"8"),
                            (2, team.encode()),
                            (3, peer.encode()),
                            (10, client.encode()),
                            (11, mid.encode()),
                            (16, peer.encode()),
                        ]
                    )
                except Exception:
                    if sender:
                        await ledger.finish(key, "rejected")
                else:
                    try:
                        code, _ = await nim.request(7, 13, body)
                        state = "accepted" if code == 200 else "rejected"
                    except BaseException as exc:
                        state = "unknown"
                        if sender:
                            await ledger.finish(key, state)
                        await ledger.db.execute(
                            "UPDATE reply_recalls SET state=? WHERE key=?",
                            (state, key),
                        )
                        await ledger.db.commit()
                        if not isinstance(exc, Exception):
                            adapter.diagnostics.emit(
                                "reply_recall", state, key, failed=True
                            )
                            raise
                    if sender:
                        await ledger.finish(key, state)
            await ledger.db.execute(
                "UPDATE reply_recalls SET state=? WHERE key=?", (state, key)
            )
            await ledger.db.commit()
            adapter.diagnostics.emit(
                "reply_recall", state, key, failed=state != "accepted"
            )


async def run(adapter) -> None:
    """Wait for committed recall jobs without repeatedly scanning an empty queue.

    Args:
        adapter: Native adapter with an open durable message ledger.
    """
    while not adapter.stopping.is_set():
        adapter.ledger.recall_wakeup.clear()
        delay = 0.5
        try:
            if (
                adapter.connection_state == "online"
                and adapter.config.get("_business_active") is not False
            ):
                async with adapter.ledger.db.execute(
                    "SELECT MIN(deadline) FROM reply_recalls WHERE state='pending'"
                ) as cursor:
                    deadline = (await cursor.fetchone())[0]
                delay = 30
                if deadline is not None:
                    remaining = deadline - time.time()
                    delay = min(30, max(0, remaining))
                    if remaining <= 0:
                        await recall_due(adapter)
        except asyncio.CancelledError:
            raise
        except Exception:
            adapter.diagnostics.emit("reply_recall", "worker_failed", failed=True)
            delay = 3
        waiters = (
            asyncio.create_task(adapter.stopping.wait()),
            asyncio.create_task(adapter.ledger.recall_wakeup.wait()),
        )
        try:
            await asyncio.wait(
                waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
