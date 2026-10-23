"""Fixed dashboard-only lottery preview; never evidence of an owner chat."""

import asyncio
import hashlib
import inspect
import re
import time
from types import SimpleNamespace

from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.group_scope import GroupAdapter
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow

from .activity_store import database
from .open_store import OpenError

GROUP, RECEIVER, SENDER, OWNER = "1459282", "43506527", "23691273", "31846829"
ACTIONS = {"start": "开启抽奖", "draw": "立即开奖", "cancel": "取消抽奖"}


def _roster(directory, accounts):
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
        or any(
            members[uid].get("groupRole") != role
            or members[uid].get("accountState") != "ACCOUNT_STATE_GOOD"
            or str(members[uid].get("nimId", "")) != str(accounts[uid].nim_account)
            for uid, role in roles.items()
        )
        or len({str(row.get("nimId", "")) for row in rows}) != 3
    ):
        raise OpenError("test_lottery_roster")


class _DashboardAdapter(GroupAdapter):
    """Carry the dashboard guard into existing activity commit/send checks."""

    def __init__(self, adapter, guard, accounts, state, action):
        super().__init__(adapter, GROUP)
        for name, value in (
            ("_check", guard),
            ("_accounts", accounts),
            ("_state", state),
            ("_action", action),
        ):
            object.__setattr__(self, name, value)

    @property
    def config(self):
        self._check()
        return super().config

    async def get_moderation_members(self, group):
        self._check()
        if group != GROUP:
            raise OpenError("test_lottery_group")
        result = await self.adapter.get_moderation_members(group)
        self._check()
        _roster(result, self._accounts)
        return result

    async def send_reply_text(self, group, key, text, *, authorize=None, **kwargs):
        self._check()
        binding = getattr(self.adapter, "_lottery_test_fixture", None)
        if not binding or binding[1] is not self.adapter.test_window:
            raise OpenError("test_lottery_binding")
        identifier = binding[0]
        suffix = {"start": "start", "draw": "result", "cancel": "cancel"}[self._action]
        expected = f"lottery-{suffix}/{RECEIVER}/{identifier}"
        if (
            group != GROUP
            or key != expected
            or self._state["fixture_id"] not in ("", identifier)
            or key in self._state["notices"]
            or len(self._state["notices"]) >= 2
            or len(text.encode()) > 2048
        ):
            raise OpenError("test_lottery_notice")
        self._state["fixture_id"] = identifier
        self._state["notices"].add(key)
        self._state["results"][self._action].update(
            fixture_id=identifier, receipt_id=key, text=text
        )

        async def check():
            self._check()
            if authorize:
                checked = authorize()
                if inspect.isawaitable(checked):
                    await checked
            self._check()

        return await self.adapter.send_reply_text(
            group, key, text, authorize=check, **kwargs
        )


async def control_lottery(service, actor, payload):
    """Preview one fixed lottery via dashboard, without changing saved settings.

    Only ordinary enrollment uses native admitted messages. This entry point
    never admits messages, changes roles, resets budgets or writes reward ledgers.
    """
    if (
        type(actor) is not str
        or not actor.startswith("dashboard/")
        or not actor.removeprefix("dashboard/")
        or type(payload) is not dict
        or set(payload) != {"operation_id", "action"}
        or type(payload["operation_id"]) is not str
        or not re.fullmatch(r"WSL_TEST_LOTTERY_[0-9a-f]{32}", payload["operation_id"])
        or type(payload["action"]) is not str
        or payload["action"] not in ACTIONS
    ):
        raise OpenError("test_lottery_arguments")
    operation, action = payload["operation_id"], payload["action"]
    adapters = service.adapters()
    accounts = {
        str(a.account): a
        for a in adapters
        if str(a.account) in {OWNER, RECEIVER, SENDER}
    }
    if len(accounts) != 3 or sum(str(a.account) in accounts for a in adapters) != 3:
        raise OpenError("test_lottery_accounts")
    receiver, sender = accounts[RECEIVER], accounts[SENDER]
    window = getattr(receiver, "test_window", None)
    if not isinstance(window, TestWindow):
        raise OpenError("test_lottery_window")
    activities = service.plugin.activities
    ledger, business, nim = receiver.ledger, receiver.business, receiver.nim
    team = receiver.groups.get(GROUP)
    tenant = dict(service.tenants.get((RECEIVER, GROUP), {}))
    identities = {
        uid: (a.account, a.nim_account, a.config["id"]) for uid, a in accounts.items()
    }
    frozen = account_guard(receiver)
    state = getattr(window, "_lottery_control", None)
    if state is None:
        state = window._lottery_control = {
            "lock": asyncio.Lock(),
            "operation_id": "",
            "fixture_id": "",
            "results": {},
            "notices": set(),
        }

    def guard():
        frozen()
        current = service.tenants.get((RECEIVER, GROUP), {})
        if (
            not service.integrated
            or not service.active
            or service.settings["paused"]
            or getattr(service, "capacity_paused", False)
            or service.plugin.activities is not activities
            or receiver.test_window is not window
            or window.receiver is not receiver
            or window.sender is not sender
            or window.groups != [GROUP]
            or window.scopes != ["group_commands"]
            or not window.active()
            or window._lottery_control is not state
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
            or OWNER not in current.get("managers", [])
            or (RECEIVER, GROUP) in service.suspended_groups
            or getattr(service, "membership_receiving", {}).get((RECEIVER, GROUP))
            or state["operation_id"] not in ("", operation)
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
            raise OpenError("test_lottery_window")
        activities.authorize(
            receiver,
            GROUP,
            OWNER,
            "dashboard-test/" + operation,
            posting=True,
            test_window=window,
            test_lottery=state["fixture_id"],
        )

    guard()
    async with state["lock"]:
        guard()
        if action in state["results"]:
            return {**state["results"][action], "replayed": True}
        if action != "start" and (
            state["operation_id"] != operation or not state["fixture_id"]
        ):
            raise OpenError("test_lottery_binding")
        adapter = _DashboardAdapter(receiver, guard, accounts, state, action)
        session = "dashboard-test/" + operation
        event = SimpleNamespace(
            platform=adapter,
            is_private_chat=lambda: True,
            is_admin=lambda: False,
            get_sender_id=lambda: OWNER,
            get_sender_name=lambda: "Dashboard test owner preview",
            get_group_id=lambda: "",
            unified_msg_origin=session,
            message_obj=SimpleNamespace(message_id=operation),
            get_extra=lambda _: {},
        )

        async def prepare():
            guard()
            await adapter.get_moderation_members(GROUP)
            async with AsyncDatabase(lambda: database(adapter), guard=guard) as db:
                settings = await db.fetchone(
                    "SELECT * FROM lottery_settings WHERE account=? AND group_id=?",
                    (RECEIVER, GROUP),
                )
                active = await db.fetchone(
                    "SELECT * FROM lotteries WHERE account=? AND group_id=? AND status "
                    "IN ('announcing','open','blocked','drawing')",
                    (RECEIVER, GROUP),
                )
                if action == "start":
                    command = hashlib.sha256(
                        f"{RECEIVER}/{session}/{operation}".encode()
                    ).hexdigest()
                    historical = await db.fetchone(
                        "SELECT id FROM lotteries WHERE command=?", (command,)
                    )
                    binding = getattr(receiver, "_lottery_test_fixture", None)
                    if active or historical or binding and binding[1] is window:
                        raise OpenError("test_lottery_existing")
                    if (
                        not settings
                        or not settings["prize"].startswith(operation)
                        or settings["packet_mode"] not in {"prize", "ordinary", "lucky"}
                        or settings["winners"]
                        != (1 if settings["packet_mode"] == "prize" else 2)
                        or settings["packet_mode"] != "prize"
                        and not 2 <= settings["total_amount"] <= 100
                        or settings["capacity"] != 2
                        or settings["invite_gate"] != 0
                        or not 60 <= settings["duration"] <= 300
                        or not settings["contact"]
                    ):
                        raise OpenError("test_lottery_settings")
                else:
                    if (
                        not active
                        or active["id"] != state["fixture_id"]
                        or active["status"] != "open"
                        or active["owner"] != OWNER
                        or active["session"] != session
                        or not active["prize"].startswith(operation)
                        or getattr(receiver, "_lottery_test_fixture", None)
                        != (active["id"], window)
                    ):
                        raise OpenError("test_lottery_binding")
                    if action == "draw":
                        if (
                            active["packet_mode"] != "prize"
                            and time.time() < active["ends"]
                        ):
                            raise OpenError("test_lottery_not_due")
                        entries = await db.fetchall(
                            "SELECT * FROM lottery_entries WHERE lottery=?",
                            (active["id"],),
                        )
                        if (
                            len(entries) != 1
                            or entries[0]["member"] != SENDER
                            or entries[0]["peer"] != str(sender.nim_account)
                        ):
                            raise OpenError("test_lottery_enrollment")
            guard()
            if action == "start":
                if window.outbound_remaining < 2:
                    raise OpenError("test_lottery_budget")
                window.outbound_remaining -= 2
                window.outbound_keys.add("test-lottery/" + operation)
                state["operation_id"] = operation
            state["results"][action] = {
                "channel": "dashboard-test",
                "operation_id": operation,
                "action": action,
                "fixture_id": state["fixture_id"],
                "status": "unknown",
                "receipt_id": "",
                "message_id": "",
                "text": "",
                "command_reply": "",
                "replayed": False,
            }

        text = await activities.command(
            event, GROUP, ACTIONS[action], "", precondition=prepare, test_window=window
        )
        guard()
        binding = getattr(receiver, "_lottery_test_fixture", None)
        if not binding or binding != (state["fixture_id"], window):
            raise OpenError("test_lottery_binding")
        suffix = {"start": "start", "draw": "result", "cancel": "cancel"}[action]
        key = f"lottery-{suffix}/{RECEIVER}/{state['fixture_id']}"
        receipt = await ledger.receipt(key)
        guard()
        result = {
            **state["results"][action],
            "fixture_id": state["fixture_id"],
            "command_reply": text,
            "receipt_id": key,
            "message_id": receipt.get("server_id", "") if receipt else "",
            "status": receipt["status"] if receipt else "unknown",
        }
        state["results"][action] = result
        return dict(result)
