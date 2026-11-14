"""Exercise SMS authentication with fake transport, never real SMS delivery."""

import ast
import asyncio
import hashlib
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data.plugins.astrbot_plugin_rebo_live.client import RemoteError


@pytest.fixture
def sms_admin(tmp_path):
    """Build the real owner action handler with isolated auth state.

    Args:
        tmp_path: Isolated directory for optional credentials.

    Returns:
        In-memory administration handler with fake upstream requests.
    """
    cls = next(
        node
        for node in ast.parse(
            Path("data/plugins/astrbot_plugin_rebo_live/main.py").read_text()
        ).body
        if isinstance(node, ast.ClassDef)
    )
    method = next(node for node in cls.body if getattr(node, "name", None) == "action")
    clock = SimpleNamespace(now=1000)
    scope = {
        "OWNER": 1,
        "time": SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now),
        "hashlib": hashlib,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<action>", "exec"), scope)
    obj = SimpleNamespace(
        lock=asyncio.Lock(),
        auth_lock=asyncio.Lock(),
        network=asyncio.Semaphore(2),
        sms_next_at=0,
        state={"rooms": {}, "grants": {}},
        status="待登录",
        client=SimpleNamespace(
            request=AsyncMock(),
            tokens={"access_token": "old", "refresh_token": "old-r"},
        ),
        clock=clock,
        save=Mock(),
        credentials={"password": "previous"},
        directory=tmp_path,
        remember=True,
        due={"r": 1},
        checked=1000,
        poll_due=2000,
        wakeup=asyncio.Event(),
    )
    obj.action = MethodType(scope["action"], obj)
    return obj


@pytest.mark.asyncio
async def test_sms_send_requires_platform_verified_human_challenge(sms_admin):
    obj = sms_admin
    obj.client.request.side_effect = [{"captcha_token": "proof-token"}, {}]
    result = await obj.action(
        {"action": "sms_send", "phone": "13900000000", "validate": "human-proof"}
    )
    assert result["retry_after"] == 60
    calls = obj.client.request.await_args_list
    assert calls[0].args == (
        "user/auth/captcha/yidun/verify",
        {"validate": "human-proof", "scene": "login"},
    )
    assert calls[1].args == (
        "user/auth/sms/send",
        {"captcha_token": "proof-token", "phone": "13900000000", "phone_region": "CN"},
    )
    assert all(call.kwargs == {"authenticated": False} for call in calls)
    assert obj.sms_next_at == 1060
    assert obj.client.tokens["access_token"] == "old"
    obj.save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RemoteError(400, business_code=1), TimeoutError()])
async def test_sms_timeout_and_rejection_do_not_retry(sms_admin, failure):
    obj = sms_admin
    obj.client.request.side_effect = [{"captcha_token": "token"}, failure]
    payload = {"action": "sms_send", "phone": "13900000000", "validate": "human-proof"}
    with pytest.raises(type(failure)):
        await obj.action(payload)
    with pytest.raises(ValueError, match="60秒"):
        await obj.action(payload)
    assert obj.client.request.await_count == 2
    obj.save.assert_not_called()


@pytest.mark.asyncio
async def test_sms_missing_proof_cannot_send(sms_admin):
    obj = sms_admin
    with pytest.raises(ValueError, match="人机验证"):
        await obj.action({"action": "sms_send", "phone": "13900000000"})
    obj.client.request.assert_not_called()
    obj.client.request.return_value = {}
    with pytest.raises(ValueError, match="未通过"):
        await obj.action(
            {"action": "sms_send", "phone": "13900000000", "validate": "human-proof"}
        )
    obj.client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_sms_success_persists_tokens_only_and_keeps_rooms_paused(sms_admin):
    obj = sms_admin
    obj.state["rooms"] = {"r": {"enabled": False, "texts": ["approved"]}}
    obj.client.request.return_value = {"access_token": "new", "refresh_token": "new-r"}
    await obj.action({"action": "sms_login", "phone": "13900000000", "code": "123456"})
    assert obj.status == "已登录" and obj.auth_due == 1300
    assert obj.client.tokens == {"access_token": "new", "refresh_token": "new-r"}
    assert obj.credentials == {} and not obj.remember
    assert obj.auto_login_used
    assert obj.due == {} and obj.checked == obj.poll_due == 0
    assert obj.state["rooms"]["r"]["enabled"] is False
    obj.save.assert_any_call("session", obj.client.tokens)
    assert all("123456" not in repr(call) for call in obj.save.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [{"access_token": "new"}, {"access_token": "", "refresh_token": "r"}, {}],
)
async def test_invalid_login_receipt_keeps_existing_auth(sms_admin, response):
    obj = sms_admin
    obj.client.request.return_value = response
    with pytest.raises(ValueError, match="缺少有效会话"):
        await obj.action(
            {"action": "sms_login", "phone": "13900000000", "code": "123456"}
        )
    assert obj.client.tokens["access_token"] == "old" and obj.remember
    assert obj.credentials == {"password": "previous"}
    obj.save.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_sms_code_keeps_existing_session(sms_admin):
    obj = sms_admin
    obj.client.request.side_effect = RemoteError(400, business_code=123)
    with pytest.raises(RemoteError):
        await obj.action(
            {"action": "sms_login", "phone": "13900000000", "code": "123456"}
        )
    assert obj.client.tokens["access_token"] == "old"
    obj.save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["sms_send", "sms_login"])
async def test_only_owner_can_request_sms_or_login(sms_admin, action):
    with pytest.raises(ValueError, match="无权"):
        await sms_admin.action({"action": action}, actor=2)
    sms_admin.client.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phone,code",
    [
        ("123", "123456"),
        ("１３９００００００００", "123456"),
        ("13900000000", "１２３４５６"),
        ("13900000000", "12345"),
        ("13900000000", 123456),
        ("13900000000", "abcdef"),
    ],
)
async def test_sms_input_validation_is_local(sms_admin, phone, code):
    with pytest.raises(ValueError):
        await sms_admin.action({"action": "sms_login", "phone": phone, "code": code})
    sms_admin.client.request.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_code_requests_send_only_once(sms_admin):
    obj = sms_admin
    obj.client.request.side_effect = [{"captcha_token": "token"}, {}]
    payload = {"action": "sms_send", "phone": "13900000000", "validate": "human-proof"}
    results = await asyncio.gather(
        obj.action(payload), obj.action(payload), return_exceptions=True
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert obj.client.request.await_count == 2
