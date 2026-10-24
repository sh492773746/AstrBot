"""Back up Wangshangliao ledgers or export latest policy for code rollback."""

import argparse
from pathlib import Path

from astrbot.builtin_stars.wangshangliao_moderation.migration import (
    backup,
    export_config,
)


def main():
    """Run an explicit backup or create a non-destructive rollback export."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "export"])
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data, output = args.data.resolve(), args.output.resolve()
    if args.action == "backup":
        print(f"Backed up {len(backup(data, output)['files'])} files to {output}")
    else:
        print(
            f"Exported {export_config(data, output)} policies to {output}; live files unchanged"
        )


if __name__ == "__main__":
    main()
