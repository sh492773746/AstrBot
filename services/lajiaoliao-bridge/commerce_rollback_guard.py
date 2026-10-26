"""Refuse a legacy downgrade that could silently remove commercial controls."""

import json
import sqlite3


def require_safe_commerce_rollback(root, plugin_backup):
    """Keep current commercial data and refuse backups lacking the license gate."""
    path = root / "data/plugin_data/astrbot_plugin_lajiaoliao/commerce.sqlite3"
    if not path.is_file():
        return
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT instance_id FROM commerce_bindings WHERE internal=0"
        ).fetchall()
        if not rows:
            return
        config = json.loads(
            (root / "data/cmd_config.json").read_text(encoding="utf-8-sig")
        )
        commercial = {row[0] for row in rows}
        if any(
            row.get("id") in commercial and row.get("enable")
            for row in config.get("platform", [])
        ):
            raise RuntimeError("Disable all commercial instances before rollback")
        if not (plugin_backup / "commerce.py").is_file():
            raise RuntimeError(
                "Legacy backup lacks commercial controls; use a reviewed gated "
                "backup or backport the gates. Never delete the commercial ledger."
            )
        if db.execute(
            "SELECT 1 FROM commerce_customers WHERE state!='paused' LIMIT 1"
        ).fetchone():
            raise RuntimeError("Pause commercial customers before rollback")
