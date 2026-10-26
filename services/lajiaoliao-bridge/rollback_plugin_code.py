"""Restore a protected plugin/frontend snapshot without touching account data."""

import argparse
import hashlib
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


def restore(root: Path, backup: Path, *, plugin_only: bool = False):
    """Restore code only while AstrBot is stopped.

    Args:
        root: Supported AstrBot installation.
        backup: Protected snapshot containing plugin and dist directories.
        plugin_only: Restore only plugin code, retaining the current Dashboard.

    Raises:
        RuntimeError: When safety checks or post-restore integrity checks fail.
    """
    if (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", "astrbot"], check=False
        ).returncode
        == 0
    ):
        raise RuntimeError("Stop AstrBot and its consumers before restoring code")
    if stat.S_IMODE(backup.stat().st_mode) & 0o077:
        raise RuntimeError("Backup must be private to the service administrator")
    plugin = backup / "plugin"
    if (root / "data/plugin_data/astrbot_plugin_lajiaoliao/commerce.sqlite3").exists():
        from commerce_rollback_guard import require_safe_commerce_rollback

        require_safe_commerce_rollback(root, plugin)
    binary = plugin / "bin/lajiaoliao-bridge"
    expected = binary.with_suffix(".sha256").read_text().split()[0]
    if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Backup binary checksum mismatch")
    if not plugin_only and not (backup / "dist/index.html").is_file():
        raise RuntimeError("Backup Dashboard entry point is missing")
    config = root / "data/cmd_config.json"
    config_digest = hashlib.sha256(config.read_bytes()).digest()
    swapped = []
    try:
        replacements = [(plugin, root / "data/plugins/astrbot_plugin_lajiaoliao")]
        if not plugin_only:
            replacements.append((backup / "dist", root / "dashboard/dist"))
        for source, destination in replacements:
            staging = Path(
                tempfile.mkdtemp(prefix=".lajiaoliao-code-", dir=destination.parent)
            )
            try:
                shutil.copytree(
                    source,
                    staging / "replacement",
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                destination.rename(staging / "previous")
                swapped.append((destination, staging))
                (staging / "replacement").rename(destination)
            except BaseException:
                if not any(item[1] == staging for item in swapped):
                    shutil.rmtree(staging)
                raise
        if hashlib.sha256(config.read_bytes()).digest() != config_digest:
            raise RuntimeError(
                "Config changed while restoring code; inspect before restarting"
            )
    except BaseException:
        for destination, staging in reversed(swapped):
            if destination.exists():
                destination.rename(staging / "failed-replacement")
            (staging / "previous").rename(destination)
        raise
    restored = "Plugin" if plugin_only else "Plugin and Dashboard"
    print(restored + " code restored; config, latest tokens and cursors unchanged.")
    print(
        "Core control extensions are retained for compatibility. Previous code remains in:"
    )
    for _, staging in swapped:
        print(staging)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/astrbot-prod"))
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--plugin-only", action="store_true")
    args = parser.parse_args()
    restore(args.root.resolve(), args.backup.resolve(), plugin_only=args.plugin_only)
