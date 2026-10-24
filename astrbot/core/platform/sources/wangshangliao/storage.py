"""Instance-scoped encrypted credentials and transactional message ledger."""

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

import aiosqlite
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .database import AsyncDatabase
from .wire import ProtocolError

_directory_lock = threading.Lock()


def instance_dir(instance: str) -> Path:
    """Resolve a non-traversable directory for a platform instance."""
    from astrbot.core import astrbot_config

    isolated = os.environ.get("ASTRBOT_WSL_OPEN") == "1"
    integrated = (
        astrbot_config.get("wangshangliao_open", {}).get("mode") == "integrated"
    )
    root = Path(get_astrbot_data_path()) / "platform_data" / "wangshangliao"
    if isolated or integrated:
        account = next(
            (
                str(bot.get("account_id", ""))
                for bot in astrbot_config.get("platform", [])
                if bot.get("id") == instance and bot.get("type") == "wangshangliao"
            ),
            "",
        )
        if account:
            if isolated:
                instance = "open-account/" + account
            else:
                with _directory_lock:
                    bindings = root / "account-roots.json"
                    if bindings.is_symlink():
                        raise ProtocolError("account_directory_binding")
                    index = (
                        json.loads(bindings.read_text()) if bindings.exists() else {}
                    )
                    if (
                        not isinstance(index, dict)
                        or len(index) > 1000
                        or any(
                            not isinstance(a, str)
                            or not a.isascii()
                            or not a.isdigit()
                            or not isinstance(d, str)
                            or len(d) != 64
                            or any(c not in "0123456789abcdef" for c in d)
                            for a, d in index.items()
                        )
                    ):
                        raise ProtocolError("account_directory_binding")
                    if account not in index:
                        index[account] = hashlib.sha256(instance.encode()).hexdigest()
                        root.mkdir(parents=True, exist_ok=True, mode=0o700)
                        temporary = root / (
                            "account-roots." + secrets.token_hex(8) + ".tmp"
                        )
                        fd = os.open(
                            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                        try:
                            with os.fdopen(fd, "w") as stream:
                                json.dump(index, stream)
                                stream.flush()
                                os.fsync(stream.fileno())
                            temporary.replace(bindings)
                        finally:
                            temporary.unlink(missing_ok=True)
                    return root / index[account]
    return root / hashlib.sha256(instance.encode()).hexdigest()


class Vault:
    """Persist credentials with instance-bound authenticated encryption."""

    def __init__(self, instance: str, root: Path | None = None):
        from astrbot.core import astrbot_config

        # Login capabilities remain instance-bound even when public business
        # ledgers use stable account directories after an instance is recreated.
        transport_root = (
            Path(get_astrbot_data_path())
            / "platform_data"
            / "wangshangliao"
            / hashlib.sha256(instance.encode()).hexdigest()
        )
        self.root = root or (
            transport_root
            if os.environ.get("ASTRBOT_WSL_OPEN") == "1"
            or (
                astrbot_config.get("wangshangliao_open", {}).get("mode") == "integrated"
                and any(
                    b.get("id") == instance and b.get("type") == "wangshangliao"
                    for b in astrbot_config.get("platform", [])
                )
            )
            else instance_dir(instance)
        )
        self.instance = instance

    def save(self, value: dict) -> None:
        """Atomically save one session; keep key and ciphertext private."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.root.chmod(0o700)
        key_path = self.root / "session.key"
        if key_path.is_symlink():
            raise ProtocolError("vault_path")
        if not key_path.exists():
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(AESGCM.generate_key(bit_length=256))
                stream.flush()
                os.fsync(stream.fileno())
        key = key_path.read_bytes()
        nonce = secrets.token_bytes(12)
        encrypted = nonce + AESGCM(key).encrypt(
            nonce, json.dumps(value).encode(), self.instance.encode()
        )
        temporary = self.root / f"session.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.root / "session.sealed")
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> dict | None:
        """Return a decrypted session only to the backend runtime."""
        path = self.root / "session.sealed"
        if not path.exists():
            return None
        try:
            if (
                path.is_symlink()
                or (self.root / "session.key").is_symlink()
                or path.stat().st_size > 65536
            ):
                raise ValueError
            encrypted = path.read_bytes()
            key = (self.root / "session.key").read_bytes()
            return json.loads(
                AESGCM(key).decrypt(
                    encrypted[:12], encrypted[12:], self.instance.encode()
                )
            )
        except Exception:
            raise ProtocolError("session_invalid") from None

    def clear(self) -> None:
        """Remove credentials while preserving audit and deduplication records."""
        (self.root / "session.sealed").unlink(missing_ok=True)


class Ledger:
    """Persist incoming messages before ACK and outgoing intent before sending."""

    def __init__(self, path: Path):
        self.path = path
        self.db = None
        self.ingress_lock = asyncio.Lock()
        self.recall_wakeup = asyncio.Event()
        self.review_groups = set()
        self.rejected_count = 0

    async def open(self) -> None:
        """Initialize tables and quarantine interrupted work on restart."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS inbox(account TEXT, team TEXT, message TEXT, payload TEXT, state TEXT, PRIMARY KEY(account,team,message));
            CREATE TABLE IF NOT EXISTS outbox(key TEXT PRIMARY KEY, fingerprint TEXT, nonce TEXT UNIQUE, state TEXT, server_id TEXT);
            CREATE TABLE IF NOT EXISTS sync(account TEXT PRIMARY KEY, cursor TEXT);
            CREATE TABLE IF NOT EXISTS observed_recalls(
                account TEXT NOT NULL, group_id TEXT NOT NULL,
                notice_id TEXT NOT NULL, team TEXT NOT NULL,
                message_id TEXT NOT NULL, client TEXT NOT NULL,
                author TEXT NOT NULL, operator TEXT NOT NULL,
                message_time TEXT NOT NULL, recalled_at TEXT NOT NULL,
                received_at REAL NOT NULL,
                PRIMARY KEY(account,notice_id));
            CREATE INDEX IF NOT EXISTS observed_recalls_group
                ON observed_recalls(account,group_id,received_at);
            CREATE TABLE IF NOT EXISTS reply_recalls(
                key TEXT PRIMARY KEY, account TEXT NOT NULL, group_id TEXT NOT NULL,
                team TEXT NOT NULL, peer TEXT NOT NULL, client TEXT NOT NULL,
                server_id TEXT NOT NULL, message_time TEXT NOT NULL,
                deadline REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                sender TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS reply_recalls_due
                ON reply_recalls(deadline) WHERE state='pending';
            CREATE TABLE IF NOT EXISTS review_holds(
                account TEXT,team TEXT,created REAL,reason TEXT,
                PRIMARY KEY(account,team));
            UPDATE inbox SET state='needs_review' WHERE state='processing';
            UPDATE outbox SET state='unknown' WHERE state='sending';
            UPDATE reply_recalls SET state='unknown' WHERE state='sending';
        """)
        async with self.db.execute("PRAGMA table_info(reply_recalls)") as cursor:
            recall_columns = {row[1] for row in await cursor.fetchall()}
        if "sender" not in recall_columns:
            await self.db.execute(
                "ALTER TABLE reply_recalls ADD COLUMN sender TEXT NOT NULL DEFAULT ''"
            )
        async with self.db.execute("PRAGMA table_info(inbox)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        for column in ("received_at", "completed_at", "pruned_at"):
            if column not in columns:
                await self.db.execute(f"ALTER TABLE inbox ADD COLUMN {column} REAL")
        if "sender" not in columns:
            await self.db.execute(
                "ALTER TABLE inbox ADD COLUMN sender TEXT NOT NULL DEFAULT ''"
            )
            await self.db.execute(
                "UPDATE inbox SET sender=COALESCE(CAST(json_extract(payload,'$.sender') AS TEXT),'') "
                "WHERE json_valid(payload) AND team NOT LIKE 'private/%'"
            )
        if "dispatch_class" not in columns:
            await self.db.execute(
                "ALTER TABLE inbox ADD COLUMN dispatch_class TEXT NOT NULL DEFAULT 'message'"
            )
        now = time.time()
        await self.db.execute(
            "UPDATE inbox SET received_at=? WHERE received_at IS NULL", (now,)
        )
        await self.db.execute(
            "UPDATE inbox SET completed_at=? WHERE completed_at IS NULL "
            "AND state NOT IN ('pending','processing','needs_review')",
            (now,),
        )
        await self.db.executescript("""
            CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(account,team) WHERE state='pending';
            CREATE INDEX IF NOT EXISTS inbox_pending_member ON inbox(account,team,sender) WHERE state='pending';
            CREATE INDEX IF NOT EXISTS inbox_retention ON inbox(completed_at) WHERE pruned_at IS NULL;
            CREATE INDEX IF NOT EXISTS inbox_ranking_window ON inbox(account,team,received_at);
            CREATE INDEX IF NOT EXISTS inbox_ingress_recent ON inbox(account,received_at DESC);
            CREATE INDEX IF NOT EXISTS inbox_admission ON inbox(account,state,sender,team);
            CREATE INDEX IF NOT EXISTS inbox_conversation ON inbox(account,team);
            CREATE TABLE IF NOT EXISTS ledger_maintenance(
                id INTEGER PRIMARY KEY CHECK(id=1), installed_at REAL NOT NULL,
                retention_approved INTEGER NOT NULL DEFAULT 0);
        """)
        async with self.db.execute("SELECT team FROM review_holds") as cursor:
            self.review_groups = {row[0] for row in await cursor.fetchall()}
        await self.db.execute(
            "INSERT OR IGNORE INTO ledger_maintenance(id,installed_at) VALUES(1,?)",
            (now,),
        )
        await self.db.commit()
        if os.name != "nt":
            self.path.chmod(0o600)

    async def observe_recall(self, account: str, group: str, notice: dict) -> bool:
        """Persist deduplicated native recall metadata without creating actions.

        Args:
            account: Current authenticated business account.
            group: Group resolved from the authenticated account's catalog.
            notice: Validated native group recall metadata.

        Returns:
            Whether a previously unseen notification was recorded.
        """
        async with self.ingress_lock:
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO observed_recalls VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    group,
                    notice["notice_id"],
                    notice["team"],
                    notice["message_id"],
                    notice["client"],
                    notice["author"],
                    notice["operator"],
                    notice["message_time"],
                    notice["recalled_at"],
                    time.time(),
                ),
            )
            inserted = cursor.rowcount == 1
            await cursor.close()
            await self.db.execute(
                "DELETE FROM observed_recalls WHERE rowid IN "
                "(SELECT rowid FROM observed_recalls WHERE received_at<? "
                "ORDER BY received_at LIMIT 1000)",
                (time.time() - 90 * 86400,),
            )
            await self.db.commit()
            return inserted

    async def ingest(
        self,
        account: str,
        team: str,
        message: str,
        payload: dict,
        *,
        observe=None,
        state="pending",
        authorize=None,
    ) -> bool:
        """Commit new evidence before ACK, observing it only once.

        Args:
            account: Authenticated account.
            team: Durable group or private session identifier.
            message: Stable upstream message ID.
            payload: Trusted adapter envelope.
            observe: Optional synchronous business observer enriching a copy.
            state: Initial routing state, committed together with the envelope.
            authorize: Optional synchronous authority for observer-free ignored_bot
                evidence, checked in an isolated transaction.

        Returns:
            Whether this was a new message.
        """
        async with self.ingress_lock:
            if authorize is not None:
                if state != "ignored_bot" or observe is not None:
                    raise ValueError("authorized_ingress_requires_ignored_bot")
                return await self._ingest_witness(
                    account, team, message, payload, authorize
                )
            async with self.db.execute(
                "SELECT 1 FROM inbox WHERE account=? AND team=? AND message=?",
                (account, team, message),
            ) as cursor:
                if await cursor.fetchone():
                    return False
            received_at = time.time()
            payload = dict(payload)
            sender = (
                str(payload.get("transport_peer", ""))
                if team.startswith("private/")
                else str(payload.get("sender", ""))
            )
            if state == "pending":
                private = team.startswith("private/")
                async with self.db.execute(
                    "SELECT COUNT(*),SUM(team LIKE 'private/%'),"
                    "SUM(team LIKE 'private/%' AND sender=?) "
                    "FROM inbox WHERE account=? AND state IN ('pending','processing')",
                    (sender, account),
                ) as cursor:
                    total, private_count, personal = await cursor.fetchone()
                private_count, personal = private_count or 0, personal or 0
                control = private and payload.get("text", "").strip() in {
                    "/chat",
                    "/exit",
                }
                if private and len(payload.get("text", "").encode()) > 16384:
                    state = "oversized"
                elif (
                    private
                    and (
                        private_count >= 256 + int(control) * 2
                        or personal >= 4 + int(control)
                    )
                    or not private
                    and total - private_count >= 4096
                ):
                    state = "queue_full"
                    if not private:
                        self.review_groups.add(team)
                        await self.db.execute(
                            "INSERT OR IGNORE INTO review_holds VALUES(?,?,?,'queue_full')",
                            (account, team, received_at),
                        )
                if state != "pending":
                    self.rejected_count += 1
                    payload = {
                        k: v
                        for k, v in payload.items()
                        if k
                        in {"sender", "transport_peer", "created_at", "recall_route"}
                    }
                    payload["reason"] = state
                else:
                    if team in self.review_groups:
                        payload["_audit_incomplete"] = True
                    if observe:
                        observe(payload, received_at)
            dispatch_class = (
                "command" if payload.get("_dispatch_class") == "command" else "message"
            )
            if team.startswith("private/") and (
                payload.get("text", "").strip() in {"/chat", "/exit"}
                or payload.get("_ai_busy")
            ):
                dispatch_class = "control"
            await self.db.execute(
                "INSERT OR IGNORE INTO inbox(account,team,message,payload,state,received_at,sender,completed_at,dispatch_class) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    team,
                    message,
                    json.dumps(payload),
                    state,
                    received_at,
                    sender,
                    None if state == "pending" else received_at,
                    dispatch_class,
                ),
            )
            await self.db.commit()
            return True

    async def _ingest_witness(self, account, team, message, payload, authorize):
        """Commit bounded evidence without sharing other writers' transactions.

        Args:
            account: Authenticated observer account.
            team: Native evidence session identifier.
            message: Stable upstream message ID.
            payload: Trusted evidence envelope.
            authorize: Synchronous frozen authority, rechecked at commit admission.

        Returns:
            Whether new evidence was committed.

        Raises:
            ProtocolError: If the frozen authority is revoked.
            sqlite3.Error: If writer admission or persistence fails.
            asyncio.CancelledError: If cancelled before commit admission.
        """
        authorize()

        def connect():
            connection = sqlite3.connect(self.path)
            try:
                connection.execute("PRAGMA synchronous=FULL")
            except BaseException:
                connection.close()
                raise
            return connection

        # Own the writer before reading; cancellation drains and rolls back only
        # this connection. AsyncDatabase also authorizes at commit admission.
        async with AsyncDatabase(connect, write=True, guard=authorize) as db:
            if await db.fetchone(
                "SELECT 1 FROM inbox WHERE account=? AND team=? AND message=?",
                (account, team, message),
            ):
                return False
            received_at = time.time()
            payload = dict(payload)
            sender = str(
                payload.get("transport_peer", "")
                if team.startswith("private/")
                else payload.get("sender", "")
            )
            dispatch_class = (
                "command" if payload.get("_dispatch_class") == "command" else "message"
            )
            if team.startswith("private/") and (
                payload.get("text", "").strip() in {"/chat", "/exit"}
                or payload.get("_ai_busy")
            ):
                dispatch_class = "control"
            authorize()
            await db.execute(
                "INSERT OR IGNORE INTO inbox"
                "(account,team,message,payload,state,received_at,sender,completed_at,dispatch_class) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    team,
                    message,
                    json.dumps(payload),
                    "ignored_bot",
                    received_at,
                    sender,
                    received_at,
                    dispatch_class,
                ),
            )
            authorize()
            return True

    async def mark(self, account: str, team: str, message: str, state: str) -> None:
        """Persist processing completion or a manual-review boundary."""
        await self.db.execute(
            "UPDATE inbox SET state=?,completed_at=? WHERE account=? AND team=? AND message=?",
            (
                state,
                None
                if state in {"pending", "processing", "needs_review"}
                else time.time(),
                account,
                team,
                message,
            ),
        )
        await self.db.commit()

    async def resume_review(self, account: str, team: str, *, guard) -> None:
        """Clear a reviewed evidence-loss hold only after the lane drains and ages out.

        Args:
            account: Verified current robot account.
            team: Exact authorized business group.
            guard: Frozen account and configuration check.

        Raises:
            ProtocolError: If pending evidence or a recent gap makes recovery unsafe.
        """
        async with self.ingress_lock:
            guard()
            async with self.db.execute(
                "SELECT created FROM review_holds WHERE account=? AND team=?",
                (account, team),
            ) as cursor:
                hold = await cursor.fetchone()
            async with self.db.execute(
                "SELECT 1 FROM inbox WHERE account=? AND team=? "
                "AND state IN ('pending','processing','needs_review') LIMIT 1",
                (account, team),
            ) as cursor:
                unresolved = await cursor.fetchone()
            if hold is None or time.time() - hold[0] < 300 or unresolved:
                raise ProtocolError("review_unresolved")
            guard()
            await self.db.execute(
                "DELETE FROM review_holds WHERE account=? AND team=?", (account, team)
            )
            await self.db.commit()
            self.review_groups.discard(team)

    async def prune(self) -> int:
        """Remove expired bodies, preserving identities and uncertain work.

        Returns:
            Number of rows pruned in this bounded batch.
        """
        now = time.time()
        await self.db.execute(
            "DELETE FROM inbox WHERE state IN ('queue_full','oversized') AND completed_at<?",
            (now - 3600,),
        )
        await self.db.commit()
        async with self.db.execute(
            "SELECT 1 FROM ledger_maintenance WHERE id=1 AND retention_approved=1 AND installed_at<=?",
            (now - 86400,),
        ) as cursor:
            if not await cursor.fetchone():
                return 0
        async with self.db.execute(
            "SELECT rowid,payload FROM inbox WHERE completed_at<=? AND pruned_at IS NULL "
            "AND state NOT IN ('pending','processing','needs_review') LIMIT 500",
            (now - 30 * 86400,),
        ) as cursor:
            rows = await cursor.fetchall()
        for rowid, encoded in rows:
            payload = json.loads(encoded)
            if not isinstance(payload, dict):
                raise ValueError("invalid_retention_payload")
            # Keep only metadata needed for routing, audit and recall validation.
            payload = {
                key: value
                for key, value in payload.items()
                if key in {"sender", "created_at", "recall_route", "format", "reason"}
            }
            await self.db.execute(
                "UPDATE inbox SET payload=?,pruned_at=? WHERE rowid=? "
                "AND state NOT IN ('pending','processing','needs_review') AND completed_at<=?",
                (json.dumps(payload), now, rowid, now - 30 * 86400),
            )
        await self.db.commit()
        return len(rows)

    async def reserve(self, key: str, text: str) -> tuple[str, str]:
        """Reserve an outgoing nonce and key atomically; never retry uncertain work."""
        digest = hashlib.sha256(text.encode()).hexdigest()
        async with self.db.execute(
            "SELECT fingerprint,nonce,state FROM outbox WHERE key=?", (key,)
        ) as cursor:
            existing = await cursor.fetchone()
        if existing:
            if existing[0] != digest:
                raise ProtocolError("send_key_conflict")
            return existing[1], existing[2]
        nonce = str(secrets.randbits(64))
        await self.db.execute(
            "INSERT INTO outbox VALUES(?,?,?,'sending','')", (key, digest, nonce)
        )
        await self.db.commit()
        return nonce, "new"

    async def finish(
        self, key: str, state: str, server_id: str = "", *, recall: tuple | None = None
    ) -> None:
        """Persist acceptance and optional self-recall intent in one commit.

        Args:
            key: Stable outbox operation identifier.
            state: Provider outcome.
            server_id: Accepted native message ID.
            recall: Account, group, team, peer, client, message time and deadline.
        """
        await self.db.execute(
            "UPDATE outbox SET state=?,server_id=? WHERE key=?", (state, server_id, key)
        )
        if state == "accepted" and recall is not None:
            account, group, team, peer, client, stamp, deadline = recall
            await self.db.execute(
                "INSERT OR IGNORE INTO reply_recalls"
                "(key,account,group_id,team,peer,client,server_id,message_time,deadline)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (key, account, group, team, peer, client, server_id, stamp, deadline),
            )
        await self.db.commit()
        if state == "accepted" and recall is not None:
            self.recall_wakeup.set()

    async def receipt(self, key: str) -> dict | None:
        """Read durable provider evidence without issuing another send.

        Args:
            key: Instance-scoped outgoing idempotency key.

        Returns:
            Status and provider message ID, or None for an unknown key.
        """
        async with self.db.execute(
            "SELECT state,server_id FROM outbox WHERE key=?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return {"status": row[0], "server_id": row[1]} if row else None

    async def close(self) -> None:
        """Close the database after all producers and consumers have stopped."""
        async with self.ingress_lock:
            connection = self.db
            self.db = None
            if connection is not None:
                await connection.close()
