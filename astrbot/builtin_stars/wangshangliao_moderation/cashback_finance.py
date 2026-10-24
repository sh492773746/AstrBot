"""Scoped native financial principals and atomic chat confirmations."""

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from contextvars import ContextVar

FINANCE_TRANSACTION = ContextVar("wsl_finance_transaction", default=None)


class DashboardFinanceActor(str):
    """Bind a total-control principal to a server-owned lifecycle guard."""

    def __new__(cls, username, guard):
        principal = "dashboard/" + hashlib.sha256(username.encode()).hexdigest()
        obj = super().__new__(cls, principal)
        obj.guard = guard
        return obj

    def authorize(self, db, *, admin=False, group=None):
        """Recheck dashboard scope inside the ledger transaction.

        Args:
            db: Current ledger transaction.
            admin: Whether financial administration is required.
            group: Exact requested group, if present.

        Returns:
            A total-control identity, never an inherited chat or Access grant.
        """
        self.guard(group)
        return {
            "email": str(self),
            "name": "管理面板总控",
            "uid": "",
            "role": "admin",
        }


class FinanceActor(str):
    """Carry a server-verified chat principal, never a client-supplied role.

    Args:
        account: Receiving robot business account.
        uid: Directory-verified sender business account.
        peer: Authenticated transport sender.
        group: Exact authorized business group.
        version: Expected financial grant version.
    """

    def __new__(cls, account, uid, peer, group, version):
        principal = (
            "chat/"
            + hashlib.sha256(
                json.dumps([account, uid, peer], separators=(",", ":")).encode()
            ).hexdigest()
        )
        obj = super().__new__(cls, principal)
        obj.account, obj.uid, obj.peer = account, uid, peer
        obj.group, obj.version = group, version
        return obj

    def authorize(self, db, *, admin=False, group=None):
        """Recheck the exact persisted grant in the caller's transaction.

        Args:
            db: Current SQLite read or write transaction.
            admin: Require this group's financial administrator.
            group: Requested group, if supplied.

        Returns:
            A domain-compatible, group-scoped financial identity.
        """
        from .cashback_store import CashbackError

        row = db.execute(
            "SELECT * FROM chat_finance_grants WHERE account=? AND group_id=? AND uid=?",
            (self.account, self.group, self.uid),
        ).fetchone()
        rule = db.execute(
            "SELECT account FROM groups WHERE id=?", (self.group,)
        ).fetchone()
        if (
            not row
            or not row["active"]
            or row["peer"] != self.peer
            or row["version"] != self.version
            or rule
            and rule["account"] != self.account
            or group is not None
            and group != self.group
            or admin
            and row["role"] != "finance_admin"
        ):
            raise CashbackError(
                "finance_forbidden", "你的结账权限已变化，请重新发送“结账”。", 403
            )
        return {
            "email": str(self),
            "name": row["name"],
            "uid": self.uid,
            "role": "admin" if row["role"] == "finance_admin" else "cashier",
        }


class FinanceStoreMixin:
    """Keep financial grants and confirmation state in the existing ledger."""

    def finance_schema(self, db):
        """Create additive chat-only metadata without rewriting ledger rows.

        Args:
            db: Migration connection.
        """
        db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_finance_grants(
                account TEXT NOT NULL,group_id TEXT NOT NULL,uid TEXT NOT NULL,
                peer TEXT NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL,
                active INTEGER NOT NULL,version INTEGER NOT NULL,
                PRIMARY KEY(account,group_id,uid));
            CREATE TABLE IF NOT EXISTS chat_finance_controls(
                actor TEXT NOT NULL,request_id TEXT NOT NULL,digest TEXT NOT NULL,
                result TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(actor,request_id));
            CREATE TABLE IF NOT EXISTS chat_finance_confirmations(
                id TEXT PRIMARY KEY,actor TEXT NOT NULL,account TEXT NOT NULL,
                group_id TEXT NOT NULL,session TEXT NOT NULL,boot TEXT NOT NULL,
                code_hash TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,
                expires REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',result TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS chat_finance_actor_recent
                ON chat_finance_confirmations(actor,created DESC);
        """)

    def finance_grants(self, account=None, uid=None, peer=None):
        """Read control-plane grants or a transport-bound sender's own grants.

        Args:
            account: Exact receiving account, or None for total control.
            uid: Verified business sender, required for sender filtering.
            peer: Verified transport sender, required for sender filtering.

        Returns:
            Matching persisted grants.
        """
        with self.connect() as db:
            if account is None:
                rows = db.execute(
                    "SELECT * FROM chat_finance_grants ORDER BY account,group_id,name,uid"
                )
            else:
                rows = db.execute(
                    "SELECT f.* FROM chat_finance_grants f "
                    "LEFT JOIN groups g ON g.id=f.group_id "
                    "WHERE f.account=? AND f.uid=? AND f.peer=? AND f.active=1 "
                    "AND (g.id IS NULL OR g.account=f.account) ORDER BY f.group_id",
                    (account, uid, peer),
                )
            return [dict(r) for r in rows]

    def finance_grant(self, controller, data):
        """Save a directory-verified, versioned total-control grant.

        Args:
            controller: Authenticated AstrBot dashboard principal.
            data: Server-normalized grant including a verified peer and name.

        Returns:
            Idempotent save acknowledgement.
        """
        from .cashback_store import CashbackError

        if (
            not controller.startswith("dashboard/")
            or set(data)
            != {
                "account",
                "group_id",
                "uid",
                "peer",
                "name",
                "role",
                "active",
                "version",
                "request_id",
            }
            or any(
                not isinstance(data[k], str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", data[k])
                for k in ("account", "group_id", "uid")
            )
            or not isinstance(data["peer"], str)
            or not data["peer"]
            or len(data["peer"]) > 128
            or not isinstance(data["role"], str)
            or data["role"] not in {"cashier", "finance_admin"}
            or type(data["active"]) is not bool
            or type(data["version"]) is not int
            or data["version"] < 0
            or not isinstance(data["request_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", data["request_id"])
        ):
            raise CashbackError("invalid_grant", "财务授权参数无效。")
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        with self.connect(True) as db:
            previous = db.execute(
                "SELECT * FROM chat_finance_controls WHERE actor=? AND request_id=?",
                (controller, data["request_id"]),
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise CashbackError("conflict", "同一授权请求内容发生变化。", 409)
                return json.loads(previous["result"])
            old = db.execute(
                "SELECT * FROM chat_finance_grants WHERE account=? AND group_id=? AND uid=?",
                (data["account"], data["group_id"], data["uid"]),
            ).fetchone()
            if data["version"] != (old["version"] if old else 0):
                raise CashbackError("conflict", "财务授权已变化，请刷新后重试。", 409)
            db.execute(
                "INSERT INTO chat_finance_grants VALUES(?,?,?,?,?,?,?,1) "
                "ON CONFLICT(account,group_id,uid) DO UPDATE SET peer=excluded.peer,"
                "name=excluded.name,role=excluded.role,active=excluded.active,"
                "version=chat_finance_grants.version+1",
                tuple(
                    data[k]
                    for k in (
                        "account",
                        "group_id",
                        "uid",
                        "peer",
                        "name",
                        "role",
                        "active",
                    )
                ),
            )
            principal = FinanceActor(
                data["account"], data["uid"], data["peer"], data["group_id"], 0
            )
            # This row supports historical display and the legacy contact FK;
            # its role is deliberately rejected by the web authorization code.
            db.execute(
                "INSERT INTO users(email,name,role) VALUES(?,?,'chat') "
                "ON CONFLICT(email) DO UPDATE SET name=excluded.name",
                (principal, data["name"]),
            )
            db.execute(
                "UPDATE chat_finance_confirmations SET status='cancelled' "
                "WHERE actor=? AND group_id=? AND status='pending'",
                (principal, data["group_id"]),
            )
            self._audit(
                db,
                data["group_id"],
                None,
                controller,
                "chat_finance_grant",
                dict(old) if old else {},
                {k: v for k, v in data.items() if k != "peer"},
            )
            result = {"status": "saved"}
            db.execute(
                "INSERT INTO chat_finance_controls VALUES(?,?,?,?,?)",
                (
                    controller,
                    data["request_id"],
                    digest,
                    json.dumps(result),
                    time.time(),
                ),
            )
            return result

    def finance_preview(self, actor, session, boot, payload):
        """Persist an exact, short-lived confirmation snapshot.

        Args:
            actor: Scoped native financial identity.
            session: Exact private navigation isolation key.
            boot: Process boot nonce.
            payload: Validated operation and exact selection.

        Returns:
            Confirmation identifier, one-time code and canonical snapshot.
        """
        from .cashback_store import CashbackError

        operation = payload["operation"]
        if operation not in {
            "pay",
            "correct",
            "reprice",
            "assign",
            "exception",
            "configure",
            "risk_release",
        }:
            raise CashbackError("invalid_operation", "操作无效。")
        admin = operation in {"configure", "assign", "exception", "risk_release"}
        with self.connect(True) as db:
            actor.authorize(db, admin=admin)
            rule = db.execute(
                "SELECT * FROM groups WHERE id=?", (actor.group,)
            ).fetchone()
            if rule and rule["account"] != actor.account:
                raise CashbackError("finance_forbidden", "此群的机器人已变化。", 403)
            snapshot = json.loads(json.dumps(payload))
            snapshot["rule_version"] = rule["version"] if rule else 0
            snapshot["grant_version"] = actor.version
            if operation == "risk_release":
                risk = db.execute(
                    "SELECT * FROM cashback_inviter_risk "
                    "WHERE account=? AND group_id=? AND inviter=?",
                    (actor.account, actor.group, snapshot["inviter"]),
                ).fetchone()
                if not risk or risk["blocked_at"] is None:
                    raise CashbackError(
                        "risk_not_blocked", "该邀请人当前未被冻结。", 409
                    )
                snapshot["risk_snapshot"] = dict(risk)
            items = snapshot.get("items", [])
            if operation not in {"configure", "risk_release"}:
                if not 1 <= len(items) <= 100:
                    raise CashbackError("invalid_selection", "每次请选择1至100条记录。")
                rows = []
                for item in items:
                    row = db.execute(
                        "SELECT * FROM entries WHERE group_id=? AND id=?",
                        (actor.group, item["id"]),
                    ).fetchone()
                    if not row or row["version"] != item["version"]:
                        raise CashbackError("conflict", "记录已变化，请重新选择。", 409)
                    rows.append(dict(row))
                if len({r["inviter"] for r in rows}) != 1:
                    raise CashbackError(
                        "mixed_recipients", "一次只处理同一邀请人的记录。"
                    )
                snapshot["amount"] = sum(r["amount"] for r in rows)
                snapshot["recipient"] = rows[0]["inviter"]
                snapshot["recipient_name"] = rows[0]["inviter_name"]
                snapshot["members"] = [r["member_name"] for r in rows]
                if operation == "reprice":
                    snapshot["price_confirmation"] = self._price_preview(
                        db, actor, actor.group, items, snapshot["category"]
                    )
                    snapshot["target_amount"] = sum(
                        r["target_amount"]
                        for r in snapshot["price_confirmation"]["items"]
                    )
            db.execute(
                "UPDATE chat_finance_confirmations SET status='cancelled' "
                "WHERE actor=? AND session=? AND status='pending'",
                (actor, session),
            )
            ticket, code = uuid.uuid4().hex, f"{secrets.randbelow(1000000):06d}"
            db.execute(
                "INSERT INTO chat_finance_confirmations"
                "(id,actor,account,group_id,session,boot,code_hash,payload,created,expires) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ticket,
                    actor,
                    actor.account,
                    actor.group,
                    session,
                    boot,
                    hashlib.sha256((ticket + code).encode()).hexdigest(),
                    json.dumps(snapshot, ensure_ascii=False),
                    time.time(),
                    time.time() + 120,
                ),
            )
            return {"id": ticket, "code": code, "payload": snapshot}

    def finance_cancel(self, principal, session):
        """Invalidate unsubmitted confirmations for one private session.

        Args:
            principal: Server-derived financial principal.
            session: Exact private navigation key.
        """
        with self.connect(True) as db:
            db.execute(
                "UPDATE chat_finance_confirmations SET status='cancelled' "
                "WHERE actor=? AND session=? AND status='pending'",
                (principal, session),
            )

    def finance_result(self, actor, ticket=""):
        """Read only this actor's receipt in this exact authorized group.

        Args:
            actor: Scoped native financial identity.
            ticket: Optional exact confirmation ID.

        Returns:
            Latest permitted operation status and receipt.
        """
        with self.connect() as db:
            actor.authorize(db)
            row = db.execute(
                "SELECT id,status,result FROM chat_finance_confirmations "
                "WHERE actor=? AND group_id=? AND (?='' OR id=?) "
                "ORDER BY created DESC LIMIT 1",
                (actor, actor.group, ticket, ticket),
            ).fetchone()
            return dict(row) if row else None

    def finance_apply(self, actor, ticket, code, session, boot, callback):
        """Atomically consume a confirmation and execute trusted domain code.

        Args:
            actor: Scoped native financial identity.
            ticket: Server-side confirmation ID.
            code: User-entered one-time code.
            session: Exact original private navigation key.
            boot: Current process boot nonce.
            callback: Internal synchronous operation, never a client callable.

        Returns:
            Committed domain result, including on duplicate confirmation.
        """
        from .cashback_store import CashbackError

        # Wrong-code attempts must commit even though no financial action runs.
        with self.connect(True) as db:
            actor.authorize(db)
            row = db.execute(
                "SELECT * FROM chat_finance_confirmations WHERE id=? AND actor=? "
                "AND group_id=? AND session=?",
                (ticket, actor, actor.group, session),
            ).fetchone()
            if row and row["status"] == "completed":
                if hmac.compare_digest(
                    row["code_hash"],
                    hashlib.sha256((ticket + code).encode()).hexdigest(),
                ):
                    return json.loads(row["result"])
                row = None
            valid = (
                row
                and row["status"] == "pending"
                and row["boot"] == boot
                and row["expires"] > time.time()
                and row["attempts"] < 5
            )
            correct = valid and hmac.compare_digest(
                row["code_hash"], hashlib.sha256((ticket + code).encode()).hexdigest()
            )
            if valid and not correct:
                db.execute(
                    "UPDATE chat_finance_confirmations SET attempts=attempts+1 WHERE id=?",
                    (ticket,),
                )
            if not correct:
                failure = True
            else:
                failure = False
                payload = json.loads(row["payload"])
                actor.authorize(
                    db,
                    admin=payload["operation"]
                    in {"configure", "assign", "exception", "risk_release"},
                )
                rule = db.execute(
                    "SELECT * FROM groups WHERE id=?", (actor.group,)
                ).fetchone()
                if (
                    actor.version != payload["grant_version"]
                    or (rule["version"] if rule else 0) != payload["rule_version"]
                    or rule
                    and rule["account"] != actor.account
                ):
                    raise CashbackError(
                        "conflict", "规则或权限已变化，请重新确认。", 409
                    )
                if payload["operation"] == "risk_release":
                    risk = db.execute(
                        "SELECT * FROM cashback_inviter_risk "
                        "WHERE account=? AND group_id=? AND inviter=?",
                        (actor.account, actor.group, payload["inviter"]),
                    ).fetchone()
                    if (dict(risk) if risk else None) != payload["risk_snapshot"]:
                        raise CashbackError(
                            "conflict", "风控情况已变化，请重新复核。", 409
                        )
                token = FINANCE_TRANSACTION.set((self, db))
                try:
                    result = callback(payload)
                    # The native grant is checked again after all domain writes.
                    actor.authorize(db)
                    db.execute(
                        "UPDATE chat_finance_confirmations SET status='completed',result=? "
                        "WHERE id=?",
                        (json.dumps(result, ensure_ascii=False), ticket),
                    )
                    self._audit(
                        db,
                        actor.group,
                        None,
                        actor,
                        "chat_finance_completed",
                        {},
                        {"operation": payload["operation"], "request_id": ticket},
                        reason=payload.get("reason", ""),
                    )
                finally:
                    FINANCE_TRANSACTION.reset(token)
        if failure:
            raise CashbackError(
                "confirmation_invalid", "确认码无效、已取消或已过期，请重新预览。", 409
            )
        return result
