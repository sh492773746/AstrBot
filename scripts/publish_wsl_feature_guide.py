"""Publish the reviewed image only; never change dashboard Access policies."""

import argparse
import base64
import hashlib
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "astrbot/builtin_stars/wangshangliao_moderation/assets"
SCRIPT = "wsl-feature-guide"


def main():
    """Upload a standalone constant-image Worker without credentials in its code."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    script = "wsl-open-guide" if args.open else SCRIPT
    config = {}
    for line in (
        Path("/root/.config/api-credentials/cloudflare.env").read_text().splitlines()
    ):
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip("\"'")
    image = (
        ASSETS / ("open-feature-guide-v1.jpg" if args.open else "feature-guide-v1.jpg")
    ).read_bytes()
    with httpx.Client(
        base_url="https://api.cloudflare.com/client/v4",
        headers={"Authorization": "Bearer " + config["CF_API_TOKEN"]},
        timeout=30,
        trust_env=False,
    ) as client:
        endpoint = f"/accounts/{config['CF_ACCOUNT_ID']}/workers/scripts/{script}"
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": "2026-10-12",
        }
        response = client.put(
            endpoint,
            files={
                "metadata": (None, json.dumps(metadata), "application/json"),
                "worker.js": (
                    "worker.js",
                    (ASSETS / "feature-guide-worker.js").read_bytes(),
                    "application/javascript+module",
                ),
                "guide-base64.txt": (
                    "guide-base64.txt",
                    base64.b64encode(image),
                    "text/plain",
                ),
            },
        )
        if not response.json().get("success"):
            raise RuntimeError("guide_worker_upload_rejected")
        response = client.post(
            endpoint + "/subdomain",
            json={"enabled": True, "previews_enabled": False},
        )
        if not response.json().get("success"):
            raise RuntimeError("guide_worker_publish_rejected")
    filename = "feature-guide-v2.jpg" if args.open else "feature-guide-v1.jpg"
    url = f"https://{script}.atlouptxlu.workers.dev/{filename}"
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        for attempt in range(5):
            response = client.get(url)
            if (
                response.status_code == 200
                and response.headers.get("content-type") == "image/jpeg"
                and hashlib.sha256(response.content).digest()
                == hashlib.sha256(image).digest()
            ):
                break
            if attempt == 4:
                raise RuntimeError("guide_image_integrity_failed")
            time.sleep(3)
    print("Published fixed feature image; public byte integrity verified.")


if __name__ == "__main__":
    main()
