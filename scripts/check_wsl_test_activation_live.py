"""Opt-in single LinXi activation message for the explicitly reset test group."""

import argparse
import asyncio
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from scripts.audit_wsl_finance import FINANCIAL_FIELDS

ROOT = Path(__file__).resolve().parents[1]
CONTROL = Path("/opt/backups/wsl-test-activation")
MEMBERSHIP = Path("/opt/backups/wsl-test-lab")
RESET_RECEIPT = Path("/opt/backups/wsl-test-reset-20261016/completion.json")
OWNER, PEER, ORDINARY = "31846829", "43506527", "23691273"
SENDER, RECEIVER = "wangshangliao_0acde7ec", "wangshangliao_f79c5b81"
INSTANCES = {
    OWNER: SENDER,
    PEER: RECEIVER,
    ORDINARY: "wangshangliao_398b2245",
}
GROUP = "1459282"
TEXT = "\u5f00\u901a"
ENABLE = f"/api/v1/bots/{SENDER}/enabled"
STATS = "/api/v1/bots/stats"


def require(condition, code):
    """Reject unsafe test admission independently of Python assertion flags.

    Args:
        condition: Mandatory safety condition.
        code: Non-secret, bounded failure identifier.

    Raises:
        RuntimeError: If the condition is false.
    """
    if not condition:
        raise RuntimeError(code)


class ActivationCheck:
    """Stop only the owner adapter, send once, close NIM, then restore the adapter."""

    def __init__(
        self, client, business, deployment, saved, pins, nim, ledger, journal, output
    ):
        """Bind fixed test credentials and a durable private operation journal.

        Args:
            client: Authenticated fixed-loopback dashboard client.
            business: Restored peer business client; no login operation is exposed.
            deployment: Existing native transport deployment.
            saved: Existing owner Vault session.
            pins: Vault-derived NIM pins for the three controlled identities.
            nim: Sole temporary owner transport, not connected yet.
            ledger: Private persistent outbox for this run.
            journal: Shared persistent SQLite test journal.
            output: Private local evidence directory.
        """
        self.client, self.business, self.deployment = client, business, deployment
        self.saved, self.pins, self.nim, self.ledger = saved, pins, nim, ledger
        self.journal, self.output = journal, output
        self.before = self.candidate = self.proof = None
        self.operation = None
        self.attempt = None
        self.retry_not_sent = None
        self.prior_attempts = []
        self.retry_proof = None
        self.tasks = []
        self.replies = []
        self.last_platform = 0
        self.disable_attempted = self.connected = False
        self.disable_confirmed = False
        self.disable_started = None
        self.original_stats = None
        self.pacing = None
        self.result = {
            "owner": OWNER,
            "bot": PEER,
            "group": GROUP,
            "reset_existing_group": True,
            "genuinely_new_group": False,
            "sent": False,
            "activation_verified": False,
            "restored": False,
        }

    def record(self, state, **values):
        """Persist bounded test evidence, never credentials or arbitrary push bodies.

        Args:
            state: Current phase identifier.
            **values: Fixed non-secret phase metadata.
        """
        self.result.update(state=state, **values)
        target = self.output / "evidence.json"
        with target.open("w") as stream:
            json.dump(self.result, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o600)

    def local(self):
        """Read fixed routing, original finance, formal bindings and reset evidence.

        Returns:
            Detached local snapshot without credentials or sensitive chat content.
        """
        config_bytes = (ROOT / "data/cmd_config.json").read_bytes()
        config = json.loads(config_bytes.decode("utf-8-sig"))
        bots = [b for b in config["platform"] if b.get("type") == "wangshangliao"]
        require(
            config.get("wangshangliao_open", {}).get("mode") == "integrated"
            and len(bots) == 2
            and {(b["id"], str(b.get("account_id"))) for b in bots}
            == {(SENDER, OWNER), (RECEIVER, PEER)},
            "fixed_integrated_pair_required",
        )
        paths = ROOT / "data/plugin_data"
        with closing(
            sqlite3.connect(
                (paths / "wangshangliao_cashback/cashback.sqlite3").as_uri()
                + "?mode=ro",
                uri=True,
            )
        ) as db:
            require(
                not db.execute("SELECT 1 FROM groups WHERE id=?", (GROUP,)).fetchone()
                and not db.execute(
                    "SELECT 1 FROM entries WHERE group_id=?", (GROUP,)
                ).fetchone(),
                "financial_test_group_forbidden",
            )
            finance = [
                db.execute(
                    f"SELECT {FINANCIAL_FIELDS} FROM entries ORDER BY id"
                ).fetchall(),
                db.execute("SELECT * FROM groups ORDER BY id").fetchall(),
            ]
        with closing(
            sqlite3.connect(
                (paths / "wangshangliao_open/open-service.sqlite3").as_uri()
                + "?mode=ro",
                uri=True,
            )
        ) as db:
            db.row_factory = sqlite3.Row
            candidate = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM tenants WHERE account=? AND group_id=?",
                    (PEER, GROUP),
                )
            ]
            owner = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM tenants WHERE account=? AND group_id=?",
                    (OWNER, GROUP),
                )
            ]
            formal = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM tenants WHERE group_id IN ('1516387','1488006') "
                    "ORDER BY account,group_id"
                )
            ]
            require(
                len(formal) == 4 and all(r["paused"] for r in formal),
                "formal_groups_must_stay_paused",
            )
            reset = db.execute(
                "SELECT created,confirmation FROM test_reset_audit WHERE account=? "
                "AND group_id=? ORDER BY id DESC LIMIT 1",
                (PEER, GROUP),
            ).fetchone()
            require(reset is not None, "authorized_reset_audit_required")
            require(len(candidate) == len(owner) == 1, "exact_test_tenants_required")
            departure = db.execute(
                "SELECT MAX(event_at) FROM membership_notices WHERE account=? AND team=? "
                "AND kind IN ('kick','leave') AND state='departure_verified' AND event_at<=?",
                (PEER, candidate[0]["team"], candidate[0]["joined"]),
            ).fetchone()[0]
        receipt = json.loads(RESET_RECEIPT.read_text())
        require(
            receipt.get("status") == "reset_complete"
            and receipt.get("account") == PEER
            and receipt.get("manager") == OWNER
            and receipt.get("group") == GROUP
            and receipt.get("reset_existing_group") is True
            and receipt.get("genuinely_new_group") is False
            and receipt.get("confirmation_sha256") == reset["confirmation"],
            "completed_reset_receipt_required",
        )
        require(len(candidate) == len(owner) == 1, "exact_test_tenants_required")
        return {
            "config": config,
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "protected": hashlib.sha256(
                json.dumps([finance, formal], sort_keys=True).encode()
            ).hexdigest(),
            "candidate": candidate[0],
            "owner": owner[0],
            "reset_at": reset["created"],
            "reset_confirmation": reset["confirmation"],
            "departure_fence": departure,
        }

    async def api(self, method, path, body=None):
        """Expose stats and the one fixed owner's enabled flag, without retries.

        Args:
            method: GET or PATCH.
            path: Fixed dashboard route.
            body: Exactly one boolean enabled field for the owner.

        Returns:
            Successful dashboard data.
        """
        require(
            (method, path, body) == ("GET", STATS, None)
            or method == "PATCH"
            and path == ENABLE
            and type(body) is dict
            and set(body) == {"enabled"}
            and type(body["enabled"]) is bool,
            "dashboard_operation_outside_activation_scope",
        )
        response = await self.client.request(method, path, json=body)
        response.raise_for_status()
        value = response.json()
        require(value.get("status") == "ok", "dashboard_operation_rejected")
        return value.get("data")

    async def platform_read(self, route, params):
        """Pace only fixed signed peer reads; never submit a business mutation.

        Args:
            route: Group roster or application-log route.
            params: Exact fixed test-group query or empty invitation query.

        Returns:
            Authenticated native response.
        """
        require(
            route == "/v1/group/get-apply-logs"
            and params == {}
            or route == "/v1/group/get-group-members"
            and params.get("groupId") == int(GROUP)
            and type(params.get("groupId")) is int
            and params.get("v") == "0"
            and not set(params) - {"groupId", "v", "cursor"},
            "business_read_outside_activation_scope",
        )
        await self.pace()

        def guard():
            require(str(self.business.fields[3]) == PEER, "peer_session_identity")
            if self.before is not None:
                self.guard(disabled=self.disable_attempted)

        try:
            return await self.business.request(route, params, authorize=guard)
        finally:
            self.finished()

    async def pace(self, *, write=False):
        """Observe shared TestLab completion deadlines without queuing other tools.

        Args:
            write: Also observe the thirty-second mutation interval.
        """
        require(self.pacing is not None, "shared_test_pacing_required")
        row = self.pacing.execute(
            "SELECT request_finished,write_finished,blocked_until FROM pacing WHERE id=1"
        ).fetchone()
        require(
            row is not None
            and row[2] <= time.time()
            and max(row[:2]) <= time.time() + 1,
            "shared_test_cooldown_or_clock_changed",
        )
        await asyncio.sleep(
            max(
                0,
                self.last_platform + 3 - time.time(),
                row[0] + 3 - time.time(),
                row[1] + 30 - time.time() if write else 0,
            )
        )

    def finished(self, *, write=False):
        """Persist completion-based pacing, including rejected/unknown requests.

        Args:
            write: Mark an attempted NIM activation mutation.
        """
        self.last_platform = time.time()
        if self.pacing is not None:
            self.pacing.execute(
                "UPDATE pacing SET request_finished=?,write_finished="
                "CASE WHEN ? THEN ? ELSE write_finished END WHERE id=1",
                (self.last_platform, write, self.last_platform),
            )
            self.pacing.commit()

    def guard(self, *, disabled, activating=False):
        """Recheck config, protected rows and pending membership before submission.

        Args:
            disabled: Whether this run disabled the owner adapter.
            activating: Allow the target's expected activation transition.
        """
        current = self.local()
        expected_config = copy.deepcopy(self.before["config"])
        sender = next(b for b in expected_config["platform"] if b["id"] == SENDER)
        sender["enable"] = not disabled
        require(
            current["config"] == expected_config
            and current["protected"] == self.before["protected"]
            and self.same_scope(current["owner"], self.before["owner"])
            and current["departure_fence"] == self.before["departure_fence"]
            and current["reset_confirmation"] == self.before["reset_confirmation"],
            "protected_state_or_configuration_changed",
        )
        if not activating:
            require(
                self.same_scope(current["candidate"], self.candidate),
                "pending_tenant_version_or_membership_changed",
            )
        return current

    @staticmethod
    def same_scope(current, expected):
        """Freeze authority and membership, excluding display/refresh metadata.

        Args:
            current: Fresh tenant row.
            expected: Reviewed tenant row.

        Returns:
            Whether every authority and membership field is unchanged.
        """
        fields = (
            "account",
            "group_id",
            "team",
            "version",
            "epoch",
            "rules",
            "managers",
            "active",
            "paused",
            "present",
            "cleanup_epoch",
            "cleanup_done",
            "admission",
            "native_role",
            "leave_state",
            "inviter",
            "evidence",
            "joined",
            "auto_claim",
        )
        return all(current.get(key) == expected.get(key) for key in fields)

    def owner_closed(self):
        """Require this run's post-close AstrBot diagnostic, not stats removal.

        Returns:
            Whether the awaited disable has a new post-NIM-close diagnostic.
        """
        if not self.disable_confirmed or self.disable_started is None:
            return False
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                "astrbot.service",
                "--no-pager",
                "-n",
                "512",
                "--since",
                f"@{self.disable_started:.6f}",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
        marker = f"Wangshangliao instance={SENDER} stage=connection outcome=stopped "
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
                message = row.get("MESSAGE", "")
                if isinstance(message, list):
                    if len(message) > 16384 or any(
                        type(byte) is not int or not 0 <= byte <= 255
                        for byte in message
                    ):
                        continue
                    message = bytes(message).decode("utf-8")
                if not isinstance(message, str) or len(message) > 16384:
                    continue
                message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)
                timestamp = int(row.get("__REALTIME_TIMESTAMP", 0))
            except (ValueError, TypeError, AttributeError, UnicodeError):
                continue
            if timestamp >= int(self.disable_started * 1e6) and marker in message:
                return True
        return False

    def review_not_sent_retry(self):
        """Verify an explicit recovery without modifying the previous attempt.

        Raises:
            RuntimeError: If any attempt or private outbox may have been submitted.
        """
        require(
            self.prior_attempts
            == self.journal.execute(
                "SELECT id,epoch,send_state,restore_state,created FROM attempts "
                "ORDER BY created,id"
            ).fetchall(),
            "prior_attempt_journal_changed",
        )
        matches = []
        for directory in CONTROL.glob("run-*"):
            if directory == self.output:
                continue
            require(
                re.fullmatch(r"run-[0-9a-f]{32}", directory.name)
                and directory.is_dir()
                and not directory.is_symlink()
                and directory.stat().st_uid == os.geteuid()
                and not directory.stat().st_mode & 0o077,
                "private_retry_evidence_directory_required",
            )
            path = directory / "evidence.json"
            require(
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_uid == os.geteuid()
                and path.stat().st_size <= 16384,
                "private_retry_evidence_required",
            )
            raw = path.read_bytes()
            value = json.loads(raw)
            if value.get("attempt", value.get("operation")) == self.retry_not_sent:
                matches.append((directory, raw, value))
        require(len(matches) == 1, "unique_prior_attempt_evidence_required")
        directory, raw, value = matches[0]
        require(
            value.get("operation") == self.operation
            and value.get("owner") == OWNER
            and value.get("bot") == PEER
            and value.get("group") == GROUP
            and value.get("epoch") == self.candidate["epoch"]
            and str(value.get("invitation_id")) == str(self.proof["applyId"])
            and value.get("reset_existing_group") is True
            and value.get("genuinely_new_group") is False
            and value.get("sent") is False
            and value.get("restored") is True
            and value.get("activation_verified") is False,
            "prior_attempt_not_proven_unsent_and_restored",
        )
        outbox = directory / "sender.sqlite3"
        require(
            outbox.is_file()
            and not outbox.is_symlink()
            and outbox.stat().st_uid == os.geteuid(),
            "prior_attempt_outbox_required",
        )
        with closing(sqlite3.connect(outbox.as_uri() + "?mode=ro", uri=True)) as db:
            require(
                db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                and db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0,
                "prior_attempt_outbox_nonempty_never_retry",
            )
        self.retry_proof = {
            "parent": self.retry_not_sent,
            "evidence_sha256": hashlib.sha256(raw).hexdigest(),
            "outbox_rows": 0,
            "original_row": self.prior_attempts[-1],
        }

    async def preflight(self):
        """Verify fresh invitation, complete native roster and exact three Vault pins."""
        from astrbot.core.platform.sources.wangshangliao.invitations import (
            latest_membership_events,
        )

        self.prior_attempts = self.journal.execute(
            "SELECT id,epoch,send_state,restore_state,created FROM attempts "
            "ORDER BY created,id"
        ).fetchall()
        require(
            (not self.prior_attempts and self.retry_not_sent is None)
            or self.prior_attempts
            and self.retry_not_sent is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.retry_not_sent)
            and self.prior_attempts[-1][0] == self.retry_not_sent
            and all(
                row[2:4] == ("not_sent", "verified") for row in self.prior_attempts
            ),
            "prior_activation_attempt_exists_never_resend_or_reconnect",
        )
        require(
            set(self.pins) == {OWNER, PEER, ORDINARY}
            and len(set(self.pins.values())) == 3
            and all(
                isinstance(v, str) and v.isascii() and v.isdigit()
                for v in self.pins.values()
            )
            and str(self.saved["business"]["uid"]) == OWNER
            and str(self.saved["nim_id"]) == self.pins[OWNER],
            "fixed_vault_identity_required",
        )
        state = self.local()
        candidate, owner = state["candidate"], state["owner"]
        require(
            all(
                b.get("enable") is True
                for b in state["config"]["platform"]
                if b["id"] in {SENDER, RECEIVER}
            )
            and owner["present"]
            and not owner["active"]
            and candidate["present"]
            and not candidate["active"]
            and not candidate["paused"]
            and json.loads(candidate["managers"]) == []
            and candidate["admission"] in {"pending", "verified"}
            and candidate["inviter"] in {"", OWNER}
            and candidate["auto_claim"]
            and candidate["joined"] >= state["reset_at"]
            and candidate["team"] == owner["team"]
            and candidate["native_role"] == "GROUP_ROLE_MEMBER"
            and candidate["cleanup_epoch"] <= candidate["cleanup_done"],
            "fresh_reset_candidate_required",
        )
        await self.roster()
        logs = await self.platform_read("/v1/group/get-apply-logs", {})
        latest = latest_membership_events(logs, GROUP).get(PEER)
        proof = latest["row"] if latest else {}
        require(
            latest
            and not latest["ambiguous"]
            and latest["timestamp"] > state["reset_at"]
            and (
                latest["timestamp"] > state["departure_fence"]
                if state["departure_fence"] is not None
                else latest["timestamp"] >= candidate["joined"] - 120
            )
            and proof.get("groupApplyType") == "INVITATION"
            and proof.get("state") == "MEMBER_STATE_GOOD"
            and str(proof.get("applicant", {}).get("nimId")) == self.pins[PEER]
            and str(proof.get("inviter", {}).get("userId")) == OWNER,
            "fresh_successful_linxi_invitation_required",
        )
        self.before, self.candidate, self.proof = state, candidate, proof
        self.guard(disabled=False)
        stats = await self.api("GET", STATS)
        require(isinstance(stats.get("platforms"), list), "invalid_bot_statistics")
        self.original_stats = {
            r["id"]: (r.get("status"), r.get("connection_state"))
            for r in stats["platforms"]
        }
        require(
            all(
                self.original_stats.get(instance) == ("running", "online")
                for instance in (SENDER, RECEIVER)
            ),
            "original_pair_connections_must_be_online",
        )
        self.operation = hashlib.sha256(
            json.dumps(
                [
                    PEER,
                    GROUP,
                    candidate["epoch"],
                    proof["applyId"],
                    state["reset_confirmation"],
                ]
            ).encode()
        ).hexdigest()
        self.attempt = self.operation
        if self.prior_attempts:
            require(
                self.prior_attempts[-1][1] == candidate["epoch"],
                "prior_attempt_epoch_changed",
            )
            self.review_not_sent_retry()
            self.attempt = hashlib.sha256(
                json.dumps(
                    [self.operation, self.retry_not_sent, uuid.uuid4().hex]
                ).encode()
            ).hexdigest()
        self.record(
            "preflight_verified",
            epoch=candidate["epoch"],
            version=candidate["version"],
            invitation_id=str(proof["applyId"]),
            departure_fence=state["departure_fence"],
            operation=self.operation,
            attempt=self.attempt,
            retry_of=self.retry_not_sent,
            retry_proof=self.retry_proof,
        )

    async def roster(self):
        """Recheck the three fixed native identities before each connection/send."""
        from astrbot.core.platform.sources.wangshangliao.directory import (
            member_directory,
        )

        directory = await member_directory(
            SimpleNamespace(request=self.platform_read), GROUP
        )
        members = {str(r["userId"]): r for r in directory["groupMemberInfo"]}
        require(
            directory["complete"]
            and set(members) == {OWNER, PEER, ORDINARY}
            and all(str(members[uid]["nimId"]) == pin for uid, pin in self.pins.items())
            and all(
                m.get("accountState") == "ACCOUNT_STATE_GOOD" for m in members.values()
            )
            and members[OWNER]["groupRole"] == "GROUP_ROLE_OWNER"
            and members[PEER]["groupRole"] == "GROUP_ROLE_MEMBER"
            and members[ORDINARY]["groupRole"] == "GROUP_ROLE_MEMBER",
            "fixed_roster_or_native_role_changed",
        )

    async def wait_owner(self, *, online):
        """Verify only the owner's connection changed, using bounded local polling.

        Args:
            online: Expected final runtime state.
        """
        for _ in range(40):
            stats = await self.api("GET", STATS)
            rows = stats.get("platforms", stats.get("bots", []))
            require(isinstance(rows, list), "invalid_bot_statistics")
            sender = [r for r in rows if r.get("id") == SENDER]
            peer = [r for r in rows if r.get("id") == RECEIVER]
            require(
                len(peer) == 1
                and peer[0].get("status") == "running"
                and peer[0].get("connection_state") == "online",
                "peer_must_remain_online",
            )
            if online:
                if (
                    len(sender) == 1
                    and sender[0].get("status") == "running"
                    and sender[0].get("connection_state") == "online"
                ):
                    return
            elif not sender or all(
                r.get("status") == "stopped"
                and r.get("connection_state")
                not in {"online", "connecting", "reconnecting"}
                for r in sender
            ):
                if self.owner_closed():
                    self.record("owner_transport_closed", owner_transport_closed=True)
                    return
            await asyncio.sleep(0.5)
        raise RuntimeError("owner_connection_transition_not_verified")

    async def witness(self):
        """ACK valid pushes but retain only this group's peer activation reply."""
        from astrbot.core.platform.sources.wangshangliao import wire
        from astrbot.core.platform.sources.wangshangliao.nim import messages

        while True:
            packet = await self.nim.pushes.get()
            incoming, _ = messages(packet)
            for outer in incoming:
                if (
                    outer.get(0) == "1"
                    and outer.get(1) == self.candidate["team"]
                    and outer.get(2) == self.pins[PEER]
                    and outer.get(8) == "100"
                ):
                    inner = wire.open_message(
                        self.deployment.key("message_key"), outer.get(10, "")
                    )
                    if (
                        inner.session == 2
                        and str(inner.sender.id) == PEER
                        and str(inner.target.id) == GROUP
                        and inner.HasField("content")
                    ):
                        text = inner.content.data
                        if "\u5df2\u5f00\u901a" in text:
                            self.replies.append(text[:2000])
                await self.nim.acknowledge(int(outer[0]), outer[12])

    async def heartbeat(self):
        """Keep the single temporary owner connection alive without reconnecting."""
        while True:
            await asyncio.sleep(8)
            await self.nim.heartbeat()

    async def send_once(self):
        """Seal exactly one real-owner group activation message with durable intent."""
        from astrbot.core.platform.sources.wangshangliao import wire

        await self.roster()
        await self.pace(write=True)
        self.guard(disabled=True)
        require(
            not any(task.done() for task in self.tasks)
            and str(self.saved["business"]["uid"]) == OWNER
            and str(self.saved["nim_id"]) == self.pins[OWNER],
            "owner_connection_or_identity_changed",
        )
        key = "test-activation/" + self.operation
        nonce, state = await self.ledger.reserve(key, json.dumps([OWNER, GROUP, TEXT]))
        require(state == "new", "durable_outbox_already_reserved_never_resend")
        client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        inner = wire.ApplicationMessage(
            sender=wire.Source(id=int(OWNER)),
            target=wire.Source(id=int(GROUP)),
            created_at=int(time.time() * 1000),
            device=1,
            session=2,
            version=2,
            format=0,
            client_id=client_id,
            content=wire.Content(data=TEXT),
        )
        attachment = wire.seal_message(
            self.deployment.key("message_key"),
            inner,
            int(nonce),
            secrets.randbits(64) or 1,
            int(time.time()),
        )
        payload = wire.properties(
            [
                (0, b"1"),
                (1, self.candidate["team"].encode()),
                (8, b"100"),
                (10, attachment.encode()),
                (11, client_id.encode()),
            ]
        )
        self.guard(disabled=True)
        self.journal.execute(
            "UPDATE attempts SET send_state='unknown' WHERE id=?", (self.attempt,)
        )
        self.journal.commit()
        try:
            code, body = await self.nim.request(8, 2, payload)
            if code != 200:
                await self.ledger.finish(key, "rejected")
                self.journal.execute(
                    "UPDATE attempts SET send_state='rejected' WHERE id=?",
                    (self.attempt,),
                )
                self.journal.commit()
                raise RuntimeError("activation_send_rejected_no_retry")
            fields, end = wire.read_properties(body)
            require(
                end == len(body)
                and fields.get(12, b"").isdigit()
                and fields.get(7, b"").isdigit()
                and fields.get(11, client_id.encode()) == client_id.encode(),
                "activation_receipt_unknown_no_retry",
            )
            await self.ledger.finish(key, "accepted", fields[12].decode())
            self.journal.execute(
                "UPDATE attempts SET send_state='accepted' WHERE id=?",
                (self.attempt,),
            )
            self.journal.commit()
            self.record("activation_submitted", sent=True, message=fields[12].decode())
        except BaseException:
            # Preserve the original unknown/rejected intent; no retry or new key.
            raise
        finally:
            self.finished(write=True)

    async def cleanup(self):
        """Close temporary NIM before restoring only this run's owner enable flag."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.nim.close()
        require(
            self.nim.closed.is_set()
            and (self.nim.socket is None or self.nim.socket.closed)
            and self.nim.reader is None,
            "temporary_nim_close_unproven_do_not_restore_owner",
        )
        if self.disable_attempted:
            current = self.local()
            config = copy.deepcopy(self.before["config"])
            sender = next(b for b in config["platform"] if b["id"] == SENDER)
            sender["enable"] = False
            if current["config"] == config:
                await self.pace()
                await self.api("PATCH", ENABLE, {"enabled": True})
            else:
                require(
                    current["config"] == self.before["config"],
                    "concurrent_config_change_requires_manual_restore",
                )
            await self.wait_owner(online=True)
            stats = await self.api("GET", STATS)
            require(
                {
                    r["id"]: (r.get("status"), r.get("connection_state"))
                    for r in stats["platforms"]
                }
                == self.original_stats,
                "other_platform_runtime_state_changed",
            )
            current = self.local()
            require(
                current["config_sha256"] == self.before["config_sha256"]
                and current["protected"] == self.before["protected"]
                and self.same_scope(current["owner"], self.before["owner"]),
                "restored_config_or_protected_state_mismatch",
            )
            self.journal.execute(
                "UPDATE attempts SET restore_state='verified' WHERE id=?",
                (self.attempt,),
            )
            self.journal.commit()
            self.record("owner_restored", restored=True)

    async def run(self):
        """Execute one opt-in activation and restore the original owner connection."""
        await self.preflight()
        if self.retry_not_sent is not None:
            self.review_not_sent_retry()
        self.journal.execute(
            "INSERT INTO attempts VALUES(?,?,'not_sent','unknown',?)",
            (self.attempt, self.candidate["epoch"], time.time()),
        )
        if self.retry_not_sent is not None:
            self.journal.execute(
                "CREATE TABLE IF NOT EXISTS attempt_recoveries("
                "parent TEXT UNIQUE,child TEXT PRIMARY KEY,proof TEXT NOT NULL,created REAL)"
            )
            self.journal.execute(
                "INSERT INTO attempt_recoveries VALUES(?,?,?,?)",
                (
                    self.retry_not_sent,
                    self.attempt,
                    json.dumps(self.retry_proof, sort_keys=True),
                    time.time(),
                ),
            )
        self.journal.commit()
        await self.ledger.open()
        try:
            self.guard(disabled=False)
            self.disable_attempted = True
            self.disable_started = time.time()
            await self.api("PATCH", ENABLE, {"enabled": False})
            self.disable_confirmed = True
            await self.wait_owner(online=False)
            self.guard(disabled=True)
            await self.roster()
            await asyncio.sleep(3)
            await self.nim.connect(
                self.deployment, self.pins[OWNER], self.saved["nim_token"]
            )
            self.connected = True
            self.finished()
            self.tasks = [
                asyncio.create_task(self.witness()),
                asyncio.create_task(self.heartbeat()),
            ]
            await self.send_once()
            for _ in range(100):
                current = self.guard(disabled=True, activating=True)
                tenant = current["candidate"]
                if tenant["active"] and json.loads(tenant["managers"]) == [OWNER]:
                    require(
                        tenant["epoch"] == self.candidate["epoch"]
                        and tenant["present"]
                        and not tenant["paused"]
                        and tenant["inviter"] == OWNER
                        and tenant["admission"] == "verified"
                        and tenant["native_role"] == "GROUP_ROLE_MEMBER",
                        "activation_authority_or_native_role_mismatch",
                    )
                    warning = any(
                        "\u7fa4\u7ba1\u7406\u5458" in text and "\u8bbe" in text
                        for text in self.replies
                    )
                    if warning:
                        self.record(
                            "activation_verified",
                            activation_verified=True,
                            native_role="GROUP_ROLE_MEMBER",
                            requires_native_admin=True,
                            reply=self.replies[-1],
                        )
                        return self.result
                require(
                    not any(task.done() for task in self.tasks),
                    "temporary_transport_worker_failed",
                )
                await asyncio.sleep(0.5)
            raise RuntimeError("activation_or_missing_privilege_warning_not_verified")
        finally:
            cleanup = asyncio.create_task(self.cleanup())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                await self.ledger.close()


async def main():
    """Require explicit apply; default preview creates no files or connections."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--retry-not-sent",
        metavar="ATTEMPT_SHA256",
        help="Explicitly recover only the latest proven-unsent, restored attempt",
    )
    args = parser.parse_args()
    if args.retry_not_sent is not None and not re.fullmatch(
        r"[0-9a-f]{64}", args.retry_not_sent
    ):
        parser.error("--retry-not-sent requires the exact previous attempt SHA-256")
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "owner": OWNER,
                    "bot": PEER,
                    "group": GROUP,
                    "message": TEXT,
                    "temporarily_disable": SENDER,
                    "reset_existing_group": True,
                    "genuinely_new_group": False,
                    "restore": "Close temporary NIM before restoring original enabled state",
                    "no_retries": True,
                    "retry_not_sent": args.retry_not_sent,
                }
            )
        )
        return
    import aiohttp
    import httpx
    import jwt

    from astrbot.core.platform.sources.wangshangliao.business import (
        BusinessClient,
        Deployment,
    )
    from astrbot.core.platform.sources.wangshangliao.nim import NimClient
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger, Vault

    CONTROL.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(
        not CONTROL.is_symlink()
        and CONTROL.stat().st_uid == os.geteuid()
        and not CONTROL.stat().st_mode & 0o077,
        "private_control_directory_required",
    )
    require(
        MEMBERSHIP.is_dir()
        and not MEMBERSHIP.is_symlink()
        and MEMBERSHIP.stat().st_uid == os.geteuid()
        and not MEMBERSHIP.stat().st_mode & 0o077,
        "shared_test_lab_lock_required",
    )
    own_fd = os.open(
        CONTROL / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    shared_fd = os.open(
        MEMBERSHIP / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(own_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(shared_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not (CONTROL / "journal.sqlite3").is_symlink(), "unsafe_journal_path")
        pacing_path = MEMBERSHIP / "control.sqlite3"
        require(
            pacing_path.is_file() and not pacing_path.is_symlink(),
            "shared_test_pacing_required",
        )
        with (
            closing(sqlite3.connect(CONTROL / "journal.sqlite3")) as journal,
            closing(sqlite3.connect(pacing_path)) as pacing,
        ):
            journal.execute(
                "CREATE TABLE IF NOT EXISTS attempts("
                "id TEXT PRIMARY KEY,epoch INTEGER,send_state TEXT,restore_state TEXT,created REAL)"
            )
            journal.commit()
            (CONTROL / "journal.sqlite3").chmod(0o600)
            require(
                not journal.execute("SELECT 1 FROM attempts LIMIT 1").fetchone()
                or args.retry_not_sent is not None,
                "prior_activation_attempt_requires_manual_review",
            )
            output = CONTROL / ("run-" + uuid.uuid4().hex)
            output.mkdir(mode=0o700)
            config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
            saved = {uid: Vault(instance).load() for uid, instance in INSTANCES.items()}
            require(
                all(
                    value and str(value["business"]["uid"]) == uid
                    for uid, value in saved.items()
                ),
                "fixed_vault_identity_required",
            )
            token = jwt.encode(
                {
                    "username": config["dashboard"]["username"],
                    "exp": int(time.time()) + 600,
                },
                config["dashboard"]["jwt_secret"],
                algorithm="HS256",
            )
            async with (
                httpx.AsyncClient(
                    base_url="http://127.0.0.1:6185",
                    trust_env=False,
                    timeout=30,
                    headers={"Authorization": "Bearer " + token},
                ) as client,
                aiohttp.ClientSession() as http,
            ):
                deployment = Deployment.load()
                peer = BusinessClient(deployment, http)
                peer.restore(saved[PEER]["business"])
                check = ActivationCheck(
                    client,
                    peer,
                    deployment,
                    saved[OWNER],
                    {uid: str(value["nim_id"]) for uid, value in saved.items()},
                    NimClient(http),
                    Ledger(output / "sender.sqlite3"),
                    journal,
                    output,
                )
                check.pacing = pacing
                check.retry_not_sent = args.retry_not_sent
                try:
                    await check.run()
                except BaseException as exc:
                    check.record("incomplete", error_type=type(exc).__name__)
                    raise
                print(json.dumps(check.result, ensure_ascii=True, indent=2))
    finally:
        os.close(shared_fd)
        os.close(own_fd)


if __name__ == "__main__":
    asyncio.run(main())
