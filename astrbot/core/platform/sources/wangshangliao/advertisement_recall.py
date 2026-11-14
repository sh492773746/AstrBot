"""Explicit once-only recall of a robot's accepted advertisement batch."""

import asyncio
import uuid

from . import wire
from .database import account_guard
from .group_scope import group_revision


async def recall_advertisement(adapter, group, operation, *, authorize):
    """Recall only retained image/text routes belonging to one accepted ad.

    Args:
        adapter: Original account-bound native adapter.
        group: Exact business group, never a transport team or nickname.
        operation: Previously accepted advertisement's stable operation key.
        authorize: Current business configuration and native-role check.

    Returns:
        Accepted, rejected, unknown or partial, with no automatic retries.

    Raises:
        ProtocolError: If scope, receipt or current authority cannot be verified.
    """
    account = str(adapter.account)
    if (
        not operation.startswith(f"advertisement/{account}/{group}/")
        or len(operation.split("/")) != 5
        or not operation.rsplit("/", 1)[-1].isascii()
        or not operation.rsplit("/", 1)[-1].isdigit()
    ):
        raise wire.ProtocolError("advertisement_recall_scope")
    guard = account_guard(adapter, authorize)
    ledger, nim, team, peer = (
        adapter.ledger,
        adapter.nim,
        adapter.groups.get(group),
        str(adapter.nim_account),
    )
    revision = group_revision(adapter.config, group)
    prefix = operation + "/part/"
    async with ledger.db.execute(
        "SELECT o.key,o.state,o.server_id,r.account,r.group_id,r.team,r.peer,"
        "r.client,r.server_id,r.message_time,r.state,r.sender "
        "FROM outbox o LEFT JOIN reply_recalls r ON r.key=o.key "
        "WHERE o.key=? OR (o.key>=? AND o.key<?) ORDER BY o.key LIMIT 11",
        (operation + "/image", prefix, operation + "/part0"),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows or len(rows) > 10:
        raise wire.ProtocolError("advertisement_recall_routes")
    # Validate the entire batch before recalling any of its messages.
    for key, status, mid, *route in rows:
        suffix = key[len(prefix) :]
        if (
            (
                key != operation + "/image"
                and not (suffix.isascii() and suffix.isdigit())
            )
            or status != "accepted"
            or route[:4] != [account, group, team, peer]
            or route[4]
            != str(uuid.uuid5(uuid.NAMESPACE_URL, f"{adapter.config['id']}/{key}"))
            or route[5] != mid
            or not str(mid).isascii()
            or not str(mid).isdigit()
            or int(mid) <= 0
            or not str(route[6]).isascii()
            or not str(route[6]).isdigit()
            or int(route[6]) <= 0
            or route[8] != ""
        ):
            raise wire.ProtocolError("advertisement_recall_routes")
        if route[7] in {"unknown", "sending"}:
            return "unknown"
        if route[7] not in {"retained", "accepted"}:
            return "rejected"
    accepted = 0
    for index, row in enumerate(rows):
        key, _, mid, _, _, _, _, client, _, stamp, state, _ = row
        if state == "accepted":
            accepted += 1
            continue
        if index:
            await asyncio.sleep(3)
        async with adapter.send_lock:
            guard()
            if (
                adapter.ledger is not ledger
                or adapter.nim is not nim
                or adapter.groups.get(group) != team
                or str(adapter.nim_account) != peer
                or adapter.members.get(group, {}).get(account) != peer
                or group_revision(adapter.config, group) != revision
                or group not in adapter.enabled_groups
                or adapter.connection_state != "online"
                or adapter.stopping.is_set()
            ):
                raise wire.ProtocolError("advertisement_recall_scope")
            async with ledger.db.execute(
                "UPDATE reply_recalls SET state='sending' WHERE key=? AND state='retained' "
                "AND account=? AND group_id=? AND team=? AND peer=? AND client=? "
                "AND server_id=? AND message_time=? AND sender='' AND EXISTS("
                "SELECT 1 FROM outbox WHERE outbox.key=reply_recalls.key "
                "AND outbox.state='accepted' AND outbox.server_id=reply_recalls.server_id)",
                (key, account, group, team, peer, client, mid, stamp),
            ) as cursor:
                claimed = cursor.rowcount == 1
            await ledger.db.commit()
            if not claimed:
                return "partial" if accepted else "unknown"
            submitted = False
            outcome = "rejected"
            try:
                guard()
                if (
                    adapter.ledger is not ledger
                    or adapter.nim is not nim
                    or adapter.groups.get(group) != team
                    or str(adapter.nim_account) != peer
                    or adapter.members.get(group, {}).get(account) != peer
                    or group_revision(adapter.config, group) != revision
                    or adapter.connection_state != "online"
                    or adapter.stopping.is_set()
                ):
                    raise wire.ProtocolError("advertisement_recall_scope")
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
                submitted = True
                code, _ = await nim.request(7, 13, body)
                outcome = "accepted" if code == 200 else "rejected"
            except BaseException as exc:
                outcome = "unknown" if submitted else "rejected"
                if not submitted or not isinstance(
                    exc, (TimeoutError, OSError, wire.ProtocolError)
                ):
                    raise
            finally:
                await ledger.db.execute(
                    "UPDATE reply_recalls SET state=? WHERE key=?", (outcome, key)
                )
                await ledger.db.commit()
                adapter.diagnostics.emit(
                    "advertisement_recall", outcome, key, failed=outcome != "accepted"
                )
            if outcome != "accepted":
                return "partial" if accepted else outcome
            accepted += 1
    return "accepted"
