"""Apply fixed-version shared-service hardening in an explicit maintenance window."""

import argparse
import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path("/opt/astrbot-runtime/python3.12.14")
BACKUP = Path("/opt/backups/wsl-public-hardening-20261014")
UNITS = ("astrbot",)


def run(*args):
    """Execute an exact administrator command without a shell or retries."""
    subprocess.run(args, check=True)


def main():
    """Back up permission metadata and live SQLite before coordinated ownership changes.

    Raises:
        RuntimeError: If the runtime, protected group state or financial data differs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.rollback:
        if os.geteuid() != 0:
            raise RuntimeError("Rollback requires root")
        record = BACKUP / "maintenance.json"
        manifest = json.loads(record.read_text())
        for unit in UNITS:
            run("systemctl", "stop", unit)
            (
                Path("/etc/systemd/system")
                / (unit + ".service.d/public-hardening.conf")
            ).unlink(missing_ok=True)
        for path, uid, gid, mode in manifest["permissions"]:
            target = Path(path)
            if target.exists() and not target.is_symlink():
                os.chown(target, uid, gid)
                target.chmod(mode)
        # New data remains intact and takes the original root service owner.
        for target in (ROOT / "data", *(ROOT / "data").rglob("*")):
            if not target.is_symlink():
                os.chown(target, 0, 0)
        (ROOT / ".venv/pyvenv.cfg").write_text(manifest["venv_config"])
        for name, destination in manifest["venv_links"].items():
            if destination is None:
                raise RuntimeError("Unknown original interpreter link")
            target = ROOT / ".venv/bin" / name
            target.unlink()
            target.symlink_to(destination)
        run("systemctl", "daemon-reload")
        for unit in UNITS:
            run("systemctl", "start", unit)
        print(
            "Service permissions and runtime rolled back; no financial database restored."
        )
        return
    if not args.apply:
        print(
            "Dry run: stop both services, preserve ledgers, relocate CPython, set fixed caps."
        )
        return
    if os.geteuid() != 0 or sys.version_info[:3] != (3, 12, 14):
        raise RuntimeError("Requires root and the existing CPython 3.12.14 runtime")
    from scripts.check_wsl_account_names import snapshot

    before = snapshot()
    for row in before["tenants"]:
        if row[1] in {"1516387", "1488006"} and row[2] and not row[3]:
            raise RuntimeError("Formal groups must remain paused")
    BACKUP.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = BACKUP / "maintenance.json"
    if record.exists():
        raise RuntimeError(
            "A maintenance attempt is already recorded; inspect before repeating"
        )
    try:
        user = pwd.getpwnam("astrbot")
    except KeyError:
        run(
            "useradd",
            "--system",
            "--user-group",
            "--home-dir",
            "/var/lib/astrbot",
            "--shell",
            "/usr/sbin/nologin",
            "astrbot",
        )
        user = pwd.getpwnam("astrbot")
    current = Path(sys.executable).resolve().parent.parent
    if not RUNTIME.exists():
        RUNTIME.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(current, RUNTIME, symlinks=True)
    original_hash = hashlib.sha256(
        (current / "bin/python3.12").read_bytes()
    ).hexdigest()
    if (
        hashlib.sha256((RUNTIME / "bin/python3.12").read_bytes()).hexdigest()
        != original_hash
    ):
        raise RuntimeError("Copied runtime differs")
    for unit in UNITS:
        run("systemctl", "stop", unit)
    try:
        for name, path in (
            ("cashback", "wangshangliao_cashback/cashback.sqlite3"),
            ("open", "wangshangliao_open/open-service.sqlite3"),
            ("settings", "wangshangliao_moderation/settings.sqlite3"),
        ):
            source = ROOT / "data/plugin_data" / path
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
                with sqlite3.connect(BACKUP / (name + "-maintenance.sqlite3")) as copy:
                    db.backup(copy)
                    if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("SQLite integrity failure")
        scopes = [
            ROOT / name for name in ("data", "astrbot", ".venv", "dashboard/dist")
        ]
        metadata = []
        for scope in scopes:
            for path in (scope, *scope.rglob("*")):
                if path.is_symlink():
                    continue
                stat = path.stat()
                metadata.append(
                    [str(path), stat.st_uid, stat.st_gid, stat.st_mode & 0o7777]
                )
        cfg = ROOT / ".venv/pyvenv.cfg"
        links = {}
        for name in ("python", "python3", "python3.12"):
            link = ROOT / ".venv/bin" / name
            links[name] = os.readlink(link) if link.is_symlink() else None
        manifest = {
            "started": time.time(),
            "before": before,
            "permissions": metadata,
            "runtime_hash": original_hash,
            "venv_config": cfg.read_text(),
            "venv_links": links,
            "status": "in_progress",
        }
        record.write_text(json.dumps(manifest))
        record.chmod(0o600)
        for unit in UNITS:
            source = Path("/etc/systemd/system") / (unit + ".service")
            shutil.copy2(source, BACKUP / source.name)
        for path, uid, gid, mode in metadata:
            target = Path(path)
            if target.is_relative_to(ROOT / "data"):
                os.chown(target, user.pw_uid, user.pw_gid)
            else:
                os.chown(target, uid, user.pw_gid)
                target.chmod(
                    mode | 0o440 | (0o050 if target.is_dir() or mode & 0o111 else 0)
                )
        home = Path("/var/lib/astrbot")
        home.mkdir(mode=0o700, exist_ok=True)
        os.chown(home, user.pw_uid, user.pw_gid)
        for name in ("cache", "tmp"):
            path = home / name
            path.mkdir(mode=0o700, exist_ok=True)
            os.chown(path, user.pw_uid, user.pw_gid)
        for name in links:
            link = ROOT / ".venv/bin" / name
            if not link.is_symlink():
                raise RuntimeError("Expected venv interpreter symlinks")
            link.unlink()
            link.symlink_to(RUNTIME / "bin/python3.12")
        lines = cfg.read_text().splitlines()
        cfg.write_text(
            "\n".join(
                "home = " + str(RUNTIME / "bin") if line.startswith("home = ") else line
                for line in lines
            )
            + "\n"
        )
        for unit, template in (
            ("astrbot", "astrbot-hardening.conf"),
        ):
            dropin = Path("/etc/systemd/system") / (unit + ".service.d")
            dropin.mkdir(exist_ok=True)
            destination = dropin / "public-hardening.conf"
            if destination.exists():
                raise RuntimeError("Existing hardening drop-in requires manual review")
            shutil.copy2(ROOT / "services" / template, destination)
        run("systemctl", "daemon-reload")
        run(
            "systemd-analyze",
            "verify",
            "astrbot.service",
        )
        run(
            "runuser",
            "-u",
            "astrbot",
            "--",
            str(ROOT / ".venv/bin/python"),
            "-c",
            "import ssl,sqlite3,aiohttp; import astrbot; print('fixed runtime imports verified')",
        )
        run("systemctl", "start", "astrbot")
        after = snapshot()
        if any(
            after[key] != before[key]
            for key in ("finance", "entries", "groups", "config", "tenants")
        ):
            raise RuntimeError("Protected state changed during maintenance")
        manifest["status"], manifest["finished"] = (
            "started_requires_acceptance",
            time.time(),
        )
        record.write_text(json.dumps(manifest))
        print(
            "Coordinated maintenance complete. Ledger snapshots and permission rollback metadata:",
            record,
        )
    except BaseException:
        print(
            "Maintenance stopped. Inspect metadata; never restore financial DB over new writes.",
            file=sys.stderr,
        )
        raise


if __name__ == "__main__":
    main()
