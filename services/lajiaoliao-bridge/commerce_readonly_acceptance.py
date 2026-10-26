"""Read-only first-release checks; no orders, fee activation, SDK calls or sends."""

import argparse
import json
import time

import jwt
from acceptance_client import INSTANCES, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    client = AcceptanceClient()
    config = json.loads(client.original_config.decode("utf-8-sig"))
    status = client.api(PREFIX + "commerce-customers")
    rates = client.api(PREFIX + "commerce-rates")
    usage = client.api(PREFIX + "commerce-usage", {"action": "status"})
    assert not status["customers"]
    assert {row["instance_id"] for row in status["bindings"]} == set(INSTANCES)
    assert all(row["internal"] for row in status["bindings"])
    assert not rates["rates"]
    assert not usage["calls"] and not usage["orders"] and not usage["periods"]
    assert client.api(PREFIX + "management")["commerce_operator"]
    client.authorization = jwt.encode(
        {"username": "synthetic-nonoperator", "exp": int(time.time()) + 60},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    for endpoint in ("commerce-customers", "commerce-rates"):
        client.api(PREFIX + endpoint, expected=403)
    client.api(PREFIX + "commerce-usage", {"action": "status"}, expected=403)
    for path in (ROOT / "data/plugin_data/astrbot_plugin_lajiaoliao").glob(
        "commerce.sqlite3*"
    ):
        assert path.stat().st_mode & 0o777 == 0o600
    assert (ROOT / "data/cmd_config.json").read_bytes() == client.original_config
    report = {
        "passed": True,
        "internal_instances": 2,
        "commercial_customers": 0,
        "effective_rates": 0,
        "commercial_calls": 0,
        "commercial_orders": 0,
        "nonoperator_denied": True,
        "configuration_unchanged": True,
        "real_model_calls": 0,
        "messages_sent": 0,
        "paid_ai_enabled": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(args.output, report)
    print("Read-only commercial rollout passed; paid AI remains disabled.")


if __name__ == "__main__":
    main()
