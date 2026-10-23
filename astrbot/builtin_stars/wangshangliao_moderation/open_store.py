"""Transactional open-service tenants, confirmations and model admission."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class OpenError(ValueError):
    """Represent a bounded public-service validation failure."""


DEFAULT_RULES = {"advertising": True, "flood": True, "keywords": []}
DEFAULT_GROUP_TEMPLATE_VERSION = 1
DEFAULT_GROUP_RULES = {
    "advertising": True,
    "flood": True,
    "keywords": [
        "稳赚不赔",
        "保本套利",
        "导师带赚",
        "刷单垫付",
        "垫付刷单",
        "充值解冻",
        "缴费解冻",
        "私聊领取彩金",
        "包赔上分",
        "刷流水返佣",
        "出售银行卡",
        "收购银行卡",
        "出售实名账号",
        "代办洗钱",
        "裸聊接单",
        "色情直播",
        "成人大秀",
        "博彩开户注册",
        "收fc",
        "免费P图上分",
        "直播打狗中",
    ],
    "custom_rules": [
        {"name": "收款截图招揽", "terms": ["收款", "截图", "加我"], "enabled": True},
        {"name": "闲鱼出租招揽", "terms": ["闲鱼", "出租", "客服"], "enabled": True},
        {"name": "QQ出租报价", "terms": ["qq", "出租", "月亮"], "enabled": True},
        {
            "name": "验证码接码招揽",
            "terms": ["接码", "验证码", "秒结账"],
            "enabled": True,
        },
        {
            "name": "视频代发招揽",
            "terms": ["代发", "朋友圈", "项目客服"],
            "enabled": True,
        },
        {
            "name": "不禁言项目群引流",
            "terms": ["不禁言", "超级群", "做项目"],
            "enabled": True,
        },
        {
            "name": "结余账号高价回收",
            "terms": ["高价收", "结余账号", "联系我"],
            "enabled": True,
        },
        {
            "name": "首充转发招揽",
            "terms": ["首冲", "转发", "交流群"],
            "enabled": True,
        },
        {
            "name": "代言费数据招揽",
            "terms": ["代言费", "绑定提现", "合营", "数据"],
            "enabled": True,
        },
        {
            "name": "无视IP充值招揽",
            "terms": ["充值", "无视ip", "也可以来"],
            "enabled": True,
        },
        {
            "name": "注册赠送导航推广",
            "terms": ["注册送", "导航网"],
            "enabled": True,
        },
    ],
}


class OpenStore:
    """Keep public tenants entirely separate from production financial stores."""

    def __init__(self, root):
        self.path = Path(root) / "open-service.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS tenants(
                    account TEXT,group_id TEXT,name TEXT NOT NULL,team TEXT NOT NULL,
                    present INTEGER NOT NULL,epoch INTEGER NOT NULL,
                    inviter TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',
                    joined REAL NOT NULL,active INTEGER NOT NULL DEFAULT 0,
                    paused INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,
                    managers TEXT NOT NULL DEFAULT '[]',rules TEXT NOT NULL,
                    auto_claim INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(account,group_id));
                CREATE TABLE IF NOT EXISTS capabilities(
                    digest TEXT PRIMARY KEY,kind TEXT,account TEXT,group_id TEXT,
                    epoch INTEGER,version INTEGER,actor TEXT,session TEXT,
                    target TEXT,operation TEXT,message TEXT,expires REAL,used INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS settings(
                    id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,
                    paused INTEGER NOT NULL,personal_limit INTEGER NOT NULL,
                    global_limit INTEGER NOT NULL);
                INSERT OR IGNORE INTO settings VALUES(1,1,0,20,1000);
                CREATE TABLE IF NOT EXISTS calls(
                    request TEXT PRIMARY KEY,sender TEXT,day TEXT,exempt INTEGER,
                    created REAL,state TEXT);
                CREATE INDEX IF NOT EXISTS calls_day ON calls(day,sender,exempt);
                CREATE INDEX IF NOT EXISTS calls_sender_time ON calls(sender,created);
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY,created REAL,actor TEXT,account TEXT,
                    group_id TEXT,action TEXT,detail TEXT);
                CREATE TABLE IF NOT EXISTS quota_admins(uid TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS quota_aliases(
                    principal TEXT PRIMARY KEY,uid TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS manager_quotas(
                    account TEXT,group_id TEXT,uid TEXT,daily_limit INTEGER NOT NULL,
                    version INTEGER NOT NULL,PRIMARY KEY(account,group_id,uid));
                CREATE TABLE IF NOT EXISTS group_ai(
                    account TEXT,group_id TEXT,version INTEGER NOT NULL,
                    config TEXT NOT NULL,owner TEXT NOT NULL DEFAULT '',
                    last_attempt REAL NOT NULL DEFAULT 0,
                    blocked TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(account,group_id));
                CREATE TABLE IF NOT EXISTS ai_profiles(
                    kind TEXT,account TEXT,group_id TEXT,version INTEGER NOT NULL,
                    selection TEXT NOT NULL,PRIMARY KEY(kind,account,group_id));
                CREATE TABLE IF NOT EXISTS retired_groups(group_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS actor_bindings(
                    account TEXT,uid TEXT,peer TEXT,checked REAL,conflicted INTEGER DEFAULT 0,
                    PRIMARY KEY(account,uid));
                CREATE TABLE IF NOT EXISTS account_labels(
                    account TEXT PRIMARY KEY,peer TEXT,nickname TEXT,checked REAL);
                CREATE TABLE IF NOT EXISTS guidance_trials(
                    account TEXT,group_id TEXT,uid TEXT,principal TEXT NOT NULL,
                    epoch INTEGER NOT NULL,version INTEGER NOT NULL,
                    quota INTEGER NOT NULL,active INTEGER NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(account,group_id,uid));
                CREATE TABLE IF NOT EXISTS ai_budget(
                    id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,
                    confirmed INTEGER NOT NULL,provider TEXT NOT NULL,evidence TEXT NOT NULL,
                    checked REAL NOT NULL);
                INSERT OR IGNORE INTO ai_budget(id,version,confirmed,provider,evidence,checked)
                    VALUES(1,1,0,'','',0);
                CREATE TABLE IF NOT EXISTS membership_notices(
                    account TEXT,team TEXT,message TEXT,kind TEXT,event_at REAL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY(account,team,message));
            """)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(tenants)")}
            for name, definition in {
                "admission": "TEXT NOT NULL DEFAULT 'legacy'",
                "native_role": "TEXT NOT NULL DEFAULT ''",
                "cleanup_epoch": "INTEGER NOT NULL DEFAULT 0",
                "cleanup_done": "INTEGER NOT NULL DEFAULT 0",
                "leave_state": "TEXT NOT NULL DEFAULT ''",
                "rules_only": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE tenants ADD COLUMN {name} {definition}")
            if "auto_claim" not in {
                r["name"] for r in db.execute("PRAGMA table_info(tenants)")
            }:
                db.execute(
                    "ALTER TABLE tenants ADD COLUMN auto_claim INTEGER NOT NULL DEFAULT 0"
                )
            if "binding" not in {
                r["name"] for r in db.execute("PRAGMA table_info(ai_budget)")
            }:
                db.execute(
                    "ALTER TABLE ai_budget ADD COLUMN binding TEXT NOT NULL DEFAULT ''"
                )
        self.path.chmod(0o600)

    def remember_roster(self, account, rows, *, guard=None):
        """Persist signed UID/peer bindings without accepting conflicting identities.

        Args:
            account: Authenticated robot account.
            rows: Complete, business-authenticated member mapping.
            guard: Account and operation revocation check.
        """
        with self.connect(write=True, guard=guard) as db:
            for uid, member in rows.items():
                peer = str(member["nimId"])
                conflict = db.execute(
                    "SELECT 1 FROM actor_bindings WHERE account=? AND "
                    "((uid=? AND peer<>?) OR (peer=? AND uid<>?))",
                    (account, uid, peer, peer, uid),
                ).fetchone()
                if conflict:
                    db.execute(
                        "UPDATE actor_bindings SET conflicted=1 WHERE account=? "
                        "AND (uid=? OR peer=?)",
                        (account, uid, peer),
                    )
                    continue
                db.execute(
                    "INSERT INTO actor_bindings(account,uid,peer,checked) VALUES(?,?,?,?) "
                    "ON CONFLICT(account,uid) DO UPDATE SET checked=excluded.checked",
                    (account, uid, peer, time.time()),
                )

    def verified_actor(self, account, uid, peer):
        """Read only recent, unambiguous bindings produced by complete rosters."""
        with self.connect() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM actor_bindings WHERE account=? AND uid=? AND peer=? "
                    "AND conflicted=0 AND checked>=?",
                    (account, uid, peer, time.time() - 60),
                ).fetchone()
            )

    def manager_bindings(self, account, group):
        """Resolve existing grants through durable, unambiguous UID/NIM bindings.

        Args:
            account: Authenticated robot owning both the grants and bindings.
            group: Exact business group whose existing managers may be revoked.

        Returns:
            Existing manager UIDs mapped to their previously verified NIM peers.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT managers FROM tenants WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if row is None:
                return {}
            users = set(json.loads(row["managers"]))
            return {
                binding["uid"]: binding["peer"]
                for binding in db.execute(
                    "SELECT uid,peer FROM actor_bindings WHERE account=? AND conflicted=0",
                    (account,),
                )
                if binding["uid"] in users and binding["peer"]
            }

    def budget(self):
        """Read the default-deny provider hard-budget gate."""
        with self.connect() as db:
            return dict(db.execute("SELECT * FROM ai_budget WHERE id=1").fetchone())

    def confirm_budget(
        self, version, confirmed, provider, evidence, actor, *, binding=""
    ):
        """Record authenticated total-control verification of a provider hard cap."""
        if (
            type(version) is not int
            or type(confirmed) is not bool
            or not isinstance(provider, str)
            or not 0 < len(provider) <= 256
            or not isinstance(evidence, str)
            or len(evidence) > 500
            or confirmed
            and not evidence.strip()
        ):
            raise OpenError("budget_invalid")
        with self.connect(write=True) as db:
            if (
                db.execute(
                    "UPDATE ai_budget SET version=version+1,confirmed=?,provider=?,evidence=?,checked=?,binding=? "
                    "WHERE id=1 AND version=?",
                    (
                        confirmed,
                        provider,
                        evidence.strip(),
                        time.time(),
                        binding,
                        version,
                    ),
                ).rowcount
                != 1
            ):
                raise OpenError("budget_changed")
            self.log(db, actor, "", "", "budget_verified", {"confirmed": confirmed})

    @contextmanager
    def connect(self, *, write=False, guard=None):
        """Own a short connection and optionally a serializable write transaction."""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                if guard:
                    guard()
                yield db
                if guard:
                    guard()
        finally:
            db.close()

    def log(self, db, actor, account, group, action, detail=None):
        """Append non-secret audit data within the owning transaction."""
        db.execute(
            "INSERT INTO audit(created,actor,account,group_id,action,detail) VALUES(?,?,?,?,?,?)",
            (time.time(), actor, account, group, action, json.dumps(detail or {})),
        )

    def settings(self):
        """Return the current versioned service limits."""
        with self.connect() as db:
            return dict(db.execute("SELECT * FROM settings").fetchone())

    def usage(self):
        """Return current Beijing-day usage without exposing prompts or model replies."""
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self.connect() as db:
            row = db.execute(
                "SELECT SUM(exempt=0),SUM(exempt=1) FROM calls WHERE day=? AND state<>'not_sent'",
                (day,),
            ).fetchone()
        return {"day": day, "used": row[0] or 0, "exempt_used": row[1] or 0}

    def quota_admins(self):
        """Read local quota exemptions without changing AstrBot's global roles.

        Returns:
            Durable account identifiers configured by total control.
        """
        with self.connect() as db:
            return [
                r[0] for r in db.execute("SELECT uid FROM quota_admins ORDER BY uid")
            ]

    def manager_quotas(self):
        """Return total-control quota overrides, never chat authority."""
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM manager_quotas ORDER BY account,group_id,uid"
                )
            ]

    def set_manager_quota(
        self, account, group, uid, limit, version, tenant_version, actor, *, guard
    ):
        """Set a daily override only for an existing manager in the exact tenant."""
        if (
            type(version) is not int
            or type(tenant_version) is not int
            or type(limit) is not int
            or not 0 <= limit <= 1000
        ):
            raise OpenError("manager_quota_invalid")
        with self.connect(write=True, guard=guard) as db:
            tenant = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if not tenant or tenant["version"] != tenant_version:
                raise OpenError("tenant_changed")
            self.management_guard(db, tenant, actor, control=True)
            if uid not in json.loads(tenant["managers"]):
                raise OpenError("manager_required")
            default = db.execute("SELECT personal_limit FROM settings").fetchone()[0]
            if limit and limit < default:
                raise OpenError("manager_quota_invalid")
            current = db.execute(
                "SELECT version FROM manager_quotas WHERE account=? AND group_id=? AND uid=?",
                (account, group, uid),
            ).fetchone()
            if (current["version"] if current else 0) != version:
                raise OpenError("quota_changed")
            # Keep a tombstone version so deleting/recreating an override cannot
            # make an old browser confirmation current again.
            db.execute(
                "INSERT INTO manager_quotas VALUES(?,?,?,?,?) "
                "ON CONFLICT(account,group_id,uid) DO UPDATE SET "
                "daily_limit=excluded.daily_limit,version=excluded.version",
                (account, group, uid, limit, version + 1),
            )
            self.log(
                db, actor, account, group, "manager_quota", {"uid": uid, "limit": limit}
            )

    def configure(
        self, version, paused, personal_limit, global_limit, actor, *, admins=None
    ):
        """Change bounded quotas and revoke chat manager previews when paused."""
        if (
            type(version) is not int
            or type(paused) is not bool
            or type(personal_limit) is not int
            or not 1 <= personal_limit <= 1000
            or type(global_limit) is not int
            or not 1 <= global_limit <= 100000
        ):
            raise OpenError("settings_invalid")
        with self.connect(write=True) as db:
            if admins is not None and (
                not isinstance(admins, list)
                or len(admins) > 20
                or any(
                    not isinstance(u, str)
                    or not u.isascii()
                    or not u.isdigit()
                    or not 0 < int(u) < 1 << 30
                    for u in admins
                )
            ):
                raise OpenError("admins_invalid")
            if (
                db.execute(
                    "UPDATE settings SET version=version+1,paused=?,personal_limit=?,global_limit=? "
                    "WHERE version=?",
                    (paused, personal_limit, global_limit, version),
                ).rowcount
                != 1
            ):
                raise OpenError("settings_changed")
            if paused:
                db.execute(
                    "UPDATE capabilities SET used=1 WHERE kind='manager' AND used=0"
                )
            if admins is not None:
                db.execute("DELETE FROM quota_admins")
                db.executemany(
                    "INSERT INTO quota_admins VALUES(?)",
                    [(u,) for u in dict.fromkeys(admins)],
                )
            self.log(db, actor, "", "", "configure")

    def tenants(self, account=""):
        """Read detached tenant rows, optionally scoped to one robot account."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM tenants" + (" WHERE account=?" if account else ""),
                (account,) if account else (),
            ).fetchall()
        return [
            {
                **dict(row),
                "managers": json.loads(row["managers"]),
                "rules": json.loads(row["rules"]),
            }
            for row in rows
        ]

    def tenant(self, account, group):
        """Resolve an exact account/group identity, never a display name."""
        return next((t for t in self.tenants(account) if t["group_id"] == group), None)

    def retired_groups(self):
        """Read minimal identifiers excluded from future discovery.

        Returns:
            Group IDs without names, accounts or business history.
        """
        with self.connect() as db:
            return {r[0] for r in db.execute("SELECT group_id FROM retired_groups")}

    def retire_group(self, group):
        """Remove all open-service group records and prevent rediscovery.

        Args:
            group: Exact platform-wide group identifier approved for removal.

        Raises:
            OpenError: If the identifier is invalid.
        """
        if (
            not isinstance(group, str)
            or not group.isascii()
            or not group.isdigit()
            or not 0 < int(group) < 1 << 30
        ):
            raise OpenError("group_invalid")
        with self.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO retired_groups VALUES(?)", (group,))
            for table in (
                "capabilities",
                "audit",
                "tenants",
                "manager_quotas",
                "group_ai",
                "ai_profiles",
            ):
                db.execute(f"DELETE FROM {table} WHERE group_id=?", (group,))
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in (
                "business_rules",
                "business_rule_drafts",
                "business_ad_cursors",
                "business_rule_images",
                "business_ad_rotations",
            ):
                if table in tables:
                    db.execute(f"DELETE FROM {table} WHERE group_id=?", (group,))

    def discover(self, account, catalog, *, allow_auto=False, guard=None):
        """Record authenticated directory membership without assigning managers.

        Args:
            account: Authenticated robot account.
            catalog: Validated native directory entries.
            allow_auto: Whether new membership may start an invitation claim.
            guard: Optional account and caller lifecycle check.

        Returns:
            Directory entries excluding explicitly retired groups.
        """
        if len(catalog) > 1000:
            raise OpenError("directory_limit")
        with self.connect(write=True, guard=guard) as db:
            retired = {r[0] for r in db.execute("SELECT group_id FROM retired_groups")}
            catalog = {
                g: entry for g, entry in catalog.items() if str(g) not in retired
            }
            prior = {
                r["group_id"]: r
                for r in db.execute("SELECT * FROM tenants WHERE account=?", (account,))
            }
            for group, entry in catalog.items():
                if (
                    not str(group).isascii()
                    or not str(group).isdigit()
                    or not 0 < int(group) < 1 << 30
                    or not str(entry["team"]).isascii()
                    or not str(entry["team"]).isdigit()
                ):
                    raise OpenError("directory_identity")
                row = prior.get(group)
                witnessed = db.execute(
                    "SELECT 1 FROM membership_notices WHERE account=? AND team=? "
                    "AND kind='join' AND state='pending' AND event_at>=? LIMIT 1",
                    (account, entry["team"], time.time() - 120),
                ).fetchone()
                trusted_join = allow_auto or bool(witnessed)
                if row is None:
                    db.execute(
                        "INSERT INTO tenants(account,group_id,name,team,present,epoch,joined,rules,auto_claim,admission) "
                        "VALUES(?,?,?,?,1,1,?,?,?,'pending')",
                        (
                            account,
                            group,
                            str(entry["name"])[:128],
                            entry["team"],
                            time.time(),
                            json.dumps(DEFAULT_RULES),
                            trusted_join,
                        ),
                    )
                else:
                    if row["cleanup_epoch"] > row["cleanup_done"]:
                        continue
                    rejoined = not row["present"] or row["team"] != entry["team"]
                    db.execute(
                        "UPDATE tenants SET name=?,team=?,present=1,epoch=epoch+?,"
                        "version=version+?,inviter=CASE WHEN ? THEN '' ELSE inviter END,"
                        "evidence=CASE WHEN ? THEN '' ELSE evidence END,"
                        "joined=CASE WHEN ? THEN ? ELSE joined END WHERE account=? AND group_id=?",
                        (
                            str(entry["name"])[:128],
                            entry["team"],
                            rejoined,
                            rejoined,
                            rejoined,
                            rejoined,
                            rejoined,
                            time.time(),
                            account,
                            group,
                        ),
                    )
                    if rejoined:
                        db.execute(
                            "UPDATE tenants SET auto_claim=?,admission='pending',"
                            "native_role='',leave_state='' WHERE account=? AND group_id=?",
                            (trusted_join and rejoined, account, group),
                        )
            for group, row in prior.items():
                if group not in catalog and row["present"]:
                    db.execute(
                        "UPDATE tenants SET present=0,epoch=epoch+1,version=version+1 WHERE account=? AND group_id=?",
                        (account, group),
                    )
                    self.log(db, "directory", account, group, "left")
        return catalog

    def prove_inviter(self, account, group, inviter, evidence, epoch, timestamp):
        """Bind signed invitation evidence to the current membership generation.

        Args:
            account: Authenticated bot business identity.
            group: Exact business group.
            inviter: Business identity in the signed invitation.
            evidence: Upstream application identifier.
            epoch: Current persisted membership generation.
            timestamp: Signed invitation application time, in seconds.

        Raises:
            OpenError: If evidence predates a confirmed departure or has changed.
        """
        if not inviter.isascii() or not inviter.isdigit() or not evidence:
            raise OpenError("invitation_invalid")
        with self.connect(write=True) as db:
            row = db.execute(
                "SELECT inviter,evidence,joined,team FROM tenants WHERE account=? AND group_id=? "
                "AND present=1 AND epoch=? AND auto_claim=1 "
                "AND admission NOT IN ('rejected','departed')",
                (account, group, epoch),
            ).fetchone()
            if not row or (row["inviter"] and row["inviter"] != inviter):
                raise OpenError("invitation_conflict")
            departure = db.execute(
                "SELECT MAX(event_at) FROM membership_notices WHERE account=? AND team=? "
                "AND kind IN ('kick','leave') AND state='departure_verified' AND event_at<=?",
                (account, row["team"], row["joined"]),
            ).fetchone()[0]
            # Application creation may precede its acceptance / join notification.
            # A corroborated departure is the generation fence, not receipt latency.
            if (
                type(timestamp) not in (int, float)
                or not 0 < timestamp <= time.time()
                or (
                    timestamp <= departure
                    if departure is not None
                    else timestamp < row["joined"] - 120
                )
            ):
                raise OpenError("inviter_unproven")
            db.execute(
                "UPDATE tenants SET inviter=?,evidence=? WHERE account=? AND group_id=?",
                (inviter, evidence, account, group),
            )

    def membership_notice(self, account, team, message, kind, event_at):
        """Persist already validated native self-membership metadata before ACK.

        Args:
            account: Authenticated business robot identity.
            team: Authenticated transport team identity.
            message: Upstream notice ID used for deduplication.
            kind: Validated self-join, departure or role transition.
            event_at: Upstream timestamp in seconds.

        Returns:
            Whether a new notice was committed.
        """
        if kind not in {"join", "kick", "leave", "role_lost", "role_gained"}:
            raise OpenError("membership_notice_invalid")
        with self.connect(write=True) as db:
            fresh = bool(
                db.execute(
                    "INSERT OR IGNORE INTO membership_notices(account,team,message,kind,event_at) "
                    "VALUES(?,?,?,?,?)",
                    (account, team, message, kind, event_at),
                ).rowcount
            )
            if fresh and kind in {"kick", "leave", "role_lost"}:
                db.execute(
                    "DELETE FROM capabilities WHERE account=? AND group_id IN "
                    "(SELECT group_id FROM tenants WHERE account=? AND team=? AND joined<=?)",
                    (account, account, team, event_at + 120),
                )
                db.execute(
                    "UPDATE tenants SET version=version+1 WHERE account=? AND team=? AND joined<=?",
                    (account, team, event_at + 120),
                )
            return fresh

    def membership_pending(self, account):
        """Read bounded durable notices without reissuing any external operation."""
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM membership_notices WHERE account=? AND state='pending' "
                    "ORDER BY event_at,message LIMIT 20",
                    (account,),
                )
            ]

    def membership_blocks(self):
        """Restore unresolved departure and role-loss fences before dispatch starts."""
        with self.connect() as db:
            return {
                (r["account"], r["group_id"])
                for r in db.execute(
                    "SELECT DISTINCT t.account,t.group_id FROM tenants t "
                    "JOIN membership_notices n ON n.account=t.account AND n.team=t.team "
                    "WHERE n.state='pending' AND n.kind IN ('kick','leave','role_lost') "
                    "AND n.event_at>=t.joined-120"
                )
            }

    def membership_finish(self, account, team, message, state):
        """Close one notice while retaining its upstream deduplication identity."""
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE membership_notices SET state=? WHERE account=? AND team=? AND message=?",
                (state, account, team, message),
            )

    def native_role(self, account, group, epoch, role):
        """Fence old actions on a verified role transition without deleting rules.

        Args:
            account: Authenticated robot identity.
            group: Exact business group.
            epoch: Current membership generation.
            role: Role from a complete signed roster or signed own-group directory.

        Returns:
            Whether a previously known role changed.
        """
        if role not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN", "GROUP_ROLE_MEMBER"}:
            raise OpenError("native_role_unproven")
        with self.connect(write=True) as db:
            row = db.execute(
                "SELECT native_role FROM tenants WHERE account=? AND group_id=? AND epoch=?",
                (account, group, epoch),
            ).fetchone()
            if not row:
                raise OpenError("membership_changed")
            changed = bool(row["native_role"] and row["native_role"] != role)
            if row["native_role"] != role:
                db.execute(
                    "UPDATE tenants SET native_role=?,version=version+? "
                    "WHERE account=? AND group_id=? AND epoch=?",
                    (role, changed, account, group, epoch),
                )
                if changed:
                    db.execute(
                        "DELETE FROM capabilities WHERE account=? AND group_id=?",
                        (account, group),
                    )
                    self.log(
                        db,
                        "native",
                        account,
                        group,
                        "native_role_changed",
                        {"role": role},
                    )
            return changed

    def admit_inviter(
        self, account, group, epoch, version, inviter, proof, allowed, *, guard=None
    ):
        """Commit a roster-verified inviter decision, reserving a rejected exit once.

        Args:
            account: Robot whose membership is being admitted.
            group: Exact business group.
            epoch: Membership generation checked after the final roster read.
            version: Configuration version checked after the final roster read.
            inviter: Real signed business inviter, never the notice sender.
            proof: Signed invitation record ID.
            allowed: Whether that exact inviter is a native owner or administrator.
            guard: Account and task lifecycle check.

        Returns:
            Whether the caller owns the one allowed external exit submission.
        """
        with self.connect(write=True, guard=guard) as db:
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if (
                not row
                or not row["present"]
                or row["epoch"] != epoch
                or row["version"] != version
                or row["inviter"] != inviter
                or row["evidence"] != proof
                or row["cleanup_epoch"] > row["cleanup_done"]
                or allowed
                and row["admission"] in {"rejected", "departed"}
            ):
                raise OpenError("membership_changed")
            if not allowed and row["leave_state"]:
                return False
            db.execute(
                "UPDATE tenants SET admission=?,leave_state=? WHERE account=? AND group_id=?",
                (
                    "verified" if allowed else "rejected",
                    "" if allowed else "unknown",
                    account,
                    group,
                ),
            )
            if not allowed:
                db.execute(
                    "DELETE FROM capabilities WHERE account=? AND group_id=?",
                    (account, group),
                )
            self.log(
                db,
                "native",
                account,
                group,
                "inviter_admitted" if allowed else "inviter_rejected",
            )
            return not allowed

    def leave_result(self, account, group, epoch, state):
        """Record an exit receipt without permitting an unknown result to retry."""
        if state not in {"accepted", "rejected", "unknown"}:
            raise OpenError("leave_result_invalid")
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE tenants SET leave_state=? WHERE account=? AND group_id=? "
                "AND epoch=? AND admission='rejected' AND leave_state='unknown'",
                (state, account, group, epoch),
            )
            self.log(
                db, "native", account, group, "automatic_leave_result", {"state": state}
            )

    def depart(self, account, group, epoch, evidence):
        """Reset working policy only after a corroborated native departure.

        Args:
            account: Authenticated robot identity.
            group: Exact group identity.
            epoch: Membership generation of the confirmed departure.
            evidence: Minimal native notice or successful exit receipt identity.

        Returns:
            Durable cleanup generation, retaining original managers and all finances.
        """
        with self.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=? AND epoch=?",
                (account, group, epoch),
            ).fetchone()
            if not row:
                raise OpenError("membership_changed")
            if row["admission"] == "departed":
                return row["cleanup_epoch"]
            new_epoch = epoch + 1
            db.execute(
                "UPDATE tenants SET present=0,active=0,epoch=?,version=version+1,"
                "admission='departed',inviter='',evidence='',auto_claim=0,native_role='',"
                "rules=?,cleanup_epoch=? WHERE account=? AND group_id=?",
                (new_epoch, json.dumps(DEFAULT_RULES), new_epoch, account, group),
            )
            db.execute(
                "DELETE FROM capabilities WHERE account=? AND group_id=?",
                (account, group),
            )
            for table in ("manager_quotas", "group_ai", "ai_profiles"):
                db.execute(
                    f"DELETE FROM {table} WHERE account=? AND group_id=?",
                    (account, group),
                )
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in (
                "business_rules",
                "business_rule_drafts",
                "business_ad_cursors",
                "business_rule_images",
                "business_ad_rotations",
            ):
                if table in tables:
                    db.execute(
                        f"DELETE FROM {table} WHERE account=? AND group_id=?",
                        (account, group),
                    )
            db.execute(
                "UPDATE audit SET detail='{}' WHERE account=? AND group_id=?",
                (account, group),
            )
            self.log(
                db,
                "native",
                account,
                group,
                "departure_confirmed",
                {"evidence": evidence, "epoch": new_epoch},
            )
            return new_epoch

    def cleanup_finished(self, account, group, epoch):
        """Complete an idempotent cleanup before rediscovery may restore admission."""
        with self.connect(write=True) as db:
            if (
                db.execute(
                    "UPDATE tenants SET cleanup_done=? WHERE account=? AND group_id=? "
                    "AND present=0 AND epoch=? AND cleanup_epoch=?",
                    (epoch, account, group, epoch, epoch),
                ).rowcount
                != 1
            ):
                raise OpenError("membership_changed")
            self.log(
                db, "native", account, group, "working_data_cleared", {"epoch": epoch}
            )

    def import_group(self, account, group, rules, managers, actor, *, guard=None):
        """Import an existing total-control group once without guessing its inviter.

        Args:
            account: Authenticated robot account.
            group: Previously enabled group with proven current membership.
            rules: Detached legacy literal rules.
            managers: Existing grants verified against the current roster.
            actor: Migration audit principal.
            guard: Account and service lifecycle check.
        """
        with self.connect(write=True, guard=guard) as db:
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            if not row or not row["present"]:
                raise OpenError("group_unavailable")
            imported = db.execute(
                "SELECT 1 FROM audit WHERE account=? AND group_id=? AND action='legacy_import'",
                (account, group),
            ).fetchone()
            if (
                row["active"]
                or imported
                or row["cleanup_epoch"]
                or row["admission"] == "rejected"
            ):
                return
            if (
                db.execute("SELECT COUNT(*) FROM tenants WHERE active=1").fetchone()[0]
                >= 20
            ):
                raise OpenError("tenant_limit")
            db.execute(
                "UPDATE tenants SET active=1,auto_claim=0,admission='legacy',rules=?,managers=?,version=version+1 "
                "WHERE account=? AND group_id=?",
                (json.dumps(rules), json.dumps(managers), account, group),
            )
            self.log(db, actor, account, group, "legacy_import", {"managers": managers})

    def mint(self, account, group, actor):
        """Create a one-use ten-minute group-bound activation capability."""
        token = secrets.token_urlsafe(24)
        with self.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=? AND present=1 AND active=0",
                (account, group),
            ).fetchone()
            if not row:
                raise OpenError("activation_unavailable")
            db.execute(
                "UPDATE capabilities SET used=1 WHERE kind='activation' AND account=? AND group_id=?",
                (account, group),
            )
            db.execute(
                "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    "activation",
                    account,
                    group,
                    row["epoch"],
                    row["version"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    time.time() + 600,
                ),
            )
            self.log(db, actor, account, group, "activation_code_created")
        return token

    def activate(
        self,
        account,
        group,
        actor,
        *,
        code="",
        epoch,
        version,
        guard=None,
        support_owner=None,
    ):
        """Atomically claim an unconfigured tenant after external membership checks."""
        with self.connect(write=True, guard=guard) as db:
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?", (account, group)
            ).fetchone()
            if (
                not row
                or not row["present"]
                or row["epoch"] != epoch
                or row["version"] != version
                or row["active"]
                or row["admission"] in {"rejected", "departed"}
                or row["cleanup_epoch"] > row["cleanup_done"]
                or db.execute("SELECT paused FROM settings").fetchone()[0]
                or db.execute(
                    "SELECT 1 FROM membership_notices WHERE account=? AND team=? "
                    "AND state='pending' AND kind IN ('kick','leave','role_lost') "
                    "AND event_at>=? LIMIT 1",
                    (account, row["team"], row["joined"] - 120),
                ).fetchone()
            ):
                raise OpenError("activation_changed")
            if (
                db.execute("SELECT COUNT(*) FROM tenants WHERE active=1").fetchone()[0]
                >= 20
            ):
                raise OpenError("tenant_limit")
            if code:
                capability = db.execute(
                    "SELECT * FROM capabilities WHERE digest=?",
                    (hashlib.sha256(code.encode()).hexdigest(),),
                ).fetchone()
                if (
                    not capability
                    or capability["kind"] != "activation"
                    or capability["used"]
                    or capability["expires"] <= time.time()
                    or (
                        capability["account"],
                        capability["group_id"],
                        capability["epoch"],
                        capability["version"],
                    )
                    != (account, group, epoch, version)
                ):
                    raise OpenError("activation_code_invalid")
                db.execute(
                    "UPDATE capabilities SET used=1 WHERE digest=?",
                    (capability["digest"],),
                )
            elif (
                not row["auto_claim"] or row["inviter"] != actor or not row["evidence"]
            ):
                raise OpenError("inviter_unproven")
            managers = json.loads(row["managers"])
            if managers and actor not in managers:
                raise OpenError("original_manager_required")
            rules = json.loads(row["rules"])
            use_template = rules == DEFAULT_RULES
            if use_template:
                rules = DEFAULT_GROUP_RULES
            db.execute(
                "UPDATE tenants SET active=1,admission='verified',managers=?,rules=?,"
                "version=version+1 WHERE account=? AND group_id=?",
                (json.dumps(managers or [actor]), json.dumps(rules), account, group),
            )
            if support_owner is not None:
                from .owner_support import TEMPLATE
                from .service_settings import SupportConfiguration

                peer, name = support_owner["peer"], support_owner["name"]
                if (
                    not actor.isascii()
                    or not actor.isdigit()
                    or not isinstance(peer, str)
                    or not 1 <= len(peer) <= 256
                    or not isinstance(name, str)
                    or not 1 <= len(name) <= 64
                    or any(ord(c) < 32 for c in name)
                ):
                    raise OpenError("support_owner_invalid")
                owner = db.execute(
                    "SELECT * FROM support_owners WHERE uid=?", (actor,)
                ).fetchone()
                conflict = db.execute(
                    "SELECT 1 FROM support_owners WHERE peer=? AND uid<>?",
                    (peer, actor),
                ).fetchone()
                if conflict or owner and owner["peer"] != peer:
                    raise OpenError("support_owner_identity_conflict")
                if not owner:
                    db.execute(
                        "INSERT INTO support_owners VALUES(?,?,?,1,1)",
                        (actor, peer, name),
                    )
                    template = db.execute(
                        "SELECT version,config FROM support_settings WHERE account=?",
                        (TEMPLATE,),
                    ).fetchone()
                    config = SupportConfiguration.model_validate(
                        json.loads(template["config"])
                        if template
                        else {
                            "wording": "您好，我是专属业务客服。只依据已确认的业务资料解答；"
                            "资料不足时请联系业务负责人，不承诺未确认事项。",
                        }
                    ).model_dump()
                    db.execute(
                        "INSERT INTO support_settings VALUES(?,1,?)",
                        ("owner:" + actor, json.dumps(config)),
                    )
                db.execute(
                    "INSERT INTO support_group_owners VALUES(?,?,?,?) "
                    "ON CONFLICT(account,group_id) DO UPDATE SET "
                    "epoch=excluded.epoch,uid=excluded.uid",
                    (account, group, epoch, actor),
                )
                self.log(
                    db,
                    actor,
                    account,
                    group,
                    "support_owner_bound",
                    {
                        "uid": actor,
                        "created": not bool(owner),
                        "template_version": (template["version"] if template else 0)
                        if not owner
                        else None,
                    },
                )
            self.log(
                db,
                actor,
                account,
                group,
                "activated",
                {
                    "template_version": DEFAULT_GROUP_TEMPLATE_VERSION
                    if use_template
                    else None,
                    "rules": rules,
                },
            )

    def change(
        self,
        account,
        group,
        version,
        actor,
        *,
        rules=None,
        managers=None,
        paused=None,
        control=False,
        guard=None,
        confirmation="",
    ):
        """Save one tenant with compare-and-swap and a final authorization check."""
        with self.connect(write=True, guard=guard) as db:
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?", (account, group)
            ).fetchone()
            if not row or row["version"] != version:
                raise OpenError("tenant_changed")
            old_managers = json.loads(row["managers"])
            if not control:
                self.management_guard(db, row, actor)
            rules = json.loads(row["rules"]) if rules is None else rules
            managers = old_managers if managers is None else managers
            from .custom_rules import validate

            try:
                validate(
                    rules.get("custom_rules", []) if isinstance(rules, dict) else None
                )
            except ValueError as exc:
                raise OpenError(str(exc)) from None
            if (
                "custom_rules" in json.loads(row["rules"])
                and isinstance(rules, dict)
                and "custom_rules" not in rules
            ):
                raise OpenError("tenant_rules_refresh")
            if (
                not isinstance(rules, dict)
                or set(rules)
                not in (
                    set(DEFAULT_RULES),
                    set(DEFAULT_RULES) | {"custom_rules"},
                )
                or type(rules["advertising"]) is not bool
                or type(rules["flood"]) is not bool
                or not isinstance(rules["keywords"], list)
                or len(rules["keywords"]) > 50
                or any(
                    not isinstance(w, str) or not w.strip() or len(w) > 100 or "\n" in w
                    for w in rules["keywords"]
                )
                or len(set(rules["keywords"])) != len(rules["keywords"])
                or not isinstance(managers, list)
                or not (
                    1 <= len(managers) <= 20
                    or control
                    and not old_managers
                    and managers == []
                )
                or len(set(managers)) != len(managers)
                or any(
                    not isinstance(u, str)
                    or not u.isascii()
                    or not u.isdigit()
                    or not 0 < int(u) < 1 << 30
                    or u == account
                    for u in managers
                )
                or (paused is not None and type(paused) is not bool)
            ):
                raise OpenError("tenant_invalid")
            if control and managers != old_managers:
                self.management_guard(db, row, actor, control=True)
                digest = hashlib.sha256(confirmation.encode()).hexdigest()
                capability = db.execute(
                    "SELECT * FROM capabilities WHERE digest=?", (digest,)
                ).fetchone()
                expected = json.dumps(
                    {"rules": rules, "managers": managers, "paused": paused},
                    sort_keys=True,
                )
                if (
                    not capability
                    or capability["kind"] != "control"
                    or capability["used"]
                    or capability["expires"] <= time.time()
                    or (
                        capability["account"],
                        capability["group_id"],
                        capability["actor"],
                        capability["version"],
                        capability["epoch"],
                        capability["target"],
                    )
                    != (account, group, actor, version, row["epoch"], expected)
                ):
                    raise OpenError("confirmation_invalid")
                db.execute("UPDATE capabilities SET used=1 WHERE digest=?", (digest,))
            db.execute(
                "UPDATE tenants SET rules=?,managers=?,paused=?,version=version+1 WHERE account=? AND group_id=?",
                (
                    json.dumps(rules),
                    json.dumps(managers),
                    row["paused"] if paused is None else paused,
                    account,
                    group,
                ),
            )
            self.log(
                db,
                actor,
                account,
                group,
                "tenant_changed",
                {
                    "version": version + 1,
                    "before": {
                        "rules": json.loads(row["rules"]),
                        "managers": old_managers,
                        "paused": bool(row["paused"]),
                    },
                    "after": {
                        "rules": rules,
                        "managers": managers,
                        "paused": bool(row["paused"] if paused is None else paused),
                    },
                },
            )

    def preview_control(
        self, account, group, version, actor, rules, managers, paused, *, guard=None
    ):
        """Bind a total-control manager change to exact values before confirmation."""
        token = secrets.token_urlsafe(24)
        with self.connect(write=True, guard=guard) as db:
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?", (account, group)
            ).fetchone()
            if (
                not row
                or not row["present"]
                or not row["active"]
                or row["version"] != version
            ):
                raise OpenError("tenant_changed")
            if managers != json.loads(row["managers"]):
                self.management_guard(db, row, actor, control=True)
            db.execute(
                "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    "control",
                    account,
                    group,
                    row["epoch"],
                    version,
                    actor,
                    "",
                    json.dumps(
                        {"rules": rules, "managers": managers, "paused": paused},
                        sort_keys=True,
                    ),
                    "",
                    "",
                    time.time() + 600,
                ),
            )
            self.log(db, actor, account, group, "manager_preview", {"version": version})
        return token

    def preview_manager(
        self,
        account,
        group,
        version,
        actor,
        session,
        message,
        target,
        operation,
        peer="",
        *,
        guard=None,
    ):
        """Persist an actor/session/revision-bound manager change preview."""
        if operation not in {"add", "remove"} or target == account:
            raise OpenError("manager_invalid")
        token = secrets.token_urlsafe(24)
        with self.connect(write=True, guard=guard) as db:
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?", (account, group)
            ).fetchone()
            if not row or row["version"] != version:
                raise OpenError("tenant_changed")
            self.management_guard(db, row, actor)
            db.execute(
                "INSERT INTO capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    "manager",
                    account,
                    group,
                    row["epoch"],
                    version,
                    actor,
                    session,
                    json.dumps({"uid": target, "peer": peer}),
                    operation,
                    message,
                    time.time() + 600,
                ),
            )
        return token

    def confirm_manager(
        self,
        token,
        actor,
        session,
        message,
        account,
        group,
        current_members,
        *,
        eligible_managers=None,
        guard=None,
    ):
        """Consume a preview and its manager update in the same transaction."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect(write=True, guard=guard) as db:
            cap = db.execute(
                "SELECT * FROM capabilities WHERE digest=?", (digest,)
            ).fetchone()
            row = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?", (account, group)
            ).fetchone()
            if (
                not cap
                or not row
                or cap["kind"] != "manager"
                or cap["used"]
                or cap["expires"] <= time.time()
                or cap["message"] == message
                or (
                    cap["actor"],
                    cap["session"],
                    cap["account"],
                    cap["group_id"],
                    cap["version"],
                    cap["epoch"],
                )
                != (actor, session, account, group, row["version"], row["epoch"])
            ):
                raise OpenError("confirmation_invalid")
            self.management_guard(db, row, actor)
            users = json.loads(row["managers"])
            target = json.loads(cap["target"])
            uid = target["uid"]
            if actor not in users or actor not in current_members:
                raise OpenError("permission_denied")
            if uid in current_members:
                if target["peer"] and current_members[uid] != target["peer"]:
                    raise OpenError("permission_denied")
            elif (
                cap["operation"] != "remove"
                or uid not in users
                or not target["peer"]
                or not db.execute(
                    "SELECT 1 FROM actor_bindings WHERE account=? AND uid=? "
                    "AND peer=? AND conflicted=0",
                    (account, uid, target["peer"]),
                ).fetchone()
            ):
                raise OpenError("permission_denied")
            if cap["operation"] == "add":
                if eligible_managers is None or uid not in eligible_managers:
                    raise OpenError("manager_native_role_required")
                if uid in users or len(users) >= 20:
                    raise OpenError("manager_limit_or_duplicate")
                users.append(uid)
            else:
                if uid not in users or len(users) == 1:
                    raise OpenError("last_manager")
                users.remove(uid)
            db.execute("UPDATE capabilities SET used=1 WHERE digest=?", (digest,))
            db.execute(
                "UPDATE tenants SET managers=?,version=version+1 WHERE account=? AND group_id=?",
                (json.dumps(users), account, group),
            )
            self.log(
                db,
                actor,
                account,
                group,
                "manager_" + cap["operation"],
                {"target": uid},
            )

    def management_guard(self, db, row, actor, *, control=False):
        """Check exact durable tenant authority while its writer lock is held.

        Args:
            db: Transaction owning the tenant write lock.
            row: Exact pre-mutation tenant row.
            actor: Current authenticated business manager.
            control: Whether the caller has independently verified total control.

        Raises:
            OpenError: If membership, native role or tenant authority is revoked.
        """
        if (
            not row["present"]
            or not row["active"]
            or not control
            and row["paused"]
            or row["admission"] not in {"legacy", "verified"}
            or row["cleanup_epoch"] > row["cleanup_done"]
            or row["native_role"] not in {"GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
            or not control
            and actor not in json.loads(row["managers"])
            or not control
            and db.execute("SELECT paused FROM settings").fetchone()[0]
            or db.execute(
                "SELECT 1 FROM membership_notices WHERE account=? AND team=? "
                "AND state='pending' AND kind IN ('kick','leave','role_lost') "
                "AND event_at>=? LIMIT 1",
                (row["account"], row["team"], row["joined"] - 120),
            ).fetchone()
        ):
            raise OpenError("permission_denied")

    def reserve(
        self, sender, request, *, exempt=False, now=None, account="", verified_uid=""
    ):
        """Reserve a billable call once; unknown outcomes remain counted on restart."""
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            limits = [
                row["daily_limit"]
                for row in db.execute(
                    "SELECT q.daily_limit,t.managers FROM manager_quotas q "
                    "JOIN tenants t USING(account,group_id) "
                    "WHERE q.account=? AND q.uid=? AND t.active=1 AND t.present=1 "
                    "AND t.paused=0 AND t.admission IN ('legacy','verified') "
                    "AND t.native_role IN ('GROUP_ROLE_ADMIN','GROUP_ROLE_OWNER') "
                    "AND t.cleanup_epoch<=t.cleanup_done "
                    "AND NOT EXISTS(SELECT 1 FROM membership_notices n "
                    "WHERE n.account=t.account AND n.team=t.team AND n.state='pending' "
                    "AND n.kind IN ('kick','leave','role_lost'))",
                    (account, verified_uid),
                )
                if verified_uid and verified_uid in json.loads(row["managers"])
            ]
            self._reserve(
                db, sender, request, exempt, now, personal_limit=max(limits, default=0)
            )

    @staticmethod
    def _reserve(db, sender, request, exempt, now, *, personal_limit=0):
        """Share one atomic public budget across private and group AI requests."""
        day = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date().isoformat()
        settings = db.execute("SELECT * FROM settings").fetchone()
        if settings["paused"]:
            raise OpenError("service_paused")
        if db.execute("SELECT 1 FROM calls WHERE request=?", (request,)).fetchone():
            raise OpenError("request_duplicate")
        recent = db.execute(
            "SELECT MAX(created) FROM calls WHERE sender=? AND state<>'not_sent'",
            (sender,),
        ).fetchone()[0]
        if recent is not None and now - recent < 5:
            raise OpenError("chat_cooldown")
        if not exempt:
            personal = db.execute(
                "SELECT COUNT(*) FROM calls WHERE day=? AND sender=? AND exempt=0 AND state<>'not_sent'",
                (day, sender),
            ).fetchone()[0]
            total = db.execute(
                "SELECT COUNT(*) FROM calls WHERE day=? AND exempt=0 AND state<>'not_sent'",
                (day,),
            ).fetchone()[0]
            limit = max(settings["personal_limit"], personal_limit)
            if personal >= limit:
                raise OpenError("personal_quota")
            if total >= settings["global_limit"]:
                raise OpenError("global_quota")
        db.execute(
            "INSERT INTO calls VALUES(?,?,?,?,?,'reserved')",
            (request, sender, day, exempt, now),
        )

    def finish(self, request, state):
        """Record outcomes without refunding calls that may have reached the provider."""
        if state not in {"completed", "unknown", "rejected", "not_sent"}:
            raise OpenError("call_state_invalid")
        with self.connect(write=True) as db:
            db.execute("UPDATE calls SET state=? WHERE request=?", (state, request))

    def audit(self):
        """Return bounded recent administrative events without any capabilities."""
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")
            ]

    def link_principal(self, uid, principal):
        """Preserve legacy daily usage when a verified sender adopts its transport key."""
        with self.connect(write=True) as db:
            conflict = db.execute(
                "SELECT 1 FROM quota_aliases WHERE (uid=? AND principal<>?) "
                "OR (principal=? AND uid<>?)",
                (uid, principal, principal, uid),
            ).fetchone()
            if conflict:
                raise OpenError("private_identity_unverified")
            db.execute(
                "INSERT OR IGNORE INTO quota_aliases VALUES(?,?)", (principal, uid)
            )
            db.execute("UPDATE calls SET sender=? WHERE sender=?", (principal, uid))

    def save_label(self, account, peer, nickname):
        """Save only the bot's global nickname from its verified own member record."""
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO account_labels VALUES(?,?,?,?) ON CONFLICT(account) "
                "DO UPDATE SET peer=excluded.peer,nickname=excluded.nickname,checked=excluded.checked",
                (account, peer, nickname[:128], time.time()),
            )

    def label(self, account, peer):
        """Read a persisted account label only for the current transport identity."""
        with self.connect() as db:
            row = db.execute(
                "SELECT nickname FROM account_labels WHERE account=? AND peer=?",
                (account, peer),
            ).fetchone()
            return row[0] if row else ""

    @staticmethod
    def model_binding(model):
        """Fingerprint an exact server-owned model credential without exposing it."""
        return hashlib.sha256(
            json.dumps(
                [model.get(k, "") for k in ("base_url", "model", "api_key")],
            ).encode()
        ).hexdigest()

    def prune(self, *, now=None):
        """Bound completed metadata retention without deleting uncertain operations."""
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            legacy_history = {
                r["name"] for r in db.execute("PRAGMA table_info(chat_history)")
            }
            if "updated" in legacy_history:
                # Compatibility cleanup only; all new AI history belongs to AstrBot.
                db.execute("DELETE FROM chat_history WHERE updated<=?", (now - 900,))
            elif legacy_history:
                db.execute("DELETE FROM chat_history")
            db.execute(
                "DELETE FROM calls WHERE created<? AND state IN ('completed','rejected','not_sent')",
                (now - 90 * 86400,),
            )
            db.execute("DELETE FROM capabilities WHERE expires<?", (now - 90 * 86400,))
            db.execute("DELETE FROM audit WHERE created<?", (now - 90 * 86400,))
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_operations'"
            ).fetchone():
                db.execute(
                    "DELETE FROM knowledge_operations WHERE created<?",
                    (now - 90 * 86400,),
                )
            db.execute(
                "DELETE FROM membership_notices WHERE event_at<? AND state<>'pending'",
                (now - 90 * 86400,),
            )
            db.execute(
                "DELETE FROM actor_bindings WHERE checked<? AND conflicted=0",
                (now - 7 * 86400,),
            )
