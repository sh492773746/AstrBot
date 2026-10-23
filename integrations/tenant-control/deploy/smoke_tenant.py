"""Offline two-tenant template check; never inserts fixtures in production DB."""

import grp
import json
import os
import pwd
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tenant_control.store import Store  # noqa: E402
from tenant_control.worker import Worker  # noqa: E402

IMAGE = "sha256:b2e2da32593e9155bcc4c561857f98b6d0f21e4c71623612e258b90d461f42ef"


def main():
    uid = pwd.getpwnam("astrworker").pw_uid
    gid = grp.getgrnam("astrtenant-state").gr_gid
    report = {}
    with tempfile.TemporaryDirectory(prefix="tenant-smoke-", dir="/var/lib") as tmp:
        root = Path(tmp)
        cipher = Fernet(Fernet.generate_key())
        store = Store(root / "isolated.db")
        os.environ["TENANT_PROVIDER_PROXY_URL"] = "http://host.docker.internal:18734/v1"
        worker = Worker(
            store,
            cipher,
            root / "tenants",
            Path("/opt/astrbot-runtime/tenant-source"),
            IMAGE,
            Path("/opt/astrbot-runtime/tenant-source/provider.json"),
        )
        configs = []
        for bot_id, owner in ((10001, 11), (10002, 22)):
            store.db.execute(
                "INSERT INTO bots(id,owner_id,username,token_cipher,expires_at) VALUES(?,?,?,?,?)",
                (
                    bot_id,
                    owner,
                    "offline_fixture",
                    cipher.encrypt(b"10001:OFFLINE_ONLY"),
                    int(time.time()) + 600,
                ),
            )
            row = store.db.execute(
                "SELECT * FROM bots WHERE id=?", (bot_id,)
            ).fetchone()
            data, _ = worker._prepare(row)
            config = json.loads((data / "cmd_config.json").read_text())
            config["platform"] = []
            (data / "cmd_config.json").write_text(json.dumps(config))
            configs.append(config)
            for path in [data, *data.rglob("*")]:
                os.chown(path, uid, gid)
            name = f"astrbot-offline-smoke-{bot_id}"
            try:
                subprocess.run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        name,
                        "--network",
                        "none",
                        "--user",
                        f"{uid}:{gid}",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges:true",
                        "-v",
                        f"{data}:/AstrBot/data:rw",
                        IMAGE,
                    ],
                    check=True,
                    capture_output=True,
                )
                time.sleep(18)
                logs = subprocess.check_output(
                    ["docker", "logs", name], stderr=subprocess.STDOUT, text=True
                )
                running = (
                    subprocess.check_output(
                        ["docker", "inspect", name, "--format", "{{.State.Running}}"],
                        text=True,
                    ).strip()
                    == "true"
                )
                report[str(bot_id)] = {
                    "running": running,
                    "superbot_loaded": "Superbot started" in logs
                    or "Plugin astrbot_plugin_superbot" in logs,
                    "profile_created": any((data / "config").glob("abconf_*.json")),
                    "no_traceback": "Traceback" not in logs,
                    "webui_disabled": "WebUI disabled" in logs,
                }
                if not all(report[str(bot_id)].values()):
                    print(
                        "\n".join(
                            line
                            for line in logs.splitlines()
                            if any(
                                term in line
                                for term in (
                                    "Traceback",
                                    "Error",
                                    "Failed to load",
                                    "line ",
                                )
                            )
                        )
                    )
                backup = worker.backup(bot_id)
                report[str(bot_id)]["backup_exists"] = (
                    backup / "cmd_config.json"
                ).is_file()
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, check=False
                )
        report["isolation"] = {
            "different_model_keys": configs[0]["provider"][0]["key"]
            != configs[1]["provider"][0]["key"],
            "different_owners": configs[0]["admins_id"] != configs[1]["admins_id"],
            "no_platform_control": all(
                "astrbot_plugin_tenant_control" not in c["plugin_set"] for c in configs
            ),
        }
        store.close()
    dest = Path(__file__).resolve().parents[1] / "var/tenant-offline-smoke.json"
    dest.write_text(json.dumps(report, indent=2))
    dest.chmod(0o600)
    print(json.dumps(report))
    if not all(all(item.values()) for item in report.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
