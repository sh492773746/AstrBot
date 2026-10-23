"""Dashboard-only fresh sanction ledger for the fixed native advertising test."""

import copy
import re
import sqlite3

from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.test_rules import AdvertisingFixture
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow

from .open_store import OpenError

GROUP, RECEIVER, SENDER, OWNER = "1459282", "43506527", "23691273", "31846829"


async def begin_advertising(service, actor, payload):
    """Bind one six-message test to fresh storage without resetting live history.

    Args:
        service: Existing integrated control-plane service.
        actor: Authenticated dashboard principal.
        payload: Fixed operation identifier; no paths or configurable targets.

    Returns:
        Confirmed test-only binding, not a platform action or acceptance result.

    Raises:
        OpenError: If identities, native roles, policy or the original window differ.
    """
    if (
        type(actor) is not str
        or not actor.startswith("dashboard/")
        or not actor.removeprefix("dashboard/")
        or type(payload) is not dict
        or set(payload) != {"operation_id"}
        or type(payload["operation_id"]) is not str
        or not re.fullmatch(r"WSL_TEST_AD_[0-9a-f]{32}", payload["operation_id"])
    ):
        raise OpenError("test_advertising_arguments")
    adapters = service.adapters()
    accounts = {str(a.account): a for a in adapters}
    if len(adapters) != 3 or set(accounts) != {RECEIVER, SENDER, OWNER}:
        raise OpenError("test_advertising_accounts")
    receiver, sender = accounts[RECEIVER], accounts[SENDER]
    window = getattr(receiver, "test_window", None)
    if not isinstance(window, TestWindow):
        raise OpenError("test_advertising_window")
    operation = payload["operation_id"]
    tenant = copy.deepcopy(service.tenants.get((RECEIVER, GROUP), {}))
    identities = {
        uid: (a.account, a.nim_account, a.config["id"]) for uid, a in accounts.items()
    }
    ledger, business, nim = receiver.ledger, receiver.business, receiver.nim
    team = receiver.groups.get(GROUP)
    frozen = account_guard(receiver)
    previous = getattr(window, "_advertising_fixture", None)

    def guard():
        frozen()
        current = service.tenants.get((RECEIVER, GROUP), {})
        rules = current.get("rules", {})
        binding = getattr(window, "_advertising_fixture", None)
        if (
            not service.active
            or not service.integrated
            or service.settings["paused"]
            or getattr(service, "capacity_paused", False)
            or receiver.test_window is not window
            or window.receiver is not receiver
            or window.sender is not sender
            or window.groups != [GROUP]
            or window.scopes != ["group_rules"]
            or window.keywords != [operation]
            or not window.active()
            or binding is not None
            and binding is not fixture
            or not team
            or receiver.groups.get(GROUP) != team
            or receiver.group_catalog.get(GROUP, {}).get("team") != team
            or receiver.ledger is not ledger
            or receiver.business is not business
            or receiver.nim is not nim
            or current != tenant
            or service.store.tenant(RECEIVER, GROUP) != current
            or not current.get("active")
            or not current.get("present")
            or current.get("paused")
            or current.get("managers") != [OWNER]
            or not rules.get("advertising")
            or rules.get("flood")
            or rules.get("keywords") != [operation]
            or (RECEIVER, GROUP) in service.suspended_groups
            or getattr(service, "membership_receiving", {}).get((RECEIVER, GROUP))
            or any(
                (a.account, a.nim_account, a.config["id"]) != identities[uid]
                or a.connection_state != "online"
                or a.stopping.is_set()
                or not any(
                    a is current_adapter for current_adapter in service.adapters()
                )
                for uid, a in accounts.items()
            )
        ):
            raise OpenError("test_advertising_revoked")

    path = (
        instance_dir(receiver.config["id"])
        / "test-advertising"
        / operation
        / "moderation.sqlite3"
    )
    fixture = previous or AdvertisingFixture(window, operation, path, guard)
    guard()
    if previous is not None:
        if type(previous) is not AdvertisingFixture or previous.operation != operation:
            raise OpenError("test_advertising_existing")
        previous.authorize()
        return {"test_only": True, "operation_id": operation, "replayed": True}
    if window.remaining != 6 or window.accepted or path.exists():
        raise OpenError("test_advertising_not_fresh")
    native_path = instance_dir(receiver.config["id"]) / "moderation.sqlite3"
    if native_path.exists():
        async with AsyncDatabase(
            lambda: sqlite3.connect(native_path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            tables = {
                row[0]
                for row in await db.fetchall(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "automatic_mutes" in tables and await db.fetchone(
                "SELECT 1 FROM automatic_mutes WHERE account=? AND group_id=? "
                "AND member=? AND closed=0 AND status='unknown' LIMIT 1",
                (RECEIVER, GROUP, SENDER),
            ):
                raise OpenError("test_advertising_unresolved")
            if "operations" in tables and await db.fetchone(
                "SELECT 1 FROM operations WHERE json_extract(result,'$.status')='unknown' "
                "AND json_extract(result,'$.action') IN ('mute','unmute') "
                "AND CAST(json_extract(result,'$.group') AS TEXT)=? "
                "AND CAST(json_extract(result,'$.member') AS TEXT)=? LIMIT 1",
                (GROUP, SENDER),
            ):
                raise OpenError("test_advertising_unresolved")
        guard()
    directory = await receiver.get_moderation_members(GROUP)
    guard()
    rows = directory.get("groupMemberInfo", [])
    members = {str(row["userId"]): row for row in rows}
    roles = {
        OWNER: "GROUP_ROLE_OWNER",
        RECEIVER: "GROUP_ROLE_ADMIN",
        SENDER: "GROUP_ROLE_MEMBER",
    }
    if (
        not directory.get("complete")
        or len(rows) != 3
        or set(members) != set(roles)
        or len({str(row.get("nimId", "")) for row in rows}) != 3
        or any(
            row.get("groupRole") != roles[uid]
            or row.get("accountState") != "ACCOUNT_STATE_GOOD"
            or str(row.get("nimId", "")) != str(accounts[uid].nim_account)
            for uid, row in members.items()
        )
    ):
        raise OpenError("test_advertising_roster")
    # No await separates the final binding check from installation. The original
    # window and tenant guard are subsequently checked through native submission.
    guard()
    window._advertising_fixture = fixture
    return {"test_only": True, "operation_id": operation, "replayed": False}
