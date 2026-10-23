"""Validated native membership metadata and narrowly scoped working-data cleanup."""

import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def self_membership_notice(outer, peer):
    """Decode only upstream type-5 metadata affecting this exact robot.

    Args:
        outer: Envelope delivered by the authenticated NIM transport.
        peer: This connection's authenticated NIM account, not a message UID.

    Returns:
        Minimal notice metadata, or None for text, malformed or unrelated events.
    """
    if outer.get(0) != "1" or outer.get(8) != "5":
        return None
    encoded = outer.get(10)
    if not isinstance(encoded, str):
        return None
    for value in (outer.get(1), outer.get(2), outer.get(12), peer):
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdigit()
            or not 0 < len(value) <= 20
        ):
            return None
    try:
        if not 0 < len(encoded.encode()) <= 16384:
            return None
        value = json.loads(encoded)
        if not isinstance(value, dict) or type(value.get("id")) is not int:
            return None
        kind = {
            0: "join",
            5: "join",
            9: "join",
            1: "kick",
            2: "leave",
            7: "role_gained",
            8: "role_lost",
            401: "join",
            410: "join",
            411: "join",
            402: "kick",
            403: "leave",
            407: "role_gained",
            408: "role_lost",
        }.get(value["id"])
        if not kind:
            return None
        data = value.get("data")
        if isinstance(data, str) and len(data.encode()) <= 16384:
            data = json.loads(data)
        if not isinstance(data, dict):
            return None
        ids = data.get("ids", [])
        if (
            not isinstance(ids, list)
            or len(ids) > 200
            or any(
                type(item) not in (str, int)
                or not str(item).isascii()
                or not str(item).isdigit()
                or len(str(item)) > 20
                for item in ids
            )
        ):
            return None
        affected = (
            peer == outer[2]
            if kind == "leave"
            else (
                peer in {str(item) for item in ids}
                or kind == "join"
                and str(data.get("id", "")) == peer
            )
        )
        if not affected:
            return None
        stamp = float(outer.get(7, ""))
        if stamp > 100000000000:
            stamp /= 1000
        if not math.isfinite(stamp) or not 0 < stamp <= time.time() + 2:
            return None
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None
    return {"team": outer[1], "message": outer[12], "kind": kind, "event_at": stamp}


def clear_working_data(root, account, group, team, guard):
    """Clear one departed robot/group while retaining finance and safety receipts.

    Args:
        root: Already resolved account-owned native data directory.
        account: Authenticated robot business identity.
        group: Exact business group whose departure was confirmed.
        team: Exact NIM team identity previously bound to that group.
        guard: Durable cleanup-generation check at admission and before every commit.

    Returns:
        Scoped row counts, without message contents or authentication material.

    Raises:
        ValueError: If routing identities or stored job shapes are invalid.
    """
    if any(
        not isinstance(v, str) or not v.isascii() or not v.isdigit()
        for v in (account, group, team)
    ):
        raise ValueError("cleanup_identity")
    root = Path(root)
    counts = {}
    # Financial invite tables and the central cashback database are excluded.
    for filename in (
        "messages.sqlite3",
        "moderation.sqlite3",
        "schedules.sqlite3",
        "cards.sqlite3",
        "activities.sqlite3",
    ):
        path = root / filename
        if not path.is_file() or path.is_symlink():
            continue
        guard()
        with closing(sqlite3.connect(path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            try:
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                deleted = 0
                if filename == "messages.sqlite3":
                    if "inbox" in tables:
                        deleted += db.execute(
                            "UPDATE inbox SET payload='{}',sender='',state='departed',"
                            "completed_at=COALESCE(completed_at,?),pruned_at=? "
                            "WHERE account=? AND team IN (?,?)",
                            (
                                time.time(),
                                time.time(),
                                account,
                                group,
                                "unsupported/1/" + team,
                            ),
                        ).rowcount
                    if "reply_recalls" in tables:
                        db.execute(
                            "UPDATE reply_recalls SET state='cancelled' WHERE account=? AND group_id=? AND state='pending'",
                            (account, group),
                        )
                    if "review_holds" in tables:
                        db.execute(
                            "DELETE FROM review_holds WHERE account=? AND team IN (?,?)",
                            (account, group, "unsupported/1/" + team),
                        )
                elif filename == "moderation.sqlite3":
                    for table in ("content_violations", "semantic_reviews"):
                        if table in tables:
                            deleted += db.execute(
                                f"DELETE FROM {table} WHERE account=? AND group_id=?",
                                (account, group),
                            ).rowcount
                    if "progressive_warnings" in tables:
                        db.execute(
                            "UPDATE progressive_warnings SET status='archived' WHERE account=? AND group_id=?",
                            (account, group),
                        )
                    if "automatic_mutes" in tables:
                        db.execute(
                            "UPDATE automatic_mutes SET closed=1 WHERE account=? AND group_id=?",
                            (account, group),
                        )
                    if "flood_recalls" in tables:
                        db.execute(
                            "UPDATE flood_recalls SET status='cancelled' WHERE account=? AND group_id=? AND status='pending'",
                            (account, group),
                        )
                    # Unknown and accepted action receipts keep their exact identities.
                elif filename == "schedules.sqlite3":
                    for table in ("schedules", "confirmations"):
                        if table in tables:
                            deleted += db.execute(
                                f"DELETE FROM {table} WHERE account=? AND group_id=?",
                                (account, group),
                            ).rowcount
                elif filename == "cards.sqlite3":
                    if "card_jobs" in tables:
                        for row in db.execute(
                            "SELECT id,data FROM card_jobs"
                        ).fetchall():
                            job = json.loads(row["data"])
                            if not isinstance(job, dict):
                                raise ValueError("cleanup_job_shape")
                            if (
                                str(job.get("account")) == account
                                and str(job.get("group")) == group
                            ):
                                minimal = {
                                    k: job[k]
                                    for k in (
                                        "id",
                                        "account",
                                        "group",
                                        "kind",
                                        "owner",
                                        "created",
                                        "expires",
                                    )
                                    if k in job
                                }
                                minimal.update(
                                    state="stopped",
                                    items=[
                                        {
                                            k: item[k]
                                            for k in (
                                                "member",
                                                "nim",
                                                "state",
                                                "operation",
                                            )
                                            if k in item
                                        }
                                        for item in job.get("items", [])
                                        if item.get("state")
                                        in {"unknown", "accepted", "verified"}
                                    ],
                                )
                                db.execute(
                                    "UPDATE card_jobs SET data=? WHERE id=?",
                                    (json.dumps(minimal), row["id"]),
                                )
                                deleted += 1
                    for table in ("card_names", "card_rules", "card_seen"):
                        if table not in tables:
                            continue
                        for row in db.execute(
                            f"SELECT rowid,identity FROM {table}"
                        ).fetchall():
                            try:
                                identity = json.loads(row["identity"])
                            except (ValueError, TypeError):
                                identity = row["identity"].split("/")
                            if (
                                isinstance(identity, list)
                                and len(identity) >= 2
                                and [str(v) for v in identity[:2]] == [account, group]
                            ):
                                deleted += db.execute(
                                    f"DELETE FROM {table} WHERE rowid=?",
                                    (row["rowid"],),
                                ).rowcount
                elif filename == "activities.sqlite3":
                    if "lotteries" in tables:
                        lottery_columns = {
                            field["name"]
                            for field in db.execute("PRAGMA table_info(lotteries)")
                        }
                        if "lottery_entries" in tables:
                            deleted += db.execute(
                                "DELETE FROM lottery_entries WHERE lottery IN (SELECT id FROM lotteries WHERE account=? AND group_id=?)",
                                (account, group),
                            ).rowcount
                        deleted += db.execute(
                            "UPDATE lotteries SET status='cancelled',prize='',contact='',owner_name='',"
                            "results='[]',error='robot_departed'"
                            + (",remarks=''" if "remarks" in lottery_columns else "")
                            + " WHERE account=? AND group_id=?",
                            (account, group),
                        ).rowcount
                    if "lottery_settings" in tables:
                        deleted += db.execute(
                            "DELETE FROM lottery_settings WHERE account=? AND group_id=?",
                            (account, group),
                        ).rowcount
                guard()
                db.commit()
                counts[filename] = deleted
            except BaseException:
                db.rollback()
                raise
    return counts


def clear_shared_group_data(data_root, account, group, guard, *, platforms=None):
    """Clear exact group routes in shared non-financial stores.

    Args:
        data_root: AstrBot's explicit application data root.
        account: Verified departed robot business account.
        group: Exact business group whose cleanup generation is locked.
        guard: Durable departure-generation check repeated before every commit.
        platforms: Trusted framework platform configurations, including disabled
            instances. Defaults to the framework's current configuration.

    Returns:
        Row counts per shared non-financial store.
    """
    if any(
        not isinstance(value, str) or not value.isascii() or not value.isdigit()
        for value in (account, group)
    ):
        raise ValueError("cleanup_identity")
    if platforms is None:
        from astrbot.core import astrbot_config

        platforms = astrbot_config.get("platform", [])
    data_root = Path(data_root)
    counts = {}
    origins = {
        f"{platform['id']}:GroupMessage:{account}/{group}"
        for platform in platforms
        if isinstance(platform, dict)
        and platform.get("type") == "wangshangliao"
        and isinstance(platform.get("id"), str)
        and platform["id"]
    }
    origin_prefixes = tuple(origin + "/" for origin in origins)
    for relative in (
        "plugin_data/wangshangliao_moderation/settings.sqlite3",
        "plugin_data/wangshangliao_moderation/group_management.sqlite3",
        "data_v4.db",
    ):
        path = data_root / relative
        if not path.is_file() or path.is_symlink():
            continue
        guard()
        with closing(sqlite3.connect(path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = 0
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if path.name == "settings.sqlite3":
                    for row in db.execute(
                        "SELECT * FROM policies WHERE account=?", (account,)
                    ).fetchall():
                        values = json.loads(row["payload"])
                        before = json.dumps(values, sort_keys=True)
                        values["enabled_groups"] = [
                            g for g in values.get("enabled_groups", []) if g != group
                        ]
                        values.get("reply_groups", {}).pop(group, None)
                        values.get("ai_routes", {}).get("groups", {}).pop(group, None)
                        proactive = values.get("proactive_send", {})
                        if "targets" in proactive:
                            proactive["targets"] = [
                                g for g in proactive["targets"] if g != group
                            ]
                        draft = values.get("developer_test", {})
                        if "groups" in draft:
                            draft["groups"] = [g for g in draft["groups"] if g != group]
                        for field in (
                            "permissions",
                            "auto_kick",
                            "card_auto",
                            "card_suffix",
                        ):
                            values.get("moderation", {}).get(field, {}).pop(group, None)
                        after = json.dumps(values, sort_keys=True)
                        if before != after:
                            now = time.time()
                            changed += db.execute(
                                "UPDATE policies SET version=version+1,payload=?,effective_at=? "
                                "WHERE instance=? AND account=? AND version=?",
                                (after, now, row["instance"], account, row["version"]),
                            ).rowcount
                            if "policy_history" in tables:
                                db.execute(
                                    "INSERT INTO policy_history(instance,account,version,effective_at,actor,before_json,after_json) "
                                    "VALUES(?,?,?,?,?,?,?)",
                                    (
                                        row["instance"],
                                        account,
                                        row["version"] + 1,
                                        now,
                                        "native/departure",
                                        "{}",
                                        after,
                                    ),
                                )
                elif path.name == "group_management.sqlite3":
                    if "grants" in tables:
                        changed += db.execute(
                            "DELETE FROM grants WHERE account=? AND group_id=?",
                            (account, group),
                        ).rowcount
                    if "audit" in tables:
                        db.execute(
                            "UPDATE audit SET before_json='[]',after_json='[]' WHERE account=? AND group_id=?",
                            (account, group),
                        )
                else:
                    for table, column in (
                        ("platform_message_history", "user_id"),
                        ("conversations", "user_id"),
                        ("platform_sessions", "session_id"),
                        ("session_project_relations", "session_id"),
                    ):
                        if table not in tables:
                            continue
                        for row in db.execute(
                            f"SELECT rowid AS cleanup_rowid,{column} FROM {table} WHERE {column} LIKE ?",
                            (f"%:GroupMessage:{account}/{group}%",),
                        ).fetchall():
                            if isinstance(row[column], str) and (
                                row[column] in origins
                                or row[column].startswith(origin_prefixes)
                            ):
                                changed += db.execute(
                                    f"DELETE FROM {table} WHERE rowid=?",
                                    (row["cleanup_rowid"],),
                                ).rowcount
                    if "cron_jobs" in tables:
                        for row in db.execute(
                            "SELECT id,payload FROM cron_jobs WHERE enabled=1"
                        ).fetchall():
                            payload = json.loads(row["payload"])
                            if isinstance(payload, dict) and any(
                                isinstance(payload.get(k), str)
                                and (
                                    payload[k] in origins
                                    or payload[k].startswith(origin_prefixes)
                                )
                                for k in (
                                    "session_id",
                                    "session",
                                    "umo",
                                    "unified_msg_origin",
                                )
                            ):
                                changed += db.execute(
                                    "UPDATE cron_jobs SET enabled=0,status='paused',last_error='robot_departed' WHERE id=?",
                                    (row["id"],),
                                ).rowcount
                guard()
                db.commit()
                counts[relative] = changed
            except BaseException:
                db.rollback()
                raise
    return counts
