"""Operator-managed subscriptions and transactional, customer-wide AI budgets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from .storage import SessionError

NAMESPACE = "__commerce__"
PERIOD_SECONDS = 30 * 86400
NANO_PER_POINT = 4_000_000
NANO_PER_MICROPOINT = 4
PLANS = {
    "standard": {
        "name": "普通版",
        "price_u": "49",
        "robots": 1,
        "groups": 10,
        "points": 0,
    },
    "ai": {
        "name": "AI 版",
        "price_u": "99",
        "robots": 1,
        "groups": 10,
        "points": 20000,
    },
}
TOPUPS = {
    "5000": {"points": 5000, "price_u": "10"},
    "10000": {"points": 10000, "price_u": "20"},
}
INTERNAL_INSTANCES = {
    "lajiaoliao_test_20261007": "9680209",
    "lajiaoliao_0fa92beedaad4eec": "3129316",
}


def amount_nano(value):
    """Convert a nonnegative currency amount without binary floating point.

    Args:
        value: Decimal-compatible amount, excluding floats.

    Returns:
        Integer nanoyuan, rounded upward.

    Raises:
        ValueError: If the amount is invalid.
    """
    if isinstance(value, (float, bool)):
        raise ValueError("invalid_commerce_amount")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= Decimal("1000000"):
            raise ValueError("invalid_commerce_amount")
        return int((number * 10**9).to_integral_value(rounding=ROUND_CEILING))
    except InvalidOperation:
        raise ValueError("invalid_commerce_amount") from None


def cost_nano(rate, ordinary, cached, output):
    """Price one measured request using a frozen rate card.

    Args:
        rate: Private, verified CNY-per-million-token rate snapshot.
        ordinary: Noncached input count.
        cached: Cached input count.
        output: Total billable output, including thinking.

    Returns:
        Integer nanoyuan, rounded upward once for the whole request.
    """
    total = sum(
        Decimal(str(rate[key])) * count
        for key, count in (
            ("input_cny_per_million", ordinary),
            ("cached_input_cny_per_million", cached),
            ("output_cny_per_million", output),
        )
    ) / Decimal(10**6)
    return amount_nano(total)


class CommerceStore:
    """One private ledger per plugin installation, not per account."""

    def __init__(self, root: Path):
        self.path = root / "commerce.sqlite3"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS commerce_meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS commerce_customers(
                    id TEXT PRIMARY KEY,label TEXT,version INTEGER,state TEXT,ai_blocked TEXT,
                    created REAL);
                CREATE TABLE IF NOT EXISTS commerce_bindings(
                    instance_id TEXT PRIMARY KEY,session_ref TEXT UNIQUE,owner TEXT,
                    customer_id TEXT,internal INTEGER,groups_json TEXT,resume_after REAL);
                CREATE TABLE IF NOT EXISTS commerce_periods(
                    id TEXT PRIMARY KEY,customer_id TEXT,plan TEXT,start REAL,end REAL,
                    total_nano INTEGER,total_micro INTEGER,used_nano INTEGER DEFAULT 0,
                    used_micro INTEGER DEFAULT 0,held_nano INTEGER DEFAULT 0,
                    held_micro INTEGER DEFAULT 0,rate_snapshot TEXT DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS commerce_period_customer ON commerce_periods(customer_id,start);
                CREATE TABLE IF NOT EXISTS commerce_orders(
                    id TEXT PRIMARY KEY,customer_id TEXT,period_id TEXT,kind TEXT,
                    product TEXT,price_u TEXT,receipt_ref TEXT UNIQUE,stamp REAL,actor TEXT);
                CREATE TABLE IF NOT EXISTS commerce_operations(
                    request_id TEXT PRIMARY KEY,digest TEXT,actor TEXT,result TEXT,stamp REAL);
                CREATE TABLE IF NOT EXISTS commerce_rates(
                    id TEXT PRIMARY KEY,provider_id TEXT,model TEXT,version INTEGER,
                    fingerprint TEXT,value TEXT,stamp REAL);
                CREATE INDEX IF NOT EXISTS commerce_rate_model ON commerce_rates(provider_id,model,version);
                CREATE TABLE IF NOT EXISTS commerce_calls(
                    id TEXT PRIMARY KEY,customer_id TEXT,period_id TEXT,instance_id TEXT,
                    logical_id TEXT,sequence INTEGER,purpose TEXT,state TEXT,stamp REAL,
                    held_nano INTEGER,held_micro INTEGER,cost_nano INTEGER,charged_micro INTEGER,
                    input_bound INTEGER,output_bound INTEGER,rate TEXT,usage TEXT,receipt TEXT,
                    UNIQUE(instance_id,logical_id,sequence));
                CREATE INDEX IF NOT EXISTS commerce_call_customer ON commerce_calls(customer_id,stamp);
                CREATE TABLE IF NOT EXISTS commerce_call_events(
                    id INTEGER PRIMARY KEY,call_id TEXT,stamp REAL,state TEXT,detail TEXT);
                CREATE TABLE IF NOT EXISTS commerce_alerts(
                    customer_id TEXT,period_id TEXT,kind TEXT,instance_id TEXT,stamp REAL,state TEXT,
                    PRIMARY KEY(customer_id,period_id,kind));
                CREATE TABLE IF NOT EXISTS commerce_audit(
                    id INTEGER PRIMARY KEY,customer_id TEXT,stamp REAL,actor TEXT,
                    kind TEXT,reference TEXT,detail TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(self.path) + suffix)
                if path.exists():
                    path.chmod(0o600)
            with db:
                yield db
        finally:
            db.close()

    def bootstrap(self, operator, bindings):
        """Initialize an explicit operator and only the two approved internal bots."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO commerce_meta VALUES('operators',?)",
                (json.dumps([operator] if operator else []),),
            )
            if db.execute(
                "SELECT 1 FROM commerce_meta WHERE key='internal_bootstrap'"
            ).fetchone():
                return
            for row in bindings:
                if (
                    INTERNAL_INSTANCES.get(row["instance_id"]) == row["account_id"]
                    and row["state"] == "bound"
                ):
                    db.execute(
                        "INSERT OR IGNORE INTO commerce_bindings VALUES(?,?,?,?,?,?,?)",
                        (
                            row["instance_id"],
                            row["session_ref"],
                            row["owner"],
                            None,
                            1,
                            "[]",
                            0,
                        ),
                    )
            db.execute("INSERT INTO commerce_meta VALUES('internal_bootstrap','1')")

    def snapshot(self):
        with self.connect() as db:
            return {
                "operators": json.loads(
                    (
                        db.execute(
                            "SELECT value FROM commerce_meta WHERE key='operators'"
                        ).fetchone()
                        or {"value": "[]"}
                    )["value"]
                ),
                "customers": {
                    r["id"]: dict(r)
                    for r in db.execute("SELECT * FROM commerce_customers")
                },
                "bindings": {
                    r["instance_id"]: dict(r)
                    for r in db.execute("SELECT * FROM commerce_bindings")
                },
                "periods": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM commerce_periods WHERE end>?", (time.time(),)
                    )
                ],
            }

    def _period(self, db, customer, now):
        row = db.execute(
            "SELECT * FROM commerce_periods WHERE customer_id=? AND start<=? AND end>? ORDER BY start DESC LIMIT 1",
            (customer, now, now),
        ).fetchone()
        if row is None:
            raise SessionError("commerce_subscription_inactive")
        return row

    def execute(self, route, body, actor, *, now=None):
        """Apply one idempotent, versioned operator mutation.

        Args:
            route: Fixed commercial API name.
            body: Validated operator input, never a chat tool payload.
            actor: Authenticated allowlisted Dashboard username.
            now: Optional test clock.

        Returns:
            The committed result or the same previously committed result.

        Raises:
            SessionError: If ownership, versions, receipts or entitlements conflict.
            ValueError: If an input is invalid.
        """
        now = time.time() if now is None else now
        request_id = body.get("request_id")
        if (
            not isinstance(request_id, str)
            or not 8 <= len(request_id) <= 128
            or body.get("confirm") is not True
            or type(body.get("version")) is not int
        ):
            raise ValueError("commerce_confirmation_required")
        digest = hashlib.sha256(
            json.dumps([route, body], sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            operators = db.execute(
                "SELECT value FROM commerce_meta WHERE key='operators'"
            ).fetchone()
            if not operators or actor not in json.loads(operators["value"]):
                raise SessionError("commerce_operator_required")
            previous = db.execute(
                "SELECT * FROM commerce_operations WHERE request_id=?", (request_id,)
            ).fetchone()
            if previous:
                if previous["actor"] != actor or previous["digest"] != digest:
                    raise SessionError("commerce_idempotency_conflict")
                return json.loads(previous["result"])
            customer = body.get("customer_id", "")
            row = db.execute(
                "SELECT * FROM commerce_customers WHERE id=?", (customer,)
            ).fetchone()
            action = body.get("action", "")
            reference = request_id
            if route == "commerce-customers" and action == "create":
                label = body.get("label", "")
                if (
                    not isinstance(label, str)
                    or not 1 <= len(label.strip()) <= 128
                    or any(ord(c) < 32 for c in label)
                    or body["version"] != 0
                ):
                    raise ValueError("invalid_commerce_customer")
                customer = secrets.token_hex(16)
                db.execute(
                    "INSERT INTO commerce_customers VALUES(?,?,1,'active','',?)",
                    (customer, label.strip(), now),
                )
                result = {"customer_id": customer, "version": 1}
            elif route == "commerce-rates":
                result = self._rate_operation(db, body, now)
                customer = ""
                reference = result["rate_id"]
            else:
                if row is None:
                    raise SessionError("commerce_customer_not_found")
                if body["version"] != row["version"]:
                    raise SessionError("commerce_changed_refresh_required")
                if route == "commerce-customers":
                    if action in {"pause", "resume"}:
                        db.execute(
                            "UPDATE commerce_customers SET state=? WHERE id=?",
                            ("paused" if action == "pause" else "active", customer),
                        )
                        if action == "resume":
                            db.execute(
                                "UPDATE commerce_bindings SET resume_after=? WHERE customer_id=?",
                                (now, customer),
                            )
                    elif action == "bind":
                        instance, ref = body.get("instance_id"), body.get("session_ref")
                        if not isinstance(instance, str) or not isinstance(ref, str):
                            raise ValueError("invalid_commerce_binding")
                        existing = db.execute(
                            "SELECT * FROM commerce_bindings WHERE instance_id=? OR session_ref=?",
                            (instance, ref),
                        ).fetchone()
                        if existing:
                            raise SessionError("commerce_instance_already_assigned")
                        count = db.execute(
                            "SELECT COUNT(*) FROM commerce_bindings WHERE customer_id=?",
                            (customer,),
                        ).fetchone()[0]
                        if count >= PLANS["standard"]["robots"]:
                            raise SessionError("commerce_robot_limit")
                        groups = body.get("groups", [])
                        if not isinstance(groups, list) or any(
                            not isinstance(g, str) for g in groups
                        ):
                            raise ValueError("invalid_commerce_groups")
                        if len(set(groups)) > PLANS["standard"]["groups"]:
                            raise SessionError("commerce_group_limit")
                        db.execute(
                            "INSERT INTO commerce_bindings VALUES(?,?,?,?,0,?,?)",
                            (
                                instance,
                                ref,
                                actor,
                                customer,
                                json.dumps(sorted(set(groups))),
                                now,
                            ),
                        )
                    else:
                        raise ValueError("invalid_commerce_action")
                    result = {"customer_id": customer, "version": row["version"] + 1}
                elif route == "commerce-subscriptions":
                    plan = body.get("plan")
                    if action not in {"activate", "renew"} or plan not in PLANS:
                        raise ValueError("invalid_commerce_plan")
                    last = db.execute(
                        "SELECT * FROM commerce_periods WHERE customer_id=? ORDER BY end DESC LIMIT 1",
                        (customer,),
                    ).fetchone()
                    if action == "activate" and last is not None:
                        raise SessionError("commerce_use_renewal")
                    if last and last["end"] > now and last["plan"] != plan:
                        raise SessionError("commerce_midperiod_plan_change")
                    start = max(now, last["end"] if last else now)
                    period = secrets.token_hex(16)
                    points = PLANS[plan]["points"]
                    rates = {
                        json.dumps([r["provider_id"], r["model"], r["fingerprint"]]): {
                            **json.loads(r["value"]),
                            "rate_id": r["id"],
                            "version": r["version"],
                            "nano_per_micropoint": NANO_PER_MICROPOINT,
                            "valuation_version": "cny-0.004-v1",
                        }
                        for r in db.execute(
                            "SELECT r.* FROM commerce_rates r WHERE version=(SELECT MAX(version) FROM commerce_rates WHERE provider_id=r.provider_id AND model=r.model)"
                        )
                        if json.loads(r["value"])["verified"]
                    }
                    db.execute(
                        "INSERT INTO commerce_periods(id,customer_id,plan,start,end,total_nano,total_micro,rate_snapshot) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            period,
                            customer,
                            plan,
                            start,
                            start + PERIOD_SECONDS,
                            points * NANO_PER_POINT,
                            points * 10**6,
                            json.dumps(rates),
                        ),
                    )
                    self._order(
                        db,
                        customer,
                        period,
                        "subscription",
                        plan,
                        PLANS[plan]["price_u"],
                        body,
                        actor,
                        now,
                    )
                    if not last or last["end"] <= now:
                        db.execute(
                            "UPDATE commerce_bindings SET resume_after=? WHERE customer_id=?",
                            (now, customer),
                        )
                    result = {
                        "customer_id": customer,
                        "period_id": period,
                        "start": start,
                        "end": start + PERIOD_SECONDS,
                        "version": row["version"] + 1,
                    }
                elif route == "commerce-topups":
                    product = body.get("product")
                    if action != "add" or product not in TOPUPS:
                        raise ValueError("invalid_commerce_topup")
                    period = self._period(db, customer, now)
                    if period["plan"] != "ai" or row["state"] != "active":
                        raise SessionError("commerce_ai_plan_required")
                    points = TOPUPS[product]["points"]
                    self._order(
                        db,
                        customer,
                        period["id"],
                        "topup",
                        product,
                        TOPUPS[product]["price_u"],
                        body,
                        actor,
                        now,
                    )
                    db.execute(
                        "UPDATE commerce_periods SET total_nano=total_nano+?,total_micro=total_micro+? WHERE id=?",
                        (points * NANO_PER_POINT, points * 10**6, period["id"]),
                    )
                    result = {
                        "customer_id": customer,
                        "period_id": period["id"],
                        "version": row["version"] + 1,
                    }
                elif route == "commerce-reconcile":
                    call = db.execute(
                        "SELECT * FROM commerce_calls WHERE id=? AND customer_id=?",
                        (body.get("call_id"), customer),
                    ).fetchone()
                    evidence = body.get("evidence_ref", "")
                    if (
                        action not in {"settle", "release"}
                        or call is None
                        or call["state"] not in {"unknown", "overrun"}
                        or not isinstance(evidence, str)
                        or not 1 <= len(evidence.strip()) <= 256
                        or any(ord(c) < 32 for c in evidence)
                    ):
                        raise ValueError("invalid_commerce_reconciliation")
                    actual = (
                        amount_nano(body.get("actual_cny", "0"))
                        if action == "settle"
                        else 0
                    )
                    self._finish(
                        db, call, actual, "reconciled", {"evidence_ref": evidence}
                    )
                    if not db.execute(
                        "SELECT 1 FROM commerce_calls WHERE customer_id=? AND state IN ('unknown','overrun') LIMIT 1",
                        (customer,),
                    ).fetchone():
                        db.execute(
                            "UPDATE commerce_customers SET ai_blocked='' WHERE id=?",
                            (customer,),
                        )
                    result = {
                        "customer_id": customer,
                        "call_id": call["id"],
                        "version": row["version"] + 1,
                    }
                else:
                    raise ValueError("invalid_commerce_action")
                db.execute(
                    "UPDATE commerce_customers SET version=version+1 WHERE id=?",
                    (customer,),
                )
            db.execute(
                "INSERT INTO commerce_audit(customer_id,stamp,actor,kind,reference,detail) VALUES(?,?,?,?,?,?)",
                (
                    customer,
                    now,
                    actor,
                    route + ":" + action,
                    reference,
                    json.dumps(result),
                ),
            )
            db.execute(
                "INSERT INTO commerce_operations VALUES(?,?,?,?,?)",
                (request_id, digest, actor, json.dumps(result), now),
            )
            return result

    def _order(self, db, customer, period, kind, product, price, body, actor, now):
        receipt = body.get("receipt_ref")
        if (
            not isinstance(receipt, str)
            or not 1 <= len(receipt) <= 128
            or receipt != receipt.strip()
            or any(ord(c) < 32 for c in receipt)
            or body.get("paid_confirmed") is not True
        ):
            raise ValueError("commerce_payment_confirmation_required")
        if db.execute(
            "SELECT 1 FROM commerce_orders WHERE receipt_ref=?", (receipt,)
        ).fetchone():
            raise SessionError("commerce_receipt_already_used")
        db.execute(
            "INSERT INTO commerce_orders VALUES(?,?,?,?,?,?,?,?,?)",
            (
                secrets.token_hex(16),
                customer,
                period,
                kind,
                product,
                price,
                receipt,
                now,
                actor,
            ),
        )

    def _rate_operation(self, db, body, now):
        provider, model = body.get("provider_id"), body.get("model")
        if (
            body.get("action") != "save"
            or not isinstance(provider, str)
            or not 1 <= len(provider) <= 256
            or not isinstance(model, str)
            or not 1 <= len(model) <= 256
        ):
            raise ValueError("invalid_commerce_rate")
        old = db.execute(
            "SELECT version FROM commerce_rates WHERE provider_id=? AND model=? ORDER BY version DESC LIMIT 1",
            (provider, model),
        ).fetchone()
        version = old["version"] if old else 0
        if body["version"] != version:
            raise SessionError("commerce_changed_refresh_required")
        value = dict(body["rate"]) if isinstance(body.get("rate"), dict) else None
        fields = {
            "input_cny_per_million",
            "cached_input_cny_per_million",
            "output_cny_per_million",
            "verified",
            "evidence_ref",
            "input_count_verified",
            "output_bound_verified",
            "usage_verified",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("invalid_commerce_rate")
        for key in (
            "input_cny_per_million",
            "cached_input_cny_per_million",
            "output_cny_per_million",
        ):
            amount_nano(value[key])
            value[key] = str(Decimal(str(value[key])))
        if Decimal(value["cached_input_cny_per_million"]) > Decimal(
            value["input_cny_per_million"]
        ):
            raise ValueError("invalid_commerce_rate")
        for key in (
            "verified",
            "input_count_verified",
            "output_bound_verified",
            "usage_verified",
        ):
            if type(value[key]) is not bool:
                raise ValueError("invalid_commerce_rate")
        if (
            not isinstance(value["evidence_ref"], str)
            or len(value["evidence_ref"]) > 256
            or any(ord(c) < 32 for c in value["evidence_ref"])
        ):
            raise ValueError("invalid_commerce_rate")
        if value["verified"] and not (
            value["evidence_ref"].strip()
            and value["input_count_verified"]
            and value["output_bound_verified"]
            and value["usage_verified"]
        ):
            raise ValueError("commerce_rate_evidence_required")
        fingerprint = body.get("fingerprint", "")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("invalid_commerce_rate_source")
        identifier = secrets.token_hex(16)
        db.execute(
            "INSERT INTO commerce_rates VALUES(?,?,?,?,?,?,?)",
            (
                identifier,
                provider,
                model,
                version + 1,
                fingerprint,
                json.dumps(value),
                now,
            ),
        )
        return {"rate_id": identifier, "version": version + 1}

    def reserve(
        self,
        instance,
        logical_id,
        sequence,
        purpose,
        provider,
        model,
        fingerprint,
        input_bound,
        output_bound,
        *,
        now=None,
    ):
        """Reserve both money and points immediately before one native request."""
        now = time.time() if now is None else now
        if (
            type(sequence) is not int
            or not 1 <= sequence <= 6
            or type(input_bound) is not int
            or not 0 <= input_bound <= 32768
            or type(output_bound) is not int
            or not 1 <= output_bound <= 4096
        ):
            raise SessionError("commerce_request_limit")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute(
                "SELECT * FROM commerce_bindings WHERE instance_id=?", (instance,)
            ).fetchone()
            if not binding or binding["internal"]:
                raise SessionError("commerce_binding_required")
            customer = db.execute(
                "SELECT * FROM commerce_customers WHERE id=?", (binding["customer_id"],)
            ).fetchone()
            if customer["state"] != "active" or customer["ai_blocked"]:
                raise SessionError("commerce_ai_paused")
            period = self._period(db, customer["id"], now)
            if period["plan"] != "ai":
                raise SessionError("commerce_ai_plan_required")
            if db.execute(
                "SELECT 1 FROM commerce_calls WHERE instance_id=? AND logical_id=? AND sequence=?",
                (instance, logical_id, sequence),
            ).fetchone():
                raise SessionError("commerce_duplicate_call")
            snapshots = json.loads(period["rate_snapshot"])
            key = json.dumps([provider, model, fingerprint])
            rate = snapshots.get(key)
            if rate is None:
                raise SessionError("commerce_rate_unverified")
            rate = {
                "nano_per_micropoint": 4,
                "valuation_version": "cny-0.004-v1",
                **rate,
            }
            # A later revocation stops requests, but never reprices past receipts.
            latest = db.execute(
                "SELECT value FROM commerce_rates WHERE provider_id=? AND model=? AND fingerprint=? ORDER BY version DESC LIMIT 1",
                (provider, model, fingerprint),
            ).fetchone()
            if latest is None or not json.loads(latest["value"])["verified"]:
                raise SessionError("commerce_rate_unverified")
            held = cost_nano(rate, input_bound, 0, output_bound)
            unit = rate["nano_per_micropoint"]
            micro = (held + unit - 1) // unit
            if (
                period["total_nano"] - period["used_nano"] - period["held_nano"] < held
                or period["total_micro"] - period["used_micro"] - period["held_micro"]
                < micro
            ):
                self._alert(
                    db, customer["id"], period["id"], "ai_budget_low", instance, now
                )
                # Persist the alert even when there is no available final slot.
                db.commit()
                raise SessionError("commerce_ai_budget_exhausted")
            identifier = secrets.token_hex(16)
            db.execute(
                "INSERT INTO commerce_calls(id,customer_id,period_id,instance_id,logical_id,sequence,purpose,state,stamp,held_nano,held_micro,input_bound,output_bound,rate) VALUES(?,?,?,?,?,?,?,'reserved',?,?,?,?,?,?)",
                (
                    identifier,
                    customer["id"],
                    period["id"],
                    instance,
                    logical_id,
                    sequence,
                    purpose,
                    now,
                    held,
                    micro,
                    input_bound,
                    output_bound,
                    json.dumps(rate),
                ),
            )
            db.execute(
                "UPDATE commerce_periods SET held_nano=held_nano+?,held_micro=held_micro+? WHERE id=?",
                (held, micro, period["id"]),
            )
            self._call_event(
                db,
                identifier,
                "reserved",
                {
                    "held_nano": held,
                    "held_micro": micro,
                    "input_bound": input_bound,
                    "output_bound": output_bound,
                    "rate_id": rate["rate_id"],
                },
                now,
            )
            return {
                "call_id": identifier,
                "rate": rate,
                "held_nano": held,
                "input_bound": input_bound,
                "output_bound": output_bound,
            }

    def dispatch(self, identifier, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            call = db.execute(
                "SELECT * FROM commerce_calls WHERE id=?", (identifier,)
            ).fetchone()
            if call is None or call["state"] != "reserved":
                raise SessionError("commerce_call_state_conflict")
            customer = db.execute(
                "SELECT * FROM commerce_customers WHERE id=?", (call["customer_id"],)
            ).fetchone()
            period = self._period(db, call["customer_id"], now)
            if (
                period["id"] != call["period_id"]
                or customer["state"] != "active"
                or customer["ai_blocked"]
            ):
                raise SessionError("commerce_ai_paused")
            db.execute(
                "UPDATE commerce_calls SET state='dispatched' WHERE id=?", (identifier,)
            )
            self._call_event(db, identifier, "dispatched", {}, now)

    def release(self, identifier, *, unsent=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            call = db.execute(
                "SELECT * FROM commerce_calls WHERE id=?", (identifier,)
            ).fetchone()
            if call and (
                call["state"] == "reserved"
                or (unsent and call["state"] == "dispatched")
            ):
                self._finish(db, call, 0, "released", {})

    def settle(self, identifier, usage, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            call = db.execute(
                "SELECT * FROM commerce_calls WHERE id=?", (identifier,)
            ).fetchone()
            if not call or call["state"] != "dispatched":
                raise SessionError("commerce_call_state_conflict")
            valid = (
                isinstance(usage, dict)
                and set(usage)
                in (
                    {"ordinary", "cached", "candidates", "thinking"},
                    {"ordinary", "cached", "candidates", "thinking", "tool_input"},
                )
                and all(type(v) is int and v >= 0 for v in usage.values())
            )
            if not valid:
                self._unknown(db, call, "unknown", now)
                return {"state": "unknown"}
            output = usage["candidates"] + usage["thinking"]
            rate = json.loads(call["rate"])
            cost = cost_nano(rate, usage["ordinary"], usage["cached"], output)
            if (
                usage["ordinary"] + usage["cached"] > call["input_bound"]
                or output > call["output_bound"]
                or cost > call["held_nano"]
            ):
                db.execute(
                    "UPDATE commerce_calls SET usage=?,receipt=? WHERE id=?",
                    (
                        json.dumps(usage),
                        json.dumps({"observed_nano": cost}),
                        identifier,
                    ),
                )
                source = db.execute(
                    "SELECT * FROM commerce_rates WHERE id=?", (rate["rate_id"],)
                ).fetchone()
                if source:
                    latest = db.execute(
                        "SELECT MAX(version) FROM commerce_rates WHERE provider_id=? AND model=?",
                        (source["provider_id"], source["model"]),
                    ).fetchone()[0]
                    revoked = {
                        **json.loads(source["value"]),
                        "verified": False,
                        "output_bound_verified": False,
                        "evidence_ref": "overrun:" + identifier,
                    }
                    db.execute(
                        "INSERT INTO commerce_rates VALUES(?,?,?,?,?,?,?)",
                        (
                            secrets.token_hex(16),
                            source["provider_id"],
                            source["model"],
                            latest + 1,
                            source["fingerprint"],
                            json.dumps(revoked),
                            now,
                        ),
                    )
                self._unknown(db, call, "overrun", now)
                return {"state": "overrun"}
            self._finish(db, call, cost, "settled", usage)
            period = db.execute(
                "SELECT * FROM commerce_periods WHERE id=?", (call["period_id"],)
            ).fetchone()
            if (
                period["used_micro"] >= period["total_micro"]
                or period["used_nano"] >= period["total_nano"]
            ):
                self._alert(
                    db,
                    call["customer_id"],
                    call["period_id"],
                    "ai_exhausted",
                    call["instance_id"],
                    now,
                )
            return {"state": "settled", "cost_nano": cost}

    def _finish(self, db, call, cost, state, usage):
        unit = json.loads(call["rate"]).get("nano_per_micropoint", 4)
        micro = (cost + unit - 1) // unit
        db.execute(
            "UPDATE commerce_periods SET held_nano=held_nano-?,held_micro=held_micro-?,used_nano=used_nano+?,used_micro=used_micro+? WHERE id=?",
            (call["held_nano"], call["held_micro"], cost, micro, call["period_id"]),
        )
        db.execute(
            "UPDATE commerce_calls SET state=?,cost_nano=?,charged_micro=?,usage=? WHERE id=?",
            (
                state,
                cost,
                micro,
                call["usage"] if state == "reconciled" else json.dumps(usage),
                call["id"],
            ),
        )
        self._call_event(
            db,
            call["id"],
            state,
            {
                "cost_nano": cost,
                "charged_micro": micro,
                "usage" if state != "reconciled" else "reconciliation": usage,
            },
            time.time(),
        )

    def _unknown(self, db, call, state, now):
        db.execute("UPDATE commerce_calls SET state=? WHERE id=?", (state, call["id"]))
        db.execute(
            "UPDATE commerce_customers SET ai_blocked=? WHERE id=?",
            (state, call["customer_id"]),
        )
        self._alert(
            db,
            call["customer_id"],
            call["period_id"],
            "ai_review_required",
            call["instance_id"],
            now,
        )
        observed = db.execute(
            "SELECT usage,receipt FROM commerce_calls WHERE id=?", (call["id"],)
        ).fetchone()
        self._call_event(
            db,
            call["id"],
            state,
            {
                "usage": json.loads(observed["usage"]) if observed["usage"] else None,
                "receipt": json.loads(observed["receipt"])
                if observed["receipt"]
                else None,
            },
            now,
        )

    @staticmethod
    def _call_event(db, identifier, state, detail, stamp):
        db.execute(
            "INSERT INTO commerce_call_events(call_id,stamp,state,detail) VALUES(?,?,?,?)",
            (identifier, stamp, state, json.dumps(detail)),
        )

    def _alert(self, db, customer, period, kind, instance, now):
        db.execute(
            "INSERT OR IGNORE INTO commerce_alerts VALUES(?,?,?,?,?,'pending')",
            (customer, period, kind, instance, now),
        )

    def recover(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for call in db.execute(
                "SELECT * FROM commerce_calls WHERE state='reserved'"
            ).fetchall():
                self._finish(db, call, 0, "released", {})
            for call in db.execute(
                "SELECT * FROM commerce_calls WHERE state='dispatched'"
            ).fetchall():
                self._unknown(db, call, "unknown", time.time())

    def usage(self, customer="", page=1, *, now=None):
        now = time.time() if now is None else now
        if type(page) is not int or not 1 <= page <= 10000:
            raise ValueError("invalid_commerce_page")
        with self.connect() as db:
            where, args = ("WHERE customer_id=?", (customer,)) if customer else ("", ())
            periods = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM commerce_periods {where} ORDER BY start DESC LIMIT 50 OFFSET ?",
                    (*args, (page - 1) * 50),
                )
            ]
            for row in periods:
                row.pop("rate_snapshot")
                row["remaining_micro"] = max(
                    0, row["total_micro"] - row["used_micro"] - row["held_micro"]
                )
                row["active"] = row["start"] <= now < row["end"]
            calls = [
                dict(row)
                for row in db.execute(
                    f"SELECT id,customer_id,period_id,instance_id,purpose,state,stamp,held_nano,cost_nano,charged_micro FROM commerce_calls {where} ORDER BY stamp DESC LIMIT 50 OFFSET ?",
                    (*args, (page - 1) * 50),
                )
            ]
            return {
                "periods": periods,
                "calls": calls,
                "page": page,
                "orders": [
                    dict(r)
                    for r in db.execute(
                        f"SELECT * FROM commerce_orders {where} ORDER BY stamp DESC LIMIT 50",
                        args,
                    )
                ],
                "audit": [
                    dict(r)
                    for r in db.execute(
                        f"SELECT * FROM commerce_audit {where} ORDER BY stamp DESC LIMIT 50",
                        args,
                    )
                ],
                "alerts": [
                    dict(r)
                    for r in db.execute(
                        f"SELECT * FROM commerce_alerts {where} ORDER BY stamp DESC LIMIT 50",
                        args,
                    )
                ],
                "call_events": [
                    dict(r)
                    for r in db.execute(
                        "SELECT e.* FROM commerce_call_events e JOIN commerce_calls c ON c.id=e.call_id "
                        + ("WHERE c.customer_id=? " if customer else "")
                        + "ORDER BY e.id DESC LIMIT 50 OFFSET ?",
                        (*args, (page - 1) * 50),
                    )
                ],
            }

    def rates(self):
        with self.connect() as db:
            return [
                {**dict(row), "value": json.loads(row["value"])}
                for row in db.execute(
                    "SELECT r.* FROM commerce_rates r WHERE version=(SELECT MAX(version) FROM commerce_rates WHERE provider_id=r.provider_id AND model=r.model) ORDER BY stamp DESC"
                )
            ]

    def sync_groups(self, instance, ref, groups):
        """Refresh the managed-group inventory under the same ledger transaction."""
        if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
            raise ValueError("invalid_commerce_groups")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM commerce_bindings WHERE instance_id=? AND session_ref=?",
                (instance, ref),
            ).fetchone()
            if row is None or row["internal"]:
                return
            count = len(set(groups))
            if count > PLANS["standard"]["groups"]:
                raise SessionError("commerce_group_limit")
            db.execute(
                "UPDATE commerce_bindings SET groups_json=? WHERE instance_id=?",
                (json.dumps(sorted(set(groups))), instance),
            )

    def suspended(self, instance):
        """Record one durable expiry/pause alert without sending to a group."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute(
                "SELECT * FROM commerce_bindings WHERE instance_id=?", (instance,)
            ).fetchone()
            if binding is None or binding["internal"]:
                return
            period = db.execute(
                "SELECT id FROM commerce_periods WHERE customer_id=? ORDER BY start DESC LIMIT 1",
                (binding["customer_id"],),
            ).fetchone()
            self._alert(
                db,
                binding["customer_id"],
                period["id"] if period else "",
                "service_inactive",
                instance,
                time.time(),
            )


class CommerceService:
    """Cached entitlement checks backed by the bounded shared database executor."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.snapshot = {
            "operators": [],
            "customers": {},
            "bindings": {},
            "periods": [],
        }
        self.lifecycle_lock = asyncio.Lock()
        self.refresh_lock = asyncio.Lock()
        self.expired = set()
        self.stopping = False

    async def db(self, method, *args, **kwargs):
        return await self.plugin.business.db(NAMESPACE, method, *args, **kwargs)

    async def refresh(self):
        async with self.refresh_lock:
            self.snapshot = await self.db("snapshot")

    async def initialize(self):
        operator = (
            self.plugin.context.get_config().get("dashboard", {}).get("username", "")
        )
        await self.db("bootstrap", operator, self.plugin.store._all())
        await self.db("recover")
        await self.refresh()

    def require(self, instance, *, ai=False, message_stamp=None):
        binding = self.snapshot["bindings"].get(instance)
        if not binding:
            raise SessionError("commerce_binding_required")
        if binding["internal"]:
            return None
        customer = self.snapshot["customers"].get(binding["customer_id"])
        now = time.time()
        period = next(
            (
                p
                for p in self.snapshot["periods"]
                if p["customer_id"] == binding["customer_id"]
                and p["start"] <= now < p["end"]
            ),
            None,
        )
        if not customer or customer["state"] != "active" or period is None:
            raise SessionError("commerce_subscription_inactive")
        config = next(
            (
                c
                for c in self.plugin.context.get_config().get("platform", [])
                if c.get("id") == instance
            ),
            None,
        )
        if config is None or config.get("session_ref") != binding["session_ref"]:
            raise SessionError("commerce_binding_mismatch")
        policy = config.get("message_policy", {})
        if (
            policy.get("group_mode") != "allowlist"
            or len(set(policy.get("group_ids", []))) > 10
        ):
            raise SessionError("commerce_group_limit")
        if message_stamp is not None and message_stamp <= binding["resume_after"]:
            raise SessionError("commerce_historical_message")
        if ai and (period["plan"] != "ai" or customer["ai_blocked"]):
            raise SessionError("commerce_ai_paused")
        return {**binding, "period": period}

    def validate_config(self, config):
        binding = self.snapshot["bindings"].get(str(config.get("id", "")))
        if binding and binding["session_ref"] != config.get("session_ref"):
            raise SessionError("commerce_binding_mismatch")
        if binding and not binding["internal"]:
            policy = config.get("message_policy", {})
            if policy.get("group_mode") != "allowlist":
                raise SessionError("commerce_group_limit")
            groups = policy.get("group_ids", [])
            total = set(groups)
            for other in self.plugin.context.get_config().get("platform", []):
                row = self.snapshot["bindings"].get(other.get("id"))
                if (
                    row
                    and row["customer_id"] == binding["customer_id"]
                    and other["id"] != config["id"]
                ):
                    total.update(other.get("message_policy", {}).get("group_ids", []))
            if len(total) > 10:
                raise SessionError("commerce_group_limit")
        if config.get("enable"):
            self.require(str(config["id"]))

    async def execute(self, route, body, owner):
        if owner not in self.snapshot["operators"]:
            raise SessionError("commerce_operator_required")
        if route == "commerce-customers" and body.get("action") == "bind":
            ref, instance = body.get("session_ref"), body.get("instance_id")
            row = self.plugin.store.get(str(ref), owner)
            if row["state"] != "bound" or row["instance_id"] != instance:
                raise SessionError("commerce_binding_mismatch")
            config = next(
                (
                    c
                    for c in self.plugin.context.get_config().get("platform", [])
                    if c.get("id") == instance and c.get("session_ref") == ref
                ),
                None,
            )
            if (
                config is None
                or config.get("enable")
                or ref in self.plugin.manager.workers
            ):
                raise SessionError("commerce_disable_before_binding")
            body = {
                **body,
                "groups": config.get("message_policy", {}).get("group_ids", []),
            }
        result = await self.db("execute", route, body, owner)
        await self.refresh()
        return result

    def next_check(self):
        """Wake at the next period boundary, with a ten-second maximum idle gap."""
        now = time.time()
        boundaries = [
            value
            for period in self.snapshot["periods"]
            for value in (period["start"], period["end"])
            if value > now
        ]
        return min(10, max(0.01, min(boundaries) - now)) if boundaries else 10

    async def lifecycle(self):
        """Stop inactive consumers and resume only after a paid period becomes active."""
        if self.stopping:
            return
        async with self.lifecycle_lock:
            for config in self.plugin.context.get_config().get("platform", []):
                if config.get("type") != "lajiaoliao" or not config.get("enable"):
                    continue
                identifier = config["id"]
                try:
                    self.require(identifier)
                except SessionError:
                    if identifier in self.expired:
                        continue
                    await self.plugin.context.platform_manager.terminate_platform(
                        identifier
                    )
                    await self.plugin.manager.stop(config["session_ref"])
                    await self.plugin.business.db(
                        config["session_ref"], "suspend_subscription"
                    )
                    await self.db("suspended", identifier)
                    self.expired.add(identifier)
                else:
                    binding = self.snapshot["bindings"].get(identifier)
                    if binding and not binding["internal"]:
                        groups = config.get("message_policy", {}).get("group_ids", [])
                        encoded = json.dumps(sorted(set(groups)))
                        if binding["groups_json"] != encoded:
                            await self.db(
                                "sync_groups", identifier, config["session_ref"], groups
                            )
                            await self.refresh()
                    if identifier in self.expired:
                        await self.plugin.context.platform_manager.reload(config)
                        self.expired.discard(identifier)
