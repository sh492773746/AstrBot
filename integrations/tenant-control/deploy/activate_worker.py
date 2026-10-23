"""Prepare a reviewed source-only template and dedicated privileged worker."""

import grp
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path


def main():
    os.umask(0o077)
    prod = Path("/opt/astrbot-prod")
    target = Path("/opt/astrbot-runtime/tenant-source")
    if target.exists():
        raise RuntimeError("Review existing source template before reapplying")
    try:
        pwd.getpwnam("astrworker")
    except KeyError:
        subprocess.run(
            [
                "useradd",
                "--system",
                "--gid",
                "astrtenant-state",
                "--groups",
                "docker",
                "--no-create-home",
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                "astrworker",
            ],
            check=True,
        )
    uid = pwd.getpwnam("astrworker").pw_uid
    gid = grp.getgrnam("astrtenant-state").gr_gid
    source = prod / "data/plugins/astrbot_plugin_superbot"
    allowed_names = {
        "metadata.yaml",
        "_conf_schema.json",
        "requirements.txt",
        "README.md",
    }
    copied = 0
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if any(
            part in {"__pycache__", ".git", "tests", ".pytest_cache"}
            for part in relative.parts
        ):
            continue
        if path.is_symlink():
            raise ValueError("Symlink in source template")
        if not path.is_file():
            continue
        asset = relative.parts[0] == "assets" and (
            path.suffix in {".png", ".gif", ".ttc", ".ttf"}
            or path.name == "FONT-LICENSE"
        )
        if (
            not asset
            and path.suffix != ".py"
            and path.name not in allowed_names
            and not (relative.parts[0] == "docs" and path.suffix == ".md")
        ):
            continue
        dest = target / "data/plugins/astrbot_plugin_superbot" / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        copied += 1
    template = {
        "provider": [
            {
                "id": "Gemini/gemini-3.8-flash",
                "type": "openai_chat_completion",
                "enable": True,
                "key": [],
                "model": "gemini-3.8-flash",
                "api_base": "http://host.docker.internal:18734/v1",
            }
        ],
        "chat_provider_id": "Gemini/gemini-3.8-flash",
        "embedding_provider_id": "",
    }
    (target / "provider.json").write_text(json.dumps(template, indent=2))
    for path in [target, *target.rglob("*")]:
        os.chown(path, 0, gid)
        path.chmod(0o750 if path.is_dir() else 0o640)
    tenants = Path("/var/lib/astrbot-tenants")
    tenants.mkdir(mode=0o700)
    os.chown(tenants, uid, gid)
    state = Path("/var/lib/astrbot-tenant-worker")
    state.mkdir(mode=0o700)
    os.chown(state, uid, gid)
    runtime = json.loads(
        (
            prod / "data/plugin_data/astrbot_plugin_tenant_control/private/runtime.json"
        ).read_text()
    )
    private = Path("/etc/astrbot-tenant-worker")
    private.mkdir(mode=0o700)
    (private / "worker.json").write_text(
        json.dumps({"CONTROL_FERNET_KEY": runtime["CONTROL_FERNET_KEY"]})
    )
    (private / "worker.json").chmod(0o600)
    unit = Path("/etc/systemd/system/astrbot-tenant-worker.service")
    shutil.copyfile(Path(__file__).with_name(unit.name), unit)
    unit.chmod(0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "start", unit.name], check=True)
    print("Worker started with source-only Superbot files:", copied)


if __name__ == "__main__":
    main()
