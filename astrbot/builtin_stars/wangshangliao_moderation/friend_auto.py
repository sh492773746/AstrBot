"""Account-bound, paced friend approvals without chat or AI entry points."""

import asyncio
import math
import sqlite3
import time

from astrbot.api import logger
from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.business_http import request_business
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.diagnostics import (
    ERROR_LABELS,
    reference,
)
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

LIST_ROUTE = "/v1/friend/friend-apply-list"
APPROVE_ROUTE = "/v1/friend/friend-apply-handler"
SCAN_SECONDS = 60
WELCOME_SCAN_SECONDS = 10
REQUEST_SECONDS = 3
ACTION_SECONDS = 30
READ_BACKOFF_SECONDS = 300
WELCOME_DEFERRED = "WELCOME_STATE_DEFERRED"


def applications(data):
    """Decode authenticated provider records, never messages or display names.

    Args:
        data: The signed business application's list response.

    Returns:
        Bounded application records keyed by the provider's log identifier.

    Raises:
        ProtocolError: If records or direction/identity evidence are ambiguous.
    """
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("list"), list)
        or len(data["list"]) > 1000
    ):
        raise ProtocolError("friend_application_shape")
    result = {}
    for item in data["list"]:
        if not isinstance(item, dict) or not isinstance(item.get("user"), dict):
            raise ProtocolError("friend_application_shape")
        provider_id, uid = item.get("flId"), item["user"].get("uid")
        identity = provider_id
        if (
            isinstance(provider_id, str)
            and 0 < len(provider_id) <= 19
            and provider_id.isascii()
            and provider_id.isdecimal()
            and provider_id[0] != "0"
        ):
            # The provider serializes int64 log identifiers as decimal strings.
            identity = int(provider_id)
        if (
            type(identity) is not int
            or not 0 < identity < 1 << 63
            or type(uid) is not int
            or not 0 < uid < 1 << 31
            or identity in result
            or item.get("direction") not in {"FROM_HIM", "FROM_ME"}
            or item.get("state")
            not in {
                "FRIENDLOG_STATE_UNKNOWN",
                "FRIENDLOG_STATE_AGREE",
                "FRIENDLOG_STATE_REFUSE",
                "FRIENDLOG_STATE_PROCESSING",
                "FRIENDLOG_STATE_DELETE",
                "FRIENDLOG_STATE_EXPIRE",
            }
        ):
            raise ProtocolError("friend_application_identity")
        circle = item.get("friendCircle")
        if (
            not isinstance(circle, dict)
            or set(circle) != {"notSeeMe", "notSeeHim"}
            or any(type(value) is not bool for value in circle.values())
        ):
            raise ProtocolError("friend_application_privacy")
        result[identity] = {
            "id": identity,
            "provider_id": provider_id,
            "uid": uid,
            "direction": item["direction"],
            "state": item["state"],
            "circle": circle,
            "peer": str(item["user"].get("nimId", "")),
        }
    return result


class FriendAutoApproval:
    """Persist intent before a single approval and halt on uncertain outcomes."""

    def __init__(self, context):
        self.context = context
        self.locks = {}
        self.cooldowns = {}
        self.business_settings = None
        self.active = False
        self.versions = {}
        self.welcome = None

    def database(self, instance):
        """Open only the selected instance's additive, account-scoped store.

        Args:
            instance: Exact configured bot instance.

        Returns:
            A connection owned and closed by AsyncDatabase.
        """
        root = instance_dir(instance)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "friend_approval.sqlite3"
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        path.chmod(0o600)
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS approval_settings(
                account TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1, paused TEXT NOT NULL DEFAULT '',
                last_scan REAL NOT NULL DEFAULT 0, last_action REAL NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS approval_attempts(
                account TEXT NOT NULL, request_id INTEGER NOT NULL, member INTEGER NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(account,request_id));
            CREATE TABLE IF NOT EXISTS welcome_observations(
                account TEXT NOT NULL, request_id INTEGER NOT NULL,
                member INTEGER NOT NULL, peer TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY(account,request_id));
        """)
        columns = {
            r["name"] for r in db.execute("PRAGMA table_info(approval_settings)")
        }
        for column in (
            "retry_at",
            "read_failures",
            "last_request",
            "welcome_initialized",
            "welcome_checked_at",
        ):
            if column not in columns:
                db.execute(
                    f"ALTER TABLE approval_settings ADD COLUMN {column} REAL NOT NULL DEFAULT 0"
                )
        return db

    async def read_applications(self, adapter, guard):
        """Share durable read pacing and backoff without releasing approval holds.

        Args:
            adapter: Account whose approval lock the caller already holds.
            guard: Frozen account and caller authority check.

        Returns:
            Validated records, or None while a transient failure defers reads.

        Raises:
            ProtocolError: On rejected reads, invalid records or revoked authority.
        """
        instance, account = adapter.meta().id, str(adapter.account)
        async with AsyncDatabase(lambda: self.database(instance), guard=guard) as db:
            settings = await db.fetchone(
                "SELECT last_request,retry_at FROM approval_settings WHERE account=?",
                (account,),
            )
        if time.time() < settings["retry_at"]:
            return None
        wait = settings["last_request"] + REQUEST_SECONDS - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        guard()
        async with AsyncDatabase(
            lambda: self.database(instance), write=True, guard=guard
        ) as db:
            await db.execute(
                "UPDATE approval_settings SET last_request=? WHERE account=?",
                (time.time(), account),
            )
        try:
            try:
                response = await request_business(
                    adapter.business, LIST_ROUTE, {"lastId": 0}, authorize=guard
                )
            finally:
                # Persist completion even after revocation or repeated cancellation.
                completed_at = time.time()
                cancelled = False
                while True:
                    try:
                        async with AsyncDatabase(
                            lambda: self.database(instance), write=True
                        ) as db:
                            await db.execute(
                                "UPDATE approval_settings "
                                "SET last_request=MAX(last_request,?) WHERE account=?",
                                (completed_at, account),
                            )
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
            records = applications(response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if str(exc) in {
                "database_authority_changed",
                "friend_approval_authority_changed",
            }:
                raise
            retryable = isinstance(exc, (TimeoutError, OSError)) or (
                isinstance(exc, BusinessError)
                and (
                    str(exc) in {"transport", "rate_limited"}
                    or str(exc) == "http_rejected"
                    and 500 <= exc.number < 600
                )
            )
            code = (
                str(exc)
                if isinstance(exc, ProtocolError)
                and 0 < len(str(exc)) <= 64
                and all(c.isascii() and (c.islower() or c == "_") for c in str(exc))
                else "friend_approval_unknown"
            )
            async with AsyncDatabase(lambda: self.database(instance), write=True) as db:
                row = await db.fetchone(
                    "SELECT read_failures FROM approval_settings WHERE account=?",
                    (account,),
                )
                failures = min(8, int(row["read_failures"]) + 1)
                delay = min(READ_BACKOFF_SECONDS, REQUEST_SECONDS * 5 ** (failures - 1))
                if (
                    isinstance(exc, BusinessError)
                    and str(exc) == "rate_limited"
                    and type(exc.retry_after) in (int, float)
                    and math.isfinite(exc.retry_after)
                ):
                    # Match the HTTP boundary's one-day Retry-After ceiling.
                    delay = max(delay, min(86400, exc.retry_after))
                await db.execute(
                    "UPDATE approval_settings SET read_failures=?,"
                    "retry_at=MAX(retry_at,?),"
                    "paused=CASE WHEN paused='' THEN ? ELSE paused END WHERE account=?",
                    (
                        failures,
                        time.time() + delay,
                        "" if retryable else code,
                        account,
                    ),
                )
            # Log only fixed labels, opaque identities and bounded numeric evidence.
            error = (
                code
                if code
                in ERROR_LABELS
                | {
                    "friend_approval_unknown",
                    "friend_application_shape",
                    "friend_application_identity",
                    "friend_application_privacy",
                    "response_limit",
                    "body_limit",
                    "body_authentication_or_compression",
                }
                else "other"
            )
            number = exc.number if isinstance(exc, BusinessError) else None
            submitted = getattr(exc, "submitted", None)
            logger.warning(
                "Friend application read failed: stage=friend_application_read "
                "instance=%s account=%s error=%s provider_code=%s "
                "retryable=%s failures=%s backoff_seconds=%s submitted=%s",
                reference(instance),
                reference(account),
                error,
                number
                if type(number) is int and -(1 << 31) <= number < 1 << 31
                else "-",
                retryable,
                failures,
                delay,
                submitted if type(submitted) is bool else "unknown",
            )
            if retryable:
                return None
            raise
        async with AsyncDatabase(
            lambda: self.database(instance), write=True, guard=guard
        ) as db:
            await db.execute(
                "UPDATE approval_settings SET read_failures=0,retry_at=0 WHERE account=?",
                (account,),
            )
        return records

    async def notify_welcomes(self, adapter):
        """Notice one newly accepted friend without approving or replaying mutations.

        Args:
            adapter: Online native account, even when auto-approval is paused/off.

        Returns:
            None. Existing friends are baselined without receiving any messages.
        """
        if (
            not self.active
            or self.welcome is None
            or adapter.connection_state != "online"
            or not adapter.config.get("_business_active")
            or not adapter.config.get("enable", True)
            or not adapter.config.get("reply_private", True)
            or adapter.config.get("_open_paused", False)
            or not getattr(adapter, "business", None)
        ):
            return
        instance, account = adapter.meta().id, str(adapter.account)
        lock = self.locks.setdefault((instance, account), asyncio.Lock())
        if lock.locked():
            return
        async with lock:

            def authorize():
                if not self.active or adapter.config.get("_open_paused", False):
                    raise ProtocolError("friend_approval_authority_changed")

            guard = account_guard(adapter, authorize)
            async with AsyncDatabase(
                lambda: self.database(instance), write=True, guard=guard
            ) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO approval_settings(account) VALUES(?)",
                    (account,),
                )
                settings = await db.fetchone(
                    "SELECT welcome_initialized,welcome_checked_at,read_failures "
                    "FROM approval_settings WHERE account=?",
                    (account,),
                )
                if (
                    not settings["read_failures"]
                    and time.time() - settings["welcome_checked_at"]
                    < WELCOME_SCAN_SECONDS
                ):
                    return
                await db.execute(
                    "UPDATE approval_settings SET welcome_checked_at=? WHERE account=?",
                    (time.time(), account),
                )
            try:
                guard()
                if not self.active or adapter.config.get("_open_paused", False):
                    return
                records = await self.read_applications(adapter, guard)
                if records is None:
                    return
                candidate = None
                async with AsyncDatabase(
                    lambda: self.database(instance), write=True, guard=guard
                ) as db:
                    previous = {
                        row["request_id"]: row
                        for row in await db.fetchall(
                            "SELECT * FROM welcome_observations WHERE account=?",
                            (account,),
                        )
                    }
                    for identity, item in records.items():
                        if item["direction"] != "FROM_HIM" or item["uid"] == int(
                            account
                        ):
                            continue
                        old = previous.get(identity)
                        newly_accepted = (
                            (
                                settings["welcome_initialized"]
                                or old
                                and old["state"] == WELCOME_DEFERRED
                            )
                            and item["state"] == "FRIENDLOG_STATE_AGREE"
                            and (not old or old["state"] != "FRIENDLOG_STATE_AGREE")
                        )
                        if old and (
                            old["member"] != item["uid"]
                            or old["peer"]
                            and old["peer"] != item["peer"]
                        ):
                            continue
                        if newly_accepted:
                            if (
                                not item["peer"].isascii()
                                or not item["peer"].isdecimal()
                                or not 0 < int(item["peer"]) < 1 << 32
                            ):
                                continue
                            if candidate is not None:
                                continue
                            candidate = item
                        await db.execute(
                            "INSERT INTO welcome_observations VALUES(?,?,?,?,?) "
                            "ON CONFLICT(account,request_id) DO UPDATE SET "
                            "peer=excluded.peer,state=excluded.state",
                            (
                                account,
                                identity,
                                item["uid"],
                                item["peer"],
                                item["state"],
                            ),
                        )
                    await db.execute(
                        "UPDATE approval_settings SET welcome_initialized=1 WHERE account=?",
                        (account,),
                    )
                if candidate:
                    outcome = await self.welcome(
                        adapter,
                        candidate["uid"],
                        candidate["peer"],
                        candidate["id"],
                        guard,
                    )
                    if outcome == "busy":
                        # Only the pre-claim busy result proves no send was attempted.
                        async with AsyncDatabase(
                            lambda: self.database(instance), write=True, guard=guard
                        ) as db:
                            await db.execute(
                                "UPDATE welcome_observations SET state=? "
                                "WHERE account=? AND request_id=? AND member=? "
                                "AND peer=? AND state='FRIENDLOG_STATE_AGREE'",
                                (
                                    WELCOME_DEFERRED,
                                    account,
                                    candidate["id"],
                                    candidate["uid"],
                                    candidate["peer"],
                                ),
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Failed reads do not enable approvals or replay an uncertain send.
                logger.warning("Friend welcome observation was not confirmed")

    async def status(self, adapter):
        """Return local settings and outcomes without an upstream friend request.

        Args:
            adapter: Selected native bot with its current account binding.

        Returns:
            Public settings, counts and cooldown; no credentials or applicant profiles.
        """
        instance, account = adapter.meta().id, str(adapter.account)
        async with AsyncDatabase(
            lambda: self.database(instance), write=True, guard=account_guard(adapter)
        ) as db:
            await db.execute(
                "INSERT OR IGNORE INTO approval_settings(account) VALUES(?)", (account,)
            )
            row = await db.fetchone(
                "SELECT * FROM approval_settings WHERE account=?", (account,)
            )
            counts = await db.fetchone(
                "SELECT COUNT(*) AS total,"
                "SUM(state='completed') AS accepted,"
                "SUM(state IN ('sending','unknown')) AS inflight "
                "FROM approval_attempts WHERE account=?",
                (account,),
            )
        key = (instance, account)
        self.versions[key] = max(self.versions.get(key, 0), row["version"])
        return {
            "enabled": bool(row["enabled"]),
            "version": row["version"],
            "paused_reason": row["paused"],
            "last_checked_at": row["last_scan"],
            "pending": row["pending"],
            "accepted": counts["accepted"] or 0,
            "inflight": counts["inflight"] or 0,
            "retry_after": max(
                0,
                math.ceil(
                    max(
                        row["last_action"] + ACTION_SECONDS,
                        row["retry_at"],
                    )
                    - time.time()
                ),
            ),
            "read_failures": int(row["read_failures"]),
            "can_reconcile": bool(row["paused"]),
            "active": self.active
            and adapter.config.get("_business_active") is True
            and adapter.config.get("enable", True)
            and not adapter.config.get("_open_paused", False)
            and bool(getattr(adapter, "business", None))
            and getattr(adapter, "connection_state", "") == "online",
        }

    async def configure(self, adapter, enabled, expected, authorize):
        """Save a dashboard-only toggle without clearing uncertain-operation holds.

        Args:
            adapter: Current native account.
            enabled: Exact Boolean requested by an authenticated dashboard principal.
            expected: Account-specific settings version.
            authorize: Saved bot identity and configuration guard.

        Returns:
            Current public settings.

        Raises:
            ValueError: On malformed or stale settings.
        """
        if type(enabled) is not bool or type(expected) is not int:
            raise ValueError("friend_approval_settings_invalid")
        instance, account = adapter.meta().id, str(adapter.account)
        guard = account_guard(adapter, authorize)
        async with AsyncDatabase(
            lambda: self.database(instance), write=True, guard=guard
        ) as db:
            count = await db.execute(
                "UPDATE approval_settings SET enabled=?,version=version+1 "
                "WHERE account=? AND version=?",
                (int(enabled), account, expected),
            )
            if count != 1:
                raise ValueError("friend_approval_settings_changed")
        self.versions[(instance, account)] = expected + 1
        return await self.status(adapter)

    async def reconcile(self, adapter, expected, authorize):
        """Read back uncertain approvals before an authenticated, versioned resume.

        Args:
            adapter: Selected native bot.
            expected: Version displayed by total control.
            authorize: Saved bot identity and configuration check.

        Returns:
            Current approval state without issuing an approval request.

        Raises:
            ValueError: If state changed or an uncertain operation remains unresolved.
        """
        instance, account = adapter.meta().id, str(adapter.account)
        lock = self.locks.setdefault((instance, account), asyncio.Lock())
        if lock.locked():
            raise ValueError("friend_approval_busy")
        async with lock:
            guard = account_guard(adapter, authorize)
            state = await self.status(adapter)
            if type(expected) is not int or state["version"] != expected:
                raise ValueError("friend_approval_settings_changed")
            async with AsyncDatabase(
                lambda: self.database(instance), guard=guard
            ) as db:
                unresolved = await db.fetchall(
                    "SELECT request_id,member FROM approval_attempts WHERE account=? "
                    "AND state IN ('sending','unknown')",
                    (account,),
                )
            records = await self.read_applications(adapter, guard)
            if records is None:
                raise ValueError("friend_approval_read_backoff")
            resolved = []
            for item in unresolved:
                verified = records.get(item["request_id"])
                if (
                    not verified
                    or verified["uid"] != item["member"]
                    or verified["direction"] != "FROM_HIM"
                    or verified["state"]
                    not in {
                        "FRIENDLOG_STATE_AGREE",
                        "FRIENDLOG_STATE_REFUSE",
                        "FRIENDLOG_STATE_DELETE",
                        "FRIENDLOG_STATE_EXPIRE",
                    }
                ):
                    raise ValueError("friend_approval_still_unknown")
                resolved.append(
                    (verified["state"] == "FRIENDLOG_STATE_AGREE", item["request_id"])
                )
            async with AsyncDatabase(
                lambda: self.database(instance), write=True, guard=guard
            ) as db:
                for accepted, identity in resolved:
                    await db.execute(
                        "UPDATE approval_attempts SET state=?,updated=? WHERE account=? AND request_id=?",
                        (
                            "completed" if accepted else "rejected",
                            time.time(),
                            account,
                            identity,
                        ),
                    )
                changed = await db.execute(
                    "UPDATE approval_settings SET paused='',read_failures=0,retry_at=0,"
                    "last_scan=?,version=version+1 WHERE account=? AND version=?",
                    (time.time(), account, expected),
                )
                if changed != 1:
                    raise ValueError("friend_approval_settings_changed")
            self.versions[(instance, account)] = expected + 1
            return await self.status(adapter)

    async def step(self, adapter):
        """Inspect a page and approve at most one verified incoming application.

        Args:
            adapter: One enabled, online bot owned by the active plugin.

        Raises:
            asyncio.CancelledError: After preserving an interrupted approval as uncertain.
        """
        if (
            not self.active
            or adapter.connection_state != "online"
            or adapter.config.get("_business_active") is not True
            or adapter.config.get("_open_paused", False)
            or not adapter.config.get("enable", True)
            or not getattr(adapter, "business", None)
        ):
            return
        instance, account, client = (
            adapter.meta().id,
            str(adapter.account),
            adapter.business,
        )
        key = (instance, account)
        lock = self.locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            state = await self.status(adapter)
            if not state["enabled"] or state["paused_reason"] or state["retry_after"]:
                return
            version = state["version"]
            identity = None
            approval_returned = False
            approval_completed = False
            approval_rejected = False

            def authorize():
                if (
                    not self.active
                    or adapter.business is not client
                    or client.fields[3] != int(account)
                    or adapter.connection_state != "online"
                    or adapter.config.get("_open_paused", False)
                    or self.versions.get((instance, account)) != version
                ):
                    raise ProtocolError("friend_approval_authority_changed")

            guard = account_guard(adapter, authorize)
            try:
                guard()
                if state["inflight"]:
                    raise ProtocolError("friend_approval_unknown")
                if (
                    not state["read_failures"]
                    and time.time() - state["last_checked_at"] < SCAN_SECONDS
                ):
                    return
                if time.monotonic() < self.cooldowns.get(key, 0):
                    return
                data = await self.read_applications(adapter, guard)
                if data is None:
                    return
                incoming = [
                    row
                    for row in data.values()
                    if row["direction"] == "FROM_HIM"
                    and row["state"] == "FRIENDLOG_STATE_PROCESSING"
                    and row["uid"] != int(account)
                ]
                async with AsyncDatabase(
                    lambda: self.database(instance), write=True, guard=guard
                ) as db:
                    await db.execute(
                        "UPDATE approval_settings SET last_scan=?,pending=? WHERE account=?",
                        (time.time(), len(incoming), account),
                    )
                    seen = {
                        row["request_id"]
                        for row in await db.fetchall(
                            "SELECT request_id FROM approval_attempts WHERE account=?",
                            (account,),
                        )
                    }
                target = next((row for row in incoming if row["id"] not in seen), None)
                if target is None:
                    return
                await asyncio.sleep(REQUEST_SECONDS)
                guard()
                async with AsyncDatabase(
                    lambda: self.database(instance), write=True, guard=guard
                ) as db:
                    await db.execute(
                        "INSERT INTO approval_attempts VALUES(?,?,?,'sending',?,?)",
                        (
                            account,
                            target["id"],
                            target["uid"],
                            time.time(),
                            time.time(),
                        ),
                    )
                    await db.execute(
                        "UPDATE approval_settings SET last_action=? WHERE account=?",
                        (time.time(), account),
                    )
                    await db.execute(
                        "UPDATE approval_settings SET last_request=? WHERE account=?",
                        (time.time(), account),
                    )
                identity = target["id"]
                self.cooldowns[key] = time.monotonic() + ACTION_SECONDS
                try:
                    await request_business(
                        client,
                        APPROVE_ROUTE,
                        {
                            "flId": target["provider_id"],
                            "state": "FRIENDLOG_STATE_AGREE",
                            "nickName": "",
                            "classId": 0,
                            "friendCircle": target["circle"],
                        },
                        authorize=guard,
                    )
                    approval_returned = True
                finally:
                    # The shared HTTP lock may have outlasted the intent cooldown.
                    completed_at = time.time()
                    self.cooldowns[key] = time.monotonic() + ACTION_SECONDS
                    cancelled = False
                    while True:
                        try:
                            async with AsyncDatabase(
                                lambda: self.database(instance), write=True
                            ) as db:
                                await db.execute(
                                    "UPDATE approval_settings "
                                    "SET last_request=MAX(last_request,?),"
                                    "last_action=MAX(last_action,?) WHERE account=?",
                                    (completed_at, completed_at, account),
                                )
                            break
                        except asyncio.CancelledError:
                            cancelled = True
                    if cancelled:
                        raise asyncio.CancelledError
                await asyncio.sleep(REQUEST_SECONDS)
                guard()
                records = await self.read_applications(adapter, guard)
                verified = records.get(identity) if records is not None else None
                if (
                    verified
                    and verified["uid"] == target["uid"]
                    and verified["direction"] == "FROM_HIM"
                    and verified["state"]
                    in {
                        "FRIENDLOG_STATE_REFUSE",
                        "FRIENDLOG_STATE_DELETE",
                        "FRIENDLOG_STATE_EXPIRE",
                    }
                ):
                    approval_rejected = True
                    raise ProtocolError("friend_approval_rejected")
                if not verified or (
                    verified["uid"] != target["uid"]
                    or verified["direction"] != "FROM_HIM"
                    or verified["state"] != "FRIENDLOG_STATE_AGREE"
                ):
                    raise ProtocolError("friend_approval_unknown")
                async with AsyncDatabase(
                    lambda: self.database(instance), write=True, guard=guard
                ) as db:
                    await db.execute(
                        "UPDATE approval_attempts SET state='completed',updated=? "
                        "WHERE account=? AND request_id=?",
                        (time.time(), account, identity),
                    )
                    await db.execute(
                        "UPDATE approval_settings SET pending=MAX(0,pending-1) "
                        "WHERE account=?",
                        (account,),
                    )
                approval_completed = True
                if self.business_settings is not None:
                    await asyncio.to_thread(
                        self.business_settings.log,
                        instance,
                        "friend-auto",
                        "friend_approval",
                        "completed",
                        "",
                        {"account": account, "request_id": identity},
                    )
                # Welcome failures must not turn a confirmed approval into "unknown".
                if self.welcome is not None:
                    try:
                        guard()
                        outcome = await self.welcome(
                            adapter,
                            target["uid"],
                            verified["peer"],
                            identity,
                            guard,
                        )
                        if outcome == "busy":
                            async with AsyncDatabase(
                                lambda: self.database(instance), write=True, guard=guard
                            ) as db:
                                await db.execute(
                                    "INSERT INTO welcome_observations VALUES(?,?,?,?,?) "
                                    "ON CONFLICT(account,request_id) DO UPDATE SET "
                                    "state=excluded.state "
                                    "WHERE member=excluded.member AND peer=excluded.peer",
                                    (
                                        account,
                                        identity,
                                        target["uid"],
                                        verified["peer"],
                                        WELCOME_DEFERRED,
                                    ),
                                )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("Friend welcome delivery was not confirmed")
            except (Exception, asyncio.CancelledError) as exc:
                if approval_completed:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    logger.warning("Post-approval notification was not confirmed")
                    return
                revoked = str(exc) in {
                    "database_authority_changed",
                    "friend_approval_authority_changed",
                }
                if identity is None and (
                    revoked or isinstance(exc, asyncio.CancelledError)
                ):
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return
                code = (
                    str(exc)
                    if isinstance(exc, ProtocolError)
                    and 0 < len(str(exc)) <= 64
                    and all(c.isascii() and (c.islower() or c == "_") for c in str(exc))
                    else "friend_approval_unknown"
                )
                async with AsyncDatabase(
                    lambda: self.database(instance), write=True
                ) as db:
                    await db.execute(
                        "UPDATE approval_settings SET paused=? WHERE account=?",
                        (code, account),
                    )
                    if identity is not None:
                        await db.execute(
                            "UPDATE approval_attempts SET state='unknown',updated=? "
                            "WHERE account=? AND request_id=?",
                            (time.time(), account, identity),
                        )
                        # Readback failures cannot prove the approval was rejected.
                        if approval_rejected or (
                            not approval_returned
                            and (
                                getattr(exc, "submitted", None) is False
                                or isinstance(exc, BusinessError)
                                and getattr(exc, "provider_rejected", False) is True
                            )
                        ):
                            await db.execute(
                                "UPDATE approval_attempts SET state='rejected' WHERE account=? AND request_id=?",
                                (account, identity),
                            )
                            if approval_rejected:
                                await db.execute(
                                    "UPDATE approval_settings SET pending=MAX(0,pending-1) "
                                    "WHERE account=?",
                                    (account,),
                                )
                            if (
                                str(exc) == "rate_limited"
                                and type(exc.retry_after) in (int, float)
                                and math.isfinite(exc.retry_after)
                            ):
                                await db.execute(
                                    "UPDATE approval_settings SET retry_at=MAX(retry_at,?) "
                                    "WHERE account=?",
                                    (
                                        time.time()
                                        + max(
                                            REQUEST_SECONDS, min(86400, exc.retry_after)
                                        ),
                                        account,
                                    ),
                                )
                if self.business_settings is not None:
                    await asyncio.to_thread(
                        self.business_settings.log,
                        instance,
                        "friend-auto",
                        "friend_approval",
                        "needs_review",
                        code,
                        {"account": account, "request_id": identity},
                    )
                if isinstance(exc, asyncio.CancelledError):
                    raise

    async def poll(self):
        """Keep the bounded approval worker separate from conversational processing."""
        while self.active:
            for adapter in list(self.context.platform_manager.platform_insts):
                if adapter.meta().name == "wangshangliao":
                    try:
                        await self.notify_welcomes(adapter)
                        await self.step(adapter)
                    except Exception:
                        # Halt approvals without affecting conversational workers.
                        logger.error(
                            "Friend approval worker failed; no automatic retry"
                        )
                        self.active = False
                        return
                    await asyncio.sleep(REQUEST_SECONDS)
            await asyncio.sleep(5)
