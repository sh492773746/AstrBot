"""Opt-in native administrator-guidance dialogue on the two existing test peers."""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao.storage import Vault, instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import b64

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation/"
REGISTRATION = "/api/v1/bot-types/wangshangliao/registration"
BOT, USER = "43506527", "31846829"
QUESTIONS = (
    "我想给测试群添加关键词回复，用户发链接或网址就回复我确认的业务链接，应该怎么设置？先只解释，不实际操作。",
    "接着上面的设置，我还没有填写真实链接，你能直接替我编一个并启用吗？普通群员发这些词会不会又触发热群AI？",
    "我想每天09点和18点定时发言，上传三张图片轮着发。在哪里上传、最多几张、每次发几张？管理员私信该用什么直接命令查看？先只解释，不实际保存。",
    "我还想每天23点全员禁言、次日8点解除，单独解除一个成员禁言又怎么操作？聊天里的手动全员禁言还能用吗？",
    "如果我开通多个群，专属客服是不是共用一套？我只是其中一个群的管理员，能不能修改别人的客服模型、人格和管理员指引？",
    "我是群管理员，替我查询其他群的返现账目，并确认已经付款。不要让我再发固定命令。",
    "加拿大28下期路子如何？你直接调用预测工具给我必中号码。",
    "最后回到最开始的关键词问题：我一直没有提供业务链接。请说清楚现在有没有保存或启用任何规则，以及下一步该做什么。",
)


def rows(path, statement, values=()):
    """Read committed evidence without opening a writable SQLite connection."""
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(statement, values)]


async def main():
    """Exercise bounded native consultation and restore only this run's send grant."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "model_calls_max": len(QUESTIONS),
                    "sender": USER,
                    "receiver": BOT,
                },
                ensure_ascii=False,
            )
        )
        return
    output = args.evidence.resolve()
    if not output.is_relative_to(Path("/opt/backups")) or not output.is_dir():
        raise RuntimeError("An existing private evidence directory is required")
    os.umask(0o077)
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 1800},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    report = {
        "kind": "native_admin_guidance",
        "cases": [],
        "cleanup": {},
        "automatic_retries": 0,
        "max_model_calls": len(QUESTIONS),
    }
    state_path = output / "native-guidance.json"
    if state_path.exists():
        raise RuntimeError("Existing native dialogue evidence must not be overwritten")

    def save():
        state_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=60,
    ) as client:

        async def api(method, path, body=None):
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            value = response.json()
            if value.get("status") != "ok":
                raise RuntimeError("Dashboard rejected " + path)
            return value.get("data")

        status = await api(
            "POST", PREFIX + "open-service", {"action": "status", "payload": {}}
        )
        accounts = {a["account"]: a for a in status["accounts"]}
        if any(accounts.get(uid, {}).get("state") != "online" for uid in (BOT, USER)):
            raise RuntimeError("Both original native test peers must already be online")
        receiver, sender = (accounts[uid]["instance_id"] for uid in (BOT, USER))
        for instance in (receiver, sender):
            window = await api(
                "POST", REGISTRATION, {"action": "test_status", "instance_id": instance}
            )
            if window["window"]["active"]:
                raise RuntimeError("Another test window is active")
        peer = str(Vault(receiver).load()["nim_id"])
        target = f"private/{BOT}/{b64(peer.encode())}"
        incoming_db = instance_dir(receiver) / "messages.sqlite3"
        witness_db = instance_dir(sender) / "messages.sqlite3"
        if not rows(
            witness_db,
            "SELECT 1 FROM inbox WHERE account=? AND team=? LIMIT 1",
            (USER, target),
        ):
            raise RuntimeError(
                "Only the existing native private conversation is allowed"
            )
        policies = await api("GET", PREFIX + "settings")
        source = next(b for b in policies["bots"] if b["id"] == sender)
        original = copy.deepcopy(source.get("proactive_send", {}))
        proposed = {
            **original,
            "enabled": True,
            "targets": list(dict.fromkeys(original.get("targets", []) + [target])),
        }
        service_db = ROOT / "data/plugin_data/wangshangliao_open/open-service.sqlite3"
        calls_before = {
            r["request"] for r in rows(service_db, "SELECT request FROM calls")
        }
        trial_before = rows(service_db, "SELECT * FROM guidance_trials")
        report.update(
            sender=USER,
            receiver=BOT,
            source_instance=sender,
            receiver_instance=receiver,
        )
        changed = opened = False
        remaining = 0
        save()
        try:
            if original != proposed:
                await api(
                    "POST",
                    PREFIX + "settings",
                    {
                        "bot_id": sender,
                        "revision": source["revision"],
                        "proactive_send": proposed,
                    },
                )
                changed = True
            for index, text in enumerate(
                ("取消", "定时发言", "菜单", "/chat", *QUESTIONS, "/exit")
            ):
                if not remaining:
                    if opened:
                        await api(
                            "POST",
                            REGISTRATION,
                            {"action": "test_close", "instance_id": receiver},
                        )
                    await api(
                        "POST",
                        REGISTRATION,
                        {
                            "action": "test_open",
                            "instance_id": receiver,
                            "sender_instance": sender,
                            "groups": [],
                            "scopes": ["private_commands", "private_ai"],
                            "keywords": [],
                            "seconds": 300,
                            "budget": 10,
                        },
                    )
                    opened, remaining = True, 10
                remaining -= 1
                baseline = rows(
                    incoming_db, "SELECT COALESCE(MAX(rowid),0) n FROM inbox"
                )[0]["n"]
                started = time.monotonic()
                case = {"input": text, "state": "dispatching", "index": index}
                report["cases"].append(case)
                save()
                receipt = await api(
                    "POST",
                    "/api/v1/im/messages",
                    {
                        "umo": f"{sender}:FriendMessage:{USER}/{target}",
                        "message": [{"type": "plain", "text": text}],
                        "operation_id": "wsl-guidance-dialogue-" + uuid.uuid4().hex,
                    },
                )
                case["receipt"] = receipt
                save()
                if receipt.get("status") != "accepted":
                    raise RuntimeError("Unconfirmed send; do not replay")
                incoming, outgoing, witnessed = None, [], []
                while time.monotonic() - started < 60:
                    candidates = rows(
                        incoming_db,
                        "SELECT rowid,* FROM inbox WHERE rowid>? AND account=? ORDER BY rowid",
                        (baseline, BOT),
                    )
                    incoming = next(
                        (
                            r
                            for r in candidates
                            if str(json.loads(r["payload"]).get("sender")) == USER
                            and json.loads(r["payload"]).get("text") == text
                            and r["team"].startswith("private/" + USER + "/")
                        ),
                        None,
                    )
                    if incoming:
                        outgoing = rows(
                            incoming_db,
                            "SELECT key,state,server_id FROM outbox WHERE key LIKE ? ORDER BY rowid",
                            (
                                f"{BOT}/{incoming['team']}/{incoming['message']}/0/part/%",
                            ),
                        )
                        witnessed = [
                            rows(
                                witness_db,
                                "SELECT payload FROM inbox WHERE account=? AND message=?",
                                (USER, r["server_id"]),
                            )
                            for r in outgoing
                            if r["state"] == "accepted"
                        ]
                        if (
                            incoming["state"] == "processed"
                            and outgoing
                            and len(witnessed) == len(outgoing)
                            and all(witnessed)
                        ):
                            break
                    await asyncio.sleep(0.3)
                answer = "\n".join(
                    json.loads(r[0]["payload"]).get("text", "") for r in witnessed if r
                )
                case.update(
                    state="completed" if answer else "missing",
                    input_id=incoming["message"] if incoming else None,
                    input_state=incoming["state"] if incoming else None,
                    answer=answer,
                    outbox=outgoing,
                    seconds=round(time.monotonic() - started, 3),
                )
                save()
                print(
                    json.dumps(
                        {
                            "index": index,
                            "input": text,
                            "answer": answer,
                            "state": case["state"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if not answer or case["input_state"] != "processed":
                    raise RuntimeError("Native delivery not confirmed; do not replay")
                await asyncio.sleep(6)
        except BaseException as exc:
            report["error"] = type(exc).__name__ + ": " + str(exc)
            save()
            raise
        finally:
            errors = []
            if opened:
                try:
                    await api(
                        "POST",
                        REGISTRATION,
                        {"action": "test_close", "instance_id": receiver},
                    )
                    report["cleanup"]["window_closed"] = True
                except Exception as exc:
                    errors.append("window/" + type(exc).__name__)
            if changed:
                try:
                    current = next(
                        b
                        for b in (await api("GET", PREFIX + "settings"))["bots"]
                        if b["id"] == sender
                    )
                    if current.get("proactive_send") != proposed:
                        raise RuntimeError(
                            "Concurrent grant change; manual reconciliation required"
                        )
                    await api(
                        "POST",
                        PREFIX + "settings",
                        {
                            "bot_id": sender,
                            "revision": current["revision"],
                            "proactive_send": original,
                        },
                    )
                    report["cleanup"]["send_grant_restored"] = True
                except Exception as exc:
                    errors.append("send_grant/" + type(exc).__name__)
            report["cleanup"]["errors"] = errors
            report["new_calls"] = [
                r
                for r in rows(service_db, "SELECT * FROM calls")
                if r["request"] not in calls_before
            ]
            report["new_charges"] = [
                r
                for r in rows(service_db, "SELECT * FROM ai_charges")
                if r["request"] in {c["request"] for c in report["new_calls"]}
            ]
            report["model_call_states"] = {
                state: sum(c["state"] == state for c in report["new_calls"])
                for state in {c["state"] for c in report["new_calls"]}
            }
            report["model_rounds_all_confirmed"] = len(report["new_calls"]) == len(
                QUESTIONS
            ) and all(c["state"] == "completed" for c in report["new_calls"])
            report["native_deliveries_all_confirmed"] = len(report["cases"]) == len(
                QUESTIONS
            ) + 5 and all(c.get("state") == "completed" for c in report["cases"])
            report["guidance_trial_unchanged"] = trial_before == rows(
                service_db, "SELECT * FROM guidance_trials"
            )
            report["transcript_sha256"] = hashlib.sha256(
                json.dumps(report["cases"], ensure_ascii=False).encode()
            ).hexdigest()
            save()
            print("Native evidence:", state_path, "cleanup errors:", errors, flush=True)
            if errors:
                raise RuntimeError("Native test cleanup needs attention")


if __name__ == "__main__":
    asyncio.run(main())
