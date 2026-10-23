"""Owner-bound businesses and group configuration without global role grants."""

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar

from .store import Rejected, encode

PLATFORM = "platform"
LOCAL_SCOPES = {"ads", "points", "game", "moderation"}
POLICY = {"public": False, "pilot_owners": [], "max_groups": 3, "invoices": False}
ADMIN_CONTEXT = ContextVar("superbot_group_admin", default=None)


@asynccontextmanager
async def group_admin(runtime, uid, chat, *, allow_inactive=False):
    """Authorize one current request for local controls, never financial ownership.

    Args:
        runtime: Plugin with Telegram client and store.
        uid: Authenticated actor.
        chat: Explicit registered group.

    Yields:
        Nothing; scoped authorization is reset even when the request fails.

    Raises:
        Rejected: Telegram rights or local ownership changed.
    """
    from .group_authority import GroupAuthority

    async with GroupAuthority(runtime).session(uid, chat, allow_paused=allow_inactive):
        yield


def migrate(store):
    """Add ownership boundaries without rewriting existing business data.

    Args:
        store: Instance database whose legacy schema already exists.
    """
    store.db.executescript("""
        CREATE TABLE IF NOT EXISTS tenants(
            id TEXT PRIMARY KEY,owner TEXT NOT NULL UNIQUE,status TEXT NOT NULL,
            created REAL NOT NULL,version INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS tenant_groups(
            chat TEXT PRIMARY KEY,tenant TEXT NOT NULL REFERENCES tenants(id),
            status TEXT NOT NULL DEFAULT 'active',version INTEGER NOT NULL DEFAULT 1,
            verified REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'verified');
        CREATE INDEX IF NOT EXISTS tenant_groups_owner ON tenant_groups(tenant,status);
        CREATE TABLE IF NOT EXISTS tenant_settings(
            chat TEXT NOT NULL REFERENCES tenant_groups(chat),key TEXT NOT NULL,
            value TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(chat,key));
        CREATE TABLE IF NOT EXISTS tenant_bindings(
            token TEXT PRIMARY KEY,owner TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS tenant_bindings_expiry ON tenant_bindings(expires);
        CREATE TABLE IF NOT EXISTS tenant_resource_grants(
            chat TEXT NOT NULL,resource TEXT NOT NULL,budget INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(chat,resource));
        CREATE TABLE IF NOT EXISTS tenant_resource_uses(
            op TEXT PRIMARY KEY,chat TEXT NOT NULL,resource TEXT NOT NULL,at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tenant_service_contexts(
            uid TEXT NOT NULL,resource TEXT NOT NULL,chat TEXT NOT NULL,expires REAL NOT NULL,
            PRIMARY KEY(uid,resource));
        CREATE TABLE IF NOT EXISTS group_admin_grants(
            chat TEXT NOT NULL,uid TEXT NOT NULL,owner TEXT NOT NULL,
            created REAL NOT NULL,PRIMARY KEY(chat,uid));
        CREATE VIEW IF NOT EXISTS platform_mod_groups AS
            SELECT m.* FROM mod_groups m WHERE NOT EXISTS(
                SELECT 1 FROM tenant_groups g WHERE g.chat=m.chat AND g.tenant<>'platform');
        CREATE VIEW IF NOT EXISTS platform_ads AS SELECT * FROM ads WHERE tenant='platform';
    """)
    with store.tx() as db:
        if "mode" not in {
            r["name"] for r in db.execute("PRAGMA table_info(tenant_groups)")
        }:
            db.execute(
                "ALTER TABLE tenant_groups ADD COLUMN mode TEXT NOT NULL DEFAULT 'verified'"
            )
        db.execute(
            "INSERT OR IGNORE INTO tenants(id,owner,status,created) VALUES(?,?,'active',?)",
            (PLATFORM, store.owner, store.clock()),
        )
        if "tenant" not in {r["name"] for r in db.execute("PRAGMA table_info(ads)")}:
            db.execute(
                "ALTER TABLE ads ADD COLUMN tenant TEXT NOT NULL DEFAULT 'platform'"
            )
        for column, definition in {
            "enabled": "INTEGER NOT NULL DEFAULT 0",
            "version": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if column not in {
                r["name"]
                for r in db.execute("PRAGMA table_info(tenant_resource_grants)")
            }:
                db.execute(
                    f"ALTER TABLE tenant_resource_grants ADD COLUMN {column} {definition}"
                )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ads_tenant_status ON ads(tenant,status,at)"
        )
        if not db.execute(
            "SELECT 1 FROM meta WHERE key='tenant_legacy_groups'"
        ).fetchone():
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mod_groups'"
            ).fetchone():
                db.execute(
                    "INSERT OR IGNORE INTO tenant_groups(chat,tenant) SELECT chat,'platform' FROM mod_groups"
                )
            db.execute("INSERT INTO meta(key,value) VALUES('tenant_legacy_groups','1')")


def binding(store, chat, db=None):
    """Read the authoritative owner and status of a group.

    Args:
        store: Instance store.
        chat: Numeric Telegram group ID.
        db: Optional active transaction.

    Returns:
        Joined ownership row, or None for unclaimed legacy-compatible groups.
    """
    return (
        (db or store.db)
        .execute(
            "SELECT g.*,t.owner,t.status AS tenant_status,t.version AS tenant_version "
            "FROM tenant_groups g JOIN tenants t ON t.id=g.tenant WHERE g.chat=?",
            (str(chat),),
        )
        .fetchone()
    )


def local_config(store, chat, key, fallback, db=None):
    """Resolve explicit group settings without copying platform mutations.

    Args:
        store: Instance store.
        chat: Group ID or None for historical private operations.
        key: Setting name.
        fallback: Legacy-compatible value.
        db: Optional active transaction.

    Returns:
        Decoded group setting, or the supplied fallback.
    """
    row = (
        (db or store.db)
        .execute(
            "SELECT value FROM tenant_settings WHERE chat=? AND key=?", (str(chat), key)
        )
        .fetchone()
    )
    return json.loads(row[0]) if row else fallback


def platform_group(store, chat, db=None):
    """Identify groups belonging to the historical platform business.

    Args:
        store: Instance store.
        chat: Group ID.
        db: Optional active transaction.

    Returns:
        Whether legacy platform grants may address the group.
    """
    row = binding(store, chat, db)
    return row is None or row["tenant"] == PLATFORM


class Tenants:
    """Verify Telegram ownership and manage versioned local configuration."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.locks = {}

    async def verify_designation(self, uid, chat, status):
        """Require the current owner or an owner-designated live administrator.

        Args:
            uid: Actor.
            chat: Exact group.
            status: Fresh Telegram membership status.

        Raises:
            Rejected: Missing designation or ownership change.
        """
        if status == "creator":
            return
        grant = self.store.db.execute(
            "SELECT owner FROM group_admin_grants WHERE chat=? AND uid=?",
            (str(chat), str(uid)),
        ).fetchone()
        if status != "administrator" or not grant:
            raise Rejected("仅群主或群主指定的群管可管理本群，请联系群主授权。")
        try:
            owner = await self.runtime.bot.get_chat_member(chat, int(grant["owner"]))
        except Exception as exc:
            from .group_authority import VerificationUnavailable

            raise VerificationUnavailable("暂时无法核实授权群主，请稍后重试。") from exc
        if owner.status != "creator":
            raise Rejected("群主已变更，原群管授权失效，请新群主重新授权。")

    async def designate(self, uid, chat, target, enabled, expected):
        """Apply one owner-confirmed designation without granting global roles.

        Args:
            uid: Current owner.
            chat: Target group.
            target: Current Telegram administrator.
            enabled: Grant or revoke.
            expected: Group version from preview.

        Raises:
            Rejected: Stale, unauthorized or invalid request.
        """
        from .group_authority import GroupAuthority, require

        async with GroupAuthority(self.runtime).session(
            uid, chat, allow_paused=True
        ) as proof:
            if proof.status != "creator":
                raise Rejected("只有当前群主可指定或撤销群管，指定群管不能转授权。")
            if (
                type(enabled) is not bool
                or not str(target).isascii()
                or not str(target).isdigit()
                or str(target) == str(uid)
            ):
                raise Rejected("请选择其他Telegram真人管理员。")
            if enabled:
                member = await self.runtime.bot.get_chat_member(chat, int(target))
                if member.status != "administrator" or getattr(
                    getattr(member, "user", None), "is_bot", False
                ):
                    raise Rejected("只能授权当前Telegram真人管理员。")
            with self.store.tx() as db:
                require(self.store, uid, chat, db=db)
                if proof.versions[0] != expected:
                    raise Rejected("群配置或授权已变化，请重新预览。")
                if enabled:
                    db.execute(
                        "INSERT INTO group_admin_grants VALUES(?,?,?,?) "
                        "ON CONFLICT(chat,uid) DO UPDATE SET owner=excluded.owner,created=excluded.created",
                        (str(chat), str(target), str(uid), self.store.clock()),
                    )
                else:
                    db.execute(
                        "DELETE FROM group_admin_grants WHERE chat=? AND uid=?",
                        (str(chat), str(target)),
                    )
                db.execute(
                    "UPDATE mod_groups SET version=version+1 WHERE chat=?", (str(chat),)
                )
                self.store.audit(
                    db,
                    uid,
                    "group_admin_designation",
                    {"chat": str(chat), "target": str(target), "enabled": enabled},
                )

    def policy(self):
        return {**POLICY, **self.store.get("tenant_policy", {})}

    def configure_policy(self, actor, policy):
        self.store.require(actor, owner=True)
        if (
            set(policy) != set(POLICY)
            or type(policy["public"]) is not bool
            or type(policy["invoices"]) is not bool
            or type(policy["max_groups"]) is not int
            or not 1 <= policy["max_groups"] <= 100
            or not isinstance(policy["pilot_owners"], list)
            or any(
                not str(uid).isascii() or not str(uid).isdigit()
                for uid in policy["pilot_owners"]
            )
        ):
            raise Rejected("接入设置无效")
        with self.store.tx() as db:
            self.store.put(db, "tenant_policy", policy)
            self.store.audit(db, actor, "tenant_policy", policy)

    def invite(self, uid):
        # New groups are auto-registered from the Telegram admin event.  Keep
        # issuing a challenge only for legacy callers that still use bindgroup.
        with self.store.tx() as db:
            db.execute(
                "DELETE FROM tenant_bindings WHERE expires<=?", (self.store.clock(),)
            )
            db.execute("UPDATE tenant_bindings SET used=1 WHERE owner=?", (str(uid),))
            token = secrets.token_urlsafe(18)
            db.execute(
                "INSERT INTO tenant_bindings(token,owner,expires) VALUES(?,?,?)",
                (token, str(uid), self.store.clock() + 600),
            )
        return token

    def read(self, uid, chat):
        """Authorize historical visibility without granting paused businesses writes."""
        from .group_authority import require

        require(self.store, uid, chat)
        row = binding(self.store, chat)
        if not row:
            raise Rejected("本群经营记录尚未登记。")
        return row

    async def status(self, uid, chat, active, expected):
        """Suspend or restore the original verified owner, never transfer liability."""
        self.store.require(uid, owner=True)
        current = self.read(uid, chat)
        if current["owner"] != str(uid):
            raise Rejected("仅本群经营群主且具备总控权限可调整群状态")
        if type(active) is not bool:
            raise Rejected("无效状态")
        if active:
            info = await self.runtime.bot.get_chat(chat)
            actor = await self.runtime.bot.get_chat_member(chat, int(uid))
            bot = await self.runtime.bot.get_chat_member(chat, self.runtime.bot.id)
            if (
                info.type != "supergroup"
                or actor.status != "creator"
                or bot.status not in {"creator", "administrator"}
            ):
                raise Rejected("恢复前须核实本群群主及机器人管理员身份")
        with self.store.tx() as db:
            row = binding(self.store, chat, db)
            if (
                not row
                or row["tenant"] == PLATFORM
                or row["owner"] != str(uid)
                or row["version"] != expected
            ):
                raise Rejected("群状态已变化，请重新核查")
            db.execute(
                "UPDATE tenant_groups SET status=?,error=?,version=version+1 WHERE chat=?",
                (
                    "active" if active else "suspended",
                    "" if active else "platform_suspended",
                    str(chat),
                ),
            )
            db.execute(
                "UPDATE mod_groups SET enabled=0,version=version+1 WHERE chat=?",
                (str(chat),),
            )
            self.store.audit(
                db, uid, "tenant_status", {"chat": str(chat), "active": active}
            )

    async def restore(self, uid, chat, expected):
        """Restore basic service after live admin and registered owner checks."""
        before = binding(self.store, chat)
        if (
            not before
            or before["tenant"] == PLATFORM
            or before["status"] != "suspended"
            or before["error"] != "bot_left"
            or before["version"] != expected
            or before["tenant_status"] != "active"
        ):
            raise Rejected("群归属或停用状态已变化，请联系本BOT核查")
        try:
            info = await self.runtime.bot.get_chat(chat)
            owner = await self.runtime.bot.get_chat_member(chat, int(before["owner"]))
            bot = await self.runtime.bot.get_chat_member(chat, self.runtime.bot.id)
            actor = await self.runtime.bot.get_chat_member(chat, int(uid))
        except Exception as exc:
            raise Rejected("当前群主身份无法核验，请稍后重试") from exc
        if (
            info.type != "supergroup"
            or owner.status != "creator"
            or actor.status not in {"creator", "administrator"}
            or bot.status not in {"administrator", "creator"}
        ):
            raise Rejected("仅本群现任群主或管理员可恢复；群主变更须先核实经营归属。")
        await self.verify_designation(uid, chat, actor.status)
        with self.store.tx() as db:
            current = binding(self.store, chat, db)
            if (
                not current
                or dict(current) != dict(before)
                or current["version"] != expected
                or current["status"] != "suspended"
                or current["error"] != "bot_left"
            ):
                raise Rejected("群归属或停用状态已变化，请重新打开")
            db.execute(
                "UPDATE tenant_groups SET status='active',error='',version=version+1,"
                "verified=? WHERE chat=? AND version=?",
                (self.store.clock(), str(chat), expected),
            )
            db.execute(
                "UPDATE mod_groups SET enabled=1,version=version+1 WHERE chat=?",
                (str(chat),),
            )
            db.execute("DELETE FROM meta WHERE key=?", ("group_exit:" + str(chat),))
            self.store.audit(db, uid, "group_restored", {"chat": str(chat)})

    async def claim_owner(self, uid, chat, expected):
        """Let current admins correct an automatic binding to the actual creator.

        Args:
            uid: Confirming Telegram user.
            chat: Target group.
            expected: Binding version shown in the confirmation.

        Raises:
            Rejected: Ownership, version, or financial safeguards fail.
        """
        before = binding(self.store, chat)
        if (
            not before
            or before["tenant"] == PLATFORM
            or before["mode"] != "auto"
            or before["version"] != expected
            or before["error"] not in {"", "bot_left", "ownership_or_bot_rights"}
            or before["tenant_status"] != "active"
        ):
            raise Rejected("本群归属需人工核查，不能自助变更。")
        info = await self.runtime.bot.get_chat(chat)
        actor = await self.runtime.bot.get_chat_member(chat, int(uid))
        bot = await self.runtime.bot.get_chat_member(chat, self.runtime.bot.id)
        if (
            info.type != "supergroup"
            or actor.status not in {"creator", "administrator"}
            or bot.status not in {"creator", "administrator"}
        ):
            raise Rejected(
                "仅 Telegram 本群现任群主或管理员可确认归属，机器人须为管理员。"
            )
        creator = str(uid)
        if actor.status != "creator":
            administrators = await self.runtime.bot.get_chat_administrators(chat)
            creators = [str(m.user.id) for m in administrators if m.status == "creator"]
            if len(creators) != 1:
                raise Rejected("无法核验真实群主，未修改经营归属。")
            creator = creators[0]
        owner = await self.runtime.bot.get_chat_member(chat, int(creator))
        bot = await self.runtime.bot.get_chat_member(chat, self.runtime.bot.id)
        actor = await self.runtime.bot.get_chat_member(chat, int(uid))
        if (
            owner.status != "creator"
            or actor.status not in {"creator", "administrator"}
            or bot.status not in {"creator", "administrator"}
        ):
            raise Rejected("群主或管理员权限已变化，请重新打开。")
        await self.verify_designation(uid, chat, actor.status)
        with self.store.tx() as db:
            current = binding(self.store, chat, db)
            if not current or dict(current) != dict(before):
                raise Rejected("群归属或状态已变化，请重新打开。")
            if before["owner"] != creator:
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                financial = db.execute(
                    "SELECT 1 FROM ads WHERE json_extract(package,'$.target')=? LIMIT 1",
                    (str(chat),),
                ).fetchone()
                for table in (
                    "merchant_addresses",
                    "merchant_packages",
                    "merchant_invoices",
                ):
                    if (
                        table in tables
                        and db.execute(
                            f"SELECT 1 FROM {table} WHERE chat=? LIMIT 1", (str(chat),)
                        ).fetchone()
                    ):
                        financial = True
                if financial:
                    raise Rejected(
                        "本群已有广告订单或收款、广告位配置，须人工核查归属；不会自动转移。"
                    )
            tenant = "owner:" + creator
            existing = db.execute(
                "SELECT status FROM tenants WHERE id=?", (tenant,)
            ).fetchone()
            if existing and existing[0] != "active":
                raise Rejected("当前群主经营权限已暂停，请联系本BOT核查。")
            db.execute(
                "INSERT OR IGNORE INTO tenants(id,owner,status,created) VALUES(?,?,'active',?)",
                (tenant, creator, self.store.clock()),
            )
            db.execute(
                "UPDATE tenant_groups SET tenant=?,mode='verified',version=version+1,"
                "verified=? WHERE chat=?",
                (tenant, self.store.clock(), str(chat)),
            )
            self.store.audit(
                db,
                uid,
                "tenant_owner_confirmed",
                {
                    "chat": str(chat),
                    "previous_owner": before["owner"],
                    "owner": creator,
                },
            )

    async def bind(self, uid, chat, token):
        """Consume a private challenge only inside its owner's actual supergroup.

        Args:
            uid: Authenticated Telegram sender.
            chat: Actual group from the update, never user-supplied identity text.
            token: Short-lived challenge issued in private chat.

        Returns:
            Newly bound business identifier.
        """
        async with self.locks.setdefault(str(chat), asyncio.Lock()):
            info = await self.runtime.bot.get_chat(chat)
            owner = await self.runtime.bot.get_chat_member(chat, int(uid))
            bot = await self.runtime.bot.get_chat_member(chat, self.runtime.bot.id)
            if info.type != "supergroup" or owner.status != "creator":
                raise Rejected("仅真实超级群群主可绑定；普通管理员不能代领。")
            if bot.status not in {"administrator", "creator"}:
                raise Rejected("请先将机器人设为本群管理员，再完成绑定。")
            with self.store.tx() as db:
                policy = self.policy()
                if not policy["public"] and str(uid) not in policy["pilot_owners"]:
                    raise Rejected("自助接入已关闭")
                challenge = db.execute(
                    "SELECT * FROM tenant_bindings WHERE token=? AND owner=? AND used=0 AND expires>?",
                    (token, str(uid), self.store.clock()),
                ).fetchone()
                if not challenge:
                    raise Rejected("绑定凭证无效、过期或不属于你，请私聊重新获取。")
                if (
                    binding(self.store, chat, db)
                    or db.execute(
                        "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                        (str(chat),),
                    ).fetchone()
                ):
                    raise Rejected(
                        "本群已绑定或已有业务，请由本BOT核查，不能覆盖认领。"
                    )
                tenant = "owner:" + str(uid)
                db.execute(
                    "INSERT OR IGNORE INTO tenants(id,owner,status,created) VALUES(?,?,'active',?)",
                    (tenant, str(uid), self.store.clock()),
                )
                state = db.execute(
                    "SELECT * FROM tenants WHERE owner=?", (str(uid),)
                ).fetchone()
                if state["id"] != tenant or state["status"] != "active":
                    raise Rejected("此经营主体不能自助接入")
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM tenant_groups WHERE tenant=?", (tenant,)
                    ).fetchone()[0]
                    >= policy["max_groups"]
                ):
                    raise Rejected("已达到可接入群数量上限，请联系本BOT。")
                db.execute(
                    "INSERT INTO mod_groups(chat,title,enabled) VALUES(?,?,0) ON CONFLICT(chat) DO NOTHING",
                    (str(chat), info.title or str(chat)),
                )
                db.execute(
                    "INSERT INTO tenant_groups(chat,tenant,verified,mode) "
                    "VALUES(?,?,?,'verified')",
                    (str(chat), tenant, self.store.clock()),
                )
                from .points import DEFAULT

                db.execute(
                    "INSERT INTO tenant_settings(chat,key,value) VALUES(?,'points',?)",
                    (str(chat), encode({**DEFAULT, "groups": [str(chat)]})),
                )
                db.execute("UPDATE tenant_bindings SET used=1 WHERE token=?", (token,))
                self.store.audit(
                    db, uid, "tenant_bound", {"tenant": tenant, "chat": str(chat)}
                )
                return tenant

    async def verify(
        self, uid, chat, scope="moderation", permission=None, *, allow_paused=False
    ):
        """Recheck real ownership and local revocation across network awaits.

        Args:
            uid: Acting group owner or platform owner.
            chat: Bound group ID.
            scope: Required local capability.
            permission: Optional Telegram bot permission for the intended action.

        Returns:
            Verified ownership snapshot for optimistic mutation checks.
        """
        from .group_authority import CURRENT, GroupAuthority, clear_proof

        clear_proof()
        if scope not in LOCAL_SCOPES:
            raise Rejected("无效的群管理范围。")
        async with GroupAuthority(self.runtime).session(
            uid, chat, permission, allow_paused=allow_paused
        ) as proof:
            result = binding(self.store, chat)
            result = dict(result) if result else {"chat": str(chat), "tenant": PLATFORM}
        CURRENT.set(proof)
        return result

    def settings(self, actor, chat, key, value, expected):
        self.store.require(actor, "points" if key == "points" else "game", chat=chat)
        if key != "points":
            raise Rejected("此设置请使用对应玩法管理入口")
        from .points import validate

        validate(value)
        if value["groups"] != [str(chat)]:
            raise Rejected("奖励配置只能作用于当前群")
        with self.store.tx() as db:
            self.store.require(actor, "points", db, chat=chat)
            current = db.execute(
                "SELECT version FROM tenant_settings WHERE chat=? AND key=?",
                (str(chat), key),
            ).fetchone()
            if (current[0] if current else 0) != expected:
                raise Rejected("设置已变化，请重新预览")
            db.execute(
                "INSERT INTO tenant_settings(chat,key,value) VALUES(?,?,?) ON CONFLICT(chat,key) DO UPDATE SET value=excluded.value,version=tenant_settings.version+1",
                (str(chat), key, encode(value)),
            )
            self.store.audit(
                db,
                actor,
                "tenant_setting",
                {"chat": str(chat), "key": key, "value": value},
            )

    def switch(self, actor, chat, feature, enabled, expected):
        """Change one local switch without opening platform-wide admission.

        Args:
            actor: Verified owner.
            chat: Owned group.
            feature: Group registration or local game identifier.
            enabled: Explicit boolean value.
            expected: Ownership version captured by the preview.
        """
        names = {
            "group",
            "canada",
            "ads",
            "points",
            "chat",
            "avatar",
            "wheel",
            "slots",
            "mines",
            "k3",
            "duel",
        }
        if feature not in names or type(enabled) is not bool:
            raise Rejected("无效开关")
        with self.store.tx() as db:
            self.store.require(actor, "moderation", db, chat=chat)
            owner = binding(self.store, chat, db)
            if not owner or owner["tenant"] == PLATFORM or owner["version"] != expected:
                raise Rejected("群归属或配置已变化，请重新预览")
            if owner["status"] != "active" or owner["tenant_status"] != "active":
                raise Rejected("本群已暂停，须本BOT复核恢复后才能调整功能")
            modules = self.store.get("modules", {}, db)
            if (
                enabled
                and feature not in {"group", "ads", "points", "chat", "avatar"}
                and (
                    not modules.get("game")
                    or not modules.get(feature, feature in {"canada", "duel"})
                )
            ):
                raise Rejected("本BOT未开放此玩法，本群不能绕过总开关。")
            if enabled and feature == "ads" and not modules.get("ads"):
                raise Rejected("本BOT广告总开关已关闭")
            if enabled and feature == "points" and not modules.get("points"):
                raise Rejected("本BOT积分总开关已关闭")
            if feature == "group":
                db.execute(
                    "UPDATE mod_groups SET enabled=?,version=version+1 WHERE chat=?",
                    (int(enabled), str(chat)),
                )
            elif feature in {"canada", "ads", "points", "chat", "avatar"}:
                db.execute(
                    "INSERT INTO tenant_settings(chat,key,value) VALUES(?,?,?) ON CONFLICT(chat,key) DO UPDATE SET value=excluded.value,version=tenant_settings.version+1",
                    (str(chat), feature + "_enabled", encode(enabled)),
                )
            elif feature == "wheel":
                from .wheel import DEFAULT

                current = db.execute(
                    "SELECT config FROM wheel_groups WHERE chat=?", (str(chat),)
                ).fetchone()
                value = {
                    **(json.loads(current[0]) if current else DEFAULT),
                    "enabled": enabled,
                }
                db.execute(
                    "INSERT INTO wheel_groups(chat,config,version) VALUES(?,?,1) ON CONFLICT(chat) DO UPDATE SET config=excluded.config,version=wheel_groups.version+1",
                    (str(chat), encode(value)),
                )
            else:
                db.execute(
                    f"INSERT INTO {feature}_groups(chat,enabled,version) VALUES(?,?,1) ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled,version=version+1",
                    (str(chat), int(enabled)),
                )
            if feature in {"slots", "mines"} and enabled:
                groups = self.store.get("games_v3_groups", [], db)
                if str(chat) not in groups:
                    self.store.put(db, "games_v3_groups", [*groups, str(chat)])
            db.execute(
                "UPDATE tenant_groups SET version=version+1 WHERE chat=?", (str(chat),)
            )
            self.store.audit(
                db,
                actor,
                "tenant_switch",
                {"chat": str(chat), "feature": feature, "enabled": enabled},
            )

    def resource_context(self, uid, resource):
        """Resolve a private user's explicit, expiring group budget selection."""
        row = self.store.db.execute(
            "SELECT chat FROM tenant_service_contexts WHERE uid=? AND resource=? AND expires>?",
            (str(uid), resource, self.store.clock()),
        ).fetchone()
        return row[0] if row else None

    def resource_grant(self, actor, chat, resource, budget, expected):
        """Authorize a bounded total call/task budget without changing global providers."""
        self.store.require(actor, owner=True)
        if self.read(actor, chat)["owner"] != str(actor):
            raise Rejected("仅本群经营群主且具备总控权限可调整服务限额")
        if (
            resource not in {"avatar", "chat"}
            or type(budget) is not int
            or not 0 <= budget <= 1000
        ):
            raise Rejected("预算须为0—1000次，0表示停用；这是调用上限，不是金额额度。")
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM tenant_resource_grants WHERE chat=? AND resource=?",
                (str(chat), resource),
            ).fetchone()
            if (row["version"] if row else 0) != expected or (
                budget and row and budget < row["used"]
            ):
                raise Rejected("预算版本已变化，或总上限低于已使用次数")
            db.execute(
                "INSERT INTO tenant_resource_grants(chat,resource,budget,enabled) VALUES(?,?,?,?) ON CONFLICT(chat,resource) DO UPDATE SET budget=excluded.budget,enabled=excluded.enabled,version=tenant_resource_grants.version+1",
                (str(chat), resource, budget, int(budget > 0)),
            )
            self.store.audit(
                db,
                actor,
                "tenant_resource_grant",
                {"chat": str(chat), "resource": resource, "budget": budget},
            )

    async def select_resource(self, uid, chat, resource):
        """Require live membership before binding private service to one group budget."""
        if resource not in {"avatar", "chat"}:
            raise Rejected("服务不存在")
        owner = binding(self.store, chat)
        if not owner or owner["tenant"] == PLATFORM:
            raise Rejected("请选择独立经营群")
        await self.verify(owner["owner"], chat)
        member = await self.runtime.bot.get_chat_member(chat, int(uid))
        if member.status not in {"creator", "administrator", "member"} and not (
            member.status == "restricted" and getattr(member, "is_member", False)
        ):
            raise Rejected("仅本群当前成员可选择此服务预算")
        if not self.resource_allowed(uid, chat, resource):
            raise Rejected("本群服务已关闭、暂停或达到总控显式限额；请联系管理员。")
        self.store.db.execute(
            "INSERT INTO tenant_service_contexts(uid,resource,chat,expires) VALUES(?,?,?,?) ON CONFLICT(uid,resource) DO UPDATE SET chat=excluded.chat,expires=excluded.expires",
            (str(uid), resource, str(chat), self.store.clock() + 1800),
        )

    def consume_resource(self, uid, chat, resource, op, db):
        """Reserve one call atomically; uncertain calls remain charged to the call cap."""
        if platform_group(self.store, chat, db):
            return
        existing = db.execute(
            "SELECT * FROM tenant_resource_uses WHERE op=?", (op,)
        ).fetchone()
        if existing:
            if (existing["chat"], existing["resource"]) != (str(chat), resource):
                raise Rejected("预算操作身份冲突")
            raise Rejected("此付费请求已经提交，不能重复调用")
        if not self.resource_allowed(uid, chat, resource):
            raise Rejected("本群服务已关闭、暂停或达到总控显式限额")
        grant = db.execute(
            "SELECT 1 FROM tenant_resource_grants WHERE chat=? AND resource=?",
            (str(chat), resource),
        ).fetchone()
        if (
            grant
            and not db.execute(
                "UPDATE tenant_resource_grants SET used=used+1 WHERE chat=? AND resource=? AND enabled=1 AND used<budget",
                (str(chat), resource),
            ).rowcount
        ):
            raise Rejected("本群已达到总控显式限额")
        db.execute(
            "INSERT INTO tenant_resource_uses(op,chat,resource,at) VALUES(?,?,?,?)",
            (op, str(chat), resource, self.store.clock()),
        )

    def resource_allowed(self, uid, chat=None, resource="avatar", op=None):
        """Use shared services by default while respecting explicit suspensions.

        Args:
            uid: Authenticated user ID.
            chat: Group context, or None for a private entry.

        Returns:
            Whether the legacy platform service may be used.
        """
        if resource not in {"chat", "avatar"}:
            return False
        if chat is None:
            chat = self.resource_context(uid, resource)
            if chat is None:
                # A private service request may only use a budgeted group where
                # the user is a known member. Do not infer ownership from chat
                # text or expose one group's budget to another tenant.
                candidate = self.store.db.execute(
                    "SELECT g.chat FROM mod_members m "
                    "JOIN tenant_groups g ON g.chat=m.chat "
                    "JOIN tenants t ON t.id=g.tenant AND t.status='active' "
                    "JOIN mod_groups mg ON mg.chat=g.chat AND mg.enabled=1 "
                    "LEFT JOIN tenant_resource_grants r ON r.chat=g.chat AND r.resource=? "
                    "WHERE m.uid=? AND g.status='active' "
                    "AND (r.resource IS NULL OR (r.enabled=1 AND r.used<r.budget)) "
                    "AND NOT EXISTS(SELECT 1 FROM tenant_settings s WHERE s.chat=g.chat AND s.key=? AND s.value='false') "
                    "ORDER BY m.seen DESC LIMIT 1",
                    (resource, str(uid), resource + "_enabled"),
                ).fetchone()
                if candidate:
                    chat = candidate["chat"]
        if chat is not None:
            if not local_config(self.store, chat, resource + "_enabled", True):
                return False
            if platform_group(self.store, chat):
                return True
            owner = binding(self.store, chat)
            grant = self.store.db.execute(
                "SELECT * FROM tenant_resource_grants WHERE chat=? AND resource=?",
                (str(chat), resource),
            ).fetchone()
            group = self.store.db.execute(
                "SELECT enabled FROM mod_groups WHERE chat=?", (str(chat),)
            ).fetchone()
            reserved = (
                op
                and self.store.db.execute(
                    "SELECT 1 FROM tenant_resource_uses WHERE op=? AND chat=? AND resource=?",
                    (op, str(chat), resource),
                ).fetchone()
            )
            return bool(
                owner
                and owner["status"] == owner["tenant_status"] == "active"
                and group
                and group[0]
                and (
                    not grant
                    or (
                        grant["enabled"]
                        and (reserved or grant["used"] < grant["budget"])
                    )
                )
            )
        if str(uid) == self.store.owner or self.store.allowed(uid):
            return True
        return bool(
            self.store.db.execute(
                "SELECT 1 FROM mod_members m LEFT JOIN tenant_groups g ON g.chat=m.chat "
                "WHERE m.uid=? AND (g.tenant IS NULL OR g.tenant='platform') LIMIT 1",
                (str(uid),),
            ).fetchone()
        )

    def operating(self, actor, chat, key, value, expected):
        """Save allowlisted group parameters bounded by platform game rules.

        Args:
            actor: Verified group owner.
            chat: Owned group ID.
            key: One local operational setting, never odds or probability.
            value: Validated stakes or limits.
            expected: Group-setting version from the confirmation.
        """
        from .k3 import CAPS
        from .slots import STAKES

        if key in {"slots_stakes", "mines_stakes"}:
            if (
                not isinstance(value, list)
                or not value
                or len(value) > len(STAKES)
                or len(set(value)) != len(value)
                or any(type(x) is not int or x not in STAKES for x in value)
            ):
                raise Rejected("档位须从100／300／800／1500／2000中选择，不得重复。")
            value = sorted(value)
        elif key == "k3_limits":
            if (
                not isinstance(value, dict)
                or set(value) != {"ordinary", "special", "number", "total"}
                or any(type(v) is not int or v < 1 for v in value.values())
            ):
                raise Rejected("请填写正整数限额")
            ceilings = {
                "ordinary": CAPS["big"],
                "special": CAPS["triple"],
                "number": CAPS["sum3"],
                "total": 2000,
            }
            if any(value[k] > ceilings[k] for k in ceilings):
                raise Rejected(
                    "不能超过本BOT上限：普通1000、特殊100、号码20、单期2000。"
                )
        elif key.startswith("canada_limits:"):
            room = self.runtime.game.rooms().get(key.split(":", 1)[1])
            if (
                not room
                or not isinstance(value, dict)
                or set(value) != {"minimum", "maximum", "total"}
                or any(type(v) is not int for v in value.values())
            ):
                raise Rejected("加拿大限额参数无效")
            if (
                not room["minimum"]
                <= value["minimum"]
                <= value["maximum"]
                <= room["maximum"]
                or not value["maximum"] <= value["total"] <= room["total"]
            ):
                raise Rejected("本群限额须在本BOT范围内，且最小≤单笔≤单期。")
        else:
            raise Rejected("该参数由本BOT管理，群主不能修改。")
        with self.store.tx() as db:
            self.store.require(actor, "game", db, chat=chat)
            row = db.execute(
                "SELECT version FROM tenant_settings WHERE chat=? AND key=?",
                (str(chat), key),
            ).fetchone()
            if (row[0] if row else 0) != expected:
                raise Rejected("配置已改变，请重新预览")
            db.execute(
                "INSERT INTO tenant_settings(chat,key,value) VALUES(?,?,?) ON CONFLICT(chat,key) DO UPDATE SET value=excluded.value,version=tenant_settings.version+1",
                (str(chat), key, encode(value)),
            )
            self.store.audit(
                db,
                actor,
                "tenant_operating",
                {"chat": str(chat), "key": key, "value": value},
            )

    async def loop(self):
        """Periodically suspend ownership drift without altering in-flight ledgers."""
        while True:
            application = getattr(self.runtime, "application", None)
            if not application or not application.running:
                await asyncio.sleep(5)
                continue
            rows = self.store.db.execute(
                "SELECT g.chat,t.owner FROM tenant_groups g JOIN tenants t ON t.id=g.tenant "
                "WHERE g.tenant<>'platform' AND g.status='active' AND t.status='active' AND g.verified<? "
                "ORDER BY g.verified,g.chat LIMIT 4",
                (self.store.clock() - 60,),
            ).fetchall()
            for row in rows:
                try:
                    await self.verify(row["owner"], row["chat"])
                except Rejected:
                    pass
                except Exception as exc:
                    self.store.db.execute(
                        "UPDATE tenant_groups SET error=? WHERE chat=?",
                        (type(exc).__name__, row["chat"]),
                    )
                    self.runtime.report("tenant_verification", exc)
            await asyncio.sleep(5)
