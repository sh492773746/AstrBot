"""Opt-in fixed-group packet and unmute acceptance using existing saved sessions."""

import argparse
import asyncio
import base64
import copy
import io
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import httpx
import jwt
from PIL import Image, ImageDraw

from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.directory import member_directory
from astrbot.core.platform.sources.wangshangliao.storage import Vault, instance_dir
from scripts.check_wsl_pair_live import protected, read_rows, require
from scripts.wsl_test_lab import CONTROL, TestLab

ROOT = Path(__file__).resolve().parents[1]
GROUP, BOT, OWNER, MEMBER = "1459282", "43506527", "31846829", "23691273"
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation/"
REGISTRATION = "/api/v1/bot-types/wangshangliao/registration"
FIELDS = (
    "account",
    "group_id",
    "active",
    "paused",
    "version",
    "epoch",
    "managers",
    "rules",
    "native_role",
)


class Pilot:
    def __init__(self, client, output):
        self.client, self.output = client, output
        self.evidence = {
            "group": GROUP,
            "phases": [],
            "real_payments": False,
            "extra_logins": 0,
            "acceptance_level": "transport_only",
            "phone_verified": False,
        }
        self.original_tenant = None
        self.expected = None
        self.original_activity = None
        self.original_business = None
        self.window = False
        self.grants = {}
        self.own_lottery = None
        self.own_mute = False
        self.baseline = None

    def record(self, phase, **details):
        self.evidence["phases"].append({"phase": phase, **details})
        (self.output / "summary.json").write_text(
            json.dumps(self.evidence, ensure_ascii=False, indent=2)
        )
        print(phase, flush=True)

    async def api(self, method, path, body=None):
        response = await self.client.request(method, path, json=body)
        value = response.json()
        require(
            response.status_code == 200 and value.get("status") == "ok",
            f"API rejected {path}: {value.get('message', response.status_code)}",
        )
        return value.get("data")

    async def rpc(self, kind, **payload):
        return await self.api(
            "POST", PREFIX + "open-service", {"action": kind, "payload": payload}
        )

    async def state(self):
        return await self.rpc("status")

    async def tenant(self):
        return next(
            t
            for t in (await self.state())["groups"]
            if t["account"] == BOT and t["group_id"] == GROUP
        )

    async def guard(self):
        require(
            protected() == self.baseline,
            "Protected formal/financial/config state changed",
        )
        state = await self.state()
        frozen = [
            {k: t.get(k) for k in FIELDS}
            for t in state["groups"]
            if t["group_id"] != GROUP
        ]
        require(frozen == self.formal, "Non-test tenant changed; stop")
        current = await self.tenant()
        require(
            all(current.get(k) == self.expected.get(k) for k in FIELDS),
            "Concurrent test-tenant change",
        )
        return current

    async def rules(self, rules):
        current = await self.guard()
        if current["rules"] == rules:
            return
        payload = {
            k: current[k] for k in ("account", "group_id", "version", "managers")
        }
        payload.update(paused=bool(current["paused"]), rules=rules)
        preview = await self.rpc("tenant_preview", **payload)
        await self.rpc("tenant", **payload, confirmation=preview["confirmation"])
        self.expected = {
            **current,
            "rules": copy.deepcopy(rules),
            "version": current["version"] + 1,
        }
        await self.guard()

    async def grant(self, instance):
        current = next(
            b
            for b in (await self.api("GET", PREFIX + "settings"))["bots"]
            if b["id"] == instance
        )
        original = current["proactive_send"]
        proposed = {
            **original,
            "enabled": True,
            "targets": list(dict.fromkeys([*original["targets"], GROUP])),
        }
        self.grants[instance] = (copy.deepcopy(original), proposed)
        if original != proposed:
            await self.api(
                "POST",
                PREFIX + "settings",
                {
                    "bot_id": instance,
                    "revision": current["revision"],
                    "proactive_send": proposed,
                },
            )

    async def open_window(self, scope, marker="", budget=10):
        if self.window:
            await self.api(
                "POST",
                REGISTRATION,
                {"action": "test_close", "instance_id": self.instances[BOT]},
            )
            self.window = False
        await self.guard()
        await self.api(
            "POST",
            REGISTRATION,
            {
                "action": "test_open",
                "instance_id": self.instances[BOT],
                "sender_instance": self.instances[MEMBER],
                "groups": [GROUP],
                "scopes": [scope],
                "keywords": [marker] if marker else [],
                "seconds": 300,
                "budget": budget,
            },
        )
        self.window = True

    async def probe(self, text):
        await self.guard()
        receipt = await self.rpc(
            "test_probe",
            operation_id="WSL_TEST_ACTIVITY_" + uuid.uuid4().hex,
            text=text,
        )
        require(receipt["status"] == "accepted", "Native probe uncertain; never replay")
        return receipt["message_id"]

    async def activity(self):
        return await self.api(
            "GET", PREFIX + f"activities?bot_id={self.instances[BOT]}&group_id={GROUP}"
        )

    async def save_activity(self, **values):
        await self.guard()
        row = await self.activity()
        return await self.api(
            "POST",
            PREFIX + "activities",
            {
                "bot_id": self.instances[BOT],
                "group_id": GROUP,
                "revision": row["revision"],
                **values,
            },
        )

    async def business(self, operation, **values):
        current = await self.guard()
        return await self.rpc(
            "business_rules",
            account=BOT,
            group_id=GROUP,
            tenant_version=current["version"],
            operation=operation,
            **values,
        )

    async def save_business(self, config):
        from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import (
            RuleConfiguration,
        )

        config = RuleConfiguration.model_validate(config).model_dump()
        row = await self.business("read")
        preview = await self.business("preview", version=row["version"], config=config)
        await self.business("commit", token=preview["token"])
        require(
            (await self.business("read"))["config"] == config,
            "Business rules save not verified",
        )

    async def packets(self):
        original = self.original_activity
        await self.save_activity(invite_rate="0.07")
        for mode in ("ordinary", "lucky"):
            operation = "WSL_TEST_LOTTERY_" + uuid.uuid4().hex
            lottery = {
                "prize": operation,
                "winners": 2,
                "duration_minutes": 3,
                "capacity": 2,
                "invite_gate": 0,
                "contact": "Controlled test; no payment",
                "total_amount": "0.03",
                "packet_mode": mode,
                "by_invites": False,
            }
            saved = await self.save_activity(lottery=lottery)
            require(
                saved["lottery"] == lottery and saved["invite_rate"] == "0.07",
                "Split save failed",
            )
            await self.open_window("group_commands")
            start = await self.rpc(
                "test_lottery", operation_id=operation, action="start"
            )
            require(start["status"] == "accepted", "Start uncertain; never replay")
            self.own_lottery = operation
            mid = await self.probe("参与玩家福利抽奖")
            path = instance_dir(self.instances[BOT]) / "activities.sqlite3"
            entries = []
            for _ in range(80):
                entries = read_rows(
                    path,
                    "SELECT member FROM lottery_entries WHERE lottery=?",
                    (start["fixture_id"],),
                )
                if entries:
                    break
                await asyncio.sleep(0.5)
            require(entries == [{"member": MEMBER}], "Native enrollment not verified")
            self.own_lottery = None
            draw = await self.rpc("test_lottery", operation_id=operation, action="draw")
            require(draw["status"] == "accepted", "Draw uncertain; never replay")
            result = read_rows(
                path,
                "SELECT status,results FROM lotteries WHERE id=?",
                (start["fixture_id"],),
            )[0]
            winners = json.loads(result["results"])
            require(
                result["status"] == "finished"
                and len(winners) == 1
                and winners[0]["uid"] == MEMBER
                and winners[0]["amount"] == 3,
                "Packet result incorrect",
            )
            self.record(
                "packet_" + mode,
                native_entry=mid,
                lottery=start["fixture_id"],
                amount_cents=3,
                actual_entrants=1,
                configured_capacity=2,
                start_receipt=start["status"],
                draw_receipt=draw["status"],
            )
        await self.save_activity(lottery=original["lottery"])
        await self.save_activity(invite_rate=original["invite_rate"])
        require(
            (await self.activity())["lottery"] == original["lottery"],
            "Lottery settings not restored",
        )

    async def scheduled_ad(self):
        self.original_business = (await self.business("read"))["config"]
        require(
            not self.original_business["keywords"]
            and not self.original_business["ads"],
            "Existing business rules; do not replace",
        )
        marker = "WSL_TEST_ACTIVITY_" + uuid.uuid4().hex
        images = []
        for index, color in enumerate(("#2d8668", "#bf445e", "#5978b2"), 1):
            bitmap = Image.new("RGB", (640, 360), color)
            ImageDraw.Draw(bitmap).text(
                (32, 32),
                f"CONTROLLED TEST IMAGE {index}",
                fill="white",
            )
            output = io.BytesIO()
            bitmap.save(output, "PNG")
            uploaded = await self.business(
                "image_upload",
                data=base64.b64encode(output.getvalue()).decode(),
            )
            require(set(uploaded) == {"id", "thumbnail"}, "Unexpected image response")
            images.append(uploaded["id"])
        clocks = [
            (
                datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=minute)
            ).strftime("%H:%M")
            for minute in range(1, 5)
        ]
        config = {
            "keywords": [
                {
                    "id": marker + "_kw",
                    "name": "Controlled keyword",
                    "enabled": True,
                    "match": "exact",
                    "terms": [marker],
                    "text": marker + "_reply",
                    "cooldown_seconds": 30,
                }
            ],
            "ads": [
                {
                    "id": marker + "_ad",
                    "name": "Controlled notice",
                    "enabled": True,
                    "text": marker + "_notice",
                    "images": images,
                    "mode": "daily",
                    "times": clocks,
                    "start": "09:00",
                    "end": "22:00",
                    "interval_minutes": 60,
                }
            ],
        }
        await self.save_business(config)
        await self.open_window("group_commands")
        await self.probe("抽奖状态")
        self.record(
            "keyword_configuration",
            preview_and_commit=True,
            native_matching=False,
            reason="Managed bots remain exempt; no exemption removed",
        )
        ledger = instance_dir(self.instances[BOT]) / "messages.sqlite3"
        database = ROOT / "data/plugin_data/wangshangliao_open/open-service.sqlite3"
        for index, clock in enumerate(clocks):
            row = await self.business("read")
            planned = next(
                s["next_at"] for s in row["schedules"] if s["rule_id"] == marker + "_ad"
            )
            require(0 < planned - time.time() < 130, "Unexpected scheduled boundary")
            key = f"advertisement/{BOT}/{GROUP}/{marker}_ad/{int(planned)}"
            sent = []
            for attempt in range(160):
                if attempt % 10 == 0:
                    await self.guard()
                sent = read_rows(
                    ledger,
                    "SELECT state,server_id FROM outbox WHERE key=?",
                    (key + "/image",),
                )
                if sent and sent[0]["state"] != "sending":
                    break
                await asyncio.sleep(1)
            require(
                sent and sent[0]["state"] == "accepted",
                "Scheduled image uncertain; inspect once, never replay",
            )
            delivery = []
            for _ in range(30):
                delivery = read_rows(
                    database,
                    "SELECT state FROM business_rule_sends WHERE operation=?",
                    (key,),
                )
                if delivery and delivery[0]["state"] != "dispatched":
                    break
                await asyncio.sleep(0.5)
            require(
                delivery == [{"state": "accepted"}],
                "Combined scheduled delivery not accepted; never replay",
            )
            text = read_rows(
                ledger, "SELECT state,server_id FROM outbox WHERE key=?", (key,)
            )
            require(
                text and text[0]["state"] == "accepted" and text[0]["server_id"],
                "Scheduled text not accepted",
            )
            witnesses = {}
            for uid in (MEMBER,):
                path = instance_dir(self.instances[uid]) / "messages.sqlite3"
                incoming = []
                for _ in range(20):
                    incoming = read_rows(
                        path,
                        "SELECT message,payload FROM inbox WHERE account=? AND team=? AND message=?",
                        (uid, GROUP, sent[0]["server_id"]),
                    )
                    if incoming:
                        break
                    await asyncio.sleep(0.5)
                payload = json.loads(incoming[0]["payload"]) if incoming else {}
                stored_image = read_rows(
                    database,
                    "SELECT asset,digest,width,height FROM business_rule_images WHERE id=?",
                    (images[index % 3],),
                )[0]
                require(
                    incoming
                    and payload.get("_test_witness") is True
                    and payload.get("sender") == BOT
                    and all(
                        payload.get("image", {}).get(k) == v
                        for k, v in stored_image.items()
                    ),
                    "Independent native image observation missing",
                )
                text_incoming = []
                for _ in range(20):
                    text_incoming = read_rows(
                        path,
                        "SELECT message,payload FROM inbox WHERE account=? AND team=? AND message=?",
                        (uid, GROUP, text[0]["server_id"]),
                    )
                    if text_incoming:
                        break
                    await asyncio.sleep(0.5)
                text_payload = (
                    json.loads(text_incoming[0]["payload"]) if text_incoming else {}
                )
                require(
                    text_payload.get("_test_witness") is True
                    and text_payload.get("sender") == BOT
                    and text_payload.get("text") == marker + "_notice"
                    and payload.get("created_at", 0) > 0
                    and payload["created_at"] <= text_payload.get("created_at", 0)
                    and int(incoming[0]["message"]) < int(text_incoming[0]["message"]),
                    "Independent image-before-text observation missing",
                )
                witnesses[uid] = {
                    "image": incoming[0]["message"],
                    "text": text_incoming[0]["message"],
                    "image_created_at": payload["created_at"],
                    "text_created_at": text_payload["created_at"],
                    "image_before_text": True,
                }
            rotation = read_rows(
                database,
                "SELECT position FROM business_ad_rotations WHERE account=? AND group_id=? AND rule_id=?",
                (BOT, GROUP, marker + "_ad"),
            )
            require(
                rotation == [{"position": (index + 1) % 3}],
                "Image rotation did not advance correctly",
            )
            self.record(
                "scheduled_image_" + str(index + 1),
                clock_beijing=clock,
                image=images[index % 3],
                receipt=sent[0],
                independent_witnesses=witnesses,
                next_position=(index + 1) % 3,
                phone_verified=False,
            )
        await self.save_business(self.original_business)

    async def flood_unmute(self):
        marker = "WSL_TEST_ACTIVITY_" + uuid.uuid4().hex
        await self.rules(
            {
                **self.expected["rules"],
                "advertising": False,
                "flood": True,
                "keywords": [],
            }
        )
        current = await self.rpc(
            "member_mutes", operation="read", account=BOT, group_id=GROUP
        )
        require(
            not current["members"],
            "Test group already has native mutes; do not disturb",
        )
        await self.open_window("group_rules", marker)
        await asyncio.sleep(31)
        sent = [await self.probe(marker) for _ in range(5)]
        path = instance_dir(self.instances[BOT]) / "moderation.sqlite3"
        batch = []
        for _ in range(100):
            batch = read_rows(
                path,
                "SELECT * FROM flood_batches WHERE account=? AND group_id=? AND trigger=?",
                (BOT, GROUP, sent[-1]),
            )
            if batch and batch[0]["mute_status"] in {"accepted", "verified"}:
                self.own_mute = True
                break
            await asyncio.sleep(0.5)
        require(
            self.own_mute,
            "Controlled flood not verified; inspect receipts without replay",
        )
        self.record("flood_mute", message_ids=sent, mute_status=batch[0]["mute_status"])
        await self.unmute()

    async def unmute(self):
        current = await self.rpc(
            "member_mutes", operation="read", account=BOT, group_id=GROUP
        )
        require(
            [m["id"] for m in current["members"]] == [MEMBER],
            "Unexpected native mute target",
        )
        payload = {
            "account": BOT,
            "group_id": GROUP,
            "version": current["version"],
            "member": MEMBER,
        }
        preview = await self.rpc("member_mutes", operation="preview", **payload)
        # Once submitted, an unknown result must never cause automatic cleanup replay.
        self.own_mute = False
        receipt = await self.rpc(
            "member_mutes",
            operation="commit",
            **payload,
            confirmation=preview["confirmation"],
        )
        require(receipt["verified"], "Unmute not verified; stop without replay")
        require(
            not (
                await self.rpc(
                    "member_mutes", operation="read", account=BOT, group_id=GROUP
                )
            )["members"],
            "Native mute still present",
        )
        self.record("current_mutes_unmute", **receipt)

    async def cleanup(self):
        errors = []
        for name, action in (
            (
                "own_lottery",
                lambda: self.rpc(
                    "test_lottery", operation_id=self.own_lottery, action="cancel"
                ),
            ),
            ("own_mute", self.unmute),
            ("original_business", lambda: self.save_business(self.original_business)),
            (
                "original_activity",
                lambda: self.save_activity(
                    lottery=self.original_activity["lottery"],
                    invite_rate=self.original_activity["invite_rate"],
                ),
            ),
            ("original_tenant", lambda: self.rules(self.original_tenant["rules"])),
        ):
            if getattr(self, name) is not None and (
                name.startswith("original") or getattr(self, name)
            ):
                try:
                    await action()
                except Exception as exc:
                    errors.append(name + ":" + str(exc))
        if self.window:
            try:
                await self.api(
                    "POST",
                    REGISTRATION,
                    {"action": "test_close", "instance_id": self.instances[BOT]},
                )
            except Exception as exc:
                errors.append("window:" + str(exc))
        for instance, (original, proposed) in self.grants.items():
            try:
                current = next(
                    b
                    for b in (await self.api("GET", PREFIX + "settings"))["bots"]
                    if b["id"] == instance
                )
                require(
                    current["proactive_send"] in (original, proposed),
                    "Concurrent send-grant change",
                )
                if current["proactive_send"] != original:
                    await self.api(
                        "POST",
                        PREFIX + "settings",
                        {
                            "bot_id": instance,
                            "revision": current["revision"],
                            "proactive_send": original,
                        },
                    )
            except Exception as exc:
                errors.append("grant:" + str(exc))
        self.record("cleanup", errors=errors)
        require(not errors, "Cleanup incomplete; inspect private evidence")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--phase", choices=("all", "scheduled"), default="all")
    parser.add_argument("--daily-write-limit", type=int, choices=(10, 20), default=10)
    args = parser.parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "group": GROUP,
                    "phase": args.phase,
                    "daily_write_limit": args.daily_write_limit,
                    "payments": False,
                    "restore_enabled_state": True,
                }
            )
        )
        return
    os.umask(0o077)
    output = Path("/opt/backups") / (
        "wsl-activity-pilot-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    output.mkdir(mode=0o700)
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    configs = {
        str(b["account_id"]): b
        for b in config["platform"]
        if b.get("type") == "wangshangliao"
    }
    require(set(configs) == {BOT, OWNER, MEMBER}, "Exact configured trio required")
    (output / "cmd_config.before.json").write_text(json.dumps(config))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 1800},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:6185",
            trust_env=False,
            timeout=60,
            headers={"Authorization": "Bearer " + token},
        ) as client,
        aiohttp.ClientSession() as http,
    ):
        pilot = Pilot(client, output)
        pilot.instances = {uid: c["id"] for uid, c in configs.items()}
        enabled = False
        granted = False
        lab = None
        protected_before = protected()
        try:
            require(
                configs[BOT].get("enable", True) and configs[OWNER].get("enable", True),
                "Existing bot or owner disabled",
            )
            if not configs[MEMBER].get("enable", True):
                await pilot.api(
                    "PATCH",
                    f"/api/v1/bots/{pilot.instances[MEMBER]}/enabled",
                    {"enabled": True},
                )
                enabled = True
            for _ in range(90):
                state = await pilot.state()
                accounts = {a["account"]: a for a in state["accounts"]}
                if set(accounts) == set(configs) and all(
                    a["state"] == "online" for a in accounts.values()
                ):
                    break
                await asyncio.sleep(1)
            require(
                set(accounts) == set(configs)
                and all(a["state"] == "online" for a in accounts.values()),
                "Trio not online",
            )
            require(
                all(accounts[u]["instance_id"] == pilot.instances[u] for u in configs),
                "Online identity mismatch",
            )
            for uid in configs:
                status = await pilot.api(
                    "POST",
                    REGISTRATION,
                    {"action": "test_status", "instance_id": pilot.instances[uid]},
                )
                require(
                    not (status.get("window") or {}).get("active"),
                    "Existing test window; do not replace",
                )
            clients, pins = {}, {}
            for uid, instance in pilot.instances.items():
                saved = Vault(instance).load()
                require(
                    str(saved["business"]["uid"]) == uid,
                    "Saved session identity mismatch",
                )
                pins[uid] = str(saved["nim_id"])
                if uid != MEMBER:
                    business = BusinessClient(Deployment.load(), http)
                    business.restore(saved["business"])
                    clients[uid] = business
            roster = await member_directory(clients[BOT], GROUP)
            members = {str(m["userId"]): m for m in roster["groupMemberInfo"]}
            require(
                roster["complete"]
                and set(members) == set(configs)
                and all(str(members[u]["nimId"]) == pins[u] for u in configs)
                and members[OWNER]["groupRole"] == "GROUP_ROLE_OWNER"
                and members[MEMBER]["groupRole"] == "GROUP_ROLE_MEMBER",
                "Controlled roster changed",
            )
            pilot.baseline = protected()
            lab = TestLab(
                clients,
                pins,
                GROUP,
                CONTROL,
                protection=lambda *a, **k: protected(),
                daily_write_limit=args.daily_write_limit,
            )
            if members[BOT]["groupRole"] == "GROUP_ROLE_MEMBER":
                preview = await lab.preview("grant")
                receipt = await lab.execute(preview["preview"], preview["confirmation"])
                require(
                    receipt["state"] == "verified", "Temporary role grant not verified"
                )
                granted = True
            await pilot.rpc("tenant_members", account=BOT, group_id=GROUP)
            pilot.original_tenant = copy.deepcopy(await pilot.tenant())
            pilot.expected = copy.deepcopy(pilot.original_tenant)
            state = await pilot.state()
            pilot.formal = [
                {k: t.get(k) for k in FIELDS}
                for t in state["groups"]
                if t["group_id"] != GROUP
            ]
            await pilot.guard()
            pilot.record(
                "three_online",
                accounts=pilot.instances,
                temporary_native_role=granted,
                daily_write_limit=args.daily_write_limit,
                protected_baseline=pilot.baseline,
            )
            await pilot.grant(pilot.instances[BOT])
            if args.phase == "all":
                pilot.original_activity = copy.deepcopy(await pilot.activity())
                current = pilot.original_activity["current_lottery"]
                require(
                    not current or current["status"] in {"finished", "cancelled"},
                    "Existing or uncertain lottery",
                )
            await pilot.rules(
                {
                    **pilot.expected["rules"],
                    "advertising": False,
                    "flood": False,
                    "keywords": [],
                }
            )
            if args.phase == "all":
                await pilot.packets()
            await pilot.scheduled_ad()
            if args.phase == "all":
                await pilot.flood_unmute()
        finally:
            try:
                if pilot.expected is not None:
                    await pilot.cleanup()
            finally:
                try:
                    if granted:
                        preview = await lab.preview("revoke")
                        receipt = await lab.execute(
                            preview["preview"], preview["confirmation"]
                        )
                        require(
                            receipt["state"] == "verified",
                            "Native role restore not verified",
                        )
                        pilot.record("native_role_restored", state=receipt["state"])
                finally:
                    if enabled:
                        await pilot.api(
                            "PATCH",
                            f"/api/v1/bots/{pilot.instances[MEMBER]}/enabled",
                            {"enabled": False},
                        )
                        pilot.record(
                            "bot_enabled_state_restored", account=MEMBER, enabled=False
                        )
        unchanged = protected() == protected_before
        require(unchanged, "Final formal/financial/config fingerprint changed")
        pilot.evidence["passed"] = True
        pilot.record("complete", protected_unchanged=unchanged)
    print(str(output), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
