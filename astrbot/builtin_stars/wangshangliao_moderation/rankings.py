"""Read-only daily text rankings from authenticated, deduplicated inbox events."""

import asyncio
import hashlib
import json
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timedelta
from heapq import merge
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.test_rules import (
    flood_fixture,
    rule_target,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .syntax import recognize_command

CPU_BATCH_SIZE = 32
NORMALIZED_CACHE_BATCHES = 512


def _display_name(value, user: str, peer: str = "") -> str:
    """Sanitize a recorded name without substituting an internal identity.

    Args:
        value: Name from an authenticated message or verified member record.
        user: Business identity associated with the record.
        peer: Transport identity associated with the record, when known.

    Returns:
        A bounded single-line name, or an empty string when unavailable.
    """
    if not isinstance(value, str):
        return ""
    name = (
        "".join(
            char
            for char in value
            if not unicodedata.category(char).startswith("C")
            and unicodedata.category(char) not in {"Zl", "Zp"}
        )
        .strip()[:32]
        .strip()
    )
    normalized = unicodedata.normalize("NFKC", name)
    if normalized.isdecimal():
        normalized = str(int(normalized))
    return "" if normalized in {user, peer} else name


def _decode_batch(rows, account):
    """Decode detached inbox rows before loop-owned managed-account filtering.

    Args:
        rows: Bounded, detached rows without known recall or violation evidence.
        account: Frozen collector identity.

    Returns:
        Decoded candidates with valid non-collector sender identities.
    """
    messages = []
    for mid, encoded, state in rows:
        if state not in {"pending", "processing", "processed"}:
            continue
        try:
            payload = json.loads(encoded)
            user = str(payload["sender"])
            text = payload["text"]
            stamp = float(
                payload.get("recall_route", {}).get("time")
                or payload.get("created_at")
                or 0
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if (
            not user.isascii()
            or not user.isdigit()
            or len(user) > 20
            or int(user) <= 0
            or user == account
        ):
            continue
        messages.append((mid, payload, user, text, stamp))
    return messages


def _message_batch(rows, nim_account, lower, upper):
    """Normalize detached candidates before live authorization and settlement.

    Args:
        rows: Bounded, decoded source candidates.
        nim_account: Frozen transport identity for verified leading mentions.
        lower: Inclusive source-day timestamp.
        upper: Exclusive source-day cutoff.

    Returns:
        Sorted candidate tuples for chronological reduction.
    """
    messages = []
    for mid, payload, user, text, stamp in rows:
        if (
            not isinstance(text, str)
            or len(text) > 16384
            or recognize_command(text)
            or text.strip().startswith("/")
        ):
            continue
        if stamp > 100000000000:
            stamp /= 1000
        if not lower <= stamp < upper:
            continue
        # Strip only a transport-verified leading bot mention.
        encoded_text = text.encode("utf-16-le", errors="replace")
        spans = payload.get("mention_spans", [])
        if not isinstance(spans, list) or any(
            not isinstance(span, dict) for span in spans
        ):
            continue
        for span in spans:
            stop = span.get("end", 0)
            if (
                span.get("uid") == nim_account
                and span.get("start") == 0
                and type(stop) is int
                and isinstance(span.get("nick"), str)
                and 0 < stop * 2 <= len(encoded_text)
                and encoded_text[: stop * 2]
                .decode("utf-16-le", errors="replace")
                .rstrip()
                == "@" + span.get("nick", "")
            ):
                text = encoded_text[stop * 2 :].decode("utf-16-le", errors="replace")
                break
        normalized = "".join(
            char
            for char in unicodedata.normalize("NFKC", text).casefold()
            if not char.isspace() and unicodedata.category(char) != "Cf"
        )
        if recognize_command(text) or not any(char.isalnum() for char in normalized):
            continue
        name = _display_name(
            payload.get("name"),
            user,
            str(payload.get("recall_route", {}).get("peer") or ""),
        )
        digest = hashlib.sha256(normalized.encode()).digest()
        messages.append((stamp, str(mid), user, name, digest))
    return sorted(messages)


def _digest_batch(digest, rows):
    """Update a scan-local source digest in a bounded worker operation.

    Args:
        digest: Digest owned only by this scan.
        rows: Detached source identity, state and pruning-marker rows.
    """
    for row in rows:
        digest.update(json.dumps(tuple(row)).encode())
        digest.update(b"\n")


def _reduce_batch(messages, state):
    """Advance the detached chronological reduction by at most one CPU batch.

    Args:
        messages: Merge iterator over individually sorted candidate batches.
        state: Scan-local count, name, last-counted, repeat and tie-break maps.

    Returns:
        Whether any candidates were consumed.
    """
    counts, names, last_counted, repeated, reached = state
    rows = list(islice(messages, CPU_BATCH_SIZE))
    for stamp, _mid, user, name, digest in rows:
        if name:
            names[user] = name
        previous = repeated.get((user, digest), float("-inf"))
        repeated[(user, digest)] = stamp
        if stamp - previous < 300:
            continue
        if stamp - last_counted.get(user, float("-inf")) < 30:
            continue
        counts[user] = counts.get(user, 0) + 1
        last_counted[user] = reached[user] = stamp
    return bool(rows)


class DailyRankings:
    """Bound ranking scans and coalesce repeated queries without changing ledgers."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.cache = {}
        self.settled_cache = OrderedDict()
        self.normalized_cache = OrderedDict()

    async def snapshot(
        self,
        adapter,
        group: str,
        *,
        settled=False,
        previous_day=False,
        include_activity=False,
    ):
        """Read a bounded Beijing-day ranking, allowing moderation to settle.

        Args:
            adapter: Native adapter owning the authenticated inbox.
            group: Enabled business group, never a displayed group number.
            settled: Exclude the newest thirty seconds for automatic exemptions.
            previous_day: Read yesterday instead of today's public ranking.
            include_activity: Append the last counted event time to each entry.

        Returns:
            Date, day-end timestamp and ranked UID/name/count tuples, optionally
            including each member's last counted event time.

        Raises:
            ProtocolError: If scope, message storage or a bounded scan is invalid.
        """
        now = time.time()
        zone = ZoneInfo("Asia/Shanghai")
        start = datetime.fromtimestamp(now, zone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if previous_day:
            start -= timedelta(days=1)
        end = start + timedelta(days=1)
        date = start.strftime("%Y-%m-%d")
        fixture = flood_fixture(adapter, group)
        ledger = adapter.ledger
        connection = getattr(ledger, "db", None)
        path = Path(ledger.path)

        def scope():
            """Read fresh authority and ledger identity without retaining a view."""
            config = adapter.config
            if (
                not config.get("enable", True)
                or group not in config.get("enabled_groups", [])
                or not adapter.account
                or adapter.ledger is not ledger
                or getattr(ledger, "db", None) is not connection
                or Path(ledger.path) != path
            ):
                raise ProtocolError("ranking_scope")
            if not path.is_file() or path.is_symlink():
                raise ProtocolError("ranking_unavailable")
            stat = path.stat()
            current_fixture = flood_fixture(adapter, group)
            return (
                config["id"],
                adapter.account,
                str(adapter.nim_account),
                config.get("_business_epoch"),
                config.get("_business_revision"),
                config.get("_business_active"),
                config.get("_open_revisions", {}).get(group),
                config.get("_open_paused"),
                config.get("moderation", {}).get("enabled"),
                config.get("moderation", {}).get("automation_enabled"),
                id(ledger),
                id(connection),
                path.resolve(),
                stat.st_dev,
                stat.st_ino,
                datetime.fromtimestamp(time.time(), zone).date(),
                id(current_fixture),
                current_fixture.operation
                if current_fixture and current_fixture.ranking
                else "",
            )

        identity = scope()
        account, nim_account = identity[1:3]
        key = (
            identity[0],
            account,
            group,
            date,
            settled,
            fixture.operation if fixture and fixture.ranking else "",
        )

        async def offload(function, *args, **kwargs):
            """Yield after detached CPU work and reject changed live authority.

            Args:
                function: Worker operation with no cache or adapter references.
                *args: Detached operation inputs.
                **kwargs: Detached operation options.

            Returns:
                The completed worker result, only while the original scope holds.
            """
            result = await asyncio.to_thread(function, *args, **kwargs)
            await asyncio.sleep(0)
            if scope() != identity:
                raise ProtocolError("ranking_scope")
            return result

        async with self.lock:
            if scope() != identity:
                raise ProtocolError("ranking_scope")
            yesterday = (
                datetime.fromtimestamp(now, zone) - timedelta(days=1)
            ).strftime("%Y-%m-%d")
            for entry in list(self.settled_cache):
                if entry[3] != yesterday:
                    self.settled_cache.pop(entry)
            cached = self.cache.get(key)
            if cached and time.monotonic() < cached[0] and cached[2] == identity:
                ranked = cached[1]
            else:
                normalized_updates = OrderedDict()
                violations = set()
                audit_path = instance_dir(identity[0]) / "moderation.sqlite3"
                if audit_path.is_file() and not audit_path.is_symlink():
                    async with aiosqlite.connect(
                        f"{audit_path.as_uri()}?mode=ro", uri=True
                    ) as db:
                        async with db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='content_violations'"
                        ) as cursor:
                            has_rules = await cursor.fetchone()
                        if has_rules:
                            async with db.execute(
                                "SELECT message FROM content_violations "
                                "WHERE account=? AND group_id=? AND observed>=? "
                                "AND status IN ('observed','claimed','warned','recalled','muted','kicked','unknown',"
                                "'preflight_failed','execution_rejected','review_only','identity_unverified')",
                                (account, group, start.timestamp()),
                            ) as cursor:
                                violations = {row[0] for row in await cursor.fetchall()}
                        async with db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ) as cursor:
                            tables = {row[0] for row in await cursor.fetchall()}
                        if {"flood_recalls", "flood_batches"} <= tables:
                            async with db.execute(
                                "SELECT r.message FROM flood_recalls r JOIN flood_batches b ON b.operation=r.batch "
                                "WHERE b.account=? AND b.group_id=? AND b.last_seen>=? "
                                "AND r.status IN ('accepted','verified','unknown')",
                                (account, group, start.timestamp()),
                            ) as cursor:
                                violations.update(
                                    row[0] for row in await cursor.fetchall()
                                )
                        if "moderation_action_audit" in tables:
                            async with db.execute(
                                "SELECT message FROM moderation_action_audit "
                                "WHERE account=? AND group_id=? AND action='recall' "
                                "AND status IN ('accepted','verified','unknown') AND created>=?",
                                (account, group, start.timestamp()),
                            ) as cursor:
                                violations.update(
                                    row[0] for row in await cursor.fetchall()
                                )
                messages = []
                scanned = 0
                reused = False
                async with aiosqlite.connect(
                    f"{path.resolve().as_uri()}?mode=ro", uri=True
                ) as db:
                    async with db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ) as cursor:
                        tables = {row[0] for row in await cursor.fetchall()}
                    if "observed_recalls" in tables:
                        async with db.execute(
                            "SELECT message_id FROM observed_recalls WHERE account=? AND group_id=? "
                            "AND received_at>=?",
                            (account, group, start.timestamp()),
                        ) as cursor:
                            violations.update(row[0] for row in await cursor.fetchall())
                    if "outbox" in tables:
                        async with db.execute(
                            "SELECT i.message FROM inbox i JOIN outbox o ON o.key=? || i.message "
                            "WHERE i.account=? AND i.team=? AND i.received_at>=? "
                            "AND o.state IN ('accepted','verified','unknown','sending')",
                            (
                                f"recall/{account}/{group}/",
                                account,
                                group,
                                start.timestamp(),
                            ),
                        ) as cursor:
                            violations.update(row[0] for row in await cursor.fetchall())
                    source = None
                    if previous_day and now >= end.timestamp() + 30:
                        # Ingested payloads are immutable until retention pruning.
                        # Check every row's identity/state/pruning marker and all
                        # recall evidence before reusing yesterday's calculation.
                        digest = hashlib.sha256()
                        async with db.execute(
                            "SELECT rowid,message,state,pruned_at FROM inbox "
                            "WHERE account=? AND team=? AND received_at>=? AND received_at<? "
                            "ORDER BY rowid",
                            (
                                account,
                                group,
                                start.timestamp(),
                                end.timestamp(),
                            ),
                        ) as cursor:
                            while rows := await cursor.fetchmany(CPU_BATCH_SIZE):
                                scanned += len(rows)
                                if scanned > 100000:
                                    raise ProtocolError("ranking_limit")
                                await offload(_digest_batch, digest, rows)
                        source = (
                            path.resolve(),
                            path.stat().st_ino,
                            digest.digest(),
                            frozenset(violations),
                            identity,
                        )
                        cached_settled = self.settled_cache.get(key)
                        if cached_settled and cached_settled[0] == source:
                            ranked = cached_settled[1]
                            reused = True
                        scanned = 0
                    if not reused:
                        async with db.execute(
                            "SELECT rowid,message,state,pruned_at,payload FROM inbox "
                            "WHERE account=? AND team=? AND received_at>=? AND received_at<? "
                            "ORDER BY rowid",
                            (account, group, start.timestamp(), end.timestamp()),
                        ) as cursor:
                            while rows := await cursor.fetchmany(CPU_BATCH_SIZE):
                                scanned += len(rows)
                                if scanned > 100000:
                                    raise ProtocolError("ranking_limit")
                                source_key = (
                                    identity,
                                    group,
                                    date,
                                    tuple(tuple(row[:4]) for row in rows),
                                )
                                normalized = self.normalized_cache.get(source_key)
                                if normalized is None:
                                    # Inbox payloads are immutable until pruning.
                                    # Row identity/state/pruning markers invalidate
                                    # cached normalization without retaining text.
                                    batch = await offload(
                                        _decode_batch,
                                        tuple((row[1], row[4], row[2]) for row in rows),
                                        account,
                                    )
                                    normalized = tuple(
                                        await offload(
                                            _message_batch,
                                            batch,
                                            nim_account,
                                            start.timestamp(),
                                            end.timestamp(),
                                        )
                                    )
                                normalized_updates[source_key] = normalized
                                while (
                                    len(normalized_updates) > NORMALIZED_CACHE_BATCHES
                                ):
                                    normalized_updates.popitem(last=False)
                                cutoff = min(
                                    end.timestamp(), now - 30 if settled else now + 1
                                )
                                # Recall evidence, managed identities and settlement
                                # remain live even when payload decoding is reused.
                                messages.append(
                                    [
                                        entry
                                        for entry in normalized
                                        if entry[1] not in violations
                                        and entry[0] < cutoff
                                        and (
                                            not is_managed_account(entry[2])
                                            or fixture
                                            and fixture.ranking
                                            and rule_target(
                                                adapter, group, entry[2], entry[1]
                                            )
                                        )
                                    ]
                                )
                if not reused:
                    counts, names, last_counted, repeated, reached = {}, {}, {}, {}, {}
                    state = (counts, names, last_counted, repeated, reached)
                    ordered = merge(*messages)
                    while await offload(_reduce_batch, ordered, state):
                        pass
                    members = iter(counts)
                    runs = []
                    while users := tuple(islice(members, CPU_BATCH_SIZE)):
                        rows = [
                            (
                                user,
                                names.get(user, ""),
                                counts[user],
                                last_counted[user],
                            )
                            for user in users
                        ]
                        runs.append(
                            await offload(
                                sorted,
                                rows,
                                key=lambda entry: (-entry[2], entry[3], entry[0]),
                            )
                        )
                    ordered = merge(
                        *runs, key=lambda entry: (-entry[2], entry[3], entry[0])
                    )
                    ranked = []
                    while rows := await offload(list, islice(ordered, CPU_BATCH_SIZE)):
                        ranked.extend(rows)
                # All DB closes and CPU awaits settle before publishing either cache.
                if scope() != identity:
                    raise ProtocolError("ranking_scope")
                for entry in list(self.normalized_cache):
                    if entry[0][-3] != identity[-3]:
                        self.normalized_cache.pop(entry)
                for entry, normalized in normalized_updates.items():
                    self.normalized_cache[entry] = normalized
                    self.normalized_cache.move_to_end(entry)
                while len(self.normalized_cache) > NORMALIZED_CACHE_BATCHES:
                    self.normalized_cache.popitem(last=False)
                self.cache = {
                    entry: value
                    for entry, value in self.cache.items()
                    if time.monotonic() < value[0]
                }
                self.cache[key] = (time.monotonic() + 10, ranked, identity)
                if source is not None:
                    self.settled_cache[key] = (source, ranked)
                    self.settled_cache.move_to_end(key)
                    while len(self.settled_cache) > 32:
                        self.settled_cache.popitem(last=False)
        if scope() != identity:
            raise ProtocolError("ranking_scope")
        return (
            date,
            end.timestamp(),
            ranked if include_activity else [entry[:3] for entry in ranked],
        )

    async def read(
        self,
        adapter,
        group: str,
        sender: str,
        *,
        page: int = 1,
        native_mentions: bool = False,
    ) -> str | MessageChain:
        """Render the first twenty members without network directory reads.

        Args:
            adapter: Authenticated native adapter.
            group: Enabled business group.
            sender: Authenticated caller's UID.
            page: Legacy positive page argument; every value returns the top twenty.
            native_mentions: Legacy flag requesting a plain-text message chain.

        Returns:
            Text or a plain-text message chain containing names and valid counts.
        """
        if type(page) is not int or not 1 <= page <= 99999:
            raise ProtocolError("ranking_page")
        _date, _end, ranked = await self.snapshot(adapter, group)
        entries = ranked[:20]
        mapping = getattr(adapter, "members", {}).get(group, {})
        snapshot = getattr(adapter, "member_snapshots", {}).get(group)
        state = getattr(adapter, "group_directory_state", {}).get(group)
        names, seen = {}, set()
        # Display enrichment must not acquire refresh/HTTP locks shared by moderation.
        # Read the adapter's already-published complete roster once, keyed only by UID.
        if (
            entries
            and isinstance(snapshot, tuple)
            and len(snapshot) == 3
            and snapshot[0] == str(adapter.account)
            and isinstance(snapshot[2], dict)
            and snapshot[2].get("complete") is True
            and isinstance(snapshot[2].get("groupMemberInfo"), list)
            and (
                state is None
                or isinstance(state, dict)
                and state.get("complete") is True
            )
            and not getattr(adapter, "member_refresh_failures", {}).get(group)
        ):
            visible = {user for user, _name, _count in entries}
            for row in snapshot[2]["groupMemberInfo"]:
                if not isinstance(row, dict):
                    continue
                user = str(row.get("userId", ""))
                if user not in visible:
                    continue
                if user in seen:
                    names[user] = ""
                    continue
                seen.add(user)
                peer = mapping.get(user, "")
                if not peer or str(row.get("nimId", "")) != peer:
                    continue
                for field in (
                    "groupMemberNick",
                    "nick",
                    "groupNick",
                    "cardName",
                    "userNick",
                    "userName",
                ):
                    name = _display_name(row.get(field), user, peer)
                    if name:
                        names[user] = name
                        break
        lines = []
        for index, (user, recorded_name, count) in enumerate(entries, 1):
            name = (
                names.get(user)
                or _display_name(recorded_name, user, mapping.get(user, ""))
                or "未获取昵称"
            )
            lines.append(f"{index}:{name}（有效发言{count}条）")
        text = "今日发言排行\n\n" + ("\n".join(lines) or "暂无记录")
        return MessageChain([Plain(text)]) if native_mentions else text
