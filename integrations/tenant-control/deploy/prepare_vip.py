"""Prepare a new, inactive VIP controller without copying production credentials."""

import json
import os
import runpy
import secrets
from pathlib import Path
from zipfile import ZipFile

from cryptography.fernet import Fernet

# Importing astrbot.core initializes its default database and config.
AUTH = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "astrbot/core/utils/auth_password.py")
)


def prepare(root: Path, archive: Path) -> None:
    """Refuse an existing configuration; never rotate keys on a repeated install."""
    if (root / "data/cmd_config.json").exists():
        raise SystemExit("Configuration already exists; refusing to overwrite it")
    os.umask(0o077)
    for directory in ("data/config", "data/plugins", "private", "control"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    config = json.loads(Path(__file__).with_name("vip-dhbot.json").read_text())
    password = AUTH["generate_dashboard_password"]()
    config["dashboard"].update(
        {
            "password": "",
            "pbkdf2_password": AUTH["hash_dashboard_password"](password),
            "password_storage_upgraded": True,
            "jwt_secret": secrets.token_urlsafe(48),
        }
    )
    plugin = {
        "enabled": False,
        "platform_id": "VIP_DHBot",
        "admin_ids": [],
        "support_text": "当前为内部安装测试，暂未开放购买。客服入口待配置。",
    }
    for relative, value in (
        ("data/cmd_config.json", config),
        ("data/config/astrbot_plugin_tenant_control_config.json", plugin),
        ("private/dashboard-login.json", {"username": "astrvip", "password": password}),
    ):
        (root / relative).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        )
    (root / "private/controller.env").write_text(
        f"CONTROL_DB_PATH={root}/control/control.db\n"
        f"CONTROL_FERNET_KEY={Fernet.generate_key().decode()}\n"
        "CONTROL_ROTATED_TOKEN_ACK=NO\n"
    )
    destination = root / "data/plugins/astrbot_plugin_tenant_control"
    with ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = Path(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe plugin archive path")
        bundle.extractall(destination)
    print(f"Prepared inactive controller: {root}")


if __name__ == "__main__":
    prepare(
        Path("/opt/astrbot-vip-dhbot"),
        Path(__file__).resolve().parents[1]
        / "dist/astrbot_plugin_tenant_control-v0.3.0.zip",
    )
