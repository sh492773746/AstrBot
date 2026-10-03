"""Validated runtime inputs; no secret values belong in the plugin schema."""

import json
import os
from pathlib import Path

TEMPLATE = "community-v1"
FEATURES = {"community"}


def credentials(root: Path) -> dict:
    path = root / "data/plugin_data/astrbot_plugin_tenant_control/private/runtime.json"
    names = ("CONTROL_DB_PATH", "CONTROL_FERNET_KEY", "CONTROL_ROTATED_TOKEN_ACK")
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError(
                "Controller runtime credentials must be a private regular file"
            )
        value = json.loads(path.read_text())
        if any(
            os.getenv(name) and os.getenv(name) != value.get(name) for name in names
        ):
            raise ValueError("Conflicting controller credential sources")
    else:
        value = {name: os.getenv(name, "") for name in names}
    if value.get("CONTROL_ROTATED_TOKEN_ACK") != "YES":
        raise ValueError("Rotate the AstrBot control platform token before activation")
    if not Path(value.get("CONTROL_DB_PATH") or ".").is_absolute():
        raise ValueError("Set an absolute CONTROL_DB_PATH shared with the worker")
    if not value.get("CONTROL_FERNET_KEY"):
        raise ValueError("Set CONTROL_FERNET_KEY outside the plugin configuration")
    return value


def tenant_defaults(config: dict) -> dict:
    template = config.get("tenant_template_version", TEMPLATE)
    features = config.get("default_features", ["community"])
    quota = config.get("default_model_quota", 1000)
    if template != TEMPLATE:
        raise ValueError("Unsupported tenant template version")
    if not isinstance(features, list) or set(features) - FEATURES:
        raise ValueError("Unsupported default tenant features")
    if type(quota) is not int or not 0 <= quota <= 1_000_000:
        raise ValueError("Default model quota must be an integer between 0 and 1000000")
    return {
        "template": template,
        "features": sorted(set(features)),
        "model_quota": quota,
    }
