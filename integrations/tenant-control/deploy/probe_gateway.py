"""One real chat request using an isolated gateway DB, never fake production tenants."""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tenant_control.store import Store  # noqa: E402


def main():
    os.umask(0o077)
    unit = "astrbot-gateway-acceptance-" + secrets.token_hex(4)
    report = {"at": int(time.time()), "isolated_database": True, "checks": {}}
    with tempfile.TemporaryDirectory(
        prefix="astrbot-gateway-probe-", dir="/var/lib"
    ) as tmp:
        path = Path(tmp) / "control.db"
        store = Store(path, group="astrtenant-state")
        token = secrets.token_urlsafe(32)
        store.db.execute(
            "INSERT INTO bots(id,owner_id,username,token_cipher,expires_at,status,"
            "provider_key_hash) VALUES(1,1,'gateway_fixture',X'00',?,'active',?)",
            (int(time.time()) + 120, hashlib.sha256(token.encode()).hexdigest()),
        )
        store.db.execute(
            "INSERT INTO tenant_entitlements VALUES(1,'community-v1','[]',1)"
        )
        store.close()
        root = Path(__file__).resolve().parents[1]
        args = [
            "systemd-run",
            "--quiet",
            "--collect",
            "--unit",
            unit,
            "--property=User=astrgateway",
            "--property=Group=astrtenant-state",
            f"--property=WorkingDirectory={root}",
            "--property=LoadCredential=gateway.json:/etc/astrbot-tenant-gateway/gateway.json",
            "--property=NoNewPrivileges=true",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=true",
            f"--property=ReadWritePaths={tmp}",
            "--property=InaccessiblePaths=/opt/astrbot-prod/data /run/docker.sock",
            "--setenv=CONTROL_DB_GROUP=astrtenant-state",
            "--setenv=TENANT_GATEWAY_BIND=172.17.0.1",
            "--setenv=TENANT_GATEWAY_PORT=18735",
            "/opt/astrbot-runtime/tenant-gateway-venv/bin/python",
            "-m",
            "tenant_control",
            "--db",
            str(path),
            "gateway",
        ]
        try:
            subprocess.run(args, check=True, capture_output=True)
            with httpx.Client(
                base_url="http://172.17.0.1:18735", trust_env=False, timeout=45
            ) as client:
                for _ in range(20):
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("Isolated gateway failed to start")
                payload = {
                    "model": "unapproved-model-must-be-overridden",
                    "messages": [{"role": "user", "content": "Reply only OK."}],
                    "max_tokens": 16,
                }
                headers = {"Authorization": "Bearer " + token}
                response = client.post(
                    "/v1/chat/completions", json=payload, headers=headers
                )
                data = response.json() if response.status_code == 200 else {}
                report["checks"]["real_forward"] = bool(
                    response.status_code == 200 and data.get("choices")
                )
                report["response_model"] = data.get("model")
                report["checks"]["model_fixed"] = (
                    data.get("model") == "gemini-3.8-flash"
                )
                response = client.post(
                    "/v1/chat/completions", json=payload, headers=headers
                )
                report["checks"]["quota_exhausted"] = response.status_code == 429
                report["checks"]["unknown_key_rejected"] = (
                    client.post(
                        "/v1/chat/completions",
                        json=payload,
                        headers={"Authorization": "Bearer other-tenant"},
                    ).status_code
                    == 403
                )
                report["checks"]["embedding_not_exposed"] = (
                    client.post(
                        "/v1/embeddings",
                        json={},
                        headers=headers,
                    ).status_code
                    == 404
                )
        finally:
            subprocess.run(
                ["systemctl", "stop", unit], capture_output=True, check=False
            )
    destination = Path(__file__).resolve().parents[1] / "var/gateway-acceptance.json"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    destination.chmod(0o600)
    print(json.dumps(report))
    if not all(report["checks"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
