"""Install the private gateway and move the authoritative DB under a shared group."""

import grp
import json
import os
import pwd
import runpy
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
from filelock import FileLock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tenant_control.gateway_config import chat_from_astrbot  # noqa: E402


def run(*args):
    subprocess.run(args, check=True, timeout=120, capture_output=True)


def main():
    os.umask(0o077)
    m = runpy.run_path(str(Path(__file__).with_name("unify_controller.py")))
    prod, base, plugin = (m[key] for key in ("PROD", "BASE", "PLUGIN"))
    load, save, api = (m[key] for key in ("load", "save", "api"))
    shared = Path("/var/lib/astrbot-tenant-state")
    destination = shared / "control.db"
    if destination.exists():
        raise RuntimeError("Shared state already exists; inspect before reapplying")
    runtime = load(base / "private/runtime.json")
    original = load(prod / f"data/config/{plugin}_config.json")
    source_db = Path(runtime["CONTROL_DB_PATH"])
    source = load(prod / "data/cmd_config.json")
    route = chat_from_astrbot(source, "Gemini/gemini-3.8-flash")
    bridge = json.loads(
        subprocess.check_output(
            ["docker", "network", "inspect", "bridge"],
            text=True,
        )
    )[0]["IPAM"]["Config"][0]["Gateway"]
    if bridge != "172.17.0.1":
        raise RuntimeError("Review gateway service bind address for this Docker bridge")
    with httpx.Client(trust_env=False, timeout=1) as check:
        try:
            check.get(f"http://{bridge}:18734/health")
        except httpx.ConnectError:
            pass
        else:
            raise RuntimeError("Gateway port is already occupied")
    canonical = Path(__file__).resolve().parents[1]
    archive = canonical / "dist/astrbot_plugin_tenant_control-v0.5.1.zip"
    previous = canonical / "dist/astrbot_plugin_tenant_control-v0.5.0.zip"
    if not archive.is_file() or not previous.is_file():
        raise RuntimeError("Reviewed bundles are missing")
    if not Path("/opt/astrbot-runtime/tenant-gateway-venv/bin/python").exists():
        raise RuntimeError("Prepare gateway virtualenv before activation")
    try:
        grp.getgrnam("astrtenant-state")
    except KeyError:
        run("groupadd", "--system", "astrtenant-state")
    try:
        pwd.getpwnam("astrgateway")
    except KeyError:
        run(
            "useradd",
            "--system",
            "--gid",
            "astrtenant-state",
            "--no-create-home",
            "--home-dir",
            "/nonexistent",
            "--shell",
            "/usr/sbin/nologin",
            "astrgateway",
        )
    gid = grp.getgrnam("astrtenant-state").gr_gid
    shared.mkdir(mode=0o2770)
    os.chown(shared, 0, gid)
    shared.chmod(0o2770)
    baseline = base / "private" / f"gateway-activation-{time.time_ns()}"
    baseline.mkdir(mode=0o700)
    save(baseline / "runtime.json", runtime)
    save(baseline / "plugin.json", original)
    save(
        Path("/etc/astrbot-tenant-gateway/gateway.json"),
        {"routes": {"/v1/chat/completions": route}},
    )
    shutil.copyfile(
        Path(__file__).with_name("astrbot-tenant-gateway.service"),
        "/etc/systemd/system/astrbot-tenant-gateway.service",
    )
    Path("/etc/systemd/system/astrbot-tenant-gateway.service").chmod(0o644)
    run("systemctl", "daemon-reload")
    with m["api_client"]() as client:

        def settings(config):
            api(
                client,
                "POST",
                "/api/config/plugin/update",
                params={"plugin_name": plugin},
                json=config,
            )

        def install(path):
            with path.open("rb") as stream:
                api(
                    client,
                    "POST",
                    "/api/plugin/install-upload",
                    files={"file": (path.name, stream, "application/zip")},
                )

        try:
            settings(dict(original, enabled=False))
            with FileLock(str(source_db) + ".controller.lock", timeout=5):
                m["backup_db"](source_db, baseline / "control.db")
                m["backup_db"](source_db, destination)
            os.chown(destination, 0, gid)
            destination.chmod(0o660)
            save(
                base / "private/runtime.json",
                dict(
                    runtime,
                    CONTROL_DB_PATH=str(destination),
                    CONTROL_DB_GROUP="astrtenant-state",
                ),
            )
            install(archive)
            settings(
                dict(
                    original,
                    enabled=True,
                    default_model_quota=100,
                    allow_test_purchase=False,
                )
            )
            for _ in range(45):
                path = base / "runtime-status.json"
                if path.exists():
                    status = load(path)
                    if (
                        status.get("version") == "v0.5.1"
                        and status.get("ready")
                        and time.time() - status["at"] < 40
                    ):
                        break
                time.sleep(2)
            else:
                raise RuntimeError("Controller failed readiness after DB move")
            run("systemctl", "start", "astrbot-tenant-gateway.service")
            with httpx.Client(trust_env=False, timeout=3) as check:
                for _ in range(20):
                    try:
                        response = check.get(f"http://{bridge}:18734/health")
                        if (
                            response.status_code == 200
                            and response.json()["status"] == "ok"
                        ):
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("Gateway health check failed")
                assert (
                    check.post(
                        f"http://{bridge}:18734/v1/chat/completions", json={}
                    ).status_code
                    == 401
                )
                assert (
                    check.post(
                        f"http://{bridge}:18734/v1/chat/completions",
                        json={},
                        headers={"Authorization": "Bearer unauthorized"},
                    ).status_code
                    == 403
                )
            run("systemctl", "enable", "astrbot-tenant-gateway.service")
            with sqlite3.connect(destination) as db:
                db.execute(
                    "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                    (int(time.time()), "operator", "gateway_activated", baseline.name),
                )
            save(
                baseline / "result.json",
                {
                    "state": "complete",
                    "model": route["model"],
                    "shared_db": str(destination),
                },
            )
            print(
                "Gateway active on private bridge; controller v0.5.1 ready; authentication checks passed."
            )
        except BaseException:
            run("systemctl", "stop", "astrbot-tenant-gateway.service")
            settings(dict(original, enabled=False))
            if destination.exists():
                with FileLock(str(destination) + ".controller.lock", timeout=5):
                    m["backup_db"](destination, source_db)
            save(base / "private/runtime.json", runtime)
            install(previous)
            settings(original)
            save(baseline / "result.json", {"state": "rolled_back"})
            raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Gateway activation stopped:", type(error).__name__)
        raise SystemExit(1) from None
