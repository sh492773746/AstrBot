from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.dashboard.services.open_api_service import (
    OpenApiService,
    OpenApiServiceError,
)


@pytest.mark.asyncio
async def test_im_at_order_and_validation():
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(id="fixture"), send_by_session=AsyncMock())
    service = object.__new__(OpenApiService)
    service.platform_manager = SimpleNamespace(platform_insts=[platform])
    service.build_message_chain_from_payload = AsyncMock(return_value=MessageChain([Plain("hello")]))
    payload = {"umo": "fixture:GroupMessage:5", "message": [
        {"type": "plain", "text": "hello"}, {"type": "at", "qq": "12", "name": "Test"}]}
    await service.send_message(payload)
    chain = platform.send_by_session.call_args.args[1].chain
    assert isinstance(chain[0], Plain)
    assert isinstance(chain[1], At)
    assert str(chain[1].qq) == "12"
    for target in (None, "", "all", 12, " 12"):
        with pytest.raises(OpenApiServiceError, match="invalid at"):
            await service.send_message({**payload, "message": [{"type": "at", "qq": target}]})
    assert platform.send_by_session.await_count == 1
