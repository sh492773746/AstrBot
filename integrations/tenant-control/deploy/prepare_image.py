"""Export a credential-free allowlisted build context outside production data."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def prepare(source: Path, target: Path):
    source = source.resolve()
    if target.exists():
        raise ValueError("Use a new build directory")
    files = (
        subprocess.check_output(
            [
                "git",
                "-c",
                f"safe.directory={source}",
                "ls-files",
                "-z",
                "--",
                "astrbot",
            ],
            cwd=source,
        )
        .decode()
        .split("\0")
    )
    files = [name for name in files if name]
    files += [
        "main.py",
        "runtime_bootstrap.py",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "README.md",
    ]
    manifest = {}
    for name in files:
        path = source / name
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(source)
        ):
            raise ValueError("Build source must contain regular files only")
        if any(
            part in {"data", ".git", ".venv", "__pycache__"}
            for part in Path(name).parts
        ):
            raise ValueError("Runtime data in build allowlist")
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        manifest[name] = hashlib.sha256(dest.read_bytes()).hexdigest()
    # Export a locked dependency set without copying the host virtualenv or pip config.
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str((target / "requirements.lock").resolve()),
        ],
        cwd=target,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    shutil.copyfile(
        Path(__file__).with_name("Tenant.Dockerfile"), target / "Dockerfile"
    )
    (target / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("--source", type=Path, default=Path("/opt/astrbot-prod"))
    args = parser.parse_args()
    print("Source files exported:", prepare(args.source, args.target))
