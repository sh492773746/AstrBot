"""Exercise test-group settings through real owner buttons and restore values."""

import argparse
import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from accept_tenant_pilot import DATABASE, GROUP, REGISTRY, Probe
from telethon import TelegramClient


async def run(preconfigure_only=False):
    """Change only test-group settings; never place orders or configure a wallet."""
    report = {"steps": [], "complete": False}
    directory = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "settings-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(DATABASE, isolation_level=None, timeout=5)
    db.row_factory = sqlite3.Row
    tables = (
        "mod_groups",
        "tenant_settings",
        "wheel_groups",
        "k3_groups",
        "slots_groups",
        "mines_groups",
        "duel_groups",
    )
    before = {
        table: [
            dict(row)
            for row in db.execute(f"SELECT * FROM {table} WHERE chat=?", (str(GROUP),))
        ]
        for table in tables
    }
    if before["mod_groups"][0]["enabled"]:
        raise RuntimeError("Test group must initially be disabled")
    if any(
        db.execute(
            f"SELECT 1 FROM {table} WHERE chat=? AND status IN ({states})",
            (str(GROUP),),
        ).fetchone()
        for table, states in (
            ("slots_tables", "'open','locked'"),
            ("mines_tables", "'open','playing'"),
            ("k3_rounds", "'open','drawing','review'"),
        )
    ):
        raise RuntimeError("Test group has in-flight business")
    original_gate = db.execute(
        "SELECT value FROM settings WHERE key='games_v3_groups'"
    ).fetchone()
    with sqlite3.connect(directory / "before.sqlite3") as target:
        db.backup(target)
    (directory / "before.sqlite3").chmod(0o600)
    temp = tempfile.TemporaryDirectory(prefix="settings-pilot-")
    spec = json.loads(REGISTRY.read_text())["keywords"]
    path = Path(temp.name) / "owner.session"
    with (
        sqlite3.connect(f"file:{spec['session']}.session?mode=ro", uri=True) as source,
        sqlite3.connect(path) as target,
    ):
        source.backup(target)
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
    probe = Probe(client)

    def record(action, **fields):
        item = {"action": action, **fields}
        report["steps"].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    async def page(label):
        home = await probe.message("我的群")
        group = await probe.click(home, "test2")
        return await probe.click(group, label)

    async def switch(label, enabled):
        panel = await page("功能启停")
        preview = await probe.click(panel, ("开启" if enabled else "关闭") + label)
        saved = await probe.click(preview, "确认")
        expected = label + "：本群" + ("开" if enabled else "关")
        if expected not in saved.raw_text:
            raise RuntimeError(f"Switch result unexpected: {saved.raw_text[:200]}")
        record("switch", feature=label, enabled=enabled, display_verified=True)

    try:
        await client.connect()
        me = await probe.call(client.get_me)
        if me.id != 1000000003:
            raise RuntimeError("Unexpected owner")
        if not preconfigure_only:
            await switch("本群服务", True)
            for label in (
                "广告投放",
                "加拿大28",
                "积分快三",
                "双人对赌",
                "积分转盘",
                "老虎机PvP",
                "扫雷接龙",
            ):
                await switch(label, True)
                await switch(label, False)
        for label, value, key, expected in (
            (
                "加拿大限额",
                "1 100 200",
                "canada_limits",
                {"minimum": 1, "maximum": 100, "total": 200},
            ),
            (
                "快三限额",
                "500 50 10 1000",
                "k3_limits",
                {"ordinary": 500, "special": 50, "number": 10, "total": 1000},
            ),
            ("老虎机开桌档位", "100 300", "slots_stakes", [100, 300]),
            ("扫雷加入档位", "100 300", "mines_stakes", [100, 300]),
        ):
            games = await page("玩法设置")
            await probe.click(games, label)
            preview = await probe.message(value)
            saved = await probe.click(preview, "确认保存")
            rows = db.execute(
                "SELECT key,value FROM tenant_settings WHERE chat=? AND key LIKE ?",
                (str(GROUP), key + "%"),
            ).fetchall()
            if not any(json.loads(row["value"]) == expected for row in rows):
                raise RuntimeError(f"Not persisted: {label}; {saved.raw_text[:200]}")
            record("operational_saved", label=label, value=expected)
        for label, value, key, expected in (
            ("投入档位", "50 100", "stakes", [50, 100]),
            ("每日次数", "7", "limit", 7),
            ("冷却秒数", "6", "cooldown", 6),
        ):
            games = await page("玩法设置")
            wheel = await probe.click(games, "转盘档位／次数／冷却")
            await probe.click(wheel, label)
            preview = await probe.message(value)
            await probe.click(preview, "确认保存")
            row = db.execute(
                "SELECT config FROM wheel_groups WHERE chat=?", (str(GROUP),)
            ).fetchone()
            if not row or json.loads(row[0])[key] != expected:
                raise RuntimeError(f"Wheel not persisted: {label}")
            record("wheel_saved", field=key, value=expected)
        for label in ("快三期次与核查", "老虎机桌次与托管", "扫雷桌次与托管"):
            games = await page("玩法设置")
            result = await probe.click(games, label)
            record("subpage", label=label, text=result.raw_text[:1200])
            if any(s in result.raw_text for s in ("无权限", "此群未启用")):
                raise RuntimeError("Game subpage rejected")
            if label in ("老虎机桌次与托管", "扫雷桌次与托管"):
                if (
                    "100 / 300" not in result.raw_text
                    or "100 / 300 / 800" in result.raw_text
                ):
                    raise RuntimeError("Management stakes do not match configuration")
        if preconfigure_only:
            assert (
                db.execute(
                    "SELECT enabled FROM mod_groups WHERE chat=?", (str(GROUP),)
                ).fetchone()[0]
                == 0
            )
            record("preconfigured_while_disabled", verified=True)
        else:
            await switch("本群服务", False)
        report["complete"] = True
    except Exception as exc:
        record("stopped", error=type(exc).__name__, detail=str(exc)[:400])
    finally:
        # Restore only this dedicated group's configuration, never business rows.
        db.execute("BEGIN IMMEDIATE")
        try:
            for table in tables:
                db.execute(f"DELETE FROM {table} WHERE chat=?", (str(GROUP),))
                for row in before[table]:
                    columns = ",".join(row)
                    marks = ",".join("?" for _ in row)
                    db.execute(
                        f"INSERT INTO {table}({columns}) VALUES({marks})",
                        tuple(row.values()),
                    )
            current = db.execute(
                "SELECT value FROM settings WHERE key='games_v3_groups'"
            ).fetchone()
            if current and str(GROUP) not in (
                json.loads(original_gate[0]) if original_gate else []
            ):
                values = [x for x in json.loads(current[0]) if x != str(GROUP)]
                db.execute(
                    "UPDATE settings SET value=?,version=version+1 WHERE key='games_v3_groups'",
                    (json.dumps(values),),
                )
            db.execute(
                "DELETE FROM dialogs WHERE uid=?",
                ("1000000003",),
            )
            # Invalidate test-era buttons so restored versions cannot accept replay.
            db.execute(
                "UPDATE callbacks SET used=1 WHERE uid=? AND json_extract(payload,'$.chat')=?",
                ("1000000003", str(GROUP)),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,data) VALUES(?,?,?,?)",
                (
                    time.time(),
                    "1000000001",
                    "tenant_settings_acceptance_restore",
                    json.dumps({"group": GROUP, "report": str(directory)}),
                ),
            )
            db.execute("COMMIT")
            report["restored"] = True
        except BaseException:
            db.execute("ROLLBACK")
            raise
        await client.disconnect()
        temp.cleanup()
        db.close()
        output = directory / "report.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        output.chmod(0o600)
        print(
            json.dumps(
                {
                    "report": str(output),
                    "complete": report["complete"],
                    "restored": report.get("restored"),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--preconfigure-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.preconfigure_only))
