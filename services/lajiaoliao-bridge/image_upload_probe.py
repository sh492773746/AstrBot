"""Inspect an authorized storage contract without sending any chat message."""

import argparse
import hashlib
import io
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from acceptance_client import AcceptanceClient
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    client = AcceptanceClient()
    ref = client.bots[0]["session_ref"]
    token = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "data/plugin_data/astrbot_plugin_lajiaoliao"
            / ref
            / "token.json"
        ).read_text()
    )
    stream = io.BytesIO()
    Image.new("RGB", (192, 96), "white").save(stream, format="PNG")
    raw = stream.getvalue()
    digest = hashlib.md5(hashlib.md5(raw).hexdigest().encode()).hexdigest()
    request = urllib.request.Request(
        "https://im-api.lajiaoliao.com/object/initiate_multipart_upload",
        data=json.dumps(
            {
                "hash": digest,
                "size": len(raw),
                "partSize": 1048576,
                "maxParts": -1,
                "name": token["user_id"] + "/" + secrets.token_hex(16),
                "contentType": "image/png",
                "cause": "image",
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "token": token["im_token"],
            "operationID": "image-storage-contract",
            "platform": "5",
            "deviceID": "astrbot-lajiaoliao-bridge",
            "version": "1.0.3",
        },
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None

    http = urllib.request.build_opener(NoRedirect())
    try:
        with http.open(request, timeout=20) as response:
            status, result = response.status, json.load(response)
    except urllib.error.HTTPError as error:
        status, result = error.code, {}
    data = result.get("data") or {}
    upload = data.get("upload") or {}
    sign = upload.get("sign") or {}
    parts = sign.get("parts") or []
    report = {
        "http_status": status,
        "errCode": result.get("errCode"),
        "data_keys": sorted(data),
        "upload_keys": sorted(upload),
        "part_size": upload.get("partSize"),
        "parts": len(parts),
        "storage_hosts": sorted(
            {
                urllib.parse.urlsplit(item).hostname
                for item in [
                    data.get("url", ""),
                    sign.get("url", ""),
                    *(part.get("url", "") for part in parts),
                ]
                if item
            }
        ),
        "signed_header_names": [item.get("key") for item in (sign.get("header") or [])],
        "signed_query_names": [item.get("key") for item in (sign.get("query") or [])],
        "sent_messages": 0,
    }
    from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

    atomic_json(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
