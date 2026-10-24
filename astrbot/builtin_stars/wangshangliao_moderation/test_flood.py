"""Opt-in fixed-account flood rehearsal; ordinary developer limits stay intact."""

import asyncio
import copy
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.blacklist import MEMBER_LIST_WRITE_LOCK
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.moderation import execute
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.test_rules import FloodFixture
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow

from .open_store import OpenError

GROUP, RECEIVER, TARGET, OWNER = "1459282", "43506527", "23691273", "31846829"
CASES = {"below", "rapid", "slow", "repeat", "rank", "rank_recall", "exempt"}
RANKING_CASES = {"rank", "rank_recall", "exempt"}


class FloodObserver:
    """Collect bounded native receipts without enabling the owner's group business."""

    def __init__(self, fixture, owner, sender, team):
        self.fixture, self.owner, self.sender, self.team = fixture, owner, sender, team
        self.receipts = {}
        self.expected = {}

    async def receive(self, adapter, outer, inner):
        """Observe only authenticated neutral probes from the fixed sender."""
        if adapter is not self.owner:
            return False
        try:
            self.fixture.authorize()
        except Exception:
            return False
        expected = self.expected.get(inner.client_id)
        if (
            expected is None
            or outer.get(0) != "1"
            or outer.get(1) != self.team
            or outer.get(2) != str(self.sender.nim_account)
            or outer.get(8) != "100"
            or outer.get(11) != inner.client_id
            or not str(outer.get(12, "")).isascii()
            or not str(outer.get(12, "")).isdigit()
            or inner.session != 2
            or inner.format != 0
            or str(inner.sender.id) != TARGET
            or str(inner.target.id) != GROUP
            or not inner.HasField("content")
            or inner.content.data != expected
        ):
            return False
        mid = str(outer[12])
        if mid not in self.receipts and len(self.receipts) >= 20:
            return False
        self.receipts[mid] = {
            "account": OWNER,
            "group": GROUP,
            "message": mid,
            "sender": TARGET,
        }
        return True


async def control_flood(service, actor, payload):
    """Send fixed neutral probes through the original live connection.

    Args:
        service: Authenticated integrated control plane.
        actor: Server-supplied dashboard principal.
        payload: Fixed operation, operation ID and optional predetermined case.

    Returns:
        Test-only evidence, not phone acceptance.

    Raises:
        OpenError: If scope, identities, roles or the original window changed.
    """
    action = payload.get("operation")
    operation = payload.get("operation_id")
    if (
        not isinstance(actor, str)
        or not actor.startswith("dashboard/")
        or not actor[10:]
        or action not in {"begin", "send", "unmute", "close"}
        or not isinstance(operation, str)
        or not re.fullmatch(r"WSL_TEST_FLOOD_[0-9a-f]{32}", operation)
        or set(payload)
        != (
            {"operation", "operation_id", "case"}
            if action in {"begin", "send"}
            else {"operation", "operation_id"}
        )
        or action in {"begin", "send"}
        and payload["case"] not in CASES
    ):
        raise OpenError("test_flood_arguments")
    accounts = {str(a.account): a for a in service.adapters()}
    if set(accounts) != {RECEIVER, TARGET, OWNER}:
        raise OpenError("test_flood_accounts")
    receiver, sender = accounts[RECEIVER], accounts[TARGET]
    window = getattr(receiver, "test_window", None)
    path = (
        instance_dir(receiver.config["id"])
        / "test-flood"
        / operation
        / "moderation.sqlite3"
    )
    if action == "begin":
        tenant = copy.deepcopy(service.tenants.get((RECEIVER, GROUP), {}))
        identities = {
            uid: (a, a.nim_account, a.business, a.nim, a.ledger)
            for uid, a in accounts.items()
        }
        team = str(receiver.groups.get(GROUP, ""))
        frozen = account_guard(receiver)
        if (
            not isinstance(window, TestWindow)
            or not window.active()
            or window.sender is not sender
            or window.groups != [GROUP]
            or window.scopes != ["group_rules"]
            or window.keywords != [operation]
            or window.remaining != 10
            or window.accepted
        ):
            raise OpenError("test_flood_window")

        def guard():
            frozen()
            current = service.tenants.get((RECEIVER, GROUP), {})
            if (
                not service.active
                or not service.integrated
                or service.settings["paused"]
                or getattr(service, "capacity_paused", False)
                or receiver.test_window is not window
                or not window.active()
                or current != tenant
                or service.store.tenant(RECEIVER, GROUP) != tenant
                or not tenant.get("active")
                or not tenant.get("present")
                or tenant.get("paused")
                or tenant.get("managers") != [OWNER]
                or not tenant.get("rules", {}).get("flood")
                or tenant.get("rules", {}).get("advertising")
                or not team.isascii()
                or not team.isdigit()
                or int(team) <= 0
                or str(receiver.groups.get(GROUP, "")) != team
                or str(receiver.group_catalog.get(GROUP, {}).get("team", "")) != team
                or any(
                    a not in service.adapters()
                    or a.nim_account != peer
                    or a.business is not business
                    or a.nim is not nim
                    or a.ledger is not ledger
                    or a.account != uid
                    or a.stopping.is_set()
                    or a.connection_state != "online"
                    for uid, (a, peer, business, nim, ledger) in identities.items()
                )
            ):
                raise OpenError("test_flood_revoked")

        guard()
        native = instance_dir(receiver.config["id"]) / "moderation.sqlite3"
        for candidate in (native, path):
            if candidate.is_file():
                with closing(
                    sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True)
                ) as db:
                    tables = {
                        row[0]
                        for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if (
                        "operations" in tables
                        and db.execute(
                            "SELECT 1 FROM operations WHERE json_extract(result,'$.status')='unknown' "
                            "AND CAST(json_extract(result,'$.group') AS TEXT)=? "
                            "AND CAST(json_extract(result,'$.member') AS TEXT)=? "
                            "AND json_extract(result,'$.action') IN ('mute','unmute') LIMIT 1",
                            (GROUP, TARGET),
                        ).fetchone()
                    ):
                        raise OpenError("test_flood_unresolved")
                    if (
                        "automatic_mutes" in tables
                        and db.execute(
                            "SELECT 1 FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
                            "AND closed=0 AND status='unknown' LIMIT 1",
                            (RECEIVER, GROUP, TARGET),
                        ).fetchone()
                    ):
                        raise OpenError("test_flood_unresolved")
        directory = await receiver.get_moderation_members(GROUP)
        guard()
        rows = directory.get("groupMemberInfo", [])
        roles = {
            RECEIVER: "GROUP_ROLE_ADMIN",
            TARGET: "GROUP_ROLE_MEMBER",
            OWNER: "GROUP_ROLE_OWNER",
        }
        if (
            not directory.get("complete")
            or len(rows) != 3
            or {str(row["userId"]) for row in rows} != set(roles)
            or any(
                row.get("groupRole") != roles[str(row["userId"])]
                or str(row.get("nimId"))
                != str(accounts[str(row["userId"])].nim_account)
                or row.get("accountState") != "ACCOUNT_STATE_GOOD"
                for row in rows
            )
        ):
            raise OpenError("test_flood_roster")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path.parent / "control.sqlite3")) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS phases(case_name TEXT,nonce TEXT PRIMARY KEY,created REAL)"
            )
            db.execute(
                "INSERT INTO phases VALUES(?,?,?)",
                (payload["case"], uuid.uuid4().hex, time.time()),
            )
        # This exact dashboard-bound rehearsal admits at most twenty messages;
        # general TestWindow validation stays capped at ten and 300 seconds.
        window.remaining = 20
        window._flood_send_budget = 20
        window._flood_cases = set()
        window._flood_team = team
        window._flood_fixture = FloodFixture(
            window, operation, path, guard, payload["case"] in RANKING_CASES
        )
        service.flood_rehearsal = FloodObserver(
            window._flood_fixture, accounts[OWNER], sender, team
        )
        return {"test_only": True, "bound": True, "case": payload["case"]}
    fixture = getattr(window, "_flood_fixture", None)
    if type(fixture) is not FloodFixture or fixture.operation != operation:
        raise OpenError("test_flood_missing")
    fixture.authorize()
    if action == "unmute":
        result = await execute(
            receiver.business,
            receiver.config["id"],
            RECEIVER,
            operation + "/unmute",
            "unmute",
            int(GROUP),
            int(TARGET),
            expected_nim=str(sender.nim_account),
            authorize=fixture.authorize,
            rule_fixture=fixture,
        )
        return {"test_only": True, "result": result}
    if action == "close":

        def retract():
            with MEMBER_LIST_WRITE_LOCK:
                window._flood_fixture = None
                if fixture.ranking:
                    auto = (
                        instance_dir(receiver.config["id"])
                        / "automatic_exemptions.sqlite3"
                    )
                    if auto.is_file():
                        with closing(sqlite3.connect(auto)) as db, db:
                            db.execute(
                                "DELETE FROM automatic_exemptions WHERE account=? AND group_id=? AND member=?",
                                (RECEIVER, GROUP, TARGET),
                            )

        await asyncio.to_thread(retract)
        service.flood_rehearsal = None
        return {"test_only": True, "closed": True}
    case = payload["case"]
    if fixture.ranking != (case in RANKING_CASES):
        raise OpenError("test_flood_case")
    offsets = (
        [0]
        if case == "rank"
        else [i * 4.0 for i in range(5)]
        if case == "repeat"
        else [batch * 3.16 + i * 0.03 for batch in range(4) for i in range(4)][:15]
        if case == "slow"
        else [i * 0.08 for i in range(4 if case == "below" else 5)]
    )
    phase = uuid.uuid4().hex
    if case in window._flood_cases or window._flood_send_budget < len(offsets):
        raise OpenError("test_flood_send_budget")
    window._flood_cases.add(case)
    window._flood_send_budget -= len(offsets)
    receipts = []
    started = time.monotonic()
    for index, offset in enumerate(offsets):
        await asyncio.sleep(max(0, started + offset - time.monotonic()))
        fixture.authorize()
        text = (
            operation + " 中性验收 " + (case if case == "repeat" else f"{case} {index}")
        )
        key = f"{operation}/{phase}/{index}"
        nonce, state = await sender.ledger.reserve(key, json.dumps([GROUP, text]))
        if state != "new":
            raise OpenError("test_flood_send_duplicate")
        cid = str(uuid.uuid4())
        inner = wire.ApplicationMessage(
            sender=wire.Source(id=int(TARGET)),
            target=wire.Source(id=int(GROUP)),
            created_at=int(time.time() * 1000),
            device=1,
            session=2,
            version=2,
            format=0,
            client_id=cid,
            content=wire.Content(data=text),
        )
        service.flood_rehearsal.expected[cid] = text
        attachment = wire.seal_message(
            sender.business.deployment.key("message_key"),
            inner,
            int(nonce),
            secrets.randbits(64) or 1,
            int(time.time()),
        )
        fixture.authorize()
        code, body = await sender.nim.request(
            8,
            2,
            wire.properties(
                [
                    (0, b"1"),
                    (1, window._flood_team.encode()),
                    (8, b"100"),
                    (10, attachment.encode()),
                    (11, cid.encode()),
                ]
            ),
        )
        if code != 200:
            await sender.ledger.finish(key, "rejected")
            raise OpenError(f"test_flood_send_rejected_{code}")
        fields, end = wire.read_properties(body)
        if end != len(body) or not fields[12].isdigit() or not fields[7].isdigit():
            raise OpenError("test_flood_send_unknown")
        mid = fields[12].decode()
        await sender.ledger.finish(key, "accepted", server_id=mid)
        receipts.append({"message": mid, "sent_at": int(fields[7]), "key": key})
    observer = service.flood_rehearsal
    for _ in range(50):
        if all(row["message"] in observer.receipts for row in receipts):
            break
        await asyncio.sleep(0.2)
        fixture.authorize()
    return {
        "test_only": True,
        "case": case,
        "receipts": receipts,
        "independent_receipts": [
            observer.receipts[row["message"]]
            for row in receipts
            if row["message"] in observer.receipts
        ],
    }
