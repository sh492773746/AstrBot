"""Run a bounded live lottery and invitation-reward acceptance test."""

import asyncio
import copy
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao.storage import Vault, instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import b64

SENDER = "wangshangliao_0acde7ec"
RECEIVER = "wangshangliao_3cf3b038"
USER = "20000001"
BOT = "20000002"
GROUP = "1143980"
PROFILE = "cba0c0a2-f0f6-4edb-b7e1-1d656fc9a7f1"


def read_rows(statement, values=()):
    """Read the activity ledger without holding a writable connection."""
    path = next(instance_dir(RECEIVER).glob("activities.sqlite3"))
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(statement, values)]


def message_rows(instance, filename, statement, values=()):
    """Read a native message ledger without creating or mutating it."""
    path = instance_dir(instance) / filename
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(statement, values)]


def update_activity(statement, values=()):
    """Apply one bounded cleanup statement to the test account and group."""
    path = next(instance_dir(RECEIVER).glob("activities.sqlite3"))
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(statement, values)


class LiveActivityTest:
    """Drive real native messages through the configured receiver account."""

    def __init__(self, client):
        self.client = client
        peer = Vault(RECEIVER).load()["nim_id"]
        self.private = f"private/{BOT}/{b64(str(peer).encode())}"
        self.window = False
        self.remaining = 0
        self.recall_keys = []
        self.test_lottery_ids = []
        self.original_settings = None
        self.test_activation = ""
        self.baseline_members = []
        self.test_started = 0

    async def api(self, method, path, data=None):
        """Call a dashboard API and expose only a safe error summary."""
        response = await self.client.request(method, path, json=data)
        value = response.json()
        if response.is_error or value.get("status") != "ok":
            raise RuntimeError(
                f"API rejected ({response.status_code}): {value.get('message', path)}"
            )
        return value.get("data")

    async def online(self):
        """Wait until both controlled native instances are online."""
        for _ in range(35):
            settings = await self.api(
                "GET",
                "/api/v1/plugins/extensions/wangshangliao_moderation/settings",
            )
            if all(
                any(
                    item["id"] == instance and item["state"] == "online"
                    for item in settings["bots"]
                )
                for instance in (SENDER, RECEIVER)
            ):
                return
            await asyncio.sleep(2)
        raise RuntimeError("Controlled Wangshangliao accounts are not online")

    async def window_open(self):
        """Open a bounded command window without changing administrator roles."""
        if self.window:
            await self.window_close()
        await self.api(
            "POST",
            "/api/v1/bot-types/wangshangliao/registration",
            {
                "action": "test_open",
                "instance_id": RECEIVER,
                "sender_instance": SENDER,
                "groups": [GROUP],
                "scopes": ["private_commands", "group_commands"],
                "keywords": [],
                "seconds": 300,
                "budget": 10,
            },
        )
        self.window = True
        self.remaining = 10

    async def window_close(self):
        """Close the receiver's developer window."""
        await self.api(
            "POST",
            "/api/v1/bot-types/wangshangliao/registration",
            {"action": "test_close", "instance_id": RECEIVER},
        )
        self.window = False
        self.remaining = 0

    async def prepare(self):
        """Grant only temporary test transport targets and group routing."""
        self.test_started = time.time()
        sender = copy.deepcopy(
            (await self.api("GET", "/api/v1/bots/by-id?bot_id=" + SENDER))["bot"]
        )
        receiver = copy.deepcopy(
            (await self.api("GET", "/api/v1/bots/by-id?bot_id=" + RECEIVER))["bot"]
        )
        self.original_sender = sender
        self.original_receiver = receiver
        sender["enabled_groups"] = list(
            dict.fromkeys(sender.get("enabled_groups", []) + [GROUP])
        )
        sender["proactive_send"] = {
            "enabled": True,
            "targets": list(
                dict.fromkeys(
                    sender.get("proactive_send", {}).get("targets", [])
                    + [GROUP, self.private]
                )
            ),
        }
        receiver["proactive_send"] = {
            "enabled": True,
            "targets": list(
                dict.fromkeys(
                    receiver.get("proactive_send", {}).get("targets", []) + [GROUP]
                )
            ),
        }
        await self.api(
            "PUT",
            "/api/v1/bots/by-id",
            {"bot_id": SENDER, "config": sender},
        )
        await self.api(
            "PUT",
            "/api/v1/bots/by-id",
            {"bot_id": RECEIVER, "config": receiver},
        )
        await self.online()

        profile_path = "/api/v1/config-profiles/" + PROFILE
        profile = (await self.api("GET", profile_path))["config"]
        admins = list(profile.get("admins_id", []))
        if USER not in admins:
            admins.append(USER)
            profile["admins_id"] = admins
            await self.api("PUT", profile_path, profile)
        print("ADMIN_READY: 我不知道啊 / 20000001", flush=True)

        existing = read_rows(
            "SELECT * FROM lottery_settings WHERE account=? AND group_id=?",
            (BOT, GROUP),
        )
        if existing:
            self.original_settings = existing[0]
        if read_rows(
            "SELECT COUNT(*) AS n FROM lotteries WHERE account=? AND group_id=?",
            (BOT, GROUP),
        )[0]["n"]:
            raise RuntimeError("Activity ledger already contains lottery history")
        if read_rows(
            "SELECT COUNT(*) AS n FROM invite_rules WHERE account=? AND group_id=?",
            (BOT, GROUP),
        )[0]["n"]:
            raise RuntimeError("Activity ledger already contains invitation settings")

    async def send(self, scope, text, expected):
        """Send one real command and verify the peer received its response."""
        if not self.remaining:
            await self.window_open()
        self.remaining -= 1
        before = message_rows(
            RECEIVER,
            "messages.sqlite3",
            "SELECT COALESCE(MAX(rowid), 0) AS n FROM inbox",
        )[0]["n"]
        incoming_target = GROUP if scope == "group" else self.private
        session_kind = "GroupMessage" if scope == "group" else "FriendMessage"
        await self.api(
            "POST",
            "/api/v1/im/messages",
            {
                "umo": f"{SENDER}:{session_kind}:{USER}/{incoming_target}",
                "message": [{"type": "plain", "text": text}],
                "operation_id": "wsl-activity-test-" + uuid.uuid4().hex,
            },
        )
        incoming = None
        outgoing = []
        for _ in range(40):
            candidates = message_rows(
                RECEIVER,
                "messages.sqlite3",
                "SELECT rowid,* FROM inbox WHERE rowid>? AND account=? ORDER BY rowid",
                (before, BOT),
            )
            incoming = next(
                (
                    row
                    for row in candidates
                    if str(json.loads(row["payload"]).get("sender")) == USER
                    and (
                        row["team"] == GROUP
                        if scope == "group"
                        else row["team"].startswith("private/" + USER + "/")
                    )
                ),
                None,
            )
            if incoming:
                prefix = f"{BOT}/{incoming['team']}/{incoming['message']}/0"
                outgoing = message_rows(
                    RECEIVER,
                    "messages.sqlite3",
                    "SELECT key,state,server_id FROM outbox WHERE key LIKE ? ORDER BY rowid",
                    (prefix + "/part/%",),
                )
                if (
                    incoming["state"] == "processed"
                    and outgoing
                    and all(row["state"] == "accepted" for row in outgoing)
                ):
                    break
            await asyncio.sleep(0.5)
        if not incoming or not outgoing:
            raise RuntimeError(f"Missing accepted response for {scope}: {text}")
        replies = []
        for row in outgoing:
            received = []
            for _ in range(20):
                received = message_rows(
                    SENDER,
                    "messages.sqlite3",
                    "SELECT payload FROM inbox WHERE account=? AND message=?",
                    (USER, row["server_id"]),
                )
                if received:
                    break
                await asyncio.sleep(0.5)
            if not received:
                raise RuntimeError(f"Peer did not receive response for {text}")
            replies.append(json.loads(received[0]["payload"]).get("text", ""))
            recalls = message_rows(
                RECEIVER,
                "messages.sqlite3",
                "SELECT * FROM reply_recalls WHERE key=?",
                (row["key"],),
            )
            if scope == "group":
                if not recalls:
                    raise RuntimeError(f"Group response has no recall job: {text}")
                self.recall_keys.append(row["key"])
            elif recalls:
                raise RuntimeError(f"Private response was scheduled for recall: {text}")
        reply = "\n".join(replies)
        if expected not in reply:
            raise RuntimeError(
                f"Unexpected response for {text}: {reply.replace(chr(10), ' | ')}"
            )
        print(f"COMMAND_OK {scope}: {text} -> {expected}", flush=True)
        return reply

    async def proactive_texts(self, prefix):
        """Return accepted proactive outbox rows for one activity prefix."""
        rows = message_rows(
            RECEIVER,
            "messages.sqlite3",
            "SELECT key,state FROM outbox WHERE key LIKE ? ORDER BY rowid",
            (prefix + "%",),
        )
        if not rows or not all(row["state"] == "accepted" for row in rows):
            raise RuntimeError(f"Proactive send was not accepted: {prefix}")
        for row in rows:
            if row["key"] not in self.recall_keys:
                self.recall_keys.append(row["key"])
        print(f"PROACTIVE_OK: {prefix}", flush=True)
        return rows

    async def run(self):
        """Configure, exercise and query both group activities."""
        await self.prepare()
        await self.window_open()
        for command, expected in (
            ("群列表", GROUP),
            ("选择群 1", GROUP),
            ("抽奖奖励 测试奖品（不派奖）", "已设置抽奖奖励"),
            ("中奖人数 1", "已设置中奖人数"),
            ("抽奖倒计时 1", "已设置抽奖倒计时"),
            ("参与上限 1", "已设置参与上限"),
            ("抽奖邀请门槛 0", "已设置抽奖邀请门槛"),
            ("领奖联系人 测试管理员（不自动付款）", "已设置领奖联系人"),
            ("抽奖设置", "测试奖品"),
        ):
            await self.send("private", command, expected)
        self.remaining = 0
        await self.send("private", "开启抽奖", "抽奖已开启")
        lottery = read_rows(
            "SELECT * FROM lotteries WHERE account=? AND group_id=?",
            (BOT, GROUP),
        )[-1]
        self.test_lottery_ids.append(lottery["id"])
        await self.proactive_texts("lottery-start/" + BOT + "/" + lottery["id"])
        await self.send("group", "抽奖状态", "报名中")
        await self.send("group", "参加抽奖", "机器人账号不参与抽奖")
        self.remaining = 0
        await self.send("private", "取消抽奖", "已取消抽奖")
        await self.proactive_texts("lottery-cancel/" + BOT + "/" + lottery["id"])
        await self.send("private", "设置邀请奖励 5.25", "5.25")
        await self.send("private", "邀请奖励状态", "未开启")
        await self.send("private", "开启邀请奖励", "5.25")
        rule = read_rows(
            "SELECT * FROM invite_rules WHERE account=? AND group_id=?",
            (BOT, GROUP),
        )[0]
        if rule["enabled"] != 1 or rule["rate"] != 525:
            raise RuntimeError("Invitation reward settings were not persisted")
        self.test_activation = rule["activation"]
        self.baseline_members = [
            row["member"]
            for row in read_rows(
                "SELECT member FROM invite_seen WHERE account=? AND group_id=? "
                "AND status='baseline' AND first_seen>=?",
                (BOT, GROUP, self.test_started),
            )
        ]
        await self.send("private", "邀请奖励状态", "开启")
        await self.send("private", "暂停邀请奖励", "暂停")
        print(
            "INVITE_BASELINE:",
            read_rows(
                "SELECT COUNT(*) AS n FROM invite_seen WHERE account=? AND group_id=?",
                (BOT, GROUP),
            )[0]["n"],
            flush=True,
        )
        await self.send("private", "邀请记录", "暂无记录")
        await self.send("private", "我的邀请", "5.25")
        print("ACTIVITY_COMMANDS_VERIFIED", flush=True)

    async def restore(self):
        """Remove only this test's activity rows and restore bot transport fields."""
        if self.window:
            await self.window_close()
        for identifier in self.test_lottery_ids:
            update_activity(
                "DELETE FROM lottery_entries WHERE lottery=?",
                (identifier,),
            )
            update_activity("DELETE FROM lotteries WHERE id=?", (identifier,))
        if self.test_activation:
            update_activity(
                "DELETE FROM invite_credits WHERE account=? AND group_id=? "
                "AND member IN (SELECT member FROM invite_seen WHERE account=? "
                "AND group_id=? AND first_seen>=?)",
                (BOT, GROUP, BOT, GROUP, self.test_started),
            )
            for member in self.baseline_members:
                update_activity(
                    "DELETE FROM invite_seen WHERE account=? AND group_id=? AND member=?",
                    (BOT, GROUP, member),
                )
            update_activity(
                "DELETE FROM invite_rules WHERE account=? AND group_id=? AND activation=?",
                (BOT, GROUP, self.test_activation),
            )
        if self.original_settings is None:
            update_activity(
                "DELETE FROM lottery_settings WHERE account=? AND group_id=?",
                (BOT, GROUP),
            )
        else:
            columns = list(self.original_settings)
            placeholders = ",".join("?" for _ in columns)
            update_activity(
                "INSERT OR REPLACE INTO lottery_settings("
                + ",".join(columns)
                + ") VALUES("
                + placeholders
                + ")",
                tuple(self.original_settings[column] for column in columns),
            )
        if hasattr(self, "original_sender"):
            for instance, original, fields in (
                (
                    SENDER,
                    self.original_sender,
                    ("enabled_groups", "proactive_send"),
                ),
                (RECEIVER, self.original_receiver, ("proactive_send",)),
            ):
                current = (
                    await self.api("GET", "/api/v1/bots/by-id?bot_id=" + instance)
                )["bot"]
                for field in fields:
                    if field in original:
                        current[field] = copy.deepcopy(original[field])
                    else:
                        current.pop(field, None)
                await self.api(
                    "PUT",
                    "/api/v1/bots/by-id",
                    {"bot_id": instance, "config": current},
                )
            await self.online()
        print(
            "CLEANUP:",
            {
                "lotteries": read_rows(
                    "SELECT COUNT(*) AS n FROM lotteries WHERE account=? AND group_id=?",
                    (BOT, GROUP),
                )[0]["n"],
                "invite_rules": read_rows(
                    "SELECT COUNT(*) AS n FROM invite_rules WHERE account=? AND group_id=?",
                    (BOT, GROUP),
                )[0]["n"],
                "invite_credits": read_rows(
                    "SELECT COUNT(*) AS n FROM invite_credits WHERE account=? AND group_id=?",
                    (BOT, GROUP),
                )[0]["n"],
                "baseline_members": len(self.baseline_members),
            },
            flush=True,
        )


async def main():
    """Run the test and always remove activity test artifacts."""
    config = json.loads(Path("data/cmd_config.json").read_text(encoding="utf-8-sig"))
    token = jwt.encode(
        {
            "username": config["dashboard"]["username"],
            "exp": int(time.time()) + 3600,
        },
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=60,
    ) as client:
        test = LiveActivityTest(client)
        try:
            await test.run()
        finally:
            await test.restore()


if __name__ == "__main__":
    asyncio.run(main())
