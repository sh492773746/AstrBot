"""Narrow native onboarding replies without enabling moderation or proactive sends."""

import asyncio
import json
import secrets
import time
import uuid

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.database import account_guard


async def send_onboarding_text(
    adapter, target, peer, key, text, authorize, *, private=True
):
    """Send one bounded welcome or activation reply on independently verified routing.

    Args:
        adapter: Current native transport owned by the plugin.
        target: Business UID or group ID from signed directory evidence.
        peer: Independently verified NIM peer or team, never inferred from target.
        key: Durable operation key for this friend application or incoming command.
        text: Plugin-owned onboarding text, not model output.
        authorize: Frozen membership, account and configuration check.
        private: Whether this is a verified friend welcome.

    Returns:
        Native receipt status; ambiguous deliveries are never automatically replayed.

    Raises:
        ProtocolError: If routing or permission evidence has changed.
    """
    frozen = account_guard(adapter)
    account, nim, business, ledger = (
        str(adapter.account),
        adapter.nim,
        adapter.business,
        adapter.ledger,
    )
    nim_account = adapter.nim_account

    def check():
        frozen()
        authorize()
        if (
            str(adapter.account) != account
            or adapter.nim is not nim
            or adapter.business is not business
            or adapter.ledger is not ledger
            or adapter.nim_account != nim_account
            or adapter.stopping.is_set()
            or adapter.connection_state != "online"
            or not adapter.config.get("enable", True)
            or not adapter.config.get("_business_active")
            or private
            and not adapter.config.get("reply_private", True)
            or not isinstance(target, str)
            or not target.isascii()
            or not target.isdecimal()
            or not 0 < int(target) < 1 << 30
            or not isinstance(peer, str)
            or not peer.isascii()
            or not peer.isdecimal()
            or not 0 < int(peer) < (1 << 32 if private else 1 << 63)
            or not text
            or len(text.encode()) > 4096
        ):
            raise wire.ProtocolError("onboarding_scope_changed")

    check()
    # Leave room after the signed directory read, including the first-ever welcome.
    await asyncio.sleep(3)
    while True:
        check()
        previous = getattr(adapter, "_onboarding_sent_at", 0)
        await asyncio.sleep(max(0, 3 - (time.monotonic() - previous)))
        async with adapter.send_lock:
            check()
            if getattr(adapter, "_onboarding_sent_at", 0) != previous:
                continue
            nonce, state = await ledger.reserve(
                key, json.dumps([account, target, peer, private, text])
            )
            if state != "new":
                return "unknown" if state == "sending" else state
            client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{account}/{key}"))
            try:
                check()
                inner = wire.ApplicationMessage(
                    sender={"id": int(account)},
                    target={"id": int(target)},
                    created_at=int(time.time() * 1000),
                    device=1,
                    session=1 if private else 2,
                    version=2,
                    format=0,
                    client_id=client_id,
                    content={"data": text},
                )
                attachment = wire.seal_message(
                    business.deployment.key("message_key"),
                    inner,
                    int(nonce),
                    secrets.randbits(64) or 1,
                    int(time.time()),
                )
                properties = wire.properties(
                    [
                        (0, b"0" if private else b"1"),
                        (1, peer.encode()),
                        (8, b"100"),
                        (10, attachment.encode()),
                        (11, client_id.encode()),
                    ]
                )
            except BaseException:
                await ledger.finish(key, "rejected")
                raise
            adapter._onboarding_sent_at = time.monotonic()
            try:
                code, body = await nim.request(
                    7 if private else 8,
                    1 if private else 2,
                    properties,
                )
                if code != 200:
                    await ledger.finish(key, "rejected")
                    return "rejected"
                receipt, end = wire.read_properties(body)
                if (
                    end != len(body)
                    or not receipt.get(12)
                    or not receipt.get(7, b"").isdigit()
                    or receipt.get(11, client_id.encode()) != client_id.encode()
                ):
                    raise wire.ProtocolError("receipt_shape")
                await ledger.finish(key, "accepted", receipt[12].decode())
                return "accepted"
            except BaseException:
                await ledger.finish(key, "unknown")
                raise
