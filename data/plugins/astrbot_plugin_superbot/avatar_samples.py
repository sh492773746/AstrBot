"""Generate isolated acceptance samples without opening a business database."""

import argparse
from pathlib import Path

from .avatar import compose


def main():
    """Write deterministic samples outside the live user artifact directory."""
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, name in (
        ("zhou.png", "周"),
        ("qingyu.png", "青鱼"),
        ("long-name.png", "大海传媒青鱼测试"),
    ):
        path = args.output / filename
        path.write_bytes(compose(name))
        print(path)


if __name__ == "__main__":
    main()
