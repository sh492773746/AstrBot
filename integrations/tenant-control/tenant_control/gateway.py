"""Tenant-scoped model proxy; master provider secrets stay out of tenant volumes."""

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from .gateway_config import load_gateway_config
from .store import Store

logger = logging.getLogger(__name__)


def create_app(
    store: Store, upstreams: dict, monthly_limit: int, models: dict | None = None
) -> web.Application:
    """Build a two-route model gateway with per-bot monthly request ceilings.

    Args:
        store: Control DB, including active tenant proxy keys.
        upstreams: Mapping of API route to (HTTPS URL, upstream bearer key).
        monthly_limit: Positive maximum requests per tenant per UTC month.
        models: Server-approved model per endpoint; caller model is ignored.

    Returns:
        A local aiohttp application.
    """
    if (
        monthly_limit < 1
        or not upstreams
        or any(
            not url.startswith("https://") or not key for url, key in upstreams.values()
        )
    ):
        raise ValueError("Gateway requires HTTPS upstreams, keys and a positive quota")
    models = models or {}
    if set(models) != set(upstreams) or not all(models.values()):
        raise ValueError("Gateway model allowlist is required for every route")
    app = web.Application(client_max_size=16 * 1024)

    async def health(request):
        return web.json_response({"status": "ok"})

    async def forward(request):
        credential = request.headers.get("Authorization", "")
        if not credential.startswith("Bearer ") or len(credential) > 256:
            raise web.HTTPUnauthorized()
        digest = hashlib.sha256(credential.removeprefix("Bearer ").encode()).hexdigest()
        row = store.db.execute(
            "SELECT id FROM bots WHERE provider_key_hash=? AND enabled=1 "
            "AND status IN ('active','provisioning') AND expires_at>?",
            (digest, int(time.time())),
        ).fetchone()
        if not row:
            raise web.HTTPForbidden()
        entitlement = store.db.execute(
            "SELECT model_quota FROM tenant_entitlements WHERE bot_id=?", (row["id"],)
        ).fetchone()
        tenant_limit = entitlement["model_quota"] if entitlement else monthly_limit
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError
            if request.path.endswith("/chat/completions"):
                payload["max_tokens"] = min(int(payload.get("max_tokens", 512)), 512)
                if payload["max_tokens"] < 1 or not isinstance(
                    payload.get("messages"), list
                ):
                    raise ValueError
                payload["stream"] = False
                payload["n"] = 1
                payload.pop("max_completion_tokens", None)
            elif "input" not in payload:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            raise web.HTTPBadRequest() from None
        payload["model"] = models[request.path]
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with store.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO model_usage(bot_id,month) VALUES(?,?)",
                (row["id"], month),
            )
            count = db.execute(
                "SELECT requests FROM model_usage WHERE bot_id=? AND month=?",
                (row["id"], month),
            ).fetchone()[0]
            if count >= tenant_limit:
                raise web.HTTPTooManyRequests(text="Model quota exhausted")
            db.execute(
                "UPDATE model_usage SET requests=requests+1 WHERE bot_id=? AND month=?",
                (row["id"], month),
            )
        url, upstream_key = upstreams[request.path]
        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {upstream_key}"},
                    allow_redirects=False,
                ) as response:
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise web.HTTPBadGateway()
                    if response.status >= 300:
                        logger.warning("Model upstream returned %s", response.status)
                        raise web.HTTPBadGateway()
                    return web.Response(
                        body=bytes(body), content_type="application/json"
                    )
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("Model upstream request failed")
            raise web.HTTPBadGateway() from None

    async def heartbeat():
        while True:
            store.db.execute(
                "INSERT INTO settings(key,value) VALUES('gateway_heartbeat',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )
            await asyncio.sleep(15)

    async def lifecycle(app):
        task = asyncio.create_task(heartbeat())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            store.db.execute("DELETE FROM settings WHERE key='gateway_heartbeat'")

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/health", health)
    for route in upstreams:
        if route not in {"/v1/chat/completions", "/v1/embeddings"}:
            raise ValueError("Unsupported upstream route")
        app.router.add_post(route, forward)
    return app


def run_gateway(db_path: Path) -> None:
    """Run a dedicated bridge-reachable gateway, not a public HTTP endpoint."""
    upstreams = {
        "/v1/chat/completions": (
            os.environ.get("MODEL_CHAT_UPSTREAM_URL", ""),
            os.environ.get("MODEL_CHAT_UPSTREAM_KEY", ""),
        ),
        "/v1/embeddings": (
            os.environ.get("MODEL_EMBED_UPSTREAM_URL", ""),
            os.environ.get("MODEL_EMBED_UPSTREAM_KEY", ""),
        ),
    }
    models = {
        "/v1/chat/completions": os.getenv("MODEL_CHAT_NAME", ""),
        "/v1/embeddings": os.getenv("MODEL_EMBED_NAME", ""),
    }
    private = load_gateway_config()
    if private is not None:
        upstreams = {
            route: (item["url"], item["key"])
            for route, item in private["routes"].items()
        }
        models = {route: item["model"] for route, item in private["routes"].items()}
    bind = os.getenv("TENANT_GATEWAY_BIND", "")
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        raise ValueError("Set a private Docker bridge IP") from None
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in ipaddress.ip_network(network)
        for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise ValueError("Bind gateway only to the private Docker bridge IP")
    store = Store(db_path)
    web.run_app(
        create_app(
            store,
            upstreams,
            int(os.getenv("TENANT_MONTHLY_MODEL_REQUESTS", "1000")),
            models,
        ),
        host=bind,
        port=int(os.getenv("TENANT_GATEWAY_PORT", "18734")),
        access_log=None,
    )
