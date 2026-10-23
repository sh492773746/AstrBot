"""Authenticated read failures preserve useful, payload-free diagnostics."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import dashboard
from astrbot.builtin_stars.wangshangliao_moderation.service_settings import (
    SupportConfiguration,
)
from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "number", "status", "message"),
    [
        ("member_identity", 0, 502, "账号资料不完整"),
        ("members_shape", 0, 502, "资料格式暂不兼容"),
        ("member_pagination_incomplete", 0, 502, "完整成员名单"),
        ("body_limit", 0, 502, "安全读取上限"),
        ("response_limit", 0, 502, "安全读取上限"),
        ("secondary_security_code_required", 1400, 502, "二级安全验证"),
        ("secondary_security_unverified", 1402, 502, "二级安全验证"),
        ("reauth_required", 401, 502, "登录已失效"),
        ("rate_limited", 429, 429, "暂缓请求"),
        ("transport", 0, 502, "连接中断"),
        ("business_rejected", 403, 502, "未接受本次请求"),
        ("private-provider-body-with-secret", 0, 502, "资料未能核对"),
    ],
)
async def test_member_reads_never_report_an_online_service_as_disconnected(
    page_app, headers, monkeypatch, code, number, status, message
):
    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    dispatch = AsyncMock(side_effect=BusinessError(code, number))
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            integrated=True,
            active=True,
            adapters=lambda: [adapter],
            rpc=SimpleNamespace(lock=asyncio.Lock(), dispatch=dispatch),
        )
    )
    emit = Mock()
    monkeypatch.setattr(dashboard.Diagnostics, "emit", emit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            PREFIX + "/open-service",
            headers=headers,
            json={
                "action": "tenant_members",
                "payload": {"account": "1", "group_id": "5"},
            },
        )
    assert response.status_code == status
    result = response.json()
    assert message in result["message"]
    assert "服务未连接" not in result["message"]
    expected = code if code in dashboard.ERROR_LABELS else "operation_failed"
    assert result["data"] == {"error": expected, "provider_code": number}
    assert code != "private-provider-body-with-secret" or code not in response.text
    assert emit.call_args.kwargs["error"] == expected
    assert emit.call_args.kwargs["provider_code"] == number
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_typed_business_validation_is_400_without_echoing_fields(
    page_app, headers
):
    app, page, _, _, _ = page_app

    async def invalid(_):
        SupportConfiguration.model_validate({"provider_id": "private-invalid-field"})

    dispatch = AsyncMock(side_effect=invalid)
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            integrated=True,
            active=True,
            rpc=SimpleNamespace(lock=asyncio.Lock(), dispatch=dispatch),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            PREFIX + "/open-service",
            headers=headers,
            json={"action": "support_settings", "payload": {"account": "1"}},
        )
    assert response.status_code == 400
    assert "参数格式" in response.json()["message"]
    assert "private-invalid-field" not in response.text
