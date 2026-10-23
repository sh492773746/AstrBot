"""Persisted card previews and serial, opt-in normalization jobs."""

import asyncio
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

import regex

from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.directory import member_card
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .observability import record

RULE_VERSION = 1
MAX_ACTIVE_JOBS = 16


class CardJobs:
    """Own card policy and persisted work without bypassing adapter capabilities."""

    def __init__(self, context=None):
        self.context = context
        self.locks = {}
        self.tasks = set()
        self.slots = set()
        self.closed = False
        self.last_write = {}

    def reserve(self, adapter, job_id):
        """Bound new and recovered workers before their first database wait.

        Args:
            adapter: Original runtime adapter and authenticated account.
            job_id: Persisted job identifier.

        Returns:
            Exact reserved runtime/account/job key.

        Raises:
            ProtocolError: If stopped, already admitted or at capacity.
        """
        key = (id(adapter), adapter.account, job_id)
        if self.closed:
            raise ProtocolError("card_service_closed")
        if key in self.slots:
            raise ProtocolError("card_job_busy")
        if len(self.slots) >= MAX_ACTIVE_JOBS:
            raise ProtocolError("card_queue_full")
        self.slots.add(key)
        return key

    def launch(self, adapter, job, owner, key):
        """Release a reserved worker slot even when execution is cancelled.

        Args:
            adapter: Original runtime adapter.
            job: Persisted queued job.
            owner: Server-derived caller binding.
            key: Previously reserved runtime/account/job key.

        Raises:
            ProtocolError: If shutdown began before worker creation.
        """
        if self.closed:
            self.slots.discard(key)
            raise ProtocolError("card_service_closed")

        async def worker():
            try:
                await self.run(adapter, job["id"], owner)
            finally:
                self.slots.discard(key)

        task = asyncio.create_task(
            worker(),
            name=f"wsl-card/{adapter.account}/{job['group']}/{job['id']}",
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda _: self.slots.discard(key))

    def check_private_owner(self, adapter, owner):
        """Revalidate server-bound private callers, including resumed jobs."""
        if not owner.startswith("["):
            return False
        try:
            identity = json.loads(owner)
            if (
                not isinstance(identity, list)
                or len(identity) != 3
                or any(not isinstance(part, str) or not part for part in identity)
                or identity[0] != adapter.account
                or self.context is None
                or identity[1]
                not in {
                    str(uid)
                    for uid in self.context.get_config(identity[2]).get("admins_id", [])
                }
            ):
                raise ValueError("invalid private owner")
        except (ValueError, TypeError, AttributeError):
            raise ProtocolError("card_private_admin_required") from None
        return True

    @contextmanager
    def database(self, adapter):
        """Open and incrementally initialize instance storage.

        Args:
            adapter: Native adapter owning the data directory.

        Yields:
            SQLite connection with initialized tables.
        """
        db = self.connect(adapter)
        try:
            with db:
                yield db
        finally:
            db.close()

    def connect(self, adapter):
        """Create a connection on the transaction's owning thread.

        Args:
            adapter: Native adapter owning the existing ledger.

        Returns:
            Initialized SQLite connection owned by the caller.
        """
        root = instance_dir(adapter.config["id"])
        root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(root / "cards.sqlite3")
        db.execute(
            "CREATE TABLE IF NOT EXISTS card_jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS card_jobs_scope ON card_jobs("
            "json_extract(data,'$.account'),json_extract(data,'$.group'),"
            "json_extract(data,'$.state')) WHERE json_valid(data)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS card_jobs_active ON card_jobs("
            "json_extract(data,'$.account'),json_extract(data,'$.state')) WHERE json_valid(data)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS card_names (identity TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        db.execute("CREATE TABLE IF NOT EXISTS card_seen (identity TEXT PRIMARY KEY)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS card_rules "
            "(identity TEXT PRIMARY KEY, base TEXT NOT NULL, suffix TEXT NOT NULL)"
        )
        return db

    def active_jobs(self, adapter, group=None, *, after="", limit=None):
        """Read only resumable jobs using the account/group/state index.

        Args:
            adapter: Original native account.
            group: Optional group filter for the periodic scan.
            after: Exclusive persisted job ID for bounded recovery pagination.
            limit: Optional maximum number of jobs to load.

        Returns:
            Persisted active jobs without loading unrelated history.
        """
        with self.database(adapter) as db:
            rows = db.execute(
                "SELECT data FROM card_jobs WHERE json_valid(data) "
                "AND json_extract(data,'$.account')=? "
                + ("AND json_extract(data,'$.group')=? " if group is not None else "")
                + "AND json_extract(data,'$.state') IN ('queued','running','needs_review') "
                + "AND id>? ORDER BY id "
                + ("LIMIT ?" if limit is not None else ""),
                (
                    *(
                        (adapter.account, group)
                        if group is not None
                        else (adapter.account,)
                    ),
                    after,
                    *((limit,) if limit is not None else ()),
                ),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    async def preview(
        self,
        adapter,
        group: str,
        owner: str,
        *,
        automatic=False,
        member=None,
        card_name=None,
        expected_nim=None,
        cleanup=False,
        cleanup_limit=None,
        cleanup_state=None,
    ):
        """Freeze a complete roster into a caller-bound, expiring preview.

        Args:
            adapter: Native adapter.
            group: Explicitly enabled business group ID.
            owner: Server-derived caller and session identity.
            automatic: Skip members already handled by an automatic scan.
            member: Optional single business member ID from a verified directory.
            card_name: Optional explicit card for that single member, including restoration.
            expected_nim: Frozen identity from a private caller's member snapshot.
            cleanup: Dashboard-only removal of banned and cancelled members.
            cleanup_limit: Optional positive maximum number of preview targets.
            cleanup_state: Optional exact platform account state filter.

        Returns:
            Persisted preview including original and proposed cards.

        Raises:
            ProtocolError: If authorization or directory completeness is missing.
        """
        if (cleanup_limit is not None or cleanup_state is not None) and (
            not cleanup
            or (
                cleanup_limit is not None
                and (type(cleanup_limit) is not int or not 1 <= cleanup_limit <= 1000)
            )
            or (
                cleanup_state is not None
                and cleanup_state
                not in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
            )
        ):
            raise ProtocolError("cleanup_arguments")
        if cleanup and (
            automatic
            or not owner.startswith("dashboard/")
            or member is not None
            or card_name is not None
        ):
            raise ProtocolError("cleanup_arguments")
        private_owner = self.check_private_owner(adapter, owner)
        if member is not None or card_name is not None:
            if (
                automatic
                or not (owner.startswith("dashboard/") or private_owner)
                or (
                    private_owner
                    and (not isinstance(expected_nim, str) or not expected_nim)
                )
                or not isinstance(member, str)
                or not member.isascii()
                or not member.isdigit()
                or int(member) <= 0
                or not isinstance(card_name, str)
                or not card_name.strip()
                or len(card_name.encode()) > 256
            ):
                raise ProtocolError("card_preview_arguments")
        authorize_action(adapter.config, group, "cleanup" if cleanup else "rename")
        if automatic and not adapter.config.get("moderation", {}).get(
            "card_auto", {}
        ).get(group, False):
            raise ProtocolError("card_automation_disabled")
        account = adapter.account

        def authorize():
            self.check_private_owner(adapter, owner)
            authorize_action(adapter.config, group, "cleanup" if cleanup else "rename")
            if automatic and not adapter.config.get("moderation", {}).get(
                "card_auto", {}
            ).get(group, False):
                raise ProtocolError("card_automation_disabled")

        db_guard = account_guard(adapter, authorize)
        roster = await adapter.get_moderation_members(group)
        self.check_private_owner(adapter, owner)
        if not roster.get("complete") or adapter.account != account:
            raise ProtocolError("member_pagination_incomplete")
        members = roster["groupMemberInfo"]
        if member is not None and expected_nim is not None:
            targets = [m for m in members if str(m.get("userId")) == member]
            if len(targets) != 1 or str(targets[0].get("nimId")) != expected_nim:
                raise ProtocolError("card_preview_stale")
        own = [m for m in members if str(m["userId"]) == account]
        if len(own) != 1 or own[0].get("groupRole") not in {
            "GROUP_ROLE_OWNER",
            "GROUP_ROLE_ADMIN",
        }:
            raise ProtocolError("moderation_permission")
        occupied = {member_card(m).strip() for m in members}
        job = {
            "id": uuid.uuid4().hex,
            "account": account,
            "group": group,
            "owner": owner,
            "expires": time.time() + 600,
            "state": "preview",
            "automatic": automatic,
            "version": RULE_VERSION,
            "items": [],
            "kind": "cleanup" if cleanup else "rename",
        }
        if not cleanup and card_name is None:
            job["suffix"] = (
                adapter.config.get("moderation", {})
                .get("card_suffix", {})
                .get(group, "")
            )
        async with AsyncDatabase(
            lambda: self.connect(adapter), write=True, guard=db_guard
        ) as db:
            prior = [
                json.loads(row[0])
                for row in (
                    await db.fetchall(
                        "SELECT data FROM card_jobs WHERE json_valid(data) "
                        "AND json_extract(data,'$.account')=? AND json_extract(data,'$.group')=?",
                        (account, group),
                    )
                )
            ]
            if any(
                j["account"] == account
                and j["group"] == group
                and j.get("kind", "rename") == job["kind"]
                and any(
                    i["state"] in {"accepted", "unknown"} and not i.get("quarantined")
                    for i in j["items"]
                )
                for j in prior
            ):
                raise ProtocolError("card_result_unresolved")
            occupied.update(
                row[0] for row in (await db.fetchall("SELECT name FROM card_names"))
            )
            quarantined = {
                i["member"]
                for j in prior
                if j["account"] == account and j["group"] == group
                for i in j["items"]
                if i.get("quarantined")
            }
            job["excluded_accounts"] = 0
            for m in members:
                uid, nim = str(m["userId"]), str(m["nimId"])
                if cleanup:
                    if (
                        (cleanup_limit is None or len(job["items"]) < cleanup_limit)
                        and (
                            cleanup_state is None
                            or m.get("accountState") == cleanup_state
                        )
                        and uid != account
                        and not is_managed_account(uid)
                        and m.get("groupRole") == "GROUP_ROLE_MEMBER"
                        and m.get("accountState")
                        in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
                    ):
                        job["items"].append(
                            {
                                "member": uid,
                                "nim": nim,
                                "original": member_card(m)
                                or str(m.get("userNick", "")),
                                "name": "",
                                "account_state": m["accountState"],
                                "identity": json.dumps([account, group, uid, nim]),
                                "state": "pending",
                            }
                        )
                    continue
                if member is not None and uid != member:
                    continue
                if (
                    m.get("accountState")
                    in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
                    or uid in quarantined
                ):
                    job["excluded_accounts"] += 1
                    continue
                if (
                    uid == account
                    or is_managed_account(uid)
                    or m.get("groupRole") != "GROUP_ROLE_MEMBER"
                ):
                    continue
                identity = json.dumps([account, group, uid, nim])
                previous_rule = await db.fetchone(
                    "SELECT base,suffix FROM card_rules WHERE identity=?", (identity,)
                )
                if (
                    automatic
                    and (previous_rule[1] if previous_rule else "") == job["suffix"]
                    and (
                        await db.fetchone(
                            "SELECT 1 FROM card_seen WHERE identity=?", (identity,)
                        )
                    )
                ):
                    continue
                original = member_card(m)
                source = (
                    original.strip()
                    or str(
                        m.get("userNick")
                        or m.get("nickname")
                        or m.get("userName")
                        or m.get("name")
                        or ""
                    ).strip()
                )
                saved = await db.fetchone(
                    "SELECT name FROM card_names WHERE identity=?", (identity,)
                )
                clusters = [
                    c
                    for c in regex.findall(r"\X", source)
                    if regex.search(r"[\p{L}\p{N}\p{P}\p{S}]", c)
                ]
                if card_name is not None:
                    name = card_name
                elif saved and original.strip() == saved[0]:
                    name = saved[0]
                elif previous_rule and original == "".join(previous_rule):
                    name = previous_rule[0]
                elif clusters:
                    name = "".join(clusters[:2])
                elif saved:
                    name = saved[0]
                    if any(
                        str(other["userId"]) != uid
                        and member_card(other).strip() == name
                        for other in members
                    ):
                        raise ProtocolError("card_name_collision")
                else:
                    for _ in range(1000):
                        name = f"大海群员{secrets.randbelow(1000000):06d}"
                        if name + job.get("suffix", "") not in occupied:
                            break
                    else:
                        raise ProtocolError("card_name_collision")
                    (
                        await db.execute(
                            "INSERT INTO card_names VALUES (?,?)", (identity, name)
                        )
                    )
                base = name
                if card_name is None:
                    name += job["suffix"]
                if len(name.encode()) > 256:
                    raise ProtocolError("card_name_too_long")
                occupied.add(name)
                job["items"].append(
                    {
                        "member": uid,
                        "nim": nim,
                        "original": original,
                        "name": name,
                        "identity": identity,
                        "state": "unchanged" if original == name else "pending",
                        **({"base": base} if card_name is None else {}),
                    }
                )
            if member is not None and len(job["items"]) != 1:
                raise ProtocolError("moderation_target")
            if automatic and not job["items"]:
                job["state"] = "completed"
            else:
                (
                    await db.execute(
                        "INSERT INTO card_jobs VALUES (?,?)",
                        (job["id"], json.dumps(job)),
                    )
                )
        return job

    async def status(self, adapter, job_id: str, owner: str):
        """Read a preview only for the original login and caller.

        Args:
            adapter: Current native adapter.
            job_id: Server-created preview ID.
            owner: Authenticated caller/session identity.

        Returns:
            Persisted job data.

        Raises:
            ProtocolError: If the caller, login or preview is invalid.
        """
        db_guard = account_guard(adapter)
        async with AsyncDatabase(
            lambda: self.connect(adapter), write=False, guard=db_guard
        ) as db:
            row = await db.fetchone("SELECT data FROM card_jobs WHERE id=?", (job_id,))
        job = json.loads(row[0]) if row else {}
        if job.get("account") != adapter.account or job.get("owner") != owner:
            raise ProtocolError("card_preview_invalid")
        return job

    async def refresh_status(self, adapter, job_id: str, owner: str):
        """Read back uncertain cards without issuing another modification.

        Args:
            adapter: Native adapter with the original account.
            job_id: Persisted preview ID.
            owner: Authenticated caller/session identity.

        Returns:
            Latest state; unconfirmed items remain unknown or accepted.

        Raises:
            ProtocolError: If the caller, account or directory is invalid.
        """
        db_guard = account_guard(adapter)
        job = await self.status(adapter, job_id, owner)
        if job["state"] in {"running", "queued"} or not any(
            item["state"] in {"unknown", "accepted"} for item in job["items"]
        ):
            return job
        roster = await adapter.get_moderation_members(job["group"])
        if not roster.get("complete"):
            raise ProtocolError("member_pagination_incomplete")
        db_guard()
        job = await self.status(adapter, job_id, owner)
        if job["state"] in {"running", "queued"}:
            return job
        for item in job["items"]:
            if item["state"] not in {"unknown", "accepted"}:
                continue
            matches = [
                m
                for m in roster["groupMemberInfo"]
                if str(m["userId"]) == item["member"] and str(m["nimId"]) == item["nim"]
            ]
            if (
                job.get("kind") == "cleanup"
                and not any(
                    str(m["userId"]) == item["member"]
                    for m in roster["groupMemberInfo"]
                )
            ) or (
                job.get("kind") != "cleanup"
                and len(matches) == 1
                and member_card(matches[0]) == item["name"]
            ):
                item["state"] = "verified"
                if job.get("kind") != "cleanup":
                    async with AsyncDatabase(
                        lambda: self.connect(adapter), write=True, guard=db_guard
                    ) as db:
                        (
                            await db.execute(
                                "INSERT OR IGNORE INTO card_seen VALUES (?)",
                                (item["identity"],),
                            )
                        )
            elif (
                job.get("kind") != "cleanup"
                and len(matches) == 1
                and matches[0].get("accountState")
                in {
                    "ACCOUNT_STATE_BAN",
                    "ACCOUNT_STATUS_CANCELLED",
                }
            ):
                # Preserve the uncertain outcome; quarantine this target permanently.
                item["quarantined"] = matches[0]["accountState"]
                item["error"] = "card_account_unavailable"
        if all(i["state"] in {"verified", "unchanged"} for i in job["items"]):
            job["state"] = "completed"
        await self.save(adapter, job)
        return job

    async def save(self, adapter, job, *, guard=None):
        """Persist progress without replacing unrelated job records.

        Args:
            adapter: Native adapter owning storage.
            job: Complete updated job record.
            guard: Optional authority check for a new action intent.
        """
        db_guard = guard
        async with AsyncDatabase(
            lambda: self.connect(adapter), write=True, guard=db_guard
        ) as db:
            row = await db.fetchone(
                "SELECT data FROM card_jobs WHERE id=?", (job["id"],)
            )
            if row is None:
                raise ProtocolError("card_preview_invalid")
            current = json.loads(row[0])
            if current.get("version", 0) != job.get("version", 0):
                if not (current.get("stop_requested") or current["state"] == "stopped"):
                    raise ProtocolError("card_version_conflict")
                # A stop may race an in-flight receipt. Merge only result evidence.
                incoming = {item["member"]: item for item in job["items"]}
                for item in current["items"]:
                    evidence = incoming.get(item["member"], {})
                    if item["state"] in {"unknown", "accepted"} and evidence.get(
                        "state"
                    ) in {"verified", "accepted", "rejected"}:
                        item.update(evidence)
                job.clear()
                job.update(current)
            job["stop_requested"] = bool(
                current.get("stop_requested")
                or current["state"] == "stopped"
                or job["state"] == "stopped"
            )
            if job["stop_requested"]:
                job["state"] = "stopped"
            job["version"] = current.get("version", 0) + 1
            (
                await db.execute(
                    "UPDATE card_jobs SET data=? WHERE id=?",
                    (json.dumps(job), job["id"]),
                )
            )
            for item in job["items"]:
                if "base" in item and item["state"] in {"verified", "unchanged"}:
                    (
                        await db.execute(
                            "INSERT INTO card_rules VALUES (?,?,?) "
                            "ON CONFLICT(identity) DO UPDATE SET base=excluded.base,suffix=excluded.suffix",
                            (item["identity"], item["base"], job.get("suffix", "")),
                        )
                    )

    async def start(self, adapter, job_id: str, owner: str):
        """Start a preview once; callers cannot replace its target list.

        Args:
            adapter: Native adapter.
            job_id: Server-created preview ID.
            owner: Authenticated caller/session identity.

        Returns:
            Queued or previously persisted job.

        Raises:
            ProtocolError: If the preview has expired or authorization was revoked.
        """
        db_guard = account_guard(adapter)
        job = await self.status(adapter, job_id, owner)
        db_guard()
        self.check_private_owner(adapter, owner)
        if job["state"] != "preview":
            return job
        if time.time() >= job["expires"]:
            raise ProtocolError("card_preview_expired")
        authorize_action(adapter.config, job["group"], job.get("kind", "rename"))
        if "suffix" in job and job["suffix"] != adapter.config.get(
            "moderation", {}
        ).get("card_suffix", {}).get(job["group"], ""):
            raise ProtocolError("card_rule_changed")
        job["state"] = "queued"

        def authorize():
            db_guard()
            self.check_private_owner(adapter, owner)
            authorize_action(adapter.config, job["group"], job.get("kind", "rename"))
            if self.closed or time.time() >= job["expires"]:
                raise ProtocolError("card_preview_expired")
            if "suffix" in job and job["suffix"] != adapter.config.get(
                "moderation", {}
            ).get("card_suffix", {}).get(job["group"], ""):
                raise ProtocolError("card_rule_changed")

        key = self.reserve(adapter, job_id)
        try:
            await self.save(adapter, job, guard=authorize)
            self.launch(adapter, job, owner, key)
        except BaseException:
            self.slots.discard(key)
            raise
        return job

    async def stop(self, adapter, job_id: str, owner: str):
        """Stop pending items without claiming an in-flight request was cancelled.

        Args:
            adapter: Native adapter.
            job_id: Server-created preview ID.
            owner: Authenticated caller/session identity.

        Returns:
            Persisted stopped job.
        """
        db_guard = account_guard(
            adapter, lambda: self.check_private_owner(adapter, owner)
        )
        job = await self.status(adapter, job_id, owner)
        job["state"] = "stopped"
        await self.save(adapter, job, guard=db_guard)
        return job

    async def run(self, adapter, job_id: str, owner: str):
        """Serialize writes and retain uncertain operations for readback only.

        Args:
            adapter: Original adapter instance, never a replacement login.
            job_id: Persisted job identifier.
            owner: Original server-derived caller binding.
        """
        lock = self.locks.setdefault(adapter.config["id"], asyncio.Lock())
        db_guard = account_guard(adapter)
        async with lock:
            db_guard()
            job = await self.status(adapter, job_id, owner)
            account = adapter.account

            async def check():
                db_guard()
                self.check_private_owner(adapter, owner)
                async with AsyncDatabase(
                    lambda: self.connect(adapter), write=False, guard=db_guard
                ) as db:
                    other_jobs = [
                        json.loads(row[0])
                        for row in (
                            await db.fetchall(
                                "SELECT data FROM card_jobs WHERE json_valid(data) AND id<>? "
                                "AND json_extract(data,'$.account')=? AND json_extract(data,'$.group')=?",
                                (job_id, account, job["group"]),
                            )
                        )
                    ]
                current = await self.status(adapter, job_id, owner)
                self.check_private_owner(adapter, owner)
                authorize_action(
                    adapter.config, job["group"], job.get("kind", "rename")
                )
                if any(
                    j["account"] == account
                    and j["group"] == job["group"]
                    and j.get("kind", "rename") == job.get("kind", "rename")
                    and any(
                        i["state"] in {"accepted", "unknown"}
                        and not i.get("quarantined")
                        for i in j["items"]
                    )
                    for j in other_jobs
                ):
                    raise ProtocolError("card_result_unresolved")
                if (
                    self.closed
                    or adapter.account != account
                    or current["state"] == "stopped"
                    or current.get("stop_requested")
                ):
                    raise ProtocolError("card_job_stopped")
                if job["automatic"] and not adapter.config.get("moderation", {}).get(
                    "card_auto", {}
                ).get(job["group"], False):
                    raise ProtocolError("card_automation_disabled")
                if "suffix" in job and job["suffix"] != adapter.config.get(
                    "moderation", {}
                ).get("card_suffix", {}).get(job["group"], ""):
                    raise ProtocolError("card_rule_changed")

            try:
                await check()
                job["state"] = "running"
                await self.save(adapter, job, guard=db_guard)
                for item in job["items"]:
                    if job.get("kind") != "cleanup" and item["state"] in {
                        "unchanged",
                        "verified",
                    }:
                        async with AsyncDatabase(
                            lambda: self.connect(adapter), write=True, guard=db_guard
                        ) as db:
                            (
                                await db.execute(
                                    "INSERT OR IGNORE INTO card_seen VALUES (?)",
                                    (item["identity"],),
                                )
                            )
                    if item["state"] != "pending":
                        continue
                    delay = 1 - (
                        time.monotonic() - self.last_write.get(adapter.config["id"], 0)
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await check()
                    item["state"] = "unknown"
                    await self.save(adapter, job, guard=db_guard)
                    await check()
                    try:
                        self.last_write[adapter.config["id"]] = time.monotonic()
                        if job.get("kind") == "cleanup":
                            result = await adapter.execute_action(
                                ActionRequest.bind(
                                    adapter,
                                    "cleanup/" + job_id + "/" + item["member"],
                                    "cleanup",
                                    int(job["group"]),
                                    member=int(item["member"]),
                                    expected_nim=item["nim"],
                                ),
                                authorize=check,
                            )
                        else:
                            result = await adapter.execute_action(
                                ActionRequest.bind(
                                    adapter,
                                    "card/" + job_id + "/" + item["member"],
                                    "rename",
                                    int(job["group"]),
                                    member=int(item["member"]),
                                    text=item["name"],
                                    expected_card=item["original"],
                                    expected_nim=item["nim"],
                                ),
                                authorize=check,
                            )
                        item["state"] = result["status"]
                        record(
                            adapter,
                            "card_item",
                            item["state"],
                            job_id,
                            group=job["group"],
                            actor=item["member"],
                            action="cleanup"
                            if job.get("kind") == "cleanup"
                            else "rename",
                            failed=item["state"] == "unknown",
                            error="result_unknown"
                            if item["state"] == "unknown"
                            else "",
                            aggregate=False,
                        )
                    except ProtocolError as exc:
                        record(
                            adapter,
                            "card_item",
                            "rejected",
                            job_id,
                            group=job["group"],
                            actor=item["member"],
                            error=str(exc),
                        )
                        item.update(state="rejected", error=str(exc))
                        if str(exc) not in {
                            "cleanup_target_changed",
                            "card_preview_stale",
                            "card_account_unavailable",
                        }:
                            job["state"] = "stopped"
                            await self.save(adapter, job)
                            return
                    # A stopped job must not be revived by an in-flight result.
                    stopped = (await self.status(adapter, job_id, owner))[
                        "state"
                    ] == "stopped"
                    if stopped:
                        job["state"] = "stopped"
                    await self.save(adapter, job)
                    if item["state"] in {"unknown", "accepted"} or stopped:
                        job["state"] = "stopped" if stopped else "needs_review"
                        await self.save(adapter, job)
                        return
                    if job.get("kind") != "cleanup" and item["state"] == "verified":
                        async with AsyncDatabase(
                            lambda: self.connect(adapter), write=True, guard=db_guard
                        ) as db:
                            (
                                await db.execute(
                                    "INSERT OR IGNORE INTO card_seen VALUES (?)",
                                    (item["identity"],),
                                )
                            )
                job["state"] = (
                    "partial"
                    if any(i["state"] == "rejected" for i in job["items"])
                    else "completed"
                )
                await self.save(adapter, job)
            except Exception as exc:
                job.update(state="stopped", error=type(exc).__name__)
                await self.save(adapter, job)
                record(
                    adapter,
                    "card_job",
                    "stopped",
                    job_id,
                    group=job["group"],
                    failed=True,
                    error=str(exc)
                    if isinstance(exc, ProtocolError)
                    else "operation_failed",
                    aggregate=False,
                )
            finally:
                record(adapter, "card_job", job["state"], job_id, group=job["group"])

    async def close(self):
        """Stop plugin workers without replaying an ambiguous request."""
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def poll(self, context):
        """Recover jobs and inspect complete rosters every five minutes without AI.

        Args:
            context: AstrBot plugin context supplying active native adapters.
        """
        recovery_cursors = {}
        while not self.closed:
            for adapter in list(context.platform_manager.platform_insts):
                if (
                    adapter.meta().name != "wangshangliao"
                    or adapter.connection_state != "online"
                ):
                    continue
                try:
                    identity = (id(adapter), adapter.account)
                    if len(self.slots) < MAX_ACTIVE_JOBS:
                        jobs = await asyncio.to_thread(
                            self.active_jobs,
                            adapter,
                            after=recovery_cursors.get(identity, ""),
                            limit=MAX_ACTIVE_JOBS,
                        )
                        for job in jobs:
                            if len(self.slots) >= MAX_ACTIVE_JOBS:
                                break
                            recovery_cursors[identity] = job["id"]
                            key = (id(adapter), adapter.account, job["id"])
                            if key in self.slots:
                                continue
                            if job["group"] not in adapter.config.get(
                                "enabled_groups", []
                            ):
                                continue
                            if job.get("kind") == "cleanup":
                                if job["account"] == adapter.account and job[
                                    "state"
                                ] in {"queued", "running"}:
                                    job["state"] = "stopped"
                                    await self.save(adapter, job)
                                continue
                            if job["account"] != adapter.account or job[
                                "state"
                            ] not in {"running", "queued", "needs_review"}:
                                continue
                            roster = await adapter.get_moderation_members(job["group"])
                            if not roster.get("complete"):
                                continue
                            job = await self.status(adapter, job["id"], job["owner"])
                            if job.get("stop_requested") or job["state"] not in {
                                "running",
                                "queued",
                                "needs_review",
                            }:
                                continue
                            for item in job["items"]:
                                if item["state"] in {"accepted", "unknown"}:
                                    current = [
                                        m
                                        for m in roster["groupMemberInfo"]
                                        if str(m["userId"]) == item["member"]
                                        and str(m["nimId"]) == item["nim"]
                                    ]
                                    if (
                                        len(current) == 1
                                        and member_card(current[0]) == item["name"]
                                    ):
                                        item["state"] = "verified"
                            unresolved = any(
                                i["state"] in {"accepted", "unknown"}
                                for i in job["items"]
                            )
                            job["state"] = "needs_review" if unresolved else "queued"
                            await self.save(adapter, job)
                            if not unresolved:
                                try:
                                    key = self.reserve(adapter, job["id"])
                                except ProtocolError as exc:
                                    if str(exc) == "card_job_busy":
                                        continue
                                    recovery_cursors[identity] = ""
                                    break
                                self.launch(adapter, job, job["owner"], key)
                        else:
                            if len(jobs) < MAX_ACTIVE_JOBS:
                                recovery_cursors[identity] = ""
                    for group, enabled in (
                        adapter.config.get("moderation", {})
                        .get("card_auto", {})
                        .items()
                    ):
                        if not enabled or not adapter.config.get("moderation", {}).get(
                            "enabled"
                        ):
                            continue
                        jobs = await asyncio.to_thread(
                            self.active_jobs, adapter, group, limit=1
                        )
                        if any(
                            j["account"] == adapter.account
                            and j["group"] == group
                            and j["state"] in {"queued", "running", "needs_review"}
                            for j in jobs
                        ):
                            continue
                        try:
                            job = await self.preview(
                                adapter, group, "automatic", automatic=True
                            )
                            if job["items"]:
                                await self.start(adapter, job["id"], "automatic")
                            record(
                                adapter,
                                "card_scan",
                                "queued" if job["items"] else "completed",
                                group=group,
                                aggregate=False,
                            )
                        except Exception as exc:
                            reason = str(exc) if isinstance(exc, ProtocolError) else ""
                            record(
                                adapter,
                                "card_scan",
                                "failed",
                                group=group,
                                failed=True,
                                error="configuration_conflict"
                                if reason == "database_authority_changed"
                                else "permission_denied"
                                if reason == "card_private_admin_required"
                                else reason
                                if reason
                                in {
                                    "card_rule_changed",
                                    "card_name_too_long",
                                    "card_name_collision",
                                    "card_result_unresolved",
                                    "card_automation_disabled",
                                    "moderation_permission",
                                    "moderation_identity",
                                    "member_pagination_incomplete",
                                }
                                else "validation_failed"
                                if reason in {"card_preview_stale", "moderation_target"}
                                else "operation_failed",
                            )
                except Exception:
                    record(
                        adapter,
                        "card_scan",
                        "failed",
                        failed=True,
                        error="operation_failed",
                    )
            await asyncio.sleep(300)
