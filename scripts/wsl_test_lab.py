"""Local-only, opt-in membership and role acceptance; never imported by the app."""

import argparse
import asyncio
import copy
import fcntl
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.business_http import _exchange_business
from astrbot.core.platform.sources.wangshangliao.directory import member_directory
from astrbot.core.platform.sources.wangshangliao.invitations import (
    latest_membership_events,
)
from astrbot.core.platform.sources.wangshangliao.storage import Vault
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from scripts.audit_wsl_finance import FINANCIAL_FIELDS
from scripts.wsl_test_membership import (
    ACCEPT,
    GROUP,
    INVITE,
    ORDINARY,
    OWNER,
    PEER,
    REMOVE,
)

ROOT = Path(__file__).resolve().parents[1]
CONTROL = Path("/opt/backups/wsl-test-lab")
FORMAL_GROUPS = {"1516387", "1488006"}
INSTANCES = {
    OWNER: "wangshangliao_0acde7ec",
    PEER: "wangshangliao_f79c5b81",
    ORDINARY: "wangshangliao_6541e09d",
}
READS = {
    "/v1/group/get-group-list",
    "/v1/group/get-group-members",
    "/v1/group/get-apply-logs",
}
GRANT = "/v1/group/add-group-manage"
REVOKE = "/v1/group/del-group-manage"
TRANSFER = "/v1/group/group-transfer"
OWNERSHIP_ACTIONS = {"transfer-owner", "restore-owner"}
ROUTES = {
    "invite": INVITE,
    "remove": REMOVE,
    "accept": ACCEPT,
    "grant": GRANT,
    "revoke": REVOKE,
    "transfer-owner": TRANSFER,
    "restore-owner": TRANSFER,
}


def protected_state(root, group, *, first=False):
    """Read protected financial/configuration state without opening writable stores.

    Args:
        root: Application installation.
        group: Explicit controlled test group, never a financial group.
        first: Require absence of local onboarding and membership history.

    Returns:
        Stable digest of all financial fields, group rules and formal bindings.

    Raises:
        ProtocolError: If the group has financial data or prior onboarding history.
        sqlite3.Error: If any required evidence cannot be read.
    """
    data = root / "data/plugin_data"
    finance = data / "wangshangliao_cashback/cashback.sqlite3"
    with closing(sqlite3.connect(finance.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("SELECT 1 FROM groups WHERE id=?", (group,)).fetchone():
            raise ProtocolError("test_lab_financial_group")
        if db.execute("SELECT 1 FROM entries WHERE group_id=?", (group,)).fetchone():
            raise ProtocolError("test_lab_financial_group")
        money = db.execute(
            f"SELECT {FINANCIAL_FIELDS} FROM entries ORDER BY id"
        ).fetchall()
        rules = db.execute("SELECT * FROM groups ORDER BY id").fetchall()
    source = data / "wangshangliao_open/open-service.sqlite3"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        formal = db.execute(
            "SELECT * FROM tenants WHERE group_id IN ('1516387','1488006') "
            "ORDER BY account,group_id"
        ).fetchall()
        if first and (
            db.execute(
                "SELECT 1 FROM tenants WHERE account=? AND group_id=?", (PEER, group)
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM audit WHERE account=? AND group_id=?", (PEER, group)
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM retired_groups WHERE group_id=?", (group,)
            ).fetchone()
        ):
            raise ProtocolError("test_lab_not_first_group")
    return hashlib.sha256(
        json.dumps([money, rules, formal]).encode()
        + (root / "data/cmd_config.json").read_bytes()
    ).hexdigest()


class TestLab:
    """Serialize bounded test requests and keep one-use confirmations outside live data."""

    __test__ = False

    def __init__(
        self,
        clients,
        pins,
        group,
        control,
        *,
        protection,
        daily_write_limit=10,
        extra_role_pair_day="",
        extra_role_pair_round=1,
    ):
        """Bind only known controlled accounts and private local acceptance state.

        Args:
            clients: Restored business clients for the fixed owner and peer.
            pins: Trusted vault transport IDs for controlled roster members.
            group: Explicit business test group, excluding the two formal groups.
            control: Private shared directory used by all invocations.
            protection: Read-only financial/history guard.
            daily_write_limit: Explicit local rehearsal cap; never exceeds twenty.
            extra_role_pair_day: Operator-approved UTC date for one extra grant/revoke
                pair after twenty attempts, only in the fixed controlled test group.
            extra_role_pair_round: One by default; two requires separate approval
                after the first extra pair has been verified and restored.

        Raises:
            ProtocolError: If identities, group or directory permissions are unsafe.
        """
        if (
            not isinstance(group, str)
            or not group.isascii()
            or not group.isdigit()
            or not 0 < int(group) < 1 << 63
            or str(int(group)) != group
            or group in FORMAL_GROUPS
            or set(clients) != {OWNER, PEER}
            or set(pins) != {OWNER, PEER, ORDINARY}
            or any(not isinstance(pin, str) or not pin for pin in pins.values())
            or len(set(pins.values())) != 3
            or type(daily_write_limit) is not int
            or daily_write_limit not in {10, 20}
            or type(extra_role_pair_day) is not str
            or type(extra_role_pair_round) is not int
            or extra_role_pair_round not in {1, 2}
            or extra_role_pair_round == 2
            and not extra_role_pair_day
            or extra_role_pair_day
            and (
                group != GROUP
                or daily_write_limit != 20
                or extra_role_pair_day
                != datetime.fromtimestamp(time.time(), timezone.utc).date().isoformat()
            )
        ):
            raise ProtocolError("test_lab_scope")
        self.clients, self.pins, self.group = clients, dict(pins), group
        self.protection = protection
        self.daily_write_limit = daily_write_limit
        self.extra_role_pair_day = extra_role_pair_day
        self.extra_role_pair_round = extra_role_pair_round
        self.control = Path(control)
        self.control.mkdir(mode=0o700, parents=True, exist_ok=True)
        stat = self.control.lstat()
        if (
            self.control.is_symlink()
            or stat.st_uid != os.geteuid()
            or stat.st_mode & 0o077
        ):
            raise ProtocolError("test_lab_private_directory")
        self.path = self.control / "control.sqlite3"
        if self.path.is_symlink():
            raise ProtocolError("test_lab_private_directory")
        self.fd, self.task, self.submitting = None, None, None
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS pacing(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    request_finished REAL NOT NULL,write_finished REAL NOT NULL,
                    blocked_until REAL NOT NULL);
                INSERT OR IGNORE INTO pacing VALUES(1,0,0,0);
                CREATE TABLE IF NOT EXISTS previews(
                    id TEXT PRIMARY KEY,digest TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,expires REAL NOT NULL,used INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY,group_id TEXT NOT NULL,action TEXT NOT NULL,
                    created REAL NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,
                    apply_id TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
                    provider_accepted INTEGER NOT NULL DEFAULT 0);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(attempts)")}
            if "provider_accepted" not in columns:
                db.execute(
                    "ALTER TABLE attempts ADD COLUMN "
                    "provider_accepted INTEGER NOT NULL DEFAULT 0"
                )
                db.execute(
                    "UPDATE attempts SET provider_accepted=1 WHERE state='accepted'"
                )
            if "evidence" not in columns:
                db.execute(
                    "ALTER TABLE attempts ADD COLUMN evidence TEXT NOT NULL DEFAULT '{}'"
                )
        self.path.chmod(0o600)

    @contextmanager
    def locked(self):
        """Fail busy instead of queueing concurrent local test runners.

        Yields:
            Exclusive ownership of this shared test journal.

        Raises:
            ProtocolError: If another runner holds the lock.
        """
        if self.fd is not None:
            raise ProtocolError("test_lab_busy")
        fd = os.open(
            self.control / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProtocolError("test_lab_busy") from None
            self.fd, self.task = fd, asyncio.current_task()
            yield
        finally:
            self.fd, self.task, self.submitting = None, None, None
            os.close(fd)

    async def exchange(self, account, route, params):
        """Dispatch fixed test IO, checking scope and pacing inside the HTTP lock.

        Args:
            account: Exact controlled actor.
            route: Reviewed read route or the currently reserved test write.
            params: Exact route-specific parameters.

        Returns:
            Decoded signed response, without credentials.

        Raises:
            ProtocolError: On scope, identity, confirmation or pacing violations.
        """
        is_write = route in ROUTES.values()

        async def guard():
            if (
                self.fd is None
                or self.task is not asyncio.current_task()
                or account not in self.clients
                or str(self.clients[account].fields[3]) != account
                or any(str(c.fields[3]) != uid for uid, c in self.clients.items())
            ):
                raise ProtocolError("test_lab_identity")
            if is_write:
                intent = self.submitting
                if not intent or (account, route, params) != intent["request"]:
                    raise ProtocolError("test_lab_unreserved")
                with closing(sqlite3.connect(self.path)) as db:
                    write, blocked = db.execute(
                        "SELECT write_finished,blocked_until FROM pacing"
                    ).fetchone()
                if blocked > time.time():
                    raise ProtocolError("test_lab_platform_cooldown")
                if write > time.time() + 1:
                    raise ProtocolError("test_lab_clock_changed")
                await asyncio.sleep(max(0, write + 30 - time.time()))
                current = await self.snapshot(
                    intent["payload"]["action"], first=intent["payload"]["first"]
                )
                if current != intent["payload"] or time.time() >= intent["expires"]:
                    raise ProtocolError("test_lab_stale_preview")
            elif route not in READS or (
                route == "/v1/group/get-group-members"
                and (
                    params.get("groupId") != int(self.group)
                    or type(params.get("groupId")) is not int
                    or params.get("v") != "0"
                    or set(params) - {"groupId", "v", "cursor"}
                )
                or route == "/v1/group/get-group-list"
                and params != {"v": "0"}
                or route == "/v1/group/get-apply-logs"
                and params != {}
            ):
                raise ProtocolError("test_lab_scope")
            with closing(sqlite3.connect(self.path)) as db:
                request, write, blocked = db.execute(
                    "SELECT request_finished,write_finished,blocked_until FROM pacing"
                ).fetchone()
            if blocked > time.time():
                raise ProtocolError("test_lab_platform_cooldown")
            if request > time.time() + 1 or write > time.time() + 1:
                raise ProtocolError("test_lab_clock_changed")
            await asyncio.sleep(
                max(
                    0,
                    request + 3 - time.time(),
                    write + 30 - time.time() if is_write else 0,
                )
            )
            if is_write and time.time() >= self.submitting["expires"]:
                raise ProtocolError("test_lab_stale_preview")
            if (
                is_write
                and self.protection(
                    self.group, first=self.submitting["payload"]["first"]
                )
                != self.submitting["payload"]["protected"]
            ):
                raise ProtocolError("test_lab_stale_preview")
            if any(str(c.fields[3]) != uid for uid, c in self.clients.items()):
                raise ProtocolError("test_lab_identity")

        submission = (
            self.submitting["submission"]
            if is_write and self.submitting
            else {"started": False}
        )
        try:
            result = await _exchange_business(
                self.clients[account],
                route,
                copy.deepcopy(params),
                authorize=guard,
                submission=submission,
            )
            if is_write:
                submission["accepted"] = True
            return result
        except BaseException as exc:
            exc.submitted = submission["started"]
            retry_after = getattr(exc, "retry_after", 0)
            if isinstance(retry_after, (float, int)) and retry_after > 0:
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(
                        "UPDATE pacing SET blocked_until=MAX(blocked_until,?)",
                        (time.time() + min(retry_after, 86400),),
                    )
            raise
        finally:
            if submission["started"]:
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(
                        "UPDATE pacing SET request_finished=?,"
                        "write_finished=CASE WHEN ? THEN ? ELSE write_finished END",
                        (time.time(), is_write, time.time()),
                    )

    async def request(self, route, params):
        """Adapt the complete native member-directory parser to paced owner reads.

        Args:
            route: Fixed directory read.
            params: Bound test-group pagination.

        Returns:
            Native member page.
        """
        return await self.exchange(OWNER, route, params)

    async def invitation(self, attempt):
        """Require one fresh signed invitation matching this exact test intent.

        Args:
            attempt: Durable invite intent from this journal.

        Returns:
            Minimal verified invitation metadata.

        Raises:
            ProtocolError: If evidence is missing, stale, duplicated or conflicting.
        """
        result = await self.exchange(PEER, "/v1/group/get-apply-logs", {})
        records = result.get("list") if isinstance(result, dict) else None
        if not isinstance(records, list) or len(records) > 4096:
            raise ProtocolError("test_lab_invitation_shape")
        matching = []
        for row in records:
            if not isinstance(row, dict):
                continue
            applicant, inviter = row.get("applicant"), row.get("inviter")
            if not isinstance(applicant, dict) or not isinstance(inviter, dict):
                continue
            if (
                str(row.get("groupId")) != self.group
                or str(applicant.get("userId")) != PEER
                or str(applicant.get("nimId")) != self.pins[PEER]
                or str(inviter.get("userId")) != OWNER
                or row.get("groupApplyType") != "INVITATION"
            ):
                continue
            try:
                stamp = datetime.fromisoformat(
                    str(row["applyTime"]).replace("Z", "+00:00")
                )
                if stamp.tzinfo is None:
                    raise ValueError
            except (KeyError, ValueError):
                raise ProtocolError("test_lab_invitation_time") from None
            if attempt["created"] - 5 <= stamp.timestamp() <= time.time() + 5:
                identity = row.get("applyId")
                if (
                    not isinstance(identity, str)
                    or not identity.isascii()
                    or not identity.isdigit()
                    or not 0 < int(identity) < 1 << 63
                    or row.get("state")
                    not in {"MEMBER_STATE_INVITED", "MEMBER_STATE_GOOD"}
                    or attempt["apply_id"] not in {"", identity}
                ):
                    raise ProtocolError("test_lab_invitation_identity")
                matching.append({"id": identity, "state": row["state"]})
        if len(matching) != 1:
            raise ProtocolError("test_lab_invitation_unproven")
        latest = latest_membership_events(result, self.group).get(PEER)
        if (
            not latest
            or latest["ambiguous"]
            or str(latest["row"]["applyId"]) != matching[0]["id"]
        ):
            raise ProtocolError("test_lab_invitation_superseded")
        return matching[0]

    async def snapshot(self, action, *, first=False):
        """Check native roles, exact controlled roster and protected local history.

        Args:
            action: A fixed membership, administrator or ownership test action.
            first: Require a genuinely unused local onboarding group.

        Returns:
            Confirmation-bound, credential-free scope snapshot.

        Raises:
            ProtocolError: If any membership, history or account check fails.
        """
        if (
            action not in ROUTES
            or first
            and action != "invite"
            or action in {"grant", "revoke"} | OWNERSHIP_ACTIONS
            and self.group != GROUP
        ):
            raise ProtocolError("test_lab_scope")
        if self.extra_role_pair_day and (
            action not in {"grant", "revoke"}
            or first
            or self.extra_role_pair_day
            != datetime.fromtimestamp(time.time(), timezone.utc).date().isoformat()
        ):
            raise ProtocolError("test_lab_role_extension_scope")
        protected = self.protection(self.group, first=first)
        listed = await self.exchange(OWNER, "/v1/group/get-group-list", {"v": "0"})
        if not isinstance(listed, dict) or any(
            not isinstance(listed.get(k), list) for k in ("owner", "member")
        ):
            raise ProtocolError("test_lab_group_directory")
        groups = [
            g
            for g in listed["owner"] + listed["member"]
            if isinstance(g, dict) and str(g.get("groupId")) == self.group
        ]
        if (
            len(groups) != 1
            or not str(groups[0].get("groupCloudId", "")).isascii()
            or not str(groups[0].get("groupCloudId", "")).isdigit()
        ):
            raise ProtocolError("test_lab_group_directory")
        directory = await member_directory(self, self.group)
        members = {str(row["userId"]): row for row in directory["groupMemberInfo"]}
        ownership = action in OWNERSHIP_ACTIONS
        actor = PEER if action in {"accept", "restore-owner"} else OWNER
        target = OWNER if action == "restore-owner" else PEER
        native_roles = {
            OWNER: {"GROUP_ROLE_OWNER"},
            PEER: {"GROUP_ROLE_ADMIN" if action == "revoke" else "GROUP_ROLE_MEMBER"},
            ORDINARY: {"GROUP_ROLE_MEMBER"},
        }
        if action == "transfer-owner":
            native_roles[PEER] = {"GROUP_ROLE_MEMBER", "GROUP_ROLE_ADMIN"}
        elif action == "restore-owner":
            native_roles[OWNER] = {"GROUP_ROLE_MEMBER", "GROUP_ROLE_ADMIN"}
            native_roles[PEER] = {"GROUP_ROLE_OWNER"}
        if (
            OWNER not in members
            or not set(members) <= set(self.pins)
            or action in {"grant", "revoke"} | OWNERSHIP_ACTIONS
            and ORDINARY not in members
            or any(
                row.get("nimId") != self.pins[uid]
                or row.get("accountState") != "ACCOUNT_STATE_GOOD"
                or row.get("groupRole") not in native_roles[uid]
                for uid, row in members.items()
            )
            or (PEER in members)
            != (action in {"remove", "grant", "revoke"} | OWNERSHIP_ACTIONS)
        ):
            raise ProtocolError("test_lab_native_scope")
        if first:
            logs = await self.exchange(PEER, "/v1/group/get-apply-logs", {})
            records = logs.get("list") if isinstance(logs, dict) else None
            if not isinstance(records, list) or len(records) > 4096:
                raise ProtocolError("test_lab_invitation_shape")
            if any(
                isinstance(row, dict)
                and str(row.get("groupId")) == self.group
                and isinstance(row.get("applicant"), dict)
                and str(row["applicant"].get("userId")) == PEER
                for row in records
            ):
                raise ProtocolError("test_lab_not_first_group")
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            pending = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM attempts WHERE group_id=? "
                    "AND state IN ('unknown','pending','accepted') ORDER BY created",
                    (self.group,),
                )
                if not self.submitting or row["id"] != self.submitting["id"]
            ]
            previous_owner = next(
                (
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM attempts WHERE group_id=? "
                        "AND state!='not_submitted' AND action IN "
                        "('transfer-owner','restore-owner') "
                        "ORDER BY created DESC,rowid DESC",
                        (self.group,),
                    )
                    if not self.submitting or row["id"] != self.submitting["id"]
                ),
                None,
            )
        proof = None
        if action == "accept":
            if len(pending) != 1 or pending[0]["action"] != "invite":
                raise ProtocolError("test_lab_invitation_unreserved")
            proof = await self.invitation(pending[0])
            if proof["state"] != "MEMBER_STATE_INVITED":
                raise ProtocolError("test_lab_invitation_not_pending")
            proof["attempt"] = pending[0]["id"]
        elif pending:
            raise ProtocolError("test_lab_unresolved_action")
        owner_proof = None
        if action == "restore-owner":
            if (
                not previous_owner
                or previous_owner["action"] != "transfer-owner"
                or previous_owner["state"] != "verified"
                or not previous_owner["provider_accepted"]
            ):
                raise ProtocolError("test_lab_ownership_unproven")
            original = json.loads(previous_owner["payload"])
            observed = json.loads(previous_owner["evidence"])
            if (
                original["group"] != self.group
                or original["actor"] != OWNER
                or original["target"] != PEER
                or original["team"] != str(groups[0]["groupCloudId"])
                or {m[0]: m[1] for m in original["members"]} != self.pins
                or observed.get("roles")
                != {uid: row["groupRole"] for uid, row in members.items()}
            ):
                raise ProtocolError("test_lab_ownership_unproven")
            owner_proof = {
                "attempt": previous_owner["id"],
                "original_roles": {m[0]: m[2] for m in original["members"]},
            }
        elif (
            ownership
            and previous_owner
            and not (
                previous_owner["action"] == "restore-owner"
                and previous_owner["state"] == "verified"
                and previous_owner["provider_accepted"]
            )
        ):
            raise ProtocolError("test_lab_ownership_unrestored")
        payload = {
            "action": action,
            "first": first,
            "group": self.group,
            "name": str(groups[0].get("groupName", ""))[:256],
            "team": str(groups[0]["groupCloudId"]),
            "actor": actor,
            "target": target,
            "members": sorted(
                [uid, row["nimId"], row["groupRole"]] for uid, row in members.items()
            ),
            "protected": protected,
            "invitation": proof,
        }
        if ownership:
            payload["ownership"] = owner_proof
            payload["previous_ownership"] = (
                previous_owner["id"] if previous_owner else ""
            )
            payload["warning"] = (
                "Changes the native group owner, not just administrator access. "
                "Restoration needs a separate confirmed action by the new owner; "
                "previous administrator roles may need separate restoration."
            )
        if self.extra_role_pair_day:
            payload["extra_role_pair_day"] = self.extra_role_pair_day
            if self.extra_role_pair_round == 2:
                payload["extra_role_pair_round"] = 2
        return payload

    async def preview(self, action, *, first=False):
        """Create a ten-minute, one-use local confirmation; store only its digest.

        Args:
            action: Exact reviewed membership or native-role action.
            first: Check that this group has no prior onboarding history.

        Returns:
            Preview, expiry and secret confirmation displayed only to the operator.
        """
        with self.locked():
            payload = await self.snapshot(action, first=first)
            identity, token = secrets.token_hex(16), secrets.token_urlsafe(24)
            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac("sha256", token.encode(), salt, 600_000).hex()
            expires = time.time() + 600
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute(
                    "INSERT INTO previews(id,digest,payload,expires) VALUES(?,?,?,?)",
                    (
                        identity,
                        f"pbkdf2_sha256:{salt.hex()}:{digest}",
                        json.dumps(payload),
                        expires,
                    ),
                )
            return {
                "preview": identity,
                "confirmation": token,
                "expires": expires,
                **payload,
                "test_only": True,
            }

    async def execute(self, identity, token):
        """Consume one matching preview and submit at most one native mutation.

        Args:
            identity: Local preview ID.
            token: Confirmation supplied through the terminal, never a CLI argument.

        Returns:
            Persisted provider/roster evidence, not a declaration of full acceptance.

        Raises:
            ProtocolError: If confirmation, scope, budget or protected state changed.
        """
        with self.locked():
            with closing(sqlite3.connect(self.path)) as db:
                db.row_factory = sqlite3.Row
                preview = db.execute(
                    "SELECT * FROM previews WHERE id=?", (identity,)
                ).fetchone()
                confirmed = False
                if preview and isinstance(token, str):
                    digest = preview["digest"].split(":")
                    if (
                        len(digest) == 3
                        and digest[0] == "pbkdf2_sha256"
                        and len(digest[1]) == 32
                        and len(digest[2]) == 64
                    ):
                        try:
                            salt = bytes.fromhex(digest[1])
                        except ValueError:
                            salt = None
                        if salt is not None:
                            actual = hashlib.pbkdf2_hmac(
                                "sha256", token.encode(), salt, 600_000
                            ).hex()
                            confirmed = hmac.compare_digest(digest[2], actual)
                if (
                    not preview
                    or preview["used"]
                    or time.time() >= preview["expires"]
                    or not confirmed
                ):
                    raise ProtocolError("test_lab_confirmation")
                payload = json.loads(preview["payload"])
            if payload["group"] != self.group:
                raise ProtocolError("test_lab_scope")
            if (
                await self.snapshot(payload["action"], first=payload["first"])
                != payload
            ):
                raise ProtocolError("test_lab_stale_preview")
            with closing(sqlite3.connect(self.path)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                used = db.execute(
                    "SELECT COUNT(*) FROM attempts WHERE created>=?",
                    (int(time.time() // 86400) * 86400,),
                ).fetchone()[0]
                limit = self.daily_write_limit
                required_writes = 2 if payload["action"] == "transfer-owner" else 1
                if self.extra_role_pair_day:
                    latest = db.execute(
                        "SELECT * FROM attempts WHERE created>=? "
                        "ORDER BY created DESC,rowid DESC LIMIT 1",
                        (int(time.time() // 86400) * 86400,),
                    ).fetchone()
                    previous = json.loads(latest["payload"]) if latest else {}
                    start = 20 + 2 * (self.extra_role_pair_round - 1)
                    restored = self.extra_role_pair_round == 1 or (
                        latest is not None
                        and latest["group_id"] == GROUP
                        and latest["action"] == "revoke"
                        and latest["state"] == "verified"
                        and latest["provider_accepted"]
                        and previous.get("extra_role_pair_day")
                        == self.extra_role_pair_day
                        and previous.get("extra_role_pair_round", 1) == 1
                    )
                    if payload["action"] == "grant" and used == start and restored:
                        # Reserve room for the separate confirmed cleanup action.
                        required_writes = 2
                    elif not (
                        payload["action"] == "revoke"
                        and used == start + 1
                        and latest["group_id"] == GROUP
                        and latest["action"] == "grant"
                        and latest["state"] == "verified"
                        and latest["provider_accepted"]
                        and previous.get("extra_role_pair_day")
                        == self.extra_role_pair_day
                        and previous.get("extra_role_pair_round", 1)
                        == self.extra_role_pair_round
                    ):
                        raise ProtocolError("test_lab_role_extension_limit")
                    limit = start + 2
                if used + required_writes > limit:
                    raise ProtocolError("test_lab_daily_write_limit")
                if (
                    db.execute(
                        "UPDATE previews SET used=1 WHERE id=? AND used=0 AND expires>?",
                        (identity, time.time()),
                    ).rowcount
                    != 1
                ):
                    raise ProtocolError("test_lab_confirmation")
                db.execute(
                    "INSERT INTO attempts(id,group_id,action,created,state,payload) "
                    "VALUES(?,?,?,?,'unknown',?)",
                    (
                        identity,
                        self.group,
                        payload["action"],
                        time.time(),
                        json.dumps(payload),
                    ),
                )
            action = payload["action"]
            if action == "accept":
                params = {"applyId": payload["invitation"]["id"], "agree": True}
            elif action == "grant":
                params = {"groupId": int(self.group), "groupManagerIds": [int(PEER)]}
            elif action == "revoke":
                params = {"groupId": int(self.group), "userId": int(PEER)}
            elif action in OWNERSHIP_ACTIONS:
                params = {"groupId": int(self.group), "userId": int(payload["target"])}
            else:
                params = {"groupId": int(self.group), "groupMemberIds": [int(PEER)]}
            self.submitting = {
                "id": identity,
                "payload": payload,
                "expires": preview["expires"],
                "request": (payload["actor"], ROUTES[action], params),
                "submission": {"started": False},
            }
            accepted = False
            try:
                await self.exchange(payload["actor"], ROUTES[action], params)
                accepted = True
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(
                        "UPDATE attempts SET state='accepted',provider_accepted=1 "
                        "WHERE id=?",
                        (identity,),
                    )
            except BaseException as exc:
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(
                        "UPDATE attempts SET state=?,error=?,"
                        "provider_accepted=MAX(provider_accepted,?) WHERE id=?",
                        (
                            "unknown"
                            if self.submitting["submission"]["started"]
                            else "not_submitted",
                            type(exc).__name__,
                            accepted
                            or self.submitting["submission"].get("accepted", False),
                            identity,
                        ),
                    )
                raise
            finally:
                self.submitting = None
            result = await self.observe(identity)
            if self.protection(self.group) != payload["protected"]:
                raise ProtocolError("test_lab_protected_state_changed")
            return result

    async def observe(self, identity):
        """Reconcile existing intent using reads only; never repeat a mutation.

        Args:
            identity: Existing attempt ID in this group's private journal.

        Returns:
            Exact status, evidence and explicit remaining acceptance requirements.

        Raises:
            ProtocolError: If the attempt is outside this group.
        """
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM attempts WHERE id=? AND group_id=?",
                (identity, self.group),
            ).fetchone()
            if not row:
                raise ProtocolError("test_lab_attempt_scope")
            attempt = dict(row)
        if attempt["state"] == "verified":
            return {
                "attempt": identity,
                "action": attempt["action"],
                "group": self.group,
                "state": "verified",
                "provider_accepted": bool(attempt["provider_accepted"]),
                "evidence": json.loads(attempt["evidence"]),
                "historical_receipt": True,
                "test_only": True,
                "activation_accepted": False,
                "phone_recall_confirmed": False,
            }
        roster = await member_directory(self, self.group)
        before = json.loads(attempt["payload"])
        ownership = attempt["action"] in OWNERSHIP_ACTIONS
        expected = {m[0]: (m[1], m[2]) for m in before["members"]}
        if attempt["action"] == "remove":
            expected.pop(PEER)
        elif ownership:
            # The platform may retain an administrator role for the previous owner.
            # Accept only the exact owner swap and an otherwise unchanged roster.
            expected[before["target"]] = (
                self.pins[before["target"]],
                "GROUP_ROLE_OWNER",
            )
        else:
            expected[PEER] = (
                self.pins[PEER],
                "GROUP_ROLE_ADMIN"
                if attempt["action"] == "grant"
                else "GROUP_ROLE_MEMBER",
            )
        members = {
            str(r["userId"]): (r["nimId"], r.get("groupRole"))
            for r in roster["groupMemberInfo"]
        }
        good = all(
            r.get("accountState") == "ACCOUNT_STATE_GOOD"
            for r in roster["groupMemberInfo"]
        )
        proof = None
        state = "unknown"
        evidence = {}
        if attempt["action"] in {"invite", "accept"}:
            source = attempt
            if attempt["action"] == "accept":
                with closing(sqlite3.connect(self.path)) as db:
                    db.row_factory = sqlite3.Row
                    source = dict(
                        db.execute(
                            "SELECT * FROM attempts WHERE id=?",
                            (before["invitation"]["attempt"],),
                        ).fetchone()
                    )
            proof = await self.invitation(source)
            state = (
                "verified"
                if good
                and members == expected
                and proof["state"] == "MEMBER_STATE_GOOD"
                else "pending"
                if (
                    good
                    and attempt["action"] == "invite"
                    and members
                    == {uid: value for uid, value in expected.items() if uid != PEER}
                    and proof["state"] == "MEMBER_STATE_INVITED"
                )
                else "unknown"
            )
        elif ownership:
            previous = before["actor"]
            previous_role = members.get(previous, ("", ""))[1]
            expected[previous] = (self.pins[previous], previous_role)
            evidence = {
                "roles": {uid: role for uid, (_, role) in members.items()},
                "restoration_required": attempt["action"] == "transfer-owner",
            }
            if before["ownership"]:
                evidence["transfer_attempt"] = before["ownership"]["attempt"]
                evidence["role_restore_required"] = (
                    before["ownership"]["original_roles"][PEER]
                    != members.get(PEER, ("", ""))[1]
                )
            if (
                good
                and previous_role in {"GROUP_ROLE_MEMBER", "GROUP_ROLE_ADMIN"}
                and members == expected
                and attempt["provider_accepted"]
            ):
                state = "verified"
        elif good and members == expected and attempt["provider_accepted"]:
            state = "verified"
        if attempt["state"] == "not_submitted":
            state = "not_submitted"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE attempts SET state=?,apply_id=?,evidence=? WHERE id=?",
                (
                    state,
                    proof["id"] if proof else "",
                    json.dumps(evidence),
                    identity,
                ),
            )
            if state == "verified" and attempt["action"] == "accept":
                db.execute(
                    "UPDATE attempts SET state='verified',apply_id=? WHERE id=?",
                    (proof["id"], before["invitation"]["attempt"]),
                )
        return {
            "attempt": identity,
            "action": attempt["action"],
            "group": self.group,
            "actor": before["actor"],
            "target": before["target"],
            "state": state,
            "provider_accepted": bool(attempt["provider_accepted"]),
            "invitation": proof,
            "evidence": evidence,
            "test_only": True,
            "activation_accepted": False,
            "phone_recall_confirmed": False,
        }

    async def reconcile(self, identity):
        """Read an existing result under the same exclusive scope lock.

        Args:
            identity: Existing attempt ID, not a new operation request.

        Returns:
            Read-only platform observations and private journal reconciliation.
        """
        with self.locked():
            self.protection(self.group)
            return await self.observe(identity)


async def main():
    """Run a local preview or explicitly confirmed test operation, never log in anew."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True)
    parser.add_argument("--action", choices=tuple(ROUTES))
    parser.add_argument("--first", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preview")
    parser.add_argument("--reconcile")
    parser.add_argument(
        "--daily-write-limit",
        type=int,
        choices=(10, 20),
        default=10,
        help="Shared journal limit; never resets existing attempts (default: 10)",
    )
    parser.add_argument(
        "--extra-role-pair-day",
        default="",
        help="Explicit approved UTC date for one additional fixed-group grant/revoke pair",
    )
    parser.add_argument(
        "--extra-role-pair-round",
        type=int,
        choices=(1, 2),
        default=1,
        help="Round 2 needs separate approval and the verified restoration of round 1",
    )
    args = parser.parse_args()
    if (
        args.apply
        and (not args.preview or args.action or args.first or args.reconcile)
        or args.reconcile
        and (args.action or args.first or args.preview or args.apply)
        or not args.apply
        and not args.reconcile
        and (not args.action or args.preview)
    ):
        parser.error(
            "Choose an action preview, --apply --preview ID, or --reconcile ID."
        )
    if args.group in FORMAL_GROUPS:
        parser.error("Formal groups cannot be used for acceptance.")
    pins, clients = {}, {}
    async with aiohttp.ClientSession() as http:
        deployment = Deployment.load()
        for account, instance in INSTANCES.items():
            saved = Vault(instance).load()
            if str(saved["business"]["uid"]) != account:
                raise ProtocolError("test_lab_vault_identity")
            pins[account] = str(saved["nim_id"])
            if account != ORDINARY:
                client = BusinessClient(deployment, http)
                client.restore(saved["business"])
                clients[account] = client
        lab = TestLab(
            clients,
            pins,
            args.group,
            CONTROL,
            protection=lambda group, first=False: protected_state(
                ROOT, group, first=first
            ),
            daily_write_limit=args.daily_write_limit,
            extra_role_pair_day=args.extra_role_pair_day,
            extra_role_pair_round=args.extra_role_pair_round,
        )
        if args.apply:
            token = getpass.getpass("Preview confirmation: ")
            result = await lab.execute(args.preview, token)
        elif args.reconcile:
            result = await lab.reconcile(args.reconcile)
        else:
            result = await lab.preview(args.action, first=args.first)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
