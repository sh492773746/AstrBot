"""Explicit live flood probes from the owner's retired ordinary test account."""

import argparse
import asyncio
import copy
import json
import secrets
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import aiohttp
import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.directory import member_directory
from astrbot.core.platform.sources.wangshangliao.moderation import execute
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.storage import Ledger, Vault
from scripts.check_wsl_authorization_live import BOT, GROUP, PROFILE, RECEIVER, rows
from scripts.check_wsl_migration_dialogue import PREFIX, ReadOnlyDialogue

ORDINARY_INSTANCE = "wangshangliao_398b2245"
ORDINARY_ACCOUNT = "23691273"


class LiveFlood:
    """Run bounded account-specific probes without changing moderation policy."""

    def __init__(self, client, http, output):
        self.client = client
        self.output = output
        self.dashboard = ReadOnlyDialogue(client, output)
        self.deployment = Deployment.load()
        self.sender = BusinessClient(self.deployment, http)
        self.admin = BusinessClient(self.deployment, http)
        self.nim = None
        self.ledger = Ledger(output / "sender.sqlite3")
        self.marker = "WSL_FLOOD_LIVE_" + uuid.uuid4().hex[:10]
        self.original_profile = None
        self.original_settings = None
        self.profile_changed = False
        self.semantic_changed = False
        self.mute_possible = False
        self.tasks = []
        self.events = []
        self.sent_receipts = {}
        self.result = {"marker": self.marker, "cases": [], "cleanup": {}}

    async def preflight(self):
        """Verify the saved identity, ordinary role, production grants and baseline.

        Raises:
            AssertionError: If a test could affect a protected or already-muted member.
        """
        saved = Vault(ORDINARY_INSTANCE).load()
        assert saved and str(saved["business"]["uid"]) == ORDINARY_ACCOUNT
        config = json.loads(
            Path("data/cmd_config.json").read_text(encoding="utf-8-sig")
        )
        assert not any(
            b.get("type") == "wangshangliao"
            and str(b.get("account_id", "")) == ORDINARY_ACCOUNT
            for b in config.get("platform", [])
        ), "Ordinary test sender is still a managed bot"
        self.sender.restore(saved["business"])
        self.admin.restore(Vault(RECEIVER).load()["business"])
        assert str(self.admin.fields[3]) == BOT
        self.original_settings = await self.dashboard.api("GET", PREFIX + "/settings")
        receiver = next(
            b for b in self.original_settings["bots"] if b["id"] == RECEIVER
        )
        assert receiver["state"] == "online" and receiver["business_active"]
        assert (
            str(receiver["account_id"]) == BOT and GROUP in receiver["enabled_groups"]
        )
        policy = receiver["moderation"]
        assert all(
            policy.get(field)
            for field in (
                "enabled",
                "automation_enabled",
                "recall_enabled",
                "progressive_mute",
            )
        )
        for action in ("mute", "unmute", "recall"):
            authorize_action(receiver, GROUP, action)
        for bot in self.original_settings["bots"]:
            state = await self.dashboard.api(
                "GET", PREFIX + "/test-window?bot_id=" + bot["id"]
            )
            assert not state["window"].get("active"), "Existing test window"
        tables = {
            row["name"]
            for row in rows(
                RECEIVER,
                "moderation.sqlite3",
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
        assert "automatic_mutes" not in tables or not rows(
            RECEIVER,
            "moderation.sqlite3",
            "SELECT 1 FROM automatic_mutes WHERE account=? AND group_id=? AND member=? "
            "AND closed=0 AND status='unknown'",
            (BOT, GROUP, ORDINARY_ACCOUNT),
        ), "Existing unknown sanction requires review first"
        latest = rows(
            RECEIVER,
            "moderation.sqlite3",
            "SELECT result FROM operations WHERE json_extract(result,'$.group')=? "
            "AND json_extract(result,'$.member')=? "
            "AND json_extract(result,'$.action') IN ('mute','unmute') ORDER BY rowid DESC LIMIT 1",
            (int(GROUP), int(ORDINARY_ACCOUNT)),
        )
        assert latest and json.loads(latest[0]["result"]).get("action") == "unmute", (
            "Test target has no known restored unmute baseline"
        )
        assert json.loads(latest[0]["result"]).get("status") in {"accepted", "verified"}
        assert not rows(
            RECEIVER,
            "messages.sqlite3",
            "SELECT 1 FROM inbox WHERE account=? AND team=? AND sender=? AND received_at>? LIMIT 1",
            (BOT, GROUP, ORDINARY_ACCOUNT, time.time() - 60),
        ), "The ordinary test account has recent concurrent activity"
        roster = await member_directory(self.admin, GROUP)
        target = next(
            m for m in roster["groupMemberInfo"] if m["userId"] == ORDINARY_ACCOUNT
        )
        assert roster["complete"] and target["groupRole"] == "GROUP_ROLE_MEMBER"
        assert target["accountState"] == "ACCOUNT_STATE_GOOD"
        assert str(target["nimId"]) == str(saved["nim_id"])
        sender_groups = await self.sender.request(
            "/v1/group/get-group-list", {"v": "0"}
        )
        entry = next(
            g
            for g in sender_groups["owner"] + sender_groups["member"]
            if str(g["groupId"]) == GROUP
        )
        self.team = str(entry["groupCloudId"])
        self.peer = str(saved["nim_id"])
        self.original_profile = (
            await self.dashboard.api("GET", "/api/v1/config-profiles/" + PROFILE)
        )["config"]
        self.dashboard.save("original-profile.json", self.original_profile)
        self.dashboard.save("original-settings.json", self.original_settings)
        self.result["preflight"] = {
            "sender": ORDINARY_ACCOUNT,
            "group": GROUP,
            "role": target["groupRole"],
            "managed": False,
            "chat_admin": ORDINARY_ACCOUNT
            in [str(v) for v in self.original_profile.get("admins_id", [])],
            "last_member_action": "unmute",
        }
        self.original_invite_rules = rows(
            RECEIVER,
            "activities.sqlite3",
            "SELECT * FROM invite_rules WHERE account=? AND group_id=? AND owner=?",
            (BOT, GROUP, ORDINARY_ACCOUNT),
        )
        self.dashboard.save("original-invite-rules.json", self.original_invite_rules)
        self.result["pid"] = (
            await asyncio.to_thread(
                subprocess.check_output,
                ["systemctl", "show", "astrbot.service", "-p", "MainPID", "--value"],
                text=True,
            )
        ).strip()
        self.nim_token = saved["nim_token"]
        print(
            "PASS ordinary saved account, group role, grants and unmute baseline",
            flush=True,
        )

    async def send(self, text, label, *, enrollment=False):
        """Send exactly one scoped group message, never retry an unknown result.

        Args:
            text: Bounded test text containing this run's marker.
            label: Unique local operation suffix.
            enrollment: Allow the exact public enrollment command, not arbitrary text.

        Returns:
            Transport code and exact platform message routing receipt.
        """
        assert (text == "参加抽奖" if enrollment else self.marker in text) and len(
            text
        ) < 160
        assert str(self.sender.fields[3]) == ORDINARY_ACCOUNT
        key = self.marker + "/" + label
        nonce, state = await self.ledger.reserve(key, json.dumps([GROUP, text]))
        assert state == "new", "Probe operation cannot be retried"
        client_id = str(uuid.uuid4())
        inner = wire.ApplicationMessage(
            sender=wire.Source(id=int(ORDINARY_ACCOUNT)),
            target=wire.Source(id=int(GROUP)),
            created_at=int(time.time() * 1000),
            device=1,
            session=2,
            version=2,
            format=0,
            client_id=client_id,
            content=wire.Content(data=text),
        )
        attachment = wire.seal_message(
            self.deployment.key("message_key"),
            inner,
            int(nonce),
            secrets.randbits(64) or 1,
            int(time.time()),
        )
        try:
            code, body = await self.nim.request(
                8,
                2,
                wire.properties(
                    [
                        (0, b"1"),
                        (1, self.team.encode()),
                        (8, b"100"),
                        (10, attachment.encode()),
                        (11, client_id.encode()),
                    ]
                ),
            )
            if code != 200:
                await self.ledger.finish(key, "rejected")
                return {"code": code, "status": "rejected", "client": client_id}
            fields, end = wire.read_properties(body)
            assert end == len(body) and fields[12].isdigit() and fields[7].isdigit()
            assert fields.get(11, client_id.encode()) == client_id.encode()
            await self.ledger.finish(key, "accepted", fields[12].decode())
            receipt = {
                "code": code,
                "status": "accepted",
                "client": client_id,
                "message": fields[12].decode(),
                "time": fields[7].decode(),
            }
            self.sent_receipts[receipt["message"]] = receipt
            return receipt
        except BaseException:
            await self.ledger.finish(key, "unknown")
            raise

    async def witness(self):
        """Drain sender notifications and ACK messages without storing others' text."""
        from astrbot.core.platform.sources.wangshangliao.nim import messages

        while True:
            packet = await self.nim.pushes.get()
            service, command, _, code, body = packet
            event = {
                "service": service,
                "command": command,
                "code": code,
                "size": len(body),
            }
            if (service, command, code) == (4, 1, 200):
                try:
                    nested = wire.read_packet(body[8:])
                    if (nested[0], nested[1], nested[3]) == (7, 14, 200):
                        fields, end = wire.read_properties(nested[4])
                        mid = fields.get(11, b"").decode("ascii")
                        receipt = self.sent_receipts.get(mid)
                        if (
                            end == len(nested[4])
                            and receipt
                            and fields.get(1) == b"8"
                            and fields.get(2) == self.team.encode()
                            and fields.get(3) == self.peer.encode()
                            and fields.get(10) == receipt["client"].encode()
                            and fields.get(14) == receipt["time"].encode()
                        ):
                            event["recall"] = {
                                "message": mid,
                                "team": self.team,
                                "sender": self.peer,
                                "client": receipt["client"],
                                "original_time": receipt["time"],
                                "notified_at_ms": fields.get(0, b"").decode("ascii"),
                            }
                except (wire.ProtocolError, UnicodeDecodeError):
                    pass
            self.events.append(event)
            incoming, _ = messages(packet)
            for message in incoming:
                await self.nim.acknowledge(int(message[0]), message[12])
            self.dashboard.save("sender-notifications.json", self.events)

    async def heartbeat(self):
        """Keep the ordinary sender online only for the bounded acceptance run."""
        while True:
            await asyncio.sleep(8)
            await self.nim.heartbeat()

    async def unmute(self, label):
        """Restore the named member through the existing guarded human action path.

        Args:
            label: Fresh cleanup operation suffix.

        Returns:
            Independent unmute receipt, never inferred from the original mute.
        """

        async def authorize():
            settings = await self.dashboard.api("GET", PREFIX + "/settings")
            receiver = next(b for b in settings["bots"] if b["id"] == RECEIVER)
            assert str(receiver["account_id"]) == BOT
            authorize_action(receiver, GROUP, "unmute")

        result = await execute(
            self.admin,
            RECEIVER,
            BOT,
            "flood-test-unmute/" + self.marker[-10:] + "/" + label,
            "unmute",
            int(GROUP),
            int(ORDINARY_ACCOUNT),
            authorize=authorize,
        )
        self.dashboard.save("unmute-" + label + ".json", result)
        assert result["status"] in {"accepted", "verified"}, (
            "Cleanup unmute not accepted"
        )
        self.mute_possible = False
        return result

    async def burst(self, label, count, same):
        """Send one real burst and read the complete production action evidence.

        Args:
            label: Threshold case name.
            count: Exactly five equal or eight different messages.
            same: Whether every message should use identical text.

        Returns:
            Exact mute and per-message recall evidence for this burst.
        """
        assert (count, same) in {(5, True), (8, False)}
        start = time.monotonic()
        receipts = []
        self.mute_possible = True
        for n in range(count):
            text = self.marker + "-" + label + ("" if same else "-" + str(n))
            result = await self.send(text, label + "-" + str(n))
            self.dashboard.save(label + "-send-" + str(n) + ".json", result)
            assert result["status"] == "accepted", "Burst send was not accepted"
            receipts.append(result)
        elapsed = time.monotonic() - start
        assert elapsed < (10 if not same else 30), "Burst exceeded the detector window"
        mids = [r["message"] for r in receipts]
        operation = None
        rec = []
        trigger = []
        # Earlier ordinary messages can already be in the real semantic-review lane.
        # Wait for their bounded processing without disabling production AI review.
        for _ in range(480):
            rec = rows(
                RECEIVER,
                "moderation.sqlite3",
                "SELECT message,batch,status FROM flood_recalls WHERE account=? "
                "AND group_id=? AND message IN (" + ",".join("?" for _ in mids) + ")",
                (BOT, GROUP, *mids),
            )
            if len(rec) == count:
                assert len({row["batch"] for row in rec}) == 1
                operation = rec[0]["batch"]
                batch = rows(
                    RECEIVER,
                    "moderation.sqlite3",
                    "SELECT * FROM flood_batches WHERE operation=?",
                    (operation,),
                )[0]
                trigger = rows(
                    RECEIVER,
                    "messages.sqlite3",
                    "SELECT message,payload FROM inbox WHERE account=? AND team=? AND message=?",
                    (BOT, GROUP, batch["trigger"]),
                )
                if batch["mute_status"] in {"accepted", "verified"} and all(
                    r["status"] in {"accepted", "verified"} for r in rec
                ):
                    break
            await asyncio.sleep(0.25)
        assert (
            operation
            and len(rec) == count
            and all(r["status"] in {"accepted", "verified"} for r in rec)
        ), "Production flood recall chain did not complete"
        mute = rows(
            RECEIVER,
            "moderation.sqlite3",
            "SELECT result FROM operations WHERE id=?",
            (operation,),
        )
        assert len(mute) == 1
        mute = json.loads(mute[0]["result"])
        assert mute["status"] in {"accepted", "verified"} and mute["action"] == "mute"
        assert str(mute["member"]) == ORDINARY_ACCOUNT
        evidence = json.loads(trigger[0]["payload"])["_ingress_flood"]
        assert set(evidence["messages"]) == set(mids)
        case = {
            "name": label,
            "count": count,
            "elapsed": elapsed,
            "messages": mids,
            "mute": mute,
            "recalls": rec,
            "evidence": evidence,
        }
        self.dashboard.save(label + "-evidence.json", case)
        self.result["cases"].append(case)
        print(
            "PASS real production burst:",
            label,
            count,
            "messages;",
            "mute=",
            mute["status"],
            "minutes=",
            mute["minutes"],
            "recalls=",
            len(rec),
            flush=True,
        )
        await asyncio.sleep(1)
        # A rejected send is independent platform enforcement evidence. Acceptance
        # is recorded as a limitation, never presented as confirmed mute effect.
        probe = await self.send(
            self.marker + "-" + label + "-mute-check", label + "-mute-check"
        )
        case["muted_send_probe"] = probe
        if probe["status"] == "accepted":
            status, _ = await self.nim.request(
                7,
                13,
                wire.properties(
                    [
                        (0, probe["time"].encode()),
                        (1, b"8"),
                        (2, self.team.encode()),
                        (3, self.peer.encode()),
                        (10, probe["client"].encode()),
                        (11, probe["message"].encode()),
                        (16, self.peer.encode()),
                    ]
                ),
            )
            case["own_probe_cleanup_code"] = status
        print(
            "Mute enforcement send probe:",
            label,
            probe["status"],
            probe["code"],
            flush=True,
        )
        await self.unmute(label)
        for _ in range(20):
            notifications = {
                event["recall"]["message"]: event["recall"]
                for event in self.events
                if event.get("recall", {}).get("message") in mids
            }
            if set(notifications) == set(mids):
                break
            await asyncio.sleep(0.25)
        case["sender_recall_notifications"] = notifications
        assert set(notifications) == set(mids), (
            "The ordinary sender did not witness every exact burst recall"
        )
        trigger_ms = int(self.sent_receipts[evidence["message"]]["time"])
        notified_ms = [
            int(receipt["notified_at_ms"]) for receipt in notifications.values()
        ]
        case["response_seconds"] = {
            "first_recall": (min(notified_ms) - trigger_ms) / 1000,
            "all_recalls": (max(notified_ms) - trigger_ms) / 1000,
        }
        self.dashboard.save(label + "-evidence.json", case)
        print(
            "Independent recall latency:", label, case["response_seconds"], flush=True
        )
        assert case["response_seconds"]["first_recall"] < 5, (
            "Real flood response exceeded the five-second acceptance target"
        )
        assert probe["status"] == "rejected", (
            "Platform did not reject the ordinary sender while muted"
        )
        return case

    async def content(self):
        """Exercise actual warning-count recalls followed by a real capped mute."""
        baseline = rows(
            RECEIVER,
            "moderation.sqlite3",
            "SELECT COUNT(*) n FROM progressive_warnings WHERE account=? AND group_id=? "
            "AND member=? AND status IN ('accepted','verified')",
            (BOT, GROUP, ORDINARY_ACCOUNT),
        )[0]["n"]
        assert baseline == 0, "Do not reset existing production warning counts"
        receiver = next(
            b for b in self.original_settings["bots"] if b["id"] == RECEIVER
        )
        self.semantic_original = copy.deepcopy(receiver["moderation"]["semantic"])
        semantic = {**self.semantic_original, "enabled": False}
        self.semantic_changed = True
        await self.dashboard.api(
            "POST",
            PREFIX + "/settings",
            {
                "bot_id": RECEIVER,
                "revision": receiver["revision"],
                "policy": {"semantic": semantic},
            },
        )
        self.result["content_route"] = "live_rule_fallback_not_ai_classification"
        mids = []
        for n in range(4):
            self.mute_possible = True
            receipt = await self.send(
                self.marker + "-" + str(n) + " \u7a33\u8d5a\u4e0d\u8d54",
                "content-" + str(n),
            )
            self.dashboard.save("content-send-" + str(n) + ".json", receipt)
            assert receipt["status"] == "accepted"
            mids.append(receipt["message"])
            result = None
            for _ in range(160):
                actions = rows(
                    RECEIVER,
                    "moderation.sqlite3",
                    "SELECT result,recall_status FROM progressive_actions WHERE account=? "
                    "AND group_id=? AND member=? AND message=?",
                    (BOT, GROUP, ORDINARY_ACCOUNT, receipt["message"]),
                )
                if actions:
                    result = json.loads(actions[0]["result"])
                    if result.get("status") in {"accepted", "verified"}:
                        break
                await asyncio.sleep(0.25)
            assert result and result["status"] in {"accepted", "verified"}
            assert result["recall_status"] in {"accepted", "verified"}
            if n < 3:
                assert (
                    result["action"] == "recall" and result["warning_number"] == n + 1
                )
            else:
                assert result["action"] == "mute" and result["minutes"] == 60
                probe = await self.send(
                    self.marker + "-content-muted-probe", "content-muted-probe"
                )
                assert probe["status"] == "rejected" and probe["code"] == 802
                self.result["content_muted_probe"] = probe
            self.result["cases"].append(
                {
                    "name": "content-" + str(n),
                    "message": receipt["message"],
                    "result": result,
                }
            )
            self.dashboard.save("result.json", self.result)
            print("PASS ordinary content rule:", n + 1, result, flush=True)
        await self.unmute("content")
        for _ in range(20):
            notices = {
                e["recall"]["message"]: e["recall"]
                for e in self.events
                if e.get("recall", {}).get("message") in mids
            }
            if set(notices) == set(mids):
                break
            await asyncio.sleep(0.25)
        self.result["content_exact_recall_notifications"] = notices
        assert set(notices) == set(mids)

    async def run(self, case="flood"):
        """Remove only the test chat-admin role, perform both bursts, then restore."""
        from astrbot.core.platform.sources.wangshangliao.nim import NimClient

        await self.ledger.open()
        self.nim = NimClient(self.sender.http)
        try:
            await self.nim.connect(self.deployment, self.peer, self.nim_token)
            self.tasks = [
                asyncio.create_task(self.witness()),
                asyncio.create_task(self.heartbeat()),
            ]
            path = "/api/v1/config-profiles/" + PROFILE
            current = (await self.dashboard.api("GET", path))["config"]
            assert current == self.original_profile, "Concurrent profile change"
            proposal = copy.deepcopy(current)
            proposal["admins_id"] = [
                value
                for value in current.get("admins_id", [])
                if str(value) != ORDINARY_ACCOUNT
            ]
            if proposal != current:
                self.profile_changed = True
                await self.dashboard.api("PUT", path, proposal)
                actual = (await self.dashboard.api("GET", path))["config"]
                assert actual == proposal
            if case == "content":
                await self.content()
            else:
                await self.burst("repeated", 5, True)
                await asyncio.sleep(32)
                await self.burst("rate", 8, False)
            self.result["completed"] = True
        finally:
            cleanup_errors = []
            if self.mute_possible:
                try:
                    await self.unmute("finally")
                except Exception as exc:
                    cleanup_errors.append("unmute:" + type(exc).__name__)
            if self.profile_changed:
                try:
                    path = "/api/v1/config-profiles/" + PROFILE
                    current = (await self.dashboard.api("GET", path))["config"]
                    original = self.original_profile["admins_id"]
                    # Restore this identity only; preserve any unrelated concurrent edits.
                    admins = [
                        v
                        for v in current.get("admins_id", [])
                        if str(v) != ORDINARY_ACCOUNT
                    ]
                    for index, value in enumerate(original):
                        if str(value) == ORDINARY_ACCOUNT:
                            admins.insert(min(index, len(admins)), value)
                    current["admins_id"] = admins
                    await self.dashboard.api("PUT", path, current)
                    final = (await self.dashboard.api("GET", path))["config"]
                    self.result["cleanup"]["profile_exact"] = (
                        final == self.original_profile
                    )
                except Exception as exc:
                    cleanup_errors.append("profile:" + type(exc).__name__)
            if self.semantic_changed:
                try:
                    settings = await self.dashboard.api("GET", PREFIX + "/settings")
                    receiver = next(b for b in settings["bots"] if b["id"] == RECEIVER)
                    semantic = copy.deepcopy(receiver["moderation"]["semantic"])
                    semantic["enabled"] = self.semantic_original["enabled"]
                    await self.dashboard.api(
                        "POST",
                        PREFIX + "/settings",
                        {
                            "bot_id": RECEIVER,
                            "revision": receiver["revision"],
                            "policy": {"semantic": semantic},
                        },
                    )
                except Exception as exc:
                    cleanup_errors.append("semantic:" + type(exc).__name__)
            if self.original_invite_rules:
                try:
                    from astrbot.core.platform.sources.wangshangliao.storage import (
                        instance_dir,
                    )

                    profile = (
                        await self.dashboard.api(
                            "GET", "/api/v1/config-profiles/" + PROFILE
                        )
                    )["config"]
                    assert ORDINARY_ACCOUNT in {str(v) for v in profile["admins_id"]}
                    before = self.original_invite_rules[0]
                    path = instance_dir(RECEIVER) / "activities.sqlite3"
                    with sqlite3.connect(path) as db:
                        db.execute(
                            "UPDATE invite_rules SET tracking_enabled=?,error=? "
                            "WHERE account=? AND group_id=? AND owner=? AND activation=? "
                            "AND rate=? AND enabled=? AND tracking_enabled=0",
                            (
                                before["tracking_enabled"],
                                before["error"],
                                BOT,
                                GROUP,
                                ORDINARY_ACCOUNT,
                                before["activation"],
                                before["rate"],
                                before["enabled"],
                            ),
                        )
                    after = rows(
                        RECEIVER,
                        "activities.sqlite3",
                        "SELECT * FROM invite_rules WHERE account=? AND group_id=? AND owner=?",
                        (BOT, GROUP, ORDINARY_ACCOUNT),
                    )[0]
                    assert all(
                        after[field] == before[field]
                        for field in (
                            "enabled",
                            "tracking_enabled",
                            "rate",
                            "owner",
                            "session",
                            "activation",
                            "tracking_started_at",
                        )
                    )
                    self.result["cleanup"]["invitation_policy_restored"] = True
                except Exception as exc:
                    cleanup_errors.append("invitation:" + type(exc).__name__)
            for task in self.tasks:
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.nim:
                await self.nim.close()
            await self.ledger.close()
            final = await self.dashboard.api("GET", PREFIX + "/settings")
            self.result["cleanup"]["runtime_snapshot_exact"] = (
                final == self.original_settings
            )
            self.result["cleanup"]["policy_exact"] = {
                bot["id"] for bot in final["bots"]
            } == {bot["id"] for bot in self.original_settings["bots"]} and all(
                {
                    key: bot.get(key)
                    for key in (
                        "id",
                        "account_id",
                        "enabled_groups",
                        "moderation",
                        "reply_private",
                        "reply_groups",
                        "proactive_send",
                        "ai_routes",
                        "developer_test",
                    )
                }
                == {
                    key: original.get(key)
                    for key in (
                        "id",
                        "account_id",
                        "enabled_groups",
                        "moderation",
                        "reply_private",
                        "reply_groups",
                        "proactive_send",
                        "ai_routes",
                        "developer_test",
                    )
                }
                for bot in final["bots"]
                for original in self.original_settings["bots"]
                if bot["id"] == original["id"]
            )
            self.result["cleanup"]["pid_unchanged"] = (
                self.result["pid"]
                == (
                    await asyncio.to_thread(
                        subprocess.check_output,
                        [
                            "systemctl",
                            "show",
                            "astrbot.service",
                            "-p",
                            "MainPID",
                            "--value",
                        ],
                        text=True,
                    )
                ).strip()
            )
            self.result["cleanup"]["errors"] = cleanup_errors
            self.dashboard.save("result.json", self.result)
            self.dashboard.save("sender-notifications.json", self.events)
            print("RESTORATION:", self.result["cleanup"], self.output, flush=True)
            assert not cleanup_errors, "Live test cleanup needs operator attention"
            assert not self.profile_changed or self.result["cleanup"].get(
                "profile_exact"
            ), "The temporary profile role was not restored exactly"
            assert self.result["cleanup"]["policy_exact"], (
                "Business policy changed during acceptance"
            )


async def main():
    """Run a read-only preflight unless live side effects are explicitly selected."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--case", choices=("flood", "content"), default="flood")
    args = parser.parse_args()
    config = json.loads(Path("data/cmd_config.json").read_text(encoding="utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 900},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    output = Path("data/temp/flood-live-" + str(int(time.time())))
    output.mkdir(parents=True, mode=0o700)
    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:6185",
            headers={"Authorization": "Bearer " + token},
            timeout=60,
            trust_env=False,
        ) as client,
        aiohttp.ClientSession() as http,
    ):
        test = LiveFlood(client, http, output)
        await test.preflight()
        if args.confirm_live:
            await test.run(args.case)
        else:
            test.dashboard.save("preflight.json", test.result)
            print(
                "Read-only preflight; --confirm-live sends real messages and causes real mutes"
            )


if __name__ == "__main__":
    asyncio.run(main())
