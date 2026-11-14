"""Activity-retained automatic exemptions with short-lived authorization leases."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing, nullcontext
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.core.platform.sources.wangshangliao.blacklist import MEMBER_LIST_WRITE_LOCK
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.group_scope import scoped
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.test_rules import flood_fixture
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

INACTIVITY_SECONDS = 7 * 86400


def read_automatic(instance, account, group, *, member=None, peer=None, manual_db=None):
    """Read fresh automatic entries, respecting blacklist and explicit removal.

    Args:
        instance: Owning native platform instance.
        account: Collector UID.
        group: Exact business group.
        member: Optional exact UID for a bounded authorization lookup.
        peer: Optional exact native identity accompanying that UID.
        manual_db: Optional caller-owned manual-list connection, used only here.

    Returns:
        Current automatic UID/peer/name/count bindings, separate from manual grants.
    """
    root = instance_dir(instance)
    path = root / "automatic_exemptions.sqlite3"
    if not path.is_file() or path.is_symlink():
        return {}
    now = time.time()
    today = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_start = today.timestamp()
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
    ) as db:
        query = (
            "SELECT member,peer,name,messages,day_start FROM automatic_exemptions "
            "WHERE account=? AND group_id=? AND valid_until>? AND day_start<=? "
            "AND last_activity>? AND ranking_day!='' AND messages>100"
        )
        arguments = (account, group, now, day_start, now - INACTIVITY_SECONDS)
        if member is not None:
            query += " AND member=? AND peer=?"
            arguments += (member, peer)
        try:
            rows = db.execute(query, arguments).fetchall()
            entries = {
                uid: {
                    "peer": peer,
                    "name": name,
                    "count": count,
                    "source": "daily_top30",
                }
                for uid, peer, name, count, _qualified in rows
            }
            qualified = {row[0]: row[4] for row in rows}
        except sqlite3.OperationalError as exc:
            if str(exc) in {
                "no such table: automatic_exemptions",
                "no such column: ranking_day",
                "no such column: last_activity",
            }:
                return {}
            raise
    if not entries:
        return {}
    for filename, query, arguments in (
        (
            "member_blacklist.sqlite3",
            "SELECT member FROM blacklist_entries WHERE account=? AND group_id=? AND status='active'",
            (account, group),
        ),
        (
            "member_exemptions.sqlite3",
            "SELECT member,created FROM speech_exemptions WHERE account=? AND group_id=? "
            "AND enabled=0",
            (account, group),
        ),
    ):
        other = root / filename
        if other.is_file():
            if member is not None:
                query += " AND member=?"
                arguments += (member,)
            context = (
                nullcontext(manual_db)
                if filename == "member_exemptions.sqlite3" and manual_db is not None
                else closing(
                    sqlite3.connect(
                        other.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2
                    )
                )
            )
            with context as db:
                for row in db.execute(query, arguments):
                    if len(row) == 1 or row[1] >= qualified.get(row[0], 0):
                        entries.pop(row[0], None)
    return entries


class AutomaticWhitelist:
    """Refresh groups sequentially off the message path; stale grants fail closed."""

    def __init__(self, context, rankings):
        self.context = context
        self.rankings = rankings
        self.active = True

    def publish(self, adapter, group, account, mapping, date, end, ranked, current=()):
        """Renew qualified identities until seven days without eligible speech.

        Args:
            adapter: Original adapter.
            group: Enabled business group.
            account: Frozen collector account.
            mapping: Frozen native member mapping.
            date: Yesterday's Beijing ranking date.
            end: Yesterday's day-end timestamp.
            ranked: Settled yesterday UID/name/count/last-activity tuples.
            current: Settled today UID/name/count/last-activity tuples.

        Returns:
            Number of active automatic grants after the refresh.

        Raises:
            ProtocolError: If the authorization or Beijing day changes.
        """
        now = time.time()
        today = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = (today + timedelta(days=1)).timestamp()
        cutoff = now - INACTIVITY_SECONDS
        today_date = today.strftime("%Y-%m-%d")
        root = instance_dir(adapter.config["id"])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "automatic_exemptions.sqlite3"
        with (
            MEMBER_LIST_WRITE_LOCK,
            closing(sqlite3.connect(path, timeout=2)) as db,
            db,
        ):
            db.executescript("""
                CREATE TABLE IF NOT EXISTS automatic_exemptions(
                    account TEXT,group_id TEXT,member TEXT,peer TEXT,name TEXT,
                    messages INTEGER,day_start REAL,valid_until REAL,
                    ranking_day TEXT NOT NULL DEFAULT '',
                    last_activity REAL NOT NULL DEFAULT 0,
                    activity_history TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(account,group_id,member));
                CREATE INDEX IF NOT EXISTS automatic_exemptions_expiry
                    ON automatic_exemptions(valid_until);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(automatic_exemptions)")
            }
            for column, declaration in (
                ("ranking_day", "TEXT NOT NULL DEFAULT ''"),
                ("last_activity", "REAL NOT NULL DEFAULT 0"),
                ("activity_history", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE automatic_exemptions ADD COLUMN {column} {declaration}"
                    )
            config = adapter.config
            if (
                not self.active
                or adapter.account != account
                or not config.get("enable", True)
                or config.get("_business_active") is False
                or group not in config.get("enabled_groups", [])
                or not config.get("moderation", {}).get("automation_enabled")
                or adapter.members.get(group, {}) != mapping
                or (today - timedelta(days=1)).strftime("%Y-%m-%d") != date
                or today.timestamp() != end
                or time.time() >= day_end
            ):
                raise ProtocolError("automatic_whitelist_scope_changed")
            blocked = set()
            removals = {}
            for filename, sql, params in (
                (
                    "member_blacklist.sqlite3",
                    "SELECT member FROM blacklist_entries WHERE account=? AND group_id=? AND status='active'",
                    (account, group),
                ),
                (
                    "member_exemptions.sqlite3",
                    "SELECT member,created FROM speech_exemptions WHERE account=? AND group_id=? AND enabled=0",
                    (account, group),
                ),
            ):
                other = root / filename
                if other.is_file():
                    with closing(
                        sqlite3.connect(other.resolve().as_uri() + "?mode=ro", uri=True)
                    ) as saved:
                        if filename == "member_exemptions.sqlite3":
                            removals.update(saved.execute(sql, params))
                        else:
                            blocked.update(row[0] for row in saved.execute(sql, params))
            saved_entries = {
                row[0]: row[1:]
                for row in db.execute(
                    "SELECT member,peer,name,messages,day_start,ranking_day,last_activity,activity_history "
                    "FROM automatic_exemptions WHERE account=? AND group_id=?",
                    (account, group),
                )
            }
            activity = {}
            for activity_date, lower, upper, ranking in (
                (date, end - 86400, end, ranked),
                (today_date, end, day_end, current),
            ):
                activity[activity_date] = {
                    uid: stamp
                    for uid, _name, count, stamp in ranking
                    if count > 0 and lower <= stamp < min(upper, now - 30)
                }
            qualified = {
                uid: (name, count)
                for uid, name, count, _stamp in ranked[:20]
                if count > 100 and uid in activity[date]
            }
            entries = []
            fixture = flood_fixture(adapter, group)
            for uid in saved_entries.keys() | qualified.keys():
                peer = mapping.get(uid, "")
                if (
                    uid in blocked
                    or uid == account
                    or is_managed_account(uid)
                    and not (fixture and fixture.ranking and uid == "23691273")
                    or not peer
                ):
                    continue
                saved = saved_entries.get(uid)
                if saved and saved[0] != peer:
                    saved = None
                history = {}
                if saved:
                    try:
                        history = json.loads(saved[6])
                        if not isinstance(history, dict):
                            history = {}
                    except (ValueError, TypeError):
                        history = {}
                # Two-day recalculation removes late recalls without keeping text.
                history = {
                    day: stamp
                    for day, stamp in history.items()
                    if day not in {date, today_date}
                    and type(stamp) in {int, float}
                    and cutoff < stamp < end - 86400
                }
                for day, stamps in activity.items():
                    if uid in stamps:
                        history[day] = stamps[uid]
                last_activity = max(history.values(), default=0)
                if last_activity <= cutoff:
                    continue
                if uid in qualified:
                    name, count = qualified[uid]
                    qualified_at, ranking_date = end, date
                elif (
                    saved
                    and saved[5] > 0
                    and saved[3] < end
                    and last_activity <= saved[5] + INACTIVITY_SECONDS
                ):
                    _peer, name, count, qualified_at, ranking_date, *_ = saved
                else:
                    continue
                if removals.get(uid, float("-inf")) >= qualified_at:
                    continue
                entries.append(
                    (
                        account,
                        group,
                        uid,
                        peer,
                        name,
                        count,
                        qualified_at,
                        min(now + 90, last_activity + INACTIVITY_SECONDS),
                        ranking_date,
                        last_activity,
                        json.dumps(history, sort_keys=True, separators=(",", ":")),
                    )
                )
            db.execute(
                "DELETE FROM automatic_exemptions WHERE "
                "(last_activity>0 AND last_activity<=? AND valid_until<=?) "
                "OR (account=? AND group_id=?)",
                (cutoff, now, account, group),
            )
            db.executemany(
                "INSERT INTO automatic_exemptions"
                "(account,group_id,member,peer,name,messages,day_start,valid_until,ranking_day,"
                "last_activity,activity_history) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                entries,
            )
            if (
                not self.active
                or adapter.account != account
                or not adapter.config.get("enable", True)
                or adapter.config.get("_business_active") is False
                or group not in adapter.config.get("enabled_groups", [])
                or adapter.members.get(group, {}) != mapping
                or not adapter.config.get("moderation", {}).get("automation_enabled")
                or time.time() >= day_end
            ):
                raise ProtocolError("automatic_whitelist_scope_changed")
        path.chmod(0o600)
        return len(entries)

    async def step(self):
        """Refresh active collectors without network roster calls or ingress scans."""
        for adapter in list(self.context.platform_manager.platform_insts):
            config = getattr(adapter, "config", {})
            if (
                not self.active
                or config.get("type") != "wangshangliao"
                or not config.get("enable", True)
                or config.get("_business_active") is False
                or getattr(adapter, "connection_state", "") != "online"
            ):
                continue
            account = adapter.account
            for group in list(config.get("enabled_groups", [])):
                if not self.active:
                    return
                view = scoped(adapter, group)
                if not view.config.get("moderation", {}).get("automation_enabled"):
                    continue
                try:
                    started = time.monotonic()
                    mapping = dict(adapter.members.get(group, {}))
                    async with asyncio.timeout(10):
                        date, end, ranked = await self.rankings.snapshot(
                            view,
                            group,
                            settled=True,
                            previous_day=True,
                            include_activity=True,
                        )
                        (
                            current_date,
                            current_end,
                            current,
                        ) = await self.rankings.snapshot(
                            view, group, settled=True, include_activity=True
                        )
                        if (
                            current_date
                            != datetime.fromtimestamp(
                                end, ZoneInfo("Asia/Shanghai")
                            ).strftime("%Y-%m-%d")
                            or current_end != end + 86400
                        ):
                            raise ProtocolError("automatic_whitelist_scope_changed")
                        count = await asyncio.to_thread(
                            self.publish,
                            view,
                            group,
                            account,
                            mapping,
                            date,
                            end,
                            ranked,
                            current,
                        )
                    adapter.diagnostics.emit(
                        "automatic_whitelist",
                        "refreshed",
                        f"{date}/{count}",
                        group=group,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Wangshangliao automatic whitelist refresh failed for %s", group
                    )

    async def run(self):
        """Bound update frequency; expired snapshots never grant exemptions."""
        while self.active:
            await self.step()
            await asyncio.sleep(30)
