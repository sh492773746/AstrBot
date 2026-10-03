"""Fixed-template paid image edits with durable claims and restart-safe polling."""

import asyncio
import hashlib
import os
import re
import secrets
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from PIL import Image
from telegram.error import BadRequest, RetryAfter

from .avatar import ASSETS, Avatar, nickname
from .store import Rejected

BASE = "https://www.sevnx.lol/v1"
MODEL = "gpt-image-2.5"
CREDENTIAL = Path("/root/.config/api-credentials/savnx-image.env")
PROMPT = (
    "编辑第一张底图，第二张仅为文字融合效果参考。"
    "保留第一张红金配色、圆形回纹金边、金龙与大海传媒四字的构图，"
    "在大海传媒正下方加入且仅加入中文昵称「{name}」。"
    "昵称必须是与上方品牌一致的豪迈金色毛笔书法，有不规则笔锋、金箔纹理、"
    "细腻浮雕高光和自然暗红阴影，如同整幅图一体设计，不能像规则电脑楷体或平面贴字。"
    "仅参考第二张昵称的笔画气势，昵称整体位置比第二张下移约画面高度的4%，"
    "水平居中，拉开与大海传媒四字的距离，保持原有字号，"
    "昵称笔画不得触碰或覆盖底部圆形金边；品牌文字和其他构图保持不动。"
    "昵称只能使用「{name}」，准确书写，不可写参考名字。"
    "不得添加其他字、标签、签名或水印，不复制参考图右下角水印。"
    "输出正方形完整头像，不裁切边框，不增加人物，不改变主题。"
)


class EligibilityUnavailable(Rejected):
    """Membership could not be confirmed because verification was unavailable."""


class AIAvatar(Avatar):
    def __init__(self, runtime):
        self.ai_mode = True
        runtime.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS avatar_ai(
                job TEXT PRIMARY KEY,stage TEXT NOT NULL,task TEXT NOT NULL DEFAULT '',
                credential TEXT NOT NULL DEFAULT '',next REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '');
            UPDATE avatar_ai SET stage='unknown',error='submission_interrupted'
                WHERE stage='submitting';
            CREATE TABLE IF NOT EXISTS avatar_progress(
                job TEXT PRIMARY KEY,chat TEXT NOT NULL,uid TEXT NOT NULL,
                message INTEGER,ephemeral TEXT,next REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active');
        """)
        super().__init__(runtime)
        if "eligible_chat" not in {
            row["name"] for row in self.store.db.execute("PRAGMA table_info(avatar_ai)")
        }:
            self.store.db.execute(
                "ALTER TABLE avatar_ai ADD COLUMN eligible_chat TEXT NOT NULL DEFAULT ''"
            )
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS avatar_paid_budget(
                job TEXT PRIMARY KEY,day TEXT NOT NULL,at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS avatar_paid_budget_day ON avatar_paid_budget(day);
            INSERT OR IGNORE INTO avatar_paid_budget(job,day,at)
                SELECT j.id,strftime('%Y-%m-%d',j.at,'unixepoch','+8 hours'),j.at
                FROM avatar_jobs j JOIN avatar_ai a ON a.job=j.id;
        """)
        if self.store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mod_members'"
        ).fetchone():
            self.store.db.execute(
                "CREATE INDEX IF NOT EXISTS mod_members_avatar_uid ON mod_members(uid,seen DESC,chat)"
            )
        self.version = hashlib.sha256(
            (ASSETS / "base.png").read_bytes()
            + (ASSETS / "reference.png").read_bytes()
            + (MODEL + PROMPT + "high:1024x1024").encode()
        ).hexdigest()[:20]
        self.client = httpx.AsyncClient(timeout=150, follow_redirects=False)
        self.tick_lock = asyncio.Lock()
        # Durable local output takes precedence over an interrupted state commit.
        for row in self.store.db.execute(
            "SELECT a.job FROM avatar_ai a JOIN avatar_jobs j ON j.id=a.job WHERE j.status='rendering'"
        ).fetchall():
            path = self.root / (row["job"] + ".png")
            if path.is_file():
                try:
                    with Image.open(path) as image:
                        image.verify()
                except Exception:
                    continue
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE avatar_jobs SET status='ready' WHERE id=?",
                        (row["job"],),
                    )
                    db.execute(
                        "UPDATE avatar_ai SET stage='done' WHERE job=?", (row["job"],)
                    )

    def credential(self):
        values = {}
        try:
            for line in CREDENTIAL.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.removeprefix("export ").split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
            key = values["SAVNX_IMAGE_API_KEY"]
            if not key or values.get("SAVNX_IMAGE_BASE_URL", BASE).rstrip("/") != BASE:
                raise ValueError
        except (OSError, KeyError, ValueError):
            raise Rejected("头像服务尚未配置，请联系管理员。") from None
        return key

    async def eligible_group(self, uid, chat):
        """Verify current membership without sweeping every registered group.

        Args:
            uid: Authenticated requesting user.
            chat: Submission chat or previously verified group.

        Returns:
            Enabled group where Telegram confirms current membership.

        Raises:
            Rejected: No verified group or membership cannot be confirmed.
        """
        if not self.store.get("modules", {}).get("moderation"):
            raise Rejected("头像制作所属群功能未启用。")
        if int(chat) < 0:
            candidates = [str(chat)]
        else:
            candidates = [
                row["chat"]
                for row in self.store.db.execute(
                    "SELECT m.chat FROM mod_members m JOIN mod_groups g ON g.chat=m.chat "
                    "WHERE m.uid=? AND g.enabled=1 ORDER BY m.seen DESC LIMIT 5",
                    (str(uid),),
                )
            ]
        try:
            async with asyncio.timeout(5):
                for group in candidates:
                    tenants = getattr(self.runtime, "tenants", None)
                    if tenants and not tenants.resource_allowed(uid, group):
                        continue
                    if not self.store.db.execute(
                        "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (group,)
                    ).fetchone():
                        continue
                    try:
                        member = await self.runtime.bot.get_chat_member(group, int(uid))
                    except BadRequest:
                        continue
                    if member.status in {"member", "administrator", "creator"} or (
                        member.status == "restricted"
                        and getattr(member, "is_member", False)
                    ):
                        if (
                            self.store.get("modules", {}).get("moderation")
                            and self.store.db.execute(
                                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                                (group,),
                            ).fetchone()
                            and (not tenants or tenants.resource_allowed(uid, group))
                        ):
                            return group
        except Exception:
            raise EligibilityUnavailable(
                "暂时无法核验群成员资格，请稍后重试；未提交制作。"
            ) from None
        raise Rejected(
            "仅已启用群的当前成员可制作。请先在所属群发送 /avatar，再到私聊操作。"
        )

    def budget_limits(self):
        """Read validated task budgets without accepting an unlimited fallback.

        Returns:
            Daily and cumulative reservation limits.

        Raises:
            Rejected: Budget configuration is invalid.
        """
        limits = (
            self.runtime.config.get("avatar_daily_budget", 10),
            self.runtime.config.get("avatar_total_budget", 100),
        )
        if any(type(value) is not int or not 0 <= value <= 100000 for value in limits):
            raise Rejected("头像预算配置需管理员核查，未提交制作。")
        return limits

    async def action(self, update, payload, token=""):
        """Reuse identity/menu gates but replace final rendering with paid jobs.

        Args:
            update: Authenticated Telegram update.
            payload: Server-bound action.
            token: Single-use confirmation identity.
        """
        kind = payload["action"]
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        if kind == "avatar_cancel":
            self.store.db.execute(
                "DELETE FROM avatar_inputs WHERE uid=? AND chat=?", (uid, chat)
            )
            return await self.panel(
                update,
                "已退出本次填写。已确认提交的制作仍会继续，可从头像首页查看进度。",
            )
        if kind == "avatar_home":
            self.check(update)
            with (ASSETS / "ai-example.png").open("rb") as photo:
                await self.runtime.bot.send_photo(
                    chat_id=chat,
                    photo=photo,
                    caption="专属头像效果示例 · 固定底图 AI 设计",
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
            rows = self.store.db.execute(
                "SELECT j.*,a.stage FROM avatar_jobs j LEFT JOIN avatar_ai a ON a.job=j.id WHERE j.uid=? ORDER BY j.at DESC LIMIT 10",
                (uid,),
            ).fetchall()
            states = {
                "queued": "排队中",
                "submitting": "提交中",
                "processing": "制作中",
                "unknown": "待核查，请勿重复提交",
                "failed": "制作失败，未扣次数",
                "done": "已制作",
            }
            summary = "\n".join(
                f"{row['name']} · {states.get(row['stage'], '已制作' if row['status'] == 'ready' else '待核查')}"
                for row in rows
            )
            choices = [
                ("开始制作", {"action": "avatar_pick"}),
                ("刷新进度", {"action": "avatar_home"}),
            ]
            choices += [
                (f"领取 · {row['name']}", {"action": "avatar_send", "id": row["id"]})
                for row in rows
                if row["status"] == "ready"
            ]
            return await self.panel(
                update,
                f"剩余 {self.remaining(uid)} 张（包含制作中的预占额度）。\n"
                "马甲以「大海传媒」开头，每人累计两张。仅可改昵称，不接受其他绘图要求。\n"
                "仅已启用群的当前成员可制作；确认时核验资格与全局任务预算。\n"
                "确认后开始 AI 制作，底图细节可能微调；群内制作会公开成品。\n"
                + summary,
                choices,
            )
        if kind == "avatar_preview":
            self.check(update, brand=True)
            await self.eligible_group(uid, chat)
            name = nickname(payload["name"])
            return await self.panel(
                update,
                f"头像文字：大海传媒 · {name}\n"
                "采用上方示例的红金龙纹底图与金色书法，确认后才开始制作。\n"
                "每次结果略有差异，成功保存占用一次额度；失败不扣次数，结果不明保留额度待核查。",
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
        if kind == "avatar_confirm":
            self.check(update, brand=True)
            group = await self.eligible_group(uid, chat)
            self.credential()
            name = nickname(payload["name"])
            if payload["template"] != self.version:
                raise Rejected("模板已更新，请重新预览。")
            job = secrets.token_hex(16)
            with self.store.tx() as db:
                self.check(update, brand=True)
                tenants = getattr(self.runtime, "tenants", None)
                if (
                    not self.store.get("modules", {}).get("moderation")
                    or not db.execute(
                        "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (group,)
                    ).fetchone()
                ):
                    raise Rejected("所属群功能已停用，未提交制作。")
                self.store.resolve(token, uid, chat)
                if not db.execute(
                    "UPDATE callbacks SET used=1 WHERE token=? AND used=0", (token,)
                ).rowcount:
                    raise Rejected("这次制作已经提交，请查看进度。")
                if db.execute(
                    "SELECT 1 FROM avatar_jobs WHERE uid=? AND status='rendering'",
                    (uid,),
                ).fetchone():
                    raise Rejected("已有制作中或待核查的头像，请先查看进度。")
                if not self.remaining(uid):
                    raise Rejected("两张额度已用完。")
                day = datetime.fromtimestamp(
                    self.store.clock(), ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d")
                limits = self.budget_limits()
                if (
                    db.execute(
                        "SELECT count(*) FROM avatar_paid_budget WHERE day=?", (day,)
                    ).fetchone()[0]
                    >= limits[0]
                ):
                    raise Rejected("今日头像制作预算已用完，请明天再试；未提交制作。")
                if (
                    db.execute("SELECT count(*) FROM avatar_paid_budget").fetchone()[0]
                    >= limits[1]
                ):
                    raise Rejected("头像累计制作预算已用完，请联系管理员；未提交制作。")
                if (
                    db.execute(
                        "SELECT count(*) FROM avatar_ai WHERE stage IN ('queued','processing','submitting')"
                    ).fetchone()[0]
                    >= 10
                ):
                    raise Rejected("当前制作较多，请稍后再试。")
                if tenants:
                    tenants.consume_resource(uid, group, "avatar", job, db)
                db.execute(
                    "INSERT INTO avatar_jobs(id,uid,chat,name,template,status,at) VALUES(?,?,?,?,?,'rendering',?)",
                    (job, uid, chat, name, self.version, self.store.clock()),
                )
                db.execute(
                    "INSERT INTO avatar_ai(job,stage,eligible_chat) VALUES(?,'queued',?)",
                    (job, group),
                )
                db.execute(
                    "INSERT INTO avatar_paid_budget(job,day,at) VALUES(?,?,?)",
                    (job, day, self.store.clock()),
                )
                self.store.audit(
                    db,
                    uid,
                    "avatar_ai_queued",
                    {
                        "id": job,
                        "template": self.version,
                        "model": MODEL,
                        "group": group,
                        "budget_limits": limits,
                    },
                )
            message = await self.panel(
                update,
                "已排队制作，完成后会发到这里。不用重复点，可随时查看进度。",
                [("查看进度", {"action": "avatar_home"})],
            )
            ephemeral = getattr(message, "ephemeral_message_id", None) or (
                getattr(message, "api_kwargs", {}) or {}
            ).get("ephemeral_message_id")
            if update.effective_chat.type == "private" or isinstance(
                ephemeral, (str, int)
            ):
                self.store.db.execute(
                    "INSERT INTO avatar_progress(job,chat,uid,message,ephemeral,next) VALUES(?,?,?,?,?,?)",
                    (
                        job,
                        chat,
                        uid,
                        message.message_id,
                        str(ephemeral) if ephemeral is not None else None,
                        self.store.clock(),
                    ),
                )
            return
        result = await super().action(update, payload, token)
        if kind in {"avatar_admin", "avatar_toggle"}:
            daily, total = self.budget_limits()
            day = datetime.fromtimestamp(
                self.store.clock(), ZoneInfo("Asia/Shanghai")
            ).strftime("%Y-%m-%d")
            used_today = self.store.db.execute(
                "SELECT count(*) FROM avatar_paid_budget WHERE day=?", (day,)
            ).fetchone()[0]
            used_total = self.store.db.execute(
                "SELECT count(*) FROM avatar_paid_budget"
            ).fetchone()[0]
            unknown = self.store.db.execute(
                "SELECT count(*) FROM avatar_ai WHERE stage='unknown'"
            ).fetchone()[0]
            await self.panel(
                update,
                f"AI 固定底图制作 · 待核查 {unknown} 项。\n"
                f"今日任务预算 {used_today}/{daily} · 累计 {used_total}/{total}。\n"
                "预算含失败及待核查，不自动释放；上限在插件配置调整。\n"
                "未知结果不会重新付费提交，请维护人员核查任务记录。",
            )
        return result

    async def tick(self):
        """Submit once and poll by durable task ID independently of delivery."""
        if self.tick_lock.locked():
            return
        async with self.tick_lock:
            now = self.store.clock()
            row = self.store.db.execute(
                "SELECT j.*,a.stage,a.task,a.credential,a.eligible_chat FROM avatar_jobs j JOIN avatar_ai a ON a.job=j.id "
                "WHERE a.stage IN ('queued','processing') AND a.next<=? ORDER BY a.next,j.at LIMIT 1",
                (now,),
            ).fetchone()
            if row:
                await self.advance(row)

    async def delivery_tick(self):
        """Deliver cached output independently of slow provider requests."""
        ready = self.store.db.execute(
            "SELECT j.* FROM avatar_jobs j JOIN avatar_ai a ON a.job=j.id "
            "WHERE j.status='ready' AND j.delivery='pending' ORDER BY j.at LIMIT 1"
        ).fetchone()
        if ready:
            if int(ready["chat"]) < 0 and (
                not self.store.get("modules", {}).get("moderation")
                or not self.store.db.execute(
                    "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                    (ready["chat"],),
                ).fetchone()
            ):
                self.store.db.execute(
                    "UPDATE avatar_jobs SET delivery='failed_delivery',error='group_disabled' WHERE id=?",
                    (ready["id"],),
                )
                return
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=int(ready["uid"])),
                effective_chat=SimpleNamespace(id=int(ready["chat"])),
            )
            await self.deliver(update, ready["id"])

    async def progress_tick(self):
        """Edit known private/ephemeral progress messages without public fallback."""
        now = self.store.clock()
        rows = self.store.db.execute(
            "SELECT p.*,j.name,j.at,j.delivery,a.stage FROM avatar_progress p "
            "JOIN avatar_jobs j ON j.id=p.job JOIN avatar_ai a ON a.job=p.job "
            "WHERE p.status='active' AND p.next<=? LIMIT 20",
            (now,),
        ).fetchall()
        for row in rows:
            elapsed = max(0, int(now - row["at"]))
            labels = {
                "queued": "等待制作",
                "submitting": "正在提交",
                "processing": "正在绘制",
                "failed": "制作失败，额度已释放",
                "unknown": "结果待核查，请勿重复提交",
                "done": "制作完成",
            }
            terminal = row["stage"] in {"failed", "unknown"} or (
                row["stage"] == "done" and row["delivery"] not in {"pending", "sending"}
            )
            label = labels[row["stage"]]
            if row["stage"] == "done":
                label += {
                    "sent": "，已发送到当前会话",
                    "sending": "，正在发送",
                    "unknown": "，发送结果待核查，可查看成品",
                    "failed_delivery": "，自动发送失败，可领取成品",
                    "pending": "，准备发送",
                }.get(row["delivery"], "")
            text = f"🎨 {row['name']} · {label}\n⏱ 已等待 {elapsed // 60:02d}:{elapsed % 60:02d}"
            if not terminal:
                text += "\n这是已等待时间，不是完成倒计时。制作完成后会自动发送。"
            self.store.db.execute(
                "UPDATE avatar_progress SET next=? WHERE job=?", (now + 5, row["job"])
            )
            try:
                if row["ephemeral"]:
                    await self.runtime.bot._post(
                        "editEphemeralMessageText",
                        data={
                            "chat_id": row["chat"],
                            "receiver_user_id": int(row["uid"]),
                            "ephemeral_message_id": row["ephemeral"],
                            "text": text,
                        },
                        read_timeout=10,
                        write_timeout=10,
                        connect_timeout=10,
                    )
                elif int(row["chat"]) > 0:
                    await self.runtime.bot.edit_message_text(
                        chat_id=row["chat"],
                        message_id=row["message"],
                        text=text,
                        read_timeout=10,
                        write_timeout=10,
                        connect_timeout=10,
                    )
                else:
                    raise ValueError("Public progress forbidden")
            except RetryAfter as exc:
                delay = exc.retry_after
                delay = (
                    delay.total_seconds()
                    if hasattr(delay, "total_seconds")
                    else float(delay)
                )
                self.store.db.execute(
                    "UPDATE avatar_progress SET next=? WHERE job=?",
                    (now + max(5, delay), row["job"]),
                )
                continue
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    self.store.db.execute(
                        "UPDATE avatar_progress SET status='stopped' WHERE job=?",
                        (row["job"],),
                    )
                    continue
            except Exception:
                self.store.db.execute(
                    "UPDATE avatar_progress SET next=? WHERE job=?",
                    (now + 30, row["job"]),
                )
                continue
            if terminal:
                self.store.db.execute(
                    "UPDATE avatar_progress SET status='complete' WHERE job=?",
                    (row["job"],),
                )

    async def advance(self, row):
        """Advance one external operation without resubmitting ambiguous requests.

        Args:
            row: Persisted task and quota reservation.
        """
        job, now = row["id"], self.store.clock()
        try:
            key = self.credential()
        except Rejected:
            self.store.db.execute(
                "UPDATE avatar_ai SET next=?,error='credentials_unavailable' WHERE job=?",
                (now + 60, job),
            )
            return
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        headers = {"Authorization": "Bearer " + key}
        if row["stage"] == "queued":
            try:
                await self.eligible_group(
                    row["uid"], row["eligible_chat"] or row["chat"]
                )
                group_valid = True
            except EligibilityUnavailable:
                self.store.db.execute(
                    "UPDATE avatar_ai SET next=?,error='membership_unverified' WHERE job=?",
                    (self.store.clock() + 60, job),
                )
                return
            except Rejected:
                group_valid = False
            if not self.enabled() or not group_valid or row["template"] != self.version:
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE avatar_ai SET stage='failed',error='disabled_or_template_changed' WHERE job=?",
                        (job,),
                    )
                    db.execute(
                        "UPDATE avatar_jobs SET status='failed' WHERE id=?", (job,)
                    )
                return
            try:
                daily, total = self.budget_limits()
                now = self.store.clock()
                day = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).strftime(
                    "%Y-%m-%d"
                )
                with self.store.tx() as db:
                    reserved = db.execute(
                        "SELECT day FROM avatar_paid_budget WHERE job=?", (job,)
                    ).fetchone()
                    if not reserved:
                        raise Rejected("预算记录缺失")
                    if reserved["day"] != day:
                        if (
                            db.execute(
                                "SELECT count(*) FROM avatar_paid_budget WHERE day=?",
                                (day,),
                            ).fetchone()[0]
                            >= daily
                        ):
                            raise Rejected("今日预算已满")
                        db.execute(
                            "UPDATE avatar_paid_budget SET day=? WHERE job=?",
                            (day, job),
                        )
                    if (
                        daily == 0
                        or db.execute(
                            "SELECT count(*) FROM avatar_paid_budget WHERE day=?",
                            (day,),
                        ).fetchone()[0]
                        > daily
                        or db.execute(
                            "SELECT count(*) FROM avatar_paid_budget"
                        ).fetchone()[0]
                        > total
                    ):
                        raise Rejected("预算已暂停或调低")
            except Rejected:
                self.store.db.execute(
                    "UPDATE avatar_ai SET next=?,error='budget_paused' WHERE job=?",
                    (now + 60, job),
                )
                return
            self.store.db.execute(
                "UPDATE avatar_ai SET stage='submitting',credential=? WHERE job=?",
                (fingerprint, job),
            )
            try:
                with (
                    (ASSETS / "base.png").open("rb") as base,
                    (ASSETS / "reference.png").open("rb") as reference,
                ):
                    response = await self.client.post(
                        BASE + "/images/edits/async",
                        headers=headers,
                        data={
                            "model": MODEL,
                            "prompt": PROMPT.format(name=nickname(row["name"])),
                            "n": "1",
                            "size": "1024x1024",
                            "quality": "high",
                        },
                        files=[
                            ("image[]", ("base.png", base, "image/png")),
                            ("image[]", ("reference.png", reference, "image/png")),
                        ],
                    )
                if response.status_code in {400, 401, 403, 404, 413, 422, 429}:
                    with self.store.tx() as db:
                        db.execute(
                            "UPDATE avatar_ai SET stage='failed',error=? WHERE job=?",
                            (f"submit_{response.status_code}", job),
                        )
                        db.execute(
                            "UPDATE avatar_jobs SET status='failed' WHERE id=?", (job,)
                        )
                    return
                response.raise_for_status()
                data = response.json()
                task = data.get("task_id") or data.get("id")
                if not isinstance(task, str) or not re.fullmatch(
                    r"imgtask_[A-Za-z0-9_-]+", task
                ):
                    raise ValueError("Missing task ID")
                delay = max(3, int(response.headers.get("Retry-After", "3")))
                self.store.db.execute(
                    "UPDATE avatar_ai SET stage='processing',task=?,next=?,error='' WHERE job=?",
                    (task, now + delay, job),
                )
            except BaseException as exc:
                self.store.db.execute(
                    "UPDATE avatar_ai SET stage='unknown',error='submit_unknown' WHERE job=?",
                    (job,),
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
            return
        if row["credential"] != fingerprint:
            self.store.db.execute(
                "UPDATE avatar_ai SET next=?,error='credential_changed' WHERE job=?",
                (now + 60, job),
            )
            return
        if now - row["at"] > 86400:
            self.store.db.execute(
                "UPDATE avatar_ai SET stage='unknown',error='task_expired' WHERE job=?",
                (job,),
            )
            return
        self.store.db.execute(
            "UPDATE avatar_ai SET next=? WHERE job=?", (now + 15, job)
        )
        try:
            response = await self.client.get(
                BASE + "/images/tasks/" + row["task"], headers=headers, timeout=30
            )
            response.raise_for_status()
            data = response.json()
            delay = max(3, int(response.headers.get("Retry-After", "3")))
            self.store.db.execute(
                "UPDATE avatar_ai SET next=? WHERE job=?", (now + delay, job)
            )
            if data.get("status") == "processing":
                return
            if data.get("status") == "failed":
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE avatar_ai SET stage='failed',error='provider_failed' WHERE job=?",
                        (job,),
                    )
                    db.execute(
                        "UPDATE avatar_jobs SET status='failed' WHERE id=?", (job,)
                    )
                return
            if data.get("status") != "completed":
                raise ValueError("Unexpected task state")
            url = data.get("image_url") or data["result"]["data"][0]["url"]
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "www.sevnx.lol"
                or parsed.port not in {None, 443}
                or parsed.username
            ):
                raise ValueError("Untrusted asset origin")
            # No credentials are sent to the output URL; redirects are disabled.
            image_data = bytearray()
            async with self.client.stream("GET", url, timeout=60) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    image_data.extend(chunk)
                    if len(image_data) > 12 * 1024 * 1024:
                        raise ValueError("Image too large")
            with Image.open(BytesIO(image_data)) as image:
                if (
                    image.format not in {"PNG", "JPEG", "WEBP"}
                    or not 512 <= image.width <= 4096
                    or not 512 <= image.height <= 4096
                ):
                    raise ValueError("Invalid image")
                image.load()
                output = BytesIO()
                image.convert("RGB").save(output, format="PNG")
            temporary, final = self.root / (job + ".tmp"), self.root / (job + ".png")
            with temporary.open("wb") as target:
                target.write(output.getvalue())
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
                db.execute("UPDATE avatar_jobs SET status='ready' WHERE id=?", (job,))
                db.execute(
                    "UPDATE avatar_ai SET stage='done',error='' WHERE job=?", (job,)
                )
                self.store.audit(
                    db,
                    row["uid"],
                    "avatar_ai_completed",
                    {"id": job, "task": row["task"], "model": MODEL},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.store.db.execute(
                "UPDATE avatar_ai SET next=?,error='poll_or_asset_retry' WHERE job=?",
                (now + 60, job),
            )
