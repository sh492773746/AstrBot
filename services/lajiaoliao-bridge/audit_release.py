"""Read-only release checksum, architecture and sensitive-resource audit."""

import argparse
import hashlib
import json
import platform
import tarfile
from pathlib import Path

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

FORBIDDEN = {
    "token.json",
    "cursor.json",
    "session.json",
    "cmd_config.json",
    "__pycache__",
    "plugin_data",
    "accounts",
    "account",
}


def inspect_archive(path, prefix):
    with tarfile.open(path, mode="r:gz") as archive:
        rows = archive.getmembers()
        for member in rows:
            parts = Path(member.name).parts
            if (
                Path(member.name).is_absolute()
                or ".." in parts
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
                or set(parts) & FORBIDDEN
                or member.name.endswith((".sqlite3", ".sqlite3-wal", ".log", ".lock"))
            ):
                raise RuntimeError("unsafe_or_private_release_resource")
            if prefix and (not parts or parts[0] != prefix):
                raise RuntimeError("unexpected_runtime_package_root")
        return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    releases = Path(__file__).parent / "releases"
    sums = {}
    for row in (releases / "SHA256SUMS").read_text().splitlines():
        digest, name = row.split()
        if Path(name).name != name or len(digest) != 64 or name in sums:
            raise RuntimeError("invalid_checksum_manifest")
        if hashlib.sha256((releases / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("release_checksum_mismatch")
        sums[name] = digest
    runtime = "astrbot_plugin_lajiaoliao-linux-" + platform.machine() + ".tar.gz"
    required = {
        runtime,
        "lajiaoliao-protocol-source.tar.gz",
        "core-message-policy.patch",
        "core-compatibility.json",
    }
    if set(sums) != required:
        raise RuntimeError("incomplete_release_manifest")
    count = inspect_archive(releases / runtime, "astrbot_plugin_lajiaoliao")
    source_count = inspect_archive(releases / "lajiaoliao-protocol-source.tar.gz", None)
    with tarfile.open(releases / runtime, mode="r:gz") as archive:
        info = json.load(
            archive.extractfile("astrbot_plugin_lajiaoliao/build-info.json")
        )
        binary = archive.extractfile(
            "astrbot_plugin_lajiaoliao/bin/lajiaoliao-bridge"
        ).read()
        dependencies = archive.extractfile(
            "astrbot_plugin_lajiaoliao/runtime-dependencies.json"
        ).read()
        if info["protocol_version"] != 2 or info["arch"] != platform.machine():
            raise RuntimeError("unexpected_protocol_or_architecture")
        if hashlib.sha256(binary).hexdigest() != info["sha256"]:
            raise RuntimeError("packaged_binary_checksum_mismatch")
        if (
            hashlib.sha256(dependencies).hexdigest()
            != info["runtime_dependencies_sha256"]
        ):
            raise RuntimeError("packaged_dependency_checksum_mismatch")
    report = {
        "passed": True,
        "runtime_members": count,
        "source_members": source_count,
        "sha256": sums,
        "private_data_present": False,
        "limits": "Checksums detect corruption; not a cryptographic publisher signature. No archive extraction or service changes.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
