"""Read media schema in the designated test group; no upload, download or send."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from acceptance_client import GROUP, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    client = AcceptanceClient()
    report = {
        "beijing_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "group_id": GROUP,
        "mode": "existing_connection_read_only",
        "instances": [],
        "media": [],
    }
    for index, bot in enumerate(client.bots):
        rows = client.inspect(index, "group", GROUP)
        report["instances"].append(
            {"instance_id": bot["id"], "history_messages": len(rows)}
        )
        report["media"].extend(
            {**row, "instance_id": bot["id"]}
            for row in rows
            if row["content_type"] in {102, 103, 104, 105, 106}
        )
    atomic_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
