"""Grant only the shared AstrBot UID access to the existing Telethon session."""

import argparse
import base64
import ctypes
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("/opt/telegram-chat-collector-prod/var")
BACKUP = Path(
    "/opt/backups/wsl-public-hardening-20261014/telegram-session-permissions.json"
)
DROPIN = Path("/etc/systemd/system/astrbot.service.d/telegram-session.conf")


def main():
    """Update named-user ACLs without changing world permissions or credentials.

    Raises:
        OSError: If ACL protection cannot be installed.
        RuntimeError: If a prior attempt must be reviewed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--extend", action="store_true")
    args = parser.parse_args()
    assert os.geteuid() == 0
    if args.rollback:
        for row in json.loads(BACKUP.read_text()):
            path = Path(row["path"])
            for attribute, value in row["acl"].items():
                if value is None:
                    try:
                        os.removexattr(path, attribute)
                    except OSError:
                        pass
                else:
                    os.setxattr(path, attribute, base64.b64decode(value))
            path.chmod(row["mode"])
        DROPIN.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        return
    if not args.apply:
        print(
            "Dry run: grant named astrbot UID access to the existing session, not public access."
        )
        return
    if BACKUP.exists() and not args.extend:
        raise RuntimeError("Existing ACL backup requires review before another apply")
    uid = pwd.getpwnam("astrbot").pw_uid
    paths = [
        DIRECTORY,
        *(
            DIRECTORY / name
            for name in (
                "account.session",
                "account.session-journal",
                "account.session-wal",
                "account.session-shm",
                "account.session.unified.lock",
                "collector.lock",
            )
            if (DIRECTORY / name).exists()
        ),
    ]
    records = json.loads(BACKUP.read_text()) if BACKUP.exists() else []
    recorded = {row["path"] for row in records}
    for path in paths:
        attributes = {}
        for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
            try:
                attributes[attribute] = base64.b64encode(
                    os.getxattr(path, attribute)
                ).decode()
            except OSError:
                attributes[attribute] = None
        if str(path) not in recorded:
            records.append(
                {
                    "path": str(path),
                    "mode": path.stat().st_mode & 0o7777,
                    "acl": attributes,
                }
            )
    BACKUP.write_text(json.dumps(records))
    BACKUP.chmod(0o600)
    library = ctypes.CDLL("libacl.so.1", use_errno=True)
    library.acl_from_text.argtypes, library.acl_from_text.restype = (
        [ctypes.c_char_p],
        ctypes.c_void_p,
    )
    library.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    library.acl_free.argtypes = [ctypes.c_void_p]
    for path in paths:
        directory = path.is_dir()
        permissions = "rwx" if directory else "rw-"
        text = f"u::{permissions},u:{uid}:{permissions},g::---,m::{permissions},o::---"
        acl = library.acl_from_text(text.encode())
        if not acl:
            raise OSError(ctypes.get_errno(), "ACL parsing failed")
        try:
            for kind in (0x8000, 0x4000) if directory else (0x8000,):
                if library.acl_set_file(os.fsencode(path), kind, acl) != 0:
                    raise OSError(ctypes.get_errno(), "ACL update failed", str(path))
        finally:
            library.acl_free(acl)
    shutil.copy2(ROOT / "services/astrbot-telegram-session.conf", DROPIN)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print(
        "Named-user ACL installed; existing root collector remains compatible. Restart AstrBot after test cleanup."
    )


if __name__ == "__main__":
    main()
