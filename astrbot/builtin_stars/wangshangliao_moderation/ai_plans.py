"""Durable AI subscriptions, consented shared pools and fixed-point cost admission."""

import hashlib
import json
import secrets
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .open_store import OpenError

MICRO = 1_000_000
MAX_BUDGET = 10_000_000 * MICRO
BEIJING = ZoneInfo("Asia/Shanghai")
PLANS = {
    "trial": {"name": "试用", "daily": 20, "usdt": 0},
    "plus": {"name": "Plus", "daily": 2000, "usdt": 20},
    "pro": {"name": "Pro", "daily": 5000, "usdt": 50},
}
QUOTA_ERRORS = {
    "personal_quota",
    "global_quota",
    "plan_budget",
    "trial_budget",
    "site_budget",
    "group_quota",
}
COMMANDS = {"我的套餐", "我的额度", "套餐说明", "本群额度", "确认本群扣费"}


def money(value):
    """Parse bounded decimal CNY into integer micro-CNY, without binary floats."""
    try:
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError
        amount = Decimal(value)
        if not amount.is_finite() or not 0 <= amount <= 10_000_000:
            raise ValueError
        scaled = amount * MICRO
        if scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError):
        raise OpenError("billing_money_invalid") from None


def dates(now):
    """Return Beijing day and calendar month for one immutable admission instant."""
    local = datetime.fromtimestamp(now, BEIJING)
    return local.date().isoformat(), local.strftime("%Y-%m")


class AIPlans:
    """Share one transactional accounting authority with public bot configuration."""

    def __init__(self, store):
        self.store = store
        with store.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ai_plan_settings(
                    id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,
                    contact_name TEXT NOT NULL,contact TEXT NOT NULL,
                    monthly_micro INTEGER NOT NULL,
                    trial_monthly_micro INTEGER NOT NULL DEFAULT 20000000);
                INSERT OR IGNORE INTO ai_plan_settings(
                    id,version,contact_name,contact,monthly_micro
                ) VALUES(1,1,'','',0);
                CREATE TABLE IF NOT EXISTS ai_pools(
                    pool TEXT PRIMARY KEY,uid TEXT NOT NULL,principal TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS ai_subscriptions(
                    id TEXT PRIMARY KEY,pool TEXT NOT NULL,tier TEXT NOT NULL,
                    starts REAL NOT NULL,ends REAL NOT NULL,received_micro INTEGER NOT NULL,
                    budget_micro INTEGER NOT NULL,created REAL NOT NULL,actor TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ai_subscriptions_pool ON ai_subscriptions(pool,starts,ends);
                CREATE TABLE IF NOT EXISTS ai_plan_previews(
                    digest TEXT PRIMARY KEY,actor TEXT NOT NULL,payload TEXT NOT NULL,
                    expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS ai_payers(
                    account TEXT,group_id TEXT,uid TEXT NOT NULL,principal TEXT NOT NULL,
                    version INTEGER NOT NULL,epoch INTEGER NOT NULL,confirmed INTEGER NOT NULL,
                    PRIMARY KEY(account,group_id));
                CREATE TABLE IF NOT EXISTS ai_prices(
                    binding TEXT PRIMARY KEY,provider TEXT NOT NULL,version INTEGER NOT NULL,
                    config TEXT NOT NULL,blocked TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS ai_charges(
                    request TEXT PRIMARY KEY,pool TEXT NOT NULL,period TEXT NOT NULL,
                    day TEXT NOT NULL,month TEXT NOT NULL,account TEXT NOT NULL,
                    group_id TEXT NOT NULL,uid TEXT NOT NULL,principal TEXT NOT NULL,
                    binding TEXT NOT NULL,price_version INTEGER NOT NULL,
                    reserved_micro INTEGER NOT NULL,charged_micro INTEGER NOT NULL,
                    state TEXT NOT NULL,created REAL NOT NULL,usage TEXT NOT NULL DEFAULT '',
                    budget_kind TEXT NOT NULL DEFAULT 'legacy');
                CREATE TABLE IF NOT EXISTS ai_continuations(
                    parent TEXT PRIMARY KEY,child TEXT UNIQUE NOT NULL);
                CREATE INDEX IF NOT EXISTS ai_charges_pool_day ON ai_charges(pool,day,state);
                CREATE INDEX IF NOT EXISTS ai_charges_period ON ai_charges(pool,period,state);
                CREATE INDEX IF NOT EXISTS ai_charges_month ON ai_charges(month,state);
                CREATE INDEX IF NOT EXISTS ai_charges_inflight_pool ON ai_charges(pool,state)
                    WHERE state IN ('reserved','dispatched');
                CREATE INDEX IF NOT EXISTS ai_charges_recent_pool ON ai_charges(pool,created DESC)
                    WHERE state<>'not_sent';
                CREATE INDEX IF NOT EXISTS ai_charges_day_pool ON ai_charges(day,pool)
                    WHERE state<>'not_sent';
                CREATE INDEX IF NOT EXISTS ai_charges_month_cost ON ai_charges(month,charged_micro)
                    WHERE state<>'not_sent';
                CREATE TABLE IF NOT EXISTS ai_quota_notices(
                    id TEXT PRIMARY KEY,pool TEXT NOT NULL,kind TEXT NOT NULL,
                    account TEXT NOT NULL,uid TEXT NOT NULL,principal TEXT NOT NULL,
                    text TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL);
                DROP INDEX IF EXISTS ai_notice_pool_kind;
                CREATE UNIQUE INDEX IF NOT EXISTS ai_notice_pool_recipient_kind
                    ON ai_quota_notices(pool,account,uid,principal,kind);
                CREATE TABLE IF NOT EXISTS ai_plan_migrations(name TEXT PRIMARY KEY);
            """)
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(ai_plan_settings)")
            }
            if "trial_monthly_micro" not in columns:
                db.execute(
                    "ALTER TABLE ai_plan_settings ADD COLUMN trial_monthly_micro "
                    "INTEGER NOT NULL DEFAULT 20000000"
                )
                db.execute("UPDATE ai_plan_settings SET version=version+1")
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(ai_charges)")
            }
            if "budget_kind" not in columns:
                db.execute(
                    "ALTER TABLE ai_charges ADD COLUMN budget_kind "
                    "TEXT NOT NULL DEFAULT 'legacy'"
                )
                db.execute(
                    "UPDATE ai_charges SET budget_kind=CASE "
                    "WHEN period='trial/'||month THEN 'trial' "
                    "WHEN EXISTS(SELECT 1 FROM ai_subscriptions s "
                    "WHERE s.id=ai_charges.period) THEN 'paid' ELSE 'legacy' END"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS ai_charges_month_kind_cost "
                "ON ai_charges(month,budget_kind,charged_micro) WHERE state<>'not_sent'"
            )
            if not db.execute(
                "SELECT 1 FROM ai_plan_migrations WHERE name='subscription-v1'"
            ).fetchone():
                db.execute(
                    "UPDATE settings SET global_limit=100000,version=version+1 "
                    "WHERE global_limit=1000"
                )
                db.execute("INSERT INTO ai_plan_migrations VALUES('subscription-v1')")
            db.execute("DROP INDEX IF EXISTS ai_notice_pool_kind")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ai_notice_pool_recipient_kind "
                "ON ai_quota_notices(pool,account,uid,principal,kind)"
            )
            # A reload cannot establish whether a prior process dispatched HTTP.
            db.execute(
                "UPDATE ai_charges SET state='unknown' WHERE state IN ('reserved','dispatched')"
            )
            db.execute(
                "UPDATE ai_quota_notices SET state='unknown' WHERE state='sending'"
            )

    def settings(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT * FROM ai_plan_settings").fetchone())

    def paid_uids(self):
        with self.store.connect() as db:
            return {r[0] for r in db.execute("SELECT uid FROM ai_pools WHERE uid<>''")}

    def configure(
        self,
        version,
        contact_name,
        contact,
        monthly_cny,
        actor,
        *,
        trial_monthly_cny=None,
    ):
        if (
            type(version) is not int
            or not isinstance(contact_name, str)
            or len(contact_name) > 80
            or not isinstance(contact, str)
            or len(contact) > 300
            or any(c in contact_name + contact for c in "\r\n\x00")
        ):
            raise OpenError("billing_settings_invalid")
        amount = money(monthly_cny)
        trial = money(trial_monthly_cny) if trial_monthly_cny is not None else None
        with self.store.connect(write=True) as db:
            if (
                db.execute(
                    "UPDATE ai_plan_settings SET version=version+1,contact_name=?,contact=?,"
                    "monthly_micro=?,trial_monthly_micro=COALESCE(?,trial_monthly_micro) "
                    "WHERE id=1 AND version=?",
                    (contact_name.strip(), contact.strip(), amount, trial, version),
                ).rowcount
                != 1
            ):
                raise OpenError("billing_changed")
            self.store.log(
                db,
                actor,
                "",
                "",
                "ai_plan_settings",
                {
                    "monthly_micro": amount,
                    "trial_monthly_micro": db.execute(
                        "SELECT trial_monthly_micro FROM ai_plan_settings"
                    ).fetchone()[0],
                },
            )

    @staticmethod
    def _site_budget(db, now, settings):
        """Read independent pools and the allocated monthly safety ceiling.

        Args:
            db: Connection owning the same accounting transaction.
            now: Immutable accounting timestamp, including for delayed receipts.
            settings: Settings read inside this transaction.

        Returns:
            Monthly spend and ceilings, including unresolved legacy holds.
        """
        local = datetime.fromtimestamp(now, BEIJING)
        starts = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        ends = starts.replace(
            year=starts.year + (starts.month == 12),
            month=1 if starts.month == 12 else starts.month + 1,
        )
        month = starts.strftime("%Y-%m")
        spend = db.execute(
            "SELECT COALESCE(SUM(charged_micro),0) AS total,"
            "COALESCE(SUM(CASE WHEN budget_kind='trial' THEN charged_micro ELSE 0 END),0) AS trial,"
            "COALESCE(SUM(CASE WHEN budget_kind='paid' THEN charged_micro ELSE 0 END),0) AS paid "
            "FROM ai_charges WHERE month=? AND state<>'not_sent'",
            (month,),
        ).fetchone()
        # Include ended/upgraded cycles: their receipts and unknown holds persist.
        paid = db.execute(
            "SELECT COALESCE(SUM(budget_micro),0) FROM ai_subscriptions "
            "WHERE starts<? AND ends>? AND ends>starts",
            (ends.timestamp(), starts.timestamp()),
        ).fetchone()[0]
        return {
            "month": month,
            "cost_micro": spend["total"],
            "trial_cost_micro": spend["trial"],
            "paid_cost_micro": spend["paid"],
            "legacy_cost_micro": spend["total"] - spend["trial"] - spend["paid"],
            "trial_limit_micro": settings["trial_monthly_micro"],
            "paid_budget_micro": paid,
            "base_limit_micro": settings["monthly_micro"],
            "limit_micro": settings["monthly_micro"] + paid
            if settings["monthly_micro"]
            else 0,
        }

    def configure_price(self, binding, provider, version, config, actor):
        required = {
            "input_per_million",
            "output_per_million",
            "multiplier",
            "credit_cny",
            "input_ceiling",
            "output_ceiling",
            "all_output_billed_in_limit",
            "evidence",
        }
        if (
            not isinstance(config, dict)
            or set(config) - {"valid_until"} != required
            or type(version) is not int
            or type(config["input_ceiling"]) is not int
            or not 1 <= config["input_ceiling"] <= 10_000_000
            or type(config["output_ceiling"]) is not int
            or not 1 <= config["output_ceiling"] <= 1_000_000
            or config["all_output_billed_in_limit"] is not True
            or not isinstance(config["evidence"], str)
            or not 1 <= len(config["evidence"].strip()) <= 500
            or "valid_until" in config
            and (
                type(config["valid_until"]) is not int
                or not time.time() < config["valid_until"] <= 4_102_444_800
            )
        ):
            raise OpenError("billing_price_invalid")
        for key in (
            "input_per_million",
            "output_per_million",
            "multiplier",
            "credit_cny",
        ):
            if money(config[key]) <= 0:
                raise OpenError("billing_price_invalid")
        with self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT version FROM ai_prices WHERE binding=?", (binding,)
            ).fetchone()
            if (old[0] if old else 0) != version:
                raise OpenError("billing_price_changed")
            db.execute(
                "INSERT INTO ai_prices VALUES(?,?,?,?,'') ON CONFLICT(binding) DO UPDATE SET "
                "provider=excluded.provider,version=excluded.version,config=excluded.config,blocked=''",
                (binding, provider, version + 1, json.dumps(config)),
            )
            self.store.log(
                db,
                actor,
                "",
                "",
                "ai_price_confirmed",
                {"provider": provider, "version": version + 1},
            )

    @staticmethod
    def cost(config, inputs, outputs):
        """Round the entire charge upward once, applying the vendor multiplier once."""
        numerator = (
            (
                money(config["input_per_million"]) * inputs
                + money(config["output_per_million"]) * outputs
            )
            * money(config["multiplier"])
            * money(config["credit_cny"])
        )
        denominator = MICRO**3
        return (numerator + denominator - 1) // denominator

    def quote(self, model, payload):
        """Reserve the documented full input ceiling, never a sample average estimate.

        The operator must attest that the configured ceilings bound all billable input
        and output, including reasoning. Without that contract the model stays closed.
        """
        binding = self.store.model_binding(model)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM ai_prices WHERE binding=?", (binding,)
            ).fetchone()
        if not row or row["blocked"]:
            raise OpenError("billing_price_missing")
        config = json.loads(row["config"])
        if config.get("valid_until") and time.time() >= config["valid_until"]:
            raise OpenError("billing_price_expired")
        outputs = payload.get("max_completion_tokens")
        if type(outputs) is not int or not 1 <= outputs <= config["output_ceiling"]:
            raise OpenError("billing_output_unbounded")
        return {
            "binding": binding,
            "price_version": row["version"],
            "config": config,
            "outputs": outputs,
            "reserved_micro": self.cost(config, config["input_ceiling"], outputs),
            "payload_digest": hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
        }

    def assert_quote(self, quote, payload=None):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT version,blocked,config FROM ai_prices WHERE binding=?",
                (quote["binding"],),
            ).fetchone()
            setting = db.execute(
                "SELECT monthly_micro FROM ai_plan_settings"
            ).fetchone()
        if (
            not row
            or row["blocked"]
            or row["version"] != quote["price_version"]
            or not setting[0]
        ):
            raise OpenError("billing_price_changed")
        valid_until = json.loads(row["config"]).get("valid_until")
        if valid_until and time.time() >= valid_until:
            raise OpenError("billing_price_expired")
        if (
            payload is not None
            and hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            != quote["payload_digest"]
        ):
            raise OpenError("billing_payload_changed")

    def _pool(self, db, principal, uid="", *, create=True):
        """Resolve a trusted pool, optionally creating or merging it atomically.

        Args:
            db: Connection owning the current read or write transaction.
            principal: Authenticated transport identity.
            uid: Independently verified business identity, if available.
            create: Whether this transaction may create or merge pool records.

        Returns:
            Current pool metadata, or None when a read requires a write first.

        Raises:
            OpenError: If a verified alias or pool belongs to another identity.
        """
        alias = db.execute(
            "SELECT uid FROM quota_aliases WHERE principal=?", (principal,)
        ).fetchone()
        if uid and alias and alias[0] != uid:
            raise OpenError("private_identity_unverified")
        uid = uid or (alias[0] if alias else "")
        pool = "uid/" + uid if uid else principal
        existing = db.execute("SELECT * FROM ai_pools WHERE pool=?", (pool,)).fetchone()
        if existing and existing["principal"] != principal:
            raise OpenError("private_identity_unverified")
        old = None
        if uid and pool != principal:
            old = db.execute(
                "SELECT 1 FROM ai_pools WHERE pool=?", (principal,)
            ).fetchone()
        if not create:
            return dict(existing) if existing and not old else None
        if not existing:
            db.execute(
                "INSERT INTO ai_pools(pool,uid,principal) VALUES(?,?,?)",
                (pool, uid, principal),
            )
        if uid:
            if old and pool != principal:
                db.execute(
                    "UPDATE ai_charges SET pool=? WHERE pool=?", (pool, principal)
                )
                db.execute(
                    "DELETE FROM ai_quota_notices WHERE pool=? AND kind IN "
                    "(SELECT kind FROM ai_quota_notices WHERE pool=?)",
                    (principal, pool),
                )
                db.execute(
                    "UPDATE ai_quota_notices SET pool=? WHERE pool=?", (pool, principal)
                )
                db.execute("DELETE FROM ai_pools WHERE pool=?", (principal,))
        return dict(
            db.execute("SELECT * FROM ai_pools WHERE pool=?", (pool,)).fetchone()
        )

    @staticmethod
    def _plan(db, pool, now):
        row = db.execute(
            "SELECT * FROM ai_subscriptions WHERE pool=? AND starts<=? AND ends>? "
            "ORDER BY starts DESC LIMIT 1",
            (pool, now, now),
        ).fetchone()
        if row:
            return dict(row)
        return {
            "id": "trial/" + dates(now)[1],
            "tier": "trial",
            "starts": 0,
            "ends": 0,
            "budget_micro": 3 * MICRO,
        }

    def _snapshot(self, db, pool, now):
        day, month = dates(now)
        plan = self._plan(db, pool["pool"], now)
        used = db.execute(
            "SELECT COUNT(*) FROM ai_charges WHERE pool=? AND day=? AND state<>'not_sent'",
            (pool["pool"], day),
        ).fetchone()[0]
        legacy = db.execute(
            "SELECT COUNT(*) FROM calls WHERE day=? AND sender IN (?,?) AND state<>'not_sent' "
            "AND NOT EXISTS(SELECT 1 FROM ai_charges a WHERE a.request=calls.request)",
            (day, pool["principal"], pool["uid"]),
        ).fetchone()[0]
        cost = db.execute(
            "SELECT COALESCE(SUM(charged_micro),0) FROM ai_charges WHERE pool=? AND period=? AND state<>'not_sent'",
            (pool["pool"], plan["id"]),
        ).fetchone()[0]
        site = self._site_budget(
            db, now, db.execute("SELECT * FROM ai_plan_settings").fetchone()
        )
        return {
            **pool,
            "tier": plan["tier"],
            "name": PLANS[plan["tier"]]["name"],
            "daily": PLANS[plan["tier"]]["daily"],
            "used": used + legacy,
            "remaining": max(0, PLANS[plan["tier"]]["daily"] - used - legacy),
            "period": plan["id"],
            "expires": plan["ends"],
            "cost_micro": cost,
            "budget_micro": plan["budget_micro"],
            "site_cost_micro": site["cost_micro"],
            "trial_site_cost_micro": site["trial_cost_micro"],
            "trial_site_budget_micro": site["trial_limit_micro"],
            "site_budget_micro": site["limit_micro"],
            "day": day,
            "month": month,
        }

    def snapshot(self, principal, uid="", *, now=None):
        """Read one consistent snapshot without taking a writer lock for known pools.

        Args:
            principal: Authenticated transport identity.
            uid: Independently verified business identity, if available.
            now: Optional immutable accounting timestamp.

        Returns:
            Identity, current plan and usage from one SQLite read snapshot.
        """
        now = time.time() if now is None else now
        while True:
            with self.store.connect() as db:
                db.execute("BEGIN")
                pool = self._pool(db, principal, uid, create=False)
                if pool is not None:
                    return self._snapshot(db, pool, now)
            # Never upgrade an old read snapshot to a writer, or hold the writer
            # lock while aggregating usage. Recheck aliases in both transactions.
            with self.store.connect(write=True) as db:
                self._pool(db, principal, uid)

    def preview(self, actor, payload, *, guard):
        """Freeze a paid period without applying a subscription.

        Args:
            actor: Authenticated total-control identity.
            payload: Server-verified manager, pool version, tier and actual income.
            guard: Admission and commit authority check.

        Returns:
            Exact period, cost ceiling and one-use confirmation.
        """
        if (
            not isinstance(payload["tier"], str)
            or payload["tier"] not in {"plus", "pro"}
            or type(payload["version"]) is not int
        ):
            raise OpenError("billing_grant_invalid")
        received = money(payload["received_cny"])
        if received <= 0:
            raise OpenError("billing_grant_invalid")
        now = time.time()
        with self.store.connect(write=True, guard=guard) as db:
            settings = db.execute("SELECT * FROM ai_plan_settings").fetchone()
            if not settings["contact_name"] or not settings["contact"]:
                raise OpenError("billing_contact_required")
            pool = self._pool(db, payload["principal"], payload["uid"])
            if pool["version"] != payload["version"]:
                raise OpenError("billing_changed")
            current = self._plan(db, pool["pool"], now)
            upgrade = PLANS[payload["tier"]]["daily"] > PLANS[current["tier"]]["daily"]
            starts = (
                now
                if upgrade
                else max(
                    now,
                    db.execute(
                        "SELECT COALESCE(MAX(ends),0) FROM ai_subscriptions WHERE pool=?",
                        (pool["pool"],),
                    ).fetchone()[0],
                )
            )
            confirmed = {
                **payload,
                "starts": starts,
                "ends": starts + 30 * 86400,
                "budget_micro": received * 70 // 100,
                "upgrade": upgrade,
                "settings_version": settings["version"],
                "deferred_periods": [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,starts,ends FROM ai_subscriptions WHERE pool=? AND starts>? "
                        "AND ends>starts ORDER BY starts,id",
                        (pool["pool"], now),
                    )
                ]
                if upgrade
                else [],
            }
            code = secrets.token_urlsafe(24)
            db.execute(
                "INSERT INTO ai_plan_previews VALUES(?,?,?,?,0)",
                (
                    hashlib.sha256(code.encode()).hexdigest(),
                    actor,
                    json.dumps(confirmed, sort_keys=True),
                    now + 600,
                ),
            )
            return {**confirmed, "confirmation": code}

    def grant(self, actor, payload, confirmation, *, guard):
        """Commit only an unchanged, unexpired preview owned by this controller.

        Args:
            actor: Authenticated total-control identity.
            payload: The same server-verified fields used for the preview.
            confirmation: One-use token whose digest is stored.
            guard: Admission and commit authority check.

        Raises:
            OpenError: If authority, pricing settings or the preview changed.
        """
        if not isinstance(confirmation, str) or len(confirmation) > 128:
            raise OpenError("billing_confirmation_invalid")
        digest = hashlib.sha256(confirmation.encode()).hexdigest()
        with self.store.connect(write=True, guard=guard) as db:
            preview = db.execute(
                "SELECT * FROM ai_plan_previews WHERE digest=?", (digest,)
            ).fetchone()
            if (
                not preview
                or preview["actor"] != actor
                or preview["used"]
                or preview["expires"] < time.time()
            ):
                raise OpenError("billing_confirmation_invalid")
            confirmed = json.loads(preview["payload"])
            if any(confirmed.get(k) != v for k, v in payload.items()):
                raise OpenError("billing_confirmation_invalid")
            setting = db.execute("SELECT * FROM ai_plan_settings").fetchone()
            pool = self._pool(db, payload["principal"], payload["uid"])
            if (
                pool["version"] != payload["version"]
                or setting["version"] != confirmed["settings_version"]
            ):
                raise OpenError("billing_changed")
            if confirmed["upgrade"]:
                db.execute(
                    "UPDATE ai_subscriptions SET ends=MIN(ends,?) WHERE pool=? AND ends>?",
                    (confirmed["starts"], pool["pool"], confirmed["starts"]),
                )
                # Preserve prepaid future renewals after the new immediate period.
                begins = confirmed["ends"]
                for deferred in confirmed["deferred_periods"]:
                    ends = begins + deferred["ends"] - deferred["starts"]
                    db.execute(
                        "UPDATE ai_subscriptions SET starts=?,ends=? WHERE id=? AND pool=?",
                        (begins, ends, deferred["id"], pool["pool"]),
                    )
                    begins = ends
            db.execute(
                "INSERT INTO ai_subscriptions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    digest,
                    pool["pool"],
                    payload["tier"],
                    confirmed["starts"],
                    confirmed["ends"],
                    money(payload["received_cny"]),
                    confirmed["budget_micro"],
                    time.time(),
                    actor,
                ),
            )
            db.execute(
                "UPDATE ai_pools SET version=version+1 WHERE pool=?", (pool["pool"],)
            )
            db.execute("UPDATE ai_plan_previews SET used=1 WHERE digest=?", (digest,))
            self.store.log(
                db,
                actor,
                payload["account"],
                payload["group_id"],
                "ai_plan_granted",
                {
                    "tier": payload["tier"],
                    "uid": payload["uid"],
                    "budget_micro": confirmed["budget_micro"],
                },
            )

    def payer(self, account, group):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM ai_payers WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            return dict(row) if row else None

    def confirm_stamp(self, account, group, principal, uid):
        """Invalidate menu confirmations when the payer, plan or budget changes."""
        with self.store.connect() as db:
            alias = db.execute(
                "SELECT uid FROM quota_aliases WHERE principal=?", (principal,)
            ).fetchone()
            pool = "uid/" + (uid or alias[0]) if uid or alias else principal
            row = db.execute(
                "SELECT version FROM ai_pools WHERE pool=?", (pool,)
            ).fetchone()
            payer = db.execute(
                "SELECT version FROM ai_payers WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            return (
                row[0] if row else 0,
                payer[0] if payer else 0,
                self._plan(db, pool, time.time())["id"],
                db.execute("SELECT version FROM ai_plan_settings").fetchone()[0],
            )

    def bind(
        self,
        account,
        group,
        uid,
        principal,
        version,
        tenant_version,
        pool_version,
        actor,
        *,
        guard,
        control=False,
    ):
        """Bind only exact current authority; control nominations never imply consent."""
        with self.store.connect(write=True, guard=guard) as db:
            tenant = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?", (account, group)
            ).fetchone()
            if not tenant or tenant["version"] != tenant_version:
                raise OpenError("tenant_changed")
            self.store.management_guard(db, tenant, actor, control=control)
            if uid not in json.loads(tenant["managers"]):
                raise OpenError("manager_required")
            old = db.execute(
                "SELECT * FROM ai_payers WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            pool = self._pool(db, principal, uid)
            if (
                pool["version"] != pool_version
                or (old["version"] if old else 0) != version
            ):
                raise OpenError("billing_changed")
            if not control and (
                actor != uid
                or (old and old["uid"] != uid)
                or (
                    not old
                    and uid
                    != (
                        tenant["inviter"]
                        if tenant["admission"] == "verified"
                        else (json.loads(tenant["managers"]) or [""])[0]
                    )
                )
            ):
                raise OpenError("billing_payer_control_required")
            db.execute(
                "INSERT INTO ai_payers VALUES(?,?,?,?,?,?,?) ON CONFLICT(account,group_id) "
                "DO UPDATE SET uid=excluded.uid,principal=excluded.principal,version=excluded.version,"
                "epoch=excluded.epoch,confirmed=excluded.confirmed",
                (
                    account,
                    group,
                    uid,
                    principal,
                    version + 1,
                    tenant["epoch"],
                    int(not control),
                ),
            )
            self.store.log(
                db,
                actor,
                account,
                group,
                "ai_payer_nominated" if control else "ai_payer_consented",
                {"uid": uid},
            )

    def group_pool(self, account, group, roster=None):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM ai_payers WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()
            tenant = db.execute(
                "SELECT * FROM tenants WHERE account=? AND group_id=?", (account, group)
            ).fetchone()
            if (
                not row
                or not tenant
                or not row["confirmed"]
                or row["epoch"] != tenant["epoch"]
            ):
                raise OpenError("billing_payer_required")
            self.store.management_guard(db, tenant, row["uid"])
            if roster is not None and str(
                roster.get(row["uid"], {}).get("nimId", "")
            ) != row["principal"].removeprefix("nim/"):
                raise OpenError("billing_payer_required")
            return dict(row)

    def reserve(
        self,
        principal,
        uid,
        request,
        quote,
        account,
        group="",
        *,
        exempt=False,
        guard=None,
        payer_version=None,
        group_version=None,
        continuation=None,
        guidance_trial=None,
        now=None,
    ):
        now = time.time() if now is None else now
        with self.store.connect(write=True, guard=guard) as db:
            settings = db.execute("SELECT * FROM settings").fetchone()
            billing = db.execute("SELECT * FROM ai_plan_settings").fetchone()
            price = db.execute(
                "SELECT version,blocked,config FROM ai_prices WHERE binding=?",
                (quote["binding"],),
            ).fetchone()
            if settings["paused"]:
                raise OpenError("service_paused")
            if (
                not billing["monthly_micro"]
                or not price
                or price["blocked"]
                or price["version"] != quote["price_version"]
            ):
                raise OpenError("billing_price_missing")
            valid_until = json.loads(price["config"]).get("valid_until")
            if valid_until and time.time() >= valid_until:
                raise OpenError("billing_price_expired")
            if db.execute("SELECT 1 FROM calls WHERE request=?", (request,)).fetchone():
                raise OpenError("request_duplicate")
            if guidance_trial is not None:
                trial = db.execute(
                    "SELECT * FROM guidance_trials WHERE account=? AND group_id=? AND uid=?",
                    (account, guidance_trial["group_id"], uid),
                ).fetchone()
                if (
                    group
                    or continuation is not None
                    or not trial
                    or dict(trial) != guidance_trial
                    or not trial["active"]
                    or trial["principal"] != principal
                ):
                    raise OpenError("ai_access_changed")
                used = db.execute(
                    "SELECT COUNT(*) FROM ai_charges WHERE account=? AND uid=? "
                    "AND principal=? AND group_id='' AND created>=? AND state<>'not_sent'",
                    (account, uid, principal, trial["created"]),
                ).fetchone()[0]
                if used >= trial["quota"]:
                    raise OpenError("guidance_trial_exhausted")
            pool = self._pool(db, principal, uid)
            if continuation is not None:
                parent = db.execute(
                    "SELECT * FROM ai_charges WHERE request=?", (continuation,)
                ).fetchone()
                if (
                    not group
                    or not isinstance(continuation, str)
                    or request != continuation + "/tool-answer"
                    or not parent
                    or parent["state"] != "settled"
                    or not 0 <= now - parent["created"] < 45
                    or any(
                        parent[k] != v
                        for k, v in {
                            "account": account,
                            "group_id": group,
                            "pool": pool["pool"],
                            "uid": uid,
                            "principal": principal,
                            "binding": quote["binding"],
                        }.items()
                    )
                    or db.execute(
                        "SELECT 1 FROM ai_continuations WHERE parent=?", (continuation,)
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM ai_continuations WHERE child=?", (continuation,)
                    ).fetchone()
                ):
                    raise OpenError("billing_continuation_invalid")
            group_limit = 0
            if group:
                payer = db.execute(
                    "SELECT * FROM ai_payers WHERE account=? AND group_id=?",
                    (account, group),
                ).fetchone()
                tenant = db.execute(
                    "SELECT * FROM tenants WHERE account=? AND group_id=?",
                    (account, group),
                ).fetchone()
                if (
                    not payer
                    or not payer["confirmed"]
                    or payer["version"] != payer_version
                    or payer["uid"] != uid
                    or payer["principal"] != principal
                    or not tenant
                    or payer["epoch"] != tenant["epoch"]
                ):
                    raise OpenError("billing_payer_required")
                self.store.management_guard(db, tenant, uid)
                ai = db.execute(
                    "SELECT * FROM group_ai WHERE account=? AND group_id=?",
                    (account, group),
                ).fetchone()
                if (
                    not ai
                    or ai["version"] != group_version
                    or ai["blocked"]
                    or not json.loads(ai["config"])["enabled"]
                ):
                    raise OpenError("group_ai_changed")
                if continuation is None and now - ai["last_attempt"] < 60:
                    raise OpenError("chat_cooldown")
                group_limit = json.loads(ai["config"])["daily_limit"]
            if db.execute(
                "SELECT 1 FROM ai_charges WHERE pool=? AND state IN ('reserved','dispatched') LIMIT 1",
                (pool["pool"],),
            ).fetchone():
                raise OpenError("chat_cooldown")
            recent = db.execute(
                "SELECT created FROM ai_charges WHERE pool=? AND state<>'not_sent' "
                "ORDER BY created DESC LIMIT 1",
                (pool["pool"],),
            ).fetchone()
            if continuation is None and recent is not None and now - recent[0] < 5:
                raise OpenError("chat_cooldown")
            state = self._snapshot(db, pool, now)
            if state["tier"] != "trial" and (
                not uid
                or not any(
                    uid in json.loads(row["managers"])
                    for row in db.execute(
                        "SELECT managers FROM tenants WHERE active=1 AND present=1 "
                        "AND admission IN ('legacy','verified')"
                    )
                )
            ):
                raise OpenError("manager_required")
            if (
                group_limit
                and db.execute(
                    "SELECT COUNT(*) FROM calls WHERE sender=? AND day=? AND state<>'not_sent'",
                    (f"group/{account}/{group}", state["day"]),
                ).fetchone()[0]
                >= group_limit
            ):
                raise OpenError("group_quota")
            if not exempt and state["used"] >= state["daily"]:
                raise OpenError("personal_quota")
            if (
                not exempt
                and db.execute(
                    "SELECT COUNT(*) FROM calls WHERE day=? AND exempt=0 AND state<>'not_sent'",
                    (state["day"],),
                ).fetchone()[0]
                >= settings["global_limit"]
            ):
                raise OpenError("global_quota")
            if state["cost_micro"] + quote["reserved_micro"] > state["budget_micro"]:
                raise OpenError("plan_budget")
            if (
                state["tier"] == "trial"
                and state["trial_site_cost_micro"] + quote["reserved_micro"]
                > state["trial_site_budget_micro"]
            ):
                raise OpenError("trial_budget")
            if (
                state["site_cost_micro"] + quote["reserved_micro"]
                > state["site_budget_micro"]
            ):
                raise OpenError("site_budget")
            db.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,'reserved')",
                (
                    request,
                    f"group/{account}/{group}" if group else principal,
                    state["day"],
                    int(exempt),
                    now,
                ),
            )
            db.execute(
                "INSERT INTO ai_charges(request,pool,period,day,month,account,group_id,uid,principal,binding,"
                "price_version,reserved_micro,charged_micro,state,created,budget_kind) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)",
                (
                    request,
                    pool["pool"],
                    state["period"],
                    state["day"],
                    state["month"],
                    account,
                    group,
                    uid,
                    principal,
                    quote["binding"],
                    quote["price_version"],
                    quote["reserved_micro"],
                    quote["reserved_micro"],
                    now,
                    "trial" if state["tier"] == "trial" else "paid",
                ),
            )
            if group:
                db.execute(
                    "UPDATE group_ai SET last_attempt=? WHERE account=? AND group_id=?",
                    (now, account, group),
                )
            if continuation is not None:
                db.execute(
                    "INSERT INTO ai_continuations VALUES(?,?)", (continuation, request)
                )

    def dispatched(self, request, quote, payload, *, guard=None):
        """Recheck the frozen price and authority at the dispatch transaction.

        Args:
            request: Durable idempotency key.
            quote: Reserved model binding, version and maximum cost.
            payload: Complete immutable model request.
            guard: Optional admission and commit authority check.

        Raises:
            OpenError: If configuration or the reservation no longer matches.
        """
        self.assert_quote(quote, payload)
        with self.store.connect(write=True, guard=guard) as db:
            current = db.execute(
                "SELECT version,blocked,config FROM ai_prices WHERE binding=?",
                (quote["binding"],),
            ).fetchone()
            if (
                not current
                or current["blocked"]
                or current["version"] != quote["price_version"]
            ):
                raise OpenError("billing_price_changed")
            valid_until = json.loads(current["config"]).get("valid_until")
            if valid_until and time.time() >= valid_until:
                raise OpenError("billing_price_expired")
            reservation = db.execute(
                "SELECT * FROM ai_charges WHERE request=?", (request,)
            ).fetchone()
            if not reservation or reservation["state"] != "reserved":
                raise OpenError("billing_call_changed")
            settings = db.execute("SELECT * FROM ai_plan_settings").fetchone()
            site = self._site_budget(db, reservation["created"], settings)
            if (
                reservation["budget_kind"] == "trial"
                and site["trial_cost_micro"] > site["trial_limit_micro"]
            ):
                raise OpenError("trial_budget")
            if not site["limit_micro"] or site["cost_micro"] > site["limit_micro"]:
                raise OpenError("site_budget")
            if (
                db.execute(
                    "UPDATE ai_charges SET state='dispatched' WHERE request=? AND state='reserved'",
                    (request,),
                ).rowcount
                != 1
            ):
                raise OpenError("billing_call_changed")

    def settle(self, request, quote, usage):
        """Settle verified tokens, retaining and auditing uncertain cost.

        Args:
            request: Durable idempotency key.
            quote: Price and bounds used for this reservation.
            usage: Supplier token receipt, not an estimated average.

        Raises:
            OpenError: If usage is missing or violates the confirmed bounds.
        """
        """Retain unknown holds; pause a model when accounting cannot be trusted."""
        valid = isinstance(usage, dict)
        inputs = usage.get("prompt_tokens") if valid else None
        outputs = usage.get("completion_tokens") if valid else None
        valid = (
            type(inputs) is int
            and inputs >= 0
            and type(outputs) is int
            and outputs >= 0
            and (
                "total_tokens" not in usage
                or (
                    type(usage["total_tokens"]) is int
                    and usage["total_tokens"] == inputs + outputs
                )
            )
        )
        amount = self.cost(quote["config"], inputs, outputs) if valid else None
        valid = (
            valid
            and inputs <= quote["config"]["input_ceiling"]
            and outputs <= quote["outputs"]
        )
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM ai_charges WHERE request=?", (request,)
            ).fetchone()
            if not row or row["state"] not in {"dispatched", "unknown"}:
                raise OpenError("billing_call_changed")
            if not valid or amount > row["reserved_micro"]:
                db.execute(
                    "UPDATE ai_charges SET state='unknown',charged_micro=MAX(charged_micro,?),usage=? WHERE request=?",
                    (
                        min(amount or 0, MAX_BUDGET + 1),
                        json.dumps(
                            {
                                "reported_cost_micro": str(amount)
                                if amount is not None
                                else "",
                                "usage": usage,
                            }
                        )[:4096],
                        request,
                    ),
                )
                db.execute(
                    "UPDATE ai_prices SET blocked='usage_unverified' WHERE binding=?",
                    (quote["binding"],),
                )
                self.store.log(
                    db,
                    "system",
                    row["account"],
                    row["group_id"],
                    "ai_billing_anomaly",
                    {
                        "request": request,
                        "reported_cost_micro": str(amount)
                        if amount is not None
                        else "",
                        "cost_hold_saturated": bool(amount and amount > MAX_BUDGET),
                    },
                )
            else:
                db.execute(
                    "UPDATE ai_charges SET state='settled',charged_micro=?,usage=? WHERE request=?",
                    (amount, json.dumps(usage)[:4096], request),
                )
        if not valid or amount > row["reserved_micro"]:
            raise OpenError("billing_usage_unverified")

    def finish(self, request, state):
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT state FROM ai_charges WHERE request=?", (request,)
            ).fetchone()
            if row and row[0] in {"settled", "unknown"} and state == "not_sent":
                state = "unknown"
            db.execute("UPDATE calls SET state=? WHERE request=?", (state, request))
            if not row:
                return
            if state == "not_sent" and row[0] in {"reserved", "dispatched"}:
                db.execute(
                    "UPDATE ai_charges SET state='not_sent',charged_micro=0 WHERE request=?",
                    (request,),
                )
            elif row[0] in {"reserved", "dispatched"}:
                db.execute(
                    "UPDATE ai_charges SET state='unknown' WHERE request=?", (request,)
                )

    def queue_notices(
        self, principal, uid, account, *, reason="", group="", exempt=False, now=None
    ):
        now = time.time() if now is None else now
        with self.store.connect(write=True) as db:
            state = self._snapshot(db, self._pool(db, principal, uid), now)
            settings = db.execute("SELECT * FROM ai_plan_settings").fetchone()
            contact = (
                f"{settings['contact_name']} {settings['contact']}".strip()
                or "机器人服务管理员"
            )
            notices = []
            if not exempt and (
                state["used"] >= state["daily"] or reason == "personal_quota"
            ):
                notices.append(
                    ("daily/" + state["day"], "今日AI次数已用完，明天零点恢复。")
                )
            if reason == "group_quota":
                notices.append(
                    (
                        f"group/{account}/{group}/{state['day']}",
                        "本群AI今日附加次数已用完，明天零点恢复；其他群及私聊仍按共享余额计算。",
                    )
                )
            if reason in {"plan_budget", "trial_budget", "site_budget", "global_quota"}:
                notices.append(
                    (
                        reason
                        + "/"
                        + (
                            state["period"]
                            if reason == "plan_budget"
                            else state["month"]
                        ),
                        "全站免费试用预算不足，免费AI暂时暂停；付费套餐不占此预算。"
                        if reason == "trial_budget"
                        else "全站AI安全预算不足，AI暂时暂停，请联系服务管理员核对。"
                        if reason == "site_budget"
                        else "本周期AI费用预算或服务额度不足，AI已暂停。",
                    )
                )
            for percent in (80, 100):
                if (
                    state["tier"] == "trial"
                    and state["trial_site_cost_micro"]
                    and state["trial_site_cost_micro"] * 100
                    >= state["trial_site_budget_micro"] * percent
                ):
                    notices.append(
                        (
                            f"trial-site/{state['month']}/{percent}",
                            f"全站免费试用费用预算已达到{percent}%，付费套餐独立计算。",
                        )
                    )
                if state["cost_micro"] * 100 >= state["budget_micro"] * percent:
                    notices.append(
                        (
                            f"cost/{state['period']}/{percent}",
                            f"套餐AI费用预算已达到{percent}%。",
                        )
                    )
            for kind, text in notices:
                key = hashlib.sha256(
                    json.dumps(
                        [state["pool"], account, uid, principal, kind],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO ai_quota_notices VALUES(?,?,?,?,?,?,?,'pending',?)",
                    (
                        key,
                        state["pool"],
                        kind,
                        account,
                        uid,
                        principal,
                        f"【AI套餐提醒】{state['name']}\n{text}\n固定命令不受影响。升级或核对请联系：{contact}",
                        now,
                    ),
                )
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM ai_quota_notices WHERE pool=? AND account=? "
                    "AND uid=? AND principal=? AND state='pending' "
                    "ORDER BY created LIMIT 4",
                    (state["pool"], account, uid, principal),
                )
            ]

    def claim_notice(self, key):
        with self.store.connect(write=True) as db:
            return (
                db.execute(
                    "UPDATE ai_quota_notices SET state='sending' WHERE id=? AND state='pending'",
                    (key,),
                ).rowcount
                == 1
            )

    def finish_notice(self, key, state):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE ai_quota_notices SET state=? WHERE id=? AND state='sending'",
                (state, key),
            )

    def prune(self, *, now=None):
        """Remove expired completed metadata without releasing unknown money holds.

        Args:
            now: Optional maintenance timestamp, in Unix seconds.
        """
        cutoff = (time.time() if now is None else now) - 90 * 86400
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM ai_plan_previews WHERE expires<?", (cutoff,))
            db.execute(
                "DELETE FROM ai_charges WHERE created<? AND state IN ('settled','not_sent') "
                "AND NOT EXISTS(SELECT 1 FROM guidance_trials t WHERE "
                "t.account=ai_charges.account AND t.uid=ai_charges.uid "
                "AND t.principal=ai_charges.principal AND ai_charges.group_id='' "
                "AND ai_charges.created>=t.created AND ai_charges.state<>'not_sent')",
                (cutoff,),
            )
            db.execute(
                "DELETE FROM ai_quota_notices WHERE created<? AND state IN ('accepted','failed','rejected')",
                (cutoff,),
            )

    def describe(self):
        settings = self.settings()
        contact = (
            f"{settings['contact_name']} {settings['contact']}".strip()
            or "机器人服务管理员"
        )
        return (
            "【AI套餐说明】\n试用：每天20次，默认长期；每人每月费用上限3元。\n"
            "Plus：20 USDT／30天，每天2000次。\nPro：50 USDT／30天，每天5000次。\n"
            "私聊与本人确认承担的群AI共享次数，不是每群单独发放。\n"
            "次数为上限，同时受成本预算保护；付费费用预算按实收金额70%计算。\n"
            f"免费试用全站每月共{settings['trial_monthly_micro'] / MICRO:g}元，"
            "付费套餐不占免费池；所有调用仍受全站安全预算保护。\n"
            "北京时间零点恢复每日次数，未用不累计；付费到期恢复试用。\n"
            "升级请联系：" + contact
        )

    def render(self, state, *, exempt=False):
        expiry = (
            datetime.fromtimestamp(state["expires"], BEIJING).strftime("%Y-%m-%d %H:%M")
            if state["expires"]
            else "默认长期"
        )
        return (
            f"当前套餐：{state['name']}\n今日已用：{state['used']}次\n"
            + (
                "每日次数：免限次（费用预算仍有效）\n"
                if exempt
                else f"每日共享：{state['daily']}次；剩余：{state['remaining']}次\n"
            )
            + f"到期时间：{expiry}（北京时间）\n"
            + self.describe()
        )

    def status(self):
        """Aggregate the existing control-plane lists in one consistent read.

        Returns:
            Existing status fields with batched pool usage and one site cost sum.
        """
        with self.store.connect() as db:
            db.execute("BEGIN")
            now = time.time()
            day, month = dates(now)
            settings = dict(db.execute("SELECT * FROM ai_plan_settings").fetchone())
            site = self._site_budget(db, now, settings)
            # Preserve the current list and exact active-period selection. Each
            # correlated lookup is indexed; usage is aggregated in batches.
            pool_query = (
                "SELECT p.*,s.id AS plan_id,s.tier AS plan_tier,s.ends AS plan_ends,"
                "s.budget_micro AS plan_budget FROM "
                "(SELECT * FROM ai_pools ORDER BY pool LIMIT 1000) p "
                "LEFT JOIN ai_subscriptions s ON s.id=("
                "SELECT id FROM ai_subscriptions WHERE pool=p.pool AND starts<=? AND ends>? "
                "ORDER BY starts DESC LIMIT 1)"
            )
            rows = db.execute(pool_query + " ORDER BY p.pool", (now, now)).fetchall()
            daily = {
                r["pool"]: r["used"]
                for r in db.execute(
                    "SELECT pool,COUNT(*) AS used FROM ai_charges "
                    "WHERE day=? AND state<>'not_sent' GROUP BY pool",
                    (day,),
                )
            }
            legacy = {
                r["pool"]: r["used"]
                for r in db.execute(
                    "SELECT p.pool,COUNT(*) AS used FROM "
                    "(SELECT * FROM ai_pools ORDER BY pool LIMIT 1000) p "
                    "JOIN calls c ON c.sender IN (p.principal,p.uid) "
                    "WHERE c.day=? AND c.state<>'not_sent' "
                    "AND NOT EXISTS(SELECT 1 FROM ai_charges a WHERE a.request=c.request) "
                    "GROUP BY p.pool",
                    (day,),
                )
            }
            costs = {
                r["pool"]: r["cost"]
                for r in db.execute(
                    "SELECT p.pool,COALESCE(SUM(a.charged_micro),0) AS cost FROM ("
                    + pool_query
                    + ") p LEFT JOIN ai_charges a ON a.pool=p.pool "
                    "AND a.period=COALESCE(p.plan_id,?) AND a.state<>'not_sent' "
                    "GROUP BY p.pool",
                    (now, now, "trial/" + month),
                )
            }
            pools = []
            for row in rows:
                pool = dict(row)
                period = pool.pop("plan_id")
                plan_tier = pool.pop("plan_tier")
                tier = plan_tier if period else "trial"
                expiry = pool.pop("plan_ends")
                budget = pool.pop("plan_budget")
                used = daily.get(pool["pool"], 0) + legacy.get(pool["pool"], 0)
                pools.append(
                    {
                        **pool,
                        "tier": tier,
                        "name": PLANS[tier]["name"],
                        "daily": PLANS[tier]["daily"],
                        "used": used,
                        "remaining": max(0, PLANS[tier]["daily"] - used),
                        "period": period or "trial/" + month,
                        "expires": expiry if period else 0,
                        "cost_micro": costs[pool["pool"]],
                        "budget_micro": budget if period else 3 * MICRO,
                        "site_cost_micro": site["cost_micro"],
                        "trial_site_cost_micro": site["trial_cost_micro"],
                        "trial_site_budget_micro": site["trial_limit_micro"],
                        "site_budget_micro": site["limit_micro"],
                        "day": day,
                        "month": month,
                    }
                )
            return {
                "settings": settings,
                "site": site,
                "plans": PLANS,
                "pools": pools,
                "subscriptions": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM ai_subscriptions ORDER BY created DESC LIMIT 200"
                    )
                ],
                "payers": [dict(r) for r in db.execute("SELECT * FROM ai_payers")],
                "prices": [
                    {**dict(r), "config": json.loads(r["config"])}
                    for r in db.execute("SELECT * FROM ai_prices")
                ],
                "notices": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM ai_quota_notices ORDER BY created DESC LIMIT 100"
                    )
                ],
                "unknown": [
                    dict(r)
                    for r in db.execute(
                        "SELECT request,account,group_id,charged_micro,created,usage FROM ai_charges WHERE state='unknown' ORDER BY created DESC LIMIT 100"
                    )
                ],
            }
