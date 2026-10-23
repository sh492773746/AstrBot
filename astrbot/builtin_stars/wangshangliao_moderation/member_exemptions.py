"""Server-maintained speech exemptions, without granting management capabilities."""

import sqlite3
import time
from contextlib import closing

from astrbot.core.platform.sources.wangshangliao.blacklist import MEMBER_LIST_WRITE_LOCK
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


def is_exempt(instance, account, group, member, peer):
    """Check an exact native sender binding in the local server-maintained list.

    Args:
        instance: Collector's saved platform instance.
        account: Collector's real business account.
        group: Exact business group.
        member: Authenticated business sender UID.
        peer: Authenticated NIM identity or recall route from the native envelope.

    Returns:
        Whether this exact binding is currently exempt from automatic moderation.
    """
    if isinstance(peer, dict):
        peer = peer.get("peer")
    if (
        not isinstance(instance, str)
        or not instance
        or any(
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or not 0 < int(value) < 1 << 63
            for value in (account, group, member, peer)
        )
    ):
        return False
    path = instance_dir(instance) / "member_exemptions.sqlite3"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return bool(
            db.execute(
                "SELECT 1 FROM speech_exemptions WHERE account=? AND group_id=? "
                "AND member=? AND peer=? AND enabled=1",
                (account, group, member, peer),
            ).fetchone()
        )


def set_exemption(
    instance,
    account,
    group,
    member,
    peer,
    *,
    enabled,
    actor,
    reason,
    guard,
    expected_version=None,
    name="",
):
    """Persist a server-authorized exemption and retain its separate audit trail.

    Args:
        instance: Saved collector instance.
        account: Verified collector business UID.
        group: Native-verified target group.
        member: Native-verified member UID.
        peer: Native-verified member NIM identity.
        enabled: Enable or revoke this binding.
        actor: Explicit local server operator, not a chat identity.
        reason: Operator's reason for the exemption.
        guard: Caller-owned verification, checked before and at commit.
        expected_version: Optional scope audit version from a confirmed preview.
        name: Saved display label; an empty label preserves an existing name.

    Raises:
        ValueError: If the exact identity or operator metadata is invalid.
        ProtocolError: If another write invalidated the confirmed version,
            or the member is already blacklisted (member_list_conflict).
    """
    if (
        type(enabled) is not bool
        or not isinstance(actor, str)
        or not actor.startswith("server/")
        or not 0 < len(actor) <= 256
        or not isinstance(reason, str)
        or not 0 < len(reason) <= 512
        or not isinstance(name, str)
        or len(name.encode()) > 1024
        or any(
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or not 0 < int(value) < 1 << 63
            for value in (account, group, member, peer)
        )
    ):
        raise ValueError("member_exemption_arguments")
    guard()
    path = instance_dir(instance) / "member_exemptions.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with MEMBER_LIST_WRITE_LOCK, closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS speech_exemptions(
                account TEXT,group_id TEXT,member TEXT,peer TEXT,enabled INTEGER,
                created REAL,actor TEXT,reason TEXT,name TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(account,group_id,member));
            CREATE TABLE IF NOT EXISTS exemption_audit(
                account TEXT,group_id TEXT,member TEXT,peer TEXT,enabled INTEGER,
                created REAL,actor TEXT,reason TEXT,name TEXT NOT NULL DEFAULT '');
        """)
        db.execute("BEGIN IMMEDIATE")
        for table in ("speech_exemptions", "exemption_audit"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "name" not in columns:
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN name TEXT NOT NULL DEFAULT ''"
                )
        version = db.execute(
            "SELECT COUNT(*) FROM exemption_audit WHERE account=? AND group_id=?",
            (account, group),
        ).fetchone()[0]
        if expected_version is not None and (
            type(expected_version) is not int or expected_version != version
        ):
            raise ProtocolError("member_whitelist_changed")
        if enabled:
            opposite = path.with_name("member_blacklist.sqlite3")
            try:
                opposite.stat()
            except FileNotFoundError:
                pass
            else:
                with closing(
                    sqlite3.connect(opposite.resolve().as_uri() + "?mode=ro", uri=True)
                ) as other:
                    if other.execute(
                        "SELECT 1 FROM blacklist_entries "
                        "WHERE account=? AND group_id=? AND member=? "
                        "AND status='active' LIMIT 1",
                        tuple(str(int(value)) for value in (account, group, member)),
                    ).fetchone():
                        raise ProtocolError("member_list_conflict")
        if not name:
            saved = db.execute(
                "SELECT name FROM speech_exemptions "
                "WHERE account=? AND group_id=? AND member=?",
                (account, group, member),
            ).fetchone()
            if saved:
                name = saved[0]
        values = (
            account,
            group,
            member,
            peer,
            int(enabled),
            time.time(),
            actor,
            reason,
            name,
        )
        db.execute(
            "INSERT OR REPLACE INTO speech_exemptions"
            "(account,group_id,member,peer,enabled,created,actor,reason,name) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            values,
        )
        db.execute(
            "INSERT INTO exemption_audit"
            "(account,group_id,member,peer,enabled,created,actor,reason,name) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            values,
        )
        guard()
    path.chmod(0o600)


def read_exemptions(
    instance, account, group, *, include_names=False, include_conflicts=False
):
    """Read one collector/group list and its monotonically increasing audit version.

    Args:
        instance: Saved collector instance.
        account: Verified collector UID.
        group: Exact authorized group.
        include_names: Return saved peer/name records instead of peer strings.
        include_conflicts: Flag historical overlaps in a named display response.

    Returns:
        Scope version and enabled UID/NIM bindings, or UID/peer-name records.
        Legacy entries retain their IDs and have an empty name until updated.
        Named records flag historical overlaps for display, not authorization.
    """
    path = instance_dir(instance) / "member_exemptions.sqlite3"
    if not path.is_file():
        return 0, {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        version = db.execute(
            "SELECT COUNT(*) FROM exemption_audit WHERE account=? AND group_id=?",
            (account, group),
        ).fetchone()[0]
        name_column = "''"
        if include_names and any(
            row[1] == "name"
            for row in db.execute("PRAGMA table_info(speech_exemptions)")
        ):
            name_column = "name"
        rows = db.execute(
            f"SELECT member,peer,{name_column} FROM speech_exemptions "
            "WHERE account=? AND group_id=? AND enabled=1",
            (account, group),
        ).fetchall()
        conflicts = set()
        if include_names and include_conflicts and rows:
            opposite = path.with_name("member_blacklist.sqlite3")
            try:
                opposite.stat()
            except FileNotFoundError:
                pass
            else:
                with closing(
                    sqlite3.connect(opposite.resolve().as_uri() + "?mode=ro", uri=True)
                ) as other:
                    conflicts = {
                        row[0]
                        for row in other.execute(
                            "SELECT member FROM blacklist_entries "
                            "WHERE account=? AND group_id=? AND status='active'",
                            (str(int(account)), str(int(group))),
                        )
                    }
        return version, {
            uid: {
                "peer": peer,
                "name": name,
                **({"conflict": True} if uid.lstrip("0") in conflicts else {}),
            }
            if include_names
            else peer
            for uid, peer, name in rows
        }
