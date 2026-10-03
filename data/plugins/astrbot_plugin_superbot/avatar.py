"""Deterministic, quota-limited avatar composition without model access."""

import asyncio
import hashlib
import os
import re
import secrets
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter

from .store import Rejected

ASSETS = Path(__file__).parent / "assets" / "avatar"
PLATFORM = "大海传媒超级机器人"
KEY = "avatar_enabled"


def nickname(value):
    """Validate literal text and reject missing font glyphs.

    Args:
        value: User-supplied nickname.

    Returns:
        Validated nickname.
    """
    if not re.fullmatch(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]{1,8}", value):
        raise Rejected("名字限1～8个汉字、英文字母或数字，不支持空格、链接和符号。")
    font = ImageFont.truetype(str(ASSETS / "font.ttc"), 80)
    missing = bytes(font.getmask("\U0010ffff"))
    if any(bytes(font.getmask(char)) == missing for char in value):
        raise Rejected("这个名字有暂不支持的字，请换一个。")
    return value


def compose(name):
    """Overlay gold lettering only inside the fixed lower text rectangle.

    Args:
        name: Validated nickname.

    Returns:
        Lossless PNG bytes.
    """
    nickname(name)
    image = Image.open(ASSETS / "base.png").convert("RGB")
    width, height = 740, 230
    font_size = 230
    while True:
        font = ImageFont.truetype(str(ASSETS / "font.ttc"), font_size)
        box = font.getbbox(name, stroke_width=2)
        if box[2] - box[0] <= width - 40 and box[3] - box[1] <= height - 40:
            break
        font_size -= 2
    mask = Image.new("L", (width, height))
    draw = ImageDraw.Draw(mask)
    xy = ((width - box[2] + box[0]) // 2 - box[0], 12 - box[1])
    draw.text(xy, name, font=font, fill=255, stroke_width=2, stroke_fill=255)
    tile = image.crop((270, 795, 1010, 1025))
    shadow = Image.new("L", (width, height))
    shadow.paste(mask, (5, 8))
    tile.paste((85, 8, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(4)))
    gold = Image.new("RGB", (width, height))
    pixels = gold.load()
    for y in range(height):
        for x in range(width):
            grain = ((x * 37 + y * 71 + x * y) % 19) - 9
            shade = abs((y % 92) - 46) / 46
            pixels[x, y] = (
                min(255, 244 + grain),
                max(0, min(255, int(192 + 49 * shade) + grain)),
                max(0, min(255, int(61 + 115 * shade) + grain)),
            )
    tile.paste(gold, (0, 0), mask)
    image.paste(tile, (270, 795))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class Avatar:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.root = self.store.path.parent / "avatars"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.lock = asyncio.Lock()
        self.version = hashlib.sha256(
            (ASSETS / "base.png").read_bytes()
            + (ASSETS / "font.ttc").read_bytes()
            + b"gold-v1:270,795,1010,1025"
        ).hexdigest()[:20]
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS avatar_jobs(
                id TEXT PRIMARY KEY, uid TEXT NOT NULL, chat TEXT NOT NULL,
                name TEXT NOT NULL, template TEXT NOT NULL,
                status TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending',
                message INTEGER, at REAL NOT NULL, error TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS avatar_user ON avatar_jobs(uid,status);
            CREATE UNIQUE INDEX IF NOT EXISTS avatar_active ON avatar_jobs(uid)
                WHERE status='rendering';
            CREATE TABLE IF NOT EXISTS avatar_inputs(
                uid TEXT NOT NULL, chat TEXT NOT NULL, message INTEGER NOT NULL,
                expires REAL NOT NULL, PRIMARY KEY(uid,chat));
        """)
        if "ephemeral" not in {
            row["name"]
            for row in self.store.db.execute("PRAGMA table_info(avatar_inputs)")
        }:
            self.store.db.execute("ALTER TABLE avatar_inputs ADD COLUMN ephemeral TEXT")
        # A crash after atomic rename can recover the artifact without charging twice.
        for row in self.store.db.execute(
            "SELECT id FROM avatar_jobs WHERE status='rendering'"
        ).fetchall():
            if (
                getattr(self, "ai_mode", False)
                and self.store.db.execute(
                    "SELECT 1 FROM avatar_ai WHERE job=?", (row["id"],)
                ).fetchone()
            ):
                continue
            path = self.root / (row["id"] + ".png")
            valid = False
            if path.is_file():
                try:
                    with Image.open(path) as image:
                        image.verify()
                    valid = True
                except Exception:
                    pass
            self.store.db.execute(
                "UPDATE avatar_jobs SET status=?,error=? WHERE id=?",
                ("ready" if valid else "failed", "restart_recovery", row["id"]),
            )
        self.store.db.execute(
            "UPDATE avatar_jobs SET delivery='unknown' WHERE delivery='sending'"
        )

    def enabled(self):
        return self.runtime.config.get("platform_id") == PLATFORM and self.store.get(
            KEY, False
        )

    def check(self, update, brand=False):
        if not self.enabled():
            raise Rejected("专属头像还没开放，稍后再来看看。")
        chat = update.effective_chat
        if chat.type != "private":
            if not self.store.get("modules", {}).get("moderation"):
                raise Rejected("本群尚未开放头像制作。")
            if not self.store.db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (str(chat.id),)
            ).fetchone():
                raise Rejected("本群尚未开放头像制作。")
            if getattr(update.effective_message, "sender_chat", None):
                raise Rejected("请使用个人身份制作头像。")
        name = update.effective_user.full_name
        if brand and not name.startswith("大海传媒"):
            raise Rejected("您的名称中必须带有「大海传媒」")
        return name[4:].lstrip(" ·・|｜-_—:：")

    def remaining(self, uid):
        used = self.store.db.execute(
            "SELECT count(*) FROM avatar_jobs WHERE uid=? AND status IN ('rendering','ready')",
            (str(uid),),
        ).fetchone()[0]
        return max(0, 2 - used)

    async def render(self, name):
        """Run bounded image work off-loop and drain it before shutdown.

        Args:
            name: Validated literal nickname.

        Returns:
            Rendered PNG bytes.
        """
        task = asyncio.create_task(asyncio.to_thread(compose, name))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def panel(self, update, text, choices=()):
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        buttons = [
            InlineKeyboardButton(
                label,
                callback_data="av:" + self.store.callback(uid, chat, payload)[3:],
            )
            for label, payload in choices
        ]
        markup = (
            InlineKeyboardMarkup(
                [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            )
            if buttons
            else None
        )
        options = {}
        if update.effective_chat.type != "private":
            options["api_kwargs"] = {
                "ephemeral_message_parameters": {"receiver_user_id": int(uid)}
            }
        return await self.runtime.bot.send_message(
            chat_id=chat, text=text, reply_markup=markup, **options
        )

    async def action(self, update, payload, token=""):
        """Handle owner-bound buttons without granting customer-support tools.

        Args:
            update: Telegram update with verified user and chat.
            payload: Server-stored callback payload.
            token: Expiring callback token, required for state changes.
        """
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        action = payload["action"]
        if action in {"avatar_admin", "avatar_toggle", "avatar_toggle_preview"}:
            self.store.require(uid, "manager")
            if update.effective_chat.type != "private":
                raise Rejected("请在私聊管理头像功能。")
            if self.runtime.config.get("platform_id") != PLATFORM:
                raise Rejected("头像仅绑定大海传媒超级机器人。")
            if action == "avatar_toggle_preview":
                return await self.runtime.ui.render(
                    update,
                    "🔌 总开关确认\n\n📋 本次调整\n功能：🎨 专属头像\n"
                    f"调整为：{'⚪ 已关闭' if self.enabled() else '🟢 已开启'}\n"
                    "范围：所有群及私聊\n\n📌 说明\n每人累计两张，群内成品公开可见；停用后不再制作新头像。",
                    [
                        (
                            "确认",
                            {
                                "action": "avatar_toggle",
                                "enabled": not self.enabled(),
                                "previous": self.enabled(),
                            },
                        )
                    ],
                )
            if action == "avatar_toggle":
                with self.store.tx() as db:
                    self.store.resolve(token, uid, chat)
                    if self.enabled() != payload["previous"]:
                        raise Rejected("开关已变化，请刷新后操作。")
                    if not db.execute(
                        "UPDATE callbacks SET used=1 WHERE token=? AND used=0", (token,)
                    ).rowcount:
                        raise Rejected("这个确认已经处理。")
                    self.store.put(db, KEY, bool(payload["enabled"]))
                    self.store.audit(
                        db, uid, "avatar_toggle", {"enabled": payload["enabled"]}
                    )
            page = max(0, int(payload.get("page", 0)))
            rows = self.store.db.execute(
                "SELECT uid,name,status,delivery FROM avatar_jobs ORDER BY at DESC,id LIMIT 11 OFFSET ?",
                (page * 10,),
            ).fetchall()
            labels = {
                "ready": "已制作",
                "failed": "制作失败",
                "rendering": "制作中",
                "unknown": "发送待核查",
                "sent": "已发送",
                "pending": "待发送",
                "sending": "发送中",
                "failed_delivery": "发送失败",
            }
            choices = [("启用 / 停用", {"action": "avatar_toggle_preview"})]
            if page:
                choices.append(("上一页", {"action": "avatar_admin", "page": page - 1}))
            if len(rows) > 10:
                choices.append(("下一页", {"action": "avatar_admin", "page": page + 1}))
            return await self.panel(
                update,
                f"专属头像：{'开启' if self.enabled() else '关闭'}\n"
                + "\n".join(
                    f"{r['uid']} · {r['name']} · {labels[r['status']]} · {labels[r['delivery']]}"
                    for r in rows[:10]
                ),
                choices,
            )
        self.check(update)
        if action == "avatar_cancel":
            self.store.db.execute(
                "DELETE FROM avatar_inputs WHERE uid=? AND chat=?", (uid, chat)
            )
            return await self.panel(update, "已取消，不扣次数。")
        if action == "avatar_send":
            return await self.deliver(update, payload["id"], explicit=True)
        if action == "avatar_home":
            with (ASSETS / "example.png").open("rb") as photo:
                await self.runtime.bot.send_photo(
                    chat_id=chat,
                    photo=photo,
                    caption="专属头像示例 · 实际效果以你的预览为准",
                )
            rows = self.store.db.execute(
                "SELECT id,name,delivery FROM avatar_jobs WHERE uid=? AND status='ready' ORDER BY at DESC",
                (uid,),
            ).fetchall()
            choices = [("开始制作", {"action": "avatar_pick"})]
            choices += [
                (f"查看成品 · {r['name']}", {"action": "avatar_send", "id": r["id"]})
                for r in rows
            ]
            return await self.panel(
                update,
                f"每人累计可制作两张，剩余 {self.remaining(uid)} 张。\n"
                "显示名须以「大海传媒」开头；固定底图，只改昵称。群内制作的成品会在本群公开。",
                choices,
            )
        extracted = self.check(update, brand=True)
        if action == "avatar_pick":
            if not self.remaining(uid):
                raise Rejected("两张头像已制作完，可在头像首页查看已有成品。")
            choices = [
                ("自填名字", {"action": "avatar_custom"}),
                ("取消", {"action": "avatar_cancel"}),
            ]
            try:
                nickname(extracted)
                choices.insert(
                    0,
                    (
                        "使用：" + extracted,
                        {"action": "avatar_preview", "name": extracted},
                    ),
                )
            except Rejected:
                pass
            return await self.panel(update, "选择头像上显示的名字：", choices)
        if action == "avatar_custom":
            private = update.effective_chat.type == "private"
            reply = await self.runtime.bot.send_message(
                chat_id=chat,
                text="请回复这条消息，发送1～8个汉字、英文字母或数字。/cancel 取消。",
                reply_markup=ForceReply(
                    selective=True, input_field_placeholder="例如：青鱼"
                ),
                reply_to_message_id=update.effective_message.message_id
                if private
                else None,
                **(
                    {
                        "api_kwargs": {
                            "ephemeral_message_parameters": {
                                "receiver_user_id": int(uid)
                            }
                        }
                    }
                    if update.effective_chat.type != "private"
                    else {}
                ),
            )
            ephemeral = getattr(reply, "ephemeral_message_id", None) or (
                getattr(reply, "api_kwargs", {}) or {}
            ).get("ephemeral_message_id")
            self.store.db.execute(
                "INSERT INTO avatar_inputs(uid,chat,message,expires,ephemeral) VALUES(?,?,?,?,?) "
                "ON CONFLICT(uid,chat) DO UPDATE SET message=excluded.message,expires=excluded.expires,ephemeral=excluded.ephemeral",
                (
                    uid,
                    chat,
                    reply.message_id,
                    self.store.clock() + 600,
                    str(ephemeral) if isinstance(ephemeral, (str, int)) else None,
                ),
            )
            return
        if action == "avatar_preview":
            name = nickname(payload["name"])
            if self.lock.locked():
                raise Rejected("正在制作其他头像，请稍后再试。")
            async with self.lock:
                image = await self.render(name)
            preview = Image.open(BytesIO(image)).convert("RGB").resize((640, 640))
            overlay = Image.new("RGBA", preview.size)
            draw = ImageDraw.Draw(overlay)
            font = ImageFont.truetype(str(ASSETS / "font.ttc"), 32)
            for y in (230, 355, 450):
                draw.text(
                    (95, y),
                    "效 果 预 览 · 确 认 后 制 作",
                    font=font,
                    fill=(255, 255, 255, 150),
                    stroke_width=1,
                    stroke_fill=(90, 0, 0, 120),
                )
            buffer = BytesIO()
            Image.alpha_composite(preview.convert("RGBA"), overlay).convert("RGB").save(
                buffer, format="PNG"
            )
            await self.runtime.bot.send_photo(
                chat_id=chat,
                photo=buffer.getvalue(),
                caption="效果预览 · 尚未扣次数 · 正式成品无预览标记",
            )
            return await self.panel(
                update,
                f"确认制作「{name}」？成功保存后使用一次额度。",
                [
                    (
                        "确认制作",
                        {
                            "action": "avatar_confirm",
                            "name": name,
                            "template": self.version,
                        },
                    ),
                    ("修改名字", {"action": "avatar_custom"}),
                    ("取消", {"action": "avatar_cancel"}),
                ],
            )
        if action != "avatar_confirm":
            raise Rejected("头像操作无效，请重新打开。")
        if self.lock.locked():
            raise Rejected("正在制作其他头像，请稍后再试。")
        async with self.lock:
            self.check(update, brand=True)
            name = nickname(payload["name"])
            if payload["template"] != self.version:
                raise Rejected("模板已更新，请重新预览。")
            job = secrets.token_hex(16)
            with self.store.tx() as db:
                self.store.resolve(token, uid, chat)
                if not db.execute(
                    "UPDATE callbacks SET used=1 WHERE token=? AND used=0", (token,)
                ).rowcount:
                    raise Rejected("已经确认过了，请在头像首页查看成品。")
                if not self.remaining(uid):
                    raise Rejected("两张额度已用完。")
                if db.execute(
                    "SELECT 1 FROM avatar_jobs WHERE uid=? AND status='rendering'",
                    (uid,),
                ).fetchone():
                    raise Rejected("你还有一个制作中的头像。")
                db.execute(
                    "INSERT INTO avatar_jobs(id,uid,chat,name,template,status,at) VALUES(?,?,?,?,?,'rendering',?)",
                    (job, uid, chat, name, self.version, self.store.clock()),
                )
            final = self.root / (job + ".png")
            temporary = self.root / (job + ".tmp")
            try:
                image = await self.render(name)
                with temporary.open("xb") as target:
                    target.write(image)
                    target.flush()
                    os.fsync(target.fileno())
                temporary.chmod(0o600)
                temporary.replace(final)
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE avatar_jobs SET status='ready' WHERE id=?", (job,)
                    )
                    self.store.audit(
                        db, uid, "avatar_created", {"id": job, "template": self.version}
                    )
            except BaseException as exc:
                # A durable artifact must never release its reserved allowance.
                if not final.exists():
                    self.store.db.execute(
                        "UPDATE avatar_jobs SET status='failed',error='render_failed' WHERE id=?",
                        (job,),
                    )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise Rejected(
                    "制作暂未完成，请稍后查看记录；不要连续重复确认。"
                ) from None
        await self.deliver(update, job)

    async def deliver(self, update, job, explicit=False):
        """Send cached output once; ambiguous sends require an explicit user retry.

        Args:
            update: Owner's current Telegram update.
            job: Persisted job identifier.
            explicit: Whether the owner explicitly requested another copy.
        """
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM avatar_jobs WHERE id=? AND uid=? AND status='ready'",
                (job, uid),
            ).fetchone()
            if not row:
                raise Rejected("找不到你的成品。")
            if row["delivery"] == "sending":
                raise Rejected("正在发送，请稍等。")
            if not explicit and row["delivery"] != "pending":
                return
            db.execute(
                "UPDATE avatar_jobs SET delivery='sending',error='' WHERE id=?", (job,)
            )
        try:
            with (self.root / (job + ".png")).open("rb") as document:
                if getattr(self, "ai_mode", False) and int(chat) < 0:
                    result = await self.runtime.bot.send_photo(
                        chat_id=chat,
                        photo=document,
                        caption=f"🎨 {row['name']} · 专属头像制作完成",
                    )
                else:
                    result = await self.runtime.bot.send_document(
                        chat_id=chat,
                        document=document,
                        filename="大海传媒-" + row["name"] + ".png",
                        caption=f"你的专属头像 · {row['name']}\n剩余 {self.remaining(uid)} 张",
                    )
        except (BadRequest, Forbidden, RetryAfter, OSError):
            self.store.db.execute(
                "UPDATE avatar_jobs SET delivery='failed_delivery',error='send_rejected' WHERE id=?",
                (job,),
            )
            raise Rejected(
                "头像已保存，发送没成功；可从头像首页再次领取，不扣次数。"
            ) from None
        except BaseException:
            self.store.db.execute(
                "UPDATE avatar_jobs SET delivery='unknown',error='send_unknown' WHERE id=?",
                (job,),
            )
            raise
        self.store.db.execute(
            "UPDATE avatar_jobs SET delivery='sent',message=? WHERE id=?",
            (result.message_id, job),
        )

    async def message(self, update, command):
        """Consume only explicit entry commands or owner replies to our prompt.

        Args:
            update: Telegram message update.
            command: Parsed command directed to this bot.

        Returns:
            Whether the update belongs to this module.
        """
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        if command == "/avatar":
            await self.action(update, {"action": "avatar_home"})
            return True
        row = self.store.db.execute(
            "SELECT * FROM avatar_inputs WHERE uid=? AND chat=? AND expires>?",
            (uid, chat, self.store.clock()),
        ).fetchone()
        if not row:
            return False
        if command == "/cancel":
            await self.action(update, {"action": "avatar_cancel"})
            return True
        reply = update.message.reply_to_message
        navigation = command.startswith("/") or (update.message.text or "") in {
            "我的",
            "帮助",
            "客服",
            "广告投递",
            "加拿大28",
            "签到积分",
            "群聊解禁",
            "⚙️ 管理",
            "🎮 玩法大全",
            "玩法大全",
            "💰 积分",
            "🎁 签到",
            "📜 历史",
            "🧾 流水",
            "📊 输赢",
            "📖 玩法规则",
            "🎨 制作头像",
            "📋 群规",
            "📝 常用说明",
            "💬 客服",
            "❓ 帮助",
        }
        if navigation:
            self.store.db.execute(
                "DELETE FROM avatar_inputs WHERE uid=? AND chat=?", (uid, chat)
            )
            return False
        ephemeral = getattr(reply, "ephemeral_message_id", None) or (
            getattr(reply, "api_kwargs", {}) or {}
        ).get("ephemeral_message_id")
        if not reply or (
            str(ephemeral) != row["ephemeral"]
            if row["ephemeral"]
            else reply.message_id != row["message"]
        ):
            return False
        await self.action(
            update, {"action": "avatar_preview", "name": update.message.text or ""}
        )
        self.store.db.execute(
            "DELETE FROM avatar_inputs WHERE uid=? AND chat=?", (uid, chat)
        )
        return True
