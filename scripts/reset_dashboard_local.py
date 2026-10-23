"""Reset local dashboard credentials from stdin without printing secrets."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astrbot.dashboard.password_state import set_dashboard_password_hashes  # noqa: E402


def main():
    """Back up the configuration and atomically update password hashes."""
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise SystemExit("Empty password rejected")
    path = Path("data/cmd_config.json")
    backup = Path("/opt/rebo-backups") / f"dashboard-password-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    shutil.copy2(path, backup / path.name)
    (backup / path.name).chmod(0o600)
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config["dashboard"]["username"] != "sh492773747":
        raise SystemExit("Unexpected username; no changes made")
    set_dashboard_password_hashes(config, password)
    temporary = path.with_name(".cmd_config.password-reset.json")
    with temporary.open("x", encoding="utf-8") as stream:
        temporary.chmod(0o600)
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    print("Password hashes updated; protected backup:", backup)


if __name__ == "__main__":
    main()
