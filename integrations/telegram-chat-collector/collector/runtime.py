"""Telethon collection, bounded reconciliation, and account health."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from telethon import errors, events, functions, types

from .store import Store


def group_unavailable(entity) -> bool:
    """Identify explicit Telegram evidence that a group is no longer accessible.

    Args:
        entity: Telegram entity, possibly absent on incomplete responses.

    Returns:
        Whether membership loss or group deactivation is explicitly known.
    """
    return isinstance(entity, (types.ChatForbidden, types.ChannelForbidden)) or bool(
        getattr(entity, "left", False) or getattr(entity, "deactivated", False)
    )


class Collector:
    """Run collection independently of AstrBot and expose sanitized health."""

    def __init__(self, client, store: Store):
        self.client = client
        self.store = store
        self.state = "starting"
        self.retry_at = 0.0
        self.rpc_lock = asyncio.Lock()
        self.public_due = {}
        self.membership_due = {}
        self.health_due = 0.0
        self.entities = {}
        self.prune_due = 0.0
        self.backfill_due = 0.0
        client.add_event_handler(self.on_message, events.NewMessage())
        client.add_event_handler(self.on_message, events.MessageEdited())
        client.add_event_handler(self.on_delete, events.MessageDeleted())

    async def on_message(self, event) -> None:
        """Persist eligible incoming events without saving unrelated content.

        Args:
            event: Telethon new-message or edited-message event.
        """
        self.ingest(event.chat_id, event.message)

    def ingest(self, chat_id: int, message) -> None:
        """Normalize a regular user message for durable storage.

        Args:
            chat_id: Signed Telegram group ID.
            message: Telegram message, including history replay records.
        """
        if not isinstance(message, types.Message) or not isinstance(
            message.from_id, types.PeerUser
        ):
            return
        media = "text"
        if message.sticker:
            media = "sticker"
        elif message.voice:
            media = "voice"
        elif isinstance(message.media, types.MessageMediaPhoto):
            media = "photo"
        elif message.video:
            media = "video"
        elif message.media:
            media = "media"
        self.store.record(
            chat_id,
            message.id,
            message.from_id.user_id,
            message.date.timestamp(),
            message.message or "",
            media,
            message.edit_date.timestamp() if message.edit_date else None,
            message.reply_to_msg_id,
            [
                entity.url
                if isinstance(entity, types.MessageEntityTextUrl)
                else (message.message or "")
                .encode("utf-16-le")[
                    entity.offset * 2 : (entity.offset + entity.length) * 2
                ]
                .decode("utf-16-le")
                for entity in (message.entities or [])
                if isinstance(
                    entity, (types.MessageEntityUrl, types.MessageEntityTextUrl)
                )
            ],
        )

    async def on_delete(self, event) -> None:
        """Only apply deletions that carry an unambiguous group identity.

        Args:
            event: Telethon message deletion event.
        """
        if event.chat_id is not None:
            self.store.delete(event.chat_id, event.deleted_ids)

    async def reconcile(self, group: dict) -> None:
        """Replay forward from a history-only cursor, never from live events.

        Args:
            group: Enabled group configuration.

        Raises:
            Exception: Telegram errors handled and sanitized by the run loop.
        """
        group_id = group["id"]
        row = self.store.db.execute(
            "SELECT * FROM progress WHERE chat_id=?", (group_id,)
        ).fetchone()
        started = time.time()
        if row["last_sync"] is None or started - row["last_sync"] > 180:
            self.store.gap(group_id, "reconciling", started)
        options = {"min_id": row["message_id"], "reverse": True, "limit": None}
        if not row["message_id"]:
            earliest = self.store.db.execute(
                "SELECT MIN(start) FROM intervals WHERE kind='groups' AND id=?",
                (group_id,),
            ).fetchone()[0]
            options["offset_date"] = datetime.fromtimestamp(earliest - 1, timezone.utc)
        async for message in self.client.iter_messages(group_id, **options):
            self.ingest(group_id, message)
            with self.store.db:
                self.store.db.execute(
                    "UPDATE progress SET message_id=MAX(message_id,?) WHERE chat_id=?",
                    (message.id, group_id),
                )
        with self.store.db:
            self.store.db.execute(
                "UPDATE progress SET last_sync=?,error=NULL WHERE chat_id=?",
                (started, group_id),
            )
            self.store.db.execute(
                "UPDATE gaps SET end=? WHERE chat_id=? AND end IS NULL",
                (started, group_id),
            )

    async def poll_public(self, group: dict, entity) -> bool:
        """Advance public group updates and persist the cursor after applying data.

        Args:
            group: Enabled monitoring group.
            entity: Readable public Telegram supergroup.

        Returns:
            Whether the current difference has been fully drained.
        """
        group_id = group["id"]
        row = self.store.db.execute(
            "SELECT pts FROM group_access WHERE id=?", (group_id,)
        ).fetchone()
        pts = row["pts"]
        if not pts:
            full = await self.client(functions.channels.GetFullChannelRequest(entity))
            pts = full.full_chat.pts
            await self.reconcile(group)
        result = await self.client(
            functions.updates.GetChannelDifferenceRequest(
                channel=entity,
                filter=types.ChannelMessagesFilterEmpty(),
                pts=pts,
                limit=100,
            )
        )
        if isinstance(result, types.updates.ChannelDifferenceTooLong):
            self.store.gap(group_id, "public_updates_lost", time.time())
            await self.reconcile(group)
            for message in result.messages:
                self.ingest(group_id, message)
            next_pts = result.dialog.pts
        elif isinstance(result, types.updates.ChannelDifference):
            for message in result.new_messages:
                self.ingest(group_id, message)
            for update in result.other_updates:
                if isinstance(update, types.UpdateEditChannelMessage):
                    self.ingest(group_id, update.message)
                elif isinstance(update, types.UpdateDeleteChannelMessages):
                    self.store.delete(group_id, update.messages)
            next_pts = result.pts
        elif isinstance(result, types.updates.ChannelDifferenceEmpty):
            next_pts = result.pts
        else:
            raise ValueError("Unexpected public difference response")
        with self.store.db:
            self.store.db.execute(
                "UPDATE group_access SET pts=? WHERE id=?", (next_pts, group_id)
            )
            if result.final:
                self.store.db.execute(
                    "UPDATE progress SET last_sync=?,error=NULL WHERE chat_id=?",
                    (time.time(), group_id),
                )
                self.store.db.execute(
                    "UPDATE gaps SET end=? WHERE chat_id=? AND end IS NULL",
                    (time.time(), group_id),
                )
        self.public_due[group_id] = time.time() + (
            max(1, getattr(result, "timeout", None) or 1) if result.final else 0
        )
        return bool(result.final)

    async def backfill_page(self) -> None:
        """Replay a bounded historical page without delaying the live listener.

        Paused watches remain queued. Each persisted cursor can safely be replayed
        after interruption because messages use a group/message unique key.
        """
        if self.store.db.execute("SELECT 1 FROM pauses WHERE end IS NULL").fetchone():
            return
        job = self.store.db.execute(
            "SELECT b.* FROM backfills b JOIN watches w USING(chat_id,user_id) "
            "JOIN targets g ON g.kind='groups' AND g.id=b.chat_id "
            "JOIN targets p ON p.kind='people' AND p.id=b.user_id "
            "WHERE b.state='pending' AND w.enabled=1 AND g.enabled=1 AND p.enabled=1 "
            "AND b.chat_id NOT IN (SELECT id FROM removed_groups) ORDER BY b.scanned,b.chat_id LIMIT 1"
        ).fetchone()
        if job is None:
            return
        group, person = job["chat_id"], job["user_id"]
        try:
            async with self.rpc_lock:
                messages = await asyncio.wait_for(
                    self.client.get_messages(
                        group,
                        limit=100,
                        offset_id=job["cursor"],
                        offset_date=datetime.fromtimestamp(job["end"], timezone.utc)
                        if not job["cursor"]
                        else None,
                    ),
                    30,
                )
            # A removal or replacement may have happened while the RPC awaited.
            current = self.store.db.execute(
                "SELECT * FROM backfills WHERE chat_id=? AND user_id=?", (group, person)
            ).fetchone()
            if current is None or current["end"] != job["end"]:
                return
            if self.store.db.execute(
                "SELECT 1 FROM pauses WHERE end IS NULL"
            ).fetchone():
                return
            if not any(
                w["user_id"] == person
                and w["enabled"]
                and w["group_enabled"]
                and w["person_enabled"]
                for w in self.store.watches(group)
            ):
                return
            finished = not messages
            cursor = job["cursor"]
            scanned = 0
            for message in messages:
                cursor = message.id
                scanned += 1
                if message.date.timestamp() < job["start"]:
                    finished = True
                    break
                if (
                    isinstance(message, types.Message)
                    and isinstance(message.from_id, types.PeerUser)
                    and message.from_id.user_id == person
                    and message.date.timestamp() < job["end"]
                ):
                    self.ingest(group, message)
            with self.store.db:
                self.store.db.execute(
                    "UPDATE backfills SET cursor=?,scanned=scanned+?,matched=(SELECT COUNT(*) FROM messages WHERE chat_id=? AND user_id=? AND sent>=? AND sent<?),state=?,error=NULL WHERE chat_id=? AND user_id=?",
                    (
                        cursor,
                        scanned,
                        group,
                        person,
                        job["start"],
                        job["end"],
                        "completed" if finished else "pending",
                        group,
                        person,
                    ),
                )
        except (errors.FloodWaitError, errors.UnauthorizedError, errors.AuthKeyError):
            raise
        except Exception as exc:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE backfills SET state='failed',error=? WHERE chat_id=? AND user_id=?",
                    (type(exc).__name__, group, person),
                )

    async def run(self) -> None:
        """Maintain connectivity and retry safely without logging RPC payloads."""
        while True:
            try:
                if time.time() >= self.prune_due:
                    self.store.prune()
                    self.prune_due = time.time() + 60
                if time.time() < self.retry_at:
                    await asyncio.sleep(min(30, self.retry_at - time.time()))
                    continue
                async with self.rpc_lock:
                    if not self.client.is_connected():
                        self.state = "connecting"
                        await asyncio.wait_for(self.client.connect(), 30)
                    # is_user_authorized caches success and can miss revoked sessions.
                    if time.time() >= self.health_due:
                        await asyncio.wait_for(
                            self.client(functions.updates.GetStateRequest()), 30
                        )
                        self.health_due = time.time() + 60
                self.state = "connected"
                for group in self.store.targets("groups"):
                    try:
                        access = self.store.db.execute(
                            "SELECT * FROM group_access WHERE id=?", (group["id"],)
                        ).fetchone()
                        public = bool(access and access["mode"] == "public")
                        due = (
                            self.public_due
                            if public and group["enabled"]
                            else self.membership_due
                        )
                        if time.time() < due.get(group["id"], 0):
                            continue
                        due[group["id"]] = time.time() + 60
                        cached = self.entities.get(group["id"])
                        if public and cached and time.time() - cached[0] < 60:
                            entity = cached[1]
                        else:
                            async with self.rpc_lock:
                                entity = await asyncio.wait_for(
                                    self.client.get_entity(group["id"]), 30
                                )
                            self.entities[group["id"]] = (time.time(), entity)
                        if public:
                            if (
                                not isinstance(entity, types.Channel)
                                or not entity.megagroup
                            ):
                                raise ValueError("Public group unavailable")
                            if not entity.left:
                                # Keep public mode until replay succeeds, so switching is retryable.
                                async with self.rpc_lock:
                                    if group["enabled"]:
                                        complete = await asyncio.wait_for(
                                            self.poll_public(group, entity), 30
                                        )
                                        if not complete:
                                            continue
                                        await asyncio.wait_for(
                                            self.reconcile(group), 30
                                        )
                                with self.store.db:
                                    self.store.db.execute(
                                        "UPDATE group_access SET mode='joined' WHERE id=?",
                                        (group["id"],),
                                    )
                                self.membership_due[group["id"]] = time.time() + 60
                                continue
                            if group["enabled"]:
                                async with self.rpc_lock:
                                    await asyncio.wait_for(
                                        self.poll_public(group, entity), 30
                                    )
                            continue
                        if group_unavailable(entity):
                            with self.store.db:
                                self.store.configure(
                                    "groups", group["id"], group["name"], False
                                )
                                for watch in self.store.watches(group["id"]):
                                    self.store.configure_watch(
                                        group["id"], watch["user_id"], False
                                    )
                                self.store.db.execute(
                                    "INSERT OR IGNORE INTO removed_groups VALUES(?)",
                                    (group["id"],),
                                )
                            continue
                        if not group["enabled"]:
                            continue
                        # Release the RPC lock between groups so administration
                        # is not blocked by the entire reconciliation round.
                        async with self.rpc_lock:
                            if time.time() < self.retry_at:
                                break
                            await asyncio.wait_for(self.reconcile(group), 30)
                    except errors.FloodWaitError:
                        raise
                    except (errors.UnauthorizedError, errors.AuthKeyError):
                        raise
                    except Exception as exc:
                        self.store.gap(group["id"], type(exc).__name__, time.time())
                if time.time() >= self.backfill_due:
                    self.backfill_due = time.time() + 5
                    await self.backfill_page()
            except asyncio.CancelledError:
                raise
            except errors.FloodWaitError as exc:
                self.retry_at = time.time() + exc.seconds + 1
                self.state = "flood_wait"
            except (errors.UnauthorizedError, errors.AuthKeyError):
                self.state = "login_required"
                self.retry_at = time.time() + 60
            except Exception:
                self.state = "connection_error"
                self.retry_at = time.time() + 60
            await asyncio.sleep(1)

    def status(self, start: float = 0, end: float | None = None) -> dict:
        """Describe health, including incomplete intervals within a query window.

        Args:
            start: Inclusive query start for historical gap filtering.
            end: Exclusive query end, defaulting to the present.

        Returns:
            Sanitized state, group progress, and gap metadata with UTC dates.
        """
        now = time.time()
        end = now if end is None else end
        paused = bool(
            self.store.db.execute("SELECT 1 FROM pauses WHERE end IS NULL").fetchone()
        )
        today = (
            datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=8)
        ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
        people = self.store.targets("people")
        watches = self.store.watches()
        groups = [
            dict(r)
            for r in self.store.db.execute("""
            SELECT t.id,t.name,t.enabled,p.message_id,p.last_sync,p.error
            FROM targets t LEFT JOIN progress p ON p.chat_id=t.id
            WHERE t.kind='groups' AND t.id NOT IN (SELECT id FROM removed_groups) ORDER BY t.id
        """)
        ]
        gaps = [
            dict(r)
            for r in self.store.db.execute(
                "SELECT * FROM gaps WHERE start<? AND (end IS NULL OR end>?) ORDER BY id DESC LIMIT 100",
                (end, start),
            )
        ]
        incomplete = self.state != "connected" or not self.client.is_connected()
        for group in groups:
            access = self.store.db.execute(
                "SELECT mode FROM group_access WHERE id=?", (group["id"],)
            ).fetchone()
            group["collection_mode"] = access["mode"] if access else "joined"
            group["stale"] = bool(
                group["enabled"]
                and not paused
                and (not group["last_sync"] or now - group["last_sync"] > 180)
            )
            group["watch_count"] = sum(
                bool(w["enabled"] and w["person_enabled"])
                for w in watches
                if w["chat_id"] == group["id"]
            )
            row = self.store.db.execute(
                "SELECT MAX(sent),COUNT(CASE WHEN sent>=? THEN 1 END) FROM messages WHERE chat_id=?",
                (today.timestamp(), group["id"]),
            ).fetchone()
            group["last_message"] = (
                datetime.fromtimestamp(row[0], timezone.utc).isoformat()
                if row[0]
                else None
            )
            group["today_count"] = row[1]
            incomplete |= group["stale"] or bool(group["enabled"] and group["error"])
            if group["last_sync"]:
                group["last_sync"] = datetime.fromtimestamp(
                    group["last_sync"], timezone.utc
                ).isoformat()
        # Recovered history cannot prove that deleted messages were not missed.
        incomplete |= bool(gaps)
        incomplete |= bool(
            self.store.db.execute(
                "SELECT 1 FROM backfills b JOIN watches w USING(chat_id,user_id) WHERE b.state!='completed' AND b.start<? AND b.end>? AND b.chat_id NOT IN (SELECT id FROM removed_groups) LIMIT 1",
                (end, start),
            ).fetchone()
        )
        for gap in gaps:
            for key in ("start", "end"):
                if gap[key] is not None:
                    gap[key] = datetime.fromtimestamp(
                        gap[key], timezone.utc
                    ).isoformat()
        return {
            "paused": paused,
            "people": people,
            "watches": watches,
            "active_watches": 0
            if paused
            else sum(
                bool(w["enabled"] and w["group_enabled"] and w["person_enabled"])
                for w in watches
            ),
            "state": self.state,
            "connected": self.client.is_connected(),
            "retry_at": datetime.fromtimestamp(self.retry_at, timezone.utc).isoformat()
            if self.retry_at
            else None,
            "groups": groups,
            "gaps": gaps,
            "incomplete": bool(incomplete),
        }
