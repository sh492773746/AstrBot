"""Send bounded group probes without bypassing managed-account protection."""

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from scripts.provision_wsl_business_knowledge import API

SENDER = "wangshangliao_0acde7ec"
RECEIVER = "wangshangliao_3cf3b038"
USER = "20000001"
BOT = "20000002"
GROUP = "1143980"


def rows(filename, table, query, parameters=(), instance=RECEIVER):
    path = instance_dir(instance) / filename
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            return []
        return [dict(row) for row in db.execute(query, parameters)]


async def run():
    config = json.loads(Path("data/cmd_config.json").read_text(encoding="utf-8-sig"))
    dashboard = config["dashboard"]
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 300},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    with httpx.Client(
        base_url="http://127.0.0.1:6185/api/v1",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=30,
    ) as client:
        api = API(client)
        registration = "/bot-types/wangshangliao/registration"
        window = api.call(
            "POST",
            registration,
            json={
                "action": "test_status",
                "instance_id": RECEIVER,
            },
        )["window"]
        assert not window.get("active"), "An existing test window is active"
        original = {
            bid: api.call("GET", "/bots/by-id?bot_id=" + bid)["bot"]
            for bid in (SENDER, RECEIVER)
        }
        assert str(original[SENDER]["account_id"]) == USER
        assert str(original[RECEIVER]["account_id"]) == BOT
        assert original[SENDER]["proactive_send"]["enabled"]
        assert GROUP in original[SENDER]["proactive_send"]["targets"]
        assert original[RECEIVER]["moderation"]["progressive_mute"]
        marker = "WSL_TEST_PROGRESSIVE_" + uuid.uuid4().hex[:8]
        baseline = rows("moderation.sqlite3", "operations", "SELECT id FROM operations")
        replies = []
        started = False
        try:
            api.call(
                "POST",
                registration,
                json={
                    "action": "test_open",
                    "instance_id": RECEIVER,
                    "sender_instance": SENDER,
                    "groups": [GROUP],
                    "scopes": ["group_commands", "group_rules"],
                    "keywords": [marker],
                    "seconds": 180,
                    "budget": 3,
                },
            )
            started = True
            cases = [
                ("ranking", "排名"),
                (
                    "report",
                    marker
                    + " 举报骗子发的：首次充值送50%，注册链接 https://example.invalid，大家别点",
                ),
                (
                    "violation",
                    marker
                    + " 首次充值送50%，注册链接 https://example.invalid，联系我vx:test123456",
                ),
            ]
            for name, text in cases:
                operation = "wsl-progressive-live-" + uuid.uuid4().hex
                api.call(
                    "POST",
                    "/im/messages",
                    json={
                        "umo": f"{SENDER}:GroupMessage:{USER}/{GROUP}",
                        "message": [{"type": "plain", "text": text}],
                        "operation_id": operation,
                    },
                )
                incoming = None
                for _ in range(80):
                    candidates = rows(
                        "messages.sqlite3",
                        "inbox",
                        "SELECT message,payload,state FROM inbox WHERE account=? AND team=? ORDER BY rowid DESC LIMIT 30",
                        (BOT, GROUP),
                    )
                    incoming = next(
                        (
                            r
                            for r in candidates
                            if json.loads(r["payload"]).get("text") == text
                            and str(json.loads(r["payload"]).get("sender")) == USER
                        ),
                        None,
                    )
                    if incoming and incoming["state"] == "processed":
                        break
                    await asyncio.sleep(0.5)
                assert incoming and incoming["state"] == "processed", (
                    name,
                    incoming and incoming["state"],
                )
                mid = incoming["message"]
                outgoing = rows(
                    "messages.sqlite3",
                    "outbox",
                    "SELECT key,state,server_id FROM outbox WHERE key LIKE ?",
                    (f"{BOT}/{GROUP}/{mid}/0/part/%",),
                )
                if name == "ranking":
                    assert outgoing and all(r["state"] == "accepted" for r in outgoing)
                    replies.extend(r["key"] for r in outgoing)
                record = rows(
                    "moderation.sqlite3",
                    "content_violations",
                    "SELECT status,warning,category FROM content_violations WHERE account=? AND group_id=? AND message=?",
                    (BOT, GROUP, mid),
                )
                print(
                    json.dumps(
                        {
                            "case": name,
                            "message_id": mid,
                            "inbox": incoming["state"],
                            "replies": [
                                {"state": r["state"], "message_id": r["server_id"]}
                                for r in outgoing
                            ],
                            "rule_record": record,
                            "semantic_skipped_by_existing_test_window": name
                            != "ranking",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            for key in replies:
                recall = []
                for _ in range(80):
                    recall = rows(
                        "messages.sqlite3",
                        "reply_recalls",
                        "SELECT state FROM reply_recalls WHERE key=?",
                        (key,),
                    )
                    if recall and recall[0]["state"] in {
                        "accepted",
                        "verified",
                        "rejected",
                    }:
                        break
                    await asyncio.sleep(0.5)
                print(json.dumps({"ranking_reply_recall": recall}), flush=True)
            after = rows(
                "moderation.sqlite3", "operations", "SELECT id FROM operations"
            )
            added = {r["id"] for r in after} - {r["id"] for r in baseline}
            print(
                json.dumps(
                    {
                        "new_moderation_operations": len(added),
                        "live_progressive_sanctions_verified": False,
                        "blocker": "managed_account_exemption",
                    }
                ),
                flush=True,
            )
        finally:
            if started:
                api.call(
                    "POST",
                    registration,
                    json={"action": "test_close", "instance_id": RECEIVER},
                )
            assert all(
                api.call("GET", "/bots/by-id?bot_id=" + bid)["bot"] == before
                for bid, before in original.items()
            ), "Configuration changed during test"
            print(
                "cleanup: test window closed; both bot configurations unchanged",
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(run())
