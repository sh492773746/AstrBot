"""Lightweight room scheduler and authenticated administration surfaces."""

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
import uuid

import aiohttp
from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup as Keyboard
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from astrbot.api import logger
from astrbot.api.star import Star, StarTools
from astrbot.api.web import error_response, json_response, request

from .client import Client, RemoteError

NAME = "astrbot_plugin_rebo_live"
GROUP = -95
OWNER = 1000000001


class ReboLive(Star):
    def __init__(self, context, config):
        super().__init__(context)
        self.config = config
        self.directory = StarTools.get_data_dir(NAME)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.state = {"rooms": {}, "device": uuid.uuid4().hex[:12]}
        if (self.directory / "state.json").exists():
            self.state.update(json.loads((self.directory / "state.json").read_text()))
        self.credentials = {}
        if (self.directory / "credentials.json").exists():
            self.credentials = json.loads(
                (self.directory / "credentials.json").read_text()
            )
        self.tokens = {}
        if (self.directory / "session.json").exists():
            self.tokens = json.loads((self.directory / "session.json").read_text())
        self.remember = bool(self.credentials)
        self.status = "待登录"
        self.visible = {}
        self.pending = {}
        self.state.update(version=2, owner=OWNER)
        self.state.setdefault("grants", {})
        self.state.setdefault("invites", {})
        self.lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()
        self.network = asyncio.Semaphore(2)
        self.wakeup = asyncio.Event()
        self.workers = {}
        self.poll_task = None
        self.checked = 0.0
        self.poll_due = 0.0
        self.backoff = 30
        self.application = None
        self.handlers = []
        self.task = None
        self.http = None
        self.client = None
        self.auto_login_used = False
        self.auth_due = 0
        self.due = {}
        for room in self.state["rooms"].values():
            room.pop("_check_due", None)
            if room.get("result") == "sending":
                room.update(
                    result="uncertain",
                    status="上次回执未确认，等待下一条发送间隔",
                )
                for attempt in room.get("history", []):
                    if attempt["result"] == "sending":
                        attempt["result"] = "uncertain"
        self.save("state", self.state)
        context.register_web_api(
            f"/{NAME}/status", self.web_status, ["GET"], "Room status"
        )
        context.register_web_api(
            f"/{NAME}/action", self.web_action, ["POST"], "Account and room settings"
        )

    def save(self, name, value):
        """Atomically persist private JSON without permissive temporary files.

        Args:
            name: Fixed internal filename stem.
            value: JSON-serializable state.
        """
        path = self.directory / (name + ".json")
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)

    async def initialize(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        self.client = Client(self.http, self.state["device"], self.tokens)
        self.task = asyncio.create_task(self.run())

    async def authenticate(self, explicit=False):
        """Serialize account renewal independently of room administration.

        Args:
            explicit: Whether the owner explicitly requested password login.
        """
        async with self.auth_lock:
            async with self.network:
                await self.renew_account(explicit)

    async def renew_account(self, explicit=False):
        """Refresh once, with bounded password recovery and explicit takeover.

        Args:
            explicit: Whether an administrator explicitly requested login.
        """
        if self.client.tokens and not explicit:
            try:
                data = await self.client.request(
                    "user/auth/token/refresh",
                    {"refresh_token": self.client.tokens["refresh_token"]},
                    authenticated=False,
                )
                self.client.tokens = {
                    k: data[k] for k in ("access_token", "refresh_token")
                }
            except RemoteError as exc:
                if (
                    exc.code == 429
                    or isinstance(exc.code, int)
                    and exc.code >= 500
                    and exc.code < 600
                ):
                    raise
                # A rejected refresh may indicate a revoked session; never fight another login.
                self.status = "登录失效或被挤下线，请点击重新登录"
                self.auth_due = float("inf")
                raise
        elif self.credentials and (explicit or not self.auto_login_used):
            self.auto_login_used = True
            await self.client.login(
                self.credentials["phone"], self.credentials["password"]
            )
        else:
            raise ValueError("请先在插件页面登录")
        self.save("session", self.client.tokens)
        self.status = "已登录"
        self.auth_due = time.time() + 300

    async def action(self, data, actor=OWNER):
        """Serialize administration with scheduling to prevent stale sends.

        Args:
            data: Validated administrator action payload.
            actor: Telegram principal, or the trusted WebUI owner.
        """
        async with self.lock:
            action = data.get("action")
            rid = str(data.get("id", ""))
            if actor != OWNER and (
                action not in ("room", "acknowledge")
                or rid not in self.state["grants"].get(str(actor), [])
            ):
                raise ValueError("无权操作此房间")
            if action == "invite":
                self.state["invites"] = {
                    key: value
                    for key, value in self.state["invites"].items()
                    if value["expires"] > time.time() and value["rooms"]
                }
                rooms = list(dict.fromkeys(str(x) for x in data.get("rooms", [])))
                if not rooms or any(x not in self.state["rooms"] for x in rooms):
                    raise ValueError("请选择已配置房间")
                token = secrets.token_urlsafe(24)
                digest = hashlib.sha256(token.encode()).hexdigest()
                self.state["invites"][digest] = {
                    "rooms": rooms,
                    "expires": time.time() + 86400,
                }
                self.save("state", self.state)
                return {"token": token}
            if action == "revoke_invites":
                self.state["invites"].clear()
                self.save("state", self.state)
                return
            if action in ("grant", "revoke"):
                user = str(int(data["user"]))
                if int(user) == OWNER:
                    raise ValueError("不能修改主管理员权限")
                rooms = list(dict.fromkeys(str(x) for x in data.get("rooms", [])))
                if any(x not in self.state["rooms"] for x in rooms):
                    raise ValueError("房间已删除")
                if action == "revoke":
                    self.state["grants"].pop(user, None)
                else:
                    self.state["grants"][user] = rooms
                self.pending.pop(int(user), None)
                self.save("state", self.state)
                return
            if action in ("delete", "acknowledge"):
                room = self.state["rooms"].get(rid)
                if not room:
                    raise ValueError("房间已删除")
                if action == "delete":
                    if room["enabled"] or room.get("result") == "sending":
                        raise ValueError("请先暂停并等待发送结束")
                    del self.state["rooms"][rid]
                    for rooms in self.state["grants"].values():
                        if rid in rooms:
                            rooms.remove(rid)
                    for invite in self.state["invites"].values():
                        invite["rooms"] = [x for x in invite["rooms"] if x != rid]
                else:
                    if room.get("result") != "uncertain":
                        raise ValueError("没有待核查发送")
                    room.update(
                        result="reviewed", enabled=True, status="已核查，等待完整间隔"
                    )
                self.due.pop(rid, None)
                self.save("state", self.state)
                return
            if action == "login":
                phone, password = (
                    str(data.get("phone", "")).strip(),
                    data.get("password", ""),
                )
                if (
                    not phone.isdecimal()
                    or not isinstance(password, str)
                    or not password
                ):
                    raise ValueError("请输入账号和密码")
                async with self.auth_lock:
                    async with self.network:
                        await self.client.login(phone, password)
                self.credentials = {"phone": phone, "password": password}
                self.remember = data.get("remember") is True
                if self.remember:
                    self.save("credentials", self.credentials)
                else:
                    (self.directory / "credentials.json").unlink(missing_ok=True)
                self.save("session", self.client.tokens)
                self.status, self.auth_due = "已登录", time.time() + 300
                self.due.clear()
            elif action == "relogin":
                await self.authenticate(explicit=True)
                self.due.clear()
            elif action == "logout":
                self.credentials = {}
                self.client.tokens = {}
                self.remember = False
                self.status = "已退出"
                self.auth_due = float("inf")
                self.visible = {}
                for name in ("credentials", "session"):
                    (self.directory / (name + ".json")).unlink(missing_ok=True)
                for room in self.state["rooms"].values():
                    room["enabled"] = False
                self.due.clear()
            elif action == "refresh":
                if self.status != "已登录":
                    raise ValueError("请先登录或重新登录")
                async with self.network:
                    self.visible = await self.client.rooms()
            elif action == "room":
                rid = str(data.get("id", ""))
                if rid not in self.state["rooms"]:
                    if rid not in self.visible or len(self.state["rooms"]) >= 10:
                        raise ValueError("请选择可见直播间，最多配置 10 个")
                    room = {
                        "title": self.visible[rid]["title"],
                        "anchor_name": self.visible[rid].get("anchor_name", ""),
                        "room_no": self.visible[rid].get("room_no"),
                        "text": "",
                        "interval": 300,
                        "enabled": False,
                        "status": "已暂停",
                    }
                else:
                    room = self.state["rooms"][rid]
                updated = dict(room)
                if "text_op" in data:
                    if data.get("revision") != room.get("revision", 0):
                        raise ValueError("文案已变更，请刷新后重试")
                    texts = list(
                        room.get("texts")
                        or [t for t in (room.get("text"), room.get("text2")) if t]
                    )
                    operation = data["text_op"]
                    value = data.get("value")
                    if operation in ("add", "edit") and (
                        not isinstance(value, str) or not 1 <= len(value.strip()) <= 300
                    ):
                        raise ValueError("每条文案请输入1—300字")
                    cursor = room.get("cursor", 0)
                    if operation == "add":
                        texts.append(value.strip())
                    elif operation in ("edit", "delete"):
                        index = data.get("index")
                        if type(index) is not int or not 0 <= index < len(texts):
                            raise ValueError("文案不存在，请刷新")
                        if operation == "edit":
                            texts[index] = value.strip()
                        else:
                            if len(texts) == 1:
                                raise ValueError("至少保留一条文案")
                            texts.pop(index)
                            if index < cursor:
                                cursor -= 1
                    else:
                        raise ValueError("未知文案操作")
                    updated.update(
                        texts=texts,
                        text=texts[0],
                        text2=texts[1] if len(texts) > 1 else "",
                        cursor=cursor % len(texts),
                    )
                if "texts" in data:
                    texts = data["texts"]
                    if (
                        not isinstance(texts, list)
                        or not texts
                        or any(
                            not isinstance(t, str) or not 1 <= len(t.strip()) <= 300
                            for t in texts
                        )
                    ):
                        raise ValueError("至少一条文案，每条1—300字")
                    updated["texts"] = [t.strip() for t in texts]
                    updated["text"] = updated["texts"][0]
                    updated["text2"] = updated["texts"][1] if len(texts) > 1 else ""
                    updated["cursor"] = 0
                if "text2" in data:
                    if (
                        not isinstance(data["text2"], str)
                        or len(data["text2"].strip()) > 300
                    ):
                        raise ValueError("第二条弹幕最多 300 个字符")
                    updated["text2"] = data["text2"].strip()
                if "text" in data:
                    if (
                        not isinstance(data["text"], str)
                        or not 1 <= len(data["text"].strip()) <= 300
                    ):
                        raise ValueError("文案请输入 1—300 个字符")
                    updated["text"] = data["text"].strip()
                if "texts" not in data and ("text" in data or "text2" in data):
                    texts = list(updated.get("texts") or [updated.get("text", "")])
                    texts[0] = updated.get("text", "")
                    if "text2" in data:
                        if len(texts) < 2:
                            texts.append(updated.get("text2", ""))
                        else:
                            texts[1] = updated.get("text2", "")
                    updated["texts"] = [t for t in texts if t]
                if "item_interval" in data:
                    value = data["item_interval"]
                    if (
                        isinstance(value, bool)
                        or not str(value).isdecimal()
                        or not 1 <= int(value) <= 3600
                    ):
                        raise ValueError("每条间隔请输入1—3600的整数秒")
                    updated["item_interval"] = int(value)
                if "interval" in data:
                    raw_interval = data["interval"]
                    if (
                        isinstance(raw_interval, bool)
                        or not str(raw_interval).isdecimal()
                    ):
                        raise ValueError("每轮间隔请输入整数秒")
                    interval = int(raw_interval)
                    if not 30 <= interval <= 86400:
                        raise ValueError("每轮间隔应为 30—86400 秒")
                    updated["interval"] = interval
                if "enabled" in data:
                    if not isinstance(data["enabled"], bool):
                        raise ValueError("启用状态必须为布尔值")
                    if data["enabled"] and (
                        not updated["text"] or self.status != "已登录"
                    ):
                        raise ValueError("请先登录并设置文案")
                    updated["enabled"] = data["enabled"]
                    if data["enabled"]:
                        updated["preflight_failures"] = 0
                updated["revision"] = room.get("revision", 0) + 1
                if rid in self.state["rooms"]:
                    room.update(updated)
                else:
                    self.state["rooms"][rid] = updated
                if data.get("enabled") is True:
                    self.state["rooms"][rid].pop("retry_at", None)
                    self.state["rooms"][rid].pop("retry_delay", None)
                if "interval" in data or "item_interval" in data or "enabled" in data:
                    self.due.pop(rid, None)
                    self.state["rooms"][rid]["status"] = (
                        "等待检查开播" if updated["enabled"] else "已暂停"
                    )
                    self.state["rooms"][rid]["_check_due"] = 0
            else:
                raise ValueError("未知操作")
            self.save("state", self.state)
            self.wakeup.set()

    async def web_status(self):
        return json_response(
            {
                "account": self.status,
                "remember": self.remember,
                "rooms": self.state["rooms"],
                "visible": self.visible,
                "next_send": {
                    rid: time.time() + max(0, due - time.monotonic())
                    for rid, due in self.due.items()
                },
                "grants": self.state["grants"],
            }
        )

    async def web_action(self):
        try:
            result = await self.action(await request.json(default={}))
            return json_response({"ok": True, **(result or {})})
        except (ValueError, RemoteError) as exc:
            return error_response(str(exc))
        except Exception as exc:
            logger.warning("Rebo page request failed: %s", type(exc).__name__)
            return error_response("网络请求失败，请稍后重试")

    async def telegram(self, update, context):
        """Authorize every private interaction before accessing room settings.

        Args:
            update: Telegram update from the configured adapter.
            context: Existing Telegram callback context.
        """
        query = update.callback_query
        uid = update.effective_user.id
        text = update.effective_message.text or ""
        navigation = {
            "📺 我的直播间": "menu",
            "📊 运行状态": "menu",
            "📝 发送记录": "records",
            "🔑 授权管理": "auth",
        }
        if (
            not query
            and text.startswith("/start ")
            and update.effective_chat.type == "private"
        ):
            digest = hashlib.sha256(
                text.split(maxsplit=1)[1].strip().encode()
            ).hexdigest()
            async with self.lock:
                invite = self.state["invites"].pop(digest, None)
                if invite and invite["expires"] > time.time() and invite["rooms"]:
                    self.state["grants"][str(uid)] = sorted(
                        set(self.state["grants"].get(str(uid), []) + invite["rooms"])
                    )
                    self.save("state", self.state)
                else:
                    await context.bot.send_message(
                        update.effective_chat.id, "授权链接已过期、已领取或已撤销"
                    )
                    raise ApplicationHandlerStop
        if (
            not query
            and not text.startswith(("/rblive", "/start"))
            and text not in navigation
            and uid not in self.pending
        ):
            raise ApplicationHandlerStop
        if update.effective_chat.type != "private" or (
            uid != OWNER and not self.state["grants"].get(str(uid))
        ):
            if query:
                await query.answer("无权访问", show_alert=True)
            elif update.effective_chat.type == "private":
                await context.bot.send_message(
                    update.effective_chat.id,
                    "尚未获得房间授权，请打开主管理员提供的授权链接并点击开始。"
                    f"\n你的 Telegram ID：{uid}",
                )
            logger.info("Rebo Telegram access denied: user_id=%s", uid)
            raise ApplicationHandlerStop
        try:
            if query:
                await query.answer()
            command = (
                query.data.split(":") if query else ["rb", navigation.get(text, "menu")]
            )
            if not query and (
                text.startswith(("/start", "/rblive")) or text in navigation
            ):
                self.pending.pop(uid, None)
                keys = [["📺 我的直播间", "📊 运行状态"], ["📝 发送记录"]]
                if uid == OWNER:
                    keys[1].append("🔑 授权管理")
                await context.bot.send_message(
                    update.effective_chat.id,
                    "📺 热播 Live · 请使用下方菜单",
                    reply_markup=ReplyKeyboardMarkup(
                        keys, resize_keyboard=True, is_persistent=True
                    ),
                )
            if uid != OWNER and command[1] not in (
                "menu",
                "detail",
                "text",
                "text2",
                "texts",
                "addtext",
                "interval",
                "item_interval",
                "toggle",
                "history",
                "records",
                "acknowledge",
            ):
                raise ValueError("仅主管理员可操作")
            if command[1] in (
                "detail",
                "text",
                "text2",
                "texts",
                "addtext",
                "interval",
                "item_interval",
                "toggle",
                "history",
                "acknowledge",
            ):
                if (
                    command[2] not in self.state["rooms"]
                    and command[2] not in self.visible
                ):
                    raise ValueError("房间已删除，请刷新列表")
                if uid != OWNER and command[2] not in self.state["grants"].get(
                    str(uid), []
                ):
                    raise ValueError("此房间授权已撤销")
            if command[1] == "invite":
                result = await self.action(
                    {"action": "invite", "rooms": [command[2]]}, actor=uid
                )
                await query.edit_message_text(
                    f"一次性授权链接（24 小时有效）：\nhttps://t.me/{context.bot.username}?start={result['token']}",
                    reply_markup=Keyboard([[Button("返回", callback_data="rb:menu")]]),
                )
                raise ApplicationHandlerStop
            if command[1] in ("delete", "acknowledge"):
                await self.action({"action": command[1], "id": command[2]}, actor=uid)
            if command[1] == "revoke":
                await self.action({"action": "revoke", "user": command[2]}, actor=uid)
            if command[1] == "revokeinvites":
                await self.action({"action": "revoke_invites"}, actor=uid)
            if command[1] in ("scope", "scopeflip"):
                user = command[2]
                if command[1] == "scopeflip":
                    rooms = list(self.state["grants"].get(user, []))
                    rid = command[3]
                    if rid in rooms:
                        rooms.remove(rid)
                    else:
                        rooms.append(rid)
                    await self.action(
                        {"action": "grant", "user": user, "rooms": rooms}, actor=uid
                    )
                rows = [
                    [
                        Button(
                            (
                                "🟢 "
                                if rid in self.state["grants"].get(user, [])
                                else "🔴 "
                            )
                            + room["title"][:28],
                            callback_data=f"rb:scopeflip:{user}:{rid}",
                        )
                    ]
                    for rid, room in self.state["rooms"].items()
                ]
                rows.append([Button("返回授权管理", callback_data="rb:auth")])
                await query.edit_message_text(
                    "选择允许操作的房间：" + user, reply_markup=Keyboard(rows)
                )
                raise ApplicationHandlerStop
            if command[1] == "auth":
                rows = [
                    [
                        Button(
                            "🔗 授权：" + room["title"][:24],
                            callback_data=f"rb:invite:{rid}",
                        )
                    ]
                    for rid, room in self.state["rooms"].items()
                ] + [
                    [
                        Button(f"调整 {user}", callback_data=f"rb:scope:{user}"),
                        Button("撤销", callback_data=f"rb:revoke:{user}"),
                    ]
                    for user in self.state["grants"]
                ]
                rows.append(
                    [Button("撤销所有未领取链接", callback_data="rb:revokeinvites")]
                )
                rows.append([Button("返回", callback_data="rb:menu")])
                response = (
                    "🔑 授权管理\n点击房间生成一次性授权链接。\n\n已授权使用者\n"
                    + "\n".join(
                        f"{user}：{len(rooms)} 个房间"
                        for user, rooms in self.state["grants"].items()
                    )
                )
                if query:
                    await query.edit_message_text(response, reply_markup=Keyboard(rows))
                else:
                    await context.bot.send_message(
                        update.effective_chat.id, response, reply_markup=Keyboard(rows)
                    )
                raise ApplicationHandlerStop
            if command[1] in ("detail", "history"):
                rid = command[2]
                room = self.state["rooms"].get(rid, {})
                visible = self.visible.get(rid, {})
                texts = room.get("texts") or [
                    t for t in (room.get("text"), room.get("text2")) if t
                ]
                page = max(
                    0,
                    min(
                        int(command[3]) if len(command) > 3 else 0,
                        max(0, (len(texts) - 1) // 5),
                    ),
                )
                lines = [
                    visible.get("title") or room.get("title") or rid,
                    "🎙 主播："
                    + (
                        visible.get("anchor_name")
                        or room.get("anchor_name")
                        or "暂未获取"
                    ),
                    "状态：" + room.get("status", "尚未配置"),
                    f"📝 已保存 {len(texts)} 条文案 · 第{page + 1}页",
                    *[
                        f"\n第{i + 1}条：\n{value}"
                        for i, value in enumerate(texts)
                        if page * 5 <= i < (page + 1) * 5
                    ],
                    f"每条间隔：{room.get('item_interval', 2)} 秒 · 每轮间隔：{room.get('interval', 300)} 秒",
                    "最近发送："
                    + (
                        time.strftime(
                            "%m-%d %H:%M:%S", time.gmtime(room["last_send"] + 28800)
                        )
                        if room.get("last_send")
                        else "暂无"
                    ),
                    "下次发送："
                    + (
                        time.strftime(
                            "%m-%d %H:%M:%S",
                            time.gmtime(
                                time.time()
                                + max(0, self.due[rid] - time.monotonic())
                                + 28800
                            ),
                        )
                        if rid in self.due
                        else "暂无"
                    ),
                ]
                rows = [
                    [
                        Button("✏️ 弹幕1", callback_data=f"rb:text:{rid}"),
                        Button("➕ 新增文案", callback_data=f"rb:addtext:{rid}"),
                        Button("⏱ 每轮间隔", callback_data=f"rb:interval:{rid}"),
                        Button("⏱ 每条间隔", callback_data=f"rb:item_interval:{rid}"),
                    ],
                    [
                        Button(
                            "🔴 暂停" if room.get("enabled") else "🟢 启用",
                            callback_data=f"rb:toggle:{rid}",
                        )
                    ],
                ]
                navigation_rows = []
                if page:
                    navigation_rows.append(
                        Button("上一页", callback_data=f"rb:detail:{rid}:{page - 1}")
                    )
                if (page + 1) * 5 < len(texts):
                    navigation_rows.append(
                        Button("下一页", callback_data=f"rb:detail:{rid}:{page + 1}")
                    )
                if navigation_rows:
                    rows.append(navigation_rows)
                labels = {
                    "confirmed": "聊天室已回执（未核验公屏）",
                    "uncertain": "待确认",
                    "rejected": "被拒绝",
                    "sending": "发送中",
                }
                for attempt in (
                    room.get("history", [])[-20:] if command[1] == "history" else []
                ):
                    lines.append(
                        f"{time.strftime('%m-%d %H:%M:%S', time.gmtime(attempt['time'] + 28800))} · {labels.get(attempt['result'], attempt['result'])}"
                        + (
                            " · 原因：" + attempt["reason"]
                            if attempt.get("reason")
                            else ""
                        )
                    )
                if uid == OWNER and rid in self.state["rooms"]:
                    rows.append(
                        [Button("🔗 生成授权链接", callback_data=f"rb:invite:{rid}")]
                    )
                    if not room.get("enabled"):
                        rows.append(
                            [Button("删除配置", callback_data=f"rb:delete:{rid}")]
                        )
                rows.append([Button("返回", callback_data="rb:menu")])
                await query.edit_message_text(
                    "\n".join(lines)[:3900], reply_markup=Keyboard(rows)
                )
                raise ApplicationHandlerStop
            if (
                not query
                and uid in self.pending
                and not text.startswith(("/rblive", "/start"))
                and text not in navigation
            ):
                field, rid, expiry = self.pending.pop(uid)
                if time.time() > expiry:
                    raise ValueError("输入已超时，请重新选择")
                await self.action(
                    {
                        "action": "room",
                        "id": rid,
                        **(
                            {
                                "text_op": "add",
                                "value": text,
                                "revision": self.text_revisions.pop(uid, -1),
                            }
                            if field == "addtext"
                            else {
                                field: text.split("\n---\n")
                                if field == "texts"
                                else (
                                    "" if field == "text2" and text == "清空" else text
                                )
                            }
                        ),
                    },
                    actor=uid,
                )
                if field == "addtext":
                    saved = self.state["rooms"][rid]["texts"]
                    count = len(saved)
                    duplicate = any(value == saved[-1] for value in saved[:-1])
                    await context.bot.send_message(
                        update.effective_chat.id,
                        f"✅ 第{count}条已保存 · 当前共{count}条\n\n{saved[-1]}"
                        + (
                            "\n\n⚠️ 与已有文案相同，已保留；如非有意重复，请在后台删除多余项。"
                            if duplicate
                            else ""
                        ),
                        reply_markup=Keyboard(
                            [
                                [
                                    Button(
                                        "➕ 继续添加", callback_data=f"rb:addtext:{rid}"
                                    ),
                                    Button(
                                        f"📝 查看文案（{count}）",
                                        callback_data=f"rb:detail:{rid}:{(count - 1) // 5}",
                                    ),
                                ]
                            ]
                        ),
                    )
                    raise ApplicationHandlerStop
            elif len(command) > 2 and command[1] in (
                "text",
                "text2",
                "texts",
                "addtext",
                "interval",
                "item_interval",
            ):
                self.pending[uid] = (command[1], command[2], time.time() + 600)
                if command[1] == "addtext":
                    if not hasattr(self, "text_revisions"):
                        self.text_revisions = {}
                    self.text_revisions[uid] = self.state["rooms"][command[2]].get(
                        "revision", 0
                    )
                    await query.edit_message_text(
                        "➕ 发送一条新文案（1—300字，可换行）。保存后追加到末尾，不覆盖已有文案；继续添加请再次点击「新增文案」。",
                        reply_markup=Keyboard(
                            [[Button("取消", callback_data="rb:menu")]]
                        ),
                    )
                    raise ApplicationHandlerStop
                await query.edit_message_text(
                    "请输入全部文案，每条最多300字；独立一行 --- 分隔不同条。按列表顺序循环，每条之间使用设定间隔。"
                    if command[1] == "texts"
                    else "当前文案："
                    + self.state["rooms"].get(command[2], {}).get(command[1], "未设置")
                    + "\n✏️ 请输入弹幕（最多 300 字；第二条发送“清空”可移除）"
                    if command[1] in ("text", "text2")
                    else "⏱ 请输入每条间隔秒数（1—3600）"
                    if command[1] == "item_interval"
                    else "⏱ 请输入每轮间隔秒数（30—86400）",
                    reply_markup=Keyboard([[Button("取消", callback_data="rb:menu")]]),
                )
                raise ApplicationHandlerStop
            elif command[1] == "toggle":
                rid = command[2]
                await self.action(
                    {
                        "action": "room",
                        "id": rid,
                        "enabled": not self.state["rooms"]
                        .get(rid, {})
                        .get("enabled", False),
                    },
                    actor=uid,
                )
            elif command[1] in ("refresh", "relogin"):
                await self.action({"action": command[1]}, actor=uid)
            self.pending.pop(uid, None)
            lines = ["📺 热播 Live", "", "账号：" + self.status]
            result_labels = {
                "confirmed": "聊天室已回执（未核验公屏）",
                "rejected": "发送被拒绝",
                "uncertain": "结果待确认",
                "sending": "发送中",
            }
            rows = [
                [
                    Button("🔄 刷新房间", callback_data="rb:refresh"),
                    Button("🔑 重新登录", callback_data="rb:relogin"),
                ]
            ]
            if uid != OWNER:
                rows = []
            else:
                rows.append([Button("🔑 授权管理", callback_data="rb:auth")])
            page = int(command[2]) if command[1] == "menu" and len(command) > 2 else 0
            items = [
                (rid, v)
                for rid, v in {**self.visible, **self.state["rooms"]}.items()
                if uid == OWNER or rid in self.state["grants"].get(str(uid), [])
            ]
            for rid, visible in items[page * 8 : page * 8 + 8]:
                room = self.state["rooms"].get(rid, {})
                title = visible.get("title") or rid
                rows.append(
                    [
                        Button(
                            "📺 " + title[:28],
                            callback_data=f"rb:{'history' if command[1] == 'records' else 'detail'}:{rid}",
                        )
                    ]
                )
                anchor = (
                    self.visible.get(rid, {}).get("anchor_name")
                    or room.get("anchor_name")
                    or "暂未获取"
                )
                lines.append(
                    f"\n{title}\n🎙 主播：{anchor}\n{'🟢' if room.get('enabled') else '🔴'} {room.get('status', '尚未配置')}\n每条间隔 {room.get('item_interval', 2)} 秒 · 每轮间隔 {room.get('interval', 300)} 秒 · 最近结果 {result_labels.get(room.get('result'), '暂无')}"
                )
                rows.append(
                    [
                        Button(
                            ("🔴 暂停 " if room.get("enabled") else "🟢 启用 ")
                            + title[:20],
                            callback_data=f"rb:toggle:{rid}",
                        ),
                        Button("✏️ 弹幕1", callback_data=f"rb:text:{rid}"),
                        Button(
                            f"📝 查看文案（{len(room.get('texts') or [t for t in (room.get('text'), room.get('text2')) if t])}）",
                            callback_data=f"rb:detail:{rid}",
                        ),
                        Button("➕ 新增文案", callback_data=f"rb:addtext:{rid}"),
                        Button("⏱ 每轮间隔", callback_data=f"rb:interval:{rid}"),
                        Button("⏱ 每条间隔", callback_data=f"rb:item_interval:{rid}"),
                    ]
                )
            rows.append([Button("📡 刷新状态", callback_data="rb:menu")])
            if page:
                rows.append([Button("上一页", callback_data=f"rb:menu:{page - 1}")])
            if (page + 1) * 8 < len(items):
                rows.append([Button("下一页", callback_data=f"rb:menu:{page + 1}")])
            if query:
                await query.edit_message_text(
                    "\n".join(lines)[:3900], reply_markup=Keyboard(rows)
                )
            else:
                await context.bot.send_message(
                    update.effective_chat.id,
                    "\n".join(lines)[:3900],
                    reply_markup=Keyboard(rows),
                )
                logger.info("Rebo Telegram menu delivered: user_id=%s", uid)
        except ApplicationHandlerStop:
            raise
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Rebo Telegram update rejected")
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, (ValueError, RemoteError))
                else "操作未完成，请刷新状态后重试"
            )
            if query:
                await query.edit_message_text(
                    message,
                    reply_markup=Keyboard([[Button("返回", callback_data="rb:menu")]]),
                )
            else:
                await context.bot.send_message(update.effective_chat.id, message)
        raise ApplicationHandlerStop

    async def poll_rooms(self):
        """Refresh read-only room visibility with bounded authentication recovery."""
        try:
            if time.time() >= self.auth_due and (
                self.client.tokens or self.credentials
            ):
                await self.authenticate()
            if self.status != "已登录":
                return
            for attempt in range(2):
                try:
                    async with self.network:
                        visible = await self.client.rooms()
                    self.visible = visible
                    # The provider rotates live session IDs; room_no identifies the room.
                    for old_id, room in list(self.state["rooms"].items()):
                        if old_id in visible:
                            room["room_no"] = visible[old_id].get(
                                "room_no"
                            ) or room.get("room_no")
                            room["anchor_name"] = visible[old_id].get(
                                "anchor_name"
                            ) or room.get("anchor_name")
                            continue
                        matches = [
                            rid
                            for rid, v in visible.items()
                            if room.get("room_no")
                            and v.get("room_no") == room["room_no"]
                        ]
                        if len(matches) != 1 or matches[0] in self.state["rooms"]:
                            continue
                        new_id = matches[0]
                        self.state["rooms"][new_id] = self.state["rooms"].pop(old_id)
                        room["anchor_name"] = visible[new_id].get(
                            "anchor_name"
                        ) or room.get("anchor_name")
                        room["revision"] = room.get("revision", 0) + 1
                        self.due.pop(old_id, None)
                        for scope in self.state["grants"].values():
                            scope[:] = [
                                new_id if rid == old_id else rid for rid in scope
                            ]
                        for invite in self.state["invites"].values():
                            invite["rooms"] = [
                                new_id if rid == old_id else rid
                                for rid in invite["rooms"]
                            ]
                        self.pending.clear()
                    self.save("state", self.state)
                    self.checked = time.monotonic()
                    self.backoff = 30
                    return
                except RemoteError as exc:
                    if exc.code != 401 or attempt:
                        raise
                    await self.authenticate()
        except Exception as exc:
            self.checked = 0
            self.due.clear()
            self.backoff = min(300, self.backoff * 2)
            if isinstance(exc, RemoteError) and exc.code == 401:
                self.status = "登录失效，请点击重新登录"
                self.auth_due = float("inf")
            logger.warning("Rebo visibility deferred: %s", type(exc).__name__)
        finally:
            self.poll_due = time.monotonic() + self.backoff
            self.wakeup.set()

    async def room_step(self, rid):
        """Check and send one room without holding the administration lock.

        Args:
            rid: Configured room ID.
        """
        room = self.state["rooms"].get(rid)
        if not room or not room["enabled"] or self.status != "已登录":
            self.due.pop(rid, None)
            return
        revision = room.get("revision", 0)
        now = time.monotonic()
        live = self.visible.get(rid)
        if now - self.checked > 60 or not live or live.get("status") != 1:
            room["status"] = "未开播、状态过期或当前不可见"
            self.due.pop(rid, None)
            self.save("state", self.state)
            return
        if live.get("anchor_name"):
            room["anchor_name"] = live["anchor_name"]
        try:
            async with self.network:
                await self.client.request(f"room/info?room_id={rid}")
                # The room-wide mute flag only blocks ordinary members.  This
                # account may be an administrator, so do not stop scheduling
                # here; `SEND_MESSAGE` is the authoritative capability check.
                # Keeping the scheduler alive also lets a newly promoted
                # administrator recover without waiting for a manual toggle.
                if (
                    not room["enabled"]
                    or revision != room.get("revision", 0)
                    or self.state["rooms"].get(rid) is not room
                ):
                    return
                room["status"] = "直播中，等待发送"
                self.due.setdefault(rid, time.monotonic() + room["interval"])
                if time.monotonic() < self.due[rid]:
                    return
                ws = await self.client.connect(rid)
                if ws is None:
                    self.due.pop(rid, None)
                    return
                async with ws:
                    if (
                        not room["enabled"]
                        or revision != room.get("revision", 0)
                        or self.state["rooms"].get(rid) is not room
                        or self.status != "已登录"
                        or time.monotonic() - self.checked > 60
                    ):
                        return
                    room["preflight_failures"] = 0
                    room.pop("retry_at", None)
                    room.pop("retry_delay", None)
                    texts = room.get("texts") or [
                        t for t in [room["text"], room.get("text2", "")] if t
                    ]
                    cursor = room.get("cursor", 0) % len(texts)
                    for part, content in [(cursor, texts[cursor])]:
                        if not content:
                            continue
                        if (
                            not room["enabled"]
                            or revision != room.get("revision", 0)
                            or self.state["rooms"].get(rid) is not room
                            or self.status != "已登录"
                            or time.monotonic() - self.checked > 60
                        ):
                            break
                        attempt = {
                            "request_id": uuid.uuid4().hex,
                            "time": time.time(),
                            "result": "sending",
                            "part": part + 1,
                        }
                        history = room.setdefault("history", [])
                        history.append(attempt)
                        del history[:-20]
                        # Advance before network I/O; unknown sends are never replayed.
                        room["cursor"] = (cursor + 1) % len(texts)
                        room.update(
                            result="sending",
                            request_id=attempt["request_id"],
                            status="正在发送，已提交消息无法撤回",
                        )
                        self.save("state", self.state)
                        diagnostics = {}
                        try:
                            message_id = await self.client.send(
                                ws, content, attempt["request_id"], diagnostics
                            )
                            attempt.update(result="confirmed", message_id=message_id)
                            room.update(
                                result="confirmed",
                                last_send=time.time(),
                                message_id=message_id,
                            )
                        except RemoteError as exc:
                            reason = str(exc.code)
                            if exc.business_code is not None:
                                reason += f" / {exc.business_code}"
                            reason = {
                                2010392001: "内容触发平台敏感词校验",
                                2010122004: "房间已开启全员禁言",
                            }.get(exc.business_code, f"平台拒绝（{reason}）")
                            attempt.update(result="rejected", reason=reason)
                            room.update(result="rejected", status=reason)
                            if exc.business_code == 2010122004:
                                # A room can report the ordinary-member mute
                                # flag while this account is being promoted
                                # or while permissions are propagating. Keep
                                # the room enabled and retry on the next
                                # interval instead of requiring manual re-
                                # enablement.
                                room["retry_at"] = time.time() + 30
                                room["status"] = "当前账号暂不可发言，30秒后重试"
                            else:
                                room["enabled"] = False
                        except BaseException as exc:
                            reason = diagnostics.get("reason", "unexpected_error")
                            description = {
                                "receipt_timeout": "等待回执超时",
                                "connection_error": "聊天室连接异常或已关闭",
                                "invalid_receipt": "回执格式异常",
                                "task_cancelled": "发送任务被取消",
                                "unexpected_error": "发送期间发生异常",
                            }.get(reason, "回执不可用")
                            diagnostics.setdefault("exception", type(exc).__name__)
                            attempt.update(
                                result="uncertain",
                                reason=f"{description}（{reason}）",
                                diagnostics=diagnostics,
                            )
                            logger.warning(
                                "Rebo receipt uncertain: request=%s diagnostics=%s",
                                attempt["request_id"],
                                json.dumps(diagnostics, sort_keys=True),
                            )
                            room.update(
                                result="uncertain",
                                status="回执未确认，按间隔继续下一条（不补发）",
                            )
                            self.save("state", self.state)
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                        attempt["diagnostics"] = diagnostics
                        self.save("state", self.state)
                    if room["enabled"]:
                        self.due[rid] = time.monotonic() + (
                            room["interval"]
                            if room.get("cursor", 0) == 0
                            else room.get("item_interval", 2)
                        )
                        room["status"] = (
                            "本轮发送结束，等待下一轮"
                            if room.get("cursor", 0) == 0
                            else f"本条处理结束，{room.get('item_interval', 2)}秒后发送下一条"
                        )
                    else:
                        self.due.pop(rid, None)
        except RemoteError as exc:
            if (
                not room["enabled"]
                or revision != room.get("revision", 0)
                or self.state["rooms"].get(rid) is not room
            ):
                return
            room["status"] = f"服务拒绝（{exc.code}）"
            room["last_service_error"] = {
                "code": exc.code,
                "endpoint": exc.endpoint,
                "at": time.time(),
            }
            if exc.code == 401:
                self.due.pop(rid, None)
                self.checked = 0
                self.poll_due = 0
            elif exc.code in {2001011, 2010132002} and exc.endpoint in {
                "room/info",
                "room/generate-chat-token",
            }:
                # No message was submitted. Retain the pending deadline and
                # cursor; retry_at gates scheduling without replaying a send.
                count = room.get("preflight_failures", 0) + 1
                room["preflight_failures"] = count
                self.poll_due = 0
                stage = "房间信息" if exc.endpoint == "room/info" else "聊天室授权"
                if count <= 3:
                    delay = (30, 60, 120)[count - 1]
                    room["retry_at"] = time.time() + delay
                    room["status"] = (
                        f"{stage}接口暂拒（{exc.code}），"
                        f"{delay}秒后第{count}次重查；未提交弹幕"
                    )
                else:
                    room["enabled"] = False
                    room.pop("retry_at", None)
                    self.due.pop(rid, None)
                    room["status"] = (
                        f"{stage}接口持续拒绝（{exc.code}），三次重查后暂停；未提交弹幕"
                    )
            elif exc.code == 429 or isinstance(exc.code, int) and 500 <= exc.code < 600:
                self.due.pop(rid, None)
                room["retry_delay"] = min(300, room.get("retry_delay", 15) * 2)
                room["retry_at"] = time.time() + room["retry_delay"]
            else:
                self.due.pop(rid, None)
                room["enabled"] = False
        except (aiohttp.ClientError, TimeoutError, ConnectionError):
            if (
                not room["enabled"]
                or revision != room.get("revision", 0)
                or self.state["rooms"].get(rid) is not room
            ):
                return
            self.due.pop(rid, None)
            if room.get("result") != "uncertain":
                room["status"] = "网络异常，等待下次检查"
                room["retry_at"] = time.time() + 30
        finally:
            self.save("state", self.state)

    async def tick(self):
        """Schedule polling and independent room work using monotonic deadlines."""
        now = time.monotonic()
        if (self.poll_task is None or self.poll_task.done()) and now >= self.poll_due:
            self.poll_task = asyncio.create_task(self.poll_rooms())
        for rid, room in list(self.state["rooms"].items()):
            worker = self.workers.get(rid)
            if worker and not worker.done():
                continue
            if worker:
                try:
                    worker.result()
                except Exception as exc:
                    logger.warning("Rebo room task failed: %s", type(exc).__name__)
            if not room["enabled"]:
                self.due.pop(rid, None)
                continue
            if time.time() < room.get("retry_at", 0):
                continue
            check_due = room.get("_check_due", 0)
            if now >= min(check_due, self.due.get(rid, float("inf"))):
                room["_check_due"] = now + 30
                self.workers[rid] = asyncio.create_task(self.room_step(rid))

    async def stop_unrelated(self, update, context):
        """Keep the dedicated bot outside unrelated plugin and AI handlers.

        Args:
            update: Incoming update on the bound bot.
            context: Telegram callback context.
        """
        raise ApplicationHandlerStop

    async def run(self):
        while True:
            try:
                platform = self.context.get_platform_inst(
                    self.config.get("platform_id", "")
                )
                application = getattr(platform, "application", None)
                if application is not self.application:
                    if self.application:
                        for handler in self.handlers:
                            self.application.remove_handler(handler, GROUP)
                    self.application = application
                    self.menu_ready = False
                    self.handlers = []
                    if application:
                        self.handlers = [
                            CommandHandler(["rblive", "start"], self.telegram),
                            CallbackQueryHandler(self.telegram, pattern=r"^rb:"),
                            MessageHandler(
                                filters.ChatType.PRIVATE
                                & filters.TEXT
                                & ~filters.COMMAND,
                                self.telegram,
                            ),
                        ]
                        self.handlers.append(TypeHandler(Update, self.stop_unrelated))
                        for handler in self.handlers:
                            application.add_handler(handler, GROUP)
                if (
                    application
                    and application.running
                    and not getattr(self, "menu_ready", False)
                ):
                    await application.bot.set_my_commands(
                        [
                            BotCommand("rblive", "热播直播间管理"),
                            BotCommand("start", "打开热播菜单"),
                        ]
                    )
                    self.menu_ready = True
                await self.tick()
            except Exception as exc:
                self.due.clear()
                logger.warning("Rebo scheduler deferred: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except TimeoutError:
                pass
            self.wakeup.clear()

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        tasks = list(self.workers.values()) + (
            [self.poll_task] if self.poll_task else []
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.application:
            for handler in self.handlers:
                self.application.remove_handler(handler, GROUP)
        self.context.registered_web_apis[:] = [
            r
            for r in self.context.registered_web_apis
            if not r[0].startswith(f"/{NAME}/")
        ]
        if self.http:
            await self.http.close()
