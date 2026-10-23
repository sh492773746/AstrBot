"""Opt-in isolated native card check; not a chat or production dashboard test.

Without --apply this module only prints the fixed plan and imports no AstrBot
runtime. An applied run restores existing business credentials, never logs in or
opens NIM, and uses fresh private runtime storage outside production.
"""

import argparse
import asyncio
import copy
import fcntl
import hashlib
import inspect
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CONTROL = Path("/opt/backups/wsl-three-card-live")
OWNER, ADMIN, MEMBER = "31846829", "43506527", "23691273"
GROUP = "1459282"
BINDINGS = {
    OWNER: "wangshangliao_0acde7ec",
    ADMIN: "wangshangliao_f79c5b81",
    MEMBER: "wangshangliao_6541e09d",
}
ROLES = dict(
    zip(BINDINGS, ("GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN", "GROUP_ROLE_MEMBER"))
)
LIST = "/v1/group/get-group-list"
MEMBERS = "/v1/group/get-group-members"
RENAME = "/v1/group/set-member-nickname"
STATUS = "/api/v1/plugins/extensions/wangshangliao_moderation/open-service"
HTTP_GAP = 3.2
WRITE_GAP = 30.0
DAILY_CAP = 20


def require(condition, reason):
    """Reject incomplete evidence without exposing upstream exception contents."""
    if not condition:
        raise RuntimeError(reason)


def checked_accounts(state):
    """Require the exact three already-online bindings; never repair their roles."""
    accounts = state.get("accounts", [])
    require(
        len(accounts) == 3
        and {str(a.get("account")) for a in accounts} == set(BINDINGS),
        "exact_three_accounts_required",
    )
    require(
        all(
            a.get("state") == "online"
            and a.get("instance_id") == BINDINGS[str(a["account"])]
            for a in accounts
        ),
        "online_binding_changed",
    )


def checked_roster(page, pins):
    """Validate complete native roles, identities and good account states."""
    rows = page.get("groupMemberInfo", [])
    require(
        page.get("complete") is True
        and len(rows) == 3
        and {str(m.get("userId")) for m in rows} == set(BINDINGS)
        and set(pins) == set(BINDINGS)
        and len(set(pins.values())) == 3,
        "exact_native_roster_required",
    )
    require(
        all(
            str(m.get("nimId")) == pins[str(m["userId"])]
            and m.get("groupRole") == ROLES[str(m["userId"])]
            and m.get("accountState") == "ACCOUNT_STATE_GOOD"
            for m in rows
        ),
        "native_role_or_identity_changed",
    )
    return {str(m["userId"]): m for m in rows}


class Evidence:
    """Keep one private run plus cross-run unknown and rolling daily admission."""

    def __init__(self, control):
        """Create fresh evidence while excluding concurrent script processes.

        Args:
            control: Dedicated control directory, never a production directory.

        Raises:
            RuntimeError: If a previous write has not been verified restored.
            BlockingIOError: If another card-check process owns the lock.
        """
        control = Path(control)
        require(not control.is_symlink(), "unsafe_control_directory")
        control.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(control.stat().st_mode & 0o077 == 0, "private_directory_required")
        lock_fd = os.open(
            control / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(lock_fd, 0o600)
        self.lock = os.fdopen(lock_fd, "a+")
        self.db = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = control / "attempts.sqlite3"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            path.chmod(0o600)
            self.db = sqlite3.connect(path)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(
                "CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,state TEXT);"
                "CREATE TABLE IF NOT EXISTS attempts("
                "operation TEXT PRIMARY KEY,run TEXT,phase TEXT,state TEXT,created REAL);"
            )
            require(
                self.db.execute(
                    "SELECT 1 FROM runs r WHERE state<>'restored' "
                    "AND EXISTS(SELECT 1 FROM attempts a WHERE a.run=r.id) LIMIT 1"
                ).fetchone()
                is None,
                "prior_run_requires_manual_review_no_replay",
            )
            self.run = uuid.uuid4().hex
            self.root = control / self.run
            self.root.mkdir(mode=0o700)
            self.db.execute("INSERT INTO runs VALUES(?,'preview')", (self.run,))
            self.db.commit()
            self.report = {
                "run": self.run,
                "group": GROUP,
                "bindings": BINDINGS,
                "boundary": "isolated CardJobs/action-port/native HTTP, not chat",
                "extra_nim_connections": 0,
                "role_writes": 0,
                "daily_cap": DAILY_CAP,
                "daily_cap_scope": "this script only; TestLab does not count rename",
                "previews": [],
                "receipts": [],
                "state": "preview",
            }
            self.save()
        except BaseException:
            self.close()
            raise

    def save(self):
        """Persist sanitized evidence, keeping both old and new cards private."""
        temporary = self.root / "evidence.tmp"
        fd = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as stream:
            json.dump(self.report, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.root / "evidence.json")

    def reserve(self, operation, phase):
        """Commit an unknown intent before IO; attempts, not successes, use quota."""
        require(phase in {"change", "restore"}, "invalid_phase")
        require(
            self.db.execute(
                "SELECT count(*) FROM attempts WHERE run=?", (self.run,)
            ).fetchone()[0]
            < 2,
            "two_write_limit",
        )
        require(
            self.db.execute(
                "SELECT count(*) FROM attempts WHERE created>=?",
                (time.time() - 86400,),
            ).fetchone()[0]
            < DAILY_CAP,
            "rolling_daily_card_cap",
        )
        with self.db:
            self.db.execute(
                "INSERT INTO attempts VALUES(?,?,?,'unknown',?)",
                (operation, self.run, phase, time.time()),
            )
            self.db.execute(
                "UPDATE runs SET state='incomplete' WHERE id=?", (self.run,)
            )

    def verified(self, operation):
        """Mark only an independently read-back operation as verified."""
        with self.db:
            self.db.execute(
                "UPDATE attempts SET state='verified' WHERE operation=?", (operation,)
            )

    def restored(self):
        """Release cross-run admission only after the restoration readback."""
        with self.db:
            self.db.execute("UPDATE runs SET state='restored' WHERE id=?", (self.run,))

    def close(self):
        """Release script-only resources without touching any runtime service."""
        if self.db is not None:
            self.db.close()
            self.db = None
        self.lock.close()


class PacedIO:
    """Serialize dashboard and business HTTP without retries."""

    def __init__(self, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = asyncio.Lock()
        self.last = None

    async def call(self, callback):
        """Leave at least HTTP_GAP seconds after every completed HTTP attempt."""
        async with self.lock:
            if self.last is not None:
                await self.sleep(max(0, self.last + HTTP_GAP - self.clock()))
            try:
                return await callback()
            finally:
                self.last = self.clock()


class NativeTransport:
    """Allow fixed reads and precisely one armed write per real card preview."""

    def __init__(self, business, status, pins, evidence, pace):
        self.business, self.status = business, status
        self.pins, self.evidence, self.pace = dict(pins), evidence, pace
        self.fields = business.fields
        self.last_write = None
        self.armed = None
        self.write_lock = asyncio.Lock()

    def identity(self):
        """Keep the restored business identity fixed even after HTTP lock waits."""
        require(
            self.business.fields is self.fields and str(self.fields[3]) == ADMIN,
            "business_identity_changed",
        )

    async def guard(self):
        """Read current online bindings; never create or reconnect an adapter."""
        self.identity()
        checked_accounts(await self.pace.call(self.status))

    async def roster(self):
        """Read the actual native roster using the production pagination parser."""
        from astrbot.core.platform.sources.wangshangliao.directory import (
            member_directory,
        )

        page = await member_directory(self, GROUP)
        return checked_roster(page, self.pins)

    async def preflight(self):
        """Require native ADMIN in both group-list and exact member evidence."""
        await self.guard()
        groups = await self.request(LIST, {"v": "0"})
        targets = [
            g
            for g in groups.get("owner", []) + groups.get("member", [])
            if str(g.get("groupId")) == GROUP
        ]
        require(
            len(targets) == 1
            and targets[0].get("me", {}).get("role") == "GROUP_ROLE_ADMIN",
            "native_admin_required_no_role_repair",
        )
        return await self.roster()

    def arm(self, job, phase):
        """Freeze the exact single preview; never permit an arbitrary rename."""
        require(self.armed is None, "write_already_armed")
        item = job["items"][0]
        require(
            job["account"] == ADMIN
            and job["group"] == GROUP
            and len(job["items"]) == 1
            and item["member"] == MEMBER
            and item["nim"] == self.pins[MEMBER]
            and item["state"] == "pending",
            "preview_scope_changed",
        )
        self.armed = (copy.deepcopy(job), phase, False)

    async def request(self, route, params, *, authorize=None):
        """Enforce route, pacing, quota and no-replay at the upstream IO boundary."""
        self.identity()
        read = (route == LIST and params == {"v": "0"}) or (
            route == MEMBERS
            and params.get("groupId") == int(GROUP)
            and params.get("v") == "0"
            and not set(params) - {"groupId", "v", "cursor"}
        )

        async def check():
            self.identity()
            if not read:
                require(
                    self.armed is not None and time.time() < self.armed[0]["expires"],
                    "card_preview_expired_before_submission",
                )
            if authorize is not None:
                value = authorize()
                if inspect.isawaitable(value):
                    await value
            self.identity()

        async def send():
            await check()
            from astrbot.core.platform.sources.wangshangliao.business import (
                BusinessError,
            )

            try:
                return await self.business.request(route, params, authorize=check)
            except BusinessError as exc:
                self.evidence.report.setdefault("provider_errors", []).append(
                    {
                        "route": route,
                        "code": str(exc),
                        "number": exc.number,
                        "submitted": getattr(exc, "submitted", None),
                    }
                )
                self.evidence.save()
                raise

        if read:
            return await self.pace.call(send)
        require(route == RENAME and self.armed is not None, "native_route_forbidden")
        async with self.write_lock:
            job, phase, used = self.armed
            item = job["items"][0]
            require(
                not used
                and params
                == {
                    "groupId": int(GROUP),
                    "userId": int(MEMBER),
                    "nick": item["name"],
                },
                "native_write_unarmed_or_replayed",
            )
            if self.last_write is not None:
                await self.pace.sleep(
                    max(0, self.last_write + WRITE_GAP - self.pace.clock())
                )
            roster = await self.preflight()
            from astrbot.core.platform.sources.wangshangliao.directory import (
                member_card,
            )

            require(
                member_card(roster[MEMBER]) == item["original"],
                "native_card_changed_since_preview",
            )
            await check()
            operation = "card/" + job["id"] + "/" + MEMBER
            self.evidence.reserve(operation, phase)
            self.armed = (job, phase, True)
            try:
                return await self.pace.call(send)
            finally:
                self.last_write = self.pace.clock()


class CardCheck:
    """Use real CardJobs, action-port dispatch and native rename implementation."""

    def __init__(self, transport, evidence):
        """Bind a synthetic adapter only inside the fresh isolated runtime.

        Args:
            transport: Fixed, paced business HTTP wrapper.
            evidence: Script-only private evidence and attempt journal.

        Raises:
            RuntimeError: If application state or a storage path is not isolated.
        """
        from astrbot.builtin_stars.wangshangliao_moderation.cards import CardJobs
        from astrbot.core import astrbot_config
        from astrbot.core.platform.sources.wangshangliao.adapter import (
            WangshangliaoAdapter,
        )
        from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        runtime = evidence.root / "runtime"
        require(
            Path(get_astrbot_data_path()).resolve() == (runtime / "data").resolve()
            and not astrbot_config.get("platform"),
            "fresh_isolated_root_required",
        )
        self.storage = instance_dir(BINDINGS[ADMIN])
        require(
            self.storage.resolve().is_relative_to(runtime.resolve()),
            "isolated_card_storage_required",
        )
        self.storage.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in ("cards.sqlite3", "moderation.sqlite3"):
            fd = os.open(
                self.storage / name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            os.close(fd)
        self.transport, self.evidence = transport, evidence
        self.jobs = CardJobs()
        self.owner = "dashboard/isolated-card-check/" + evidence.run
        self.adapter = SimpleNamespace(
            account=ADMIN,
            config={
                "id": BINDINGS[ADMIN],
                "enable": True,
                "_business_active": True,
                "_business_epoch": evidence.run,
                "enabled_groups": [GROUP],
                "moderation": {
                    "enabled": True,
                    "permissions": {GROUP: ["rename"]},
                },
            },
            stopping=asyncio.Event(),
            connection_state="online",
            send_lock=asyncio.Lock(),
            business=transport,
        )

        async def members(group):
            require(group == GROUP, "group_outside_fixed_scope")
            return {
                "complete": True,
                "groupMemberInfo": list((await transport.roster()).values()),
            }

        self.adapter.get_moderation_members = members
        for method in ("execute_action", "rename_member"):
            setattr(
                self.adapter,
                method,
                getattr(WangshangliaoAdapter, method).__get__(self.adapter),
            )

    async def preview(self, original, name):
        """Persist and verify an explicit single-target production card preview."""
        job = await self.jobs.preview(
            self.adapter,
            GROUP,
            self.owner,
            member=MEMBER,
            card_name=name,
            expected_nim=self.transport.pins[MEMBER],
        )
        require(
            len(job["items"]) == 1
            and job["items"][0]["original"] == original
            and job["items"][0]["name"] == name
            and job["items"][0]["state"] == "pending",
            "preview_card_comparison_failed",
        )
        self.evidence.report["previews"].append(job)
        self.evidence.save()
        return job

    async def execute(self, job, phase):
        """Start once and require durable receipt plus fresh exact native readback."""
        self.transport.arm(job, phase)
        try:
            await self.jobs.start(self.adapter, job["id"], self.owner)
            tasks = tuple(self.jobs.tasks)
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=240)
            result = await self.jobs.status(self.adapter, job["id"], self.owner)
            operation = "card/" + job["id"] + "/" + MEMBER
            with sqlite3.connect(
                (self.storage / "moderation.sqlite3").as_uri() + "?mode=ro", uri=True
            ) as db:
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
                ).fetchone()
                row = (
                    db.execute(
                        "SELECT result FROM operations WHERE id=?", (operation,)
                    ).fetchone()
                    if exists
                    else None
                )
            receipt = json.loads(row[0]) if row else {}
            self.evidence.report["receipts"].append(
                {"phase": phase, "job": result, "native": receipt}
            )
            self.evidence.save()
            require(
                result["state"] == "completed"
                and result["items"][0]["state"] == "verified"
                and receipt.get("status") == "verified"
                and receipt.get("submitted") is True
                and receipt.get("operation") == operation
                and receipt.get("action") == "rename"
                and receipt.get("group") == int(GROUP)
                and receipt.get("member") == int(MEMBER),
                "write_not_verified_no_retry_or_restore",
            )
            from astrbot.core.platform.sources.wangshangliao.directory import (
                member_card,
            )

            roster = await self.transport.preflight()
            require(
                member_card(roster[MEMBER]) == job["items"][0]["name"],
                "independent_native_readback_failed",
            )
            require(
                all(
                    member_card(roster[uid]) == self.baseline[uid]
                    for uid in (OWNER, ADMIN)
                ),
                "non_target_card_changed",
            )
            self.evidence.report[phase + "_roster"] = list(roster.values())
            self.evidence.save()
            self.evidence.verified(operation)
        finally:
            self.transport.armed = None

    async def run(self, confirm):
        """Preview, confirm change and restore, stopping on any uncertain outcome.

        Args:
            confirm: Local operator callback accepting the frozen preview.

        Returns:
            Sanitized report, with success only after verified restoration.

        Raises:
            RuntimeError: If a prerequisite, receipt or readback is unproven.
        """
        from astrbot.core.platform.sources.wangshangliao.directory import member_card

        try:
            roster = await self.transport.preflight()
            self.baseline = {uid: member_card(row) for uid, row in roster.items()}
            original = self.baseline[MEMBER]
            require(
                original.strip()
                and len(original.encode()) <= 256
                and not original.startswith("WSL_TEST_"),
                "original_card_not_safely_restorable",
            )
            proposed = "WSL_TEST_" + self.evidence.run[:4]
            self.evidence.report.update(
                original=original,
                proposed=proposed,
                before_roster=list(roster.values()),
            )
            job = await self.preview(original, proposed)
            if not confirm(job):
                self.evidence.report["state"] = "not_confirmed"
                return self.evidence.report
            self.evidence.report["state"] = "applying"
            self.evidence.save()
            await self.execute(job, "change")
            restore = await self.preview(proposed, original)
            await self.execute(restore, "restore")
            self.evidence.report["state"] = "verified_restored"
            self.evidence.restored()
            return self.evidence.report
        except BaseException:
            self.evidence.report["state"] = "stopped_manual_review_no_replay"
            raise
        finally:
            await self.jobs.close()
            self.evidence.save()


async def live_check(evidence, pace):
    """Load existing sessions only for an operator-confirmed opt-in execution.

    Args:
        evidence: New private, isolated run directory.
        pace: Shared HTTP pacing for dashboard and business requests.

    Returns:
        Verified-restored or not-confirmed report.

    Raises:
        RuntimeError: If configuration, bindings or preconditions are unsuitable.
    """
    import aiohttp
    import httpx
    import jwt

    from astrbot.core.platform.sources.wangshangliao.business import (
        BusinessClient,
        Deployment,
    )
    from astrbot.core.platform.sources.wangshangliao.storage import Vault

    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    configured = [
        item
        for item in config.get("platform", [])
        if item.get("type") == "wangshangliao"
    ]
    require(
        len(configured) == 3
        and {str(item.get("account_id")): item.get("id") for item in configured}
        == BINDINGS,
        "production_bindings_changed",
    )
    base = ROOT / "data/platform_data/wangshangliao"
    pins, saved_admin = {}, None
    for uid, instance in BINDINGS.items():
        saved = Vault(
            instance, base / hashlib.sha256(instance.encode()).hexdigest()
        ).load()
        require(
            isinstance(saved, dict)
            and str(saved.get("business", {}).get("uid")) == uid
            and bool(saved.get("nim_id")),
            "existing_session_identity_required",
        )
        pins[uid] = str(saved["nim_id"])
        if uid == ADMIN:
            saved_admin = saved["business"]
    if os.environ.get("ASTRBOT_WANGSHANGLIAO_CONFIG"):
        deployment = Deployment.load()
    else:
        saved = Vault("wangshangliao-deployment-v1", base / "deployment").load()
        require(isinstance(saved, dict), "existing_deployment_required")
        deployment = Deployment.model_validate(saved)
        parsed = urlsplit(deployment.origin)
        require(
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not (
                parsed.username or parsed.password or parsed.query or parsed.fragment
            )
            and parsed.path in ("", "/"),
            "invalid_deployment_origin",
        )
        for name in ("signing_seed", "body_key", "server_key"):
            deployment.key(name)
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 1800},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:6185",
            trust_env=False,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + token},
            timeout=60,
        ) as dashboard,
        aiohttp.ClientSession(trust_env=False) as http,
    ):

        async def status():
            response = await dashboard.post(
                STATUS, json={"action": "status", "payload": {}}
            )
            response.raise_for_status()
            body = response.json()
            require(body.get("status") == "ok", "dashboard_status_failed")
            return body["data"]

        business = BusinessClient(deployment, http)
        business.restore(saved_admin)
        transport = NativeTransport(business, status, pins, evidence, pace)
        check = CardCheck(transport, evidence)

        def confirm(job):
            item = job["items"][0]
            print(
                json.dumps(
                    {
                        "preview": job["id"],
                        "group": GROUP,
                        "member": MEMBER,
                        "original": item["original"],
                        "proposed": item["name"],
                        "restore_after_verified_change": item["original"],
                        "maximum_native_writes": 2,
                        "evidence": str(evidence.root),
                    },
                    ensure_ascii=True,
                )
            )
            expected = f"APPLY {GROUP} {MEMBER} {job['id']}"
            return input(f"Confirm change AND restoration: {expected}\n") == expected

        return await check.run(confirm)


def main(argv=None):
    """Default to a credential-free plan; --apply needs an isolated root process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "bindings": BINDINGS,
                    "group": GROUP,
                    "target": MEMBER,
                    "native_admin_required": ADMIN,
                    "maximum_native_writes": 2,
                    "http_gap_seconds": HTTP_GAP,
                    "write_gap_seconds": WRITE_GAP,
                    "boundary": "isolated native check, not chat; no NIM connection",
                }
            )
        )
        return 0
    require(os.geteuid() == 0, "root_test_process_required")
    require("astrbot.core" not in sys.modules, "fresh_test_process_required")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.umask(0o077)
    os.environ["ASTRBOT_DISABLE_FILE_LOGGING"] = "1"
    os.environ["ASTRBOT_DISABLE_METRICS"] = "1"
    sys.dont_write_bytecode = True
    with_environment = Evidence(CONTROL)
    try:
        os.environ["ASTRBOT_ROOT"] = str(with_environment.root / "runtime")
        try:
            result = asyncio.run(live_check(with_environment, PacedIO()))
            print(
                json.dumps(
                    {"state": result["state"], "evidence": str(with_environment.root)}
                )
            )
            return 0 if result["state"] == "verified_restored" else 2
        except (Exception, KeyboardInterrupt):
            print(
                json.dumps(
                    {
                        "state": "stopped_manual_review_no_replay",
                        "evidence": str(with_environment.root),
                    }
                )
            )
            return 1
    finally:
        with_environment.close()


if __name__ == "__main__":
    raise SystemExit(main())
