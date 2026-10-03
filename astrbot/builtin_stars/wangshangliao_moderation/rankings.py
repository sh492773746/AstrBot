"""Read-only daily text rankings from authenticated, deduplicated inbox events."""

import asyncio
import hashlib
import json
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .syntax import recognize_command


class DailyRankings:
    """Bound ranking scans and coalesce repeated queries without changing ledgers."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.cache = {}

    async def read(
        self,
        adapter,
        group: str,
        sender: str,
        *,
        page: int = 1,
        native_mentions: bool = False,
    ) -> str | MessageChain:
        """Return a twenty-member page of today's rankings.

        Args:
            adapter: Native adapter owning the authenticated inbox.
            group: Enabled business group, never a displayed group number.
            sender: Authenticated caller's business UID.
            page: Positive one-based page number.
            native_mentions: Render verified group identities as native mentions.

        Returns:
            A concise text or native message chain based on received evidence.

        Raises:
            ProtocolError: If scope, message storage or a bounded scan is invalid.
        """
        if type(page) is not int or not 1 <= page <= 99999:
            raise ProtocolError("ranking_page")
        if (
            not adapter.config.get("enable", True)
            or group not in adapter.config.get("enabled_groups", [])
            or not adapter.account
        ):
            raise ProtocolError("ranking_scope")
        now = time.time()
        zone = ZoneInfo("Asia/Shanghai")
        start = datetime.fromtimestamp(now, zone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=1)
        date = start.strftime("%Y-%m-%d")
        key = (adapter.config["id"], adapter.account, group, date)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() < cached[0]:
                ranked = cached[1]
            else:
                path = Path(adapter.ledger.path)
                if not path.is_file() or path.is_symlink():
                    raise ProtocolError("ranking_unavailable")
                violations = set()
                audit_path = instance_dir(adapter.config["id"]) / "moderation.sqlite3"
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
                                "AND status IN ('claimed','warned','muted','kicked','unknown')",
                                (adapter.account, group, start.timestamp()),
                            ) as cursor:
                                violations = {row[0] for row in await cursor.fetchall()}
                messages = []
                scanned = 0
                async with aiosqlite.connect(
                    f"{path.resolve().as_uri()}?mode=ro", uri=True
                ) as db:
                    async with db.execute(
                        "SELECT message,payload,state FROM inbox "
                        "WHERE account=? AND team=? AND received_at>=?",
                        (adapter.account, group, start.timestamp()),
                    ) as cursor:
                        while rows := await cursor.fetchmany(500):
                            scanned += len(rows)
                            if scanned > 100000:
                                raise ProtocolError("ranking_limit")
                            for mid, encoded, state in rows:
                                if state not in {"pending", "processing", "processed"}:
                                    continue
                                if mid in violations:
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
                                except (
                                    ValueError,
                                    TypeError,
                                    KeyError,
                                    AttributeError,
                                ):
                                    continue
                                if (
                                    not user.isascii()
                                    or not user.isdigit()
                                    or len(user) > 20
                                    or int(user) <= 0
                                    or user == adapter.account
                                    or is_managed_account(user)
                                    or not isinstance(text, str)
                                    or len(text) > 16384
                                    or recognize_command(text)
                                    or text.strip().startswith("/")
                                ):
                                    continue
                                if stamp > 100000000000:
                                    stamp /= 1000
                                if (
                                    not start.timestamp()
                                    <= stamp
                                    < min(end.timestamp(), now + 1)
                                ):
                                    continue
                                # Strip only a transport-verified leading bot mention.
                                encoded_text = text.encode(
                                    "utf-16-le", errors="replace"
                                )
                                for span in payload.get("mention_spans", []):
                                    stop = span.get("end", 0)
                                    if (
                                        span.get("uid") == str(adapter.nim_account)
                                        and span.get("start") == 0
                                        and type(stop) is int
                                        and 0 < stop * 2 <= len(encoded_text)
                                        and encoded_text[: stop * 2]
                                        .decode("utf-16-le", errors="replace")
                                        .rstrip()
                                        == "@" + span.get("nick", "")
                                    ):
                                        text = encoded_text[stop * 2 :].decode(
                                            "utf-16-le", errors="replace"
                                        )
                                        break
                                normalized = "".join(
                                    char
                                    for char in unicodedata.normalize(
                                        "NFKC", text
                                    ).casefold()
                                    if not char.isspace()
                                    and unicodedata.category(char) != "Cf"
                                )
                                if recognize_command(text) or not any(
                                    char.isalnum() for char in normalized
                                ):
                                    continue
                                name = "".join(
                                    char
                                    for char in str(payload.get("name") or user)
                                    if ord(char) >= 32
                                    and unicodedata.category(char)
                                    not in {"Cf", "Zl", "Zp"}
                                )[:32]
                                digest = hashlib.sha256(normalized.encode()).digest()
                                messages.append((stamp, str(mid), user, name, digest))
                counts, names, last_counted, repeated, reached = {}, {}, {}, {}, {}
                for stamp, _mid, user, name, digest in sorted(messages):
                    names[user] = name or user
                    previous = repeated.get((user, digest), float("-inf"))
                    repeated[(user, digest)] = stamp
                    if stamp - previous < 300:
                        continue
                    if stamp - last_counted.get(user, float("-inf")) < 30:
                        continue
                    counts[user] = counts.get(user, 0) + 1
                    last_counted[user] = reached[user] = stamp
                ranked = [
                    (user, names[user], count)
                    for user, count in sorted(
                        counts.items(),
                        key=lambda pair: (-pair[1], reached[pair[0]], pair[0]),
                    )
                ]
                self.cache = {
                    entry: value
                    for entry, value in self.cache.items()
                    if time.monotonic() < value[0]
                }
                self.cache[key] = (time.monotonic() + 10, ranked)
        if (
            group not in adapter.config.get("enabled_groups", [])
            or not adapter.config.get("enable", True)
            or adapter.account != key[1]
        ):
            raise ProtocolError("ranking_scope")
        offset = (page - 1) * 20
        entries = ranked[offset : offset + 20]
        header = f"\U0001f4ac 今日发言排行\n第 {page} 页 / 每页 20 条\n\n"
        if not native_mentions:
            return header + (
                "\n".join(
                    f"{index}. {name} - {count} 条"
                    for index, (_user, name, count) in enumerate(entries, offset + 1)
                )
                or "暂无记录"
            )
        chain = [Plain(header)]
        mapping = getattr(adapter, "members", {}).get(group, {})
        for index, (user, name, count) in enumerate(entries, offset + 1):
            chain.append(Plain(f"{index}. "))
            peer = mapping.get(user, "")
            if (
                isinstance(peer, str)
                and peer.isascii()
                and peer.isdigit()
                and 0 < int(peer) < 1 << 32
            ):
                chain.append(At(qq=user, name=name))
                chain.append(Plain(f"- {count} 条"))
            else:
                chain.append(Plain(f"{name} - {count} 条"))
            if index != offset + len(entries):
                chain.append(Plain("\n"))
        if not entries:
            chain.append(Plain("暂无记录"))
        return MessageChain(chain)
