"""Create a private, consistent backup without rolling credentials backward."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path


def main():
    """Back up plugin code and account databases.

    Returns:
        None. Prints only the backup path, never account credentials.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/astrbot-prod"))
    parser.add_argument("--restore-code", type=Path)
    parser.add_argument(
        "--destination", type=Path, default=Path("/opt/lajiaoliao-migration-backups")
    )
    args = parser.parse_args()
    os.umask(0o077)
    if args.restore_code:
        config = json.loads(
            (args.root / "data/cmd_config.json").read_text(encoding="utf-8-sig")
        )
        if any(
            row.get("type") == "lajiaoliao" and row.get("enable")
            for row in config["platform"]
        ):
            raise RuntimeError("Disable only LajiaoLiao instances before code rollback")
        binary = str(
            args.root / "data/plugins/astrbot_plugin_lajiaoliao/bin/lajiaoliao-bridge"
        )
        for process in Path("/proc").glob("[0-9]*"):
            try:
                if os.readlink(process / "exe").startswith(binary):
                    raise RuntimeError("LajiaoLiao consumer still running")
            except (FileNotFoundError, PermissionError):
                continue
        backup = args.restore_code.resolve()
        from commerce_rollback_guard import require_safe_commerce_rollback

        require_safe_commerce_rollback(args.root, backup / "plugin")
        if (
            not (backup / "manifest.json").is_file()
            or not (backup / "plugin/metadata.yaml").is_file()
        ):
            raise RuntimeError("Invalid code backup")
        for path in (backup, *(backup / "plugin").rglob("*")):
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise RuntimeError(
                    "Code backup must be private and contain no symlinks"
                )
        source_binary = backup / "plugin/bin/lajiaoliao-bridge"
        expected = source_binary.with_suffix(".sha256").read_text().split()[0]
        if hashlib.sha256(source_binary.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Backup binary checksum mismatch")
        shutil.copytree(
            backup / "plugin",
            args.root / "data/plugins/astrbot_plugin_lajiaoliao",
            dirs_exist_ok=True,
        )
        print(
            "Code restored; current tokens, cursors, configuration and business ledgers were not replaced. Reload the plugin before enabling LajiaoLiao instances."
        )
        return
    args.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = Path(tempfile.mkdtemp(prefix="image-automation-", dir=args.destination))
    plugin = args.root / "data/plugins/astrbot_plugin_lajiaoliao"
    shutil.copy2(args.root / "data/cmd_config.json", target / "cmd_config.json")
    shutil.copytree(
        plugin, target / "plugin", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        args.root / "services/lajiaoliao-bridge",
        target / "protocol",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    source = args.root / "data/plugin_data/astrbot_plugin_lajiaoliao"
    data = target / "data"
    data.mkdir(mode=0o700)
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_symlink() or path.name.endswith(("-wal", "-shm", ".lock")):
            continue
        destination = data / relative
        if path.is_dir():
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif path.suffix == ".sqlite3":
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as old:
                with sqlite3.connect(destination) as new:
                    old.backup(new)
        else:
            shutil.copy2(path, destination)
    for path in target.rglob("*"):
        path.chmod(
            0o700 if path.is_dir() or path.name == "lajiaoliao-bridge" else 0o600
        )
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "created": time.time(),
                "supported_astrbot": "4.28.1",
                "rollback": "Stop new consumers; restore code only after reconciling current credentials and cursors. Never blindly restore token snapshots.",
            },
            indent=2,
        )
    )
    print(target)


if __name__ == "__main__":
    main()
