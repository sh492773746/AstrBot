"""Verify numbered menus over native private messages without executing actions."""

import argparse
import asyncio
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao.storage import Vault
from astrbot.core.platform.sources.wangshangliao.wire import b64
from scripts.check_wsl_account_names import PREFIX, ROOT, snapshot
from scripts.check_wsl_authorization_live import rows

SENDER = "wangshangliao_0acde7ec"
RECEIVER = "wangshangliao_f79c5b81"
USER = "31846829"
BOT = "43506527"
REGISTRATION = "/api/v1/bot-types/wangshangliao/registration"


class MenuCheck:
    """Record bounded native sends and restore their single-recipient send grant."""

    def __init__(self, client, output, *, native_admin_gate=False, billing=False):
        self.client = client
        self.output = output
        self.results = []
        self.remaining = 0
        self.window = False
        self.changed = False
        self.original = None
        self.proposed = None
        self.native_admin_gate = native_admin_gate
        self.billing = billing
        peer = str(Vault(RECEIVER).load()["nim_id"])
        self.target = f"private/{BOT}/{b64(peer.encode())}"

    async def api(self, method, path, body=None):
        """Call the local dashboard without disclosing authentication material.

        Args:
            method: HTTP verb.
            path: Authenticated local API path.
            body: Optional structured request.

        Returns:
            Successful response data.

        Raises:
            RuntimeError: If the API rejects the request.
        """
        response = await self.client.request(method, path, json=body)
        response.raise_for_status()
        value = response.json()
        if value.get("status") != "ok":
            raise RuntimeError(f"Dashboard rejected {path}: {value.get('message')}")
        return value.get("data")

    async def settings(self):
        """Read the sending account's effective policy and optimistic revision.

        Returns:
            Current sending account settings.
        """
        data = await self.api("GET", PREFIX + "settings")
        return next(bot for bot in data["bots"] if bot["id"] == SENDER)

    async def send(self, text, expected):
        """Send one menu input and verify its reply on the receiving account.

        Args:
            text: Exact fixed menu command or numeric selection.
            expected: Required reply substring, or accepted scope-denial alternatives.

        Raises:
            AssertionError: If delivery or the numbered screen is incorrect.
        """
        if not self.remaining:
            if self.window:
                await self.api(
                    "POST",
                    REGISTRATION,
                    {"action": "test_close", "instance_id": RECEIVER},
                )
                self.window = False
            await self.api(
                "POST",
                REGISTRATION,
                {
                    "action": "test_open",
                    "instance_id": RECEIVER,
                    "sender_instance": SENDER,
                    "groups": [],
                    "scopes": ["private_commands"],
                    "keywords": [],
                    "seconds": 300,
                    "budget": 10,
                },
            )
            self.window = True
            self.remaining = 10
        self.remaining -= 1
        baseline = rows(
            RECEIVER,
            "messages.sqlite3",
            "SELECT COALESCE(MAX(rowid),0) AS n FROM inbox",
        )[0]["n"]
        started = time.monotonic()
        receipt = await self.api(
            "POST",
            "/api/v1/im/messages",
            {
                "umo": f"{SENDER}:FriendMessage:{USER}/{self.target}",
                "message": [{"type": "plain", "text": text}],
                "operation_id": "wsl-tree-read-" + uuid.uuid4().hex,
            },
        )
        assert receipt.get("status") == "accepted", receipt
        incoming, outgoing, delivered = None, [], []
        for _ in range(80):
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
                    if row["team"].startswith(f"private/{USER}/")
                    and str(json.loads(row["payload"]).get("sender")) == USER
                    and json.loads(row["payload"]).get("text") == text
                ),
                None,
            )
            if incoming:
                prefix = f"{BOT}/{incoming['team']}/{incoming['message']}/0"
                outgoing = rows(
                    RECEIVER,
                    "messages.sqlite3",
                    "SELECT key,state,server_id FROM outbox WHERE key LIKE ? "
                    "ORDER BY rowid",
                    (prefix + "/part/%",),
                )
                if outgoing and all(row["state"] == "accepted" for row in outgoing):
                    delivered = [
                        rows(
                            SENDER,
                            "messages.sqlite3",
                            "SELECT payload FROM inbox WHERE account=? AND message=?",
                            (USER, row["server_id"]),
                        )
                        for row in outgoing
                    ]
                    if incoming["state"] == "processed" and all(delivered):
                        break
            await asyncio.sleep(0.25)
        reply = "\n".join(
            json.loads(part[0]["payload"]).get("text", "") for part in delivered if part
        )
        result = {
            "input": text,
            "expected": expected,
            "incoming": incoming["message"] if incoming else "",
            "state": incoming["state"] if incoming else "",
            "receipt": receipt,
            "outgoing": outgoing,
            "reply": reply,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "passed": bool(incoming and incoming["state"] == "processed")
            and bool(outgoing)
            and bool(delivered)
            and all(delivered)
            and any(
                part in reply
                for part in ((expected,) if isinstance(expected, str) else expected)
            ),
        }
        if "即将执行：" in reply:
            result["passed"] = (
                result["passed"]
                and "0. 放弃并返回" in reply
                and "2. 放弃并返回" not in reply
            )
        if self.billing:
            result["passed"] = result["passed"] and reply.count("【AI套餐说明】") <= 1
        self.results.append(result)
        self.output.joinpath("messages.json").write_text(
            json.dumps(self.results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.output.joinpath("messages.json").chmod(0o600)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        assert result["passed"], "Native menu did not return the expected screen"
        await asyncio.sleep(3.2)

    async def run(self):
        """Exercise navigation, queries and abandoned previews without confirmation.

        Raises:
            AssertionError: If the account, window or protected scope is incorrect.
        """
        state = await self.api(
            "POST", PREFIX + "open-service", {"action": "status", "payload": {}}
        )
        identities = {bot["instance_id"]: bot for bot in state["accounts"]}
        assert identities[SENDER]["account"] == USER
        assert identities[RECEIVER]["account"] == BOT
        assert all(
            identities[instance]["state"] == "online" for instance in (SENDER, RECEIVER)
        )
        if self.native_admin_gate:
            tenant = next(
                group
                for group in state["groups"]
                if group["account"] == BOT and group["group_id"] == "1459282"
            )
            assert tenant["active"] and not tenant["paused"]
            assert tenant["managers"] == [USER]
            assert tenant["native_role"] == "GROUP_ROLE_MEMBER"
        assert all(
            tenant["paused"] or not tenant["active"]
            for tenant in state["groups"]
            if tenant["group_id"] in {"1516387", "1488006"}
        )
        for instance in (SENDER, RECEIVER):
            status = await self.api(
                "POST", REGISTRATION, {"action": "test_status", "instance_id": instance}
            )
            assert not status["window"]["active"], "Existing test window"
        assert rows(
            SENDER,
            "messages.sqlite3",
            "SELECT 1 FROM inbox WHERE account=? AND team=? LIMIT 1",
            (USER, self.target),
        ), "A real existing private peer is required"
        current = await self.settings()
        self.original = copy.deepcopy(current["proactive_send"])
        self.proposed = copy.deepcopy(self.original)
        self.proposed["enabled"] = True
        self.proposed["targets"] = list(
            dict.fromkeys(self.proposed.get("targets", []) + [self.target])
        )
        self.changed = True
        await self.api(
            "POST",
            PREFIX + "settings",
            {
                "bot_id": SENDER,
                "revision": current["revision"],
                "proactive_send": self.proposed,
            },
        )
        if self.billing:
            assert not state["ai_budget"]["confirmed"]
            for text, expected in (
                ("取消", "已取消"),
                ("我的套餐", "当前套餐：试用"),
                ("我的额度", "当前套餐：试用"),
                ("套餐说明", "次数为上限，同时受成本预算保护"),
                ("菜单", "└─ 9. 群聊与热群AI"),
                ("8", "私聊AI咨询"),
                ("3", "当前套餐：试用"),
                ("4", "Plus：20 USDT"),
                ("0", "└─ 9. 群聊与热群AI"),
                ("9", "确认本群使用我的共享额度"),
                ("取消", "已取消"),
            ):
                await self.send(text, expected)
            after = await self.api(
                "POST", PREFIX + "open-service", {"action": "status", "payload": {}}
            )
            assert not after["ai_budget"]["confirmed"]
            assert after["usage"] == state["usage"], "Menu navigation charged AI usage"
            for field in ("settings", "subscriptions", "prices"):
                assert after["billing"][field] == state["billing"][field], (
                    "Menu navigation changed billing " + field
                )
            return
        if self.native_admin_gate:
            for text, expected in (
                ("群列表", "1. 测试群"),
                ("选择群 2", "编号无效"),
                ("选择群 1", "当前机器人不是本群群管理员"),
                ("规则", "当前机器人不是本群群管理员"),
                ("能力", "当前机器人不是本群群管理员"),
                ("成员列表", "当前机器人不是本群群管理员"),
                ("定时状态", "当前机器人不是本群群管理员"),
                ("菜单", "└─ 9. 群聊与热群AI"),
                ("5", "命令菜单 > 公告与定时禁言"),
                ("1", "当前机器人不是本群群管理员"),
                ("0", "└─ 9. 群聊与热群AI"),
                ("4", "命令菜单 > 成员与管理员操作"),
                ("4", "当前机器人不是本群群管理员"),
                ("0", "└─ 9. 群聊与热群AI"),
                ("2", "命令菜单 > 群与成员"),
                ("1", "1. 测试群"),
                ("1", "当前机器人不是本群群管理员"),
                ("取消", "已取消并退出菜单"),
            ):
                await self.send(text, expected)
                if text == "群列表":
                    assert (
                        "\n".join(
                            line
                            for line in self.results[-1]["reply"].splitlines()
                            if line
                        )
                        == "已启用群（编号十分钟有效）：\n1. 测试群"
                    )
            return
        # Numeric confirmations are previewed and abandoned, never submitted.
        for text, expected in (
            ("菜单", "└─ 8. AI咨询"),
            ("999", "请按本页的数字选择"),
            ("1", "命令菜单 > 开通与身份"),
            ("3", "无权限"),
            ("0", "└─ 8. AI咨询"),
            ("2", "命令菜单 > 群与成员"),
            ("1", "无权限"),
            ("0", "└─ 8. AI咨询"),
            ("3", "命令菜单 > 审核规则"),
            ("1", ("无权限", "私聊请先发送「群列表」")),
            ("2", "命令菜单 > 审核规则 > 广告审核"),
            ("2", "即将执行：关闭广告审核"),
            ("0", "命令菜单 > 审核规则 > 广告审核"),
            ("0", "命令菜单 > 审核规则"),
            ("4", "请输入一个关键词"),
            ("0", "命令菜单 > 审核规则"),
            ("取消", "已取消并退出菜单"),
            ("1", "菜单已退出"),
            ("菜单", "└─ 8. AI咨询"),
            ("4", "命令菜单 > 成员与管理员操作"),
            ("4", ("无权限", "私聊请先发送「群列表」")),
            ("0", "└─ 8. AI咨询"),
            ("5", "命令菜单 > 公告与定时禁言"),
            ("5", ("无权限", "私聊请先发送「群列表」")),
            ("0", "└─ 8. AI咨询"),
            ("6", "命令菜单 > 抽奖活动"),
            ("2", ("无权限", "私聊请先发送「群列表」")),
            ("0", "└─ 8. AI咨询"),
            ("7", "命令菜单 > 邀请查询与活动奖励"),
            ("1", "0 返回"),
            ("0", "命令菜单 > 邀请查询与活动奖励"),
            ("0", "└─ 8. AI咨询"),
            ("8", "命令菜单 > AI咨询"),
            ("2", "已结束AI咨询"),
            ("0", "菜单已退出"),
            ("帮助", "└─ 8. AI咨询"),
            ("8", "命令菜单 > AI咨询"),
            ("1", "已进入AI咨询"),
            ("/exit", "已结束AI咨询"),
            ("菜单", "└─ 8. AI咨询"),
            ("0", "└─ 8. AI咨询"),
            ("取消", "已取消并退出菜单"),
        ):
            await self.send(text, expected)

    async def restore(self):
        """Close the test window and restore only the temporary private send grant.

        Raises:
            AssertionError: If another writer changed the grant during this check.
        """
        if self.window:
            await self.api(
                "POST", REGISTRATION, {"action": "test_close", "instance_id": RECEIVER}
            )
            self.window = False
        if self.changed:
            current = await self.settings()
            assert current["proactive_send"] == self.proposed, "Send grant changed"
            await self.api(
                "POST",
                PREFIX + "settings",
                {
                    "bot_id": SENDER,
                    "revision": current["revision"],
                    "proactive_send": self.original,
                },
            )
            assert (await self.settings())["proactive_send"] == self.original
            self.changed = False


async def main():
    """Run the native check and fingerprint unchanged financial and action state.

    Raises:
        AssertionError: If menus fail or any protected record changes.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-admin-gate", action="store_true")
    parser.add_argument("--billing", action="store_true")
    args = parser.parse_args()
    output = Path("/opt/backups") / time.strftime(
        "wsl-tree-menu-live-%Y%m%dT%H%M%S", time.gmtime()
    )
    output.mkdir(mode=0o700)
    before = snapshot()
    operations = {
        instance: rows(instance, "moderation.sqlite3", "SELECT * FROM operations")
        for instance in (SENDER, RECEIVER)
    }
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 1200},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": "Bearer " + token},
        timeout=30,
        trust_env=False,
    ) as client:
        test = MenuCheck(
            client,
            output,
            native_admin_gate=args.native_admin_gate,
            billing=args.billing,
        )
        try:
            await test.run()
        finally:
            await test.restore()
            after = snapshot()
            unchanged_actions = all(
                rows(instance, "moderation.sqlite3", "SELECT * FROM operations")
                == baseline
                for instance, baseline in operations.items()
            )
            state = {
                "protected_before": before,
                "protected_after": after,
                "unchanged": before == after,
                "no_actions": unchanged_actions,
                "temporary_grant_restored": not test.changed,
                "test_window_closed": not test.window,
                "passed": sum(bool(result["passed"]) for result in test.results),
                "checks": len(test.results),
                "messages_sha256": hashlib.sha256(
                    json.dumps(test.results, ensure_ascii=False).encode()
                ).hexdigest(),
            }
            output.joinpath("verification.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            output.joinpath("verification.json").chmod(0o600)
            print("Evidence:", output, flush=True)
            assert state["unchanged"] and state["no_actions"], state


if __name__ == "__main__":
    asyncio.run(main())
