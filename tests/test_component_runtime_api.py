"""Dashboard-only runtime routes reject credentials delegated to plugins."""

import httpx
import pytest
from fastapi import FastAPI

from astrbot.dashboard.api import plugins
from astrbot.dashboard.responses import ApiError
from tests.test_fastapi_v1_dashboard import (
    JWT_SECRET,
    _jwt_headers,
)


@pytest.mark.asyncio
async def test_runtime_routes_require_dashboard_auth_and_strict_scope():
    app = FastAPI()
    app.state.jwt_secret = JWT_SECRET
    app.include_router(plugins.router, prefix="/api/v1/plugins")

    @app.exception_handler(ApiError)
    async def rejected(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"status": "error", "message": exc.message}, exc.status_code
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://runtime.test",
    ) as client:
        for path in ("catalog", "status"):
            response = await client.get("/api/v1/plugins/runtime/" + path)
            assert response.status_code == 401
            delegated = await client.get(
                "/api/v1/plugins/runtime/" + path,
                headers={"X-API-Key": "plugin-delegation"},
            )
            assert delegated.status_code == 401
            response = await client.get(
                "/api/v1/plugins/runtime/" + path, headers=_jwt_headers()
            )
            assert response.status_code == 200
            assert "handler" not in response.text
        response = await client.post(
            "/api/v1/plugins/runtime/recover",
            headers=_jwt_headers(),
            json={
                "capability": "missing",
                "scope": ["a"],
                "version": 0,
                "operation": "reset_after_review",
                "reason": "checked",
            },
        )
        assert response.status_code == 409
        response = await client.post(
            "/api/v1/plugins/runtime/recover",
            headers=_jwt_headers(),
            json={
                "capability": "missing",
                "scope": ["a"],
                "version": 0,
                "operation": "reset_after_review",
                "reason": "checked",
                "function": "arbitrary",
            },
        )
        assert response.status_code == 422
