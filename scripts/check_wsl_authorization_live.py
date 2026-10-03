"""Exercise an explicit live WSL administrator/member matrix with restoration."""

import asyncio
import copy
import json
import re
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
TARGET = "23691273"
PROFILE = "cba0c0a2-f0f6-4edb-b7e1-1d656fc9a7f1"


def rows(instance, filename, statement, values=()):
    """Read durable evidence without changing the running bot's ledgers.

    Args:
        instance: Native instance identifier.
        filename: Local ledger filename.
        statement: Read-only SQL statement.
        values: Bound query values.

    Returns:
        List of row dictionaries, or an empty list for an absent ledger.
    """
    path = instance_dir(instance) / filename
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(statement, values)]


class Matrix:
    def __init__(self, client):
        self.client = client
        self.original_bots = {}
        self.original_profile = None
        self.window = False
        self.seed_window = False
        self.remaining = 0
        self.mute_pending = False
        self.preview_token = ""
        self.recall_keys = []
        self.results = []
        self.phase = ""
        self.source_touched = False
        self.profile_touched = False
        own_peer = Vault(RECEIVER).load()["nim_id"]
        self.private = f"private/{BOT}/{b64(str(own_peer).encode())}"

    async def api(self, method, path, data=None):
        """Call the authenticated dashboard without printing credentials.

        Args:
            method: HTTP method.
            path: Local dashboard API path.
            data: Optional structured body.

        Returns:
            Successful API envelope data.

        Raises:
            RuntimeError: If the server rejects the request.
        """
        response = await self.client.request(method, path, json=data)
        value = response.json()
        if response.is_error:
            raise RuntimeError(
                f"API rejected ({response.status_code}): {path}: "
                f"{value.get('message', 'unspecified error')}"
            )
        if value.get("status") != "ok":
            raise RuntimeError(f"API rejected: {path}")
        return value.get("data")

    async def bot(self, instance):
        return (await self.api("GET", "/api/v1/bots/by-id?bot_id=" + instance))["bot"]

    async def online(self):
        for _ in range(35):
            data = await self.api(
                "GET",
                "/api/v1/plugins/extensions/wangshangliao_moderation/settings",
            )
            if all(
                any(
                    item["id"] == instance and item["state"] == "online"
                    for item in data["bots"]
                )
                for instance in (SENDER, RECEIVER)
            ):
                return
            await asyncio.sleep(2)
        raise RuntimeError("Native test accounts are not online")

    async def new_window(self):
        if self.window:
            await self.api(
                "POST",
                "/api/v1/bot-types/wangshangliao/registration",
                {"action": "test_close", "instance_id": RECEIVER},
            )
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

    def operations(self):
        return rows(RECEIVER, "moderation.sqlite3", "SELECT id,result FROM operations")

    async def send(self, scope, text, expected, *, mention=None, mutation=None):
        """Send once and verify inbound, reply delivery, action and recall evidence.

        Args:
            scope: Private or group command context.
            text: Exact command text.
            expected: Required response substring, exact permission denial, or None for silence.
            mention: Optional native target business UID.
            mutation: Optional allowed action and minute count.

        Returns:
            Verified plain reply text.
        """
        if not self.remaining:
            await self.new_window()
        self.remaining -= 1
        baseline = rows(
            RECEIVER,
            "messages.sqlite3",
            "SELECT COALESCE(MAX(rowid),0) AS n FROM inbox",
        )[0]["n"]
        before = {row["id"] for row in self.operations()}
        parts = [{"type": "plain", "text": text}]
        if mention:
            action, _, suffix = text.partition(" ")
            parts = [
                {"type": "plain", "text": action + " "},
                {"type": "at", "qq": mention, "name": "testfork"},
            ]
            if suffix:
                parts.append({"type": "plain", "text": " " + suffix})
        incoming_target = GROUP if scope == "group" else self.private
        session_kind = "GroupMessage" if scope == "group" else "FriendMessage"
        operation = "wsl-auth-matrix-" + uuid.uuid4().hex
        # Never retry a send whose outcome is unknown.
        await self.api(
            "POST",
            "/api/v1/im/messages",
            {
                "umo": f"{SENDER}:{session_kind}:{USER}/{incoming_target}",
                "message": parts,
                "operation_id": operation,
            },
        )
        incoming = None
        outgoing = []
        for _ in range(30):
            candidates = rows(
                RECEIVER,
                "messages.sqlite3",
                "SELECT rowid,* FROM inbox WHERE rowid>? AND account=? ORDER BY rowid",
                (baseline, BOT),
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
                outgoing = rows(
                    RECEIVER,
                    "messages.sqlite3",
                    "SELECT key,state,server_id FROM outbox WHERE key LIKE ? ORDER BY rowid",
                    (prefix + "/part/%",),
                )
                if incoming["state"] == "processed" and (
                    expected is None
                    or outgoing
                    and all(row["state"] == "accepted" for row in outgoing)
                ):
                    break
            await asyncio.sleep(0.5)
        if expected is None:
            if (
                not incoming
                or incoming["state"] != "processed"
                or outgoing
                or any(row["id"] not in before for row in self.operations())
            ):
                raise RuntimeError(
                    "Silent group command replied, executed, or did not finish"
                )
            print(
                json.dumps(
                    {
                        "phase": self.phase,
                        "scope": scope,
                        "command": text,
                        "reply": "silent",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return ""
        if not incoming or not outgoing:
            print(
                json.dumps(
                    {
                        "phase": self.phase,
                        "scope": scope,
                        "command": text,
                        "inbox": incoming["state"] if incoming else "missing",
                        "reply": "missing",
                        "new_actions": len(self.operations()) - len(before),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            raise RuntimeError("Live command did not produce an accepted reply")
        replies = []
        for row in outgoing:
            delivered = []
            for _ in range(20):
                delivered = rows(
                    SENDER,
                    "messages.sqlite3",
                    "SELECT payload,state FROM inbox WHERE account=? AND message=?",
                    (USER, row["server_id"]),
                )
                if delivered:
                    break
                await asyncio.sleep(0.5)
            if not delivered:
                raise RuntimeError("Peer did not receive the command result")
            replies.append(json.loads(delivered[0]["payload"]).get("text", ""))
            recalls = rows(
                RECEIVER,
                "messages.sqlite3",
                "SELECT * FROM reply_recalls WHERE key=?",
                (row["key"],),
            )
            if scope == "group":
                if not recalls:
                    raise RuntimeError("Group feature response lacks self-recall")
                self.recall_keys.append(row["key"])
            elif recalls:
                raise RuntimeError("Private reply was incorrectly scheduled for recall")
        reply = "\n".join(replies)
        if expected == "无权限":
            if reply.removeprefix(f"@{USER} ").strip() != "无权限":
                raise RuntimeError(
                    "Unauthorized command disclosed a non-denial response"
                )
        elif expected not in reply:
            print("Unexpected reply:", reply, flush=True)
            raise RuntimeError(f"Missing expected reply for {scope}: {text}")
        after = [
            json.loads(row["result"])
            for row in self.operations()
            if row["id"] not in before
        ]
        if mutation is None and after:
            raise RuntimeError("Read/denied command submitted a moderation operation")
        if mutation is not None:
            action, minutes = mutation
            if len(after) != 1 or not all(
                r["action"] == action
                and str(r["member"]) == TARGET
                and str(r["group"]) == GROUP
                and r["status"] == "accepted"
                and (minutes is None or r.get("minutes") == minutes)
                for r in after
            ):
                raise RuntimeError("Mutation evidence differs from the authorized test")
            self.mute_pending = action == "mute"
        result = {
            "phase": self.phase,
            "scope": scope,
            "command": text,
            "inbox": incoming["state"],
            "reply": "accepted_and_peer_received",
            "expected": expected,
            "actions": len(after),
            "recall": "scheduled_20s" if scope == "group" else "not_scheduled",
        }
        self.results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return reply

    async def role(self, enabled):
        """Temporarily change only the verified user's current profile role.

        Args:
            enabled: Whether the test user should be an administrator.
        """
        path = "/api/v1/config-profiles/" + PROFILE
        current = (await self.api("GET", path))["config"]
        admins = [uid for uid in current.get("admins_id", []) if str(uid) != USER]
        if enabled:
            admins.append(USER)
        current["admins_id"] = admins
        self.profile_touched = True
        await self.api("PUT", path, current)
        actual = (await self.api("GET", path))["config"]["admins_id"]
        if (USER in actual) != enabled:
            raise RuntimeError("Effective role did not change")

    async def run(self):
        """Run real positive and negative command cases in the authorized group."""
        await self.online()
        status = await self.api(
            "POST",
            "/api/v1/bot-types/wangshangliao/registration",
            {"action": "test_status", "instance_id": RECEIVER},
        )
        if status["window"].get("active"):
            raise RuntimeError("An existing developer window is active")
        self.original_bots = {
            instance: copy.deepcopy(await self.bot(instance))
            for instance in (SENDER, RECEIVER)
        }
        self.original_profile = copy.deepcopy(
            (await self.api("GET", "/api/v1/config-profiles/" + PROFILE))["config"]
        )
        if USER not in self.original_profile.get("admins_id", []):
            raise RuntimeError("Preflight expected an already authorized user")
        if str(self.original_bots[SENDER]["account_id"]) != USER:
            raise RuntimeError("Sender account changed")
        cfg = copy.deepcopy(self.original_bots[SENDER])
        cfg["enabled_groups"] = list(
            dict.fromkeys(cfg.get("enabled_groups", []) + [GROUP])
        )
        cfg.setdefault("reply_groups", {})[GROUP] = False
        cfg["proactive_send"] = {
            "enabled": True,
            "targets": list(
                dict.fromkeys(
                    cfg.get("proactive_send", {}).get("targets", [])
                    + [GROUP, self.private]
                )
            ),
        }
        self.source_touched = True
        await self.api("PUT", "/api/v1/bots/by-id", {"bot_id": SENDER, "config": cfg})
        await self.online()
        if not rows(
            SENDER,
            "messages.sqlite3",
            "SELECT 1 FROM inbox WHERE account=? AND team=? LIMIT 1",
            (USER, self.private),
        ):
            status = await self.api(
                "POST",
                "/api/v1/bot-types/wangshangliao/registration",
                {"action": "test_status", "instance_id": SENDER},
            )
            if status["window"].get("active"):
                raise RuntimeError("Sender already has an active developer window")
            await self.api(
                "POST",
                "/api/v1/bot-types/wangshangliao/registration",
                {
                    "action": "test_open",
                    "instance_id": SENDER,
                    "sender_instance": RECEIVER,
                    "groups": [],
                    "scopes": ["private_commands"],
                    "keywords": [],
                    "seconds": 60,
                    "budget": 1,
                },
            )
            self.seed_window = True
            peer = Vault(SENDER).load()["nim_id"]
            target = f"private/{USER}/{b64(str(peer).encode())}"
            receipt = await self.api(
                "POST",
                "/api/v1/im/messages",
                {
                    "umo": f"{RECEIVER}:FriendMessage:{BOT}/{target}",
                    "message": [{"type": "plain", "text": "sid"}],
                    "operation_id": "wsl-auth-seed-" + uuid.uuid4().hex,
                },
            )
            if receipt.get("status") != "accepted":
                raise RuntimeError("Native private seed was not accepted")
            for _ in range(30):
                if rows(
                    SENDER,
                    "messages.sqlite3",
                    "SELECT 1 FROM inbox WHERE account=? AND team=? LIMIT 1",
                    (USER, self.private),
                ):
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("Native private seed was not received")
            await self.api(
                "POST",
                "/api/v1/bot-types/wangshangliao/registration",
                {"action": "test_close", "instance_id": SENDER},
            )
            self.seed_window = False
        await self.new_window()
        self.phase = "administrator"
        private_queries = [
            ("我的权限", "AstrBot 管理员: 是"),
            ("sid", USER),
            ("群列表", GROUP),
            ("选择群 1", GROUP),
            ("能力", "当前群动作授权"),
            ("规则", "业务内容规则"),
            ("违规计数", "业务规则"),
            ("排名", "今日发言排行"),
            ("抽奖状态", "尚未开启抽奖"),
            ("抽奖设置", "中奖"),
            ("中奖名单", "尚未开启抽奖"),
            ("抽奖记录", "暂无"),
            ("邀请奖励状态", "邀请奖励"),
            ("邀请记录", "暂无"),
            ("定时状态", "定时"),
            ("中奖人数 0", "数值范围"),
            ("设置邀请奖励 -1", "数值"),
            ("开启抽奖", "主动发送授权"),
            ("公告 WSL_TEST_AUTH_NO_GRANT", "拒绝"),
            ("全员禁言", "拒绝"),
            ("解除全员禁言", "拒绝"),
            ("成员搜索 testfork", TARGET),
        ]
        for text, expected in private_queries:
            reply = await self.send("private", text, expected)
            if text == "成员搜索 testfork":
                match = re.search(r"(?m)^(\d+)\.\s+.*testfork", reply)
                if not match:
                    raise RuntimeError("Target snapshot number was not verified")
                target_number = match[1]
        for text, expected in [
            (f"禁言 {target_number} 0", "禁言时长"),
            (f"踢出 {target_number}", "尚未执行"),
        ]:
            reply = await self.send("private", text, expected)
            if text.startswith("踢出"):
                self.preview_token = re.search(r"确认踢出 ([a-f0-9]{24})", reply)[1]
        self.mute_pending = True
        await self.send(
            "private", f"禁言 {target_number} 1", "禁言已受理", mutation=("mute", 1)
        )
        await self.send(
            "private", f"解禁 {target_number}", "解禁已受理", mutation=("unmute", None)
        )
        for text, expected in [
            ("排名", "今日发言排行"),
            ("排名 2", "第 2 页"),
            ("排行", "今日发言排行"),
            ("抽奖状态", "尚未开启抽奖"),
            ("参加抽奖", "尚未开启抽奖"),
            ("我的邀请", "有效邀请"),
            ("邀请奖励", "累计奖励"),
            ("开启抽奖", None),
            ("设置邀请奖励 5", None),
            ("踢出 @testfork", None),
            ("公告 WSL_TEST_AUTH_NO_GRANT", "拒绝"),
            ("全员禁言", "拒绝"),
            ("解除全员禁言", "拒绝"),
            ("禁言 @testfork 1", "真实 @"),
        ]:
            await self.send("group", text, expected)
        await self.send("group", "禁言 0", "禁言时长", mention=TARGET)
        self.mute_pending = True
        await self.send(
            "group", "禁言", "禁言已受理", mention=TARGET, mutation=("mute", 30)
        )
        await self.send(
            "group", "解禁", "解禁已受理", mention=TARGET, mutation=("unmute", None)
        )
        self.mute_pending = True
        await self.send(
            "group", "禁言 3", "禁言已受理", mention=TARGET, mutation=("mute", 3)
        )
        await self.send(
            "group", "解禁", "解禁已受理", mention=TARGET, mutation=("unmute", None)
        )

        await self.role(False)
        self.phase = "member"
        for text in [
            "我的权限",
            "sid",
            "帮助",
            "/群管 帮助",
            "群列表",
            "选择群 1",
            "成员列表",
            "能力",
            "规则",
            "违规计数",
            "排名",
            "抽奖状态",
            "抽奖设置",
            "参加抽奖",
            "我的邀请",
            "邀请奖励状态",
            "邀请记录",
            "设置邀请奖励 5",
            "开启邀请奖励",
            "暂停邀请奖励",
            "抽奖奖励 WSL_TEST_NO_PERMISSION",
            "中奖人数 1",
            "开启抽奖",
            "立即开奖",
            "取消抽奖",
            "定时禁言 23:00 08:00",
            f"禁言 {target_number} 1",
            f"解禁 {target_number}",
            f"确认踢出 {self.preview_token}",
        ]:
            await self.send("private", text, "无权限")
        for text, expected in [
            ("排名", "今日发言排行"),
            ("排名 2", "第 2 页"),
            ("今日排行", "今日发言排行"),
            ("抽奖状态", "尚未开启抽奖"),
            ("参加抽奖", "尚未开启抽奖"),
            ("我的邀请", "有效邀请"),
            ("邀请奖励", "累计奖励"),
            ("禁言 @testfork 1", "无权限"),
            ("解禁 @testfork", "无权限"),
            ("公告 WSL_TEST_NO_PERMISSION", "无权限"),
            ("全员禁言", "无权限"),
            ("解除全员禁言", "无权限"),
            ("开启抽奖", None),
            ("设置邀请奖励 5", None),
        ]:
            await self.send("group", text, expected)
        await self.send("group", "禁言 1", "无权限", mention=TARGET)
        await self.send("group", "解禁", "无权限", mention=TARGET)
        await self.role(True)
        self.phase = "restored"
        await self.send("private", "我的权限", "AstrBot 管理员: 是")
        await self.send("private", "能力", "当前群动作授权")
        await self.send("group", "排名", "今日发言排行")
        for _ in range(60):
            state = [
                rows(
                    RECEIVER,
                    "messages.sqlite3",
                    "SELECT state FROM reply_recalls WHERE key=?",
                    (key,),
                )[0]["state"]
                for key in self.recall_keys
            ]
            if all(value == "accepted" for value in state):
                break
            await asyncio.sleep(1)
        if not all(value == "accepted" for value in state):
            raise RuntimeError("Some group feature recall operations were not accepted")
        print("ALL GROUP RECALLS ACCEPTED:", len(state), flush=True)

    async def restore(self):
        """Restore changed permissions while preserving unrelated concurrent edits."""
        if self.profile_touched and self.original_profile is not None:
            path = "/api/v1/config-profiles/" + PROFILE
            cfg = (await self.api("GET", path))["config"]
            cfg["admins_id"] = copy.deepcopy(self.original_profile["admins_id"])
            await self.api("PUT", path, cfg)
        if self.mute_pending:
            print("Restoring the test member with a fresh unmute command", flush=True)
            self.phase = "cleanup"
            await self.new_window()
            await self.send("private", "群列表", GROUP)
            await self.send("private", "选择群 1", GROUP)
            reply = await self.send("private", "成员搜索 testfork", TARGET)
            number = re.search(r"(?m)^(\d+)\.\s+.*testfork", reply)[1]
            await self.send(
                "private",
                f"解禁 {number}",
                "解禁已受理",
                mutation=("unmute", None),
            )
        if self.preview_token:
            path = instance_dir(RECEIVER) / "moderation.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "UPDATE kick_confirmations SET used=1 WHERE token=? AND owner=?",
                    (self.preview_token, USER),
                )
        if self.window:
            await self.api(
                "POST",
                "/api/v1/bot-types/wangshangliao/registration",
                {"action": "test_close", "instance_id": RECEIVER},
            )
        if self.seed_window:
            await self.api(
                "POST",
                "/api/v1/bot-types/wangshangliao/registration",
                {"action": "test_close", "instance_id": SENDER},
            )
        if self.source_touched:
            current = await self.bot(SENDER)
            for field in ("enabled_groups", "reply_groups", "proactive_send"):
                if field in self.original_bots[SENDER]:
                    current[field] = copy.deepcopy(self.original_bots[SENDER][field])
                else:
                    current.pop(field, None)
            await self.api(
                "PUT", "/api/v1/bots/by-id", {"bot_id": SENDER, "config": current}
            )
            await self.online()
        equal = {
            instance: (await self.bot(instance)) == original
            for instance, original in self.original_bots.items()
        }
        if self.original_profile is not None:
            profile = (await self.api("GET", "/api/v1/config-profiles/" + PROFILE))[
                "config"
            ]
            equal["profile"] = profile == self.original_profile
        print("RESTORATION:", equal, flush=True)
        if not all(equal.values()):
            raise RuntimeError("Configuration restoration differs from the original")


async def main():
    """Run only the explicitly named user's authorized live test matrix."""
    cfg = json.loads(Path("data/cmd_config.json").read_text(encoding="utf-8-sig"))
    token = jwt.encode(
        {"username": cfg["dashboard"]["username"], "exp": int(time.time()) + 3600},
        cfg["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=60,
    ) as client:
        matrix = Matrix(client)
        try:
            await matrix.run()
        finally:
            await matrix.restore()
        print("VERIFIED CASES:", len(matrix.results), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
