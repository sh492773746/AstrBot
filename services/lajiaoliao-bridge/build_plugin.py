"""Build the staged Linux package without changing the running service."""

import argparse
import difflib
import gzip
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path

import tomllib


def normalized_member(member):
    if member.issym() or member.islnk():
        raise RuntimeError("Release archives must not contain linked resources")
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ""
    member.mode = (
        0o755 if member.isdir() or member.name.endswith("/lajiaoliao-bridge") else 0o644
    )
    return member


@contextmanager
def archive_writer(path):
    """Create timestamp-free release archives."""
    with path.open("wb") as stream:
        with gzip.GzipFile(
            fileobj=stream, mode="wb", filename="", mtime=0
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as package:
                yield package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--worker-only",
        action="store_true",
        help="Build the offline test worker without certifying a runtime package.",
    )
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    root = source.parents[1]
    target = root / "data/plugins/astrbot_plugin_lajiaoliao"
    dependencies = json.loads((target / "runtime-dependencies.json").read_text())
    if not args.worker_only:
        project = tomllib.loads((root / "pyproject.toml").read_text())
        if project["project"]["version"] != dependencies["astrbot"]:
            raise SystemExit("Runtime package requires the declared AstrBot version")
        if (
            f"{sys.version_info.major}.{sys.version_info.minor}"
            != dependencies["python"]
        ):
            raise SystemExit("Runtime package requires the declared Python version")
        for distribution, expected in dependencies["distribution_versions"].items():
            if version(distribution) != expected:
                raise SystemExit(
                    "Build environment dependency mismatch: " + distribution
                )
    if platform.system() != "Linux" and not args.worker_only:
        raise SystemExit("Linux build host required")
    binary_dir = target / "bin"
    binary_dir.mkdir(exist_ok=True)
    binary = binary_dir / "lajiaoliao-bridge"
    with tempfile.TemporaryDirectory(prefix=".build-", dir=binary_dir) as directory:
        staged = Path(directory) / "lajiaoliao-bridge"
        subprocess.run(
            [
                "go",
                "build",
                "-buildvcs=false",
                "-trimpath",
                "-ldflags=-s -w -buildid=",
                "-o",
                str(staged),
                ".",
            ],
            cwd=source,
            env={**os.environ, "CGO_ENABLED": "0"},
            check=True,
        )
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        if (
            not binary.exists()
            or hashlib.sha256(binary.read_bytes()).hexdigest() != digest
        ):
            os.replace(staged, binary)
    binary.with_suffix(".sha256").write_text(digest + "\n")
    if args.worker_only:
        print(f"Built offline test worker: {binary.name} sha256={digest}")
        return
    for destination in (target / "logo.ico", target / "pages/management/logo.ico"):
        shutil.copyfile(
            root / "dashboard/src/assets/images/platform_logos/lajiaoliao.ico",
            destination,
        )
    bundled = target / "protocol-worker"
    bundled.mkdir(exist_ok=True)
    for path in source.iterdir():
        if path.suffix == ".go" or path.name in {
            "go.mod",
            "go.sum",
            "login.html",
            "verify.html",
        }:
            shutil.copyfile(path, bundled / path.name)
    (target / "build-info.json").write_text(
        json.dumps(
            {
                "protocol_version": 2,
                "arch": platform.machine(),
                "go": subprocess.check_output(["go", "version"], text=True).strip(),
                "sha256": digest,
                "source": "services/lajiaoliao-bridge",
                "go_mod_sha256": hashlib.sha256(
                    (source / "go.mod").read_bytes()
                ).hexdigest(),
                "go_sum_sha256": hashlib.sha256(
                    (source / "go.sum").read_bytes()
                ).hexdigest(),
                "runtime_dependencies_sha256": hashlib.sha256(
                    (target / "runtime-dependencies.json").read_bytes()
                ).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    releases = source / "releases"
    releases.mkdir(exist_ok=True)
    runtime_files = [
        "metadata.yaml",
        "main.py",
        "adapter.py",
        "event.py",
        "protocol.py",
        "runtime.py",
        "storage.py",
        "reauth.py",
        "test_window.py",
        "business.py",
        "capabilities.py",
        "lottery.py",
        "assets",
        "automation.py",
        "maintenance.py",
        "media.py",
        "commands.py",
        "ai.py",
        "commerce.py",
        "metering.py",
        "COMMERCE.md",
        "AI_RUNTIME.md",
        "SAAS.md",
        "logo.ico",
        "bin",
        "pages",
        ".astrbot-plugin",
        "README.md",
        "FEATURES.md",
        "AUTOMATION.md",
        "PERFORMANCE.md",
        "RELEASE.md",
        "LOTTERY_RESULTS.md",
        "capabilities.json",
        "build-info.json",
        "runtime-dependencies.json",
    ]
    archive = releases / f"astrbot_plugin_lajiaoliao-linux-{platform.machine()}.tar.gz"
    with archive_writer(archive) as package:
        for name in runtime_files:
            package.add(
                target / name,
                arcname=f"astrbot_plugin_lajiaoliao/{name}",
                filter=normalized_member,
            )
    with archive_writer(releases / "lajiaoliao-protocol-source.tar.gz") as package:
        package.add(bundled, arcname="protocol-worker", filter=normalized_member)
        for name in (
            "build_plugin.py",
            "deploy_plugin.py",
            "plugin_stability_test.py",
            "rollback_plugin_code.py",
            "plugin_business_benchmark.py",
            "plugin_hundred_groups_benchmark.py",
            "plugin_management_smoke_test.py",
            "plugin_adapter_smoke_test.py",
            "plugin_native_probe.py",
            "native_acceptance_live.py",
            "member_acceptance_live.py",
            "rejoin_reward_acceptance.py",
            "capability_configuration_acceptance.py",
            "business_acceptance_live.py",
            "optimize_reply_config.py",
            "build_management_icons.mjs",
            "plugin_management_readonly_browser.py",
            "acceptance_client.py",
            "acceptance_routes.py",
            "inspect_recent_media.py",
            "mutual_management_acceptance.py",
            "automation_readonly_acceptance.py",
            "backup_release.py",
            "image_acceptance_live.py",
            "gif_acceptance_live.py",
            "reply_recall_acceptance_live.py",
            "lottery_result_acceptance_live.py",
            "bot_exclusion_acceptance_live.py",
            "private_bot_exclusion_acceptance.py",
            "full_feature_acceptance.py",
            "image_upload_probe.py",
            "live_stability_acceptance.py",
            "planned_business_acceptance.py",
            "performance_acceptance.py",
            "rollback_v051.py",
            "reload_release.py",
            "audit_release.py",
            "ai_release_acceptance.py",
            "commerce_rollback_guard.py",
            "commerce_browser_acceptance.py",
            "commerce_benchmark.py",
            "commerce_readonly_acceptance.py",
        ):
            package.add(
                source / name, arcname=f"tools/{name}", filter=normalized_member
            )
        package.add(
            target / "migration.py", arcname="migration.py", filter=normalized_member
        )
        package.add(
            root / "astrbot/core/platform/message_policy.py",
            arcname="core-extension/message_policy.py",
            filter=normalized_member,
        )
        package.add(
            root / "astrbot/core/platform/instance_control.py",
            arcname="core-extension/instance_control.py",
            filter=normalized_member,
        )
        for name in (
            "astrbot/core/provider/event_guard.py",
            "astrbot/core/provider/sources/gemini_source.py",
            "astrbot/builtin_stars/builtin_commands/commands/conversation.py",
            "astrbot/core/astr_main_agent.py",
            "astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py",
            "astrbot/core/pipeline/process_stage/method/agent_request.py",
        ):
            package.add(
                root / name, arcname="core-extension/" + name, filter=normalized_member
            )
    extension = subprocess.check_output(
        ["git", "diff", "--", "astrbot/core/platform/platform.py"],
        cwd=root,
        text=True,
    )
    extension += "".join(
        difflib.unified_diff(
            [],
            (root / "astrbot/core/platform/message_policy.py")
            .read_text()
            .splitlines(keepends=True),
            fromfile="/dev/null",
            tofile="b/astrbot/core/platform/message_policy.py",
        )
    )
    (releases / "core-message-policy.patch").write_text(extension)
    manifest = {
        "astrbot_version": "4.28.1",
        "plugin_version": "v0.8.3",
        "requires_matching_dashboard": True,
        "core_files": {},
        "note": "Not a drop-in plugin for unmodified official builds. Review core changes; do not overwrite unrelated deployment files.",
    }
    for relative in (
        "astrbot/core/platform/message_policy.py",
        "astrbot/core/platform/instance_control.py",
        "astrbot/dashboard/services/config_service.py",
        "astrbot/dashboard/api/bots.py",
        "astrbot/core/provider/event_guard.py",
        "astrbot/core/provider/sources/gemini_source.py",
        "astrbot/builtin_stars/builtin_commands/commands/conversation.py",
        "astrbot/core/astr_main_agent.py",
        "astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py",
        "astrbot/core/pipeline/process_stage/method/agent_request.py",
    ):
        manifest["core_files"][relative] = hashlib.sha256(
            (root / relative).read_bytes()
        ).hexdigest()
    (releases / "core-compatibility.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n"
    )
    names = [
        archive.name,
        "lajiaoliao-protocol-source.tar.gz",
        "core-message-policy.patch",
        "core-compatibility.json",
    ]
    (releases / "SHA256SUMS").write_text(
        "".join(
            hashlib.sha256((releases / name).read_bytes()).hexdigest()
            + "  "
            + name
            + "\n"
            for name in names
        )
    )
    print("Staged plugin binary and source bundle built; no service restarted.")


if __name__ == "__main__":
    main()
