"""Build a single allowlisted install ZIP, excluding every private runtime file."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

FILES = (
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "adapter.py",
    "control.py",
    "customer.py",
    "account_login.py",
    "group_names.py",
    "group_control.py",
    "group_store.py",
    "user_names.py",
    "notifications.py",
    "policy.py",
    "profile_policy.py",
    "native_pipeline.py",
    "service_adapter.py",
    "tenants.py",
    "registry.py",
    "enrollment.py",
    "setup_account.py",
    "SOUL.md",
    "README.md",
    "pages/accounts/index.html",
    "pages/accounts/app.js",
    "pages/accounts/style.css",
)


def build(source, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", ZIP_DEFLATED) as archive:
        for name in FILES:
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Missing or unsafe package source")
            archive.write(path, name)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = (
        Path(__file__).resolve().parents[2] / "data/plugins/astrbot_plugin_telethon_ai"
    )
    print(build(source, args.output.resolve()))
