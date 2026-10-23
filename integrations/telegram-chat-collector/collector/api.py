"""Authenticated local HTTP API; errors never include upstream RPC details."""

import asyncio
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone

import regex
from aiohttp import web
from telethon import errors, functions, types, utils

from .runtime import group_unavailable

SERVICE = web.AppKey("service", object)
TOKEN = web.AppKey("token", str)


@web.middleware
async def authenticate(request, handler):
    """Authenticate before parsing request data and sanitize failures.

    Args:
        request: Incoming HTTP request.
        handler: Selected route handler.

    Returns:
        JSON response or authenticated route result.
    """
    expected = "Bearer " + request.app[TOKEN]
    if not hmac.compare_digest(
        request.headers.get("Authorization", "").encode(), expected.encode()
    ):
        return web.json_response({"error": "unauthorized"}, status=401)
    service = request.app[SERVICE]
    try:
        return await handler(request)
    except (ValueError, KeyError, TypeError) as exc:
        code = {
            "Start must be within the last 90 days": "invalid_history_window",
            "UTC offset required": "invalid_history_timezone",
            "Configure the group and person first": "watch_target_missing",
            "Active target capacity reached": "target_capacity",
            "Join the group using the Telegram client first": "group_not_joined",
            "Public group required": "public_access_required",
            "Configured group and nonnegative page required": "group_selection_expired",
            "Accessible group required": "group_inaccessible",
            "A user identity is required": "user_required",
            "Pause configured group before removal": "pause_before_remove",
        }.get(str(exc), "invalid_request")
        logging.getLogger(__name__).warning(
            "Collector validation rejected: resource=%s method=%s code=%s type=%s",
            request.match_info.get("resource", "unknown"),
            request.method,
            code,
            type(exc).__name__,
        )
        return web.json_response({"error": code}, status=400)
    except errors.FloodWaitError as exc:
        service.retry_at = time.time() + exc.seconds + 1
        service.state = "flood_wait"
        return web.json_response({"error": "telegram_rate_limited"}, status=503)
    except (errors.UnauthorizedError, errors.AuthKeyError):
        service.state = "login_required"
        return web.json_response({"error": "account_unavailable"}, status=503)
    except web.HTTPException:
        raise
    except Exception:
        return web.json_response({"error": "service_unavailable"}, status=503)


async def handle(request):
    """Serve status, target management, and bounded record queries.

    Args:
        request: Authenticated aiohttp request.

    Returns:
        JSON data with UTC ISO timestamps.

    Raises:
        ValueError: If IDs, windows, or payloads are invalid.
        HTTPNotFound: If the endpoint does not exist.
    """
    service = request.app[SERVICE]
    store = service.store
    resource = request.match_info["resource"]
    if request.method == "GET" and resource == "status":
        return web.json_response(service.status())
    if request.method == "PUT" and resource == "remove_person":
        data = await request.json()
        person = int(data["person"])
        group = int(data.get("group", 0))
        if person <= 0 or group > 0:
            raise ValueError("Invalid person or group")
        with store.db:
            if group:
                for table in ("messages", "watches", "watch_intervals", "backfills"):
                    store.db.execute(
                        f"DELETE FROM {table} WHERE chat_id=? AND user_id=?",
                        (group, person),
                    )
            else:
                for table in (
                    "messages",
                    "watches",
                    "watch_intervals",
                    "link_rules",
                    "backfills",
                ):
                    store.db.execute(f"DELETE FROM {table} WHERE user_id=?", (person,))
            if not store.db.execute(
                "SELECT 1 FROM watches WHERE user_id=?", (person,)
            ).fetchone():
                store.db.execute(
                    "DELETE FROM targets WHERE kind='people' AND id=?", (person,)
                )
                store.db.execute(
                    "DELETE FROM intervals WHERE kind='people' AND id=?", (person,)
                )
                store.db.execute("DELETE FROM link_rules WHERE user_id=?", (person,))
        return web.json_response({"ok": True})
    if request.method == "PUT" and resource == "remove_group":
        data = await request.json()
        group = int(data["group"])
        if not isinstance(data.get("leave"), bool):
            raise ValueError("Boolean leave required")
        target = next((g for g in store.targets("groups") if g["id"] == group), None)
        if target is None or target["enabled"]:
            raise ValueError("Pause configured group before removal")
        if data["leave"]:
            if service.state != "connected":
                return web.json_response({"error": "account_unavailable"}, status=503)
            async with service.rpc_lock:
                entity = await asyncio.wait_for(service.client.get_entity(group), 30)
                if isinstance(entity, types.Channel) and not entity.left:
                    await asyncio.wait_for(
                        service.client(functions.channels.LeaveChannelRequest(entity)),
                        30,
                    )
                elif isinstance(entity, types.Chat):
                    await asyncio.wait_for(
                        service.client(
                            functions.messages.DeleteChatUserRequest(
                                entity.id, types.InputUserSelf()
                            )
                        ),
                        30,
                    )
                elif not isinstance(entity, types.Channel):
                    raise ValueError("Group required")
        with store.db:
            for watch in store.watches(group):
                store.configure_watch(group, watch["user_id"], False)
            store.db.execute("INSERT OR IGNORE INTO removed_groups VALUES(?)", (group,))
        return web.json_response({"ok": True})
    if resource == "link_rules":
        if request.method == "GET":
            return web.json_response(
                {
                    "items": [
                        {**dict(row), "patterns": json.loads(row["patterns"])}
                        for row in store.db.execute(
                            "SELECT r.*,t.name FROM link_rules r JOIN targets t ON t.kind='people' AND t.id=r.user_id ORDER BY r.user_id"
                        )
                    ]
                }
            )
        data = await request.json()
        user = int(data["person"])
        patterns = data["patterns"]
        if (
            not isinstance(data.get("enabled"), bool)
            or not isinstance(patterns, list)
            or not 1 <= len(patterns) <= 10
        ):
            raise ValueError("Invalid rule settings")
        if not any(p["id"] == user for p in store.targets("people")):
            raise ValueError("Configured person required")
        try:
            for pattern in patterns:
                if not isinstance(pattern, str) or not 1 <= len(pattern) <= 500:
                    raise ValueError("Invalid pattern length")
                regex.compile(pattern)
        except regex.error:
            return web.json_response({"error": "invalid_regex"}, status=400)
        with store.db:
            store.db.execute(
                "INSERT INTO link_rules VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET patterns=excluded.patterns,enabled=excluded.enabled",
                (user, json.dumps(patterns), int(data["enabled"])),
            )
        return web.json_response({"ok": True})
    if request.method == "GET" and resource == "violations":
        person = int(request.query.get("person", "0"))
        before = int(request.query.get("before", str(2**63 - 1)))
        rules = {
            r["user_id"]: [regex.compile(p) for p in json.loads(r["patterns"])]
            for r in store.db.execute("SELECT * FROM link_rules WHERE enabled=1")
        }
        rows = store.db.execute(
            "SELECT m.rowid AS cursor,m.*,p.name AS person_name,g.name AS group_name FROM messages m JOIN targets p ON p.kind='people' AND p.id=m.user_id JOIN targets g ON g.kind='groups' AND g.id=m.chat_id WHERE m.rowid<? AND (?=0 OR m.user_id=?) AND m.deleted=0 AND m.text IS NOT NULL ORDER BY m.rowid DESC LIMIT 201",
            (before, person, person),
        ).fetchall()
        items = []
        cursor = before
        more = len(rows) > 200
        deadline = time.monotonic() + 2
        for index, row in enumerate(rows[:200]):
            cursor = row["cursor"]
            if row["user_id"] not in rules:
                continue
            urls = (
                json.loads(row["links"])
                if row["links"] is not None
                else re.findall(
                    r"(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>\"，。！？；]+",
                    row["text"],
                )
            )
            bad = []
            for url in dict.fromkeys(urls):
                try:
                    if time.monotonic() > deadline:
                        raise TimeoutError
                    if not any(
                        pattern.fullmatch(url, timeout=0.01)
                        for pattern in rules[row["user_id"]]
                    ):
                        bad.append(url)
                except TimeoutError:
                    return web.json_response({"error": "regex_timeout"}, status=422)
            if bad:
                items.append(
                    {
                        "person": row["user_id"],
                        "name": row["person_name"],
                        "group": row["group_name"],
                        "chat_id": row["chat_id"],
                        "message_id": row["message_id"],
                        "sent": datetime.fromtimestamp(
                            row["sent"], timezone.utc
                        ).isoformat(),
                        "links": bad,
                    }
                )
            if len(items) == 10:
                more = index + 1 < len(rows)
                break
        return web.json_response(
            {
                "items": items,
                "next": cursor if more else None,
                "health": service.status(),
                "rule_count": len(rules),
            }
        )
    if request.method == "PUT" and resource == "join":
        data = await request.json()
        link = data.get("link", "")
        if not isinstance(link, str):
            raise ValueError("Group link required")
        match = re.fullmatch(
            r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+([A-Za-z0-9_-]+)|joinchat/([A-Za-z0-9_-]+)|([A-Za-z][A-Za-z0-9_]{3,31}))/?",
            link.strip(),
        )
        if not match:
            return web.json_response({"error": "invalid_group_link"}, status=400)
        if service.state != "connected" or time.time() < service.retry_at:
            return web.json_response({"error": "account_unavailable"}, status=503)
        invite_hash = match[1] or match[2]
        submitted = False
        prepare = data.get("prepare", False)
        try:
            async with service.rpc_lock:
                if invite_hash:
                    invite = await asyncio.wait_for(
                        service.client(
                            functions.messages.CheckChatInviteRequest(invite_hash)
                        ),
                        30,
                    )
                    if isinstance(invite, types.ChatInviteAlready):
                        entity = invite.chat
                        joined = True
                    else:
                        if getattr(invite, "channel", False) and not getattr(
                            invite, "megagroup", False
                        ):
                            return web.json_response(
                                {"error": "group_required"}, status=400
                            )
                        if prepare:
                            return web.json_response({"status": "join_required"})
                        submitted = True
                        result = await asyncio.wait_for(
                            service.client(
                                functions.messages.ImportChatInviteRequest(invite_hash)
                            ),
                            30,
                        )
                        entity = next(
                            (
                                c
                                for c in getattr(result, "chats", [])
                                if isinstance(c, types.Chat)
                                or isinstance(c, types.Channel)
                                and c.megagroup
                            ),
                            None,
                        )
                        if entity is None:
                            checked = await asyncio.wait_for(
                                service.client(
                                    functions.messages.CheckChatInviteRequest(
                                        invite_hash
                                    )
                                ),
                                30,
                            )
                            if isinstance(checked, types.ChatInviteAlready):
                                entity = checked.chat
                        joined = False
                else:
                    entity = await asyncio.wait_for(
                        service.client.get_entity(match[3]), 30
                    )
                    if not isinstance(entity, types.Channel) or not entity.megagroup:
                        return web.json_response(
                            {"error": "group_required"}, status=400
                        )
                    joined = not entity.left
                    if not joined and prepare:
                        group_id = utils.get_peer_id(entity)
                        count = store.db.execute(
                            "SELECT COUNT(*) FROM group_access a JOIN targets t ON t.id=a.id AND t.kind='groups' WHERE a.mode='public' AND t.enabled=1 AND a.id!=? AND a.id NOT IN (SELECT id FROM removed_groups)",
                            (group_id,),
                        ).fetchone()[0]
                        if count >= 10:
                            return web.json_response(
                                {"status": "join_required", "reason": "public_limit"}
                            )
                        # Verify readability before offering monitoring without membership.
                        await asyncio.wait_for(
                            service.client.get_messages(entity, limit=1), 30
                        )
                        with store.db:
                            store.db.execute(
                                "INSERT INTO group_access(id,mode,username) VALUES(?,'public',?) ON CONFLICT(id) DO UPDATE SET mode='public',username=excluded.username",
                                (group_id, match[3]),
                            )
                        return web.json_response(
                            {"status": "public", "id": group_id, "name": entity.title}
                        )
                    if not joined:
                        submitted = True
                        await asyncio.wait_for(
                            service.client(
                                functions.channels.JoinChannelRequest(entity)
                            ),
                            30,
                        )
                if not isinstance(entity, types.Chat) and not (
                    isinstance(entity, types.Channel) and entity.megagroup
                ):
                    return web.json_response({"error": "join_unconfirmed"}, status=503)
                return web.json_response(
                    {
                        "status": "already_joined" if joined else "joined",
                        "id": utils.get_peer_id(entity),
                        "name": entity.title,
                    }
                )
        except errors.InviteRequestSentError:
            return web.json_response({"status": "pending"})
        except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
            return web.json_response({"error": "invite_invalid"}, status=400)
        except errors.ChannelPrivateError:
            if prepare and not invite_hash:
                return web.json_response({"status": "join_required"})
            return web.json_response({"error": "join_denied"}, status=403)
        except (
            errors.UserBannedInChannelError,
            errors.ChannelsTooMuchError,
            errors.UsersTooMuchError,
        ):
            return web.json_response({"error": "join_denied"}, status=403)
        except errors.UserAlreadyParticipantError:
            return web.json_response({"status": "already_joined"})
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return web.json_response({"error": "join_unconfirmed"}, status=503)
        except (errors.FloodWaitError, errors.UnauthorizedError, errors.AuthKeyError):
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Join operation failed: type=%s submitted=%s",
                type(exc).__name__,
                submitted,
            )
            if invite_hash and submitted:
                try:
                    async with service.rpc_lock:
                        checked = await asyncio.wait_for(
                            service.client(
                                functions.messages.CheckChatInviteRequest(invite_hash)
                            ),
                            15,
                        )
                    if isinstance(checked, types.ChatInviteAlready):
                        return web.json_response(
                            {
                                "status": "already_joined",
                                "id": utils.get_peer_id(checked.chat),
                                "name": checked.chat.title,
                            }
                        )
                except Exception:
                    pass
            return web.json_response({"error": "join_unconfirmed"}, status=503)

    if resource == "control":
        if request.method == "PUT":
            data = await request.json()
            if not isinstance(data.get("paused"), bool):
                raise ValueError("Boolean paused value required")
            store.pause(data["paused"])
        return web.json_response(service.status())
    if resource == "watches":
        if request.method == "GET":
            return web.json_response(
                {"items": store.watches(int(request.query.get("group", "0")))}
            )
        data = await request.json()
        if any(
            not isinstance(data.get(k), int) or isinstance(data[k], bool)
            for k in ("group", "person")
        ):
            raise ValueError("Integer IDs required")
        if not isinstance(data.get("enabled"), bool):
            raise ValueError("Boolean enabled value required")
        start = None
        if data.get("start") is not None:
            start = datetime.fromisoformat(data["start"])
            if start.tzinfo is None:
                raise ValueError("UTC offset required")
            start = start.timestamp()
            now = time.time()
            if not data["enabled"] or not now - 90 * 86400 <= start <= now:
                raise ValueError("Start must be within the last 90 days")
        store.configure_watch(data["group"], data["person"], data["enabled"])
        if start is not None:
            with store.db:
                store.db.execute(
                    "INSERT INTO backfills(chat_id,user_id,start,end) VALUES(?,?,?,?) "
                    "ON CONFLICT(chat_id,user_id) DO UPDATE SET start=MIN(backfills.start,excluded.start),end=excluded.end,cursor=0,scanned=0,matched=0,state='pending',error=NULL",
                    (data["group"], data["person"], start, time.time()),
                )
        return web.json_response({"ok": True})
    if resource in ("groups", "people"):
        if request.method == "GET":
            return web.json_response({"items": store.targets(resource)})
        data = await request.json()
        if not isinstance(data["id"], int) or isinstance(data["id"], bool):
            raise ValueError("Integer ID required")
        target_id = data["id"]
        if not isinstance(data["enabled"], bool):
            raise ValueError("Boolean required")
        existing = next(
            (t for t in store.targets(resource) if t["id"] == target_id), None
        )
        name = data.get("name", existing["name"] if existing else str(target_id))
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Display name required")
        if resource == "groups" and data["enabled"]:
            if service.state != "connected" or time.time() < service.retry_at:
                return web.json_response({"error": "account_unavailable"}, status=503)
            access = store.db.execute(
                "SELECT * FROM group_access WHERE id=?", (target_id,)
            ).fetchone()
            if access and access["mode"] == "public":
                async with service.rpc_lock:
                    entity = await asyncio.wait_for(
                        service.client.get_entity(target_id), 30
                    )
                    if not isinstance(entity, types.Channel) or not entity.megagroup:
                        raise ValueError("Public group required")
                    await asyncio.wait_for(
                        service.client.get_messages(entity, limit=1), 30
                    )
                count = store.db.execute(
                    "SELECT COUNT(*) FROM group_access a JOIN targets t ON t.id=a.id AND t.kind='groups' WHERE a.mode='public' AND t.enabled=1 AND a.id!=? AND a.id NOT IN (SELECT id FROM removed_groups)",
                    (target_id,),
                ).fetchone()[0]
                if entity.left and count >= 10:
                    return web.json_response({"error": "public_limit"}, status=409)
                store.configure(resource, target_id, entity.title, True)
                return web.json_response({"ok": True})
            async with service.rpc_lock:
                dialogs = await asyncio.wait_for(service.client.get_dialogs(), 30)
            dialog = next(
                (
                    d
                    for d in dialogs
                    if d.id == target_id
                    and d.is_group
                    and not group_unavailable(getattr(d, "entity", None))
                ),
                None,
            )
            if dialog is None:
                raise ValueError("Join the group using the Telegram client first")
            name = dialog.name
        store.configure(resource, target_id, str(name), data["enabled"])
        return web.json_response({"ok": True})
    if request.method == "GET" and resource == "members":
        group = int(request.query["group"])
        page = int(request.query.get("page", "0"))
        if not 0 <= page <= 100000 or not any(
            g["id"] == group for g in store.targets("groups")
        ):
            raise ValueError("Configured group and nonnegative page required")
        if service.state != "connected" or time.time() < service.retry_at:
            return web.json_response({"error": "account_unavailable"}, status=503)
        try:
            async with service.rpc_lock:
                entity = await asyncio.wait_for(service.client.get_entity(group), 30)
                if isinstance(entity, types.Channel) and entity.megagroup:
                    result = await asyncio.wait_for(
                        service.client(
                            functions.channels.GetParticipantsRequest(
                                channel=entity,
                                filter=types.ChannelParticipantsSearch(""),
                                offset=page * 10,
                                limit=11,
                                hash=0,
                            )
                        ),
                        30,
                    )
                    users_by_id = {u.id: u for u in result.users}
                    users = [
                        users_by_id[p.user_id]
                        for p in result.participants
                        if p.user_id in users_by_id
                    ]
                elif isinstance(entity, types.Chat):
                    result = await asyncio.wait_for(
                        service.client(
                            functions.messages.GetFullChatRequest(entity.id)
                        ),
                        30,
                    )
                    if isinstance(
                        result.full_chat.participants, types.ChatParticipantsForbidden
                    ):
                        return web.json_response(
                            {"error": "members_unavailable"}, status=403
                        )
                    participant_ids = {
                        p.user_id for p in result.full_chat.participants.participants
                    }
                    users = sorted(
                        (u for u in result.users if u.id in participant_ids),
                        key=lambda u: u.id,
                    )[page * 10 : page * 10 + 11]
                else:
                    raise ValueError("Accessible group required")
        except (errors.ChatAdminRequiredError, errors.ChannelPrivateError):
            return web.json_response({"error": "members_unavailable"}, status=403)
        return web.json_response(
            {
                "items": [
                    {
                        "id": u.id,
                        "name": utils.get_display_name(u) or str(u.id),
                        "username": u.username,
                        "selectable": not bool(u.bot or u.deleted or u.is_self),
                    }
                    for u in users[:10]
                ],
                "page": page,
                "has_next": len(users) > 10,
            }
        )
    if request.method == "GET" and resource in ("dialogs", "resolve"):
        if service.state != "connected" or time.time() < service.retry_at:
            return web.json_response({"error": "account_unavailable"}, status=503)
        async with service.rpc_lock:
            if resource == "dialogs":
                dialogs = await asyncio.wait_for(service.client.get_dialogs(), 30)
                return web.json_response(
                    {
                        "items": [
                            {"id": d.id, "name": d.name}
                            for d in dialogs
                            if d.is_group
                            and not group_unavailable(getattr(d, "entity", None))
                        ]
                    }
                )
            value = request.query["user"]
            if value.isdecimal():
                user_id = int(value)
                if user_id <= 0 or user_id > 2**63 - 1:
                    raise ValueError("Invalid user ID")
                return web.json_response({"id": user_id, "name": value})
            entity = await asyncio.wait_for(service.client.get_entity(value), 30)
            if not isinstance(entity, types.User):
                raise ValueError("A user identity is required")
            return web.json_response(
                {"id": entity.id, "name": utils.get_display_name(entity)}
            )
    if request.method == "GET" and resource in ("stats", "messages"):
        values = []
        for key in (
            ("start", "end", "cumulative_start")
            if resource == "stats"
            else ("start", "end")
        ):
            value = datetime.fromisoformat(request.query[key].replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("Timezone is required")
            values.append(value.timestamp())
        start, end = values[:2]
        if start >= end or end - start > 366 * 86400:
            raise ValueError("Invalid window")
        if resource == "stats":
            if not start - 86400 <= values[2] <= start:
                raise ValueError("Invalid cumulative start")
            return web.json_response(
                {
                    "items": store.stats(start, end, values[2]),
                    "health": service.status(values[2], end),
                }
            )
        page = int(request.query.get("page", "0"))
        if not 0 <= page <= 100000:
            raise ValueError("Invalid page")
        result = store.messages(
            start,
            end,
            int(request.query.get("person", "0")),
            int(request.query.get("group", "0")),
            page,
        )
        result["health"] = service.status(start, end)
        if end - start <= 86400:
            person = int(request.query.get("person", "0"))
            group = int(request.query.get("group", "0"))
            result["hours"] = [
                dict(row)
                for row in store.db.execute(
                    "SELECT CAST((sent-?)/3600 AS INTEGER) AS hour, COUNT(*) AS count FROM messages "
                    "WHERE sent>=? AND sent<? AND (?=0 OR user_id=?) AND (?=0 OR chat_id=?) GROUP BY hour ORDER BY hour",
                    (start, start, end, person, person, group, group),
                )
            ]
            result["total"] = sum(row["count"] for row in result["hours"])
        return web.json_response(result)
    raise web.HTTPNotFound()


def create_app(service, token: str) -> web.Application:
    """Construct the local authenticated API.

    Args:
        service: Collector owning the database and Telegram client.
        token: Random secret with at least 32 characters.

    Returns:
        Configured aiohttp application.

    Raises:
        ValueError: If the secret is missing or a template placeholder.
    """
    if len(token) < 32 or token.startswith("REPLACE"):
        raise ValueError("Configure a random API token of at least 32 characters")
    app = web.Application(middlewares=[authenticate], client_max_size=16384)
    app[SERVICE] = service
    app[TOKEN] = token
    app.router.add_get("/v1/{resource}", handle)
    app.router.add_put(
        "/v1/{resource:groups|people|watches|control|join|link_rules|remove_group|remove_person}",
        handle,
    )
    return app
