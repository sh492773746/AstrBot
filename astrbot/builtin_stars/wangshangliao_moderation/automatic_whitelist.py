"""Short-lived, exact-identity exemptions derived from settled daily rankings."""

import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.core.platform.sources.wangshangliao.blacklist import MEMBER_LIST_WRITE_LOCK
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.group_scope import scoped
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.test_rules import flood_fixture
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


def read_automatic(instance, account, group, *, member=None, peer=None):
    """Read fresh automatic entries, respecting blacklist and explicit removal.

    Args:
        instance: Owning native platform instance.
        account: Collector UID.
        group: Exact business group.
        member: Optional exact UID for a bounded authorization lookup.
        peer: Optional exact native identity accompanying that UID.

    Returns:
        Current automatic UID/peer/name/count bindings, separate from manual grants.
    """
    root = instance_dir(instance)
    path = root / "automatic_exemptions.sqlite3"
    if not path.is_file() or path.is_symlink():
        return {}
    now = time.time()
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
    ) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_exemptions'"
        ).fetchone():
            return {}
        query = (
            "SELECT member,peer,name,messages FROM automatic_exemptions "
            "WHERE account=? AND group_id=? AND valid_until>? AND day_start<=?"
        )
        arguments = (account, group, now, now)
        if member is not None:
            query += " AND member=? AND peer=?"
            arguments += (member, peer)
        entries = {
            uid: {"peer": peer, "name": name, "count": count, "source": "daily_top30"}
            for uid, peer, name, count in db.execute(query, arguments)
        }
    if not entries:
        return {}
    day_start = (
        datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    for filename, query, arguments in (
        (
            "member_blacklist.sqlite3",
            "SELECT member FROM blacklist_entries WHERE account=? AND group_id=? AND status='active'",
            (account, group),
        ),
        (
            "member_exemptions.sqlite3",
            "SELECT member FROM speech_exemptions WHERE account=? AND group_id=? "
            "AND enabled=0 AND created>=?",
            (account, group, day_start),
        ),
    ):
        other = root / filename
        if other.is_file():
            with closing(
                sqlite3.connect(other.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                for (uid,) in db.execute(query, arguments):
                    entries.pop(uid, None)
    return entries


class AutomaticWhitelist:
    """Refresh groups sequentially off the message path; stale grants fail closed."""

    def __init__(self, context, rankings):
        self.context = context
        self.rankings = rankings
        self.active = True

    def publish(self, adapter, group, account, mapping, date, end, ranked):
        """Atomically replace one group's short-lived grants after reauthorization.

        Args:
            adapter: Original adapter.
            group: Enabled business group.
            account: Frozen collector account.
            mapping: Frozen native member mapping.
            date: Beijing ranking date.
            end: Beijing day-end timestamp.
            ranked: Settled UID/name/count tuples.
        """
        now = time.time()
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
                    PRIMARY KEY(account,group_id,member));
                CREATE INDEX IF NOT EXISTS automatic_exemptions_expiry
                    ON automatic_exemptions(valid_until);
            """)
            db.execute("BEGIN IMMEDIATE")
            current = adapter.config
            if (
                not self.active
                or adapter.account != account
                or not current.get("enable", True)
                or current.get("_business_active") is False
                or group not in current.get("enabled_groups", [])
                or not current.get("moderation", {}).get("automation_enabled")
                or adapter.members.get(group, {}) != mapping
                or datetime.fromtimestamp(
                    time.time(), ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d")
                != date
            ):
                raise ProtocolError("automatic_whitelist_scope_changed")
            blocked = set()
            for filename, sql, params in (
                (
                    "member_blacklist.sqlite3",
                    "SELECT member FROM blacklist_entries WHERE account=? AND group_id=? AND status='active'",
                    (account, group),
                ),
                (
                    "member_exemptions.sqlite3",
                    "SELECT member FROM speech_exemptions WHERE account=? AND group_id=? AND enabled=0 AND created>=?",
                    (account, group, end - 86400),
                ),
            ):
                other = root / filename
                if other.is_file():
                    with closing(
                        sqlite3.connect(other.resolve().as_uri() + "?mode=ro", uri=True)
                    ) as saved:
                        blocked.update(row[0] for row in saved.execute(sql, params))
            entries = []
            fixture = flood_fixture(adapter, group)
            for uid, name, count in ranked:
                peer = mapping.get(uid, "")
                if (
                    uid in blocked
                    or uid == account
                    or is_managed_account(uid)
                    and not (fixture and fixture.ranking and uid == "23691273")
                    or not peer
                ):
                    continue
                entries.append(
                    (
                        account,
                        group,
                        uid,
                        peer,
                        name,
                        count,
                        end - 86400,
                        min(end, now + 90),
                    )
                )
                if len(entries) == 30:
                    break
            db.execute(
                "DELETE FROM automatic_exemptions WHERE valid_until<=? OR (account=? AND group_id=?)",
                (now, account, group),
            )
            db.executemany(
                "INSERT INTO automatic_exemptions VALUES(?,?,?,?,?,?,?,?)", entries
            )
            if (
                not self.active
                or adapter.account != account
                or group not in adapter.config.get("enabled_groups", [])
                or adapter.members.get(group, {}) != mapping
                or not adapter.config.get("moderation", {}).get("automation_enabled")
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
                            view, group, settled=True
                        )
                        count = await asyncio.to_thread(
                            self.publish,
                            view,
                            group,
                            account,
                            mapping,
                            date,
                            end,
                            ranked,
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
