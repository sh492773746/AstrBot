"""Dashboard binding for the fixed blacklist acceptance fixture; no mutations."""

import asyncio
import copy
import math
import re
import sqlite3
import time
from contextlib import closing

from astrbot.core.platform.sources.wangshangliao.blacklist import BLACKLIST_MUTE_MINUTES
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.directory import muted_member_ids
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.test_blacklist import (
    GROUP,
    OWNER,
    RECEIVER,
    TARGET,
    BlacklistFixture,
)
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .open_store import OpenError


async def begin_blacklist(service, actor, payload):
    """Bind or explicitly renew one operation without changing a test window.

    Args:
        service: Existing integrated control plane with original authenticated bots.
        actor: Authenticated dashboard principal, never a payload-supplied identity.
        payload: Only operation_id, exactly WSL_TEST_BLACKLIST_<32 lowercase hex>.

    Returns:
        Test-only binding metadata, not live acceptance or a native action.

    Raises:
        OpenError: If scope, original identities, roles or renewal are unsafe.
        ProtocolError: If a prior attempt is unknown or authority was revoked.
    """
    if (
        type(actor) is not str
        or not actor.startswith("dashboard/")
        or not actor.removeprefix("dashboard/")
        or type(payload) is not dict
        or set(payload) != {"operation_id"}
        or type(payload["operation_id"]) is not str
        or not re.fullmatch(r"WSL_TEST_BLACKLIST_[0-9a-f]{32}", payload["operation_id"])
    ):
        raise OpenError("test_blacklist_arguments")
    adapters = service.adapters()
    accounts = {str(a.account): a for a in adapters}
    if (
        len(adapters) != 3
        or set(accounts) != {OWNER, RECEIVER, TARGET}
        or len({a.config["id"] for a in adapters}) != 3
        or len({str(a.nim_account) for a in adapters}) != 3
    ):
        raise OpenError("test_blacklist_accounts")
    receiver, target = accounts[RECEIVER], accounts[TARGET]
    window = getattr(receiver, "test_window", None)
    operation = payload["operation_id"]
    if (
        type(window) is not TestWindow
        or window.receiver is not receiver
        or window.sender is not target
        or window.groups != [GROUP]
        or window.scopes not in (["group_rules"], ["group_commands"])
        or window.keywords != [operation]
        or not window.active()
        or not math.isfinite(window.expires)
        or not 0 < window.expires - time.monotonic() <= 300
    ):
        raise OpenError("test_blacklist_window")
    previous = getattr(service, "_blacklist_fixture", None)
    if previous is not None:
        if type(previous) is not BlacklistFixture or previous.operation != operation:
            raise OpenError("test_blacklist_existing")
        previous.binding_guard()
        previous.check_native()
        with closing(
            sqlite3.connect(previous.path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            if db.execute(
                "SELECT 1 FROM attempts WHERE state='unknown'"
            ).fetchone() or db.execute("SELECT restored FROM fixture").fetchone() != (
                0,
            ):
                raise OpenError("test_blacklist_unresolved")
        if previous.window is window:
            previous.check()
            directory = await receiver.get_moderation_members(GROUP)
            previous.check()
            previous.roster(directory, present=None)
            return {
                "test_only": True,
                "operation_id": operation,
                "replayed": True,
                "rebound": False,
            }
    tenant = copy.deepcopy(service.tenants.get((RECEIVER, GROUP), {}))
    settings = copy.deepcopy(service.settings)
    store = service.store
    team = receiver.groups.get(GROUP)
    peers = {uid: str(a.nim_account) for uid, a in accounts.items()}
    snapshots = {
        uid: (a.config["id"], a.account, a.nim_account, a.business, a.nim, a.ledger)
        for uid, a in accounts.items()
    }
    guards = [account_guard(a) for a in adapters]

    def guard():
        """Recheck the original accounts and tenant after every awaited read."""
        if previous is not None:
            previous.binding_guard()
        for check in guards:
            check()
        current = service.adapters()
        if (
            not service.active
            or not service.integrated
            or service.settings != settings
            or type(settings.get("paused")) not in (int, bool)
            or settings["paused"] != 0
            or getattr(service, "capacity_paused", False)
            or service.store is not store
            or service.tenants.get((RECEIVER, GROUP)) != tenant
            or store.tenant(RECEIVER, GROUP) != tenant
            or any(
                type(tenant.get(key)) not in (int, bool) or tenant[key] != expected
                for key, expected in (("active", 1), ("present", 1), ("paused", 0))
            )
            or tenant.get("managers") != [OWNER]
            or (RECEIVER, GROUP) in service.suspended_groups
            or getattr(service, "membership_receiving", {}).get((RECEIVER, GROUP))
            or not team
            or receiver.groups.get(GROUP) != team
            or receiver.group_catalog.get(GROUP, {}).get("team") != team
            or GROUP not in receiver.enabled_groups
            or len(current) != 3
            or any(not any(a is live for live in current) for a in adapters)
        ):
            raise OpenError("test_blacklist_revoked")
        for uid, a in accounts.items():
            original = snapshots[uid]
            if (
                (a.config["id"], a.account, a.nim_account) != original[:3]
                or a.business is not original[3]
                or a.nim is not original[4]
                or a.ledger is not original[5]
                or a.connection_state != "online"
                or a.stopping.is_set()
                or a.config.get("_business_active") is not True
                or a.config.get("enable") is not True
                or type(a.config.get("_business_epoch")) is not int
                or type(a.config.get("_business_revision")) is not str
                or not 1 <= len(a.config["_business_revision"]) <= 128
                or not peers[uid].isascii()
                or not peers[uid].isdecimal()
                or not 0 < int(peers[uid]) < 1 << 32
                or a.business is None
                or not a.business.jwt
                or str(a.business.fields[3]) != uid
                or a.nim is None
                or a.ledger is None
            ):
                raise OpenError("test_blacklist_identity")

    async def restored():
        """Require native blacklist removal and unmute, without performing either."""
        guard()
        muted = await muted_member_ids(receiver, GROUP, authorize=guard)
        guard()
        if TARGET in muted:
            raise ProtocolError("test_blacklist_restoration_unproven")
        directory = await receiver.get_moderation_members(GROUP)
        guard()
        fixture.roster(directory)
        native = instance_dir(receiver.config["id"]) / "member_blacklist.sqlite3"
        if not native.exists():
            raise ProtocolError("test_blacklist_restoration_unproven")
        with closing(sqlite3.connect(native.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            with closing(
                sqlite3.connect(fixture.path.as_uri() + "?mode=ro", uri=True)
            ) as test_db:
                start = test_db.execute("SELECT native_start FROM fixture").fetchone()[
                    0
                ]
            entries = db.execute(
                "SELECT id,cycle,version,peer,status FROM blacklist_entries "
                "WHERE account=? AND group_id=? AND member=?",
                (RECEIVER, GROUP, TARGET),
            ).fetchall()
            actions = db.execute(
                "SELECT entry,cycle,version,join_id,peer,instance,finished,"
                "action,minutes,status,policy_version FROM blacklist_intents "
                "WHERE account=? AND group_id=? AND member=? AND rowid>? ORDER BY rowid",
                (RECEIVER, GROUP, TARGET, start),
            ).fetchall()
        if (
            not entries
            or any(row["status"] != "removed" for row in entries)
            or len(actions) != 1
            or actions[0]["action"] != "mute"
            or type(actions[0]["minutes"]) is not int
            or not 1 <= actions[0]["minutes"] <= BLACKLIST_MUTE_MINUTES
            or actions[0]["status"] != "accepted"
            or actions[0]["finished"] != 1
            or actions[0]["peer"] != peers[TARGET]
            or actions[0]["instance"] != receiver.config["id"]
            or type(actions[0]["policy_version"]) is not int
            or actions[0]["policy_version"] < 0
            or not any(
                (row["id"], row["cycle"], row["version"], row["peer"])
                == (
                    actions[0]["entry"],
                    actions[0]["cycle"],
                    actions[0]["version"],
                    peers[TARGET],
                )
                for row in entries
            )
        ):
            raise ProtocolError("test_blacklist_restoration_unproven")

    fixture = BlacklistFixture(window, operation, peers, service, guard, restored)
    guard()
    fixture.check_native()
    if previous is None and fixture.path.exists():
        raise OpenError("test_blacklist_existing")
    if previous is None:
        # A new marker or a restarted service must not bypass a previous freeze.
        root = instance_dir(receiver.config["id"]) / "test-blacklist"
        for path in root.glob("*/fixture.sqlite3"):
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                if (
                    db.execute("SELECT restored FROM fixture").fetchone() != (1,)
                    or db.execute(
                        "SELECT 1 FROM attempts WHERE state!='verified'"
                    ).fetchone()
                ):
                    raise OpenError("test_blacklist_unresolved")
    directory = await receiver.get_moderation_members(GROUP)
    guard()
    fixture.roster(directory, present=True if previous is None else None)
    if (
        receiver.test_window is not window
        or window.receiver is not receiver
        or window.sender is not target
        or window.groups != [GROUP]
        or window.scopes not in (["group_rules"], ["group_commands"])
        or tuple(window.scopes) != fixture.scopes
        or window.keywords != [operation]
        or window.expires != fixture.deadline
        or not window.active()
        or getattr(service, "_blacklist_fixture", None) is not previous
    ):
        raise OpenError("test_blacklist_revoked")
    if previous is None:
        native_start = 0
        native = instance_dir(receiver.config["id"]) / "member_blacklist.sqlite3"
        if native.exists():
            with closing(sqlite3.connect(native.as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute(
                    "SELECT 1 FROM blacklist_entries WHERE account=? AND group_id=? "
                    "AND member=? AND status='active'",
                    (RECEIVER, GROUP, TARGET),
                ).fetchone():
                    raise OpenError("test_blacklist_existing")
                native_start = db.execute(
                    "SELECT COALESCE(MAX(rowid),0) FROM blacklist_intents"
                ).fetchone()[0]
        fixture.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(fixture.path)) as db, db:
            db.execute(
                "CREATE TABLE fixture(restored INTEGER NOT NULL,native_start INTEGER)"
            )
            db.execute("INSERT INTO fixture VALUES(0,?)", (native_start,))
            db.execute(
                "CREATE TABLE attempts(step TEXT PRIMARY KEY,nonce TEXT UNIQUE,"
                "state TEXT NOT NULL,created REAL NOT NULL)"
            )
        fixture.path.chmod(0o600)
    guard()
    service._blacklist_fixture = fixture
    window._blacklist_fixture = fixture
    fixture.check()
    return {
        "test_only": True,
        "operation_id": operation,
        "replayed": False,
        "rebound": previous is not None,
    }


async def control_blacklist(service, actor, payload):
    """Expose only fixed, separately requested acceptance steps to total control.

    Args:
        service: Original integrated control plane.
        actor: Authenticated dashboard principal, never taken from payload.
        payload: Begin, membership, unmute or restore with an exact operation.

    Returns:
        Bounded fixed-test evidence without private provider material.

    Raises:
        OpenError: If the operation or original installed capability changed.
    """
    if (
        not isinstance(actor, str)
        or not actor.startswith("dashboard/")
        or type(payload) is not dict
        or payload.get("operation") not in {"begin", "membership", "unmute", "restore"}
        or set(payload)
        != (
            {"operation", "operation_id", "step"}
            if payload.get("operation") == "membership"
            else {"operation", "operation_id"}
        )
    ):
        raise OpenError("test_blacklist_arguments")
    operation = payload["operation"]
    if operation == "begin":
        return await begin_blacklist(
            service, actor, {"operation_id": payload["operation_id"]}
        )
    fixture = getattr(service, "_blacklist_fixture", None)
    if (
        type(fixture) is not BlacklistFixture
        or fixture.operation != payload["operation_id"]
    ):
        raise OpenError("test_blacklist_existing")
    await asyncio.to_thread(fixture.check)
    if operation == "membership":
        from scripts.wsl_test_blacklist import BlacklistMembershipTest

        return await BlacklistMembershipTest(fixture).change(payload["step"])
    if operation == "unmute":
        from astrbot.core.platform.sources.wangshangliao.moderation import execute

        await asyncio.to_thread(fixture.check)
        result = await execute(
            fixture.receiver.business,
            fixture.receiver.config["id"],
            RECEIVER,
            "test-blacklist-unmute/"
            + fixture.operation.removeprefix("WSL_TEST_BLACKLIST_"),
            "unmute",
            int(GROUP),
            int(TARGET),
            expected_nim=fixture.peers[TARGET],
            blacklist_fixture=fixture,
            authorize=fixture.check,
        )
        return {"test_only": True, "result": result}
    await fixture.restore()
    if getattr(service, "_blacklist_fixture", None) is fixture:
        service._blacklist_fixture = None
    if getattr(fixture.window, "_blacklist_fixture", None) is fixture:
        fixture.window._blacklist_fixture = None
    return {"test_only": True, "restored": True}
