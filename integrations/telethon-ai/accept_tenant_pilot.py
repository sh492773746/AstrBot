"""Run a bounded owner pilot and restore its temporary admission grant."""

import asyncio
import contextlib
import json
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from telethon import TelegramClient, errors, functions

ROOT = Path("/opt/astrbot-prod")
REGISTRY = ROOT / "data/plugin_data/astrbot_plugin_telethon_ai/private/accounts.json"
DATABASE = next(
    (ROOT / "data/plugin_data/astrbot_plugin_superbot").glob("*/superbot.sqlite3")
)
GROUP = -1001000000004
OWNER = 1000000003
BOT = "dhcmsuperBOT"
DEFAULT = {"public": False, "pilot_owners": [], "max_groups": 3, "invoices": False}


class Probe:
    """Throttle all acceptance calls without replaying uncertain mutations."""

    def __init__(self, client):
        self.client = client
        self.last = 0.0

    async def call(self, method, *args, **kwargs):
        await asyncio.sleep(max(0, self.last + 2 - time.monotonic()))
        self.last = time.monotonic()
        return await method(*args, **kwargs)

    async def message(self, text):
        sent = await self.call(self.client.send_message, BOT, text, parse_mode=None)
        return await self.read(sent.id)

    async def read(self, after=0):
        for delay in (2, 4, 8):
            await asyncio.sleep(delay)
            messages = await self.call(self.client.get_messages, BOT, limit=5)
            found = [m for m in messages if not m.out and m.id > after]
            if found:
                return found[0]
        raise RuntimeError("No bot reply; mutation will not be replayed")

    async def click(self, message, label):
        button = next(
            b for row in message.buttons or [] for b in row if b.text == label
        )
        await self.call(message.click, data=button.data)
        return await self.read()


async def run():
    """Verify real owner admission, menus and unauthorized callback replay."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output = Path("/root/Projects/agents/telethon-ai-deployment") / f"tenant-{stamp}"
    output.mkdir(mode=0o700)
    report = {"group": GROUP, "owner": OWNER, "steps": [], "complete": False}
    db = sqlite3.connect(DATABASE, isolation_level=None, timeout=5)
    backup = output / "before.sqlite3"
    with sqlite3.connect(backup) as dest:
        db.backup(dest)
    backup.chmod(0o600)
    granted = False
    clients = []
    old = None
    temporary = tempfile.TemporaryDirectory(prefix="tenant-pilot-")

    def record(action, **data):
        report["steps"].append({"action": action, **data})
        print(json.dumps({"action": action, **data}, ensure_ascii=False), flush=True)

    try:
        with contextlib.nullcontext(temporary.name) as tmp:
            registry = json.loads(REGISTRY.read_text())
            probes = {}
            for alias in ("keywords", "collector"):
                spec = registry[alias]
                path = Path(tmp) / f"{alias}.session"
                with (
                    sqlite3.connect(
                        f"file:{spec['session']}.session?mode=ro", uri=True
                    ) as source,
                    sqlite3.connect(path) as dest,
                ):
                    source.backup(dest)
                path.chmod(0o600)
                client = TelegramClient(
                    str(path),
                    spec["api_id"],
                    spec["api_hash"],
                    receive_updates=False,
                    request_retries=0,
                    connection_retries=1,
                    flood_sleep_threshold=0,
                )
                clients.append(client)
                await client.connect()
                probes[alias] = Probe(client)
            owner, member = probes["keywords"], probes["collector"]
            me = await owner.call(owner.client.get_me)
            if me.id != OWNER:
                raise RuntimeError("Unexpected owner session")
            group = await owner.call(owner.client.get_entity, "yunwm1")
            if group.id != 4359554801 or not group.megagroup:
                raise RuntimeError("Unexpected test group")
            permissions = await owner.call(owner.client.get_permissions, group, me)
            bot_permissions = await owner.call(owner.client.get_permissions, group, BOT)
            if not permissions.is_creator or not bot_permissions.is_admin:
                raise RuntimeError("Owner or bot rights missing")
            member_group = await member.call(member.client.get_entity, "yunwm1")
            member_me = await member.call(member.client.get_me)
            rights = await member.call(
                member.client.get_permissions, member_group, member_me
            )
            if rights.is_admin or rights.is_creator:
                raise RuntimeError("Negative-test account is privileged")
            record("identities", owner=me.id, ordinary_member=member_me.id)
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute(
                    "SELECT value FROM settings WHERE key='tenant_policy'"
                ).fetchone()
                policy = {**DEFAULT, **(json.loads(old[0]) if old else {})}
                if policy["public"] or policy["invoices"]:
                    raise RuntimeError("Unexpected public/payment policy")
                if db.execute(
                    "SELECT 1 FROM tenant_groups WHERE chat=?", (str(GROUP),)
                ).fetchone():
                    raise RuntimeError("Test group already bound; no takeover")
                proposed = {
                    **policy,
                    "pilot_owners": list(
                        dict.fromkeys([*policy["pilot_owners"], str(OWNER)])
                    ),
                }
                encoded = json.dumps(proposed, sort_keys=True)
                db.execute(
                    "INSERT INTO settings(key,value,version) VALUES('tenant_policy',?,1) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,version=settings.version+1",
                    (encoded,),
                )
                db.execute(
                    "INSERT INTO audit(at,actor,action,data) VALUES(?,?,?,?)",
                    (
                        time.time(),
                        "1000000001",
                        "tenant_acceptance_grant",
                        json.dumps(
                            {"owner": OWNER, "group": GROUP, "report": str(output)}
                        ),
                    ),
                )
                db.execute("COMMIT")
                granted = True
            except BaseException:
                db.execute("ROLLBACK")
                raise
            record("temporary_pilot_granted", public=False, invoices=False)
            home = await owner.message("我的群")
            challenge = await owner.click(home, "接入群")
            match = re.search(r"/bindgroup\s+([A-Za-z0-9_-]+)", challenge.raw_text)
            if not match:
                raise RuntimeError("Binding challenge not received")
            sent = await owner.call(
                owner.client.send_message,
                group,
                f"/bindgroup {match.group(1)}",
                parse_mode=None,
            )
            await asyncio.sleep(4)
            binding = db.execute(
                "SELECT g.tenant,t.owner,g.status FROM tenant_groups g "
                "JOIN tenants t ON t.id=g.tenant WHERE g.chat=?",
                (str(GROUP),),
            ).fetchone()
            record("real_binding", source_message=sent.id, binding=binding)
            if not binding or binding[1] != str(OWNER):
                raise RuntimeError("Real plugin binding failed")
            pages = {}
            for label in (
                "功能启停",
                "积分与奖励",
                "玩法设置",
                "广告位与订单",
                "收款设置",
                "异常记录",
            ):
                home = await owner.message("我的群")
                page = await owner.click(home, group.title)
                panel = await owner.click(page, label)
                pages[label] = panel.raw_text
                record("owner_page", label=label, text=panel.raw_text[:1800])
                if any(
                    s in panel.raw_text for s in ("没有权限", "操作失败", "没有此群")
                ):
                    raise RuntimeError(f"Owner page rejected: {label}")
            ordinary_home = await member.message("我的群")
            if group.title in ordinary_home.raw_text:
                raise RuntimeError("Tenant leaked into another user's home")
            owner_home = await owner.message("我的群")
            group_button = next(
                b for row in owner_home.buttons for b in row if b.text == group.title
            )
            bot = await member.call(member.client.get_input_entity, BOT)
            answer = await member.call(
                member.client,
                functions.messages.GetBotCallbackAnswerRequest(
                    peer=bot, msg_id=ordinary_home.id, data=group_button.data
                ),
            )
            await asyncio.sleep(3)
            denied = await member.read()
            record(
                "foreign_owner_callback",
                alert=answer.message,
                response=denied.raw_text[:1000],
            )
            # An unrelated transport rejection is not an authorization pass.
            if not any(
                word in (answer.message or "") + denied.raw_text
                for word in ("无效", "过期", "权限", "不属于", "失效")
            ):
                raise RuntimeError("Callback denial not conclusively verified")
            record(
                "test_group_state",
                enabled=db.execute(
                    "SELECT enabled FROM mod_groups WHERE chat=?", (str(GROUP),)
                ).fetchone()[0],
            )
            report["complete"] = True
    except errors.FloodWaitError as exc:
        record("flood_wait_stopped", seconds=exc.seconds)
    except Exception as exc:
        record("blocked", error=type(exc).__name__, detail=str(exc)[:300])
    finally:
        if granted:
            db.execute("BEGIN IMMEDIATE")
            try:
                current = db.execute(
                    "SELECT value FROM settings WHERE key='tenant_policy'"
                ).fetchone()
                if current and current[0] == encoded:
                    if old:
                        db.execute(
                            "UPDATE settings SET value=?,version=version+1 WHERE key='tenant_policy'",
                            (old[0],),
                        )
                    else:
                        db.execute("DELETE FROM settings WHERE key='tenant_policy'")
                    report["pilot_restored"] = True
                else:
                    # Preserve concurrent policy edits; remove only our added UID.
                    value = json.loads(current[0]) if current else {}
                    original = json.loads(old[0]) if old else DEFAULT
                    if str(OWNER) not in original["pilot_owners"] and current:
                        value["pilot_owners"] = [
                            uid
                            for uid in value.get("pilot_owners", [])
                            if str(uid) != str(OWNER)
                        ]
                        db.execute(
                            "UPDATE settings SET value=?,version=version+1 WHERE key='tenant_policy'",
                            (json.dumps(value),),
                        )
                    report["pilot_restored"] = "concurrent_edits_preserved"
                db.execute(
                    "UPDATE tenant_bindings SET used=1 WHERE owner=? AND used=0",
                    (str(OWNER),),
                )
                db.execute(
                    "INSERT INTO audit(at,actor,action,data) VALUES(?,?,?,?)",
                    (
                        time.time(),
                        "1000000001",
                        "tenant_acceptance_revoke",
                        json.dumps({"owner": OWNER, "group": GROUP}),
                    ),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        for client in clients:
            await client.disconnect()
        temporary.cleanup()
        db.close()
        report_path = output / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        report_path.chmod(0o600)
        print(
            json.dumps(
                {
                    "report": str(report_path),
                    "complete": report["complete"],
                    "pilot_restored": report.get("pilot_restored"),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(run())
