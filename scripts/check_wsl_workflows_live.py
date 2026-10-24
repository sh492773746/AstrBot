"""Opt-in ordinary-account workflow probes with scoped grants and restoration."""

import argparse
import asyncio
import copy
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.moderation import execute
from astrbot.core.platform.sources.wangshangliao.nim import NimClient
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from scripts.check_wsl_authorization_live import (
    BOT,
    GROUP,
    PROFILE,
    RECEIVER,
    SENDER,
    USER,
    rows,
)
from scripts.check_wsl_flood_live import ORDINARY_ACCOUNT, LiveFlood
from scripts.check_wsl_migration_dialogue import PREFIX


class Workflows:
    """Own one bounded production test without resetting durable business history."""

    def __init__(self, probe):
        self.probe = probe
        self.dashboard = probe.dashboard
        self.original = {}
        self.proposed = {}
        self.activity_before = None
        self.activity_proposed = None
        self.lottery = None
        self.group_mute_possible = False
        self.schedule_changed = False
        self.role_changed = False
        self.revoked_admins = None
        self.transport_paused = False
        self.transport_before = None
        self.result = {"cases": [], "cleanup": {}}

    async def prepare(self, case):
        """Grant only required scopes using the normal versioned settings API.

        Args:
            case: Explicitly selected live workflow.
        """
        profile = self.probe.original_profile
        if USER not in [str(v) for v in profile.get("admins_id", [])]:
            self.role_changed = True
            await self.dashboard.role(True)
        assert not rows(
            RECEIVER,
            "schedules.sqlite3",
            "SELECT 1 FROM schedules WHERE account=? AND group_id=? AND status!='deleted'",
            (BOT, GROUP),
        ), "An existing schedule must not be interrupted"
        for instance in (RECEIVER, SENDER):
            settings = await self.dashboard.api("GET", PREFIX + "/settings")
            current = next(b for b in settings["bots"] if b["id"] == instance)
            original = {"proactive_send": copy.deepcopy(current["proactive_send"])}
            proposed = copy.deepcopy(original)
            proposed["proactive_send"]["enabled"] = True
            proposed["proactive_send"]["targets"] = list(
                dict.fromkeys(
                    proposed["proactive_send"]["targets"]
                    + ([GROUP] if instance == RECEIVER else [self.dashboard.private])
                )
            )
            if instance == RECEIVER and case in {
                "group",
                "schedule",
                "schedule_revocation",
                "schedule_disconnect",
            }:
                original["group_id"] = GROUP
                original["group"] = {
                    "permissions": copy.deepcopy(
                        current["moderation"]["permissions"].get(GROUP, [])
                    )
                }
                proposed["group_id"] = GROUP
                proposed["group"] = {
                    "permissions": list(
                        dict.fromkeys(
                            original["group"]["permissions"]
                            + ["mute_all", "unmute_all"]
                        )
                    )
                }
            self.original[instance] = original
            self.proposed[instance] = proposed
            await self.dashboard.api(
                "POST",
                PREFIX + "/settings",
                {"bot_id": instance, "revision": current["revision"], **proposed},
            )
        await self.dashboard.new_window()
        self.dashboard.phase = "ordinary_workflow"
        directory = await self.dashboard.send_case("private", "群列表", "大海兼职群")
        number = re.search(r"(?m)^(\d+)\.\s+大海兼职群\s*$", directory)
        assert number, "Group snapshot does not identify the authorized group"
        await self.dashboard.send_case("private", "选择群 " + number[1], "大海兼职群")
        self.dashboard.save("temporary-grants.json", self.proposed)

    async def probe_send(self, label, *, muted=False):
        """Check real ordinary send enforcement and clean up accepted probe text.

        Args:
            label: Unique local probe suffix.
            muted: Require native rejection while the group is muted.

        Returns:
            Exact transport receipt and optional self-recall acceptance.
        """
        receipt = await self.probe.send(self.probe.marker + "-" + label, label)
        self.dashboard.save(label + "-receipt.json", receipt)
        if muted:
            assert receipt["status"] == "rejected" and receipt["code"] in {802, 812}
        else:
            assert receipt["status"] == "accepted"
            code, _ = await self.probe.nim.request(
                7,
                13,
                wire.properties(
                    [
                        (0, receipt["time"].encode()),
                        (1, b"8"),
                        (2, self.probe.team.encode()),
                        (3, self.probe.peer.encode()),
                        (10, receipt["client"].encode()),
                        (11, receipt["message"].encode()),
                        (16, self.probe.peer.encode()),
                    ]
                ),
            )
            receipt["self_recall_code"] = code
            assert code == 200
        return receipt

    async def group(self):
        """Verify group-wide muting with an actual ordinary sender."""
        latest = rows(
            RECEIVER,
            "moderation.sqlite3",
            "SELECT result FROM operations WHERE json_extract(result,'$.group')=? "
            "AND json_extract(result,'$.action') IN ('mute_all','unmute_all') "
            "ORDER BY rowid DESC LIMIT 1",
            (int(GROUP),),
        )
        assert latest and json.loads(latest[0]["result"])["action"] == "unmute_all"
        result = {"case": "group_mute"}
        result["baseline_send"] = await self.probe_send("group-before")
        started = time.monotonic()
        self.group_mute_possible = True
        await self.dashboard.send_case("private", "全员禁言", "服务端已接受")
        result["muted_send"] = await self.probe_send("group-muted", muted=True)
        await self.dashboard.send_case("private", "解除全员禁言", "服务端已接受")
        self.group_mute_possible = False
        result["toggle_seconds"] = round(time.monotonic() - started, 3)
        result["restored_send"] = await self.probe_send("group-restored")
        self.result["cases"].append(result)
        print(
            "PASS real group mute: rejected",
            result["muted_send"]["code"],
            "baseline and restored sends accepted",
            flush=True,
        )

    async def activity(self):
        """Enroll an ordinary account once, reject duplication and draw one winner."""
        assert not rows(
            RECEIVER,
            "activities.sqlite3",
            "SELECT 1 FROM lotteries WHERE account=? AND group_id=? "
            "AND status NOT IN ('finished','cancelled')",
            (BOT, GROUP),
        ), "Do not interfere with an unresolved activity"
        path = PREFIX + "/activities?bot_id=" + RECEIVER + "&group_id=" + GROUP
        self.activity_before = await self.dashboard.api("GET", path)
        proposed = copy.deepcopy(self.activity_before["lottery"])
        proposed.update(
            prize="普通账号验收-" + self.probe.marker[-10:] + "（无实物）",
            winners=1,
            duration_minutes=1,
            capacity=2,
            invite_gate=0,
            contact="测试结束无需领取",
        )
        self.activity_proposed = proposed
        await self.dashboard.api(
            "POST",
            PREFIX + "/activities",
            {
                "bot_id": RECEIVER,
                "group_id": GROUP,
                "revision": self.activity_before["revision"],
                "lottery": proposed,
                "invite_rate": self.activity_before["invitation"]["rate"],
            },
        )
        await self.dashboard.send_case("private", "开启抽奖", "已开启")
        candidates = rows(
            RECEIVER,
            "activities.sqlite3",
            "SELECT * FROM lotteries WHERE account=? AND group_id=? AND prize=?",
            (BOT, GROUP, proposed["prize"]),
        )
        assert len(candidates) == 1 and candidates[0]["status"] == "open"
        self.lottery = candidates[0]["id"]
        result = {"case": "ordinary_lottery", "lottery": self.lottery}
        for number in range(2):
            receipt = await self.probe.send(
                "参加抽奖", "enrollment-" + str(number), enrollment=True
            )
            assert receipt["status"] == "accepted"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                inbound = rows(
                    RECEIVER,
                    "messages.sqlite3",
                    "SELECT state FROM inbox WHERE account=? AND message=?",
                    (BOT, receipt["message"]),
                )
                if inbound and inbound[0]["state"] == "processed":
                    break
                await asyncio.sleep(0.1)
            assert inbound and inbound[0]["state"] == "processed"
            entries = rows(
                RECEIVER,
                "activities.sqlite3",
                "SELECT * FROM lottery_entries WHERE lottery=?",
                (self.lottery,),
            )
            assert len(entries) == 1 and entries[0]["member"] == ORDINARY_ACCOUNT
            result["enrollment_" + str(number)] = receipt
        deadline = candidates[0]["ends"] + 20
        while time.time() < deadline:
            lottery = rows(
                RECEIVER,
                "activities.sqlite3",
                "SELECT * FROM lotteries WHERE id=?",
                (self.lottery,),
            )[0]
            if lottery["status"] == "finished":
                break
            await asyncio.sleep(0.2)
        assert lottery["status"] == "finished" and lottery["total"] == 1
        winners = json.loads(lottery["results"])
        assert len(winners) == 1
        assert winners[0]["uid"] == ORDINARY_ACCOUNT
        assert winners[0]["peer"] == self.probe.peer
        result["lottery_result"] = lottery
        notices = rows(
            RECEIVER,
            "messages.sqlite3",
            "SELECT key,state,server_id FROM outbox WHERE key LIKE ?",
            ("lottery-result/" + BOT + "/" + self.lottery + "%",),
        )
        assert notices and all(n["state"] == "accepted" for n in notices)
        for notice in notices:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                received = rows(
                    SENDER,
                    "messages.sqlite3",
                    "SELECT payload FROM inbox WHERE account=? AND message=?",
                    (USER, notice["server_id"]),
                )
                if received:
                    break
                await asyncio.sleep(0.2)
            assert received, "Independent receiving account did not receive the draw"
            payload = json.loads(received[0]["payload"])
            assert winners[0]["name"] in payload.get("text", "")
            assert payload.get("mentions") == [self.probe.peer]
        result["received_notices"] = notices
        self.result["cases"].append(result)
        print(
            "PASS ordinary enrollment, duplicate protection and real automatic draw",
            flush=True,
        )

    async def schedule(self):
        """Verify both future boundaries while keeping group mute intervals short."""
        for stage in ("mute_all", "unmute_all"):
            current = datetime.now(ZoneInfo("Asia/Shanghai"))
            boundary = (current + timedelta(minutes=2)).replace(second=0, microsecond=0)
            later = boundary + timedelta(minutes=2)
            start, end = (boundary, later) if stage == "mute_all" else (later, boundary)
            preview = await self.dashboard.send_case(
                "private",
                f"定时禁言 {start:%H:%M} {end:%H:%M}",
                "确认定时",
            )
            token = re.search(r"确认定时\s+([a-f0-9]{24})", preview)
            assert token
            self.schedule_changed = True
            await self.dashboard.send_case(
                "private", "确认定时 " + token[1], "定时规则已启用"
            )
            rule = rows(
                RECEIVER,
                "schedules.sqlite3",
                "SELECT * FROM schedules WHERE account=? AND group_id=?",
                (BOT, GROUP),
            )[0]
            assert rule["status"] == "active"
            assert rule["next_action"] == stage
            assert rule["next_at"] == boundary.timestamp()
            if stage == "unmute_all":
                await asyncio.sleep(max(0, boundary.timestamp() - time.time() - 8))

                # A manual command would pause the schedule; use a separately
                # authorized native action solely to establish the test baseline.
                async def authorized():
                    settings = await self.dashboard.api("GET", PREFIX + "/settings")
                    config = next(b for b in settings["bots"] if b["id"] == RECEIVER)
                    assert str(config["account_id"]) == BOT
                    assert config["state"] == "online" and config["business_active"]
                    profile = (
                        await self.dashboard.api(
                            "GET", "/api/v1/config-profiles/" + PROFILE
                        )
                    )["config"]
                    assert USER in [str(v) for v in profile.get("admins_id", [])]
                    authorize_action(config, GROUP, "mute_all")

                await authorized()
                self.group_mute_possible = True
                receipt = await execute(
                    self.probe.admin,
                    RECEIVER,
                    BOT,
                    "workflow-baseline/" + self.probe.marker[-10:],
                    "mute_all",
                    int(GROUP),
                    authorize=authorized,
                )
                assert receipt["status"] in {"accepted", "verified"}
                await self.probe_send("schedule-baseline-muted", muted=True)
            else:
                self.group_mute_possible = True
            deadline = boundary.timestamp() + 15
            executions = []
            while time.time() < deadline:
                executions = rows(
                    RECEIVER,
                    "schedules.sqlite3",
                    "SELECT * FROM executions WHERE account=? AND group_id=? "
                    "AND version=? AND planned=?",
                    (BOT, GROUP, rule["version"], boundary.timestamp()),
                )
                if executions and executions[0]["status"] in {"accepted", "verified"}:
                    break
                await asyncio.sleep(0.15)
            assert executions and executions[0]["status"] in {"accepted", "verified"}
            result = {
                "case": "future_boundary",
                "action": stage,
                "execution": executions[0],
            }
            if stage == "mute_all":
                result["send"] = await self.probe_send("scheduled-muted", muted=True)
                await self.dashboard.send_case(
                    "private", "解除全员禁言", "服务端已接受"
                )
                self.group_mute_possible = False
                result["restored_send"] = await self.probe_send("schedule-recovered")
            else:
                self.group_mute_possible = False
                result["send"] = await self.probe_send("scheduled-restored")
            await self.dashboard.send_case("private", "删除定时", "已停止")
            self.schedule_changed = False
            self.result["cases"].append(result)
            print("PASS real future schedule boundary:", stage, flush=True)

    async def schedule_revocation(self):
        """Verify real creator revocation at a future boundary without muting anyone."""
        baseline = await self.probe_send("revocation-before")
        current = datetime.now(ZoneInfo("Asia/Shanghai"))
        boundary = (current + timedelta(minutes=2)).replace(second=0, microsecond=0)
        following = boundary + timedelta(minutes=2)
        preview = await self.dashboard.send_case(
            "private",
            f"定时禁言 {boundary:%H:%M} {following:%H:%M}",
            "确认定时",
        )
        token = re.search(r"确认定时\s+([a-f0-9]{24})", preview)
        assert token
        self.schedule_changed = True
        self.group_mute_possible = True
        await self.dashboard.send_case(
            "private", "确认定时 " + token[1], "定时规则已启用"
        )
        rule = rows(
            RECEIVER,
            "schedules.sqlite3",
            "SELECT * FROM schedules WHERE account=? AND group_id=?",
            (BOT, GROUP),
        )[0]
        assert (
            rule["status"] == "active"
            and rule["owner"] == USER
            and rule["next_at"] == boundary.timestamp()
            and rule["next_action"] == "mute_all"
        )
        path = "/api/v1/config-profiles/" + PROFILE
        profile = (await self.dashboard.api("GET", path))["config"]
        assert USER in [str(uid) for uid in profile["admins_id"]]
        assert time.time() < boundary.timestamp() - 15
        self.revoked_admins = list(profile["admins_id"])
        profile["admins_id"] = [uid for uid in profile["admins_id"] if str(uid) != USER]
        await self.dashboard.api("PUT", path, profile)
        observed = (await self.dashboard.api("GET", path))["config"]
        assert observed == profile
        print("Creator revoked before future boundary; no mute requested", flush=True)
        paused = None
        deadline = boundary.timestamp() + 15
        while time.time() < deadline:
            paused = rows(
                RECEIVER,
                "schedules.sqlite3",
                "SELECT * FROM schedules WHERE account=? AND group_id=?",
                (BOT, GROUP),
            )[0]
            if paused["status"] == "paused":
                break
            await asyncio.sleep(0.2)
        assert time.time() >= boundary.timestamp()
        assert paused["status"] == "paused"
        assert paused["error"] == "schedule_owner_revoked"
        executions = rows(
            RECEIVER,
            "schedules.sqlite3",
            "SELECT * FROM executions WHERE account=? AND group_id=? "
            "AND version=? AND planned=?",
            (BOT, GROUP, rule["version"], boundary.timestamp()),
        )
        assert not executions, "Revoked creator must not claim a native action"
        after = await self.probe_send("revocation-after")
        self.group_mute_possible = False
        result = {
            "case": "future_boundary_creator_revoked",
            "planned": boundary.timestamp(),
            "schedule": paused,
            "executions": 0,
            "baseline_send": baseline,
            "after_boundary_send": after,
        }
        self.result["cases"].append(result)
        print(
            "PASS live revoked schedule: paused, zero native executions, ordinary send200",
            flush=True,
        )

    async def schedule_disconnect(self):
        """Cross a real deadline with only this transport stopped, then recover."""
        assert not rows(
            RECEIVER,
            "schedules.sqlite3",
            "SELECT 1 FROM schedules WHERE account=? AND group_id<>? AND status='active'",
            (BOT, GROUP),
        ), "Other active schedules must not be interrupted"
        baseline = await self.probe_send("disconnect-before")
        current = datetime.now(ZoneInfo("Asia/Shanghai"))
        boundary = (current + timedelta(minutes=2)).replace(second=0, microsecond=0)
        following = boundary + timedelta(minutes=2)
        preview = await self.dashboard.send_case(
            "private",
            f"定时禁言 {boundary:%H:%M} {following:%H:%M}",
            "确认定时",
        )
        token = re.search(r"确认定时\s+([a-f0-9]{24})", preview)
        assert token
        self.schedule_changed = True
        self.group_mute_possible = True
        await self.dashboard.send_case(
            "private", "确认定时 " + token[1], "定时规则已启用"
        )
        rule = rows(
            RECEIVER,
            "schedules.sqlite3",
            "SELECT * FROM schedules WHERE account=? AND group_id=?",
            (BOT, GROUP),
        )[0]
        assert (
            rule["status"] == "active"
            and rule["owner"] == USER
            and rule["next_at"] == boundary.timestamp()
            and rule["next_action"] == "mute_all"
        )
        before = await self.dashboard.api("GET", "/api/v1/bots/stats")
        settings = json.loads(
            Path("data/cmd_config.json").read_text(encoding="utf-8-sig")
        )
        self.transport_before = next(
            copy.deepcopy(bot) for bot in settings["platform"] if bot["id"] == RECEIVER
        )
        assert (
            self.transport_before["enable"]
            and self.transport_before["account_id"] == BOT
        )
        assert time.time() < boundary.timestamp() - 15
        self.transport_paused = True
        await self.dashboard.api(
            "PATCH", "/api/v1/bots/" + RECEIVER + "/enabled", {"enabled": False}
        )
        offline = await self.dashboard.api("GET", "/api/v1/bots/stats")
        state = next(bot for bot in offline["platforms"] if bot["id"] == RECEIVER)
        assert state["connection_state"] == "stopped"
        print(
            "Only the test receiver transport stopped across its future deadline",
            flush=True,
        )
        await asyncio.sleep(max(0, boundary.timestamp() + 1 - time.time()))
        ordinary = await self.probe_send("disconnect-boundary")
        executions = rows(
            RECEIVER,
            "schedules.sqlite3",
            "SELECT * FROM executions WHERE account=? AND group_id=? "
            "AND version=? AND planned=?",
            (BOT, GROUP, rule["version"], boundary.timestamp()),
        )
        assert not executions, "Offline transport must not execute the due action"
        settings = json.loads(
            Path("data/cmd_config.json").read_text(encoding="utf-8-sig")
        )
        current = next(bot for bot in settings["platform"] if bot["id"] == RECEIVER)
        assert current == {**self.transport_before, "enable": False}, (
            "Concurrent transport edit; do not overwrite"
        )
        await self.dashboard.api(
            "PATCH", "/api/v1/bots/" + RECEIVER + "/enabled", {"enabled": True}
        )
        await self.dashboard.online()
        self.transport_paused = False
        deadline = time.time() + 15
        paused = None
        while time.time() < deadline:
            paused = rows(
                RECEIVER,
                "schedules.sqlite3",
                "SELECT * FROM schedules WHERE account=? AND group_id=?",
                (BOT, GROUP),
            )[0]
            if paused["status"] == "paused":
                break
            await asyncio.sleep(0.2)
        assert paused["status"] == "paused" and paused["error"] == "restart_review"
        assert not rows(
            RECEIVER,
            "schedules.sqlite3",
            "SELECT 1 FROM executions WHERE account=? AND group_id=? "
            "AND version=? AND planned=?",
            (BOT, GROUP, rule["version"], boundary.timestamp()),
        ), "Reconnection must not replay the missed action"
        after = await self.probe_send("disconnect-restored")
        self.group_mute_possible = False
        final = await self.dashboard.api("GET", "/api/v1/bots/stats")
        for original in before["platforms"]:
            if original["id"] == RECEIVER:
                continue
            other = next(
                bot for bot in final["platforms"] if bot["id"] == original["id"]
            )
            assert all(
                other.get(key) == original.get(key)
                for key in ("type", "status", "started_at", "last_connected_at")
            ), "Another platform's connection changed during the scoped test"
        await self.dashboard.new_window()
        self.result["cases"].append(
            {
                "case": "future_boundary_transport_stopped_and_recovered",
                "planned": boundary.timestamp(),
                "schedule": paused,
                "executions": 0,
                "baseline_send": baseline,
                "offline_boundary_send": ordinary,
                "recovered_send": after,
                "other_platform_connections_unchanged": True,
                "scope": "controlled_instance_stop_not_unplanned_network_failure",
            }
        )
        print(
            "PASS live transport recovery: paused, no catch-up, ordinary send200",
            flush=True,
        )

    async def cleanup(self):
        """Restore only fields this run owns; retain action and activity evidence."""
        errors = []
        if self.transport_paused:
            try:
                settings = json.loads(
                    Path("data/cmd_config.json").read_text(encoding="utf-8-sig")
                )
                current = next(
                    bot for bot in settings["platform"] if bot["id"] == RECEIVER
                )
                assert current in (
                    self.transport_before,
                    {**self.transport_before, "enable": False},
                ), "Concurrent transport edit; do not overwrite"
                if not current["enable"]:
                    await self.dashboard.api(
                        "PATCH",
                        "/api/v1/bots/" + RECEIVER + "/enabled",
                        {"enabled": True},
                    )
                await self.dashboard.online()
                self.transport_paused = False
                await self.dashboard.new_window()
            except Exception as exc:
                errors.append("transport: " + type(exc).__name__ + ": " + str(exc))
        if self.revoked_admins is not None:
            try:
                path = "/api/v1/config-profiles/" + PROFILE
                current = (await self.dashboard.api("GET", path))["config"]
                expected = [uid for uid in self.revoked_admins if str(uid) != USER]
                assert current["admins_id"] in (expected, self.revoked_admins), (
                    "Concurrent administrator edit; do not overwrite"
                )
                current["admins_id"] = self.revoked_admins
                await self.dashboard.api("PUT", path, current)
                self.revoked_admins = None
            except Exception as exc:
                errors.append("revocation: " + str(exc))
        if self.schedule_changed:
            try:
                if self.transport_before is not None:
                    directory = await self.dashboard.send_case(
                        "private", "群列表", "大海兼职群"
                    )
                    number = re.search(r"(?m)^(\d+)\.\s+大海兼职群\s*$", directory)
                    assert number, "Recovered group snapshot is incomplete"
                    await self.dashboard.send_case(
                        "private", "选择群 " + number[1], "大海兼职群"
                    )
                await self.dashboard.send_case("private", "删除定时", "已停止")
                restored = rows(
                    RECEIVER,
                    "schedules.sqlite3",
                    "SELECT status FROM schedules WHERE account=? AND group_id=?",
                    (BOT, GROUP),
                )
                assert len(restored) == 1 and restored[0]["status"] == "deleted", (
                    "Test schedule was not deleted"
                )
                self.schedule_changed = False
                self.result["cleanup"]["schedule_deleted"] = True
            except Exception as exc:
                errors.append("schedule: " + type(exc).__name__ + ": " + str(exc))
        if self.group_mute_possible:
            try:
                await self.dashboard.send_case(
                    "private", "解除全员禁言", "服务端已接受"
                )
                self.group_mute_possible = False
            except Exception as exc:
                errors.append("unmute: " + type(exc).__name__ + ": " + str(exc))
        try:
            if self.lottery:
                current = rows(
                    RECEIVER,
                    "activities.sqlite3",
                    "SELECT status FROM lotteries WHERE id=?",
                    (self.lottery,),
                )[0]
                if current["status"] not in {"finished", "cancelled"}:
                    await self.dashboard.send_case("private", "取消抽奖", "已取消")
            if self.activity_before:
                path = PREFIX + "/activities?bot_id=" + RECEIVER + "&group_id=" + GROUP
                current = await self.dashboard.api("GET", path)
                assert current["lottery"] in (
                    self.activity_proposed,
                    self.activity_before["lottery"],
                ), "Concurrent activity edit; do not overwrite"
                await self.dashboard.api(
                    "POST",
                    PREFIX + "/activities",
                    {
                        "bot_id": RECEIVER,
                        "group_id": GROUP,
                        "revision": current["revision"],
                        "lottery": self.activity_before["lottery"],
                        "invite_rate": self.activity_before["invitation"]["rate"],
                    },
                )
                restored = await self.dashboard.api("GET", path)
                assert restored["lottery"] == self.activity_before["lottery"]
                self.result["cleanup"]["activity_exact"] = True
        except Exception as exc:
            errors.append("workflow: " + type(exc).__name__ + ": " + str(exc))
        if self.dashboard.window:
            try:
                await self.dashboard.api(
                    "POST",
                    "/api/v1/bot-types/wangshangliao/registration",
                    {"action": "test_close", "instance_id": RECEIVER},
                )
            except Exception as exc:
                errors.append("window: " + str(exc))
        for instance, original in self.original.items():
            try:
                settings = await self.dashboard.api("GET", PREFIX + "/settings")
                current = next(b for b in settings["bots"] if b["id"] == instance)
                for field, value in self.proposed[instance].items():
                    if field == "group_id":
                        continue
                    observed = (
                        {
                            "permissions": current["moderation"]["permissions"].get(
                                GROUP, []
                            )
                        }
                        if field == "group"
                        else current[field]
                    )
                    assert observed in (value, original[field]), (
                        "Concurrent configuration edit; do not overwrite"
                    )
                await self.dashboard.api(
                    "POST",
                    PREFIX + "/settings",
                    {"bot_id": instance, "revision": current["revision"], **original},
                )
            except Exception as exc:
                errors.append(instance + ": " + str(exc))
        if self.role_changed:
            try:
                path = "/api/v1/config-profiles/" + PROFILE
                current = (await self.dashboard.api("GET", path))["config"]
                original_admins = self.probe.original_profile.get("admins_id", [])
                expected_admins = [v for v in original_admins if str(v) != USER] + [
                    USER
                ]
                assert current["admins_id"] == expected_admins, (
                    "Concurrent administrator edit; do not overwrite"
                )
                current["admins_id"] = original_admins
                await self.dashboard.api("PUT", path, current)
            except Exception as exc:
                errors.append("role: " + str(exc))
        final = await self.dashboard.api("GET", PREFIX + "/settings")
        self.result["cleanup"]["grants_exact"] = all(
            (
                {
                    "permissions": next(
                        b for b in final["bots"] if b["id"] == instance
                    )["moderation"]["permissions"].get(GROUP, [])
                }
                if key == "group"
                else next(b for b in final["bots"] if b["id"] == instance)[key]
            )
            == value
            for instance, original in self.original.items()
            for key, value in original.items()
            if key != "group_id"
        )
        profile = (
            await self.dashboard.api("GET", "/api/v1/config-profiles/" + PROFILE)
        )["config"]
        self.result["cleanup"]["profile_exact"] = profile == self.probe.original_profile
        self.result["cleanup"]["pid_unchanged"] = (
            self.probe.result["pid"]
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
        self.result["cleanup"]["errors"] = errors
        if self.transport_before is not None:
            saved = json.loads(
                Path("data/cmd_config.json").read_text(encoding="utf-8-sig")
            )
            self.result["cleanup"]["transport_exact"] = (
                next(
                    (bot for bot in saved["platform"] if bot["id"] == RECEIVER),
                    None,
                )
                == self.transport_before
            )
            if not self.result["cleanup"]["transport_exact"]:
                errors.append("Original transport configuration was not restored")
        self.dashboard.save("result.json", self.result)
        print("RESTORATION", self.result["cleanup"], self.probe.output, flush=True)
        assert not errors and all(
            self.result["cleanup"][key]
            for key in ("grants_exact", "profile_exact", "pid_unchanged")
        ), "Production restoration needs operator attention"


async def main():
    """Require explicit live confirmation before any state changes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument(
        "--case",
        choices=(
            "group",
            "activity",
            "schedule",
            "schedule_revocation",
            "schedule_disconnect",
        ),
        required=True,
    )
    args = parser.parse_args()
    config = json.loads(Path("data/cmd_config.json").read_text(encoding="utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 900},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    output = Path("data/temp/workflow-live-" + str(int(time.time())))
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
        probe = LiveFlood(client, http, output)
        await probe.preflight()
        if not args.confirm_live:
            print("Read-only preflight; no workflow changed", flush=True)
            await probe.ledger.close()
            return
        test = Workflows(probe)
        tasks = []
        try:
            await probe.ledger.open()
            probe.nim = NimClient(http)
            await probe.nim.connect(probe.deployment, probe.peer, probe.nim_token)
            tasks = [
                asyncio.create_task(probe.witness()),
                asyncio.create_task(probe.heartbeat()),
            ]
            await test.prepare(args.case)
            await getattr(test, args.case)()
        finally:
            try:
                await test.cleanup()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if probe.nim:
                    await probe.nim.close()
                await probe.ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
