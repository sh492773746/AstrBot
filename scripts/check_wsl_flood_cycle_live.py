"""Explicit fixed-three-account acceptance with naturally elapsed penalty timers."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import aiohttp
import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.directory import member_directory
from astrbot.core.platform.sources.wangshangliao.storage import Vault, instance_dir

ROOT = Path(__file__).resolve().parents[1]
GROUP, RECEIVER, OWNER, TARGET = "1459282", "43506527", "31846829", "23691273"
IDS = {
    RECEIVER: "wangshangliao_f79c5b81",
    OWNER: "wangshangliao_b35431aa",
    TARGET: "wangshangliao_6541e09d",
}
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation/"


def rows(path, sql, arguments=()):
    """Read bounded evidence without creating or changing application stores."""
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, arguments)]


def protected():
    """Fingerprint business data outside this fixed test group."""
    root = ROOT / "data"
    result = {}
    store = root / "plugin_data/wangshangliao_open/open-service.sqlite3"
    for table in ("tenants", "business_rules", "group_ai"):
        values = rows(
            store,
            f"SELECT * FROM {table} WHERE group_id<>? ORDER BY account,group_id",
            (GROUP,),
        )
        for row in values:
            if table == "tenants":
                row.pop("native_role", None)
        result[table] = hashlib.sha256(
            json.dumps(values, sort_keys=True).encode()
        ).hexdigest()
    finance = root / "plugin_data/wangshangliao_cashback/cashback.sqlite3"
    for table in ("groups", "entries"):
        result["finance/" + table] = hashlib.sha256(
            json.dumps(
                rows(finance, f"SELECT * FROM {table} ORDER BY id"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    for name, table in (
        ("member_blacklist.sqlite3", "blacklist_entries"),
        ("member_exemptions.sqlite3", "speech_exemptions"),
    ):
        for path in sorted((root / "platform_data/wangshangliao").rglob(name)):
            result[str(path.relative_to(ROOT))] = hashlib.sha256(
                json.dumps(
                    rows(
                        path,
                        f"SELECT * FROM {table} WHERE group_id<>? ORDER BY account,group_id,member",
                        (GROUP,),
                    ),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
    return result


def restore_values(original):
    """Normalize legacy rule snapshots without erasing newer rule fields."""
    rules = dict(original["rules"])
    rules.setdefault("custom_rules", [])
    return {
        "rules": rules,
        "managers": list(original["managers"]),
        "paused": bool(original["paused"]),
    }


async def cleanup_steps(steps, record):
    """Attempt every independent restoration even if an earlier step fails."""
    errors = []
    for name, restore in steps:
        try:
            await restore()
        except Exception as exc:
            errors.append(name)
            record("cleanup_failed", step=name, detail=str(exc)[:600])
    return errors


async def main():
    """Open short windows, verify real receipts and always restore temporary state."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume-directory", type=Path)
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"dry_run": True, "group": GROUP, "natural_wait_minutes": 14}))
        return
    from scripts.wsl_test_lab import CONTROL, TestLab, protected_state

    os.umask(0o077)
    output = Path("/opt/backups") / (
        "wsl-flood-cycle-live-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    output.mkdir(mode=0o700)
    operation = "WSL_TEST_FLOOD_" + uuid.uuid4().hex
    resume = None
    if args.resume_directory:
        directory = args.resume_directory.resolve(strict=True)
        if (
            directory.parent != Path("/opt/backups")
            or not directory.name.startswith("wsl-flood-cycle-live-")
            or directory.stat().st_uid != os.geteuid()
            or directory.stat().st_mode & 0o077
        ):
            raise RuntimeError("Unsafe continuation evidence directory")
        resume = json.loads((directory / "baseline.json").read_text())
        operation = resume["operation"]
        if not re.fullmatch(r"WSL_TEST_FLOOD_[0-9a-f]{32}", operation):
            raise RuntimeError("Invalid continuation operation")
    evidence = []

    def record(case, **data):
        evidence.append({"case": case, "at": time.time(), **data})
        (output / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps({"case": case, **data}, ensure_ascii=False), flush=True)

    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    dashboard = config["dashboard"]
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 7200},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    baseline = protected()
    native = instance_dir(IDS[RECEIVER])
    isolated = native / "test-flood" / operation / "moderation.sqlite3"
    if resume:
        completed = rows(
            isolated,
            "SELECT d.minutes,m.status FROM automatic_mutes m "
            "JOIN automatic_mute_durations d ON d.operation=m.operation "
            "WHERE m.account=? AND m.group_id=? AND m.member=?",
            (RECEIVER, GROUP, TARGET),
        )
        if (
            {r["minutes"] for r in completed if r["status"] in {"accepted", "verified"}}
            != {1, 3, 10}
            or any(r["status"] not in {"accepted", "verified"} for r in completed)
            or protected() != resume["protected"]
        ):
            raise RuntimeError("Continuation requires known accepted prior sanctions")
    original = changed = lab = None
    granted = window_open = rules_changed = bound = False
    original_rules = None
    async with (
        aiohttp.ClientSession() as session,
        httpx.AsyncClient(
            base_url="http://127.0.0.1:6185",
            timeout=75,
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
        ) as http,
    ):

        async def api(method, route, payload=None):
            response = await http.request(method, route, json=payload)
            if response.is_error:
                raise RuntimeError(
                    f"{route}: HTTP {response.status_code}: {response.text[:500]}"
                )
            value = response.json()
            if value.get("status") != "ok":
                raise RuntimeError(f"{route}: {value.get('message')}")
            return value.get("data")

        async def rpc(action, **payload):
            return await api(
                "POST", PREFIX + "open-service", {"action": action, "payload": payload}
            )

        async def bots():
            return {
                row["account_id"]: row
                for row in (await api("GET", PREFIX + "settings"))["bots"]
            }

        async def tenant():
            return next(
                row
                for row in (await rpc("status"))["groups"]
                if row["account"] == RECEIVER and row["group_id"] == GROUP
            )

        async def patch(value):
            current = await tenant()
            proposal = restore_values(value)
            proposal.update(
                account=RECEIVER, group_id=GROUP, version=current["version"]
            )
            preview = await rpc("tenant_preview", **proposal)
            assert (
                await rpc("tenant", **proposal, confirmation=preview["confirmation"])
            )["saved"]

        async def control(action, **payload):
            return await rpc(
                "test_flood", operation=action, operation_id=operation, **payload
            )

        async def close():
            nonlocal window_open, bound
            try:
                if bound:
                    await control("close")
                    bound = False
            finally:
                if window_open:
                    bot = (await bots())[RECEIVER]
                    await api(
                        "POST",
                        PREFIX + "test-window",
                        {
                            "bot_id": IDS[RECEIVER],
                            "revision": bot["revision"],
                            "action": "test_close",
                        },
                    )
                    window_open = bound = False

        async def begin(case):
            nonlocal window_open, bound
            await close()
            bot = (await bots())[RECEIVER]
            await api(
                "POST",
                PREFIX + "test-window",
                {
                    "bot_id": IDS[RECEIVER],
                    "revision": bot["revision"],
                    "action": "test_open",
                    "sender_instance": IDS[TARGET],
                    "groups": [GROUP],
                    "scopes": ["group_rules"],
                    "keywords": [operation],
                    "seconds": 300,
                    "budget": 10,
                },
            )
            window_open = True
            assert (await control("begin", case=case))["bound"]
            bound = True

        async def send(case, warning=None, minutes=None):
            response = await control("send", case=case)
            messages = [row["message"] for row in response["receipts"]]
            assert messages
            for _ in range(60):
                placeholders = ",".join("?" for _ in messages)
                incoming = rows(
                    native / "messages.sqlite3",
                    f"SELECT message,payload FROM inbox WHERE account=? AND team=? AND message IN ({placeholders})",
                    (RECEIVER, GROUP, *messages),
                )
                actions = rows(
                    isolated,
                    "SELECT message,result FROM progressive_actions ORDER BY created",
                )
                matching = [row for row in actions if row["message"] in messages]
                completed = bool(matching) and all(
                    json.loads(row["result"]).get("status")
                    in {"accepted", "verified", "rejected"}
                    for row in matching
                )
                if len(incoming) == len(messages) and (
                    completed or warning is None and minutes is None
                ):
                    break
                await asyncio.sleep(0.5)
            assert len(incoming) == len(messages), "Missing real receiver ingress"
            peer = response["independent_receipts"]
            assert {row["message"] for row in peer} == set(messages), (
                "Missing independent owner receipt"
            )
            assert all(
                (row["account"], row["group"], row["sender"]) == (OWNER, GROUP, TARGET)
                for row in peer
            )
            if warning is not None or minutes is not None:
                assert len(matching) == 1, matching
                result = json.loads(matching[0]["result"])
                assert (
                    result.get("warning_number") == warning
                    if warning is not None
                    else result.get("minutes") == minutes
                ), result
                assert result["status"] in {"accepted", "verified"}, result
                for _ in range(100):
                    recalls = rows(
                        native / "moderation.sqlite3",
                        f"SELECT message,status FROM flood_recalls WHERE message IN ({placeholders})",
                        tuple(messages),
                    )
                    if len(recalls) == len(messages) and all(
                        row["status"] in {"accepted", "verified"} for row in recalls
                    ):
                        break
                    await asyncio.sleep(0.3)
                assert len(recalls) == len(messages) and all(
                    row["status"] in {"accepted", "verified"} for row in recalls
                ), recalls
            else:
                await asyncio.sleep(3)
                assert not [
                    row
                    for row in rows(isolated, "SELECT message FROM progressive_actions")
                    if row["message"] in messages
                ], "Unexpected sanction"
            record(
                case,
                messages=messages,
                independent_receipts=len(peer),
                warning=warning,
                minutes=minutes,
                ingress_counters=[
                    json.loads(row["payload"]).get("_ingress_flood") for row in incoming
                ],
            )
            return response

        try:
            original = await bots()
            assert set(original) == set(IDS)
            changed = []
            for uid in (OWNER, TARGET):
                if not original[uid]["enable"]:
                    changed.append(uid)
                    await api(
                        "PATCH",
                        "/api/v1/bots/enabled",
                        {"bot_id": IDS[uid], "enabled": True},
                    )
            for _ in range(60):
                current = await bots()
                if all(current[uid]["state"] == "online" for uid in IDS):
                    break
                await asyncio.sleep(2)
            assert all(current[uid]["state"] == "online" for uid in IDS)
            clients, pins = {}, {}
            for uid, instance in IDS.items():
                saved = Vault(instance).load()
                assert str(saved["business"]["uid"]) == uid
                pins[uid] = str(saved["nim_id"])
                if uid in {RECEIVER, OWNER}:
                    client = BusinessClient(Deployment.load(), session)
                    client.restore(saved["business"])
                    clients[uid] = client
            roster = await member_directory(clients[OWNER], GROUP)
            members = {str(row["userId"]): row for row in roster["groupMemberInfo"]}
            assert roster["complete"] and set(members) == set(IDS)
            assert members[OWNER]["groupRole"] == "GROUP_ROLE_OWNER"
            assert members[TARGET]["groupRole"] == "GROUP_ROLE_MEMBER"
            lab = TestLab(
                clients,
                pins,
                GROUP,
                CONTROL,
                protection=lambda group, first=False: protected_state(
                    ROOT, group, first=first
                ),
                daily_write_limit=20,
            )
            if members[RECEIVER]["groupRole"] == "GROUP_ROLE_MEMBER":
                preview = await lab.preview("grant")
                assert (await lab.execute(preview["preview"], preview["confirmation"]))[
                    "state"
                ] == "verified"
                granted = True
            await rpc("tenant_members", account=RECEIVER, group_id=GROUP)
            original_rules = await tenant()
            assert original_rules["active"] and not original_rules["paused"]
            muted = await rpc(
                "member_mutes", account=RECEIVER, group_id=GROUP, operation="read"
            )
            assert TARGET not in {row["id"] for row in muted["members"]}
            await patch(
                {
                    **original_rules,
                    "rules": {
                        **original_rules["rules"],
                        "advertising": False,
                        "flood": True,
                        "keywords": [],
                        "custom_rules": [],
                    },
                    "managers": [OWNER],
                    "paused": False,
                }
            )
            rules_changed = True
            (output / "baseline.json").write_text(
                json.dumps(
                    {
                        "protected": baseline,
                        "configuration_digest": digest,
                        "original_rules": original_rules,
                        "operation": operation,
                        "original_enables": {
                            uid: row["enable"] for uid, row in original.items()
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            record("three_accounts_online_and_fixed_scope_ready", group=GROUP)
            if resume:
                record(
                    "prior_final_mute_now_absent_no_sanction_replayed",
                    resumed_operation=operation,
                    original_expiry_readback_delayed=True,
                )
            else:
                await begin("below")
                await send("below")
            for case, warning in (
                () if resume else (("rapid", 1), ("slow", 2), ("repeat", 3))
            ):
                await begin(case)
                await send(case, warning=warning)
            for minutes in () if resume else (1, 3, 10):
                await begin("rapid")
                await send("rapid", minutes=minutes)
                muted = await rpc(
                    "member_mutes", account=RECEIVER, group_id=GROUP, operation="read"
                )
                assert TARGET in {row["id"] for row in muted["members"]}
                await close()
                record(
                    "waiting_for_natural_expiry", minutes=minutes, window_closed=True
                )
                await asyncio.sleep(minutes * 60 + 3)
                muted = await rpc(
                    "member_mutes", account=RECEIVER, group_id=GROUP, operation="read"
                )
                assert TARGET not in {row["id"] for row in muted["members"]}, muted
                record("native_expiry_verified", minutes=minutes)
            await begin("rank_recall")
            await send("rank_recall", warning=1)
            await asyncio.sleep(33)
            listing = await rpc(
                "member_whitelist",
                account=RECEIVER,
                group_id=GROUP,
                tenant_version=(await tenant())["version"],
                operation="read",
            )
            assert TARGET not in {row["id"] for row in listing["members"]}
            record("recalled_text_did_not_award_whitelist")
            await send("rank")
            for _ in range(40):
                await asyncio.sleep(2)
                listing = await rpc(
                    "member_whitelist",
                    account=RECEIVER,
                    group_id=GROUP,
                    tenant_version=(await tenant())["version"],
                    operation="read",
                )
                member = next(
                    (row for row in listing["members"] if row["id"] == TARGET), None
                )
                if member:
                    break
            assert (
                member
                and member.get("source") == "daily_top30"
                and member.get("count") == 1
            ), member
            record("automatic_whitelist_verified", count=member["count"])
            await send("exempt")
            record("controlled_acceptance_passed", phone_observation=False)
        except BaseException as exc:
            record("failed", error=type(exc).__name__, detail=str(exc)[:800])
            raise
        finally:

            async def restore_window():
                try:
                    if rules_changed:
                        muted = await rpc(
                            "member_mutes",
                            account=RECEIVER,
                            group_id=GROUP,
                            operation="read",
                        )
                        if TARGET in {row["id"] for row in muted["members"]}:
                            if not bound:
                                await begin("below")
                            result = await control("unmute")
                            assert result["result"]["status"] in {
                                "accepted",
                                "verified",
                            }
                            record("cleanup_unmute_accepted")
                            current = await rpc(
                                "member_mutes",
                                account=RECEIVER,
                                group_id=GROUP,
                                operation="read",
                            )
                            assert TARGET not in {
                                row["id"] for row in current["members"]
                            }, "Test member remains muted"
                finally:
                    await close()

            async def restore_rules():
                if rules_changed:
                    await patch(original_rules)
                    record("test_rules_restored_via_preview_commit")

            async def restore_role():
                if granted and lab is not None:
                    preview = await lab.preview("revoke")
                    result = await lab.execute(
                        preview["preview"], preview["confirmation"]
                    )
                    assert result["state"] == "verified"
                    record("native_role_restored")

            async def restore_enable(uid):
                await api(
                    "PATCH",
                    "/api/v1/bots/enabled",
                    {"bot_id": IDS[uid], "enabled": bool(original[uid]["enable"])},
                )

            errors = await cleanup_steps(
                [
                    ("window", restore_window),
                    ("rules", restore_rules),
                    ("role", restore_role),
                    *[
                        ("enable/" + uid, lambda uid=uid: restore_enable(uid))
                        for uid in reversed(changed or [])
                    ],
                ],
                record,
            )
            after = await bots()
            configuration = json.loads(
                (ROOT / "data/cmd_config.json").read_text("utf-8-sig")
            )
            result = {
                "protected_business_data_unchanged": protected() == baseline,
                "original_enables_restored": all(
                    after[uid]["enable"] == original[uid]["enable"] for uid in IDS
                )
                if original
                else not changed,
                "configuration_restored": hashlib.sha256(
                    json.dumps(configuration, sort_keys=True).encode()
                ).hexdigest()
                == digest,
                "all_cleanup_steps_completed": not errors,
            }
            record("restoration", **result)
            assert all(result.values()), result


if __name__ == "__main__":
    asyncio.run(main())
