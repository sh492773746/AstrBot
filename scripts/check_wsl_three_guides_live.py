"""Opt-in two-guide replay using only the existing three native connections."""

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

import aiohttp
import httpx
import jwt

from astrbot.builtin_stars.wangshangliao_moderation.feature_image import REVIEWED_ASSETS
from astrbot.builtin_stars.wangshangliao_moderation.native_guide_upload import (
    IMAGE_LIMIT,
    ONBOARDING_GUIDES,
    REVIEWED_MEDIA_ENDPOINTS,
)
from astrbot.core.platform.sources.wangshangliao.business import Deployment
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from scripts.check_wsl_pair_live import (
    GROUP,
    ORDINARY,
    OWNER,
    PEER,
    PREFIX,
    RECEIVER,
    REGISTRATION,
    ROOT,
    SENDER,
    protected,
    read_rows,
    require,
)
from scripts.check_wsl_three_live import FIXTURE


async def main():
    """Send two fixed guides, correlate independent ingress, and close the window."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"dry_run": True, "group": GROUP, "images": 2}))
        return
    output = Path("/opt/backups") / time.strftime(
        "wsl-three-guides-%Y%m%dT%H%M%SZ", time.gmtime()
    )
    output.mkdir(mode=0o700)
    evidence = {
        "extra_logins": 0,
        "first_welcome": False,
        "phone_display_verified": False,
        "financial_writes": False,
        "images": [],
    }
    before = protected()
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    operation = "WSL_TEST_GUIDES_" + uuid.uuid4().hex
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": "Bearer " + token},
        timeout=60,
        trust_env=False,
    ) as client:

        async def post(route, body):
            response = await client.post(route, json=body)
            response.raise_for_status()
            result = response.json()
            require(result.get("status") == "ok", "Controlled guide call rejected")
            return result["data"]

        state = await post(PREFIX + "open-service", {"action": "status", "payload": {}})
        require(
            {a["account"]: (a["instance_id"], a["state"]) for a in state["accounts"]}
            == {
                OWNER: (SENDER, "online"),
                PEER: (RECEIVER, "online"),
                ORDINARY: (FIXTURE, "online"),
            },
            "Exact three existing online connections required",
        )
        for instance in (SENDER, RECEIVER, FIXTURE):
            status = await post(
                REGISTRATION, {"action": "test_status", "instance_id": instance}
            )
            require(
                not (status.get("window") or {}).get("active"),
                "An unrelated test window is active",
            )
        attempted = False
        try:
            attempted = True
            await post(
                REGISTRATION,
                {
                    "action": "test_open",
                    "instance_id": RECEIVER,
                    "sender_instance": FIXTURE,
                    "groups": [GROUP],
                    "scopes": ["group_commands"],
                    "keywords": [],
                    "seconds": 120,
                    "budget": 6,
                },
            )
            origin = await post(
                PREFIX + "open-service",
                {
                    "action": "test_probe",
                    "payload": {"operation_id": operation + "/menu", "text": "菜单"},
                },
            )
            require(origin["status"] == "accepted", "Native menu probe not accepted")
            evidence["menu"] = origin
            await asyncio.sleep(3.2)
            result = await post(
                PREFIX + "open-service",
                {"action": "test_guides", "payload": {"operation_id": operation}},
            )
            evidence["delivery"] = result
            require(
                result["status"] == "accepted"
                and set(result["receipts"]) == set(ONBOARDING_GUIDES)
                and all(
                    r and r["status"] == "accepted" for r in result["receipts"].values()
                ),
                "Native guide delivery incomplete; do not replay",
            )
            path = instance_dir(FIXTURE) / "messages.sqlite3"
            for kind, receipt in result["receipts"].items():
                found = None
                for _ in range(60):
                    rows = read_rows(
                        path,
                        "SELECT state,payload FROM inbox WHERE account=? AND team=? AND message=?",
                        (ORDINARY, GROUP, receipt["server_id"]),
                    )
                    if rows:
                        found = rows[0]
                        break
                    await asyncio.sleep(0.5)
                require(found is not None, "Guide not independently received")
                payload = json.loads(found["payload"])
                require(
                    found["state"] == "ignored_bot"
                    and payload.get("sender") == PEER
                    and payload.get("_test_probe") == operation + "/menu"
                    and payload.get("image", {}).get("digest")
                    == ONBOARDING_GUIDES[kind][1],
                    "Native image witness does not match the original test operation",
                )
                evidence["images"].append(
                    {"kind": kind, "message": receipt["server_id"], **payload["image"]}
                )
            # Verify the exact reviewed media, without uploading or storing a remote file.
            prefix = REVIEWED_MEDIA_ENDPOINTS[Deployment.load().origin.rstrip("/")][1]
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as http:
                for image in evidence["images"]:
                    require(
                        REVIEWED_ASSETS[image["digest"]] == image["asset"],
                        "Unexpected asset ID",
                    )
                    image["readback"] = []
                    for suffix in ("", ".jpg"):
                        await asyncio.sleep(3.1)
                        data = bytearray()
                        async with http.get(
                            prefix + image["asset"] + suffix,
                            allow_redirects=False,
                        ) as response:
                            require(response.status == 200, "Media readback refused")
                            async for chunk in response.content.iter_chunked(8192):
                                data.extend(chunk)
                                require(
                                    len(data) <= IMAGE_LIMIT, "Media response too large"
                                )
                        require(
                            hashlib.sha256(data).hexdigest() == image["digest"],
                            "Original or thumbnail digest mismatch",
                        )
                        image["readback"].append(
                            {
                                "thumbnail": bool(suffix),
                                "bytes": len(data),
                                "digest_verified": True,
                            }
                        )
            evidence["passed"] = True
        finally:
            if attempted:
                await post(
                    REGISTRATION, {"action": "test_close", "instance_id": RECEIVER}
                )
                evidence["window_closed"] = True
            evidence["protected_unchanged"] = protected() == before
            target = output / "evidence.json"
            target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
            target.chmod(0o600)
            print("Private evidence:", output)
            require(
                evidence["protected_unchanged"], "Protected production state changed"
            )


if __name__ == "__main__":
    asyncio.run(main())
