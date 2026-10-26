"""Account/group-scoped business replies and durable, non-AI advertisements."""

import asyncio
import json
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .custom_rules import normalize
from .open_store import OpenError

COMMANDS = {
    "关键词回复",
    "添加回复",
    "修改回复",
    "删除回复",
    "启用回复",
    "停用回复",
    "定时广告",
    "广告图片",
    "添加广告",
    "修改广告",
    "删除广告",
    "启用广告",
    "停用广告",
    "确认回复",
    "确认广告",
}
SHANGHAI = ZoneInfo("Asia/Shanghai")
ADMISSION_WINDOW_SECONDS = 120
ACCOUNT_AD_GAP_SECONDS = 3
IMAGE_TEXT_GAP_SECONDS = 1


class KeywordReply(BaseModel):
    """One literal rule, independent of moderation keywords."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=40)
    enabled: bool = False
    match: Literal["exact", "contains"] = "exact"
    terms: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=20
    )
    text: str = Field(default="", max_length=2000)
    images: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(
        default_factory=list, max_length=1
    )
    cooldown_seconds: int = Field(default=30, ge=0, le=86400)

    @field_validator("terms")
    @classmethod
    def literal_terms(cls, value):
        """Reject empty or duplicate normalized literals."""
        if any(not 1 <= len(term) <= 100 or not normalize(term) for term in value):
            raise ValueError("invalid_literal")
        if len({normalize(term) for term in value}) != len(value):
            raise ValueError("duplicate_literal")
        return value

    @model_validator(mode="after")
    def content_required(self):
        """Require a literal reply body, a scoped image, or both."""
        if not self.text.strip() and not self.images:
            raise ValueError("keyword_content_required")
        return self


class Advertisement(BaseModel):
    """A bounded scheduled message with up to three rotating local images."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=40)
    enabled: bool = False
    text: str = Field(default="", max_length=2000)
    images: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(
        default_factory=list, max_length=3
    )
    mode: Literal["daily", "interval"] = "daily"
    times: list[str] = Field(default_factory=list, max_length=24)
    start: str = "09:00"
    end: str = "22:00"
    interval_minutes: int = Field(default=60, ge=5, le=1440)

    @model_validator(mode="after")
    def valid_clock(self):
        """Keep all schedules bounded and unambiguously Beijing-local."""
        if not self.text.strip() and not self.images:
            raise ValueError("advertisement_content_required")
        if len(set(self.images)) != len(self.images):
            raise ValueError("duplicate_image")
        clocks = [self.start, self.end, *self.times]
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", s) for s in clocks):
            raise ValueError("invalid_clock")
        if len(set(self.times)) != len(self.times):
            raise ValueError("duplicate_clock")
        if self.mode == "daily" and not self.times:
            raise ValueError("daily_times_required")
        if self.mode == "interval" and self.start == self.end:
            raise ValueError("empty_window")
        return self


class RuleConfiguration(BaseModel):
    """The complete bounded business-rule scope used in a confirmed preview."""

    model_config = ConfigDict(extra="forbid", strict=True)
    keywords: list[KeywordReply] = Field(default_factory=list, max_length=50)
    ads: list[Advertisement] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def distinct_ids(self):
        """Do not let edited identifiers alias a different rule."""
        ids = [r.id for r in [*self.keywords, *self.ads]]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_rule")
        return self


def next_ad_boundary(ad, now):
    """Return a strictly future daily boundary without replaying missed slots.

    Args:
        ad: Validated Advertisement or its stored dictionary.
        now: Current epoch seconds.

    Returns:
        Epoch seconds for the next future occurrence.
    """
    ad = ad if isinstance(ad, Advertisement) else Advertisement.model_validate(ad)
    local = datetime.fromtimestamp(now, SHANGHAI)
    candidates = []
    for offset in (-1, 0, 1, 2):
        day = (local + timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if ad.mode == "daily":
            for clock in ad.times:
                hour, minute = map(int, clock.split(":"))
                point = day.replace(hour=hour, minute=minute).timestamp()
                if point > now:
                    candidates.append(point)
        else:
            sh, sm = map(int, ad.start.split(":"))
            eh, em = map(int, ad.end.split(":"))
            start = day.replace(hour=sh, minute=sm)
            end = day.replace(hour=eh, minute=em)
            if end <= start:
                end += timedelta(days=1)
            step = timedelta(minutes=ad.interval_minutes)
            index = max(
                0, int((local - start).total_seconds() // step.total_seconds()) + 1
            )
            point = start + index * step
            if point < end and point.timestamp() > now:
                candidates.append(point.timestamp())
    if not candidates:
        raise OpenError("advertisement_schedule_invalid")
    return min(candidates)


class RuleReplies:
    """Own only literal replies and advertisements, with no model dependencies."""

    def __init__(self, service):
        self.service, self.store = service, service.store
        self.tasks = {}
        self.ad_locks = {}
        self.ad_ready_at = {}
        self.image_sessions = {}
        self.started = time.time()
        with self.store.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS business_rules(
                    account TEXT,group_id TEXT,version INTEGER NOT NULL,
                    tenant_version INTEGER NOT NULL,epoch INTEGER NOT NULL,
                    owner TEXT NOT NULL,peer TEXT NOT NULL,config TEXT NOT NULL,
                    PRIMARY KEY(account,group_id));
                CREATE TABLE IF NOT EXISTS business_rule_drafts(
                    token TEXT PRIMARY KEY,account TEXT,group_id TEXT,actor TEXT,
                    session TEXT,version INTEGER,tenant_version INTEGER,epoch INTEGER,
                    owner TEXT,peer TEXT,config TEXT,expires REAL,used INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS business_rule_sends(
                    operation TEXT PRIMARY KEY,account TEXT,group_id TEXT,kind TEXT,
                    rule_id TEXT,planned REAL,state TEXT,created REAL,error TEXT DEFAULT '');
                CREATE INDEX IF NOT EXISTS business_rule_cooldown
                    ON business_rule_sends(account,group_id,rule_id,created);
                CREATE TABLE IF NOT EXISTS business_ad_cursors(
                    account TEXT,group_id TEXT,rule_id TEXT,next_at REAL,
                    blocked TEXT NOT NULL DEFAULT '',PRIMARY KEY(account,group_id,rule_id));
            """)
            # A process interruption cannot establish whether native delivery succeeded.
            db.execute(
                "UPDATE business_ad_cursors SET blocked='delivery_unknown' WHERE EXISTS("
                "SELECT 1 FROM business_rule_sends s WHERE s.account=business_ad_cursors.account "
                "AND s.group_id=business_ad_cursors.group_id "
                "AND s.rule_id=business_ad_cursors.rule_id AND s.kind='advertisement' "
                "AND s.state='dispatched')"
            )
            db.execute(
                "UPDATE business_rule_sends SET state='unknown',error='process_interrupted' "
                "WHERE kind='advertisement' AND state='dispatched'"
            )
        from .advertisement_media import AdvertisementMedia

        self.media = AdvertisementMedia(self)

    def read(self, account, group):
        """Read only one exact scope, never other groups' messages or rules."""
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM business_rules WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            return (
                {**dict(row), "config": json.loads(row["config"])}
                if row
                else {
                    "account": account,
                    "group_id": group,
                    "version": 0,
                    "config": {"keywords": [], "ads": []},
                }
            )

    def preview(
        self,
        account,
        group,
        version,
        config,
        actor,
        session,
        tenant,
        *,
        owner="",
        peer="",
        guard,
    ):
        """Freeze a caller/session-bound configuration; never send from a preview."""
        config = RuleConfiguration.model_validate(config).model_dump()
        token = secrets.token_urlsafe(18)
        with self.store.connect(write=True, guard=guard) as db:
            self.media.validate(account, group, config, db)
            saved = db.execute(
                "SELECT version FROM business_rules WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if (saved["version"] if saved else 0) != version:
                raise OpenError("business_rules_changed")
            current = self.store.tenant(account, group)
            if current != tenant:
                raise OpenError("tenant_changed")
            db.execute(
                "DELETE FROM business_rule_drafts WHERE expires<?", (time.time(),)
            )
            db.execute(
                "INSERT INTO business_rule_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    token,
                    account,
                    group,
                    actor,
                    session,
                    version,
                    tenant["version"],
                    tenant["epoch"],
                    owner,
                    peer,
                    json.dumps(config),
                    time.time() + 120,
                ),
            )
        return {"token": token, "config": config, "version": version, "expires_in": 120}

    def commit(self, account, group, token, actor, session, *, guard):
        """Commit the exact reviewed configuration under current native authority."""
        now = time.time()
        with self.store.connect(write=True, guard=guard) as db:
            draft = db.execute(
                "SELECT * FROM business_rule_drafts WHERE token=?", (token,)
            ).fetchone()
            if (
                not draft
                or draft["account"] != account
                or draft["group_id"] != group
                or draft["actor"] != actor
                or draft["session"] != session
                or draft["expires"] <= now
            ):
                raise OpenError("business_confirmation_invalid")
            if draft["used"]:
                return {"saved": True, "version": draft["version"] + 1}
            tenant = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?", (account, group)
            ).fetchone()
            saved = db.execute(
                "SELECT version FROM business_rules WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if (
                not tenant
                or tenant["version"] != draft["tenant_version"]
                or tenant["epoch"] != draft["epoch"]
                or (saved["version"] if saved else 0) != draft["version"]
            ):
                raise OpenError("business_rules_changed")
            if draft["owner"]:
                self.store.management_guard(db, tenant, draft["owner"])
            config = json.loads(draft["config"])
            self.media.validate(account, group, config, db)
            db.execute(
                "INSERT INTO business_rules VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account,group_id) DO UPDATE SET version=excluded.version,"
                "tenant_version=excluded.tenant_version,epoch=excluded.epoch,"
                "owner=excluded.owner,peer=excluded.peer,config=excluded.config",
                (
                    account,
                    group,
                    draft["version"] + 1,
                    draft["tenant_version"],
                    draft["epoch"],
                    draft["owner"],
                    draft["peer"],
                    draft["config"],
                ),
            )
            db.execute(
                "DELETE FROM business_ad_cursors WHERE account=? AND group_id=?",
                (account, group),
            )
            ad_ids = [ad["id"] for ad in config["ads"]]
            db.execute(
                "DELETE FROM business_ad_rotations WHERE account=? AND group_id=?"
                + (
                    f" AND rule_id NOT IN ({','.join('?' for _ in ad_ids)})"
                    if ad_ids
                    else ""
                ),
                (account, group, *ad_ids),
            )
            for ad in config["ads"]:
                encoded_images = json.dumps(ad.get("images", []))
                db.execute(
                    "INSERT INTO business_ad_rotations VALUES(?,?,?,?,0) "
                    "ON CONFLICT(account,group_id,rule_id) DO UPDATE SET "
                    "position=CASE WHEN images=excluded.images THEN position ELSE 0 END,"
                    "images=excluded.images",
                    (account, group, ad["id"], encoded_images),
                )
                if ad["enabled"]:
                    db.execute(
                        "INSERT INTO business_ad_cursors VALUES(?,?,?,?, '')",
                        (account, group, ad["id"], next_ad_boundary(ad, now)),
                    )
            db.execute("UPDATE business_rule_drafts SET used=1 WHERE token=?", (token,))
            self.store.log(
                db,
                actor,
                account,
                group,
                "business_rules_saved",
                {"version": draft["version"] + 1},
            )
        return {"saved": True, "version": draft["version"] + 1}

    async def receive_image(self, event):
        """Preview one requested attachment under the same native rule authority.

        Args:
            event: Fresh authenticated private image event.

        Returns:
            Explicit preview or scope feedback; no schedule is committed.

        Raises:
            OpenError: If the request, image or current authorization changed.
        """
        key = self.service.navigation.key(event)
        pending = self.image_sessions.get(key)
        if not pending:
            return ""
        if pending["receiving"]:
            return "上一张图片仍在录入，请稍后核对预览。"
        pending["receiving"] = True
        try:
            group, tenant, authority = await self.context(event)
            stamp = self.service.navigation.stamp(event)

            def guard():
                authority()
                if (
                    self.image_sessions.get(key) is not pending
                    or pending["expires"] <= time.monotonic()
                    or pending["stamp"] != stamp
                    or self.service.navigation.stamp(event) != stamp
                ):
                    raise OpenError("advertisement_image_request_changed")

            guard()
            payload = event.get_extra("wangshangliao_payload") or {}
            if (
                payload.get("kind") != "image"
                or type(payload.get("created_at")) not in (int, float)
                or payload["created_at"] / 1000 < pending["created"]
            ):
                raise OpenError("advertisement_image_invalid")
            account = str(event.platform.account)
            row = await asyncio.to_thread(self.read, account, group)
            if row["version"] != pending["version"]:
                raise OpenError("business_rules_changed")
            ad = next(
                (ad for ad in row["config"]["ads"] if ad["id"] == pending["rule"]),
                None,
            )
            if ad is None or len(ad.get("images", [])) >= 3:
                raise OpenError("advertisement_image_request_changed")
            image = await self.media.receive(
                event.platform, group, payload.get("image"), guard=guard
            )
            guard()
            ad.setdefault("images", []).append(image["id"])
            preview = await asyncio.to_thread(
                self.preview,
                account,
                group,
                row["version"],
                row["config"],
                event.actor_identity.principal,
                str(event.unified_msg_origin),
                tenant,
                owner=event.actor_identity.verified_uid,
                peer=event.actor_identity.peer,
                guard=guard,
            )
            return (
                f"图片已录入预览，尚未保存。\n目标群：{group}\n"
                f"发言计划：{ad['name']}，共{len(ad['images'])}/3张图片。\n"
                f"确认保存请发送：确认广告 {preview['token']}\n"
                "不确认不会修改计划；再加一张，请保存后重新发送“广告图片 计划序号”。"
            )
        finally:
            if self.image_sessions.get(key) is pending:
                self.image_sessions.pop(key, None)

    async def context(self, event):
        """Resolve a native manager's current/selected group without AI authority."""
        service, adapter = self.service, event.platform
        if event.is_private_chat():
            await service.verify_actor(event)
        identity = event.actor_identity
        identity.require_verified()
        group = event.get_group_id() or service.navigation.stamp(event)[3]
        if not group and event.is_private_chat():
            raise OpenError("business_group_required")
        if not group or not service.management.allowed(event, group):
            raise OpenError("permission_denied")
        tenant = dict(service.tenants.get((identity.account, group), {}))
        members = await service.roster(adapter, group)
        if str(
            members.get(identity.claimed_uid, {}).get("nimId", "")
        ) != identity.peer or members.get(identity.claimed_uid, {}).get(
            "groupRole"
        ) not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}:
            raise OpenError("permission_denied")
        service.member_evidence[(identity.account, group)] = members
        authority = service.write_guard(
            adapter, event, group=group, tenant=tenant, members=members
        )

        def guard():
            authority()
            current = service.member_evidence.get((identity.account, group), {}).get(
                identity.claimed_uid, {}
            )
            if str(current.get("nimId", "")) != identity.peer or current.get(
                "groupRole"
            ) not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}:
                raise OpenError("business_owner_revoked")

        guard()
        previous = event.get_extra("_wsl_management_delivery_guard")

        def delivery_guard():
            guard()
            if callable(previous):
                previous()

        event.set_extra("_wsl_management_delivery_guard", delivery_guard)
        bind = getattr(event, "bind_management_reply_guard", None)
        if callable(bind):
            bind(service.management, (group,))
        return group, tenant, guard

    async def command(self, event, command):
        """Use fixed native commands for reading, previewing and confirming rules."""
        group, tenant, guard = await self.context(event)
        account, actor, session = (
            str(event.platform.account),
            event.actor_identity.principal,
            str(event.unified_msg_origin),
        )
        action, _, argument = command.partition(" ")
        row = await asyncio.to_thread(self.read, account, group)
        if action == "广告图片":
            if not event.is_private_chat():
                return "图片录入请私信机器人，先选择群，再发送“广告图片 计划序号”。"
            if not re.fullmatch(r"[1-9][0-9]?", argument):
                raise OpenError("business_rule_number")
            index = int(argument) - 1
            ads = row["config"]["ads"]
            if index >= len(ads):
                raise OpenError("business_rule_number")
            if len(ads[index].get("images", [])) >= 3:
                return "这个计划已有3张图片，请先到插件页面替换或删除图片。"
            key = self.service.navigation.key(event)
            self.image_sessions = {
                k: value
                for k, value in self.image_sessions.items()
                if value["expires"] > time.monotonic()
            }
            if len(self.image_sessions) >= 100:
                raise OpenError("advertisement_image_busy")
            self.image_sessions[key] = {
                "stamp": self.service.navigation.stamp(event),
                "version": row["version"],
                "rule": ads[index]["id"],
                "expires": time.monotonic() + 300,
                "created": time.time(),
                "receiving": False,
            }
            return (
                f"已选择本群计划：{ads[index]['name']}（已有{len(ads[index].get('images', []))}/3张）。\n"
                "请在5分钟内发送一张5MB以内的JPG、PNG或WebP静态图片。\n"
                "收到后只生成预览，按提示确认才会保存；发送“取消”放弃。"
            )
        if action in {"确认回复", "确认广告"}:
            result = await asyncio.to_thread(
                self.commit,
                account,
                group,
                argument.strip(),
                actor,
                session,
                guard=guard,
            )
            return f"规则已保存（版本{result['version']}），不会开启AI。"
        key = "ads" if "广告" in action else "keywords"
        items = row["config"][key]
        if action in {"关键词回复", "定时广告"}:
            return (
                (
                    "【本群关键词自动回复】"
                    if key == "keywords"
                    else "【本群定时发言·北京时间】"
                )
                + "\n"
                + (
                    "\n".join(
                        f"{n}. {r['name']} · {'开启' if r['enabled'] else '关闭'}\n"
                        f"{r['text'][:160]}"
                        + (
                            f"\n{'配图' if key == 'keywords' else '轮换图片'}：{len(r['images'])}张"
                            if r.get("images")
                            else ""
                        )
                        for n, r in enumerate(items, 1)
                    )
                    or "暂无规则。"
                )
                + (
                    "\n添加回复 名称|精确或包含|词一/词二|回复正文"
                    if key == "keywords"
                    else "\n直接新增：添加广告 通知|每日|09:00,18:00|通知正文"
                    "\n图片可在插件页面上传；私信先选群，再发“广告图片 计划序号”并发送一张图片。\n"
                    "每计划最多3张，每次轮换1张，图片须预览确认保存。"
                )
                + "\n新规则默认关闭；修改、删除、启停均须预览确认。"
            )
        config = json.loads(json.dumps(row["config"]))
        target = config[key]
        if action.startswith(("添加", "修改")):
            if action.startswith("修改"):
                number, _, argument = argument.partition(" ")
                index = int(number) - 1
                if not 0 <= index < len(target):
                    raise OpenError("business_rule_number")
            else:
                index = len(target)
            old = target[index] if index < len(target) else None
            fields = argument.split("|", 3 if key == "keywords" else 4)
            base = {
                "id": old["id"] if old else uuid.uuid4().hex,
                "enabled": old["enabled"] if old else False,
                "images": old.get("images", []) if old else [],
            }
            if key == "keywords" and len(fields) == 4:
                name, mode, terms, text = fields
                if mode.strip() not in {"精确", "包含"}:
                    raise OpenError("business_rules_invalid")
                item = KeywordReply(
                    **base,
                    name=name.strip(),
                    match={"精确": "exact", "包含": "contains"}[mode.strip()],
                    terms=[v.strip() for v in terms.split("/")],
                    text=text.strip(),
                    cooldown_seconds=old["cooldown_seconds"] if old else 30,
                ).model_dump()
            elif key == "ads" and len(fields) >= 4:
                name, mode, clock, *body = fields
                if mode.strip() == "每日":
                    item = Advertisement(
                        **base,
                        name=name.strip(),
                        mode="daily",
                        times=[v.strip() for v in clock.split(",")],
                        text="|".join(body).strip(),
                    ).model_dump()
                elif mode.strip() == "间隔" and len(body) == 2:
                    start, end = clock.strip().split("-", 1)
                    item = Advertisement(
                        **base,
                        name=name.strip(),
                        mode="interval",
                        start=start,
                        end=end,
                        interval_minutes=int(body[0]),
                        text=body[1].strip(),
                    ).model_dump()
                else:
                    raise OpenError("business_rules_invalid")
            else:
                raise OpenError("business_rules_invalid")
            if old:
                target[index] = item
            else:
                target.append(item)
        else:
            index = int(argument.strip()) - 1
            if not 0 <= index < len(target):
                raise OpenError("business_rule_number")
            if action.startswith("删除"):
                target.pop(index)
            else:
                target[index]["enabled"] = action.startswith("启用")
        preview = await asyncio.to_thread(
            self.preview,
            account,
            group,
            row["version"],
            config,
            actor,
            session,
            tenant,
            owner=event.actor_identity.verified_uid,
            peer=event.actor_identity.peer,
            guard=guard,
        )
        return (
            "【规则修改预览】\n"
            + "\n".join(
                f"{n}. {item['name']} · {'开启' if item['enabled'] else '关闭'}\n"
                + (
                    f"{'整句精确' if item['match'] == 'exact' else '包含'}："
                    + " / ".join(item["terms"])
                    if key == "keywords"
                    else (
                        "每日 " + "、".join(item["times"])
                        if item["mode"] == "daily"
                        else f"{item['start']}—{item['end']}，每{item['interval_minutes']}分钟"
                    )
                )
                + f"\n{item['text']}"
                + (
                    f"\n{'配图' if key == 'keywords' else '轮换图片'}：{len(item['images'])}张"
                    if item.get("images")
                    else ""
                )
                for n, item in enumerate(preview["config"][key], 1)
            )
            + "\n不会立即发送。两分钟内在本对话发送：\n"
            + ("确认广告 " if key == "ads" else "确认回复 ")
            + preview["token"]
        )

    def send_guard(self, adapter, row, *, posting=False):
        """Recheck persisted rule, tenant, native role, creator and send grants."""
        from astrbot.core.star.component_runtime import check_execution

        check_execution()
        service, account, group = self.service, row["account"], row["group_id"]
        tenant = service.store.tenant(account, group)
        key = (account, group)
        if (
            not service.active
            or service.settings["paused"]
            or service.capacity_paused
            or not tenant
            or not tenant["active"]
            or not tenant["present"]
            or tenant["paused"]
            or tenant["epoch"] != row["epoch"]
            or tenant["version"] != row["tenant_version"]
            or tenant.get("admission", "legacy") not in {"legacy", "verified"}
            or tenant["native_role"] not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
            or tenant["cleanup_epoch"] > tenant["cleanup_done"]
            or key in service.suspended_groups
            or service.membership_receiving.get(key)
            or service.reconciling_groups.get(key)
            or group in service.retired_groups
            or str(adapter.account) != account
            or adapter.connection_state != "online"
            or not adapter.config.get("enable", True)
            or not adapter.config.get("_business_active")
            or group not in adapter.config.get("enabled_groups", [])
            or not adapter.config.get("reply_groups", {}).get(group, True)
            or self.read(account, group)["version"] != row["version"]
        ):
            raise OpenError("business_rules_inactive")
        members = service.member_evidence.get(key, {})
        if (
            service.capabilities.get(key, {}).get("checked_at", 0) < time.time() - 60
            or str(members.get(account, {}).get("nimId", ""))
            != str(adapter.nim_account)
            or members.get(account, {}).get("groupRole")
            not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
        ):
            raise OpenError("business_native_permission")
        if row["owner"] and (
            row["owner"] not in tenant["managers"]
            or str(members.get(row["owner"], {}).get("nimId", "")) != row["peer"]
            or members.get(row["owner"], {}).get("groupRole")
            not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
        ):
            raise OpenError("business_owner_revoked")
        if posting and (
            not adapter.config.get("proactive_send", {}).get("enabled", False)
            or group not in adapter.config.get("proactive_send", {}).get("targets", [])
        ):
            raise OpenError("proactive_not_authorized")

    async def observe(self, event):
        """Consume a matching group message even when no reply can be sent."""
        if event.is_private_chat():
            return False
        identity = getattr(event, "actor_identity", None)
        account, group = str(event.platform.account), str(event.get_group_id())
        if (
            not isinstance(identity, ActorIdentity)
            or identity.account != account
            or identity.claimed_uid != str(event.get_sender_id())
            or identity.verified_uid != identity.claimed_uid
        ):
            return False
        managed = is_managed_account(identity.claimed_uid)
        if (
            managed
            and getattr(event, "get_extra", lambda _: None)("wsl_test_scope")
            != "group_rules"
        ):
            return False
        row = await asyncio.to_thread(self.read, account, group)
        text = normalize(event.message_str)
        candidates = sorted(
            row["config"]["keywords"], key=lambda r: r["match"] != "exact"
        )
        rule = next(
            (
                r
                for r in candidates
                if r["enabled"]
                and any(
                    text == normalize(term)
                    if r["match"] == "exact"
                    else normalize(term) in text
                    for term in r["terms"]
                )
            ),
            None,
        )
        if not rule:
            return False
        test_guard = None
        if managed:
            from .test_probe import keyword_probe_guard

            try:
                test_guard = keyword_probe_guard(self.service, event, rule)
            except OpenError:
                return False
        from astrbot.core.star.component_runtime import current_execution

        execution = current_execution.get()
        if execution is not None:
            event.set_extra("_component_claimed", True)
            execution.claim()
        self.service.group_ai.activity[(account, group)] = time.time()
        operation = f"keyword/{account}/{group}/{event.message_obj.message_id}"
        now = time.time()
        try:
            if test_guard:
                test_guard()
            if (
                self.service.capabilities.get((account, group), {}).get("checked_at", 0)
                < now - 60
            ):
                await self.service.roster(event.platform, group)
            if test_guard:
                test_guard()
            self.send_guard(event.platform, row)
            with self.store.connect(write=True) as db:
                if db.execute(
                    "SELECT 1 FROM business_rule_sends WHERE operation=?", (operation,)
                ).fetchone():
                    return True
                recent = db.execute(
                    "SELECT 1 FROM business_rule_sends WHERE account=? AND group_id=? "
                    "AND rule_id=? AND kind='keyword' AND state IN ('dispatched','accepted','verified','unknown','partial') "
                    "AND created>? LIMIT 1",
                    (account, group, rule["id"], now - rule["cooldown_seconds"]),
                ).fetchone()
                db.execute(
                    "INSERT INTO business_rule_sends VALUES(?,?,?,?,?,?,?,?,'')",
                    (
                        operation,
                        account,
                        group,
                        "keyword",
                        rule["id"],
                        now,
                        "cooldown" if recent else "dispatched",
                        now,
                    ),
                )
            if recent:
                return True
            state = "unknown"
            try:

                def guard():
                    if test_guard:
                        test_guard()
                    self.send_guard(event.platform, row)

                sent_image = False
                if rule.get("images"):
                    state = await self.media.send(
                        event.platform,
                        group,
                        rule["images"][0],
                        operation + "/image",
                        guard,
                        proactive=False,
                    )
                    sent_image = state in {"accepted", "verified"}
                if rule["text"].strip() and (not rule.get("images") or sent_image):
                    state = "unknown"
                    guard()
                    state = await event.platform.send_reply_text(
                        group,
                        operation,
                        rule["text"],
                        auto_recall=False,
                        authorize=guard,
                    )
                    if sent_image and state == "rejected":
                        state = "partial"
                if state not in {
                    "accepted",
                    "verified",
                    "unknown",
                    "rejected",
                    "partial",
                }:
                    state = "unknown"
            finally:
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE business_rule_sends SET state=? WHERE operation=?",
                        (state, operation),
                    )
        except Exception as exc:
            from astrbot.core.star.component_runtime import report_execution_fault

            if not isinstance(exc, (OpenError, ProtocolError, ValueError)):
                report_execution_fault(exc)
            # A claimed literal is consumed even if transport or storage fails.
            pass
        return True

    def tick(self):
        """Dispatch only future-runtime ad slots with bounded detached tasks."""
        if not self.service.active:
            return
        now = time.time()
        with self.store.connect() as db:
            due = [
                dict(r)
                for r in db.execute(
                    "SELECT c.* FROM business_ad_cursors c WHERE c.next_at<=? "
                    "AND c.blocked='' ORDER BY c.next_at LIMIT 20",
                    (now,),
                )
            ]
        adapters = {str(a.account): a for a in self.service.adapters()}
        for cursor in due:
            key = (cursor["account"], cursor["group_id"], cursor["rule_id"])
            if key in self.tasks or len(self.tasks) >= 2:
                continue
            task = asyncio.create_task(self.send_ad(adapters.get(key[0]), cursor))
            self.tasks[key] = task
            task.add_done_callback(lambda done, key=key: self.tasks.pop(key, None))

    async def send_ad(self, adapter, cursor):
        """Serialize account campaigns without holding the native message lock."""
        account = cursor["account"]
        async with self.ad_locks.setdefault(account, asyncio.Lock()):
            while True:
                remaining = self.ad_ready_at.get(account, 0) - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            try:
                governance = getattr(self.service, "governance", None)
                if governance is None or adapter is None:
                    return await self._send_ad(adapter, cursor)
                return await governance.run(
                    "advertisements",
                    adapter,
                    lambda: self._send_ad(adapter, cursor),
                    group=cursor["group_id"],
                )
            finally:
                self.ad_ready_at[account] = time.monotonic() + ACCOUNT_AD_GAP_SECONDS

    async def _send_ad(self, adapter, cursor):
        """Claim one scheduled slot before native IO; never retry uncertain sends."""
        account, group, rule_id = (
            cursor["account"],
            cursor["group_id"],
            cursor["rule_id"],
        )
        row = await asyncio.to_thread(self.read, account, group)
        ad = next(
            (r for r in row["config"]["ads"] if r["id"] == rule_id and r["enabled"]),
            None,
        )
        if not ad:
            return
        now, planned = time.time(), cursor["next_at"]
        operation = f"advertisement/{account}/{group}/{rule_id}/{int(planned)}"
        state, error, claimed = "skipped", "", False
        image_ids, image_position = ad.get("images", []), 0
        try:
            if adapter is None:
                error = "advertisement_adapter_unavailable"
                return
            if planned < self.started:
                error = "advertisement_before_runtime"
                return
            if now - planned > ADMISSION_WINDOW_SECONDS:
                error = "advertisement_slot_expired"
                return
            members = await self.service.roster(adapter, group)
            self.service.member_evidence[(account, group)] = members
            self.send_guard(adapter, row, posting=True)
            if time.time() - planned > ADMISSION_WINDOW_SECONDS:
                error = "advertisement_slot_expired"
                return
            with self.store.connect(write=True) as db:
                current = db.execute(
                    "SELECT next_at,blocked FROM business_ad_cursors WHERE "
                    "account=? AND group_id=? AND rule_id=?",
                    (account, group, rule_id),
                ).fetchone()
                if (
                    not current
                    or current["next_at"] != planned
                    or current["blocked"]
                    or db.execute(
                        "SELECT 1 FROM business_rule_sends WHERE operation=?",
                        (operation,),
                    ).fetchone()
                ):
                    return
                if image_ids:
                    rotation = db.execute(
                        "SELECT images,position FROM business_ad_rotations "
                        "WHERE account=? AND group_id=? AND rule_id=?",
                        (account, group, rule_id),
                    ).fetchone()
                    if not rotation or rotation["images"] != json.dumps(image_ids):
                        raise OpenError("advertisement_image_rotation_changed")
                    image_position = rotation["position"] % len(image_ids)
                db.execute(
                    "INSERT INTO business_rule_sends VALUES(?,?,?,?,?,?,'dispatched',?,'')",
                    (operation, account, group, "advertisement", rule_id, planned, now),
                )
                claimed = True
            state = "unknown"
            if image_ids:
                state = await self.media.send(
                    adapter,
                    group,
                    image_ids[image_position],
                    operation + "/image",
                    lambda: self.send_guard(adapter, row, posting=True),
                )
            if ad["text"].strip() and (
                not image_ids or state in {"accepted", "verified"}
            ):
                state = "unknown"
                if image_ids:
                    ready_at = time.monotonic() + IMAGE_TEXT_GAP_SECONDS
                    while True:
                        remaining = ready_at - time.monotonic()
                        if remaining <= 0:
                            break
                        await asyncio.sleep(remaining)
                state = await adapter.send_reply_text(
                    group,
                    operation,
                    ad["text"],
                    proactive=True,
                    auto_recall=False,
                    authorize=lambda: self.send_guard(adapter, row, posting=True),
                )
                if image_ids and state in {"not_sent", "rejected"}:
                    state, error = "partial", "advertisement_text_not_sent"
            if state not in {"accepted", "verified", "not_sent", "rejected", "partial"}:
                state = "unknown"
        except asyncio.CancelledError:
            if not claimed:
                error = "advertisement_cancelled_before_send"
            raise
        except (OpenError, ProtocolError) as exc:
            error = str(exc)
        except Exception as exc:
            from astrbot.core.star.component_runtime import report_execution_fault

            report_execution_fault(exc)
            error = (
                "advertisement_delivery_unknown"
                if claimed
                else "advertisement_preflight_failed"
            )
        finally:
            with self.store.connect(write=True) as db:
                if claimed:
                    db.execute(
                        "UPDATE business_rule_sends SET state=?,error=? WHERE operation=?",
                        (state, error, operation),
                    )
                    if image_ids and state in {"accepted", "verified"}:
                        db.execute(
                            "UPDATE business_ad_rotations SET position=? WHERE "
                            "account=? AND group_id=? AND rule_id=? AND images=? AND "
                            "EXISTS(SELECT 1 FROM business_rules WHERE account=? AND "
                            "group_id=? AND version=?)",
                            (
                                (image_position + 1) % len(image_ids),
                                account,
                                group,
                                rule_id,
                                json.dumps(image_ids),
                                account,
                                group,
                                row["version"],
                            ),
                        )
                advanced = db.execute(
                    "UPDATE business_ad_cursors SET next_at=?,blocked=? WHERE "
                    "account=? AND group_id=? AND rule_id=? AND next_at=?",
                    (
                        next_ad_boundary(ad, time.time()),
                        "delivery_unknown"
                        if state == "unknown"
                        else "partial_delivery"
                        if state == "partial"
                        else "",
                        account,
                        group,
                        rule_id,
                        planned,
                    ),
                ).rowcount
                if advanced and not claimed and error:
                    db.execute(
                        "INSERT OR IGNORE INTO business_rule_sends "
                        "VALUES(?,?,?,?,?,?,'skipped',?,?)",
                        (
                            operation,
                            account,
                            group,
                            "advertisement",
                            rule_id,
                            planned,
                            time.time(),
                            error,
                        ),
                    )

    async def close(self):
        """Cancel detached ad jobs before unloading their original service."""
        self.image_sessions.clear()
        tasks = tuple(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
