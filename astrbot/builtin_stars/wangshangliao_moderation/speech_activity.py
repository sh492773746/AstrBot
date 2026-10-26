"""Two-day, content-free activity statistics from authenticated inbox records."""

import asyncio
import json
import math
import sqlite3
import time
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .syntax import recognize_command

ZONE = ZoneInfo("Asia/Shanghai")
BATCH_SIZE = 2000
SETTLE_SECONDS = 30


def day_window(now):
    """Return Beijing date boundaries, independent of the host timezone.

    Args:
        now: Current Unix timestamp.

    Returns:
        Yesterday, today and tomorrow midnight timestamps.
    """
    today = datetime.fromtimestamp(now, ZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tuple((today + timedelta(days=offset)).timestamp() for offset in (-1, 0, 1))


class SpeechActivity:
    """Collect small batches off the event loop without storing message bodies."""

    def __init__(self, context):
        self.context = context
        self.active = True

    @staticmethod
    def database(instance):
        """Open the isolated statistics store, never an operational ledger.

        Args:
            instance: Existing native platform instance ID.

        Returns:
            SQLite connection with content erasure enabled.
        """
        root = instance_dir(instance)
        root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(root / "speech_activity.sqlite3", timeout=1)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS speech_messages(
                account TEXT NOT NULL, group_id TEXT NOT NULL,
                message TEXT NOT NULL, member TEXT NOT NULL, name TEXT NOT NULL,
                day REAL NOT NULL, stamp REAL NOT NULL, kind TEXT NOT NULL,
                PRIMARY KEY(account,group_id,message)
            );
            CREATE INDEX IF NOT EXISTS speech_day_member
                ON speech_messages(account,group_id,day,member);
            CREATE INDEX IF NOT EXISTS speech_retention ON speech_messages(day);
            CREATE TABLE IF NOT EXISTS speech_cursors(
                account TEXT NOT NULL, group_id TEXT NOT NULL,
                received REAL NOT NULL, inbox_row INTEGER NOT NULL,
                updated REAL NOT NULL, catching_up INTEGER NOT NULL,
                PRIMARY KEY(account,group_id)
            );
            """
        )
        return db

    def collect(self, instance, account, group, ledger_path, now):
        """Import one bounded batch and reconcile recalls in the retained window.

        Args:
            instance: Frozen native instance ID.
            account: Frozen robot account.
            group: Frozen enabled group.
            ledger_path: Authenticated adapter's inbox database path.
            now: Collection time.

        Returns:
            Whether another batch is immediately available.
        """
        yesterday, _today, tomorrow = day_window(now)
        ledger_path = Path(ledger_path)
        if not ledger_path.is_file() or ledger_path.is_symlink():
            return False
        with (
            closing(self.database(instance)) as db,
            closing(
                sqlite3.connect(f"{ledger_path.as_uri()}?mode=ro", uri=True, timeout=1)
            ) as inbox,
            db,
        ):
            db.execute(
                "DELETE FROM speech_messages WHERE day<? OR day>=?",
                (yesterday, tomorrow),
            )
            db.execute("DELETE FROM speech_cursors WHERE updated<?", (yesterday,))
            cursor = db.execute(
                "SELECT received,inbox_row FROM speech_cursors WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            received, rowid = (
                (max(cursor[0], yesterday), cursor[1]) if cursor else (yesterday, 0)
            )
            rows = inbox.execute(
                "SELECT rowid,message,payload,state,received_at FROM inbox "
                "WHERE account=? AND team=? AND received_at>=? AND received_at<=? "
                "AND (received_at>? OR (received_at=? AND rowid>?)) "
                "ORDER BY received_at,rowid LIMIT ?",
                (
                    account,
                    group,
                    received,
                    now - SETTLE_SECONDS,
                    received,
                    received,
                    rowid,
                    BATCH_SIZE,
                ),
            ).fetchall()
            accepted = []
            for inbox_row, mid, encoded, state, _arrived in rows:
                if state not in {"pending", "processing", "processed"}:
                    continue
                try:
                    payload = json.loads(encoded)
                    user = payload["sender"]
                    kind = payload.get("kind", "text")
                    text = payload.get("text", "")
                    stamp = float(
                        payload.get("recall_route", {}).get("time")
                        or payload.get("created_at", 0)
                    )
                    if stamp > 1e11:
                        stamp /= 1000
                    if (
                        not isinstance(user, str)
                        or not user.isascii()
                        or not user.isdigit()
                        or not 0 < int(user)
                        or len(user) > 20
                        or user == account
                        or is_managed_account(user)
                        or kind not in {"text", "image", "video", "forward"}
                        or not math.isfinite(stamp)
                        or not yesterday <= stamp <= now - SETTLE_SECONDS
                        or not isinstance(text, str)
                        or len(text) > 16384
                        or (
                            kind == "text"
                            and (
                                not text.strip()
                                or recognize_command(text)
                                or text.lstrip().startswith("/")
                            )
                        )
                    ):
                        continue
                    name = payload.get("name", "")
                    name = (
                        "".join(
                            char
                            for char in name
                            if unicodedata.category(char)
                            not in {"Cc", "Cf", "Zl", "Zp"}
                        )[:128]
                        if isinstance(name, str)
                        else ""
                    )
                    day = (
                        datetime.fromtimestamp(stamp, ZONE)
                        .replace(hour=0, minute=0, second=0, microsecond=0)
                        .timestamp()
                    )
                    accepted.append(
                        (account, group, str(mid), user, name or user, day, stamp, kind)
                    )
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    continue
            db.executemany(
                "INSERT OR IGNORE INTO speech_messages VALUES(?,?,?,?,?,?,?,?)",
                accepted,
            )
            excluded = set()
            tables = {
                row[0]
                for row in inbox.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "observed_recalls" in tables:
                excluded.update(
                    row[0]
                    for row in inbox.execute(
                        "SELECT message_id FROM observed_recalls WHERE account=? AND group_id=? AND received_at>=?",
                        (account, group, yesterday),
                    )
                )
            if "outbox" in tables:
                prefix = f"recall/{account}/{group}/"
                excluded.update(
                    row[0]
                    for row in inbox.execute(
                        "SELECT i.message FROM inbox i JOIN outbox o ON o.key=?||i.message "
                        "WHERE i.account=? AND i.team=? AND i.received_at>=? AND i.received_at<? "
                        "AND o.state IN ('accepted','verified','unknown','sending')",
                        (prefix, account, group, yesterday, tomorrow),
                    )
                )
            moderation = ledger_path.parent / "moderation.sqlite3"
            if moderation.is_file() and not moderation.is_symlink():
                with closing(
                    sqlite3.connect(
                        f"{moderation.as_uri()}?mode=ro", uri=True, timeout=1
                    )
                ) as audit:
                    tables = {
                        row[0]
                        for row in audit.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "content_violations" in tables:
                        excluded.update(
                            row[0]
                            for row in audit.execute(
                                "SELECT message FROM content_violations WHERE account=? AND group_id=? AND observed>=? "
                                "AND status IN ('claimed','warned','recalled','muted','kicked','unknown')",
                                (account, group, yesterday),
                            )
                        )
                    if {"flood_recalls", "flood_batches"} <= tables:
                        excluded.update(
                            row[0]
                            for row in audit.execute(
                                "SELECT r.message FROM flood_recalls r JOIN flood_batches b ON b.operation=r.batch "
                                "WHERE b.account=? AND b.group_id=? AND b.last_seen>=? "
                                "AND r.status IN ('accepted','verified','unknown')",
                                (account, group, yesterday),
                            )
                        )
                    if "moderation_action_audit" in tables:
                        excluded.update(
                            row[0]
                            for row in audit.execute(
                                "SELECT message FROM moderation_action_audit WHERE account=? AND group_id=? "
                                "AND action='recall' AND status IN ('accepted','verified','unknown') AND created>=?",
                                (account, group, yesterday),
                            )
                        )
            db.executemany(
                "DELETE FROM speech_messages WHERE account=? AND group_id=? AND message=?",
                ((account, group, str(mid)) for mid in excluded),
            )
            received, rowid = (rows[-1][4], rows[-1][0]) if rows else (received, rowid)
            catching_up = len(rows) == BATCH_SIZE
            db.execute(
                "INSERT OR REPLACE INTO speech_cursors VALUES(?,?,?,?,?,?)",
                (account, group, received, rowid, now, int(catching_up)),
            )
            return catching_up

    @staticmethod
    def prune():
        """Expire statistics even for disabled or removed bot instances.

        Returns:
            None. Operational inboxes and audit stores are never touched.
        """
        yesterday, _today, tomorrow = day_window(time.time())
        root = Path(get_astrbot_data_path()) / "platform_data" / "wangshangliao"
        if not root.is_dir() or root.is_symlink():
            return
        for path in root.glob("*/speech_activity.sqlite3"):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            with closing(sqlite3.connect(path, timeout=1)) as db, db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute(
                    "DELETE FROM speech_messages WHERE day<? OR day>=?",
                    (yesterday, tomorrow),
                )
                db.execute("DELETE FROM speech_cursors WHERE updated<?", (yesterday,))

    def read(
        self, instance, account, group, *, member="", query="", names=None, now=None
    ):
        """Read only yesterday/today aggregates and exact scoped member choices.

        Args:
            instance: Authorized native instance.
            account: Authorized robot account.
            group: Authorized enabled group.
            member: Optional exact member ID.
            query: Optional literal name or UID substring.
            names: Cached complete native group directory labels.
            now: Optional test clock.

        Returns:
            Two daily totals, hourly counts, rankings and optional member totals.
        """
        now = time.time() if now is None else now
        yesterday, today, tomorrow = day_window(now)
        names = dict(names or {})
        with closing(self.database(instance)) as db, db:
            db.execute(
                "DELETE FROM speech_messages WHERE day<? OR day>=?",
                (yesterday, tomorrow),
            )
            db.execute("DELETE FROM speech_cursors WHERE updated<?", (yesterday,))
            rows = db.execute(
                "SELECT day,member,MAX(name) name,COUNT(*) count,"
                "SUM(kind='text') text,SUM(kind='image') image,SUM(kind='video') video,"
                "SUM(kind='forward') forward,MIN(stamp) first_at,MAX(stamp) last_at "
                "FROM speech_messages WHERE account=? AND group_id=? AND day>=? AND day<? "
                "GROUP BY day,member ORDER BY day DESC,count DESC,member",
                (account, group, yesterday, tomorrow),
            ).fetchall()
            for row in rows:
                if (
                    not names.get(row["member"])
                    or names[row["member"]] == row["member"]
                ):
                    names[row["member"]] = row["name"]
            cursor = db.execute(
                "SELECT updated,catching_up FROM speech_cursors WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            days = []
            for start in (today, yesterday):
                entries = [dict(row) for row in rows if row["day"] == start]
                days.append(
                    {
                        "date": datetime.fromtimestamp(start, ZONE).date().isoformat(),
                        "total": sum(row["count"] for row in entries),
                        "active_members": len(entries),
                        "kinds": {
                            kind: sum(row[kind] for row in entries)
                            for kind in ("text", "image", "video", "forward")
                        },
                        "top": [
                            {
                                "id": row["member"],
                                "name": names.get(row["member"], row["name"]),
                                "count": row["count"],
                            }
                            for row in entries[:30]
                        ],
                        "hours": [0] * 24,
                        "member": next(
                            (row for row in entries if row["member"] == member), None
                        ),
                    }
                )
                for hour, count in db.execute(
                    "SELECT CAST((stamp-?)/3600 AS INTEGER),COUNT(*) FROM speech_messages "
                    "WHERE account=? AND group_id=? AND day=? "
                    + ("AND member=? " if member else "")
                    + "GROUP BY 1",
                    (start, account, group, start, member)
                    if member
                    else (start, account, group, start),
                ):
                    if 0 <= hour < 24:
                        days[-1]["hours"][hour] = count
            matches = [
                {"id": uid, "name": name}
                for uid, name in names.items()
                if query and query.casefold() in f"{name} {uid}".casefold()
            ]
            matches.sort(key=lambda row: (row["name"], row["id"]))
            return {
                "timezone": "Asia/Shanghai",
                "days": days,
                "settle_seconds": SETTLE_SECONDS,
                "updated_at": cursor["updated"] if cursor else 0,
                "catching_up": bool(cursor["catching_up"]) if cursor else True,
                "members": matches[:20],
                "has_more": len(matches) > 20,
                "selected_member": {"id": member, "name": names.get(member, member)}
                if member and member in names
                else None,
            }

    @staticmethod
    def report_config(instance, account):
        """Read a bounded, account-specific report definition, not chat history.

        Args:
            instance: Authorized native platform instance.
            account: Authorized robot account.

        Returns:
            The configured member/group identities, or None when not configured.

        Raises:
            ValueError: If the definition is malformed or belongs to another bot.
        """
        path = instance_dir(instance) / "speech_report.json"
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError("speech_report_invalid")
        config = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(config, dict)
            or set(config) != {"account", "groups", "members"}
            or config["account"] != account
        ):
            raise ValueError("speech_report_invalid")
        for key, limit in (("groups", 20), ("members", 30)):
            items = config[key]
            if not isinstance(items, list) or not 1 <= len(items) <= limit:
                raise ValueError("speech_report_invalid")
            seen = set()
            for item in items:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"id", "name"}
                    or not isinstance(item["id"], str)
                    or not item["id"].isascii()
                    or not item["id"].isdigit()
                    or len(item["id"]) > 20
                    or int(item["id"]) <= 0
                    or item["id"] in seen
                    or not isinstance(item["name"], str)
                    or not 1 <= len(item["name"]) <= 128
                    or any(
                        unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
                        for char in item["name"]
                    )
                ):
                    raise ValueError("speech_report_invalid")
                seen.add(item["id"])
        return config

    def report(self, instance, account, config, *, now=None):
        """Aggregate configured members' effective text across authorized groups.

        Args:
            instance: Authorized native platform instance.
            account: Authorized robot account.
            config: Validated definition whose every group passed authorization.
            now: Optional test clock.

        Returns:
            Today's and yesterday's text-only member/group totals and freshness.
        """
        now = time.time() if now is None else now
        yesterday, today, tomorrow = day_window(now)
        groups = [row["id"] for row in config["groups"]]
        members = [row["id"] for row in config["members"]]
        group_slots = ",".join("?" for _ in groups)
        member_slots = ",".join("?" for _ in members)
        with closing(self.database(instance)) as db, db:
            db.execute(
                "DELETE FROM speech_messages WHERE day<? OR day>=?",
                (yesterday, tomorrow),
            )
            db.execute("DELETE FROM speech_cursors WHERE updated<?", (yesterday,))
            rows = db.execute(
                f"SELECT day,group_id,member,COUNT(*) count FROM speech_messages "
                f"WHERE account=? AND group_id IN ({group_slots}) "
                f"AND member IN ({member_slots}) AND kind='text' AND day>=? AND day<? "
                "GROUP BY day,group_id,member",
                (account, *groups, *members, yesterday, tomorrow),
            ).fetchall()
            counts = {
                (row["day"], row["group_id"], row["member"]): row["count"]
                for row in rows
            }
            cursors = db.execute(
                f"SELECT group_id,updated,catching_up FROM speech_cursors WHERE account=? AND group_id IN ({group_slots})",
                (account, *groups),
            ).fetchall()
        days = []
        for start in (today, yesterday):
            people = []
            for member in config["members"]:
                values = [
                    counts.get((start, group, member["id"]), 0) for group in groups
                ]
                people.append({**member, "counts": values, "total": sum(values)})
            totals = [
                sum(row["counts"][index] for row in people)
                for index in range(len(groups))
            ]
            days.append(
                {
                    "date": datetime.fromtimestamp(start, ZONE).date().isoformat(),
                    "members": people,
                    "totals": totals,
                    "total": sum(totals),
                }
            )
        return {
            "groups": config["groups"],
            "days": days,
            "timezone": "Asia/Shanghai",
            "catching_up": len(cursors) != len(groups)
            or any(row["catching_up"] for row in cursors),
            "updated_at": min((row["updated"] for row in cursors), default=0),
        }

    async def run(self):
        """Collect asynchronously; failures never interrupt moderation.

        Returns:
            None when cancelled or stopped.
        """
        while self.active:
            more = False
            try:
                await asyncio.to_thread(self.prune)
            except (sqlite3.Error, OSError):
                logger.warning("WSL speech statistics retention deferred")
            for adapter in list(self.context.platform_manager.platform_insts):
                config = getattr(adapter, "config", {})
                if config.get("type") != "wangshangliao":
                    continue
                path = getattr(getattr(adapter, "ledger", None), "path", None)
                if (
                    not config.get("enable", True)
                    or not path
                    or not getattr(adapter, "account", "")
                ):
                    continue
                for group in list(config.get("enabled_groups", [])):
                    if not self.active:
                        return
                    try:
                        more |= await asyncio.to_thread(
                            self.collect,
                            config["id"],
                            str(adapter.account),
                            group,
                            path,
                            time.time(),
                        )
                    except (sqlite3.Error, OSError):
                        logger.warning("WSL speech statistics collection deferred")
                    await asyncio.sleep(0)
            await asyncio.sleep(1 if more else 20)
