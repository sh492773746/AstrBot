"""Consistent backups and policy export; never restore financial ledgers."""

import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def migrate_cashback_collector(
    store, actor, group, source_bot, source_account, version, roster
) -> dict:
    """Transfer one ledger to a verified replacement collector, not a new ledger.

    Args:
        store: Existing CashbackStore, backed up before this maintenance call.
        actor: Existing active administrator principal.
        group: Business group ID to transfer.
        source_bot: Expected retired collector instance.
        source_account: Expected retired business account.
        version: Expected current group settings version.
        roster: Fresh complete roster with target bot_id, account, group,
            observed timestamp and normalized members from the target account.

    Returns:
        New binding and baseline count; no credentials or member details.

    Raises:
        CashbackError: If identities, roster, permissions or version conflict.
    """
    from .cashback_store import CashbackError

    now = time.time()
    target_bot, target_account = roster.get("bot_id"), roster.get("account")
    observed, members = roster.get("observed"), roster.get("members")
    if (
        roster.get("group") != group
        or roster.get("complete") is not True
        or not isinstance(target_bot, str)
        or not target_bot
        or not isinstance(target_account, str)
        or not target_account.isascii()
        or not target_account.isdigit()
        or int(target_account) <= 0
        or (target_bot == source_bot or target_account == source_account)
        or type(observed) not in {int, float}
        or not math.isfinite(observed)
        or not 0 <= now - observed <= 300
        or not isinstance(members, dict)
        or not members
        or target_account not in members
    ):
        raise CashbackError(
            "migration_roster_invalid", "新机器人身份或完整群名单尚未核验。", 409
        )
    peers = set()
    for uid, member in members.items():
        peer = member.get("peer") if isinstance(member, dict) else None
        if (
            not isinstance(uid, str)
            or not uid.isascii()
            or not uid.isdigit()
            or int(uid) <= 0
            or not isinstance(peer, str)
            or not peer.strip()
            or len(peer.encode()) > 1024
            or peer in peers
            or type(member.get("eligible")) is not bool
        ):
            raise CashbackError(
                "migration_roster_invalid", "新机器人群名单身份不完整。", 409
            )
        peers.add(peer)
    with store.connect(True) as db:
        rule, _ = store._scope(db, actor, group, True)
        if (
            not rule
            or type(version) is not int
            or rule["version"] != version
            or rule["bot_id"] != source_bot
            or rule["account"] != source_account
        ):
            raise CashbackError(
                "migration_conflict", "群绑定已变化，本次迁移未修改数据。", 409
            )
        for row in db.execute(
            "SELECT member,peer FROM entries WHERE group_id=?", (group,)
        ):
            if (
                row["member"] in members
                and row["peer"]
                and members[row["member"]]["peer"] != row["peer"]
            ):
                raise CashbackError(
                    "migration_identity_conflict",
                    "历史成员身份不一致，迁移已停止。",
                    409,
                )
        # Never extend an earlier migration's cutoff during a subsequent move.
        ancestors = db.execute(
            "SELECT source_bot,source_account,cutoff FROM collector_migrations "
            "WHERE group_id=? AND target_bot=? AND target_account=?",
            (group, source_bot, source_account),
        ).fetchall()
        for old_bot, old_account, cutoff in [
            (source_bot, source_account, now),
            *((r["source_bot"], r["source_account"], r["cutoff"]) for r in ancestors),
        ]:
            db.execute(
                "INSERT INTO collector_migrations VALUES(?,?,?,?,?,?,?)",
                (
                    group,
                    old_bot,
                    old_account,
                    target_bot,
                    target_account,
                    cutoff,
                    actor,
                ),
            )
        db.execute(
            "UPDATE groups SET bot_id=?,account=?,version=version+1,scanned=0,"
            "error='migration_verification_required',source_cursor='',source_scanned=0,"
            "source_error='',source_retry_at=0,source_failures=0 WHERE id=?",
            (target_bot, target_account, group),
        )
        db.execute(
            "INSERT INTO reward_rule_history"
            "(group_id,version,effective_at,enabled,rate,bot_id,account,"
            "agent_rate,qualification_seconds) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                group,
                version + 1,
                now,
                rule["enabled"],
                rule["rate"],
                target_bot,
                target_account,
                rule["agent_rate"],
                rule["qualification_seconds"],
            ),
        )
        added = 0
        for uid, member in members.items():
            before = db.total_changes
            store._insert(db, group, uid, member, now, 0, False)
            added += db.total_changes - before
        result = {
            "bot_id": target_bot,
            "account": target_account,
            "version": version + 1,
            "baseline_added": added,
            "historical_entries_unchanged": True,
            "historical_evidence_unchanged": True,
        }
        store._audit(
            db,
            group,
            None,
            actor,
            "collector_migrated",
            {
                "bot_id": source_bot,
                "account": source_account,
                "version": version,
            },
            result,
            "Explicit administrator maintenance transfer after collector replacement.",
        )
        return result


def recover_cashback_migration_entries(
    store,
    actor,
    group,
    source_bot,
    source_account,
    version,
    roster,
    items,
    *,
    dry_run=False,
) -> dict:
    """Recover explicitly selected migration baselines with approved invite proof.

    Args:
        store: Existing CashbackStore, backed up before a maintenance write.
        actor: Active local administrator principal.
        group: Exact business group ID.
        source_bot: Retired collector instance from the recorded migration.
        source_account: Retired collector account from the recorded migration.
        version: Expected current group settings version.
        roster: Fresh complete replacement collector roster, as for migration.
        items: Exact baseline entry IDs and expected entry versions.
        dry_run: Validate and preview without changing entries or evidence.

    Returns:
        Selected count, ordinary reward total and new qualification deadline.

    Raises:
        CashbackError: If scope, identity, history or source proof is unsafe.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from .cashback_store import CashbackError, label, member_pricing

    now = time.time()
    members, observed = roster.get("members"), roster.get("observed")
    target_bot, target_account = roster.get("bot_id"), roster.get("account")
    if (
        roster.get("group") != group
        or roster.get("complete") is not True
        or type(observed) not in {int, float}
        or not math.isfinite(observed)
        or not 0 <= now - observed <= 60
        or not isinstance(members, dict)
        or not members
        or target_account not in members
        or type(dry_run) is not bool
        or not isinstance(items, list)
        or not 1 <= len(items) <= 100
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or type(item.get("version")) is not int
            or item["version"] < 1
            for item in items
        )
        or len({item["id"] for item in items}) != len(items)
    ):
        raise CashbackError(
            "recovery_selection_invalid", "完整名单或补账记录尚未核验。", 409
        )
    peers = set()
    for uid, member in members.items():
        peer = member.get("peer") if isinstance(member, dict) else None
        if (
            not isinstance(uid, str)
            or not uid.isascii()
            or not uid.isdigit()
            or int(uid) <= 0
            or not isinstance(peer, str)
            or not peer.strip()
            or len(peer.encode()) > 1024
            or peer in peers
            or type(member.get("eligible")) is not bool
        ):
            raise CashbackError(
                "recovery_roster_invalid", "群成员身份不完整，未补账。", 409
            )
        peers.add(peer)
    with store.connect(True) as db:
        rule, _ = store._scope(db, actor, group, True)
        migration = db.execute(
            "SELECT cutoff FROM collector_migrations WHERE group_id=? "
            "AND source_bot=? AND source_account=? AND target_bot=? AND target_account=?",
            (group, source_bot, source_account, target_bot, target_account),
        ).fetchone()
        if (
            not rule
            or type(version) is not int
            or rule["version"] != version
            or rule["bot_id"] != target_bot
            or rule["account"] != target_account
            or not rule["enabled"]
            or rule["error"]
            or rule["source_error"]
            or not 0 <= now - rule["scanned"] <= 60
            or not 0 <= now - rule["source_scanned"] <= 120
            or not migration
        ):
            raise CashbackError(
                "recovery_scope_changed", "采集范围或核验状态已变化，未补账。", 409
            )
        cutoff = migration["cutoff"]
        records = {}
        for cached in db.execute(
            "SELECT payload FROM invitation_source_records WHERE group_id=? "
            "AND bot_id=? AND account=? AND started=?",
            (group, target_bot, target_account, rule["started"]),
        ):
            try:
                record = json.loads(cached["payload"])
                if not isinstance(record, dict):
                    raise ValueError
                records.setdefault(record["member_id"], []).append(record)
            except (TypeError, ValueError, KeyError) as exc:
                raise CashbackError(
                    "recovery_source_invalid", "邀请来源缓存无法核验，未补账。", 409
                ) from exc
        selected = []
        for item in items:
            row = db.execute(
                "SELECT * FROM entries WHERE group_id=? AND id=?",
                (group, item["id"]),
            ).fetchone()
            if (
                not row
                or row["version"] != item["version"]
                or row["first_seen"] != cutoff
                or row["eligible"]
                or row["state"] != "void"
                or row["verification"] != "pending"
                or row["amount"] != 0
                or row["inviter"]
                or not row["present"]
                or row["ever_left"]
                or row["left_at"] is not None
                or row["paid_at"] is not None
                or row["paid_by"] is not None
                or row["self_join"]
                or row["rule_version"] != 0
                or row["member_class"] != "legacy"
                or db.execute(
                    "SELECT 1 FROM audit WHERE entry_id=? LIMIT 1", (row["id"],)
                ).fetchone()
            ):
                raise CashbackError(
                    "recovery_entry_changed",
                    "记录不是未经处理的迁移基线，未补账。",
                    409,
                )
            candidates = records.get(row["member"], [])
            if len(candidates) != 1:
                raise CashbackError(
                    "recovery_source_ambiguous",
                    "邀请来源缺失或存在重复入群，未补账。",
                    409,
                )
            proof = candidates[0]
            uid, inviter = row["member"], proof.get("inviter_id")
            member, recruiter = members.get(uid), members.get(inviter)
            try:
                text = proof["apply_time"]
                stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
                stamp = (
                    stamp
                    if stamp.tzinfo
                    else stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                ).timestamp()
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                raise CashbackError(
                    "recovery_source_invalid", "邀请来源时间无法核验，未补账。", 409
                ) from exc
            if (
                proof.get("state") != "MEMBER_STATE_GOOD"
                or not isinstance(proof.get("apply_id"), str)
                or not proof["apply_id"].isascii()
                or not proof["apply_id"].isdigit()
                or not 0 < len(proof["apply_id"]) <= 20
                or int(proof["apply_id"]) <= 0
                or not rule["started"] <= stamp <= cutoff
                or uid == inviter
                or not member
                or not recruiter
                or not member["eligible"]
                or not recruiter["eligible"]
                or uid == target_account
                or inviter == target_account
                or row["peer"] != member["peer"]
                or proof.get("member_nim_id") != member["peer"]
                or proof.get("inviter_nim_id") != recruiter["peer"]
                or db.execute(
                    "SELECT 1 FROM evidence WHERE group_id=? AND member=? LIMIT 1",
                    (group, uid),
                ).fetchone()
            ):
                raise CashbackError(
                    "recovery_identity_conflict",
                    "邀请双方身份或历史存在冲突，未补账。",
                    409,
                )
            policy = store._entry_price_policy(db, rule, row, stamp)
            policy = (
                db.execute(
                    "SELECT * FROM reward_rule_history WHERE group_id=? AND version=?",
                    (group, policy["version"]),
                ).fetchone()
                if policy
                else None
            )
            if not policy or not store._valid_price_policy(
                db, policy, rule, row["first_seen"]
            ):
                raise CashbackError(
                    "recovery_price_unavailable", "原返现价格无法核验，未补账。", 409
                )
            category, _, avatar_status, amount, pricing = member_pricing(member, policy)
            selected.append(
                (
                    dict(row),
                    proof,
                    stamp,
                    policy,
                    category,
                    avatar_status,
                    amount,
                    pricing,
                )
            )
        # An application timestamp is not the actual arrival or proof of an
        # uninterrupted stay during the outage. Start a fresh qualification.
        hold_until = now + max(10800, rule["qualification_seconds"])
        result = {
            "count": len(selected),
            "amount": sum(item[6] for item in selected),
            "hold_until": hold_until,
            "dry_run": dry_run,
        }
        if dry_run:
            return result
        for (
            row,
            proof,
            stamp,
            policy,
            category,
            avatar_status,
            amount,
            pricing,
        ) in selected:
            inviter = proof["inviter_id"]
            db.execute(
                "INSERT INTO evidence(group_id,member,source,event_id,inviter,"
                "observed,account,member_peer,inviter_peer,event_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    group,
                    row["member"],
                    "business_apply_log",
                    proof["apply_id"],
                    inviter,
                    now,
                    target_account,
                    proof["member_nim_id"],
                    proof["inviter_nim_id"],
                    stamp,
                ),
            )
            db.execute(
                "UPDATE entries SET eligible=1,state='verifying',verification='verified',"
                "inviter=?,inviter_name=?,inviter_peer=?,amount=?,hold_until=?,"
                "last_seen=?,rule_version=?,member_class=?,inviter_class='legacy',"
                "inviter_name_snapshot=?,name_digit_count=0,pricing_status=?,"
                "member_avatar_status=?,member_avatar_digit_count=0,version=version+1 "
                "WHERE id=?",
                (
                    inviter,
                    label(members[inviter]["name"]),
                    members[inviter]["peer"],
                    amount,
                    hold_until,
                    now,
                    policy["version"],
                    category,
                    label(members[inviter]["name"]),
                    pricing,
                    avatar_status,
                    row["id"],
                ),
            )
            store._audit(
                db,
                group,
                row["id"],
                actor,
                "migration_invitation_recovered",
                row,
                {
                    "eligible": True,
                    "state": "verifying",
                    "verification": "verified",
                    "inviter": inviter,
                    "amount": amount,
                    "hold_until": hold_until,
                    "rule_version": policy["version"],
                    "source": "business_apply_log",
                    "event_id": proof["apply_id"],
                    "source_bot": source_bot,
                    "source_account": source_account,
                    "migration_cutoff": cutoff,
                },
                "Explicit administrator recovery of a migration baseline using approved "
                "invitation proof; arrival time is not inferred from application time.",
            )
        return result


def detach_unverified_paid_inviters(store, actor, group, version, items) -> int:
    """Remove unproved recruiter claims while retaining payment registrations.

    Args:
        store: Backed-up CashbackStore.
        actor: Active local administrator principal.
        group: Exact business group ID.
        version: Expected group settings version.
        items: Exact conflicted paid entry IDs and expected entry versions.

    Returns:
        Number of records whose unproved inviter claim was removed.

    Raises:
        CashbackError: If authorization, versions or payment evidence conflict.
    """
    from .cashback_store import CashbackError

    if (
        not isinstance(items, list)
        or not 1 <= len(items) <= 100
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or type(item.get("version")) is not int
            for item in items
        )
        or len({item["id"] for item in items}) != len(items)
    ):
        raise CashbackError("invalid_selection", "请选择确切的历史记录。", 409)
    with store.connect(True) as db:
        rule, _ = store._scope(db, actor, group, True)
        if rule["version"] != version:
            raise CashbackError("conflict", "群设置已变化，未修改历史记录。", 409)
        rows = []
        for item in items:
            row = db.execute(
                "SELECT * FROM entries WHERE group_id=? AND id=?",
                (group, item["id"]),
            ).fetchone()
            if (
                not row
                or row["version"] != item["version"]
                or row["state"] != "paid"
                or row["verification"] != "conflict"
                or not row["inviter"]
                or not row["paid_at"]
                or not row["paid_by"]
                or db.execute(
                    "SELECT 1 FROM current_invitation_evidence WHERE group_id=? "
                    "AND member=? AND inviter=? AND source='business_apply_log' "
                    "AND member_peer=? AND inviter_peer=? AND event_at>0 "
                    "AND event_at<=? LIMIT 1",
                    (
                        group,
                        row["member"],
                        row["inviter"],
                        row["peer"],
                        row["inviter_peer"],
                        row["paid_at"],
                    ),
                ).fetchone()
            ):
                raise CashbackError(
                    "paid_attribution_changed",
                    "历史记录已变化或存在可信邀请证据，未清除归属。",
                    409,
                )
            rows.append(dict(row))
        for row in rows:
            db.execute(
                "UPDATE entries SET inviter='',inviter_name='',inviter_peer='',"
                "inviter_name_snapshot='',version=version+1 WHERE id=?",
                (row["id"],),
            )
            store._audit(
                db,
                group,
                row["id"],
                actor,
                "unverified_paid_inviter_detached",
                row,
                {
                    "inviter": "",
                    "verification": "conflict",
                    "state": "paid",
                    "payment_unchanged": True,
                },
                "Unproved join-operator attribution is not an invitation. Original "
                "payment registration and historical recipient remain in the audit.",
            )
        return len(rows)


def backup(data: Path, destination: Path) -> dict:
    """Back up core config, encrypted sessions and Wangshangliao databases.

    Args:
        data: Existing AstrBot data directory.
        destination: New private backup directory.

    Returns:
        File manifest with hashes; no credentials are returned.
    """
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    paths = [data / "cmd_config.json"]
    for root in (
        data / "platform_data" / "wangshangliao",
        data / "plugin_data" / "wangshangliao_cashback",
        data / "plugin_data" / "wangshangliao_moderation",
        data / "plugin_data" / "wangshangliao_open",
    ):
        if root.exists():
            paths.extend(
                p
                for p in root.rglob("*")
                if p.is_file()
                and (p.suffix in {".sqlite3", ".key", ".sealed", ".json"})
                and "backups" not in p.relative_to(root).parts
            )
    manifest = {"files": []}
    for source in paths:
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(data)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.suffix == ".sqlite3":
            with closing(
                sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
            ) as old:
                with closing(sqlite3.connect(target)) as new:
                    old.backup(new)
                    if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("backup_integrity_failed")
        else:
            shutil.copy2(source, target)
        target.chmod(0o600)
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest["files"].append(
            {
                "path": str(relative),
                "sha256": digest.hexdigest(),
            }
        )
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (destination / "manifest.json").chmod(0o600)
    return manifest


def export_config(data: Path, output: Path) -> int:
    """Export latest policy into a separate legacy-compatible config file.

    Args:
        data: AstrBot data directory, read only.
        output: New output file; must not overwrite the live config or a file.

    Returns:
        Number of account-matched policies exported.

    Raises:
        ValueError: On an identity mismatch or invalid policy payload.
    """
    from .settings import validate_settings

    config = json.loads((data / "cmd_config.json").read_text(encoding="utf-8-sig"))
    path = data / "plugin_data" / "wangshangliao_moderation" / "settings.sqlite3"
    count = 0
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for bot in config.get("platform", []):
            if bot.get("type") != "wangshangliao":
                continue
            row = db.execute(
                "SELECT * FROM policies WHERE instance=?", (bot["id"],)
            ).fetchone()
            if row is None:
                raise ValueError("policy_missing")
            if row["account"] != str(bot["account_id"]):
                raise ValueError("business_account_binding_conflict")
            values = json.loads(row["payload"])
            validate_settings(values)
            bot.update(values)
            count += 1
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
    output.chmod(0o600)
    return count
