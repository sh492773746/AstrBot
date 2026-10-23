"""Check native Wangshangliao readiness without connecting or exposing secrets."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrbot.core.platform.sources.wangshangliao.business import (
    Deployment,  # noqa: E402
)
from astrbot.core.platform.sources.wangshangliao.storage import Vault  # noqa: E402
from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # noqa: E402


def main() -> int:
    """Print readiness counts and fixed diagnostic labels.

    Returns:
        Zero when deployment and at least one saved session are ready, else one.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", help="Check one saved bot instance")
    args = parser.parse_args()
    result = {"deployment": "unchecked", "instances": []}
    try:
        deployment = Deployment.load()
        result["deployment"] = "valid"
        result["messaging"] = (
            "configured"
            if deployment.message_key.get_secret_value()
            else "message_key_missing"
        )
    except ValueError as exc:
        result["deployment"] = str(exc)
    path = Path(get_astrbot_data_path()) / "cmd_config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        for bot in config.get("platform", []):
            if bot.get("type") != "wangshangliao" or (
                args.instance and bot.get("id") != args.instance
            ):
                continue
            state = {
                "enabled": bool(bot.get("enable")),
                "enabled_group_count": len(bot.get("enabled_groups", [])),
                "session": "missing",
            }
            try:
                saved = Vault(bot["id"]).load()
                if saved:
                    state["session"] = (
                        "bound"
                        if str(saved.get("business", {}).get("uid"))
                        == str(bot.get("account_id"))
                        else "identity_mismatch"
                    )
            except (ValueError, OSError):
                state["session"] = "invalid"
            result["instances"].append(state)
    except (ValueError, OSError, TypeError, KeyError):
        result["configuration"] = "invalid_or_missing"
    result["live_acceptance"] = "not_performed"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (
        0
        if result["deployment"] == "valid"
        and result.get("messaging") == "configured"
        and any(item["session"] == "bound" for item in result["instances"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
