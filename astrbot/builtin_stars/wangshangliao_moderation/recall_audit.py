"""Read scoped native recall and warning receipts without executing actions."""

import math


async def read_native_audit(db, account, group, rules, peers, own_peer):
    """Correlate bounded native evidence; notification authors are NIM identities.

    Args:
        db: Read-only native message database with an authorization guard.
        account: Frozen business bot account.
        group: Frozen business group.
        rules: Recent content-rule rows to enrich with separate send receipts.
        peers: Unambiguous NIM-to-business labels from this group's directory.
        own_peer: Current authenticated bot NIM identity, if available.

    Returns:
        At most fifty deduplicated observations, or None if notices are unavailable.
    """
    tables = {
        row[0]
        for row in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for row in rules:
        mid = str(row["message"])
        key = f"recall/{account}/{group}/{mid}"
        if "outbox" in tables:
            receipt = await db.fetchone("SELECT state FROM outbox WHERE key=?", (key,))
            if receipt:
                row["recall_status"] = receipt[0]
            for warning_key in (
                f"open-warning/{account}/{group}/{mid}",
                f"content-warning/{group}/{mid}",
            ):
                if warning_key.startswith("content-warning/"):
                    scoped = (
                        await db.fetchone(
                            "SELECT 1 FROM inbox WHERE account=? AND team=? "
                            "AND message=? AND sender=?",
                            (account, group, mid, str(row["sender"])),
                        )
                        if "inbox" in tables
                        else None
                    )
                    if not scoped:
                        continue
                receipt = await db.fetchone(
                    "SELECT state FROM outbox WHERE key=?", (warning_key,)
                )
                if receipt:
                    row["warning_status"] = (
                        "sent" if receipt[0] in {"accepted", "verified"} else receipt[0]
                    )
                    if "reply_recalls" in tables:
                        cleanup = await db.fetchone(
                            "SELECT state FROM reply_recalls "
                            "WHERE key=? AND account=? AND group_id=?",
                            (warning_key, account, group),
                        )
                        if cleanup:
                            row["warning_cleanup_status"] = cleanup[0]
                    break
        row.setdefault(
            "warning_status",
            "not_applicable"
            if row["category"]
            in {
                "message_flood",
                "prohibited_image",
                "prohibited_video",
                "prohibited_forward",
            }
            else "not_recorded",
        )

    if "observed_recalls" not in tables:
        return None
    observations = await db.fetchall(
        "SELECT message_id,client,author,operator,recalled_at,received_at "
        "FROM observed_recalls WHERE account=? AND group_id=? "
        "ORDER BY received_at DESC LIMIT 200",
        (account, group),
    )
    jobs_by_message = {}
    if observations and "reply_recalls" in tables:
        mids = {row[0] for row in observations}
        jobs = await db.fetchall(
            "SELECT key,state,sender,server_id,client,peer FROM reply_recalls "
            "WHERE account=? AND group_id=? AND server_id IN ("
            + ",".join("?" for _ in mids)
            + ")",
            (account, group, *mids),
        )
        for job in jobs:
            if job[1] in {"accepted", "verified"}:
                jobs_by_message.setdefault(tuple(job[3:]), []).append(job[:3])
    seen, recalls = set(), []
    for mid, client, author, operator, recalled_at, received_at in observations:
        identity = (mid, client, author, operator)
        if identity in seen:
            continue
        seen.add(identity)
        row = {
            "message": mid,
            "client": client,
            "author_peer": author,
            "operator_peer": operator,
            "source": "unattributed",
            "received_at": received_at,
            "status": "recall_observed",
        }
        try:
            stamp = float(recalled_at)
            if stamp > 100000000000:
                stamp /= 1000
            if math.isfinite(stamp) and 0 < stamp <= received_at + 60:
                row["recalled_at"] = stamp
        except (ValueError, TypeError):
            pass
        for role, peer in (("author", author), ("operator", operator)):
            if peer in peers:
                row[f"{role}_id"], row[f"{role}_name"] = peers[peer]

        key = f"recall/{account}/{group}/{mid}"
        receipt = (
            await db.fetchone("SELECT state FROM outbox WHERE key=?", (key,))
            if "outbox" in tables
            else None
        )
        # A transport notification alone does not prove this process performed
        # the recall: the same bot account may also be used in the official app.
        accepted_jobs = jobs_by_message.get((mid, client, author), [])
        if operator and operator == own_peer and accepted_jobs:
            row["source"] = (
                "bot_command_cleanup"
                if any(job[2] and job[0] == key for job in accepted_jobs)
                else "bot_reply_cleanup"
            )
            row["receipt_status"] = "accepted"
        elif (
            operator
            and operator == own_peer
            and receipt
            and receipt[0] in {"accepted", "verified"}
        ):
            row["source"] = "bot_rule_recall"
            row["receipt_status"] = receipt[0]
        elif operator and operator == author:
            row["source"] = "self_recall"
        elif operator and own_peer and operator != own_peer:
            row["source"] = "other_member_recall"
        elif operator and operator == own_peer:
            row["source"] = "bot_account_unattributed"
        recalls.append(row)
    return sorted(
        recalls,
        key=lambda row: row.get("recalled_at", row["received_at"]),
        reverse=True,
    )[:50]
