"""Reset dashboard hashes through the authenticated local configuration API."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

import httpx
import jwt

from astrbot.core.utils.auth_password import validate_dashboard_password
from astrbot.dashboard.password_state import set_dashboard_password_hashes


def main():
    """Read the requested secret from stdin, reset and test without logging it."""
    os.umask(0o077)
    password = sys.stdin.readline().rstrip("\r\n")
    validate_dashboard_password(password)
    path = Path("/opt/astrbot-prod/data/cmd_config.json")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    backup = Path("/opt/rebo-backups") / f"dashboard-password-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    shutil.copy2(path, backup / "cmd_config.json")
    (backup / "cmd_config.json").chmod(0o600)
    dashboard = config["dashboard"]
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 120},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    set_dashboard_password_hashes(config, password)
    with httpx.Client(base_url="http://127.0.0.1:6185", timeout=60) as client:
        response = client.post(
            "/api/config/astrbot/update",
            headers={"Authorization": "Bearer " + token},
            json={"conf_id": "default", "config": config},
        )
        assert (
            response.status_code == 200 and response.json().get("status") != "error"
        ), "Configuration update rejected"
        response = client.post(
            "/api/auth/login",
            json={
                "username": dashboard["username"],
                "password": password,
            },
        )
        result = response.json()
        print("Login HTTP status:", response.status_code)
        print("Login result:", result.get("status"))
        print("Username:", dashboard["username"])
        if result.get("status") == "error":
            print("Login requires further verification; no secret or token printed.")
        else:
            print("Password reset and local login verified. No restart performed.")


if __name__ == "__main__":
    main()
