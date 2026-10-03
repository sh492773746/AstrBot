"""Exercise the bound test tenant without enabling services or payments."""

import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from accept_tenant_pilot import DATABASE, GROUP, OWNER, REGISTRY, Probe
from telethon import TelegramClient


async def run():
    """Check live configuration writes and restore the exact original value."""
    report = {"steps": [], "complete": False}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output = Path("/root/Projects/agents/telethon-ai-deployment") / f"bound-{stamp}"
    output.mkdir(mode=0o700)
    db = sqlite3.connect(DATABASE, isolation_level=None, timeout=5)
    before = db.execute(
        "SELECT value FROM tenant_settings WHERE chat=? AND key='points'",
        (str(GROUP),),
    ).fetchone()
    if not before:
        raise RuntimeError("Expected bound tenant points settings")
    binding = db.execute(
        "SELECT t.owner FROM tenant_groups g JOIN tenants t ON t.id=g.tenant WHERE g.chat=?",
        (str(GROUP),),
    ).fetchone()
    if binding != (str(OWNER),):
        raise RuntimeError("Unexpected tenant ownership")
    with sqlite3.connect(output / "before.sqlite3") as target:
        db.backup(target)
    (output / "before.sqlite3").chmod(0o600)
    clients = []
    temporary = tempfile.TemporaryDirectory(prefix="bound-tenant-")
    changed = None

    def record(action, **data):
        report["steps"].append({"action": action, **data})
        print(json.dumps({"action": action, **data}, ensure_ascii=False), flush=True)

    async def page(probe, label):
        home = await probe.message("我的群")
        group_page = await probe.click(home, "test2")
        return await probe.click(group_page, label)

    try:
        registry = json.loads(REGISTRY.read_text())
        probes = {}
        for alias in ("keywords", "collector"):
            spec = registry[alias]
            path = Path(temporary.name) / f"{alias}.session"
            with (
                sqlite3.connect(
                    f"file:{spec['session']}.session?mode=ro", uri=True
                ) as src,
                sqlite3.connect(path) as dest,
            ):
                src.backup(dest)
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
        rewards = await page(owner, "积分与奖励")
        prompt = await owner.click(rewards, "设置奖励")
        record("reward_edit_prompt", text=prompt.raw_text)
        preview = await owner.message("1 0 60 0")
        record(
            "reward_preview",
            text=preview.raw_text,
            buttons=[b.text for row in preview.buttons or [] for b in row],
        )
        confirm = next(
            b.text for row in preview.buttons or [] for b in row if "确认" in b.text
        )
        changed = {
            **json.loads(before[0]),
            "enabled": True,
            "checkin": 1,
            "chat": 0,
            "interval": 60,
            "cap": 0,
            "groups": [str(GROUP)],
        }
        saved = await owner.click(preview, confirm)
        actual = db.execute(
            "SELECT value FROM tenant_settings WHERE chat=? AND key='points'",
            (str(GROUP),),
        ).fetchone()
        record("reward_saved", text=saved.raw_text, config=json.loads(actual[0]))
        if json.loads(actual[0]) != changed:
            raise RuntimeError("Reward write did not match expected configuration")
        # The disabled group cannot issue rewards during this test.
        if db.execute(
            "SELECT enabled FROM mod_groups WHERE chat=?", (str(GROUP),)
        ).fetchone() != (0,):
            raise RuntimeError("Test group service changed unexpectedly")
        home = await owner.message("我的群")
        group_page = await owner.click(home, "test2")
        group = await owner.call(owner.client.get_entity, "yunwm1")
        forwarded = await owner.call(owner.client.forward_messages, group, group_page)
        member_group = await member.call(member.client.get_entity, "yunwm1")
        shared = await member.call(
            member.client.get_messages, member_group, ids=forwarded.id
        )
        if shared.buttons:
            answer = await member.call(shared.click, 0)
            record(
                "shared_owner_button_denial",
                alert=getattr(answer, "message", None),
                message_id=shared.id,
            )
        else:
            record("shared_owner_button", status="telegram_stripped_keyboard")
        sent = await member.call(
            member.client.send_message,
            member_group,
            "/bindgroup invalid-tenant-acceptance",
            parse_mode=None,
        )
        await asyncio.sleep(4)
        replies = await member.call(
            member.client.get_messages, member_group, limit=8, min_id=sent.id
        )
        texts = [m.raw_text for m in replies if m.sender_id == 1000000005]
        record("ordinary_group_bind_denial", texts=texts)
        if not any("群主" in text or "权限" in text for text in texts):
            raise RuntimeError("Ordinary group binding denial not observed")
        for command in ("群管理", "/admin", "/bindgroup invalid-tenant-acceptance"):
            answer = await member.message(command)
            record("ordinary_private_entry", command=command, text=answer.raw_text)
        for label in (
            "转盘档位／次数／冷却",
            "快三期次与核查",
            "老虎机桌次与托管",
            "扫雷桌次与托管",
        ):
            games = await page(owner, "玩法设置")
            result = await owner.click(games, label)
            record("game_subpage", label=label, text=result.raw_text[:1500])
            if any(s in result.raw_text for s in ("没有权限", "操作失败", "无权")):
                raise RuntimeError(f"Game management denied: {label}")
        report["complete"] = True
    except Exception as exc:
        record("blocked", error=type(exc).__name__, detail=str(exc)[:250])
    finally:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT value FROM tenant_settings WHERE chat=? AND key='points'",
            (str(GROUP),),
        ).fetchone()
        if current and changed is not None and json.loads(current[0]) == changed:
            db.execute(
                "UPDATE tenant_settings SET value=?,version=version+1 WHERE chat=? AND key='points'",
                (before[0], str(GROUP)),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,data) VALUES(?,?,?,?)",
                (
                    time.time(),
                    "1000000001",
                    "tenant_acceptance_restore_rewards",
                    json.dumps({"chat": GROUP, "report": str(output)}),
                ),
            )
            report["rewards_restored"] = True
        else:
            report["rewards_restored"] = current == before
        db.execute("COMMIT")
        report["public_policy"] = db.execute(
            "SELECT value FROM settings WHERE key='tenant_policy'"
        ).fetchone()
        db.close()
        for client in clients:
            await client.disconnect()
        temporary.cleanup()
        result = output / "report.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        result.chmod(0o600)
        print(
            json.dumps(
                {
                    "report": str(result),
                    "complete": report["complete"],
                    "restored": report.get("rewards_restored"),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(run())
